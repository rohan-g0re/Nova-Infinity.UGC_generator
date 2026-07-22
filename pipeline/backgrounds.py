"""pipeline/backgrounds.py — fetch per-beat scene backgrounds for chromakey compositing."""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

from pipeline import settings


# ---------------------------------------------------------------------------
# Slugify
# ---------------------------------------------------------------------------

def slugify(query: str) -> str:
    """Convert a query string to a filesystem-safe slug.

    Lowercases, replaces non-alphanumeric chars with underscores,
    collapses repeated underscores, strips leading/trailing underscores.
    """
    s = query.lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s)
    s = s.strip("_")
    return s or "background"


# ---------------------------------------------------------------------------
# Retry / download helpers (mirroring broll.py)
# ---------------------------------------------------------------------------

def _request_with_retry(method, url, *, max_retries=3, base_delay=1.0, logger=None, **kwargs):
    """HTTP request with exponential backoff on 429/5xx and connection errors."""
    import requests  # type: ignore
    import time

    resp = None
    for attempt in range(max_retries):
        try:
            resp = requests.request(method, url, **kwargs)
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                if logger:
                    logger.warning(
                        "HTTP %d from %s; retry %d/%d in %.1fs",
                        resp.status_code, url, attempt + 1, max_retries, delay,
                    )
                import time as _time
                _time.sleep(delay)
                continue
            return resp
        except Exception as exc:
            if attempt >= max_retries - 1:
                raise
            delay = base_delay * (2 ** attempt)
            if logger:
                logger.warning(
                    "Request error %s; retry %d/%d in %.1fs",
                    exc, attempt + 1, max_retries, delay,
                )
            import time as _time
            _time.sleep(delay)
    return resp


def _download_to(url: str, dest: str, logger: logging.Logger) -> bool:
    """Download *url* to *dest*. Returns True on success."""
    try:
        r = _request_with_retry("GET", url, stream=True, timeout=60, logger=logger)
        r.raise_for_status()
        with open(dest, "wb") as f:
            shutil.copyfileobj(r.raw, f)
        logger.info("Downloaded %s -> %s", url, dest)
        return True
    except Exception as exc:
        logger.warning("Download failed for %s: %s", url, exc)
        return False


# ---------------------------------------------------------------------------
# Video background fetchers
# ---------------------------------------------------------------------------

def _fetch_pexels_video(query: str, out_path: str, logger: logging.Logger) -> bool:
    """Try to fetch a video from Pexels. Returns True on success."""
    try:
        api_key = settings.get_env("PEXELS_API_KEY")
        resp = _request_with_retry(
            "GET",
            "https://api.pexels.com/videos/search",
            params={"query": query, "per_page": 5, "orientation": "landscape"},
            headers={"Authorization": api_key},
            timeout=15,
            logger=logger,
        )
        resp.raise_for_status()
        data = resp.json()

        videos = data.get("videos", [])
        if not videos:
            logger.warning("Pexels video: no results for '%s'", query)
            return False

        video = videos[0]
        files = video.get("video_files", [])
        if not files:
            return False

        # Sort by resolution (largest first)
        files.sort(key=lambda f: f.get("width", 0) * f.get("height", 0), reverse=True)
        url = files[0].get("link") or files[0].get("url")
        if not url:
            return False

        return _download_to(url, out_path, logger)

    except Exception as exc:
        logger.warning("Pexels video fetch failed for '%s': %s", query, exc)
        return False


def _fetch_pixabay_video(query: str, out_path: str, logger: logging.Logger) -> bool:
    """Try to fetch a video from Pixabay. Returns True on success."""
    try:
        api_key = settings.get_env("PIXABAY_API_KEY")
        resp = _request_with_retry(
            "GET",
            "https://pixabay.com/api/videos/",
            params={"key": api_key, "q": query, "per_page": 3},
            timeout=15,
            logger=logger,
        )
        resp.raise_for_status()
        data = resp.json()

        hits = data.get("hits", [])
        if not hits:
            logger.warning("Pixabay video: no results for '%s'", query)
            return False

        hit = hits[0]
        videos_dict = hit.get("videos", {})
        for size in ("large", "medium", "small", "tiny"):
            entry = videos_dict.get(size)
            if entry and entry.get("url"):
                return _download_to(entry["url"], out_path, logger)

        return False

    except Exception as exc:
        logger.warning("Pixabay video fetch failed for '%s': %s", query, exc)
        return False


