"""Transporte de arc-cua hacia JEV a través de la API de Command Code.

Es un `ChoiceTransport` de arc-cua: recibe el estado y las preguntas que
construye su política y devuelve las respuestas del modelo de decisión.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

import httpx

log = logging.getLogger(__name__)

DEFAULT_URL = "https://api.commandcode.ai/provider/v1/systemone"
DEFAULT_MODEL = "typesafe/jev"
# Dólares por millón de tokens de entrada; la salida de JEV no se cobra.
DEFAULT_PRICE_PER_MTOK = 0.042

_RETRYABLE = {429, 503, 529}
_ATTEMPTS = 3


class QuotaExceeded(RuntimeError):
    """Command Code no atiende más peticiones: tope del plan o plan sin acceso."""


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    ms: int = 0

    def cost_usd(self, price_per_mtok: float) -> float:
        return self.input_tokens * price_per_mtok / 1_000_000

    def since(self, earlier: Usage) -> Usage:
        return Usage(self.calls - earlier.calls, self.input_tokens - earlier.input_tokens, self.ms - earlier.ms)

    def copy(self) -> Usage:
        return Usage(self.calls, self.input_tokens, self.ms)


class CommandCodeJevTransport:
    name = "JEV"
    supports_images = False

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEFAULT_URL,
        model: str = DEFAULT_MODEL,
        price_per_mtok: float = DEFAULT_PRICE_PER_MTOK,
        timeout_s: float = 25,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise ValueError("falta la clave de Command Code")
        self._api_key = api_key
        self.base_url = base_url
        self.model = model
        self.price_per_mtok = price_per_mtok
        self._client = client or httpx.Client(http2=True, timeout=timeout_s)
        self._sleep = sleep
        self.usage = Usage()

    def ask(
        self,
        state: Mapping[str, Any],
        questions: Mapping[str, Any],
        *,
        images: Sequence[bytes] = (),
    ) -> Mapping[str, Any]:
        # R2: las decisiones se toman sobre elementos, nunca sobre capturas.
        if images:
            raise ValueError("JEV no recibe imágenes; no se ejecuta nada")
        body = {"model": self.model, "state": state, "questions": questions}
        started = time.perf_counter()
        try:
            answer = self._post(body)
        finally:
            self.usage.calls += 1
            self.usage.ms += round((time.perf_counter() - started) * 1000)
        self.usage.input_tokens += int((answer.get("usage") or {}).get("input_tokens", 0))
        return answer

    def _post(self, body: Mapping[str, Any]) -> Mapping[str, Any]:
        for attempt in range(_ATTEMPTS):
            try:
                response = self._client.post(
                    self.base_url, json=body, headers={"Authorization": f"Bearer {self._api_key}"}
                )
            except httpx.HTTPError as exc:
                raise RuntimeError(f"JEV no responde ({type(exc).__name__}); no se ejecuta nada") from exc
            status = response.status_code
            if status in _RETRYABLE and attempt < _ATTEMPTS - 1:
                self._sleep(0.5 * 2**attempt)
                continue
            if status == 403 or status == 429:
                # Command Code no documenta cómo distingue un tope del plan de un
                # límite de velocidad: un 429 que persiste se trata como tope.
                raise QuotaExceeded(f"Command Code respondió {status} ({_error_type(response)})")
            if response.is_error:
                raise RuntimeError(f"JEV respondió {status} ({_error_type(response)}); no se ejecuta nada")
            return response.json()
        raise RuntimeError("JEV no disponible; no se ejecuta nada")


def _error_type(response: httpx.Response) -> str:
    """Solo el tipo de error estructurado; nunca texto arbitrario de la respuesta."""
    try:
        error = response.json().get("error")
    except (ValueError, AttributeError):
        return "sin detalle"
    kind = error.get("type") if isinstance(error, dict) else None
    return str(kind)[:60] if kind else "sin detalle"
