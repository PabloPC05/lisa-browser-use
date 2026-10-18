"""MCP del navegador: herramientas, formularios a Pablo y resultados, sobre un ejecutor ficticio."""

from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from unittest import mock

import httpx
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from lisa_browser.library import Library
from lisa_browser import mcp_server
from lisa_browser.mcp_server import BearerAuth, BrowserMcp, Rules
from lisa_browser.executor import Operation, Settings


class FakeExecutor:
    """Hace lo que diga `script` con cada operación, como el ejecutor real: en un hilo y con eventos."""

    def __init__(self, tmp: Path, script: Callable[[Operation], dict[str, Any]]) -> None:
        self.settings = Settings(approval_seconds=30, code_seconds=30)
        self.uploads_dir = tmp / "subidas"
        self.uploads_dir.mkdir()
        self.library = Library(tmp / "biblioteca.sqlite")
        self.script = script
        self.ran: list[tuple[str, dict]] = []
        self.downloads: dict[str, Path] = {}

    def run(self, operation: Operation) -> None:
        self.ran.append((operation.op, dict(operation.args)))
        operation.emit("done", result=self.script(operation))

    def download_path(self, download_id: str) -> Path | None:
        return self.downloads.get(download_id)

    def saved_downloads(self, limit: int = 30) -> list[dict]:
        return [{"file": "2026-10-17_120000_factura.pdf", "name": "factura.pdf", "bytes": 3, "from": "https://ejemplo.es/"}][:limit]


PNG = base64.b64encode(b"PNG-captura-de-prueba").decode()


def ask_approval(category: str) -> Callable[[Operation], dict[str, Any]]:
    def script(operation: Operation) -> dict[str, Any]:
        answer = operation.ask("approval", 20, category=category, domain="ejemplo.es", action="CLICK Enviar",
                               fields=[["Nombre", "Ana"]], png=PNG)
        return {"status": "SUBTASK_COMPLETE" if answer == "once" else "DENIED", "answer": answer}
    return script


def ask_secret(kind: str) -> Callable[[Operation], dict[str, Any]]:
    def script(operation: Operation) -> dict[str, Any]:
        return {"status": "ok", "got": operation.ask("secret", 20, kind=kind, domain="ejemplo.es", username="ana")}
    return script


class McpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.forms: list[str] = []

    def make(self, script: Callable[[Operation], dict[str, Any]]) -> tuple[FakeExecutor, BrowserMcp]:
        executor = FakeExecutor(self.tmp, script)
        self.addCleanup(executor.library.close)
        return executor, BrowserMcp(executor, Rules(self.tmp / "reglas.json"))  # type: ignore[arg-type]

    def answering(self, content: dict[str, Any] | None, action: str = "accept") -> Any:
        async def callback(context: Any, params: types.ElicitRequestParams) -> types.ElicitResult:
            self.forms.append(params.message)
            return types.ElicitResult(action=action, content=content)
        return callback

    async def call(self, browser: BrowserMcp, tool: str, args: dict[str, Any], callback: Any = None) -> types.CallToolResult:
        async with create_connected_server_and_client_session(
            browser.mcp._mcp_server, elicitation_callback=callback,
        ) as client:
            return await client.call_tool(tool, args)

    @staticmethod
    def data(result: types.CallToolResult) -> dict[str, Any]:
        return json.loads(result.content[0].text)

    async def test_open_sends_only_given_args_and_drops_metrics(self) -> None:
        executor, browser = self.make(lambda op: {"ok": True, "title": "Portada", "text": "hola", "metrics": {"n": 1}})
        result = await self.call(browser, "open", {"url": "https://ejemplo.es"})
        self.assertEqual(executor.ran, [("open", {"url": "https://ejemplo.es", "read_chars": 1500})])
        self.assertEqual(self.data(result), {"ok": True, "title": "Portada", "text": "hola"})

    async def test_open_without_reading_keeps_zero(self) -> None:
        executor, browser = self.make(lambda op: {"ok": True})
        await self.call(browser, "open", {"url": "https://ejemplo.es", "read_chars": 0})
        self.assertEqual(executor.ran[0][1]["read_chars"], 0)

    async def test_approval_accepted_once(self) -> None:
        _, browser = self.make(ask_approval("borrar"))
        result = await self.call(browser, "task", {"goal": "Borrar", "verification": ["Borrado"]},
                                 self.answering({"permitir": True}))
        self.assertEqual(self.data(result)["status"], "SUBTASK_COMPLETE")
        self.assertIn("ejemplo.es", self.forms[0])
        self.assertIn("Nombre: Ana", self.forms[0])

    async def test_approval_declined_denies(self) -> None:
        _, browser = self.make(ask_approval("dinero"))
        result = await self.call(browser, "task", {"goal": "Pagar", "verification": ["Pagado"]},
                                 self.answering(None, action="decline"))
        self.assertEqual(self.data(result)["status"], "DENIED")

    async def test_unchecked_box_denies(self) -> None:
        _, browser = self.make(ask_approval("borrar"))
        result = await self.call(browser, "task", {"goal": "Borrar", "verification": ["Borrado"]},
                                 self.answering({"permitir": False}))
        self.assertEqual(self.data(result)["status"], "DENIED")

    async def test_without_forms_a_payment_is_never_asked_by_chat(self) -> None:
        _, browser = self.make(ask_approval("dinero"))
        result = await self.call(browser, "task", {"goal": "Pagar", "verification": ["Pagado"]})
        self.assertEqual(self.data(result)["status"], "DENIED")

    async def test_without_forms_sending_is_asked_by_chat(self) -> None:
        # Como claude.ai: sin formularios, la operación se para y el agente pregunta a Pablo en el chat.
        _, browser = self.make(ask_approval("enviar"))
        parked = await self.call(browser, "task", {"goal": "Enviar", "verification": ["Enviado"]})
        asked = self.data(parked)
        self.assertEqual(asked["status"], "NEEDS_APPROVAL")
        self.assertEqual((asked["domain"], asked["action"], asked["fields"]), ("ejemplo.es", "CLICK Enviar", ["Nombre: Ana"]))
        self.assertEqual([c.data for c in parked.content if isinstance(c, types.ImageContent)], [PNG])
        done = await self.call(browser, "answer", {"operation": asked["operation"], "permitir": True})
        self.assertEqual(self.data(done)["status"], "SUBTASK_COMPLETE")
        again = await self.call(browser, "answer", {"operation": asked["operation"], "permitir": True})
        self.assertEqual(self.data(again)["status"], "no_pending")

    async def test_a_no_by_chat_denies(self) -> None:
        _, browser = self.make(ask_approval("borrar"))
        asked = self.data(await self.call(browser, "task", {"goal": "Borrar", "verification": ["Borrado"]}))
        missing = self.data(await self.call(browser, "answer", {"operation": asked["operation"]}))
        self.assertEqual(missing["status"], "invalid")
        done = await self.call(browser, "answer", {"operation": asked["operation"], "permitir": False})
        self.assertEqual(self.data(done)["status"], "DENIED")

    async def test_without_forms_a_code_is_asked_by_chat(self) -> None:
        _, browser = self.make(ask_secret("code"))
        asked = self.data(await self.call(browser, "login", {}))
        self.assertEqual((asked["status"], asked["domain"]), ("NEEDS_CODE", "ejemplo.es"))
        done = await self.call(browser, "answer", {"operation": asked["operation"], "codigo": " 654321 "})
        self.assertEqual(self.data(done)["got"], "654321")

    async def test_answer_to_nothing(self) -> None:
        _, browser = self.make(lambda op: {})
        result = self.data(await self.call(browser, "answer", {"operation": "op_nada", "permitir": True}))
        self.assertEqual(result["status"], "no_pending")

    async def test_always_allow_send_on_domain(self) -> None:
        _, browser = self.make(ask_approval("enviar"))
        args = {"goal": "Enviar", "verification": ["Enviado"]}
        await self.call(browser, "task", args, self.answering({"permitir": True, "siempre": True}))
        result = await self.call(browser, "task", args, self.answering(None, action="decline"))
        self.assertEqual(self.data(result)["status"], "SUBTASK_COMPLETE")
        self.assertEqual(len(self.forms), 1)

    async def test_always_is_not_offered_for_delete(self) -> None:
        _, browser = self.make(ask_approval("borrar"))
        args = {"goal": "Borrar", "verification": ["Borrado"]}
        await self.call(browser, "task", args, self.answering({"permitir": True, "siempre": True}))
        await self.call(browser, "task", args, self.answering({"permitir": True}))
        self.assertEqual(len(self.forms), 2)

    async def test_code_goes_to_executor(self) -> None:
        _, browser = self.make(ask_secret("code"))
        result = await self.call(browser, "login", {}, self.answering({"codigo": " 123456 "}))
        self.assertEqual(self.data(result)["got"], "123456")

    async def test_declined_code_sends_nothing(self) -> None:
        _, browser = self.make(ask_secret("code"))
        result = await self.call(browser, "login", {}, self.answering(None, action="cancel"))
        self.assertNotIn("got", self.data(result))

    async def test_screenshot_comes_back_as_image(self) -> None:
        png = base64.b64encode(b"\x89PNG-falso").decode()

        def script(operation: Operation) -> dict[str, Any]:
            operation.emit("screenshot", png=png)
            return {"status": "ok", "sent": True}

        _, browser = self.make(script)
        result = await self.call(browser, "screenshot", {})
        images = [c for c in result.content if isinstance(c, types.ImageContent)]
        self.assertEqual([i.data for i in images], [png])

    async def test_download_is_embedded(self) -> None:
        pdf = self.tmp / "justificante.pdf"
        pdf.write_bytes(b"%PDF-1.4 falso")
        executor, browser = self.make(lambda op: {
            "ok": True, "downloads": [{"id": "d_1", "name": "justificante.pdf", "bytes": pdf.stat().st_size}]})
        executor.downloads["d_1"] = pdf
        result = await self.call(browser, "open", {"url": "https://ejemplo.es"})
        self.assertEqual(self.data(result)["downloads"], [{"name": "justificante.pdf", "bytes": 14, "sent": True}])
        embedded = [c for c in result.content if isinstance(c, types.EmbeddedResource)]
        self.assertEqual(base64.b64decode(embedded[0].resource.blob), b"%PDF-1.4 falso")
        self.assertEqual(embedded[0].resource.mimeType, "application/pdf")

    async def test_attach_with_content(self) -> None:
        executor, browser = self.make(lambda op: {"status": "ok"})
        await self.call(browser, "attach", {"file": "dni.pdf", "content_base64": base64.b64encode(b"dni").decode()})
        self.assertEqual((executor.uploads_dir / "dni.pdf").read_bytes(), b"dni")
        self.assertEqual(executor.ran, [("attach", {"file": "dni.pdf"})])

    async def test_attach_refuses_paths_and_private_urls(self) -> None:
        executor, browser = self.make(lambda op: {"status": "ok"})
        bad_name = await self.call(browser, "attach", {"file": "../x", "content_base64": ""})
        local = await self.call(browser, "attach", {"file": "x.pdf", "url": "https://127.0.0.1/x.pdf"})
        plain = await self.call(browser, "attach", {"file": "x.pdf", "url": "http://ejemplo.es/x.pdf"})
        self.assertEqual(self.data(bad_name)["status"], "invalid_name")
        self.assertEqual(self.data(local)["status"], "url_not_allowed")
        self.assertEqual(self.data(plain)["status"], "url_not_allowed")
        self.assertEqual(executor.ran, [])

    async def test_library_save_and_search(self) -> None:
        _, browser = self.make(lambda op: {})
        await self.call(browser, "library", {"action": "save_site", "name": "Campus", "url": "https://cv.ejemplo.es/",
                                             "notes": "Asignaturas en «Mis cursos»"})
        found = self.data(await self.call(browser, "library", {"action": "search", "query": "campus"}))
        self.assertIn("Campus", json.dumps(found, ensure_ascii=False))
        missing = self.data(await self.call(browser, "library", {"action": "save_site"}))
        self.assertEqual(missing["status"], "ERROR")

    async def test_official_site(self) -> None:
        _, browser = self.make(lambda op: {})
        self.assertTrue(self.data(await self.call(browser, "official_site", {"url": "https://sede.dgt.gob.es"}))["official"])
        self.assertFalse(self.data(await self.call(browser, "official_site", {"url": "https://dgt-multas.com"}))["official"])

    async def test_user_step(self) -> None:
        _, browser = self.make(lambda op: {})
        done = await self.call(browser, "user_step", {"instructions": "Aprueba en Cl@ve"}, self.answering({"hecho": True}))
        cannot = await self.call(browser, "user_step", {"instructions": "Aprueba en Cl@ve"}, self.answering({"hecho": False}))
        self.assertEqual(self.data(done)["status"], "done")
        self.assertEqual(self.data(cannot)["status"], "cannot")

    async def test_no_keepalive_without_forms(self) -> None:
        _, browser = self.make(lambda op: {"ok": True})
        logs: list[Any] = []

        async def on_log(params: types.LoggingMessageNotificationParams) -> None:
            logs.append(params.data)

        # Una operación que contesta antes del plazo no avisa de nada.
        with mock.patch.object(mcp_server, "KEEPALIVE_SECONDS", 2.0):
            async with create_connected_server_and_client_session(browser.mcp._mcp_server, logging_callback=on_log) as client:
                await client.call_tool("open", {"url": "https://ejemplo.es"})
                await client.call_tool("user_step", {"instructions": "Aprueba en Cl@ve"})
                await asyncio.sleep(0.1)
        self.assertEqual(logs, [])

    async def test_a_quiet_operation_keeps_the_call_alive(self) -> None:
        # En la cola o en un paso largo no hay eventos: también se avisa, para que el cliente no corte.
        def slow(operation: Operation) -> dict[str, Any]:
            time.sleep(0.4)
            return {"ok": True}

        _, browser = self.make(slow)
        logs: list[Any] = []

        async def on_log(params: types.LoggingMessageNotificationParams) -> None:
            logs.append(params.data)

        with mock.patch.object(mcp_server, "KEEPALIVE_SECONDS", 0.1):
            async with create_connected_server_and_client_session(browser.mcp._mcp_server, logging_callback=on_log) as client:
                result = await client.call_tool("open", {"url": "https://ejemplo.es"})
        self.assertEqual(self.data(result), {"ok": True})
        self.assertGreaterEqual(logs.count(mcp_server.WORKING_TEXT), 2)

    async def test_tool_names(self) -> None:
        _, browser = self.make(lambda op: {})
        names = {t.name for t in await browser.mcp.list_tools()}
        self.assertEqual(names, {
            "open", "follow", "task", "read", "login", "save_password", "attach", "screenshot", "sessions",
            "logout", "log", "routine", "library", "downloads", "answer", "boe_search", "official_site", "user_step"})

    async def test_saved_downloads_are_listed(self) -> None:
        _, browser = self.make(lambda op: {})
        listed = self.data(await self.call(browser, "downloads", {}))
        self.assertEqual(listed["downloads"][0]["name"], "factura.pdf")


