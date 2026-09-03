import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from urllib.parse import unquote

app = FastAPI(title="Railway Private CORS Proxy")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.api_route("/proxy", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD"])
async def proxy_request(url: str, request: Request):
    if request.method == "OPTIONS":
        return Response(status_code=204)

    target_url = unquote(url)
    
    # Forward original headers but strip host and connection to avoid conflicts
    req_headers = {}
    for k, v in request.headers.items():
        if k.lower() not in ["host", "connection", "content-length", "cf-connecting-ip", "x-forwarded-for"]:
            req_headers[k] = v

    # 1. Force modern browser User-Agent
    req_headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    req_headers["Accept-Language"] = "en-US,en;q=0.9"

    # 2. CRITICAL FIX: Inject Age Verification Cookie to Bypass 403 WAF Blocks
    if "pornhub" in target_url.lower():
        req_headers["Cookie"] = "accessAgeDisclaimerPH=1; platform=pc;"

    # 3. Read POST bodies into memory; ignore streams for GET requests to prevent httpx crashes
    req_content = None
    if request.method not in ["GET", "HEAD", "OPTIONS"]:
        req_content = await request.body() 

    # 4. Strict connection pooling and 60-second timeouts to handle video streams
    limits = httpx.Limits(max_keepalive_connections=5, max_connections=20)
    client = httpx.AsyncClient(follow_redirects=True, timeout=60.0, limits=limits)
    req = client.build_request(request.method, target_url, headers=req_headers, content=req_content)
    
    try:
        # Stream the response back (crucial for M3U8 and MP4 chunks)
        r = await client.send(req, stream=True)
    except httpx.TimeoutException:
        return Response(content="504 Gateway Timeout: Upstream CDN took too long.", status_code=504)
    except Exception as e:
        return Response(content=f"502 Proxy Error: {str(e)}", status_code=502)
    
    resp_headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
    }
    
    # 5. Forward encoding and range headers so Cloudflare UI can play the videos
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
    return {"status": "Proxy Online", "usage": "/proxy?url=YOUR_URL"}
