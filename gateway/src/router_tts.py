"""TTS routes — /tts/*, /voices/* proxied to Chatterbox TTS backend.

Chatterbox uses a two-step flow for voice cloning:
  1. POST /upload_reference  (multipart file → returns filename)
  2. POST /tts               (JSON with reference_audio_filename)

The gateway caches uploaded filenames per voice_id to avoid re-uploading
the same reference on every synthesis request.
"""

import io
import logging
import os
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from .backend_client import client
from .config import get_backend_url
from .models import InterpolateRequest, SynthesizeRequest, StreamRequest

router = APIRouter(tags=["tts"])
logger = logging.getLogger(__name__)

# Cache: voice_id → filename returned by Chatterbox /upload_reference
_ref_upload_cache: dict[str, str] = {}
_MAX_VOICE_FILES = 10
_MAX_VOICE_FILE_SIZE = 50 * 1024 * 1024  # 50 MB
_ALLOWED_WAV_CONTENT_TYPES = {
    "",
    "application/octet-stream",
    "audio/wav",
    "audio/wave",
    "audio/x-wav",
    "audio/vnd.wave",
}


def _get_config():
    from .main import get_backends_config
    return get_backends_config()


def _get_vm():
    from .main import get_voice_manager
    return get_voice_manager()


async def _collect_validated_wav_files(files: list[UploadFile]) -> list[bytes]:
    """Read uploaded files and enforce basic WAV constraints."""
    if not files or len(files) > _MAX_VOICE_FILES:
        raise HTTPException(400, f"Provide 1-{_MAX_VOICE_FILES} WAV reference files")

    audio_data: list[bytes] = []
    for f in files:
        content_type = (f.content_type or "").lower()
        filename = (f.filename or "").lower()
        is_wav_name = filename.endswith(".wav")
        is_wav_type = content_type in _ALLOWED_WAV_CONTENT_TYPES
        if not is_wav_name and not is_wav_type:
            raise HTTPException(400, f"Only WAV files are supported: {f.filename}")

        data = await f.read()
        if len(data) < 44:  # WAV header minimum
            raise HTTPException(400, f"File too small: {f.filename}")
        if len(data) > _MAX_VOICE_FILE_SIZE:
            raise HTTPException(400, f"File too large (max 50MB): {f.filename}")
        audio_data.append(data)
    return audio_data


# --- Voice CRUD (local, backed by PVC) ---


@router.get("/voices")
async def list_voices():
    """List all voices in the library."""
    vm = _get_vm()
    voices = await vm.list_voices()
    return [v.model_dump() for v in voices]


@router.post("/voices", status_code=201)
async def upload_voice(
    name: Annotated[str, Form()],
    files: list[UploadFile] = File(...),
):
    """Upload a new voice with 1-10 WAV reference samples."""
    audio_data = await _collect_validated_wav_files(files)

    vm = _get_vm()
    result = await vm.upload_voice(name, audio_data)
    return result.model_dump()


@router.post("/voices/{voice_id}/references")
async def add_references(
    voice_id: str,
    files: list[UploadFile] = File(...),
):
    """Add more reference samples to an existing voice."""
    audio_data = await _collect_validated_wav_files(files)

    vm = _get_vm()
    try:
        result = await vm.add_references(voice_id, audio_data)
    except FileNotFoundError:
        raise HTTPException(404, f"Voice not found: {voice_id}")

    return result.model_dump()


@router.delete("/voices/{voice_id}")
async def delete_voice(voice_id: str):
    """Delete a voice and all its reference files."""
    vm = _get_vm()
    deleted = vm.delete_voice(voice_id)
    if not deleted:
        raise HTTPException(404, f"Voice not found: {voice_id}")
    _ref_upload_cache.pop(voice_id, None)
    return {"status": "deleted", "voice_id": voice_id}


# --- Helpers ---


