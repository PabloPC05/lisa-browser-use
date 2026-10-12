"""Subidas y descargas con Chromium real sobre el sitio de pruebas."""

from __future__ import annotations

import tempfile
import time
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
    # Sin Content-Disposition: el navegador lo abre en su visor en vez de descargarlo.
    "/enunciado.pdf": ("application/pdf", PDF, {}),
    "/Presentaci%C3%B3n_SO1.pdf": ("application/pdf", PDF, {}),
    "/practicas": page("Prácticas", "<h1>Prácticas</h1><p><a href='/enunciado.pdf'>Práctica 2</a></p>"),
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


class PdfInTheViewer(FilesCase):
    def test_a_pdf_opened_in_the_viewer_is_downloaded_once(self) -> None:
        self.browser.open(self.site.url("/practicas"))
        self.assertFalse(self.files.save_shown_pdf())  # una página normal no se toca
        # Como en producción, con el idioma fijado. (El error «Another locale override is already in effect»
        # que se vio allí al abrirse el visor no se reproduce aquí; esto solo cubre el camino normal.)
        self.browser.present_as_desktop(locale="es-ES", timezone="Europe/Madrid")
        result = run_subtask(
            self.browser.backend, ByName([("CLICK", "Práctica 2"), ("SUBTASK_COMPLETE",)]),
            {"goal": "Abrir la práctica 2", "verification": ["Se abre el PDF"]},
        )
        self.assertEqual((result["status"], result["reason"]), ("SUBTASK_COMPLETE", None))
        already = self.files.collect(wait_s=0.5)
        if not already:  # el visor lo muestra: hay que bajarlo
            self.assertTrue(self.files.save_shown_pdf())
            already = self.files.collect(wait_s=3.0)
        [download] = already
        self.assertEqual((download["name"], download["bytes"]), ("enunciado.pdf", len(PDF)))
        self.assertEqual(Path(download["path"]).read_bytes(), PDF)
        # Mientras siga en pantalla no se vuelve a bajar.
        self.assertFalse(self.files.save_shown_pdf())
        self.assertEqual(self.files.collect(wait_s=0.5), [])
        # Pedirlo otra vez es abrirlo otra vez, y entonces sí se baja de nuevo (con otro nombre de archivo).
        self.browser.open(self.site.url("/practicas"))
        self.browser.open(self.site.url("/enunciado.pdf"))
        again = self.files.collect(wait_s=0.5)
        if not again:
            self.assertTrue(self.files.save_shown_pdf())
            again = self.files.collect(wait_s=3.0)
        self.assertEqual([d["bytes"] for d in again], [len(PDF)])


    def test_a_pdf_with_accents_in_its_address_keeps_a_readable_name(self) -> None:
        self.browser.open(self.site.url("/Presentaci%C3%B3n_SO1.pdf"))
        found = self.files.collect(wait_s=0.5)
        if not found:
            self.assertTrue(self.files.save_shown_pdf())
            found = self.files.collect(wait_s=3.0)
        # Sin la tilde: el navegador de producción no sabe escribirla en el nombre del archivo.
        self.assertEqual([d["name"] for d in found], ["Presentacion_SO1.pdf"])


class ReadableName(unittest.TestCase):
    def test_the_name_comes_from_the_address_without_accents_or_odd_characters(self) -> None:
        from lisa_browser.files import readable_name

        self.assertEqual(readable_name("https://cv.usc.es/pluginfile.php/3/mod_resource/content/47/Presentaci%C3%B3n_SO1.pdf"), "Presentacion_SO1.pdf")
        self.assertEqual(readable_name("https://a.test/docs/Gu%C3%ADa%20docente%20(2026).pdf?x=1#p2"), "Guia docente _2026_.pdf")
        self.assertEqual(readable_name("https://a.test/"), "")


class Upload(FilesCase):
    def test_file_is_attached_to_the_named_field_and_sent(self) -> None:
        self.browser.open(self.site.url("/anuncio"))
        attached = self.files.attach("bici.png", "foto del articulo")
        self.assertEqual((attached["status"], attached["file"]), ("ok", "bici.png"))
        self.browser._eval("document.querySelector('#f').submit()")
        self.browser.backend._wait_loaded()
        # El envío del formulario puede llegar al servidor un instante después de que la página diga que cargó.
        deadline = time.monotonic() + 5
        while not self.site.posts("/subido") and time.monotonic() < deadline:
            time.sleep(0.05)
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
