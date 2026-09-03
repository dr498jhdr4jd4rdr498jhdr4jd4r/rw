from contextlib import asynccontextmanager
from urllib.parse import unquote
import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

# Headers that trigger Cloudflare WAF when forwarded from a proxy
FORBIDDEN_HEADERS = {
    "host",
    "connection",
    "content-length",
    "cf-ray",
    "cf-connecting-ip",
    "cf-ipcountry",
    "cf-visitor",
    "cf-worker",
    "cdn-loop",
    "x-forwarded-for",
    "x-forwarded-proto",
    "x-forwarded-host",
    "x-real-ip"
}

# Reusable high-performance HTTP client
http_client: httpx.AsyncClient = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(45.0, connect=15.0),
        limits=httpx.Limits(max_connections=150, max_keepalive_connections=30)
    )
    yield
    await http_client.aclose()

app = FastAPI(title="Railway Private CORS Proxy", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.api_route("/proxy", methods=["GET", "POST", "OPTIONS", "HEAD"])
async def proxy_request(url: str, request: Request):
    if request.method == "OPTIONS":
        return Response(
            status_code=204,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS, HEAD",
                "Access-Control-Allow-Headers": "Range, Content-Type, Authorization",
                "Access-Control-Max-Age": "86400"
            }
        )

    target_url = unquote(url)
    
    # 1. Clean and sanitize incoming headers
    req_headers = {}
    for k, v in request.headers.items():
        if k.lower() not in FORBIDDEN_HEADERS:
            req_headers[k] = v

    # 2. Inject authentic browser spoofing & Age gate bypass
    req_headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    req_headers["Accept-Language"] = "en-US,en;q=0.9"
    
    # Ensure Pornhub age disclaimer is always set
    existing_cookie = req_headers.get("cookie", "")
    if "accessAgeDisclaimerPH=1" not in existing_cookie:
        req_headers["cookie"] = (existing_cookie + "; accessAgeDisclaimerPH=1; platform=pc;").strip("; ")

    # Set referer if missing
    if "referer" not in req_headers:
        req_headers["referer"] = "https://www.pornhub.com/"

    # 3. Handle request body safely
    req_content = None
    if request.method not in ["GET", "HEAD", "OPTIONS"]:
        req_content = await request.body()

    req = http_client.build_request(request.method, target_url, headers=req_headers, content=req_content)
    
    try:
        r = await http_client.send(req, stream=True)
    except httpx.TimeoutException:
        return Response(content="504 Gateway Timeout: Upstream server took too long.", status_code=504)
    except Exception as e:
        return Response(content=f"502 Proxy Error: {str(e)}", status_code=502)
    
    resp_headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS, HEAD",
        "Access-Control-Expose-Headers": "Content-Range, Content-Length, Accept-Ranges",
    }
    
    for h in ["Content-Type", "Content-Length", "Content-Range", "Accept-Ranges", "Content-Encoding"]:
        if h in r.headers:
            resp_headers[h] = r.headers[h]

    return StreamingResponse(
        r.aiter_raw(),
        status_code=r.status_code,
        headers=resp_headers,
        background=r.aclose
    )

@app.get("/")
def read_root():
    return {"status": "Proxy Online", "gateway": "Railway-NL"}
