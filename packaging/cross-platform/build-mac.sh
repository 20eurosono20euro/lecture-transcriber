#!/usr/bin/env bash
# Costruisce il binario di Lecture Transcriber per macOS.
#
# Prodotto: un eseguibile headless "lecture-transcriber" che trascrive
# file audio e parla via riga di comando (nessuna GUI GTK: quella resta
# Linux-only). Utile per l'uso da terminale e per essere incapsulato in
# una Automator/Shortcuts se si vuole un avvio "a icona".
#
#   su un Mac con Python 3.11+ e Homebrew:
#     bash build-mac.sh
#
# Dipendenze di sistema (una volta sola):
#     xcode-select --install
#     brew install python@3.11
#
# Processo:

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
VENV="${ROOT}/.venv-mac"

echo "==> Creo il virtualenv mac ($VENV)"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip wheel setuptools

echo "==> Installo le dipendenze runtime"
# Core portabile: PortAudio fornisce WASAPI/CoreAudio dietro sounddevice.
"$VENV/bin/pip" install \
    numpy sounddevice soxr faster-whisper huggingface-hub pyinstaller

echo "==> Build PyInstaller (onefile, headless)"
cd "$ROOT"
"$VENV/bin/pyinstaller" \
    --noconfirm --clean \
    --specpath packaging/cross-platform \
    packaging/cross-platform/lecture-transcriber.spec

BIN="$ROOT/dist/lecture-transcriber"
echo ""
echo "==> Fatto: $BIN"
echo ""
echo "L'architettura del binario e' quella del Python che ha buildato:"
file "$BIN"
echo ""
echo "Per un binario universale (arm64+x86_64) builda due volte (su un Mac"
echo "arm e su uno x86_64 che condividono il codice) o usa lipo:"
echo "  lipo -create dist-arm/lecture-transcriber dist-x86/lecture-transcriber \\"
echo "        -output lecture-transcriber-universal"
echo ""
echo "Prova:"
echo "  $BIN --help"
echo "  $BIN mio.wav --course 'Analisi' --export out.md"
