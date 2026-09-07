from __future__ import annotations

import asyncio
import base64
import html
import ipaddress
import json
import os
import re
import shutil
import tempfile
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, Field
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

try:
    import yt_dlp
except Exception:  # lets the UI boot even if dependencies are not installed yet
    yt_dlp = None

try:
    import instaloader
except Exception:
    instaloader = None

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"
TEMP_ROOT = Path(os.getenv("TEMP_DIR", "/tmp/reelfetch"))
TEMP_ROOT.mkdir(parents=True, exist_ok=True)

APP_NAME = os.getenv("APP_NAME", "ReelFetch")
SECRET_KEY = os.getenv("SECRET_KEY", "dev-change-me-before-production")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
MAX_DURATION_SECONDS = int(os.getenv("MAX_DURATION_SECONDS", "7200"))
MAX_DOWNLOAD_MB = int(os.getenv("MAX_DOWNLOAD_MB", "500"))
TOKEN_TTL_SECONDS = int(os.getenv("TOKEN_TTL_SECONDS", "900"))
ANALYZE_LIMIT_PER_HOUR = int(os.getenv("ANALYZE_LIMIT_PER_HOUR", "80"))
DOWNLOAD_LIMIT_PER_HOUR = int(os.getenv("DOWNLOAD_LIMIT_PER_HOUR", "40"))
COOKIES_PATH = os.getenv("COOKIES_PATH", "").strip()
COOKIES_B64 = os.getenv("INSTAGRAM_COOKIES_B64", "").strip()

ALLOWED_INPUT_HOSTS = {
    "instagram.com", "www.instagram.com", "m.instagram.com", "instagr.am",
    "facebook.com", "www.facebook.com", "m.facebook.com", "fb.watch",
}
ALLOWED_MEDIA_SUFFIXES = (
    ".cdninstagram.com", ".fbcdn.net", ".facebook.com", ".akamaihd.net",
    ".akamaized.net", ".fna.fbcdn.net",
)

serializer = URLSafeTimedSerializer(SECRET_KEY, salt="reelfetch-media-v1")
app = FastAPI(title=APP_NAME, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
env = Environment(loader=FileSystemLoader(TEMPLATES_DIR), autoescape=select_autoescape(["html", "xml"]))

# Basic in-memory throttling. Replace with Redis for multi-instance production.
_rate_buckets: dict[str, deque[float]] = defaultdict(deque)


def _client_ip(request: Request) -> str:
    cf = request.headers.get("cf-connecting-ip")
    if cf:
        return cf.strip()
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",", 1)[0].strip()
    return request.client.host if request.client else "unknown"


def _check_rate(key: str, limit: int) -> None:
    now = time.time()
    q = _rate_buckets[key]
    while q and q[0] < now - 3600:
        q.popleft()
    if len(q) >= limit:
        raise HTTPException(429, "Too many requests. Please try again later.")
    q.append(now)


def _normalize_url(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        raise HTTPException(400, "Paste a valid Instagram or Facebook URL.")
    if len(raw) > 2000:
        raise HTTPException(400, "URL is too long.")
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    p = urlparse(raw)
    host = (p.hostname or "").lower().rstrip(".")
    if host not in ALLOWED_INPUT_HOSTS:
        raise HTTPException(400, "Only Instagram and Facebook public links are supported.")
    if p.username or p.password:
        raise HTTPException(400, "Invalid URL.")
    # Reject literal private/local IPs if a future allowlist entry ever becomes numeric.
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            raise HTTPException(400, "Invalid URL host.")
    except ValueError:
        pass
    return p._replace(fragment="").geturl()


def _media_url_allowed(url: str) -> bool:
    try:
        p = urlparse(url)
        host = (p.hostname or "").lower()
        if p.scheme != "https" or not host:
            return False
        return host.endswith(ALLOWED_MEDIA_SUFFIXES)
    except Exception:
        return False


def _ensure_cookie_file() -> str | None:
    if COOKIES_PATH and Path(COOKIES_PATH).is_file():
        return COOKIES_PATH
    if not COOKIES_B64:
        return None
    target = TEMP_ROOT / "instagram-cookies.txt"
    if not target.exists():
        try:
            target.write_bytes(base64.b64decode(COOKIES_B64))
            os.chmod(target, 0o600)
        except Exception as exc:
            raise RuntimeError("INSTAGRAM_COOKIES_B64 is not valid base64") from exc
    return str(target)


def _ydl_base_opts() -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 20,
        "retries": 2,
        "fragment_retries": 2,
        "extractor_retries": 2,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Linux; Android 15; Mobile) AppleWebKit/537.36 Chrome/139.0 Mobile Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        },
    }
    cookie = _ensure_cookie_file()
    if cookie:
        opts["cookiefile"] = cookie
    return opts


