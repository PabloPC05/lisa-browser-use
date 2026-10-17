"""Sedes oficiales y búsqueda en el BOE (sin internet: la API se simula)."""

from __future__ import annotations

import json
import unittest
from urllib.parse import parse_qs, urlsplit

import httpx

from lisa_browser.tramites import OFFICIAL_DOMAINS, build_boe_query, official_site, search_boe


class OfficialSiteTests(unittest.TestCase):
    def test_suffix_must_match_whole_labels(self) -> None:
        self.assertEqual(official_site("https://sede.agenciatributaria.gob.es/Sede/"),
                         {"official": True, "domain": "gob.es", "level": "state"})
        self.assertFalse(official_site("https://evilgob.es")["official"])
        self.assertFalse(official_site("https://gob.es.ejemplo.com")["official"])

    def test_only_https(self) -> None:
        self.assertFalse(official_site("http://www.boe.es")["official"])
        self.assertTrue(official_site("www.boe.es")["official"])

    def test_list_was_ported_whole(self) -> None:
        self.assertEqual(len(OFFICIAL_DOMAINS), 212)
        self.assertIn(("registradores.org", "other"), OFFICIAL_DOMAINS)


class BoeTests(unittest.TestCase):
    def test_query_keeps_phrases_and_drops_stopwords(self) -> None:
        self.assertEqual(build_boe_query('ley de "protección de datos" personales'),
                         'titulo:("protección de datos" and ley and personales) and vigencia_agotada:N')
        self.assertEqual(build_boe_query("tasas del DNI", "texto", False), "texto:(tasas and DNI)")
        with self.assertRaises(ValueError):
            build_boe_query("de la y")

    def test_search_maps_results(self) -> None:
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["query"] = json.loads(parse_qs(urlsplit(str(request.url)).query)["query"][0])
            seen["limit"] = parse_qs(urlsplit(str(request.url)).query)["limit"][0]
            return httpx.Response(200, json={"status": {"code": "200"}, "data": [{
                "identificador": "BOE-A-2018-16673", "titulo": "Ley Orgánica 3/2018",
                "rango": {"texto": "Ley Orgánica"}, "fecha_publicacion": "20181206", "vigencia_agotada": "N",
                "url_html_consolidada": "https://www.boe.es/buscar/act.php?id=BOE-A-2018-16673",
            }, {"titulo": "sin identificador"}]})

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            laws = search_boe("protección de datos", limit=50, client=client)
        self.assertEqual(seen["limit"], "20")
        self.assertIn("vigencia_agotada:N", seen["query"]["query"]["query_string"]["query"])
        self.assertEqual(laws, [{
            "id": "BOE-A-2018-16673", "title": "Ley Orgánica 3/2018", "rank": "Ley Orgánica",
            "published_at": "2018-12-06", "in_force": True,
            "url": "https://www.boe.es/buscar/act.php?id=BOE-A-2018-16673",
        }])

    def test_api_error_is_raised(self) -> None:
        with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503))) as client:
            with self.assertRaises(RuntimeError):
                search_boe("tasas", client=client)


if __name__ == "__main__":
    unittest.main()
