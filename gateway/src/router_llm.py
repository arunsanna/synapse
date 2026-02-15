"""LLM proxy routes — /v1/chat/completions, /v1/embeddings, /v1/models."""

import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse, JSONResponse

from .backend_client import client
from .config import get_backend_url

router = APIRouter(tags=["llm"])
logger = logging.getLogger(__name__)


def _get_config():
    from .main import get_backends_config
    return get_backends_config()


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """Proxy chat completions to Ollama (or vLLM when deployed)."""
    config = _get_config()
    backend_url = get_backend_url(config, "ollama")
    body = await request.body()

    # Detect streaming
    try:
        payload = json.loads(body)
        is_stream = payload.get("stream", False)
    except (json.JSONDecodeError, UnicodeDecodeError):
        is_stream = False

    if is_stream:
        # Ollama native chat endpoint for streaming
        url = f"{backend_url}/api/chat"
        return StreamingResponse(
            client.stream_bytes(
                "ollama", "POST", url,
                content=body,
                headers={"Content-Type": "application/json"},
                timeout_type="llm",
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    # Non-streaming: use OpenAI-compatible endpoint
    url = f"{backend_url}/v1/chat/completions"
    resp = await client.request(
        "ollama", "POST", url,
        content=body,
        headers={"Content-Type": "application/json"},
        timeout_type="llm",
    )
    return JSONResponse(content=resp.json(), status_code=resp.status_code)


@router.post("/v1/embeddings")
async def embeddings(request: Request):
    """Proxy embeddings to llama-embed."""
    config = _get_config()
    backend_url = get_backend_url(config, "llama-embed")
    body = await request.body()

    url = f"{backend_url}/v1/embeddings"
    resp = await client.request(
        "llama-embed", "POST", url,
        content=body,
        headers={"Content-Type": "application/json"},
        timeout_type="embeddings",
    )
    return JSONResponse(content=resp.json(), status_code=resp.status_code)


@router.get("/v1/models")
async def list_models():
    """Aggregate model lists from all LLM backends."""
    config = _get_config()
    models = []

    # Ollama models
    try:
        ollama_url = get_backend_url(config, "ollama")
        resp = await client.request(
            "ollama", "GET", f"{ollama_url}/api/tags",
            timeout_type="default",
        )
        if resp.status_code == 200:
            data = resp.json()
            for m in data.get("models", []):
                models.append({
                    "id": m.get("name", "unknown"),
                    "object": "model",
                    "owned_by": "ollama",
                })
    except Exception as e:
        logger.warning("Failed to list Ollama models: %s", e)

    # llama-embed models
    try:
        embed_url = get_backend_url(config, "llama-embed")
        resp = await client.request(
            "llama-embed", "GET", f"{embed_url}/v1/models",
            timeout_type="default",
        )
        if resp.status_code == 200:
            data = resp.json()
            for m in data.get("data", []):
                models.append(m)
    except Exception as e:
        logger.warning("Failed to list llama-embed models: %s", e)

    # vLLM models (when deployed — currently commented out in backends.yaml)
    try:
        vllm_url = get_backend_url(config, "vllm")
        resp = await client.request(
            "vllm", "GET", f"{vllm_url}/v1/models",
            timeout_type="default",
        )
        if resp.status_code == 200:
            data = resp.json()
            for m in data.get("data", []):
                models.append(m)
    except KeyError:
        pass  # vLLM not configured yet
    except Exception as e:
        logger.warning("Failed to list vLLM models: %s", e)

    return {"object": "list", "data": models}
