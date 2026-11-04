"""Collaudo della vista trascrizione e del testo provvisorio in-place.

Il difetto che questi test bloccano: ``set_partial`` scriveva l'ipotesi
come riga nuova in fondo, e quando il segmento finale arrivava le stesse
parole ricomparivano una riga piu' sopra — un "salto di riga" duplicato
che si vedeva a ogni ridecodifica del segmento aperto (~3 s, misurato).
Ora l'ipotesi continua l'ultima riga finalizzata: si controlla il buffer
reale, non i mock.

Invarianti ricontrollati qui:
* lo span del provvisorio comprende il terminatore (niente a capo orfano);
* ``text_for`` restituisce il solo testo finalizzato, senza provvisorio;
* dopo la sequenza la trascrizione contiene esattamente le due righe
  finalizzate, contigue e non moltiplicate.
"""

from __future__ import annotations

import unittest

from _bootstrap import ensure_paths

ensure_paths()

from lecture_transcriber.config import Settings  # noqa: E402
from lecture_transcriber.ui import TranscriptView  # noqa: E402


def _make_view() -> TranscriptView:
    return TranscriptView(Settings())


class TranscriptPartialTest(unittest.TestCase):
    """Il testo provvisorio continua l'ultima riga invece di duplicarla."""

    def test_partial_without_segments_shows_text(self) -> None:
        """(a) senza segmenti l'ipotesi e' visibile nel buffer."""
        view = _make_view()
        view.set_partial("parole in corsivo")
        self.assertEqual(view.full_text(), "parole in corsivo\n")
        self.assertIsNotNone(view._partial_span)

    def test_partial_does_not_duplicate_line_on_segment(self) -> None:
        """(b) dopo il segmento finale niente riga duplicata: testo contiguo."""
        view = _make_view()
        view.add_segment(0, 0.0, "Buongiorno")
        view.set_partial("a tutti")
        # l'ipotesi e' in coda alla riga, non su una riga separata
        text = view.full_text()
        self.assertIn("Buongiorno a tutti", text)
        self.assertEqual(text.count("Buongiorno"), 1)

        view.add_segment(1, 5.0, "eccoci qui")
        text = view.full_text()
        # nessuna riga duplicata: "a tutti" non deve comparire due volte
        self.assertEqual(text.count("Buongiorno"), 1)
        self.assertNotIn("a tutti", text)
        # le due righe finalizzate sono contigue, non moltiplicate
        self.assertEqual(text.count("eccoci qui"), 1)
        self.assertLess(
            text.index("Buongiorno"), text.index("eccoci qui")
        )

    def test_text_for_excludes_partial(self) -> None:
        """(c) ``text_for`` restituisce solo il segmento finalizzato."""
        view = _make_view()
        view.add_segment(0, 0.0, "testo certo")
        view.set_partial("ipotesi")
        self.assertEqual(view.text_for(0), "testo certo")

    def test_no_orphan_newline_after_sequence(self) -> None:
        """(d) al termine il buffer ha esattamente le due righe, senza orfani."""
        view = _make_view()
        view.add_segment(0, 0.0, "prima")
        view.set_partial("provvisorio")
        view.add_segment(1, 5.0, "seconda")
        text = view.full_text()
        # il provvisorio e' sparito
        self.assertNotIn("provvisorio", text)
        # due righe finalizzate, separate da una riga vuota (il separatore)
        lines = [ln for ln in text.split("\n") if ln.strip()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0].split("  ")[-1].strip(), "prima")
        self.assertEqual(lines[1].split("  ")[-1].strip(), "seconda")
        # nessun a capo extra oltre il separatore dell'ultima riga: un
        # "'\\n\\n\\n'" in coda rivelerebbe l'orfano lasciato dal provvisorio
        self.assertFalse(text.endswith("\n\n\n"))

    def test_last_line_returns_committed_not_partial(self) -> None:
        """``last_line`` non deve restituire la coda provvisoria."""
        view = _make_view()
        view.add_segment(0, 0.0, "contenuto certo")
        view.set_partial("ipotesi ancora in corso")
        self.assertEqual(view.last_line(), "contenuto certo")
        # l'ipotesi resta nel buffer: non e' stata cancellata
        self.assertIn("ipotesi ancora in corso", view.full_text())

    def test_clear_partial_leaves_no_orphan(self) -> None:
        """cancellare l'ipotesi riporta il testo al punto esatto."""
        view = _make_view()
        view.add_segment(0, 0.0, "originale")
        before = view.full_text()
        view.set_partial("continuazione")
        view.clear_partial()
        self.assertEqual(view.full_text(), before)


if __name__ == "__main__":
    unittest.main()
