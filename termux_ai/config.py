"""Configuration loading. Stored as TOML under ~/.config/termux-ai/config.toml."""
from __future__ import annotations

import json
import os
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path


def _home() -> Path:
    return Path(os.environ.get("HOME", str(Path.home())))


CONFIG_DIR = Path(os.environ.get("TERMUX_AI_HOME", _home() / ".config" / "termux-ai"))
DATA_DIR = Path(os.environ.get("TERMUX_AI_DATA", _home() / ".local" / "share" / "termux-ai"))
CONFIG_PATH = CONFIG_DIR / "config.toml"

DEFAULT_FEEDS = {
    "tech": [
        "https://hnrss.org/frontpage",
        "https://feeds.arstechnica.com/arstechnica/technology-lab",
        "https://www.theverge.com/rss/index.xml",
    ],
    "ai": [
        "https://huggingface.co/blog/feed.xml",
        "https://www.technologyreview.com/topic/artificial-intelligence/feed",
        "https://export.arxiv.org/rss/cs.LG",
    ],
    "general": [
        "https://feeds.bbci.co.uk/news/rss.xml",
        "https://www.thehindu.com/news/national/feeder/default.rss",
    ],
}


@dataclass
class Config:
    ollama_host: str = "http://127.0.0.1:11434"
    model: str = ""  # empty = auto-detect first installed model
    temperature: float = 0.4
    num_ctx: int = 4096
    request_timeout: float = 300.0
    command_timeout: int = 60
    max_output_chars: int = 6000
    history_turns: int = 12
    refresh_seconds: float = 3.0
    workspace: str = str(_home() / "projects")
    search_provider: str = "duckduckgo"
    feeds: dict[str, list[str]] = field(default_factory=lambda: dict(DEFAULT_FEEDS))

    @property
    def db_path(self) -> Path:
        return DATA_DIR / "memory.db"

    @property
    def command_log(self) -> Path:
        return DATA_DIR / "commands.log"


def _toml_dump(cfg: Config) -> str:
    d = asdict(cfg)
    feeds = d.pop("feeds")
    lines = []
    for k, v in d.items():
        lines.append(f"{k} = {json.dumps(v)}")
    lines.append("")
    lines.append("[feeds]")
    for k, urls in feeds.items():
        lines.append(f"{k} = {json.dumps(urls)}")
    return "\n".join(lines) + "\n"


def load_config() -> Config:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cfg = Config()
    if CONFIG_PATH.exists():
        try:
            data = tomllib.loads(CONFIG_PATH.read_text())
        except (tomllib.TOMLDecodeError, OSError):
            data = {}
        for k, v in data.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
    else:
        save_config(cfg)
    if os.environ.get("OLLAMA_HOST"):
        host = os.environ["OLLAMA_HOST"]
        cfg.ollama_host = host if host.startswith("http") else f"http://{host}"
    return cfg


def save_config(cfg: Config) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(_toml_dump(cfg))
