"""
pipeline/lipsync.py — Lip-sync integration for the video generation pipeline.

Public API:
    lipsync(video_path, audio_path, out_path, logger=None) -> str

Sync.so v2 API schema (confirmed via live probing 2026-07-15):

    UPLOAD (per file):
        POST https://api.sync.so/v2/assets/upload
        Headers: x-api-key: <key>, Content-Type: application/json
        Body: {"fileName": "<name>", "contentType": "<mime>", "size": <bytes>}
        Response 201: {"uploadUrl": "<s3-presigned-PUT-url>", "url": "<public-url>", "expiresIn": 604800}
        Then: PUT <uploadUrl> with raw file bytes, Content-Type header, Content-Length

    GENERATE:
        POST https://api.sync.so/v2/generate
        Headers: x-api-key: <key>, Content-Type: application/json
        Body: {
            "model": "lipsync-2",
            "input": [
                {"type": "video", "url": "<public_video_url>"},
                {"type": "audio", "url": "<public_audio_url>"}
            ],
            "options": {"output_format": "mp4"}
        }
        Response 2xx: {"id": "<job_id>", ...}

    POLL:
        GET https://api.sync.so/v2/generate/<job_id>
        Headers: x-api-key: <key>
        Response: {"id": "...", "status": "PENDING"|"PROCESSING"|"COMPLETED"|"FAILED", ...}
        On completion: response contains "outputUrl" field with public download URL.

    STATUS values handled case-insensitively:
        terminal-success: "completed", "done"
        terminal-failure: "failed", "error"

Log markers:
    Live success  -> "SYNC.SO LIVE lipsync completed"
    Fallback      -> "SYNC.SO lipsync FALLBACK to mock mux (reason: ...)"
"""

from __future__ import annotations

import json
import mimetypes
import os
import time
import urllib.request
import urllib.error
from pathlib import Path

import requests

import pipeline.settings as settings


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SYNCSO_BASE = "https://api.sync.so"
_UPLOAD_URL = f"{_SYNCSO_BASE}/v2/assets/upload"
_GENERATE_URL = f"{_SYNCSO_BASE}/v2/generate"

# Retry / polling config
_MAX_SUBMIT_RETRIES = 3          # retries on 429/5xx for the submit POST
_SUBMIT_BACKOFF_BASE = 2.0       # seconds, doubles each retry
_POLL_INTERVAL_SECS = 8          # seconds between status polls
_POLL_TIMEOUT_SECS = 600         # 10 minutes max wait
_UPLOAD_RETRIES = 3              # retries for the S3 PUT

# Status string sets (compared case-insensitively)
_STATUS_SUCCESS = {"completed", "done"}
_STATUS_FAILURE = {"failed", "error", "cancelled"}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _log(logger, level: str, msg: str, *args) -> None:
    """Null-safe logger call."""
    if logger:
        getattr(logger, level)(msg, *args)


def _syncso_headers(api_key: str) -> dict:
    return {"x-api-key": api_key, "Content-Type": "application/json"}


