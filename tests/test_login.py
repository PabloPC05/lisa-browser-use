"""Inicio de sesión con el baúl y segundo factor, con Chromium real sobre el sitio de pruebas."""

from __future__ import annotations

import json
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path

from arc_cua.policies import ChoicePolicy

from lisa_browser.journal import Journal
from lisa_browser.login import login
from lisa_browser.vault import BitwardenCliVault, Entry, FakeVault, VaultUnavailable, registrable_domain
from tests.site import Site, page
from tests.support import ByName, launch_browser

USER = "pablo"
PASSWORD = "correcta-Horse-42"
CODE = "123456"

_VERIFY = (
    "document.body.innerHTML = \"<h1>Verificación</h1><form id='g'><label>Código de verificación "
    "<input id='codigo' autocomplete='one-time-code'></label><button type='submit'>Verificar</button></form><p id='m2'></p>\";"
    "window.codigos = 0;"
    "g.addEventListener('submit', (ev) => { ev.preventDefault(); window.codigos += 1;"
    " if (codigo.value === '%s') { location.href = '/dentro'; } else { m2.textContent = 'Código incorrecto'; codigo.value = ''; } });"
) % CODE


def login_page(after_password: str) -> str:
    return page(
        "Zona de socios",
        "<h1>Zona de socios</h1><form id='f'><label>Usuario <input id='usuario' type='text'></label>"
        "<label>Contraseña <input id='clave' type='password'></label><button type='submit'>Entrar</button></form>"
        "<p id='msg'></p><script>window.intentos = 0;"
        "f.addEventListener('submit', (ev) => { ev.preventDefault(); window.intentos += 1;"
        f" if (usuario.value === '{USER}' && clave.value === '{PASSWORD}') {{ {after_password} }}"
        " else { msg.textContent = 'Usuario o contraseña incorrectos'; clave.value = ''; } });</script>",
    )


LOGIN_PAGES = {
    "/login": login_page("location.href = '/dentro';"),
    "/login-codigo": login_page(_VERIFY),
    "/dentro": page("Tu carné", "<h1>Hola, Pablo</h1><p>Tu carné caduca el 31 de diciembre.</p>"),
    "/portada": page("Portada", "<h1>Portada</h1><p>Sin formulario.</p>"),
}

ENTRY = Entry("e1", "Biblioteca Socios", USER, PASSWORD, ("http://socios.test/login",))
TYPE_AND_ENTER = [("TYPE_TEXT", "Usuario", "usuario"), ("TYPE_TEXT", "Contraseña", "contraseña"), ("CLICK", "Entrar")]
TYPE_CODE = [("TYPE_TEXT", "Código de verificación", "codigo"), ("CLICK", "Verificar")]


class LoginCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.site = Site(LOGIN_PAGES).__enter__()
        cls.browser = launch_browser()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.site.__exit__()

    def setUp(self) -> None:
        self.code_requests: list[tuple[str, float]] = []
        self.codes: list[str | None] = [CODE]
        self.secrets: list[str] = []
        self.steps: list[dict] = []

    def ask_code(self, domain: str, timeout: float) -> str | None:
        self.code_requests.append((domain, timeout))
        return self.codes.pop(0) if self.codes else None

    def login(self, path: str, steps, vault=None, host: str = "socios.test", **options):
        self.assertTrue(self.browser.open(self.site.url(path, host))["ok"])
        return login(
            self.browser, ByName(steps), vault if vault is not None else FakeVault([ENTRY]),
            ask_code=self.ask_code, on_secret=self.secrets.append, on_step=self.steps.append, **options,
        )

    def where(self) -> str:
        return self.browser.read()["url"].rsplit("/", 1)[-1]


class LogsIn(LoginCase):
    def test_logs_in_with_the_vault_entry_of_the_page_domain(self) -> None:
        result = self.login("/login", TYPE_AND_ENTER + [("SUBTASK_COMPLETE",)])
        self.assertEqual(result, {"status": "ok", "domain": "socios.test"})
        self.assertEqual(self.where(), "dentro")
        self.assertEqual(self.code_requests, [])