def _flatten_info(info: dict[str, Any]) -> dict[str, Any]:
    if info.get("_type") in {"playlist", "multi_video"} and info.get("entries"):
        entries = [e for e in info.get("entries") or [] if e]
        if len(entries) == 1:
            return entries[0]
        # For carousel-like extraction, keep first video for metadata, but caller may expose entries.
        info = dict(info)
        info["_entries_clean"] = entries
    return info


def _format_options(info: dict[str, Any]) -> list[dict[str, Any]]:
    formats = info.get("formats") or []
    best_by_height: dict[int, dict[str, Any]] = {}
    for f in formats:
        if not isinstance(f, dict):
            continue
        if f.get("vcodec") in {None, "none"}:
            continue
        h = f.get("height")
        if not isinstance(h, (int, float)) or h <= 0:
            continue
        h = int(h)
        prev = best_by_height.get(h)
        score = (1 if f.get("acodec") not in {None, "none"} else 0, f.get("tbr") or 0)
        prev_score = (-1, -1) if prev is None else (1 if prev.get("acodec") not in {None, "none"} else 0, prev.get("tbr") or 0)
        if prev is None or score > prev_score:
            best_by_height[h] = f
    heights = sorted(best_by_height.keys(), reverse=True)
    if not heights:
        # Instagram often exposes one combined format without explicit height.
        if info.get("url") or formats:
            return [{"label": "Original", "height": None, "ext": info.get("ext") or "mp4"}]
        return []
    out = []
    for h in heights[:6]:
        f = best_by_height[h]
        out.append({
            "label": f"{h}p",
            "height": h,
            "ext": f.get("ext") or "mp4",
            "fps": f.get("fps"),
            "filesize": f.get("filesize") or f.get("filesize_approx"),
        })
        return out


def _best_direct_video_url(info: dict[str, Any]) -> str | None:
    candidates = []

    direct = info.get("url")
    if direct and _media_url_allowed(direct):
        candidates.append({
            "url": direct,
            "audio": info.get("acodec") not in {None, "none"},
            "height": info.get("height") or 0,
            "tbr": info.get("tbr") or 0,
            "ext": info.get("ext") or "",
        })

    for f in info.get("formats") or []:
        if not isinstance(f, dict):
            continue

        url = f.get("url")

        if not url or not _media_url_allowed(url):
            continue

        if f.get("vcodec") in {None, "none"}:
            continue

        candidates.append({
            "url": url,
            "audio": f.get("acodec") not in {None, "none"},
            "height": f.get("height") or 0,
            "tbr": f.get("tbr") or 0,
            "ext": f.get("ext") or "",
        })

    if not candidates:
        return None

    candidates.sort(
        key=lambda x: (
            1 if x["audio"] else 0,
            1 if x["ext"] == "mp4" else 0,
            x["height"],
            x["tbr"],
        ),
        reverse=True,
    )

    return candidates[0]["url"]


def _safe_title(info: dict[str, Any]) -> str:


def _safe_title(info: dict[str, Any]) -> str:
    title = info.get("title") or info.get("description") or "Instagram media"
    title = re.sub(r"\s+", " ", str(title)).strip()
    return title[:160]


