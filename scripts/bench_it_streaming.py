#!/usr/bin/env python3
"""Measure the Italian streaming Zipformer (sherpa-onnx) on a real audio file.

This exists because the feasibility claim "no streaming ASR model supports
Italian" turned out to be FALSE: a community Italian streaming Zipformer2
exists (Kroko-IT, repackaged into the sherpa-onnx layout). The question that
matters for the project is not "does it exist" but "is it fast enough on
16 CPU cores to transcribe a lecture in real time".

Measures:
  - RTF (real-time factor) = processing time / audio duration
  - time-to-first-partial and time-to-final segment
  - the transcript itself, so the Italian tokenization can be eyeballed

Usage: python bench_it_streaming.py <model_dir> <wav> [num_threads]
"""
import sys
import time
import wave

import sherpa_onnx


def read_wav(path):
    with wave.open(path, "rb") as w:
        assert w.getnchannels() == 1, "expect mono"
        assert w.getsampwidth() == 2, "expect 16-bit PCM"
        assert w.getframerate() == 16000, "expect 16 kHz"
        raw = w.readframes(w.getnframes())
        sr = w.getframerate()
    # sherpa-onnx wants float samples in [-1, 1]
    import array

    samples = array.array("h")
    samples.frombytes(raw)
    return [s / 32768.0 for s in samples], sr


def main():
    model_dir = sys.argv[1]
    wav_path = sys.argv[2]
    threads = int(sys.argv[3]) if len(sys.argv) > 3 else 4

    samples, sr = read_wav(wav_path)
    duration = len(samples) / sr
    print(f"audio        : {wav_path}")
    print(f"duration     : {duration:.2f} s @ {sr} Hz mono")
    print(f"threads      : {threads}")
    print(f"model dir    : {model_dir}")
    print()

    recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
        tokens=model_dir + "/tokens.txt",
        encoder=model_dir + "/encoder.onnx",
        decoder=model_dir + "/decoder.onnx",
        joiner=model_dir + "/joiner.onnx",
        num_threads=threads,
        sample_rate=sr,
        feature_dim=80,
        decoding_method="greedy_search",
        # Endpoint on silence so segments are committed as the speaker pauses.
        enable_endpoint_detection=True,
        rule1_min_trailing_silence=0.8,   # trailing silence (s) that ends an utterance
        rule2_min_trailing_silence=0.4,   # mid-utterance silence (s) that splits a segment
        rule3_min_utterance_length=20.0,  # max utterance length (s)
    )

    stream = recognizer.create_stream()
    t0 = time.monotonic()
    processed = 0
    first_partial_at = None
    first_final_at = None
    finals = []

    # Feed in 0.4 s chunks: this is what a live capture loop does.
    chunk = int(0.4 * sr)
    while processed < len(samples):
        end = min(processed + chunk, len(samples))
        stream.accept_waveform(sr, samples[processed:end])
        processed = end

        while recognizer.is_ready(stream):
            recognizer.decode_stream(stream)

        text = recognizer.get_result(stream)
        if text and first_partial_at is None:
            first_partial_at = time.monotonic() - t0
            print(f"first partial: {first_partial_at:.2f}s elapsed -> {text!r}")

        if recognizer.is_endpoint(stream):
            recognizer.decode_stream(stream)
            final = recognizer.get_result(stream)
            finals.append(final)
            if first_final_at is None:
                first_final_at = time.monotonic() - t0
            print(f"endpoint @{processed / sr:5.2f}s audio -> {final!r}")
            recognizer.reset(stream)

    # Flush the tail with trailing silence: a transducer only commits the tail
    # of an utterance once it sees silence, so a short flush truncates it.
    stream.accept_waveform(sr, [0.0] * int(1.5 * sr))
    while recognizer.is_ready(stream):
        recognizer.decode_stream(stream)
    tail = recognizer.get_result(stream)
    if tail:
        finals.append(tail)
        print(f"tail         -> {tail!r}")

    elapsed = time.monotonic() - t0
    rtf = elapsed / duration

    print()
    print("=" * 62)
    print(f"processing   : {elapsed:.2f} s for {duration:.2f} s audio")
    print(f"RTF          : {rtf:.3f}   ({'faster than real time' if rtf < 1 else 'SLOWER than real time'})")
    if first_partial_at is not None:
        print(f"time-to-first-partial: {first_partial_at:.2f}s")
    if first_final_at is not None:
        print(f"time-to-first-final  : {first_final_at:.2f}s")
    print()
    print("transcript   :", " ".join(finals).strip())
    print("=" * 62)


if __name__ == "__main__":
    main()
