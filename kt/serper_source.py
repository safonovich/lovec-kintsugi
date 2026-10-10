"""Источник лидов: обычный веб-поиск через Serper (Google Search API).

Зачем он нужен. OSM — это карта магазинов, она не знает отраслей: по запросу
«event-агентства Москвы» там десяток записей, и они давно выбраны. Нейросеть
по памяти выдумывает компании — половина с мёртвыми сайтами. Веб-поиск даёт
то, что нужно: живые сайты реальных компаний по отраслевому запросу.

Как работает:
  1. берёт поисковые запросы из business.toml;
  2. спрашивает у Serper выдачу (по странице за раз, курсор крутится,
     чтобы не возвращать одно и то же);
  3. выбрасывает агрегаторы, каталоги и соцсети — нужны сайты самих компаний;
  4. заходит на сайт и снимает почту с главной и со страницы контактов.

Ключ — в переменной окружения SERPER_API_KEY (секрет репозитория).
Без ключа модуль молча возвращает пустой список: прогон не падает,
остальные источники работают как работали.
"""

from __future__ import annotations

import os
import re
import time

import requests

API_URL = "https://google.serper.dev/search"
UA = {"User-Agent": "Mozilla/5.0 (compatible; lovec-outreach/1.0)"}

EMAIL_RE = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+")
TAG_RE = re.compile(r"<script.*?</script>|<style.*?</style>", re.S | re.I)

# Почты, которые ничего не стоят: хостеры, заглушки, технические ящики.
BAD_EMAIL_PARTS = (
    "beget.", "reg.ru", "nic.ru", "timeweb", "sweb.ru", "masterhost",
    "example.", "domain.ru", "sitename", "yourmail", "test@", "noreply",
    "no-reply", "@sentry", "wixpress", "@wix.com", "@tilda", "sentry.io",
    "godaddy", "@2x.png", ".png", ".jpg", ".gif", ".webp",
)

# Не компании, а площадки: на них писать бессмысленно.
# Сверка идёт по границе домена: "mos.ru" не должен задевать "laterra-mos.ru",
# а "novostroy" — живое агентство best-novostroy.ru.
BLOCK_DOMAINS = (
    "cian.ru", "avito.ru", "domclick.ru", "2gis.ru", "2gis.com", "ya.ru",
    "hh.ru", "zoon.ru", "yell.ru", "flamp.ru", "rusprofile.ru",
    "list-org.com", "spark-interfax.ru", "sbis.ru", "youtube.com",
    "vk.com", "ok.ru", "t.me", "dzen.ru", "vc.ru", "habr.com", "pikabu.ru",
    "irr.ru", "youla.ru", "kp.ru", "rbc.ru", "forbes.ru", "tproger.ru",
    "pinterest.com", "tiktok.com", "restate.ru", "move.ru", "afisha.ru",
    "timepad.ru", "profi.ru", "youdo.com", "the-village.ru",
    # СМИ, госсайты, отраслевые порталы и каталоги — это не компании-клиенты
    "sostav.ru", "adindex.ru", "mosreg.ru", "mos.ru", "kontur.ru", "1c.ru",
    "consultant.ru", "garant.ru", "interfax.ru", "avaho.ru", "cottage.ru",
    "zagorod.ru", "ydacha.ru", "domzamkad.ru", "estate-top.ru", "rgr.ru",
    "rgr42.ru", "mediakassir.ru", "mskguru.ru", "poselkino.ru",
    "novostroy-m.ru", "vedomosti.ru", "kommersant.ru", "tass.ru", "ria.ru",
    "lenta.ru", "banki.ru", "sravni.ru", "tbank.ru", "sberbank.ru",
    "vtb.ru", "gosuslugi.ru",
)

# Куски, которые сами по себе однозначны: совпадение где угодно в домене.
BLOCK_PARTS = (
    "yandex.", "google.", "wikipedia.", "telegram.", "instagram.",
    "facebook.", "blogspot.", "livejournal.",
    "xn--",                      # кириллические домены рейтингов и каталогов
    ".gov.ru", "gov.ru",
)

