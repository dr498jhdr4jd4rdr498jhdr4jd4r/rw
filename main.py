import re
import json
import httpx
import logging
from contextlib import asynccontextmanager
from urllib.parse import unquote, quote, urljoin
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# --- CONFIGURATION & LOGGING ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

FORBIDDEN_HEADERS = {
    "host", "connection", "content-length", "cf-ray", "cf-connecting-ip", 
    "cf-ipcountry", "cf-visitor", "cf-worker", "cdn-loop", "x-forwarded-for", 
    "x-forwarded-proto", "x-forwarded-host", "x-real-ip"
}

# --- GLOBAL HTTP CLIENT ---
http_client: httpx.AsyncClient = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(45.0, connect=15.0),
        limits=httpx.Limits(max_connections=500, max_keepalive_connections=100)
    )
    logger.info("Enterprise HTTP Client Initialized.")
    yield
    await http_client.aclose()
    logger.info("Enterprise HTTP Client Shutdown.")

app = FastAPI(title="VexoStream Enterprise Scraper API", version="3.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- UTILITIES ---
def get_clean_headers():
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.pornhub.com/",
        "Origin": "https://www.pornhub.com",
        "Cookie": "accessAgeDisclaimerPH=1; platform=pc;"
    }

class MediaExtractor:
    @staticmethod
    def extract_thumbnail(block: str) -> str:
        """Deep scan for the highest quality, non-lazy-loaded thumbnail."""
        candidates = re.findall(r'data-(?:image|thumb_url|mediumthumb|largeimage)=["\']([^"\']+)["\']', block, re.I)
        
        for img in candidates:
            img = img.replace("\\/", "/")
            if not any(bad in img.lower() for bad in ['blank', 'pixel', 'transparent', 'data:image', '.webm', '.mp4']):
                return img if img.startswith('http') else f"https:{img}" if img.startswith('//') else img
                
        # Fallback to pure src if data-attributes fail (Ensuring it's a real image)
        src_match = re.findall(r'src=["\']([^"\']+(?:\.jpg|\.webp|\.jpeg|\.png)[^"\']*)["\']', block, re.I)
        for img in src_match:
            img = img.replace("\\/", "/")
            if not img.startswith("data:") and "pixel" not in img.lower():
                return img if img.startswith('http') else f"https:{img}" if img.startswith('//') else img
                
        return ""

    @staticmethod
    def extract_duration(block: str) -> str:
        match = re.search(r'<var class="duration">([^<]+)<\/var>|<span class="duration">([^<]+)<\/span>', block, re.I)
        if match:
            val = match.group(1) or match.group(2)
            return val.strip() if val else "HD"
        return "HD"

    @staticmethod
    def clean_title(title: str) -> str:
        return title.replace("&quot;", '"').replace("&amp;", "&").strip()

    @staticmethod
    def is_ad(title: str) -> bool:
        return bool(re.search(r'\b(sponsor(?:ed)?|promo(?:tion)?|banner|signup|premium|ads?|advert)\b', title, re.I))


# --- API ENDPOINTS ---
@app.get("/api/explore")
async def explore_network(q: str = "brazzers", page: int = 1):
    try:
        target_url = f"https://www.pornhub.com/video/search?search={quote(q)}&page={page}&o=mv"
        logger.info(f"Fetching network page: {target_url}")
        
        try:
            r = await http_client.get(target_url, headers=get_clean_headers())
            html = r.text
        except Exception as err:
            logger.error(f"Upstream fetch failed: {err}")
            return JSONResponse([])

        videos = []
        blocks = html.split('class="pcVideoListItem')
        
        for block in blocks[1:]:
            try:
                # Early ad-block filtering
                if "adblock" in block.lower() or "sponsor" in block.lower():
                    continue

                vkey_match = re.search(r'data-video-vkey=["\']([a-z0-9]+)["\']', block, re.I)
                if not vkey_match: 
                    continue
                vkey = vkey_match.group(1)

                title_match = re.search(r'title=["\']([^"\']+)["\']', block, re.I)
                title = MediaExtractor.clean_title(title_match.group(1)) if title_match else "Unknown Title"
                
                if MediaExtractor.is_ad(title):
                    continue

                thumb = MediaExtractor.extract_thumbnail(block)
                # Strict enforcement: If no valid thumbnail is found, drop the video to prevent blank cards
                if not thumb:
                    continue

                date_match = re.search(r'<var class="added">([^<]+)<\/var>', block, re.I)
                upload_date = date_match.group(1).strip() if date_match else ""

                if not any(v['vkey'] == vkey for v in videos):
                    videos.append({
                        "vkey": vkey, 
                        "title": title, 
                        "thumbnail": thumb, 
                        "duration": MediaExtractor.extract_duration(block),
                        "date": upload_date,
                        "url": f"https://www.pornhub.com/view_video.php?viewkey={vkey}", 
                        "provider": "pornhub"
                    })
                
                if len(videos) >= 48: 
                    break
            except Exception as e:
                continue 

        return JSONResponse(videos)
    except Exception as e:
        logger.error(f"Explore Endpoint Error: {e}")
        return JSONResponse([])


