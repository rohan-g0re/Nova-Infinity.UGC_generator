"""
pipeline/tts.py — voice cloning + TTS synthesis for the UGC ad pipeline.

REAL path: ElevenLabs SDK (IVC clone + text_to_speech).
MOCK path: gTTS (internet) → silent ffmpeg clip (offline fallback).
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from pathlib import Path

from pipeline import settings

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Well-known ElevenLabs premade voice used as stock fallback when IVC clone
# fails due to subscription tier restrictions.  "Charlie" is a premade voice
# available on all tiers including Free.
STOCK_FALLBACK_VOICE_ID = "IKne3meq5aSn9XLyUdCD"  # Charlie – premade, all tiers

# Status codes that are transient and safe to retry.
_RETRYABLE_STATUS_CODES = (429, 500, 502, 503, 504)
# Status codes that indicate a permanent client/auth/payment error — do NOT retry.
_PERMANENT_STATUS_CODES = (400, 401, 402, 403)


def _elevenlabs_call_with_retry(fn, *, max_retries=3, base_delay=1.0, logger=None):
    """
    Call *fn()* (a zero-argument callable wrapping an ElevenLabs SDK call)
    with exponential backoff on transient errors (429/5xx).

    Raises immediately on permanent errors (400/401/402/403) or after
    exhausting retries, so callers can apply their own fallback logic.
    """
    from elevenlabs.core import ApiError  # type: ignore

    last_exc = None
    for attempt in range(max_retries):
        try:
            return fn()
        except ApiError as exc:
            status = exc.status_code
            if status in _PERMANENT_STATUS_CODES:
                # Auth/payment/bad-request — not worth retrying
                raise
            if status in _RETRYABLE_STATUS_CODES and attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                if logger:
                    logger.warning(
                        "ElevenLabs API HTTP %s; retry %d/%d in %.1fs",
                        status, attempt + 1, max_retries, delay,
                    )
                time.sleep(delay)
                last_exc = exc
                continue
            raise
        except Exception as exc:
            # Connection / network errors — retry on these too
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                if logger:
                    logger.warning(
                        "ElevenLabs call error (%s); retry %d/%d in %.1fs",
                        exc, attempt + 1, max_retries, delay,
                    )
                time.sleep(delay)
                last_exc = exc
                continue
            raise
    # Should not reach here, but guard anyway
    if last_exc:
        raise last_exc


# ---------------------------------------------------------------------------
# Voice cloning
# ---------------------------------------------------------------------------

def clone_voice(
    sample_path: str,
    name: str = "demo",
    logger: logging.Logger | None = None,
) -> str | None:
    """
    Clone a voice from a local audio sample via ElevenLabs IVC.

    Returns the cloned voice_id string on success.

    If the ElevenLabs API key is present but IVC clone fails due to subscription
    tier restrictions (free plan does not include instant voice cloning), falls
    back to a prebuilt stock voice so that ElevenLabs TTS can still be used
    instead of gTTS.  Logs clearly which path was taken.

    Returns None only when there is no ELEVENLABS_API_KEY at all (mock mode).
    """
    if logger is None:
        logger = settings.get_logger("tts")

    if not settings.has_key("ELEVENLABS_API_KEY"):
        logger.info("No ELEVENLABS_API_KEY - voice cloning skipped (mock TTS will be used)")
        return None

    try:
        from elevenlabs.client import ElevenLabs  # type: ignore

        api_key = settings.get_env("ELEVENLABS_API_KEY")
        client = ElevenLabs(api_key=api_key)

        with open(sample_path, "rb") as f:
            # Try IVC namespace first; fall back to voices.add across SDK versions
            try:
                def _do_clone_ivc():
                    return client.voices.ivc.create(name=name, files=[f])
                result = _elevenlabs_call_with_retry(_do_clone_ivc, logger=logger)
            except AttributeError:
                f.seek(0)
                def _do_clone_add():
                    return client.voices.add(name=name, files=[f])
                result = _elevenlabs_call_with_retry(_do_clone_add, logger=logger)

        voice_id: str = result.voice_id
        logger.info("Cloned voice '%s' -> voice_id=%s [cloned voice]", name, voice_id)
        return voice_id

    except Exception as exc:
        # Tier/permission errors (e.g. "can_not_use_instant_voice_cloning") mean
        # IVC is unavailable on this plan.  Fall back to a stock premade voice so
        # ElevenLabs TTS is still used instead of gTTS.
        logger.warning(
            "ElevenLabs clone_voice failed (%s) — falling back to stock voice %s [stock voice fallback]",
            exc,
            STOCK_FALLBACK_VOICE_ID,
        )
        return STOCK_FALLBACK_VOICE_ID


# ---------------------------------------------------------------------------
# Single-utterance synthesis
# ---------------------------------------------------------------------------

def synthesize(
    text: str,
    out_path: str,
    voice_id: str | None = None,
    logger: logging.Logger | None = None,
) -> str:
    """
    Synthesize *text* to an mp3 at *out_path*.

    Priority:
      1. ElevenLabs (ELEVENLABS_API_KEY set AND voice_id provided)
      2. gTTS (no key or no voice_id, but internet available)
      3. Silent ffmpeg clip of estimated spoken length (fully offline fallback)

    Always returns out_path.
    """
    if logger is None:
        logger = settings.get_logger("tts")

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)

    # --- Real path: ElevenLabs ---
    if settings.has_key("ELEVENLABS_API_KEY") and voice_id:
        try:
            from elevenlabs.client import ElevenLabs  # type: ignore

            api_key = settings.get_env("ELEVENLABS_API_KEY")
            client = ElevenLabs(api_key=api_key)

            def _do_tts():
                return client.text_to_speech.convert(
                    voice_id=voice_id,
                    model_id="eleven_flash_v2_5",
                    text=text,
                    output_format="mp3_44100_128",
                )

            audio_iter = _elevenlabs_call_with_retry(_do_tts, logger=logger)
            with open(out_path, "wb") as f:
                for chunk in audio_iter:
                    if chunk:
                        f.write(chunk)

            logger.info("ElevenLabs TTS -> %s", out_path)
            return out_path

        except Exception as exc:
            logger.warning("ElevenLabs TTS failed (%s) - falling back to gTTS/silent", exc)

    # --- Mock path: gTTS ---
    try:
        from gtts import gTTS  # type: ignore

        gTTS(text=text, lang="en").save(out_path)
        logger.info("gTTS TTS -> %s", out_path)
        return out_path

    except Exception as exc:
        logger.warning("gTTS failed (%s) - generating silent placeholder audio", exc)
        return _silent_mp3(text, out_path, logger)


def _silent_mp3(text: str, out_path: str, logger: logging.Logger) -> str:
    """Generate a silent mp3 of estimated spoken duration via ffmpeg."""
    word_count = max(1, len(text.split()))
    duration_s = max(1.5, word_count / 2.7)

    settings.run_ffmpeg(
        [
            "-f", "lavfi",
            "-i", f"anullsrc=r=44100:cl=mono:d={duration_s:.2f}",
            "-acodec", "libmp3lame",
            "-b:a", "128k",
            "-t", str(duration_s),
            out_path,
        ],
        logger=logger,
    )
    logger.info("Silent placeholder audio (%.1fs) -> %s", duration_s, out_path)
    return out_path


# ---------------------------------------------------------------------------
# Beat-level batch synthesis
# ---------------------------------------------------------------------------

def synthesize_beats(
    beats: list,
    work_dir: str,
    voice_id: str | None = None,
    logger: logging.Logger | None = None,
) -> list[str]:
    """
    Synthesize each beat's text to work_dir/vo_<i>.mp3.

    Returns an ordered list of output paths.
    """
    if logger is None:
        logger = settings.get_logger("tts")

    Path(work_dir).mkdir(parents=True, exist_ok=True)
    paths: list[str] = []

    for i, beat in enumerate(beats):
        text = beat.get("text", "")
        out_path = str(Path(work_dir) / f"vo_{i}.mp3")
        paths.append(synthesize(text, out_path, voice_id=voice_id, logger=logger))

    logger.info("Synthesized %d beat VO files in %s", len(paths), work_dir)
    return paths


# ---------------------------------------------------------------------------
# __main__
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json
    import sys

    log = settings.get_logger("tts.__main__")

    with tempfile.TemporaryDirectory() as tmpdir:
        out = str(Path(tmpdir) / "hello.mp3")
        result_path = synthesize("Hello from the pipeline", out, logger=log)

        # ffprobe duration
        probe = settings.run_ffprobe(
            [
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "json",
                result_path,
            ],
            logger=log,
        )
        info = json.loads(probe)
        duration = float(info["format"]["duration"])

        print(f"Output path : {result_path}")
        print(f"Duration    : {duration:.2f}s")

    sys.exit(0)
