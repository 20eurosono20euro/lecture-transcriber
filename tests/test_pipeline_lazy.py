"""Test della pipeline: avvio, caricamento modello, arresto.

Questi test coprono due difetti reali trovati per ispezione:

1. **Il modello veniva caricato all'apertura della finestra.** ``open()``
   chiamava ``load_async()``, quindi aprire l'app per consultare una
   sessione vecchia costava ~1,1 GB di RAM. Il requisito e' "poche risorse
   quando inattivo". Ora il caricamento parte al primo "ascolta".

2. **La prima pressione di "ascolta" andava perduta.** Se il modello
   stava ancora caricando, ``start()`` metteva lo stato LOADING e usciva:
   nessuno onorava la richiesta, e l'utente doveva ripremere a mano.
   Ora la richiesta resta in attesa (``_start_when_ready``) e viene onorata
   da ``_on_model_ready``.

I test usano un motore finto: non scarichiamo ne carichiamo Whisper.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Callable
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _bootstrap  # noqa: F401,E402  (importa dopo l'inserimento nel path)

from lecture_transcriber import app as appmod  # noqa: E402
from lecture_transcriber.app import (  # noqa: E402
    ListenerState,
    PipelineCallbacks,
    PipelineConfig,
    TranscriptionPipeline,
)


class FakeEngine:
    """Motore ASR finto: conta le chiamate invece di decodificare."""

    def __init__(self, ready: bool = False, error: str | None = None) -> None:
        self._ready = ready
        self._error = error
        self.load_calls = 0
        self.is_loading = False
        self.on_ready = None

    @property
    def is_ready(self) -> bool:
        return self._ready

    @property
    def load_error(self) -> str | None:
        return self._error

    def load_async(self, on_ready=None):  # noqa: ANN001
        self.load_calls += 1
        self.is_loading = True
        self.on_ready = on_ready
        return None

    def finish_load(self) -> None:
        """Simula la fine del caricamento."""
        self._ready = True
        self.is_loading = False
        if self.on_ready is not None:
            self.on_ready()


def _make_pipeline(
    dispatcher: Callable[[Callable[[], None]], None] | None = None,
) -> TranscriptionPipeline:
    return TranscriptionPipeline(
        PipelineConfig(), PipelineCallbacks(), dispatcher=dispatcher
    )


class TestOpenDoesNotLoadTheModel(unittest.TestCase):
    """Aprire la finestra non deve caricare Whisper."""

    def test_open_does_not_start_the_load(self) -> None:
        pipeline = _make_pipeline()
        fake = FakeEngine()
        with mock.patch.object(pipeline, "_engine", fake, create=True):
            pipeline.open()
        self.assertEqual(fake.load_calls, 0, "open() non deve caricare il modello")

    def test_open_leaves_the_state_idle(self) -> None:
        pipeline = _make_pipeline()
        fake = FakeEngine()
        with mock.patch.object(pipeline, "_engine", fake, create=True):
            pipeline.open()
        self.assertEqual(pipeline.state, ListenerState.IDLE)

    def test_open_is_idempotent(self) -> None:
        """Aprire due volte non deve ricreare store o segmenter."""
        pipeline = _make_pipeline()
        pipeline.open()
        store = pipeline._store
        segmenter = pipeline._segmenter
        engine = pipeline._engine
        pipeline.open()
        self.assertIs(pipeline._store, store)
        self.assertIs(pipeline._segmenter, segmenter)
        self.assertIs(pipeline._engine, engine)


class TestStartLoadsTheModel(unittest.TestCase):
    """Il primo "ascolta" deve avviare il caricamento."""

    def test_start_triggers_the_load_when_not_ready(self) -> None:
        pipeline = _make_pipeline()
        pipeline.open()
        fake = FakeEngine()
        pipeline._engine = fake

        pipeline.start()

        self.assertEqual(fake.load_calls, 1)
        self.assertEqual(pipeline.state, ListenerState.LOADING)

    def test_start_does_not_load_twice(self) -> None:
        """Se sta gia' caricando, una seconda pressione non riavvia nulla."""
        pipeline = _make_pipeline()
        pipeline.open()
        fake = FakeEngine()
        pipeline._engine = fake

        pipeline.start()
        pipeline.start()

        self.assertEqual(fake.load_calls, 1)

    def test_load_error_is_surfaced(self) -> None:
        """Un errore di caricamento deve arrivare allo stato ERROR."""
        pipeline = _make_pipeline()
        pipeline.open()
        fake = FakeEngine(error="modello assente")
        pipeline._engine = fake

        pipeline.start()

        self.assertEqual(pipeline.state, ListenerState.ERROR)


