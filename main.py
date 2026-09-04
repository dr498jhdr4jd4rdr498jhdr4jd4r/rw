import re
import json
import httpx
import logging
from contextlib import asynccontextmanager
from urllib.parse import quote, urljoin, unquote
from fastapi import FastAPI
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
        timeout=httpx.Timeout(35.0, connect=10.0),
        limits=httpx.Limits(max_connections=400, max_keepalive_connections=80)
    )
    logger.info("Railway Scraper Core Started.")
    yield
    await http_client.aclose()
    logger.info("Railway Scraper Core Stopped.")

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
        for attr in ['data-mediumthumb', 'data-thumb_url', 'data-image', 'data-src', 'src']:
            match = re.search(rf'{attr}=["\'](https?://[^"\']+\.(?:jpg|jpeg|webp|png)(?:\?[^"\']*)?)["\']', block, re.I)
            if match:
                img = match.group(1).replace(r"\/", "/").replace("&amp;", "&")
                if not any(bad in img.lower() for bad in ['pixel', 'blank', 'transparent', 'data:image', '.webm', '.mp4']):
                    return img

        match = re.search(r'(https?://[^\s"\'<>]*(?:phncdn|pornhub)[^\s"\'<>]*(?:\.jpg|\.webp|\.jpeg|\.png)(?:\?[^\s"\'<>]*)?)', block, re.I)
        if match:
            img = match.group(1).replace(r"\/", "/").replace("&amp;", "&")
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
        return bool(re.search(r'\b(sponsor(?:ed)?|promo(?:tion)?|banner|signup|premium|ads?|advert)\b', title, re.I))

    @staticmethod
    def clean_title(title: str) -> str:
        return title.replace("&quot;", '"').replace("&amp;", "&").replace("&#039;", "'").strip()

@app.get("/api/explore")
async def explore(q: str = "brazzers", page: int = 1):
    try:
        target_url = f"https://www.pornhub.com/video/search?search={quote(q)}&page={page}"
        logger.info(f"Scraping Page {page}: {target_url}")

        try:
            r = await http_client.get(target_url, headers=get_clean_headers())
            html = r.text
        except Exception as err:
            logger.error(f"Search failed on page {page}: {err}")
            return JSONResponse([])

        videos = []
        blocks = re.split(r'data-video-vkey=["\']', html, flags=re.I)

        for block in blocks[1:]:
            try:
                if "adblock" in block[:400].lower() or "sponsor" in block[:400].lower():
                    continue

                vkey_match = re.match(r'^([a-zA-Z0-9]+)', block)
                if not vkey_match:
                    continue
                vkey = vkey_match.group(1)

                sub_block = block[:3000]
                title_match = re.search(r'title=["\']([^"\']+)["\']', sub_block, re.I)
                title = MediaExtractor.clean_title(title_match.group(1)) if title_match else "Unknown Video"

                if MediaExtractor.is_ad(title):
                    continue

                thumb = MediaExtractor.extract_thumbnail(sub_block)
                if not thumb:
                    continue

                date_match = re.search(r'<var class="added">([^<]+)<\/var>', sub_block, re.I)
                upload_date = date_match.group(1).strip() if date_match else ""

                if not any(v['vkey'] == vkey for v in videos):
                    videos.append({
                        "vkey": vkey,
                        "title": title,
                        "thumbnail": thumb,
                        "duration": MediaExtractor.extract_duration(sub_block),
                        "date": upload_date,
                        "url": f"https://www.pornhub.com/view_video.php?viewkey={vkey}",
                        "provider": "pornhub"
                    })

                if len(videos) >= 44:
                    break
            except Exception:
                continue

        logger.info(f"Page {page} Extracted: {len(videos)} videos.")
        return JSONResponse(videos)
    except Exception as e:
        logger.error(f"Explore Endpoint Error: {e}")
        return JSONResponse([])

@app.get("/api/extract")
async def extract(url: str):
    try:
        if not url:
            return JSONResponse({"status": "error", "error": "URL missing"})

        vkey_match = re.search(r'viewkey=([a-zA-Z0-9]+)', url, re.I)
        if not vkey_match:
            return JSONResponse({"status": "error", "error": "Invalid format"})
        vkey = vkey_match.group(1)

        target_url = f"https://www.pornhub.com/view_video.php?viewkey={vkey}"

        try:
            r = await http_client.get(target_url, headers=get_clean_headers())
            html = r.text
        except Exception as e:
            return JSONResponse({"status": "error", "error": f"Upstream connection failed: {str(e)}"})

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

        if not title:
            h1_match = re.search(r'<h1[^>]*class="[^"]*title[^"]*"[^>]*>(?:<span[^>]*class="inlineFree"[^>]*>)?([^<]+)', html, re.I)
            if h1_match:
                title = h1_match.group(1).strip()

        if not title:
            og_match = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
            if og_match:
                title = og_match.group(1).replace(" - Pornhub.com", "").strip()

        title = MediaExtractor.clean_title(title or "Video Player")

        if not poster:
            p_match = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
            if p_match:
                poster = p_match.group(1)

        qualities_dict = {}

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

            if is_hls and ("master.m3u8" in v_url or "index.m3u8" in v_url):
                try:
                    m3_r = await http_client.get(v_url, headers={"User-Agent": get_clean_headers()["User-Agent"], "Referer": "https://www.pornhub.com/"})
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

            if not q_val or q_val.lower() in ["source", "auto", "unknown"]:
                q_val = "Adaptive HD"

            if q_val not in qualities_dict:
                qualities_dict[q_val] = {
                    "quality": q_val,
                    "url": v_url,
                    "type": "hls" if is_hls else "mp4"
                }

        if not qualities_dict:
            clean_html = html.replace(r"\/", "/")
            m3u8_links = re.findall(r'(https?://[^"\'\s<>]+\.m3u8[^"\'\s<>]*)', clean_html)
            for link in m3u8_links:
                try:
                    m3_r = await http_client.get(link, headers={"User-Agent": get_clean_headers()["User-Agent"], "Referer": "https://www.pornhub.com/"})
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
                qualities_dict["Adaptive HD"] = {"quality": "Adaptive HD", "url": m3u8_links[0], "type": "hls"}

        qualities = list(qualities_dict.values())

        def get_res(q):
            num = re.sub(r'\D', '', q['quality'])
            return int(num) if num else 0

        qualities.sort(key=get_res, reverse=True)

        return JSONResponse({
            "status": "success",
            "title": title,
            "thumbnail": poster.replace(r"\/", "/").replace("&amp;", "&") if poster else "",
            "streams": {"qualities": qualities},
            "url": target_url,
            "provider": "pornhub"
        })
    except Exception as e:
        logger.error(f"Extract API Error: {e}")
        return JSONResponse({"status": "error", "error": f"Internal API Error: {str(e)}"})

@app.get("/")
def read_root():
    return {"status": "Proxy Online", "gateway": "Railway VexoStream Core"}
