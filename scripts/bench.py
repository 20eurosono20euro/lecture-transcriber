#!/usr/bin/env python3
"""Benchmark delle prestazioni in tempo reale della pipeline ASR.

Misura, per ogni modello disponibile in locale:

* tempo di caricamento del modello;
* tempo di decodifica per segmento e RTF (secondi di calcolo per secondo
  di audio);
* CPU utilizzata (dall'utente + sistema del processo, in percentuale
  rispetto al tempo trascorso);
* picco di RSS letto da ``/proc/self/status`` (``VmHWM``).

Il punto operativo che conta e' la **latenza di decodifica**: se il
modello impiega piu' di 1-3 secondi per una finestra di 10-30 s, il
segmento non puo' essere finalizzato in tempo utile durante una lezione.
Per questo la tabella riporta anche "s per 10 s di audio" e la latenza
misurata su finestre di 10 s e 30 s.

I modelli non presenti in locale vengono **saltati** con un messaggio
chiaro; ``--download`` permette di scaricarli (richiede la rete).

    python scripts/bench.py
    python scripts/bench.py --models large-v3-turbo --windows 10 30
"""

from __future__ import annotations

import argparse
import os
import resource
import subprocess
import sys
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

DEFAULT_MODELS = ("small", "medium", "large-v3-turbo")
DEFAULT_AUDIO = "/tmp/lt_test_italian.wav"
DEFAULT_WINDOWS = (10.0, 30.0)


# ---------------------------------------------------------------------------
# misure di sistema
# ---------------------------------------------------------------------------
def peak_rss_mb() -> float:
    """Picco di RSS del processo in MiB (VmHWM su Linux)."""
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmHWM:"):
                kb = float(line.split()[1])
                return kb / 1024.0
    except OSError:
        pass
    # fallback: max RSS di getrusage (in KiB su Linux)
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def cpu_percent() -> float:
    """CPU del processo (utente + sistema) in percentuale della wall time."""
    usage = resource.getrusage(resource.RUSAGE_SELF)
    cpu_s = usage.ru_utime + usage.ru_stime
    wall = max(time.process_time(), 1e-9)
    return 100.0 * cpu_s / max(wall, 1e-9)


def cpu_time_s() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


# ---------------------------------------------------------------------------
# modelli
# ---------------------------------------------------------------------------
@dataclass
class ModelResult:
    """Esito del benchmark per un modello."""

    name: str
    status: str = "ok"  # ok | skipped | failed
    message: str = ""
    load_s: float = 0.0
    segments: list[tuple[float, float, float]] = field(default_factory=list)
    # (durata audio, tempo decode, testo)
    rtf: float = 0.0
    per_10s: float = 0.0
    rss_mb: float = 0.0
    cpu_pct: float = 0.0
    windows: dict[float, float] = field(default_factory=dict)
    # ripetizioni: (rtf, decodifica totale, {finestra: latenza})
    runs: list[tuple[float, float, dict[float, float]]] = field(default_factory=list)

    @property
    def total_audio_s(self) -> float:
        return sum(a for a, _d, _t in self.segments)

    @property
    def total_decode_s(self) -> float:
        """Secondi di decodifica totali (mediana se ci sono ripetizioni)."""
        if self.runs:
            decodes = sorted(r[1] for r in self.runs)
            return decodes[len(decodes) // 2]
        return sum(d for _a, d, _t in self.segments)


def model_is_local(name: str, model_dir: Path) -> bool:
    """True se il checkpoint e' gia' presente nella cache locale."""
    from faster_whisper.utils import _MODELS

    repo = _MODELS.get(name, name)
    slug = "models--" + repo.replace("/", "--")
    cache = model_dir / slug / "snapshots"
    if not cache.is_dir():
        return False
    for snap in cache.iterdir():
        if (snap / "model.bin").exists():
            return True
    return False


def download_model(name: str, model_dir: Path) -> None:
    """Scarica il modello nella cache locale (richiede la rete)."""
    from faster_whisper.utils import download_model as hf_download

    print(f"      download di '{name}' in corso...")
    hf_download(name, cache_dir=str(model_dir))
    print(f"      '{name}' scaricato")


# ---------------------------------------------------------------------------
# audio
# ---------------------------------------------------------------------------
def ensure_audio(path: Path) -> tuple[np.ndarray, int]:
    """Carica il WAV di prova, generandolo se manca."""
    if not path.exists():
        print(f"[audio] {path} assente: genero l'audio di prova")
        subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "make_test_audio.py"), str(path)],
            check=True,
        )
    with wave.open(str(path), "rb") as w:
        n_ch = w.getnchannels()
        width = w.getsampwidth()
        rate = w.getframerate()
        raw = w.readframes(w.getnframes())
    if width == 2:
        data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif width == 4:
        data = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        raise RuntimeError(f"formato WAV non supportato: {width * 8} bit")
    if n_ch > 1:
        data = data.reshape(-1, n_ch).mean(axis=1)
    return np.ascontiguousarray(data, dtype=np.float32), rate