class FakeContext:
    """Lo que `_ask` usa del contexto de una llamada. El cliente en memoria del SDK no sirve aquí: no lee
    avisos mientras su formulario está abierto, y el envío del servidor se queda esperando."""

    def __init__(self, answer_after: float, token: str | None = "p1") -> None:
        self.answer_after = answer_after
        self.request_id = "7"
        self.request_context = SimpleNamespace(meta=SimpleNamespace(progressToken=token))
        self.session = self
        self.sent: list[tuple[str, Any, Any]] = []

    async def elicit(self, message: str, schema: type) -> Any:
        await asyncio.sleep(self.answer_after)
        return SimpleNamespace(action="accept", data=schema(hecho=True))

    async def log(self, level: str, message: str, *, logger_name: str | None = None) -> None:
        self.sent.append(("log", self.request_id, message))

    async def send_log_message(self, level: str, data: Any, logger: str | None = None,
                               related_request_id: Any = None) -> None:
        self.sent.append(("log", related_request_id, data))

    async def send_progress_notification(self, token: Any, progress: float, total: float | None = None,
                                         message: str | None = None, related_request_id: Any = None) -> None:
        self.sent.append(("progress", related_request_id, progress))


class KeepaliveTests(unittest.IsolatedAsyncioTestCase):
    """Sin tráfico, Claude Code corta la llamada a los 300 s: mientras Pablo no contesta van avisos."""

    def setUp(self) -> None:
        patcher = mock.patch.object(mcp_server, "KEEPALIVE_SECONDS", 0.05)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_notices_while_form_is_pending_and_stop_after(self) -> None:
        ctx = FakeContext(answer_after=0.3)
        answer = await mcp_server._ask(ctx, "Haz un paso", mcp_server.Paso, 5.0)  # type: ignore[arg-type]
        self.assertTrue(answer.hecho)
        progress = [value for kind, _, value in ctx.sent if kind == "progress"]
        self.assertGreaterEqual(len(progress), 3)
        self.assertEqual(progress, sorted(set(progress)), "el número de progreso siempre crece")
        logs = [(related, text) for kind, related, text in ctx.sent if kind == "log"]
        self.assertEqual({text for _, text in logs}, {mcp_server.WAITING_TEXT})
        # Por la respuesta de la llamada y, sin ligar, por el flujo GET.
        self.assertEqual({related for related, _ in logs}, {"7", None})
        self.assertTrue(all(related == "7" for kind, related, _ in ctx.sent if kind == "progress"))
        count = len(ctx.sent)
        await asyncio.sleep(0.2)
        self.assertEqual(len(ctx.sent), count, "los avisos paran al contestar")

    async def test_progress_continues_the_operation_count(self) -> None:
        ctx = FakeContext(answer_after=0.12)
        progress = mcp_server.Progress(ctx)  # type: ignore[arg-type]
        await progress.report("click Enviar")
        await mcp_server._ask(ctx, "¿Lo permites?", mcp_server.Paso, 5.0, progress)  # type: ignore[arg-type]
        await progress.report("click Aceptar")
        values = [value for kind, _, value in ctx.sent if kind == "progress"]
        self.assertGreaterEqual(len(values), 3)
        self.assertEqual(values, list(range(1, len(values) + 1)))

    async def test_without_progress_token_only_logs(self) -> None:
        ctx = FakeContext(answer_after=0.12, token=None)
        await mcp_server._ask(ctx, "Haz un paso", mcp_server.Paso, 5.0)  # type: ignore[arg-type]
        self.assertTrue(ctx.sent)
        self.assertEqual({kind for kind, _, _ in ctx.sent}, {"log"})


class BearerTests(unittest.IsolatedAsyncioTestCase):
    async def test_requires_token(self) -> None:
        async def inner(scope: dict, receive: Any, send: Any) -> None:
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        transport = httpx.ASGITransport(app=BearerAuth(inner, "secreto"))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            self.assertEqual((await client.post("/mcp")).status_code, 401)
            self.assertEqual((await client.post("/mcp", headers={"Authorization": "Bearer otro"})).status_code, 401)
            self.assertEqual((await client.post("/mcp", headers={"Authorization": "Bearer secreto"})).status_code, 200)


if __name__ == "__main__":
    unittest.main()
