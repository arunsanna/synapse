"""Standalone faster-whisper STT microservice.

Endpoints:
  POST /transcribe       — Full transcription with word timestamps
  POST /detect-language   — Detect spoken language
  POST /stream            — SSE streaming segments
  GET  /health            — Health check
"""

import asyncio
import logging
import tempfile
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from config import settings
from models import (
    LanguageDetectionResult,
    LanguageInfo,
    TranscriptionResult,
    TranscriptSegment,
    TranscriptWord,
)

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

_MODEL_LOAD_TIMEOUT = 600.0

# --- Engine ---

_model = None
_lock = asyncio.Lock()


def _load_model():
    from faster_whisper import WhisperModel

    return WhisperModel(
        settings.model_size,
        device=settings.device,
        compute_type=settings.compute_type,
        download_root=settings.model_cache_dir,
    )


async def _ensure_loaded():
    global _model
    if _model is not None:
        return
    async with _lock:
        if _model is not None:
            return
        logger.info("Loading STT model: %s (device=%s, compute=%s)",
                     settings.model_size, settings.device, settings.compute_type)
        loop = asyncio.get_event_loop()
        _model = await asyncio.wait_for(
            loop.run_in_executor(None, _load_model),
            timeout=_MODEL_LOAD_TIMEOUT,
        )
        logger.info("STT model loaded")


# --- Sync inference helpers ---

def _transcribe_sync(audio_path: str, language: str | None, word_timestamps: bool) -> dict:
    kwargs = {"word_timestamps": word_timestamps}
    if language:
        kwargs["language"] = language

    segments_iter, info = _model.transcribe(audio_path, **kwargs)

    segments = []
    full_text_parts = []
    for seg in segments_iter:
        words = None
        if word_timestamps and seg.words:
            words = [
                TranscriptWord(word=w.word, start=w.start, end=w.end, probability=w.probability)
                for w in seg.words
            ]
        segments.append(TranscriptSegment(
            id=seg.id, text=seg.text.strip(), start=seg.start, end=seg.end, words=words
        ))
        full_text_parts.append(seg.text.strip())

    return TranscriptionResult(
        text=" ".join(full_text_parts),
        language=info.language,
        language_probability=info.language_probability,
        duration=info.duration,
        segments=segments,
    ).model_dump()


def _detect_language_sync(audio_path: str) -> dict:
    _, info = _model.transcribe(audio_path, language=None)

    all_langs = []
    if hasattr(info, "all_language_probs") and info.all_language_probs:
        sorted_probs = sorted(info.all_language_probs, key=lambda x: x[1], reverse=True)[:5]
        all_langs = [LanguageInfo(code=lang, name=lang) for lang, _ in sorted_probs]

    return LanguageDetectionResult(
        detected_language=info.language,
        probability=info.language_probability,
        all_languages=all_langs,
    ).model_dump()


def _stream_segments_sync(audio_path: str, language: str | None) -> list[dict]:
    kwargs = {"word_timestamps": True}
    if language:
        kwargs["language"] = language

    segments_iter, _ = _model.transcribe(audio_path, **kwargs)

    segments = []
    for seg in segments_iter:
        words = None
        if seg.words:
            words = [
                TranscriptWord(word=w.word, start=w.start, end=w.end, probability=w.probability)
                for w in seg.words
            ]
        segments.append(TranscriptSegment(
            id=seg.id, text=seg.text.strip(), start=seg.start, end=seg.end, words=words
        ).model_dump())
    return segments


# --- App ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("whisper-stt starting (model=%s, device=%s)", settings.model_size, settings.device)
    yield
    logger.info("whisper-stt shutting down")


app = FastAPI(title="Whisper STT", lifespan=lifespan)


async def _save_upload(file: UploadFile) -> str:
    """Save uploaded file to a temp path and return the path."""
    suffix = ".wav"
    if file.filename and "." in file.filename:
        suffix = "." + file.filename.rsplit(".", 1)[1]
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    content = await file.read()
    tmp.write(content)
    tmp.close()
    return tmp.name


def _cleanup(path: str):
    import os
    try:
        os.unlink(path)
    except OSError:
        pass


@app.get("/health")
async def health():
    return {"status": "ok", "model_loaded": _model is not None, "model": settings.model_size}


@app.post("/transcribe")
async def transcribe(
    file: UploadFile = File(...),
    language: str | None = Form(None),
    word_timestamps: bool = Form(True),
):
    await _ensure_loaded()
    tmp_path = await _save_upload(file)
    try:
        async with _lock:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, _transcribe_sync, tmp_path, language, word_timestamps
            )
        return JSONResponse(result)
    finally:
        _cleanup(tmp_path)


@app.post("/detect-language")
async def detect_language(file: UploadFile = File(...)):
    await _ensure_loaded()
    tmp_path = await _save_upload(file)
    try:
        async with _lock:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, _detect_language_sync, tmp_path)
        return JSONResponse(result)
    finally:
        _cleanup(tmp_path)


@app.post("/stream")
async def stream_transcribe(
    file: UploadFile = File(...),
    language: str | None = Form(None),
):
    await _ensure_loaded()
    tmp_path = await _save_upload(file)

    async def event_generator() -> AsyncGenerator:
        try:
            async with _lock:
                loop = asyncio.get_event_loop()
                segments = await loop.run_in_executor(
                    None, _stream_segments_sync, tmp_path, language
                )
            import json
            for seg in segments:
                yield {"event": "segment", "data": json.dumps(seg)}
            yield {"event": "done", "data": "{}"}
        finally:
            _cleanup(tmp_path)

    return EventSourceResponse(event_generator())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.host, port=settings.port)
