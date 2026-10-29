#!/usr/bin/env python3
"""Genera un WAV di prova in italiano per il collaudo della pipeline ASR.

Lo script non usa la rete e dipende solo da un TTS gia' installato sul
sistema. La strategia e' a due livelli:

* se ``espeak-ng`` (o ``espeak``) e' disponibile, si sintetizzano frasi
  italiane reali con ``-v it`` e le si incolla con silenzi controllati
  (0,3 s fra i gruppi di parole, 1,0 s fra le frasi): i silenzi da 1,0 s
  sono piu' lunghi della ``silence_ms`` del segmentatore (700 ms) e quindi
  devono produrre un confine di segmento;
* se non c'e' nessun TTS, si genera un segnale "a parlato" sintetico
  (rumore con inviluppo di armoni e modulazione sillabica) che rispetta
  la stessa struttura temporale; il file e' allora etichettato
  ``synthetic-non-lexical`` e non ha un riferimento lessicale.

Il WAV viene scritto nel percorso indicato (default
``/tmp/lt_test_italian.wav``); il riferimento testuale va nel ``.txt``
adiacente, con una riga di intestazione ``#`` che ne dichiara il tipo.

    python scripts/make_test_audio.py /tmp/lt_test_italian.wav
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

# --- parametri del segnale -------------------------------------------------
SAMPLE_RATE = 16000
CHUNK_GAP_S = 0.3  # silenzio fra gruppi di parole della stessa frase
SENTENCE_GAP_S = 1.0  # silenzio fra frasi (supera la silence_ms = 700 ms)
LEAD_S = 0.5  # silenzio iniziale: lascia assestare il rumore di fondo
TAIL_S = 1.0  # silenzio finale
WORDS_PER_CHUNK = 4  # parole per gruppo (il TTS viene invocato per gruppo)
ESPEAK_SPEED = 150  # parole/minuto
ESPEAK_VOICE = "it"

# Frasi di lezione: contengono parole riconoscibili per valutare l'accuratezza.
SENTENCES: list[str] = [
    "Buongiorno a tutti, benvenuti alla lezione di oggi.",
    "Il sistema di trascrizione funziona interamente in locale, senza inviare dati in rete.",
    "Oggi parliamo della struttura del modello acustico e del riconoscimento vocale.",
    "Il segmento viene chiuso quando il rilevatore trova settecento millisecondi di silenzio.",
    "Questo permette di finalizzare il testo rapidamente, entro uno o due secondi.",
    "La puntinatura è nativa del modello, quindi non costa nulla in termini di latenza.",
    "Il tutto funziona senza microfono grazie a un segnale audio sintetico generato per il collaudo.",
    "Per favore, aprite il quaderno e annotate la definizione di segmentazione temporale.",
]

# Candidati TTS: il primo disponibile vince.
TTS_CANDIDATES: tuple[str, ...] = ("espeak-ng", "espeak", "festival", "piper")


# ---------------------------------------------------------------------------
# TTS
# ---------------------------------------------------------------------------
def find_tts() -> str | None:
    """Restituisce il nome del primo TTS disponibile, o None."""
    for name in TTS_CANDIDATES:
        if shutil.which(name) is not None:
            return name
    return None


def espeak_voice_ok(binary: str) -> bool:
    """Verifica che il binario accetti la voce italiana."""
    if "espeak" not in binary:
        return True  # festival/piper: non conosciamo i flag, si tenta comunque
    try:
        proc = subprocess.run(
            [binary, "--voices"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return ESPEAK_VOICE in proc.stdout


def synth_chunk(binary: str, text: str, workdir: Path) -> tuple[np.ndarray, int]:
    """Sintetizza un frammento e restituisce (segnale float32, frequenza)."""
    out = workdir / "chunk.wav"
    cmd = [binary]
    if "espeak" in binary:
        cmd += ["-v", ESPEAK_VOICE, "-s", str(ESPEAK_SPEED), "-w", str(out)]
    else:
        cmd += ["-w", str(out)]
    cmd.append(text)
    proc = subprocess.run(cmd, capture_output=True, timeout=120)
    if proc.returncode != 0 or not out.exists():
        raise RuntimeError(
            f"{binary} fallito (rc={proc.returncode}): "
            f"{proc.stderr.decode('utf-8', 'replace')[:200]}"
        )
    data, rate = read_wav(out)
    return data, rate


# ---------------------------------------------------------------------------
# I/O WAV
# ---------------------------------------------------------------------------
def read_wav(path: Path) -> tuple[np.ndarray, int]:
    """Legge un WAV PCM e restituisce (float32 mono, frequenza)."""
    with wave.open(str(path), "rb") as w:
        n_ch = w.getnchannels()
        width = w.getsampwidth()
        rate = w.getframerate()
        raw = w.readframes(w.getnframes())
    if width == 2:
        data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif width == 4:
        data = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    elif width == 1:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        raise RuntimeError(f"formato WAV non supportato: {width * 8} bit")
    if n_ch > 1:
        data = data.reshape(-1, n_ch).mean(axis=1)
    return np.ascontiguousarray(data, dtype=np.float32), rate


def write_wav(path: Path, data: np.ndarray, rate: int = SAMPLE_RATE) -> None:
    """Scrive un WAV PCM 16 bit mono."""
    path.parent.mkdir(parents=True, exist_ok=True)
    clipped = np.clip(data, -1.0, 1.0)
    pcm = (clipped * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


# ---------------------------------------------------------------------------
# elaborazione del segnale
# ---------------------------------------------------------------------------
def trim_silence(data: np.ndarray, rel: float = 0.02, floor: float = 0.004) -> np.ndarray:
    """Rimuove silenzio iniziale e finale (soglia relativa al picco)."""
    if data.size == 0:
        return data
    peak = float(np.abs(data).max())
    if peak <= 0.0:
        return data
    thresh = max(peak * rel, floor)
    loud = np.nonzero(np.abs(data) > thresh)[0]
    if loud.size == 0:
        return data
    return data[loud[0] : loud[-1] + 1]


def resample(data: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Riscampiona a 16 kHz (soxr se disponibile, altrimenti lineare)."""
    if src_rate == dst_rate:
        return data
    try:
        import soxr

        return np.ascontiguousarray(
            soxr.resample(data, src_rate, dst_rate, quality="HQ"), dtype=np.float32
        )
    except Exception:
        n_out = int(round(len(data) * dst_rate / src_rate))
        x_old = np.arange(len(data), dtype=np.float64) * (src_rate / dst_rate)
        x_new = np.arange(n_out, dtype=np.float64)
        return np.interp(x_new, x_old, data).astype(np.float32)


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(round(seconds * SAMPLE_RATE)), dtype=np.float32)


