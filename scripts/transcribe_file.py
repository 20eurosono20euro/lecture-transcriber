#!/usr/bin/env python3
"""Trascrizione headless di un file audio, senza interfaccia grafica.

Utile per verificare la pipeline, per ri-elaborare una registrazione e per
avere una modalita' da riga di comando leggibile da script:

    scripts/transcribe_file.py lezione.wav
    scripts/transcribe_file.py lezione.wav --course "Analisi II" --export out.md
    scripts/transcribe_file.py --mic --course "Analisi II"     # dal microfono

Usa esattamente gli stessi moduli della GUI: AudioCapture (o un WAV),
Segmenter, TranscriptionEngine e SessionStore.
"""

from __future__ import annotations

import argparse
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import numpy as np  # noqa: E402
import soxr  # noqa: E402

from lecture_transcriber.asr import (  # noqa: E402
    ASRConfig,
    Segmenter,
    TranscriptionEngine,
)
from lecture_transcriber.audio import AudioCapture  # noqa: E402
from lecture_transcriber.config import (  # noqa: E402
    DATA_DIR,
    MODEL_DIR,
    ensure_dirs,
    load_settings,
)
from lecture_transcriber.export import Exporter  # noqa: E402
from lecture_transcriber.sessions import SessionStore  # noqa: E402

TARGET_RATE = 16000


def read_wav(path: Path) -> np.ndarray:
    """Legge un WAV e restituisce mono float32 a 16 kHz."""
    with wave.open(str(path), "rb") as w:
        channels = w.getnchannels()
        width = w.getsampwidth()
        rate = w.getframerate()
        frames = w.readframes(w.getnframes())

    if width == 2:
        data = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    elif width == 4:
        data = np.frombuffer(frames, dtype=np.int32).astype(np.float32) / 2147483648.0
    elif width == 1:
        data = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        raise SystemExit(f"Formato WAV non supportato: {width * 8} bit per campione")

    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    if rate != TARGET_RATE:
        data = soxr.resample(data, rate, TARGET_RATE)
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--version",
        "-V",
        action="store_true",
        help="stampa la versione ed esce",
    )
    parser.add_argument("audio", nargs="?", help="file WAV da trascrivere")
    parser.add_argument(
        "--mic",
        action="store_true",
        help="trascrive dal microfono fino all'interruzione (Ctrl+C)",
    )
    parser.add_argument("--course", default="", help="nome del corso")
    parser.add_argument("--professor", default="", help="nome del docente")
    parser.add_argument("--date", default="", help="data della lezione (YYYY-MM-DD)")
    parser.add_argument("--model", default=None, help="modello Whisper da usare")
    parser.add_argument("--export", default=None, help="percorso del file esportato")
    parser.add_argument(
        "--format",
        default="md_ts",
        choices=["txt", "md", "md_ts", "srt"],
        help="formato di esportazione",
    )
    parser.add_argument("--quiet", action="store_true", help="solo il testo finale")
    args = parser.parse_args(argv)

    if args.version:
        from lecture_transcriber import __version__

        print(f"lecture-transcriber {__version__}")
        return 0

    if not args.audio and not args.mic:
        parser.error("serve un file WAV oppure --mic")

    ensure_dirs()
    settings = load_settings()
    model_name = args.model or settings.model_name

    cfg = ASRConfig(
        model_name=model_name,
        compute_type=settings.compute_type,
        device=settings.device,
        language=settings.language,
        silence_ms=settings.vad_silence_ms,
        min_speech_ms=settings.vad_min_speech_ms,
        max_segment_s=settings.max_segment_s,
    )

    if not args.quiet:
        print(f"Modello: {model_name} ({cfg.compute_type}, {cfg.device})", flush=True)

    engine = TranscriptionEngine(cfg, model_dir=str(MODEL_DIR))
    ready = {"ok": False}
    t0 = time.time()
    engine.load_async(on_ready=lambda: ready.update(ok=True))
    while not ready["ok"]:
        time.sleep(0.3)
        if engine.load_error:
            print(f"Errore: {engine.load_error}", file=sys.stderr)
            return 2
    if not args.quiet:
        print(f"Modello caricato in {time.time() - t0:.1f}s", flush=True)

    store = SessionStore(DATA_DIR / "sessions.db")
    session_id = store.create_session(args.course, args.professor, args.date)
    segmenter = Segmenter(cfg, sample_rate=TARGET_RATE)

    index = 0
    t_start = 0.0
    total_audio = 0.0
    decode_time = 0.0
    prev_text = ""

    def handle(segment: np.ndarray) -> None:
        nonlocal index, t_start, total_audio, decode_time, prev_text
        if segment is None or len(segment) == 0:
            return
        t_dec = time.time()
        segments = engine.transcribe(segment, final=True, previous_text=prev_text)
        decode_time += time.time() - t_dec
        text = " ".join(s.text for s in segments).strip()
        t_end = t_start + len(segment) / TARGET_RATE
        if text:
            store.add_segment(session_id, index, t_start, t_end, text, final=True)
            if not args.quiet:
                print(f"[{t_end:7.2f}s] {text}", flush=True)
            prev_text = text
            index += 1
        t_start = t_end
        total_audio += len(segment) / TARGET_RATE

    if args.audio:
        audio = read_wav(Path(args.audio))
        if not args.quiet:
            print(f"Audio: {len(audio) / TARGET_RATE:.1f}s", flush=True)
        step = 1600
        for i in range(0, len(audio), step):
            handle(segmenter.push(audio[i : i + step]))
        handle(segmenter.flush())
    else:
        capture = AudioCapture(
            device=settings.input_device,
            sample_rate=settings.sample_rate,
            target_rate=TARGET_RATE,
        )
        try:
            capture.start()
        except Exception as exc:
            print(f"Impossibile aprire il microfono: {exc}", file=sys.stderr)
            return 3
        if not args.quiet:
            print("In ascolto: Ctrl+C per fermare", flush=True)
        try:
            while True:
                block = capture.read(timeout=0.5)
                if block is None:
                    continue
                handle(segmenter.push(block))
        except KeyboardInterrupt:
            print("\nInterrotto.", flush=True)
        finally:
            handle(segmenter.flush())
            capture.stop()

    store.end_session(session_id)

    if not args.quiet:
        print("-" * 60)
        print(
            f"Segmenti: {index}  audio: {total_audio:.1f}s  "
            f"decodifica: {decode_time:.1f}s  "
            f"RTF: {decode_time / max(total_audio, 0.001):.3f}"
        )

    if args.export:
        exporter = Exporter(store)
        path = exporter.export(session_id, args.format, Path(args.export))
        print(f"Esportato in {path}", flush=True)

    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
