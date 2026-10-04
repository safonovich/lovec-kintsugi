"""Тесты фильтров источника Serper. Сеть не трогаем."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kt import serper_source as s  # noqa: E402


def silent(*a, **kw):
    pass


class TestSiteFilter(unittest.TestCase):

    def test_aggregators_rejected(self):
        for url in ["https://www.cian.ru/company/488537/",
                    "https://2gis.ru/moscow/search/x",
                    "https://hh.ru/employer/123",
                    "https://vk.com/agency",
                    "https://www.avito.ru/moskva",
                    "https://ru.wikipedia.org/wiki/X",
                    "https://vc.ru/life/123"]:
            self.assertFalse(s._is_company_site(url), url)

    def test_company_sites_accepted(self):
        for url in ["https://kre.ru/contacts",
                    "http://www.slrealty.ru/",
                    "https://kaskad-n.ru"]:
            self.assertTrue(s._is_company_site(url), url)

    def test_garbage_rejected(self):
        self.assertFalse(s._is_company_site(""))
        self.assertFalse(s._is_company_site("localhost"))


class TestEmailPicking(unittest.TestCase):

    def test_hoster_and_placeholder_dropped(self):
        bad = ["support@beget.com", "ivanov@example.com", "noreply@x.ru",
               "logo@2x.png", "a@sentry.io"]
        self.assertIsNone(s._pick_email(bad, "x.ru"))

    def test_own_domain_wins(self):
        got = s._pick_email(["manager@gmail.com", "info@alpha.ru"], "alpha.ru")
        self.assertEqual(got, "info@alpha.ru")

    def test_role_address_preferred(self):
        got = s._pick_email(
            ["petrov@alpha.ru", "info@alpha.ru"], "alpha.ru")
        self.assertEqual(got, "info@alpha.ru")

    def test_falls_back_to_any_usable(self):
        got = s._pick_email(["petrov@alpha.ru"], "alpha.ru")
        self.assertEqual(got, "petrov@alpha.ru")

    def test_empty(self):
        self.assertIsNone(s._pick_email([], "alpha.ru"))


class TestNoKeyNoCrash(unittest.TestCase):

    def test_search_without_key_returns_empty(self):
        import os
        saved = os.environ.pop("SERPER_API_KEY", None)
        try:
            self.assertEqual(s.search_once("что угодно", silent), [])
        finally:
            if saved is not None:
                os.environ["SERPER_API_KEY"] = saved

    def test_find_leads_without_queries(self):
        self.assertEqual(s.find_leads(10, {"search": {"queries": []}}, silent),
                         [])


class TestDomain(unittest.TestCase):

    def test_variants(self):
        for u in ["https://Alpha.RU/path", "http://www.alpha.ru",
                  "alpha.ru:443"]:
            self.assertEqual(s._domain(u), "alpha.ru")


if __name__ == "__main__":
    unittest.main()
