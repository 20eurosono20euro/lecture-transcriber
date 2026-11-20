"""Test della registrazione audio opzionale (``Settings.record_audio``).

Difetto coperto: la preferenza prometteva per iscritto che l'audio della
sessione finisse in ``AUDIO_DIR/<sessione>.wav`` — la casella si accendeva,
il toast diceva "l'audio viene scritto su disco" — e **nessun byte veniva
scritto**. Una promessa sulla privacy lasciata finta e' peggio di una
funzione assente: chi la usa crede di avere una registrazione e non ce l'ha.

I test verificano il file prodotto (intestazione WAV, frequenza, campioni)
e il collegamento con la pipeline: l'audio che alimenta la trascrizione e'
lo stesso che finisce nel WAV, e un errore di scrittura non deve fermare la
trascrizione.
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _bootstrap  # noqa: F401,E402  (importa dopo l'inserimento nel path)

import numpy as np  # noqa: E402

from lecture_transcriber import app as appmod  # noqa: E402
from lecture_transcriber.app import (  # noqa: E402
    ListenerState,
    PipelineCallbacks,
    PipelineConfig,
    TranscriptionPipeline,
)
from lecture_transcriber.config import Settings  # noqa: E402
from lecture_transcriber.recorder import WavRecorder  # noqa: E402


def _read_wav(path: Path) -> tuple[wave._wave_params, np.ndarray]:
    with wave.open(str(path), "rb") as wav:
        params = wav.getparams()
        raw = wav.readframes(params.nframes)
    return params, np.frombuffer(raw, dtype="<i2")


class TestWavRecorder(unittest.TestCase):
    """Il file deve essere un WAV PCM 16 bit mono valido e leggibile."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "sotto" / "cartella" / "lezione.wav"

    def test_writing_creates_a_readable_16k_mono_wav(self) -> None:
        with WavRecorder(self.path) as rec:
            rec.write(np.zeros(1600, dtype=np.float32))
        params, samples = _read_wav(self.path)
        self.assertEqual(params.nchannels, 1)
        self.assertEqual(params.sampwidth, 2)
        self.assertEqual(params.framerate, 16000)
        self.assertEqual(params.nframes, 1600)
        self.assertEqual(samples.size, 1600)

    def test_the_parent_directory_is_created_lazily(self) -> None:
        # nessuna cartella audio per chi non registra, ma se registra deve
        # esistere: la creazione sta in open(), non in import.
        self.assertFalse(self.path.parent.exists())
        with WavRecorder(self.path):
            pass
        self.assertTrue(self.path.parent.is_dir())

    def test_amplitude_survives_the_conversion(self) -> None:
        tone = np.full(100, 0.5, dtype=np.float32)
        with WavRecorder(self.path) as rec:
            rec.write(tone)
        _, samples = _read_wav(self.path)
        # 0,5 * 32767 = 16383,5 -> 16383 in troncamento
        self.assertTrue(np.all(samples == 16383), f"campioni: {samples[:3]}")

    def test_out_of_range_samples_are_clipped_not_wrapped(self) -> None:
        """Un valore oltre il fondo scala non deve "girare" e fare un click."""
        with WavRecorder(self.path) as rec:
            rec.write(np.array([2.0, -2.0], dtype=np.float32))
        _, samples = _read_wav(self.path)
        self.assertEqual(list(samples), [32767, -32767])

    def test_non_finite_samples_do_not_produce_garbage(self) -> None:
        """NaN/Inf non sono convertibili in int16: devono diventare silenzio.

        Il cast di un NaN e' indefinito: produceva un campione spazzatura e
        un ``RuntimeWarning: invalid value encountered in cast``.
        """
        with WavRecorder(self.path) as rec:
            rec.write(np.array([np.nan, np.inf, -np.inf], dtype=np.float32))
        _, samples = _read_wav(self.path)
        self.assertEqual(list(samples), [0, 0, 0])

    def test_empty_and_missing_blocks_are_ignored(self) -> None:
        with WavRecorder(self.path) as rec:
            rec.write(None)
            rec.write(np.zeros(0, dtype=np.float32))
        self.assertEqual(rec.frames_written, 0)

    def test_close_is_idempotent(self) -> None:
        rec = WavRecorder(self.path)
        rec.open()
        rec.write(np.zeros(10, dtype=np.float32))
        rec.close()
        rec.close()
        self.assertFalse(rec.active)
        params, _ = _read_wav(self.path)
        self.assertEqual(params.nframes, 10)

    def test_frames_are_counted_at_the_target_rate(self) -> None:
        with WavRecorder(self.path, sample_rate=16000) as rec:
            rec.write(np.zeros(16000, dtype=np.float32))
        self.assertEqual(rec.frames_written, 16000)


