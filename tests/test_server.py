"""API del ejecutor de punta a punta: HTTP real, Chromium real, política fijada y baúl ficticio."""

from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from lisa_browser.files import Files
from lisa_browser.journal import Journal
from lisa_browser.runner import Limits
from lisa_browser.server import Executor, Settings, make_app
from lisa_browser.session import Browser
from lisa_browser.vault import Entry, FakeVault
from tests.site import Site, page
from tests.support import ByName, launch_browser
from tests.test_files import FILE_PAGES, PDF, PHOTO
from tests.test_gate import GATE_PAGES
from tests.test_login import CODE, LOGIN_PAGES, PASSWORD, TYPE_AND_ENTER, TYPE_CODE, USER
from tests.test_tasks import TASK_PAGES

TOKEN = "token-de-prueba-abcdef"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
PAGES = {
    **TASK_PAGES, **GATE_PAGES, **FILE_PAGES, **LOGIN_PAGES,
    "/dentro": page("Tu carné", "<h1>Hola, Pablo</h1><script>document.cookie = 'sesion=1; path=/; max-age=3600'</script>"),
}
ENTRY = Entry("e1", "Biblioteca Socios", USER, PASSWORD, ("http://socios.test/login",))


class ServerCase(unittest.IsolatedAsyncioTestCase):
    settings: dict = {}

    @classmethod
    def setUpClass(cls) -> None:
        cls.site = Site(PAGES).__enter__()
        cls.browser = launch_browser()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.site.__exit__()

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        for name in ("descargas", "subidas"):
            (tmp / name).mkdir()
        self.journal_path = tmp / "registro.sqlite"
        self.script: dict[str, tuple[list, bool]] = {}  # conversación -> (pasos, repetir)
        self.site.requests.clear()
        self.executor = Executor(
            settings=Settings(token=TOKEN, **self.settings),
            connect=self.connect,
            policy_for=self.policy_for,
            journal=Journal(self.journal_path),
            files_for=lambda browser: Files(browser, tmp / "descargas", tmp / "subidas"),
            uploads_dir=tmp / "subidas",
            vault=FakeVault([ENTRY]),
        )
        self.client = TestClient(TestServer(make_app(self.executor)))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self._tmp.cleanup()

    def connect(self):
        return self.browser

    def policy_for(self, operation) -> ByName:
        steps, repeat = self.script.get(operation.conversation, ([], False))
        return ByName(list(steps), repeat=repeat)

    def steps(self, conversation: str, *steps: tuple, repeat: bool = False) -> None:
        self.script[conversation] = (list(steps), repeat)

    async def op(self, op: str, args: dict | None = None, *, conversation: str = "c1", answers: list | None = None,
                 on_event=None) -> list[dict]:
        """Lanza una operación, contesta a lo que pregunte con `answers` y devuelve todos sus eventos."""
        events: list[dict] = []
        body = {"conversation": conversation, "op": op, "args": args or {}}
        async with self.client.post("/v1/operations", json=body, headers=AUTH) as response:
            self.assertEqual(response.status, 200)
            operation = None
            async for raw in response.content:
                event = json.loads(raw)
                events.append(event)
                if event["type"] == "started":
                    operation = event["operation"]
                if on_event is not None:
                    await on_event(operation, event)
                if event["type"] in ("approval", "secret") and answers:
                    value = answers.pop(0)
                    if isinstance(value, float):  # espera antes de contestar
                        await asyncio.sleep(value)
                        value = answers.pop(0)
                    await self.client.post(
                        f"/v1/operations/{operation}/answers", json={"event": event["event"], "value": value}, headers=AUTH,
                    )
        return events

    @staticmethod
    def done(events: list[dict]) -> dict:
        return events[-1]["result"]

    async def open(self, path: str, host: str = "127.0.0.1") -> None:
        self.assertTrue(self.done(await self.op("open", {"url": self.site.url(path, host)}))["ok"])

    def dump(self) -> str:
        raw = sqlite3.connect(self.journal_path)
        text = "\n".join(raw.iterdump())
        raw.close()
        return text


class Access(ServerCase):
    async def test_requests_without_the_token_are_refused(self) -> None:
        for headers in ({}, {"Authorization": "Bearer otro"}, {"Authorization": TOKEN}):
            response = await self.client.post("/v1/operations", json={"op": "read"}, headers=headers)
            self.assertEqual(response.status, 401)
        self.assertEqual((await self.client.get("/v1/journal")).status, 401)

    async def test_unknown_operation_is_rejected(self) -> None:
        response = await self.client.post("/v1/operations", json={"op": "formatear"}, headers=AUTH)
        self.assertEqual(response.status, 400)


