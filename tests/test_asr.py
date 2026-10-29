"""Test del segmentatore audio (nessun modello, nessun microfono).

I test pilotano :class:`Segmenter` con segnali sintetici e verificano:

* un blocco di parlato seguito da 1 s di silenzio chiude un segmento;
* un segmento troppo breve viene scartato;
* ``max_segment_s`` forza la chiusura di un segmento troppo lungo;
* ``flush()`` chiude il segmento in corso;
* il testo di un segmento e' utilizzabile dal transcript.

Scrivibili sia con pytest sia con ``python -m unittest`` (i metodi hanno
il nome ``test_*`` e usano le asserzioni di :mod:`unittest`).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# rende importabile ``lecture_transcriber`` (layout src/) e installa la
# shim di compatibilita' per soxr, anche sotto ``python -m unittest``
import _bootstrap  # noqa: F401,E402  (importa dopo l'inserimento nel path)

import numpy as np

from lecture_transcriber.asr import ASRConfig, Segmenter, TranscriptSegment

SAMPLE_RATE = 16000


def speech_like(seconds: float, freq: float = 220.0, amp: float = 0.4) -> np.ndarray:
    """Segnale continuo che il segmentatore riconosce come parlato."""
    n = int(seconds * SAMPLE_RATE)
    t = np.arange(n, dtype=np.float32) / SAMPLE_RATE
    wave_ = amp * np.sin(2 * np.pi * freq * t)
    # modulazione lenta: evita un tono puro, piu' vicino alla voce
    wave_ *= 1.0 + 0.3 * np.sin(2 * np.pi * 3.0 * t)
    return wave_.astype(np.float32)


def silence(seconds: float) -> np.ndarray:
    """Silenzio puro."""
    return np.zeros(int(seconds * SAMPLE_RATE), dtype=np.float32)


class TestSegmenterSilenceBoundary(unittest.TestCase):
    """Un silenzio >= silence_ms deve chiudere il segmento."""

    def setUp(self) -> None:
        self.config = ASRConfig()
        self.segmenter = Segmenter(self.config, sample_rate=SAMPLE_RATE)

    def test_speech_then_one_second_silence_closes_segment(self) -> None:
        """1 s di parlato + 1 s di silenzio -> un segmento restituito."""
        self.assertIsNone(self.segmenter.push(speech_like(1.0)))
        self.assertTrue(self.segmenter.in_speech)

        closed = self.segmenter.push(silence(1.0))

        self.assertIsNotNone(closed, "il silenzio doveva chiudere il segmento")
        assert closed is not None
        # il segmento contiene parlato + il silenzio che lo ha chiuso
        self.assertGreater(len(closed) / SAMPLE_RATE, 1.0)
        self.assertLess(len(closed) / SAMPLE_RATE, 3.0)
        self.assertFalse(self.segmenter.in_speech)
        self.assertEqual(self.segmenter.pending_seconds, 0.0)

    def test_boundary_lands_inside_the_silence(self) -> None:
        """Il confine deve trovarsi durante il silenzio, non prima."""
        self.segmenter.push(speech_like(1.0))
        closed = self.segmenter.push(silence(1.0))
        assert closed is not None
        # i campioni finali del segmento devono essere silenziosi
        tail = closed[-SAMPLE_RATE // 2 :]
        self.assertLess(float(np.abs(tail).max()), 1e-4)

    def test_short_silence_does_not_close(self) -> None:
        """Un silenzio piu' corto di silence_ms non chiude il segmento."""
        self.segmenter.push(speech_like(1.0))
        # 0,3 s: meno dei 700 ms di silence_ms
        self.assertIsNone(self.segmenter.push(silence(0.3)))
        self.assertTrue(self.segmenter.in_speech)


class TestSegmenterMinimumLength(unittest.TestCase):
    """I segmenti troppo brevi (min_speech_ms) vengono scartati.

    Nota sulla semantica: la soglia ``min_speech_ms`` confronta la
    lunghezza dell'intero buffer accumulato (parlato piu' il silenzio che
    lo ha chiuso), non la sola durata del parlato. Per verificare lo
    scarto si accorcia quindi anche ``silence_ms``, altrimenti il
    silenzio di chiusura da solo farebbe superare la soglia.
    """

    def setUp(self) -> None:
        self.config = ASRConfig()
        self.segmenter = Segmenter(self.config, sample_rate=SAMPLE_RATE)

    def test_too_short_segment_is_discarded(self) -> None:
        """100 ms di parlato + 100 ms di silenzio: sotto min_speech_ms."""
        config = ASRConfig(silence_ms=50, min_speech_ms=250)
        segmenter = Segmenter(config, sample_rate=SAMPLE_RATE)

        segmenter.push(speech_like(0.1))
        closed = segmenter.push(silence(0.1))

        self.assertIsNone(closed, "un segmento da 200 ms doveva essere scartato")

    def test_flush_of_too_short_segment_is_discarded(self) -> None:
        """Anche flush() applica il minimo di lunghezza."""
        self.segmenter.push(speech_like(0.1))
        self.assertIsNone(self.segmenter.flush())

    def test_short_speech_with_long_silence_is_discarded(self) -> None:
        """100 ms di parlato chiusi da 1 s di silenzio: viene scartato.

        Il filtro guarda il **parlato**, non la durata del buffer. Prima
        misurava ``len(audio)``, e la coda di silenzio faceva passare il
        minimo: un colpo di tosse da 100 ms chiuso da 1 s di silenzio
        veniva trascritto, sprecando ~3 s di decodifica Whisper.
        """
        self.segmenter.push(speech_like(0.1))
        closed = self.segmenter.push(silence(1.0))
        self.assertIsNone(closed)

    def test_speech_above_minimum_survives_long_silence(self) -> None:
        """400 ms di parlato (> 250) chiusi da 1 s di silenzio: conservato.

        Il parlato basta a superare il minimo, il silenzio in coda non
        conta ne' per ne' contro.
        """
        config = ASRConfig(silence_ms=800, min_speech_ms=250)
        segmenter = Segmenter(config, sample_rate=SAMPLE_RATE)
        segmenter.push(speech_like(0.4))
        closed = segmenter.push(silence(1.0))
        self.assertIsNotNone(closed)
        assert closed is not None
        self.assertGreaterEqual(len(closed) / SAMPLE_RATE, 1.0)

    def test_segment_above_minimum_is_kept(self) -> None:
        """400 ms (> min_speech_ms = 250) vengono conservati."""
        self.segmenter.push(speech_like(0.4))
        closed = self.segmenter.flush()
        self.assertIsNotNone(closed)
        assert closed is not None
        self.assertGreaterEqual(len(closed) / SAMPLE_RATE, 0.25)


