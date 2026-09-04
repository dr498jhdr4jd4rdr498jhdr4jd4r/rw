import os
import asyncio
import logging
from urllib.parse import quote, unquote
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
import httpx
from lxml import html
import yt_dlp
from cachetools import TTLCache

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Cache up to 2000 extractions for 2 hours to handle high traffic spikes
extraction_cache = TTLCache(maxsize=2000, ttl=7200)

# Limit ThreadPool to prevent OOM/CPU starvation under 1000+ concurrent loads
thread_pool = ThreadPoolExecutor(max_workers=50)

app = FastAPI(title="Media Extraction Engine")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

def extract_with_ytdlp(url: str) -> dict:
    """Blocking yt-dlp extraction logic to be run in a thread pool. STRICTLY HLS."""
    if url in extraction_cache:
        return extraction_cache[url]

    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
        'format': 'all',  # Fetch all formats to parse qualities
        'nocheckcertificate': True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        title = info.get('title', 'Unknown Video')
        thumbnail = info.get('thumbnail', '')
        
        # Aggregate all thumbnails
        thumbnails = [t['url'] for t in info.get('thumbnails', []) if 'url' in t]
        if thumbnail and thumbnail not in thumbnails:
            thumbnails.insert(0, thumbnail)

        qualities = []
        seen_qualities = set()

        # Parse strictly HLS formats
        for f in info.get('formats', []):
            height = f.get('height')
            if not height:
                continue
            
            q_label = f"{height}p"
            f_url = f.get('url', '')
            protocol = f.get('protocol', '')
            ext = f.get('ext', '')

            # STRICT FILTER: Only allow m3u8 / HLS streams
            if 'm3u8' not in protocol and ext != 'm3u8':
                continue

            if q_label not in seen_qualities:
                seen_qualities.add(q_label)
                qualities.append({
                    "quality": q_label,
                    "url": f_url,
                    "type": "hls"
                })

        # Sort qualities highest to lowest
        qualities.sort(key=lambda x: int(x['quality'].replace('p', '')), reverse=True)

        if not qualities:
            return {"status": "error", "error": "No valid HLS streams found", "url": url}

        result = {
            "status": "success",
            "title": title,
            "thumbnail": thumbnail or (thumbnails[0] if thumbnails else ""),
            "thumbnails": thumbnails,
            "streams": {"qualities": qualities},
            "url": url,
            "provider": "pornhub"
        }
        
        extraction_cache[url] = result
        return result

    except yt_dlp.utils.DownloadError as e:
        return {"status": "error", "error": f"Download error: {str(e)}", "url": url}
    except Exception as e:
        logger.error(f"yt-dlp extraction failed: {e}")
        return {"status": "error", "error": str(e), "url": url}


@app.get("/api/explore")
async def explore(q: str = "brazzers", page: int = 1):
    search_url = f"https://www.pornhub.com/video/search?search={quote(q)}&page={page}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Cookie': 'has_accepted_cookie=1; age_verified=1;'
    }
    
    try:
        # Use httpx for asynchronous, non-blocking requests
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(search_url, headers=headers)
            
        if resp.status_code != 200:
            return JSONResponse([])

        tree = html.fromstring(resp.content)
        items = tree.xpath('//li[contains(@class, "pcVideoListItem")]')
        videos = []

        for item in items:
            vkey = item.get("data-video-vkey") or (item.xpath('.//@data-video-vkey') or [None])[0]
            if not vkey:
                continue

            title_elem = item.xpath('.//span[@class="title"]//a/text() | .//a[contains(@class, "title")]/text() | .//img/@alt')
            title = title_elem[0].strip() if title_elem else "Unknown Video"

            raw_thumbs = item.xpath('.//img/@data-thumb_url | .//img/@data-mediumthumb | .//img/@data-image | .//img/@src')
            thumb = raw_thumbs[0] if raw_thumbs else ""
            
            # Skip placeholders
            if not thumb or "data:image" in thumb or "blank" in thumb:
                continue

            videos.append({
                "vkey": vkey,
                "title": title,
                "thumbnail": thumb,
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
    
    # Ensure it's a full URL if only viewkey is passed
    if "viewkey=" not in target_url and len(target_url) in [13, 15, 16] and "." not in target_url:
         target_url = f"https://www.pornhub.com/view_video.php?viewkey={target_url}"
         
    loop = asyncio.get_running_loop()
    # Offload the blocking yt-dlp task to the ThreadPoolExecutor
    res = await loop.run_in_executor(thread_pool, extract_with_ytdlp, target_url)
    return JSONResponse(res)


@app.get("/proxy-image")
async def fallback_proxy_image(url: str):
    target = unquote(url).strip()
    if target.startswith('//'):
        target = "https:" + target
        
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            req = await client.get(target)
            return StreamingResponse(
                (chunk async for chunk in req.aiter_bytes()),
                status_code=req.status_code,
                headers={
                    "Content-Type": req.headers.get("Content-Type", "image/jpeg"),
                    "Access-Control-Allow-Origin": "*"
                }
            )
    except Exception:
        return Response(status_code=404)

@app.get("/")
def health():
    return {"status": "Online", "engine": "yt-dlp Core (Strict HLS)", "concurrency": "async-threadpool"}
