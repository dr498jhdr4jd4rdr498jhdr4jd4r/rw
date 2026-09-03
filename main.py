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
        if k.lower() not in ["host", "connection"]:
            req_headers[k] = v

    # Add browser spoofing if missing
    headers_lower = [k.lower() for k in req_headers.keys()]
    if "user-agent" not in headers_lower:
        req_headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

    # Don't pass streams for GET requests! Read POST bodies into memory.
    req_content = None
    if request.method not in ["GET", "HEAD", "OPTIONS"]:
        req_content = await request.body() 

    client = httpx.AsyncClient(follow_redirects=True, timeout=60.0)
    req = client.build_request(request.method, target_url, headers=req_headers, content=req_content)
    
    try:
        # Stream the response back to handle large video files and fast HTML delivery
        r = await client.send(req, stream=True)
    except httpx.TimeoutException:
        return Response(content="504 Gateway Timeout: Upstream server took too long to respond.", status_code=504)
    except Exception as e:
        return Response(content=f"502 Proxy Error: {str(e)}", status_code=502)
    
    resp_headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
    }
    
    # CRITICAL FIX: Added "Content-Encoding" to the list. 
    # This prevents the gibberish text issue when upstream sends GZIP/Brotli data.
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