def _yt_extract(url: str) -> dict[str, Any]:
    if yt_dlp is None:
        raise RuntimeError("yt-dlp dependency is not installed")
    opts = _ydl_base_opts()
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise RuntimeError("No downloadable media found")
    return _flatten_info(info)


def _shortcode_from_url(url: str) -> str | None:
    m = re.search(r"instagram\.com/(?:p|reel|tv)/([^/?#]+)", url, re.I)
    return m.group(1) if m else None


def _instaloader_fallback(url: str) -> dict[str, Any] | None:
    """Best-effort image/carousel fallback. Instagram can rate-limit this endpoint."""
    if instaloader is None:
        return None
    shortcode = _shortcode_from_url(url)
    if not shortcode:
        return None
    loader = instaloader.Instaloader(
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        save_metadata=False,
        compress_json=False,
    )
    cookie_file = _ensure_cookie_file()
    if cookie_file:
        try:
            import http.cookiejar
            jar = http.cookiejar.MozillaCookieJar(cookie_file)
            jar.load(ignore_discard=True, ignore_expires=True)
            loader.context.update_cookies({c.name: c.value for c in jar if "instagram.com" in c.domain})
        except Exception:
            pass
    post = instaloader.Post.from_shortcode(loader.context, shortcode)
    media: list[dict[str, Any]] = []
    if getattr(post, "typename", "") == "GraphSidecar":
        for node in post.get_sidecar_nodes():
            if getattr(node, "is_video", False) and getattr(node, "video_url", None):
                media.append({"type": "video", "url": node.video_url, "thumbnail": node.display_url})
            else:
                media.append({"type": "image", "url": node.display_url})
    elif getattr(post, "is_video", False):
        media.append({"type": "video", "url": post.video_url, "thumbnail": post.url})
    else:
        media.append({"type": "image", "url": post.url})
    media = [m for m in media if m.get("url") and _media_url_allowed(m["url"])]
    if not media:
        return None
    return {
        "title": (getattr(post, "caption", None) or "Instagram post")[:160],
        "uploader": getattr(post, "owner_username", None),
        "thumbnail": media[0].get("thumbnail") or media[0].get("url"),
        "duration": getattr(post, "video_duration", None) if getattr(post, "is_video", False) else None,
        "media": media,
        "formats": [{"label": "Original", "height": None, "ext": "mp4"}] if any(m["type"] == "video" for m in media) else [],
        "extractor": "instaloader-fallback",
    }


class AnalyzeBody(BaseModel):
    url: str = Field(min_length=3, max_length=2000)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' https: data:; media-src 'self' https:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'self'"
    )
    return response


PAGE_COPY = {
    "reels": ("Instagram Reels Downloader", "Save public Reels in the best available quality."),
    "video": ("Instagram Video Downloader", "Download public Instagram videos without installing an app."),
    "photo": ("Instagram Photo Downloader", "Save public post images and thumbnails when available."),
    "audio": ("Instagram Audio Downloader", "Extract MP3 audio from a public Reel or video."),
    "facebook": ("Facebook Video Downloader", "Save public Facebook videos in available qualities."),
}


def _render_home(mode: str, request: Request) -> HTMLResponse:
    title, subtitle = PAGE_COPY[mode]
    template = env.get_template("index.html")
    return HTMLResponse(template.render(
        request=request,
        app_name=APP_NAME,
        mode=mode,
        title=title,
        subtitle=subtitle,
        canonical=(PUBLIC_BASE_URL + request.url.path) if PUBLIC_BASE_URL else "",
    ))


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return _render_home("reels", request)


@app.get("/video", response_class=HTMLResponse)
def video(request: Request):
    return _render_home("video", request)


@app.get("/photo", response_class=HTMLResponse)
def photo(request: Request):
    return _render_home("photo", request)


@app.get("/audio", response_class=HTMLResponse)
def audio(request: Request):
    return _render_home("audio", request)


@app.get("/facebook", response_class=HTMLResponse)
def facebook(request: Request):
    return _render_home("facebook", request)