@app.get("/api/extract")
async def extract_streams(url: str):
    try:
        if not url:
            return JSONResponse({"status": "error", "error": "URL parameter missing"})

        vkey_match = re.search(r'viewkey=([a-z0-9]+)', url, re.I)
        if not vkey_match:
            return JSONResponse({"status": "error", "error": "Invalid URL structure"})
        vkey = vkey_match.group(1)

        target_url = f"https://www.pornhub.com/view_video.php?viewkey={vkey}"

        try:
            r = await http_client.get(target_url, headers=get_clean_headers())
            html = r.text
        except Exception as e:
            return JSONResponse({"status": "error", "error": "Upstream timeout"})

        title, poster, media_defs = "Unknown Media", "", []

        # Find Core Metadata Object
        fv_match = re.search(r'flashvars_\d+\s*=\s*(\{.*?\});', html, re.DOTALL) or \
                   re.search(r'flashvars\s*=\s*(\{.*?\});', html, re.DOTALL) or \
                   re.search(r'var\s+playerObjList\s*=\s*(\{.*?\});', html, re.DOTALL)
        
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

        qualities_dict = {}

        for m in media_defs:
            if not isinstance(m, dict): continue
            v_url = m.get("videoUrl") or m.get("url")
            if not v_url: continue
            
            q_val = m.get("quality", "")
            if isinstance(q_val, list) and len(q_val) > 0: q_val = str(q_val[0])
            else: q_val = str(q_val)
            
            if q_val.isdigit(): q_val += "p"
            
            fmt = str(m.get("format", "")).lower()
            is_hls = fmt == "hls" or ".m3u8" in v_url
            stream_type = "hls" if is_hls else "mp4"

            # Parse deep HLS Master playlists to extract native resolutions instead of showing "Source"
            if is_hls and ("master.m3u8" in v_url or "index.m3u8" in v_url):
                try:
                    m3_r = await http_client.get(v_url, headers=get_clean_headers())
                    if m3_r.status_code == 200:
                        lines = m3_r.text.splitlines()
                        base_url = v_url[:v_url.rfind('/')+1]
                        for i, line in enumerate(lines):
                            if line.startswith("#EXT-X-STREAM-INF:"):
                                res_match = re.search(r'RESOLUTION=(\d+x\d+)', line)
                                parsed_q = ""
                                if res_match:
                                    parts = res_match.group(1).split('x')
                                    if len(parts) > 1: parsed_q = f"{parts[1]}p"
                                
                                if parsed_q and i + 1 < len(lines):
                                    uri = lines[i+1].strip()
                                    abs_uri = uri if uri.startswith("http") else urljoin(base_url, uri)
                                    if parsed_q not in qualities_dict:
                                        qualities_dict[parsed_q] = {"quality": parsed_q, "url": abs_uri, "type": "hls"}
                        continue # Skip adding the master playlist if sub-playlists were mapped
                except:
                    pass

            # Fallback formatting if deep parsing failed
            if not q_val:
                q_val = "Auto (Adaptive)" if is_hls else "Native"

            if q_val not in qualities_dict:
                qualities_dict[q_val] = {
                    "quality": q_val, 
                    "url": v_url.replace("\\/", "/"), 
                    "type": stream_type
                }

        qualities = list(qualities_dict.values())

        # Final Fallback for broken mediaDefinitions
        if not qualities:
            clean_html = html.replace('\\/', '/')
            master_m3u8 = re.search(r'(https?:\/\/[^"\'\s]+(?:master|index)\.m3u8(?:[^\'"]*))', clean_html)
            if master_m3u8:
                qualities.append({"quality": "Auto (Adaptive)", "url": master_m3u8.group(1), "type": "hls"})
            else:
                any_m3u8 = re.search(r'(https?:\/\/[^"\'\s]+\.m3u8(?:[^\'"]*))', clean_html)
                if any_m3u8:
                    qualities.append({"quality": "Auto (Adaptive)", "url": any_m3u8.group(1), "type": "hls"})

        # Sorting logic: Descending numerical, putting "Auto" at the end
        def get_res(q):
            num = re.sub(r'\D', '', q['quality'])
            return int(num) if num else -1
        
        qualities.sort(key=get_res, reverse=True)

        if not title or title == "Unknown Media":
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
        logger.error(f"Extraction Error: {str(e)}")
        return JSONResponse({"status": "error", "error": f"Internal API Error"})

@app.get("/")
def health_check():
    return {"status": "Enterprise Proxy API V3 Online", "health": "Stable"}
