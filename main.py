from contextlib import asynccontextmanager
from urllib.parse import unquote, quote, urljoin
import re
import json
import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

http_client: httpx.AsyncClient = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(20.0, connect=10.0),
        limits=httpx.Limits(max_connections=300, max_keepalive_connections=100)
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

PH_HEADERS = {
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
        r = await http_client.get(target_url, headers=PH_HEADERS)
        if r.status_code != 200:
            return JSONResponse([])

        html = r.text
        videos = []
        blocks = re.split(r'data-video-vkey="', html, flags=re.I)

        for block in blocks[1:]:
            try:
                block = block[:3500]
                vkey_match = re.search(r'^([a-z0-9]+)"?', block, re.I)
                title_match = re.search(r'(?:title|alt)="([^"]+)"', block, re.I)

                # Prioritized check for raw thumbnail paths (avoiding blank.gif/pixel.gif)
                thumb_match = (
                    re.search(r'data-thumb_url="([^"]+)"', block, re.I) or
                    re.search(r'data-mediumthumb="([^"]+)"', block, re.I) or
                    re.search(r'data-image="([^"]+)"', block, re.I) or
                    re.search(r'data-src="([^"]+)"', block, re.I) or
                    re.search(r'src="([^"]+\.(?:jpg|jpeg|png|webp)[^"]*)"', block, re.I)
                )

                dur_match = re.search(r'<var class="duration">([^<]+)<\/var>|<span class="duration">([^<]+)<\/span>', block, re.I)
                date_match = re.search(r'<var class="added">([^<]+)<\/var>', block, re.I)
                upload_date = date_match.group(1).strip() if date_match else ""

                if vkey_match and title_match and thumb_match:
                    vkey = vkey_match.group(1)
                    title = title_match.group(1).replace("&quot;", '"').replace("&amp;", "&").strip()
                    thumb = thumb_match.group(1).replace("&amp;", "&").strip()

                    if thumb.startswith('//'):
                        thumb = 'https:' + thumb
                    elif thumb.startswith('/'):
                        thumb = 'https://www.pornhub.com' + thumb

                    # Discard 1x1 tracking pixels
                    if 'data:image' in thumb or 'blank.gif' in thumb or 'pixel' in thumb or not thumb.startswith('http'):
                        continue

                    dur = "HD"
                    if dur_match:
                        raw_dur = dur_match.group(1) or dur_match.group(2)
                        if raw_dur:
                            dur = raw_dur.strip()

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

                if len(videos) >= 44:
                    break
            except Exception:
                continue

        return JSONResponse(videos)
    except Exception:
        return JSONResponse([])

@app.get("/api/extract")
async def extract(url: str):
    try:
        if not url:
            return JSONResponse({"status": "error", "error": "Missing URL parameter"})

        vkey_match = re.search(r'viewkey=([a-z0-9]+)', url, re.I) or re.search(r'embed/([a-z0-9]+)', url, re.I)
        if not vkey_match:
            return JSONResponse({"status": "error", "error": "Invalid viewkey"})

        vkey = vkey_match.group(1)
        target_url = f"https://www.pornhub.com/view_video.php?viewkey={vkey}"

        r = await http_client.get(target_url, headers=PH_HEADERS)
        html = r.text

        title = "Pornhub Video"
        poster = ""
        media_defs = []

        fv_match = (
            re.search(r'flashvars_\d+\s*=\s*(\{.*?\});', html, re.DOTALL) or
            re.search(r'flashvars\s*=\s*(\{.*?\});', html, re.DOTALL) or
            re.search(r'var\s+playerObjList\s*=\s*(\{.*?\});', html, re.DOTALL)
        )

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

        qualities = []
        seen_qualities = set()

        # Parse HLS master playlists only once
        for m in media_defs:
            if not isinstance(m, dict):
                continue

            v_url = m.get("videoUrl") or m.get("url")
            if not v_url:
                continue

            fmt = m.get("format", "")
            if fmt == "hls" or ".m3u8" in v_url:
                try:
                    m3_r = await http_client.get(v_url, headers=PH_HEADERS)
                    if m3_r.status_code == 200 and "#EXTM3U" in m3_r.text:
                        lines = m3_r.text.splitlines()
                        base_url = v_url[:v_url.rfind('/') + 1]

                        for i, line in enumerate(lines):
                            line = line.strip()
                            if line.startswith("#EXT-X-STREAM-INF:"):
                                res_match = re.search(r'RESOLUTION=(\d+x\d+)', line)
                                height = "0"
                                if res_match and 'x' in res_match.group(1):
                                    height = res_match.group(1).split('x')[1]

                                lbl = f"{height}p" if height.isdigit() and int(height) > 0 else ""

                                if i + 1 < len(lines) and not lines[i + 1].startswith("#"):
                                    uri = lines[i + 1].strip()
                                    abs_uri = uri if uri.startswith("http") else urljoin(base_url, uri)

                                    if not lbl or lbl == "0p":
                                        for res_guess in ["1080", "720", "480", "360", "240"]:
                                            if res_guess in abs_uri:
                                                lbl = f"{res_guess}p"
                                                break
                                        if not lbl:
                                            lbl = f"Stream {len(qualities) + 1}"

                                    if lbl not in seen_qualities:
                                        seen_qualities.add(lbl)
                                        qualities.append({"quality": lbl, "url": abs_uri})

                        # Successfully parsed master playlist, stop duplicate network calls
                        if qualities:
                            break
                except Exception:
                    pass

        # Sort highest resolution to lowest
        def sort_key(q):
            digits = re.sub(r'\D', '', q['quality'])
            return int(digits) if digits else 0

        qualities.sort(key=sort_key, reverse=True)

        if not title or title == "Pornhub Video":
            og = re.search(r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)["\']', html, re.I)
            if og:
                title = og.group(1).replace(" - Pornhub.com", "").strip()

        return JSONResponse({
            "status": "success",
            "title": title,
            "thumbnail": poster,
            "streams": {"qualities": qualities},
            "url": target_url,
            "provider": "pornhub"
        })
    except Exception as e:
        return JSONResponse({"status": "error", "error": f"Extraction failed: {str(e)}"})

@app.get("/")
def root():
    return {"status": "VexoStream Backend Online", "engine": "FastAPI Railway"}
