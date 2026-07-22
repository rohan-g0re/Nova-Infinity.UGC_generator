"""pipeline/audio.py — VO + music mixing and audio mux."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from pipeline.settings import (
    FFPROBE,
    OUTPUT_DIR,
    ASSETS_DIR,
    get_logger,
    run_ffmpeg,
    run_ffprobe,
)


def mix_vo_music(
    vo_path: str,
    music_path: str | None,
    out_path: str,
    music_gain_db: float = -12.0,
    logger=None,
) -> str:
    """Mix a voice-over with optional background music.

    If music_path is None or missing the VO is simply loudnorm-ed and
    re-encoded.  Otherwise the music is ducked under the VO via
    sidechaincompress and the two are mixed + loudnorm-ed.

    Returns out_path.
    """
    if logger is None:
        logger = get_logger("audio.mix_vo_music")

    out_path = str(out_path)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)

    music_valid = music_path is not None and Path(music_path).is_file()

    if not music_valid:
        logger.info("No valid music_path — loudnorm-only pass on VO.")
        args = [
            "-i", vo_path,
            "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
            "-c:a", "aac",
            "-ar", "44100",
            "-ac", "2",
            out_path,
        ]
    else:
        filter_complex = (
            "[1:a]asplit=2[vo][sidechain];"
            f"[0:a]volume={music_gain_db}dB[music];"
            "[music][sidechain]sidechaincompress=threshold=0.02:ratio=6:attack=10:release=200[ducked];"
            "[ducked][vo]amix=inputs=2:duration=first:dropout_transition=3[mixed];"
            "[mixed]loudnorm=I=-14:TP=-1.5:LRA=11[out]"
        )
        args = [
            "-stream_loop", "-1", "-i", music_path,
            "-i", vo_path,
            "-filter_complex", filter_complex,
            "-map", "[out]",
            "-c:a", "aac",
            "-ar", "44100",
            "-ac", "2",
            "-shortest",
            out_path,
        ]

    run_ffmpeg(args, logger)
    logger.info("mix_vo_music -> %s", out_path)
    return out_path


def mux_audio_into_video(
    video_path: str,
    audio_path: str,
    out_path: str,
    logger=None,
) -> str:
    """Replace the audio track of video_path with audio_path.

    Video stream is stream-copied; audio is re-encoded to aac 44100 stereo.
    Returns out_path.
    """
    if logger is None:
        logger = get_logger("audio.mux_audio_into_video")

    out_path = str(out_path)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)

    args = [
        "-i", video_path,
        "-i", audio_path,
        "-map", "0:v",
        "-map", "1:a",
        "-c:v", "copy",
        "-c:a", "aac",
        "-ar", "44100",
        "-ac", "2",
        "-shortest",
        out_path,
    ]
    run_ffmpeg(args, logger)
    logger.info("mux_audio_into_video -> %s", out_path)
    return out_path


# ---------------------------------------------------------------------------
# Quick smoke-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    log = get_logger("audio.__main__")

    # sample.mp3 may be corrupt — probe it first; fall back to a generated
    # 5-second silent tone so the mix test can always run.
    _sample = ASSETS_DIR / "clients" / "demo" / "voice" / "sample.mp3"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    _fallback_vo = OUTPUT_DIR / "_test_vo.mp3"

    try:
        _probe = subprocess.run(
            [FFPROBE, "-hide_banner", "-v", "error",
             "-show_entries", "stream=sample_rate,channels",
             "-of", "json", str(_sample)],
            capture_output=True, text=True,
        )
        _info = json.loads(_probe.stdout)
        _sr = int((_info.get("streams") or [{}])[0].get("sample_rate") or 0)
        if _sr > 0:
            vo = str(_sample)
            log.info("Using sample.mp3 as VO.")
        else:
            raise ValueError("sample_rate=0")
    except Exception as _e:
        log.warning("sample.mp3 unusable (%s); generating 5s silent VO.", _e)
        run_ffmpeg([
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-t", "5",
            "-c:a", "libmp3lame", "-ar", "44100", "-ac", "2",
            str(_fallback_vo),
        ], log)
        vo = str(_fallback_vo)

    music = str(ASSETS_DIR / "music" / "track.mp3")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out = str(OUTPUT_DIR / "test_audio_mix.m4a")

    result = mix_vo_music(vo, music, out, music_gain_db=-12.0, logger=log)
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
        if ctype == "audio":
            print(
                f"Audio    : {cname}  sr={s.get('sample_rate')}  "
                f"ch={s.get('channels')}  "
                f"bitrate={s.get('bit_rate', '?')}bps"
            )
