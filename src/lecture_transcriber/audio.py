"""Acquisizione audio dal microfono via PipeWire/PulseAudio.

Strategia (verificata su Fedora 44, GNOME 50, PipeWire 1.6.9):

* si cattura alla frequenza nativa del dispositivo (48 kHz sul microfono
  interno) per evitare conversioni non intere nel server audio;
* si downmix a mono e si risampiona a 16 kHz in-process con soxr, che è la
  frequenza attesa dai modelli Whisper;
* lo stream resta aperto in modo continuo: PipeWire sospende le sorgenti
  inattive (``node.suspend-on-idle=true``) e riattivarle a ogni frase
  costerebbe latenza;
* il callback PortAudio gira nel thread realtime, il consumo del buffer
  avviene in un thread Python separato tramite una coda bounded: se l'ASR
  rallenta, l'audio non viene perso ma la coda cresce (e viene segnalato).
"""

from __future__ import annotations

import queue
import sys
import threading
import time
from dataclasses import dataclass

# numpy sta nella stessa categoria di soxr e sounddevice, non in una
# categoria a parte: anche lui blocca l'apertura della finestra se manca,
# perche' ``asr`` e ``audio`` lo importano in testa al modulo e ``app``
# importa entrambi. Misurato: senza numpy ``from lecture_transcriber.app
# import TranscriptionPipeline`` sollevava ``ImportError`` e la finestra
# non si apriva — mentre il messaggio di ``__main__`` prometteva che le
# librerie di riconoscimento vocale mancanti non impediscono l'avvio.
# La promessa vale solo se l'import e' differito anche per numpy.
try:
    import numpy as np
except ImportError:  # pragma: no cover - dipende dall'ambiente
    np = None  # type: ignore[assignment]

# soxr e sounddevice NON sono importati in testa al modulo. Il requisito
# del progetto e' che l'applicazione si apra con un tap: se il venv
# dell'utente non e' ancora pronto (primo avvio, o librerie mancanti) un
# import in cima al file faceva morire l'intero programma con
# ``ModuleNotFoundError: No module named 'soxr'`` -- misurato: la finestra
# non si apriva affatto, e l'utente non aveva modo di consultare la
# cronologia, esportare o correggere. L'errore ora arriva solo quando si
# prova ad avviare la cattura, con un messaggio che dice cosa installare.
try:
    import soxr
except ImportError:  # pragma: no cover - dipende dall'ambiente
    soxr = None  # type: ignore[assignment]

try:
    import sounddevice as sd
except ImportError:  # pragma: no cover - dipende dall'ambiente
    sd = None  # type: ignore[assignment]


def _require_audio_libs() -> None:
    """Impone che soxr e sounddevice ci siano, con un errore leggibile.

    Chiamata prima di toccare il microfono. Senza queste librerie la
    trascrizione non puo' funzionare, ma tutto il resto dell'applicazione
    (cronologia, ricerca, esportazione, correzioni, segnalibri) deve
    continuare a funzionare.
    """
    missing = [
        name
        for name, mod in (("soxr", soxr), ("sounddevice", sd))
        if mod is None
    ]
    if not missing:
        return
    from . import config

    raise RuntimeError(
        "Librerie audio mancanti: "
        + ", ".join(missing)
        + ". Servono per l'acquisizione dal microfono; il resto "
        "dell'applicazione funziona. Esegui "
        + config.ensure_venv_hint()
        + " (crea l'ambiente Python dell'utente con faster-whisper e soxr)."
    )


def _make_resampler(
    in_rate: float, out_rate: float, quality: str = "HQ"
):
    """Costruisce un risampionatore soxr che funzioni su 0.x e su 1.x.

    soxr 1.x ha **rimosso** la classe ``soxr.Resample``: esiste solo la
    funzione ``soxr.resample(x, in_rate, out_rate, quality)`` e lo stream
    ``soxr.ResampleStream(...).resample_chunk(x)``. Con la versione
    installata (soxr 1.1.0) ``soxr.Resample(...)`` solleva
    ``AttributeError`` gia' in fase di costruzione — misurato:
    ``AudioCapture()`` non si costruiva affatto, e il difetto passava i
    test perche' ``tests/_bootstrap.py`` installava uno shim che
    riaggiungeva ``Resample`` solo dentro ai test.

    Lo stream va preferito alla funzione: nel callback realtime arriva un
    blocco alla volta, e ``resample()`` senza stato taglia i bordi a ogni
    chiamata (click e perdita di campioni tra un blocco e l'altro).
    """
    if hasattr(soxr, "Resample"):
        return soxr.Resample(in_rate, out_rate, quality=quality)  # 0.x
    return _ResampleStream(in_rate, out_rate, quality)


