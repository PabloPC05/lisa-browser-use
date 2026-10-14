"""Pago con tarjeta en una pasarela (tasas de trámites): los datos los da Pablo en el momento.

La tarjeta no se guarda. Pablo la escribe en el chat como dato secreto; llega del puente al
ejecutor sin pasar por el agente, JEV solo ve `[secret]` y el registro la oculta. Pulsar
«Pagar» sigue pasando por la barrera: aprobación de dinero, con los números enmascarados.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .gate import ApprovalRequest, run_gated_subtask
from .session import Browser
from .vault import registrable_domain

CARD_SECONDS = 300

# ask_card(domain, timeout_seconds) -> texto con número, caducidad y CVV, o None si no contesta.
AskCard = Callable[[str, float], str | None]


@dataclass(frozen=True)
class Card:
    number: str = field(repr=False)
    month: str
    year: str  # dos cifras
    cvv: str = field(repr=False)

    def inputs(self) -> dict[str, str]:
        return {
            "numero_tarjeta": self.number,
            "caducidad_mm_aa": f"{self.month}/{self.year}",
            "mes_caducidad": self.month,
            "anio_caducidad": self.year,
            "anio_caducidad_4_cifras": "20" + self.year,
            "cvv": self.cvv,
            # Muchas pasarelas formatean el número al escribirlo. Como entrada secreta, también
            # esta forma se oculta en lo que ve JEV.
            "numero_tarjeta_con_espacios": " ".join(self.number[i:i + 4] for i in range(0, len(self.number), 4)),
        }

    # Solo el número y el CVV son secretos: ocultar «12» o «27» en todo lo que lee JEV
    # emborronaría la página entera, y la caducidad sola no identifica la tarjeta.
    SECRET_INPUTS = ("numero_tarjeta", "numero_tarjeta_con_espacios", "cvv")

    def secrets(self) -> list[str]:
        """Lo que no debe aparecer en ningún registro."""
        inputs = self.inputs()
        return [inputs[k] for k in self.SECRET_INPUTS]


class CardFormatError(ValueError):
    pass


def _luhn(number: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(number)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def parse_card(text: str) -> Card:
    """`4111 1111 1111 1111 12/27 123` (también `12/2027`, `1227` o con guiones) -> Card."""
    cleaned = re.sub(r"[^\d/ ]", " ", text or "")
    expiry = re.search(r"\b(0[1-9]|1[0-2])\s*/\s*(\d{2}|\d{4})\b", cleaned)
    rest = cleaned
    if expiry:
        month, year = expiry.group(1), expiry.group(2)[-2:]
        rest = cleaned[: expiry.start()] + " " + cleaned[expiry.end():]
    digits_groups = rest.split()
    # El número puede venir en grupos (de cuatro, o 4-6-5 en American Express): se juntan
    # grupos mientras falten cifras, o mientras todos sean de cuatro y no pase de 16.
    number, others, grouped = "", [], True
    for group in digits_groups:
        if not others and (len(number) < 13 or (grouped and len(group) == 4 and len(number) < 16)):
            number += group
            grouped = grouped and len(group) == 4
        else:
            others.append(group)
    if not expiry:
        # Sin barra: MMAA justo después del número.
        if others and re.fullmatch(r"(0[1-9]|1[0-2])\d{2}", others[0]):
            month, year = others[0][:2], others[0][2:]
            others = others[1:]
        else:
            raise CardFormatError("falta la caducidad (MM/AA)")
    if not (13 <= len(number) <= 19) or not _luhn(number):
        raise CardFormatError("el número de tarjeta no es válido")
    if not others or not re.fullmatch(r"\d{3,4}", others[0]):
        raise CardFormatError("falta el CVV (3 o 4 cifras)")
    return Card(number=number, month=month, year=year, cvv=others[0])


def pay_with_card(
    browser: Browser,
    policy: Any,
    approve: Callable[[ApprovalRequest], str],
    *,
    ask_card: AskCard,
    on_secret: Callable[[str], None] | None = None,
    card_seconds: float = CARD_SECONDS,
    **options: Any,
) -> dict[str, Any]:
    """Rellena la tarjeta en la pasarela abierta y pulsa pagar, con aprobación de dinero.

    Devuelve el resultado de la subtarea; `card_timeout` o `card_format` si no hay una tarjeta válida.
    """
    domain = registrable_domain(browser._eval("location.href") or "")
    answer = ask_card(domain, card_seconds)
    if not answer:
        return {"status": "card_timeout", "domain": domain}
    try:
        card = parse_card(answer)
    except CardFormatError as exc:
        return {"status": "card_format", "domain": domain, "reason": str(exc)}
    if on_secret is not None:
        for value in card.secrets():
            on_secret(value)
    inputs = card.inputs()
    result = run_gated_subtask(
        browser, policy,
        {
            "goal": "Rellenar el formulario de pago con los datos de la tarjeta dados y pulsar el botón de pagar.",
            "verification": ["La página ha dejado el formulario de la tarjeta: muestra el resultado del pago o pide confirmarlo en el banco."],
            "inputs": inputs,
            "secret_inputs": list(Card.SECRET_INPUTS),
            "constraints": [
                "Escribe cada dato de la tarjeta una sola vez, en su campo. El número va en numero_tarjeta.",
                "Si la caducidad va en dos campos, usa el mes y el año; si va en uno, usa caducidad_mm_aa.",
                "No cambies el importe ni otros datos del pago.",
            ],
        },
        approve, **options,
    )
    result["domain"] = domain
    return result
