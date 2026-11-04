"""Collaudo dell'export PDF ("Salva lezione" nella barra superiore).

Il tasto salva la lezione in ``~/Documents`` come PDF. Qui si controlla la
parte pura — preparazione dell'HTML e scrittura del file — separata
dall'interfaccia.

La scrittura del PDF reale richiede WeasyPrint, che e' una dipendenza di
sistema (Fedora: ``python3-weasyprint``). Se non c'e', il PDF non si crea
ma l'app resta funzionante: i test che toccano ``save_pdf`` vengono quindi
saltati quando WeasyPrint manca, mentre quelli sul solo HTML girano sempre.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from _bootstrap import ensure_paths

ensure_paths()

from lecture_transcriber import pdf_export  # noqa: E402
from lecture_transcriber.export import Exporter  # noqa: E402
from lecture_transcriber.sessions import SessionStore  # noqa: E402

HAVE_WEASYPRINT = True
try:
    import weasyprint  # noqa: F401
except ImportError:  # pragma: no cover - dipende dall'ambiente
    HAVE_WEASYPRINT = False


class PdfExportTestCase(unittest.TestCase):
    """Sessione vera con segmenti, segnalibri, note e una provvisoria."""

    def setUp(self) -> None:
        self._db = tempfile.mktemp(suffix=".db")
        self.store = SessionStore(self._db)
        self.sid = self.store.create_session(
            "Analisi Matematica", "Prof. Rossi", date="2026-10-30"
        )
        self.store.add_segment(self.sid, 0, 0.0, 30.0, "Introduzione alla convergenza.")
        self.store.add_segment(
            self.sid, 1, 30.0, 62.0, "Il criterio di Cauchy è equivalente."
        )
        rows = self.store.get_segments(self.sid)
        self.store.set_bookmark(int(rows[1]["id"]), True)
        self.store.set_note(int(rows[0]["id"]), "rivedere la definizione di epsilon")
        # segmento provvisorio: deve restare fuori dal PDF
        self.store.add_segment(
            self.sid, 2, 62.0, 65.0, "ipotesi provvisoria", final=False
        )

    def tearDown(self) -> None:
        try:
            Path(self._db).unlink(missing_ok=True)
        except OSError:
            pass

    def _segments(self):
        exporter = Exporter(self.store)
        return exporter._collect(self.sid, include_partial=False)

    def test_only_final_segments_are_collected(self) -> None:
        """La provvisoria non deve entrare nel PDF (come per gli altri export)."""
        _, segments = self._segments()
        text = [seg.text for seg in segments]
        self.assertIn("convergenza", " ".join(text))
        self.assertNotIn("ipotesi provvisoria", " ".join(text))

    def test_render_html_has_header_meta_and_segments(self) -> None:
        _, segments = self._segments()
        duration = max(seg.end for seg in segments)
        html_text = pdf_export.render_pdf_html(
            self.store.get_session(self.sid), segments, duration
        )
        # intestazione con i metadati
        self.assertIn("Analisi Matematica", html_text)
        self.assertIn("Prof. Rossi", html_text)
        self.assertIn("2026-10-30", html_text)
        # timestamp (MM:SS) nel blocco tempo
        self.assertIn('>00:00<', html_text)
        self.assertIn('>00:30<', html_text)
        # segnalibro e nota hanno i loro marcatori
        self.assertIn("★", html_text)
        self.assertIn("rivedere la definizione di epsilon", html_text)
        # la provvisoria non c'e'
        self.assertNotIn("ipotesi provvisoria", html_text)
        # scappamento dell'HTML: un segno "è" resta leggibile, e < > non rompono
        self.assertIn("Cauchy è equivalente", html_text)

    def test_html_escapes_special_characters(self) -> None:
        _, segments = self._segments()
        # aggiungiamo un segmento che contiene markup pericoloso
        self.store.add_segment(
            self.sid, 3, 100.0, 110.0, "a < b && c > d", final=True
        )
        exporter = Exporter(self.store)
        _, segs = exporter._collect(self.sid, include_partial=False)
        html_text = pdf_export.render_pdf_html(
            self.store.get_session(self.sid), segs, max(s.end for s in segs)
        )
        self.assertIn("a &lt; b &amp;&amp; c &gt; d", html_text)

    @unittest.skipUnless(HAVE_WEASYPRINT, "serve WeasyPrint (python3-weasyprint)")
    def test_save_pdf_writes_a_valid_pdf(self) -> None:
        out = Path(tempfile.mktemp(suffix=".pdf"))
        try:
            written = pdf_export.save_pdf(self.store, self.sid, out)
            self.assertEqual(written, out)
            self.assertTrue(out.exists())
            with out.open("rb") as fh:
                self.assertEqual(fh.read(5), b"%PDF-")
        finally:
            try:
                out.unlink(missing_ok=True)
            except OSError:
                pass

    @unittest.skipUnless(HAVE_WEASYPRINT, "serve WeasyPrint (python3-weasyprint)")
    def test_save_pdf_creates_parent_directories(self) -> None:
        out = Path(tempfile.mkdtemp()) / "nidificate" / "lezione.pdf"
        try:
            pdf_export.save_pdf(self.store, self.sid, out)
            self.assertTrue(out.exists())
        finally:
            try:
                out.unlink(missing_ok=True)
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()
