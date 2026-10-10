"""Sesiones guardadas en el navegador: listar y cerrar por sitio."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lisa_browser.files import Files
from lisa_browser.journal import Journal
from tests.site import Site, page
from tests.support import launch_browser

PAGES = {
    "/": page("Portada", "<h1>Portada</h1>"),
    "/entrar": page("Entrar", """
      <form>
        <label>Usuario <input id="usuario" name="usuario" placeholder="tu usuario"></label>
        <label>Contraseña <input id="clave" name="clave" type="password" placeholder="tu contraseña"></label>
        <label>Código <input id="codigo" name="codigo" autocomplete="one-time-code" placeholder="6 cifras"></label>
      </form>"""),
}
SET_SESSION = "document.cookie = 'sesion=abc; path=/; max-age=3600'; localStorage.setItem('token', 'xyz')"


class SessionsCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.site = Site(PAGES).__enter__()
        cls.browser = launch_browser()
        cls._tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmp.name)
        (tmp / "d").mkdir()
        (tmp / "s").mkdir()
        cls.files = Files(cls.browser, tmp / "d", tmp / "s")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.site.__exit__()
        cls._tmp.cleanup()

    def visit(self, host: str) -> None:
        self.assertTrue(self.browser.open(self.site.url("/", host))["ok"])

    def start_session(self, host: str) -> None:
        self.visit(host)
        self.browser._eval(SET_SESSION)

    def state(self, host: str) -> tuple[str, str | None]:
        self.visit(host)
        return self.browser._eval("document.cookie"), self.browser._eval("localStorage.getItem('token')")


class LogoutClearsCookies(SessionsCase):
    def setUp(self) -> None:
        self.files.logout("all")
        self.start_session("socios.test")
        self.start_session("banco.test")

    def test_sites_with_cookies_are_listed(self) -> None:
        self.assertEqual(self.files.cookie_domains(), ["banco.test", "socios.test"])

    def test_closing_one_site_leaves_the_others(self) -> None:
        self.visit("socios.test")
        self.assertEqual(self.files.logout("socios.test"), ["socios.test"])
        self.assertEqual(self.state("socios.test"), ("", None))
        self.assertEqual(self.state("banco.test"), ("sesion=abc", "xyz"))
        self.assertEqual(self.files.cookie_domains(), ["banco.test"])

    def test_closing_everything(self) -> None:
        self.assertEqual(self.files.logout("all"), ["banco.test", "socios.test"])
        self.assertEqual(self.files.cookie_domains(), [])
        self.assertEqual(self.state("banco.test")[0], "")

    def test_closing_a_site_without_session_closes_nothing(self) -> None:
        self.assertEqual(self.files.logout("otro.test"), [])
        self.assertEqual(self.files.cookie_domains(), ["banco.test", "socios.test"])


class ScreenshotHidesPassword(SessionsCase):
    def shot(self, **values: str) -> bytes:
        self.assertTrue(self.browser.open(self.site.url("/entrar", "socios.test"))["ok"])
        for field, value in values.items():
            self.browser._eval(f"document.getElementById({field!r}).value = {value!r}")
        return self.browser.screenshot()

    def test_screenshot_is_the_same_with_or_without_a_password_or_code(self) -> None:
        empty = self.shot()
        self.assertEqual(self.shot(clave="Tr0ub4dor&3-muy-larga"), empty)
        self.assertEqual(self.shot(codigo="482913"), empty)
        # La comparación distingue de verdad: un campo normal sí cambia la captura.
        self.assertNotEqual(self.shot(usuario="pablo"), empty)

    def test_the_page_is_left_as_it_was(self) -> None:
        self.shot(clave="secreta")
        self.assertEqual(self.browser._eval("document.adoptedStyleSheets.length"), 0)
        color = self.browser._eval("getComputedStyle(document.getElementById('clave')).color")
        self.assertNotIn("0, 0, 0, 0", color)


class SessionList(unittest.TestCase):
    def test_journal_remembers_open_sessions_until_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            journal = Journal(Path(tmp) / "r.sqlite")
            journal.session_opened("usc.gal")
            journal.session_opened("usc.gal")
            journal.session_opened("vinted.es")
            self.assertEqual([s["domain"] for s in journal.sessions()], ["usc.gal", "vinted.es"])
            journal.session_closed("usc.gal")
            self.assertEqual([s["domain"] for s in journal.sessions()], ["vinted.es"])
            journal.session_closed()
            self.assertEqual(journal.sessions(), [])
            journal.close()


if __name__ == "__main__":
    unittest.main()