def normalize(data: np.ndarray, peak: float = 0.89) -> np.ndarray:
    """Normalizza il picco: il resampler HQ puo' sbordare oltre 1,0."""
    if data.size == 0:
        return data
    current = float(np.abs(data).max())
    if current <= 0.0:
        return data
    return (data * (peak / current)).astype(np.float32)


def fallback_chunk(text: str, seed: int) -> np.ndarray:
    """Segnale 'a parlato' non lessicale: armoni + modulazione sillabica.

    Non e' voce: serve solo a esercitare il segmentatore e a misurare le
    prestazioni. La durata e' proporzionale al numero di parole.
    """
    words = max(1, len(text.split()))
    duration = 0.32 * words
    n = int(duration * SAMPLE_RATE)
    t = np.arange(n, dtype=np.float32) / SAMPLE_RATE
    rng = np.random.default_rng(seed)
    # tre "formanti" con inviluppo decadente, piu' una corda vocale a 120 Hz
    sig = np.zeros(n, dtype=np.float32)
    for freq, amp in ((700.0, 1.0), (1220.0, 0.6), (2600.0, 0.35)):
        sig += amp * np.sin(2 * np.pi * freq * t).astype(np.float32)
    sig += 0.4 * np.sin(2 * np.pi * 120.0 * t).astype(np.float32)
    sig += 0.05 * rng.standard_normal(n).astype(np.float32)
    # inviluppo sillabico (~4,5 sillabe/s) e tremolo leggero
    syl = 0.5 + 0.5 * np.sin(2 * np.pi * 4.5 * t - np.pi / 2)
    env = (syl ** 2).astype(np.float32)
    fade = np.minimum(np.arange(n) / (0.02 * SAMPLE_RATE), 1.0)
    fade = np.minimum(fade, (n - np.arange(n)) / (0.02 * SAMPLE_RATE)).astype(np.float32)
    out = sig * env * fade
    peak = float(np.abs(out).max())
    if peak > 0:
        out = out / peak * 0.45
    return out.astype(np.float32)


