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
        timeout=httpx.Timeout(40.0, connect=15.0),
        limits=httpx.Limits(max_connections=500, max_keepalive_connections=100)
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

def get_stealth_headers():
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Ch-Ua": '"Chromium";v="128", "Not;A=Brand";v="24", "Google Chrome";v="128"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "Referer": "https://www.pornhub.com/",
        "Cookie": "accessAgeDisclaimerPH=1; platform=pc; bs=1; hasVisited=1; cookiesBanner=1;"
    }

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
        # Strategy 1: Standard Desktop Search
        target_url = f"https://www.pornhub.com/video/search?search={quote(q)}&page={page}"
        html = ""
        
        try:
            r = await http_client.get(target_url, headers=get_stealth_headers())
            if r.status_code == 200 and "data-video-vkey" in r.text:
                html = r.text
            else:
                logger.warning(f"Strategy 1 blocked with status {r.status_code}. Trying mobile fallback...")
        except Exception as err:
            logger.error(f"Search request failed: {err}")

        # Strategy 2: Mobile Fallback if blocked
        if not html:
            try:
                m_headers = get_stealth_headers()
                m_headers["User-Agent"] = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
                m_headers["Cookie"] = "accessAgeDisclaimerPH=1; platform=mobile;"
                r_m = await http_client.get(f"https://www.pornhub.com/video/search?search={quote(q)}&page={page}", headers=m_headers)
                if r_m.status_code == 200:
                    html = r_m.text
            except Exception as e:
                logger.error(f"Strategy 2 failed: {e}")

        if not html:
            return JSONResponse([])

        videos = []
        blocks = re.split(r'data-video-vkey=["\']|data-vkey=["\']', html, flags=re.I)

        for block in blocks[1:]:
            try:
                if "adblock" in block[:400].lower() or "sponsor" in block[:400].lower():
                    continue

                vkey_match = re.match(r'^([a-zA-Z0-9]+)', block)
                if not vkey_match:
                    continue
                vkey = vkey_match.group(1)

                sub_block = block[:3500]
                title_match = re.search(r'title=["\']([^"\']+)["\']|alt=["\']([^"\']+)["\']', sub_block, re.I)
                title_raw = title_match.group(1) or title_match.group(2) if title_match else "Unknown Video"
                title = MediaExtractor.clean_title(title_raw)

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

        logger.info(f"Page {page}: successfully extracted {len(videos)} videos.")
        return JSONResponse(videos)
    except Exception as e:
        logger.error(f"Explore Endpoint Failure: {e}")
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
            r = await http_client.get(target_url, headers=get_stealth_headers())
            html = r.text
        except Exception as e:
            return JSONResponse({"status": "error", "error": f"Upstream error: {str(e)}"})

        title = ""
        poster = ""
        media_defs = []

        fv_match = re.search(r'flashvars_\d+\s*=\s*(\{.*?\});', html, re.DOTALL) or \
                   re.search(r'flashvars\s*=\s*(\{.*?\});', html, re.DOTALL) or \
                   re.search(r'var\s+playerObjList\s*=\s*(\{.*?\});', html, re.DOTALL)
        if fv_match:
            try:
                data = json.loads(fv_match.group(1))
                media_defs = data.get("mediaDefinitions", [])
                title = data.get("video_title", "")
                poster = data.get("image_url") or data.get("thumb_url") or ""
            except Exception:
                pass

        if not media_defs:
            md_raw = re.search(r'"mediaDefinitions"\s*:\s*(\[\{.*?\}\])', html, re.DOTALL)
            if md_raw:
                try:
                    media_defs = json.loads(md_raw.group(1))
                except Exception:
                    pass

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
                    m3_r = await http_client.get(v_url, headers={"User-Agent": get_stealth_headers()["User-Agent"], "Referer": "https://www.pornhub.com/"})
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

@app.get("/proxy-image")
async def fallback_proxy_image(url: str):
    try:
        req = http_client.build_request("GET", unquote(url), headers=get_stealth_headers())
        r = await http_client.send(req, stream=True)
        return StreamingResponse(r.aiter_raw(), status_code=r.status_code, headers={"Content-Type": r.headers.get("Content-Type", "image/jpeg"), "Access-Control-Allow-Origin": "*"})
    except Exception:
        return Response(status_code=404)

@app.get("/")
def read_root():
    return {"status": "Online", "gateway": "VexoStream Scraper Core"}
