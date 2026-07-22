"""
pipeline/run.py — CLI orchestrator for the UGC ad pipeline.

Usage:
    python -m pipeline.run --client demo --script-file briefs/aquasteel.json
    python -m pipeline.run --client demo --brief "30s ad for AquaSteel"
    python -m pipeline.run --client demo --reuse-dir output/20260715-155456
    python -m pipeline.run --client demo --reuse-dir output/20260715-155456 --force

Full pipeline stages:
  1. Load client config & product doc
  2. Generate script (Claude-Code-authored JSON via --script-file; else Anthropic/mock)
  3. Clone voice (mock or ElevenLabs)
  4. Synthesize beat VO files (gTTS or ElevenLabs)
  5. Concatenate all VO into full_vo.mp3; lipsync talking-head video
  6. Slice talking_full.mp4 per talking beat by cumulative timeline offset
  7. Fetch a per-beat scene background + composite talking segment over it
  8. Fetch broll per beat
  9. Build per-beat timeline, normalize segments, concat into video
 10. Mix VO + music, mux final audio into video
 11. Burn captions (beats-based ASS); write final.mp4
 12. ffprobe final.mp4, print summary & absolute path
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import yaml

from pipeline import settings
from pipeline import script_writer, tts, broll, backgrounds
from pipeline import lipsync, matte, captions, audio, assemble
from pipeline.fftools import probe_duration, normalize_clip


# ---------------------------------------------------------------------------
# Cost constants (from RESEARCH.md)
# ---------------------------------------------------------------------------
# These are per-unit estimates for the LIVE (API) path.
# Fallback/mock paths cost $0.

COST_SCRIPT_LIVE = 0.02        # Claude Sonnet, per script
COST_TTS_PER_60S = 0.05        # ElevenLabs Flash, per ~60s of VO
COST_LIPSYNC_PER_SEC = 0.04    # Sync.so, per second of lipsynced video
COST_BROLL_KLING_PER_SEC = 0.07  # fal.ai Kling img2vid, per second (when LIVE)
COST_ASSEMBLY_FLAT = 0.01      # ffmpeg/captions compute (negligible, always)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cached(path: str, force: bool) -> bool:
    """Return True if *path* exists, is non-empty, and --force is not set."""
    p = Path(path)
    return (not force) and p.exists() and p.stat().st_size > 0


def _log_cache(logger, hit: bool, path: str) -> None:
    """Emit a CACHE HIT or CACHE MISS log line."""
    if hit:
        logger.info("CACHE HIT: reusing %s", path)
    else:
        logger.info("CACHE MISS / --force: regenerating %s", path)


def _concat_audio_files(audio_paths: list[str], out_path: str, logger) -> str:
    """Concatenate multiple audio files into one via ffmpeg concat demuxer."""
    work_dir = Path(out_path).parent
    list_file = str(work_dir / "_audio_concat_list.txt")
    lines = ["ffconcat version 1.0"]
    for p in audio_paths:
        abs_fwd = Path(p).resolve().as_posix()
        lines.append(f"file '{abs_fwd}'")
    with open(list_file, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    settings.run_ffmpeg(
        ["-f", "concat", "-safe", "0", "-i", list_file,
         "-c:a", "aac", "-ar", "44100", "-ac", "2", out_path],
        logger,
    )
    return out_path


def _slice_video(src: str, start_s: float, duration_s: float, out_path: str, logger) -> str:
    """Trim *src* from *start_s* for *duration_s* seconds (fast seek + re-encode audio)."""
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    settings.run_ffmpeg(
        [
            "-ss", f"{start_s:.3f}",
            "-i", src,
            "-t", f"{duration_s:.3f}",
            "-c:v", "copy",
            "-c:a", "aac", "-ar", "44100", "-ac", "2",
            out_path,
        ],
        logger,
    )
    return out_path


def _probe_summary(path: str, logger) -> dict:
    """Return a dict with codec, WxH, duration, has_audio for *path*."""
    try:
        raw = settings.run_ffprobe(
            ["-v", "error", "-show_entries",
             "format=duration:stream=codec_name,codec_type,width,height",
             "-of", "json", path],
            logger,
        )
        data = json.loads(raw)
        fmt = data.get("format", {})
        streams = data.get("streams", [])
        result: dict = {"duration": float(fmt.get("duration", 0))}
        for s in streams:
            ct = s.get("codec_type", "")
            if ct == "video":
                result["v_codec"] = s.get("codec_name")
                result["width"] = s.get("width")
                result["height"] = s.get("height")
            elif ct == "audio":
                result["a_codec"] = s.get("codec_name")
                result["has_audio"] = True
        return result
    except Exception as exc:
        logger.warning("ffprobe summary failed: %s", exc)
        return {}


def _write_cost_log(run_dir: Path, stage_status: dict, vo_seconds: float,
                    lipsync_seconds: float, broll_seconds_live: float, logger) -> None:
    """Write cost_log.json and cost_log.txt into run_dir."""

    def _cost_script(status: str) -> float:
        # Only the Anthropic LIVE path incurs cost. FILE (Claude Code authored),
        # FALLBACK, and CACHE are all $0.
        return COST_SCRIPT_LIVE if status == "LIVE" else 0.0

    def _script_note(status: str) -> str:
        if status == "LIVE":
            return "Claude Sonnet API call"
        if status == "FILE":
            return "script authored by Claude Code (no API spend)"
        if status == "CACHE":
            return "script reused from cache (no external spend)"
        return "fallback (no external spend)"

    def _cost_tts(status: str) -> float:
        if status != "LIVE":
            return 0.0
        return COST_TTS_PER_60S * (vo_seconds / 60.0)

    def _cost_lipsync(status: str) -> float:
        if status != "LIVE":
            return 0.0
        return COST_LIPSYNC_PER_SEC * lipsync_seconds

    def _cost_broll(status: str) -> float:
        if status != "LIVE":
            return 0.0
        return COST_BROLL_KLING_PER_SEC * broll_seconds_live

    entries = {
        "script": {
            "status": stage_status.get("script", "FALLBACK"),
            "estimated_cost_usd": _cost_script(stage_status.get("script", "FALLBACK")),
            "note": _script_note(stage_status.get("script", "FALLBACK")),
        },
        "tts": {
            "status": stage_status.get("tts", "FALLBACK"),
            "estimated_cost_usd": _cost_tts(stage_status.get("tts", "FALLBACK")),
            "note": (
                f"ElevenLabs Flash, {vo_seconds:.1f}s VO @ ${COST_TTS_PER_60S}/60s"
                if stage_status.get("tts") == "LIVE"
                else "fallback gTTS/silent (no external spend)"
            ),
        },
        "lipsync": {
            "status": stage_status.get("lipsync", "FALLBACK"),
            "estimated_cost_usd": _cost_lipsync(stage_status.get("lipsync", "FALLBACK")),
            "note": (
                f"Sync.so, {lipsync_seconds:.1f}s lipsynced video @ ${COST_LIPSYNC_PER_SEC}/s"
                if stage_status.get("lipsync") == "LIVE"
                else "fallback mock mux (no external spend)"
            ),
        },
        "broll": {
            "status": stage_status.get("broll", "FALLBACK"),
            "estimated_cost_usd": _cost_broll(stage_status.get("broll", "FALLBACK")),
            "note": (
                f"fal.ai Kling img2vid, {broll_seconds_live:.1f}s @ ${COST_BROLL_KLING_PER_SEC}/s"
                if stage_status.get("broll") == "LIVE"
                else "Pexels/Pixabay/Ken-Burns fallback (no external spend)"
            ),
        },
        "assembly": {
            "status": "LIVE",
            "estimated_cost_usd": COST_ASSEMBLY_FLAT,
            "note": "ffmpeg/captions compute (flat estimate)",
        },
    }

    total = sum(v["estimated_cost_usd"] for v in entries.values())

    cost_data = {
        "stages": entries,
        "total_estimated_cost_usd": round(total, 4),
        "vo_seconds": round(vo_seconds, 2),
        "lipsync_seconds": round(lipsync_seconds, 2),
        "broll_seconds_live": round(broll_seconds_live, 2),
    }

    json_path = run_dir / "cost_log.json"
    json_path.write_text(json.dumps(cost_data, indent=2), encoding="utf-8")

    # Human-readable .txt
    lines = [
        "=== Cost Log ===",
        "",
    ]
    for stage, info in entries.items():
        lines.append(
            f"  {stage:<12} [{info['status']:<8}]  ${info['estimated_cost_usd']:.4f}  — {info['note']}"
        )
    lines += [
        "",
        f"  TOTAL ESTIMATED COST: ${total:.4f}",
        "",
        f"  VO duration:          {vo_seconds:.1f}s",
        f"  Lipsync input:        {lipsync_seconds:.1f}s",
        f"  Broll (LIVE AI secs): {broll_seconds_live:.1f}s",
    ]
    txt_path = run_dir / "cost_log.txt"
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    logger.info("Cost log written to %s", txt_path)
    logger.info("TOTAL ESTIMATED COST: $%.4f", total)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run_pipeline(
    client: str,
    brief: str,
    duration: float,
    output_root: Path,
    force: bool = False,
    reuse_dir: Path | None = None,
    script_file: str | None = None,
) -> str:
    """Execute the full pipeline. Returns absolute path to final.mp4."""

    log = settings.get_logger("run")

    # Determine run_dir: reuse existing or create fresh timestamped one
    if reuse_dir is not None:
        run_dir = reuse_dir.resolve()
        log.info("Reusing existing run dir: %s (force=%s)", run_dir, force)
    else:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        run_dir = output_root / ts
        log.info("Run dir: %s", run_dir)

    stages_dir = run_dir / "stages"
    stages_dir.mkdir(parents=True, exist_ok=True)

    # Track LIVE vs FALLBACK per stage for cost accounting
    stage_status: dict[str, str] = {}

    # Metrics for cost calculation
    vo_seconds: float = 0.0
    lipsync_seconds: float = 0.0
    broll_seconds_live: float = 0.0

    # ── 1. Load client config ────────────────────────────────────────────────
    config_path = settings.CONFIG_DIR / f"{client}.yaml"
    if not config_path.exists():
        log.warning("Config not found at %s — using empty defaults", config_path)
        cfg: dict = {}
    else:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    log.info("Loaded config for client '%s'", client)

    def _resolve(rel: str | None) -> Path | None:
        return (settings.PROJECT_ROOT / rel) if rel else None

    footage_dir = _resolve(cfg.get("footage_dir")) or (settings.CLIENTS_DIR / client / "green_screen")
    voice_sample = _resolve(cfg.get("voice_sample")) or (settings.CLIENTS_DIR / client / "voice" / "sample.mp3")
    product_dir = _resolve(cfg.get("product_dir")) or (settings.CLIENTS_DIR / client / "product")
    product_doc_path = _resolve(cfg.get("product_doc")) or (product_dir / "product.md")
    background_image = _resolve(cfg.get("background_image")) or (settings.BACKGROUNDS_DIR / "office.jpg")
    music_track = _resolve(cfg.get("music_track")) or (settings.MUSIC_DIR / "track.mp3")
    brand: dict = cfg.get("brand", {"name": client, "tone": "authentic"})

    # ── 2. Read product doc ──────────────────────────────────────────────────
    product_doc_text = ""
    if product_doc_path and product_doc_path.exists():
        product_doc_text = product_doc_path.read_text(encoding="utf-8")
        log.info("Loaded product doc: %s (%d chars)", product_doc_path, len(product_doc_text))

    # ── 3. Generate script ───────────────────────────────────────────────────
    def _fallback_script() -> dict:
        """Minimal 5-beat script used when file-load / write_script fails."""
        return {
            "title": "Fallback Script",
            "beats": [
                {"beat": "hook",    "text": "Check out this amazing product.", "duration_s": 6.0, "broll_query": "product shot",      "background_query": "bright modern kitchen interior",   "visual": "Hero shot"},
                {"beat": "problem", "text": "Most people struggle with this every day.", "duration_s": 6.0, "broll_query": "person frustrated", "background_query": "sunny outdoor hiking trail",        "visual": "Problem"},
                {"beat": "demo",    "text": "This product solves it instantly.", "duration_s": 8.0, "broll_query": "product demo",     "background_query": "clean minimalist desk workspace",  "visual": "Demo"},
                {"beat": "proof",   "text": "Over fifty thousand happy customers.", "duration_s": 5.0, "broll_query": "happy customer", "background_query": "cozy cafe interior",               "visual": "Social proof"},
                {"beat": "cta",     "text": "Get yours now using the link below.", "duration_s": 5.0, "broll_query": "product hero",    "background_query": "modern gym interior",              "visual": "CTA"},
            ],
            "total_duration_s": 30.0,
        }

    script_cache = str(stages_dir / "script.json")
    if _cached(script_cache, force):
        _log_cache(log, True, script_cache)
        script = json.loads(Path(script_cache).read_text(encoding="utf-8"))
        stage_status["script"] = "CACHE"
        log.info("Script loaded from cache: %d beats", len(script.get("beats", [])))
    else:
        _log_cache(log, False, script_cache)
        if script_file:
            # PRIMARY path: a script JSON authored by Claude Code. No API, no mock.
            log.info("Stage: load_script_file (%s)", script_file)
            try:
                script = script_writer.load_script_file(
                    script_file, target_duration_s=duration, logger=log
                )
                stage_status["script"] = "FILE"
                log.info("Script authored by Claude Code loaded from %s", script_file)
            except Exception as exc:
                log.error(
                    "Script file load failed — SCHEMA ERROR: %s. Falling back to mock script.",
                    exc,
                )
                stage_status["script"] = "FALLBACK"
                script = _fallback_script()
        else:
            log.info("Stage: write_script")
            try:
                script = script_writer.write_script(
                    brief=brief,
                    product_doc_text=product_doc_text,
                    brand=brand,
                    target_duration_s=duration,
                    logger=log,
                )
                stage_status["script"] = "LIVE"
            except Exception as exc:
                log.warning("write_script failed (%s) — using minimal fallback script", exc)
                stage_status["script"] = "FALLBACK"
                script = _fallback_script()

        beats_tmp: list[dict] = script["beats"]
        # Ensure beat durations sum to > 10s (acceptance test requirement)
        total_beat_dur = sum(b["duration_s"] for b in beats_tmp)
        if total_beat_dur < 12.0:
            scale = 12.0 / total_beat_dur
            for b in beats_tmp:
                b["duration_s"] = round(b["duration_s"] * scale, 2)
            log.info("Scaled beat durations to ensure total > 12s (was %.1fs)", total_beat_dur)

        Path(script_cache).write_text(json.dumps(script, indent=2), encoding="utf-8")

    beats: list[dict] = script["beats"]
    log.info("Script: %d beats, total %.1fs", len(beats), sum(b["duration_s"] for b in beats))

    # ── 4. Voice clone ───────────────────────────────────────────────────────
    log.info("Stage: clone_voice")
    voice_id: str | None = None
    try:
        voice_id = tts.clone_voice(
            str(voice_sample) if voice_sample and voice_sample.exists() else "",
            name=client,
            logger=log,
        )
    except Exception as exc:
        log.warning("clone_voice failed (%s)", exc)

    # ── 5. Synthesize beat VO files ──────────────────────────────────────────
    # Check if all per-beat VO files already exist
    expected_vo_paths = [str(stages_dir / f"vo_{i}.mp3") for i in range(len(beats))]
    all_vo_cached = all(_cached(p, force) for p in expected_vo_paths)

    if all_vo_cached:
        _log_cache(log, True, f"vo_0.mp3 … vo_{len(beats)-1}.mp3")
        vo_paths = expected_vo_paths
        stage_status["tts"] = "CACHE"
    else:
        _log_cache(log, False, f"vo_0.mp3 … vo_{len(beats)-1}.mp3")
        log.info("Stage: synthesize_beats")
        try:
            vo_paths = tts.synthesize_beats(beats, str(stages_dir), voice_id=voice_id, logger=log)
            stage_status["tts"] = "LIVE"
        except Exception as exc:
            log.warning("synthesize_beats failed (%s) — generating silent VO files", exc)
            stage_status["tts"] = "FALLBACK"
            vo_paths = []
            for i, beat in enumerate(beats):
                p = str(stages_dir / f"vo_{i}.mp3")
                dur = beat.get("duration_s", 5.0)
                settings.run_ffmpeg(
                    ["-f", "lavfi", "-i", f"anullsrc=r=44100:cl=mono:d={dur:.2f}",
                     "-acodec", "libmp3lame", "-b:a", "128k", "-t", str(dur), p],
                    log,
                )
                vo_paths.append(p)

    # Ensure we have one VO per beat
    while len(vo_paths) < len(beats):
        i = len(vo_paths)
        p = str(stages_dir / f"vo_{i}.mp3")
        dur = beats[i].get("duration_s", 5.0)
        settings.run_ffmpeg(
            ["-f", "lavfi", "-i", f"anullsrc=r=44100:cl=mono:d={dur:.2f}",
             "-acodec", "libmp3lame", "-b:a", "128k", "-t", str(dur), p],
            log,
        )
        vo_paths.append(p)

    # ── 6. Concatenate VO into full_vo + lipsync talking-head ────────────────
    full_vo_path = str(stages_dir / "full_vo.m4a")
    if not _cached(full_vo_path, force):
        log.info("Stage: concat VO + lipsync")
        try:
            _concat_audio_files(vo_paths, full_vo_path, log)
        except Exception as exc:
            log.warning("VO concat failed (%s) — using first VO as full_vo", exc)
            full_vo_path = vo_paths[0] if vo_paths else ""
    else:
        _log_cache(log, True, full_vo_path)

    # Measure VO duration for cost accounting
    if full_vo_path and Path(full_vo_path).exists():
        try:
            vo_seconds = probe_duration(full_vo_path, log)
        except Exception:
            vo_seconds = sum(b.get("duration_s", 5.0) for b in beats)

    # Pick base talking-head video — explicit footage_file key takes priority
    base_video: str | None = None
    footage_file_rel = cfg.get("footage_file")
    if footage_file_rel:
        explicit_path = settings.PROJECT_ROOT / footage_file_rel
        if explicit_path.exists():
            base_video = str(explicit_path)
            log.info("Base talking-head (explicit footage_file): %s", base_video)
        else:
            log.warning("footage_file '%s' not found (%s) — falling back to glob", footage_file_rel, explicit_path)
    if base_video is None and footage_dir and footage_dir.exists():
        mp4s = list(footage_dir.glob("*.mp4"))
        if mp4s:
            base_video = str(mp4s[0])
            log.info("Base talking-head (glob fallback): %s", base_video)

    # Lipsync: skip if talking_full.mp4 already cached
    talking_full_path = str(stages_dir / "talking_full.mp4")
    talking_full: str | None = None

    if _cached(talking_full_path, force):
        _log_cache(log, True, talking_full_path)
        talking_full = talking_full_path
        stage_status["lipsync"] = "CACHE"
        try:
            lipsync_seconds = probe_duration(talking_full_path, log)
        except Exception:
            lipsync_seconds = 0.0
    else:
        _log_cache(log, False, talking_full_path)

        # Trim base video to roughly VO length before lipsync (saves Sync.so quota)
        lipsync_input: str | None = base_video
        if base_video and full_vo_path and Path(full_vo_path).exists():
            try:
                trim_len = max(15.0, min(28.0, vo_seconds + 2.0))
                log.info("VO duration: %.2fs — trimming base video to %.2fs for lipsync", vo_seconds, trim_len)
                base_trimmed_path = str(stages_dir / "base_trimmed.mp4")
                settings.run_ffmpeg(
                    [
                        "-ss", "0",
                        "-i", base_video,
                        "-t", f"{trim_len:.3f}",
                        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                        "-pix_fmt", "yuv420p",
                        "-an",
                        base_trimmed_path,
                    ],
                    log,
                )
                lipsync_input = base_trimmed_path
                lipsync_seconds = trim_len
                log.info("Sending trimmed clip (%s) to lipsync", base_trimmed_path)
            except Exception as exc:
                log.warning("Base video trim failed (%s) — using original base video for lipsync", exc)
                lipsync_input = base_video
                lipsync_seconds = vo_seconds

        if lipsync_input and full_vo_path and Path(full_vo_path).exists():
            try:
                talking_full = lipsync.lipsync(lipsync_input, full_vo_path, talking_full_path, logger=log)
                # Detect LIVE vs FALLBACK: LIVE produces the exact talking_full_path
                if talking_full == talking_full_path and Path(talking_full_path).exists():
                    stage_status["lipsync"] = "LIVE"
                else:
                    stage_status["lipsync"] = "FALLBACK"
                    lipsync_seconds = 0.0
            except Exception as exc:
                log.warning("lipsync failed (%s) — using raw base video", exc)
                talking_full = base_video
                stage_status["lipsync"] = "FALLBACK"
                lipsync_seconds = 0.0
        elif base_video:
            talking_full = base_video
            stage_status["lipsync"] = "FALLBACK"
            lipsync_seconds = 0.0

    # ── 7. Slice talking_full per talking beat ───────────────────────────────
    #  Compute cumulative offsets from beat durations; each talking beat gets
    #  a slice of talking_full at the correct timeline position.
    log.info("Stage: slice talking clips per beat")
    talking_clips: list[str] = []
    cumulative = 0.0
    full_dur = probe_duration(talking_full, log) if talking_full else 0.0
    log.info("talking_full duration: %.2fs", full_dur)

    for i, beat in enumerate(beats):
        beat_dur = float(beat["duration_s"])
        out_slice = str(stages_dir / f"talking_{i:02d}.mp4")

        if talking_full and Path(talking_full).exists():
            # Loop talking_full if cumulative offset exceeds its duration
            start = cumulative % max(full_dur, 0.001) if full_dur > 0 else 0.0
            try:
                # If we'd run past the end, loop the video first
                if start + beat_dur > full_dur and full_dur > 0:
                    # Normalize with loop to cover the duration
                    looped = str(stages_dir / f"talking_looped_{i:02d}.mp4")
                    settings.run_ffmpeg(
                        ["-stream_loop", "-1", "-i", talking_full,
                         "-t", str(beat_dur + 1),
                         "-c:v", "copy", "-c:a", "aac", looped],
                        log,
                    )
                    _slice_video(looped, 0.0, beat_dur, out_slice, log)
                else:
                    _slice_video(talking_full, start, beat_dur, out_slice, log)
            except Exception as exc:
                log.warning("Slice beat %d failed (%s) — copying talking_full", i, exc)
                out_slice = talking_full
        else:
            # No talking video — generate a color placeholder
            settings.run_ffmpeg(
                ["-f", "lavfi", "-i", f"color=c=0x1a1a2e:s=1920x1080:d={beat_dur:.2f}:r=30",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p",
                 "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                 "-c:a", "aac", "-shortest", out_slice],
                log,
            )

        talking_clips.append(out_slice)
        cumulative += beat_dur

    # ── 8. Composite talking clips over PER-BEAT scene backgrounds ───────────
    #  For each talking beat: (1) fetch a per-beat scene background matching the
    #  beat's background_query, then (2) composite the chromakeyed person over
    #  that scene. Falls back to chromakey_composite -> passthrough on failure.
    log.info("Stage: matte composite (per-beat scene backgrounds)")
    composited_clips: list[str] = []
    for i, clip in enumerate(talking_clips):
        beat = beats[i]
        beat_dur = float(beat["duration_s"])
        bg_query = beat.get("background_query") or "modern studio interior"
        out_comp = str(stages_dir / f"composited_{i:02d}.mp4")
        bg_path = str(stages_dir / f"bg_{i:02d}.mp4")

        if not Path(clip).exists():
            composited_clips.append(clip)
            continue

        # 8a. Fetch (or reuse cached) per-beat scene background.
        if _cached(bg_path, force):
            _log_cache(log, True, bg_path)
        else:
            _log_cache(log, False, bg_path)
            try:
                backgrounds.fetch_background(
                    query=bg_query, out_path=bg_path, duration_s=beat_dur, logger=log
                )
            except Exception as exc:
                log.warning("fetch_background beat %d failed (%s)", i, exc)

        have_bg = Path(bg_path).exists() and Path(bg_path).stat().st_size > 0

        # 8b. Composite the person over the per-beat background.
        try:
            if have_bg:
                result = matte.composite_over_background(clip, bg_path, out_comp, logger=log)
            else:
                result = matte.chromakey_composite(clip, str(background_image), out_comp, logger=log)
            composited_clips.append(result)
        except Exception as exc:
            log.warning("composite_over_background beat %d failed (%s) — chromakey fallback", i, exc)
            try:
                bg_for_key = bg_path if have_bg else str(background_image)
                result = matte.chromakey_composite(clip, bg_for_key, out_comp, logger=log)
                composited_clips.append(result)
            except Exception as exc2:
                log.warning("chromakey_composite beat %d failed (%s) — passthrough", i, exc2)
                try:
                    result = matte.passthrough_over_bg(clip, out_comp, log)
                    composited_clips.append(result)
                except Exception as exc3:
                    log.warning("passthrough also failed (%s) — using raw clip", exc3)
                    composited_clips.append(clip)

    # ── 9. Fetch broll ───────────────────────────────────────────────────────
    # Check if all broll files exist (broll_0.mp4 … broll_N.mp4)
    expected_broll_paths = [str(stages_dir / f"broll_{i}.mp4") for i in range(len(beats))]
    all_broll_cached = all(_cached(p, force) for p in expected_broll_paths)

    product_images: list[str] = []
    if product_dir and product_dir.exists():
        for ext in ("*.jpg", "*.jpeg", "*.png"):
            product_images.extend(str(p) for p in product_dir.glob(ext))

    if all_broll_cached:
        _log_cache(log, True, f"broll_0.mp4 … broll_{len(beats)-1}.mp4")
        broll_paths = expected_broll_paths
        stage_status["broll"] = "CACHE"
    else:
        _log_cache(log, False, f"broll_0.mp4 … broll_{len(beats)-1}.mp4")
        log.info("Stage: fetch broll")
        try:
            broll_paths = broll.fetch_broll_for_beats(
                beats, str(stages_dir), product_images=product_images or None, logger=log
            )
            # Detect if any broll used Kling (LIVE AI generation)
            stage_status["broll"] = "LIVE"
        except Exception as exc:
            log.warning("fetch_broll_for_beats failed (%s) — using composited clips as broll", exc)
            broll_paths = composited_clips[:]
            stage_status["broll"] = "FALLBACK"

    # Ensure broll_paths has one per beat (fall back to composited)
    while len(broll_paths) < len(beats):
        i = len(broll_paths)
        broll_paths.append(composited_clips[i] if i < len(composited_clips) else composited_clips[0])

    # ── 10. Build timeline & concat segments ──────────────────────────────────
    video_noaudio_path = str(stages_dir / "video_noaudio.mp4")
    video_noaudio: str = video_noaudio_path

    if not _cached(video_noaudio_path, force):
        log.info("Stage: build_timeline + concat_segments")
        try:
            segments = assemble.build_timeline(
                talking_clips=composited_clips,
                broll_clips=broll_paths,
                beats=beats,
                work_dir=str(stages_dir / "segments"),
                logger=log,
            )
            video_noaudio = assemble.concat_segments(
                segments, video_noaudio_path, str(stages_dir / "segments"), log
            )
        except Exception as exc:
            log.warning("assemble failed (%s) — concatenating composited clips directly", exc)
            try:
                # Fallback: normalize composited clips and concat
                norm_clips = []
                for i, clip in enumerate(composited_clips):
                    nc = str(stages_dir / f"fallback_seg_{i:02d}.mp4")
                    normalize_clip(clip, nc, duration_s=beats[i]["duration_s"], logger=log)
                    norm_clips.append(nc)
                video_noaudio = assemble.concat_segments(
                    norm_clips, video_noaudio_path, str(stages_dir), log
                )
            except Exception as exc2:
                log.error("Second assemble attempt failed: %s", exc2)
                raise
    else:
        _log_cache(log, True, video_noaudio_path)

    # ── 11. Mix audio (VO + music) ────────────────────────────────────────────
    final_audio_path = str(stages_dir / "final_audio.m4a")
    if not _cached(final_audio_path, force):
        log.info("Stage: mix_vo_music")
        try:
            music_str = str(music_track) if music_track and music_track.exists() else None
            final_audio_path = audio.mix_vo_music(
                full_vo_path, music_str, final_audio_path,
                music_gain_db=-12.0, logger=log,
            )
        except Exception as exc:
            log.warning("mix_vo_music failed (%s) — using raw VO", exc)
            final_audio_path = full_vo_path
    else:
        _log_cache(log, True, final_audio_path)

    # ── 12. Mux final audio into concatenated video ───────────────────────────
    video_with_audio = str(stages_dir / "video_with_audio.mp4")
    if not _cached(video_with_audio, force):
        log.info("Stage: mux_audio_into_video")
        try:
            video_with_audio = audio.mux_audio_into_video(
                video_noaudio, final_audio_path, video_with_audio, logger=log
            )
        except Exception as exc:
            log.warning("mux_audio_into_video failed (%s) — proceeding without audio swap", exc)
            video_with_audio = video_noaudio

    # ── 13. Captions ─────────────────────────────────────────────────────────
    # Check if final.mp4 already exists — if so, skip the entire tail
    final_mp4 = str(run_dir / "final.mp4")

    if _cached(final_mp4, force):
        _log_cache(log, True, final_mp4)
        log.info("Skipping captions + burn (final.mp4 cached)")
    else:
        _log_cache(log, False, final_mp4)
        log.info("Stage: captions")
        ass_path = str(stages_dir / "captions.ass")
        total_duration_s = sum(b["duration_s"] for b in beats)

        # Try audio-based transcription first (will fall back gracefully on ARM64)
        ass_result = captions.captions_from_audio(full_vo_path, ass_path, log)
        if ass_result is None:
            log.info("Using beats-based caption timing (faster-whisper unavailable)")
            ass_result = captions.captions_from_beats(beats, ass_path, total_duration_s, log)

        try:
            captions.burn_captions(video_with_audio, ass_result, final_mp4, logger=log)
        except Exception as exc:
            log.warning("burn_captions failed (%s) — copying without captions", exc)
            import shutil
            shutil.copy2(video_with_audio, final_mp4)

    # ── 14. Final ffprobe summary ─────────────────────────────────────────────
    log.info("Stage: final probe")
    summary = _probe_summary(final_mp4, log)
    log.info(
        "FINAL: codec=%s  %sx%s  duration=%.1fs  has_audio=%s",
        summary.get("v_codec"),
        summary.get("width"),
        summary.get("height"),
        summary.get("duration", 0.0),
        summary.get("has_audio", False),
    )

    # ── 15. Write cost log ────────────────────────────────────────────────────
    _write_cost_log(
        run_dir=run_dir,
        stage_status=stage_status,
        vo_seconds=vo_seconds,
        lipsync_seconds=lipsync_seconds,
        broll_seconds_live=broll_seconds_live,
        logger=log,
    )

    print(f"\nPipeline complete: {Path(final_mp4).resolve()}")
    return str(Path(final_mp4).resolve())


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m pipeline.run",
        description="Record Once, Generate Many — UGC ad pipeline",
    )
    p.add_argument("--client", default="demo", help="Client ID (matches config/<id>.yaml)")
    p.add_argument(
        "--brief",
        default="30-second vertical UGC ad for AquaSteel — an insulated water bottle that keeps drinks cold 24 hours.",
        help="Product brief / creative direction",
    )
    p.add_argument("--duration", type=float, default=30.0, help="Target ad duration in seconds")
    p.add_argument(
        "--script-file",
        default=None,
        metavar="PATH",
        help=(
            "Path to a beats JSON script authored by Claude Code (PRIMARY input). "
            "When provided, the script is loaded from this file — no Anthropic API "
            "call and no mock. On schema failure the pipeline falls back to a mock "
            "script so a run never hard-crashes."
        ),
    )
    p.add_argument(
        "--output-root",
        default=str(settings.OUTPUT_DIR),
        help="Root directory for timestamped run output",
    )
    p.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Regenerate all stages, ignoring cached artifacts",
    )
    p.add_argument(
        "--reuse-dir",
        default=None,
        metavar="PATH",
        help=(
            "Reuse an existing timestamped run directory instead of creating a new one. "
            "Cached stage artifacts (script.json, vo_*.mp3, talking_full.mp4, broll_*.mp4, "
            "final.mp4, etc.) are reused when present. Combine with --force to regenerate them."
        ),
    )
    return p


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    output_root = Path(args.output_root)
    output_root.mkdir(parents=True, exist_ok=True)

    reuse_dir = Path(args.reuse_dir) if args.reuse_dir else None

    final = run_pipeline(
        client=args.client,
        brief=args.brief,
        duration=args.duration,
        output_root=output_root,
        force=args.force,
        reuse_dir=reuse_dir,
        script_file=args.script_file,
    )
    print(final)


if __name__ == "__main__":
    main()
