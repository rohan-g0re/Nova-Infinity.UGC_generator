"""
pipeline/script_writer.py — turn a product brief into a 5-beat UGC ad script.

PRIMARY path: a script JSON file authored by Claude Code (the LLM writes the
beats to disk; the pipeline reads them via ``load_script_file``). No API key
is required for this path.

OPTIONAL / LEGACY path: Anthropic API (claude-sonnet-4-6) with tool-use via
``write_script`` — used only when ANTHROPIC_API_KEY is set. Never required.

MOCK path: hard-coded AquaSteel demo script (runs with zero keys).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pipeline import settings


# ---------------------------------------------------------------------------
# Beat defaults used for validation / gap-filling
# ---------------------------------------------------------------------------
BEAT_TYPES = ["hook", "problem", "demo", "proof", "cta"]

_MOCK_BROLL = {
    "hook": "pouring water closeup",
    "problem": "person hiking trail",
    "demo": "water bottle desk",
    "proof": "happy customer smiling",
    "cta": "product on white background",
}

_MOCK_BACKGROUNDS = {
    "hook": "bright modern kitchen interior",
    "problem": "sunny outdoor hiking trail",
    "demo": "clean minimalist desk workspace",
    "proof": "cozy cafe interior",
    "cta": "modern gym interior",
}

_MOCK_VISUALS = {
    "hook": "Close-up of water pouring into sleek steel bottle",
    "problem": "Person sweating on a trail, reaching for a warm water bottle",
    "demo": "AquaSteel bottle on desk — condensation-free, stays cold",
    "proof": "Happy customer giving thumbs-up with AquaSteel bottle",
    "cta": "Product hero shot on clean white background",
}

_MOCK_TEXTS = {
    "hook": "Okay, I have to talk about this water bottle because it genuinely changed my hydration game.",
    "problem": "I used to carry cheap plastic bottles that got warm within an hour — so frustrating on long hikes.",
    "demo": "The AquaSteel keeps my water ice-cold for 24 hours. I tested it on a 6-hour trail and it still had ice.",
    "proof": "Over 50,000 people swear by it — and honestly, once you try it you'll never go back.",
    "cta": "Grab yours using the link below — they sell out fast, just saying.",
}


def _make_mock_beats(target_duration_s: float) -> list[dict]:
    """Return 5 plausible beats for AquaSteel scaled to target_duration_s."""
    # Distribute time: hook 20%, problem 15%, demo 30%, proof 20%, cta 15%
    weights = {"hook": 0.20, "problem": 0.15, "demo": 0.30, "proof": 0.20, "cta": 0.15}
    beats = []
    for beat_type in BEAT_TYPES:
        dur = max(1.5, round(target_duration_s * weights[beat_type], 1))
        beats.append({
            "beat": beat_type,
            "text": _MOCK_TEXTS[beat_type],
            "duration_s": dur,
            "broll_query": _MOCK_BROLL[beat_type],
            "background_query": _MOCK_BACKGROUNDS[beat_type],
            "visual": _MOCK_VISUALS[beat_type],
        })
    return beats


def _derive_background_query(beat: dict, beat_type: str) -> str:
    """Pick a sensible scene background for a beat.

    Precedence: explicit background_query -> per-beat-type mock scene ->
    derived from visual/broll_query -> generic studio interior.
    """
    explicit = beat.get("background_query")
    if explicit and str(explicit).strip():
        return str(explicit).strip()
    if beat_type in _MOCK_BACKGROUNDS:
        return _MOCK_BACKGROUNDS[beat_type]
    derived = beat.get("visual") or beat.get("broll_query")
    if derived and str(derived).strip():
        return str(derived).strip()
    return "modern studio interior"


def _validate_beat(beat: dict, index: int, target_duration_s: float) -> dict:
    """Ensure a beat has all required fields; fill sensible defaults if missing."""
    beat_type = beat.get("beat", BEAT_TYPES[min(index, len(BEAT_TYPES) - 1)])
    if beat_type not in BEAT_TYPES:
        beat_type = BEAT_TYPES[min(index, len(BEAT_TYPES) - 1)]

    return {
        "beat": beat_type,
        "text": str(beat.get("text", _MOCK_TEXTS.get(beat_type, "..."))),
        "duration_s": max(1.5, float(beat.get("duration_s", target_duration_s / 5))),
        "broll_query": str(beat.get("broll_query", _MOCK_BROLL.get(beat_type, "product closeup"))),
        "background_query": _derive_background_query(beat, beat_type),
        "visual": str(beat.get("visual", _MOCK_VISUALS.get(beat_type, "Product shot"))),
    }


def _normalize_script(title: str, beats: list[dict]) -> dict:
    """Recompute total_duration_s and return finished script dict."""
    total = sum(b["duration_s"] for b in beats)
    return {"title": title, "beats": beats, "total_duration_s": round(total, 2)}


# ---------------------------------------------------------------------------
# PRIMARY path: load a Claude-Code-authored script JSON file
# ---------------------------------------------------------------------------

def load_script_file(
    path: str,
    target_duration_s: float = 30.0,
    logger: logging.Logger | None = None,
) -> dict:
    """Load a UGC ad script from a JSON file authored by Claude Code.

    This is the PRIMARY scripting path — no API key required. The file may be
    EITHER a bare list of beats::

        [{"beat": ..., "text": ..., "duration_s": ..., "broll_query": ...,
          "background_query": ..., "visual": ...}, ...]

    OR an object with an optional title::

        {"title": "...", "beats": [ ... ]}

    Each beat is validated/normalized via ``_validate_beat`` so the returned
    beats always carry: beat, text, duration_s, broll_query, background_query,
    visual. ``background_query`` defaults to a sensible scene derived from the
    beat type / visual / broll_query when absent.

    Raises ValueError with a clear message when the file is missing, is not
    valid JSON, contains zero beats, or a beat lacks required text/beat fields.
    Returns a normalized script dict via ``_normalize_script``.
    """
    if logger is None:
        logger = settings.get_logger("script_writer")

    from pathlib import Path

    p = Path(path)
    if not p.exists():
        raise ValueError(f"Script file not found: {path}")

    try:
        raw = p.read_text(encoding="utf-8")
    except Exception as exc:
        raise ValueError(f"Could not read script file {path}: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Script file {path} is not valid JSON: {exc}") from exc

    # Accept either a bare list of beats or an object {title, beats}
    if isinstance(data, list):
        title = "UGC Ad Script"
        raw_beats = data
    elif isinstance(data, dict):
        title = str(data.get("title", "UGC Ad Script"))
        raw_beats = data.get("beats", [])
    else:
        raise ValueError(
            f"Script file {path} must be a JSON list of beats or an object with a 'beats' array."
        )

    if not isinstance(raw_beats, list) or len(raw_beats) == 0:
        raise ValueError(f"Script file {path} contains zero beats.")

    beats: list[dict] = []
    for i, b in enumerate(raw_beats):
        if not isinstance(b, dict):
            raise ValueError(f"Script file {path}: beat #{i} is not an object.")
        # Hard-require the two load-bearing fields for a usable beat.
        if not str(b.get("text", "")).strip():
            raise ValueError(f"Script file {path}: beat #{i} is missing required 'text'.")
        if not str(b.get("beat", "")).strip():
            raise ValueError(f"Script file {path}: beat #{i} is missing required 'beat'.")
        beats.append(_validate_beat(b, i, target_duration_s))

    logger.info(
        "Script loaded from file '%s': %s (%d beats)", path, title, len(beats)
    )
    return _normalize_script(title, beats)


# ---------------------------------------------------------------------------
# Tool schema for Anthropic tool-use
# ---------------------------------------------------------------------------
_EMIT_SCRIPT_TOOL = {
    "name": "emit_script",
    "description": (
        "Emit the final structured UGC ad script as a JSON object with a title "
        "and an array of exactly 5 beats (hook, problem, demo, proof, cta)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short title for this script"},
            "beats": {
                "type": "array",
                "description": "Exactly 5 beats in order: hook, problem, demo, proof, cta",
                "items": {
                    "type": "object",
                    "properties": {
                        "beat": {
                            "type": "string",
                            "enum": ["hook", "problem", "demo", "proof", "cta"],
                        },
                        "text": {
                            "type": "string",
                            "description": "Spoken VO text — concise, punchy, first-person UGC tone",
                        },
                        "duration_s": {
                            "type": "number",
                            "description": "Estimated spoken duration in seconds",
                        },
                        "broll_query": {
                            "type": "string",
                            "description": "2-4 word stock-footage search phrase for this beat",
                        },
                        "background_query": {
                            "type": "string",
                            "description": "Scene the spokesperson stands in for this beat (e.g. 'bright modern kitchen interior')",
                        },
                        "visual": {
                            "type": "string",
                            "description": "Brief visual/shot note for this beat",
                        },
                    },
                    "required": ["beat", "text", "duration_s", "broll_query", "visual"],
                },
            },
        },
        "required": ["title", "beats"],
    },
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def write_script(
    brief: str,
    product_doc_text: str,
    brand: dict,
    target_duration_s: float = 30.0,
    logger: logging.Logger | None = None,
) -> dict:
    """
    Generate a 5-beat UGC ad script from a product brief.

    Uses Anthropic API when ANTHROPIC_API_KEY is set; otherwise returns a
    hard-coded AquaSteel demo script.

    Returns a script dict: {"title": str, "beats": [...], "total_duration_s": float}
    """
    if logger is None:
        logger = settings.get_logger("script_writer")

    if settings.has_key("ANTHROPIC_API_KEY"):
        return _write_script_real(brief, product_doc_text, brand, target_duration_s, logger)
    else:
        return _write_script_mock(target_duration_s, logger)


def _write_script_real(
    brief: str,
    product_doc_text: str,
    brand: dict,
    target_duration_s: float,
    logger: logging.Logger,
) -> dict:
    """Call Anthropic claude-sonnet-4-6 with tool-use to generate the script."""
    try:
        import anthropic  # type: ignore
    except ImportError:
        logger.warning("anthropic SDK not installed - falling back to mock script")
        return _write_script_mock(target_duration_s, logger)

    brand_str = json.dumps(brand, indent=2)
    system = (
        "You are an expert UGC/DTC ad scriptwriter. "
        "Write authentic, first-person, punchy scripts that feel like real user testimonials. "
        "Always produce exactly 5 beats in order: hook, problem, demo, proof, cta. "
        "Keep each beat's text concise and natural-sounding. "
        "Ensure the sum of all beat duration_s values equals approximately the requested target duration."
    )
    user = (
        f"Write a {target_duration_s:.0f}-second UGC ad script.\n\n"
        f"BRIEF:\n{brief}\n\n"
        f"PRODUCT DOCUMENTATION:\n{product_doc_text}\n\n"
        f"BRAND GUIDELINES:\n{brand_str}\n\n"
        f"Use the emit_script tool to return the structured script."
    )

    try:
        client = anthropic.Anthropic(api_key=settings.get_env("ANTHROPIC_API_KEY"))
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            system=system,
            tools=[_EMIT_SCRIPT_TOOL],
            tool_choice={"type": "any"},
            messages=[{"role": "user", "content": user}],
        )

        # Extract the tool_use block
        tool_input: dict[str, Any] | None = None
        for block in response.content:
            if block.type == "tool_use" and block.name == "emit_script":
                tool_input = block.input
                break

        if tool_input is None:
            logger.warning("No emit_script tool call in response - falling back to mock")
            return _write_script_mock(target_duration_s, logger)

        title = str(tool_input.get("title", "UGC Ad Script"))
        raw_beats: list = tool_input.get("beats", [])

        # Validate / fill defaults for each beat
        beats = [_validate_beat(b, i, target_duration_s) for i, b in enumerate(raw_beats)]

        # Pad to 5 beats if the model returned fewer
        while len(beats) < 5:
            idx = len(beats)
            beats.append(_validate_beat({}, idx, target_duration_s))

        logger.info("Script generated via Anthropic API: %s (%d beats)", title, len(beats))
        return _normalize_script(title, beats[:5])

    except Exception as exc:
        logger.warning("Anthropic API error (%s) - falling back to mock script", exc)
        return _write_script_mock(target_duration_s, logger)


def _write_script_mock(target_duration_s: float, logger: logging.Logger) -> dict:
    """Return a canned AquaSteel demo script."""
    logger.info("Using mock/canned AquaSteel script (no ANTHROPIC_API_KEY set)")
    beats = _make_mock_beats(target_duration_s)
    return _normalize_script("AquaSteel — Stay Hydrated, Stay Cold", beats)


# ---------------------------------------------------------------------------
# __main__
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    log = settings.get_logger("script_writer.__main__")
    script = write_script(
        brief="AquaSteel is a premium insulated water bottle that keeps drinks cold 24h.",
        product_doc_text="Made from 18/8 stainless steel. BPA-free. 32oz. Leak-proof lid.",
        brand={"tone": "authentic", "audience": "outdoor enthusiasts"},
        target_duration_s=30.0,
        logger=log,
    )
    print(json.dumps(script, indent=2))
    sys.exit(0)
