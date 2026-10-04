"""Автопополнение базы лидов — общий модуль для всех ловцов.

Задача одна: следить, чтобы в базе всегда лежал запас необработанных лидов.
Если запас просел ниже порога — дёрнуть существующий механизм поиска,
отфильтровать дубли и дописать уникальных.

Модуль НИЧЕГО не знает про почту, Telegram и рассылку. Он работает со
списком словарей и функцией поиска, которую ему передали. Поэтому его можно
положить в любой ловец без изменений.

Настройки — в config.toml, секция [replenish]:

    [replenish]
    min_new_leads        = 40   # сколько доступных лидов держать в запасе
    batch_size           = 50   # сколько искать за один подход
    buffer_percent       = 30   # запас сверх нехватки, % (дубли съедят часть)
    max_attempts         = 3    # попыток за один прогон, защита от зацикливания
"""

from __future__ import annotations

import re

# ──────────────────────────────────────────────────────────────────────
# Что считается «доступным» лидом
#
# Единое определение на весь проект. До этого их было два: автоотправка
# брала только status == "new", а рассылка — всё, кроме sent/skipped/replied.
# Из-за расхождения лиды в статусе "offered" (карточка показана, письмо не
# ушло) для одного механизма были мёртвыми, для другого живыми.
#
# Доступен тот, кому мы ещё не написали и кого не отбраковали.
# ──────────────────────────────────────────────────────────────────────
DONE_STATUSES = frozenset({"sent", "skipped", "replied"})

DEFAULTS = {
    "min_new_leads": 40,
    "batch_size": 50,
    "buffer_percent": 30,
    "max_attempts": 3,
}


# ───────────────────────────── нормализация ───────────────────────────

_SCHEME_RE = re.compile(r"^[a-z]+://", re.I)
_NON_ALNUM_RE = re.compile(r"[^a-zа-яё0-9]+", re.I)
_LEGAL_RE = re.compile(
    r"\b(ооо|оао|зао|ип|ао|пао|llc|ltd|inc|gmbh|корп|group|групп)\b", re.I)


def normalize_domain(url: str | None) -> str:
    """example.com, www.example.com, https://example.com/path → example.com"""
    if not url:
        return ""
    d = _SCHEME_RE.sub("", str(url).strip().lower())
    d = d.split("/")[0].split("?")[0].split("#")[0]
    d = d.split("@")[-1]          # на случай, если прилетела почта
    d = d.split(":")[0]           # порт
    return d.removeprefix("www.").strip(".")


def normalize_email(email: str | None) -> str:
    return (email or "").strip().lower()


def normalize_name(name: str | None) -> str:
    """«ООО "Белые Ветры"» и «Белые ветры» — одна компания."""
    n = _LEGAL_RE.sub(" ", (name or "").lower())
    return _NON_ALNUM_RE.sub("", n)


def lead_keys(lead: dict) -> set[str]:
    """Набор признаков, по которым лид считается уже известным."""
    keys = set()
    email = normalize_email(lead.get("email"))
    if email:
        keys.add("e:" + email)
        # почта на своём домене — тоже признак компании
        dom = email.split("@")[-1]
        if dom:
            keys.add("d:" + dom.removeprefix("www."))
    dom = normalize_domain(lead.get("site"))
    if dom:
        keys.add("d:" + dom)
    name = normalize_name(lead.get("name"))
    if len(name) >= 4:
        keys.add("n:" + name)
    lid = (lead.get("id") or "").strip().lower()
    if lid:
        keys.add("i:" + lid)
    return keys


def build_index(leads: list[dict]) -> set[str]:
    """Индекс всех признаков базы — включая уже обработанных лидов."""
    index: set[str] = set()
    for lead in leads:
        index |= lead_keys(lead)
    return index


def is_duplicate(lead: dict, index: set[str]) -> bool:
    return bool(lead_keys(lead) & index)


# ───────────────────────────── счётчики ───────────────────────────────

def count_available_leads(leads: list[dict]) -> int:
    """Сколько лидов ещё можно обработать."""
    return sum(1 for a in leads
               if (a.get("status") or "new") not in DONE_STATUSES)


# для совместимости с формулировкой ТЗ
count_new_leads = count_available_leads


