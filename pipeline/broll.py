"""
pipeline/broll.py — fetch or generate B-roll clips for UGC ad beats.

Backends (config-driven priority with automatic fallthrough):
  - sora   : OpenAI Sora 2 text/image-to-video   (OPENAI_API_KEY)
  - veo    : Google Veo 3.1 text/image-to-video  (GEMINI_API_KEY)
  - kling  : fal.ai Kling img2vid                 (FAL_KEY, needs an image)
  - stock  : Pexels -> Pixabay -> ffmpeg mock     (always succeeds; terminal)

Priority is settings.BROLL_BACKEND_PRIORITY (env BROLL_BACKENDS). A per-run
generative budget (settings.MAX_GEN_CLIPS) caps how many sora/veo/kling clips
are produced; once depleted the chain jumps straight to stock. On any missing
key / import / quota / permission / API error a generative backend logs a
warning and returns False so the caller falls through to the next backend.
"""

from __future__ import annotations

import logging
import re
import shutil
import tempfile
from pathlib import Path

from pipeline import settings


# Default backend chain when neither an explicit priority nor a settings
# override is provided. "stock" is terminal (always succeeds).
DEFAULT_BACKEND_PRIORITY = ["sora", "veo", "kling", "stock"]


# ---------------------------------------------------------------------------
# Retry helper
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
                time.sleep(delay)
                continue
            return resp
        except requests.exceptions.RequestException as exc:
            if attempt >= max_retries - 1:
                raise
            delay = base_delay * (2 ** attempt)
            if logger:
                logger.warning(
                    "Request error %s; retry %d/%d in %.1fs",
                    exc, attempt + 1, max_retries, delay,
                )
            time.sleep(delay)
    return resp


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _download_to(url: str, dest: str, logger: logging.Logger) -> bool:
    """Download *url* to *dest*. Returns True on success."""
    try:
        import shutil as _shutil

        r = _request_with_retry("GET", url, stream=True, timeout=30, logger=logger)
        r.raise_for_status()
        with open(dest, "wb") as f:
            _shutil.copyfileobj(r.raw, f)
        logger.info("Downloaded %s -> %s", url, dest)
        return True
    except Exception as exc:
        logger.warning("Download failed for %s: %s", url, exc)
        return False


def _safe_drawtext(query: str) -> str:
    """Escape a query string for ffmpeg drawtext filter."""
    # Escape special chars that break ffmpeg filter syntax
    text = re.sub(r"[\\:'\[\]()]", "_", query)
    return text


def _mock_clip(query: str, out_path: str, duration_s: float, logger: logging.Logger) -> str:
    """Generate a solid-color placeholder video with query text via ffmpeg."""
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    safe_text = _safe_drawtext(query)

    # Try with drawtext first; if it fails (e.g. missing font on Windows) use plain color
    try:
        settings.run_ffmpeg(
            [
                "-f", "lavfi",
                "-i", f"color=c=0x203040:s=1080x1920:d={duration_s:.2f}:r=30",
                "-vf", (
                    f"drawtext=text='{safe_text}'"
                    ":fontsize=60"
                    ":fontcolor=white"
                    ":x=(w-text_w)/2"
                    ":y=(h-text_h)/2"
                ),
                "-c:v", settings.VIDEO_CODEC,
                "-pix_fmt", settings.PIX_FMT,
                "-an",
                out_path,
            ],
            logger=logger,
        )
    except RuntimeError:
        # Fallback: plain color without text (Windows may lack default font)
        logger.warning("drawtext failed - using plain color placeholder for '%s'", query)
        settings.run_ffmpeg(
            [
                "-f", "lavfi",
                "-i", f"color=c=0x203040:s=1080x1920:d={duration_s:.2f}:r=30",
                "-c:v", settings.VIDEO_CODEC,
                "-pix_fmt", settings.PIX_FMT,
                "-an",
                out_path,
            ],
            logger=logger,
        )

    logger.info("Mock placeholder broll (%.1fs) -> %s", duration_s, out_path)
    return out_path


# ---------------------------------------------------------------------------
# Pexels
# ---------------------------------------------------------------------------