class SeveralAccounts(LoginCase):
    OTHER = Entry("e2", "Biblioteca (cuenta antigua)", "13579@socios.test", "otra-clave-0000", ("http://socios.test/",))

    def vault(self) -> FakeVault:
        return FakeVault([self.OTHER, ENTRY])

    def test_with_several_accounts_nothing_is_typed_until_one_is_chosen(self) -> None:
        self.site.requests.clear()
        result = self.login("/login", TYPE_AND_ENTER, vault=self.vault())
        self.assertEqual((result["status"], result["domain"]), ("choose_account", "socios.test"))
        self.assertEqual(
            result["accounts"],
            [{"name": self.OTHER.name, "username": self.OTHER.username}, {"name": ENTRY.name, "username": USER}],
        )
        self.assertNotIn("clave", json.dumps(result))
        self.assertEqual((self.where(), self.steps, self.secrets), ("login", [], []))
        self.assertEqual(self.site.posts(), [])

    def test_the_chosen_account_is_the_one_used(self) -> None:
        result = self.login("/login", TYPE_AND_ENTER + [("SUBTASK_COMPLETE",)], vault=self.vault(), account=USER[:4].upper())
        self.assertEqual(result, {"status": "ok", "domain": "socios.test"})
        # La contraseña que se usó es la de la cuenta elegida, no la de la primera entrada.
        self.assertEqual(self.secrets, [PASSWORD])
        self.assertEqual(self.where(), "dentro")

    def test_a_hint_that_fits_several_accounts_still_asks(self) -> None:
        result = self.login("/login", TYPE_AND_ENTER, vault=self.vault(), account="biblioteca")
        self.assertEqual((result["status"], len(result["accounts"])), ("choose_account", 2))
        self.assertEqual(self.secrets, [])


class SecretNeverLeaves(LoginCase):
    def test_password_is_not_in_results_steps_or_what_the_model_would_receive(self) -> None:
        result = self.login("/login", TYPE_AND_ENTER[:2] + [("NEEDS_AGENT",)])
        self.assertNotIn(PASSWORD, json.dumps([result, self.steps]))
        self.assertEqual(self.secrets, [PASSWORD])  # quien registra recibe el valor solo para poder ocultarlo

        # Lo que arc-cua enviaría a JEV ahora, con la contraseña ya escrita en la página:
        sent: list[dict] = []

        class Recorder:
            name = "grabadora"
            supports_images = False

            def ask(self, state, questions, *, images=()):
                sent.append({"state": state, "questions": questions})
                return {}

        from arc_cua import subtask_from_dict

        subtask = subtask_from_dict({
            "goal": "Iniciar sesión", "verification": ["Sesión iniciada"],
            "inputs": {"usuario": USER, "contraseña": PASSWORD}, "secret_inputs": ["usuario", "contraseña"],
        })
        try:  # la grabadora no contesta nada útil; solo interesa la petición
            ChoicePolicy(Recorder()).decide(subtask=subtask, snapshot=self.browser.backend.observe(), history=[])
        except Exception:
            pass
        self.assertGreaterEqual(len(sent), 1)
        self.assertNotIn(PASSWORD, json.dumps(sent, ensure_ascii=False))
        self.assertIn("[secret]", json.dumps(sent, ensure_ascii=False))


class WrongDomain(LoginCase):
    def test_lookalike_domain_gets_no_password_and_a_warning(self) -> None:
        result = self.login("/login", TYPE_AND_ENTER, host="socios-acceso.test")
        self.assertEqual(result, {"status": "wrong_domain", "domain": "socios-acceso.test", "expected": "socios.test"})
        self.assertEqual(self.browser._eval("document.querySelector('#clave').value"), "")
        self.assertEqual(self.browser._eval("window.intentos"), 0)
        self.assertEqual(self.secrets, [])

    def test_unknown_site_has_no_credentials(self) -> None:
        result = self.login("/login", TYPE_AND_ENTER, host="otra-cosa.test")
        self.assertEqual(result, {"status": "no_credentials", "domain": "otra-cosa.test"})
        result = self.login("/portada", [], host="socios-acceso.test")
        self.assertEqual(result["status"], "no_credentials")


class SingleAttempt(LoginCase):
    def test_rejected_password_is_tried_only_once(self) -> None:
        wrong = FakeVault([Entry("e1", "Biblioteca Socios", USER, "equivocada-000", ("http://socios.test",))])
        result = self.login("/login", TYPE_AND_ENTER + TYPE_AND_ENTER + [("SUBTASK_COMPLETE",)], vault=wrong)
        self.assertEqual(result, {"status": "rejected", "domain": "socios.test", "reason": "password"})
        self.assertEqual(self.browser._eval("window.intentos"), 1)


