#!/usr/bin/env bash
# Crea (o ripara) il virtualenv dell'utente con le librerie di
# riconoscimento vocale che Fedora non pacchettizza.
#
#   faster-whisper  -> motore Whisper (dipende da ctranslate2)
#   soxr            -> risampionatore dell'audio
#   weasyprint      -> export in PDF ("Salva lezione")
#
# Perche' un virtualenv e non il pip di sistema:
#   * nessun privilegio di root (requisito del §8);
#   * `pip install --user` su Fedora 44 e' bloccato da PEP 668
#     ("externally-managed-environment"), quindi non e' un'alternativa;
#   * l'ambiente resta dell'utente e si cancella con la sua home.
#
# Lo script e' idempotente: se il virtualenv c'e' e ha gia' le librerie,
# non fa nulla. Non stampa nulla se va tutto bene.
set -euo pipefail

APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/lecture-transcriber"
VENV="$APP_DIR/venv"
PY="$VENV/bin/python"
MARKER="$VENV/.lecture-transcriber-ok"
# Requisiti bloccati: l'app funziona senza, ma non trascrive.
REQUIREMENTS="faster-whisper soxr"

# --- casi in cui non si deve toccare nulla --------------------------------
if [ -f "$MARKER" ] && [ -x "$PY" ]; then
    exit 0
fi

if ! command -v python3.14 >/dev/null 2>&1; then
    echo "lecture-transcriber: python3.14 non trovato nel PATH." >&2
    echo "  Installa Python 3.14 (pacchetto python3.14) e riprova." >&2
    exit 1
fi

mkdir -p "$APP_DIR"

# --- creazione del virtualenv --------------------------------------------
# --system-site-packages: GTK4, libadwaita, PyGObject, numpy, sounddevice,
# portaudio e huggingface-hub arrivano dai pacchetti di sistema (verificati
# come RPM), e duplicarli nel venv sarebbe un secondo Python con dentro
# mezzo sistema. Solo le due librerie mancanti vengono installate qui.
if [ ! -x "$PY" ]; then
    if ! python3.14 -m venv --system-site-packages "$VENV" >&2; then
        echo "lecture-transcriber: creazione del virtualenv fallita." >&2
        echo "  Servono i pacchetti python3-virtualenv e python3-pip." >&2
        exit 1
    fi
fi

# --- installazione delle librerie mancanti -------------------------------
# --no-build-isolation richiesto: ctranslate2 compila estensioni e isola il
# build di default, perdendo le librerie di sistema.
if ! "$PY" -m pip install --quiet --disable-pip-version-check \
        --no-build-isolation $REQUIREMENTS >&2; then
    echo "lecture-transcriber: installazione di $REQUIREMENTS fallita." >&2
    echo "  Controlla la connessione (i modelli e le librerie si scaricano" >&2
    echo "  una sola volta) e riavvia l'applicazione." >&2
    exit 1
fi

# WeasyPrint serve solo all'export in PDF. Se manca o non si installa
# (rete assente, librerie di sistema assenti) l'app deve aprire lo stesso:
# il tasto "Salva lezione" lo segnala con un messaggio chiaro invece di
# impedire l'avvio. Percio' l'errore qui non esce con exit 1.
if ! "$PY" -c "import weasyprint" >/dev/null 2>&1; then
    "$PY" -m pip install --quiet --disable-pip-version-check \
            --no-build-isolation weasyprint >&2 \
        || echo "lecture-transcriber: WeasyPrint non installato," \
               "l'export in PDF non sara' disponibile." >&2
fi

# --- verifica finale -----------------------------------------------------
if ! "$PY" -c "import faster_whisper, soxr" >&2; then
    echo "lecture-transcriber: le librerie sono installate ma non importabili." >&2
    exit 1
fi

touch "$MARKER"
exit 0
