"""Motore di riconoscimento vocale locale.

Architettura scelta (vedi docs/analisi-fattibilita.md §3.1): si usa Whisper in
modalità a segmenti, delimitati dal VAD:

    cattura -> VAD (Silero via faster-whisper) -> segmenti da 0,7 s di silenzio
             -> decodifica Whisper -> testo finale + ipotesi parziale

Il testo parziale e' un'anteprima che viene ricalcolata a ogni passo; il testo
finale viene emesso alla fine del segmento. La punteggiatura e' nativa di
Whisper, quindi gratuita e affidabile in italiano.

Nota: esiste un ASR streaming italiano (Zipformer2 "kroko-it" su
sherpa-onnx, RTF 0,017, prima ipotesi in 40 ms) ma ha WER 19,1 % contro
7,0 %, non produce punteggiatura ed e' licenziato CC-BY-SA (copyleft),
incompatibile con la MIT di questo progetto. Se un giorno la licenza
dovesse chiarirsi, il suo ruolo naturale e' il testo *provvisorio* e un
rilevatore di parole chiave a bassissimo costo, non il testo definitivo.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable

# Anche numpy e' differito, per la stessa ragione di soxr e sounddevice in
# ``audio``: ``app`` importa questo modulo, quindi un ``import numpy`` in
# testa al file impediva di aprire la finestra. Vedi il commento in
# ``audio.py``.
#
# Differire l'import pero' NON basta: ``np`` resta ``None`` e il primo
# ``np.zeros`` trasforma l'assenza in ``AttributeError: 'NoneType' object
# has no attribute 'zeros'`` — misurato, ``Segmenter()`` sollevava e
# ``do_activate`` moriva prima di presentare la finestra, mentre
# ``__main__`` aveva gia' stampato "L'app si apre comunque". Con numpy
# assente questo modulo non e' utilizzabile, e va **detto**: chi prova a
# usarlo deve ricevere un errore che spieghi cosa installare, non un
# AttributeError. Per questo esiste :func:`require_asr_libs`, chiamata
# da tutto ciò che tocca l'audio o il modello.
try:
    import numpy as np
except ImportError:  # pragma: no cover - dipende dall'ambiente
    np = None  # type: ignore[assignment]

_MISSING_HINT = (
    "Le librerie di riconoscimento vocale non sono installate. "
    "Per trascrivere esegui scripts/ensure-venv.sh; nel frattempo "
    "cronologia, ricerca, esportazione e correzioni funzionano."
)


def require_asr_libs() -> None:
    """Impone numpy, con un errore che dice come rimediare.

    Chiamata all'ingresso di ogni percorso che tocca numpy
    (:class:`Segmenter`, :class:`TranscriptionEngine`). Senza, l'assenza
    si manifesta come ``AttributeError`` in un punto qualsiasi del
    codice — sintomo lontanissimo dalla causa, e irraggiungibile per chi
    non ha davanti il traceback.
    """
    if np is None:
        raise RuntimeError("numpy non e' installato. " + _MISSING_HINT)


@dataclass
class TranscriptSegment:
    """Un segmento di trascrizione, con timestamp relativo."""

    index: int
    start: float
    end: float
    text: str
    final: bool = True
    words: list[tuple[float, float, str]] = field(default_factory=list)

    @property
    def timestamp(self) -> str:
        return f"{int(self.start // 60):02d}:{int(self.start % 60):02d}"


@dataclass
class ASRConfig:
    model_name: str = "large-v3-turbo"
    compute_type: str = "int8"
    device: str = "cpu"
    language: str = "it"
    silence_ms: int = 700
    min_speech_ms: int = 250
    max_segment_s: float = 15.0
    beam_size: int = 1
    threads: int = 0  # 0 = auto


class TranscriptionEngine:
    """Incapsula il modello Whisper e la macchina a stati del segmentatore.

    Il caricamento del modello e' costoso (qualche secondo) e va eseguito in
    un thread separato per non bloccare l'interfaccia: per questo
    :meth:`load_async` esiste e :meth:`is_ready` dice quando si puo' parlare.
    """

    def __init__(self, config: ASRConfig, model_dir: str) -> None:
        require_asr_libs()
        self.config = config
        self.model_dir = model_dir
        self._model = None
        self._lock = threading.Lock()
        self._load_error: str | None = None
        self._last_error: str | None = None
        self._decode_failures = 0
        self._loading = False
        self._prev_text = ""

    # -- caricamento -------------------------------------------------------
    @property
    def is_ready(self) -> bool:
        return self._model is not None

    @property
    def load_error(self) -> str | None:
        return self._load_error

    @property
    def last_error(self) -> str | None:
        """Ultimo errore di decodifica (non impedisce di riavviare)."""
        return self._last_error

    @property
    def decode_failures(self) -> int:
        """Errori di decodifica consecutivi; 0 dopo una riuscita."""
        return self._decode_failures

    @property
    def is_loading(self) -> bool:
        return self._loading

    def load_async(self, on_ready: Callable[[], None] | None = None) -> threading.Thread:
        """Carica il modello in background; ``on_ready`` viene chiamato alla fine."""

        def _worker() -> None:
            self._loading = True
            try:
                from faster_whisper import WhisperModel

                kwargs = {
                    "device": self.config.device,
                    "compute_type": self.config.compute_type,
                    "download_root": self.model_dir,
                }
                if self.config.threads > 0:
                    kwargs["cpu_threads"] = self.config.threads
                self._model = WhisperModel(self.config.model_name, **kwargs)
                self._load_error = None
            except ImportError as exc:
                # Il venv dell'utente non e' pronto: il messaggio deve dire
                # quale libreria manca e come rimediare, perche' "Caricamento
                # del modello fallito" non e' azionabile.
                self._load_error = (
                    f"Librerie di riconoscimento vocale mancanti ({exc}). "
                    "Esegui scripts/ensure-venv.sh per creare l'ambiente "
                    "Python dell'utente con faster-whisper e soxr."
                )
                self._model = None
            except Exception as exc:
                self._load_error = f"Caricamento del modello fallito: {exc}"
                self._model = None
            finally:
                self._loading = False
                if on_ready is not None:
                    on_ready()

        t = threading.Thread(target=_worker, name="asr-load", daemon=True)
        t.start()
        return t

    # -- decodifica --------------------------------------------------------
    def transcribe(
        self,
        audio: np.ndarray,
        final: bool = True,
        previous_text: str = "",
    ) -> list[TranscriptSegment]:
        """Decodifica un array di float32 a 16 kHz mono.

        Restituisce i segmenti decodificati; con ``final=False`` il testo e'
        un'ipotesi provvisoria sul segmento corrente.
        """
        if self._model is None or len(audio) == 0:
            return []

        segments: list[TranscriptSegment] = []
        self._last_error = None
        try:
            with self._lock:
                segs, info = self._model.transcribe(
                    audio,
                    language=self.config.language,
                    beam_size=self.config.beam_size,
                    vad_filter=True,
                    vad_parameters={
                        "min_silence_duration_ms": self.config.silence_ms,
                        "speech_pad_ms": 200,
                    },
                    condition_on_previous_text=bool(previous_text),
                    initial_prompt=previous_text or None,
                    word_timestamps=False,
                    without_timestamps=False,
                )
                for s in segs:
                    text = (s.text or "").strip()
                    if not text:
                        continue
                    segments.append(
                        TranscriptSegment(
                            index=int(s.id),
                            start=float(s.start),
                            end=float(s.end),
                            text=text,
                            final=final,
                        )
                    )
        except Exception as exc:
            # attenzione: questo e' un errore di **decodifica**, non di
            # caricamento. Metterlo in ``_load_error`` faceva credere che
            # il modello non fosse disponibile: ``start()`` vede
            # ``load_error`` e rifiuta di avviare (misurato: un blocco
            # andato in OOM chiudeva la sessione e impediva di riavviarla,
            # con il modello perfettamente in memoria).
            self._last_error = f"Errore durante la decodifica: {exc}"
            self._decode_failures += 1
            return []
        self._decode_failures = 0
        return segments

    def transcribe_text(self, audio: np.ndarray) -> str:
        """Decodifica e restituisce solo il testo (comodo per i test)."""
        segs = self.transcribe(audio, final=True)
        return " ".join(s.text for s in segs).strip()


class Segmenter:
    """Trasforma un flusso di blocchi audio in segmenti di parlato.

    Un segmento si chiude quando si rileva silenzio per ``silence_ms``, o
    quando supera ``max_segment_s`` (per non accumulare troppo contesto).
    Si usa una semplice envelope RMS: e' lineare, non richiede modelli extra
    e per un'aula con rumore di fondo e' sufficiente con soglie tarate.
    """

    def __init__(self, config: ASRConfig, sample_rate: int = 16000) -> None:
        require_asr_libs()
        self.config = config
        self.sample_rate = sample_rate
        self._buffer = np.zeros(0, dtype=np.float32)
        self._speech = np.zeros(0, dtype=np.float32)
        self._silence_run = 0.0
        self._in_speech = False
        self._speech_samples = 0
        self._silence_samples = 0
        self._noise_floor = 0.008
        self._speech_threshold = 0.02
        self._min_threshold = 0.012
        # isteresi: la soglia per restare "in parlato" e' piu' bassa di quella
        # per entrarci, altrimenti le pause naturali tra sillabe chiuderebbero
        # il segmento. Va inizializzata **subito**, non lasciata a 0.0: a
        # freddo il ramo che la aggiorna (il "non in parlato") non e' ancora
        # stato eseguito, quindi con 0.0 ogni frame — silenzio assoluto
        # compreso — contava come parlato e il segmento non si chiudeva
        # mai (misurato: da freddo la prima frase non finiva nella
        # trascrizione e il buffer cresceva fino a max_segment_s).
        self._continue_threshold = self._speech_threshold * 0.45

    # -- accessori ---------------------------------------------------------
    @property
    def in_speech(self) -> bool:
        return self._in_speech

    @property
    def speech_seconds(self) -> float:
        return self._speech_samples / self.sample_rate

    @property
    def pending_seconds(self) -> float:
        return len(self._buffer) / self.sample_rate

    @property
    def pending_audio(self) -> np.ndarray:
        """Copia dell'audio del segmento non ancora chiuso.

        Serve al thread che calcola l'ipotesi provvisoria: la decodifica
       Whisper dura sempre ~3 s (la finestra e' di 30 s), quindi conviene
        decodificare solo i primi secondi del segmento.
        """
        return self._buffer.copy()

    # -- logica ------------------------------------------------------------
    def push(self, block: np.ndarray) -> np.ndarray | None:
        """Accoda un blocco; restituisce l'audio del segmento chiuso, se c'e'."""
        if block is None or len(block) == 0:
            return None

        self._buffer = np.concatenate([self._buffer, block])
        out: np.ndarray | None = None

        # controlla l'inviluppo RMS a blocchi di 20 ms
        frame = int(self.sample_rate * 0.02)
        for start in range(0, len(block) - frame + 1, frame):
            chunk = block[start : start + frame]
            rms = float(np.sqrt(np.mean(np.square(chunk))))

            if rms > self._speech_threshold:
                if not self._in_speech:
                    self._in_speech = True
                    self._speech_samples = 0
                    self._silence_run = 0.0
                self._silence_run = 0.0
                self._silence_samples = 0
                self._speech_samples += frame
            elif self._in_speech and rms > self._continue_threshold:
                # siamo ancora in parlato: pausa brevissima tra sillabe
                self._silence_run = 0.0
                self._speech_samples += frame
            elif self._in_speech:
                self._silence_run += frame / self.sample_rate
                self._silence_samples += frame
                if self._silence_run * 1000 >= self.config.silence_ms:
                    out = self._close_segment()
                    break
            else:
                # il rumore di fondo si adatta solo quando non si sta parlando:
                # adattarlo durante il parlato farebbe salire la soglia fino a
                # far perdere il segnale
                self._noise_floor = 0.95 * self._noise_floor + 0.05 * rms
                self._speech_threshold = max(
                    self._noise_floor * 3.0, self._min_threshold
                )
                self._continue_threshold = self._speech_threshold * 0.45

        # segmento troppo lungo: lo chiude comunque
        if out is None and len(self._buffer) / self.sample_rate >= self.config.max_segment_s:
            out = self._close_segment()

        return out

    def flush(self) -> np.ndarray | None:
        """Chiude il segmento in corso (fine lezione, pausa manuale)."""
        if self._in_speech or len(self._buffer) > 0:
            return self._close_segment()
        return None

    def _close_segment(self) -> np.ndarray | None:
        audio = self._buffer
        speech_s = self._speech_samples / self.sample_rate
        self._buffer = np.zeros(0, dtype=np.float32)
        self._in_speech = False
        self._silence_run = 0.0
        self._speech_samples = 0
        self._silence_samples = 0

        if len(audio) == 0:
            return None
        # scarta i segmenti troppo brevi (tosse, rumori). Il confronto va
        # fatto sul parlato effettivo, non sulla durata del buffer: prima
        # leggeva ``len(audio)``, che include il silenzio iniziale e finale
        # — misurato: un colpo di tosse seguito da 1,5 s di silenzio
        # passava il filtro ``min_speech_ms=300`` con 40 ms di parlato e
        # Whisper ci perdeva ~3 s di decodifica sopra.
        if speech_s < self.config.min_speech_ms / 1000:
            return None
        return audio
