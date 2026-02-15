# Synapse Changelog

## 2026-02-15 -- Voice Cloning E2E Verification (all passing)

**E2E test results** (17 endpoints tested):

- 17/17 endpoints passing (200 OK with correct response data)
- Voice cloning works including collision scenario (upload short voice A -> delete -> upload long voice B -> synthesize with B -> 200 OK)

**Reference filename collision bug**: Found and fixed. Gateway now prefixes uploaded filenames with `voice_id` to prevent collisions when multiple voices use the same `ref_001.wav` filename. Cache is invalidated on voice deletion.

**Additional validation**:

- Chatterbox requires reference audio >5 seconds (hardcoded assertion in `tts_turbo.py:221`)
- Circuit breaker correctly triggers after 5 consecutive 500s, recovers after 30s cooldown
- TTS stream with voice cloning (fallback to `/tts` endpoint) works correctly

## 2026-02-15 -- Phase 2 + 3 Deployment (STT, Speaker, Audio)

**Deployment**:

- Built and deployed whisper-stt (faster-whisper large-v3-turbo, int8, CPU)
- Built and deployed pyannote-speaker (pyannote 3.1, CPU)
- Built and deployed deepfilter-audio (DeepFilterNet3 + ffmpeg, CPU)
- Replaced all 501 stub routers with real multipart-file-forwarding proxies in gateway
- Updated gateway ConfigMap with 3 new backend URLs and routes
- All 6 pods running (gateway + 5 backends), all health checks passing

**Fixes during deployment**:

| Issue                                       | Resolution                                                                  |
| ------------------------------------------- | --------------------------------------------------------------------------- |
| `groupadd: group 'audio' already exists`    | Renamed user/group from `audio` to `appuser` in deepfilter-audio Dockerfile |
| `No module named 'torchaudio'`              | Added `torchaudio` to deepfilter-audio requirements.txt                     |
| `No module named 'torchaudio.backend'`      | Pinned `torch==2.1.0` + `torchaudio==2.1.0` (DeepFilterNet needs older API) |
| `No such file or directory: 'git'`          | Added `git` to deepfilter-audio Dockerfile apt-get install                  |
| `use_auth_token` TypeError in pyannote      | Changed `use_auth_token=` to `token=` in pyannote-speaker main.py           |
| `imagePullPolicy: IfNotPresent` stale cache | Temporarily patched to `Always` to force pull new images, then reverted     |

**Resolved**: pyannote-speaker HF_TOKEN configured and speaker endpoints fully operational.

## 2026-02-14 -- Voice Cloning Bug Fix

| Issue                                                         | Resolution                                                                                                                                                                       |
| ------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `data.get("files", [])` in `router_tts.py` returns empty list | Changed to `data.get("uploaded_files", [])` -- Chatterbox `/upload_reference` returns `{"uploaded_files": [...]}`, not `{"files": [...]}`. This caused all voice cloning to 502. |

## 2026-02-14 -- Full Phase 1 Deployment

**Deployment**:

- Built and deployed Synapse Gateway (`registry.arunlabs.com/synapse-gateway:latest`)
- Built Chatterbox TTS CPU image (`registry.arunlabs.com/chatterbox-tts-server:cpu-latest`)
- Deployed all PVCs: model-cache (10Gi), hf-cache (20Gi), voices (5Gi)
- All 3 pods running and healthy, all endpoints verified E2E

**Fixes during deployment**:

| Issue                                       | Resolution                                                                                 |
| ------------------------------------------- | ------------------------------------------------------------------------------------------ |
| `exec format error` on gateway              | ARM/x86 mismatch -- rebuilt image on forge (x86_64) instead of megamind (arm64)            |
| `args: ["--nvidia"]` crashes Chatterbox     | Removed -- NVIDIA entrypoint doesn't accept CLI flags, Chatterbox uses `python3 server.py` |
| `CUDA error: no kernel image` on RTX 5090   | Built CPU variant with `RUNTIME=cpu` -- Blackwell (sm_120) not in stable PyTorch           |
| `predefined_voice_id: "default.wav"` -> 404 | Changed to `Alice.wav` -- Chatterbox ships 28 named voices, no `default.wav`               |
| `/v1/audio/speech` voice `"default"` -> 404 | Changed to `Alice.wav` -- OpenAI-compatible endpoint also requires named voice             |
| Deployment strategy `RollingUpdate` stuck   | Changed to `Recreate` -- single GPU (or CPU-bound) can't run 2 pods simultaneously         |
| No HF cache persistence                     | Added `chatterbox-hf-cache` PVC (20Gi) mounted at `/app/hf_cache`                          |
| No startup probe                            | Added startup probe (40 x 15s = 10min window) for first-time model download                |

## 2026-02-14 -- Voice Agent Feedback (all resolved)

| Issue                                           | Severity | Resolution                                                                                                                                         |
| ----------------------------------------------- | -------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| Chatterbox API is two-step, not multipart       | CRITICAL | Rewrote `router_tts.py`: upload via `/upload_reference`, synthesize via `/tts` with `reference_audio_filename`. Added per-voice_id filename cache. |
| `/tts/stream` ignores voice_id                  | CRITICAL | Stream with `voice_id` now falls back to `/tts` endpoint (two-step flow). Default voice uses `/v1/audio/speech` (chunked).                         |
| Default synthesis missing `predefined_voice_id` | CRITICAL | Added `predefined_voice_id: "Alice.wav"` when `voice_mode` is `"predefined"`.                                                                      |
| Chatterbox PVC mount path wrong                 | HIGH     | Changed `mountPath: /app/voices` to `/app/reference_audio` in `chatterbox-tts.yaml`.                                                               |
| No file upload size limits                      | MEDIUM   | Added 50MB per-file limit on voice upload.                                                                                                         |
| TTS timeout 60s too short                       | MEDIUM   | Increased TTS timeout from 60s to 120s in `backend_client.py`.                                                                                     |
| Missing `/tts/languages` endpoint               | MEDIUM   | Added `GET /tts/languages` returning 23 supported languages.                                                                                       |
