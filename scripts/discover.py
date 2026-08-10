"""Автопополнение базы лидов. Два источника по очереди:
1. OpenStreetMap (Overpass) — office=event_management по Москве.
   Исторический факт: покрытие нулевое, но вдруг появится.
2. LLM-кандидаты: нейронка называет московские event-агентства и компании
   (имя + сайт), каждый сайт проверяется живым HTTP-запросом — в базу
   попадают только отвечающие сайты. Контакты добивает Collect.

Всегда отчитывается в Telegram, даже если не нашлось ничего.
Запуск: workflow Discover (пн 08:00 МСК), вручную или кнопкой «🔄 Обновить базу»."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tomllib
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kt import llm, store

OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
UA = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36")}
OSM_UA = {"User-Agent": "lovec-kintsugi/1.0 (+https://github.com/safonovich/lovec-kintsugi)"}
QUERY = """
[out:json][timeout:120];
area["ISO3166-2"="RU-MOW"][admin_level=4]->.msk;
(
  nwr["office"="event_management"](area.msk);
  nwr["shop"="events"](area.msk);
);
out tags;
"""
MAX_NEW_PER_RUN = 15

LLM_SYSTEM = """Ты помогаешь пополнять базу для точечной B2B-рассылки о корпоративных
мастер-классах кинцуги (тимбилдинги). Назови РЕАЛЬНЫЕ существующие московские
организации двух типов:
- event-агентства и агентства тимбилдингов (segment "event")
- средние IT/финтех/консалтинг-компании Москвы, у которых открыт карьерный
  сайт или почта hr@/career@ (segment "company")

Только организации, в существовании которых ты уверен, с их настоящими
сайтами. Никаких выдумок: лучше 8 настоящих, чем 20 сомнительных.

Ответь СТРОГО JSON-массивом без пояснений:
[{"name": "...", "site": "https://...", "segment": "event"|"company", "why": "1 фраза, чем интересны"}]"""


def log(msg: str) -> None:
    print(f"[discover] {msg}", flush=True)


def _notify(text: str) -> None:
    if os.environ.get("TG_BOT_TOKEN") and os.environ.get("TG_CHAT_ID"):
        from kt import notify
        notify.send_service(text, log)


def _domain(url: str) -> str:
    d = re.sub(r"^https?://", "", (url or "").lower()).split("/")[0]
    return d.removeprefix("www.")


def _slug(name: str, site: str) -> str:
    d = _domain(site)
    if d:
        return re.sub(r"[^a-z0-9]", "", d.split(".")[0]) or "x" + hashlib.sha1(d.encode()).hexdigest()[:8]
    return "x" + hashlib.sha1(name.lower().encode()).hexdigest()[:8]


def _site_alive(url: str) -> bool:
    try:
        r = requests.get(url, headers=UA, timeout=10, allow_redirects=True)
        return r.ok and "html" in r.headers.get("content-type", "html")
    except Exception:
        return False


def _add(leads: list, known: dict, name: str, site: str | None, segment: str,
         note: str, email=None, phone=None) -> bool:
    if not name or len(name) < 3:
        return False
    if name.lower() in known["names"] or (_domain(site or "") and _domain(site or "") in known["domains"]):
        return False
    aid = _slug(name, site or "")
    while aid in known["ids"]:
        aid += "x"
    leads.append({"id": aid, "name": name, "site": (site or "").rstrip("/") or None,
                  "email": email, "phone": phone, "tg": None,
                  "segment": segment, "city": "Москва", "note": note,
                  "status": "new", "sent_ts": None})
    known["ids"].add(aid)
    known["names"].add(name.lower())
    if _domain(site or ""):
        known["domains"].add(_domain(site))
    log(f"+ {name} ({site or email})")
    return True


def main() -> None:
    cfg = tomllib.loads(
        (Path(__file__).resolve().parent.parent / "kt" / "config.toml")
        .read_text(encoding="utf-8"))
    leads: list = store.load("leads.json", [])
    known = {"ids": {a["id"] for a in leads},
             "domains": {_domain(a.get("site", "")) for a in leads} - {""},
             "names": {a["name"].lower() for a in leads}}
    added = 0

    # --- Источник 1: OSM ---
    elements = []
    for mirror in OVERPASS_MIRRORS:
        try:
            r = requests.post(mirror, data={"data": QUERY}, headers=OSM_UA, timeout=180)
            r.raise_for_status()
            elements = r.json().get("elements", [])
            break
        except Exception as e:
            log(f"Overpass ({mirror.split('/')[2]}) не ответил — {e}")
    log(f"OSM вернул объектов: {len(elements)}")
    for el in elements:
        if added >= MAX_NEW_PER_RUN:
            break
        t = el.get("tags", {})
        site = t.get("website") or t.get("contact:website") or ""
        email = t.get("email") or t.get("contact:email")
        if not site and not email:
            continue
        if _add(leads, known, t.get("name", "").strip(), site, "event",
                "найдено автоматически (OSM)", email=email,
                phone=t.get("phone") or t.get("contact:phone")):
            added += 1

    # --- Источник 2: LLM-кандидаты с проверкой сайтов ---
    if added < 5:
        sample = ", ".join(sorted(known["names"]))[:1500]
        txt = llm.chat(LLM_SYSTEM,
                       f"Уже в базе (НЕ повторяй): {sample}", cfg, log,
                       max_tokens=1200)
        cand = []
        if txt:
            try:
                cand = json.loads(txt[txt.find("["):txt.rfind("]") + 1])
            except Exception as e:
                log(f"LLM: не разобрал ответ ({e})")
        else:
            log("LLM недоступен — кандидатов нет")
        checked = 0
        for c in cand:
            if added >= MAX_NEW_PER_RUN or checked >= 25:
                break
            site = str(c.get("site", "")).strip()
            seg = c.get("segment") if c.get("segment") in ("event", "company") else "event"
            if not site.startswith("http"):
                continue
            checked += 1
            if _domain(site) in known["domains"]:
                continue
            if not _site_alive(site):
                log(f"мимо (сайт не отвечает): {c.get('name')} {site}")
                continue
            if _add(leads, known, str(c.get("name", "")).strip(), site, seg,
                    f"кандидат от нейронки, сайт проверен · {c.get('why', '')}"[:200]):
                added += 1

    store.save("leads.json", leads)
    fresh = sum(1 for a in leads if a.get("status", "new") == "new")
    log(f"добавлено новых: {added}, всего в базе: {len(leads)}, статус new: {fresh}")

    if added:
        _notify(f"🔎 База пополнена: +{added} лидов (в очереди new: {fresh}). "
                "Контакты добьёт Collect / прогон.")
    else:
        _notify("🔎 Поиск прошёл, но нового не нашлось: OSM по event-агентствам "
                f"Москвы пуст, нейронка не дала непроверенных кандидатов. "
                f"В очереди new: {fresh}. Надёжный способ пополнить базу — "
                "попросить Claude в чате «Ловец-Кинцуги»: он найдёт и проверит "
                "лиды через веб-поиск.")


if __name__ == "__main__":
    main()
