import re
import json
import httpx
import logging
from contextlib import asynccontextmanager
from urllib.parse import quote, urljoin
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

http_client: httpx.AsyncClient = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(40.0, connect=12.0),
        limits=httpx.Limits(max_connections=500, max_keepalive_connections=100)
    )
    logger.info("Railway Scraper Core Initialized.")
    yield
    await http_client.aclose()
    logger.info("Railway Scraper Core Shutdown.")

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
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.pornhub.com/",
        "Origin": "https://www.pornhub.com",
        "Cookie": "accessAgeDisclaimerPH=1; platform=pc; hasVisited=1; cookiesBanner=1;"
    }

def extract_balanced_json(text: str, trigger_key: str):
    idx = text.find(trigger_key)
    if idx == -1:
        return None
    
    start_pos = -1
    is_array = False
    for i in range(idx + len(trigger_key), len(text)):
        if text[i] == '{':
            start_pos = i
            is_array = False
            break
        elif text[i] == '[':
            start_pos = i
            is_array = True
            break
        elif text[i] in [';', '\n'] and i > idx + 50:
            break
            
    if start_pos == -1:
        return None

    open_char = '[' if is_array else '{'
    close_char = ']' if is_array else '}'
    depth = 0
    in_string = False
    escape = False

    for i in range(start_pos, len(text)):
        char = text[i]
        if escape:
            escape = False
            continue
        if char == '\\':
            escape = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if not in_string:
            if char == open_char:
                depth += 1
            elif char == close_char:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start_pos:i+1])
                    except Exception:
                        return None
    return None

class MediaExtractor:
    @staticmethod
    def extract_thumbnail(block: str) -> str:
        # Handles protocol-relative URLs (//ei.phncdn.com/...) and standard https
        matches = re.findall(
            r'(?:data-mediumthumb|data-thumb_url|data-image|data-src|src)=["\']((?:https?:)?//[^\s"\'<>]+\.(?:jpg|jpeg|webp|png)(?:\?[^\s"\'<>]*)?)["\']',
            block,
            re.I
        )
        for img in matches:
            img = img.replace(r"\/", "/")
            if img.startswith("//"):
                img = "https:" + img
            if not any(bad in img.lower() for bad in ['pixel', 'blank', 'transparent', 'data:image', '.webm', '.mp4']):
                return img

        # Fallback search for any CDN image inside the block
        cdn_match = re.search(r'((?:https?:)?//[^\s"\'<>]*(?:phncdn|pornhub)[^\s"\'<>]*(?:\.jpg|\.webp|\.jpeg|\.png)(?:\?[^\s"\'<>]*)?)', block, re.I)
        if cdn_match:
            img = cdn_match.group(1).replace(r"\/", "/")
            if img.startswith("//"):
                img = "https:" + img
            if not any(bad in img.lower() for bad in ['pixel', 'blank', 'transparent', '.webm', '.mp4']):
                return img
        return ""

    @staticmethod
    def extract_duration(block: str) -> str:
        match = re.search(r'<var class="duration">([^<]+)<\/var>|<span class="duration">([^<]+)<\/span>', block, re.I)
        if match:
            val = match.group(1) or match.group(2)
            return val.strip() if val else "HD"
        return "HD"

    @staticmethod
    def is_ad(title: str) -> bool:
        return bool(re.search(r'\b(sponsor(?:ed)?|promo(?:tion)?|banner|signup|premium ads|advertisement)\b', title, re.I))

    @staticmethod
    def clean_title(title: str) -> str:
        return title.replace("&quot;", '"').replace("&amp;", "&").replace("&#039;", "'").strip()

