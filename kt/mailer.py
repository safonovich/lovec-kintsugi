"""Отправка КП по email через SMTP (app password в секретах).
Письмо уходит в двух версиях: plain text + HTML, где ссылка на портфолио
завуалирована в аккуратный анкор вместо голого URL."""

from __future__ import annotations

import html as _html
import os
import re
import smtplib
from email.message import EmailMessage
from email.utils import formataddr


def _to_html(body: str, cfg: dict) -> str:
    esc = _html.escape(body)
    kp = cfg.get("kp", {})
    purl, brand = kp.get("portfolio_url", ""), kp.get("brand", "KingTsugi")
    if purl and purl in esc:
        esc = esc.replace(
            purl, f'<a href="{purl}" style="color:#8a6d3b;text-decoration:underline">'
                  f'{brand} — фото работ и детали формата</a>')
    esc = re.sub(r'(?<!")(https?://[^\s<]+)', r'<a href="\1">\1</a>', esc)
    return ('<div style="font-family:Georgia,serif;font-size:15px;line-height:1.65;'
            f'color:#1a1a1a;white-space:pre-wrap;max-width:640px">{esc}</div>')


def send(to: str, subject: str, body: str, cfg: dict, log,
         in_reply_to: str | None = None) -> bool:
    e = cfg["email"]
    user = os.environ.get("SMTP_USER", "").strip()
    pwd = os.environ.get("SMTP_PASS", "").strip()
    if not e.get("enabled") or not user or not pwd:
        log("mailer: SMTP не настроен (секреты SMTP_USER/SMTP_PASS)")
        return False

    msg = EmailMessage()
    msg["From"] = formataddr((cfg["kp"]["author_name"], user))
    msg["To"] = to
    msg["Subject"] = subject
    msg["X-Lovec-Kintsugi"] = "1"        # метка своих писем — inbox их не трогает
    if in_reply_to:                      # чтобы ответ лёг в тот же тред
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body)
    try:
        msg.add_alternative(_to_html(body, cfg), subtype="html")
    except Exception:
        pass  # HTML-версия опциональна — plain text уйдёт в любом случае
    try:
        host, port = e["smtp_host"], int(e.get("smtp_port", 465))
        if port == 465:
            s = smtplib.SMTP_SSL(host, port, timeout=30)
        else:
            s = smtplib.SMTP(host, port, timeout=30)
            s.starttls()
        with s:
            s.login(user, pwd)
            s.send_message(msg)
        log(f"mailer: отправлено → {to}")
        return True
    except Exception as ex:
        log(f"mailer: ошибка отправки → {to} — {ex}")
        return False
