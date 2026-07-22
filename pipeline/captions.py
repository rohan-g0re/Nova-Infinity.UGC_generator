"""pipeline/captions.py — word-level karaoke captions burned onto video."""

from __future__ import annotations

from pathlib import Path

import pipeline.settings as settings

# ---------------------------------------------------------------------------
# ASS header template
# ---------------------------------------------------------------------------
_ASS_HEADER = """\
[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,72,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,2,2,40,40,80,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ass_time(seconds: float) -> str:
    """Format seconds as ASS timestamp H:MM:SS.cs (centiseconds, 2 digits)."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int((seconds % 1) * 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _chunk_words(words: list[str], chunk_size: int = 4) -> list[list[str]]:
    """Split a list of words into chunks of at most *chunk_size*."""
    return [words[i : i + chunk_size] for i in range(0, len(words), chunk_size)]


def _escape_ass_path(path: str) -> str:
    """
    Convert a Windows path to the format the ffmpeg subtitles filter expects.

    ffmpeg on Windows requires forward slashes and the drive colon escaped
    as ``C\\:/path/to/file``.
    """
    p = str(path).replace("\\", "/")
    # Escape the colon after the drive letter: C:/path -> C\:/path
    if len(p) >= 2 and p[1] == ":":
        p = p[0] + "\\:" + p[2:]
    return p


# ---------------------------------------------------------------------------
# Function 1: transcribe_words
# ---------------------------------------------------------------------------

def transcribe_words(audio_path: str, logger=None) -> list[dict] | None:
    """
    Attempt word-level transcription via faster-whisper.

    Returns a list of dicts with keys ``word``, ``start``, ``end`` (floats in
    seconds), or None if faster-whisper is unavailable or transcription fails.
    """
    try:
        from faster_whisper import WhisperModel  # noqa: PLC0415

        model = WhisperModel("tiny", device="cpu", compute_type="int8")
        segments, _ = model.transcribe(audio_path, word_timestamps=True)
        words: list[dict] = []
        for segment in segments:
            if segment.words:
                for w in segment.words:
                    words.append({"word": w.word.strip(), "start": w.start, "end": w.end})
        return words if words else None
    except Exception as e:  # includes ImportError for missing ctranslate2
        if logger:
            logger.info("faster-whisper unavailable (%s), using text-timing fallback", e)
        return None


# ---------------------------------------------------------------------------
# Function 2: captions_from_beats
# ---------------------------------------------------------------------------

def captions_from_beats(
    beats: list[dict],
    out_ass_path: str,
    total_duration_s: float,
    logger=None,
) -> str:
    """
    Build an ASS subtitle file from a list of beat dicts.

    Each beat must have ``text`` (str) and ``duration_s`` (float).
    Words are split into chunks of ≤4 and distributed evenly across the
    beat's time window.  Cumulative time across beats is tracked so
    timestamps are continuous.

    Returns *out_ass_path*.
    """
    out_path = Path(out_ass_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = [_ASS_HEADER]
    cursor = 0.0  # running time offset in seconds

    for beat in beats:
        text: str = beat.get("text", "")
        beat_duration: float = float(beat.get("duration_s", 0.0))

        word_list = text.split()
        if not word_list:
            cursor += beat_duration
            continue

        chunks = _chunk_words(word_list, chunk_size=4)
        num_chunks = len(chunks)

        # Each chunk gets an equal slice of the beat, minimum 0.5 s
        chunk_dur = max(beat_duration / num_chunks, 0.5)

        for chunk in chunks:
            start = cursor
            end = cursor + chunk_dur
            chunk_text = " ".join(chunk)
            lines.append(
                f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},"
                f"Default,,0,0,0,,{chunk_text}"
            )
            cursor += chunk_dur

    out_path.write_text("".join(lines), encoding="utf-8-sig")

    if logger:
        logger.info("captions_from_beats: wrote %s", out_path)

    return str(out_path)


# ---------------------------------------------------------------------------
# Function 3: captions_from_audio
# ---------------------------------------------------------------------------