def _fetch_pexels(query: str, out_path: str, duration_s: float, logger: logging.Logger) -> bool:
    """Try to fetch a portrait video from Pexels. Returns True on success."""
    try:
        api_key = settings.get_env("PEXELS_API_KEY")
        resp = _request_with_retry(
            "GET",
            "https://api.pexels.com/videos/search",
            params={"query": query, "per_page": 5, "orientation": "portrait"},
            headers={"Authorization": api_key},
            timeout=15,
            logger=logger,
        )
        resp.raise_for_status()
        data = resp.json()

        videos = data.get("videos", [])
        if not videos:
            return False

        # Pick first video; prefer a portrait file near 1080x1920
        video = videos[0]
        files = video.get("video_files", [])
        if not files:
            return False

        # Sort: prefer portrait orientation (height > width) and largest resolution
        portrait_files = [f for f in files if f.get("height", 0) > f.get("width", 0)]
        chosen_files = portrait_files if portrait_files else files
        chosen_files.sort(key=lambda f: f.get("width", 0) * f.get("height", 0), reverse=True)
        url = chosen_files[0].get("link") or chosen_files[0].get("url")

        if not url:
            return False

        return _download_to(url, out_path, logger)

    except Exception as exc:
        logger.warning("Pexels fetch failed for '%s': %s", query, exc)
        return False


# ---------------------------------------------------------------------------
# Pixabay
# ---------------------------------------------------------------------------

def _fetch_pixabay(query: str, out_path: str, duration_s: float, logger: logging.Logger) -> bool:
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
            return False

        hit = hits[0]
        videos_dict = hit.get("videos", {})
        # Prefer medium, then large, then small
        for size in ("medium", "large", "small", "tiny"):
            entry = videos_dict.get(size)
            if entry and entry.get("url"):
                return _download_to(entry["url"], out_path, logger)

        return False

    except Exception as exc:
        logger.warning("Pixabay fetch failed for '%s': %s", query, exc)
        return False


# ---------------------------------------------------------------------------
# Public: fetch_broll
# ---------------------------------------------------------------------------

def fetch_broll(
    query: str,
    out_path: str,
    duration_s: float = 5.0,
    logger: logging.Logger | None = None,
) -> str:
    """
    Fetch a B-roll clip for *query* and save to *out_path*.

    Tries Pexels → Pixabay → mock placeholder (in that order).
    Always returns out_path.
    """
    if logger is None:
        logger = settings.get_logger("broll")

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)

    if settings.has_key("PEXELS_API_KEY"):
        if _fetch_pexels(query, out_path, duration_s, logger):
            return out_path
        logger.warning("Pexels returned nothing for '%s' - trying Pixabay", query)

    if settings.has_key("PIXABAY_API_KEY"):
        if _fetch_pixabay(query, out_path, duration_s, logger):
            return out_path
        logger.warning("Pixabay returned nothing for '%s' - using mock", query)

    return _mock_clip(query, out_path, duration_s, logger)


# ---------------------------------------------------------------------------
# Public: generate_product_video
# ---------------------------------------------------------------------------

def generate_product_video(
    image_path: str,
    out_path: str,
    duration_s: float = 5.0,
    prompt: str = "",
    logger: logging.Logger | None = None,
) -> str:
    """
    Generate a product video from a still image.

    REAL path: fal.ai Kling image-to-video (FAL_KEY).
    MOCK path: Ken-Burns zoompan via ffmpeg.

    Always returns out_path.
    """
    if logger is None:
        logger = settings.get_logger("broll")

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)

    if settings.has_key("FAL_KEY"):
        try:
            import fal_client  # type: ignore

            logger.info("Uploading product image to fal.ai...")
            image_url = fal_client.upload_file(image_path)

            result = fal_client.subscribe(
                "fal-ai/kling-video/v1/standard/image-to-video",
                arguments={
                    "image_url": image_url,
                    "prompt": prompt or "slow cinematic product reveal",
                    "duration": min(int(duration_s), 10),
                },
            )

            video_url = (
                result.get("video", {}).get("url")
                or result.get("video_url")
                or (result.get("videos") or [{}])[0].get("url")
            )
            if video_url and _download_to(video_url, out_path, logger):
                logger.info("fal.ai Kling video -> %s", out_path)
                return out_path

        except Exception as exc:
            logger.warning("fal.ai Kling failed (%s) - falling back to Ken-Burns mock", exc)

    # Mock: Ken-Burns zoompan from the product image
    return _ken_burns(image_path, out_path, duration_s, logger)


