"""Проставить id лидам, у которых его нет.

Зачем. Бот строит справочник лидов по полю id. Поиск через Serper
добавлял записи без него — и прогон падал с KeyError: 'id' ещё до того,
как успевал обработать нажатые кнопки. Этот скрипт чинит базу и
запускается отдельным заданием, не трогая остальную логику.

Идентификатор делается из имени домена: kre.ru -> kre. Если такой уже
занят, добавляется число. Существующие id не трогаются никогда —
по ним привязаны письма и статусы.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def slug(lead: dict) -> str:
    site = (lead.get("site") or "").lower()
    host = re.sub(r"^[a-z]+://", "", site).split("/")[0]
    host = host.removeprefix("www.").split(":")[0]
    base = re.sub(r"[^a-z0-9]+", "", host.split(".")[0])
    if not base:
        name = (lead.get("name") or "").lower()
        base = re.sub(r"[^a-z0-9]+", "", name)[:20]
    return base[:20] or "lead"


def fix(path: Path) -> int:
    if not path.exists():
        return 0
    leads = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(leads, list):
        return 0
    used = {l.get("id") for l in leads if l.get("id")}
    fixed = 0
    for lead in leads:
        if lead.get("id"):
            continue
        base = slug(lead)
        new_id, n = base, 2
        while new_id in used:
            new_id = f"{base}{n}"
            n += 1
        used.add(new_id)
        # id первым ключом — так же, как в записях, заведённых вручную
        rest = dict(lead)
        lead.clear()
        lead["id"] = new_id
        lead.update(rest)
        fixed += 1
        print(f"[fix_ids] {new_id} <- {rest.get('name')}", flush=True)
    if fixed:
        path.write_text(json.dumps(leads, ensure_ascii=False, indent=1) + "\n",
                        encoding="utf-8")
    return fixed


def main() -> None:
    total = 0
    for name in ("agencies.json", "leads.json"):
        total += fix(DATA / name)
    print(f"[fix_ids] проставлено id: {total}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