@app.get("/api/explore")
async def explore(q: str = "brazzers", page: int = 1):
    try:
        q_clean = q.strip() if q else "brazzers"
        # Standard search URL with page parameter and no sorting overrides
        target_url = f"https://www.pornhub.com/video/search?search={quote(q_clean)}&page={page}"
        logger.info(f"Targeting: {target_url}")

        try:
            r = await http_client.get(target_url, headers=get_clean_headers())
            html = r.text
        except Exception as err:
            logger.error(f"Search fetch error: {err}")
            return JSONResponse([])

        videos = []
        
        # Primary container splitter
        blocks = html.split('class="pcVideoListItem')
        if len(blocks) <= 1:
            blocks = re.split(r'data-video-vkey=["\']', html, flags=re.I)

        for block in blocks[1:]:
            try:
                # Target viewkey
                vkey_match = re.search(r'data-video-vkey=["\']([a-zA-Z0-9]+)["\']', block) or \
                             re.search(r'viewkey=([a-zA-Z0-9]+)', block) or \
                             re.match(r'^([a-zA-Z0-9]+)', block.lstrip('"\' '))
                if not vkey_match:
                    continue
                vkey = vkey_match.group(1)

                title_match = re.search(r'title=["\']([^"\']+)["\']', block, re.I)
                title = MediaExtractor.clean_title(title_match.group(1)) if title_match else "Pornhub Video"

                if MediaExtractor.is_ad(title):
                    continue

                thumb = MediaExtractor.extract_thumbnail(block)
                if not thumb:
                    continue

                dur = MediaExtractor.extract_duration(block)

                if not any(v['vkey'] == vkey for v in videos):
                    videos.append({
                        "vkey": vkey,
                        "title": title,
                        "thumbnail": thumb,
                        "duration": dur,
                        "date": "",  # Purged to eliminate "56 years ago" bug
                        "url": f"https://www.pornhub.com/view_video.php?viewkey={vkey}",
                        "provider": "pornhub"
                    })

                if len(videos) >= 48:
                    break
            except Exception:
                continue

        logger.info(f"Page {page} extracted {len(videos)} items.")
        return JSONResponse(videos)
    except Exception as e:
        logger.error(f"Explore Endpoint Error: {e}")
        return JSONResponse([])

