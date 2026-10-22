import os
from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeConfig:
    log_level: str
    cloud_max_mb: float
    tts_insecure_ssl: bool
    projects_root_override: str
    allow_vip: bool


def load_config() -> RuntimeConfig:
    return RuntimeConfig(
        log_level=os.getenv("JY_LOG_LEVEL", "INFO"),
        cloud_max_mb=float(os.getenv("JY_CLOUD_MAX_MB", "512")),
        tts_insecure_ssl=os.getenv("JY_TTS_INSECURE_SSL", "0") == "1",
        projects_root_override=os.getenv("JY_PROJECTS_ROOT", "").strip(),
        allow_vip=os.getenv("JY_ALLOW_VIP", "1") != "0",
    )


CONFIG = load_config()
