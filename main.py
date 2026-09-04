import re
import json
import httpx
import logging
from contextlib import asynccontextmanager
from urllib.parse import quote, urljoin
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

http_client: httpx.AsyncClient = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(40.0, connect=12.0),
        limits=httpx.Limits(max_connections=400, max_keepalive_connections=80)
    )
    logger.info("FastAPI HTTP Client Initialized.")
    yield
    await http_client.aclose()
    logger.info("FastAPI HTTP Client Shutdown.")

app = FastAPI(title="Railway VexoStream JSON API", lifespan=lifespan)

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

class MediaExtractor:
    @staticmethod
    def extract_thumbnail(block: str) -> str:
        """Deep regex extraction ensuring real JPG/WEBP thumbnails on Page 1."""
        # Check all possible data-attributes where PH hides the real image
        for attr in ['data-mediumthumb', 'data-thumb_url', 'data-image', 'data-src', 'src']:
            match = re.search(rf'{attr}=["\'](https?://[^"\']+\.(?:jpg|jpeg|webp|png)(?:\?[^"\']*)?)["\']', block, re.I)
            if match:
                img = match.group(1).replace(r"\/", "/")
                if not any(bad in img.lower() for bad in ['pixel', 'blank', 'transparent', 'data:image', '.webm', '.mp4']):
                    return img

        # Fallback: scan any phncdn/pornhub CDN image directly
        match = re.search(r'(https?://[^\s"\'<>]*(?:phncdn|pornhub)[^\s"\'<>]*(?:\.jpg|\.webp|\.jpeg|\.png)(?:\?[^\s"\'<>]*)?)', block, re.I)
        if match:
            img = match.group(1).replace(r"\/", "/")
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


@app.get("/api/explore")
async def explore(q: str = "brazzers", page: int = 1):
    try:
        target_url = f"https://www.pornhub.com/video/search?search={quote(q)}&page={page}&o=mv"

        try:
            r = await http_client.get(target_url, headers=get_clean_headers())
            html = r.text
        except Exception as err:
            logger.error(f"Pornhub search request failed: {err}")
            return JSONResponse([])

        videos = []
        blocks = html.split('class="pcVideoListItem')

        for block in blocks[1:]:
            try:
                if "adblock" in block.lower() or "sponsor" in block.lower():
                    continue

                vkey_match = re.search(r'data-video-vkey=["\']([a-z0-9]+)["\']', block, re.I)
                if not vkey_match:
                    continue
                vkey = vkey_match.group(1)

                title_match = re.search(r'title=["\']([^"\']+)["\']', block, re.I)
                title = title_match.group(1).replace("&quot;", '"').replace("&amp;", "&").strip() if title_match else "Unknown"

                if MediaExtractor.is_ad(title):
                    continue

                # Strictly drop video if no valid image is found
                thumb = MediaExtractor.extract_thumbnail(block)
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
            except Exception:
                continue

        return JSONResponse(videos)
    except Exception as e:
        logger.error(f"Explore Endpoint Error: {e}")
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
            return JSONResponse({"status": "error", "error": f"Upstream connection failed: {str(e)}"})

        title, poster, media_defs = "Unknown Title", "", []

        fv_match = re.search(r'flashvars_\d+\s*=\s*(\{.*?\});', html, re.DOTALL) or \
                   re.search(r'flashvars\s*=\s*(\{.*?\});', html, re.DOTALL) or \
                   re.search(r'var\s+playerObjList\s*=\s*(\{.*?\});', html, re.DOTALL)
        if fv_match:
            try:
                data = json.loads(fv_match.group(1))
                media_defs = data.get("mediaDefinitions", [])
                title = data.get("video_title", title)
                poster = data.get("image_url") or data.get("thumb_url") or poster
            except Exception:
                pass

        if not media_defs:
            md_match = re.search(r'"mediaDefinitions"\s*:\s*(\[\{.*?\}\])', html, re.DOTALL)
            if md_match:
                try:
                    media_defs = json.loads(md_match.group(1))
                except Exception:
                    pass

        qualities_dict = {}

        # Parse qualities and delete "Source"
        for m in media_defs:
            if not isinstance(m, dict):
                continue
            v_url = m.get("videoUrl") or m.get("url")
            if not v_url:
                continue

            q_val = m.get("quality", "")
            if isinstance(q_val, list) and len(q_val) > 0:
                q_val = str(q_val[0])
            else:
                q_val = str(q_val)

            if q_val.isdigit():
                q_val += "p"

            fmt = str(m.get("format", "")).lower()
            is_hls = fmt == "hls" or ".m3u8" in v_url

            # Unpack Master M3U8 directly into 1080p, 720p, etc.
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
                                    if len(parts) > 1:
                                        parsed_q = f"{parts[1]}p"

                                if parsed_q and i + 1 < len(lines):
                                    uri = lines[i+1].strip()
                                    abs_uri = uri if uri.startswith("http") else urljoin(base_url, uri)
                                    if parsed_q not in qualities_dict:
                                        qualities_dict[parsed_q] = {
                                            "quality": parsed_q,
                                            "url": abs_uri,
                                            "type": "hls"
                                        }
                        continue  # Master mapped successfully, skip appending generic "Source"
                except Exception:
                    pass

            # Ignore any stream that would produce "Source" or empty label
            if not q_val or q_val.lower() in ["source", "auto"]:
                continue

            if q_val not in qualities_dict:
                qualities_dict[q_val] = {
                    "quality": q_val,
                    "url": v_url.replace(r"\/", "/"),
                    "type": "hls" if is_hls else "mp4"
                }

        qualities = list(qualities_dict.values())

        # Sort descending: 1080p -> 720p -> 480p -> 240p
        def get_res(q):
            num = re.sub(r'\D', '', q['quality'])
            return int(num) if num else 0

        qualities.sort(key=get_res, reverse=True)

        if not title or title == "Unknown Title":
            og = re.search(r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)["\']', html, re.I)
            if og:
                title = og.group(1).replace(" - Pornhub.com", "")

        return JSONResponse({
            "status": "success",
            "title": title.strip(),
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
    return {"status": "Proxy Online", "gateway": "Railway VexoStream API V4"}
