"""Audio processing routes — /audio/* proxied to DeepFilterNet backend (Phase 3).

Endpoints:
  POST /audio/denoise  — Remove background noise (returns cleaned WAV)
  POST /audio/convert  — Convert between audio formats (returns audio)
"""

import logging
from typing import Optional

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import JSONResponse, Response

from .backend_client import client
from .config import get_backend_url

router = APIRouter(prefix="/audio", tags=["audio"])
logger = logging.getLogger(__name__)

# Media type mapping for audio formats
_MEDIA_TYPES = {
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "flac": "audio/flac",
    "ogg": "audio/ogg",
}


def _get_config():
    from .main import get_backends_config
    return get_backends_config()


@router.post("/denoise")
async def denoise(file: UploadFile = File(...)):
    """Remove background noise from audio. Returns cleaned WAV. Proxied to DeepFilterNet."""
    config = _get_config()
    backend_url = get_backend_url(config, "deepfilter-audio")

    audio_data = await file.read()

    files = [("file", (file.filename or "audio.wav", audio_data, file.content_type or "audio/wav"))]

    resp = await client.request(
        "deepfilter-audio", "POST", f"{backend_url}/denoise",
        files=files,
        timeout_type="audio",
    )

    if resp.status_code != 200:
        return JSONResponse(
            status_code=resp.status_code,
            content={"error": "Audio backend error", "detail": resp.text},
        )

    return Response(
        content=resp.content,
        media_type="audio/wav",
        headers={"Content-Disposition": "attachment; filename=denoised.wav"},
    )


@router.post("/convert")
async def convert(
    file: UploadFile = File(...),
    output_format: str = Form("wav"),
    sample_rate: Optional[int] = Form(None),
    bitrate: Optional[str] = Form(None),
):
    """Convert audio between formats. Proxied to DeepFilterNet (ffmpeg)."""
    config = _get_config()
    backend_url = get_backend_url(config, "deepfilter-audio")

    audio_data = await file.read()

    files = [("file", (file.filename or "audio.wav", audio_data, file.content_type or "audio/wav"))]
    data = {"output_format": output_format}
    if sample_rate is not None:
        data["sample_rate"] = str(sample_rate)
    if bitrate is not None:
        data["bitrate"] = bitrate

    resp = await client.request(
        "deepfilter-audio", "POST", f"{backend_url}/convert",
        files=files,
        data=data,
        timeout_type="audio",
    )

    if resp.status_code != 200:
        return JSONResponse(
            status_code=resp.status_code,
            content={"error": "Audio backend error", "detail": resp.text},
        )

    media_type = _MEDIA_TYPES.get(output_format, "application/octet-stream")
    return Response(
        content=resp.content,
        media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename=converted.{output_format}"},
    )
