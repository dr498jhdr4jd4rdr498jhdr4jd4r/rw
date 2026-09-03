from contextlib import asynccontextmanager
from urllib.parse import unquote, quote, urljoin
import re
import json
import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse

FORBIDDEN_HEADERS = {
    "host", "connection", "content-length", "cf-ray", "cf-connecting-ip", 
    "cf-ipcountry", "cf-visitor", "cf-worker", "cdn-loop", "x-forwarded-for", 
    "x-forwarded-proto", "x-forwarded-host", "x-real-ip"
}

http_client: httpx.AsyncClient = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(45.0, connect=15.0),
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=50)
    )
    yield
    await http_client.aclose()

app = FastAPI(title="Railway VexoStream API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_clean_headers(request: Request):
    req_headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36", "Accept-Language": "en-US,en;q=0.9", "Referer": "https://www.pornhub.com/"}
    for k, v in request.headers.items():
        if k.lower() not in FORBIDDEN_HEADERS:
            req_headers[k] = v
    cookie = req_headers.get("cookie", "")
    if "accessAgeDisclaimerPH=1" not in cookie:
        req_headers["cookie"] = (cookie + "; accessAgeDisclaimerPH=1; platform=pc;").strip("; ")
    return req_headers

@app.api_route("/proxy-video", methods=["GET", "OPTIONS", "HEAD"])
async def proxy_video(url: str, request: Request):
    if request.method == "OPTIONS": return Response(status_code=204)
    req_headers = get_clean_headers(request)
    req = http_client.build_request(request.method, unquote(url), headers=req_headers)
    try:
        r = await http_client.send(req, stream=True)
    except Exception as e:
        return Response(f"502 Error: {str(e)}", status_code=502)
    
    resp_headers = {"Access-Control-Allow-Origin": "*", "Access-Control-Expose-Headers": "Content-Range, Content-Length, Accept-Ranges"}
    for h in ["Content-Type", "Content-Length", "Content-Range", "Accept-Ranges", "Content-Encoding"]:
        if h in r.headers: resp_headers[h] = r.headers[h]
    return StreamingResponse(r.aiter_raw(), status_code=r.status_code, headers=resp_headers, background=r.aclose)

@app.api_route("/proxy-image", methods=["GET"])
async def proxy_image(url: str, request: Request):
    req_headers = get_clean_headers(request)
    req = http_client.build_request("GET", unquote(url), headers=req_headers)
    try:
        r = await http_client.send(req, stream=True)
        resp_headers = {"Access-Control-Allow-Origin": "*", "Content-Type": r.headers.get("Content-Type", "image/jpeg")}
        return StreamingResponse(r.aiter_raw(), status_code=r.status_code, headers=resp_headers, background=r.aclose)
    except:
        return Response(status_code=404)

@app.api_route("/proxy-m3u8", methods=["GET"])
async def proxy_m3u8(url: str, request: Request, request_obj: Request):
    target = unquote(url)
    req_headers = get_clean_headers(request)
    r = await http_client.get(target, headers=req_headers)
    
    if r.status_code != 200:
        return Response(status_code=r.status_code)
        
    base = target[:target.rfind('/')+1]
    cf_host = f"{request_obj.url.scheme}://{request_obj.headers.get('host')}"
    
    rewritten = []
    for line in r.text.splitlines():
        line = line.strip()
        if not line: continue
        
        if line.startswith("#EXT-X-MAP:") or line.startswith("#EXT-X-KEY:"):
            def replacer(match):
                uri = match.group(1)
                abs_uri = uri if uri.startswith('http') else urljoin(base, uri)
                return f'URI="{cf_host}/proxy-video?url={quote(abs_uri)}"'
            rewritten.append(re.sub(r'URI="([^"]+)"', replacer, line))
        elif line.startswith("#"):
            rewritten.append(line)
        else:
            abs_uri = line if line.startswith('http') else urljoin(base, line)
            if ".m3u8" in abs_uri:
                rewritten.append(f"{cf_host}/proxy-m3u8?url={quote(abs_uri)}")
            else:
                rewritten.append(f"{cf_host}/proxy-video?url={quote(abs_uri)}")

    return Response("\n".join(rewritten), media_type="application/vnd.apple.mpegurl", headers={"Access-Control-Allow-Origin": "*"})

@app.get("/")
def read_root():
    return {"status": "Proxy Online", "gateway": "Railway-NL CORS Node"}
