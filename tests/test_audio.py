"""Test dell'acquisizione audio, senza aprire mai un microfono reale.

Il ``sounddevice.InputStream`` viene sostituito con un doppietto finto:
il test verifica il percorso del callback, l'applicazione del
risampionamento e il comportamento della coda quando il consumatore
non tiene il passo (``overruns``) invece di bloccare il thread realtime.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

# rende importabile ``lecture_transcriber`` (layout src/) e installa la
# shim di compatibilita' per soxr, anche sotto ``python -m unittest``
import _bootstrap  # noqa: F401,E402  (importa dopo l'inserimento nel path)

import numpy as np

from lecture_transcriber import audio as audio_module
from lecture_transcriber.audio import AudioCapture, InputDevice, list_input_devices


class FakeStream:
    """Doppietto di ``sounddevice.InputStream``."""

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.started = False
        self.stopped = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True


def tone(frames: int, rate: int, freq: float = 440.0, amp: float = 0.5) -> np.ndarray:
    """Tono mono float32."""
    t = np.arange(frames, dtype=np.float32) / rate
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


class AudioCaptureTestCase(unittest.TestCase):
    """Base: nessun dispositivo reale viene aperto."""

    def setUp(self) -> None:
        self._patcher = mock.patch.object(audio_module.sd, "InputStream", FakeStream)
        self._patcher.start()

    def tearDown(self) -> None:
        self._patcher.stop()


class TestCallbackPath(AudioCaptureTestCase):
    """Il callback PortAudio produce blocchi alla frequenza di uscita."""

    def setUp(self) -> None:
        super().setUp()
        self.capture = AudioCapture(
            sample_rate=48000, target_rate=16000, channels=2, block_ms=100
        )

    def test_callback_resamples_and_queues(self) -> None:
        """Un blocco stereo a 48 kHz diventa mono a 16 kHz in coda."""
        frames = 4800  # 100 ms a 48 kHz
        stereo = np.stack([tone(frames, 48000), tone(frames, 48000)], axis=1)

        self.capture._callback(stereo, frames, None, None)

        block = self.capture.read(timeout=1.0)
        self.assertIsNotNone(block)
        assert block is not None
        # 100 ms a 16 kHz (tolleranza per il ritardo algoritmico del resampler)
        self.assertAlmostEqual(len(block) / 16000, 0.1, delta=0.02)
        self.assertEqual(block.dtype, np.float32)
        self.assertEqual(block.ndim, 1, "il blocco deve essere mono")

    def test_callback_accepts_mono_input(self) -> None:
        frames = 4800
        mono = tone(frames, 48000)

        self.capture._callback(mono, frames, None, None)

        block = self.capture.read(timeout=1.0)
        self.assertIsNotNone(block)
        assert block is not None
        self.assertAlmostEqual(len(block) / 16000, 0.1, delta=0.02)

    def test_callback_updates_the_level_meter(self) -> None:
        frames = 4800
        mono = tone(frames, 48000, amp=0.5)

        self.capture._callback(mono, frames, None, None)

        rms, peak = self.capture.level
        self.assertGreater(rms, 0.0)
        self.assertGreater(peak, 0.0)
        self.assertLessEqual(peak, 1.0 + 1e-6)
        # per un seno puro rms = picco / sqrt(2)
        self.assertAlmostEqual(rms, peak / np.sqrt(2.0), delta=0.02)

    def test_callback_preserves_the_signal(self) -> None:
        """Un tono a 440 Hz deve restare a 440 Hz dopo il risampionamento."""
        frames = 48000  # 1 s
        mono = tone(frames, 48000, amp=0.5)

        self.capture._callback(mono, frames, None, None)
        block = self.capture.read(timeout=1.0)
        assert block is not None

        spectrum = np.abs(np.fft.rfft(block * np.hanning(len(block))))
        peak_hz = float(np.argmax(spectrum)) * 16000 / len(block)
        self.assertAlmostEqual(peak_hz, 440.0, delta=5.0)

    def test_callback_survives_a_status_error(self) -> None:
        """Uno status di underflow non deve far fallire il callback."""
        frames = 4800
        mono = tone(frames, 48000)
        self.capture._callback(mono, frames, None, "underflow")
        self.assertIsNotNone(self.capture.read(timeout=1.0))

    def test_callback_survives_malformed_data(self) -> None:
        """Un blocco non numerico viene ignorato, senza eccezioni."""
        self.capture._callback(np.array([["a", "b"]]), 1, None, None)
        self.assertIsNone(self.capture.read(timeout=0.1))


class TestQueueOverrun(AudioCaptureTestCase):
    """Una coda piena deve contare un overrun, non bloccare."""

    def setUp(self) -> None:
        super().setUp()
        self.capture = AudioCapture(
            sample_rate=48000, target_rate=16000, channels=1, block_ms=10, queue_size=4
        )

    def _fill(self, count: int) -> None:
        frames = 480  # 10 ms a 48 kHz
        for _ in range(count):
            self.capture._callback(tone(frames, 48000), frames, None, None)

    def test_overruns_start_at_zero(self) -> None:
        self.assertEqual(self.capture.overruns, 0)

    def test_filling_the_queue_does_not_overrun(self) -> None:
        self._fill(4)
        self.assertEqual(self.capture.overruns, 0)

    def test_overflowing_the_queue_increments_overruns(self) -> None:
        self._fill(10)
        self.assertGreater(self.capture.overruns, 0)

    def test_callback_never_blocks_when_the_queue_is_full(self) -> None:
        """Il callback deve tornare subito anche a coda piena."""
        self._fill(4)
        import time

        t0 = time.monotonic()
        self._fill(20)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 1.0, "il callback realtime non deve bloccare")

    def test_queue_keeps_the_newest_block(self) -> None:
        """Scartato il piu' vecchio, il blocco piu' recente resta in coda."""
        frames = 480
        marker = np.full(frames, 0.9, dtype=np.float32)
        self._fill(4)
        self.capture._callback(marker, frames, None, None)
        self.assertIsNotNone(self.capture.read(timeout=1.0))


class TestLifecycle(AudioCaptureTestCase):
    """Avvio e arresto del flusso (con stream finto)."""

    def test_start_opens_the_stream(self) -> None:
        capture = AudioCapture(block_ms=100)
        capture.start()
        try:
            self.assertTrue(capture.running)
            self.assertIsNone(capture.error)
            self.assertEqual(capture._stream.kwargs["samplerate"], 48000)
            self.assertEqual(capture._stream.kwargs["channels"], 2)
            self.assertEqual(capture._stream.kwargs["dtype"], "float32")
            self.assertEqual(capture._stream.kwargs["blocksize"], 4800)
        finally:
            capture.stop()

    def test_start_is_idempotent(self) -> None:
        capture = AudioCapture()
        capture.start()
        stream = capture._stream
        capture.start()
        try:
            self.assertIs(capture._stream, stream)
        finally:
            capture.stop()

    def test_stop_closes_the_stream_and_clears_the_queue(self) -> None:
        capture = AudioCapture(block_ms=100)
        capture.start()
        frames = 4800
        capture._callback(tone(frames, 48000), frames, None, None)
        self.assertIsNotNone(capture.read(timeout=1.0))

        capture.stop()

        self.assertFalse(capture.running)
        self.assertIsNone(capture._stream)
        self.assertEqual(len(capture.read_all()), 0)

    def test_start_failure_sets_the_error(self) -> None:
        def boom(**kwargs):
            raise RuntimeError("dispositivo assente")

        with mock.patch.object(audio_module.sd, "InputStream", boom):
            capture = AudioCapture()
            with self.assertRaises(RuntimeError):
                capture.start()
        self.assertIsNotNone(capture.error)
        self.assertFalse(capture.running)


class TestConsumption(AudioCaptureTestCase):
    """I metodi di consumo della coda."""

    def setUp(self) -> None:
        super().setUp()
        self.capture = AudioCapture(sample_rate=48000, target_rate=16000, block_ms=100)

    def _push(self, frames: int = 4800) -> None:
        self.capture._callback(tone(frames, 48000), frames, None, None)

    def test_read_returns_none_on_timeout(self) -> None:
        self.assertIsNone(self.capture.read(timeout=0.05))

    def test_read_all_concatenates_blocks(self) -> None:
        self._push()
        self._push()
        total = self.capture.read_all()
        self.assertAlmostEqual(len(total) / 16000, 0.2, delta=0.05)

    def test_read_all_on_empty_queue_returns_empty_array(self) -> None:
        total = self.capture.read_all()
        self.assertEqual(len(total), 0)
        self.assertEqual(total.dtype, np.float32)

    def test_drain_collects_the_requested_duration(self) -> None:
        for _ in range(10):
            self._push()
        chunk = self.capture.drain(0.5)
        self.assertAlmostEqual(len(chunk) / 16000, 0.5, delta=0.05)


class TestListInputDevices(unittest.TestCase):
    """L'enumerazione dei dispositivi non deve mai sollevare."""

    def test_returns_a_list_of_input_devices(self) -> None:
        devices = list_input_devices()
        self.assertIsInstance(devices, list)
        for dev in devices:
            self.assertIsInstance(dev, InputDevice)
            self.assertIsInstance(dev.index, int)
            self.assertIsInstance(dev.name, str)
            self.assertGreaterEqual(dev.channels, 1)
            self.assertIsInstance(dev.is_default, bool)

    def test_virtual_nodes_are_excluded(self) -> None:
        devices = list_input_devices()
        names = {d.name for d in devices}
        self.assertNotIn("pipewire", names)
        self.assertNotIn("default", names)

    def test_failure_is_swallowed(self) -> None:
        with mock.patch.object(
            audio_module.sd, "query_devices", side_effect=RuntimeError("boom")
        ):
            self.assertEqual(list_input_devices(), [])


if __name__ == "__main__":
    unittest.main()
