"""Обогащение: тянем сайт агентства, достаём email/телефон и текст для персонализации КП."""

from __future__ import annotations

import re

import requests

UA = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36")}

EMAIL_RE = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)*\.[a-zA-Z]{2,}")
PHONE_RE = re.compile(r"(?<!\d)(?:\+7|8)[\s(-]*(\d{3})[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}(?!\d)")
GOOD_CODES = {"495", "499", "498", "800"}   # + мобильные 9xx
TG_RE = re.compile(r"(?:t\.me|telegram\.me)/([A-Za-z0-9_]{4,32})")
WA_RE = re.compile(r"(?:wa\.me/|api\.whatsapp\.com/send\?phone=)\+?(\d{10,15})")
TAG_RE = re.compile(r"<script.*?</script>|<style.*?</style>|<[^>]+>", re.S)

BAD_EMAIL = ("example.", "sentry", "wixpress", "@2x", ".png", ".jpg", ".webp", ".svg")
CONTACT_PATHS = ("", "/contacts", "/contact", "/kontakty", "/about", "/o-kompanii")


def _get(url: str, log) -> str:
    try:
        r = requests.get(url, headers=UA, timeout=8, allow_redirects=True)
        if r.ok and "text/html" in r.headers.get("content-type", "html"):
            return r.text[:400_000]
    except Exception as e:
        log(f"enrich: {url} — {e}")
    return ""


def _clean_email(m: str) -> str | None:
    m = m.strip().strip(".").lower()
    return None if any(b in m for b in BAD_EMAIL) else m


def enrich(agency: dict, log) -> dict:
    """Возвращает {"email","phone","tg","site_text"} — только найденное, ничего не выдумывает."""
    site = (agency.get("site") or "").rstrip("/")
    found: dict = {}
    if not site:
        return found
    text_all = ""
    for path in CONTACT_PATHS:
        html = _get(site + path, log)
        if not html:
            if path == "":      # главная не отвечает (гео-блок/лежит) — не мучаем остальные пути
                log(f"enrich: {site} недоступен — пропускаю")
                break
            continue
        text_all += html
        if path == "":
            plain = TAG_RE.sub(" ", html)
            found["site_text"] = re.sub(r"\s+", " ", plain).strip()[:1500]
        if found.get("email") and found.get("phone"):
            break
        if not found.get("email"):
            for m in EMAIL_RE.findall(html):
                if (e := _clean_email(m)):
                    found["email"] = e
                    break
        if not found.get("phone"):
            for p in PHONE_RE.finditer(html):
                code = p.group(1)
                if code in GOOD_CODES or code.startswith("9"):
                    digits = re.sub(r"\D", "", p.group())
                    found["phone"] = "+7" + digits[-10:]
                    break
    if not agency.get("tg") and (t := TG_RE.search(text_all)):
        found["tg"] = "@" + t.group(1)
    # wa.me-ссылка с их сайта = гарантированно живой WhatsApp агентства
    if not agency.get("wa") and (w := WA_RE.search(text_all)):
        digits = w.group(1)
        found["wa"] = "+" + (("7" + digits[-10:]) if not digits.startswith("7") else digits)
    return found
