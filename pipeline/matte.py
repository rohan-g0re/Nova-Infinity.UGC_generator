"""pipeline/matte.py — chromakey composite and background overlay."""

from __future__ import annotations

import json
from pathlib import Path

from pipeline import settings
from pipeline.settings import (
    FFPROBE,
    OUTPUT_DIR,
    ASSETS_DIR,
    get_logger,
    run_ffmpeg,
    run_ffprobe,
)

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}


# ---------------------------------------------------------------------------
# Audio-probe helper
# ---------------------------------------------------------------------------

def _probe_has_audio(path: str, logger=None) -> bool:
    """Return True if *path* contains at least one audio stream."""
    try:
        raw = run_ffprobe(
            ["-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_type",
             "-of", "csv=p=0", path],
            logger,
        )
        return bool(raw.strip())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Filter building blocks
# ---------------------------------------------------------------------------

def _build_chroma_filter(key_color: str, similarity: float, blend: float) -> str:
    """Build the crop + colorkey + despill filter chain for the FG person.

    The source green-screen clip is 1920x1080 landscape with the presenter in
    the center. Scaling the whole landscape frame to the 1080-wide portrait
    canvas shrank the person to a tiny inset. Instead we:

      1. Center-crop a 9:16 slice of the source (``ih*9/16 x ih``) so the frame
         tightly contains the presenter (for 1920x1080 this is ~608x1080).
      2. Reset PTS so the seeked FG frames align with the background under
         overlay (otherwise overlay shows only the base).
      3. colorkey on RGB distance to remove the dark-teal screen (chromakey
         cannot separate the desaturated screen from a black shirt).
      4. despill the residual green fringe and erode the alpha by one pixel to
         kill the white edge halo.
      5. Scale the keyed presenter to ~1620px tall (~85% of the 1920 canvas)
         so, anchored to the bottom center, they read as a natural presenter in
         the scene (roughly two-thirds+ of frame height) rather than an inset.

    ``similarity``/``blend`` default to the caller's values (settings.CHROMA_*).

    Note on despill: the source screen is a *desaturated* dark-teal, and the
    presenter wears a black shirt whose RGB distance to that screen is small.
    That forces ``similarity`` to stay low (~0.13) — raising it keys the shirt.
    A ``despill=type=green`` pass was previously applied here, but at any usable
    mix it desaturated the green-lit skin and turned the face into white/grey
    posterized patches (verified on the live source). Despill is therefore
    omitted; a single ``erosion`` handles the residual green edge fringe, which
    is a far better trade-off than a wrecked face.
    """
    return (
        # Center-crop a 9:16 slice of the source around the presenter.
        f"crop=ih*9/16:ih:(iw-ih*9/16)/2:0,"
        # Align seeked FG timestamps with the background for overlay.
        f"setpts=PTS-STARTPTS,"
        # Colorkey on RGB distance (removes the dark-teal screen). Kept at a low
        # similarity so the black shirt is not keyed out along with the screen.
        f"colorkey={key_color}:{similarity}:{blend},"
        # Erode the alpha one pixel to trim the residual green edge fringe.
        # (No despill: it turns green-lit skin into white patches on this source.)
        f"erosion,"
        # Ensure YUVA for alpha compositing.
        f"format=yuva420p,"
        # Scale the presenter to ~1620px tall (~two-thirds+ of the canvas).
        f"scale=-2:1620,"
        f"setsar=1"
    )


def _build_bg_filter() -> str:
    """Build the background scale-to-cover filter chain."""
    return (
        "scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,"
        "setsar=1"
    )


# ---------------------------------------------------------------------------
# composite_over_background  (NEW — primary function for per-beat backgrounds)
# ---------------------------------------------------------------------------

def composite_over_background(
    fg_video: str,
    bg_clip: str,
    out_path: str,
    key_color: str | None = None,
    similarity: float | None = None,
    blend: float | None = None,
    logger=None,
) -> str:
    """Composite a green-screen fg_video over a pre-processed bg_clip (video).

    bg_clip is expected to already be 1080x1920 (produced by backgrounds.fetch_background).
    The person is colorkeyed (dark-teal screen removed via RGB distance), scaled
    to fit the 1080-wide frame, centered horizontally, and anchored to the bottom
    of frame (feet near the bottom edge).

    Key color/similarity/blend default to settings.CHROMA_* when the caller passes
    None. Outputs a 1080x1920 h264 yuv420p file carrying the FG audio. Falls back
    to passthrough_over_bg on failure.
    """
    if logger is None:
        logger = get_logger("matte.composite_over_background")

    key_color = key_color or settings.CHROMA_KEY_COLOR
    similarity = settings.CHROMA_SIMILARITY if similarity is None else similarity
    blend = settings.CHROMA_BLEND if blend is None else blend

    out_path = str(out_path)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)

    chroma_chain = _build_chroma_filter(key_color, similarity, blend)
    bg_chain = _build_bg_filter()

    # bg_clip is input [0], fg_video is input [1]
    filter_complex = (
        f"[0:v]{bg_chain}[bg];"
        f"[1:v]{chroma_chain}[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h),fps=30[out]"
    )

    # Detect whether fg_video has an audio stream
    _has_audio = _probe_has_audio(fg_video, logger)

    try:
        if _has_audio:
            args = [
                "-i", bg_clip,
                "-i", fg_video,
                "-filter_complex", filter_complex,
                "-map", "[out]",
                "-map", "1:a:0",
                "-c:v", "libx264",
                "-preset", "fast",
                "-crf", "23",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac",
                "-ar", "44100",
                "-ac", "2",
                "-shortest",
                out_path,
            ]
        else:
            # No audio in fg — synthesise silence
            args = [
                "-i", bg_clip,
                "-i", fg_video,
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                "-filter_complex", filter_complex,
                "-map", "[out]",
                "-map", "2:a:0",
                "-c:v", "libx264",
                "-preset", "fast",
                "-crf", "23",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac",
                "-ar", "44100",
                "-ac", "2",
                "-shortest",
                out_path,
            ]
        run_ffmpeg(args, logger)
        logger.info("composite_over_background -> %s", out_path)
        return out_path

    except Exception as exc:
        logger.warning(
            "composite_over_background failed (%s); falling back to "
            "chromakey_composite.", exc
        )
        try:
            return chromakey_composite(
                fg_video, bg_clip, out_path,
                key_color=key_color, similarity=similarity, blend=blend,
                logger=logger,
            )
        except Exception as exc2:
            logger.warning(
                "chromakey_composite fallback failed (%s); falling back to "
                "passthrough.", exc2
            )
            return passthrough_over_bg(fg_video, out_path, logger)


