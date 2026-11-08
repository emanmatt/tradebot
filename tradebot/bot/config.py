import os
from pathlib import Path

import yaml

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

ROOT = Path(__file__).resolve().parent.parent


def load_config(path: str | None = None) -> dict:
    p = Path(path) if path else ROOT / "config.yaml"
    with open(p) as f:
        cfg = yaml.safe_load(f)
    cfg["journal_path"] = str(ROOT / cfg.get("journal_path", "data/journal.sqlite"))
    cfg["cache_dir"] = str(ROOT / cfg.get("cache_dir", "data/cache"))
    Path(cfg["cache_dir"]).mkdir(parents=True, exist_ok=True)
    Path(cfg["journal_path"]).parent.mkdir(parents=True, exist_ok=True)
    return cfg


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def is_live(cfg: dict) -> bool:
    """Live trading only when BOTH the config and the env guard say so."""
    return cfg.get("mode") == "live" and env("CONFIRM_LIVE").upper() == "YES"
