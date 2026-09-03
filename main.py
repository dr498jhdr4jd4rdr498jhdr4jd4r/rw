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
    # Ensure bypass cookie exists
    cookie = req_headers.get("cookie", "")
    if "accessAgeDisclaimerPH=1" not in cookie:
        req_headers["cookie"] = (cookie + "; accessAgeDisclaimerPH=1; platform=pc;").strip("; ")
    return req_headers

# ============================================================================
# JSON Scraper Endpoints (Now 100% Crash-Proof)
# ============================================================================

@app.get("/api/explore")
async def explore(q: str = "brazzers", page: int = 1):
    try:
        target_url = f"https://www.pornhub.com/video/search?search={quote(q)}&page={page}&o=mv"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Cookie": "accessAgeDisclaimerPH=1; platform=pc;"}
        
        try:
            r = await http_client.get(target_url, headers=headers)
            html = r.text
        except Exception:
            return JSONResponse([])

        videos = []
        blocks = re.split(r'data-video-vkey="', html, flags=re.IGNORECASE)
        
        for block in blocks[1:]:
            block = block[:1500]
            vkey_match = re.search(r'^([a-z0-9]+)"?', block, re.IGNORECASE)
            title_match = re.search(r'(?:title|alt)="([^"]+)"', block, re.IGNORECASE)
            thumb_match = re.search(r'(?:data-thumb_url|data-mediabook|data-src|src)="([^"]+\.(?:jpg|jpeg|png|webp|gif)[^"]*)"', block, re.IGNORECASE)
            dur_match = re.search(r'<var class="duration">([^<]+)<\/var>|<span class="duration">([^<]+)<\/span>', block, re.IGNORECASE)

            if vkey_match and title_match and thumb_match:
                vkey = vkey_match.group(1)
                title = title_match.group(1).replace("&quot;", '"').replace("&amp;", "&").strip()
                thumb = thumb_match.group(1)
                if thumb.startswith('//'): thumb = 'https:' + thumb
                
                dur = "HD"
                if dur_match:
                    # Safety fallback if duration regex groups are None
                    raw_dur = dur_match.group(1) or dur_match.group(2)
                    if raw_dur:
                        dur = raw_dur.strip()
                
                is_ad = re.search(r'\b(sponsor|promo|banner|signup|premium ads)\b', title, re.IGNORECASE)
                
                if not is_ad and not any(v['vkey'] == vkey for v in videos):
                    videos.append({
                        "vkey": vkey,
                        "title": title,
                        "thumbnail": thumb,
                        "duration": dur,
                        "url": f"https://www.pornhub.com/view_video.php?viewkey={vkey}",
                        "provider": "pornhub"
                    })
            if len(videos) >= 48: break
        
        return JSONResponse(videos)
    except Exception as e:
        # Prevents 500 error from crashing the UI
        print(f"Explore Error: {e}")
        return JSONResponse([])

@app.get("/api/extract")
async def extract(url: str):
    try:
        vkey_match = re.search(r'viewkey=([a-z0-9]+)', url)
        if not vkey_match:
            return JSONResponse({"status": "error", "error": "Invalid viewkey in URL"})
        
        vkey = vkey_match.group(1)
        target_url = f"https://www.pornhub.com/view_video.php?viewkey={vkey}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Cookie": "accessAgeDisclaimerPH=1; platform=pc;"}

        try:
            r = await http_client.get(target_url, headers=headers)
            html = r.text
        except Exception as e:
            return JSONResponse({"status": "error", "error": f"Upstream connection failed: {str(e)}"})

        title = "Unknown Title"
        poster = ""
        media_defs = []
        
        fv_match = re.search(r'flashvars(?:_\d+)?\s*=\s*(\{.*?\});', html, re.DOTALL)
        if fv_match:
            try:
                data = json.loads(fv_match.group(1))
                media_defs = data.get("mediaDefinitions", [])
                title = data.get("video_title", title)
                poster = data.get("image_url") or data.get("thumb_url") or poster
            except: pass
        
        if not media_defs:
            md_match = re.search(r'"mediaDefinitions"\s*:\s*(\[\{.*?\}\])', html, re.DOTALL)
            if md_match:
                try: media_defs = json.loads(md_match.group(1))
                except: pass
                
        if not media_defs:
            mp4_links = re.findall(r'https?:\/\/[^"\']+\.mp4(?:\?[^"\']*)?', html)
            for link in mp4_links:
                media_defs.append({"videoUrl": link, "quality": "Auto", "format": "mp4"})

        streams = {"direct_mp4": {}, "qualities": []}
        
        for m in media_defs:
            if not isinstance(m, dict): continue # Prevent dict crash
            v_url = m.get("videoUrl") or m.get("url")
            if not v_url: continue
            
            fmt = m.get("format", "")
            qual = str(m.get("quality", ["Auto"])[0] if isinstance(m.get("quality"), list) else m.get("quality", "Auto"))
            if not qual or qual == "[]": qual = "Auto"
            
            if fmt == "mp4" or "mp4" in v_url:
                label = f"{qual}p" if qual.isdigit() else qual.upper()
                streams["direct_mp4"][label] = v_url
                
            if fmt == "hls" or ".m3u8" in v_url:
                try:
                    m3_r = await http_client.get(v_url, headers=headers)
                    if m3_r.status_code == 200:
                        lines = m3_r.text.splitlines()
                        base_url = v_url[:v_url.rfind('/')+1]
                        for i, line in enumerate(lines):
                            line = line.strip()
                            if line.startswith("#EXT-X-STREAM-INF:"):
                                res_match = re.search(r'RESOLUTION=(\d+x\d+)', line)
                                height = res_match.group(1).split('x')[1] if res_match and 'x' in res_match.group(1) else '0'
                                lbl = f"{height}p" if height.isdigit() and int(height)>0 else "Auto"
                                if i+1 < len(lines) and not lines[i+1].startswith("#"):
                                    uri = lines[i+1].strip()
                                    abs_uri = uri if uri.startswith("http") else urljoin(base_url, uri)
                                    streams["qualities"].append({"quality": lbl, "url": abs_uri})
                except:
                    streams["qualities"].append({"quality": "Auto", "url": v_url})

        return JSONResponse({
            "status": "success",
            "title": title.strip(),
            "thumbnail": poster,
            "streams": streams,
            "url": target_url,
            "provider": "pornhub"
        })
    except Exception as e:
        # Guarantee JSON format response so Javascript UI doesn't crash
        return JSONResponse({"status": "error", "error": f"Internal API Error: {str(e)}"})

# ============================================================================
# Streaming Media Proxy Endpoints
# ============================================================================

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
    return {"status": "Proxy Online", "gateway": "Railway-NL API JSON Router"}