class Tasks(ServerCase):
    async def test_open_task_and_read(self) -> None:
        await self.open("/")
        self.steps("c1", ("CLICK", "Horarios"), ("SUBTASK_COMPLETE",))
        events = await self.op("task", {"goal": "Ver horarios", "verification": ["Se ve el horario"]})
        self.assertEqual([e["type"] for e in events], ["started", "step", "done"])
        self.assertEqual((events[1]["action"], events[1]["target"]), ("CLICK", "Horarios"))
        self.assertEqual(self.done(events)["status"], "SUBTASK_COMPLETE")
        self.assertIn("9:00 a 21:00", self.done(await self.op("read"))["text"])

    async def test_open_already_returns_the_start_of_the_text(self) -> None:
        opened = self.done(await self.op("open", {"url": self.site.url("/horarios")}))
        self.assertIn("9:00 a 21:00", opened["text"])
        silent = self.done(await self.op("open", {"url": self.site.url("/horarios"), "read_chars": 0}))
        self.assertNotIn("text", silent)
        found = self.done(await self.op("open", {"url": self.site.url("/horarios"), "read_chars": 0, "find": "21:00"}))
        self.assertEqual(found["matches"], 1)
        missing = self.done(await self.op("open", {"url": self.site.url("/no-existe")}))
        self.assertEqual((missing["ok"], "text" in missing), (False, False))

    async def test_task_result_is_brief_and_can_bring_what_is_looked_for(self) -> None:
        await self.open("/")
        self.steps("c1", ("CLICK", "Horarios"), ("SUBTASK_COMPLETE",))
        result = self.done(await self.op("task", {"goal": "Ver horarios", "verification": ["x"], "find": "abre"}))
        # Dónde ha quedado la página, no su lista de elementos.
        self.assertEqual(set(result["page"]), {"url", "title", "elements", "more_below"})
        self.assertEqual((result["page"]["title"], result["page"]["url"]), ("Horarios", self.site.url("/horarios")))
        self.assertIsInstance(result["page"]["elements"], int)
        self.assertEqual(result["matches"], 1)
        self.assertIn("9:00 a 21:00", result["text"])
        self.assertLess(len(json.dumps(result)), 900)

    async def test_read_with_find_and_offset(self) -> None:
        await self.open("/horarios")
        self.assertEqual(self.done(await self.op("read", {"find": "piscina"}))["matches"], 0)
        whole = self.done(await self.op("read"))["text"]
        tail = self.done(await self.op("read", {"offset": 5, "max_chars": 10}))["text"]
        self.assertEqual(tail, whole[5:15])

    async def test_every_operation_is_in_the_journal(self) -> None:
        await self.open("/")
        self.steps("c1", ("CLICK", "Horarios"), ("SUBTASK_COMPLETE",))
        await self.op("task", {"goal": "Ver horarios", "verification": ["x"]})
        listed = (await (await self.client.get("/v1/journal", headers=AUTH)).json())["operations"]
        self.assertEqual([(o["op"], o["status"], o["steps"]) for o in listed], [("open", "200", 0), ("task", "SUBTASK_COMPLETE", 1)])
        self.assertEqual(listed[1]["sites"], ["127.0.0.1"])
        self.assertEqual(self.done(await self.op("log"))["operations"][1]["goal"], "Ver horarios")

    async def test_cancel_stops_a_running_task(self) -> None:
        await self.open("/contador")
        self.steps("c1", ("CLICK", "Otra vez"), repeat=True)

        async def cancel_on_first_step(operation, event):
            if event["type"] == "step" and event["n"] == 1:
                await self.client.post(f"/v1/operations/{operation}/cancel", headers=AUTH)

        events = await self.op("task", {"goal": "No acabar", "verification": ["x"], "max_actions": 40}, on_event=cancel_on_first_step)
        self.assertEqual(self.done(events)["status"], "CANCELLED")
        self.assertLess(self.done(events)["actions_taken"], 10)

    async def test_screenshot_is_an_event_not_part_of_the_result(self) -> None:
        await self.open("/")
        events = await self.op("screenshot")
        self.assertTrue(base64.b64decode(events[1]["png"]).startswith(b"\x89PNG"))
        self.assertEqual(self.done(events), {"status": "ok", "sent": True})


