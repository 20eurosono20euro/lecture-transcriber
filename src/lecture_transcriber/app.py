"""Applicazione di trascrizione e orchestrazione della pipeline.

Il modulo contiene due pezzi nettamente separati:

``TranscriptionPipeline``
    La macchina a stati della trascrizione: cattura -> segmentazione ->
    decodifica -> persistenza -> inserimento. Non conosce GTK e puo' essere
    usata da una modalita' headless o da un futuro CLI.

``LectureTranscriberApp``
    Il sottosistema GTK4/libadwaita: crea la finestra, registra le azioni e
    traduce gli eventi della pipeline in aggiornamenti dell'interfaccia.

I due si parlano attraverso callback (nessuna dipendenza da segnali GTK
dentro la pipeline), cosi' la pipeline resta collaudabile senza display.

Costo della decodifica (misurato su questo sistema con
large-v3-turbo/int8): la decodifica Whisper riempie sempre una finestra di
30 s, quindi dura ~3 s **indipendentemente dalla lunghezza dell'audio**.
Due conseguenze governano il design:

* un'ipotesi provvisoria costa quanto un segmento finale: conviene
  calcolarla solo nei primi secondi del segmento, quando il testo e'
  ancora instabile;
* l'ipotesi provvisoria non deve mai ritardare la finalizzazione di un
  segmento chiuso: il thread parziale ha quindi sempre la precedenza
  revocata dal thread di cattura.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

# numpy NON e' importato in testa al file: qui serve solo per un'annotazione
# di tipo (``_handle_closed_segment``), e con ``from __future__ import
# annotations`` le annotazioni non vengono valutate. Tenerlo pero' bloccava
# l'apertura della finestra — misurato: senza numpy ``from
# lecture_transcriber.app import TranscriptionPipeline`` sollevava
# ``ImportError``, mentre il requisito e' che l'app si apra con un tap.
from . import config as _config
from .asr import ASRConfig, Segmenter, TranscriptSegment, TranscriptionEngine
from .audio import AudioCapture

logger = logging.getLogger(__name__)
from .insertion import InsertMethod, InsertResult, TextInserter, active_window_title
from .recorder import WavRecorder
from .sessions import SessionStore


class ListenerState(str, Enum):
    """Stato della macchina di ascolto."""

    IDLE = "idle"
    LOADING = "loading"
    LISTENING = "listening"
    PAUSED = "paused"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass
class SessionInfo:
    """Metadati della sessione corrente."""

    session_id: int | None = None
    course: str = ""
    professor: str = ""
    date: str = ""

    @property
    def title(self) -> str:
        base = self.course or "Lezione"
        if self.professor:
            return f"{base} — {self.professor}"
        return base


@dataclass
class PipelineCallbacks:
    """Callback che la pipeline invoca; ognuno e' facoltativo.

    Vengono sempre chiamati dal thread che produce l'evento: chi riceve deve
    fare il marshalling verso il main loop GTK.
    """

    on_segment: Callable[[TranscriptSegment], None] | None = None
    on_partial: Callable[[str], None] | None = None
    on_level: Callable[[float, float], None] | None = None
    on_state: Callable[[ListenerState, str], None] | None = None
    on_insert_result: Callable[[InsertResult], None] | None = None
    on_error: Callable[[str], None] | None = None


@dataclass
class PipelineConfig:
    """Parametri della pipeline, derivati dalle preferenze utente."""

    settings: _config.Settings = field(default_factory=_config.Settings)
    db_path: Path = field(default_factory=lambda: _config.DATA_DIR / "sessions.db")
    model_dir: Path = field(default_factory=lambda: Path(_config.MODEL_DIR))

    #: Soglia (in secondi di audio pendente) sotto la quale vale la pena
    #: calcolare l'ipotesi provvisoria: la decodifica costa ~3 s a
    #: prescindere della lunghezza, conviene farla solo all'inizio del
    #: segmento, quando il testo e' ancora instabile.
    partial_max_pending_s: float = 5.0

    #: Intervallo minimo fra due ipotesi provvisorie consecutive.
    partial_interval_s: float = 3.0

    #: Il testo provvisorio viene ricalcolato solo se il segmento e' cresciuto
    #: di almeno questo numero di secondi.
    partial_min_growth_s: float = 1.0

    #: Ritardo fra il controllo del thread parziale e il successivo.
    partial_poll_s: float = 0.5

    #: Quanto si attende la chiusura dei thread di lavoro.
    join_timeout_s: float = 15.0


def _default_dispatcher() -> Callable[[Callable[[], None]], None]:
    """Dispatcher predefinito: main loop GTK se raggiungibile, altrimenti inline.

    Ritorna una chiusura che, data una funzione, la esegue nel main loop
    GTK ``GLib.idle_add`` quando PyGObject e' importabile (Linux+GNOME);
    se GTK manca, la esegue direttamente sul thread corrente. In questo
    modo la pipeline non dipende piu' da GTK per funzionare: il main loop
    e' solo il veicolo preferito, non un requisito.
    """

    def dispatch(fn: Callable[[], None]) -> None:
        try:
            import gi

            gi.require_version("GLib", "2.0")
            from gi.repository import GLib

            GLib.idle_add(fn)
            return
        except Exception:
            fn()

    return dispatch


class TranscriptionPipeline:
    """Pipeline di trascrizione indipendente dalla GUI.

    Tre thread cooperano:

    * il thread di cattura legge i blocchi da :class:`AudioCapture` e li
      spinge nel :class:`Segmenter`; quando un segmento si chiude lo
      decodifica e lo emette;
    * il thread parziale calcola l'ipotesi provvisoria sul segmento ancora
      aperto, ma solo quando il segmento e' breve e solo se non sta gia'
      girando una decodifica;
    * il thread del timer del livello pubblica il VU meter.

    La decodifica e' sempre protetta dallo stesso lock: il thread parziale
    non deve mai ritardare la finalizzazione di un segmento chiuso, quindi
    se il segmento si chiude mentre una decodifica parziale e' in corso,
    la parziale viene abbandonata e si attende la sua fine solo per poter
    usare il lock in modo esclusivo.
    """

    def __init__(
        self,
        cfg: PipelineConfig | None = None,
        callbacks: PipelineCallbacks | None = None,
        dispatcher: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self.cfg = cfg or PipelineConfig()
        self.cb = callbacks or PipelineCallbacks()
        # Chi riceve gli eventi decide su quale thread eseguirli. Di default
        # si usa il main loop GTK quando c'e' (Linux+GNOME): senza GTK il
        # codice gira comunque, eseguendo inline. Un client headless (CLI,
        # test, backend Windows/macOS) puo' iniettare un dispatcher proprio.
        self._dispatcher = dispatcher or _default_dispatcher()

        self.state = ListenerState.IDLE
        self.error_message: str = ""

        self.session = SessionInfo()
        self._store: SessionStore | None = None
        self._exporter = None

        self._engine: TranscriptionEngine | None = None
        self._segmenter: Segmenter | None = None
        self._capture: AudioCapture | None = None
        # registrazione audio opzionale (``Settings.record_audio``)
        self._recorder: WavRecorder | None = None

        self._engine_lock = threading.Lock()
        self._partial_lock = threading.Lock()
        self._partial_running = False
        self._finalize_pending = False

        self._threads: list[threading.Thread] = []
        self._stop_flag = threading.Event()
        self._pause_flag = threading.Event()

        # "ascolta" premuto mentre il modello stava ancora caricando: la
        # richiesta resta qui e viene onorata da _on_model_ready.
        self._start_when_ready = False

        self._segment_index = 0
        # fallimenti consecutivi di scrittura su disco: tre e si ferma
        # la sessione, perche' l'autosave non sta funzionando
        self._save_failures = 0
        self._session_start = 0.0
        self._elapsed = 0.0
        self._last_partial_at = 0.0
        self._last_partial_len = 0.0
        self._partial_text = ""

        self._insert_enabled = False
        self._insert_paragraphs = True
        self._own_window = ""
        self._inserter = TextInserter()
        self._target_watcher: threading.Thread | None = None
        self._target_watch_stop = threading.Event()

        self._level_timer: threading.Timer | None = None

    # -- accessori ---------------------------------------------------------
    @property
    def settings(self) -> _config.Settings:
        return self.cfg.settings

    @property
    def engine(self) -> TranscriptionEngine | None:
        return self._engine

    @property
    def segmenter(self) -> Segmenter | None:
        return self._segmenter

    @property
    def store(self) -> SessionStore | None:
        return self._store

    @property
    def exporter(self):
        return self._exporter

    @property
    def elapsed(self) -> float:
        """Secondi trascorsi dall'avvio dell'ascolto (sessione corrente)."""
        if self._session_start:
            return self._elapsed + (time.monotonic() - self._session_start)
        return self._elapsed

    @property
    def is_listening(self) -> bool:
        return self.state == ListenerState.LISTENING

    @property
    def insert_enabled(self) -> bool:
        return self._insert_enabled

    @property
    def insert_armed(self) -> bool:
        """True se l'inserter e' armato su una finestra esterna valida."""
        return self._inserter.armed

    @property
    def segment_index(self) -> int:
        """Indice dell'ultimo segmento finalizzato."""
        return self._segment_index

    # -- stato -------------------------------------------------------------
    def _set_state(self, state: ListenerState, message: str = "") -> None:
        self.state = state
        self.error_message = message
        if self.cb.on_state is not None:
            self.cb.on_state(state, message)

    # -- ciclo di vita -----------------------------------------------------
    def open(self) -> None:
        """Prepara store e segmenter. Non avvia la cattura.

        Il modello Whisper **non** viene caricato qui: pesa ~1,1 GB di RAM
        e il requisito e' "poche risorse quando inattivo". Si carica al
        primo "ascolta" (:meth:`_ensure_model`), cosi' aprire la finestra
        per consultare una sessione vecchia non costa nulla.
        """
        _config.ensure_dirs()
        if self._store is None:
            self._store = SessionStore(self.cfg.db_path)
            try:
                from .export import Exporter

                self._exporter = Exporter(self._store)
            except ImportError:
                self._exporter = None

        s = self.cfg.settings
        asr_cfg = ASRConfig(
            model_name=s.model_name,
            compute_type=s.compute_type,
            device=s.device,
            language=s.language,
            silence_ms=s.vad_silence_ms,
            min_speech_ms=s.vad_min_speech_ms,
            max_segment_s=s.max_segment_s,
        )
        # ``Segmenter`` e ``TranscriptionEngine`` costruiscono subito
        # ``np.zeros`` (il buffer del segmenter) e tengono il modello, ma
        # con numpy assente la costruzione solleva. Deve restare un
        # errore **di stato**, non un'eccezione che risale fino a
        # ``do_activate``: misurato, ``main()`` stampava "L'app si apre
        # comunque" e poi moriva su
        # ``AttributeError: 'NoneType' object has no attribute 'zeros'``
        # — la finestra non compariva mai, e l'utente non poteva
        # consultare la cronologia ne' esportare. L'errore ora finisce
        # in ``error_message``, che l'interfaccia mostra con il rimedio.
        try:
            if self._segmenter is None:
                self._segmenter = Segmenter(asr_cfg, sample_rate=s.target_rate)
            if self._engine is None:
                self._engine = TranscriptionEngine(asr_cfg, str(self.cfg.model_dir))
        except RuntimeError as exc:
            self._set_state(ListenerState.ERROR, str(exc))
            if self.cb.on_error is not None:
                self.cb.on_error(str(exc))

    def _ensure_model(self) -> None:
        """Avvia il caricamento del modello se non e' gia' in corso.

        Se il modello sta caricando, la richiesta di ascolto resta in
        attesa (``_start_when_ready``) e viene onorata da
        :meth:`_on_model_ready`.
        """
        if self._engine is None:
            self.open()
        engine = self._engine
        # ``open()`` puo' non avercela fatta: l'errore e' gia' in stato, e
        # da qui non si puo' caricare niente. Uscire senza dirlo lascia la
        # finestra in "Caricamento del modello in corso" per sempre.
        if engine is None:
            if not self.error_message:
                self._set_state(
                    ListenerState.ERROR,
                    "Riconoscimento vocale non disponibile: esegui "
                    f"{_config.ensure_venv_hint()}.",
                )
            return
        if engine.is_ready or engine.is_loading:
            return
        self._set_state(ListenerState.LOADING, "Caricamento del modello in corso")
        engine.load_async(on_ready=self._on_model_ready)

    def _on_model_ready(self) -> None:
        engine = self._engine
        if engine is None:
            return
        if engine.load_error:
            self._start_when_ready = False
            self._set_state(ListenerState.ERROR, engine.load_error)
            if self.cb.on_error is not None:
                self.cb.on_error(engine.load_error)
            return
        if self.state == ListenerState.LOADING:
            self._set_state(ListenerState.IDLE, "")
        # se l'utente aveva premuto "ascolta" durante il caricamento, la
        # richiesta era stata messa in attesa: ora si puo' onorare.
        if self._start_when_ready:
            self._start_when_ready = False
            self._dispatcher(self._start_after_load)

    def _start_after_load(self) -> bool:
        """Onora la richiesta di ascolto arrivata durante il caricamento.

        Gira nel main loop (via ``GLib.idle_add``), non nel thread del
        caricamento: ``start()`` tocca lo stato dell'interfaccia e GTK non
        e' thread-safe.
        """
        if self.state == ListenerState.LOADING:
            self._set_state(ListenerState.IDLE, "")
        if self._engine is not None and self._engine.is_ready:
            self.start()
        return False

    def start(self) -> None:
        """Avvia la cattura e i thread di lavoro."""
        if self.state in (ListenerState.LISTENING, ListenerState.PAUSED):
            return
        if self._engine is None or self._segmenter is None:
            self.open()
        engine = self._engine
        if engine is None:
            # ``open()`` non ce l'ha fatta (numpy assente): l'errore e'
            # gia' in ``error_message``, non va sovrascritto. Tacere qui
            # significherebbe un "ascolta" che non fa nulla e non dice
            # perche'.
            if not self.error_message:
                self._set_state(
                    ListenerState.ERROR,
                    "Riconoscimento vocale non disponibile: esegui "
                    f"{_config.ensure_venv_hint()}.",
                )
                if self.cb.on_error is not None:
                    self.cb.on_error(self.error_message)
            return
        if not engine.is_ready:
            if engine.load_error:
                self._set_state(ListenerState.ERROR, engine.load_error)
                return
            # Il modello non e' pronto: invece di scartare la richiesta (la
            # prima pressione del tasto andrebbe perduta e l'utente dovrebbe
            # ripremere a mano), la si mette in attesa e la si onora appena il
            # caricamento finisce.
            self._start_when_ready = True
            self._ensure_model()
            return

        s = self.cfg.settings
        try:
            capture = AudioCapture(
                device=s.input_device,
                sample_rate=s.sample_rate,
                target_rate=s.target_rate,
            )
        except Exception as exc:
            # soxr/sounddevice assenti (venv non pronto): la costruzione
            # solleva. Deve uscire come errore di stato con un rimedio,
            # non come eccezione non catturata che chiude la finestra.
            self._set_state(ListenerState.ERROR, str(exc))
            if self.cb.on_error is not None:
                self.cb.on_error(str(exc))
            return
        try:
            capture.start()
        except Exception as exc:
            self._set_state(ListenerState.ERROR, str(exc))
            if self.cb.on_error is not None:
                self.cb.on_error(str(exc))
            return
        self._capture = capture

        if self._store is not None and self.session.session_id is None:
            sid = self._store.create_session(
                self.session.course, self.session.professor, self.session.date
            )
            self.session.session_id = sid

        # dopo la creazione della sessione: il nome del WAV usa il suo id
        self._open_recorder()

        self._segment_index = 0
        self._last_partial_at = 0.0
        self._last_partial_len = 0.0
        self._partial_text = ""
        self._session_start = time.monotonic()
        self._stop_flag.clear()
        self._pause_flag.clear()

        self._threads = [
            threading.Thread(target=self._capture_loop, name="lt-capture", daemon=True),
            threading.Thread(target=self._partial_loop, name="lt-partial", daemon=True),
        ]
        for t in self._threads:
            t.start()

        self._start_level_timer()
        self._set_state(ListenerState.LISTENING, "")

    def pause(self) -> None:
        """Sospende l'acquisizione senza chiudere la sessione.

        Non blocca: la chiusura dell'ultimo segmento e' fatta dal thread di
        cattura, perche' la decodifica dura ~3 s e non deve fermare la
        interfaccia.
        """
        if self.state != ListenerState.LISTENING:
            return
        self._pause_flag.set()
        self._set_state(ListenerState.PAUSED, "")

    def resume(self) -> None:
        """Riprende dopo una pausa."""
        if self.state != ListenerState.PAUSED:
            return
        self._pause_flag.clear()
        self._set_state(ListenerState.LISTENING, "")

    def stop(self) -> None:
        """Ferma tutto e chiude la sessione."""
        self._stop_flag.set()
        self._pause_flag.clear()

        capture = self._capture
        self._capture = None

        # il thread di cattura chiude l'ultimo segmento prima di uscire:
        # va joinato PRIMA di ``capture.stop()``, perche' ``stop()``
        # svuota la coda -- misurato: gli ultimi blocchi gia' acquisiti
        # (fino a ``queue_size`` = 200, cioe' 20 s di audio) venivano
        # buttati prima che il thread li leggesse, e l'ultima frase
        # pronunciata non finiva nella trascrizione.
        for t in self._threads:
            t.join(timeout=self.cfg.join_timeout_s)
        self._threads = []

        if capture is not None:
            try:
                capture.stop()
            except Exception:
                pass

        self._stop_level_timer()
        self._stop_target_watcher()

        # il WAV si chiude dopo il join: il thread di cattura potrebbe
        # avere ancora l'ultimo blocco in mano.
        recorder = self._recorder
        self._close_recorder()

        if self._store is not None and self.session.session_id is not None:
            try:
                self._store.end_session(self.session.session_id)
            except Exception:
                pass

        self._elapsed = self.elapsed
        self._session_start = 0.0
        parts = [
            self._drop_summary(capture),
            self._recording_summary(recorder),
        ]
        self._set_state(
            ListenerState.STOPPED, " ".join(p for p in parts if p)
        )

    @staticmethod
    def _drop_summary(capture: object) -> str:
        """Riassumo l'audio perso, se n'e' stato perso.

        ``AudioCapture`` conta gli overrun (blocchi scartati perche'
        l'ASR non teneva il passo) e i glitch (campioni NaN/Inf), ma
        nessuno leggeva quei contatori: la trascrizione perdeva parole in
        silenzio e l'utente non aveva modo di saperlo. Il transcript e'
        il prodotto dell'applicazione, quindi va detto quando e'
        incompleto. Non e' un errore — la registrazione continua — ma il
        messaggio deve essere visibile, non solo loggato.

        La cattura va **passata**: ``stop()`` azzera ``self._capture``
        prima di chiamare questo metodo, quindi leggerla da ``self``
        darebbe sempre ``None`` (misurato: il primo tentaggio di questo
        test falliva con ``''`` — il difetto era nel mio codice, non nel
        codice sotto test).
        """
        if capture is None:
            return ""
        overruns = getattr(capture, "overruns", 0) or 0
        glitches = getattr(capture, "glitches", 0) or 0
        if overruns <= 0 and glitches <= 0:
            return ""
        parts: list[str] = []
        if overruns > 0:
            # un blocco sono 100 ms a 16 kHz
            parts.append(
                f"{overruns} blocchi audio scartati "
                f"(~{overruns * 0.1:.1f} s): la trascrizione e' incompleta"
            )
        if glitches > 0:
            parts.append(
                f"{glitches} blocchi con campioni corrotti sostituiti con silenzio"
            )
        return "Attenzione: " + "; ".join(parts) + "."

    def close(self) -> None:
        """Rilascia le risorse. Chiamabile anche senza aver avviato nulla."""
        if self.state in (ListenerState.LISTENING, ListenerState.PAUSED):
            self.stop()
        self._close_recorder()
        self._stop_level_timer()
        self._stop_target_watcher()
        if self._store is not None:
            try:
                self._store.close()
            except Exception:
                pass
            self._store = None

    # -- metadati ----------------------------------------------------------
    def set_session_info(
        self, course: str = "", professor: str = "", date: str = ""
    ) -> None:
        """Aggiorna i metadati della sessione (prima o durante l'ascolto)."""
        self.session.course = course
        self.session.professor = professor
        self.session.date = date
        store = self._store
        if store is None or self.session.session_id is None:
            return
        # La tabella non ha un metodo di aggiornamento: si ricrea la
        # sessione solo se non e' ancora partita una trascrizione.
        if self._segment_index == 0:
            try:
                store.delete_session(self.session.session_id)
            except Exception:
                pass
            self.session.session_id = store.create_session(course, professor, date)

    # -- inserimento --------------------------------------------------------
    def set_insert_enabled(
        self,
        enabled: bool,
        expected_window: str | None = None,
        own_window: str = "",
    ) -> None:
        """Accende o spegne l'inserimento nel documento esterno.

        ``own_window`` e' il titolo della nostra finestra: se la finestra
        attiva e' la nostra, il bersaglio non e' ancora noto e si attende
        che l'utente passi all'applicazione delle note.
        """
        self._insert_enabled = enabled
        self._own_window = own_window or ""
        if not enabled:
            self._stop_target_watcher()
            self._inserter.disarm()
            return

        target = expected_window if expected_window is not None else active_window_title()
        if self._is_own_window(target):
            # siamo noi in primo piano: il bersaglio si registra quando
            # l'utente apre l'app delle note
            self._inserter.disarm()
            self._start_target_watcher()
            return
        self._stop_target_watcher()
        self._inserter.arm(target)

    def _is_own_window(self, title: str) -> bool:
        if not title or not self._own_window:
            return False
        return title.strip() == self._own_window.strip()

    def _start_target_watcher(self) -> None:
        """Registra il bersaglio appena l'utente lascia la nostra finestra."""
        self._stop_target_watcher()
        self._target_watch_stop.clear()

        def watch() -> None:
            while not self._target_watch_stop.is_set():
                self._target_watch_stop.wait(0.5)
                if self._target_watch_stop.is_set():
                    return
                if not self._insert_enabled:
                    return
                title = active_window_title()
                if title and not self._is_own_window(title):
                    self._inserter.arm(title)
                    self._target_watch_stop.set()
                    return

        self._target_watcher = threading.Thread(
            target=watch, name="lt-insert-target", daemon=True
        )
        self._target_watcher.start()

    def _stop_target_watcher(self) -> None:
        self._target_watch_stop.set()
        watcher = self._target_watcher
        self._target_watcher = None
        if watcher is not None and watcher is not threading.current_thread():
            watcher.join(timeout=1.0)

    def set_insert_paragraphs(self, paragraphs: bool) -> None:
        self._insert_paragraphs = paragraphs

    def set_insert_method(self, method: InsertMethod) -> None:
        self._inserter.method = method

    # -- thread di cattura --------------------------------------------------
    def _capture_loop(self) -> None:
        capture = self._capture
        if capture is None:
            return
        flushed = False
        while not self._stop_flag.is_set():
            if self._pause_flag.is_set():
                if not flushed:
                    flushed = True
                    self._flush_segment()
                time.sleep(0.05)
                continue
            flushed = False
            try:
                block = capture.read(timeout=0.2)
            except Exception as exc:
                self._report_error(f"Errore di cattura: {exc}")
                return
            if block is None:
                continue
            if self._pause_flag.is_set():
                continue
            # solo i blocchi che alimentano la trascrizione finiscono anche
            # nel WAV: cosi' la registrazione e il transcript coprono lo
            # stesso intervallo (una pausa non aggiunge audio fantasma).
            self._record_block(block)
            try:
                closed = self._segmenter.push(block) if self._segmenter else None
            except Exception as exc:
                self._report_error(f"Errore di segmentazione: {exc}")
                return
            if closed is not None:
                self._handle_closed_segment(closed)

        # ultima chiusura: il segmento ancora aperto non va perso
        self._flush_segment()

    def _handle_closed_segment(self, audio: np.ndarray) -> None:
        """Decodifica un segmento chiuso e lo emette come finale."""
        engine = self._engine
        if engine is None:
            return

        # la finalizzazione ha la precedenza: il thread parziale non deve
        # iniziare altre decodifiche finche' questa non e' finita.
        self._finalize_pending = True
        try:
            with self._engine_lock:
                segments = engine.transcribe(audio, final=True)
        finally:
            self._finalize_pending = False

        if not segments:
            self._clear_partial()
            return

        for seg in segments:
            self._segment_index += 1
            seg.index = self._segment_index
            self._persist(seg)
            if self.cb.on_segment is not None:
                self.cb.on_segment(seg)
            self._maybe_insert(seg.text)

        self._clear_partial()

    def _persist(self, seg: TranscriptSegment) -> None:
        """Scrive il segmento su disco (autosave).

        Il §8 chiede il salvataggio automatico: se la scrittura fallisce,
        la trascrizione **non** e' al sicuro. Un errore isolato (disco
        pieno per un attimo, lock SQLite) viene segnalato senza fermare
        la sessione, ma tre fallimenti consecutivi significano che non
        stiamo salvando niente: misura — prima la sessione continuava
        all'infinito, il toast con l'errore spariva dopo pochi secondi e
        l'utente restava con una lezione che credeva salvata e non lo era.
        """
        store = self._store
        if store is None or self.session.session_id is None:
            return
        try:
            store.add_segment(
                self.session.session_id,
                seg.index,
                seg.start,
                seg.end,
                seg.text,
                final=True,
            )
            self._save_failures = 0
        except Exception as exc:
            self._save_failures += 1
            message = f"Salvataggio del segmento fallito: {exc}"
            if self._save_failures >= 3:
                # non e' un intoppo passeggero: fermare la sessione e
                # dirlo, invece di accumulare testo che nessuno salvera'
                self._report_error(
                    f"{message} (salvataggio interrotto dopo {self._save_failures} "
                    "tentativi: la trascrizione rischia di essere persa)"
                )
                self._stop_flag.set()
                return
            self._report_error(message)

    def _maybe_insert(self, text: str) -> None:
        """Invia il testo finalizzato al documento esterno, se abilitato.

        Non inserisce mai un'ipotesi provvisoria e non inserisce in pausa:
        in pausa la finestra attiva e' la nostra e l'inserimento andrebbe
        a finire nel posto sbagliato.
        """
        if not self._insert_enabled or not text:
            return
        if self.state != ListenerState.LISTENING:
            return
        if not self._inserter.armed:
            return
        payload = f"{text}\n\n" if self._insert_paragraphs else f" {text}"
        try:
            result = self._inserter.insert(payload)
        except Exception as exc:
            result = InsertResult(False, "errore", str(exc))
        if self.cb.on_insert_result is not None:
            self.cb.on_insert_result(result)

    # -- thread parziale ----------------------------------------------------
    def _partial_loop(self) -> None:
        """Calcola l'ipotesi provvisoria sul segmento ancora aperto.

        La decodifica Whisper costa ~3 s a prescindere dalla lunghezza
        dell'audio, quindi l'ipotesi ha senso solo nei primi secondi del
        segmento: piu' in la' il testo non cambia e si bloccherebbe la
        finalizzazione.
        """
        while not self._stop_flag.is_set():
            time.sleep(self.cfg.partial_poll_s)
            if self._stop_flag.is_set():
                return
            if self.state != ListenerState.LISTENING:
                continue
            self._maybe_partial()

    def _maybe_partial(self) -> None:
        segmenter = self._segmenter
        engine = self._engine
        if segmenter is None or engine is None or not engine.is_ready:
            return
        if self._stop_flag.is_set() or self._pause_flag.is_set():
            return
        if self._finalize_pending:
            return

        pending_s = segmenter.pending_seconds
        if pending_s <= 0.0:
            return
        # Solo segmenti brevi: oltre, il costo e' uguale e il testo non
        # cambia sensibilmente.
        if pending_s > self.cfg.partial_max_pending_s:
            return

        now = time.monotonic()
        if now - self._last_partial_at < self.cfg.partial_interval_s:
            return

        audio = segmenter.pending_audio
        if len(audio) == 0:
            return
        growth = len(audio) / segmenter.sample_rate - self._last_partial_len
        if self._partial_text and growth < self.cfg.partial_min_growth_s:
            return

        # una sola decodifica parziale in volo
        with self._partial_lock:
            if self._partial_running:
                return
            self._partial_running = True

        try:
            with self._engine_lock:
                segments = engine.transcribe(audio, final=False)
        except Exception:
            segments = []
        finally:
            with self._partial_lock:
                self._partial_running = False

        if not segments:
            return

        text = " ".join(s.text for s in segments).strip()
        if not text:
            return

        self._last_partial_at = now
        self._last_partial_len = len(audio) / segmenter.sample_rate
        self._partial_text = text
        if self.cb.on_partial is not None:
            self.cb.on_partial(text)

    def _clear_partial(self) -> None:
        self._partial_text = ""
        self._last_partial_len = 0.0
        if self.cb.on_partial is not None:
            self.cb.on_partial("")

    # -- livello del microfono ----------------------------------------------
    def _start_level_timer(self) -> None:
        self._stop_level_timer()

        def tick() -> None:
            capture = self._capture
            if capture is not None and self.cb.on_level is not None:
                rms, peak = capture.level
                self.cb.on_level(rms, peak)
            if not self._stop_flag.is_set():
                self._level_timer = threading.Timer(0.1, tick)
                self._level_timer.daemon = True
                self._level_timer.start()

        self._level_timer = threading.Timer(0.1, tick)
        self._level_timer.daemon = True
        self._level_timer.start()

    def _stop_level_timer(self) -> None:
        timer = self._level_timer
        self._level_timer = None
        if timer is not None:
            timer.cancel()

    # -- utili --------------------------------------------------------------
    def _flush_segment(self) -> None:
        """Chiude il segmento aperto (pausa o fine lezione)."""
        segmenter = self._segmenter
        if segmenter is None:
            return
        try:
            closed = segmenter.flush()
        except Exception:
            return
        if closed is not None:
            self._handle_closed_segment(closed)

    def _report_error(self, message: str) -> None:
        self._set_state(ListenerState.ERROR, message)
        if self.cb.on_error is not None:
            self.cb.on_error(message)

    def _warn(self, message: str) -> None:
        """Segnala un problema senza fermare la sessione.

        ``_report_error`` porta lo stato in ERROR: giusto quando la
        trascrizione non puo' continuare, sbagliato per un extra come la
        registrazione audio. Il messaggio arriva comunque all'interfaccia
        (toast), cosi' l'utente sa che il WAV non sta crescendo.
        """
        if self.cb.on_error is not None:
            self.cb.on_error(message)

    # -- registrazione audio opzionale --------------------------------------
    def set_record_audio(self, enabled: bool) -> None:
        """Accende o spegne la registrazione anche a sessione in corso.

        La casella nelle preferenze e' azionabile mentre si ascolta: se il
        cambio valesse solo dal prossimo "ascolta", l'utente crederebbe di
        registrare e non registrerebbe (o viceversa). Aprendo il file qui
        il comportamento segue subito la preferenza.
        """
        self.cfg.settings.record_audio = bool(enabled)
        if not enabled:
            self._close_recorder()
            return
        if self.state in (ListenerState.LISTENING, ListenerState.PAUSED):
            self._open_recorder()

    @property
    def recording_path(self) -> Path | None:
        """Il WAV aperto adesso, o ``None`` se non si sta registrando."""
        recorder = self._recorder
        return recorder.path if recorder is not None else None

    def _open_recorder(self) -> None:
        """Apre il WAV della sessione se ``record_audio`` e' acceso.

        La preferenza prometteva ``AUDIO_DIR/<sessione>.wav`` e non lo
        produceva: la casella si accendeva, il toast diceva "l'audio viene
        scritto su disco" e non veniva scritto un byte. Qui la promessa si
        mantiene.

        Un errore di apertura (disco pieno, permessi) **non** ferma la
        trascrizione: si avvisa e si prosegue senza registrazione. Il
        transcript e' il prodotto principale, il WAV e' un extra.
        """
        self._close_recorder()
        if not self.cfg.settings.record_audio:
            return
        session_id = self.session.session_id
        if session_id is not None:
            name = f"lezione-{session_id}.wav"
        else:
            # nessuno store (Uso senza database): il nome resta riconoscibile
            name = time.strftime("lezione-%Y%m%d-%H%M%S.wav", time.localtime())
        recorder = WavRecorder(
            _config.AUDIO_DIR / name, self.cfg.settings.target_rate
        )
        try:
            recorder.open()
        except Exception as exc:
            self._warn(f"Registrazione audio non avviata: {exc}")
            return
        self._recorder = recorder

    def _record_block(self, block) -> None:  # noqa: ANN001 - np.ndarray
        """Scrive un blocco nel WAV, se la registrazione e' attiva."""
        recorder = self._recorder
        if recorder is None:
            return
        try:
            recorder.write(block)
        except Exception as exc:
            # un errore di scrittura (disco pieno) e' definitivo: il file
            # resterebbe troncato e ogni blocco successivo fallirebbe. Si
            # chiude e si dice una volta sola.
            self._close_recorder()
            self._warn(f"Registrazione audio interrotta: {exc}")

    def _close_recorder(self) -> None:
        recorder, self._recorder = self._recorder, None
        if recorder is not None:
            recorder.close()

    @staticmethod
    def _recording_summary(recorder: WavRecorder | None) -> str:
        """Dice dove e' finito l'audio, se ne e' stato registrato."""
        if recorder is None or recorder.frames_written <= 0:
            return ""
        rate = max(int(recorder.sample_rate), 1)
        seconds = recorder.frames_written / rate
        return f"Audio salvato in {recorder.path} ({seconds:.0f} s)."


