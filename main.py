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

app = FastAPI(title="Railway VexoStream JSON API", lifespan=lifespan)

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
        target_url = f"https://www.pornhub.com/video/search?search={quote(q)}&page={page}&o=mv"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Cookie": "accessAgeDisclaimerPH=1; platform=pc;"}

        try:
            r = await http_client.get(target_url, headers=headers)
            html = r.text
        except Exception:
            return JSONResponse([])

        videos = []
        blocks = re.split(r'data-video-vkey="', html, flags=re.I)

        for block in blocks[1:]:
            try:
                block = block[:2500]
                vkey_match = re.search(r'^([a-z0-9]+)"?', block, re.I)
                title_match = re.search(r'(?:title|alt)="([^"]+)"', block, re.I)

                # FIX: Strictly target real image URLs avoiding lazy-load base64 placeholders
                thumb = ""
                img_match = re.search(r'data-image=["\']([^"\']+)["\']', block, re.I)
                if not img_match:
                    img_match = re.search(r'data-thumb_url=["\']([^"\']+)["\']', block, re.I)
                if not img_match:
                    src_match = re.search(r'src=["\']([^"\']+)["\']', block, re.I)
                    if src_match and not src_match.group(1).startswith("data:"):
                        img_match = src_match
                
                if img_match:
                    thumb = img_match.group(1).replace("\\/", "/")
                
                # Check for validity
                if not thumb or 'data:image' in thumb or 'pixel' in thumb or 'transparent' in thumb or 'blank' in thumb:
                    continue
                if thumb.startswith('//'): 
                    thumb = 'https:' + thumb
                if not thumb.startswith('http'):
                    continue

                dur_match = re.search(r'<var class="duration">([^<]+)<\/var>|<span class="duration">([^<]+)<\/span>', block, re.I)
                date_match = re.search(r'<var class="added">([^<]+)<\/var>', block, re.I)
                upload_date = date_match.group(1).strip() if date_match else ""

                if vkey_match and title_match:
                    vkey = vkey_match.group(1)
                    title = title_match.group(1).replace("&quot;", '"').replace("&amp;", "&").strip()

                    dur = "HD"
                    if dur_match:
                        raw_dur = dur_match.group(1) or dur_match.group(2)
                        if raw_dur: dur = raw_dur.strip()

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
                if len(videos) >= 48: break
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
            return JSONResponse({"status": "error", "error": "Invalid or unsupported link format."})
        vkey = vkey_match.group(1)

        target_url = f"https://www.pornhub.com/view_video.php?viewkey={vkey}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Cookie": "accessAgeDisclaimerPH=1; platform=pc;"}

        try:
            r = await http_client.get(target_url, headers=headers)
            html = r.text
        except Exception as e:
            return JSONResponse({"status": "error", "error": f"Upstream connection failed: {str(e)}"})

        title, poster, media_defs = "Unknown Title", "", []

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

        # FIX: Robust quality scraper for both MP4 and HLS variants
        for m in media_defs:
            if not isinstance(m, dict): continue
            v_url = m.get("videoUrl") or m.get("url")
            if not v_url: continue
            fmt = str(m.get("format", "")).lower()

            if fmt == "mp4":
                q_val = m.get("quality")
                if isinstance(q_val, list) and len(q_val) > 0: q_val = str(q_val[0])
                elif q_val: q_val = str(q_val)
                else: q_val = ""
                
                if q_val.isdigit(): q_val += "p"
                
                if q_val and not any(q['url'] == v_url for q in qualities):
                    qualities.append({"quality": q_val, "url": v_url, "type": "mp4"})

            elif fmt == "hls" or ".m3u8" in v_url:
                try:
                    m3_r = await http_client.get(v_url, headers=headers)
                    if m3_r.status_code == 200:
                        lines = m3_r.text.splitlines()
                        base_url = v_url[:v_url.rfind('/')+1]
                        for i, line in enumerate(lines):
                            line = line.strip()
                            if line.startswith("#EXT-X-STREAM-INF:"):
                                name_match = re.search(r'NAME=["\']([^"\']+)["\']', line)
                                res_match = re.search(r'RESOLUTION=(\d+x\d+)', line)
                                lbl = ""
                                if name_match:
                                    lbl = name_match.group(1)
                                elif res_match:
                                    parts = res_match.group(1).split('x')
                                    if len(parts) > 1: lbl = f"{parts[1]}p"
                                
                                if lbl and i+1 < len(lines) and not lines[i+1].startswith("#"):
                                    uri = lines[i+1].strip()
                                    abs_uri = uri if uri.startswith("http") else urljoin(base_url, uri)
                                    if not any(q['url'] == abs_uri for q in qualities):
                                        qualities.append({"quality": lbl, "url": abs_uri, "type": "hls"})
                except: pass

        if not qualities:
            clean_html = html.replace('\\/', '/')
            m3u8_links = set(re.findall(r'(https?:\/\/[^"\'\s]+\.m3u8(?:[^\'"]*))', clean_html))
            for link in m3u8_links:
                if "master.m3u8" in link or "index.m3u8" in link:
                    if not any(q['url'] == link for q in qualities):
                        qualities.append({"quality": "Source", "url": link, "type": "hls"})

        def get_res(q):
            num = re.sub(r'\D', '', q['quality'])
            return int(num) if num else 0
        
        qualities.sort(key=get_res, reverse=True)

        if not title or title == "Unknown Title":
            og = re.search(r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)["\']', html, re.I)
            if og: title = og.group(1).replace(" - Pornhub.com", "")

        return JSONResponse({
            "status": "success", "title": title.strip(), "thumbnail": poster.replace('\\/', '/'),
            "streams": {"qualities": qualities}, "url": target_url, "provider": "pornhub"
        })
    except Exception as e:
        return JSONResponse({"status": "error", "error": f"Internal API Error: {str(e)}"})

@app.get("/")
def read_root():
    return {"status": "Proxy API Online"}