# ---------------------------------------------------------------------------
# Image background fetchers
# ---------------------------------------------------------------------------

def _fetch_pexels_image(query: str, out_path: str, logger: logging.Logger) -> bool:
    """Try to fetch a portrait image from Pexels. Returns True on success."""
    try:
        api_key = settings.get_env("PEXELS_API_KEY")
        resp = _request_with_retry(
            "GET",
            "https://api.pexels.com/v1/search",
            params={"query": query, "per_page": 5, "orientation": "portrait"},
            headers={"Authorization": api_key},
            timeout=15,
            logger=logger,
        )
        resp.raise_for_status()
        data = resp.json()

        photos = data.get("photos", [])
        if not photos:
            logger.warning("Pexels image: no results for '%s'", query)
            return False

        photo = photos[0]
        src = photo.get("src", {})
        url = src.get("large2x") or src.get("large") or src.get("original")
        if not url:
            return False

        return _download_to(url, out_path, logger)

    except Exception as exc:
        logger.warning("Pexels image fetch failed for '%s': %s", query, exc)
        return False


def _fetch_pixabay_image(query: str, out_path: str, logger: logging.Logger) -> bool:
    """Try to fetch an image from Pixabay. Returns True on success."""
    try:
        api_key = settings.get_env("PIXABAY_API_KEY")
        resp = _request_with_retry(
            "GET",
            "https://pixabay.com/api/",
            params={
                "key": api_key,
                "q": query,
                "image_type": "photo",
                "per_page": 3,
                "safesearch": "true",
            },
            timeout=15,
            logger=logger,
        )
        resp.raise_for_status()
        data = resp.json()

        hits = data.get("hits", [])
        if not hits:
            logger.warning("Pixabay image: no results for '%s'", query)
            return False

        hit = hits[0]
        url = hit.get("largeImageURL") or hit.get("webformatURL")
        if not url:
            return False

        return _download_to(url, out_path, logger)

    except Exception as exc:
        logger.warning("Pixabay image fetch failed for '%s': %s", query, exc)
        return False


# ---------------------------------------------------------------------------
# FFmpeg encode helpers
# ---------------------------------------------------------------------------

def _video_to_portrait(raw_video: str, out_path: str, duration_s: float, logger: logging.Logger) -> bool:
    """Scale/crop/loop a raw video to 1080x1920 portrait, trimmed to duration_s."""
    try:
        settings.run_ffmpeg(
            [
                "-stream_loop", "-1",
                "-i", raw_video,
                "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,fps=30",
                "-t", str(duration_s),
                "-an",
                "-c:v", "libx264",
                "-preset", "fast",
                "-crf", "23",
                "-pix_fmt", "yuv420p",
                out_path,
            ],
            logger,
        )
        logger.info("Video background -> %s (%.1fs)", out_path, duration_s)
        return True
    except Exception as exc:
        logger.warning("Video-to-portrait failed: %s", exc)
        return False


def _image_ken_burns(image_path: str, out_path: str, duration_s: float, logger: logging.Logger) -> bool:
    """Apply slow Ken-Burns zoompan to a still image -> 1080x1920 for duration_s."""
    try:
        frames = int(duration_s * settings.FPS)
        zoompan = (
            f"zoompan=z='min(zoom+0.0012,1.15)'"
            f":x='iw/2-(iw/zoom/2)'"
            f":y='ih/2-(ih/zoom/2)'"
            f":d={frames}"
            f":s=1080x1920"
            f":fps={settings.FPS}"
        )
        settings.run_ffmpeg(
            [
                "-loop", "1",
                "-i", image_path,
                "-vf", zoompan,
                "-c:v", "libx264",
                "-preset", "fast",
                "-crf", "23",
                "-pix_fmt", "yuv420p",
                "-t", str(duration_s),
                "-an",
                out_path,
            ],
            logger,
        )
        logger.info("Ken-Burns image background -> %s (%.1fs)", out_path, duration_s)
        return True
    except Exception as exc:
        logger.warning("Ken-Burns image failed (%s): %s", image_path, exc)
        return False


