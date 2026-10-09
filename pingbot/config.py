import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def boolean(value: str) -> bool:
    if value.strip().lower() not in {"true", "false"}:
        raise ValueError("ALL_ALLOW_EVERYONE должен быть True или False")
    return value.strip().lower() == "true"


def parse_admin_ids(value: str) -> frozenset[int]:
    if not value.strip():
        return frozenset()
    parts = [part.strip() for part in value.split(',')]
    if any(not re.fullmatch(r"[0-9]+", part) or not 0 < int(part) < 2**63 for part in parts):
        raise ValueError("ADMIN_IDS: положительные Telegram ID через запятую")
    return frozenset(map(int, parts))


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    bot_token: str
    proxy_host: str
    proxy_port: int
    proxy_secret: str
    all_allow_everyone: bool = True
    cooldown_seconds: int = 30
    data_dir: Path = Path("data")
    admin_ids: frozenset[int] = frozenset()

    @classmethod
    def from_env(cls):
        load_dotenv()
        required = ("API_ID", "API_HASH", "BOT_TOKEN", "TG_WS_PROXY_SECRET")
        missing = [key for key in required if not os.getenv(key, "").strip()]
        if missing:
            raise ValueError("Заполните .env: " + ", ".join(missing))
        api_id = int(os.environ["API_ID"])
        api_hash = os.environ["API_HASH"].strip()
        token = os.environ["BOT_TOKEN"].strip()
        secret = os.environ["TG_WS_PROXY_SECRET"].strip()
        port = int(os.getenv("TG_WS_PROXY_PORT", "1443"))
        cooldown = int(os.getenv("PING_COOLDOWN_SECONDS", "30"))
        if api_id <= 0 or not re.fullmatch(r"[a-fA-F0-9]{32}", api_hash):
            raise ValueError("Проверьте API_ID и API_HASH")
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
            raise ValueError("Некорректный BOT_TOKEN")
        if not re.fullmatch(r"[a-fA-F0-9]{32}", secret):
            raise ValueError("TG_WS_PROXY_SECRET: 32 hex-символа без префикса dd/ee")
        if not 1 <= port <= 65535 or cooldown < 0:
            raise ValueError("Некорректный порт или PING_COOLDOWN_SECONDS")
        return cls(api_id, api_hash, token,
                   os.getenv("TG_WS_PROXY_HOST", "tg-ws-proxy"), port, secret,
                   boolean(os.getenv("ALL_ALLOW_EVERYONE", "True")), cooldown,
                   Path(os.getenv("DATA_DIR", "data")),
                   parse_admin_ids(os.getenv("ADMIN_IDS", "")))
