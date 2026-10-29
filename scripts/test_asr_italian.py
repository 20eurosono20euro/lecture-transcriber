#!/usr/bin/env python3
"""Collaudo end-to-end della pipeline ASR su un WAV italiano.

Il percorso verificato e' quello reale dell'applicazione:

    Segmenter.push(blocchi 16 kHz mono)  ->  segmento chiuso
                                          ->  TranscriptionEngine.transcribe()
                                          ->  testo, latenza, RTF

Lo script carica il modello **in locale** (``HF_HUB_OFFLINE=1``): non deve
tentare alcun download. Se il modello non e' gia' in cache fallisce con
un messaggio esplicito e un codice di uscita diverso da zero.

Il codice di uscita segnala un **pipeline rotto** (modello che non si
carica, nessun segmento prodotto, testo vuoto su audio non silenzioso),
non un riconoscimento mediocre: la qualita' del riconoscimento su una
voce sintetica e' un'osservazione, e viene stampata come tale.

    python scripts/test_asr_italian.py --audio /tmp/lt_test_italian.wav
"""

from __future__ import annotations

import argparse
import difflib
import os
import re
import sys
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

# HF_HUB_OFFLINE va impostata PRIMA di importare huggingface_hub: la
# libreria legge la variabile al momento dell'import e la congela.
os.environ.setdefault("HF_HUB_OFFLINE", "1")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import numpy as np  # noqa: E402

from lecture_transcriber.asr import (  # noqa: E402
    ASRConfig,
    Segmenter,
    TranscriptionEngine,
)

DEFAULT_AUDIO = "/tmp/lt_test_italian.wav"
DEFAULT_MODEL_DIR = REPO_ROOT / "models"
BLOCK_MS = 100  # dimensione dei blocchi, come nella cattura reale
SILENCE_S = 0.7  # silenzio che deve produrre un confine di segmento
SPEECH_RMS_MIN = 0.005  # sotto questa soglia il blocco e' considerato muto


# ---------------------------------------------------------------------------
# strutture dati
# ---------------------------------------------------------------------------
@dataclass
class SegmentReport:
    """Esito della decodifica di un segmento."""

    index: int
    start: float
    end: float
    duration: float
    decode_s: float
    texts: list[str] = field(default_factory=list)
    rms: float = 0.0

    @property
    def text(self) -> str:
        return " ".join(self.texts).strip()

    @property
    def rtf(self) -> float:
        """Real-time factor: secondi di calcolo per secondo di audio."""
        return self.decode_s / self.duration if self.duration > 0 else float("inf")


@dataclass
class Reference:
    """Trascrizione di riferimento letta dal .txt accanto al WAV."""

    kind: str
    generator: str
    sentences: list[str]

    @property
    def usable(self) -> bool:
        """Il riferimento e' confrontabile solo se e' lessicale."""
        return self.kind == "tts-lexical" and bool(self.sentences)


# ---------------------------------------------------------------------------
# I/O e normalizzazione
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


def read_reference(path: Path) -> Reference | None:
    """Legge il file .txt di riferimento, se presente."""
    if not path.exists():
        return None
    kind = "unknown"
    generator = ""
    sentences: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            parts = line.lstrip("#").split(":", 1)
            if len(parts) == 2:
                key, value = parts[0].strip(), parts[1].strip()
                if key == "kind":
                    kind = value
                elif key == "generator":
                    generator = value
            continue
        sentences.append(line)
    return Reference(kind=kind, generator=generator, sentences=sentences)


_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """Minuscole, senza punteggiatura, spazi compattati."""
    text = text.lower()
    text = text.replace("’", "'")
    text = _PUNCT_RE.sub(" ", text)
    return _SPACE_RE.sub(" ", text).strip()