@app.get("/stories")
def stories():
    # Stories often require auth and are intentionally not exposed as anonymous surveillance functionality.
    return RedirectResponse(url="/?notice=stories", status_code=302)


@app.get("/profile")
def profile():
    return RedirectResponse(url="/?notice=profile", status_code=302)


@app.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request):
    return HTMLResponse(env.get_template("legal.html").render(app_name=APP_NAME, page="Privacy Policy", kind="privacy", canonical=(PUBLIC_BASE_URL + "/privacy") if PUBLIC_BASE_URL else ""))


@app.get("/terms", response_class=HTMLResponse)
def terms(request: Request):
    return HTMLResponse(env.get_template("legal.html").render(app_name=APP_NAME, page="Terms of Service", kind="terms", canonical=(PUBLIC_BASE_URL + "/terms") if PUBLIC_BASE_URL else ""))


@app.get("/dmca", response_class=HTMLResponse)
def dmca(request: Request):
    return HTMLResponse(env.get_template("legal.html").render(app_name=APP_NAME, page="Copyright & DMCA", kind="dmca", canonical=(PUBLIC_BASE_URL + "/dmca") if PUBLIC_BASE_URL else ""))


@app.get("/contact", response_class=HTMLResponse)
def contact(request: Request):
    return HTMLResponse(env.get_template("legal.html").render(app_name=APP_NAME, page="Contact", kind="contact", canonical=(PUBLIC_BASE_URL + "/contact") if PUBLIC_BASE_URL else ""))


@app.get("/health")
def health():
    return {"ok": True, "yt_dlp": yt_dlp is not None, "instaloader": instaloader is not None, "cookies": bool(_ensure_cookie_file())}


@app.post("/api/analyze")
async def analyze(body: AnalyzeBody, request: Request):
    _check_rate(f"a:{_client_ip(request)}", ANALYZE_LIMIT_PER_HOUR)
    url = _normalize_url(body.url)
    result: dict[str, Any] | None = None
    primary_error = ""
    try:
        info = await asyncio.wait_for(asyncio.to_thread(_yt_extract, url), timeout=40)
        duration = info.get("duration")
        if duration and duration > MAX_DURATION_SECONDS:
            raise HTTPException(413, f"Media longer than {MAX_DURATION_SECONDS // 60} minutes is not supported.")
        entries = info.get("_entries_clean") or []
        media = []
        # Direct entry URLs are included only when they point at known Meta CDNs.
        for e in entries:
            direct = e.get("url")
            if direct and _media_url_allowed(direct):
                media.append({"type": "video", "url": direct, "thumbnail": e.get("thumbnail")})
        result = {
            "title": _safe_title(info),
            "uploader": info.get("uploader") or info.get("channel") or info.get("uploader_id"),
            "thumbnail": info.get("thumbnail"),
            "duration": duration,
            "formats": _format_options(info),
            "media": media,
            "direct_video": _best_direct_video_url(info),
            "extractor": info.get("extractor_key") or info.get("extractor") or "yt-dlp",
        }
    except HTTPException:
        raise
    except Exception as exc:
        primary_error = str(exc)

    # yt-dlp intentionally does not handle image-only Instagram posts; try a best-effort fallback.
    if not result or (not result.get("formats") and "instagram.com" in url):
        try:
            fb = await asyncio.wait_for(asyncio.to_thread(_instaloader_fallback, url), timeout=30)
            if fb:
                result = fb
        except Exception:
            pass

    
        message = "Could not fetch this public post right now. Instagram may be rate-limiting the server, the post may require login, or the URL may be unavailable."
        if os.getenv("DEBUG_ERRORS") == "1" and primary_error:
            message += " " + primary_error[:240]
        raise HTTPException(422, message)

    token_payload = {
        "url": url,
        "title": result.get("title") or "media",
        "heights": [f.get("height") for f in result.get("formats", [])],
        "thumbnail": result.get("thumbnail") if _media_url_allowed(result.get("thumbnail") or "") else None, 
        "direct_video": (
    result.get("direct_video")
    if _media_url_allowed(result.get("direct_video") or "")
    else None
),
        "media": [m for m in (result.get("media") or []) if _media_url_allowed(m.get("url") or "")][:20],
    }
    token = serializer.dumps(token_payload)

    return {
        "ok": True,
        "token": token,
        "title": result.get("title"),
        "uploader": result.get("uploader"),
        "thumbnail": f"/api/preview?token={token}" if token_payload.get("thumbnail") else result.get("thumbnail"),
        "duration": result.get("duration"),
        "formats": result.get("formats") or [],
        "media": [
            {"type": m.get("type", "media"), "download": f"/api/download/direct?token={token}&index={i}"}
            for i, m in enumerate(token_payload["media"])
        ],
        "audio": bool(result.get("formats")),
        "notice": "Only download content you own or have permission to save.",
    }


