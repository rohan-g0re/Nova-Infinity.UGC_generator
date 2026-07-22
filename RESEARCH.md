# UGC Ad Video Generator — Research & Build Plan (July 2026)

## The core realization

Your business model changes everything. You have a **real client who records once on a green screen**. That means you do **not** have the "60-second problem" that kills everyone else.

The 60s problem only exists for people generating a talking human *from scratch* with a video model (Veo/Kling/Sora), because those cap at 5–10s per clip and drift on identity. **You skip that entirely.** Your talking human comes from *real recorded footage* + *lip re-sync*, which has **no length limit**. Only your B-roll needs AI generation — and B-roll is naturally short cuts anyway.

So the architecture that fits you is **"record once, generate infinite ads"** — not "generate an AI actor."

---

## Verdict on MoneyPrinter

**Not useful for you.** MoneyPrinter / MoneyPrinterTurbo / ShortGPT are *faceless* pipelines: script → TTS → stock-clip slideshow + captions. No real person, no lipsync, no product spokesperson. It's a stock-footage montage generator.

What IS worth stealing from them: the **script-segmentation + timed-caption + stock-broll-lookup** logic. That code is a solved, copyable pattern (see ShortGPT `content_video_engine.py`, keyed by `[t_start, t_end]` intervals). Use it for the B-roll layer only.

---

## Recommended architecture: "Record Once, Generate Many"

```
CLIENT (one-time): records 2–5 min green-screen base footage + 2 min clean voice sample
        │
        ├──► Voice clone (ElevenLabs) ──────────────┐
        │                                            │
        └──► Base face/body footage ─────────┐       │
                                             │       │
PER AD (infinite, no re-shoot):              ▼       ▼
  Product docs/images ──► LLM script writer ──► script (hook/problem/demo/proof/CTA beats)
        │                                            │
        │                          ┌─────────────────┤
        │                          ▼                 ▼
        │              TTS in cloned voice    beat timing plan
        │                          │
        │                          ▼
        │              LIPSYNC re-drive base footage to say NEW script
        │              (LatentSync / MuseTalk / Sync.so)  ← UNLIMITED length
        │                          │
        │                          ▼
        │              Green-screen matte (RVM / MatAnyone / ffmpeg chromakey)
        │                          │
        ├──► product B-roll ───────┤
        │    (Kling/Veo img2vid    ▼
        │     of product photos,   COMPOSITE person onto AI/stock background
        │     or Pexels stock)     │
        │                          ▼
        │              STITCH beats w/ jump cuts (ffmpeg concat/xfade)
        │                          │
        │                          ▼
        │              Word-level captions (WhisperX) + music ducking + loudnorm
        │                          │
        │                          ▼
        │                    FINISHED 10–60s 9:16 AD
```

**Why this wins:** the hard part (a realistic talking human for 60s) is not generated — it's your real client footage, re-lipped. Realism is automatic because it's a real person. You only AI-generate the easy, short stuff (product B-roll cutaways).

---

## Component stack (tested, ranked)

### 1. Lipsync — re-drive client footage to new scripts (THE core engine)

| Tool                                  | Type      | License             | Fit                                                                  |
| ------------------------------------- | --------- | ------------------- | -------------------------------------------------------------------- |
| **Sync.so** (API)               | hosted    | commercial          | **Start here.** $0.04–0.05/sec, no infra, best quality/effort |
| **LatentSync v1.6** (ByteDance) | self-host | Apache-2.0          | Best open quality (512²), needs video input, 8–18GB VRAM, batch    |
| **MuseTalk v1.5**               | self-host | MIT (commercial ok) | Real-time, 4GB VRAM, cheapest at scale, 256² face (lower detail)    |
| **EchoMimicV3** (AAAI 2026)     | self-host | Apache-2.0          | Newest, 1.3B DiT, 12GB, single-image capable                         |

Note: Wav2Lip is **non-commercial** — avoid for production. Sonic is **CC-BY-NC** — avoid.

