#!/usr/bin/env python3
"""Evaluate the Italian streaming Zipformer (sherpa-onnx) with WER and CER.

The feasibility analysis claimed no streaming ASR supports Italian, so the
question that matters is not "does a model exist" but "how good is it, and is
it fast enough to transcribe a lecture in real time". This measures both.

Reference transcripts are the espeak-ng input strings, so the WER is measured
against exactly what was spoken.

Usage: python eval_it_streaming.py <model_dir> [threads]
"""
import sys
import time
import wave

import sherpa_onnx

SENTENCES = [
    "Buongiorno a tutti.",
    "In questa lezione parliamo della struttura dei dati.",
    "Il professor Rossi ha spiegato che le reti convoluzionali sono utili per le immagini.",
    "La teoria della probabilita e la base di ogni modello statistico.",
    "Dobbiamo ricordare che il teorema di Bayes collega probabilita condizionali e marginali.",
]


def synth(sentence, path):
    import subprocess

    subprocess.run(
        ["espeak-ng", "-v", "it", "-s", "145", "-w", path, sentence],
        check=True,
        capture_output=True,
    )


def to_16k_mono(src, dst):
    import subprocess

    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-ar", "16000", "-ac", "1", dst],
        check=True,
    )


def read_wav(path):
    import array

    with wave.open(path, "rb") as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        sr = w.getframerate()
        raw = w.readframes(w.getnframes())
    samples = array.array("h")
    samples.frombytes(raw)
    return [s / 32768.0 for s in samples], sr


def normalize(text):
    """Lowercase, strip punctuation, collapse whitespace."""
    out = []
    for ch in text.lower():
        if ch.isalpha() or ch.isspace():
            out.append(ch)
        else:
            out.append(" ")
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


def cer(ref, hyp):
    r, h = normalize(ref).replace(" ", ""), normalize(hyp).replace(" ", "")
    if not r:
        return 0.0 if not h else 1.0
    return levenshtein(r, h) / len(r)


def wer(ref, hyp):
    r, h = normalize(ref).split(), normalize(hyp).split()
    if not r:
        return 0.0 if not h else 1.0
    return levenshtein(r, h) / len(r)


def main():
    model_dir = sys.argv[1]
    threads = int(sys.argv[2]) if len(sys.argv) > 2 else 4

    recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
        tokens=model_dir + "/tokens.txt",
        encoder=model_dir + "/encoder.onnx",
        decoder=model_dir + "/decoder.onnx",
        joiner=model_dir + "/joiner.onnx",
        num_threads=threads,
        sample_rate=16000,
        feature_dim=80,
        decoding_method="greedy_search",
    )
    # NOTE: sherpa-onnx 1.13.8's built-in endpoint detection aborts the process
    # on this model ("GetFrames: 256 + 269 > 279" in features.cc). Segmentation
    # is therefore done by the CALLER (the app already runs its own RMS VAD),
    # and this script feeds one utterance per stream.

    print("=" * 78)
    print(f" Italian streaming Zipformer — WER/CER   (threads={threads})")
    print("=" * 78)

    all_wer, all_cer, total_audio, total_proc = [], [], 0.0, 0.0

    for i, sentence in enumerate(SENTENCES):
        raw, wav = f"/tmp/_it_eval_{i}_raw.wav", f"/tmp/_it_eval_{i}.wav"
        synth(sentence, raw)
        to_16k_mono(raw, wav)
        samples, sr = read_wav(wav)
        duration = len(samples) / sr

        stream = recognizer.create_stream()
        t0 = time.monotonic()
        # Feed in 0.4 s chunks, exactly as a live capture loop would.
        chunk = int(0.4 * sr)
        processed = 0
        while processed < len(samples):
            end = min(processed + chunk, len(samples))
            stream.accept_waveform(sr, samples[processed:end])
            processed = end
            while recognizer.is_ready(stream):
                recognizer.decode_stream(stream)
        # A transducer only commits the tail of an utterance once it hears
        # silence, so the stream must be closed with trailing silence.
        stream.accept_waveform(sr, [0.0] * int(2.0 * sr))
        while recognizer.is_ready(stream):
            recognizer.decode_stream(stream)
        hyp = recognizer.get_result(stream).strip()

        elapsed = time.monotonic() - t0
        w, c = wer(sentence, hyp), cer(sentence, hyp)
        all_wer.append(w)
        all_cer.append(c)
        total_audio += duration
        total_proc += elapsed

        print(f"\n[{i + 1}] REF: {sentence}")
        print(f"    HYP: {hyp}")
        print(f"    WER {w * 100:5.1f}%   CER {c * 100:5.1f}%   "
              f"audio {duration:5.2f}s  proc {elapsed:5.2f}s  RTF {elapsed / duration:.3f}")

    n = len(SENTENCES)
    print()
    print("=" * 78)
    print(f" utterances      : {n}")
    print(f" mean WER        : {sum(all_wer) / n * 100:.1f}%")
    print(f" mean CER        : {sum(all_cer) / n * 100:.1f}%")
    print(f" total audio     : {total_audio:.2f} s")
    print(f" total processing: {total_proc:.2f} s")
    print(f" aggregate RTF   : {total_proc / total_audio:.4f}")
    print("=" * 78)


if __name__ == "__main__":
    main()
