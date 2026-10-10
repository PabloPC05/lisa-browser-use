"""Registro de operaciones del navegador."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from lisa_browser.journal import Journal

SECRET = "s3creta-muy-larga"


class JournalCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "registro.sqlite"
        self.journal = Journal(self.path)

    def tearDown(self) -> None:
        self.journal.close()
        self._tmp.cleanup()


class Metrics(JournalCase):
    def test_steps_time_and_cost_are_recorded(self) -> None:
        op = self.journal.start("c_1", "task", "Ver el horario")
        self.journal.step(op, 1, "CLICK", "link", "Horarios", 950, "jev")
        self.journal.step(op, 2, "CLICK", "button", "Aceptar", 1050, "jev")
        self.journal.finish(op, "SUBTASK_COMPLETE", 3.2, input_tokens=900, cost_usd=0.000038, sites=["usc.gal"])
        [entry] = self.journal.summary()
        self.assertEqual((entry["status"], entry["steps"], entry["seconds"]), ("SUBTASK_COMPLETE", 2, 3.2))
        self.assertEqual((entry["input_tokens"], entry["cost_usd"]), (900, 0.000038))
        self.assertEqual(entry["decided_by"], {"jev": {"steps": 2, "avg_ms": 1000}})
        self.assertEqual(entry["sites"], ["usc.gal"])

    def test_summary_can_start_from_a_date(self) -> None:
        self.journal.finish(self.journal.start(None, "open", None), "ok", 0.1)
        self.assertEqual(len(self.journal.summary("2000-01-01")), 1)
        self.assertEqual(self.journal.summary("2999-01-01"), [])


class ListsEffects(JournalCase):
    def test_effects_are_listed_with_their_approval(self) -> None:
        op = self.journal.start("c_1", "task", "Publicar un anuncio")
        self.journal.effect(op, "enviar", "ejemplo.es", "CLICK Publicar", "once")
        self.journal.effect(op, "borrar", "ejemplo.es", "CLICK Eliminar", "deny")
        self.journal.finish(op, "DENIED", 12.0, sites=["ejemplo.es"])
        effects = self.journal.summary()[0]["effects"]
        self.assertEqual(
            [(e["category"], e["domain"], e["action"], e["decision"]) for e in effects],
            [("enviar", "ejemplo.es", "CLICK Publicar", "once"), ("borrar", "ejemplo.es", "CLICK Eliminar", "deny")],
        )


class NoSecrets(JournalCase):
    def test_secret_values_never_reach_the_file(self) -> None:
        op = self.journal.start("c_1", "login", f"Entrar con {SECRET}", secrets=[SECRET])
        self.journal.step(op, 1, "TYPE_TEXT", "textbox", f"Campo con {SECRET}", 900, "jev")
        self.journal.effect(op, "enviar", "ejemplo.es", f"CLICK Enviar {SECRET}", "once")
        self.journal.finish(op, "ok", 1.0)
        self.journal.close()
        raw = sqlite3.connect(self.path)
        dump = "\n".join(raw.iterdump())
        raw.close()
        self.journal = Journal(self.path)
        self.assertNotIn(SECRET, dump)
        self.assertIn("[secret]", dump)


if __name__ == "__main__":
    unittest.main()
