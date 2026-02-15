import os
from pathlib import Path

import yaml
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    gateway_config_path: str = "/config/backends.yaml"
    voice_library_dir: str = "/data/voices"
    log_level: str = "INFO"

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
