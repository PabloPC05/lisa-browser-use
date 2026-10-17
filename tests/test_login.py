"""Inicio de sesión con el baúl y segundo factor, con Chromium real sobre el sitio de pruebas."""

from __future__ import annotations

import base64
import json
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path

from arc_cua.policies import ChoicePolicy

from lisa_browser.journal import Journal
from lisa_browser.login import login, save_password
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


# Como Instagram: tras la contraseña, la web pasa un momento después a otra página que pide el código,
# y el campo solo lo dice en su etiqueta.
_CODE_ELSEWHERE = page(
    "Revisa tu correo",
    "<h1>Revisa tu correo</h1><form id='g'><label>Código <input id='c1' name='email' type='text'></label>"
    "<button type='submit'>Continuar</button></form><p id='m2'></p><script>window.codigos = 0;"
    "g.addEventListener('submit', (ev) => { ev.preventDefault(); window.codigos += 1;"
    f" if (c1.value === '{CODE}') {{ location.href = '/dentro'; }} else {{ m2.textContent = 'Código incorrecto'; }} }});</script>",
)

LOGIN_PAGES = {
    "/login": login_page("location.href = '/dentro';"),
    "/login-codigo-aparte": login_page("setTimeout(() => { location.href = '/codigo-aparte'; }, 1500);"),
    "/codigo-aparte": _CODE_ELSEWHERE,
    "/login-postal": login_page("location.href = '/perfil';"),
    "/perfil": page("Tu perfil", "<h1>Tu perfil</h1><label>Código postal <input id='cp' type='text'></label>"),
    "/login-codigo": login_page(_VERIFY),
    "/login-codigo-sin-marca": login_page(_VERIFY.replace("autocomplete='one-time-code'", "name='code'")),
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

    def test_code_field_without_autocomplete_is_recognised_by_its_name(self) -> None:
        # Como Amazon: tras la contraseña el paso se da por completo y el campo no lleva autocomplete.
        steps = TYPE_AND_ENTER + [("SUBTASK_COMPLETE",)] + TYPE_CODE + [("SUBTASK_COMPLETE",)]
        result = self.login("/login-codigo-sin-marca", steps)
        self.assertEqual(result, {"status": "ok", "domain": "socios.test"})
        self.assertEqual(self.code_requests, [("socios.test", 300)])
        self.assertEqual(self.where(), "dentro")

    def test_wrong_code_is_asked_again_once_and_then_gives_up(self) -> None:
        self.codes = ["000000", "111111", CODE]
        steps = TYPE_AND_ENTER + [("NEEDS_INPUT", "Código de verificación")] + (TYPE_CODE + [("NEEDS_AGENT",)]) * 2
        result = self.login("/login-codigo", steps)
        self.assertEqual(result, {"status": "rejected", "domain": "socios.test", "reason": "code"})
        self.assertEqual(len(self.code_requests), 2)
        self.assertEqual(self.browser._eval("window.codigos"), 2)


class CodeOnAnotherPage(LoginCase):
    TYPE_CODE_HERE = [("TYPE_TEXT", "Código", "codigo"), ("CLICK", "Continuar")]

    def test_a_code_page_that_arrives_later_is_not_taken_for_a_session(self) -> None:
        steps = TYPE_AND_ENTER + [("SUBTASK_COMPLETE",)] + self.TYPE_CODE_HERE + [("SUBTASK_COMPLETE",)]
        result = self.login("/login-codigo-aparte", steps)
        self.assertEqual(result, {"status": "ok", "domain": "socios.test"})
        self.assertEqual(self.code_requests, [("socios.test", 300)])
        self.assertEqual(self.where(), "dentro")

    def test_on_the_code_page_the_code_is_asked_without_typing_the_password(self) -> None:
        result = self.login("/codigo-aparte", self.TYPE_CODE_HERE + [("SUBTASK_COMPLETE",)])
        self.assertEqual(result, {"status": "ok", "domain": "socios.test"})
        self.assertEqual(self.secrets, [CODE])
        self.assertEqual(self.where(), "dentro")

    def test_a_postal_code_is_not_a_second_factor(self) -> None:
        result = self.login("/login-postal", TYPE_AND_ENTER + [("SUBTASK_COMPLETE",)])
        self.assertEqual(result, {"status": "ok", "domain": "socios.test"})
        self.assertEqual(self.code_requests, [])


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


class SavesNewPassword(LoginCase):
    """La entrada del baúl tiene una contraseña que ya no vale y Pablo da la nueva por el chat."""

    def setUp(self) -> None:
        super().setUp()
        self.vault = FakeVault([Entry("e1", "Biblioteca Socios", USER, "caducada-000", ("http://socios.test",))])
        self.password_requests: list[tuple[str, str, float]] = []
        self.new_password: str | None = PASSWORD

    def ask_password(self, domain: str, username: str, timeout: float) -> str | None:
        self.password_requests.append((domain, username, timeout))
        return self.new_password

    def save(self, path: str, steps, **options):
        self.assertTrue(self.browser.open(self.site.url(path, "socios.test"))["ok"])
        return save_password(
            self.browser, ByName(steps), self.vault, ask_password=self.ask_password, ask_code=self.ask_code,
            on_secret=self.secrets.append, on_step=self.steps.append, **options,
        )

    def test_a_password_that_works_is_saved_in_the_entry(self) -> None:
        result = self.save("/login", TYPE_AND_ENTER + [("SUBTASK_COMPLETE",)])
        self.assertEqual(result, {"status": "ok", "domain": "socios.test", "saved": True})
        self.assertEqual(self.password_requests, [("socios.test", USER, 300)])
        self.assertEqual(self.vault.entries[0].password, PASSWORD)
        self.assertEqual(self.where(), "dentro")
        # La contraseña nueva se trata como la del baúl: quien registra la recibe para ocultarla y no sale en nada más.
        self.assertEqual(self.secrets, [PASSWORD])
        self.assertNotIn(PASSWORD, json.dumps([result, self.steps]))

    def test_a_password_that_does_not_work_is_tried_once_and_not_saved(self) -> None:
        self.new_password = "tampoco-es-111"
        result = self.save("/login", TYPE_AND_ENTER + TYPE_AND_ENTER + [("SUBTASK_COMPLETE",)])
        self.assertEqual(result, {"status": "rejected", "domain": "socios.test", "reason": "password"})
        self.assertEqual(self.browser._eval("window.intentos"), 1)
        self.assertEqual(self.vault.entries[0].password, "caducada-000")

    def test_without_an_answer_nothing_is_typed_or_saved(self) -> None:
        self.new_password = None
        result = self.save("/login", TYPE_AND_ENTER)
        self.assertEqual(result, {"status": "rejected", "domain": "socios.test", "reason": "password_timeout"})
        self.assertEqual((self.browser._eval("window.intentos"), self.steps), (0, []))
        self.assertEqual(self.vault.entries[0].password, "caducada-000")

    def test_with_several_accounts_it_asks_which_before_asking_for_the_password(self) -> None:
        self.vault.entries.append(Entry("e2", "Otra", "13579@socios.test", "otra-clave-0000", ("http://socios.test/",)))
        result = self.save("/login", TYPE_AND_ENTER)
        self.assertEqual(result["status"], "choose_account")
        self.assertEqual(self.password_requests, [])

    def test_a_site_without_entry_is_not_created(self) -> None:
        self.vault.entries.clear()
        result = self.save("/login", TYPE_AND_ENTER)
        self.assertEqual(result, {"status": "no_credentials", "domain": "socios.test"})
        self.assertEqual((self.password_requests, self.vault.entries), ([], []))

    def test_a_vault_that_fails_to_save_is_reported_with_the_session_open(self) -> None:
        def broken(entry, password):
            raise VaultUnavailable("bw edit terminó con código 1")

        self.vault.set_password = broken
        result = self.save("/login", TYPE_AND_ENTER + [("SUBTASK_COMPLETE",)])
        self.assertEqual(
            result, {"status": "ok", "domain": "socios.test", "saved": False, "reason": "bw edit terminó con código 1"},
        )


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
  "get item") [ "$BW_SESSION" = "SESION-123" ] || exit 1
    echo '{"id":"i1","type":1,"name":"USC","notes":"no tocar","login":{"username":"pablo","password":"pw-usc","totp":"otpauth://x","uris":[{"uri":"https://cas.usc.gal/login"}]}}' ;;
  "edit item") [ "$BW_SESSION" = "SESION-123" ] || exit 1
    cat > "$FAKE_BW_LOG.stdin" ;;
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

    def test_saves_a_new_password_keeping_the_rest_of_the_entry(self) -> None:
        vault = self.vault()
        vault._bw_call = _with_log_env(vault._bw_call, self.log)
        [entry] = vault.find("https://aulas.usc.gal/curso")
        vault.set_password(entry, "nueva-Clave-77")
        calls = self.log.read_text()
        self.assertIn("get item i1", calls)
        self.assertIn("edit item i1 --nointeraction", calls)
        # La contraseña nueva no viaja como argumento de programa, ni en claro ni codificada.
        sent = Path(f"{self.log}.stdin").read_text()
        self.assertNotIn("nueva-Clave-77", calls)
        self.assertNotIn(sent, calls)
        saved = json.loads(base64.b64decode(sent))
        self.assertEqual(saved["login"]["password"], "nueva-Clave-77")
        self.assertEqual(
            (saved["notes"], saved["login"]["username"], saved["login"]["totp"], saved["login"]["uris"]),
            ("no tocar", "pablo", "otpauth://x", [{"uri": "https://cas.usc.gal/login"}]),
        )

    def test_wrong_master_password_is_unavailable_without_leaking_it(self) -> None:
        vault = self.vault(master="equivocada")
        vault._bw_call = _with_log_env(vault._bw_call, self.log)
        with self.assertRaises(VaultUnavailable) as caught:
            vault.find("https://aulas.usc.gal")
        self.assertNotIn("equivocada", str(caught.exception))