def _ken_burns(image_path: str, out_path: str, duration_s: float, logger: logging.Logger) -> str:
    """Create a slow Ken-Burns (zoompan) clip from a still image via ffmpeg."""
    frames = int(duration_s * settings.FPS)
    # zoompan: slow zoom from 1.0 to 1.2 over the duration
    zoompan = (
        f"zoompan=z='min(zoom+0.0015,1.2)'"
        f":x='iw/2-(iw/zoom/2)'"
        f":y='ih/2-(ih/zoom/2)'"
        f":d={frames}"
        f":s={settings.WIDTH}x{settings.HEIGHT}"
        f":fps={settings.FPS}"
    )

    settings.run_ffmpeg(
        [
            "-loop", "1",
            "-i", image_path,
            "-vf", zoompan,
            "-c:v", settings.VIDEO_CODEC,
            "-pix_fmt", settings.PIX_FMT,
            "-t", str(duration_s),
            "-an",
            out_path,
        ],
        logger=logger,
    )
    logger.info("Ken-Burns mock product video (%.1fs) -> %s", duration_s, out_path)
    return out_path


# ---------------------------------------------------------------------------
# Generative backend: OpenAI Sora 2
# ---------------------------------------------------------------------------

def _resize_image_for_sora(image_path: str, logger: logging.Logger) -> str:
    """
    Return a path to a 720x1280 PNG copy of *image_path*, suitable for Sora 2.

    Uses ffmpeg scale-to-cover+crop (no black bars).  The caller is responsible
    for deleting the returned temp file when done.
    """
    import os

    suffix = ".png"
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    os.close(tmp_fd)

    settings.run_ffmpeg(
        [
            "-i", image_path,
            "-vf", "scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280,setsar=1",
            "-frames:v", "1",
            "-y",
            tmp_path,
        ],
        logger=logger,
    )
    return tmp_path


def _gen_sora(
    prompt: str,
    out_path: str,
    duration_s: float,
    image_path: str | None,
    logger: logging.Logger,
) -> bool:
    """
    Generate a portrait clip with OpenAI Sora 2 (model "sora-2").

    Returns True only if a non-empty video was downloaded to *out_path*.
    On missing key / SDK import failure / any API / quota / permission error
    it logs a warning and returns False so the caller falls through.
    Never spends without OPENAI_API_KEY present.
    """
    if not settings.has_key("OPENAI_API_KEY"):
        logger.warning("Sora skipped: OPENAI_API_KEY missing - falling through.")
        return False

    try:
        import openai  # type: ignore
        from openai import OpenAI  # type: ignore
    except ImportError as exc:
        logger.warning("Sora skipped: openai SDK not installed (%s) - falling through.", exc)
        return False

    # Defensive import of specific exception classes for precise messaging.
    try:
        _AuthError = openai.AuthenticationError
        _PermError = openai.PermissionDeniedError
        _RateError = openai.RateLimitError
        _NotFound = openai.NotFoundError
        _APIError = openai.APIError
        _ConnError = openai.APIConnectionError
    except AttributeError:  # pragma: no cover - old SDK
        _AuthError = _PermError = _RateError = _NotFound = _APIError = _ConnError = Exception

    import time

    # sora-2 only accepts seconds of 4, 8, or 12. Any other value returns a
    # 400 — and when combined with input_reference the API misreports it as an
    # "input_reference: expected an object" error. Snap to the nearest valid
    # value (capped at 8 to keep generative cost/time bounded).
    _clamped = int(max(4, min(8, round(duration_s))))
    dur = min((4, 8), key=lambda v: abs(v - _clamped))

    try:
        client = OpenAI()  # reads OPENAI_API_KEY

        create_kwargs = dict(
            model="sora-2",
            prompt=prompt,
            size="720x1280",  # portrait
            seconds=dur,
        )
        # input_reference must be a structured (filename, bytes, content_type)
        # tuple — the API rejects a bare file handle with
        # "expected an object, but got a file instead." The reference image
        # must also match the requested size (720x1280), so resize first.
        _resized_tmp: str | None = None
        if image_path and Path(image_path).is_file():
            _resized_tmp = _resize_image_for_sora(image_path, logger)
            _ref_bytes = Path(_resized_tmp).read_bytes()
            create_kwargs["input_reference"] = (
                "input_reference.png",
                _ref_bytes,
                "image/png",
            )

        logger.info("Sora 2: starting generation (%.0fs, size=720x1280)...", dur)
        try:
            video = client.videos.create(**create_kwargs)
        finally:
            if _resized_tmp is not None:
                import os as _os
                try:
                    _os.unlink(_resized_tmp)
                except OSError:
                    pass

        while getattr(video, "status", None) in ("queued", "in_progress"):
            time.sleep(10)
            video = client.videos.retrieve(video.id)

        if getattr(video, "status", None) == "failed":
            err = getattr(video, "error", None)
            code = getattr(err, "code", "?")
            msg = getattr(err, "message", err)
            logger.warning("Sora 2 job failed (%s: %s) - falling through.", code, msg)
            return False

        client.videos.download_content(video.id, variant="video").write_to_file(out_path)

        if Path(out_path).is_file() and Path(out_path).stat().st_size > 0:
            logger.info("Sora 2 video -> %s", out_path)
            return True
        logger.warning("Sora 2 produced empty/missing file - falling through.")
        return False

    except _AuthError as exc:
        logger.warning("Sora skipped: 401 auth/missing-key (%s) - falling through.", exc)
        return False
    except _PermError as exc:
        logger.warning("Sora skipped: 403 billing/permission (%s) - falling through.", exc)
        return False
    except _RateError as exc:
        logger.warning("Sora skipped: 429 quota/rate-limit (%s) - falling through.", exc)
        return False
    except _NotFound as exc:
        logger.warning("Sora skipped: 404 not-found/bad-model (%s) - falling through.", exc)
        return False
    except (_ConnError, _APIError) as exc:
        logger.warning("Sora skipped: API/connection error (%s) - falling through.", exc)
        return False
    except Exception as exc:  # noqa: BLE001 - never raise at backend level
        logger.warning("Sora skipped: unexpected error (%s) - falling through.", exc)
        return False