# ---------------------------------------------------------------------------
# chromakey_composite  (legacy — image or video bg, same improved logic)
# ---------------------------------------------------------------------------

def chromakey_composite(
    fg_video: str,
    bg_image_or_video: str,
    out_path: str,
    key_color: str | None = None,
    similarity: float | None = None,
    blend: float | None = None,
    logger=None,
) -> str:
    """Composite fg_video over bg_image_or_video using colorkey.

    Accepts an image or video background. The person is colorkeyed (dark-teal
    screen removed via RGB distance), scaled to fit the 1080-wide frame, and
    anchored to the bottom. Key color/similarity/blend default to
    settings.CHROMA_* when the caller passes None. Outputs a 1080x1920 portrait
    video (h264 yuv420p) carrying fg audio. Falls back to passthrough_over_bg on
    error.
    """
    if logger is None:
        logger = get_logger("matte.chromakey_composite")

    key_color = key_color or settings.CHROMA_KEY_COLOR
    similarity = settings.CHROMA_SIMILARITY if similarity is None else similarity
    blend = settings.CHROMA_BLEND if blend is None else blend

    out_path = str(out_path)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)

    bg_ext = Path(bg_image_or_video).suffix.lower()
    is_image = bg_ext in _IMAGE_EXTS

    chroma_chain = _build_chroma_filter(key_color, similarity, blend)
    bg_chain = _build_bg_filter()

    # bg is [0], fg is [1]
    filter_complex = (
        f"[0:v]{bg_chain}[bg];"
        f"[1:v]{chroma_chain}[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h),fps=30[out]"
    )

    _has_audio = _probe_has_audio(fg_video, logger)
    # fg index is 1 for image bg, 1 for video bg; silence is added as extra input when needed
    _audio_input_idx = 1 if _has_audio else 2

    try:
        if is_image:
            base_args = ["-loop", "1", "-i", bg_image_or_video, "-i", fg_video]
        else:
            base_args = ["-i", bg_image_or_video, "-i", fg_video]

        if not _has_audio:
            base_args += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]

        args = (
            base_args
            + [
                "-filter_complex", filter_complex,
                "-map", "[out]",
                "-map", f"{_audio_input_idx}:a:0",
                "-c:v", "libx264",
                "-preset", "fast",
                "-crf", "23",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac",
                "-ar", "44100",
                "-ac", "2",
                "-shortest",
                out_path,
            ]
        )

        run_ffmpeg(args, logger)
        logger.info("chromakey_composite -> %s", out_path)
        return out_path

    except Exception as exc:
        logger.warning(
            "chromakey_composite failed (%s); falling back to passthrough.", exc
        )
        return passthrough_over_bg(fg_video, out_path, logger)


# ---------------------------------------------------------------------------
# passthrough_over_bg  (final fallback)
# ---------------------------------------------------------------------------

def passthrough_over_bg(fg_video: str, out_path: str, logger=None) -> str:
    """Normalize fg_video to 1080x1920 without a background (fallback)."""
    if logger is None:
        logger = get_logger("matte.passthrough_over_bg")

    out_path = str(out_path)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)

    args = [
        "-i", fg_video,
        "-vf", (
            "scale=1080:1920:force_original_aspect_ratio=decrease,"
            "pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30"
        ),
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-ar", "44100",
        "-ac", "2",
        out_path,
    ]
    run_ffmpeg(args, logger)
    logger.info("passthrough_over_bg -> %s", out_path)
    return out_path


# ---------------------------------------------------------------------------
# Quick smoke-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import subprocess, sys

    log = get_logger("matte.__main__")

    fg = str(
        ASSETS_DIR
        / "clients" / "demo" / "green_screen"
        / "greenscreen_person_1.mp4"
    )
    bg = str(ASSETS_DIR / "backgrounds" / "office.jpg")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out = str(OUTPUT_DIR / "test_matte.mp4")

    result = chromakey_composite(fg, bg, out, logger=log)
    print(f"\nOutput: {result}")

    # ffprobe the result
    probe_args = [
        FFPROBE, "-hide_banner",
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        result,
    ]
    raw = subprocess.run(probe_args, capture_output=True, text=True)
    data = json.loads(raw.stdout)

    fmt = data.get("format", {})
    print(f"Duration : {float(fmt.get('duration', 0)):.3f}s")
    for s in data.get("streams", []):
        ctype = s.get("codec_type", "?")
        cname = s.get("codec_name", "?")
        if ctype == "video":
            print(
                f"Video    : {cname}  {s.get('width')}x{s.get('height')}  "
                f"pix_fmt={s.get('pix_fmt')}"
            )
        elif ctype == "audio":
            print(f"Audio    : {cname}  sr={s.get('sample_rate')}  ch={s.get('channels')}")
