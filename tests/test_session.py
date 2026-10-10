"""Sesión de Chromium real, sin interfaz, contra el sitio de pruebas."""

from __future__ import annotations

import unittest

from lisa_browser.session import excerpt
from tests.site import Site
from tests.support import launch_browser

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class SessionCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.site = Site().__enter__()
        cls.browser = launch_browser()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.site.__exit__()

    def set_value(self, selector: str, value: str) -> None:
        self.browser._eval(f"document.querySelector({selector!r}).value = {value!r}")


class OpenAndRead(SessionCase):
    def test_open_reports_the_page_and_read_returns_its_text(self) -> None:
        opened = self.browser.open(self.site.url("/horarios"))
        self.assertEqual((opened["ok"], opened["status"], opened["title"]), (True, 200, "Horarios"))
        self.assertEqual(opened["url"], self.site.url("/horarios"))
        read = self.browser.read()
        self.assertIn("abre de 9:00 a 21:00", read["text"])
        self.assertFalse(read["truncated"])

    def test_read_is_cut_at_the_requested_length(self) -> None:
        self.browser.open(self.site.url("/largo"))
        read = self.browser.read(max_chars=100)
        self.assertEqual(len(read["text"]), 100)
        self.assertTrue(read["truncated"])


class ReadWithFind(SessionCase):
    def test_find_returns_only_the_matching_part_of_the_page(self) -> None:
        self.browser.open(self.site.url("/horarios"))
        read = self.browser.read(find="9:00")
        self.assertEqual(read["matches"], 1)
        self.assertIn("abre de 9:00 a 21:00", read["text"])
        self.assertEqual(self.browser.read(find="piscina")["matches"], 0)

    def test_reading_can_continue_where_it_stopped(self) -> None:
        self.browser.open(self.site.url("/largo"))
        first = self.browser.read(max_chars=100)
        second = self.browser.read(max_chars=100, offset=first["next_offset"])
        whole = self.browser.read(max_chars=200)
        self.assertEqual(first["text"] + second["text"], whole["text"])


class Excerpt(unittest.TestCase):
    TEXT = "Portada\n\n\n\nTarifas   \nMatrícula ordinaria: 836,40 €\nPlazo: del 1 al 15\n" + "relleno\n" * 50 + "Teléfono: 881 811 000\nFin"

    def test_blank_runs_are_collapsed(self) -> None:
        self.assertTrue(excerpt(self.TEXT)["text"].startswith("Portada\n\nTarifas\nMatrícula"))

    def test_find_ignores_case_and_accents_and_brings_one_line_of_context(self) -> None:
        found = excerpt(self.TEXT, find="matricula")
        self.assertEqual(found["matches"], 1)
        self.assertEqual(found["text"], "Tarifas\nMatrícula ordinaria: 836,40 €\nPlazo: del 1 al 15")

    def test_several_terms_and_a_mark_between_distant_parts(self) -> None:
        found = excerpt(self.TEXT, find="MATRÍCULA, telefono")
        self.assertEqual(found["matches"], 2)
        self.assertIn("Plazo: del 1 al 15\n…\nrelleno\nTeléfono: 881 811 000\nFin", found["text"])
        self.assertLess(len(found["text"]), 120)

    def test_find_respects_the_size_limit(self) -> None:
        found = excerpt(self.TEXT, find="relleno", max_chars=60)
        self.assertEqual(found["matches"], 50)
        self.assertTrue(found["truncated"])
        self.assertLessEqual(len(found["text"]), 60)

    def test_a_very_long_line_is_cut_around_the_match(self) -> None:
        line = "a " * 2000 + "el importe es 42 euros " + "b " * 2000
        found = excerpt(line, find="importe")
        self.assertIn("el importe es 42 euros", found["text"])
        self.assertLess(len(found["text"]), 700)

    def test_without_find_it_says_where_to_continue(self) -> None:
        first = excerpt("0123456789", max_chars=4)
        self.assertEqual((first["text"], first["truncated"], first["next_offset"], first["total_chars"]), ("0123", True, 4, 10))
        last = excerpt("0123456789", max_chars=4, offset=8)
        self.assertEqual((last["text"], last["truncated"]), ("89", False))
        self.assertNotIn("next_offset", last)


class MissingPage(SessionCase):
    def test_missing_page_is_reported_not_raised(self) -> None:
        opened = self.browser.open(self.site.url("/no-existe"))
        self.assertEqual((opened["ok"], opened["status"], opened["error"]), (False, 404, "HTTP 404"))

    def test_unreachable_server_is_reported_not_raised(self) -> None:
        opened = self.browser.open("http://127.0.0.1:1/")
        self.assertFalse(opened["ok"])
        self.assertIsNone(opened["status"])
        self.assertIn("127.0.0.1:1", opened["error"])


class ScreenshotHidesPassword(SessionCase):
    def test_password_characters_are_not_visible_in_the_screenshot(self) -> None:
        self.browser.open(self.site.url("/login"))
        # Dos contraseñas distintas de la misma longitud se ven igual…
        self.set_value("#clave", "secreto-uno")
        first = self.browser.screenshot()
        self.set_value("#clave", "clave--dos!")
        second = self.browser.screenshot()
        self.assertTrue(first.startswith(PNG_MAGIC))
        self.assertEqual(first, second)
        # …mientras que en un campo normal esos mismos textos sí cambian la imagen.
        self.set_value("#usuario", "secreto-uno")
        third = self.browser.screenshot()
        self.set_value("#usuario", "clave--dos!")
        self.assertNotEqual(third, self.browser.screenshot())


if __name__ == "__main__":
    unittest.main()
