# Installazione, configurazione e disinstallazione su Fedora

Tutte le operazioni descritte qui sono state **provate su Fedora 44 / GNOME
50.6 / Wayland**. I comandi che richiedono `sudo` sono marcati come tali.

---

## 1. Requisiti

**Già presenti su Fedora 44** (verificato):

- GNOME Shell 50.6 con Wayland
- PipeWire 1.6.9 (`pipewire-pulseaudio`) + WirePlumber
- GTK4 4.22.5, libadwaita 1.9.4, PyGObject 3.56.3
- Python 3.14 (interprete di sistema)

**Da installare**:

```bash
sudo dnf install python3-gobject gtk4 libadwaita python3-numpy portaudio libsoxr
```

**Installate dallo script, non da `dnf`**: `faster-whisper`, `soxr` e
`weasyprint` non sono pacchettizzate in Fedora 44 (verificato:
`python3-weasyprint` non esiste nei repo). `install.sh` le mette nel
virtualenv; `weasyprint` serve solo all'export in PDF, quindi se la sua
installazione fallisce l'app si apre lo stesso e il tasto "Salva lezione"
lo segnala con un messaggio chiaro.

**Opzionali**:

| Pacchetto | Serve per |
|---|---|
| `ydotool` | inserimento del testo in Obsidian / app Electron |
| `wl-clipboard` | copia negli appunti e fallback di inserimento |
| `libreoffice-writer` | inserimento via API UNO |
| `espeak-ng` | generare audio di prova per i test |

---

## 2. Installazione per l'utente (consigliata, senza root)

```bash
cd ~/lecture-transcriber
./scripts/install.sh
```

Lo script:

