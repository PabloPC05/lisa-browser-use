"""Biblioteca de sitios y rutinas: el almacén, su API y la ejecución de una rutina de punta a punta."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lisa_browser.library import Library, LibraryError, MissingParams, check_steps, key_of, params_of, substitute
from tests.support import ByName
from tests.test_login import PASSWORD, TYPE_AND_ENTER
from tests.test_executor import ServerCase

HORARIOS = [
    {"op": "open", "url": "http://127.0.0.1/"},
    {"op": "task", "goal": "Ir a los horarios", "verification": ["Se ve el horario"]},
    {"op": "read", "find": "abre"},
]


class Store(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "biblioteca.sqlite"
        self.library = Library(self.path)

    def tearDown(self) -> None:
        self.library.close()
        self._tmp.cleanup()

    def test_names_ignore_case_accents_and_spaces(self) -> None:
        self.assertEqual(key_of("  Campus   Virtual "), key_of("campus virtual"))
        self.assertEqual(key_of("Secretaría"), key_of("secretaria"))

    def test_sites_are_saved_replaced_listed_and_deleted(self) -> None:
        self.library.save_site("Campus virtual", "https://cv.usc.es/", "Las asignaturas están en «Mis cursos».", ["cv"])
        self.library.save_site("Renfe", "https://www.renfe.com/es/es")
        # Guardar con el mismo nombre, escrito de otra forma, lo sustituye.
        self.library.save_site("campus  VIRTUAL", "https://cv.usc.es/my/", "Entrar con la cuenta de la USC.")
        self.assertEqual(
            [(s["name"], s["url"], s["notes"]) for s in self.library.sites()],
            [("campus VIRTUAL", "https://cv.usc.es/my/", "Entrar con la cuenta de la USC."), ("Renfe", "https://www.renfe.com/es/es", "")],
        )
        # El más usado va primero: es el orden en que los ve el planificador.
        self.library.visited("https://www.renfe.com/viajar/horarios")
        self.assertEqual([s["name"] for s in self.library.sites()], ["Renfe", "campus VIRTUAL"])
        self.assertTrue(self.library.delete_site("RENFE"))
        self.assertFalse(self.library.delete_site("Renfe"))

    def test_a_site_needs_a_web_address_and_short_notes(self) -> None:
        for url in ("cv.usc.es", "javascript:alert(1)", "file:///etc/passwd", ""):
            with self.assertRaises(LibraryError):
                self.library.save_site("Campus", url)
        with self.assertRaisesRegex(LibraryError, "600 caracteres"):
            self.library.save_site("Campus", "https://cv.usc.es", "x" * 601)
        with self.assertRaises(LibraryError):
            self.library.save_site("", "https://cv.usc.es")

    def test_routine_steps_are_checked_and_stripped_of_unknown_fields(self) -> None:
        clean = check_steps([
            {"op": "open", "url": "https://cv.usc.es/", "contraseña": "no-se-guarda", "read_chars": 0},
            {"op": "task", "goal": "Abrir {asignatura}", "verification": ["Se ve el curso"], "inputs": {"texto": "{asignatura}"}},
        ])
        self.assertEqual(clean[0], {"op": "open", "url": "https://cv.usc.es/", "read_chars": 0})
        self.assertNotIn("contraseña", json.dumps(clean, ensure_ascii=False))
        self.assertEqual(params_of(clean), ["asignatura"])
        for bad in (
            [], "abrir", [{"op": "logout"}], [{"op": "attach", "file": "x"}], [{"op": "open", "url": "cv.usc.es"}],
            [{"op": "follow"}],
            [{"op": "task", "goal": "Sin comprobación"}], [{"op": "task", "goal": "x", "verification": "no es lista"}],
            [{"op": "open", "url": "https://a.test"}] * 21,
        ):
            with self.assertRaises(LibraryError, msg=repr(bad)[:60]):
                check_steps(bad)

    def test_params_are_filled_everywhere_and_missing_ones_are_named(self) -> None:
        steps = check_steps([
            {"op": "open", "url": "https://cv.usc.es/curso/{codigo}"},
            {"op": "task", "goal": "Buscar {tema}", "verification": ["Sale {tema}"], "inputs": {"busqueda": "{tema}"}},
        ])
        filled = substitute(steps, {"codigo": "G1011", "tema": "exámenes"})
        self.assertEqual(filled[0]["url"], "https://cv.usc.es/curso/G1011")
        self.assertEqual(filled[1], {"op": "task", "goal": "Buscar exámenes", "verification": ["Sale exámenes"], "inputs": {"busqueda": "exámenes"}})
        self.assertEqual(steps[0]["url"], "https://cv.usc.es/curso/{codigo}")  # la rutina guardada no cambia
        with self.assertRaises(MissingParams) as raised:
            substitute(steps, {"codigo": "G1011", "tema": ""})
        self.assertEqual(raised.exception.missing, ["tema"])

    def test_routines_keep_their_record_and_survive_a_restart(self) -> None:
        saved = self.library.save_routine("Horario de la biblioteca", "Dice a qué hora abre la biblioteca.", HORARIOS)
        self.assertEqual(saved, {"name": "Horario de la biblioteca", "description": "Dice a qué hora abre la biblioteca.", "params": [], "steps": 3})
        self.library.ran("horario de la BIBLIOTECA", True, "ok")
        self.library.ran("Horario de la biblioteca", False, "NEEDS_AGENT")
        self.library.close()
        self.library = Library(self.path)
        self.assertEqual(
            self.library.routines(),
            [{"name": "Horario de la biblioteca", "description": "Dice a qué hora abre la biblioteca.", "params": [], "steps": 3,
              "runs": 2, "ok": 1, "last_status": "NEEDS_AGENT", "dormant": False}],
        )
        self.assertEqual(self.library.routine("horario de la biblioteca")["steps"], HORARIOS)
        self.assertIsNone(self.library.routine("otra"))
        self.assertTrue(self.library.delete_routine("Horario de la biblioteca"))
        self.assertEqual(self.library.listing(), {"sites": [], "routines": []})


class Ranking(unittest.TestCase):
    """Con muchas cosas guardadas, el planificador lleva pocas y encuentra el resto por lo que se le pide."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.library = Library(Path(self._tmp.name) / "biblioteca.sqlite")
        for n in range(1, 9):
            self.library.save_routine(f"Gestión {n}", f"Hace la gestión número {n} en la sede.", HORARIOS)
            for _ in range(n):
                self.library.ran(f"Gestión {n}", True, "ok")
        self.library.save_routine("Notas del campus", "Dice las notas de una asignatura en el campus virtual.", HORARIOS)
        self.library.save_routine("Factura de la luz", "Descarga la última factura de la compañía eléctrica.", HORARIOS)
        self.library.save_site("Campus virtual", "https://cv.usc.es/", "Asignaturas en Os meus cursos.", ["cv", "Moodle"])
        self.library.save_site("Renfe", "https://www.renfe.com/")

    def tearDown(self) -> None:
        self.library.close()
        self._tmp.cleanup()

    def test_the_short_list_is_the_most_used_and_says_how_many_are_left(self) -> None:
        listing = self.library.listing(limit=3)
        self.assertEqual([r["name"] for r in listing["routines"]], ["Gestión 8", "Gestión 7", "Gestión 6"])
        self.assertEqual(listing["more"], {"sites": 0, "routines": 7})
        self.assertNotIn("matches", listing)
        # La misma petición da siempre la misma lista: no depende de lo que se pregunte.
        self.assertEqual(self.library.listing("la factura de la luz", limit=3)["routines"], listing["routines"])

    def test_what_fits_the_request_is_found_even_if_it_is_rarely_used(self) -> None:
        matches = self.library.listing("¿me bajas la FACTURA de la luz de este mes?", limit=3)["matches"]
        self.assertEqual([r["name"] for r in matches["routines"]], ["Factura de la luz"])
        notas = self.library.listing("qué notas tengo en el campus", limit=3)["matches"]
        self.assertEqual([r["name"] for r in notas["routines"]], ["Notas del campus"])
        # Lo que ya está en la lista corta no se repite, y lo que no encaja no aparece.
        again = self.library.listing("la gestión de la sede", limit=3)
        self.assertFalse({r["name"] for r in again["matches"]["routines"]} & {r["name"] for r in again["routines"]})
        self.assertLessEqual(len(again["matches"]["routines"]), 5)
        self.assertEqual(self.library.listing("receta de lentejas", limit=3)["matches"], {"sites": [], "routines": []})
        # Los sitios también se encuentran por sus otros nombres.
        self.assertEqual([s["name"] for s in self.library.listing("entra en moodle", limit=0)["matches"]["sites"]], ["Campus virtual"])

    def test_a_routine_that_keeps_failing_is_no_longer_shown_but_can_still_be_found(self) -> None:
        for _ in range(2):
            self.library.ran("Gestión 8", False, "ROUTINE_FAILED")
        self.assertIn("Gestión 8", [r["name"] for r in self.library.listing(limit=3)["routines"]])
        self.library.ran("Gestión 8", False, "ROUTINE_FAILED")
        listing = self.library.listing("la gestión 8 de la sede", limit=3)
        self.assertEqual([r["name"] for r in listing["routines"]], ["Gestión 7", "Gestión 6", "Gestión 5"])
        self.assertEqual([(r["name"], r["dormant"]) for r in listing["matches"]["routines"]][:1], [("Gestión 8", True)])
        # En cuanto vuelve a salir bien, se enseña otra vez.
        self.library.ran("Gestión 8", True, "ok")
        self.assertEqual(self.library.listing(limit=1)["routines"][0]["name"], "Gestión 8")

    def test_an_older_library_gains_the_new_column(self) -> None:
        path = Path(self._tmp.name) / "antigua.sqlite"
        import sqlite3
        old = sqlite3.connect(path)
        old.executescript(
            "CREATE TABLE routines (key TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL, params TEXT NOT NULL DEFAULT '[]',"
            " steps TEXT NOT NULL, updated_at TEXT NOT NULL, runs INTEGER NOT NULL DEFAULT 0, ok INTEGER NOT NULL DEFAULT 0,"
            " last_run TEXT, last_status TEXT);"
            "INSERT INTO routines (key, name, description, steps, updated_at) VALUES ('vieja', 'Vieja', 'De antes.', '[]', '2026-10-12');"
        )
        old.commit()
        old.close()
        reopened = Library(path)
        reopened.ran("Vieja", False, "ROUTINE_FAILED")
        self.assertEqual([(r["name"], r["runs"], r["dormant"]) for r in reopened.routines()], [("Vieja", 1, False)])
        reopened.close()


