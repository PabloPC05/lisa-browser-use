"""Sesión de Chromium real, sin interfaz, contra el sitio de pruebas."""

from __future__ import annotations

import unittest

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
