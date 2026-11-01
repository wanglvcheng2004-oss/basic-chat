from __future__ import annotations

import os
import hashlib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT / ".env"


def load_env_file(path: Path = ENV_FILE) -> None:
    """读取极简 .env（KEY=VALUE）；已存在的环境变量优先。"""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _thinking(name: str, default: str) -> str:
    value = (os.getenv(name, "").strip().lower() or default)
    return value if value in {"enabled", "disabled"} else default


@dataclass(frozen=True, slots=True)
class AgentConfig:
    name: str = "小深"
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    router_model: str = "deepseek-flash"
    worker_model: str = "deepseek-flash"
    worker_model_heavy: str = "deepseek-v4-pro"
    router_thinking: str = "disabled"
    worker_thinking: str = "enabled"
    reasoning_effort: str = "high"
    temperature: float = 0.3
    router_max_tokens: int = 400
    worker_max_tokens: int = 1600
    request_timeout: float = 180.0
    max_workers: int = 3
    max_steps: int = 8
    batch_size: int = 6
    history_limit: int = 16
    spill_chars: int = 300
    rate_limit: int = 12
    rate_window: float = 60.0
    allow_write: bool = False
    dsh_enabled: bool = False
    dsh_command: str = "dsh"
    dsh_profile: str = "headless"
    dsh_provider: str = "deepseek-official"
    dsh_base_url: str = ""
    dsh_extra_args: str = ""
    dsh_timeout: float = 1800.0
    dsh_home: str = ""
    dsh_policy_verified: bool = False
    data_dir: str = ''
    task_timeout: float = 600.0
    heartbeat_interval: float = 10.0
    stall_seconds: float = 180.0

    @classmethod
    def from_env(cls) -> "AgentConfig":
        load_env_file()
        return cls(
            name=(os.getenv("AGENT_NAME", "小深").strip() or "小深"),
            api_key=os.getenv("LLM_API_KEY", "").strip(),
            base_url=(os.getenv("LLM_BASE_URL", "https://api.deepseek.com").strip() or "https://api.deepseek.com"),
            router_model=os.getenv("ROUTER_MODEL", "deepseek-flash").strip() or "deepseek-flash",
            worker_model=os.getenv("WORKER_MODEL", "deepseek-flash").strip() or "deepseek-flash",
            worker_model_heavy=os.getenv("WORKER_MODEL_HEAVY", "deepseek-v4-pro").strip() or "deepseek-v4-pro",
            router_thinking=_thinking("ROUTER_THINKING", "disabled"),
            worker_thinking=_thinking("WORKER_THINKING", "enabled"),
            reasoning_effort=os.getenv("REASONING_EFFORT", "high").strip() or "high",
            temperature=_float("LLM_TEMPERATURE", 0.3),
            router_max_tokens=_int("ROUTER_MAX_TOKENS", 400),
            worker_max_tokens=_int("WORKER_MAX_TOKENS", 1600),
            request_timeout=_float("LLM_TIMEOUT", 180.0),
            max_workers=max(2, _int("MAX_WORKERS", 3)),
            max_steps=max(1, _int("MAX_STEPS", 8)),
            batch_size=max(1, _int("ROUTER_BATCH", 6)),
            history_limit=max(4, _int("HISTORY_LIMIT", 16)),
            spill_chars=max(80, _int("SPILL_CHARS", 300)),
            rate_limit=max(1, _int("RATE_LIMIT", 12)),
            rate_window=max(1.0, _float("RATE_WINDOW", 60.0)),
            allow_write=_bool("AGENT_ALLOW_WRITE", False),
            dsh_enabled=_bool("HARNESS_ENABLED", False),
            dsh_command=os.getenv("HARNESS_COMMAND", "dsh").strip() or "dsh",
            dsh_profile=os.getenv("HARNESS_PROFILE", "headless").strip() or "headless",
            dsh_provider=os.getenv("HARNESS_PROVIDER", "deepseek-official").strip() or "deepseek-official",
            dsh_base_url=os.getenv("HARNESS_BASE_URL", "").strip(),
            dsh_extra_args=os.getenv("HARNESS_EXTRA_ARGS", "").strip(),
            dsh_timeout=max(30.0, _float("HARNESS_TIMEOUT", 1800.0)),
            dsh_home=os.getenv("HARNESS_HOME", "").strip(),
            dsh_policy_verified=_bool('HARNESS_POLICY_VERIFIED', False),
            data_dir=os.getenv('CHAT_DATA_DIR', '').strip(),
            task_timeout=max(30., _float('TASK_TIMEOUT', 600.)),
            heartbeat_interval=max(1., _float('HEARTBEAT_INTERVAL', 10.)),
            stall_seconds=max(10., _float('STALL_SECONDS', 180.)),
        )

    @property
    def demo_mode(self) -> bool:
        return not self.api_key

    @property
    def state_path(self) -> Path:
        if self.data_dir:
            return Path(self.data_dir).expanduser().resolve()
        base = Path(os.getenv('LOCALAPPDATA') or (Path.home() / '.local' / 'share'))
        workspace_key = hashlib.sha256(str(ROOT).encode()).hexdigest()[:12]
        return base / 'basic-chat' / workspace_key