@app.get("/api/extract")
async def extract(url: str):
    try:
        if not url:
            return JSONResponse({"status": "error", "error": "URL parameter missing"})

        vkey_match = re.search(r'viewkey=([a-zA-Z0-9]+)', url, re.I)
        if not vkey_match:
            return JSONResponse({"status": "error", "error": "Invalid format"})
        vkey = vkey_match.group(1)

        target_url = f"https://www.pornhub.com/view_video.php?viewkey={vkey}"

        try:
            r = await http_client.get(target_url, headers=get_clean_headers())
            html = r.text
        except Exception as e:
            return JSONResponse({"status": "error", "error": f"Upstream error: {str(e)}"})

        title = ""
        poster = ""
        media_defs = []

        flashvars_data = extract_balanced_json(html, "flashvars_") or \
                         extract_balanced_json(html, "playerObjList_") or \
                         extract_balanced_json(html, "flashvars")

        if flashvars_data and isinstance(flashvars_data, dict):
            media_defs = flashvars_data.get("mediaDefinitions", [])
            title = flashvars_data.get("video_title", "")
            poster = flashvars_data.get("image_url") or flashvars_data.get("thumb_url") or ""

        if not media_defs:
            media_defs = extract_balanced_json(html, '"mediaDefinitions"') or []

        # Fallback Title Extraction
        if not title or title == "Unknown Title":
            og_match = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
            if og_match:
                title = og_match.group(1).replace(" - Pornhub.com", "").strip()

        if not title:
            h1_match = re.search(r'<h1[^>]*>.*?<span[^>]*class="inlineFree"[^>]*>([^<]+)</span>', html, re.DOTALL | re.I)
            if h1_match:
                title = h1_match.group(1).strip()

        if not title:
            t_match = re.search(r'<title>([^<]+)</title>', html, re.I)
            if t_match:
                title = t_match.group(1).replace(" - Pornhub.com", "").strip()

        title = MediaExtractor.clean_title(title or "Pornhub Video")

        if not poster:
            p_match = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
            if p_match:
                poster = p_match.group(1)

        qualities_dict = {}

        # Parse Stream Definitions
        for m in media_defs:
            if not isinstance(m, dict):
                continue
            v_url = m.get("videoUrl") or m.get("url")
            if not v_url:
                continue

            v_url = v_url.replace(r"\/", "/")
            q_val = m.get("quality", "")
            if isinstance(q_val, list) and len(q_val) > 0:
                q_val = str(q_val[0])
            else:
                q_val = str(q_val)

            fmt = str(m.get("format", "")).lower()
            is_hls = fmt == "hls" or ".m3u8" in v_url

            # Unpack Master HLS Manifest to eliminate generic "Source" labels
            if is_hls and ("master.m3u8" in v_url or "index.m3u8" in v_url):
                try:
                    m3_r = await http_client.get(
                        v_url,
                        headers={"User-Agent": get_clean_headers()["User-Agent"], "Referer": "https://www.pornhub.com/"}
                    )
                    if m3_r.status_code == 200 and "#EXT-X-STREAM-INF" in m3_r.text:
                        lines = m3_r.text.splitlines()
                        base_url = v_url[:v_url.rfind('/')+1]
                        for i, line in enumerate(lines):
                            if line.startswith("#EXT-X-STREAM-INF:"):
                                res_match = re.search(r'RESOLUTION=(\d+x\d+)', line)
                                parsed_q = ""
                                if res_match:
                                    parts = res_match.group(1).split('x')
                                    if len(parts) > 1:
                                        parsed_q = f"{parts[1]}p"
                                else:
                                    name_match = re.search(r'NAME=["\']?([^"\']+)["\']?', line)
                                    if name_match:
                                        parsed_q = name_match.group(1)

                                if parsed_q and i + 1 < len(lines):
                                    uri = lines[i+1].strip()
                                    if not uri.startswith("#"):
                                        abs_uri = uri if uri.startswith("http") else urljoin(base_url, uri)
                                        if parsed_q not in qualities_dict:
                                            qualities_dict[parsed_q] = {"quality": parsed_q, "url": abs_uri, "type": "hls"}
                        continue
                except Exception:
                    pass

            if q_val.isdigit():
                q_val = f"{q_val}p"

            # Rename generic labels to explicit high-definition labels
            if not q_val or q_val.lower() in ["source", "auto", "unknown"]:
                q_val = "1080p Full HD" if is_hls else "720p HD"

            if q_val not in qualities_dict:
                qualities_dict[q_val] = {
                    "quality": q_val,
                    "url": v_url,
                    "type": "hls" if is_hls else "mp4"
                }

        # Master Playlist Fallback
        if not qualities_dict:
            clean_html = html.replace(r"\/", "/")
            m3u8_links = re.findall(r'(https?://[^"\'\s<>]+\.m3u8[^"\'\s<>]*)', clean_html)
            for link in m3u8_links:
                try:
                    m3_r = await http_client.get(
                        link,
                        headers={"User-Agent": get_clean_headers()["User-Agent"], "Referer": "https://www.pornhub.com/"}
                    )
                    if m3_r.status_code == 200 and "#EXT-X-STREAM-INF" in m3_r.text:
                        lines = m3_r.text.splitlines()
                        base_url = link[:link.rfind('/')+1]
                        for i, line in enumerate(lines):
                            if line.startswith("#EXT-X-STREAM-INF:"):
                                res_match = re.search(r'RESOLUTION=(\d+x\d+)', line)
                                if res_match:
                                    parsed_q = f"{res_match.group(1).split('x')[1]}p"
                                    if i + 1 < len(lines):
                                        sub_uri = lines[i+1].strip()
                                        if not sub_uri.startswith("#"):
                                            abs_uri = sub_uri if sub_uri.startswith("http") else urljoin(base_url, sub_uri)
                                            if parsed_q not in qualities_dict:
                                                qualities_dict[parsed_q] = {"quality": parsed_q, "url": abs_uri, "type": "hls"}
                except Exception:
                    pass
                if qualities_dict:
                    break

            if not qualities_dict and m3u8_links:
                qualities_dict["1080p Full HD"] = {"quality": "1080p Full HD", "url": m3u8_links[0], "type": "hls"}

        qualities = list(qualities_dict.values())

        # Sort descending (1080p -> 720p -> 480p)
        def get_res(q):
            num = re.sub(r'\D', '', q['quality'])
            return int(num) if num else 0

        qualities.sort(key=get_res, reverse=True)

        return JSONResponse({
            "status": "success",
            "title": title,
            "thumbnail": poster.replace(r"\/", "/") if poster else "",
            "streams": {"qualities": qualities},
            "url": target_url,
            "provider": "pornhub"
        })
    except Exception as e:
        logger.error(f"Extract API Error: {e}")
        return JSONResponse({"status": "error", "error": f"Internal API Error: {str(e)}"})

@app.get("/")
def read_root():
    return {"status": "Online", "gateway": "VexoStream Scraper Core"}
