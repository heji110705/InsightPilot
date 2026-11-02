"""Runtime configuration.

All settings come from environment variables or a project-root `.env` file.
No third-party settings library is used on purpose: the config surface is small
and an explicit loader is easier to audit than magic field resolution.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)))
    except ValueError:
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    raw = _env(key, "1" if default else "0").lower()
    return raw in {"1", "true", "yes", "on"}


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env(key, str(default)) or default)
    except ValueError:
        return default


@dataclass(slots=True)
class Settings:
    # LLM
    llm_mode: str = "stub"                 # stub | api
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_temperature: float = 0.0
    llm_timeout_s: int = 60
    llm_max_tokens: int = 1200

    # Data
    db_path: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "warehouse.sqlite")
    semantic_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "semantic")

    # Execution guard
    row_limit: int = 2000
    query_timeout_ms: int = 3000
    max_retries: int = 3
    max_critiques: int = 2
    max_steps: int = 24

    # Context budget
    context_token_budget: int = 2600

    # Observability / state
    state_db_path: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "runtime.sqlite")
    trace_enabled: bool = True

    # Cache
    cache_enabled: bool = True

    # 成本折算单价（元 / 百万 token）。默认值按 DeepSeek-chat 公开价填，
    # 只用于横向比较「多跑一个 Agent 贵多少」，不作为账单依据。
    price_in_per_m: float = 1.0
    price_out_per_m: float = 2.0

    def cost_cny(self, tokens_in: int, tokens_out: int) -> float:
        return (
            tokens_in / 1_000_000 * self.price_in_per_m
            + tokens_out / 1_000_000 * self.price_out_per_m
        )

    @property
    def is_stub(self) -> bool:
        return self.llm_mode != "api" or not self.llm_api_key

    @property
    def effective_mode(self) -> str:
        return "stub" if self.is_stub else "api"


def load_settings() -> Settings:
    _load_dotenv(PROJECT_ROOT / ".env")
    data_dir = PROJECT_ROOT / "data"
    return Settings(
        llm_mode=_env("IP_LLM_MODE", "stub").lower(),
        llm_base_url=_env("IP_LLM_BASE_URL", "https://api.deepseek.com/v1"),
        llm_api_key=_env("IP_LLM_API_KEY", ""),
        llm_model=_env("IP_LLM_MODEL", "deepseek-chat"),
        llm_temperature=float(_env("IP_LLM_TEMPERATURE", "0") or 0),
        llm_timeout_s=_env_int("IP_LLM_TIMEOUT_S", 60),
        llm_max_tokens=_env_int("IP_LLM_MAX_TOKENS", 1200),
        db_path=Path(_env("IP_DB_PATH", str(data_dir / "warehouse.sqlite"))),
        semantic_dir=Path(_env("IP_SEMANTIC_DIR", str(data_dir / "semantic"))),
        row_limit=_env_int("IP_ROW_LIMIT", 2000),
        query_timeout_ms=_env_int("IP_QUERY_TIMEOUT_MS", 3000),
        max_retries=_env_int("IP_MAX_RETRIES", 3),
        max_critiques=_env_int("IP_MAX_CRITIQUES", 2),
        max_steps=_env_int("IP_MAX_STEPS", 24),
        context_token_budget=_env_int("IP_CONTEXT_TOKEN_BUDGET", 2600),
        state_db_path=Path(_env("IP_STATE_DB", str(data_dir / "runtime.sqlite"))),
        trace_enabled=_env_bool("IP_TRACE", True),
        cache_enabled=_env_bool("IP_CACHE", True),
        price_in_per_m=_env_float("IP_PRICE_IN_PER_M", 1.0),
        price_out_per_m=_env_float("IP_PRICE_OUT_PER_M", 2.0),
    )


SETTINGS = load_settings()
