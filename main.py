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

app = FastAPI(title="Railway VexoStream HLS API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_clean_headers(request: Request):
    req_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36", 
        "Accept-Language": "en-US,en;q=0.9", 
        "Referer": "https://www.pornhub.com/",
        "Origin": "https://www.pornhub.com"
    }
    for k, v in request.headers.items():
        if k.lower() not in FORBIDDEN_HEADERS:
            req_headers[k] = v
    cookie = req_headers.get("cookie", "")
    if "accessAgeDisclaimerPH=1" not in cookie:
        req_headers["cookie"] = (cookie + "; accessAgeDisclaimerPH=1; platform=pc;").strip("; ")
    return req_headers

@app.get("/api/explore")
async def explore(q: str = "brazzers", page: int = 1):
    try:
        target_url = f"https://www.pornhub.com/video/search?search={quote(q)}&page={page}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", 
            "Cookie": "accessAgeDisclaimerPH=1; platform=pc;",
            "Referer": "https://www.pornhub.com/"
        }
        
        try:
            r = await http_client.get(target_url, headers=headers)
            html = r.text
        except Exception:
            return JSONResponse([])

        videos = []
        blocks = re.split(r'data-video-vkey\s*=\s*"', html, flags=re.I)
        
        for block in blocks[1:]:
            try:
                block = block[:3000]
                vkey_match = re.search(r'^([a-z0-9]+)"?', block, re.I)
                if not vkey_match:
                    continue
                vkey = vkey_match.group(1)

                title_match = re.search(r'(?:title|alt)\s*=\s*"([^"]+)"', block, re.I)
                title = title_match.group(1).replace("&quot;", '"').replace("&amp;", "&").strip() if title_match else f"Video {vkey}"
                
                thumb_match = re.search(r'data-(?:mediumthumb|thumb|thumb_url|mediabook|image)\s*=\s*"([^"]+)"', block, re.I)
                if not thumb_match:
                    thumb_match = re.search(r'src\s*=\s*"([^"]+\.(?:jpg|jpeg|png|webp)[^"]*)"', block, re.I)
                
                thumb = thumb_match.group(1) if thumb_match else ""
                thumb = thumb.replace("&amp;", "&")
                if thumb.startswith('//'): 
                    thumb = 'https:' + thumb
                
                if not thumb or not thumb.startswith('http') or any(x in thumb for x in ['data:image', 'pixel', 'transparent', 'blank', 'spinner']):
                    continue

                dur_match = re.search(r'<(?:var|span)\s+class="duration"[^>]*>([^<]+)</(?:var|span)>', block, re.I)
                dur = dur_match.group(1).strip() if dur_match else "HD"

                date_match = re.search(r'<(?:var|span)\s+class="added"[^>]*>([^<]+)</(?:var|span)>', block, re.I)
                upload_date = date_match.group(1).strip() if date_match else ""
                
                is_ad = re.search(r'\b(sponsor|promo|banner|signup|premium ads)\b', title, re.I)
                
                if not is_ad and not any(v['vkey'] == vkey for v in videos):
                    videos.append({
                        "vkey": vkey, 
                        "title": title, 
                        "thumbnail": thumb, 
                        "duration": dur,
                        "date": upload_date,
                        "url": f"https://www.pornhub.com/view_video.php?viewkey={vkey}", 
                        "provider": "pornhub"
                    })
                if len(videos) >= 48: 
                    break
            except Exception:
                continue 
        
        return JSONResponse(videos)
    except Exception as e:
        return JSONResponse([])