class CodeFromChat(LoginCase):
    def test_code_is_asked_once_and_used(self) -> None:
        steps = TYPE_AND_ENTER + [("NEEDS_INPUT", "Código de verificación")] + TYPE_CODE + [("SUBTASK_COMPLETE",)]
        result = self.login("/login-codigo", steps)
        self.assertEqual(result, {"status": "ok", "domain": "socios.test"})
        self.assertEqual(self.code_requests, [("socios.test", 300)])
        self.assertEqual(self.where(), "dentro")
        self.assertNotIn(CODE, json.dumps([result, self.steps]))

    def test_wrong_code_is_asked_again_once_and_then_gives_up(self) -> None:
        self.codes = ["000000", "111111", CODE]
        steps = TYPE_AND_ENTER + [("NEEDS_INPUT", "Código de verificación")] + (TYPE_CODE + [("NEEDS_AGENT",)]) * 2
        result = self.login("/login-codigo", steps)
        self.assertEqual(result, {"status": "rejected", "domain": "socios.test", "reason": "code"})
        self.assertEqual(len(self.code_requests), 2)
        self.assertEqual(self.browser._eval("window.codigos"), 2)


class CodeFromVault(LoginCase):
    def test_vault_generator_is_used_without_asking(self) -> None:
        entry = Entry("e1", "Biblioteca Socios", USER, PASSWORD, ("http://socios.test",), has_totp=True)
        vault = FakeVault([entry], codes={"e1": CODE})
        steps = TYPE_AND_ENTER + [("NEEDS_INPUT", "Código de verificación")] + TYPE_CODE + [("SUBTASK_COMPLETE",)]
        result = self.login("/login-codigo", steps, vault=vault)
        self.assertEqual(result["status"], "ok")
        self.assertEqual((self.code_requests, vault.totp_calls), ([], 1))


class CodeTimeout(LoginCase):
    def test_no_answer_within_five_minutes_ends_the_login(self) -> None:
        self.codes = []  # Pablo no contesta: ask_code devuelve None al agotar su plazo
        result = self.login("/login-codigo", TYPE_AND_ENTER + [("NEEDS_INPUT", "Código de verificación")])
        self.assertEqual(result, {"status": "rejected", "domain": "socios.test", "reason": "code_timeout"})
        self.assertEqual(self.code_requests, [("socios.test", 300)])
        self.assertEqual(self.browser._eval("window.codigos"), 0)


class CodeNotInJournal(LoginCase):
    def test_password_and_code_never_reach_the_journal_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "registro.sqlite"
            journal = Journal(path)
            op = journal.start("c_1", "login", "Iniciar sesión en socios.test")
            self.assertTrue(self.browser.open(self.site.url("/login-codigo", "socios.test"))["ok"])
            steps = TYPE_AND_ENTER + [("NEEDS_INPUT", "Código de verificación")] + TYPE_CODE + [("SUBTASK_COMPLETE",)]
            known: list[str] = []

            def on_secret(value: str) -> None:
                known.append(value)
                journal.add_secret(op, value)

            result = login(
                self.browser, ByName(steps), FakeVault([ENTRY]), ask_code=self.ask_code, on_secret=on_secret,
                # Peor caso: un registro que anotara, en cada paso, todos los secretos conocidos hasta entonces.
                on_step=lambda s: journal.step(op, s["n"], s["action"], None, f"{s['target']} {' '.join(known)}", s["ms"], "regla"),
            )
            self.assertEqual(known, [PASSWORD, CODE])
            journal.finish(op, result["status"], 1.0, sites=[result["domain"]])
            journal.close()
            raw = sqlite3.connect(path)
            dump = "\n".join(raw.iterdump())
            raw.close()
        self.assertEqual(result["status"], "ok")
        self.assertNotIn(PASSWORD, dump)
        self.assertNotIn(CODE, dump)
        self.assertIn("[secret]", dump)


class VaultDown(LoginCase):
    def test_unavailable_vault_is_reported(self) -> None:
        result = self.login("/login", TYPE_AND_ENTER, vault=FakeVault(broken=True))
        self.assertEqual(result["status"], "vault_unavailable")
        self.assertEqual(self.browser._eval("window.intentos"), 0)


