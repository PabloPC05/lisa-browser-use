"""Transporte de JEV contra una API de Command Code simulada."""

from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from arc_cua.policies import ChoicePolicy

from lisa_browser.jev import CommandCodeJevTransport, QuotaExceeded
from lisa_browser.runner import run_subtask
from tests.test_runner import counter_app

KEY = "clave-de-prueba-0123456789"
ANSWER = {
    "model": "typesafe/jev",
    "answers": {"q": {"type": "choice", "choice": "a", "confidence": 1, "probabilities": {"a": 1, "b": 0}}},
    "usage": {"input_tokens": 400, "output_tokens": 35},
}


class FakeApi:
    """Responde con la lista de `(código, cuerpo)` que se le da, en orden; la última se repite."""

    def __init__(self, responses: list[tuple[int, dict]]) -> None:
        self.responses = list(responses)
        self.requests: list[dict] = []
        api = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                api.requests.append({"body": body, "auth": self.headers.get("Authorization")})
                status, payload = api.responses.pop(0) if len(api.responses) > 1 else api.responses[0]
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/systemone"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class JevCase(unittest.TestCase):
    def api(self, *responses: tuple[int, dict]) -> FakeApi:
        api = FakeApi(list(responses))
        self.addCleanup(api.close)
        return api

    def transport(self, api: FakeApi) -> CommandCodeJevTransport:
        self.waits: list[float] = []
        return CommandCodeJevTransport(api_key=KEY, base_url=api.url, sleep=self.waits.append)


class RequestHasNoImages(JevCase):
    def test_request_carries_only_model_state_and_questions(self) -> None:
        api = self.api((200, ANSWER))
        answer = self.transport(api).ask({"desktop": {"elements": []}}, {"q": {"type": "choice"}})
        self.assertEqual(answer["answers"]["q"]["choice"], "a")
        [request] = api.requests
        self.assertEqual(set(request["body"]), {"model", "state", "questions"})
        self.assertEqual(request["body"]["model"], "typesafe/jev")
        self.assertEqual(request["auth"], f"Bearer {KEY}")

    def test_images_are_refused_before_any_request(self) -> None:
        api = self.api((200, ANSWER))
        with self.assertRaises(ValueError):
            self.transport(api).ask({}, {}, images=(b"\x89PNG",))
        self.assertEqual(api.requests, [])


class Usage(JevCase):
    def test_calls_tokens_and_cost_accumulate(self) -> None:
        transport = self.transport(self.api((200, ANSWER)))
        transport.ask({}, {})
        transport.ask({}, {})
        self.assertEqual((transport.usage.calls, transport.usage.input_tokens), (2, 800))
        self.assertAlmostEqual(transport.usage.cost_usd(0.042), 800 * 0.042 / 1_000_000)


class Errors(JevCase):
    def test_temporary_limit_is_retried(self) -> None:
        api = self.api((429, {"error": {"type": "rate_limit_error"}}), (503, {}), (200, ANSWER))
        self.transport(api).ask({}, {})
        self.assertEqual(len(api.requests), 3)
        self.assertEqual(self.waits, [0.5, 1.0])

    def test_persistent_limit_is_quota(self) -> None:
        api = self.api((429, {"error": {"type": "rate_limit_error", "message": "texto del proveedor"}}))
        with self.assertRaises(QuotaExceeded) as caught:
            self.transport(api).ask({}, {})
        self.assertEqual(len(api.requests), 3)
        self.assertIn("rate_limit_error", str(caught.exception))
        self.assertNotIn("texto del proveedor", str(caught.exception))

    def test_plan_without_access_is_quota_at_once(self) -> None:
        api = self.api((403, {"error": {"type": "upgrade_required"}}))
        with self.assertRaises(QuotaExceeded):
            self.transport(api).ask({}, {})
        self.assertEqual(len(api.requests), 1)

    def test_server_error_is_not_quota_and_hides_the_key(self) -> None:
        api = self.api((500, {"error": {"type": "server_error"}}))
        with self.assertRaises(RuntimeError) as caught:
            self.transport(api).ask({}, {})
        self.assertNotIsInstance(caught.exception, QuotaExceeded)
        self.assertNotIn(KEY, str(caught.exception))


class QuotaStopsTask(JevCase):
    def test_subtask_ends_as_quota_without_acting(self) -> None:
        api = self.api((403, {"error": {"type": "upgrade_required"}}))
        backend, state = counter_app()
        transport = self.transport(api)
        result = run_subtask(
            backend, ChoicePolicy(transport),
            {"goal": "Pulsa Sumar", "verification": ["El contador muestra 1"]}, usage=transport.usage,
        )
        self.assertEqual(result["status"], "QUOTA")
        self.assertEqual(state["count"], 0)
        self.assertEqual(result["actions_taken"], 0)
        self.assertIsNotNone(result["page"])


if __name__ == "__main__":
    unittest.main()