# Заголовок статьи, а не название компании.
ARTICLE_MARKERS = (
    "рейтинг", "топ-", "топ ", "лучши", "каталог", "список", "отзыв",
    "сравнени", "обзор", "как выбрать", "сколько стоит", "цены на",
    "ваканси", "новости", "статьи", "блог", "форум", "википедия",
)

CONTACT_PATHS = ("", "/contacts", "/contacts/", "/kontakty", "/kontakty/",
                 "/contact", "/about/contacts/")


def _domain(url: str) -> str:
    d = re.sub(r"^[a-z]+://", "", (url or "").lower()).split("/")[0]
    return d.removeprefix("www.").split(":")[0]


def _is_company_site(url: str) -> bool:
    d = _domain(url)
    if not d or "." not in d:
        return False
    if any(part in d for part in BLOCK_PARTS):
        return False
    return not any(d == bad or d.endswith("." + bad) for bad in BLOCK_DOMAINS)


def _looks_like_article(title: str) -> bool:
    t = title.lower()
    if any(m in t for m in ARTICLE_MARKERS):
        return True
    return bool(re.search(r"\b20[12]\d\b", t))      # «…в 2026 году»


GENERIC_WORDS = {
    "агентство", "агентства", "агентств", "недвижимость", "недвижимости",
    "элитной", "элитная", "загородной", "загородная", "городской",
    "москве", "москва", "московской", "области", "подмосковье", "подмосковья",
    "в", "и", "по", "на", "от", "для", "продажа", "продаже", "аренда",
    "аренде", "купить", "снять", "официальный", "сайт", "компания",
    "компании", "строительная", "девелопер", "застройщик", "риелтор",
    "риэлтор", "услуги", "центр", "бюро", "группа", "коттеджных",
    "посёлков", "поселков", "домов", "квартир", "новостройки", "новостроек",
}


def _strip_generic(name: str) -> str:
    """Срезать обобщающие слова с краёв, чтобы осталось имя бренда.

    «Агентство элитной недвижимости в Москве Contact Real» → «Contact Real».
    """
    words = name.split()
    while words and words[0].lower().strip(".,:;") in GENERIC_WORDS:
        words.pop(0)
    while words and words[-1].lower().strip(".,:;") in GENERIC_WORDS:
        words.pop()
    return " ".join(words).strip(" .,:;-–—…")


# Слова, которые остаются от шапки сайта и названием компании не являются.
JUNK_NAMES = {
    "ооо", "оао", "зао", "ип", "ao", "llc", "контакты", "контакт", "главная",
    "о компании", "о нас", "главная страница", "каталог", "услуги", "недвижимость",
    "агентство недвижимости", "агентство", "сайт", "домой", "home", "contacts",
    "about", "menu", "меню", "новости", "вакансии",
    "https", "http", "www", "index", "page", "страница",
}


def _name_is_junk(name: str) -> bool:
    """«Контакты», «ООО», «Главная» — это не название компании."""
    n = name.strip().lower().strip(".,:;«»\"'")
    return n in JUNK_NAMES or len(n) < 3


# Описание услуги вместо названия: «Выездной тимбилдинг для компаний».
# Такой заголовок ничего не говорит о том, кому мы пишем — берём домен.
DESCRIPTIVE_MARKERS = (
    "тимбилдинг", "корпоратив", "мастер-класс", "мастер класс", "организация",
    "проведение", "выездн", "для компаний", "для сотрудников", "под ключ",
    "квест", "лазертаг", "праздник", "мероприяти", "деловой туризм",
    "заказать", "цена", "купить", "аренда", "доставка",
)


def _is_description(name: str) -> bool:
    n = name.lower()
    return any(m in n for m in DESCRIPTIVE_MARKERS)