# ---------------------------------------------------------------------------
# piano di generazione
# ---------------------------------------------------------------------------
def chunk_sentences() -> list[tuple[str, bool]]:
    """Spezza le frasi in gruppi di parole.

    Restituisce una lista di (testo, ultimo_gruppo_della_frase).
    """
    plan: list[tuple[str, bool]] = []
    for sentence in SENTENCES:
        words = sentence.split()
        groups = [
            " ".join(words[i : i + WORDS_PER_CHUNK])
            for i in range(0, len(words), WORDS_PER_CHUNK)
        ]
        for j, group in enumerate(groups):
            plan.append((group, j == len(groups) - 1))
    return plan


def build_audio(tts: str | None) -> tuple[np.ndarray, str, str]:
    """Costruisce il segnale completo. Restituisce (audio, kind, dettaglio)."""
    plan = chunk_sentences()
    pieces: list[np.ndarray] = [silence(LEAD_S)]
    rng_seed = 20240101

    if tts is None:
        for i, (text, _last) in enumerate(plan):
            pieces.append(fallback_chunk(text, rng_seed + i))
            pieces.append(silence(CHUNK_GAP_S))
        pieces.append(silence(TAIL_S - CHUNK_GAP_S))
        return np.concatenate(pieces), "synthetic-non-lexical", "formant-noise"

    with tempfile.TemporaryDirectory(prefix="lt-tts-") as tmp:
        workdir = Path(tmp)
        for i, (text, last) in enumerate(plan):
            data, rate = synth_chunk(tts, text, workdir)
            data = trim_silence(data)
            data = resample(data, rate, SAMPLE_RATE)
            pieces.append(data)
            gap = SENTENCE_GAP_S if last else CHUNK_GAP_S
            pieces.append(silence(gap))
        pieces.append(silence(TAIL_S))

    audio = np.concatenate(pieces).astype(np.float32)
    return normalize(audio), "tts-lexical", tts


def write_reference(path: Path, kind: str, generator: str, sentences: list[str]) -> None:
    """Scrive il riferimento testuale accanto al WAV."""
    lines = [
        f"# kind: {kind}",
        f"# generator: {generator}",
        f"# sentences: {len(sentences)}",
    ]
    if kind == "tts-lexical":
        lines += sentences
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Genera un WAV di prova in italiano (senza rete)."
    )
    parser.add_argument(
        "out",
        nargs="?",
        default="/tmp/lt_test_italian.wav",
        help="percorso del WAV (default: /tmp/lt_test_italian.wav)",
    )
    parser.add_argument(
        "--json",
        dest="json_path",
        default=None,
        help="scrive anche un riepilogo JSON in questo percorso",
    )
    args = parser.parse_args(argv)

    out = Path(args.out).expanduser()
    tts = find_tts()
    if tts is not None and not espeak_voice_ok(tts):
        print(f"[warn] {tts} non espone la voce '{ESPEAK_VOICE}': uso il fallback")
        tts = None

    if tts:
        print(f"[tts] TTS usato: {tts}")
    else:
        print("[tts] nessun TTS trovato (cercati: " + ", ".join(TTS_CANDIDATES) + ")")
        print("[tts] fallback: segnale sintetico non lessicale")

    try:
        audio, kind, generator = build_audio(tts)
    except Exception as exc:
        print(f"[errore] generazione fallita: {exc}", file=sys.stderr)
        return 1

    write_wav(out, audio)
    ref = out.with_suffix(".txt")
    write_reference(ref, kind, generator, SENTENCES)

    duration = len(audio) / SAMPLE_RATE
    print(f"[ok] WAV: {out}")
    print(f"[ok] riferimento: {ref}")
    print(f"[ok] tipo: {kind} ({generator})")
    print(
        f"[ok] durata: {duration:.2f} s, campioni: {len(audio)}, "
        f"frasi: {len(SENTENCES)}"
    )
    print(
        f"[ok] silenzi: {LEAD_S} s iniziale, {CHUNK_GAP_S} s fra gruppi, "
        f"{SENTENCE_GAP_S} s fra frasi, {TAIL_S} s finale"
    )
    peak = float(np.abs(audio).max())
    rms = float(np.sqrt(np.mean(np.square(audio))))
    print(f"[ok] livelli: picco {peak:.3f}, rms {rms:.4f}")

    if args.json_path:
        summary = {
            "wav": str(out),
            "reference": str(ref),
            "kind": kind,
            "generator": generator,
            "sample_rate": SAMPLE_RATE,
            "duration_s": duration,
            "sentences": SENTENCES,
            "silences": {
                "lead_s": LEAD_S,
                "chunk_gap_s": CHUNK_GAP_S,
                "sentence_gap_s": SENTENCE_GAP_S,
                "tail_s": TAIL_S,
            },
        }
        Path(args.json_path).write_text(
            json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"[ok] riepilogo JSON: {args.json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