1. sceglie `python3.14` (l'unico con `gi`, `uno` e `Atspi`);
2. crea `.venv` con `--system-site-packages`, così le librerie GTK di sistema
   restano visibili;
3. installa `numpy`, `sounddevice`, `soxr`, `faster-whisper`,
   `huggingface-hub`;
4. scarica il modello `large-v3-turbo` (1,6 GB, una volta sola) in
   `~/.local/share/lecture-transcriber/models`;
5. installa l'estensione GNOME in
   `~/.local/share/gnome-shell/extensions/` (senza root) e il launcher in
   `~/.local/bin`.

Per saltare l'estensione: `./scripts/install.sh --no-ext`.

### Avvio

Dopo `install.sh` l'app è nel **menu delle applicazioni** come
"Trascrizione Lezioni" e si apre con un tap. Da terminale, uno di questi
due — funzionano da **qualsiasi** cartella:

```bash
lecture-transcriber                                        # il launcher
~/lecture-transcriber/.venv/bin/python -m lecture_transcriber    # diretto
```

> Il launcher prova due interpreti, in ordine: il virtualenv del progetto
> (quello con `faster-whisper` e `soxr`) e il `python3` di sistema (quello
> con `gi`/GTK). Se nessuno dei due ha le librerie, l'app si apre lo
> stesso — cronologia, ricerca, esportazione funzionano — e dice a video
> cosa manca. Un `.pth` nel site-packages del virtualev rende il pacchetto
> importabile senza `PYTHONPATH`.

---

## 3. Installazione come pacchetto RPM

```bash
sudo dnf install rpm-build rpmdevtools
rpmbuild -ta packaging/lecture-transcriber.spec
sudo dnf install ~/rpmbuild/RPMS/noarch/lecture-transcriber-*.rpm
```

L'RPM installa l'app in `/usr/share/lecture-transcriber` e usa
l'interprete di sistema: `gi`, `uno` e `Atspi` funzionano senza sandbox.

**Perche' non Flatpak**: il permesso del microfono, la scrittura dei modelli
(1,6 GB) e soprattutto l'accesso al socket UNO di `soffice` sono fortemente
penalizzati dalla sandbox. Il RPM e' il formato naturale su Fedora e non
introduce limiti.

---

## 4. L'estensione GNOME (sempre in primo piano)

Su GNOME 50 Wayland **nessun client puo' chiedere** di restare sopra le
altre finestre: `xdg-shell` non ha quella richiesta e Mutter non implementa
`wlr-layer-shell`. L'unica soluzione e' un'estensione GNOME Shell.

L'UUID e' `lecture-transcriber@localhost` (deve combaciare con `uuid` in
`shell-extension/metadata.json`):

```bash
mkdir -p ~/.local/share/gnome-shell/extensions/lecture-transcriber@localhost
cp shell-extension/extension.js shell-extension/metadata.json \
   ~/.local/share/gnome-shell/extensions/lecture-transcriber@localhost/

# poi: esci e rientra dalla sessione (Wayland non supporta il reload)
gnome-extensions enable lecture-transcriber@localhost
gnome-extensions list --enabled
```

> `./scripts/install.sh` fa già tutto questo: la copia qui sopra serve solo
> se vuoi installarla a mano o senza l'estensione (`--no-ext`).

Se l'estensione non e' installata **l'app funziona lo stesso**: la finestra
semplicemente non resta sopra le altre, e si puo' usare la voce
"**Sempre in primo piano**" del menu di GNOME.

---

## 5. Inserimento del testo nelle applicazioni di scrittura

### LibreOffice Writer — funziona subito, senza privilegi

L'app usa l'API UNO. Al primo inserimento lancia `soffice` con un socket di
accettazione:

```bash
soffice --accept="socket,host=127.0.0.1,port=2002;urp;" &
```

Non e' necessario avviarlo a mano: `TextInserter` lo fa da solo e si
riconnette. Il testo viene inserito **ancorato al cursore**, senza riscrivere
il documento.

### Editor GTK (gnome-text-editor, gedit) — funziona subito

Via accessibilita' at-spi (`EditableText`). Il testo viene inserito alla fine
del contenuto del campo attivo.

### Obsidian / VS Code (Electron) — richiede una preparazione una tantum

Gli applicativi Electron espongono un albero di accessibilita' in sola
lettura: **at-spi non puo' scriverci**. L'unica via e' la simulazione di
tastiera via `uinput`:

```bash
sudo dnf install ydotool wl-clipboard
sudo sh -c 'echo "KERNEL==\"uinput\", GROUP=\"input\", MODE=\"0660\"" > /etc/udev/rules.d/90-uinput.rules'
sudo usermod -aG input "$USER"
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Poi **esci e rientra dalla sessione** (i gruppi si applicano al login).

> Nota: `wtype` **non funziona** su GNOME 50 — Mutter non espone i globali
> `wlr-virtual-keyboard` (verificato per ispezione del binario).

### Come funziona la sicurezza

L'inserimento e' **sempre** subordinato a due condizioni:

1. la **modalita' inserimento** e' attiva (il pulsante in alto a destra);
2. la **finestra attiva** e' quella registrata quando la modalita' e' stata
   abilitata.

Se cambi finestra, l'inserimento viene **saltato** con un avviso e il
segmento resta nella cronologia. Questo e' necessario perche' uinput e
clipboard scrivono globalmente sulla superficie in primo piano.

---

## 6. Configurazione

Le preferenze sono in `~/.config/lecture-transcriber/settings.json`:

```json
{
  "model_name": "large-v3-turbo",
  "compute_type": "int8",
  "language": "it",
  "input_device": 13,
  "opacity": 0.85,
  "font_size": 14,
  "auto_scroll": true,
  "insert_enabled": false,
  "insert_paragraphs": true,
  "record_audio": false
}
```

| Chiave | Effetto |
|---|---|
| `model_name` | `large-v3-turbo` (default), `medium` (latenza minima), `large-v3` (accuratezza massima) |
| `compute_type` | `int8` (default), `float16`, `int8_float16` |
| `input_device` | indice del dispositivo; `null` = sorgente predefinita |
| `opacity` | 0.2–1.0 (opacita' della finestra) |
| `auto_scroll` | se `false`, la cronologia non scorre da sola |
| `insert_paragraphs` | `true` = paragrafi separati, `false` = testo continuo |
| `record_audio` | **default `false`**: la registrazione audio e' opzionale |

**Il modello si scarica da solo** al primo avvio se manca; `install.sh` lo
scarica già durante l'installazione. Per anticiparlo a mano:

```bash
~/.local/share/lecture-transcriber/bin/fetch-model.sh    # se presente
```

oppure direttamente (il percorso viene da `config.MODEL_DIR`, che è l'unica
fonte di verità):

```bash
MODEL_DIR=~/.local/share/lecture-transcriber/models
mkdir -p "$MODEL_DIR"
HF_HOME="$MODEL_DIR" ~/lecture-transcriber/.venv/bin/python -c "
from faster_whisper import WhisperModel
WhisperModel('large-v3-turbo', device='cpu', compute_type='int8',
             download_root='$MODEL_DIR')"
```

> Il percorso **non** è dentro la cartella del progetto: metterlo lì
> significava che l'app non lo trovava (guarda in `MODEL_DIR`) e che
> cancellando il clone si perdeva il modello senza che l'app se ne
> accorgesse. È misurabile: 1,6 GB in due cartelle diverse.

---

## 7. Scorciatoie

Predefinite (rimappabili da `settings.json`):

| Azione | Chiave | Tasto |
|---|---|---|
| Avvia / mette in pausa | `toggle_listen` | `Ctrl+Super+Shift+A` |
| Attiva / disattiva l'inserimento | `toggle_insert` | `Ctrl+Super+Shift+I` |
| Modalita' semitrasparente | `toggle_transparent` | `Ctrl+Super+Shift+T` |
| Mostra / nascondi la finestra | `toggle_window` | `Ctrl+Super+Shift+V` |
| Crea un segnalibro | `bookmark` | `Ctrl+Super+Shift+B` |
| Torna al testo piu' recente | `jump_to_latest` | `Ctrl+Super+Shift+L` |

`toggle_listen` è un **interruttore**: con la sessione in corso mette in
pausa, da fermo la avvia. Il tasto nella finestra fa lo stesso e la sua
icona dice cosa farà il click (pausa mentre ascolta, play quando è in pausa).

In caso di conflitto con altre applicazioni rimappa le scorciatoie
modificando `settings.json`; se il `media-keys` non e' disponibile, l'app
resta usabile con le scorciatoie locali della finestra (`Ctrl+i`,
`Ctrl+m`, `Ctrl+b`, `Ctrl+l`, `Ctrl+f` per la ricerca).

Le scorciatoie globali passano dal `media-keys` di GSettings
(`org.gnome.settings-daemon.plugins.media-keys`), non dal portal
`org.freedesktop.portal.GlobalShortcuts`: su GNOME 50 il portal pretende un
app-id risolto da uno scope systemd e `BindShortcuts` apre un dialogo di
conferma che blocca, mentre il percorso GSettings funziona senza nessuno dei
due.

La registrazione avviene all'avvio dell'app e viene rimossa alla chiusura.
Mentre l'app gira, le scorciatoie sono visibili in **Impostazioni di sistema
-> Tastiera -> Scorciatoie personalizzate**.

**Non esiste ancora una rimappatura dalla UI dell'app**: per cambiare un
tasto si modifica la preferenza corrispondente in
`~/.config/lecture-transcriber/settings.json` e si riavvia l'app. Le chiavi
sono quelle della tabella qui sopra (`toggle_listen`, `toggle_insert`,
`toggle_transparent`, `toggle_window`, `bookmark`, `jump_to_latest`).

In caso di conflitto con altre applicazioni, cambia il tasto in
`settings.json`; se il `media-keys` non e' disponibile, l'app resta usabile
con le scorciatoie locali della finestra.

**Attenzione:** `gsd-media-keys` muove con SIGSEGV se trova un percorso non
valido fra le scorciatoie personalizzate di GNOME — e in quel caso smettono
di funzionare *tutte* le scorciatoie di GNOME, non solo quelle dell'app.
L'app valida ogni percorso prima di scriverlo, quindi non dovrebbe mai
succedere; se un giorno le scorciatoie di GNOME smettono di funzionare,
controllare con:

```
gsettings get org.gnome.settings-daemon.plugins.media-keys custom-keybindings
```

e rimuovere a mano qualsiasi voce che non sia un percorso che comincia con
`/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/`.

---

## 8. Dati, privacy e disinstallazione

| Cosa | Dove |
|---|---|
| Modelli | `~/.local/share/lecture-transcriber/models` (`LT_MODEL_DIR` per spostarli) |
| Trascrizioni | `~/.local/share/lecture-transcriber/sessions.db` (SQLite) |
| Preferenze | `~/.config/lecture-transcriber/settings.json` |
| Cache | `~/.cache/lecture-transcriber` |

Entrambe le cartelle sono spostabili con le variabili d'ambiente
`LT_DATA_DIR` e `LT_MODEL_DIR` (vedi `config.py`): servono a chi tiene i
dati su un disco diverso.

**Nessun dato lascia il computer.** La registrazione audio e' disattivata di
default e la trascrizione funziona senza conservare l'audio originale.

### Disinstallazione

```bash
# 2. rimuovi l'estensione GNOME
gnome-extensions disable lecture-transcriber@localhost
rm -rf ~/.local/share/gnome-shell/extensions/lecture-transcriber@localhost

# 3. rimuovi il launcher e la voce di menu
rm -f ~/.local/bin/lecture-transcriber ~/.local/share/applications/lecture-transcriber.desktop

# 4. rimuovi i dati (facoltativo)
rm -rf ~/.local/share/lecture-transcriber ~/.config/lecture-transcriber ~/.cache/lecture-transcriber

# 5. rimuovi il codice
rm -rf ~/lecture-transcriber
```

Se hai installato l'RPM: `sudo dnf remove lecture-transcriber`.

### Disinstallazione delle regole per ydotool (opzionale)

```bash
sudo rm -f /etc/udev/rules.d/90-uinput.rules
sudo udevadm control --reload-rules && sudo udevadm trigger
```

> **Non usare `usermod -G ""`**: quel comando *svuota* tutti i gruppi
> dell'utente (compresi `wheel`, `video`, `input` e su Fedora anche i
> gruppi di sessione), non li ricrea. Chi lo esegue per togliere `input`
> perde ogni altro gruppo e resta fuori dal gruppo che permette `sudo`.
> Per togliere il solo `input`: `sudo gpasswd -d "$USER" input`.

---

## 9. Diagnostica

```bash
# che cosa e' disponibile su questa macchina
~/lecture-transcriber/.venv/bin/python -c \
  "from lecture_transcriber.insertion import capabilities; print(capabilities())"
# atteso: {'libreoffice_uno': True, 'atspi': True, 'clipboard': True,
#           'ydotool': False, 'wtype': False}

# dispositivi di cattura
~/lecture-transcriber/.venv/bin/python -c \
  "from lecture_transcriber.audio import list_input_devices; [print(d) for d in list_input_devices()]"

# stato di PipeWire
wpctl status | grep -A3 Sources

# test end-to-end del motore ASR
~/lecture-transcriber/.venv/bin/python scripts/test_asr_italian.py
```

**Problemi frequenti**:

| Sintomo | Causa | Rimedio |
|---|---|---|
| `ModuleNotFoundError: No module named 'gi'` | stai usando `python3.13` | usa `.venv/bin/python` (3.14) |
| La finestra non resta sopra | manca l'estensione GNOME | installa `shell-extension/` |
| Nessun testo in Obsidian | manca `ydotool` + regola udev | vedi §5 |
| Testo vuoto in un'aula rumorosa | soglia VAD troppo alta | abbassa `vad_silence_ms` o parla piu' vicino |
| Il modello non si scarica | rete assente | scaricalo con `HF_HOME=...` a parte |
