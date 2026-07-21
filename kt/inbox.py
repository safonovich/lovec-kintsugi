"""Входящие: проверяем ящик по IMAP, находим ответы компаний из базы,
LLM готовит черновик ответа — отправка ТОЛЬКО по кнопке в Telegram.
Сложные случаи (договор, юр. вопросы, негатив) — handoff: бот зовёт человека."""

from __future__ import annotations

import email
import email.utils
import imaplib
import json
import os
import time

from kt import llm, notify

PUBLIC_DOMAINS = {"mail.ru", "gmail.com", "yandex.ru", "ya.ru", "bk.ru",
                  "inbox.ru", "list.ru", "rambler.ru", "icloud.com",
                  "outlook.com", "hotmail.com"}

SYSTEM = """Ты — ассистент {author}, мастера кинцуги (бренд {brand}, Москва),
который разослал компаниям и event-агентствам предложение о корпоративных
мастер-классах кинцуги. Адресат ответил на письмо. Пиши от первого лица
(«я»), тон тёплый, спокойный и профессиональный — как пишет сам мастер.

Напиши короткий деловой ответ (60–120 слов). Цель — довести до конкретики:
узнать дату/размер группы, назначить созвон или отправить презентацию
(если просят презентацию — напиши, что высылаешь её следующим письмом,
и задай 1 уточняющий вопрос про группу/дату).

Жёсткие правила:
- формат и цены ТОЛЬКО отсюда, новые не выдумывай:
{prices}
- скидки не предлагай, конкретные даты не подтверждай
  («согласуем дату под ваше событие»)
- кейсы, клиентов и цифры не выдумывай
- полезные факты (если уместно): опыт не нужен — получается у всех;
  выезд в офис/лофт, нужны только столы и стулья; группы больше 20
  разбиваем на потоки; есть запасные предметы — испортить невозможно
- event-агентствам: комиссия и условия партнёрства — «обсудим напрямую,
  под ваш запрос»
- явный отказ — поблагодари одной фразой и не дави
- не выдумывай факты, которых нет в переписке
- подпись: {author}, {portfolio}, {phone}

Если в письме: вопросы по договору/юридические, претензия, негатив, просьба
о нестандартных условиях — НЕ отвечай сам, верни handoff.

Ответь СТРОГО одним JSON-объектом:
{{"handoff": false, "body": "текст ответа"}}
или
{{"handoff": true, "reason": "почему нужен человек, до 15 слов"}}"""


def _body_text(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    return part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", "replace")
                except Exception:
                    continue
        return ""
    try:
        return msg.get_payload(decode=True).decode(
            msg.get_content_charset() or "utf-8", "replace")
    except Exception:
        return ""


def _strip_quotes(text: str) -> str:
    lines = []
    for ln in text.splitlines():
        if ln.strip().startswith(">"):
            continue
        lines.append(ln)
    return "\n".join(lines).strip()[:1500]


def _match(addr: str, leads: list[dict]):
    addr = addr.lower().strip()
    if not addr or "@" not in addr:
        return None
    dom = addr.split("@")[-1]
    active = [a for a in leads
              if a.get("email") and a.get("status") in ("sent", "replied")]
    for a in active:
        if a["email"].lower() == addr:
            return a
    if dom in PUBLIC_DOMAINS:
        return None
    for a in active:
        if a["email"].lower().split("@")[-1] == dom:
            return a
    return None


def _draft(lead: dict, incoming: str, cfg: dict, log):
    """Возвращает {"handoff":..., "body"/"reason":...} или None (нет ключа/ошибка)."""
    kp_cfg = cfg["kp"]
    system = SYSTEM.format(author=kp_cfg["author_name"],
                           brand=kp_cfg.get("brand", "KingTsugi"),
                           phone=kp_cfg["author_phone"],
                           portfolio=kp_cfg["portfolio_url"],
                           prices=kp_cfg.get("prices", ""))
    user = (f"Адресат: {lead['name']} (segment: {lead.get('segment', 'company')})\n"
            f"Их письмо:\n{incoming}")
    txt = llm.chat(system, user, cfg, log, max_tokens=600)
    if not txt:
        return None
    try:
        return json.loads(txt[txt.find("{"):txt.rfind("}") + 1])
    except Exception as e:
        log(f"inbox: не разобрал ответ LLM ({e})")
        return None


def check(leads: list[dict], pending: dict, last_uid: int, cfg: dict, log) -> int:
    """Проверяет ящик, возвращает новый last_uid. leads/pending правятся на месте."""
    user = os.environ.get("SMTP_USER", "").strip()
    pwd = os.environ.get("SMTP_PASS", "").strip()
    host = cfg["email"].get("imap_host", "").strip()
    if not (user and pwd and host):
        return last_uid

    M = None
    try:
        M = imaplib.IMAP4_SSL(host, timeout=30)
        M.login(user, pwd)
        M.select("INBOX")
        _, data = M.uid("search", None, f"UID {last_uid + 1}:*")
        uids = [int(u) for u in (data[0] or b"").split() if int(u) > last_uid]
    except Exception as e:
        log(f"inbox: IMAP не сработал — {e}")
        if M is not None:
            try:
                M.logout()
            except Exception:
                pass
        return last_uid

    new_last = last_uid
    for uid in uids:
        new_last = max(new_last, uid)
        try:
            _, md = M.uid("fetch", str(uid), "(BODY.PEEK[])")
            msg = email.message_from_bytes(md[0][1])
        except Exception:
            continue
        if msg.get("X-Lovec-Kintsugi") or msg.get("X-Lovec-FPV"):
            continue                     # письма ботов (свои и соседа) — пропускаем
        from_addr = email.utils.parseaddr(msg.get("From", ""))[1]
        lead = _match(from_addr, leads)
        if not lead:
            continue

        incoming = _strip_quotes(_body_text(msg))
        subj = msg.get("Subject", "")
        lead["status"] = "replied"
        log(f"inbox: ответ от {lead['name']} ({from_addr})")

        draft = _draft(lead, incoming, cfg, log)
        sk = notify.short_key(f"reply:{uid}:{lead['id']}")
        if draft and not draft.get("handoff") and draft.get("body"):
            pending[sk] = {"kind": "reply", "lead_id": lead["id"],
                           "to": from_addr,
                           "subject": ("Re: " + subj.replace("Re: ", "").strip())[:150],
                           "body": str(draft["body"]),
                           "msgid": msg.get("Message-ID"), "ts": time.time()}
            notify.send_reply_card(lead, incoming, str(draft["body"]), sk, log)
        else:
            reason = (draft or {}).get("reason", "LLM недоступен")
            notify.send_service(
                f"📨 Ответ от {lead['name']} ({from_addr}) — нужен твой ответ "
                f"({reason}).\n\n«{incoming[:600]}»", log)

    try:
        M.logout()
    except Exception:
        pass
    return new_last