async def _upload_reference_to_chatterbox(
    backend_url: str, voice_id: str, ref_path: str
) -> str:
    """Upload a reference WAV to Chatterbox and cache the returned filename.

    Chatterbox requires a two-step flow:
      1. POST /upload_reference (multipart) → returns {"uploaded_files": ["filename.wav"], ...}
      2. POST /tts (JSON with reference_audio_filename)

    We cache the filename per voice_id so subsequent synthesis calls skip re-upload.
    """
    if voice_id in _ref_upload_cache:
        return _ref_upload_cache[voice_id]

    with open(ref_path, "rb") as f:
        filename = f"{voice_id}_{os.path.basename(ref_path)}"
        resp = await client.request(
            "chatterbox-tts", "POST",
            f"{backend_url}/upload_reference",
            files=[("files", (filename, f, "audio/wav"))],
            timeout_type="default",
        )

    if resp.status_code != 200:
        raise HTTPException(502, f"Failed to upload reference to Chatterbox: {resp.text}")

    data = resp.json()
    uploaded_files = data.get("uploaded_files", [])
    if not uploaded_files:
        raise HTTPException(502, "Chatterbox returned no uploaded files")

    remote_filename = uploaded_files[0]
    _ref_upload_cache[voice_id] = remote_filename
    logger.info("Uploaded reference for voice %s → %s", voice_id, remote_filename)
    return remote_filename


# --- TTS synthesis (proxied to Chatterbox) ---


@router.post("/tts/synthesize")
async def synthesize(req: SynthesizeRequest):
    """Synthesize speech. Resolves voice references, proxies to Chatterbox.

    Uses Chatterbox two-step flow:
      - clone mode: upload reference via /upload_reference, then /tts with filename
      - predefined mode: /tts with predefined_voice_id
    """
    config = _get_config()
    backend_url = get_backend_url(config, "chatterbox-tts")

    tts_payload: dict = {
        "text": req.text,
        "split_text": req.split_sentences,
    }
    if req.language:
        tts_payload["language"] = req.language
    if req.speed != 1.0:
        tts_payload["speed_factor"] = req.speed

    if req.voice_id:
        # Clone mode: upload reference first, then synthesize
        vm = _get_vm()
        ref_paths = vm.get_reference_paths(req.voice_id)
        if not ref_paths:
            raise HTTPException(404, f"Voice not found or has no references: {req.voice_id}")

        remote_filename = await _upload_reference_to_chatterbox(
            backend_url, req.voice_id, ref_paths[0]
        )
        tts_payload["voice_mode"] = "clone"
        tts_payload["reference_audio_filename"] = remote_filename
    else:
        # Predefined mode: use default voice
        tts_payload["voice_mode"] = "predefined"
        tts_payload["predefined_voice_id"] = "Alice.wav"

    resp = await client.request(
        "chatterbox-tts", "POST", f"{backend_url}/tts",
        json=tts_payload,
        timeout_type="tts",
    )

    if resp.status_code != 200:
        return JSONResponse(
            status_code=resp.status_code,
            content={"error": "TTS backend error", "detail": resp.text},
        )

    return StreamingResponse(
        io.BytesIO(resp.content),
        media_type="audio/wav",
        headers={"Content-Disposition": "attachment; filename=synapse_tts.wav"},
    )


@router.post("/tts/stream")
async def stream_tts(req: StreamRequest):
    """Stream TTS audio via Chatterbox /v1/audio/speech (OpenAI-compatible).

    Voice cloning via streaming: pre-uploads reference, then uses the /tts
    endpoint (Chatterbox /v1/audio/speech doesn't support clone mode).
    When voice_id is provided, falls back to non-streaming /tts endpoint.
    """
    config = _get_config()
    backend_url = get_backend_url(config, "chatterbox-tts")

    if req.voice_id:
        # Voice cloning not supported via OpenAI-compatible streaming endpoint.
        # Fall back to the /tts endpoint (non-chunked but supports cloning).
        vm = _get_vm()
        ref_paths = vm.get_reference_paths(req.voice_id)
        if not ref_paths:
            raise HTTPException(404, f"Voice not found: {req.voice_id}")

        remote_filename = await _upload_reference_to_chatterbox(
            backend_url, req.voice_id, ref_paths[0]
        )

        tts_payload = {
            "text": req.text,
            "voice_mode": "clone",
            "reference_audio_filename": remote_filename,
            "split_text": req.split_sentences,
        }
        if req.language:
            tts_payload["language"] = req.language
        if req.speed != 1.0:
            tts_payload["speed_factor"] = req.speed

        resp = await client.request(
            "chatterbox-tts", "POST", f"{backend_url}/tts",
            json=tts_payload,
            timeout_type="tts",
        )

        if resp.status_code != 200:
            return JSONResponse(
                status_code=resp.status_code,
                content={"error": "TTS backend error", "detail": resp.text},
            )

        return StreamingResponse(
            io.BytesIO(resp.content),
            media_type="audio/wav",
            headers={"Content-Disposition": "attachment; filename=synapse_tts.wav"},
        )

    # Default voice: use OpenAI-compatible streaming endpoint
    return StreamingResponse(
        client.stream_bytes(
            "chatterbox-tts", "POST",
            f"{backend_url}/v1/audio/speech",
            json={
                "model": "chatterbox",
                "input": req.text,
                "voice": "Alice.wav",
                "speed": req.speed,
            },
            timeout_type="tts",
        ),
        media_type="audio/wav",
    )


