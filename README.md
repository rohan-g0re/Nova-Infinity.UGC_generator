# Record Once, Generate Many — UGC Ad Pipeline

A CLI pipeline that takes a single recorded footage clip and a beats JSON script, then automatically generates polished UGC-style video ads. **Claude Code writes the script JSON → the pipeline renders it.** Given a client config and a script file, it synthesises a cloned voice (ElevenLabs), composites the green-screen person over a **per-beat scene background**, adds lip-sync (Sync.so), overlays music, burns captions, and exports a final vertical video ready for social media.

## Workflow: Claude Code writes the script, the pipeline renders it

The LLM scripting step is done by **Claude Code authoring a beats JSON file** — no LLM API key is needed. You (or Claude Code) write a script file like `briefs/aquasteel.json`, then hand it to the renderer:

```bash
python -m pipeline.run --client demo --script-file briefs/aquasteel.json
```

The Anthropic API path still exists as an **optional legacy fallback** (see below), but it is never required.

### Script JSON schema

The `--script-file` may be either a bare list of beats or an object `{"title": ..., "beats": [...]}`. It must contain exactly 5 beats (`hook`, `problem`, `demo`, `proof`, `cta`). Each beat has:

| Field | Description |
|-------|-------------|
| `beat` | One of `hook`, `problem`, `demo`, `proof`, `cta` |
| `text` | Spoken first-person UGC voiceover for this beat |
| `duration_s` | Estimated spoken duration in seconds (all beats sum to ~28–32s) |
| `broll_query` | 2–4 word stock-footage search phrase for the beat's b-roll cutaway |
| `background_query` | **The SCENE the spokesperson stands in** for this beat (e.g. `"bright modern kitchen interior"`). Each talking-head beat composites the chromakeyed client over this per-beat scene background — not raw green, and not a single shared office background. |
| `visual` | Brief shot note |

`background_query` is optional per-beat: if omitted, a sensible scene is derived from the beat type / `visual` / `broll_query`. See `briefs/aquasteel.json` for a complete example.

## Setup

```powershell
# 1. Activate the virtual environment (Windows PowerShell)
.venv\Scripts\Activate.ps1

# 2. Install dependencies
pip install -r requirements.txt

# 3. (Optional) Configure API keys for live paths
copy .env.example .env
# Then open .env and fill in any keys you have — all are optional; the
# pipeline falls back gracefully when a key is absent.
```

## Usage

```bash
# Primary: render a Claude-Code-authored script
python -m pipeline.run --client demo --script-file briefs/aquasteel.json

# Without --script-file: legacy Anthropic path (optional) → mock fallback
python -m pipeline.run --client demo --brief "30s ad for AquaSteel water bottle"
```

With no API keys set, the pipeline runs in **full-mock mode** and still produces `output/<timestamp>/final.mp4`.

## Pipeline Stages

| Stage | Description | Env var needed | Mock fallback |
|-------|-------------|----------------|---------------|
| Script generation | Claude Code writes the beats JSON; pipeline loads it | none (Claude Code writes JSON) — Anthropic API optional | Hard-coded placeholder script |
| Voice synthesis | ElevenLabs clones voice and generates audio | `ELEVENLABS_API_KEY` | gTTS (Google TTS, no key needed) |
| Lip-sync | Sync.so animates the footage to match audio | `SYNC_SO_API_KEY` or `SYNC_MOCK=1` | Passes footage through unchanged |
| Scene compositing | ffmpeg composites the green-screen person over a per-beat scene background | `PEXELS_API_KEY` / `PIXABAY_API_KEY` (for scene footage) | Bundled background image → solid color |
| B-roll generation | Generative video (Sora 2 / Veo 3.1 / Kling) then stock footage | `OPENAI_API_KEY` / `GEMINI_API_KEY` / `FAL_KEY`, `PEXELS_API_KEY`, `PIXABAY_API_KEY` | Stock footage → Ken-Burns on product image |
| Music mix | ffmpeg mixes in background music track | — (ffmpeg, always runs) | Skipped if no music file |

## B-roll video-generation backends

Per beat, the pipeline tries generative video backends in priority order, then falls through to stock footage when keys are absent or budgets are exhausted:

| Backend | Env var | Notes |
|---------|---------|-------|
| Sora 2 | `OPENAI_API_KEY` | OpenAI text-to-video |
| Veo 3.1 | `GEMINI_API_KEY` | Google Gemini text-to-video |
| Kling (img2vid) | `FAL_KEY` | fal.ai image-to-video from the product photo |
| Stock | `PEXELS_API_KEY` / `PIXABAY_API_KEY` | Pexels/Pixabay clips → Ken-Burns fallback |

Config (via env, read in `pipeline/settings.py`):

- `BROLL_BACKENDS` — comma-separated priority order (default `sora,veo,kling,stock`).
- `MAX_GEN_CLIPS` — max generative clips per run (default `2`); remaining beats use stock. Caps spend on paid video-gen APIs.

When no video-gen keys are present, every beat cleanly falls through to stock footage — no key is required.

## Live Run

Command executed:

```bash
python -m pipeline.run --client demo --brief "30-second energetic UGC ad for AquaSteel insulated water bottle"
```