class _NullSegmenter:
    """Segmenter che non chiude mai un segmento."""

    sample_rate = 16000
    pending_seconds = 0.0
    pending_audio = np.zeros(0, dtype=np.float32)

    def push(self, _block):  # noqa: ANN001, ANN201
        return None

    def flush(self):  # noqa: ANN201
        return None


class _ScriptedCapture:
    """Cattura che consegna i blocchi indicati e poi ferma la pipeline."""

    def __init__(self, blocks, stop_event) -> None:  # noqa: ANN001
        self._blocks = list(blocks)
        self._stop = stop_event
        self.overruns = 0
        self.glitches = 0

    def read(self, timeout: float = 0.2):  # noqa: ANN201
        if not self._blocks:
            self._stop.set()
            return None
        block = self._blocks.pop(0)
        if not self._blocks:
            self._stop.set()
        return block

    def stop(self) -> None:
        pass


class TestPipelineRecording(unittest.TestCase):
    """La pipeline deve alimentare il WAV quando la preferenza e' accesa."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        patcher = mock.patch.object(appmod._config, "AUDIO_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _pipeline(self, record: bool = True) -> TranscriptionPipeline:
        settings = Settings()
        settings.record_audio = record
        return TranscriptionPipeline(
            PipelineConfig(settings=settings),
            PipelineCallbacks(),
            dispatcher=lambda fn: fn(),
        )

    def test_record_audio_off_opens_nothing(self) -> None:
        pipeline = self._pipeline(record=False)
        pipeline._open_recorder()
        self.assertIsNone(pipeline.recording_path)
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_record_audio_on_opens_a_named_file(self) -> None:
        pipeline = self._pipeline()
        pipeline.session.session_id = 42
        pipeline._open_recorder()
        self.assertEqual(pipeline.recording_path, self.dir / "lezione-42.wav")

    def test_the_capture_loop_feeds_the_wav(self) -> None:
        """Regressione del difetto: i blocchi ascoltati devono finire su disco."""
        pipeline = self._pipeline()
        pipeline.session.session_id = 7
        pipeline._segmenter = _NullSegmenter()
        block = np.full(160, 0.25, dtype=np.float32)
        pipeline._capture = _ScriptedCapture([block], pipeline._stop_flag)

        pipeline._open_recorder()
        pipeline._capture_loop()
        # ``wave`` tiene i byte in un buffer da 8 KB: finche' non si chiude
        # il file su disco e' vuoto. Nella realta' lo chiude ``stop()``.
        pipeline._close_recorder()

        path = self.dir / "lezione-7.wav"
        self.assertTrue(path.is_file(), "la cattura non ha scritto il WAV")
        params, samples = _read_wav(path)
        self.assertEqual(params.framerate, 16000)
        self.assertEqual(samples.size, 160)

    def test_a_paused_session_does_not_grow_the_wav(self) -> None:
        """In pausa il thread resta nel ramo "salta": nessun blocco letto.

        Il ciclo in pausa non termina da solo (``continue`` con ``sleep``),
        quindi lo si fa girare su un thread vero e lo si ferma dal test:
        chiamarlo inline bloccherebbe la suite per sempre.
        """
        pipeline = self._pipeline()
        pipeline.session.session_id = 8
        pipeline._segmenter = _NullSegmenter()
        block = np.zeros(160, dtype=np.float32)
        pipeline._capture = _ScriptedCapture([block], pipeline._stop_flag)
        pipeline._open_recorder()
        pipeline._pause_flag.set()

        worker = threading.Thread(target=pipeline._capture_loop, daemon=True)
        worker.start()
        time.sleep(0.15)  # piu' di un giro del ciclo (sleep 0.05)
        pipeline._stop_flag.set()
        worker.join(timeout=2.0)

        self.assertFalse(worker.is_alive(), "il ciclo non esce dopo lo stop")
        # in pausa l'audio non alimenta la trascrizione: nemmeno il WAV
        self.assertEqual(pipeline._recorder.frames_written, 0)

    def test_disabling_mid_session_closes_the_file(self) -> None:
        pipeline = self._pipeline()
        pipeline.session.session_id = 9
        pipeline._open_recorder()
        path = pipeline.recording_path
        self.assertIsNotNone(path)

        pipeline.set_record_audio(False)

        self.assertIsNone(pipeline.recording_path)
        self.assertFalse(pipeline.cfg.settings.record_audio)
        self.assertTrue(path.is_file())

    def test_a_write_failure_stops_recording_but_not_the_transcript(self) -> None:
        """Disco pieno: si perde il WAV, non la lezione trascritta."""
        pipeline = self._pipeline()
        pipeline.session.session_id = 11
        seen: list[str] = []
        pipeline.cb.on_error = seen.append
        pipeline._open_recorder()
        recorder = pipeline._recorder
        self.assertIsNotNone(recorder)

        with mock.patch.object(recorder, "write", side_effect=OSError("disco pieno")):
            pipeline._record_block(np.zeros(10, dtype=np.float32))

        self.assertIsNone(pipeline._recorder)
        self.assertEqual(len(seen), 1)
        self.assertIn("disco pieno", seen[0])
        self.assertNotEqual(pipeline.state, ListenerState.ERROR)

    def test_an_open_failure_warns_instead_of_failing_the_session(self) -> None:
        pipeline = self._pipeline()
        seen: list[str] = []
        pipeline.cb.on_error = seen.append
        with mock.patch.object(
            appmod.WavRecorder, "open", side_effect=OSError("permesso negato")
        ):
            pipeline._open_recorder()
        self.assertIsNone(pipeline.recording_path)
        self.assertEqual(len(seen), 1)
        self.assertIn("permesso negato", seen[0])
        self.assertNotEqual(pipeline.state, ListenerState.ERROR)

    def test_the_summary_names_the_file_and_its_duration(self) -> None:
        pipeline = self._pipeline()
        pipeline.session.session_id = 13
        pipeline._open_recorder()
        pipeline._record_block(np.zeros(32000, dtype=np.float32))
        recorder = pipeline._recorder

        summary = pipeline._recording_summary(recorder)

        self.assertIn("lezione-13.wav", summary)
        self.assertIn("2 s", summary)

    def test_stop_mentions_the_recording_when_one_was_made(self) -> None:
        pipeline = self._pipeline()
        pipeline.session.session_id = 15
        pipeline._open_recorder()
        pipeline._record_block(np.zeros(16000, dtype=np.float32))
        messages: list[tuple[object, str]] = []
        pipeline.cb.on_state = lambda state, msg: messages.append((state, msg))

        pipeline.stop()

        stopped = [msg for state, msg in messages if state == ListenerState.STOPPED]
        self.assertTrue(stopped)
        self.assertIn("lezione-15.wav", stopped[-1])


if __name__ == "__main__":
    unittest.main()
