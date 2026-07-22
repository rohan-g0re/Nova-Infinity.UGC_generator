"""
pipeline/fftools.py — FFmpeg utility helpers for the video generation pipeline.

Public API:
    probe_duration(path, logger=None) -> float
    normalize_clip(in_path, out_path, duration_s=None, has_audio=True, logger=None) -> str
"""

from __future__ import annotations

import json
from pathlib import Path

import pipeline.settings as settings


# ---------------------------------------------------------------------------
# probe_duration
# ---------------------------------------------------------------------------

def probe_duration(path: str, logger=None) -> float:
    """
    Return the duration of *path* in seconds as a float.

    Tries three ffprobe strategies in order:
      1. format=duration CSV
      2. stream v:0 duration CSV
      3. format=duration JSON
    Returns 0.0 if all strategies fail.
    """
    # Strategy 1: format duration, CSV
    try:
        raw = settings.run_ffprobe(
            ["-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            logger,
        )
        token = raw.strip().split(",")[0].strip()
        if token:
            return float(token)
    except Exception:
        pass

    # Strategy 2: stream v:0 duration, CSV
    try:
        raw = settings.run_ffprobe(
            ["-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=duration",
             "-of", "csv=p=0", path],
            logger,
        )
        token = raw.strip().split(",")[0].strip()
        if token:
            return float(token)
    except Exception:
        pass

    # Strategy 3: format duration, JSON
    try:
        raw = settings.run_ffprobe(
            ["-v", "error", "-show_entries", "format=duration",
             "-of", "json", path],
            logger,
        )
        data = json.loads(raw)
        val = data.get("format", {}).get("duration")
        if val is not None:
            return float(val)
    except Exception:
        pass

    return 0.0


# ---------------------------------------------------------------------------
# normalize_clip
# ---------------------------------------------------------------------------

def normalize_clip(
    in_path: str,
    out_path: str,
    duration_s: float | None = None,
    has_audio: bool = True,
    logger=None,
) -> str:
    """
    Re-encode *in_path* to exactly 1080×1920 px, 30 fps, h264 yuv420p, aac 44100 Hz stereo.

    Scale+pad to fit 9:16, letterboxing/pillarboxing as needed.

    Parameters
    ----------
    in_path     : source clip (any format ffmpeg can read)
    out_path    : destination path (parent dirs created automatically)
    duration_s  : if given, trim/pad output to this exact length in seconds
    has_audio   : False → synthesise silent audio track via anullsrc
    logger      : optional Logger

    Returns
    -------
    out_path (str)
    """
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)

    BASE_VF = (
        "scale=1080:1920:force_original_aspect_ratio=decrease,"
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2,"
        "setsar=1,"
        "fps=30"
    )
    TPAD_SUFFIX = ",tpad=stop_mode=clone:stop_duration=999"
    ENCODE = [
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-ar", "44100",
        "-ac", "2",
    ]

    if has_audio and duration_s is None:
        # Case A: has audio, no trim
        args = (
            ["-i", in_path,
             "-vf", BASE_VF]
            + ENCODE
            + [out_path]
        )

    elif has_audio and duration_s is not None:
        # Case B: has audio, with trim/pad
        vf = BASE_VF + TPAD_SUFFIX
        args = (
            ["-i", in_path,
             "-vf", vf]
            + ENCODE
            + ["-af", "apad",
               "-t", str(duration_s),
               out_path]
        )

    elif not has_audio and duration_s is None:
        # Case C: no audio, no trim
        args = (
            ["-i", in_path,
             "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
             "-vf", BASE_VF]
            + ENCODE
            + ["-shortest", out_path]
        )

    else:
        # Case D: no audio, with trim/pad
        vf = BASE_VF + TPAD_SUFFIX
        args = (
            ["-i", in_path,
             "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
             "-vf", vf]
            + ENCODE
            + ["-t", str(duration_s),
               out_path]
        )

    settings.run_ffmpeg(args, logger)
    return out_path


# ---------------------------------------------------------------------------
# __main__ smoke-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    log = settings.get_logger("fftools.test")

    src = str(
        settings.CLIENTS_DIR
        / "demo" / "green_screen" / "person_talking_nogreen.mp4"
    )
    dst = str(settings.OUTPUT_DIR / "test_norm.mp4")

    log.info("Normalising %s -> %s (duration_s=5.0)", src, dst)
    normalize_clip(src, dst, duration_s=5.0, has_audio=True, logger=log)
    log.info("Done. Probing output...")

    # Print width, height, duration, codecs
    try:
        info_raw = settings.run_ffprobe(
            [
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height,codec_name,duration",
                "-of", "json",
                dst,
            ],
            log,
        )
        info = json.loads(info_raw)
        vs = info.get("streams", [{}])[0]
        print(f"  width   : {vs.get('width')}")
        print(f"  height  : {vs.get('height')}")
        print(f"  v_codec : {vs.get('codec_name')}")
        print(f"  v_dur   : {vs.get('duration')}")
    except Exception as exc:
        log.warning("Video stream probe failed: %s", exc)

    try:
        a_raw = settings.run_ffprobe(
            [
                "-v", "error",
                "-select_streams", "a:0",
                "-show_entries", "stream=codec_name,duration",
                "-of", "json",
                dst,
            ],
            log,
        )
        a_info = json.loads(a_raw)
        as_ = a_info.get("streams", [{}])[0]
        print(f"  a_codec : {as_.get('codec_name')}")
        print(f"  a_dur   : {as_.get('duration')}")
    except Exception as exc:
        log.warning("Audio stream probe failed: %s", exc)

    dur = probe_duration(dst, log)
    print(f"  probe_duration() -> {dur:.3f}s")
    sys.exit(0)
