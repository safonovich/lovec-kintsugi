"""Кнопки: забираем нажатия через getUpdates и выполняем действия.
s|<sk> — отправить КП на email, x|<sk> — пропустить.
Плюс кнопки-меню: «📇 Прислать карточку» и «🔄 Обновить базу».
У бота НЕ должен стоять webhook (боты из BotFather по умолчанию без него)."""

from __future__ import annotations

import os
import time

import requests

from kt import mailer, notify


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


def process(pending: dict, leads: list[dict], offset: int, cfg: dict, log):
    """Возвращает (новый offset, команды меню). pending и leads правятся на месте."""
    cmds: dict = {}
    # Диагностика/самолечение: webhook блокирует getUpdates — снимаем его
    try:
        info = requests.get(_api("getWebhookInfo"), timeout=10).json().get("result", {})
        if info.get("url"):
            log(f"callbacks: ⚠️ на боте стоял webhook ({info['url'][:60]}…) — снимаю")
            requests.get(_api("deleteWebhook"), timeout=10)
        if info.get("pending_update_count"):
            log(f"callbacks: в очереди Telegram {info['pending_update_count']} необработанных событий")
        # Полная диагностика: видно, не забирает ли события кто-то другой
        log(f"callbacks: webhook_info = {info}")
    except Exception:
        pass
    try:
        r = requests.get(_api("getUpdates"),
                         params={"offset": offset, "timeout": 0}, timeout=25)
        data = r.json()
        if not data.get("ok"):
            log(f"callbacks: Telegram отверг getUpdates — {data.get('description')}"
                " (если тут ошибка 409/webhook — у бота настроен webhook,"
                " нужен отдельный бот без webhook)")
            return offset, cmds
        updates = data.get("result", [])
    except Exception as e:
        log(f"callbacks: getUpdates не сработал — {e}")
        return offset, cmds

    if updates:
        log(f"callbacks: получено событий: {len(updates)}")
    else:
        log(f"callbacks: очередь пуста (offset={offset}) — если кнопки жали "
            "только что, события забирает другой процесс с этим же токеном")
    by_id = {a["id"]: a for a in leads}
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
