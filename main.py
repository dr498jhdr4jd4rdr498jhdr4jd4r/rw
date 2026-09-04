import os
import re
import json
import logging
from urllib.parse import urlparse, urljoin, quote, unquote
from contextlib import asynccontextmanager

import requests
from lxml import html
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ============================================================================
# CORE SCRAPER ENGINE (PORNHUB, XNXX, XVIDEOS, 3MOVS)
# ============================================================================

class VideoScraper:
    def __init__(self):
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Cookie': 'has_accepted_cookie=1; age_verified=1; platform=pc; accessAgeDisclaimerPH=1; accessAgeDisclaimer=1;'
        }
        self.proxies = {
            "http": os.getenv("HTTP_PROXY", ""),
            "https": os.getenv("HTTPS_PROXY", "")
        }

    def _fetch_page(self, url, referer=None):
        headers = self.headers.copy()
        if referer:
            headers['Referer'] = referer
            headers['Origin'] = referer.rstrip('/')
        else:
            parsed = urlparse(url)
            headers['Referer'] = f"{parsed.scheme}://{parsed.netloc}/"

        try:
            resp = requests.get(url, headers=headers, timeout=12, allow_redirects=True)
            if resp.status_code == 200:
                return resp
        except Exception:
            pass

        if self.proxies.get("http") or self.proxies.get("https"):
            try:
                return requests.get(url, headers=headers, proxies=self.proxies, timeout=20, allow_redirects=True)
            except Exception:
                pass
        return None

    def title_case(self, text):
        return re.sub(r"\b\w", lambda m: m.group(0).upper(), text.strip().lower()) if text else ""

    def clean_thumbnails(self, thumbs, base_url):
        clean = []
        seen = set()
        for t in thumbs:
            if not t or not isinstance(t, str):
                continue
            t = t.replace('\\/', '/').replace('&amp;', '&').strip().strip('\'"')
            if t.startswith('//'):
                t = "https:" + t
            elif t.startswith('/'):
                t = urljoin(base_url, t)

            if t.startswith('http'):
                t_lower = t.lower()
                if any(bad in t_lower for bad in ['favicon', 'logo', 'icon', 'banner', 'avatar', 'blank', 'pixel', 'sprite', 'timeline', '.vtt', '.gif']):
                    continue
                if not any(ext in t_lower for ext in ['.jpg', '.jpeg', '.png', '.webp', 'preview', 'thumb', 'poster', 'screenshots']):
                    continue

                if t not in seen:
                    seen.add(t)
                    clean.append(t)
        return clean

    def parse_hls_qualities(self, master_m3u8_url, referer=None):
        qualities = []
        try:
            resp = self._fetch_page(master_m3u8_url, referer)
            if resp and resp.status_code == 200:
                lines = resp.text.splitlines()
                base_url = master_m3u8_url.rsplit('/', 1)[0] + '/'
                for i, line in enumerate(lines):
                    line_clean = line.strip()
                    if line_clean.startswith("#EXT-X-STREAM-INF:"):
                        res_match = re.search(r"RESOLUTION=(\d+x\d+)", line_clean)
                        height = res_match.group(1).split("x")[1] if res_match and "x" in res_match.group(1) else "0"
                        quality_label = f"{height}p" if height.isdigit() and int(height) > 0 else "Adaptive Auto"

                        if i + 1 < len(lines) and not lines[i + 1].strip().startswith("#"):
                            stream_uri = lines[i + 1].strip()
                            stream_url = stream_uri if stream_uri.startswith('http') else urljoin(base_url, stream_uri)
                            qualities.append({"quality": quality_label, "url": stream_url})
        except Exception:
            pass

        qualities.sort(key=lambda x: int(re.search(r'(\d+)', x['quality']).group(1)) if re.search(r'(\d+)', x['quality']) else 0, reverse=True)
        if not qualities:
            qualities.append({"quality": "HLS Master", "url": master_m3u8_url})
        return qualities

    def _scrape_pornhub(self, url):
        viewkey = None
        if 'viewkey=' in url:
            viewkey = url.split('viewkey=')[1].split('&')[0]
        elif 'embed/' in url:
            viewkey = url.split('embed/')[1].split('?')[0]
        if not viewkey:
            return {"status": "error", "error": "Invalid viewkey identifier", "url": url}

        standard_url = f"https://www.pornhub.com/view_video.php?viewkey={viewkey}"
        title, poster, media_defs = "Pornhub Video", "", []
        raw_thumbs = set()
        page_text = ""

        try:
            resp = self._fetch_page(standard_url, referer="https://www.pornhub.com/")
            if not resp or resp.status_code != 200:
                return {"status": "error", "error": "Failed to fetch source page", "url": url}

            page_text = resp.text
            fv_match = re.search(r'var\s+flashvars_\d+\s*=\s*(\{.*?\});', page_text, re.DOTALL) or re.search(r'flashvars(?:_\d+)?\s*=\s*(\{.*?\});', page_text, re.DOTALL)
            if fv_match:
                try:
                    data = json.loads(fv_match.group(1))
                    media_defs = data.get('mediaDefinitions', [])
                    title = data.get('video_title') or title
                    poster = data.get('image_url') or data.get('thumb_url') or poster

                    if poster:
                        raw_thumbs.add(poster)
                    for _, v in data.items():
                        if isinstance(v, str) and v.startswith('http') and any(ext in v.lower() for ext in ['.jpg', '.jpeg', '.png', '.webp']):
                            raw_thumbs.add(v)
                except Exception:
                    pass

            if not media_defs:
                md_match = re.search(r'"mediaDefinitions"\s*:\s*(\[\{.*?\}\])', page_text, re.DOTALL)
                if md_match:
                    try:
                        media_defs = json.loads(md_match.group(1))
                    except Exception:
                        pass
        except Exception as e:
            return {"status": "error", "error": str(e), "url": url}

        if title == "Pornhub Video":
            og_match = re.search(r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)["\']', page_text, re.I)
            if og_match:
                title = og_match.group(1).replace(" - Pornhub.com", "").strip()

        clean_thumbs = self.clean_thumbnails(raw_thumbs, standard_url)
        if clean_thumbs and not poster:
            poster = clean_thumbs[0]

        stream_data = {"direct_mp4": {}, "qualities": []}
        seen_q = set()

        for m in media_defs:
            if not isinstance(m, dict):
                continue
            v_url = m.get('videoUrl') or m.get('url')
            if not v_url or not isinstance(v_url, str):
                continue

            fmt = m.get('format', '').lower()
            qual_raw = m.get('quality')
            qual = str(qual_raw[0]) if isinstance(qual_raw, list) and qual_raw else str(qual_raw or "auto")
            if qual == "[]" or not qual:
                qual = "auto"

            if fmt == 'mp4' or ('.mp4' in v_url and 'm3u8' not in v_url):
                q_label = f"{qual}p" if qual.isdigit() else qual.upper()
                stream_data["direct_mp4"][q_label] = v_url

            if fmt == 'hls' or '.m3u8' in v_url:
                parsed_streams = self.parse_hls_qualities(v_url, referer=standard_url)
                for pq in parsed_streams:
                    if pq["quality"] not in seen_q:
                        seen_q.add(pq["quality"])
                        stream_data["qualities"].append(pq)

        return {
            "status": "success",
            "title": title.strip(),
            "thumbnail": poster,
            "thumbnails": clean_thumbs,
            "streams": stream_data,
            "url": url,
            "provider": "pornhub"
        }

    def _scrape_xnxx_xvideos(self, url):
        try:
            resp = self._fetch_page(url)
            if not resp or resp.status_code != 200:
                return {"status": "error", "error": f"HTTP {resp.status_code if resp else 'No Response'}", "url": url}
        except Exception as e:
            return {"status": "error", "error": str(e), "url": url}

        page = resp.text
        tree = html.fromstring(resp.content)
        title_raw = tree.xpath('//meta[@property="og:title"]/@content')
        title = self.title_case(title_raw[0]) if title_raw else "Video Player"
        thumb_raw = tree.xpath('//meta[@property="og:image"]/@content')

        raw_thumbs = set()
        if thumb_raw:
            raw_thumbs.add(thumb_raw[0])
        for m in re.finditer(r'setThumbUrl(?:169|Slide|)?\(\s*[\'"](https?://[^\'"]+)[\'"]\s*\)', page):
            raw_thumbs.add(m.group(1))

        clean_thumbs = self.clean_thumbnails(raw_thumbs, url)
        thumbnail = clean_thumbs[0] if clean_thumbs else ""

        stream_data = {"direct_mp4": {}, "qualities": []}
        hls_match = re.search(r'(?:setVideoHLS|html5player\.setVideoHLS)\(\s*[\'"]([^\'"]+)[\'"]\s*\)', page)
        high_match = re.search(r'(?:setVideoUrlHigh|html5player\.setVideoUrlHigh)\(\s*[\'"]([^\'"]+)[\'"]\s*\)', page)
        low_match = re.search(r'(?:setVideoUrlLow|html5player\.setVideoUrlLow)\(\s*[\'"]([^\'"]+)[\'"]\s*\)', page)

        if hls_match:
            stream_data["qualities"] = self.parse_hls_qualities(hls_match.group(1), referer=url)
        if high_match:
            stream_data["direct_mp4"]["720p"] = high_match.group(1)
        if low_match:
            stream_data["direct_mp4"]["360p"] = low_match.group(1)

        return {
            "status": "success",
            "title": title.strip(),
            "thumbnail": thumbnail,
            "thumbnails": clean_thumbs,
            "streams": stream_data,
            "url": url,
            "provider": "xnxx" if "xnxx" in url else "xvideos"
        }

    def _scrape_3movs(self, url):
        try:
            resp = self._fetch_page(url)
            if not resp or resp.status_code != 200:
                return {"status": "error", "error": f"HTTP {resp.status_code if resp else 'No Response'}", "url": url}
        except Exception as e:
            return {"status": "error", "error": str(e), "url": url}

        page = resp.text
        tree = html.fromstring(resp.content)

        title_raw = tree.xpath('//meta[@property="og:title"]/@content')
        title = self.title_case(title_raw[0]) if title_raw else "Video Player"
        if title == "Video Player":
            title_tag = tree.xpath('//title/text()')
            if title_tag:
                title = title_tag[0].split('|')[0].split('-')[0].strip()

        raw_thumbs = set()
        for meta in tree.xpath('//meta[@property="og:image"]/@content | //meta[@name="twitter:image"]/@content'):
            raw_thumbs.add(meta)
        for poster in tree.xpath('//video/@poster | //div[contains(@id, "player")]//img/@src'):
            raw_thumbs.add(poster)
        for m in re.finditer(r'poster(?:Image)?\s*[:=]\s*["\']([^"\']+)["\']', page, re.I):
            raw_thumbs.add(m.group(1))

        clean_thumbs = self.clean_thumbnails(raw_thumbs, url)
        thumbnail = clean_thumbs[0] if clean_thumbs else ""

        stream_data = {"direct_mp4": {}, "qualities": []}
        candidates = []

        player_source_elements = tree.xpath('//div[contains(@id, "player") or contains(@class, "player")]//video//source | //video//source')
        for s in player_source_elements:
            src = s.get('src')
            if src:
                candidates.append(src)

        if not candidates:
            for m in re.finditer(r'(?:video_url|video_alt_url|src|file)\s*[:=]\s*["\']([^"\']+)["\']', page):
                candidates.append(m.group(1))

        for raw_src in set(candidates):
            full_src = raw_src.replace('\\/', '/').strip()
            if full_src.startswith('//'):
                full_src = "https:" + full_src
            elif full_src.startswith('/'):
                full_src = urljoin(url, full_src)

            if '.m3u8' in full_src:
                for q in self.parse_hls_qualities(full_src, referer=url):
                    stream_data["qualities"].append(q)
            elif '.mp4' in full_src:
                res_match = re.search(r'(\d{3,4})[pP]?', full_src)
                label = f"{res_match.group(1)}p" if res_match else "Direct MP4"
                stream_data["direct_mp4"][label] = full_src

        return {
            "status": "success",
            "title": title.strip(),
            "thumbnail": thumbnail,
            "thumbnails": clean_thumbs,
            "streams": stream_data,
            "url": url,
            "provider": "3movs"
        }

    def extract(self, url):
        m = re.search(r"\[([a-z0-9]{6,8})\]\.[^.]+$", url, re.I) or re.match(r"^([a-z0-9]{6,8})_.+", url, re.I)
        if m:
            url = f"https://www.xnxx.com/video-{m.group(1)}/x"

        if 'pornhub' in url:
            return self._scrape_pornhub(url)
        elif 'xnxx' in url or 'xvideos' in url:
            return self._scrape_xnxx_xvideos(url)
        elif '3movs' in url:
            return self._scrape_3movs(url)
        return {"status": "error", "error": "Unsupported provider"}

scraper = VideoScraper()

# ============================================================================
# FASTAPI CORE APPLICATION
# ============================================================================

@asynccontextmanager
async def lifespan_context(app: FastAPI):
    yield

app = FastAPI(title="VexoStream Enterprise Core", lifespan=lifespan_context)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/api/explore")
def explore(q: str = "brazzers", page: int = 1):
    try:
        search_url = f"https://www.pornhub.com/video/search?search={quote(q)}&page={page}"
        resp = scraper._fetch_page(search_url, referer="https://www.pornhub.com/")
        if not resp or resp.status_code != 200:
            return JSONResponse([])

        tree = html.fromstring(resp.content)
        items = tree.xpath('//li[contains(@class, "pcVideoListItem")]')
        videos = []
        search_words = [w.strip().lower() for w in q.split() if w.strip()]

        for item in items:
            vkey = item.get("data-video-vkey")
            if not vkey:
                vkey_attr = item.xpath('.//@data-video-vkey')
                if vkey_attr:
                    vkey = vkey_attr[0]

            if not vkey:
                continue

            title_elem = item.xpath('.//span[@class="title"]//a/text() | .//a[contains(@class, "title")]/text()')
            if not title_elem:
                title_elem = item.xpath('.//img/@alt')
            title = title_elem[0].strip() if title_elem else "Unknown Video"

            # Strict Case-Insensitive Filter: Every keyword must be in the title
            title_lower = title.lower()
            if search_words and not all(w in title_lower for w in search_words):
                continue

            # Core Thumbnail Scanner: Extract and clean immediately
            raw_thumbs = item.xpath('.//img/@data-thumb_url | .//img/@data-mediumthumb | .//img/@data-image | .//img/@data-src | .//img/@src')
            clean_thumbs = scraper.clean_thumbnails(raw_thumbs, "https://www.pornhub.com/")
            if not clean_thumbs:
                continue
            thumb = clean_thumbs[0]

            dur_elem = item.xpath('.//var[@class="duration"]/text() | .//span[@class="duration"]/text()')
            duration = dur_elem[0].strip() if dur_elem else "HD"

            date_elem = item.xpath('.//var[@class="added"]/text()')
            date = date_elem[0].strip() if date_elem else ""

            videos.append({
                "vkey": vkey,
                "title": title,
                "thumbnail": thumb,
                "duration": duration,
                "date": date,
                "url": f"https://www.pornhub.com/view_video.php?viewkey={vkey}",
                "provider": "pornhub"
            })

            if len(videos) >= 44:
                break

        return JSONResponse(videos)
    except Exception as e:
        logger.error(f"Explore error: {e}")
        return JSONResponse([])

@app.get("/api/extract")
def extract_endpoint(url: str):
    if not url:
        return JSONResponse({"status": "error", "error": "Missing URL"})
    res = scraper.extract(unquote(url))
    return JSONResponse(res)

@app.get("/proxy-image")
def fallback_proxy_image(url: str):
    target = unquote(url).strip()
    if target.startswith('//'):
        target = "https:" + target
    try:
        req = requests.get(target, headers=scraper.headers, stream=True, timeout=15)
        return StreamingResponse(req.iter_content(chunk_size=8192), status_code=req.status_code, headers={"Content-Type": req.headers.get("Content-Type", "image/jpeg"), "Access-Control-Allow-Origin": "*"})
    except Exception:
        return Response(status_code=404)

@app.get("/")
def health():
    return {"status": "Online", "engine": "VexoStream Core Scraper"}
