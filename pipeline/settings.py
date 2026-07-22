"""
pipeline/settings.py — shared contract for the UGC ad pipeline.

Imported by all other pipeline modules. Loads .env at import time,
exposes project-wide paths, constants, and helpers. Zero third-party
deps beyond python-dotenv.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Project root — this file lives at <root>/pipeline/settings.py
# ---------------------------------------------------------------------------
PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]

# Load .env from project root (silently a no-op if the file doesn't exist)
load_dotenv(PROJECT_ROOT / ".env")

# ---------------------------------------------------------------------------
# Directory paths
# ---------------------------------------------------------------------------
CONFIG_DIR: Path = PROJECT_ROOT / "config"
ASSETS_DIR: Path = PROJECT_ROOT / "assets"
CLIENTS_DIR: Path = ASSETS_DIR / "clients"
MUSIC_DIR: Path = ASSETS_DIR / "music"
BACKGROUNDS_DIR: Path = ASSETS_DIR / "backgrounds"
OUTPUT_DIR: Path = PROJECT_ROOT / "output"

# ---------------------------------------------------------------------------
# External binary paths (overridable via env)
# ---------------------------------------------------------------------------
FFMPEG: str = os.environ.get("FFMPEG_BIN", "ffmpeg")
FFPROBE: str = os.environ.get("FFPROBE_BIN", "ffprobe")

# ---------------------------------------------------------------------------
# Video constants
# ---------------------------------------------------------------------------
WIDTH: int = 1080
HEIGHT: int = 1920
FPS: int = 30
VIDEO_CODEC: str = "libx264"
AUDIO_CODEC: str = "aac"
PIX_FMT: str = "yuv420p"

# ---------------------------------------------------------------------------
# B-roll generation backends
# ---------------------------------------------------------------------------
# Priority order for B-roll backends, read from env BROLL_BACKENDS
# (comma-separated, e.g. "sora,veo,kling,stock"). Defaults below.
def _parse_broll_backends() -> list[str]:
    raw = os.environ.get("BROLL_BACKENDS", "").strip()
    if raw:
        parts = [p.strip().lower() for p in raw.split(",") if p.strip()]
        if parts:
            return parts
    return ["sora", "veo", "kling", "stock"]


BROLL_BACKEND_PRIORITY: list[str] = _parse_broll_backends()

# Max generative (sora/veo/kling) clips per run; rest fall back to stock.
def _parse_max_gen_clips() -> int:
    raw = os.environ.get("MAX_GEN_CLIPS", "").strip()
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 2


MAX_GEN_CLIPS: int = _parse_max_gen_clips()

# ---------------------------------------------------------------------------
# Chroma / color key defaults (overridable via env)
# ---------------------------------------------------------------------------
# The client's green-screen footage is a DARK TEAL green (~0x175a48), not bright
# green. colorkey (RGB distance) separates it cleanly from a black shirt where
# chromakey (chroma-only) cannot. These are the defaults; a client with a
# different screen color can override via env.
CHROMA_KEY_COLOR: str = os.environ.get("CHROMA_KEY_COLOR", "0x175a48")


def _parse_float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


CHROMA_SIMILARITY: float = _parse_float_env("CHROMA_SIMILARITY", 0.13)
CHROMA_BLEND: float = _parse_float_env("CHROMA_BLEND", 0.03)

# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------

def get_env(name: str, default: str | None = None) -> str | None:
    """Return the value of an environment variable, or *default* if unset."""
    return os.environ.get(name, default)


def has_key(name: str) -> bool:
    """Return True if *name* is set in the environment and non-empty."""
    return bool(os.environ.get(name, "").strip())

# ---------------------------------------------------------------------------
# Logging helper
# ---------------------------------------------------------------------------

def get_logger(name: str) -> logging.Logger:
    """Return a Logger configured with a simple timestamped format."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter("[%(asctime)s] %(levelname)s %(name)s: %(message)s")
        )
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger

# ---------------------------------------------------------------------------
# ffmpeg / ffprobe subprocess helpers
# ---------------------------------------------------------------------------

def run_ffmpeg(args: list[str], logger: logging.Logger | None = None) -> str:
    """
    Run ffmpeg with *args*.

    Prepends FFMPEG binary, adds ``-y`` (overwrite) and
    ``-hide_banner -loglevel error`` automatically.

    Returns combined stdout+stderr as a string.
    Raises RuntimeError (with stderr text) on non-zero exit.
    """
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error"] + args
    if logger:
        logger.info("ffmpeg: %s", " ".join(cmd))
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed (exit {result.returncode}):\n{result.stderr}"
        )
    return result.stdout + result.stderr


def run_ffprobe(args: list[str], logger: logging.Logger | None = None) -> str:
    """
    Run ffprobe with *args*.

    Prepends FFPROBE binary and ``-hide_banner``.

    Returns stdout as a string (caller parses JSON / lines as needed).
    Raises RuntimeError (with stderr text) on non-zero exit.
    """
    cmd = [FFPROBE, "-hide_banner"] + args
    if logger:
        logger.info("ffprobe: %s", " ".join(cmd))
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"ffprobe failed (exit {result.returncode}):\n{result.stderr}"
        )
    return result.stdout
