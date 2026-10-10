"""Subidas y descargas con Chromium real sobre el sitio de pruebas."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lisa_browser.files import Files
from lisa_browser.runner import run_subtask
from tests.site import Site, page
from tests.support import ByName, launch_browser

PDF = b"%PDF-1.4 factura de prueba " + b"x" * 5000
PHOTO = b"\x89PNG\r\n\x1a\n" + b"foto-de-prueba" * 50

FILE_PAGES = {
    "/facturas": page(
        "Facturas",
        "<h1>Facturas</h1><p><a href='/factura-marzo.pdf'>Descargar factura de marzo</a></p>",
    ),
    "/factura-marzo.pdf": (
        "application/pdf", PDF, {"Content-Disposition": "attachment; filename=\"factura-marzo.pdf\""},
    ),
    "/anuncio": page(
        "Nuevo anuncio",
        "<h1>Nuevo anuncio</h1><form id='f' method='post' action='/subido' enctype='multipart/form-data'>"
        "<label>Foto del artículo <input type='file' name='foto'></label>"
        "<label>Factura de compra <input type='file' name='factura'></label>"
        "<button type='submit'>Publicar</button></form>",
    ),
    "/subido": page("Subido", "<h1>Recibido</h1>"),
}


class FilesCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.site = Site(FILE_PAGES).__enter__()
        cls.browser = launch_browser()
        cls._tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmp.name)
        (tmp / "descargas").mkdir()
        (tmp / "subidas").mkdir()
        (tmp / "subidas" / "bici.png").write_bytes(PHOTO)
        cls.files = Files(cls.browser, tmp / "descargas", tmp / "subidas")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.site.__exit__()
        cls._tmp.cleanup()

    def setUp(self) -> None:
        self.site.requests.clear()


class Download(FilesCase):
    def test_a_clicked_download_is_collected_with_name_and_size(self) -> None:
        self.browser.open(self.site.url("/facturas"))
        self.assertEqual(self.files.collect(wait_s=0.2), [])
        run_subtask(
            self.browser.backend, ByName([("CLICK", "Descargar factura de marzo"), ("SUBTASK_COMPLETE",)]),
            {"goal": "Descargar la factura", "verification": ["Descargada"]},
        )
        [download] = self.files.collect()
        self.assertEqual((download["name"], download["bytes"]), ("factura-marzo.pdf", len(PDF)))
        self.assertEqual(Path(download["path"]).read_bytes(), PDF)
        self.assertEqual(self.files.collect(wait_s=0.2), [])  # no se entrega dos veces


class Upload(FilesCase):
    def test_file_is_attached_to_the_named_field_and_sent(self) -> None:
        self.browser.open(self.site.url("/anuncio"))
        attached = self.files.attach("bici.png", "foto del articulo")
        self.assertEqual((attached["status"], attached["file"]), ("ok", "bici.png"))
        self.browser._eval("document.querySelector('#f').submit()")
        self.browser.backend._wait_loaded()
        [body] = self.site.posts("/subido")
        self.assertIn(PHOTO, body)
        self.assertIn(b'name="foto"; filename="bici.png"', body)

    def test_missing_file_ambiguous_field_and_path_tricks_are_refused(self) -> None:
        self.browser.open(self.site.url("/anuncio"))
        self.assertEqual(self.files.attach("no-existe.png", "foto")["status"], "no_file")
        self.assertEqual(self.files.attach("../subidas/bici.png", "foto")["status"], "no_file")
        self.assertEqual(self.files.attach("bici.png", "")["status"], "ambiguous_field")
        self.assertEqual(self.files.attach("bici.png", "pasaporte")["status"], "no_field")


if __name__ == "__main__":
    unittest.main()