def clean_name(title: str, domain: str) -> str | None:
    """Из заголовка страницы сделать название компании.

    Заголовки-статьи («Рейтинг лучших… 2026») отбрасываем целиком.
    Из описательных вытаскиваем бренд; если не вышло — берём имя домена.
    """
    name = (title or "").strip()
    for sep in ("|", "—", " - ", " – ", "::", " • ", ":"):
        if sep in name:
            name = name.split(sep)[0]
    name = name.strip(" .,:;-–—…").strip()
    if not name or _looks_like_article(name):
        return None

    brand = _strip_generic(name)
    if (2 < len(brand) <= 60 and len(brand.split()) <= 4
            and not _name_is_junk(brand) and not _is_description(brand)):
        return brand

    base = domain.split(".")[0].replace("-", " ")
    if len(base) < 3:
        return None
    return (base.upper() if len(base) <= 4 else base.title())[:120]


# ── Живой ли домен ───────────────────────────────────────────────────
# Письмо на несуществующий домен отбивается и портит репутацию ящика.
# «Ящик не найден» так не поймать — это делает разбор отбоев в inbox.py.
_MX_CACHE: dict[str, bool] = {}
_DNS_OK: bool | None = None


def _dns_works() -> bool:
    """Отвечает ли DNS вообще.

    Если в окружении DNS недоступен, проверка начнёт отбрасывать всех
    подряд и база перестанет пополняться. Поэтому один раз сверяемся
    с заведомо живым доменом: молчит — проверку не делаем вовсе.
    """
    global _DNS_OK
    if _DNS_OK is None:
        try:
            import dns.resolver
            r = dns.resolver.Resolver()
            r.timeout = r.lifetime = 5.0
            r.resolve("ya.ru", "MX")
            _DNS_OK = True
        except Exception:
            _DNS_OK = False
    return _DNS_OK


def _accepts_mail(domain: str) -> bool:
    """Принимает ли домен почту. При любом сомнении — да."""
    dom = (domain or "").lower().strip(".")
    if not dom or "." not in dom:
        return False
    if dom in _MX_CACHE:
        return _MX_CACHE[dom]
    if not _dns_works():
        return True
    try:
        import dns.resolver
        r = dns.resolver.Resolver()
        r.timeout = r.lifetime = 5.0
    except Exception:
        _MX_CACHE[dom] = True
        return True
    try:
        ok = bool(r.resolve(dom, "MX"))
    except dns.resolver.NXDOMAIN:
        ok = False
    except dns.resolver.NoAnswer:
        try:                       # без MX почта может идти на сам хост
            ok = bool(r.resolve(dom, "A"))
        except Exception:
            ok = False
    except Exception:
        ok = True                  # таймаут или сбой сети — не наказываем лид
    _MX_CACHE[dom] = ok
    return ok

def _usable_email(email: str) -> bool:
    e = email.lower()
    if any(p in e for p in BAD_EMAIL_PARTS):
        return False
    if len(e) >= 70 or e.count("@") != 1:
        return False
    tld = e.rsplit(".", 1)[-1]
    # отсекает мусор вроде bootstrap@4.5.3, пойманный регуляркой из вёрстки
    return tld.isalpha() and len(tld) >= 2


def _pick_email(emails: list[str], domain: str) -> str | None:
    """Почта на домене компании лучше, чем сборная солянка с чужих доменов."""
    good = [e for e in dict.fromkeys(emails)
            if _usable_email(e) and _accepts_mail(e.rsplit("@", 1)[-1])]
    if not good:
        return None
    own = [e for e in good if e.lower().endswith("@" + domain)]
    pool = own or good
    # info@ / sales@ / hello@ полезнее, чем личная почта сотрудника
    for prefix in ("info@", "sales@", "hello@", "office@", "mail@",
                   "pr@", "partner", "zakaz@", "shop@"):
        for e in pool:
            if e.lower().startswith(prefix):
                return e
    return pool[0]


