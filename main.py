import os
import re
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, unquote, urljoin
from contextlib import asynccontextmanager

import httpx
import yt_dlp
from lxml import html
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Powerful ThreadPool to handle heavy concurrent extractions without blocking the main event loop
executor = ThreadPoolExecutor(max_workers=100)

# Async client for lightweight requests (like search exploration)
client = httpx.AsyncClient(
    headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Cookie': 'has_accepted_cookie=1; age_verified=1; platform=pc;'
    },
    timeout=15.0,
    follow_redirects=True
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await client.aclose()
    executor.shutdown()

app = FastAPI(title="VexoStream Enterprise Core Async", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

def clean_thumbnails(thumbs, base_url="https://www.pornhub.com/"):
    clean = []
    seen = set()
    for t in thumbs:
        if not t or not isinstance(t, str): continue
        t = t.replace('\\/', '/').replace('&amp;', '&').strip().strip('\'"')
        if t.startswith('//'): t = "https:" + t
        elif t.startswith('/'): t = urljoin(base_url, t)
        
        if t.startswith('http'):
            t_lower = t.lower()
            if any(bad in t_lower for bad in ['favicon', 'logo', 'icon', 'banner', 'avatar', 'blank', 'pixel']):
                continue
            if not any(ext in t_lower for ext in ['.jpg', '.jpeg', '.png', '.webp', 'preview', 'thumb', 'poster']):
                continue
            if t not in seen:
                seen.add(t)
                clean.append(t)
    return clean

def sync_extract_video(url: str):
    """Uses yt-dlp to bypass protections, tokens, and dynamically extract all streams."""
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'format': 'all', # Grab all available formats
        'nocheckcertificate': True,
        # Uncomment below if you need proxies to bypass region blocks
        # 'proxy': os.getenv("HTTP_PROXY", ""),
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception as e:
        logger.error(f"yt-dlp extraction failed for {url}: {e}")
        return None

@app.get("/api/explore")
async def explore(q: str = "brazzers", page: int = 1):
    try:
        search_url = f"https://www.pornhub.com/video/search?search={quote(q)}&page={page}"
        resp = await client.get(search_url)
        if resp.status_code != 200:
            return JSONResponse([])

        tree = html.fromstring(resp.content)
        items = tree.xpath('//li[contains(@class, "pcVideoListItem")]')
        videos = []
        search_words = [w.strip().lower() for w in q.split() if w.strip()]

        for item in items:
            vkey = item.get("data-video-vkey") or (item.xpath('.//@data-video-vkey') or [None])[0]
            if not vkey or any(v["vkey"] == vkey for v in videos):
                continue

            title_elem = item.xpath('.//span[@class="title"]//a/text() | .//a[contains(@class, "title")]/text() | .//img/@alt')
            title = title_elem[0].strip() if title_elem else "Unknown Video"
            
            if search_words and not all(w in title.lower() for w in search_words):
                continue

            raw_thumbs = item.xpath('.//img/@data-thumb_url | .//img/@data-mediumthumb | .//img/@data-image | .//img/@data-src | .//img/@src')
            clean_thumbs = clean_thumbnails(raw_thumbs)
            if not clean_thumbs:
                continue

            videos.append({
                "vkey": vkey,
                "title": title,
                "thumbnail": clean_thumbs[0],
                "url": f"https://www.pornhub.com/view_video.php?viewkey={vkey}",
                "provider": "pornhub"
            })
            if len(videos) >= 24:
                break

        return JSONResponse(videos)
    except Exception as e:
        logger.error(f"Explore error: {e}")
        return JSONResponse([])

@app.get("/api/extract")
async def extract_endpoint(url: str):
    if not url:
        return JSONResponse({"status": "error", "error": "Missing URL"})
    
    target_url = unquote(url)
    
    # Run heavy extraction in threadpool so 1000 users don't crash the event loop
    loop = asyncio.get_event_loop()
    info = await loop.run_in_executor(executor, sync_extract_video, target_url)
    
    if not info:
        return JSONResponse({"status": "error", "error": "Video not found, region locked, or extraction failed.", "url": target_url})

    qualities = []
    seen_q = set()
    
    for f in info.get('formats', []):
        height = f.get('height')
        if not height: 
            continue
            
        q_label = f"{height}p"
        proto = f.get('protocol', '')
        ext = f.get('ext', '')

        # Identify stream type
        if proto in ['m3u8_native', 'm3u8']:
            v_type = 'hls'
        elif ext == 'mp4' and proto in ['http', 'https']:
            v_type = 'mp4'
        else:
            continue

        if q_label not in seen_q:
            qualities.append({
                "quality": q_label,
                "url": f.get('url'),
                "type": v_type
            })
            seen_q.add(q_label)

    # Sort qualities from highest to lowest
    qualities.sort(key=lambda x: int(x['quality'].replace('p', '')), reverse=True)

    if not qualities:
        return JSONResponse({"status": "error", "error": "No streamable qualities found.", "url": target_url})

    thumb = info.get('thumbnail', '')
    
    res = {
        "status": "success",
        "title": info.get('title', 'Unknown Video'),
        "thumbnail": thumb,
        "thumbnails": [thumb] if thumb else [],
        "streams": {"qualities": qualities},
        "url": target_url,
        "provider": "pornhub"
    }
    return JSONResponse(res)

@app.get("/proxy-image")
async def fallback_proxy_image(url: str):
    target = unquote(url).strip()
    if target.startswith('//'): target = "https:" + target
    try:
        req = await client.get(target)
        return Response(
            content=req.content,
            status_code=req.status_code,
            headers={
                "Content-Type": req.headers.get("Content-Type", "image/jpeg"),
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "public, max-age=86400"
            }
        )
    except Exception:
        return Response(status_code=404)

@app.get("/")
def health():
    return {"status": "Online", "engine": "VexoStream Dedicated Core Async"}