def _load_token(token: str) -> dict[str, Any]:
    try:
        data = serializer.loads(token, max_age=TOKEN_TTL_SECONDS)
        if not isinstance(data, dict):
            raise BadSignature("invalid")
        return data
    except SignatureExpired:
        raise HTTPException(410, "This download link expired. Analyze the post again.")
    except BadSignature:
        raise HTTPException(400, "Invalid download token.")


def _download_with_ytdlp(url: str, title: str, height: int | None, audio_only: bool) -> Path:
    if yt_dlp is None:
        raise RuntimeError("yt-dlp dependency is not installed")
    tmp = Path(tempfile.mkdtemp(prefix="rf-", dir=TEMP_ROOT))
    outtmpl = str(tmp / "%(title).100s-%(id)s.%(ext)s")
    opts = _ydl_base_opts()
    opts.update({
        "skip_download": False,
        "outtmpl": outtmpl,
        "restrictfilenames": True,
        "noplaylist": True,
        "overwrites": True,
        "continuedl": True,
        "max_filesize": MAX_DOWNLOAD_MB * 1024 * 1024,
    })
    if audio_only:
        opts.update({
            "format": "bestaudio/best",
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
        })
    elif height:
        opts.update({
            "format": f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/bestvideo[height<={height}]/best",
            "merge_output_format": "mp4",
        })
    else:
        opts.update({"format": "bestvideo+bestaudio/best", "merge_output_format": "mp4"})

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        files = [p for p in tmp.iterdir() if p.is_file() and p.suffix not in {".part", ".ytdl", ".json"}]
        if not files:
            raise RuntimeError("Download completed but no media file was produced")
        file = max(files, key=lambda p: p.stat().st_size)
        if file.stat().st_size > MAX_DOWNLOAD_MB * 1024 * 1024:
            raise HTTPException(413, f"File exceeds the {MAX_DOWNLOAD_MB} MB server limit.")
        return file
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


@app.get("/api/download/video")
async def download_video(token: str, request: Request, height: str = "original"):
    _check_rate(f"d:{_client_ip(request)}", DOWNLOAD_LIMIT_PER_HOUR)
    data = _load_token(token)
        if height == "original":
        direct_video = data.get("direct_video")

        if direct_video and _media_url_allowed(direct_video):
            source_host = (urlparse(data.get("url", "")).hostname or "").lower()

            if "facebook.com" in source_host or source_host == "fb.watch":
                referer = "https://www.facebook.com/"
            else:
                referer = "https://www.instagram.com/"

            try:
                return await _proxy_media(
                    direct_video,
                    f"{APP_NAME.lower()}-video.mp4",
                    referer=referer,
                )
            except HTTPException:
                pass
    allowed = data.get("heights") or []
    selected: int | None
    if height == "original":
        selected = None
    else:
        try:
            selected = int(height)
        except ValueError:
            raise HTTPException(400, "Invalid quality.")
        if selected not in allowed:
            raise HTTPException(400, "That quality was not offered for this post.")
    try:
        file = await asyncio.wait_for(asyncio.to_thread(_download_with_ytdlp, data["url"], data.get("title", "media"), selected, False), timeout=210)
    except asyncio.TimeoutError:
        raise HTTPException(504, "Download timed out. Try a lower quality or try again later.")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, "The media host refused the download or changed its response. Analyze the post again.") from exc
    bg = BackgroundTasks()
    bg.add_task(shutil.rmtree, file.parent, True)
    return FileResponse(file, filename=file.name, media_type="video/mp4", background=bg)


