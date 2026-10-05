"""Откуда берутся нажатия кнопок: событие от приёмника или опрос Telegram.

Приёмник (Cloudflare Worker) ловит нажатие по webhook-у, сразу отвечает
человеку «Принял, запускаю…», будит GitHub Actions через repository_dispatch
и передаёт само событие в client_payload. Тогда getUpdates не нужен — и не
сработал бы: Telegram уже отдал событие webhook-ом.

Если приёмник молчит (ручной запуск, расписание), работает старый путь —
опрос getUpdates, как было до приёмника.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import requests


def from_event() -> list[dict] | None:
    """События от приёмника.

    [] — режим webhook, но событий в этом запуске нет (прогон по расписанию).
    None — приёмник не используется, нужно опрашивать Telegram самим.
    """
    path = os.environ.get("GITHUB_EVENT_PATH")
    if path and Path(path).exists():
        try:
            event = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:
            event = {}
        update = (event.get("client_payload") or {}).get("update")
        if update:
            return [update]
    if os.environ.get("TG_WEBHOOK", "").strip().lower() in ("1", "true", "yes"):
        return []
    return None


def poll(api, offset: int, log) -> tuple[list[dict], bool]:
    """Опрос Telegram. Возвращает (события, удалось ли).

    api — функция, собирающая URL метода Bot API (callbacks._api).
    """
    try:
        info = requests.get(api("getWebhookInfo"), timeout=10).json().get("result", {})
        if info.get("url"):
            log(f"callbacks: на боте стоит webhook ({info['url'][:60]}…) —"
                " события забирает приёмник, getUpdates вернёт пусто")
        if info.get("pending_update_count"):
            log(f"callbacks: в очереди Telegram {info['pending_update_count']}"
                " необработанных событий")
    except Exception:
        pass
    try:
        data = requests.get(api("getUpdates"),
                            params={"offset": offset, "timeout": 0},
                            timeout=25).json()
        if not data.get("ok"):
            log(f"callbacks: Telegram отверг getUpdates — {data.get('description')}")
            return [], False
        return data.get("result", []), True
    except Exception as e:
        log(f"callbacks: getUpdates не сработал — {e}")
        return [], False
