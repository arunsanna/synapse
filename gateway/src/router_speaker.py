"""Speaker routes — /speakers/* proxied to pyannote backend (Phase 3).

Endpoints:
  POST /speakers/diarize  — Speaker diarization (who spoke when)
  POST /speakers/verify   — Speaker verification (are these the same person?)
"""

import logging
from typing import Optional

from fastapi import APIRouter, File, Form, UploadFile

from .backend_client import client
from .config import get_backend_url
from .http_utils import json_or_error_response

router = APIRouter(prefix="/speakers", tags=["speaker"])
logger = logging.getLogger(__name__)


def _get_config():
    from .main import get_backends_config
    return get_backends_config()


@router.post("/diarize")
async def diarize(
    file: UploadFile = File(...),
    num_speakers: Optional[int] = Form(None),
    min_speakers: Optional[int] = Form(None),
    max_speakers: Optional[int] = Form(None),
):
    """Diarize audio — identify who spoke when. Proxied to pyannote backend."""
    config = _get_config()
    backend_url = get_backend_url(config, "pyannote-speaker")

    audio_data = await file.read()

    files = [("file", (file.filename or "audio.wav", audio_data, file.content_type or "audio/wav"))]
    data = {}
    if num_speakers is not None:
        data["num_speakers"] = str(num_speakers)
    if min_speakers is not None:
        data["min_speakers"] = str(min_speakers)
    if max_speakers is not None:
        data["max_speakers"] = str(max_speakers)

    resp = await client.request(
        "pyannote-speaker", "POST", f"{backend_url}/diarize",
        files=files,
        data=data,
        timeout_type="speaker",
    )

    return json_or_error_response(resp, "Speaker backend error")


@router.post("/verify")
async def verify(
    file1: UploadFile = File(...),
    file2: UploadFile = File(...),
):
    """Verify if two audio samples are from the same speaker. Proxied to pyannote."""
    config = _get_config()
    backend_url = get_backend_url(config, "pyannote-speaker")

    data1 = await file1.read()
    data2 = await file2.read()

    files = [
        ("file1", (file1.filename or "speaker1.wav", data1, file1.content_type or "audio/wav")),
        ("file2", (file2.filename or "speaker2.wav", data2, file2.content_type or "audio/wav")),
    ]

    resp = await client.request(
        "pyannote-speaker", "POST", f"{backend_url}/verify",
        files=files,
        timeout_type="speaker",
    )

    return json_or_error_response(resp, "Speaker backend error")
