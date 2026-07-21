"""Сборщик контактов: проходит по data/leads.json и для лидов без
email/телефона тянет их сайт (главная + /contacts и т.п.), достаёт контакты.
Запуск: вручную через Actions → Collect → Run workflow (или локально)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kt import enrich, store


def log(msg: str) -> None:
    print(f"[collect] {msg}", flush=True)


def main() -> None:
    leads: list = store.load("leads.json", [])
    if not leads:
        log("leads.json пуст")
        return
    filled, dead = 0, []
    for a in leads:
        if a.get("email") and a.get("phone"):
            continue
        found = enrich.enrich(a, log)
        got = False
        for f in ("email", "phone", "tg", "wa"):
            if found.get(f) and not a.get(f):
                a[f] = found[f]
                got = True
        if got:
            filled += 1
            log(f"{a['name']}: email={a.get('email')} phone={a.get('phone')} tg={a.get('tg')}")
        elif not found:
            dead.append(a["name"])
    store.save("leads.json", leads)
    log(f"обогащено: {filled}")
    if dead:
        log("сайт не ответил (проверь адреса): " + ", ".join(dead))


if __name__ == "__main__":
    main()