# ---------------------------------------------------------------------------
# il sottosistema GTK
# ---------------------------------------------------------------------------
def _gtk_modules() -> None:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    gi.require_version("Gdk", "4.0")


def _make_application_class():
    """Costruisce la classe applicativa (GTK importato pigramente)."""
    _gtk_modules()
    from gi.repository import Adw, Gio, GLib

    class LectureTranscriberApp(Adw.Application):
        """L'applicazione GTK4: crea la finestra e le azioni.

        La pipeline vive qui dentro: l'applicazione e' il proprietario
        unico del ciclo di vita, cosi' il modello si carica una volta sola
        e la sessione viene chiusa in modo ordinato.
        """

        def __init__(self, settings: _config.Settings | None = None) -> None:
            super().__init__(
                application_id=_config.APP_ID,
                flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
            )
            self.settings = settings or _config.load_settings()
            self.pipeline = TranscriptionPipeline(
                PipelineConfig(settings=self.settings),
                callbacks=PipelineCallbacks(),
            )
            self.window = None
            self._shortcuts = None
            self._gsettings_shortcuts = None
            self._dbus_registration = None

        # -- azioni ------------------------------------------------------------
        def _install_actions(self) -> None:
            export = Gio.SimpleAction.new("export", GLib.VariantType.new("s"))
            export.connect("activate", self._on_export)
            self.add_action(export)

            copy = Gio.SimpleAction.new("copy", GLib.VariantType.new("s"))
            copy.connect("activate", self._on_copy)
            self.add_action(copy)

            quit_action = Gio.SimpleAction.new("quit", None)
            quit_action.connect("activate", self._on_quit)
            self.add_action(quit_action)

            for fmt in ("txt", "md", "md_ts"):
                action = Gio.SimpleAction.new(f"export-{fmt}", None)
                action.connect("activate", lambda _a, _p, f=fmt: self._export(f))
                self.add_action(action)

            # Azione invocata dal ``media-keys`` di GNOME: il tasto globale
            # fa partire ``gdbus call ... ActivateShortcut <id>``.
            activate = Gio.SimpleAction.new(
                "ActivateShortcut", GLib.VariantType.new("s")
            )
            activate.connect("activate", self._on_activate_shortcut)
            self.add_action(activate)

            self._export_shortcut_interface()

        def _export_shortcut_interface(self) -> None:
            """Espone ``ActivateShortcut`` come metodo D-Bus dell'app.

            Le scorciatoie globali registrate nel ``media-keys`` di GNOME
            sono eseguite come comando di sistema; il comando che
            ``shortcuts_gsettings`` scrive e' una chiamata D-Bus a questo
            metodo.

            ``GApplication`` esporta le sue azioni sotto l'interfaccia
            standard ``org.gtk.Actions``, che non e' invocabile con la
            sintassi di ``gdbus call`` perche' il parametro e' un
            ``av``. Per questo l'interfaccia viene esportata a parte,
            sull'oggetto gia' di proprietà dell'applicazione.
            """
            conn = self.get_dbus_connection()
            if conn is None:
                return
            node = Gio.DBusNodeInfo.new_for_xml(
                f"""
                <node>
                  <interface name="{_config.APP_ID}">
                    <method name="ActivateShortcut">
                      <arg type="s" name="shortcut_id" direction="in"/>
                    </method>
                  </interface>
                </node>
                """
            )
            try:
                self._dbus_registration = conn.register_object_with_closures2(
                    self.get_dbus_object_path(),
                    node.interfaces[0],
                    self._on_shortcut_dbus_call,
                    None,
                    None,
                )
            except Exception:
                self._dbus_registration = None

        def _on_shortcut_dbus_call(
            self, _conn, _sender, _path, _iface, method, params, invocation
        ) -> None:
            """Gestisce la chiamata D-Bus ``ActivateShortcut``."""
            if method != "ActivateShortcut":
                invocation.return_dbus_error(
                    "org.freedesktop.DBus.Error.UnknownMethod", "metodo sconosciuto"
                )
                return
            try:
                shortcut_id = params.unpack()[0]
            except Exception:
                invocation.return_dbus_error(
                    "org.freedesktop.DBus.Error.InvalidArgs", "parametro non valido"
                )
                return
            self._on_activate_shortcut(None, GLib.Variant("s", shortcut_id))
            invocation.return_value(None)

        def _on_export(self, _action, param) -> None:
            fmt = param.get_string() if param is not None else "md_ts"
            self._export(fmt)

        def _export(self, fmt: str) -> None:
            if self.window is not None:
                self.window.export(fmt)

        def _on_copy(self, _action, param) -> None:
            target = param.get_string() if param is not None else "selection"
            if self.window is not None:
                self.window.copy(target)

        def _on_quit(self, _action, _param) -> None:
            self.quit()

        # -- ciclo di vita ------------------------------------------------------
        def do_activate(self) -> None:
            from .ui import MainWindow

            if self.window is None:
                self._install_actions()
                self.window = MainWindow(self, self.pipeline)
                self.pipeline.open()
                self.window.apply_position()
                self.window.apply_always_on_top()
                self._install_global_shortcuts()
            self.window.present()

        def _shortcut_handlers(self) -> dict[str, object]:
            """Le azioni attivabili da una scorciatoia globale.

            Un'unica fonte: la usano sia il percorso GSettings (che
            registra i tasti e invoca l'app via D-Bus) sia il portal (che
            chiama le funzioni in processo).
            """
            return {
                "toggle_listen": self._toggle_listen,
                "toggle_insert": self._toggle_insert,
                "toggle_transparent": self._toggle_opacity,
                "toggle_window": self._toggle_window,
                "toggle_always_on_top": self._toggle_always_on_top,
                "bookmark": self._bookmark_last,
                "jump_to_latest": self._jump_to_latest,
            }

        def _install_global_shortcuts(self) -> None:
            """Registra le scorciatoie globali.

            Si prova prima il percorso GSettings del ``media-keys`` di
            GNOME e, solo se non e' disponibile, il portal
            ``org.freedesktop.portal.GlobalShortcuts``.

            Il portal su GNOME 50 ha tre limiti misurati: pretende un
            app-id (che si ottiene solo lanciando l'app da GNOME, in uno
            scope systemd ``app-*.scope``), ``BindShortcuts`` apre un
            dialogo di conferma che blocca la registrazione, e una
            sessione puo' fare un solo bind. Il percorso GSettings non ha
            nessuno dei tre problemi, e per questo e' il preferito; il
            portal resta come alternativa per i desktop dove il
            ``media-keys`` non e' disponibile.

            Se **entrambi** falliscono l'app resta usabile, ma deve dirlo:
            misura — prima il fallimento era muto, l'utente non aveva
            scorciatoie da tastiera e non aveva modo di scoprire perche'.
            """
            handlers = self._shortcut_handlers()
            reason = "le scorciatoie da tastiera non sono state registrate"

            # 1. GSettings: funziona sempre su GNOME, senza dialogo
            try:
                from .shortcuts_gsettings import GSettingsShortcutManager

                manager = GSettingsShortcutManager()
                if manager.register(
                    {
                        "toggle_listen": "Avvia o ferma la trascrizione",
                        "toggle_insert": "Attiva o disattiva l'inserimento",
                        "toggle_transparent": "Modalita' semitrasparente",
                        "toggle_window": "Mostra o nascondi la finestra",
                        "toggle_always_on_top": (
                            "Tieni la finestra sopra le altre"
                        ),
                        "bookmark": "Crea un segnalibro",
                        "jump_to_latest": "Torna al testo piu' recente",
                    }
                ):
                    self._gsettings_shortcuts = manager
                    return
                reason = (
                    manager.last_error
                    or "lo schema media-keys di GNOME non ha accettato la registrazione"
                )
            except Exception as exc:
                reason = f"registrazione via media-keys fallita: {exc}"

            # 2. portal: alternativa, se il GSettings non ha funzionato
            try:
                from .shortcuts import ShortcutManager
            except ImportError as exc:
                self._shortcuts = None
                self._warn_shortcuts(f"{reason}; modulo del portal assente ({exc})")
                return
            try:
                manager = ShortcutManager()
                manager.register(handlers)
                if manager.available:
                    self._shortcuts = manager
                    return
                reason = f"{reason}; {manager.last_error or 'portal non disponibile'}"
            except Exception as exc:
                reason = f"{reason}; portal fallito ({exc})"
            self._shortcuts = None
            self._warn_shortcuts(reason)

        def _warn_shortcuts(self, reason: str) -> None:
            """Segnala che le scorciatoie globali non sono attive."""
            logger.warning("Scorciatoie globali non registrate: %s", reason)
            self._shortcuts_warning = reason

        def _on_activate_shortcut(self, _action, param) -> None:
            """Attiva una scorciatoia globale (chiamata via D-Bus).

            E' il destinatario delle scorciatoie registrate nel
            ``media-keys`` di GNOME: ``gnome-settings-daemon`` esegue il
            comando ``gdbus call ... ActivateShortcut <id>`` alla pressione
            del tasto.
            """
            if param is None:
                return
            shortcut_id = param.get_string()
            handler = self._shortcut_handlers().get(shortcut_id)
            if handler is None:
                return
            try:
                handler()
            except Exception:
                pass

        # -- azioni delle scorciatoie ------------------------------------------
        def _toggle_listen(self) -> None:
            state = self.pipeline.state
            if state == ListenerState.LISTENING:
                self.pipeline.pause()
            elif state == ListenerState.PAUSED:
                self.pipeline.resume()
            else:
                self.pipeline.start()

        def _toggle_insert(self) -> None:
            if self.window is not None:
                self.window.insert_button.set_active(
                    not self.window.insert_button.get_active()
                )

        def _toggle_opacity(self) -> None:
            if self.window is None:
                return
            self.settings.opacity = 0.35 if self.settings.opacity > 0.5 else 0.95
            self.window.apply_opacity()

        def _toggle_window(self) -> None:
            if self.window is None:
                return
            if self.window.get_visible():
                self.window.hide()
            else:
                self.window.present()

        def _toggle_always_on_top(self) -> None:
            """Accende o spegne il sempre-in-primo-piano da tastiera.

            Passa dal tasto della barra superiore, non scrivendo la
            preferenza a mano: cosi' lo stato mostrato, la preferenza e
            l'estensione restano d'accordo per costruzione.
            """
            if self.window is None:
                return
            self.window.pin_button.set_active(
                not self.window.pin_button.get_active()
            )

        def _bookmark_last(self) -> None:
            if self.window is None:
                return
            index = self.pipeline.segment_index
            if index <= 0:
                return
            self.window.toggle_bookmark(index)

        def _jump_to_latest(self) -> None:
            if self.window is not None:
                self.window.jump_to_latest()

        def do_shutdown(self) -> None:
            try:
                if self._shortcuts is not None:
                    self._shortcuts.unregister()
            except Exception:
                pass
            try:
                if self._gsettings_shortcuts is not None:
                    self._gsettings_shortcuts.unregister()
            except Exception:
                pass
            try:
                if self._dbus_registration is not None:
                    self.get_dbus_connection().unregister_object(
                        self._dbus_registration
                    )
                    self._dbus_registration = None
            except Exception:
                pass
            try:
                self.pipeline.close()
            except Exception:
                pass
            try:
                _config.save_settings(self.settings)
            except Exception:
                pass
            Adw.Application.do_shutdown(self)

    return LectureTranscriberApp


def main(argv: list[str] | None = None) -> int:
    """Punto d'ingresso dell'applicazione grafica."""
    _gtk_modules()
    from gi.repository import Adw

    Adw.init()
    app_class = _make_application_class()
    app = app_class()
    return app.run(argv if argv is not None else [])


if __name__ == "__main__":
    raise SystemExit(main())