@app.get("/api/extract")
async def extract(url: str):
    try:
        if not url:
            return JSONResponse({"status": "error", "error": "URL parameter is missing"})

        vkey_match = re.search(r'viewkey=([a-z0-9]+)', url, re.I)
        if not vkey_match:
            alt_match = re.search(r'embed/([a-z0-9]+)', url, re.I)
            if alt_match:
                vkey = alt_match.group(1)
            else:
                return JSONResponse({"status": "error", "error": "Invalid or unsupported Pornhub link format."})
        else:
            vkey = vkey_match.group(1)

        target_url = f"https://www.pornhub.com/view_video.php?viewkey={vkey}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Cookie": "accessAgeDisclaimerPH=1; platform=pc;", "Referer": "https://www.pornhub.com/"}

        try:
            r = await http_client.get(target_url, headers=headers)
            html = r.text
        except Exception as e:
            return JSONResponse({"status": "error", "error": f"Upstream connection failed: {str(e)}"})

        title = "Unknown Title"
        poster = ""
        media_defs = []
        
        fv_match = re.search(r'flashvars_\d+\s*=\s*(\{.*?\});', html, re.DOTALL) or re.search(r'flashvars\s*=\s*(\{.*?\});', html, re.DOTALL) or re.search(r'var\s+playerObjList\s*=\s*(\{.*?\});', html, re.DOTALL)
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

        qualities = []

        for m in media_defs:
            if not isinstance(m, dict): continue
            v_url = m.get("videoUrl") or m.get("url")
            if not v_url: continue
            
            fmt = m.get("format", "")
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
                                height = "0"
                                if res_match and res_match.group(1) and 'x' in res_match.group(1):
                                    parts = res_match.group(1).split('x')
                                    if len(parts) > 1: height = parts[1]
                                lbl = f"{height}p" if height.isdigit() and int(height)>0 else ""
                                if lbl and i+1 < len(lines) and not lines[i+1].startswith("#"):
                                    uri = lines[i+1].strip()
                                    abs_uri = uri if uri.startswith("http") else urljoin(base_url, uri)
                                    if not any(q['url'] == abs_uri for q in qualities):
                                        qualities.append({"quality": lbl, "url": abs_uri})
                except:
                    pass

        if not qualities:
            clean_html = html.replace('\\/', '/')
            m3u8_links = set(re.findall(r'(https?:\/\/[^"\'\s]+\.m3u8(?:[^\'"]*))', clean_html))
            for link in m3u8_links:
                if "master.m3u8" in link or "index.m3u8" in link:
                    if not any(q['url'] == link for q in qualities):
                        qualities.append({"quality": "Source", "url": link})

        def get_res(q):
            num = re.sub(r'\D', '', q['quality'])
            return int(num) if num else 0
        qualities.sort(key=get_res, reverse=True)

        if not title or title == "Unknown Title":
            og = re.search(r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)["\']', html, re.I)
            if og: title = og.group(1).replace(" - Pornhub.com", "")

        return JSONResponse({
            "status": "success", "title": title.strip(), "thumbnail": poster,
            "streams": {"qualities": qualities}, "url": target_url, "provider": "pornhub"
        })
    except Exception as e:
        return JSONResponse({"status": "error", "error": f"Internal API Error: {str(e)}"})

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
    if not url:
        return Response(status_code=400)
    target_url = unquote(url)
    req_headers = get_clean_headers(request)
    req_headers["Referer"] = "https://www.pornhub.com/"
    try:
        req = http_client.build_request("GET", target_url, headers=req_headers)
        r = await http_client.send(req, stream=True)
        if r.status_code != 200:
            return Response(status_code=r.status_code)
        resp_headers = {
            "Access-Control-Allow-Origin": "*",
            "Content-Type": r.headers.get("Content-Type", "image/jpeg"),
            "Cache-Control": "public, max-age=86400"
        }
        return StreamingResponse(r.aiter_raw(), status_code=r.status_code, headers=resp_headers, background=r.aclose)
    except Exception:
        return Response(status_code=404)

@app.api_route("/proxy-m3u8", methods=["GET"])
async def proxy_m3u8(url: str, request: Request):
    target = unquote(url)
    req_headers = get_clean_headers(request)
    r = await http_client.get(target, headers=req_headers)
    
    if r.status_code != 200:
        return Response(status_code=r.status_code)
        
    base = target[:target.rfind('/')+1]
    request_host = request.query_params.get("request_host")
    cf_host = f"https://{request_host}" if request_host else f"{request.url.scheme}://{request.headers.get('host')}"
    
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
                rewritten.append(f"{cf_host}/proxy-m3u8?url={quote(abs_uri)}&request_host={quote(request_host or '')}")
            else:
                rewritten.append(f"{cf_host}/proxy-video?url={quote(abs_uri)}")

    return Response("\n".join(rewritten), media_type="application/vnd.apple.mpegurl", headers={"Access-Control-Allow-Origin": "*"})

@app.get("/")
def read_root():
    return {"status": "Proxy Online", "gateway": "Railway-NL HLS Router"}
