"""STT routes — /stt/* proxied to faster-whisper backend (Phase 2).

Endpoints:
  POST /stt/transcribe      — Full transcription (returns JSON)
  POST /stt/detect-language  — Detect spoken language
  POST /stt/stream           — Streaming transcription (SSE)
"""

import logging
from typing import Optional

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from .backend_client import client
from .config import get_backend_url

router = APIRouter(prefix="/stt", tags=["stt"])
logger = logging.getLogger(__name__)


def _get_config():
    from .main import get_backends_config
    return get_backends_config()


@router.post("/transcribe")
async def transcribe(
    file: UploadFile = File(...),
    language: Optional[str] = Form(None),
    word_timestamps: bool = Form(False),
):
    """Transcribe audio file. Proxied to faster-whisper backend."""
    config = _get_config()
    backend_url = get_backend_url(config, "whisper-stt")

    audio_data = await file.read()

    files = [("file", (file.filename or "audio.wav", audio_data, file.content_type or "audio/wav"))]
    data = {}
    if language:
        data["language"] = language
    if word_timestamps:
        data["word_timestamps"] = "true"

    resp = await client.request(
        "whisper-stt", "POST", f"{backend_url}/transcribe",
        files=files,
        data=data,
        timeout_type="stt",
    )

    return JSONResponse(status_code=resp.status_code, content=resp.json())


@router.post("/detect-language")
async def detect_language(file: UploadFile = File(...)):
    """Detect spoken language from audio. Proxied to faster-whisper backend."""
    config = _get_config()
    backend_url = get_backend_url(config, "whisper-stt")

    audio_data = await file.read()

    files = [("file", (file.filename or "audio.wav", audio_data, file.content_type or "audio/wav"))]

    resp = await client.request(
        "whisper-stt", "POST", f"{backend_url}/detect-language",
        files=files,
        timeout_type="stt",
    )

    return JSONResponse(status_code=resp.status_code, content=resp.json())


@router.post("/stream")
async def stream(
    file: UploadFile = File(...),
    language: Optional[str] = Form(None),
):
    """Stream transcription segments as Server-Sent Events. Proxied to faster-whisper."""
    config = _get_config()
    backend_url = get_backend_url(config, "whisper-stt")

    audio_data = await file.read()

    files = [("file", (file.filename or "audio.wav", audio_data, file.content_type or "audio/wav"))]
    data = {}
    if language:
        data["language"] = language

    return StreamingResponse(
        client.stream_bytes(
            "whisper-stt", "POST", f"{backend_url}/stream",
            files=files,
            data=data,
            timeout_type="stt",
        ),
        media_type="text/event-stream",
    )
