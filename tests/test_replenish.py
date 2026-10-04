"""Тесты механизма автопополнения.

Запуск из корня репозитория:   python -m unittest discover -s tests -v
Новых зависимостей не требует — только стандартный unittest.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kt import replenish  # noqa: E402


def lead(name, status="new", email=None, site=None, lid=None):
    return {"id": lid or name.lower().replace(" ", ""), "name": name,
            "email": email, "site": site, "status": status, "sent_ts": None}


def base(n_new=0, n_done=0):
    out = [lead(f"Компания {i}") for i in range(n_new)]
    out += [lead(f"Отработана {i}", status="sent") for i in range(n_done)]
    return out


CFG = {"replenish": {"min_new_leads": 100, "batch_size": 50,
                     "buffer_percent": 30, "max_attempts": 3}}


def silent(*a, **kw):
    pass


class TestNormalization(unittest.TestCase):

    def test_domain_forms_collapse(self):
        forms = ["example.com", "www.example.com", "https://example.com/",
                 "http://WWW.Example.com/path?x=1", "https://example.com:443"]
        got = {replenish.normalize_domain(f) for f in forms}
        self.assertEqual(got, {"example.com"})

    def test_empty_domain(self):
        self.assertEqual(replenish.normalize_domain(None), "")
        self.assertEqual(replenish.normalize_domain(""), "")

    def test_name_forms_collapse(self):
        a = replenish.normalize_name('ООО "Белые Ветры"')
        b = replenish.normalize_name("Белые ветры")
        self.assertEqual(a, b)

    def test_duplicate_by_domain_variant(self):
        leads = [lead("Alpha", site="https://alpha.ru/")]
        index = replenish.build_index(leads)
        self.assertTrue(replenish.is_duplicate(
            {"name": "Альфа другое имя", "site": "www.alpha.ru"}, index))

    def test_duplicate_by_email(self):
        leads = [lead("Alpha", email="Info@Alpha.RU")]
        index = replenish.build_index(leads)
        self.assertTrue(replenish.is_duplicate(
            {"name": "Совсем другое", "email": "info@alpha.ru"}, index))

    def test_not_duplicate(self):
        leads = [lead("Alpha", site="https://alpha.ru")]
        index = replenish.build_index(leads)
        self.assertFalse(replenish.is_duplicate(
            {"name": "Beta", "site": "https://beta.ru"}, index))


class TestCounting(unittest.TestCase):

    def test_done_statuses_not_counted(self):
        leads = [lead("a"), lead("b", "offered"), lead("c", "sent"),
                 lead("d", "skipped"), lead("e", "replied")]
        # доступны: new + offered
        self.assertEqual(replenish.count_available_leads(leads), 2)

    def test_missing_status_treated_as_new(self):
        self.assertEqual(replenish.count_available_leads([{"name": "x"}]), 1)


class TestScenarios(unittest.TestCase):
    """Сценарии A–E из задания."""

    def test_a_enough_leads_no_search(self):
        """A: 120 доступных при пороге 100 → поиск не запускается."""
        leads = base(120)
        calls = []

        def discover(limit, log):
            calls.append(limit)
            return []

        rep = replenish.ensure_minimum_leads(leads, CFG, discover, silent)
        self.assertEqual(calls, [])
        self.assertEqual(rep["added"], 0)
        self.assertTrue(rep["enough"])

    def test_b_replenishes_with_buffer(self):
        """B: 70 доступных → ищем недостающие 30 плюс запас 30%."""
        leads = base(70)
        calls = []

        def discover(limit, log):
            calls.append(limit)
            return [lead(f"Новая {i}", site=f"https://new{i}.ru")
                    for i in range(limit)]

        rep = replenish.ensure_minimum_leads(leads, CFG, discover, silent)
        self.assertEqual(calls[0], 39)              # 30 * 1.3
        self.assertGreaterEqual(rep["available_after"], 100)
        self.assertTrue(rep["enough"])

    def test_c_mostly_duplicates_then_second_pass(self):
        """C: поиск вернул 50, из них 35 дублей → добавлено 15, ищем ещё."""
        existing = [lead(f"Старая {i}", site=f"https://old{i}.ru")
                    for i in range(35)]
        leads = [lead(f"Компания {i}") for i in range(65)] + existing
        # доступно 100? нет: 65 + 35 = 100 → опустим порог проверки
        leads = leads[:35] + existing            # 35 обычных + 35 старых = 70
        self.assertEqual(replenish.count_available_leads(leads), 70)

        rounds = []

        def discover(limit, log):
            rounds.append(limit)
            if len(rounds) == 1:
                dupes = [lead(f"Старая {i}", site=f"www.old{i}.ru")
                         for i in range(35)]
                fresh = [lead(f"Свежая {i}", site=f"https://fresh{i}.ru")
                         for i in range(15)]
                return dupes + fresh
            return [lead(f"Ещё {i}", site=f"https://more{i}.ru")
                    for i in range(limit)]

        rep = replenish.ensure_minimum_leads(leads, CFG, discover, silent)
        self.assertEqual(len(rounds), 2, "после дублей должен быть второй заход")
        self.assertGreaterEqual(rep["available_after"], 100)

    def test_d_only_duplicates_does_not_loop(self):
        """D: поиск отдаёт только дубли → выходим, не зацикливаемся."""
        leads = base(10)
        rounds = []

        def discover(limit, log):
            rounds.append(limit)
            return [lead("Компания 0")]          # уже в базе

        notes = []
        rep = replenish.ensure_minimum_leads(leads, CFG, discover, silent,
                                             notify=notes.append)
        self.assertLessEqual(len(rounds), CFG["replenish"]["max_attempts"])
        self.assertFalse(rep["enough"])
        self.assertEqual(rep["added"], 0)
        self.assertTrue(notes, "должно прийти понятное сообщение о неудаче")
        self.assertIn("дубли", notes[0])

    def test_d_attempt_limit_respected(self):
        """D: даже если каждый раз приходит по одному новому — не крутим вечно."""
        leads = base(0)
        rounds = []

        def discover(limit, log):
            rounds.append(limit)
            i = len(rounds)
            return [lead(f"Одна {i}", site=f"https://one{i}.ru")]

        rep = replenish.ensure_minimum_leads(leads, CFG, discover, silent)
        self.assertEqual(len(rounds), CFG["replenish"]["max_attempts"])
        self.assertFalse(rep["enough"])
        self.assertEqual(rep["added"], 3)

    def test_e_after_sending_stock_is_refilled(self):
        """E: письма ушли, запас просел → следующий прогон добирает."""
        leads = base(100)
        for a in leads[:40]:
            a["status"] = "sent"                 # 40 писем ушло
        self.assertEqual(replenish.count_available_leads(leads), 60)

        def discover(limit, log):
            return [lead(f"Добор {i}", site=f"https://add{i}.ru")
                    for i in range(limit)]

        rep = replenish.ensure_minimum_leads(leads, CFG, discover, silent)
        self.assertTrue(rep["enough"])
        self.assertGreaterEqual(replenish.count_available_leads(leads), 100)


class TestRobustness(unittest.TestCase):

    def test_search_failure_does_not_raise(self):
        leads = base(5)

        def discover(limit, log):
            raise RuntimeError("Serper недоступен")

        rep = replenish.ensure_minimum_leads(leads, CFG, discover, silent)
        self.assertFalse(rep["enough"])
        self.assertIn("упал", rep["reason"])

    def test_candidates_without_name_skipped(self):
        leads = []
        added = replenish.add_unique(
            leads, [{"name": "", "site": "https://x.ru"}, {"name": "  "}],
            log=silent)
        self.assertEqual(added, 0)
        self.assertEqual(leads, [])

    def test_duplicates_inside_one_batch(self):
        leads = []
        batch = [lead("Alpha", site="https://alpha.ru"),
                 lead("Alpha", site="www.alpha.ru/")]
        added = replenish.add_unique(leads, batch, log=silent)
        self.assertEqual(added, 1)

    def test_defaults_when_no_config(self):
        leads = base(1000)
        rep = replenish.ensure_minimum_leads(leads, {}, lambda l, g: [], silent)
        self.assertTrue(rep["enough"])


if __name__ == "__main__":
    unittest.main()