class TestPendingStartIsHonoured(unittest.TestCase):
    """La richiesta di ascolto arrivata durante il caricamento non si perde."""

    def test_start_during_load_sets_the_pending_flag(self) -> None:
        pipeline = _make_pipeline()
        pipeline.open()
        pipeline._engine = FakeEngine()

        pipeline.start()

        self.assertTrue(pipeline._start_when_ready)

    def test_pending_start_is_cleared_after_the_load(self) -> None:
        pipeline = _make_pipeline()
        pipeline.open()
        fake = FakeEngine()
        pipeline._engine = fake
        pipeline.start()

        with mock.patch.object(pipeline, "start") as start_again:
            fake.finish_load()

        self.assertFalse(pipeline._start_when_ready)
        start_again.assert_not_called()  # la ripetizione la fa idle_add

    def test_model_ready_restarts_when_a_start_was_pending(self) -> None:
        """Il callback di fine caricamento deve rilanciare start()."""
        calls: list = []
        pipeline = _make_pipeline(dispatcher=lambda fn: calls.append(fn))
        pipeline.open()
        fake = FakeEngine()
        pipeline._engine = fake
        pipeline.start()
        self.assertTrue(pipeline._start_when_ready)

        fake.finish_load()

        self.assertEqual(len(calls), 1)
        self.assertFalse(pipeline._start_when_ready)

    def test_load_error_clears_the_pending_flag(self) -> None:
        """Se il modello non si carica, la richiesta pendente va scartata.

        Con l'errore gia' presente ``start()`` va subito in ERROR e non
        mette nulla in attesa: il flag resta falsi. Se invece la richiesta
        era pendente e il caricamento poi fallisce, ``_on_model_ready``
        deve scartarla, non provare a riavviare.
        """
        pipeline = _make_pipeline()
        pipeline.open()
        fake = FakeEngine(error="rete assente")
        pipeline._engine = fake

        pipeline.start()
        self.assertFalse(pipeline._start_when_ready)
        self.assertEqual(pipeline.state, ListenerState.ERROR)

        # ora il caso in cui la richiesta era pendente e il caricamento
        # fallisce dopo: il flag va azzerato.
        fake._error = "rete assente"
        pipeline._start_when_ready = True
        pipeline._on_model_ready()
        self.assertFalse(pipeline._start_when_ready)
        self.assertEqual(pipeline.state, ListenerState.ERROR)


class TestDefaultDispatcher(unittest.TestCase):
    """Il dispatcher predefinito puo' girare senza GTK (fallback inline).

    E' il cuore della portabilita': una pipeline costruita su una
    macchina senza PyGObject deve comunque onorare la richiesta di
    ascolto pendente eseguendo la funzione direttamente sul thread di
    caricamento, invece di tacerla.
    """

    def test_default_dispatcher_executes_inline_when_gi_missing(self) -> None:
        calls: list = []
        dispatched = appmod._default_dispatcher()

        with mock.patch.dict("sys.modules", {"gi": None}):
            # simuliamo l'assenza di gi: l'import fallisce e il dispatcher
            # deve eseguire inline
            dispatched(lambda: calls.append("ok"))

        self.assertEqual(calls, ["ok"])

    def test_default_dispatcher_prefers_idle_add_when_gi_present(self) -> None:
        calls: list = []
        dispatched = appmod._default_dispatcher()

        # quando gi esiste, il dispatcher deve passare dal main loop:
        # simuliamo con un idle_add finto
        with mock.patch(
            "gi.repository.GLib.idle_add", lambda fn: calls.append(fn)
        ):
            dispatched(lambda: None)
        self.assertEqual(len(calls), 1)


class _FakeCapture:
    """Cattura finta: simula i contatori di AudioCapture."""

    def __init__(self, overruns: int = 0, glitches: int = 0) -> None:
        self._overruns = overruns
        self._glitches = glitches

    @property
    def overruns(self) -> int:
        return self._overruns

    @property
    def glitches(self) -> int:
        return self._glitches

    def stop(self) -> None:
        pass


class TestDroppedAudioIsReported(unittest.TestCase):
    """L'audio scartato va detto, non solo contato.

    ``AudioCapture`` contava gli overrun (blocchi buttati perche' l'ASR
    non teneva il passo) e i glitch (campioni NaN/Inf), ma **nessuno
    leggeva quei contatori**: la trascrizione perdeva parole in silenzio.
    Il transcript e' il prodotto dell'applicazione, quindi la sua
    incompletezza deve essere visibile.
    """

    def test_a_clean_session_says_nothing(self) -> None:
        pipeline = _make_pipeline()
        self.assertEqual(pipeline._drop_summary(_FakeCapture(overruns=0, glitches=0)), "")

    def test_overruns_are_reported_with_the_lost_time(self) -> None:
        pipeline = _make_pipeline()
        message = pipeline._drop_summary(_FakeCapture(overruns=7))
        self.assertIn("7 blocchi audio scartati", message)
        # 7 blocchi da 100 ms = 0,7 s
        self.assertIn("0.7 s", message)
        self.assertIn("incompleta", message)

    def test_glitches_are_reported_separately(self) -> None:
        pipeline = _make_pipeline()
        message = pipeline._drop_summary(_FakeCapture(glitches=3))
        self.assertIn("3 blocchi con campioni corrotti", message)
        self.assertNotIn("scartati", message)

    def test_both_are_reported_together(self) -> None:
        pipeline = _make_pipeline()
        message = pipeline._drop_summary(_FakeCapture(overruns=2, glitches=1))
        self.assertIn("2 blocchi audio scartati", message)
        self.assertIn("1 blocchi con campioni corrotti", message)

    def test_the_summary_reaches_the_state_message_on_stop(self) -> None:
        """Deve finire nel messaggio di stato, non solo in un metodo."""
        pipeline = _make_pipeline()
        pipeline._capture = _FakeCapture(overruns=4)
        seen: list[tuple[object, str]] = []
        pipeline.cb.on_state = lambda state, message: seen.append((state, message))
        with mock.patch.object(type(pipeline), "elapsed", 12.0):
            pipeline.stop()
        stopped = [msg for state, msg in seen if state == ListenerState.STOPPED]
        self.assertTrue(stopped, "stop() non ha emesso lo stato STOPPED")
        self.assertIn("4 blocchi audio scartati", stopped[-1])


if __name__ == "__main__":
    unittest.main()
