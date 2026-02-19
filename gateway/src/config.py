import os
from pathlib import Path

import yaml
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    gateway_config_path: str = "/config/backends.yaml"
    voice_library_dir: str = "/data/voices"
    model_profiles_path: str = "/data/voices/model-profiles.json"
    llama_router_deployment_namespace: str = "llm-infra"
    llama_router_deployment_name: str = "llama-router"
    llama_router_container_name: str = "llama-server"
    runtime_reconfigure_timeout_seconds: float = 300.0
    log_level: str = "INFO"
    terminal_feed_mode: str = "mock"
    terminal_feed_buffer_size: int = 500
    terminal_feed_subscriber_queue_size: int = 200
    terminal_feed_backlog_lines: int = 80
    terminal_feed_keepalive_seconds: float = 15.0
    terminal_feed_max_line_chars: int = 1200
    terminal_feed_default_level: str = "INFO"
    terminal_feed_redact_extra_patterns: str = ""
    terminal_feed_bus_mode: str = "local"
    terminal_feed_redis_url: str = ""
    terminal_feed_redis_channel: str = "synapse:terminal_feed"
    terminal_feed_redis_connect_timeout_seconds: float = 5.0
    instance_id: str = os.getenv("HOSTNAME", "synapse-gateway")

    model_config = {"env_prefix": "SYNAPSE_"}


settings = Settings()


def load_backends_config() -> dict:
    """Load backend registry from YAML config."""
    config_path = Path(settings.gateway_config_path)
    if not config_path.exists():
        raise FileNotFoundError(f"Backend config not found: {config_path}")
    with open(config_path) as f:
        return yaml.safe_load(f)


def get_backend_url(config: dict, name: str) -> str:
    """Get the base URL for a named backend."""
    backend = config.get("backends", {}).get(name)
    if not backend:
        raise KeyError(f"Backend not found in config: {name}")
    return backend["url"]
