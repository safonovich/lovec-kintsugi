"""Пополнение базы лидов — отдельный прогон.

Запускается своим workflow по расписанию и ничего не трогает в основном
боте: только дописывает новых лидов в базу. Основной прогон потом видит
пополненную базу и работает как работал.

Что делает:
  1. смотрит, сколько в базе доступных лидов (кому ещё не писали);
  2. если меньше порога из business.toml — ищет новых через Serper;
  3. отсекает дубли по почте, домену и названию;
  4. сохраняет базу.

Безопасно по построению: если ключа Serper нет, поиск упал или пришли одни
дубли — скрипт пишет об этом в лог и в Telegram и завершается без ошибки.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Пакет называется по-разному у разных ловцов — находим сами.
PKG = None
for name in ("fpv", "kt", "lovec"):
    if (ROOT / name / "replenish.py").exists():
        PKG = name
        break
if PKG is None:
    raise SystemExit("не нашёл пакет с replenish.py — проверь структуру репозитория")

pkg = __import__(PKG, fromlist=["replenish", "serper_source", "store", "notify"])
replenish = pkg.replenish
serper_source = pkg.serper_source
store = pkg.store
notify = getattr(pkg, "notify", None)

# Файл базы тоже называется по-разному.
DATA_FILE = "agencies.json" if (ROOT / "data" / "agencies.json").exists() \
    else "leads.json"
STATE_FILE = "tg_state.json"


def log(msg: str) -> None:
    print(f"[replenish] {msg}", flush=True)


def _notify(text: str) -> None:
    if notify is None:
        return
    try:
        notify.send_service(text, log)
    except Exception as e:
        log(f"уведомление не ушло: {e}")


def load_business() -> dict:
    path = ROOT / "business.toml"
    if not path.exists():
        log("business.toml не найден — работаю на значениях по умолчанию")
        return {}
    return tomllib.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    business = load_business()
    leads = store.load(DATA_FILE, [])
    state = store.load(STATE_FILE, {})

    log(f"база: {DATA_FILE}, всего записей {len(leads)}, "
        f"доступных {replenish.count_available_leads(leads)}")

    def discover(limit: int, lg) -> list[dict]:
        """Источник кандидатов. Сейчас — Serper; сюда же можно добавить другие."""
        return serper_source.find_leads(limit, business, lg, state)

    report = replenish.ensure_minimum_leads(
        leads, business, discover, log, notify=_notify)

    if report["added"]:
        store.save(DATA_FILE, leads)
        store.save(STATE_FILE, state)
        log(f"сохранено: +{report['added']}, всего {len(leads)}")
    else:
        # курсор поиска мог сдвинуться даже без добавлений — сохраним его
        store.save(STATE_FILE, state)
        log("новых лидов не добавлено")


if __name__ == "__main__":
    main()