class LibraryCase(ServerCase):
    """Como en producción, la política de decisión es la misma para todos los pasos de una operación."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.library = Library(Path(self._tmp.name) / "biblioteca.sqlite")
        self.executor.library = self.library
        self.policies: dict[str, ByName] = {}

    def policy_for(self, operation) -> ByName:
        if operation.conversation not in self.policies:
            steps, repeat = self.script.get(operation.conversation, ([], False))
            self.policies[operation.conversation] = ByName(list(steps), repeat=repeat)
        return self.policies[operation.conversation]

    def routine(self, *steps: dict) -> list[dict]:
        """Pasos con las direcciones del sitio de pruebas."""
        return [{**s, "url": self.site.url(s["url"], s.pop("host", "127.0.0.1"))} if "url" in s else s for s in map(dict, steps)]


class Routines(LibraryCase):
    async def test_a_routine_does_all_its_steps_in_one_operation(self) -> None:
        self.library.save_routine("Horario", "A qué hora abre.", self.routine(
            {"op": "open", "url": "/"}, {"op": "task", "goal": "Ir a los horarios", "verification": ["Se ve el horario"]},
            {"op": "read", "find": "abre"},
        ))
        self.steps("c1", ("CLICK", "Horarios"), ("SUBTASK_COMPLETE",))
        events = await self.op("routine", {"name": "horario"})

        self.assertEqual(
            [(e["action"], e["target"]) for e in events if e["type"] == "step"],
            [("rutina 1/3: open", self.site.url("/")), ("rutina 2/3: task", "Ir a los horarios"), ("CLICK", "Horarios"),
             ("rutina 3/3: read", "abre")],
        )
        result = self.done(events)
        self.assertEqual((result["status"], result["routine"], result["steps"]), ("ok", "Horario", 3))
        self.assertEqual((result["url"], result["title"]), (self.site.url("/horarios"), "Horarios"))
        self.assertIn("9:00 a 21:00", result["text"])
        self.assertLess(len(json.dumps(result)), 900)
        self.assertEqual([(r["runs"], r["ok"], r["last_status"]) for r in self.library.routines()], [(1, 1, "ok")])
        # En el registro es una sola operación, con las acciones que decidió JEV.
        journal = self.journal()
        self.assertEqual([(o["op"], o["goal"], o["status"], o["steps"]) for o in journal], [("routine", "horario", "ok", 1)])

    async def test_params_are_asked_for_and_then_used(self) -> None:
        self.library.save_routine("Buscar en horarios", "Busca una palabra en los horarios.", self.routine(
            {"op": "open", "url": "/horarios", "find": "{palabra}", "read_chars": 0},
        ))
        missing = self.done(await self.op("routine", {"name": "Buscar en horarios"}))
        self.assertEqual(missing, {"status": "NEEDS_INPUT", "routine": "Buscar en horarios", "missing": ["palabra"]})
        self.assertEqual(self.site.requests, [])  # sin el dato no se abre nada
        found = self.done(await self.op("routine", {"name": "Buscar en horarios", "params": {"palabra": "21:00"}}))
        self.assertEqual((found["status"], found["matches"]), ("ok", 1))

    async def test_a_routine_logs_in_with_the_vault_and_skips_it_when_already_inside(self) -> None:
        self.library.save_routine("Mi carné", "Abre el carné de socio.", self.routine(
            {"op": "open", "url": "/login", "host": "socios.test", "read_chars": 0}, {"op": "login"}, {"op": "read", "find": "Hola"},
        ))
        self.steps("c1", *TYPE_AND_ENTER, ("SUBTASK_COMPLETE",))
        events = await self.op("routine", {"name": "Mi carné"})
        self.assertEqual(self.done(events)["status"], "ok")
        self.assertIn("Hola, Pablo", self.done(events)["text"])
        self.assertNotIn(PASSWORD, json.dumps(events))
        self.assertNotIn(PASSWORD, self.dump())

        # Una página que no pide contraseña: la sesión sigue abierta y el paso de entrar se salta.
        self.library.save_routine("Mi carné directo", "Abre el carné con la sesión ya iniciada.", self.routine(
            {"op": "open", "url": "/dentro", "host": "socios.test", "read_chars": 0}, {"op": "login"}, {"op": "read", "find": "Hola"},
        ))
        self.policies["c2"] = ByName([])  # si se intentara decidir algo, fallaría
        again = self.done(await self.op("routine", {"name": "Mi carné directo"}, conversation="c2"))
        self.assertEqual((again["status"], self.policies["c2"].calls), ("ok", 0))

    async def test_a_failed_step_stops_the_routine_and_says_where(self) -> None:
        self.library.save_routine("Horario", "A qué hora abre.", self.routine(
            {"op": "open", "url": "/"}, {"op": "task", "goal": "Ir a los horarios", "verification": ["Se ve el horario"]},
            {"op": "open", "url": "/contador"},
        ))
        self.steps("c1", ("NEEDS_AGENT",))
        result = self.done(await self.op("routine", {"name": "Horario"}))
        self.assertEqual((result["status"], result["failed_step"], result["of"]), ("ROUTINE_FAILED", 2, 3))
        self.assertEqual(result["step"], {"op": "task", "goal": "Ir a los horarios"})
        self.assertEqual(result["result"]["status"], "NEEDS_AGENT")
        self.assertEqual(result["url"], self.site.url("/"))
        # El paso siguiente no se llegó a hacer, y la rutina queda anotada como fallida.
        self.assertNotIn("/contador", [path for _, path, _ in self.site.requests])
        self.assertEqual([(r["runs"], r["ok"], r["last_status"]) for r in self.library.routines()], [(1, 0, "NEEDS_AGENT")])

        missing_page = self.routine({"op": "open", "url": "/no-existe"}, {"op": "read"})
        self.library.save_routine("Rota", "Abre una página que ya no existe.", missing_page)
        broken = self.done(await self.op("routine", {"name": "Rota"}))
        self.assertEqual((broken["status"], broken["failed_step"]), ("ROUTINE_FAILED", 1))

    async def test_actions_with_effect_still_ask_for_approval_inside_a_routine(self) -> None:
        self.library.save_routine("Saludar", "Envía un saludo por el formulario de contacto.", self.routine(
            {"op": "open", "url": "/contacto", "read_chars": 0},
            {"op": "task", "goal": "Enviar el mensaje", "verification": ["Enviado"], "inputs": {"texto": "{mensaje}"}},
        ))
        script = (("TYPE_TEXT", "Mensaje", "texto"), ("CLICK", "Enviar"), ("SUBTASK_COMPLETE",))
        self.steps("c1", *script)
        denied = await self.op("routine", {"name": "Saludar", "params": {"mensaje": "Hola"}}, answers=["deny"])
        approval = next(e for e in denied if e["type"] == "approval")
        self.assertEqual((approval["category"], approval["fields"]), ("enviar", [["Mensaje", "Hola"]]))
        self.assertEqual((self.done(denied)["status"], self.done(denied)["result"]["status"]), ("ROUTINE_FAILED", "DENIED"))
        self.assertEqual(self.site.posts("/enviado"), [])

        self.steps("c2", *script)
        allowed = await self.op("routine", {"name": "Saludar", "params": {"mensaje": "Hola"}}, conversation="c2", answers=["once"])
        self.assertEqual(self.done(allowed)["status"], "ok")
        self.assertEqual(len(self.site.posts("/enviado")), 1)

    async def test_a_routine_can_follow_links_by_name_without_the_decision_model(self) -> None:
        self.library.save_routine("Horario", "A qué hora abre.", self.routine(
            {"op": "open", "url": "/"}, {"op": "follow", "text": "{enlace}", "find": "abre"},
        ))
        self.policies["c1"] = ByName([])  # si se pidiera una decisión, fallaría
        events = await self.op("routine", {"name": "Horario", "params": {"enlace": "horarios"}})
        result = self.done(events)
        self.assertEqual((result["status"], result["url"], self.policies["c1"].calls), ("ok", self.site.url("/horarios"), 0))
        self.assertIn("9:00 a 21:00", result["text"])
        self.assertEqual([e["action"] for e in events if e["type"] == "step"], ["rutina 1/2: open", "rutina 2/2: follow"])

        gone = self.done(await self.op("routine", {"name": "Horario", "params": {"enlace": "piscina"}}))
        self.assertEqual((gone["status"], gone["failed_step"], gone["result"]["error"]), ("ROUTINE_FAILED", 2, "no_link"))

    async def test_follow_and_links_as_operations(self) -> None:
        opened = self.done(await self.op("open", {"url": self.site.url("/"), "read_chars": 0, "links": True}))
        self.assertEqual(
            opened["links"], [{"text": "Horarios", "url": self.site.url("/horarios")}, {"text": "Zona de socios", "url": self.site.url("/login")}],
        )
        self.assertEqual(self.done(await self.op("read", {"find": "socios", "links": True}))["links"], [{"text": "Zona de socios", "url": self.site.url("/login")}])
        followed = self.done(await self.op("follow", {"text": "Horarios", "find": "abre"}))
        self.assertEqual((followed["ok"], followed["followed"], followed["matches"]), (True, "Horarios", 1))
        self.assertFalse(self.done(await self.op("follow", {"text": "Piscina"}))["ok"])

    async def test_unknown_routine_lists_the_known_ones(self) -> None:
        self.library.save_routine("Horario", "A qué hora abre.", HORARIOS)
        self.assertEqual(self.done(await self.op("routine", {"name": "Notas"})), {"status": "no_routine", "name": "Notas", "routines": ["Horario"]})

    async def test_opening_a_saved_site_counts_as_a_visit(self) -> None:
        self.library.save_site("Biblioteca", self.site.url("/"))
        self.library.save_site("Otro", "https://otro.test/")
        await self.open("/horarios")
        self.assertEqual([(s["name"], s["uses"]) for s in self.library.sites()], [("Biblioteca", 1), ("Otro", 0)])


if __name__ == "__main__":
    unittest.main()
