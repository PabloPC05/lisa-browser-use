"""Pago con tarjeta dada por Pablo en el momento, con Chromium real sobre el sitio de pruebas."""

from __future__ import annotations

import unittest
from urllib.parse import parse_qs

from lisa_browser.card import CardFormatError, parse_card, pay_with_card
from lisa_browser.gate import ApprovalRequest
from tests.site import Site, page
from tests.support import ByName, launch_browser

# Tarjeta de prueba de las pasarelas (número de Visa de pruebas, válido por Luhn).
NUMBER = "4548812049400004"
CVV = "123"

CARD_PAGES = {
    "/pasarela": page(
        "Pasarela de pago",
        "<h1>Pago de la tasa</h1><p>Importe: 10,91 €</p><form method='post' action='/pagado'>"
        "<label>Número de tarjeta <input id='card-number' name='card_number' autocomplete='cc-number'></label>"
        "<label>Caducidad <input id='caducidad' name='caducidad' placeholder='MM/AA'></label>"
        "<label>CVV <input id='cvv' name='cvv'></label>"
        "<button type='submit'>Pagar</button></form>",
    ),
    "/pagado": page("Pago realizado", "<h1>Pago realizado</h1>"),
}


class ParseCard(unittest.TestCase):
    def test_accepts_usual_spellings(self) -> None:
        for text in (
            f"{NUMBER} 12/27 {CVV}",
            "4548 8120 4940 0004 12/2027 123",
            "4548-8120-4940-0004, 12 / 27, cvv 123",
            f"{NUMBER} 1227 {CVV}",
        ):
            card = parse_card(text)
            self.assertEqual((card.number, card.month, card.year, card.cvv), (NUMBER, "12", "27", CVV), text)

    def test_rejects_bad_numbers_and_missing_parts(self) -> None:
        for text in ("4548812049400005 12/27 123", f"{NUMBER} 123", f"{NUMBER} 12/27", "hola"):
            with self.assertRaises(CardFormatError, msg=text):
                parse_card(text)

    def test_card_numbers_never_appear_in_repr(self) -> None:
        card = parse_card(f"{NUMBER} 12/27 {CVV}")
        self.assertNotIn(NUMBER, repr(card))
        self.assertNotIn(CVV, repr(card))


class PayWithCard(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.site = Site(CARD_PAGES).__enter__()
        cls.browser = launch_browser()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.site.__exit__()

    def setUp(self) -> None:
        self.site.requests.clear()
        self.asked: list[ApprovalRequest] = []
        self.secrets: list[str] = []
        self.assertTrue(self.browser.open(self.site.url("/pasarela"))["ok"])

    def approve(self, request: ApprovalRequest) -> str:
        self.asked.append(request)
        return "once"

    def pay(self, answer: str | None, steps):
        self.policy = ByName(steps)
        return pay_with_card(
            self.browser, self.policy, self.approve, ask_card=lambda domain, timeout: answer,
            on_secret=self.secrets.append,
        )

    def test_fills_the_card_and_pays_after_a_money_approval_with_masked_numbers(self) -> None:
        result = self.pay(f"{NUMBER} 12/27 {CVV}", [
            ("TYPE_TEXT", "Número de tarjeta", "numero_tarjeta"),
            ("TYPE_TEXT", "Caducidad", "caducidad_mm_aa"),
            ("TYPE_TEXT", "CVV", "cvv"),
            ("CLICK", "Pagar"),
        ])
        # Tras pagar no se decide nada más: el planificador comprueba el resultado.
        self.assertEqual(result["status"], "NEEDS_AGENT")
        [request] = self.asked
        self.assertEqual(request.category, "dinero")
        shown = dict(request.fields)
        self.assertEqual(shown["Número de tarjeta"], "•••• 0004")
        self.assertEqual(shown["CVV"], "••••")
        self.assertEqual(shown["Caducidad"], "12/27")
        [body] = self.site.posts("/pagado")
        self.assertEqual(parse_qs(body.decode())["card_number"], [NUMBER])
        # Lo que hay que ocultar en el registro: el número (también con espacios) y el CVV.
        self.assertIn(NUMBER, self.secrets)
        self.assertIn("4548 8120 4940 0004", self.secrets)
        self.assertIn(CVV, self.secrets)

    def test_no_answer_or_a_bad_card_does_not_touch_the_page(self) -> None:
        self.assertEqual(self.pay(None, [])["status"], "card_timeout")
        result = self.pay("1234 12/27 123", [])
        self.assertEqual(result["status"], "card_format")
        self.assertNotIn("1234", result["reason"])
        self.assertEqual(self.policy.calls, 0)
        self.assertEqual(self.site.posts(), [])

    def test_the_card_is_hidden_in_screenshots(self) -> None:
        self.browser._eval(f"document.getElementById('card-number').value = '{NUMBER}'")
        color = self.browser._eval(
            "(async () => { await " + _HIDE + "; const c = getComputedStyle(document.getElementById('card-number')).color;"
            " " + _SHOW + "; return c; })()"
        )
        self.assertEqual(color, "rgba(0, 0, 0, 0)")


from lisa_browser.session import _HIDE_SECRETS_JS as _HIDE, _SHOW_SECRETS_JS as _SHOW  # noqa: E402


if __name__ == "__main__":
    unittest.main()