class Domains(unittest.TestCase):
    def test_registrable_domain(self) -> None:
        cases = {
            "https://aulas.usc.gal/login": "usc.gal", "https://www.usc.gal": "usc.gal", "http://tienda.ejemplo.co.uk/x": "ejemplo.co.uk",
            "http://127.0.0.1:8080/": "127.0.0.1", "http://socios.test:9000/login": "socios.test", "usc.gal": "usc.gal",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(registrable_domain(url), expected)

    def test_entry_only_matches_its_own_domain(self) -> None:
        entry = Entry("e", "USC", "u", "p", ("https://cas.usc.gal/login",))
        self.assertTrue(entry.matches("https://aulas.usc.gal/curso"))
        self.assertFalse(entry.matches("https://usc.gal.acceso-seguro.com/login"))
        self.assertFalse(entry.matches("https://usc-gal.com"))

    def test_entry_repr_hides_the_password(self) -> None:
        self.assertNotIn("valor-visible", repr(Entry("e", "USC", "u", "valor-visible")))


FAKE_BW = r"""#!/usr/bin/env bash
# bw de pruebas: anota sus argumentos y responde como la línea de comandos de Bitwarden.
echo "$*" >> "$FAKE_BW_LOG"
case "$1 $2" in
  "status "*) echo '{"status":"unauthenticated"}' ;;
  "config server") ;;
  "login --apikey") [ "$BW_CLIENTID" = "user.abc" ] && [ "$BW_CLIENTSECRET" = "s3cr3t-api" ] || exit 1 ;;
  "unlock --passwordenv") [ "$BW_PASSWORD" = "maestra-de-prueba" ] && printf 'SESION-123' || exit 1 ;;
  "sync "*) [ "$BW_SESSION" = "SESION-123" ] || exit 1 ;;
  "list items") [ "$BW_SESSION" = "SESION-123" ] || exit 1
    echo '[{"id":"i1","type":1,"name":"USC","login":{"username":"pablo","password":"pw-usc","totp":"otpauth://x","uris":[{"uri":"https://cas.usc.gal/login"}]}},
           {"id":"i2","type":1,"name":"Otro","login":{"username":"x","password":"pw-otro","uris":[{"uri":"https://otro.example"}]}},
           {"id":"i3","type":2,"name":"Nota segura"}]' ;;
  "get totp") echo "654321" ;;
  *) exit 2 ;;
esac
"""


class BitwardenCli(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.log = tmp / "bw.log"
        self.bw = tmp / "bw"
        self.bw.write_text(FAKE_BW)
        self.bw.chmod(self.bw.stat().st_mode | stat.S_IXUSR)
        self.addCleanup(self._tmp.cleanup)

    def vault(self, master: str = "maestra-de-prueba") -> BitwardenCliVault:
        return BitwardenCliVault(
            server="https://lisa.example", client_id="user.abc", client_secret="s3cr3t-api",
            master_password=master, data_dir=self._tmp.name, bw=str(self.bw),
        )

    def test_finds_entries_for_the_site_and_reads_the_code(self) -> None:
        vault = self.vault()
        vault._bw_call = _with_log_env(vault._bw_call, self.log)
        [entry] = vault.find("https://aulas.usc.gal/curso")
        self.assertEqual((entry.username, entry.password, entry.has_totp), ("pablo", "pw-usc", True))
        self.assertEqual(vault.totp(entry), "654321")
        calls = self.log.read_text()
        self.assertIn("config server https://lisa.example", calls)
        self.assertIn("list items --url https://aulas.usc.gal/curso", calls)
        # Ningún secreto viaja como argumento de programa.
        for secret in ("maestra-de-prueba", "s3cr3t-api", "SESION-123"):
            self.assertNotIn(secret, calls)

    def test_wrong_master_password_is_unavailable_without_leaking_it(self) -> None:
        vault = self.vault(master="equivocada")
        vault._bw_call = _with_log_env(vault._bw_call, self.log)
        with self.assertRaises(VaultUnavailable) as caught:
            vault.find("https://aulas.usc.gal")
        self.assertNotIn("equivocada", str(caught.exception))


def _with_log_env(call, log: Path):
    """El bw de pruebas necesita saber dónde anotar; el real no recibe nada más que sus secretos."""

    def wrapped(*args, env=None):
        return call(*args, env={**(env or {}), "FAKE_BW_LOG": str(log)})

    return wrapped


if __name__ == "__main__":
    unittest.main()