def slice_window(audio: np.ndarray, rate: int, seconds: float) -> np.ndarray:
    """Prende una finestra di `seconds` dall'inizio del segnale."""
    n = min(int(seconds * rate), len(audio))
    return audio[:n]


# ---------------------------------------------------------------------------
# benchmark
# ---------------------------------------------------------------------------
def bench_model(
    name: str,
    audio: np.ndarray,
    rate: int,
    model_dir: Path,
    windows: tuple[float, ...],
    device: str = "cpu",
    compute_type: str = "int8",
    repeat: int = 1,
) -> ModelResult:
    """Esegue il benchmark su un modello.

    Con ``repeat > 1`` la decodifica viene eseguita piu' volte e i numeri
    riportati sono la **mediana**: su una macchina caricata la singola
    misura e' rumorosa (RTF 0,45-0,60 sullo stesso audio).
    """
    from lecture_transcriber.asr import ASRConfig, TranscriptionEngine, Segmenter

    result = ModelResult(name=name)
    rss_before = peak_rss_mb()

    config = ASRConfig(
        model_name=name, compute_type=compute_type, device=device, language="it"
    )
    engine = TranscriptionEngine(config, str(model_dir))
    t0 = time.perf_counter()
    engine.load_async().join()
    result.load_s = time.perf_counter() - t0
    if engine.load_error or not engine.is_ready:
        result.status = "failed"
        result.message = engine.load_error or "modello non pronto"
        return result

    block = int(rate * 0.1)

    for iteration in range(max(1, repeat)):
        # -- decodifica segmenti reali (dal segmentatore) -------------------
        segmenter = Segmenter(config, sample_rate=rate)
        cpu0 = cpu_time_s()
        wall0 = time.perf_counter()
        segments: list[tuple[float, float, str]] = []

        def decode(chunk: np.ndarray) -> None:
            t = time.perf_counter()
            segs = engine.transcribe(chunk, final=True)
            dt = time.perf_counter() - t
            segments.append(
                (len(chunk) / rate, dt, " ".join(s.text for s in segs))
            )

        for i in range(0, len(audio), block):
            closed = segmenter.push(audio[i : i + block])
            if closed is not None:
                decode(closed)
        tail = segmenter.flush()
        if tail is not None:
            decode(tail)

        wall = time.perf_counter() - wall0
        cpu = cpu_time_s() - cpu0

        total_audio = sum(a for a, _d, _t in segments)
        total_decode = sum(d for _a, d, _t in segments)
        rtf = total_decode / total_audio if total_audio else 0.0

        # -- latenza di decodifica su finestre di N secondi ------------------
        window_times: dict[float, float] = {}
        for w in windows:
            window = slice_window(audio, rate, w)
            if len(window) < int(0.5 * rate):
                window_times[w] = float("nan")
                continue
            t = time.perf_counter()
            engine.transcribe(window, final=True)
            window_times[w] = time.perf_counter() - t

        result.runs.append((rtf, total_decode, window_times))
        if iteration == 0:
            result.segments = segments
            result.cpu_pct = 100.0 * cpu / wall if wall > 0 else 0.0

    # -- riassunto: mediana delle ripetizioni --------------------------------
    rtfs = sorted(r[0] for r in result.runs)
    result.rtf = rtfs[len(rtfs) // 2]
    result.per_10s = result.rtf * 10.0
    decodes = sorted(r[1] for r in result.runs)
    for w in windows:
        vals = [r[2].get(w, float("nan")) for r in result.runs]
        vals = [v for v in vals if v == v]
        result.windows[w] = sorted(vals)[len(vals) // 2] if vals else float("nan")
    result.rss_mb = peak_rss_mb()
    result.message = f"caricamento +{result.rss_mb - rss_before:.0f} MiB"

    return result


# ---------------------------------------------------------------------------
# presentazione
# ---------------------------------------------------------------------------
def print_table(results: list[ModelResult]) -> None:
    """Stampa la tabella riassuntiva."""
    print()
    print("=" * 92)
    print("RISULTATI")
    print("=" * 92)
    header = (
        f"{'modello':<18} {'RTF':>7} {'s/10s':>8} {'caric. s':>9} "
        f"{'CPU %':>7} {'RSS MiB':>9}  stato"
    )
    print(header)
    print("-" * 92)
    for r in results:
        if r.status == "ok":
            print(
                f"{r.name:<18} {r.rtf:>7.3f} {r.per_10s:>8.2f} {r.load_s:>9.2f} "
                f"{r.cpu_pct:>7.1f} {r.rss_mb:>9.0f}  ok"
            )
        elif r.status == "skipped":
            print(f"{r.name:<18} {'-':>7} {'-':>8} {'-':>9} {'-':>7} {'-':>9}  SKIP: {r.message}")
        else:
            print(f"{r.name:<18} {'-':>7} {'-':>8} {'-':>9} {'-':>7} {'-':>9}  FALLITO: {r.message}")
    print("-" * 92)
    print("RTF = secondi di calcolo per secondo di audio (< 1 = piu' veloce del realtime)")
    print("s/10s = secondi necessari per elaborare 10 s di audio")


def print_windows(results: list[ModelResult], windows: tuple[float, ...]) -> None:
    """Stampa la tabella delle latenze di decodifica."""
    print()
    print("=" * 92)
    print("LATENZA DI DECODIFICA (il numero che decide se un segmento puo' essere finalizzato in 1-3 s)")
    print("=" * 92)
    header = f"{'modello':<18}" + "".join(f"{'finestra ' + format(w, '.0f') + ' s':>18}" for w in windows)
    print(header)
    print("-" * 92)
    for r in results:
        if r.status != "ok":
            print(f"{r.name:<18}" + "".join(f"{'-':>18}" for _ in windows))
            continue
        cells = ""
        for w in windows:
            dt = r.windows.get(w, float("nan"))
            if dt != dt:  # NaN
                cells += f"{'n/d':>18}"
            else:
                verdict = ""
                if dt > 3.0:
                    verdict = " LENTO"
                elif dt > 1.0:
                    verdict = " lento"
                cells += f"{dt:>13.2f} s{verdict}"
        print(f"{r.name:<18}{cells}")
    print("-" * 92)
    print("<= 1 s: finalizzazione quasi immediata | 1-3 s: accettabile | > 3 s: troppo lento per una lezione")


def print_details(results: list[ModelResult]) -> None:
    """Dettaglio per segmento dei modelli che hanno girato."""
    for r in results:
        if r.status != "ok":
            continue
        print()
        print("=" * 92)
        print(f"DETTAGLIO — {r.name}")
        print("=" * 92)
        print(f"  {'#':>2}  {'audio s':>8}  {'decode s':>9}  {'RTF':>6}  testo")
        print("  " + "-" * 88)
        for i, (dur, dt, text) in enumerate(r.segments):
            rtf = dt / dur if dur else 0.0
            preview = text[:64] + ("..." if len(text) > 64 else "")
            print(f"  {i:>2}  {dur:>7.2f}s  {dt:>8.2f}s  {rtf:>6.3f}  {preview}")
        print(f"  audio totale {r.total_audio_s:.2f} s, decode totale {r.total_decode_s:.2f} s")
        print(f"  picco RSS {r.rss_mb:.0f} MiB, CPU {r.cpu_pct:.1f} %, caricamento {r.load_s:.2f} s")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark delle prestazioni realtime della pipeline ASR."
    )
    parser.add_argument(
        "--models",
        nargs="+",
        default=list(DEFAULT_MODELS),
        help="modelli da provare (default: small medium large-v3-turbo)",
    )
    parser.add_argument("--audio", default=DEFAULT_AUDIO, help="WAV di prova")
    parser.add_argument(
        "--model-dir", default=str(REPO_ROOT / "models"), help="cache dei modelli"
    )
    parser.add_argument(
        "--windows",
        nargs="+",
        type=float,
        default=list(DEFAULT_WINDOWS),
        help="finestre (secondi) per la latenza di decodifica (default: 10 30)",
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help="scarica i modelli mancanti (richiede la rete; default: OFF)",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=3,
        help="ripetizioni della decodifica; i numeri riportati sono la "
        "mediana (default: 3; usa 1 per la misura singola)",
    )
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--compute-type", default="int8")
    args = parser.parse_args(argv)

    model_dir = Path(args.model_dir).expanduser()
    windows = tuple(args.windows)

    audio, rate = ensure_audio(Path(args.audio).expanduser())
    print("=" * 92)
    print("BENCHMARK — LECTURE TRANSCRIBER")
    print("=" * 92)
    print(f"audio        : {args.audio} ({len(audio) / rate:.2f} s @ {rate} Hz mono)")
    print(f"cache modelli: {model_dir}")
    print(f"modelli      : {', '.join(args.models)}")
    print(f"finestre     : {', '.join(f'{w:g} s' for w in windows)}")
    print(f"download     : {'ON' if args.download else 'OFF'}")
    print(
        f"ripetizioni  : {args.repeat} "
        f"({'mediana' if args.repeat > 1 else 'misura singola'})"
    )
    print(f"CPU          : {os.cpu_count()} thread(i)")

    results: list[ModelResult] = []
    for name in args.models:
        print("-" * 92)
        print(f"[bench] {name}")
        local = model_is_local(name, model_dir)
        if not local:
            if not args.download:
                results.append(
                    ModelResult(
                        name=name,
                        status="skipped",
                        message=f"non presente in {model_dir} (usa --download)",
                    )
                )
                print(f"      SKIP: '{name}' non e' presente in locale")
                continue
            try:
                download_model(name, model_dir)
            except Exception as exc:
                results.append(
                    ModelResult(name=name, status="failed", message=f"download: {exc}")
                )
                print(f"      download fallito: {exc}")
                continue
        try:
            res = bench_model(
                name,
                audio,
                rate,
                model_dir,
                windows,
                device=args.device,
                compute_type=args.compute_type,
                repeat=args.repeat,
            )
        except Exception as exc:
            res = ModelResult(name=name, status="failed", message=f"{type(exc).__name__}: {exc}")
        results.append(res)
        if res.status == "ok":
            print(
                f"      caricamento {res.load_s:.2f} s, "
                f"RTF {res.rtf:.3f}, {res.per_10s:.2f} s per 10 s di audio, "
                f"RSS {res.rss_mb:.0f} MiB"
            )
            if len(res.runs) > 1:
                all_rtfs = ", ".join(f"{r[0]:.3f}" for r in res.runs)
                print(f"      RTF delle ripetizioni: {all_rtfs} (mediana usata)")
            for w in windows:
                dt = res.windows.get(w, float("nan"))
                if dt == dt:
                    print(f"      latenza finestra {w:g} s: {dt:.2f} s")
        else:
            print(f"      {res.status.upper()}: {res.message}")

    print_table(results)
    print_windows(results, windows)
    print_details(results)

    print()
    print("=" * 92)
    ok = [r for r in results if r.status == "ok"]
    if not ok:
        print("NESSUN MODELLO BENCHMATTO: controlla la cache e la rete (--download).")
        return 1
    best = min(ok, key=lambda r: r.rtf)
    print(f"Modello piu' rapido: {best.name} (RTF {best.rtf:.3f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
