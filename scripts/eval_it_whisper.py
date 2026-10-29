#!/usr/bin/env python3
"""Baseline: faster-whisper large-v3-turbo (int8, CPU) on the SAME sentences
used by eval_it_streaming.py, so the streaming Zipformer and the batch
Whisper can be compared on identical audio and identical references.

Also reports the batch-mode latency floor, which is the number that decides
whether a "real time" claim is honest: a 30 s window re-decoded every time
cannot be called streaming.

Usage: python eval_it_whisper.py [model_dir]
"""
import sys
import time
import wave

import numpy as np
from faster_whisper import WhisperModel

SENTENCES = [
    "Buongiorno a tutti.",
    "In questa lezione parliamo della struttura dei dati.",
    "Il professor Rossi ha spiegato che le reti convoluzionali sono utili per le immagini.",
    "La teoria della probabilita e la base di ogni modello statistico.",
    "Dobbiamo ricordare che il teorema di Bayes collega probabilita condizionali e marginali.",
]

# Il modello di default e' la cache di Hugging Face dell'utente: il
# percorso assoluto di *questa* macchina non compariva nel repository
# perche' uno script di valutazione con un path altrui non gira da
# nessun'altra parte. Si cerca nella cache standard, e se non c'e' si
# chiede il nome del modello (faster-whisper lo scarica da se').
def _default_model() -> str:
    import os
    from pathlib import Path

    cache = Path(
        os.environ.get(
            "HF_HOME", Path.home() / ".cache" / "huggingface"
        )
    )
    hits = sorted(cache.glob("hub/models--*/snapshots/*"), reverse=True)
    for hit in hits:
        if "whisper" in hit.name.lower() or "whisper" in str(hit.parent).lower():
            return str(hit)
    if hits:
        return str(hits[0])
    return "large-v3-turbo"  # nome simbolico: lo scarica faster-whisper


DEFAULT_MODEL = _default_model()


def normalize(text):
    out = []
    for ch in text.lower():
        out.append(ch if (ch.isalpha() or ch.isspace()) else " ")
    return " ".join("".join(out).split())


def levenshtein(a, b):
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def wer(ref, hyp):
    r, h = normalize(ref).split(), normalize(hyp).split()
    if not r:
        return 0.0 if not h else 1.0
    return levenshtein(r, h) / len(r)


def cer(ref, hyp):
    r, h = normalize(ref).replace(" ", ""), normalize(hyp).replace(" ", "")
    if not r:
        return 0.0 if not h else 1.0
    return levenshtein(r, h) / len(r)


def main():
    model_dir = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MODEL
    t_load = time.monotonic()
    model = WhisperModel(model_dir, device="cpu", compute_type="int8", cpu_threads=16)
    load_s = time.monotonic() - t_load
    print(f"model        : {model_dir.split('/')[-1]}")
    print(f"device       : cpu / int8 / 16 threads")
    print(f"model load   : {load_s:.1f} s")
    print()

    wers, cers, tot_a, tot_p = [], [], 0.0, 0.0
    print("=" * 78)
    for i, sentence in enumerate(SENTENCES):
        wav = f"/tmp/_v{i}.wav"  # same files the streaming eval synthesised
        with wave.open(wav) as w:
            sr = w.getframerate()
            n = w.getnframes()
            raw = w.readframes(n)
        dur = n / sr
        # Decode here: faster-whisper's own decode path uses a PyAV kwarg that
        # the installed PyAV version rejects.
        pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

        t0 = time.monotonic()
        segments, info = model.transcribe(
            pcm, language="it", beam_size=1, without_timestamps=True,
        )
        hyp = " ".join(s.text for s in segments).strip()
        elapsed = time.monotonic() - t0

        w, c = wer(sentence, hyp), cer(sentence, hyp)
        wers.append(w)
        cers.append(c)
        tot_a += dur
        tot_p += elapsed
        print(f"[{i + 1}] REF: {sentence}")
        print(f"    HYP: {hyp}")
        print(f"    WER {w * 100:5.1f}%   CER {c * 100:5.1f}%   "
              f"audio {dur:5.2f}s  proc {elapsed:6.2f}s  RTF {elapsed / dur:.3f}")

    n = len(SENTENCES)
    print()
    print("=" * 78)
    print(f" utterances      : {n}")
    print(f" mean WER        : {sum(wers) / n * 100:.1f}%")
    print(f" mean CER        : {sum(cers) / n * 100:.1f}%")
    print(f" total audio     : {tot_a:.2f} s")
    print(f" total processing: {tot_p:.2f} s")
    print(f" aggregate RTF   : {tot_p / tot_a:.4f}")
    print("=" * 78)


if __name__ == "__main__":
    main()