Path: **prototype on Sync.so API → migrate hot path to self-hosted LatentSync/MuseTalk when volume justifies GPU.**

### 2. Voice clone

**ElevenLabs** — clone client voice from their base recording. ~$0.05–0.09 per 60s. Instant voice clone from 2 min sample. This is what lets the client "say" any future script.

### 3. Green-screen → background

- **Matte:** ffmpeg `chromakey` (fast/cheap) → upgrade to **MatAnyone** or **RVM** (Robust Video Matting) for clean edges/hair.
- **Composite:** ffmpeg `overlay` w/ `format=yuva420p`. Premultiplied-alpha `over`. Color-match keyed person to bg (`pymatting` / color transfer) so they look native.
- **Backgrounds:** AI-generated scene (Nano Banana / Flux image → static or Ken-Burns), or product setting.

### 4. Product B-roll (the only AI-generated video)

- **Image****-to-video** **of pr**oduct photos: **Kling 2.5 Turbo** (~$0.07/sec, best face+object realism, "Elements" for product lock) or **Veo 3.1** (native audio, pricier). 5–10s clips = perfect for cutaways.
- **Free stock fill:** Pexels + Pixabay APIs ($0, commercial ok).

### 5. Script → beats

LLM (Claude) writes 60s script segmented into beats: **hook (0–3s) → problem → demo → social proof → CTA**. Copy ShortGPT's timed-interval structure. Structured output (Pydantic/JSON schema) → `{beat, text, duration, broll_query, visual}`.

### 6. Assembly / captions / audio

- **Stitch:** ffmpeg `concat` demuxer (same codec, stream-copy = instant) or `xfade` for transitions. UGC style = hard jump cuts, so concat is usually right.
- **Captions:** WhisperX word-level timestamps → karaoke captions (ffmpeg drawtext or Remotion). This is the single highest-ROI "looks pro" element.
- **Audio:** `sidechaincompress` for music ducking under VO; `loudnorm I=-14 TP=-1.5` (YouTube/TikTok target).
- **Orchestration engine:** ffmpeg (cheapest, ~$0.01/render) or **Remotion** (React, programmatic, Lambda render ~$0.02) if you want templated designs. Avoid Shotstack (5–15× pricier).

---

## Cost per finished 60s ad

| Component                     | Cheap                                                 | Mid                  | Premium |
| ----------------------------- | ----------------------------------------------------- | -------------------- | ------- |
| Script (LLM)                  | ~$0.01 | ~$0.02                                       | ~$0.05               |         |
| TTS voice clone               | $0.05 (ElevenLabs Flash) | $0.09                      | $0.09                |         |
| Lipsync 60s                   | self-host ~$0.10 | Sync.so ~$2.40                     | HeyGen Precision ~$4 |         |
| Product B-roll (2× 8s Kling) | Pexels $0 | ~$1.10                                    | Veo ~$4              |         |
| Matte + composite + assembly  | ffmpeg ~$0.02 | ~$0.05                                | Remotion ~$0.10      |         |
| **TOTAL / 60s ad**      | **~$0.20** (self-host) | **~$3.70** (API) | **~$12**       |         |

Lipsync dominates cost. Self-hosting LatentSync/MuseTalk on one rented GPU crushes per-ad cost once you're doing volume. Compare: **Arcads charges ~$11–44 per usable ad** and can't even use *your* real client.

---

## What the market actually uses (creator intel)