def _upload_file_to_syncso(
    local_path: str,
    api_key: str,
    logger=None,
) -> str:
    """
    Upload a local file to Sync.so's asset storage.

    1. POST /v2/assets/upload to get a presigned S3 PUT URL + a public URL.
    2. PUT the file bytes to the presigned URL.
    3. Return the public URL.
    """
    path = Path(local_path)
    file_size = path.stat().st_size
    content_type, _ = mimetypes.guess_type(str(path))
    if not content_type:
        # Fallback guesses
        ext = path.suffix.lower()
        content_type = {
            ".mp4": "video/mp4",
            ".mov": "video/quicktime",
            ".mp3": "audio/mpeg",
            ".wav": "audio/wav",
            ".aac": "audio/aac",
            ".m4a": "audio/mp4",
        }.get(ext, "application/octet-stream")

    _log(logger, "info", "Sync.so upload: requesting presigned URL for %s (%d bytes, %s)",
         path.name, file_size, content_type)

    # Step 1: get presigned URL
    payload = json.dumps({
        "fileName": path.name,
        "contentType": content_type,
        "size": file_size,
    }).encode("utf-8")

    for attempt in range(_UPLOAD_RETRIES):
        try:
            resp = requests.post(
                _UPLOAD_URL,
                headers={"x-api-key": api_key, "Content-Type": "application/json"},
                data=payload,
                timeout=30,
            )
            if resp.status_code in (429, 500, 502, 503, 504):
                wait = _SUBMIT_BACKOFF_BASE ** attempt
                _log(logger, "warning", "Sync.so presigned URL request HTTP %d; retrying in %.1fs",
                     resp.status_code, wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            break
        except requests.exceptions.RequestException as exc:
            if attempt >= _UPLOAD_RETRIES - 1:
                raise
            wait = _SUBMIT_BACKOFF_BASE ** attempt
            _log(logger, "warning", "Sync.so presigned URL request error %s; retrying in %.1fs", exc, wait)
            time.sleep(wait)

    upload_data = resp.json()
    presigned_put_url: str = upload_data["uploadUrl"]
    public_url: str = upload_data["url"]

    _log(logger, "info", "Sync.so upload: PUT to presigned S3 URL (%d bytes)", file_size)

    # Step 2: PUT the raw bytes to S3 presigned URL
    with open(local_path, "rb") as fh:
        file_bytes = fh.read()

    for attempt in range(_UPLOAD_RETRIES):
        try:
            put_resp = requests.put(
                presigned_put_url,
                data=file_bytes,
                headers={
                    "Content-Type": content_type,
                    "Content-Length": str(file_size),
                },
                timeout=120,
            )
            if put_resp.status_code in (429, 500, 502, 503, 504):
                wait = _SUBMIT_BACKOFF_BASE ** attempt
                _log(logger, "warning", "Sync.so S3 PUT HTTP %d; retrying in %.1fs",
                     put_resp.status_code, wait)
                time.sleep(wait)
                continue
            put_resp.raise_for_status()
            break
        except requests.exceptions.RequestException as exc:
            if attempt >= _UPLOAD_RETRIES - 1:
                raise
            wait = _SUBMIT_BACKOFF_BASE ** attempt
            _log(logger, "warning", "Sync.so S3 PUT error %s; retrying in %.1fs", exc, wait)
            time.sleep(wait)

    _log(logger, "info", "Sync.so upload complete: public URL = %s", public_url)
    return public_url


def _submit_lipsync_job(
    video_url: str,
    audio_url: str,
    api_key: str,
    logger=None,
) -> str:
    """
    POST to /v2/generate and return the job ID.
    Retries on transient HTTP errors (429/5xx).
    """
    payload = json.dumps({
        "model": "lipsync-2",
        "input": [
            {"type": "video", "url": video_url},
            {"type": "audio", "url": audio_url},
        ],
        "options": {"output_format": "mp4"},
    }).encode("utf-8")

    for attempt in range(_MAX_SUBMIT_RETRIES):
        try:
            resp = requests.post(
                _GENERATE_URL,
                headers={"x-api-key": api_key, "Content-Type": "application/json"},
                data=payload,
                timeout=30,
            )
            if resp.status_code in (429, 500, 502, 503, 504):
                wait = _SUBMIT_BACKOFF_BASE ** attempt
                _log(logger, "warning", "Sync.so submit HTTP %d; retrying in %.1fs (attempt %d/%d)",
                     resp.status_code, wait, attempt + 1, _MAX_SUBMIT_RETRIES)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            job = resp.json()
            job_id = job["id"]
            _log(logger, "info", "Sync.so job submitted: id=%s", job_id)
            return job_id
        except (KeyError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Sync.so submit: unexpected response format: {exc}") from exc
        except requests.exceptions.RequestException as exc:
            if attempt >= _MAX_SUBMIT_RETRIES - 1:
                raise
            wait = _SUBMIT_BACKOFF_BASE ** attempt
            _log(logger, "warning", "Sync.so submit error %s; retrying in %.1fs", exc, wait)
            time.sleep(wait)

    raise RuntimeError("Sync.so submit: exhausted retries")


def _poll_lipsync_job(
    job_id: str,
    api_key: str,
    logger=None,
) -> str:
    """
    Poll GET /v2/generate/<job_id> until completed.
    Returns the output URL.
    Raises RuntimeError on failure or timeout.
    """
    poll_url = f"{_GENERATE_URL}/{job_id}"
    deadline = time.monotonic() + _POLL_TIMEOUT_SECS
    consecutive_errors = 0

    while time.monotonic() < deadline:
        time.sleep(_POLL_INTERVAL_SECS)
        try:
            resp = requests.get(
                poll_url,
                headers={"x-api-key": api_key},
                timeout=30,
            )
            if resp.status_code in (429, 500, 502, 503, 504):
                consecutive_errors += 1
                wait = _SUBMIT_BACKOFF_BASE ** min(consecutive_errors, 5)
                _log(logger, "warning", "Sync.so poll HTTP %d (job %s); backing off %.1fs",
                     resp.status_code, job_id, wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            consecutive_errors = 0

            data = resp.json()
            raw_status: str = data.get("status", "")
            status = raw_status.lower().strip()

            _log(logger, "info", "Sync.so job %s status: %s", job_id, raw_status)

            if status in _STATUS_SUCCESS:
                # Extract output URL — handle multiple possible field names
                output_url = (
                    data.get("outputUrl")
                    or data.get("output_url")
                    or (data.get("output") or {}).get("url")
                    or (data.get("result") or {}).get("url")
                )
                if not output_url:
                    raise RuntimeError(
                        f"Sync.so job {job_id} completed but no output URL found in response: {data}"
                    )
                return output_url

            if status in _STATUS_FAILURE:
                raise RuntimeError(
                    f"Sync.so job {job_id} failed with status '{raw_status}': {data}"
                )

            # Still running (PENDING, PROCESSING, etc.) — keep polling

        except requests.exceptions.RequestException as exc:
            consecutive_errors += 1
            _log(logger, "warning", "Sync.so poll request error (job %s): %s", job_id, exc)
            if consecutive_errors > 5:
                raise RuntimeError(
                    f"Sync.so poll: too many consecutive errors for job {job_id}"
                ) from exc

    raise RuntimeError(
        f"Sync.so job {job_id} did not complete within {_POLL_TIMEOUT_SECS}s"
    )


def _download_output(output_url: str, out_path: str, logger=None) -> None:
    """Download the completed lipsync video to out_path."""
    _log(logger, "info", "Sync.so: downloading output from %s -> %s", output_url, out_path)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(output_url, out_path)
    _log(logger, "info", "Sync.so: download complete, size=%d bytes", Path(out_path).stat().st_size)


def _mock_mux(video_path: str, audio_path: str, out_path: str, logger=None) -> str:
    """
    Fallback: mux *audio_path* over *video_path*, copying the video stream.
    Produces an output with the new audio track; no actual lip-sync applied.
    """
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    if logger:
        logger.info("MOCK lipsync: muxing audio over video")
    settings.run_ffmpeg(
        [
            "-i", video_path,
            "-i", audio_path,
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-c:v", "copy",
            "-c:a", "aac",
            "-shortest",
            out_path,
        ],
        logger,
    )
    return out_path


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def lipsync(
    video_path: str,
    audio_path: str,
    out_path: str,
    logger=None,
) -> str:
    """
    Apply lip-sync of *audio_path* onto the talking-head in *video_path*.

    In mock mode (SYNC_MOCK=1 or no SYNC_SO_API_KEY set) this simply muxes
    the audio over the video without any lip-sync processing.

    In real mode:
      1. Upload both files to Sync.so's asset storage (POST /v2/assets/upload
         + PUT to the returned S3 presigned URL) to get public HTTPS URLs.
      2. POST to https://api.sync.so/v2/generate with model "lipsync-2".
      3. Poll GET /v2/generate/<id> until status is COMPLETED.
      4. Download the outputUrl to out_path.

    On ANY exception in the real path, falls back to mock mux and logs clearly.

    Parameters
    ----------
    video_path : path to the source video (talking-head clip)
    audio_path : path to the replacement audio (TTS or recorded voice)
    out_path   : destination path for the lip-synced output
    logger     : optional Logger

    Returns
    -------
    out_path (str)
    """
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)

    mock_mode = (
        settings.get_env("SYNC_MOCK") == "1"
        or not settings.has_key("SYNC_SO_API_KEY")
    )

    if mock_mode:
        _log(logger, "info",
             "SYNC.SO lipsync FALLBACK to mock mux (reason: SYNC_MOCK=1 or no SYNC_SO_API_KEY)")
        return _mock_mux(video_path, audio_path, out_path, logger)

    # --- Real Sync.so path ---------------------------------------------------
    api_key = settings.get_env("SYNC_SO_API_KEY")

    try:
        # Step 1: Upload video and audio to Sync.so asset storage
        _log(logger, "info", "Sync.so real lipsync: uploading video %s", video_path)
        video_url = _upload_file_to_syncso(video_path, api_key, logger)

        _log(logger, "info", "Sync.so real lipsync: uploading audio %s", audio_path)
        audio_url = _upload_file_to_syncso(audio_path, api_key, logger)

        # Step 2: Submit lipsync job
        job_id = _submit_lipsync_job(video_url, audio_url, api_key, logger)

        # Step 3: Poll until done
        output_url = _poll_lipsync_job(job_id, api_key, logger)

        # Step 4: Download result
        _download_output(output_url, out_path, logger)

        _log(logger, "info", "SYNC.SO LIVE lipsync completed -> %s", out_path)
        return out_path

    except Exception as exc:
        _log(logger, "error",
             "SYNC.SO lipsync FALLBACK to mock mux (reason: %s: %s)",
             type(exc).__name__, exc)
        return _mock_mux(video_path, audio_path, out_path, logger)


# ---------------------------------------------------------------------------
# __main__ smoke-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    log = settings.get_logger("lipsync.test")

    video_src = str(
        settings.CLIENTS_DIR
        / "demo" / "green_screen" / "person_talking_nogreen.mp4"
    )
    audio_candidate = Path(settings.CLIENTS_DIR / "demo" / "voice" / "sample.mp3")
    dst = str(settings.OUTPUT_DIR / "test_lipsync.mp4")

    # Verify the mp3 is decodable; if not, extract audio from the video itself
    try:
        raw_sr = settings.run_ffprobe(
            ["-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=sample_rate",
             "-of", "csv=p=0", str(audio_candidate)],
        ).strip()
        if not raw_sr or raw_sr == "0":
            raise ValueError("sample_rate is 0 or missing")
        audio_src = str(audio_candidate)
        log.info("Using sample.mp3 as audio source")
    except Exception as probe_err:
        log.warning(
            "sample.mp3 appears unreadable (%s); extracting audio from source video instead",
            probe_err,
        )
        extracted = str(settings.OUTPUT_DIR / "test_lipsync_audio.aac")
        Path(extracted).parent.mkdir(parents=True, exist_ok=True)
        settings.run_ffmpeg(
            ["-i", video_src, "-vn", "-c:a", "aac", "-ar", "44100", "-ac", "2",
             extracted],
            log,
        )
        audio_src = extracted
        log.info("Extracted audio -> %s", extracted)

    log.info("Running lipsync: video=%s audio=%s -> %s", video_src, audio_src, dst)
    result = lipsync(video_src, audio_src, dst, logger=log)
    log.info("Done -> %s", result)

    # Probe and print result info
    try:
        info_raw = settings.run_ffprobe(
            [
                "-v", "error",
                "-show_entries",
                "format=duration:stream=codec_name,width,height",
                "-of", "json",
                dst,
            ],
            log,
        )
        info = json.loads(info_raw)
        streams = info.get("streams", [])
        fmt = info.get("format", {})
        for s in streams:
            codec = s.get("codec_name")
            w = s.get("width", "—")
            h = s.get("height", "—")
            print(f"  stream codec={codec}  {w}x{h}")
        print(f"  duration : {fmt.get('duration')}s")
    except Exception as exc:
        log.warning("Probe failed: %s", exc)

    sys.exit(0)