def similarity(a: str, b: str) -> float:
    """Somiglianza difflib fra due stringhe normalizzate (0..1)."""
    if not a and not b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def wer(reference: str, hypothesis: str) -> tuple[float, int, int, int, int]:
    """Word Error Rate approssimato (allineamento difflib a parole).

    Restituisce (wer, errori, parole_riferimento, parole_ipotesi, dettaglio).
    Il conteggio errori usa i tag di difflib: sostituzioni + cancellazioni
    + inserzioni; un "replace" vale come 1 errore (non 2).
    """
    ref_words = normalize_text(reference).split()
    hyp_words = normalize_text(hypothesis).split()
    matcher = difflib.SequenceMatcher(None, ref_words, hyp_words, autojunk=False)
    errors = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "replace":
            errors += max(i2 - i1, j2 - j1)
        elif tag == "delete":
            errors += i2 - i1
        elif tag == "insert":
            errors += j2 - j1
    n_ref = len(ref_words)
    wer_value = errors / n_ref if n_ref else 0.0
    return wer_value, errors, n_ref, len(hyp_words), errors


# ---------------------------------------------------------------------------
# analisi dei silenzi
# ---------------------------------------------------------------------------
def detect_silences(
    audio: np.ndarray, sample_rate: int, min_silence_s: float = SILENCE_S
) -> list[tuple[float, float]]:
    """Trova gli intervalli silenziosi (>= min_silence_s) del segnale.

    Usa un inviluppo RMS a blocchi di 20 ms con soglia relativa al picco,
    coerente con l'approssimazione del :class:`Segmenter`.
    """
    frame = int(sample_rate * 0.02)
    n_frames = len(audio) // frame
    if n_frames == 0:
        return []
    frames = audio[: n_frames * frame].reshape(n_frames, frame)
    rms = np.sqrt(np.mean(np.square(frames), axis=1))
    peak = float(rms.max())
    if peak <= 0.0:
        return []
    threshold = max(peak * 0.08, 0.004)
    quiet = rms <= threshold

    silences: list[tuple[float, float]] = []
    start: int | None = None
    for i, is_quiet in enumerate(quiet):
        if is_quiet and start is None:
            start = i
        elif not is_quiet and start is not None:
            duration = (i - start) * frame / sample_rate
            if duration >= min_silence_s:
                silences.append(
                    (start * frame / sample_rate, i * frame / sample_rate)
                )
            start = None
    if start is not None:
        duration = (n_frames - start) * frame / sample_rate
        if duration >= min_silence_s:
            silences.append((start * frame / sample_rate, len(audio) / sample_rate))
    return silences


def boundary_in_silence(t: float, silences: list[tuple[float, float]]) -> bool:
    """True se il tempo t cade dentro (o ai bordi di) un intervallo silenzioso."""
    for start, end in silences:
        if start - 0.15 <= t <= end + 0.15:
            return True
    return False