def _contacts_from_site(site: str, log, timeout: int = 12) -> str | None:
    """Пройтись по главной и типичным страницам контактов, снять почту."""
    domain = _domain(site)
    base = "https://" + domain
    found: list[str] = []
    for path in CONTACT_PATHS:
        try:
            r = requests.get(base + path, headers=UA, timeout=timeout,
                             allow_redirects=True)
            if not r.ok or "html" not in r.headers.get("content-type", "html"):
                continue
            text = TAG_RE.sub(" ", r.text[:400_000])
            found += EMAIL_RE.findall(text)
            email = _pick_email(found, domain)
            if email:
                return email
        except Exception:
            continue
    return _pick_email(found, domain)


def search_once(query: str, log, page: int = 1, num: int = 10,
                gl: str = "ru", hl: str = "ru") -> list[dict]:
    """Одна страница выдачи. Пустой список при любой ошибке."""
    key = os.environ.get("SERPER_API_KEY", "").strip()
    if not key:
        log("serper: ключ SERPER_API_KEY не задан — источник пропущен")
        return []
    try:
        r = requests.post(
            API_URL,
            headers={"X-API-KEY": key, "Content-Type": "application/json"},
            json={"q": query, "gl": gl, "hl": hl, "num": num, "page": page},
            timeout=30)
        if r.status_code == 403:
            log("serper: ключ отвергнут (403) — проверь секрет")
            return []
        if r.status_code == 429:
            log("serper: лимит запросов исчерпан (429)")
            return []
        r.raise_for_status()
        return r.json().get("organic", []) or []
    except Exception as e:
        log(f"serper: запрос не прошёл — {e}")
        return []


def find_leads(limit: int, cfg: dict, log, state: dict | None = None,
               pause: float = 0.5) -> list[dict]:
    """Главная функция источника: вернуть до `limit` кандидатов.

    Кандидат = {name, site, email, phone, note, status}. Почта может быть
    None — её потом добьёт Collect; дубли отсекает replenish.add_unique.

    `state` — любой словарь, который переживает прогоны (у нас tg_state).
    В нём хранится курсор, чтобы каждый раз не перебирать одни и те же
    страницы выдачи по одному и тому же запросу.
    """
    search_cfg = (cfg or {}).get("search", {})
    queries = [q for q in search_cfg.get("queries", []) if str(q).strip()]
    if not queries:
        log("serper: в business.toml нет поисковых запросов — источник пропущен")
        return []

    gl = search_cfg.get("gl", "ru")
    hl = search_cfg.get("hl", "ru")
    max_pages = int(search_cfg.get("max_pages", 5))
    fetch_contacts = bool(search_cfg.get("fetch_contacts", True))

    state = state if state is not None else {}
    cursor = int(state.get("serper_cursor", 0))

    out: list[dict] = []
    seen_domains: set[str] = set()
    # по запросу за подход, страницы крутятся курсором
    for step in range(len(queries) * max_pages):
        if len(out) >= limit:
            break
        idx = (cursor + step) % (len(queries) * max_pages)
        query = queries[idx % len(queries)]
        page = idx // len(queries) + 1

        results = search_once(query, log, page=page, gl=gl, hl=hl)
        log(f"serper: «{query}» стр.{page} → {len(results)} результатов")
        if not results:
            continue

        for item in results:
            if len(out) >= limit:
                break
            link = item.get("link") or ""
            if not _is_company_site(link):
                continue
            domain = _domain(link)
            if domain in seen_domains:
                continue
            seen_domains.add(domain)

            name = clean_name(item.get("title") or "", domain)
            if not name:
                log(f"мимо (похоже на статью, не компанию): {item.get('title')}")
                continue

            email = _contacts_from_site(link, log) if fetch_contacts else None
            out.append({
                "name": name,
                "site": "https://" + domain,
                "email": email,
                "phone": None,
                "tg": None,
                "note": f"найдено поиском: {query}"[:200],
                "status": "new",
                "sent_ts": None,
            })
            if fetch_contacts:
                time.sleep(pause)      # не долбим чужие сайты очередью

    state["serper_cursor"] = cursor + len(queries)
    with_mail = sum(1 for a in out if a["email"])
    log(f"serper: кандидатов {len(out)}, из них с почтой {with_mail}")
    return out