class _ResampleStream:
    """Adattatore minimo dell'API 1.x con l'interfaccia di ``soxr.Resample``."""

    def __init__(self, in_rate: float, out_rate: float, quality: str = "HQ") -> None:
        self._stream = soxr.ResampleStream(
            in_rate, out_rate, 1, dtype="float32", quality=quality
        )

    def resample(self, x: np.ndarray) -> np.ndarray:
        arr = np.asarray(x, dtype=np.float32)
        if arr.size == 0:
            return np.zeros(0, dtype=np.float32)
        return self._stream.resample_chunk(arr)


@dataclass
class InputDevice:
    index: int
    name: str
    channels: int
    rate: float
    is_default: bool


def _is_virtual_noise_node(name: str, platform: str | None = None) -> bool:
    """Vero se ``name`` e' un nodo virtuale, non un microfono fisico.

    Su Linux i nodi "pipewire", "default" e "sysdefault" sono creati dal
    server audio e non corrispondono a un microfono. Su Windows/macOS quei
    nomi non esistono, quindi il filtro e' innocuo (non esclude mai un
    device reale) ma resta per difesa. ``platform`` e' iniettabile per i
    test; di default si usa la piattaforma reale.
    """
    if platform is None:
        platform = sys.platform
    if platform.startswith("linux"):
        return name in ("pipewire", "default", "sysdefault")
    return False


def list_input_devices() -> list[InputDevice]:
    """Elenca i dispositivi di cattura fisici (esclude i nodi virtuali).

    Restituisce una lista vuota se ``sounddevice`` non c'e': la tendina
    dei dispositivi mostra solo "Predefinito di sistema", invece di
    impedire l'apertura della finestra.
    """
    if sd is None:
        return []
    devices: list[InputDevice] = []
    try:
        infos = sd.query_devices()
    except Exception:
        return devices
    default_in = None
    try:
        # sd.default.device e' la coppia (input, output): [0] e' l'ingresso,
        # [1] l'uscita. Con [1] il confronto veniva fatto contro il
        # dispositivo di OUTPUT (misurato su questa macchina: 12, il nodo
        # virtuale "default"), quindi nessun microfono reale risultava
        # predefinito e la tendina partiva senza selezione.
        default_in = sd.default.device[0]
    except Exception:
        pass

    for idx, info in enumerate(infos):
        if info.get("max_input_channels", 0) < 1:
            continue
        name = str(info.get("name", ""))
        # i nodi virtuali (PipeWire su Linux) non sono microfoni fisici.
        # Su Windows/macOS il filtro e' innocuo: nessun device si chiama
        # cosi'.
        if _is_virtual_noise_node(name):
            continue
        devices.append(
            InputDevice(
                index=idx,
                name=name,
                channels=int(info.get("max_input_channels", 1)),
                rate=float(info.get("default_samplerate", 48000.0)),
                is_default=(idx == default_in),
            )
        )
    if devices and not any(d.is_default for d in devices):
        # sotto PipeWire il default di sistema e' il nodo virtuale
        # "default", che questa funzione esclude perche' non e' un
        # microfono: senza questo ripiego nessun dispositivo risulterebbe
        # predefinito e la tendina partirebbe senza selezione. Il primo
        # della lista e' la scelta naturale (PortAudio usa il default del
        # server quando ``device=None``, che su PipeWire e' quello).
        devices[0].is_default = True
    return devices


