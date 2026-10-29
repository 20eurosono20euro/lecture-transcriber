#!/usr/bin/env bash
# Installa l'estensione GNOME Shell per Lecture Transcriber.
set -euo pipefail

UUID="lecture-transcriber@localhost"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${HOME}/.local/share/gnome-shell/extensions/${UUID}"

echo "==> Installazione in ${DEST}"
mkdir -p "${DEST}"
cp "${SRC}/extension.js" "${SRC}/metadata.json" "${DEST}/"

if command -v gnome-extensions >/dev/null 2>&1; then
    echo "==> Abilitazione dell'estensione"
    gnome-extensions enable "${UUID}" 2>/dev/null || \
        echo "    (non abilitata automaticamente: serve un riavvio della sessione)"
else
    echo "    'gnome-extensions' non trovato: abilita l'estensione a mano dopo il riavvio"
fi

cat <<'EOF'

Fatto. Perche' l'estensione abbia effetto serve riavviare la sessione:

  * X11:    Alt+F2 -> r -> Invio
  * Wayland: logout e login completi (non si puo' riavviare la shell)

Poi verifica con:

  gnome-extensions list --enabled | grep lecture-transcriber
  gdbus call --session --dest org.gnome.Shell.Extensions.LectureTranscriber \
    --object-path /org/gnome/Shell/Extensions/LectureTranscriber \
    --method org.gnome.Shell.Extensions.LectureTranscriber.GetAlwaysOnTop

L'applicazione funziona anche senza l'estensione: il sempre-in-primo-piano
semplicemente non verra' attivato.
EOF
