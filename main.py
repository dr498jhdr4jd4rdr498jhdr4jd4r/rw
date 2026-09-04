import re
import json
import httpx
from contextlib import asynccontextmanager
from urllib.parse import unquote, quote, urljoin
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

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
        timeout=httpx.Timeout(30.0, connect=10.0),
        limits=httpx.Limits(max_connections=300, max_keepalive_connections=100)
    )
    yield
    await http_client.aclose()

app = FastAPI(title="VexoStream Enterprise Scraper", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_clean_headers():
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.pornhub.com/",
        "Origin": "https://www.pornhub.com",
        "Cookie": "accessAgeDisclaimerPH=1; platform=pc;"
    }

@app.get("/api/explore")
async def explore(q: str = "brazzers", page: int = 1):
    try:
        target_url = f"https://www.pornhub.com/video/search?search={quote(q)}&page={page}&o=mv"
        
        try:
            r = await http_client.get(target_url, headers=get_clean_headers())
            html = r.text
        except Exception:
            return JSONResponse([])

        videos = []
        
        # Split by video item container for precision
        blocks = html.split('class="pcVideoListItem')
        
        for block in blocks[1:]:
            try:
                # 1. Reject ad blocks immediately
                if "adblock" in block.lower() or "sponsor" in block.lower():
                    continue

                # 2. Extract Viewkey
                vkey_match = re.search(r'data-video-vkey=["\']([a-z0-9]+)["\']', block, re.I)
                if not vkey_match: 
                    continue
                vkey = vkey_match.group(1)

                # 3. Extract Title
                title_match = re.search(r'title=["\']([^"\']+)["\']', block, re.I)
                title = title_match.group(1).replace("&quot;", '"').replace("&amp;", "&").strip() if title_match else "Unknown"
                
                if re.search(r'\b(sponsor(?:ed)?|promo(?:tion)?|banner|signup|premium|ads?|advert)\b', title, re.I):
                    continue

                # 4. STRICT Thumbnail Extraction (Fixes the black box issue)
                thumb = None
                # Search for any valid image URL in the block (.jpg, .jpeg, .webp)
                # This ignores base64 lazy-loaders and empty pixels
                img_urls = re.findall(r'(https?://[^\s"\'<>]+(?:\.jpg|\.jpeg|\.webp)[^\s"\'<>]*)', block, re.I)
                
                for img in img_urls:
                    img_clean = img.replace("\\/", "/")
                    if not any(bad in img_clean.lower() for bad in ['blank', 'pixel', 'transparent', 'data:image']):
                        thumb = img_clean
                        break # Found the best valid image, stop searching
                
                # IF NO VALID IMAGE FOUND, SKIP THE VIDEO COMPLETELY
                if not thumb:
                    continue

                # 5. Extract Duration and Date
                dur_match = re.search(r'<var class="duration">([^<]+)<\/var>|<span class="duration">([^<]+)<\/span>', block, re.I)
                dur = "HD"
                if dur_match:
                    raw_dur = dur_match.group(1) or dur_match.group(2)
                    if raw_dur: dur = raw_dur.strip()

                date_match = re.search(r'<var class="added">([^<]+)<\/var>', block, re.I)
                upload_date = date_match.group(1).strip() if date_match else ""

                # Avoid duplicates
                if not any(v['vkey'] == vkey for v in videos):
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
            return JSONResponse({"status": "error", "error": "URL missing"})

        vkey_match = re.search(r'viewkey=([a-z0-9]+)', url, re.I)
        if not vkey_match:
            return JSONResponse({"status": "error", "error": "Invalid format"})
        vkey = vkey_match.group(1)

        target_url = f"https://www.pornhub.com/view_video.php?viewkey={vkey}"

        try:
            r = await http_client.get(target_url, headers=get_clean_headers())
            html = r.text
        except Exception as e:
            return JSONResponse({"status": "error", "error": "Upstream error"})

        title, poster, media_defs = "Unknown Title", "", []

        # Find Flashvars
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

        # ==========================================
        # FIX: DEDUPLICATED QUALITY EXTRACTION
        # ==========================================
        qualities_dict = {}

        for m in media_defs:
            if not isinstance(m, dict): continue
            v_url = m.get("videoUrl") or m.get("url")
            if not v_url: continue
            
            # Extract resolution from quality attribute
            q_val = m.get("quality")
            if isinstance(q_val, list) and len(q_val) > 0: q_val = str(q_val[0])
            elif q_val: q_val = str(q_val)
            else: q_val = ""
            
            if q_val.isdigit(): q_val += "p"
            
            fmt = str(m.get("format", "")).lower()
            is_hls = fmt == "hls" or ".m3u8" in v_url
            stream_type = "hls" if is_hls else "mp4"

            if not q_val:
                q_val = "Auto" if is_hls else "Source"

            # Deduplicate by saving to dictionary (overwrites duplicates with same label)
            # Prioritize adding if it doesn't exist. We want only one URL per quality label.
            if q_val not in qualities_dict:
                qualities_dict[q_val] = {
                    "quality": q_val, 
                    "url": v_url.replace("\\/", "/"), 
                    "type": stream_type
                }

        qualities = list(qualities_dict.values())

        # Fallback if dictionary is empty (only add ONE master m3u8 to prevent the 4x SourceHLS bug)
        if not qualities:
            clean_html = html.replace('\\/', '/')
            # Look for master manifest first
            master_m3u8 = re.search(r'(https?:\/\/[^"\'\s]+(?:master|index)\.m3u8(?:[^\'"]*))', clean_html)
            if master_m3u8:
                qualities.append({"quality": "Auto", "url": master_m3u8.group(1), "type": "hls"})
            else:
                # Any m3u8
                any_m3u8 = re.search(r'(https?:\/\/[^"\'\s]+\.m3u8(?:[^\'"]*))', clean_html)
                if any_m3u8:
                    qualities.append({"quality": "Auto", "url": any_m3u8.group(1), "type": "hls"})

        # Sort qualities descending (e.g., 1080p, 720p, 480p, Auto)
        def get_res(q):
            num = re.sub(r'\D', '', q['quality'])
            return int(num) if num else 0
        
        qualities.sort(key=get_res, reverse=True)

        if not title or title == "Unknown Title":
            og = re.search(r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)["\']', html, re.I)
            if og: title = og.group(1).replace(" - Pornhub.com", "")

        return JSONResponse({
            "status": "success", 
            "title": title.strip(), 
            "thumbnail": poster.replace('\\/', '/') if poster else "",
            "streams": {"qualities": qualities}, 
            "url": target_url, 
            "provider": "pornhub"
        })
    except Exception as e:
        return JSONResponse({"status": "error", "error": f"Internal API Error: {str(e)}"})

@app.get("/")
def read_root():
    return {"status": "Enterprise API Online"}
