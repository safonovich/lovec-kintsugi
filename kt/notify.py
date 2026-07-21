"""Telegram: карточка лида с готовым КП и кнопками отправки."""

from __future__ import annotations

import hashlib
import os
import re
import time
import urllib.parse

import requests


def _api(method: str) -> str:
    return f"https://api.telegram.org/bot{os.environ['TG_BOT_TOKEN']}/{method}"


def _chat_id() -> str:
    return os.environ["TG_CHAT_ID"]


def short_key(lead_id: str) -> str:
    return hashlib.sha1(lead_id.encode()).hexdigest()[:12]


MENU_KB = {"keyboard": [[{"text": "📇 Прислать карточку"},
                         {"text": "🔄 Обновить базу"}]],
           "resize_keyboard": True, "is_persistent": True}

SEGMENT_ICON = {"event": "🎪", "company": "🏢"}


def send_menu(text: str, log) -> None:
    """Сообщение с постоянной клавиатурой-меню внизу чата."""
    try:
        requests.post(_api("sendMessage"), json={
            "chat_id": _chat_id(), "text": text,
            "reply_markup": MENU_KB}, timeout=20)
    except Exception as e:
        log(f"telegram(menu): {e}")


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send_lead(lead: dict, subject: str, body: str, pending: dict, log,
              portfolio: str = "") -> None:
    sk = short_key(lead["id"])
    contacts = []
    if lead.get("email"):
        contacts.append(f"📧 {lead['email']}")
    if lead.get("phone"):
        contacts.append(f"📞 {lead['phone']}")
    if lead.get("tg"):
        contacts.append(f"✈️ {lead['tg']}")
    body_html = _esc(body[:2800])
    if portfolio and _esc(portfolio) in body_html:   # голый URL → аккуратная гиперссылка
        body_html = body_html.replace(
            _esc(portfolio), f'<a href="{portfolio}">kingtsugi.ru — сайт мастерской</a>')
    icon = SEGMENT_ICON.get(lead.get("segment", "company"), "🏢")
    text = (f"{icon} <b>{_esc(lead['name'])}</b>"
            f" <i>({'event-агентство' if lead.get('segment') == 'event' else 'компания/HR'})</i>\n"
            f"🌍 {lead.get('site', '—')}\n"
            + (" · ".join(contacts) + "\n" if contacts else "⚠️ контакты не найдены\n")
            + f"\n<b>Тема:</b> {_esc(subject)}\n"
            f"<blockquote>{body_html}</blockquote>\n"
            "Кнопка 📧 отправит именно этот текст (в письме ссылка тоже будет кликабельной).")

    row1 = []
    if lead.get("email"):
        row1.append({"text": "📧 Отправить на email", "callback_data": f"s|{sk}"})
    row1.append({"text": "👎 Пропустить", "callback_data": f"x|{sk}"})
    row2 = []
    if lead.get("tg"):     # с сайтов собираются каналы, не личные контакты — не врём
        row2.append({"text": "📣 Канал в TG",
                     "url": f"https://t.me/{lead['tg'].lstrip('@')}"})
    # WA-кнопка только там, где реально есть WhatsApp: wa.me-ссылка с их сайта
    # или мобильный номер. Городские (495 и т.п.) в WA не регистрируют.
    wa_num = lead.get("wa") or (
        lead.get("phone") if str(lead.get("phone", "")).startswith("+79") else None)
    if wa_num:
        wa = re.sub(r"\D", "", wa_num)
        wa_text = urllib.parse.quote(body[:1500])
        row2.append({"text": "💬 WhatsApp (текст подставится)",
                     "url": f"https://wa.me/{wa}?text={wa_text}"})
    kb = {"inline_keyboard": [r for r in (row1, row2) if r]}

    try:
        r = requests.post(_api("sendMessage"), json={
            "chat_id": _chat_id(), "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True, "reply_markup": kb}, timeout=20)
        r.raise_for_status()
        pending[sk] = {"lead_id": lead["id"], "subject": subject,
                       "body": body, "ts": time.time()}
    except Exception as e:
        log(f"telegram: не отправилось — {e}")


def send_reply_card(lead: dict, incoming: str, draft: str, sk: str, log) -> None:
    """Ответ компании + черновик LLM. Отправка — по кнопке."""
    text = (f"📨 <b>{_esc(lead['name'])}</b> ответили:\n"
            f"<blockquote>{_esc(incoming[:800])}</blockquote>\n"
            f"✍️ <b>Черновик ответа:</b>\n"
            f"<blockquote>{_esc(draft[:1800])}</blockquote>")
    kb = {"inline_keyboard": [[
        {"text": "📤 Отправить ответ", "callback_data": f"r|{sk}"},
        {"text": "✏️ Сам отвечу", "callback_data": f"n|{sk}"},
    ]]}
    try:
        r = requests.post(_api("sendMessage"), json={
            "chat_id": _chat_id(), "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True, "reply_markup": kb}, timeout=20)
        r.raise_for_status()
    except Exception as e:
        log(f"telegram(reply): не отправилось — {e}")


def send_service(text: str, log) -> None:
    try:
        requests.post(_api("sendMessage"), json={
            "chat_id": _chat_id(), "text": text,
            "disable_web_page_preview": True}, timeout=20)
    except Exception as e:
        log(f"telegram(service): {e}")
