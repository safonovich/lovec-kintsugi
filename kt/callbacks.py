"""Кнопки: разбираем нажатия и выполняем действия.
s|<sk> — отправить КП на email, x|<sk> — пропустить.
Плюс кнопки-меню: «📇 Прислать карточку» и «🔄 Обновить базу».
Нажатие приходит либо от приёмника (webhook), либо опросом getUpdates — см. poll.py."""

from __future__ import annotations

import os
import time

import requests

from kt import mailer, notify, poll


def _api(method: str) -> str:
    return f"https://api.telegram.org/bot{os.environ['TG_BOT_TOKEN']}/{method}"


def _answer(cq_id: str, text: str) -> None:
    try:
        requests.post(_api("answerCallbackQuery"),
                      json={"callback_query_id": cq_id, "text": text}, timeout=10)
    except Exception:
        pass


def _mark(cq: dict, label: str) -> None:
    try:
        msg = cq.get("message") or {}
        requests.post(_api("editMessageReplyMarkup"), json={
            "chat_id": msg.get("chat", {}).get("id"),
            "message_id": msg.get("message_id"),
            "reply_markup": {"inline_keyboard":
                             [[{"text": label, "callback_data": "noop|x"}]]},
        }, timeout=10)
    except Exception:
        pass


def process(pending: dict, leads: list[dict], offset: int, cfg: dict, log,
            injected: list[dict] | None = None):
    """Возвращает (новый offset, команды меню). pending и leads правятся на месте."""
    cmds: dict = {}
    if injected is None:
        updates, ok = poll.poll(_api, offset, log)
        if not ok:
            return offset, cmds
    else:
        # Событие принёс приёмник: Telegram уже отдал его webhook-ом,
        # getUpdates на него больше не ответит.
        updates = list(injected)
    if updates:
        log(f"callbacks: получено событий: {len(updates)}")
    # Лид без id — свежий, из Replenish: fix_ids проставит его в ближайший час.
    # Нажать на него всё равно нельзя, но и ронять из-за него весь прогон незачем.
    by_id = {a["id"]: a for a in leads if a.get("id")}
    new_offset = offset
    for u in updates:
        new_offset = max(new_offset, u["update_id"] + 1)
        msg = u.get("message")
        if msg:                                     # текстовые команды/кнопки меню
            if str(msg.get("chat", {}).get("id", "")) != os.environ.get("TG_CHAT_ID", ""):
                continue
            t = (msg.get("text") or "").strip().lower()
            if "карточк" in t or t.startswith("/card"):
                cmds["card"] = cmds.get("card", 0) + 1
                notify.send_menu("📇 Принято — готовлю карточку…", log)
                log("menu: запрошена карточка")
            elif "обновить базу" in t or t.startswith("/discover"):
                cmds["discover"] = True
                notify.send_menu("🔄 Принято — ищу новые event-агентства в OSM…", log)
                log("menu: запрошено обновление базы")
            elif t.startswith("/start") or t.startswith("/menu"):
                notify.send_menu("KingTsugi-ловец на связи. Кнопки меню снизу 👇", log)
            continue
        cq = u.get("callback_query")
        if not cq or "|" not in cq.get("data", ""):
            continue
        action, sk = cq["data"].split("|", 1)
        info = pending.get(sk)
        if action == "noop" or not info:
            if action != "noop":
                log(f"callbacks: нажатие по устаревшей карточке ({action}|{sk})")
            _answer(cq["id"], "Карточка устарела" if action != "noop" else "")
            continue
        lead = by_id.get(info["lead_id"])
        if not lead:
            _answer(cq["id"], "Лид не найден в базе")
            continue

        if action == "r":               # отправить ответ (переговоры)
            if info.get("kind") != "reply":
                _answer(cq["id"], "Карточка устарела")
                continue
            ok = mailer.send(info["to"], info["subject"], info["body"], cfg, log,
                             in_reply_to=info.get("msgid"))
            if ok:
                _answer(cq["id"], "Ответ улетел 📤")
                _mark(cq, f"✅ ответ отправлен → {info['to']}")
                notify.send_service(f"✉️ Ответ отправлен → {info['to']}", log)
                pending.pop(sk, None)
            else:
                _answer(cq["id"], "Ошибка отправки — смотри логи Actions")
        elif action == "n":             # человек ответит сам
            _answer(cq["id"], "Ок, отвечаешь сам")
            _mark(cq, "✍️ отвечаешь сам")
            pending.pop(sk, None)
        elif action == "x":
            if lead.get("status") == "sent":
                _answer(cq["id"], "КП уже отправлено — статус не трогаю")
                log(f"skip проигнорирован (уже sent): {lead['name']}")
                continue
            lead["status"] = "skipped"
            _answer(cq["id"], "Пропустили 👌")
            _mark(cq, "🚫 пропущено")
            log(f"skip: {lead['name']}")
        elif action == "s":
            if lead.get("status") == "sent":
                _answer(cq["id"], "Уже отправляли этому адресату")
                continue
            ok = mailer.send(lead["email"], info["subject"], info["body"], cfg, log,
                             attach_kp=True)
            if ok:
                lead["status"] = "sent"
                lead["sent_ts"] = time.time()
                _answer(cq["id"], "КП улетело 📧")
                _mark(cq, f"✅ отправлено → {lead['email']}")
                notify.send_service(
                    f"✉️ Отправлено: КП для «{lead['name']}» → {lead['email']}\n"
                    f"Копия — в «Отправленных» твоей почты.", log)
            else:
                _answer(cq["id"], "Ошибка отправки — смотри логи Actions")
                notify.send_service(
                    f"⚠️ НЕ отправлено: «{lead['name']}» — ошибка почты, "
                    f"детали в логах Actions.", log)
    return new_offset, cmds