def _with_log_env(call, log: Path):
    """El bw de pruebas necesita saber dónde anotar; el real no recibe nada más que sus secretos."""

    def wrapped(*args, env=None, stdin=None):
        return call(*args, env={**(env or {}), "FAKE_BW_LOG": str(log)}, stdin=stdin)

    return wrapped


if __name__ == "__main__":
    unittest.main()


class ClaveSharesItsPassword(LoginCase):
    """Cl@ve Permanente: la contraseña guardada para clave.gob.es vale donde Cl@ve la pide."""

    def test_entry_matches_every_domain_of_the_clave_group_and_no_other(self) -> None:
        entry = Entry("c1", "Cl@ve Permanente", "00000000T", PASSWORD, ("https://pasarela.clave.gob.es/",))
        for url in ("https://pasarela.clave.gob.es/x", "https://clave-dninbrt.seg-social.gob.es/login",
                    "https://www1.agenciatributaria.gob.es/y", "https://w2.seg-social.es/z"):
            self.assertTrue(entry.matches(url), url)
        for url in ("https://clave.gob.es.example.com/", "https://sede.dgt.gob.es/", "https://evil-seg-social.gob.es.com/"):
            self.assertFalse(entry.matches(url), url)

    def test_logs_in_on_another_domain_of_the_group(self) -> None:
        from lisa_browser import vault as vault_module

        groups = vault_module.EQUIVALENT_DOMAINS
        vault_module.EQUIVALENT_DOMAINS = (frozenset({"pasarela.test", "formulario.test"}),)
        self.addCleanup(setattr, vault_module, "EQUIVALENT_DOMAINS", groups)
        entry = Entry("c1", "Cl@ve de pruebas", USER, PASSWORD, ("http://pasarela.test/",))
        result = self.login("/login", TYPE_AND_ENTER + [("SUBTASK_COMPLETE",)], vault=FakeVault([entry]), host="formulario.test")
        self.assertEqual(result["status"], "ok")

    def test_bitwarden_looks_for_the_entry_in_each_domain_of_the_group(self) -> None:
        bw = BitwardenCliVault(server="https://x", client_id="i", client_secret="s", master_password="m", data_dir="/tmp")
        asked: list[str] = []
        entry = Entry("c1", "Cl@ve", "00000000T", PASSWORD, ("https://pasarela.clave.gob.es/",))

        def items(flag: str, value: str) -> list[Entry]:
            asked.append(value)
            return [entry] if "clave.gob.es" in value else []

        bw._items = items  # type: ignore[method-assign]
        found = bw.find("https://clave-dninbrt.seg-social.gob.es/login")
        self.assertEqual([e.id for e in found], ["c1"])
        self.assertIn("https://clave.gob.es/", asked)
        self.assertEqual(bw.find("https://sede.dgt.gob.es/"), [])
