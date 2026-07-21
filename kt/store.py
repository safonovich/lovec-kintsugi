"""Хранилище состояния в data/ — коммитится обратно в репозиторий после каждого запуска."""

from __future__ import annotations

import json
import time
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"


def load(name: str, default):
    p = DATA / name
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def save(name: str, obj) -> None:
    DATA.mkdir(exist_ok=True)
    (DATA / name).write_text(
        json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def prune_pending(pending: dict, days: int = 30) -> dict:
    cutoff = time.time() - days * 86400
    return {k: v for k, v in pending.items() if v.get("ts", 0) > cutoff}