class Approvals(ServerCase):
    SEND = {"goal": "Enviar el mensaje", "verification": ["Enviado"], "inputs": {"texto": "Hola"}}
    STEPS = (("TYPE_TEXT", "Mensaje", "texto"), ("CLICK", "Enviar"), ("SUBTASK_COMPLETE",))

    async def test_approval_round_trip(self) -> None:
        await self.open("/contacto")
        self.steps("c1", *self.STEPS)
        events = await self.op("task", self.SEND, answers=["once"])
        approval = next(e for e in events if e["type"] == "approval")
        self.assertEqual((approval["category"], approval["domain"], approval["fields"]), ("enviar", "127.0.0.1", [["Mensaje", "Hola"]]))
        self.assertTrue(base64.b64decode(approval["png"]).startswith(b"\x89PNG"))
        self.assertEqual(self.done(events)["status"], "SUBTASK_COMPLETE")
        self.assertEqual(len(self.site.posts("/enviado")), 1)
        journal = (await (await self.client.get("/v1/journal", headers=AUTH)).json())["operations"]
        self.assertEqual([(e["category"], e["decision"]) for e in journal[-1]["effects"]], [("enviar", "once")])

    async def test_deny(self) -> None:
        await self.open("/contacto")
        self.steps("c1", *self.STEPS)
        events = await self.op("task", self.SEND, answers=["deny"])
        self.assertEqual(self.done(events)["status"], "DENIED")
        self.assertEqual(self.site.posts(), [])


class ApprovalTimes(ServerCase):
    settings = {"approval_seconds": 0.4}

    async def test_unanswered_approval_expires_as_denied(self) -> None:
        await self.open("/contacto")
        self.steps("c1", *Approvals.STEPS)
        events = await self.op("task", Approvals.SEND)
        self.assertEqual(self.done(events)["status"], "DENIED")
        self.assertEqual(self.site.posts(), [])
        journal = (await (await self.client.get("/v1/journal", headers=AUTH)).json())["operations"]
        self.assertEqual(journal[-1]["effects"][0]["decision"], "expired")



class WaitsAreNotCounted(ServerCase):
    settings = {"limits": Limits(operation_seconds=5.0)}

    async def test_waiting_for_pablo_does_not_count_against_the_time_limit(self) -> None:
        await self.open("/contacto")
        self.steps("c1", *Approvals.STEPS)
        # La operación tiene 5 s de trabajo; Pablo tarda 6 s en aprobar. No debe acabar en LIMIT.
        events = await self.op("task", Approvals.SEND, answers=[6.0, "once"])
        self.assertEqual(self.done(events)["status"], "SUBTASK_COMPLETE")
        self.assertGreater(self.done(events)["metrics"]["seconds"], 6)

    async def test_the_limit_still_applies_to_the_work_itself(self) -> None:
        self.executor.settings = Settings(token=TOKEN, limits=Limits(operation_seconds=0.5))
        await self.open("/contador")
        self.steps("c1", ("CLICK", "Otra vez"), repeat=True)
        events = await self.op("task", {"goal": "No acabar", "verification": ["x"], "max_actions": 40})
        self.assertEqual(self.done(events)["status"], "LIMIT")
        self.assertLess(self.done(events)["actions_taken"], 40)


class Queue(ServerCase):
    async def test_second_operation_waits_for_the_first(self) -> None:
        await self.open("/contacto")
        self.steps("c1", *Approvals.STEPS)
        release = asyncio.Event()
        first_asked = asyncio.Event()

        async def hold(operation, event):
            if event["type"] == "approval":
                first_asked.set()
                await release.wait()

        first = asyncio.create_task(self.op("task", Approvals.SEND, answers=["once"], on_event=hold))
        await first_asked.wait()
        second = asyncio.create_task(self.op("read", conversation="c2"))
        await asyncio.sleep(0.3)
        self.assertFalse(second.done())
        release.set()
        self.assertEqual(self.done(await first)["status"], "SUBTASK_COMPLETE")
        events = await second
        self.assertEqual([e["type"] for e in events], ["started", "queued", "done"])
        self.assertEqual(events[1]["position"], 1)