Assets used: `assets/clients/demo/green_screen/greenscreen_person_1.mp4` (52s, 1080p, trimmed to ~28s before lipsync to conserve Sync.so quota) and `assets/clients/demo/voice/sample.mp3`.

Output location pattern: `output/<timestamp>/final.mp4`. The demo produced `output/20260715-155456/final.mp4` — a valid 26.1s, 1080x1920, h264/AAC stereo vertical ad at ~7 MB.

### Stage status

| Stage | Status | Evidence / Reason |
|-------|--------|-------------------|
| Script (Claude Code JSON) | FILE | Script authored by Claude Code as a beats JSON file (`briefs/aquasteel.json`) and loaded via `--script-file`. No LLM API spend. The optional Anthropic API path is available but not used here. |
| Voice clone (ElevenLabs IVC) | FALLBACK | Free ElevenLabs tier does not include Instant Voice Cloning — API returned 400 `payment_required / paid_plan_required`. Fell back to stock prebuilt voice "Charlie" (`IKne3meq5aSn9XLyUdCD`). |
| TTS synthesis (ElevenLabs) | **LIVE** | Real ElevenLabs TTS (`eleven_flash_v2_5`) used with the stock voice. All 5 beat voiceovers synthesised live (~26s of VO total). |
| B-roll stock (Pexels) | **LIVE** | Real 4K portrait footage downloaded for 4 of 5 beats. Pexels was preferred over Pixabay at runtime. |
| B-roll stock (Pixabay) | **LIVE** (validated) | API key validated in smoke test; Pexels was used at runtime. |
| B-roll AI gen (fal.ai Kling img2vid) | FALLBACK | fal.ai account balance is $0 and locked — API returned 403 "User is locked. Reason: Exhausted balance". Even file uploads/storage are blocked at this tier. Fell back to Ken-Burns zoompan on the product image. |
| Lipsync (Sync.so) | FALLBACK | Sync.so returned 402 Payment Required on the lipsync-2 generate call — free Hobbyist quota did not cover the API job. The integration is fully implemented (native `/v2/assets/upload`, submit, poll, download with retry/backoff); uploads succeeded but paid generation was blocked. Fell back to mock mux (audio muxed over video, no real lip re-sync). |
| Compositing (chromakey + background) | **LIVE** | Local ffmpeg chromakey composite — always runs. |
| Audio mix (music ducking + loudnorm) | **LIVE** | Local ffmpeg — always runs. |
| Captions (beats-based ASS timing) | **LIVE** (beat-level) | `ctranslate2` / faster-whisper not installed, so word-level transcription fell back to beat-level timing. Install `ctranslate2` to enable word-level karaoke captions. |

### CLI flags added

| Flag | Purpose |
|------|---------|
| `--script-file <path>` | Load the beats JSON authored by Claude Code (PRIMARY input) — no Anthropic API call, no mock. On schema failure the run falls back to a mock script so it never hard-crashes. |
| `--force` | Regenerate all stages, ignoring any cached artifacts from a previous run. |
| `--reuse-dir <path>` | Reuse an existing run directory and its cached artifacts (skip already-completed stages). |

A `cost_log.json` and `cost_log.txt` are written to each run's output directory with per-stage cost estimates.

## User Review Items (Billing / Plans)

The pipeline uses graceful fallbacks for all of the following — nothing was auto-purchased. These are manual actions required to enable the live code paths.

> **Note on scripting:** LLM scripting is done by **Claude Code writing the beats JSON** (`--script-file`) — no API is needed and there is nothing to fund. Anthropic API is an **optional legacy path**, not a requirement.

| Service | What's blocked | Current fallback | Action required |
|---------|---------------|-----------------|-----------------|
| Anthropic API (optional) | Legacy live LLM script generation (only used without `--script-file`) | Claude-Code-authored JSON, or canned mock script | Optional — not needed. Add credits at [console.anthropic.com/settings/billing](https://console.anthropic.com/settings/billing) only if you want the legacy API path. |
| ElevenLabs | Instant Voice Cloning (IVC) of the client's real voice | Stock "Charlie" voice — TTS itself works live | Upgrade to Starter tier or above at [elevenlabs.io/subscription](https://elevenlabs.io/subscription) (see also [elevenlabs.io/pricing](https://elevenlabs.io/pricing)) |
| OpenAI (Sora 2) | Generative b-roll video | Veo/Kling → stock footage → Ken-Burns | Add `OPENAI_API_KEY` with Sora access |
| Google Gemini (Veo 3.1) | Generative b-roll video | Kling → stock footage → Ken-Burns | Add `GEMINI_API_KEY` with Veo access |
| fal.ai (Kling) | AI product-video generation AND file uploads/storage | Ken-Burns pan/zoom on the product photo | Add credits at [fal.ai/dashboard/billing](https://fal.ai/dashboard/billing) |
| Sync.so | Real lipsync job (lipsync-2 model) | Mock audio mux — no actual lip re-sync | Check plan/quota at [app.sync.so](https://app.sync.so/) (dashboard → billing/plans) |

Once any of these are funded or upgraded, no code changes are needed — the same commands will use the live path automatically.

**Optional local dependency:** installing `ctranslate2` (plus the faster-whisper backend) enables word-level karaoke captions; without it, captions fall back to beat-level timing.
