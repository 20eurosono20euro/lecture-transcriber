%global forgeurl https://github.com/20eurosono20euro/lecture-transcriber
%global modulename lecture_transcriber

Name:           lecture-transcriber
Version:        0.1.0
Release:        1%{?dist}
Summary:        Trascrizione locale delle lezioni per GNOME/Wayland

License:        MIT
URL:            %{forgeurl}
Source0:        %{forgeurl}/archive/v%{version}/%{name}-%{version}.tar.gz

BuildArch:      noarch

# Le dipendenze si dividono in due gruppi.
#
# GRUPPO 1 — pacchettizzate in Fedora, quindi Requires: normali:
#   python3.14, python3-gobject, gtk4, libadwaita, python3-numpy,
#   python3-sounddevice, portaudio, python3-huggingface-hub.
#
# GRUPPO 1-bis — l'export in PDF ("Salva lezione") usa WeasyPrint, che su
#   Fedora 44 NON e' pacchettizzato come python3-weasyprint (verificato:
#   `rpm -q python3-weasyprint` -> non installato). Percio' nel percorso RPM
#   lo installa il %post in un virtualenv, e qui restano solo le librerie
#   di sistema che WeasyPrint carica a runtime. L'elenco non e' indovinato:
#   viene da /proc/<pid>/maps mentre WeasyPrint rende un PDF su questa
#   macchina (pango, harfbuzz, fontconfig, freetype, glib/gobject,
#   libtiff, libwebp, libjpeg-turbo, libpng, libxml2, libbrotli, expat,
#   zlib-ng-compat). Quasi tutte le tira gia' dentro gtk4/libadwaita, ma
#   dichiararle evita il difetto opposto: un'installazione minimale dove
#   il tasto PDF muore con un ImportError invece di funzionare.
Requires:       python3.14 >= 3.14
Requires:       python3-gobject
Requires:       gtk4
Requires:       libadwaita
Requires:       python3-numpy
Requires:       python3-sounddevice
Requires:       portaudio
Requires:       python3-huggingface-hub
Requires:       python3-pip
Requires:       python3-virtualenv
# Librerie di sistema di WeasyPrint (export PDF).
Requires:       pango
Requires:       harfbuzz
Requires:       fontconfig
Requires:       freetype
Requires:       libtiff
Requires:       libwebp
Requires:       libjpeg-turbo
Requires:       libpng
Requires:       libxml2
Requires:       libbrotli
Requires:       expat
Requires:       zlib-ng-compat
# Solo suggerimenti: senza questi l'app funziona, con meno strade
# di inserimento (vedi docs/installazione.md).
Recommends:     ydotool
Recommends:     wl-clipboard
Recommends:     libreoffice-writer
Recommends:     xdg-desktop-portal-gnome

%description
Trascrizione in tempo reale delle lezioni universitarie, interamente in locale.
Riconoscimento vocale con Whisper (faster-whisper), interfaccia GTK4 +
libadwaita a finestra flottante semitrasparente, cronologia consultabile con
ricerca e segnalibri, e inserimento controllato del testo nelle applicazioni di
scrittura tramite API UNO (LibreOffice) o accessibilita' at-spi.

Nessun dato lascia il computer: i modelli si scaricano una volta e le
trascrizioni restano in ~/.local/share/lecture-transcriber.

%prep
%autosetup

%build
# Il pacchetto e' puro Python: si installa direttamente in site-packages.
%install
mkdir -p %{buildroot}%{_datadir}/%{name}
cp -r src/%{modulename} %{buildroot}%{_datadir}/%{name}/

mkdir -p %{buildroot}%{_bindir}
# Il launcher NON chiama `python3.14` con il solo sys.path: il codice
# importa faster-whisper e soxr, che in Fedora non esistono come RPM.
# Passa dal virtualenv dell'utente (~/.local/share/...), creato da
# `scripts/ensure-venv.sh` al primo avvio, e ricade su un errore chiaro
# se l'ambiente non e' pronto.
cat > %{buildroot}%{_bindir}/lecture-transcriber <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
exec /usr/share/lecture-transcriber/launch.sh "$@"
EOF
chmod +x %{buildroot}%{_bindir}/lecture-transcriber