class Logins(ServerCase):
    async def test_login_with_code_then_sessions_and_logout(self) -> None:
        await self.open("/login-codigo", "socios.test")
        self.steps("c1", *TYPE_AND_ENTER, ("NEEDS_INPUT", "Código de verificación"), *TYPE_CODE, ("SUBTASK_COMPLETE",))
        events = await self.op("login", answers=[CODE])
        secret = next(e for e in events if e["type"] == "secret")
        self.assertEqual((secret["kind"], secret["domain"]), ("code", "socios.test"))
        self.assertEqual(self.done(events)["status"], "ok")
        # Ni la contraseña ni el código salen del ejecutor ni quedan en el registro.
        self.assertNotIn(PASSWORD, json.dumps(events))
        self.assertNotIn(CODE, json.dumps(events))
        dump = self.dump()
        self.assertNotIn(PASSWORD, dump)
        self.assertNotIn(CODE, dump)

        self.assertEqual([s["domain"] for s in self.done(await self.op("sessions"))["sessions"]], ["socios.test"])
        self.assertEqual(self.done(await self.op("logout", {"domain": "socios.test"}))["closed"], ["socios.test"])
        self.assertEqual(self.done(await self.op("sessions"))["sessions"], [])

    async def test_save_password_asks_pablo_tries_it_and_updates_the_vault(self) -> None:
        vault = self.executor._vault
        vault.entries[:] = [Entry("e1", "Biblioteca Socios", USER, "caducada-000", ("http://socios.test/login",))]
        await self.open("/login", "socios.test")
        self.steps("c1", *TYPE_AND_ENTER, ("SUBTASK_COMPLETE",))
        events = await self.op("save_password", answers=[PASSWORD])
        secret = next(e for e in events if e["type"] == "secret")
        self.assertEqual((secret["kind"], secret["domain"], secret["username"]), ("password", "socios.test", USER))
        self.assertEqual((self.done(events)["status"], self.done(events)["saved"]), ("ok", True))
        self.assertEqual(vault.entries[0].password, PASSWORD)
        self.assertNotIn(PASSWORD, json.dumps(events))
        self.assertNotIn(PASSWORD, self.dump())
        self.assertEqual([s["domain"] for s in self.done(await self.op("sessions"))["sessions"]], ["socios.test"])
        await self.op("logout", {"domain": "socios.test"})

    async def test_unanswered_code_ends_the_login(self) -> None:
        self.executor.settings = Settings(token=TOKEN, code_seconds=0.3)
        await self.open("/login-codigo", "socios.test")
        self.steps("c1", *TYPE_AND_ENTER, ("NEEDS_INPUT", "Código de verificación"))
        events = await self.op("login")
        self.assertEqual((self.done(events)["status"], self.done(events)["reason"]), ("rejected", "code_timeout"))


class FilesOverHttp(ServerCase):
    async def test_upload_then_attach(self) -> None:
        response = await self.client.put("/v1/uploads/bici.png", data=PHOTO, headers=AUTH)
        self.assertEqual((await response.json())["bytes"], len(PHOTO))
        self.assertEqual((await self.client.put("/v1/uploads/..%2Fx", data=b"x", headers=AUTH)).status, 400)
        await self.open("/anuncio")
        attached = self.done(await self.op("attach", {"file": "bici.png", "field": "foto"}))
        self.assertEqual(attached["status"], "ok")

    async def test_download_is_announced_and_can_be_fetched(self) -> None:
        await self.open("/facturas")
        self.steps("c1", ("CLICK", "Descargar factura de marzo"), ("SUBTASK_COMPLETE",))
        events = await self.op("task", {"goal": "Descargar la factura", "verification": ["Descargada"]})
        download = next(e for e in events if e["type"] == "download")
        self.assertEqual((download["name"], download["bytes"]), ("factura-marzo.pdf", len(PDF)))
        self.assertEqual(self.done(events)["downloads"][0]["id"], download["id"])
        fetched = await self.client.get(f"/v1/downloads/{download['id']}", headers=AUTH)
        self.assertEqual(await fetched.read(), PDF)
        self.assertEqual((await self.client.get("/v1/downloads/d_nada", headers=AUTH)).status, 404)


class LostConnection(ServerCase):
    def connect(self):
        return Browser.connect("http://" + self.browser.backend._process.ws_url.split("/")[2])

    async def test_a_dropped_connection_is_rebuilt_before_the_operation_and_the_page_is_kept(self) -> None:
        await self.open("/horarios")
        self.executor._browser.backend._conn.close()  # lo que pasa tras un rato sin uso
        events = await self.op("read")
        self.assertIn("9:00 a 21:00", self.done(events)["text"])
        self.assertEqual(self.done(events)["title"], "Horarios")


class BrowserDown(ServerCase):
    def connect(self):
        raise OSError("sin contenedor")

    async def test_missing_browser_is_reported_and_nothing_else_is_tried(self) -> None:
        events = await self.op("open", {"url": self.site.url("/")})
        self.assertEqual(self.done(events), {"status": "ERROR", "reason": "browser_unavailable"})


if __name__ == "__main__":
    unittest.main()
