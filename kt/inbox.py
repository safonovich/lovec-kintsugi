"""Входящие: проверяем ящик по IMAP, находим ответы компаний из базы,
LLM готовит черновик ответа — отправка ТОЛЬКО по кнопке в Telegram.
Сложные случаи (договор, юр. вопросы, негатив) — handoff: бот зовёт человека."""

from __future__ import annotations

import email
import email.header
import email.utils
import imaplib
import json
import os
import re
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
узнать дату/размер группы или назначить созвон. КП в PDF уже было
приложено к первому письму — если просят его снова, напомни, что оно
во вложении первого письма, и предложи ответить на вопросы напрямую.

Жёсткие правила:
- формат и цены ТОЛЬКО отсюда, новые не выдумывай:
{prices}
- скидки не предлагай, конкретные даты не подтверждай
  («согласуем дату под ваше событие»)
- кейсы, клиентов и цифры не выдумывай
- полезные факты (если уместно): опыт не нужен — получается у всех;
  выезд в офис/лофт, нужны только столы и стулья; размер группы
  не ограничен; есть запасные предметы — испортить невозможно
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
    own = os.environ.get("SMTP_USER", "").lower().strip()
    if own and addr == own:      # наш собственный ящик — это не лид
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


_ADDR_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _bounce_addresses(msg) -> list[str]:
    """Если это ОКОНЧАТЕЛЬНЫЙ отказ — адреса, которые не приняли письмо.

    Почтовик шлёт два похожих отчёта. «Delay» (Action: delayed, код 4.x.x) —
    письмо ещё в пути, Gmail будет пробовать двое суток; такой лид трогать
    нельзя. «Failure» (Action: failed, код 5.x.x) — адреса нет, это конец.
    Считаем мёртвыми только вторые.
    """
    ctype = (msg.get("Content-Type") or "").lower()
    sender = (email.utils.parseaddr(msg.get("From", ""))[1] or "").lower()
    if not ("delivery-status" in ctype
            or sender.startswith(("mailer-daemon@", "postmaster@"))):
        return []
    out: list[str] = []
    seen_report = False
    try:
        for part in msg.walk():
            if part.get_content_type() != "message/delivery-status":
                continue
            for block in part.get_payload():
                raw = (block.get("Final-Recipient")
                       or block.get("Original-Recipient"))
                if not raw or ";" not in str(raw):
                    continue
                seen_report = True
                action = str(block.get("Action") or "").strip().lower()
                status = str(block.get("Status") or "").strip()
                if action == "failed" or status.startswith("5."):
                    out.append(str(raw).split(";", 1)[1].strip().strip("<>").lower())
    except Exception:
        pass
    if seen_report:
        return out        # отчёт разобрали: пусто — значит задержка, не отказ
    subj = (msg.get("Subject") or "").lower()
    if "delay" in subj or "задерж" in subj:
        return []
    return [a.lower() for a in _ADDR_RE.findall(_body_text(msg))]


def _mark_bounced(addrs: list[str], leads: list[dict], log) -> bool:
    """Пометить лид мёртвым, чтобы больше на этот адрес не писать."""
    own = os.environ.get("SMTP_USER", "").lower().strip()
    for a in addrs:
        if not a or a == own:
            continue
        for lead in leads:
            if (lead.get("email") or "").lower() != a:
                continue
            if lead.get("status") != "bounced":
                lead["status"] = "bounced"
                log(f"inbox: отбой — {lead.get('name')} ({a}), больше не пишем")
                notify.send_service(
                    f"✉️ Адрес не принимает письма: {lead.get('name')} ({a}). "
                    f"Пометил мёртвым, дожимать не будем.", log)
            return True
    return False

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
        # Письмо любого из наших ботов (у каждого своя метка X-Lovec-*):
        # это не ответ компании, а наше же письмо, попавшее себе во входящие.
        if any(h.lower().startswith("x-lovec-") for h in msg.keys()):
            continue
        # Отчёт о недоставке: адрес мёртв. Помечаем лид и идём дальше —
        # это не ответ компании, отвечать тут некому.
        bounced = _bounce_addresses(msg)
        if bounced:
            if not _mark_bounced(bounced, leads, log):
                log(f"inbox: отбой с {bounced[0]} — в базе такого адреса нет")
            continue
        from_addr = email.utils.parseaddr(msg.get("From", ""))[1]
        lead = _match(from_addr, leads)
        if not lead:
            continue

        incoming = _strip_quotes(_body_text(msg))
        try:    # =?utf-8?B?…?= → человеческий текст (складные темы роняли отправку)
            subj = str(email.header.make_header(
                email.header.decode_header(msg.get("Subject", ""))))
        except Exception:
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