# ---------------------------------------------------------------------------
# Generative backend: Google Veo 3.1
# ---------------------------------------------------------------------------

def _gen_veo(
    prompt: str,
    out_path: str,
    duration_s: float,
    image_path: str | None,
    logger: logging.Logger,
) -> bool:
    """
    Generate a portrait clip with Google Veo 3.1 ("veo-3.1-generate-preview").

    Returns True only if a non-empty video was downloaded to *out_path*.
    On missing key / SDK import failure / any API / quota / permission error
    it logs a warning and returns False so the caller falls through.
    Never spends without GEMINI_API_KEY present.
    """
    if not settings.has_key("GEMINI_API_KEY"):
        logger.warning("Veo skipped: GEMINI_API_KEY missing - falling through.")
        return False

    try:
        from google import genai  # type: ignore
        from google.genai import types  # type: ignore
    except ImportError as exc:
        logger.warning("Veo skipped: google-genai SDK not installed (%s) - falling through.", exc)
        return False

    # Defensive import of specific exception classes for precise messaging.
    try:
        from google.genai import errors as _genai_errors  # type: ignore
        _APIError = _genai_errors.APIError
        _ClientError = _genai_errors.ClientError
        _ServerError = _genai_errors.ServerError
    except ImportError:  # pragma: no cover
        _APIError = _ClientError = _ServerError = Exception

    import os as _os
    import time

    # veo-3.1-generate-preview only accepts EVEN durations of 4, 6, or 8
    # seconds (odd values return 400 "out of bound"). Snap to the nearest
    # valid value.
    _clamped = int(max(4, min(8, round(duration_s))))
    dur = min((4, 6, 8), key=lambda v: abs(v - _clamped))

    try:
        client = genai.Client(api_key=_os.environ["GEMINI_API_KEY"])

        source_kwargs = dict(prompt=prompt)
        if image_path and Path(image_path).is_file():
            source_kwargs["image"] = types.Image.from_file(location=image_path)

        logger.info("Veo 3.1: starting generation (%.0fs, 9:16 1080p)...", dur)
        operation = client.models.generate_videos(
            model="veo-3.1-generate-preview",
            source=types.GenerateVideosSource(**source_kwargs),
            config=types.GenerateVideosConfig(
                aspect_ratio="9:16",
                duration_seconds=dur,
                number_of_videos=1,
                resolution="1080p",
                # "dont_allow" is not supported by veo-3.1-preview; allow people
                # (product b-roll may legitimately include a presenter/hands).
                person_generation="allow_all",
            ),
        )

        while not operation.done:
            time.sleep(15)
            operation = client.operations.get(operation)

        if getattr(operation, "error", None):
            logger.warning("Veo 3.1 job error (%s) - falling through.", operation.error)
            return False

        generated = operation.result.generated_videos[0].video
        video_bytes = client.files.download(file=generated)
        with open(out_path, "wb") as f:
            f.write(video_bytes)

        if Path(out_path).is_file() and Path(out_path).stat().st_size > 0:
            logger.info("Veo 3.1 video -> %s", out_path)
            return True
        logger.warning("Veo 3.1 produced empty/missing file - falling through.")
        return False

    except _ClientError as exc:
        code = getattr(exc, "code", "?")
        hint = {
            401: "missing/invalid key",
            403: "billing/permission",
            404: "bad model",
            429: "quota/rate-limit",
        }.get(code, "client error")
        logger.warning("Veo skipped: %s (%s: %s) - falling through.", code, hint, exc)
        return False
    except _ServerError as exc:
        logger.warning("Veo skipped: 5xx server error (%s) - falling through.", exc)
        return False
    except _APIError as exc:
        logger.warning("Veo skipped: API error (%s) - falling through.", exc)
        return False
    except Exception as exc:  # noqa: BLE001 - never raise at backend level
        logger.warning("Veo skipped: unexpected error (%s) - falling through.", exc)
        return False