class AudioCapture:
    """Acquisisce audio dal microfono e lo produce a 16 kHz mono.

    I blocchi audio vengono consegnati tramite :meth:`read` (bloccante) e
    :meth:`read_latest` (non bloccante, utile per il meter di livello).
    """

    def __init__(
        self,
        device: int | None = None,
        sample_rate: int = 48000,
        target_rate: int = 16000,
        channels: int | None = None,
        block_ms: int = 100,
        queue_size: int = 200,
    ) -> None:
        self.device = device
        self.sample_rate = sample_rate
        self.target_rate = target_rate
        self.channels = self._resolve_channels(device, channels)
        self.block_ms = block_ms
        self._resampler = self._build_resampler(sample_rate, target_rate)
        self._q: queue.Queue[np.ndarray] = queue.Queue(maxsize=queue_size)
        self._stream: sd.InputStream | None = None
        self._lock = threading.Lock()
        self._running = False
        self._error: str | None = None
        self._overruns = 0
        self._glitches = 0
        self._last_rms = 0.0
        self._last_peak = 0.0

    @staticmethod
    def _build_resampler(sample_rate: int, target_rate: int):
        """Costruisce il risampionatore, o solleva se manca soxr."""
        _require_audio_libs()
        return _make_resampler(sample_rate, target_rate)

    @staticmethod
    def _resolve_channels(device: int | None, channels: int | None) -> int:
        """Numero di canali da chiedere al dispositivo.

        ``channels=2`` fisso era un difetto: un microfono mono (la maggior
        parte di quelli da scrivania, e i microfoni integrati) rifiuta
        l'apertura a 2 canali e ``InputStream()`` solleva -- misurato: la
        cattura non partiva affatto. Con ``channels=None`` si chiede al
        dispositivo quanti canali ha e si usa quel numero; se la query
        fallisce si ripiega su 1 (il mono e' accettato da tutti).
        """
        if channels is not None and channels > 0:
            return int(channels)
        try:
            import sounddevice as _sd

            info = _sd.query_devices(device, "input")
            got = int(info.get("max_input_channels", 0) or 0)
            if got > 0:
                return min(got, 2)
        except Exception:
            pass
        return 1

    # -- stato -------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._running

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def overruns(self) -> int:
        """Numero di blocchi scartati perché l'ASR non teneva il passo."""
        return self._overruns

    @property
    def glitches(self) -> int:
        """Blocchi con campioni non finiti (NaN/Inf), azzerati al volo."""
        return self._glitches

    @property
    def level(self) -> tuple[float, float]:
        """Ultimo (rms, picco) calcolato sul segnale, per il meter."""
        return self._last_rms, self._last_peak

    # -- ciclo di vita -----------------------------------------------------
    def start(self) -> None:
        if self._running:
            return
        self._error = None
        self._overruns = 0
        self._glitches = 0
        blocksize = int(self.sample_rate * self.block_ms / 1000)
        try:
            stream = sd.InputStream(
                device=self.device,
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="float32",
                blocksize=blocksize,
                callback=self._callback,
            )
            stream.start()
        except Exception as exc:  # dispositivo assente, negato, occupato
            self._error = f"Impossibile aprire il microfono: {exc}"
            self._running = False
            raise
        self._stream = stream
        self._running = True

    def stop(self) -> None:
        with self._lock:
            stream, self._stream = self._stream, None
            self._running = False
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        # il risampionatore a stream trattiene in coda l'ultima parte del
        # segnale (misurato: 477 campioni su 160000, cioe' ~30 ms a 16 kHz):
        # senza questo flush l'ultima sillaba pronunciata viene persa.
        # Va fatto PRIMA di svuotare la coda, non dopo.
        self._flush_resampler()
        self._flush()

    def _flush_resampler(self) -> None:
        """Estrae il residuo dal risampionatore e lo mette in coda."""
        flush = getattr(self._resampler, "flush", None)
        if flush is None:
            return
        try:
            tail = flush()
        except Exception:
            return
        if tail is not None and len(tail):
            try:
                self._q.put_nowait(np.asarray(tail, dtype=np.float32))
            except queue.Full:
                pass

    def _flush(self) -> None:
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                return

    # -- callback realtime -------------------------------------------------
    def _callback(self, indata, frames, time_info, status) -> None:
        if status:
            # underflow/overflow: non è fatale, si continua
            pass
        try:
            mono = indata.mean(axis=1) if indata.ndim > 1 else indata
            mono = mono.astype(np.float32, copy=False)
            # Un campione NaN/Inf in un blocco (glitch del driver, cavallo
            # di troia classico dei dispositivi USB) basta ad avvelenare
            # per sempre media e picco: np.mean di un array con un NaN e'
            # NaN, e da qui in poi il livello mostrato e' NaN, la soglia
            # adattiva del segmenter non si chiude piu' e il resample
            # propaga il NaN a tutto il resto del blocco. Misurato: un
            # singolo blocco corrotto azzerava la trascrizione per il
            # resto della sessione. Si sostituiscono i valori non finiti
            # con 0 (semplice silenzio) e si conta l'evento.
            if not np.isfinite(mono).all():
                self._glitches += 1
                mono = np.nan_to_num(mono, nan=0.0, posinf=0.0, neginf=0.0)
            self._last_rms = float(np.sqrt(np.mean(np.square(mono))))
            self._last_peak = float(np.max(np.abs(mono)))
            resampled = self._resampler.resample(mono)
        except Exception:
            return
        try:
            self._q.put_nowait(resampled)
        except queue.Full:
            # l'ASR non tiene il passo: scarta il blocco più vecchio e
            # conta l'overrun invece di bloccare il thread realtime
            try:
                self._q.get_nowait()
                self._q.put_nowait(resampled)
                self._overruns += 1
            except (queue.Empty, queue.Full):
                pass

    # -- consumo -----------------------------------------------------------
    def read(self, timeout: float = 1.0) -> np.ndarray | None:
        """Restituisce il prossimo blocco a 16 kHz mono, o None se scaduto."""
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def read_all(self) -> np.ndarray:
        """Svuota la coda concatenando i blocchi disponibili."""
        blocks = []
        while True:
            try:
                blocks.append(self._q.get_nowait())
            except queue.Empty:
                break
        if not blocks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(blocks)

    def drain(self, seconds: float) -> np.ndarray:
        """Raccoglie audio per un intervallo di tempo, bloccante."""
        target = int(self.target_rate * seconds)
        chunks: list[np.ndarray] = []
        got = 0
        deadline = time.monotonic() + seconds + 0.5
        while got < target and time.monotonic() < deadline:
            block = self.read(timeout=0.5)
            if block is None:
                continue
            chunks.append(block)
            got += len(block)
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(chunks)[:target]