class TestSegmenterMaxLength(unittest.TestCase):
    """max_segment_s forza la chiusura anche senza silenzio."""

    def test_max_segment_s_forces_close(self) -> None:
        """Con max_segment_s = 1 s il segmento viene chiuso a 1 s."""
        config = ASRConfig(max_segment_s=1.0)
        segmenter = Segmenter(config, sample_rate=SAMPLE_RATE)

        closed = None
        for _ in range(3):
            closed = segmenter.push(speech_like(1.0))
            if closed is not None:
                break

        self.assertIsNotNone(closed, "max_segment_s avrebbe dovuto chiudere")
        assert closed is not None
        self.assertLessEqual(len(closed) / SAMPLE_RATE, 1.05)

    def test_max_segment_s_respected_over_long_speech(self) -> None:
        """Nessun segmento supera max_segment_s di piu' di un blocco."""
        config = ASRConfig(max_segment_s=2.0)
        segmenter = Segmenter(config, sample_rate=SAMPLE_RATE)

        lengths: list[float] = []
        for _ in range(10):
            closed = segmenter.push(speech_like(1.0))
            if closed is not None:
                lengths.append(len(closed) / SAMPLE_RATE)

        self.assertTrue(lengths, "nessun segmento chiuso")
        for length in lengths:
            self.assertLessEqual(length, 2.05)


class TestSegmenterFlush(unittest.TestCase):
    """flush() chiude il segmento pendente."""

    def setUp(self) -> None:
        self.config = ASRConfig()
        self.segmenter = Segmenter(self.config, sample_rate=SAMPLE_RATE)

    def test_flush_closes_pending_segment(self) -> None:
        self.segmenter.push(speech_like(1.0))
        self.assertGreater(self.segmenter.pending_seconds, 0.0)

        closed = self.segmenter.flush()

        self.assertIsNotNone(closed)
        assert closed is not None
        self.assertAlmostEqual(len(closed) / SAMPLE_RATE, 1.0, delta=0.05)
        self.assertEqual(self.segmenter.pending_seconds, 0.0)
        self.assertFalse(self.segmenter.in_speech)

    def test_flush_on_empty_segmenter_returns_none(self) -> None:
        self.assertIsNone(self.segmenter.flush())

    def test_flush_is_idempotent(self) -> None:
        self.segmenter.push(speech_like(1.0))
        self.assertIsNotNone(self.segmenter.flush())
        self.assertIsNone(self.segmenter.flush())

    def test_flush_after_close_returns_none(self) -> None:
        self.segmenter.push(speech_like(1.0))
        self.assertIsNotNone(self.segmenter.push(silence(1.0)))
        self.assertIsNone(self.segmenter.flush())


class TestSegmenterCounters(unittest.TestCase):
    """I contatori pubblici del segmentatore."""

    def test_speech_seconds_and_pending_seconds(self) -> None:
        config = ASRConfig()
        segmenter = Segmenter(config, sample_rate=SAMPLE_RATE)

        segmenter.push(speech_like(1.0))
        self.assertGreater(segmenter.speech_seconds, 0.5)
        self.assertAlmostEqual(segmenter.pending_seconds, 1.0, delta=0.05)

        segmenter.push(silence(1.0))
        self.assertAlmostEqual(segmenter.pending_seconds, 0.0)

    def test_empty_push_is_ignored(self) -> None:
        config = ASRConfig()
        segmenter = Segmenter(config, sample_rate=SAMPLE_RATE)
        self.assertIsNone(segmenter.push(np.zeros(0, dtype=np.float32)))
        self.assertEqual(segmenter.pending_seconds, 0.0)


class TestTranscriptSegment(unittest.TestCase):
    """Il dataclass del segmento espone i timestamp."""

    def test_timestamp_format(self) -> None:
        seg = TranscriptSegment(index=0, start=65.0, end=67.5, text="ciao")
        self.assertEqual(seg.timestamp, "01:05")

    def test_timestamp_at_zero(self) -> None:
        seg = TranscriptSegment(index=0, start=0.0, end=1.0, text="ciao")
        self.assertEqual(seg.timestamp, "00:00")

    def test_default_is_final(self) -> None:
        seg = TranscriptSegment(index=0, start=0.0, end=1.0, text="ciao")
        self.assertTrue(seg.final)
        self.assertEqual(seg.words, [])


if __name__ == "__main__":
    unittest.main()