@app.get("/api/download/audio")
async def download_audio(token: str, request: Request):
    _check_rate(f"d:{_client_ip(request)}", DOWNLOAD_LIMIT_PER_HOUR)
    data = _load_token(token)
    try:
        file = await asyncio.wait_for(asyncio.to_thread(_download_with_ytdlp, data["url"], data.get("title", "audio"), None, True), timeout=210)
    except asyncio.TimeoutError:
        raise HTTPException(504, "Audio conversion timed out.")
    except Exception as exc:
        raise HTTPException(502, "Could not extract audio from this media.") from exc
    bg = BackgroundTasks()
    bg.add_task(shutil.rmtree, file.parent, True)
    return FileResponse(file, filename=file.name, media_type="audio/mpeg", background=bg)


async def _proxy_media(
    url: str,
    attachment_name: str | None = None,
    referer: str = "https://www.instagram.com/",
):
    if not _media_url_allowed(url):
        raise HTTPException(400, "Media URL is not allowed.")
    headers = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 15; Mobile) AppleWebKit/537.36 Chrome/139.0 Mobile Safari/537.36",
    "Accept": "*/*",
    "Referer": referer,
}
    client = httpx.AsyncClient(timeout=30, follow_redirects=True, headers=headers)
    try:
        r = await client.send(client.build_request("GET", url), stream=True)
        if r.status_code >= 400:
            await r.aclose(); await client.aclose()
            raise HTTPException(502, "Media CDN refused the request.")
        final_host = (urlparse(str(r.url)).hostname or "").lower()
        if not final_host.endswith(ALLOWED_MEDIA_SUFFIXES):
            await r.aclose(); await client.aclose()
            raise HTTPException(502, "Unexpected media redirect.")
        content_type = r.headers.get("content-type", "application/octet-stream")
        max_bytes = MAX_DOWNLOAD_MB * 1024 * 1024
        try:
            content_length = int(r.headers.get("content-length") or 0)
        except ValueError:
            content_length = 0
        if content_length and content_length > max_bytes:
            await r.aclose(); await client.aclose()
            raise HTTPException(413, f"File exceeds the {MAX_DOWNLOAD_MB} MB server limit.")
        response_headers = {"Cache-Control": "private, max-age=300"}
        if attachment_name:
            response_headers["Content-Disposition"] = f'attachment; filename="{re.sub(r"[^A-Za-z0-9._-]+", "-", attachment_name)}"'

        async def iterator():
            sent = 0
            try:
                async for chunk in r.aiter_bytes(64 * 1024):
                    sent += len(chunk)
                    if sent > max_bytes:
                        break
                    yield chunk
            finally:
                await r.aclose()
                await client.aclose()
        return StreamingResponse(iterator(), media_type=content_type, headers=response_headers)
    except HTTPException:
        raise
    except Exception as exc:
        await client.aclose()
        raise HTTPException(502, "Could not fetch media from the source CDN.") from exc


@app.get("/api/preview")
async def preview(token: str):
    data = _load_token(token)
    url = data.get("thumbnail")
    if not url:
        raise HTTPException(404, "No thumbnail available.")
    return await _proxy_media(url)


@app.get("/api/download/direct")
async def direct_media(token: str, index: int, request: Request):
    _check_rate(f"d:{_client_ip(request)}", DOWNLOAD_LIMIT_PER_HOUR)
    data = _load_token(token)
    media = data.get("media") or []
    if index < 0 or index >= len(media):
        raise HTTPException(404, "Media item not found.")
    item = media[index]
    ext = ".mp4" if item.get("type") == "video" else ".jpg"
    return await _proxy_media(item["url"], f"{APP_NAME.lower()}-{index+1}{ext}")
