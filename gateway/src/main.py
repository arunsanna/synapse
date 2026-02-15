"""Synapse Gateway — unified AI proxy for ArunLabs Forge cluster."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .backend_client import client
from .config import load_backends_config, settings
from .voice_manager import VoiceManager

logger = logging.getLogger(__name__)

# Shared state populated at startup
_backends_config: dict = {}
_voice_manager: VoiceManager | None = None


def get_backends_config() -> dict:
    return _backends_config


def get_voice_manager() -> VoiceManager:
    return _voice_manager


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: load config, init httpx pool, init voice manager."""
    global _backends_config, _voice_manager

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    _backends_config = load_backends_config()
    logger.info(
        "Loaded %d backends from %s",
        len(_backends_config.get("backends", {})),
        settings.gateway_config_path,
    )

    _voice_manager = VoiceManager(library_dir=settings.voice_library_dir)
    await client.start()
    logger.info("Synapse Gateway started")

    yield

    await client.stop()
    logger.info("Synapse Gateway stopped")


app = FastAPI(title="Synapse Gateway", version="1.0.0", lifespan=lifespan)


# --- Error handling ---


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    import httpx as _httpx

    if isinstance(exc, _httpx.ConnectError):
        return JSONResponse(status_code=503, content={"error": "Backend unavailable", "detail": str(exc)})
    if isinstance(exc, (_httpx.ReadTimeout, _httpx.WriteTimeout)):
        return JSONResponse(status_code=504, content={"error": "Backend timeout", "detail": str(exc)})
    logger.exception("Unhandled error: %s", exc)
    return JSONResponse(status_code=500, content={"error": "Internal server error"})


# --- Health endpoint ---


@app.get("/health")
async def health():
    """Aggregated health check across all registered backends."""
    config = get_backends_config()
    backends = config.get("backends", {})
    results = {}

    for name, backend in backends.items():
        health_path = backend.get("health", "/health")
        url = f"{backend['url']}{health_path}"
        results[name] = await client.health_check(name, url)

    all_healthy = all(r["status"] == "healthy" for r in results.values())
    return {
        "status": "healthy" if all_healthy else "degraded",
        "backends": results,
    }


# --- Mount routers ---

from .router_llm import router as llm_router  # noqa: E402
from .router_tts import router as tts_router  # noqa: E402
from .router_stt import router as stt_router  # noqa: E402
from .router_speaker import router as speaker_router  # noqa: E402
from .router_audio import router as audio_router  # noqa: E402

app.include_router(llm_router)
app.include_router(tts_router)
app.include_router(stt_router)
app.include_router(speaker_router)
app.include_router(audio_router)
