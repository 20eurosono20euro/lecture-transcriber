"""Registrazione opzionale dell'audio della sessione.

La preferenza ``Settings.record_audio`` (spenta per impostazione predefinita,
brief §8) promette che l'audio della lezione finisca in
``AUDIO_DIR/<sessione>.wav``. Prima di questo modulo la promessa era solo
un'etichetta: la casella si accendeva, il toast diceva "l'audio viene
scritto su disco" e **nessun byte** veniva scritto. Un impegno preso con
l'utente sulla privacy va mantenuto o tolto, non lasciato finto.

Il file e' WAV PCM 16 bit mono alla frequenza di lavoro del modello
(16 kHz): lo stesso segnale che arriva all'ASR, nessuna conversione
in piu'. La cartella si crea al primo uso, cosi' chi non registra non si
trova una cartella audio vuota.

La scrittura avviene sul thread di cattura, quindi resta fuori dal
callback realtime di PortAudio: un disco lento puo' rallentare la
trascrizione (e produrre overrun, gia' contati e riportati) ma non puo'
bloccare il thread realtime.
"""

from __future__ import annotations

import wave
from pathlib import Path

try:
    import numpy as np
except ImportError:  # pragma: no cover - dipende dall'ambiente
    np = None  # type: ignore[assignment]

#: Fondo scala del PCM a 16 bit con segno.
_FULL_SCALE = 32767.0


class WavRecorder:
    """Scrive blocchi float32 mono [-1, 1] in un WAV PCM 16 bit."""

    def __init__(self, path: Path, sample_rate: int = 16000) -> None:
        self.path = Path(path)
        self.sample_rate = int(sample_rate)
        self._wav: wave.Wave_write | None = None
        self.frames_written = 0

    @property
    def active(self) -> bool:
        return self._wav is not None

    def open(self) -> None:
        """Apre il file, creando la cartella se serve."""
        if self._wav is not None:
            return
        if np is None:
            raise RuntimeError("numpy non e' installato")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        wav = wave.open(str(self.path), "wb")
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(self.sample_rate)
        self._wav = wav

    def write(self, block) -> None:
        """Aggiunge un blocco. Il silenzio (``None``/vuoto) e' innocuo."""
        wav = self._wav
        if wav is None or block is None:
            return
        samples = np.asarray(block, dtype=np.float32).reshape(-1)
        if samples.size == 0:
            return
        # NaN/Inf non possono essere convertiti in int16: ``np.clip`` li
        # lascia passare (``clip(nan) == nan``) e il cast e' indefinito —
        # misurato: ``RuntimeWarning: invalid value encountered in cast`` e
        # un campione spazzatura nel WAV. ``AudioCapture`` li sostituisce
        # gia' nell'audio che arriva qui, ma il WAV non deve dipendere da
        # quella garanzia: si azzerano anche qui.
        samples = np.nan_to_num(samples, nan=0.0, posinf=0.0, neginf=0.0)
        clipped = np.clip(samples, -1.0, 1.0) * _FULL_SCALE
        wav.writeframes(clipped.astype("<i2").tobytes())
        self.frames_written += int(samples.size)

    def close(self) -> None:
        """Chiude il file (idempotente). Aggiorna l'intestazione WAV."""
        wav, self._wav = self._wav, None
        if wav is None:
            return
        try:
            wav.writeframes(b"")  # forza la riscrittura dell'header
        except Exception:
            pass
        try:
            wav.close()
        except Exception:
            pass

    def __enter__(self) -> "WavRecorder":
        self.open()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
