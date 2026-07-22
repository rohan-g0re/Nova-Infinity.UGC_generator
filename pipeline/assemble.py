"""pipeline/assemble.py — timeline building and segment concatenation."""

from __future__ import annotations

import logging
from pathlib import Path

import pipeline.settings as settings
from pipeline.fftools import probe_duration, normalize_clip


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BROLL_BEATS: set[str] = {"demo", "problem"}


# ---------------------------------------------------------------------------
# build_timeline
# ---------------------------------------------------------------------------

def build_timeline(
    talking_clips: list[str],
    broll_clips: list[str],
    beats: list[dict],
    work_dir: str,
    logger: logging.Logger | None = None,
) -> list[str]:
    """
    Build a list of normalised per-beat segment paths.

    For each beat:
      - "demo" / "problem" beats → prefer the matching broll clip
      - All other beats ("hook", "proof", "cta") → prefer the talking clip
    Falls back gracefully when lists are shorter than `beats`.

    Parameters
    ----------
    talking_clips : one lipsynced/talking-head clip per beat (or fewer)
    broll_clips   : one broll clip per beat (or fewer)
    beats         : list of beat dicts with keys: beat, text, duration_s, ...
    work_dir      : directory for intermediate segment files
    logger        : optional Logger

    Returns
    -------
    List of absolute paths to normalised segment .mp4 files (one per beat).
    """
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)

    def _valid(paths: list[str], idx: int) -> str | None:
        """Return paths[idx % len] if it exists on disk, else None."""
        if not paths:
            return None
        p = paths[idx % len(paths)]
        return p if p and Path(p).exists() else None

    def _first_valid(paths: list[str]) -> str | None:
        """Return the first path in *paths* that exists on disk."""
        for p in paths:
            if p and Path(p).exists():
                return p
        return None

    segments: list[str] = []

    for i, beat in enumerate(beats):
        beat_name: str = beat.get("beat", "")
        duration_s: float = float(beat.get("duration_s", 5.0))

        # --- choose source clip ---
        chosen: str | None = None

        if beat_name in BROLL_BEATS:
            chosen = _valid(broll_clips, i)
            if chosen is None:
                chosen = _valid(talking_clips, i)
        else:
            chosen = _valid(talking_clips, i)
            if chosen is None:
                chosen = _valid(broll_clips, i)

        # final fallback: any valid clip from either list
        if chosen is None:
            chosen = _first_valid(talking_clips) or _first_valid(broll_clips)

        if chosen is None:
            raise FileNotFoundError(
                f"build_timeline: no valid clip found for beat {i} ({beat_name!r}). "
                f"talking_clips={talking_clips}, broll_clips={broll_clips}"
            )

        if logger:
            logger.info(
                "beat %d (%s): %s  -> seg_%02d.mp4  (%.2fs)",
                i, beat_name, chosen, i, duration_s,
            )

        out_seg = str(work / f"seg_{i:02d}.mp4")
        normalize_clip(chosen, out_seg, duration_s=duration_s, has_audio=True, logger=logger)
        segments.append(out_seg)

    return segments


# ---------------------------------------------------------------------------
# concat_segments
# ---------------------------------------------------------------------------

def concat_segments(
    segment_paths: list[str],
    out_path: str,
    work_dir: str,
    logger: logging.Logger | None = None,
) -> str:
    """
    Concatenate *segment_paths* into a single video at *out_path*.

    Tries the fast concat-demuxer (copy) first; falls back to the
    concat filter (re-encode) if that fails.

    Parameters
    ----------
    segment_paths : ordered list of normalised segment .mp4 paths
    out_path      : destination file path
    work_dir      : directory for the concat list file
    logger        : optional Logger

    Returns
    -------
    out_path (str)
    """
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)

    # Build the ffconcat list file (forward slashes, absolute paths)
    concat_list_path = str(work / "concat_list.txt")
    lines = ["ffconcat version 1.0"]
    for p in segment_paths:
        # Use forward slashes for ffmpeg compatibility on Windows
        abs_fwd = Path(p).resolve().as_posix()
        lines.append(f"file '{abs_fwd}'")
    concat_text = "\n".join(lines) + "\n"

    with open(concat_list_path, "w", encoding="utf-8") as fh:
        fh.write(concat_text)

    if logger:
        logger.info("concat list -> %s", concat_list_path)

    # --- Attempt 1: concat demuxer (stream-copy, fast) ---
    try:
        args = [
            "-f", "concat",
            "-safe", "0",
            "-i", concat_list_path,
            "-c", "copy",
            out_path,
        ]
        settings.run_ffmpeg(args, logger)
        if logger:
            logger.info("concat demuxer succeeded -> %s", out_path)
        return out_path
    except RuntimeError as exc:
        if logger:
            logger.warning(
                "concat demuxer failed (%s); falling back to filter re-encode", exc
            )

    # --- Attempt 2: concat filter (re-encode) ---
    inputs: list[str] = []
    for p in segment_paths:
        inputs.extend(["-i", p])

    n = len(segment_paths)
    filter_parts = "".join(f"[{i}:v][{i}:a]" for i in range(n))
    filter_complex = f"{filter_parts}concat=n={n}:v=1:a=1[outv][outa]"

    args = (
        inputs
        + [
            "-filter_complex", filter_complex,
            "-map", "[outv]",
            "-map", "[outa]",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-ar", "44100",
            "-ac", "2",
            out_path,
        ]
    )
    settings.run_ffmpeg(args, logger)
    if logger:
        logger.info("concat filter succeeded -> %s", out_path)
    return out_path


# ---------------------------------------------------------------------------
# __main__ smoke-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    log = settings.get_logger("assemble.__main__")

    ROOT = Path("C:/Users/rohan/Desktop/Work/STUFF/Projects/video_generator")
    out_dir = ROOT / "output"
    out_dir.mkdir(parents=True, exist_ok=True)

    vid1 = str(ROOT / "assets/clients/demo/green_screen/person_talking_nogreen.mp4")
    vid2 = str(ROOT / "assets/clients/demo/green_screen/person_talking2_nogreen.mp4")

    work_dir = str(out_dir / "test_assemble_work")
    Path(work_dir).mkdir(parents=True, exist_ok=True)

    # Test normalize_clip on both videos
    log.info("Normalizing clip 1...")
    norm1 = normalize_clip(vid1, str(Path(work_dir) / "norm1.mp4"), duration_s=5.0, logger=log)
    log.info("Normalizing clip 2...")
    norm2 = normalize_clip(vid2, str(Path(work_dir) / "norm2.mp4"), duration_s=5.0, logger=log)

    # Test concat
    log.info("Concatenating segments...")
    final = concat_segments([norm1, norm2], str(out_dir / "test_concat.mp4"), work_dir, log)

    # ffprobe final
    probe = settings.run_ffprobe([
        "-v", "error",
        "-show_entries",
        "format=duration:stream=codec_name,codec_type,width,height",
        "-of", "json",
        final,
    ], log)
    print("CONCAT RESULT:")
    print(probe)

    sys.exit(0)