def has_speech(audio: np.ndarray, sample_rate: int) -> tuple[bool, float]:
    """Verifica con lo stesso VAD Silero usato dalla pipeline se c'e' parlato.

    Restituisce (c'e' parlato, secondi di parlato). Serve a distinguere un
    modello che non riconosce nulla da un audio che **non contiene voce**
    (per esempio il fallback sintetico non lessicale): in quest'ultimo
    caso il silenzio e' la risposta corretta, non un difetto.
    """
    try:
        from faster_whisper.vad import VadOptions, get_speech_timestamps

        chunks = get_speech_timestamps(
            audio,
            VadOptions(min_silence_duration_ms=700, speech_pad_ms=200),
            sampling_rate=sample_rate,
        )
    except Exception:
        # senza VAD non sappiamo distinguere: assume che ci sia parlato
        return True, len(audio) / sample_rate
    speech_s = sum((c["end"] - c["start"]) for c in chunks) / sample_rate
    return speech_s > 0.0, speech_s


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------
def run_pipeline(
    audio: np.ndarray,
    sample_rate: int,
    engine: TranscriptionEngine,
    config: ASRConfig,
) -> tuple[list[SegmentReport], float]:
    """Passa l'audio attraverso il segmentatore e decodifica ogni segmento."""
    segmenter = Segmenter(config, sample_rate=sample_rate)
    block = int(sample_rate * BLOCK_MS / 1000)
    reports: list[SegmentReport] = []
    audio_pos = 0.0
    t_start = time.perf_counter()

    def handle(chunk: np.ndarray) -> None:
        nonlocal audio_pos
        t0 = time.perf_counter()
        segments = engine.transcribe(chunk, final=True)
        decode_s = time.perf_counter() - t0
        duration = len(chunk) / sample_rate
        rms = float(np.sqrt(np.mean(np.square(chunk)))) if len(chunk) else 0.0
        reports.append(
            SegmentReport(
                index=len(reports),
                start=audio_pos,
                end=audio_pos + duration,
                duration=duration,
                decode_s=decode_s,
                texts=[s.text for s in segments],
                rms=rms,
            )
        )
        audio_pos += duration

    for i in range(0, len(audio), block):
        closed = segmenter.push(audio[i : i + block])
        if closed is not None:
            handle(closed)
    tail = segmenter.flush()
    if tail is not None:
        handle(tail)

    return reports, time.perf_counter() - t_start


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Collaudo end-to-end della pipeline ASR (solo locale)."
    )
    parser.add_argument("--audio", default=DEFAULT_AUDIO, help="WAV 16 kHz mono")
    parser.add_argument(
        "--model", default="large-v3-turbo", help="nome o percorso del modello"
    )
    parser.add_argument(
        "--model-dir",
        default=str(DEFAULT_MODEL_DIR),
        help="cache locale dei modelli (HF)",
    )
    parser.add_argument("--reference", default=None, help="file .txt di riferimento")
    parser.add_argument(
        "--block-ms", type=int, default=BLOCK_MS, help="dimensione dei blocchi"
    )
    parser.add_argument(
        "--silence-ms", type=int, default=None, help="soglia di silenzio del segmentatore"
    )
    args = parser.parse_args(argv)

    audio_path = Path(args.audio).expanduser()
    if not audio_path.exists():
        print(f"[ERRORE] WAV non trovato: {audio_path}", file=sys.stderr)
        print(
            "[ERRORE] generarlo con: python scripts/make_test_audio.py "
            f"{audio_path}",
            file=sys.stderr,
        )
        return 2

    reference_path = (
        Path(args.reference).expanduser()
        if args.reference
        else audio_path.with_suffix(".txt")
    )
    reference = read_reference(reference_path)

    try:
        audio, sample_rate = read_wav(audio_path)
    except Exception as exc:
        print(f"[ERRORE] lettura WAV fallita: {exc}", file=sys.stderr)
        return 2

    duration = len(audio) / sample_rate
    print("=" * 78)
    print("COLLAUDO PIPELINE ASR — LECTURE TRANSCRIBER")
    print("=" * 78)
    print(f"audio        : {audio_path}")
    print(f"campioni     : {len(audio)} @ {sample_rate} Hz mono ({duration:.2f} s)")
    print(f"modello      : {args.model}")
    print(f"cache modelli: {args.model_dir}")
    print(f"HF_HUB_OFFLINE: {os.environ.get('HF_HUB_OFFLINE')}")
    if reference is not None:
        print(f"riferimento  : {reference_path} (kind={reference.kind})")
    else:
        print("riferimento  : assente (nessun confronto di accuratezza)")

    config = ASRConfig(model_name=args.model)
    if args.silence_ms is not None:
        config.silence_ms = args.silence_ms

    # -- caricamento --------------------------------------------------------
    print("-" * 78)
    print("[1/4] caricamento del modello")
    engine = TranscriptionEngine(config, args.model_dir)
    t0 = time.perf_counter()
    engine.load_async().join()
    load_s = time.perf_counter() - t0
    print(f"      tempo di caricamento: {load_s:.2f} s")
    if engine.load_error or not engine.is_ready:
        print(f"      ERRORE: {engine.load_error}", file=sys.stderr)
        print("PIPELINE BROKEN: il modello non si e' caricato.")
        return 3
    print("      modello pronto")

    # -- segmentazione ------------------------------------------------------
    print("-" * 78)
    print("[2/4] segmentazione")
    silences = detect_silences(audio, sample_rate)
    print(f"      silenzi >= {SILENCE_S:.1f} s rilevati: {len(silences)}")
    for i, (s, e) in enumerate(silences):
        print(f"        #{i + 1} {s:7.2f} s -> {e:7.2f} s  ({e - s:.2f} s)")

    # -- decodifica ---------------------------------------------------------
    print("-" * 78)
    print("[3/4] decodifica")
    reports, total_s = run_pipeline(audio, sample_rate, engine, config)
    print(
        f"      segmenti prodotti: {len(reports)}  "
        f"(tempo totale: {total_s:.2f} s, RTF globale {total_s / duration:.3f})"
    )
    print()
    print(
        f"      {'#':>2}  {'inizio':>7}  {'dur':>6}  {'decode':>7}  "
        f"{'RTF':>6}  confine  testo"
    )
    print("      " + "-" * 74)
    hypothesis_parts: list[str] = []
    boundary_hits = 0
    for r in reports:
        on_boundary = boundary_in_silence(r.end, silences)
        if on_boundary:
            boundary_hits += 1
        hypothesis_parts.append(r.text)
        print(
            f"      {r.index:>2}  {r.start:>6.2f}s  {r.duration:>5.2f}s  "
            f"{r.decode_s:>6.2f}s  {r.rtf:>6.3f}  "
            f"{'si' if on_boundary else 'no ':>7}  {r.text}"
        )
    print()
    if silences:
        print(
            f"      confini su silenzio >= {SILENCE_S:.1f} s: "
            f"{boundary_hits}/{len(reports)} segmenti"
        )

    # -- accuratezza --------------------------------------------------------
    print("-" * 78)
    print("[4/4] accuratezza")
    hypothesis = " ".join(hypothesis_parts).strip()
    wer_value: float | None = None
    if reference is not None and reference.usable:
        ref_text = " ".join(reference.sentences)
        ratio = similarity(ref_text, hypothesis)
        wer_value, errors, n_ref, n_hyp, _ = wer(ref_text, hypothesis)
        print(f"      riferimento ({len(reference.sentences)} frasi):")
        for sentence in reference.sentences:
            print(f"        - {sentence}")
        print()
        print(f"      ipotesi:")
        print(f"        {hypothesis}")
        print()
        print(f"      somiglianza difflib : {ratio:.4f}")
        print(
            f"      WER approssimato    : {wer_value:.4f} "
            f"({errors} errori su {n_ref} parole di riferimento, "
            f"{n_hyp} parole riconosciute)"
        )
        print(f"      WER in percentuale   : {wer_value * 100:.1f} %")
    elif reference is not None:
        print(
            f"      riferimento non lessicale (kind={reference.kind}): "
            "confronto di accuratezza non applicabile"
        )
    else:
        print("      nessun riferimento disponibile: accuratezza non calcolata")

    # -- verdetto -----------------------------------------------------------
    print("=" * 78)
    speech_present, speech_s = has_speech(audio, sample_rate)
    print(
        f"[VAD] parlo rilevato dal VAD Silero: {speech_s:.2f} s "
        f"su {duration:.2f} s di audio"
    )
    problems: list[str] = []
    if not reports:
        problems.append("nessun segmento prodotto dal segmentatore")
    empty_loud = [r.index for r in reports if r.rms > SPEECH_RMS_MIN and not r.text]
    if empty_loud and speech_present:
        problems.append(
            f"testo vuoto su audio che il VAD riconosce come parlato "
            f"(segmenti {empty_loud})"
        )
    if problems:
        for p in problems:
            print(f"[ERRORE] {p}", file=sys.stderr)
        print("PIPELINE BROKEN")
        return 4

    if not speech_present:
        print(
            "PIPELINE OK (con avvertenza): il VAD non rileva parlato in questo "
            "audio, quindi non c'e' testo da riconoscere. Il silenzio in "
            "output e' la risposta corretta, non un difetto della pipeline."
        )
        print("=" * 78)
        return 0

    print("PIPELINE OK")
    if wer_value is not None:
        verdict = "ottima" if wer_value < 0.10 else ("accettabile" if wer_value < 0.25 else "poor")
        print(
            f"Osservazione di accuratezza: WER {wer_value * 100:.1f} % "
            f"({verdict}) su voce sintetica espeak-ng."
        )
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
