#!/usr/bin/env bash
# Installazione di lecture-transcriber per l'utente (senza root).
#
# Crea un virtualenv con python3.14, installa le dipendenze Python,
# installa il launcher e il file .desktop, e installa l'estensione GNOME
# Shell per il "sempre in primo piano".
#
#   ./scripts/install.sh            # installazione
#   ./scripts/install.sh --no-ext   # senza estensione GNOME

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$HERE/.venv"
# Deve combaciare con "uuid" in shell-extension/metadata.json.
EXT_UUID="lecture-transcriber@localhost"
EXT_DIR="$HOME/.local/share/gnome-shell/extensions/$EXT_UUID"
LAUNCHER="$HOME/.local/bin/lecture-transcriber"
DESKTOP_DIR="$HOME/.local/share/applications"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[avviso]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[errore]\033[0m %s\n' "$*" >&2; exit 1; }

WITH_EXT=1
[[ "${1:-}" == "--no-ext" ]] && WITH_EXT=0

# --- interprete -------------------------------------------------------------
# python3.14 e' l'unico con gi/uno/Atspi di sistema su Fedora 44.
PY=""
for cand in /usr/bin/python3.14 python3.14 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
        if "$cand" -c 'import gi' 2>/dev/null; then PY="$(command -v "$cand")"; break; fi
    fi
done
[[ -n "$PY" ]] || die "Serve python3.14 con PyGObject (dnf install python3-gobject gtk4 libadwaita)."

log "Interprete: $PY ($("$PY" --version))"

# --- dipendenze di sistema --------------------------------------------------
missing=()
rpm -q python3-gobject >/dev/null 2>&1 || missing+=(python3-gobject)
rpm -q gtk4          >/dev/null 2>&1 || missing+=(gtk4)
rpm -q libadwaita    >/dev/null 2>&1 || missing+=(libadwaita)
if (( ${#missing[@]} )); then
    warn "Mancano: ${missing[*]}"
    warn "Installali con: sudo dnf install ${missing[*]}"
fi

# --- virtualenv -------------------------------------------------------------
if [[ ! -x "$VENV/bin/python" ]]; then
    log "Creo il virtualenv in $VENV"
    "$PY" -m venv --system-site-packages "$VENV"
else
    log "Virtualenv gia' presente"
fi

log "Aggiorno pip"
"$VENV/bin/pip" install --upgrade pip >/dev/null

log "Installo le dipendenze Python"
"$VENV/bin/pip" install \
    "numpy>=2.0" "sounddevice>=0.5" "soxr>=0.4" \
    "faster-whisper>=1.2" "huggingface-hub"

# WeasyPrint e' opzionale: serve solo a "Salva lezione" (export PDF). Metterlo
# nel blocco fatale faceva fallire l'installazione **intera** — quindi anche
# la trascrizione — per una funzione accessoria, e in contraddizione con
# scripts/ensure-venv.sh, dove lo stesso pacchetto e' gia' best-effort. Qui si
# fa lo stesso: se non si installa, l'app funziona e il tasto lo dice.
log "Installo WeasyPrint (opzionale, per l'export PDF)"
if ! "$VENV/bin/pip" install "weasyprint>=60"; then
    warn "WeasyPrint non installato: l'export in PDF non sara' disponibile."
fi

log "Verifico l'ambiente"
"$VENV/bin/python" - <<'PYEOF'
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw
import numpy, sounddevice, soxr, faster_whisper
# WeasyPrint e' opzionale: se manca, si segnala ma non si fallisce (il
# blocco e' senza "set -e" effetto sul padre solo se l'exit e' 0).
try:
    import weasyprint
    pdf = weasyprint.__version__
except Exception:
    pdf = "assente (export PDF non disponibile)"
print(f"  GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()}"
      f"  Adw {Adw.MAJOR_VERSION}.{Adw.MINOR_VERSION}"
      f"  numpy {numpy.__version__}"
      f"  faster-whisper {faster_whisper.__version__}"
      f"  weasyprint {pdf}")
PYEOF

# --- launcher e file .desktop ------------------------------------------------
# Il launcher e' uno script che punta al virtualenv del progetto:
# l'installazione avviene per copia, quindi gli entry point dichiarati
# in pyproject.toml non verrebbero installati.
#
# Il launcher prova **due** interpreti, in ordine: il virtualenv del
# progetto (quello con faster-whisper e soxr) e il python3 di sistema
# (quello con gi/GTK). Se nessuno dei due ha le librerie, stampa come
# rimediare invece di morire con un errore oscuro: il requisito e' che
# l'app si apra con un tap, e "bad interpreter" non e' un messaggio per
# chi ha appena cliccato un'icona.
log "Installo il launcher in $LAUNCHER"
mkdir -p "$(dirname "$LAUNCHER")"
cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
# Launcher di Lecture Transcriber (generato da scripts/install.sh).
set -u
APP_DIR="$HERE"

for PY in "\${APP_DIR}/.venv/bin/python" "\${SYS_PY:-/usr/bin/python3}"; do
    if [ -x "\$PY" ] && "\$PY" -c 'import lecture_transcriber' 2>/dev/null; then
        export PYTHONPATH="\${APP_DIR}/src\${PYTHONPATH:+:\${PYTHONPATH}}"
        exec "\$PY" -m lecture_transcriber "\$@"
    fi
done

# Nessun interprete funzionante. Si prova comunque con il migliore
# disponibile: l'app si apre lo stesso (cronologia, ricerca,
# esportazione), e segnala a video cosa manca.
if [ -x "\${APP_DIR}/.venv/bin/python" ]; then
    export PYTHONPATH="\${APP_DIR}/src\${PYTHONPATH:+:\${PYTHONPATH}}"
    exec "\${APP_DIR}/.venv/bin/python" -m lecture_transcriber "\$@"
fi

echo "Lecture Transcriber: interprete non trovato." >&2
echo "  Esegui: \$APP_DIR/scripts/install.sh" >&2
exit 1
EOF
chmod +x "$LAUNCHER"

# Un file .pth nel site-packages del virtualenv: rende il pacchetto
# importabile da **qualsiasi** cartella, senza dover esportare
# PYTHONPATH a mano. Senza, `.venv/bin/python -m lecture_transcriber`
# lanciato da una cartella qualsiasi (o dal terminale dell'utente, che
# non ha la variabile) fallisce con "No module named
# lecture_transcriber" — misurato su un clone pulito: neanche dalla
# cartella del progetto funzionava, perche' il pacchetto sta in src/.
# E' il meccanismo standard, non un ripiego: e' cosi' che un
# `pip install -e` avrebbe fatto.
#
# ``site.getsitepackages()[0]`` NON va bene: la lista inizia con i
# percorsi di *sistema* (/usr/local/...) e su questa macchina il primo
# non esisteva — misurato, la scrittura del .pth falliva con "No such
# file or directory". ``sysconfig.get_path('purelib')`` risolve dentro
# il virtualenv, che e' quello che vogliamo.
log "Registro il pacchetto nel virtualenv"
SITE="$("$VENV/bin/python" -c 'import sysconfig; print(sysconfig.get_path("purelib"))')"
mkdir -p "$SITE"
echo "$HERE/src" > "$SITE/lecture-transcriber.pth"
"$VENV/bin/python" -c 'import lecture_transcriber' \
    || die "pacchetto non importabile dopo l'installazione"

log "Installo il file .desktop in $DESKTOP_DIR"
mkdir -p "$DESKTOP_DIR"
cp "$HERE/packaging/lecture-transcriber.desktop" \
   "$DESKTOP_DIR/lecture-transcriber.desktop"

# --- icone -------------------------------------------------------------------
# Le icone hicolor vanno in ~/.local/share/icons (niente root), con le
# varianti chiara (default) e dark (suffisso .dark) da hicolor-1.3.
# gtk-update-icon-cache rinnova l'indice: senza, le nuove icone restano
# invisibili finche' il cache non viene rigenerato da qualcos'altro.
log "Installo le icone in ~/.local/share/icons/hicolor"
ICON_BASE="$HOME/.local/share/icons/hicolor"
ICON_TREE="$HERE/packaging/icons/hicolor"
for dir in "$ICON_TREE"/*/apps; do
    size="$(basename "$(dirname "$dir")")"
    dest="$ICON_BASE/$size/apps"
    mkdir -p "$dest"
    cp "$dir"/*.png "$dest/" 2>/dev/null || true
done
mkdir -p "$ICON_BASE/scalable/apps"
cp "$ICON_TREE/scalable/apps"/*.svg "$ICON_BASE/scalable/apps/" 2>/dev/null || true
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f -t "$ICON_BASE" >/dev/null 2>&1 || true
fi

# --- estensione GNOME --------------------------------------------------------
if (( WITH_EXT )); then
    if [[ -d "$HERE/shell-extension" ]]; then
        log "Installo l'estensione GNOME in $EXT_DIR"
        mkdir -p "$EXT_DIR"
        cp "$HERE/shell-extension/extension.js" "$HERE/shell-extension/metadata.json" "$EXT_DIR/"
        if command -v gnome-extensions >/dev/null 2>&1; then
            gnome-extensions enable "$EXT_UUID" 2>/dev/null || true
        fi
        log "Estensione installata. Per attivarla serve un riavvio della sessione:"
        echo "    * X11:    Alt+F2 -> r -> Invio"
        echo "    * Wayland: logout e login completi"
        echo "    poi: gnome-extensions enable $EXT_UUID"
    else
        warn "shell-extension/ non presente: salto l'installazione dell'estensione"
    fi
else
    log "Saltata l'installazione dell'estensione GNOME (--no-ext)"
fi

# --- modello -----------------------------------------------------------------
# Il modello va nella **stessa** cartella in cui l'app lo cerca
# (``config.MODEL_DIR``, cioe' ``~/.local/share/lecture-transcriber/models``),
# non dentro il progetto: scaricandolo in ``$HERE/models`` finiva in un
# posto che l'app non guarda, occupando il doppio dello spazio — misurato
# su questa macchina, 1,6 GB in due cartelle diverse. E se l'utente
# cancellava il clone perdeva il modello senza che l'app se ne accorgesse.
# Si lascia che sia ``config`` a dire dove: e' l'unica fonte di verita'.
log "Scarico il modello Whisper (large-v3-turbo, ~1,6 GB) se non presente"
# Il percorso deve essere **assoluto**: ``download_root="models"`` e'
# relativo alla cartella da cui parte lo script, quindi finiva dentro il
# clone — misurato, con una HOME vergine l'app non trovava il modello
# (``MODEL_DIR`` vuoto) mentre 1,6 GB erano nel progetto. ``config``
# resta l'unica fonte di verita' su dove l'app guarda.
MODEL_DIR_OUT="$("$VENV/bin/python" -c 'from lecture_transcriber import config; print(config.MODEL_DIR)')"
log "  in $MODEL_DIR_OUT"
HF_HOME="$MODEL_DIR_OUT" "$VENV/bin/python" - "$MODEL_DIR_OUT" <<'PYEOF'
import sys
from faster_whisper import WhisperModel

root = sys.argv[1]
WhisperModel(
    "large-v3-turbo",
    device="cpu",
    compute_type="int8",
    download_root=root,
)
print("  modello pronto")
PYEOF
[ -e "$MODEL_DIR_OUT" ] || die "il modello non e' stato scaricato in $MODEL_DIR_OUT"

cat <<EOF

$(log "Installazione completata")

Per avviare:
    lecture-transcriber
(oppure direttamente: $VENV/bin/python -m lecture_transcriber)

Note:
  * Le scorciatoie globali vengono registrate nel "media-keys" di GNOME
    all'avvio dell'app e rimosse alla chiusura. Finche' l'app gira, le
    trovi in Impostazioni di sistema -> Tastiera -> Scorciatoie
    personalizzate. Non compare alcun dialogo di conferma.
  * L'inserimento del testo in LibreOffice Writer usa l'API UNO (nessun
    privilegio). Negli editor Electron (Obsidian, Code) serve ydotool:
        sudo dnf install ydotool wl-clipboard
        sudo sh -c 'echo "KERNEL==\"uinput\", GROUP=\"input\", MODE=\"0660\"" > /etc/udev/rules.d/90-uinput.rules'
        sudo usermod -aG input \$USER
    poi esci e rientra dalla sessione.
  * Il click-through e il blur di sfondo non sono disponibili su GNOME 50
    Wayland: l'opacita' regolabile e' l'alternativa reale.
EOF
