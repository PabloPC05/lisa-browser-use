"""Tareas de punta a punta en el sitio de pruebas: Chromium real y política fijada en lugar de JEV."""

from __future__ import annotations

import unittest

from lisa_browser.runner import run_subtask
from tests.site import PAGES, Site, page
from tests.support import ByName, launch_browser

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

TASK_PAGES = {
    **PAGES,
    "/contador": page(
        "Contador",
        "<h1>Contador</h1><p id='n'>Veces: 0</p><button id='b'>Otra vez</button>"
        "<script>let n = 0; b.onclick = () => { n += 1; document.getElementById('n').textContent = 'Veces: ' + n; };</script>",
    ),
    "/envio": page(
        "Calcular envío",
        "<h1>Calcular envío</h1><label>Código postal <input id='cp' type='text'></label>"
        "<button id='calc' type='button'>Calcular</button><p id='r'></p>"
        "<script>calc.onclick = () => { r.textContent = 'Envío a ' + cp.value + ': 3,90 €'; };</script>",
    ),
    "/captcha": page(
        "Comprobación",
        "<h1>Demuestra que no eres un robot</h1><p>Selecciona todas las imágenes con semáforos.</p>"
        "<div><img alt='' width='80' height='80'><img alt='' width='80' height='80'><img alt='' width='80' height='80'></div>",
    ),
}


class TaskCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.site = Site(TASK_PAGES).__enter__()
        cls.browser = launch_browser()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.site.__exit__()

    def run_steps(self, steps, **payload):
        policy = ByName(steps, repeat=payload.pop("repeat", False))
        payload.setdefault("goal", "Tarea de prueba")
        payload.setdefault("verification", ["La tarea de prueba está hecha"])
        return run_subtask(self.browser.backend, policy, payload)


class PublicLookup(TaskCase):
    def test_navigates_and_the_answer_can_be_read_with_its_address(self) -> None:
        self.assertTrue(self.browser.open(self.site.url("/"))["ok"])
        result = self.run_steps([("CLICK", "Horarios"), ("SUBTASK_COMPLETE",)])
        self.assertEqual((result["status"], result["actions_taken"]), ("SUBTASK_COMPLETE", 1))
        read = self.browser.read()
        self.assertEqual(read["url"], self.site.url("/horarios"))
        self.assertIn("abre de 9:00 a 21:00", read["text"])
        self.assertTrue(self.browser.screenshot().startswith(PNG_MAGIC))


class MissingPage(TaskCase):
    def test_missing_page_gives_a_failure_and_no_data(self) -> None:
        opened = self.browser.open(self.site.url("/horarios-de-verano"))
        self.assertFalse(opened["ok"])
        self.assertEqual(opened["error"], "HTTP 404")
        self.assertNotIn("9:00", self.browser.read()["text"])


class StepLimit(TaskCase):
    def test_a_task_that_never_finishes_stops_at_the_limit(self) -> None:
        self.browser.open(self.site.url("/contador"))
        result = self.run_steps([("CLICK", "Otra vez")], repeat=True, max_actions=3)
        self.assertEqual((result["status"], result["actions_taken"]), ("LIMIT", 3))
        self.assertIn("Veces: 3", self.browser.read()["text"])


class NeedsInputThenContinues(TaskCase):
    def test_asks_for_the_missing_value_and_continues_with_it(self) -> None:
        self.browser.open(self.site.url("/envio"))
        first = self.run_steps([("NEEDS_INPUT", "Código postal")])
        self.assertEqual(first["status"], "NEEDS_INPUT")
        self.assertEqual(first["needs_input"]["name"], "Código postal")
        second = self.run_steps(
            [("TYPE_TEXT", "Código postal", "cp"), ("CLICK", "Calcular"), ("SUBTASK_COMPLETE",)],
            inputs={"cp": "15705"},
        )
        self.assertEqual(second["status"], "SUBTASK_COMPLETE")
        self.assertIn("Envío a 15705: 3,90 €", self.browser.read()["text"])


class ImageCaptchaBlocks(TaskCase):
    def test_an_image_captcha_ends_as_blocked_with_a_screenshot_available(self) -> None:
        self.browser.open(self.site.url("/captcha"))
        result = self.run_steps([("BLOCKED",)])
        self.assertEqual((result["status"], result["actions_taken"]), ("BLOCKED", 0))
        self.assertTrue(self.browser.screenshot().startswith(PNG_MAGIC))


if __name__ == "__main__":
    unittest.main()