def captions_from_audio(
    audio_path: str,
    out_ass_path: str,
    logger=None,
) -> str | None:
    """
    Build an ASS subtitle file from word-level timestamps via faster-whisper.

    Returns *out_ass_path* on success, or None if faster-whisper is
    unavailable (caller should fall back to captions_from_beats).
    """
    words = transcribe_words(audio_path, logger=logger)
    if words is None:
        return None

    out_path = Path(out_ass_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = [_ASS_HEADER]

    # Group words into chunks of 4
    chunks: list[tuple[float, float, str]] = []
    for i in range(0, len(words), 4):
        group = words[i : i + 4]
        start = group[0]["start"]
        end = group[-1]["end"]
        text = " ".join(w["word"] for w in group)
        chunks.append((start, end, text))

    for start, end, text in chunks:
        lines.append(
            f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},"
            f"Default,,0,0,0,,{text}"
        )

    out_path.write_text("".join(lines), encoding="utf-8-sig")

    if logger:
        logger.info("captions_from_audio: wrote %s", out_path)

    return str(out_path)


# ---------------------------------------------------------------------------
# Function 4: burn_captions
# ---------------------------------------------------------------------------

def burn_captions(
    video_path: str,
    ass_path: str,
    out_path: str,
    logger=None,
) -> str:
    """
    Burn an ASS subtitle file onto *video_path* and write the result to
    *out_path*.

    Uses ffmpeg's ``subtitles`` filter.  On Windows the path must have its
    drive colon escaped (``C\\:/…``).

    Returns *out_path*.
    """
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    escaped = _escape_ass_path(ass_path)
    filter_str = f"subtitles=filename='{escaped}'"

    args = [
        "-i", video_path,
        "-vf", filter_str,
        "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", "44100", "-ac", "2",
        str(out),
    ]

    settings.run_ffmpeg(args, logger)

    if logger:
        logger.info("burn_captions: wrote %s", out)

    return str(out)


# ---------------------------------------------------------------------------
# __main__ smoke-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json

    logger = settings.get_logger("captions")

    PROJECT_ROOT = Path(__file__).resolve().parents[1]
    OUTPUT_DIR = PROJECT_ROOT / "output"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    VIDEO_SRC = str(
        PROJECT_ROOT
        / "assets"
        / "clients"
        / "demo"
        / "green_screen"
        / "person_talking_nogreen.mp4"
    )

    # ── 1. Mock beats ────────────────────────────────────────────────────────
    beats = [
        {
            "beat": "hook",
            "text": "Okay I have to talk about this water bottle because it genuinely changed my hydration game",
            "duration_s": 5.0,
        },
        {
            "beat": "cta",
            "text": "Grab yours using the link below they sell out fast just saying",
            "duration_s": 5.758,
        },
    ]
    total_duration_s = 10.758

    # ── 2. Build ASS from beats ───────────────────────────────────────────────
    ass_path = str(OUTPUT_DIR / "test_captions.ass")
    captions_from_beats(beats, ass_path, total_duration_s=total_duration_s, logger=logger)
    logger.info("ASS file written to: %s", ass_path)

    # ── 3. Burn captions onto the demo video ─────────────────────────────────
    burned_path = str(OUTPUT_DIR / "test_captions_burned.mp4")
    burn_captions(VIDEO_SRC, ass_path, burned_path, logger=logger)
    logger.info("Burned video written to: %s", burned_path)

    # ── 4. ffprobe the result ────────────────────────────────────────────────
    probe_args = [
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        burned_path,
    ]
    probe_out = settings.run_ffprobe(probe_args, logger)
    info = json.loads(probe_out)

    fmt = info.get("format", {})
    logger.info("Duration : %s s", fmt.get("duration"))

    for stream in info.get("streams", []):
        codec = stream.get("codec_name")
        codec_type = stream.get("codec_type")
        w = stream.get("width")
        h = stream.get("height")
        if codec_type == "video":
            logger.info("Video    : codec=%s  %sx%s", codec, w, h)
        elif codec_type == "audio":
            logger.info("Audio    : codec=%s  sr=%s  ch=%s", codec, stream.get("sample_rate"), stream.get("channels"))

    print("\n--- raw ffprobe JSON ---")
    print(probe_out)