def _settings(cfg: dict) -> dict:
    raw = (cfg or {}).get("replenish", {})
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        if isinstance(raw.get(k), int) and raw[k] >= 0:
            out[k] = raw[k]
    return out


# ───────────────────────────── пополнение ─────────────────────────────

def add_unique(leads: list[dict], candidates: list[dict],
               index: set[str] | None = None, log=print) -> int:
    """Дописать в базу только тех, кого там ещё нет. Вернуть сколько добавили.

    Дубли ищутся и внутри самой пачки кандидатов, и против всей базы —
    включая лидов, которым уже написали.
    """
    if index is None:
        index = build_index(leads)
    added = 0
    for cand in candidates:
        if not cand or not (cand.get("name") or "").strip():
            continue
        if is_duplicate(cand, index):
            continue
        cand.setdefault("status", "new")
        cand.setdefault("sent_ts", None)
        leads.append(cand)
        index |= lead_keys(cand)
        added += 1
        log(f"+ {cand.get('name')} ({cand.get('email') or cand.get('site') or '—'})")
    return added


def replenish_leads(leads: list[dict], cfg: dict, discover_fn, log=print,
                    notify=None) -> dict:
    """Добрать лидов до порога.

    discover_fn(limit, log) -> list[dict] — существующий механизм поиска.
    Возвращает отчёт: сколько добавлено, сколько попыток, хватило ли.
    """
    st = _settings(cfg)
    minimum = st["min_new_leads"]
    report = {"added": 0, "attempts": 0, "available_before":
              count_available_leads(leads), "available_after": 0,
              "enough": False, "reason": ""}

    index = build_index(leads)

    for attempt in range(1, st["max_attempts"] + 1):
        available = count_available_leads(leads)
        if available >= minimum:
            break

        report["attempts"] = attempt
        missing = minimum - available
        # ищем с запасом: часть кандидатов неизбежно окажется дублями
        limit = min(
            st["batch_size"],
            max(1, int(round(missing * (1 + st["buffer_percent"] / 100)))))
        log(f"пополнение: доступно {available}/{minimum}, "
            f"не хватает {missing} — ищу {limit} кандидатов "
            f"(попытка {attempt}/{st['max_attempts']})")

        try:
            candidates = discover_fn(limit, log) or []
        except Exception as e:                      # поиск не должен ронять прогон
            log(f"пополнение: поиск упал — {e}")
            report["reason"] = f"поиск упал: {e}"
            break

        if not candidates:
            log("пополнение: поиск не вернул кандидатов")
            report["reason"] = "поиск не вернул кандидатов"
            break

        added = add_unique(leads, candidates, index, log)
        report["added"] += added
        log(f"пополнение: кандидатов {len(candidates)}, "
            f"уникальных {added}, дублей {len(candidates) - added}")

        if added == 0:
            report["reason"] = "поиск возвращает только дубли"
            # смысла повторять тот же запрос нет — выходим, не крутим вхолостую
            break

    report["available_after"] = count_available_leads(leads)
    report["enough"] = report["available_after"] >= minimum

    if not report["enough"] and not report["reason"]:
        report["reason"] = "исчерпан лимит попыток"

    if not report["enough"]:
        msg = (f"⚠️ Не удалось набрать запас лидов: "
               f"{report['available_after']} из {minimum}. "
               f"Добавлено за прогон: {report['added']}. "
               f"Причина: {report['reason']}.")
        log(msg)
        if notify:
            notify(msg)
    elif report["added"]:
        msg = (f"🔎 База пополнена: +{report['added']}. "
               f"Доступно лидов: {report['available_after']}.")
        log(msg)
        if notify:
            notify(msg)

    return report


def ensure_minimum_leads(leads: list[dict], cfg: dict, discover_fn, log=print,
                         notify=None) -> dict:
    """Точка входа. Дёргается в начале каждого прогона.

    Если запаса хватает — не делает ничего и не ходит в сеть.
    """
    st = _settings(cfg)
    available = count_available_leads(leads)
    if available >= st["min_new_leads"]:
        log(f"пополнение не нужно: доступно {available} "
            f"(порог {st['min_new_leads']})")
        return {"added": 0, "attempts": 0, "available_before": available,
                "available_after": available, "enough": True, "reason": ""}
    return replenish_leads(leads, cfg, discover_fn, log, notify)