# ---------------------------------------------------------------------------
# Generative budget holder
# ---------------------------------------------------------------------------

class GenBudget:
    """
    Mutable holder for the remaining number of generative clips allowed
    this run. Passed by reference so a single budget is shared across all
    beats. ``remaining`` is decremented each time a generative backend
    (sora/veo/kling) succeeds.
    """

    __slots__ = ("remaining",)

    def __init__(self, remaining: int) -> None:
        self.remaining = int(remaining)

    def available(self) -> bool:
        return self.remaining > 0

    def spend(self) -> None:
        self.remaining -= 1


# ---------------------------------------------------------------------------
# Public dispatcher: generate_broll_clip
# ---------------------------------------------------------------------------

def generate_broll_clip(
    query: str,
    out_path: str,
    duration_s: float,
    image_path: str | None = None,
    product_images: list[str] | None = None,
    priority: list[str] | None = None,
    gen_budget: "GenBudget | None" = None,
    logger: logging.Logger | None = None,
) -> tuple[str, str]:
    """
    Produce a single B-roll clip via a config-driven backend chain with
    automatic fallthrough.

    Backends (by name): "sora", "veo", "kling", "stock". The first backend
    that succeeds wins. "stock" always succeeds and is the terminal fallback,
    so the chain always terminates.

    Parameters
    ----------
    priority   : list of backend names; defaults to
                 settings.BROLL_BACKEND_PRIORITY.
    gen_budget : optional GenBudget (mutable). If provided and depleted
                 (remaining <= 0), sora/veo/kling are skipped and the chain
                 goes straight to stock. Decremented on generative success.

    Returns
    -------
    (out_path, backend_used_label) — e.g. ("...mp4", "sora") or
    ("...mp4", "stock:pexels/pixabay/mock").
    """
    if logger is None:
        logger = settings.get_logger("broll")

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)

    if priority is None:
        priority = list(getattr(settings, "BROLL_BACKEND_PRIORITY", DEFAULT_BACKEND_PRIORITY))

    # Best-effort image for image-to-video / input_reference.
    img = image_path or (product_images[0] if product_images else None)

    _GENERATIVE = ("sora", "veo", "kling")

    for backend in priority:
        backend = (backend or "").strip().lower()

        # Enforce generative budget: skip gen backends when depleted.
        if backend in _GENERATIVE and gen_budget is not None and not gen_budget.available():
            logger.info("Generative budget depleted - skipping '%s'.", backend)
            continue

        if backend == "sora":
            if _gen_sora(query, out_path, duration_s, img, logger):
                if gen_budget is not None:
                    gen_budget.spend()
                return out_path, "sora"

        elif backend == "veo":
            if _gen_veo(query, out_path, duration_s, img, logger):
                if gen_budget is not None:
                    gen_budget.spend()
                return out_path, "veo"

        elif backend == "kling":
            if not img:
                logger.info("Kling skipped: no image available - falling through.")
                continue
            if not settings.has_key("FAL_KEY"):
                logger.warning("Kling skipped: FAL_KEY missing - falling through.")
                continue
            try:
                result = generate_product_video(
                    image_path=img,
                    out_path=out_path,
                    duration_s=duration_s,
                    prompt=f"cinematic b-roll: {query}",
                    logger=logger,
                )
            except Exception as exc:  # noqa: BLE001 - never raise at dispatcher level
                logger.warning("Kling errored (%s) - falling through.", exc)
                result = None
            # generate_product_video falls back to Ken-Burns mock (still a real
            # file) if fal.ai fails; treat a non-empty file as success.
            if result and Path(out_path).is_file() and Path(out_path).stat().st_size > 0:
                if gen_budget is not None:
                    gen_budget.spend()
                return out_path, "kling"

        elif backend == "stock":
            # Terminal fallback: Pexels -> Pixabay -> mock (always succeeds).
            fetch_broll(query, out_path, duration_s=duration_s, logger=logger)
            label = "stock:pexels/pixabay/mock"
            return out_path, label

        else:
            logger.warning("Unknown backend '%s' - ignoring.", backend)

    # Safety net: if priority lacked a terminal 'stock', always finish on stock.
    logger.warning("No backend in priority succeeded/terminated - using stock fallback.")
    fetch_broll(query, out_path, duration_s=duration_s, logger=logger)
    return out_path, "stock:pexels/pixabay/mock"


