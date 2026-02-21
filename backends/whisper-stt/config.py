from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Whisper STT backend configuration."""

    model_size: str = "large-v3-turbo"
    device: str = "cpu"
    compute_type: str = "int8"
    model_cache_dir: str = "/cache/huggingface"

    host: str = "0.0.0.0"
    port: int = 8000

    model_config = {"env_prefix": "WHISPER_"}


settings = Settings()