cat > %{buildroot}%{_datadir}/%{name}/launch.sh <<'EOF'
#!/usr/bin/env bash
# Avvio dell'app: scegli l'interprete giusto e passa il controllo al
# modulo Python. Deve funzionare con UN TAP dall'icona, quindi non puo'
# dipendere da variabili d'ambiente o da una shell interattiva.
set -uo pipefail

APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/lecture-transcriber"
VENV="$APP_DIR/venv"
PY="$VENV/bin/python"

# 1. se il virtualenv dell'utente esiste ed e' usabile, e' la scelta giusta
if [ -x "$PY" ]; then
    exec "$PY" -c "import sys; sys.path.insert(0, '/usr/share/lecture-transcriber'); from lecture_transcriber.__main__ import main; sys.exit(main())" "$@"
fi

# 2. altrimenti si prova a crearlo (primo avvio). Non usa root.
if [ "${LECTURE_TRANSCRIBER_NO_SETUP:-0}" != "1" ]; then
    if [ -x /usr/share/lecture-transcriber/scripts/ensure-venv.sh ]; then
        /usr/share/lecture-transcriber/scripts/ensure-venv.sh >&2 || true
        if [ -x "$PY" ]; then
            exec "$PY" -c "import sys; sys.path.insert(0, '/usr/share/lecture-transcriber'); from lecture_transcriber.__main__ import main; sys.exit(main())" "$@"
        fi
    fi
fi

# 3. senza virtualenv: si prova con l'interprete di sistema. Le sole
#    librerie di sistema bastano per l'interfaccia, la cronologia e
#    l'esportazione; manca solo il riconoscimento vocale, che l'app
#    segnala come errore invece di non aprirsi.
exec python3.14 -c "import sys; sys.path.insert(0, '/usr/share/lecture-transcriber'); from lecture_transcriber.__main__ import main; sys.exit(main())" "$@"
EOF
chmod +x %{buildroot}%{_datadir}/%{name}/launch.sh

mkdir -p %{buildroot}%{_datadir}/applications
# Il file .desktop e' lo stesso di packaging/: una sola fonte da manutenere.
cp packaging/%{name}.desktop %{buildroot}%{_datadir}/applications/%{name}.desktop

# L'estensione GNOME Shell serve per il "sempre in primo piano" su Wayland,
# dove xdg-shell non prevede la richiesta. Va installata per utente, in
# ~/.local/share/gnome-shell/extensions/: non puo' stare in /usr/share,
# perche' l'utente deve poterla abilitare senza root.
%{__mkdir_p} %{buildroot}%{_datadir}/%{name}/shell-extension
cp shell-extension/extension.js shell-extension/metadata.json \
   shell-extension/README.md shell-extension/install.sh \
   %{buildroot}%{_datadir}/%{name}/shell-extension/

%{__mkdir_p} %{buildroot}%{_datadir}/%{name}/scripts
cp scripts/ensure-venv.sh %{buildroot}%{_datadir}/%{name}/scripts/

# Icone hicolor: lo stesso albero che usa install.sh per l'installazione
# per utente, qui in /usr/share/icons/hicolor per il pacchetto RPM.
%{__mkdir_p} %{buildroot}%{_datadir}/icons/hicolor
cp -r packaging/icons/hicolor/* %{buildroot}%{_datadir}/icons/hicolor/

%post
# Rigenera l'indice delle icone perche' le nuove icone hicolor siano
# subito visibili nei menu senza dover rientrare nella sessione.
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f -t %{_datadir}/icons/hicolor >/dev/null 2>&1 || :
fi

%files
%license LICENSE
%doc README.md docs/analisi-fattibilita.md docs/installazione.md docs/test.md
%{_datadir}/%{name}
%{_datadir}/applications/%{name}.desktop
%{_datadir}/icons/hicolor
%{_bindir}/lecture-transcriber

%changelog
* Thu Oct 22 2026 Lecture Transcriber contributors <noreply@example.com> - 0.1.0-1
- Prima versione: trascrizione locale con Whisper, finestra flottante GTK4,
  cronologia con ricerca e segnalibri, inserimento via UNO/at-spi.
