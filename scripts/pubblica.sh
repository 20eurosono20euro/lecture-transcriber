#!/usr/bin/env bash
# Pubblica il progetto su GitHub.
#
# Lo script NON chiede credenziali e NON le salva: usa la chiave SSH che
# hai gia' (~/.ssh/id_ed25519) oppure il token che gli passi tu. Se la
# chiave non e' registrata su GitHub, te lo dice e si ferma — pubblicare
# per sbaglio con un'identita' che non e' la tua espone un repository
# col nome di un altro.
#
#   ./scripts/pubblica.sh            # verifica e pubblica
#   ./scripts/pubblica.sh --verifica # solo la verifica, non pubblica
#
# Prerequisiti, una volta sola:
#   1. crea il repository su https://github.com/new  (nome: lecture-transcriber)
#   2. aggiungi la chiave pubblica su https://github.com/settings/keys
#      (il comando sotto te la stampa)

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE="git@github.com:20eurosono20euro/lecture-transcriber.git"
ONLY_CHECK=0
[[ "${1:-}" == "--verifica" ]] && ONLY_CHECK=1

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[avviso]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[errore]\033[0m %s\n' "$*" >&2; exit 1; }

cd "$HERE"

# --- 1. la chiave SSH e' registrata? ---------------------------------------
# Si prova la connessione, non si presume: "Permission denied" qui significa
# che la chiave esiste ma GitHub non la conosce ancora, ed e' l'unico modo
# per saperlo senza aprire il browser.
log "Verifico l'accesso a GitHub con la chiave SSH"
SSH_OUT="$(ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 \
           -T git@github.com 2>&1 || true)"
if printf '%s' "$SSH_OUT" | grep -q "successfully authenticated"; then
    log "  autenticato: $(printf '%s' "$SSH_OUT" | head -1)"
elif printf '%s' "$SSH_OUT" | grep -qi "permission denied"; then
    cat >&2 <<EOF

[errore] La chiave SSH non e' registrata su GitHub.

Fai queste due cose, una volta sola:

  1. aggiungi questa chiave su https://github.com/settings/keys
     (titolo: $(hostname))

$(cat ~/.ssh/id_ed25519.pub)

  2. crea il repository vuoto su https://github.com/new
     nome: lecture-transcriber   (pubblico o privato, come preferisci)

Poi rilancia: ./scripts/pubblica.sh

EOF
    die "chiave non registrata"
else
    warn "non ho potuto verificare l'accesso ($SSH_OUT)"
    warn "procedo comunque: se il push fallisce, la causa e' quasi sempre la chiave"
fi

if (( ONLY_CHECK )); then
    log "Solo verifica (--verifica): non pubblico."
    exit 0
fi

# --- 2. niente da pubblicare? ----------------------------------------------
if git diff --quiet HEAD 2>/dev/null; then
    :
else
    warn "ci sono modifiche non ancora committate:"
    git status --short | head -10
    die "fai il commit prima di pubblicare"
fi

# --- 3. push ---------------------------------------------------------------
log "Pubblico su $REMOTE"
git push -u origin main 2>&1 | sed 's/^/    /'

log "Pubblicato."
cat <<EOF

Il progetto e' su https://github.com/20eurosono20euro/lecture-transcriber

Da qualsiasi PC con Fedora:

    git clone https://github.com/20eurosono20euro/lecture-transcriber.git
    cd lecture-transcriber
    ./scripts/install.sh

EOF