- **Arcads** ($110/mo, 10 videos, ~$11 each; effective ~$44/usable after hook testing): most realistic *synthetic-actor* UGC, but no public pricing page, no free trial, no custom real-person. "Luxury tax" per Reddit.
- **Argil** ($39–499/mo, YC-backed): clone from 1 photo + 1 min voice, ~2 min render, built-in captions/b-roll/transitions, API. Closest to your "clone the client" need off-the-shelf.
- **HeyGen / Hedra / Synthesia**: avatar+lipsync APIs. Hedra Character-3 = best raw lipsync (9/10, phoneme-accurate). HeyGen Avatar IV/V = best expressiveness + solved identity drift on long videos. Both have APIs (~$1–4/60s). These are your **build-vs-buy benchmark** and a viable API backend for the lipsync layer.
- **Real workflows**: script → beat split → per-beat clip → stitch with jump cuts (jump cuts are *native* to UGC, hide seams for free) → word-captions in CapCut. Product-in-hand shots via image-to-video of a product photo.
- **Known failure modes**: lipsync drift on fast/long speech (>200wpm), hand/product artifacts, character inconsistency across generated clips (you dodge this — real footage), voice uncanniness. **Ad-policy:** Meta/TikTok require disclosure of AI-generated/synthetic people — matters less for you since it's a *real* consenting client.

---

## Frontier video models (for B-roll only — you don't need them for the human)

| Model           | Max clip    | Native audio  | I2V | Consistency     | $/sec  | Talking-head       |
| --------------- | ----------- | ------------- | --- | --------------- | ------ | ------------------ |
| Kling 2.5 Turbo | 10s         | 2.6=yes       | yes | Elements, 4-ref | ~$0.07 | best of gen models |
| Kling 2.6       | 10s         | **yes** | yes | 4-ref           | ~$0.08 | native lipsync     |
| Runway Gen-4.5  | 10s         | phased        | yes | References      | ~$0.12 | medium             |
| Luma Ray 3.2    | 20s         | no            | yes | 16 keyframes    | ~$0.06 | poor (no audio)    |
| Veo 3.1         | 8s + extend | **yes** | yes | ingredients     | higher | strong             |

**60s from these** = chain via last-frame/scene-extension (degrades after 2–3 min) or stitch 8–10s cuts. You'll only ever need short B-roll cuts, so this is a non-issue for you.

---

## Research papers worth knowing (multi-shot consistency)

Mostly relevant if you later generate *fully synthetic* multi-scene ads. Usable code: **VideoStudio** (ECCV24, entity-ref consistency), **MultiShotMaster** (Kling, CVPR26), **STAGE** (storyboard-anchored), **ShotStream** (real-time multi-shot, HuggingFace weights), **VideoGen-of-Thought**. Talking-human papers: **OmniHuman-1.5** (ByteDance, 60–90s, best realism 9.5/10, API via fal.ai/BytePlus ~$0.12–0.14/sec, not open-source) — this is the one to watch if you ever want full-body synthetic spokespeople.

---

## Build plan (phased)

**Phase 0 — Prove the core loop (1–2 wks).** One client's green-screen clip + ElevenLabs voice clone + Sync.so lipsync + ffmpeg chromakey composite + WhisperX captions. Manually script one 30s ad end-to-end. Goal: prove "record once → new script → believable ad."

**Phase 1 — Automate the pipeline.** LLM script→beats (structured JSON) → TTS → lipsync → matte → B-roll lookup (Pexels + Kling img2vid) → ffmpeg stitch + captions + audio. Queue-based (one job = one ad). Config-driven per client (footage path, voice ID, brand assets).

**Phase 2 — Cut cost, scale.** Migrate lipsync from Sync.so API to self-hosted LatentSync/MuseTalk on rented GPU (RunPod/Modal). Drops per-ad from ~$3.70 to ~$0.20. Add Remotion for branded caption/overlay templates.

**Phase 3 — Multi-tenant product.** Client portal: upload footage + product docs → pick ad length/angle → batch-generate variations for A/B testing. This is the actual business.

---

## Immediate next steps

1. Get one client's raw green-screen footage + 2-min clean voice sample.
2. ElevenLabs instant voice clone.
3. Sync.so API test: feed footage + new TTS line → verify lipsync quality on YOUR client (not a demo face).
4. If quality passes → build Phase 1. If not → try Hedra Character-3 API (best lipsync) before self-hosting.