# ---------------------------------------------------------------------------
# Public: fetch_broll_for_beats
# ---------------------------------------------------------------------------

def fetch_broll_for_beats(
    beats: list,
    work_dir: str,
    product_images: list[str] | None = None,
    logger: logging.Logger | None = None,
    priority: list[str] | None = None,
    max_gen_clips: int | None = None,
) -> list[str]:
    """
    Fetch/generate one B-roll clip per beat, saved as work_dir/broll_<i>.mp4.

    Uses the config-driven backend chain (generate_broll_clip) with a shared
    per-run generative budget. Up to *max_gen_clips* generative (sora/veo/kling)
    clips are produced; the rest fall back to stock.

    Backward compatible: fetch_broll_for_beats(beats, work_dir,
    product_images=..., logger=...) still works exactly as before.

    Returns an ordered list of output paths (unchanged contract for run.py).
    """
    if logger is None:
        logger = settings.get_logger("broll")

    if max_gen_clips is None:
        max_gen_clips = int(getattr(settings, "MAX_GEN_CLIPS", 2))

    Path(work_dir).mkdir(parents=True, exist_ok=True)
    paths: list[str] = []

    gen_budget = GenBudget(max_gen_clips)
    image_path = product_images[0] if product_images else None

    for i, beat in enumerate(beats):
        out_path = str(Path(work_dir) / f"broll_{i}.mp4")
        duration_s = float(beat.get("duration_s", 5.0))
        query = beat.get("broll_query", "product video")

        path, backend = generate_broll_clip(
            query=query,
            out_path=out_path,
            duration_s=duration_s,
            image_path=image_path,
            product_images=product_images,
            priority=priority,
            gen_budget=gen_budget,
            logger=logger,
        )
        logger.info("Beat %d ('%s') -> backend=%s", i, query, backend)
        paths.append(path)

    logger.info(
        "Fetched %d broll clips in %s (gen budget left: %d/%d)",
        len(paths), work_dir, gen_budget.remaining, max_gen_clips,
    )
    return paths


# ---------------------------------------------------------------------------
# __main__
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json
    import sys
    import tempfile

    log = settings.get_logger("broll.__main__")

    with tempfile.TemporaryDirectory() as tmpdir:
        out = str(Path(tmpdir) / "test_broll.mp4")
        result_path = fetch_broll("mountain hiking", out, duration_s=3.0, logger=log)

        probe = settings.run_ffprobe(
            [
                "-v", "error",
                "-show_entries", "format=duration,size",
                "-of", "json",
                result_path,
            ],
            logger=log,
        )
        info = json.loads(probe)
        fmt = info.get("format", {})
        duration = float(fmt.get("duration", 0))
        size = int(fmt.get("size", 0))

        print(f"Output path : {result_path}")
        print(f"Duration    : {duration:.2f}s")
        print(f"File size   : {size} bytes")

    sys.exit(0)