def _lavfi_solid(out_path: str, duration_s: float, logger: logging.Logger) -> bool:
    """Generate a subtle dark gradient/solid color 1080x1920 clip via lavfi."""
    try:
        settings.run_ffmpeg(
            [
                "-f", "lavfi",
                "-i", f"color=c=0x1a1a2e:s=1080x1920:d={duration_s:.2f}:r=30",
                "-c:v", "libx264",
                "-preset", "fast",
                "-crf", "23",
                "-pix_fmt", "yuv420p",
                "-an",
                out_path,
            ],
            logger,
        )
        logger.info("Lavfi solid color background -> %s (%.1fs)", out_path, duration_s)
        return True
    except Exception as exc:
        logger.warning("Lavfi solid color failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_background(
    query: str,
    out_path: str,
    duration_s: float,
    logger: logging.Logger | None = None,
) -> str:
    """Fetch or generate a scene background clip for a beat.

    Priority order:
      1. Pexels video  (PEXELS_API_KEY)
      2. Pixabay video (PIXABAY_API_KEY)
      3. Pexels image  (PEXELS_API_KEY)
      4. Pixabay image (PIXABAY_API_KEY)
      5. Bundled assets/backgrounds/office.jpg (Ken-Burns)
      6. Lavfi solid color (always available)

    Raw downloads are cached under BACKGROUNDS_DIR/<slug>/ so re-runs skip
    re-downloading.

    Always returns out_path. Never raises.
    """
    if logger is None:
        logger = settings.get_logger("backgrounds")

    out_path = str(out_path)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)

    slug = slugify(query)
    slug_dir = settings.BACKGROUNDS_DIR / slug
    slug_dir.mkdir(parents=True, exist_ok=True)

    raw_video_path = str(slug_dir / "raw_video.mp4")
    raw_image_path = str(slug_dir / "raw_image.jpg")

    logger.info("fetch_background: query='%s' slug='%s' duration=%.1fs", query, slug, duration_s)

    # ---- 1. Check cache for raw video ----
    raw_video_cached = Path(raw_video_path).exists() and Path(raw_video_path).stat().st_size > 1024
    raw_image_cached = (
        Path(raw_image_path).exists() and Path(raw_image_path).stat().st_size > 1024
    )

    # ---- 2. Try to get a video background ----
    if not raw_video_cached:
        if settings.has_key("PEXELS_API_KEY"):
            logger.info("Trying Pexels video for '%s'...", query)
            raw_video_cached = _fetch_pexels_video(query, raw_video_path, logger)

        if not raw_video_cached and settings.has_key("PIXABAY_API_KEY"):
            logger.info("Trying Pixabay video for '%s'...", query)
            raw_video_cached = _fetch_pixabay_video(query, raw_video_path, logger)
    else:
        logger.info("Using cached raw video: %s", raw_video_path)

    if raw_video_cached:
        if _video_to_portrait(raw_video_path, out_path, duration_s, logger):
            return out_path
        logger.warning("Video processing failed; falling back to image.")

    # ---- 3. Try to get an image background ----
    if not raw_image_cached:
        if settings.has_key("PEXELS_API_KEY"):
            logger.info("Trying Pexels image for '%s'...", query)
            raw_image_cached = _fetch_pexels_image(query, raw_image_path, logger)

        if not raw_image_cached and settings.has_key("PIXABAY_API_KEY"):
            logger.info("Trying Pixabay image for '%s'...", query)
            raw_image_cached = _fetch_pixabay_image(query, raw_image_path, logger)
    else:
        logger.info("Using cached raw image: %s", raw_image_path)

    if raw_image_cached:
        if _image_ken_burns(raw_image_path, out_path, duration_s, logger):
            return out_path
        logger.warning("Image Ken-Burns failed; falling back to bundled office.jpg.")

    # ---- 4. Fall back to bundled office.jpg ----
    office_jpg = settings.BACKGROUNDS_DIR / "office.jpg"
    if office_jpg.exists():
        logger.warning("Using fallback bundled background: %s", office_jpg)
        if _image_ken_burns(str(office_jpg), out_path, duration_s, logger):
            return out_path
        logger.warning("Ken-Burns on office.jpg failed; generating solid color.")

    # ---- 5. Last resort: lavfi solid color ----
    logger.warning("Generating solid color background as last resort.")
    _lavfi_solid(out_path, duration_s, logger)
    return out_path


# ---------------------------------------------------------------------------
# __main__ smoke-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    from pipeline import settings as _s

    log = _s.get_logger("backgrounds.__main__")
    _s.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    out = str(_s.OUTPUT_DIR / "test_bg.mp4")
    result = fetch_background("modern gym interior", out, duration_s=4.0, logger=log)

    size = Path(result).stat().st_size if Path(result).exists() else 0
    print(f"\nOutput path : {result}")
    print(f"File size   : {size:,} bytes")
    sys.exit(0 if size > 0 else 1)