@router.post("/tts/interpolate")
async def interpolate(req: InterpolateRequest):
    """Blend multiple voices and synthesize. Proxied to Chatterbox.

    Chatterbox doesn't support native interpolation — uses the
    highest-weighted voice for cloning.
    """
    config = _get_config()
    backend_url = get_backend_url(config, "chatterbox-tts")
    vm = _get_vm()

    # Resolve all voice references
    voice_refs = []
    for vw in req.voices:
        paths = vm.get_reference_paths(vw.voice_id)
        if not paths:
            raise HTTPException(404, f"Voice not found: {vw.voice_id}")
        voice_refs.append({"voice_id": vw.voice_id, "path": paths[0], "weight": vw.weight})

    # Use the highest-weighted voice for cloning
    primary = max(voice_refs, key=lambda v: v["weight"])

    remote_filename = await _upload_reference_to_chatterbox(
        backend_url, primary["voice_id"], primary["path"]
    )

    tts_payload = {
        "text": req.text,
        "voice_mode": "clone",
        "reference_audio_filename": remote_filename,
        "split_text": True,
    }
    if req.language:
        tts_payload["language"] = req.language
    if req.speed != 1.0:
        tts_payload["speed_factor"] = req.speed

    resp = await client.request(
        "chatterbox-tts", "POST", f"{backend_url}/tts",
        json=tts_payload,
        timeout_type="tts",
    )

    if resp.status_code != 200:
        return JSONResponse(
            status_code=resp.status_code,
            content={"error": "TTS backend error", "detail": resp.text},
        )

    return StreamingResponse(
        io.BytesIO(resp.content),
        media_type="audio/wav",
        headers={"Content-Disposition": "attachment; filename=synapse_interpolate.wav"},
    )


# --- TTS metadata ---


# Chatterbox Turbo supported languages (ISO 639-1)
SUPPORTED_LANGUAGES = [
    {"code": "en", "name": "English"},
    {"code": "de", "name": "German"},
    {"code": "es", "name": "Spanish"},
    {"code": "fr", "name": "French"},
    {"code": "hi", "name": "Hindi"},
    {"code": "it", "name": "Italian"},
    {"code": "ja", "name": "Japanese"},
    {"code": "ko", "name": "Korean"},
    {"code": "nl", "name": "Dutch"},
    {"code": "pl", "name": "Polish"},
    {"code": "pt", "name": "Portuguese"},
    {"code": "ru", "name": "Russian"},
    {"code": "tr", "name": "Turkish"},
    {"code": "zh", "name": "Chinese"},
    {"code": "ar", "name": "Arabic"},
    {"code": "cs", "name": "Czech"},
    {"code": "da", "name": "Danish"},
    {"code": "fi", "name": "Finnish"},
    {"code": "hu", "name": "Hungarian"},
    {"code": "nb", "name": "Norwegian"},
    {"code": "ro", "name": "Romanian"},
    {"code": "sv", "name": "Swedish"},
    {"code": "uk", "name": "Ukrainian"},
]


@router.get("/tts/languages")
async def list_languages():
    """List supported TTS languages."""
    return SUPPORTED_LANGUAGES
