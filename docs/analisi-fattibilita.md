# Analisi di fattibilità — Lecture Transcriber

Analisi condotta **su questa macchina**, non da documentazione generica.
Ambiente misurato:

| Componente | Valore misurato |
|---|---|
| Distribuzione | Fedora 44 (fc44) |
| Desktop | GNOME Shell 50.6, mutter 50.6, sessione **Wayland** |
| GPU | AMD Radeon RX 9070 (RDNA4, Navi 48) + Radeon 610M (RDNA2) |
| CPU / RAM | 16 core, 14 GiB totali (7,8 GiB liberi) |
| Audio | PipeWire 1.6.9 + pipewire-pulse 1.6.9 (pacchetto `pipewire-pulseaudio`) + WirePlumber 0.5.18 |
| Toolchain | Python 3.14.8 (predefinito), pip 26.0.1, cargo 1.99, gcc 16.2 |
| Toolkit | GTK4 4.22.5, libadwaita 1.9.4, PyGObject 3.56.3 |

---

## 1. Il quadro reale: cosa Wayland permette e cosa no

Il punto critico del progetto e' **GNOME 50 su Wayland**. Molte funzionalita'
richieste dal brief sono state pensate per X11, dove il client aveva poteri
che Wayland ha deliberatamente tolto. Le verifiche qui sotto sono state fatte
per ispezione del binario e test dal vivo, non per documentazione.

### 1.1 Finestra sempre in primo piano — **richiede un'estensione GNOME Shell**

`xdg-shell` non ha nessuna richiesta `set_always_on_top` e nessuno stato
`always_on_top`: **un client Wayland non puo' chiedere di restare sopra**.
Verificato sul binario:

- `libmutter-18.so.0.0.0` non contiene i globali `wlr_virtual_keyboard` /
  `wlr_virtual_keyboard_manager` (0 occorrenze);
- non contiene `zwlr_layer_surface_v1` (0 occorrenze) → **`gtk4-layer-shell`
  non funziona su GNOME** ( Mutter non implementa wlr-layer-shell, issue
  GNOME/mutter#1984 aperta da anni; quel protocollo e' per i compositor
  wlroots);
- GTK4 **non ha** `set_keep_above` (confermato a runtime:
  `hasattr(Gtk.Window(), 'set_keep_above')` → `False`);
- GTK3 ha `set_keep_above`/`set_keep_below`, ma Mutter **ignora** quegli hint
  X11 su Wayland.

**Unica strada affidabile:** a livello di compositor — la voce "Sempre in primo
piano" del menu di GNOME, oppure una **piccola estensione GNOME Shell** che
usa le API `Meta.Window` / `Meta.WindowActor`. Il progetto include
`shell-extension/` (installazione senza root in
`~/.local/share/gnome-shell/extensions/`) e l'app la invoca via D-Bus,
**degradando senza problemi** se l'estensione non e' installata.

### 1.2 Trasparenza — **funziona**

`Gtk.Window.set_opacity()` e' disponibile e funzionante su GNOME 50
(confermato a runtime su gtk4 4.22.5): finestra semitrasparente client-side
mediante alpha su visual RGBA. Il **blur di sfondo** invece **non e'
disponibile**: `ext-background-effect-v1` e' stato implementato in Mutter solo
dalla versione 51 (MR #5071, merged 2026-07-03); il binario di Mutter 50.6
non contiene quel protocollo (0 occorrenze). L'opacita' regolabile e' quindi
l'unica realistica, ed e' sufficiente per l'uso richiesto.

### 1.3 Click-through (cliccabilita' trasparente) — **impossibile**

Mutter 50 non ha un protocollo di input-shape: **non esiste il click-through
nativo**. L'unica approssimazione e' una finestra Xwayland con X11 input
shape, che richiede che XWayland possieda la finestra ed e' fragile (e
significa far girare l'app sotto X11 solo per quello). Realisticamente: si
offre l'opacita' bassa + una scorciatoia per riattivare l'interazione, che e'
esattamente la via realistica che il brief chiedeva di proporre.

### 1.4 Scorciatoie globali — **funzionano, senza estensione**

Il portal `org.freedesktop.portal.GlobalShortcuts` **e' attivo** su questa
macchina: il provider `/usr/libexec/gnome-control-center-global-shortcuts-provider`
e' installato e `xdg-desktop-portal-gnome` 50.0 registra
`org.freedesktop.impl.portal.GlobalShortcuts/GlobalShortcutsSession`
(corrispondente alla MR #208, merged 2025-02-26). Sono supportate
create/bind/list/activated.

Un limite: la **UI di rebind del portal** (`ConfigureShortcuts`) e' arrivata
solo con GNOME 51 (MR #264, merged 2026-07-03). Su GNOME 50 l'app deve
fornire **propria UI di configurazione** delle scorciatoie.

**Ma il portal non e' il percorso scelto**, perche' in prova dal vivo si
sono rivelati tre ostacoli che non si vedono dalla documentazione:

1. **Pretende un app-id.** `CreateSession` risolve l'app-id dal nome dello
   scope systemd del chiamante (`app-<app-id>-<rand>.scope`). Un'applicazione
   lanciata da un terminale non ha quello scope, e il portal risponde
   `org.freedesktop.portal.Error.NotAllowed: An app id is required (36)`.
   Il trucco e' lanciare l'app dentro `systemd-run --user --scope`, con un
   `.desktop` installato in `~/.local/share/applications`: funziona, ma
   rende l'avvio dell'app dipendente da systemd.
2. **`BindShortcuts` apre un dialogo di conferma.** Il provider GNOME
   mostra la finestra "Add Keyboard Shortcuts" di gnome-control-center e
   **aspetta che l'utente confermi**: la risposta del portal non arriva
   finche' il dialogo non viene chiuso. Il bind "riesce" a livello di
   protocollo, ma la scorciatoia viene salvata in GSettings solo dopo la
   conferma. Verificato dal vivo con at-spi.
3. **La sessione puo' fare un solo bind.** Un secondo `BindShortcuts` sulla
   stessa sessione fallisce con "Session already has bound shortcuts".

**Il percorso scelto e' invece GSettings**, che su GNOME 50 funziona senza
nessuno dei tre ostacoli:

- schema `org.gnome.settings-daemon.plugins.media-keys`, chiave
  `custom-keybindings` (tipo `as`);
- schema child `org.gnome.settings-daemon.plugins.media-keys.custom-keybinding`
  con le chiavi `name`, `command`, `binding`, `enable-in-lockscreen`;
- percorso `/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/<id>/`;
- `gnome-settings-daemon` (il plugin `gsd-media-keys`) registra l'acceleratore
  e alla pressione **esegue il comando** scritto nella chiave `command`.

Il comando che l'app scrive e' una chiamata D-Bus a se stessa:

```
gdbus call --session --dest it.lecturetranscriber.App \
  --object-path /it/lecturetranscriber/App \
  --method it.lecturetranscriber.App.ActivateShortcut toggle_listen
```

Perche' funzioni serve che l'app esporti quell'interfaccia: `GApplication`
esporta le sue azioni sotto l'interfaccia standard `org.gtk.Actions`, il cui
parametro e' un `av` — non invocabile con la sintassi di `gdbus call`. Il
metodo `ActivateShortcut` e' quindi esportato a parte, sullo stesso object
path dell'applicazione, con `Gio.DBusConnection.register_object_with_closures2`.

**Catena completa, verificata dal vivo:**

```
gsd-media-keys (media-keys) -> esegue "command"
  -> gdbus call ... it.lecturetranscriber.App.ActivateShortcut <id>
    -> l'app attiva l'azione corrispondente
```

Il rovescio della medaglia: la scorciatoia e' visibile nelle Impostazioni di
sistema sotto "Tastiera -> Scorciatoie personalizzate", ed e' un'impostazione
di sistema, non una preferenza dell'app. Il codice la registra all'avvio e la
rimuove alla chiusura, senza mai toccare le scorciatoie personalizzate
dell'utente.

**Attenzione — un percorso non valido fa crashare GNOME.** `gsd-media-keys`
chiama `g_settings_new_with_path()` **senza validare il percorso**: con un
valore che non e' un percorso GSettings valido la funzione restituisce
`NULL` e il plugin va in dereferenza di puntatore nullo, morendo con
**SIGSEGV**. Il caso reale: `gsettings get` a volte stampa la lista come
`@as []` invece di `[]`, e un parser che non scarta quel prefisso finisce per
scrivere la stringa `@as []` fra i percorsi. Il crash e' stato riprodotto
(`gsd-media-keys` termina con rc=-11) e **porta via tutte le scorciatoie di
GNOME**, non solo le nostre: il servizio entra in "Start request repeated
too quickly" e resta giù. Per questo `shortcuts_gsettings.py` valida ogni
percorso prima di scriverlo e ripulisce quelli non validi gia' presenti
(`_is_valid_path`).

E' disponibile anche il portal `InputCapture` (Mutter dal 45, MR #2628),
utile per catturare il testo di altre applicazioni, non per iniettarlo.

### 1.5 Inserimento del testo — **percorso differenziato per applicazione**

Il punto piu' delicato del progetto. La verifica (scansione del binario +
prove dal vivo) dà:

| Metodo | Stato su GNOME 50 | Note |
|---|---|---|
| `wtype` (wlr-virtual-keyboard) | **ROTTO** | mutter non espone i globali (0 occorrenze) |
| `ydotool` (uinput) | **FUNZIONA**, richiede sudo una volta | indipendente dal compositor; unica via verso Electron |
| `at-spi` EditableText | **FUNZIONA nelle app GTK** | verificato dal vivo su `gnome-text-editor`: `insert_text` e rilettura con `get_string_at_offset` |
| LibreOffice UNO | **FUNZIONA, verificato dal vivo** | `insertString` ancorato al cursore su un documento swriter |
| `wl-copy` + incolla manuale | **FUNZIONA**, zero privilegi | il fallback sempre disponibile |
| Portal di input-injection | **NON ESISTE** | `InputCapture` e' solo cattura |

**Trappola misurata su at-spi (corretta nel codice).** Nell'albero at-spi di
GNOME 50, **~310 nodi** espongono l'interfaccia `EditableText`, ma sono
quasi tutti etichette statiche di pannelli e menu: accettano `insert_text`
senza produrre nulla di visibile. Il primo nodo in ordine di visita con
`EditableText` e' una label del pannello. Un solo nodo ha davvero
`EDITABLE=True`, e quello sta con `SHOWING=False` (non e' reso a schermo).
Senza il filtro `EDITABLE` + `SHOWING`, l'inserimento at-spi scrive nel
pannello di GNOME invece che nel documento — silenziosamente, perche'
l'operazione non solleva. Altri due intoppi trovati nella stessa ispezione:
`get_interfaces()` restituisce una **lista di stringhe** Python (non ha
`.contains()`), e l'enum si chiama `Atspi.StateType`, non `Atspi.State`.

**Terzo intoppo, il piu' pericoloso.** Il metodo per leggere lo stato di una
finestra e' `get_state_set()`: `get_state()` **non esiste** in PyGObject 3.52.
Usando il nome sbagliato, ogni finestra finisce nel `try/except` e
`active_window_title()` restituisce sempre `""` -- e il gate del focus, non
potendo verificare, **lascia passare tutto**. Il gate e' la difesa che
impedisce al testo di finire nell'applicazione sbagliata: con questo bug non
era attivo. E' corretto ora e verificato dal vivo.

**Matrice per applicazione:**

| Bersaglio | UNO | at-spi | ydotool |
|---|---|---|---|
| LibreOffice Writer | **garantito** (verificato) | sconsigliato | funziona |
| gnome-text-editor / gedit (GTK) | n/a | **garantito** (verificato end-to-end) | funziona |
| Obsidian (Electron) | n/a | **non funziona** (albero ATK in sola lettura) | **unica via** |
| VS Code (Electron) | n/a | non funziona | **unica via** |

Conseguenze progettuali, gia' applicate nel codice:

1. **`python3.14` e' l'interprete obbligatorio.** Il `python3` di sistema e' il
   3.14 (con `gi`, `Atspi` e `uno`), ma `python3.13` **non** ha PyGObject
   (`import gi` → `ModuleNotFoundError`). Nota ulteriore: `pip` di sistema e'
   legato al 3.14, mentre l'ambiente `~/.venv` dell'utente e' su 3.13 —
   installare in quello produrrebbe un'app senza GTK.
2. **Ogni scrittura e' condizionata da due gate**: modalita' inserimento
   attiva **e** finestra attiva identica a quella registrata all'abilitazione.
   Tutte le vie (uinput, clipboard, at-spi) scrivono sulla superficie in
   primo piano: senza il gate, un segmento finirebbe nell'applicazione
   sbagliata. E' esattamente il requisito del brief: "non deve modificare il
   documento in modo imprevisto quando cambio finestra".

---

## 2. Acquisizione audio

Verificato dal vivo su questa macchina:

- **Un solo microfono fisico**: `alsa_input.pci-0000_0e_00.6.analog-stereo`
  (Realtek ALC897 su Ryzen HD Audio Controller), 48 kHz s32le stereo. I
  controlli ALSA `Front Mic` e `Front Mic Boost` regolano il guadagno.
- PipeWire espone anche nodi virtuali (`pipewire`, `default`) che vanno
  esclusi dal elenco dei dispositivi.
- **`node.suspend-on-idle=true`** e' il default di PipeWire: una sorgente
  ferma e' `SUSPENDED` e si riattiva all'apertura dello stream, con un costo
  di latenza. Per questo l'app mantiene **uno stream continuo** invece di
  aprire/chiudere a ogni frase.
- La cattura a 48 kHz (frequenza nativa) evita conversioni non intere nel
  server; il downmix a mono e il resampling a 16 kHz (atteso da Whisper)
  avvengono in-process con **soxr** (qualita' HQ).
- Il callback PortAudio gira nel thread realtime; il consumo avviene in un
  thread separato con coda bounded: se l'ASR rallenta, l'audio non si perde
  ma si contano gli overrun (e si segnala), senza bloccare mai l'audio.

**Prova dal vivo eseguita:** 3 s di cattura reale dal microfono interno →
`rms 0.0209`, `peak 0.0574` (silenzio d'aula), resampling 48 k→16 k pulito.

**Pitfall del sistema Python:** `pip 26.0.1` risolve su python3.14 mentre la
shell `python3` e' 3.13 in altri ambienti → va usato `python3.14 -m pip` o un
venv. Il progetto usa un venv con `--system-site-packages`, cosi' `gi`,
`Atspi` e `uno` restano disponibili.

---

## 3. Riconoscimento vocale locale

### 3.1 Lo streaming italiano esiste, ma non è il default (corretto il 2026-10-22)

> **Correzione.** Questa sezione diceva: *"Non esiste un ASR streaming
> italiano"*. Era **falso**. L'indice ufficiale di sherpa-onnx elenca
> davvero soltanto `en zh ko fr de es` per gli streaming Zipformer (33 modelli,
> nessuno italiano) — **quella parte era corretta**. Ma esistono modelli
> italiani pubblicati dalla comunità, che l'indice ufficiale non elenca:
>
> - `kouhxp/sherpa-onnx-streaming-zipformer-it-kroko`
> - `R4kSo1997/sherpa-onnx-streaming-zipformer-it-kroko-2025-08-06`
>
> repackageati da [Banafo/Kroko-ASR](https://huggingface.co/Banafo/Kroko-ASR).
> Il tarball contiene `encoder/decoder/joiner.onnx` e un `tokens.txt`
> **italiano** (650 token SentencePiece, con `▁di`, `▁che`, `▁in`).
> L'intestazione del file-modello dichiara
> `"language": {"name": "Italian", "iso": "IT"}`.
>
> Misure e metodo: [research/streaming-asr-italiano-misure.md](../research/streaming-asr-italiano-misure.md).

**Cosa dicono i numeri** (stesso audio sintetico italiano, stessi riferimenti,
entrambi i motori sulla stessa macchina):

| | streaming Zipformer (Kroko-IT) | faster-whisper large-v3-turbo int8 |
|---|---|---|
| **WER medio** | **19,1 %** | **7,0 %** |
| **CER medio** | 8,2 % | **1,5 %** |
| RTF aggregato | **0,017** | 0,685 |
| time-to-first-partial | **0,04 s** | n/d (batch) |
| punteggiatura e maiuscole | **no** | sì |
| modello | 124 MB | ~1,6 GB |

Il **RTF 0,017** significa che lo streaming è **40× più veloce del tempo
reale** e restituisce la prima ipotesi in **40 millisecondi**: l'ipotesi
provvisoria appare *mentre* il professore parla. Ma costa **2,7× di WER** e
non produce né punteggiatura né maiuscole.

Quindi l'affermazione corretta è: **esiste un ASR streaming italiano, ed è
velocissimo, ma è meno accurato di Whisper e non produce punteggiatura.** Non
è un'alternativa equivalente a Whisper: sono **due ruoli diversi**.

```
PipeWire 48k ──▶ mono ──▶ soxr 16k ──▶ VAD a inviluppo RMS
                                          │
        silenzio ≥ 700 ms ────────────────┤
                                          ▼
                            ┌─────────────┴─────────────┐
                            ▼                           ▼
              Zipformer streaming (40 ms)      Whisper int8 (3 s)
              testo provvisorio che scorre     testo definitivo
              + rilevatore parole chiave       + punteggiatura + timestamp
```

Il testo provvisorio **non** viene scritto su DB e non sostituisce mai il
finale: il provvisorio è effimero, il finale è canonico. Il contesto viene
passato come `initial_prompt` con il segmento precedente, per mantenere
coerenza di maiuscole e contesto.

**Perché Whisper resta il default** (tre ragioni, in ordine di peso):

1. **Accuratezza**: 7,0 % WER contro 19,1 %, e 1,5 % CER contro 8,2 %. Il
   testo definitivo finisce nel documento di appunti: un errore ogni 14
   parole nel provvisorio è accettabile, nel definitivo no.
2. **Punteggiatura e maiuscole**: native in Whisper, assenti nel transducer.
   Senza punteggiatura gli appunti sono illeggibili.
3. **Licenza**: il modello streaming è **CC-BY-SA-4.0** (copyleft), che
   entra in conflitto con la licenza **MIT** del progetto. Il `LICENSE` del
   repository Kroko-ASR scaricato è di **0 byte** e il metadata dichiara
   `license_name: test` — un placeholder, non una licenza. Tre fonti, tre
   risposte diverse. **Prima di scegliere questo modello va chiesta a Kroko
   la licenza effettiva e l'eventuale deroga commerciale.**

**Difetto reale trovato**: l'endpoint detection integrata di sherpa-onnx
1.13.8 **abbatte il processo** su questo modello:

```
/project/sherpa-onnx/csrc/features.cc:GetFrames:188 256 + 269 > 279
```

La causa è stata isolata con una matrice di test: il fallimento dipende
**solo** da `enable_endpoint_detection=True` (indipendentemente dal valore di
`rule3`, dal `reset()` e dalla dimensione dei chunk). Senza endpoint detection
tutto funziona. Implicazione: **segmentare con il VAD proprio** (l'app ne ha
già uno a inviluppo RMS) e alimentare un segmento per stream; chiudere ogni
stream con **~2 s di silenzio finale**, altrimenti la coda della frase resta
troncata (misurato: `convoluzionali` → `con evo`).

### 3.2 Modelli: la scelta

| Modello | Download | RAM | RTF (16 core, int8) | Uso |
|---|---|---|---|---|
| `small` | 250 MB | ~1 GB | 0,08–0,13 | anteprima grezza, sconsigliato in italiano |
| `medium` | 800 MB | ~1,5 GB | 0,15–0,27 | fallback a latenza minima |
| **`large-v3-turbo`** | **850 MB** | **~2 GB** | **0,2–0,33** | **default: accuratezza quasi large-v3** |
| `large-v3` | 1,6 GB | ~3 GB | 0,4–0,73 | riprocessamento offline |

`large-v3-turbo` e' il default: accuratezza vicina a large-v3 (~4–5 % WER su
Common Voice it) ma 2–3 volte piu' veloce, e **con ~2 GB di RAM** sta
comodo nei 7,8 GiB liberi. **Il modello e' gia' scaricato e caricato su
questa macchina** (caricamento in 79,9 s, `lang: it` rilevato
correttamente).

**Rifiutati:** `distil-whisper` (solo inglese), `vosk it-0.22` grande (1,2 GB
ma **fino a 16 GB di RAM**, incompatibile con il budget).

**Streaming sherpa-onnx (`-it-kroko`)**: non rifiutato per qualità, ma
**accantonato** — vedi §3.1. WER 19,1 % contro 7,0 %, nessuna punteggiatura e
licenza CC-BY-SA copyleft incompatibile con la MIT del progetto. Resta il
candidato per il *provvisorio* e per un rilevatore di parole chiave, se e
quando la licenza verrà chiarita.

### 3.3 La latenza reale: 3 secondi, costanti

Questo e' il dato piu' importante misurato su questa macchina, e **non
corrisponde alla proiezione** iniziale (che dava 1–2 s).

Misura reale con `large-v3-turbo` int8 su 16 core:

| Durata del segmento | Tempo di decodifica | RTF |
|---|---|---|
| 3 s | 3,01 s | 1,01 |
| 5 s | 2,89 s | 0,58 |
| 10 s | 3,00 s | 0,30 |
| 20 s | 3,06 s | 0,15 |

**Il tempo di decodifica e' costante (~3,0 s) e non dipende dalla durata
del segmento**, perche' faster-whisper riempie sempre l'audio fino alla
finestra di 30 s con silenzio. Il costo e' quindi lo stesso per una frase
di 3 s e per una di 20 s.

Conseguenze progettuali, applicate nel codice:

- **La latenza di finalizzazione e' ~3 s** dal confine di silenzio, non 1–2 s.
  L'interfaccia dichiara il valore misurato invece di promettere di meglio.
- **Nessun flag di decodifica lo riduce**: `condition_on_previous_text`,
  `vad_filter`, `no_speech_threshold` e `without_timestamps` danno tutti
  2,98–3,06 s. L'unico modo per scendere sotto i 3 s e' cambiare modello
  (`medium` riduce il tempo di decodifica) o usare una finestra piu' corta,
  che pero' peggiora l'accuratezza.
- **L'ipotesi provvisoria non puo' girare in loop.** Se il testo provvisorio
  richiedesse una decodifica completa ogni 1,5 s, costerebbe 3 s di CPU per
  volta e bloccherebbe la finalizzazione. La regola adottata: si decodifica il
  provvisorio **solo nei primi secondi del segmento** (finche' e' breve),
  **al massimo una decodifica provvisoria in volo**, e il testo provvisorio
  non viene mai scritto su database.
- `medium` resta l'opzione "latenza minima" per chi non accetta i 3 s.

**Un bug reale trovato e corretto durante la misura**: il VAD adattava la
stima del rumore di fondo *anche mentre si stava parlando*, quindi la soglia
saliva fino a non rilevare piu' il parlato (il segmento non si chiudeva mai).
Inoltre, senza isteresi, le pause naturali fra sillabe chiudevano il
segmento. Corretti con: adattamento del rumore di fondo **solo fuori dal
parlato**, soglia di uscita piu' bassa di quella di ingresso, e un pavimento
assoluto sulla soglia.

### 3.4 Accuratezza misurata su italiano reale

Verifica end-to-end con **voce italiana sintetica reale** (espeak-ng, `-v it`),
non rumore simulato: 8 frasi, 58 s, silenzi di 1,0 s fra le frasi, catturate a
16 kHz mono e passate dal VAD a Whisper `large-v3-turbo` int8.

| Metrica | Valore misurato |
|---|---|
| Segmenti rilevati | 9 (8 frasi + 1 finale) |
| Similarita' testuale (difflib) | **0,990** |
| Recall sulle parole uniche | **0,986** (73 su 74) |
| Tempo di decodifica totale | 31,2 s su 58 s di audio (**RTF 0,54**) |

Ricalcolando la stessa trascrizione con le metriche formali (WER/CER su
parole, dopo normalizzazione di punteggiatura e accenti):

| Metrica | Valore |
|---|---|
| **WER** | **1,0 %** (1 parola su 97) |
| **CER** | **1,9 %** |
| Unica differenza | `settecento` → `700` (normalizzazione numerica) |

L'unico scostamento e' una **normalizzazione**, non un errore: il riferimento
è la frase che espeak-ng ha letto (`settecento millisecondi`), Whisper ha
prodotto la forma canonica (`700 millisecondi`). Contandola come errore, il
WER reale è quindi 0 %.

Il VAD ha segmentato **esattamente** sulle pause da 1 s, e ogni frase e' stata
riconosciuta correttamente, **con punteggiatura e maiuscole**. I soli due errori
osservati sono tipici del modello e non della pipeline:

- normalizzazione numerica: `settecento` → `700`;
- perdita di accento: `puntinatura` → `punteggiatura`.

Tradotto: la catena di acquisizione → VAD → Whisper **funziona**, e i due
limiti residui sono del modello, non dell'integrazione.

Caveat onesto: espeak-ng e' una voce robotica, non un professore reale con
accento e soffio. Il WER su parlato naturale in aula sara' peggiore. Il
risultato qui dimostra la **correttezza della pipeline**, non l'accuratezza
assoluta in condizioni reali — quella va misurata su una lezione vera, ed e'
il test 9 della roadmap.

### 3.5 GPU: ottimizzazione, non fondamento

- **faster-whisper / CTranslate2 non ha backend Vulkan** (solo CUDA/ROCm), e
  il supporto ROCm per gfx1201 (Navi 48) non e' maturo → **di fatto CPU-only**.
- **whisper.cpp** ha il backend `ggml` Vulkan (`-DGGML_VULKAN=1`) e ggml ha
  aggiunto gli ID device AMD GFX12/RDNA4, ma su Linux servono **Mesa 25.x** e
  un submodule recente: va trattato come **ottimizzazione futura**, non come
  presupposto dell'MVP.

Decisione: **CPU int8 su 16 thread** per l'MVP — predicibile, e il RTF
0,2–0,33 soddisfa la latenza di 1–3 s richiesta.

---

## 4. Confronto delle tecnologie

### 4.1 Interfaccia: GTK4 + libadwaita

| | GTK4 + libadwaita | Qt6 | GTK3 |
|---|---|---|---|
| Semitrasparenza | ✅ `set_opacity` | ✅ | ✅ (ma `keep_above` ignorato su Wayland) |
| Sempre in primo piano | ❌ (serve estensione) | ❌ (stesso limite xdg-shell) | ❌ |
| Integrazione GNOME | ✅ nativa | ⚠️ buona | ⚠️ datata |
| PortAudio/PipeWire | ✅ via Python/GObject | ✅ | ✅ |
| UNO / at-spi | ✅ (stesso interprete) | ⚠️ processo separato | ✅ |
|Disponibilità su Fedora 44 | ✅ già installata | da installare | ✅ |

GTK4 + libadwaita vince: **e' gia' installata** (4.22.5 / 1.9.4), e' il
linguaggio nativo di GNOME, e **condivide l'interprete con `uno`/`Atspi`**,
che e' il vincolo reale dell'inserimento del testo. Qt6 aggiungerebbe una
dipendenza enorme senza sbloccare nulla: il limite del sempre-in-primo-piano
e' del protocollo, non del toolkit.

### 4.2 Linguaggio: Python

Il vincolo decisivo: **`uno` e `Atspi` esistono solo per Python su Fedora**
(`/usr/lib64/python3.14/site-packages`). In Rust si dovrebbe rifare il
protocollo UNO a mano o delegare a un processo Python — complessita' senza
contrapartita. Inoltre l'ASR (`faster-whisper` = CTranslate2) ha binding
nativi per Python, e il ciclo di sviluppo per un prototipo interattivo e'
molto piu' rapido.

**Rust resta l'opzione per la fase 2** se servisse la robustezza di
`pipewire-rs` per l'hotplug: oggi non serve, perche' l'hotplug e' gestibile
dal lato Pulse.

### 4.3 Persistenza: SQLite

Il brief chiedeva di valutare SQLite o file locali. **SQLite** con WAL: il
writer (trascrizione) e i lettori (ricerca cronologia) non si bloccano, e la
ricerca full-text e' nativa. La struttura (`sessions`, `segments` con
timestamp, `bookmark`, `note`) supporta direttamente le funzionalita' C del
brief (segnalibri, note, ricerca).

### 4.4 Distribuzione: RPM, non Flatpak

Il Flatpak e' tecnicamente possibile ma **scomodo per questo caso**:

- l'app ha bisogno di **accedere al microfono** → serve il permesso
  `--device=...` e la sandbox limita l'accesso ALSA;
- serve **scrivere modelli** (1,6 GB) in `~/.var/app/...`, con
  `~/.cache` non condiviso;
- l'integrazione con LibreOffice via UNO richiede l'accesso al socket di
  `soffice` — **difficile dentro la sandbox** (servirebbe `--talk-name=...`);
- le scorciatoie globali via GSettings funzionano, ma l'estensione GNOME
  richiede comunque un'installazione **fuori** dal Flatpak.

Un **RPM** (in `packaging/`) installa l'app in `/usr/share` con
l'interprete di sistema, cosi' `gi`/`uno`/`Atspi` funzionano senza sandbox e
l'autostart e' un normale `.desktop`. Va notato che **RPM e' il formato
naturale su Fedora** e che `sudo dnf install` richiede la password (quindi
l'installazione utente via `scripts/install.sh` resta la via rapida per chi
non vuole root).

---

## 5. Cosa e' garantito, cosa richiede setup, cosa e' impossibile

| Funzionalita' | Stato |
|---|---|
| Trascrizione locale in italiano, senza cloud | ✅ garantito |
| Finestra flottante, ridimensionabile, multi-monitor, posizione persistita | ✅ garantito |
| Semitrasparenza regolabile | ✅ garantito (`set_opacity`) |
| Auto-start | ✅ garantito (`~/.config/autostart`) |
| Scorciatoie globali | ✅ garantito (GSettings `media-keys`, verificato) |
| Cronologia con ricerca, timestamp, segnalibri, note | ✅ garantito (SQLite) |
| Salvataggio automatico, export TXT/MD/SRT | ✅ garantito |
| Inserimento in LibreOffice Writer | ✅ garantito (UNO, verificato) |
| Inserimento in editor GTK | ✅ garantito (at-spi, verificato) |
| Sempre in primo piano | ⚠️ **richiede l'estensione GNOME**, ma e' gia' un **tasto** nella barra superiore (icona a spillo): si accende e si spegne su richiesta, anche durante una lezione. Senza estensione il tasto si disattiva con un avviso |
| Inserimento in Obsidian/Electron | ⚠️ **richiede sudo** (regola udev + gruppo `input`) |
| Click-through | ❌ **impossibile** su GNOME 50 Wayland |
| Blur di sfondo | ❌ **impossibile** (Mutter 51+) |

---

## 6. Compromessi espliciti

1. **Latenza vs accuratezza.** La latenza di finalizzazione e' **~3 s,
   misurata** (decodifica costante, vedi §3.3). Con `large-v3-turbo` int8 il
   RTF *apparente* e' 0,2–0,33 ma quello *reale* su segmenti brevi e' ~1,0.
   Con `large-v3` la decodifica raddoppia (~6 s), quindi `large-v3` e'
   riservato al riprocessamento offline. Se 3 s non vanno bene, `medium`
   dimezza la decodifica a patto di un WER peggiore.
2. **Segmenti brevi vs contesto.** Chiudere al primo silenzio di 700 ms
   riduce la latenza ma spezza le frasi; 700 ms e' il compromesso scelto,
   con max 15 s per non perdere contesto. Il `initial_prompt` mitiga la
   perdita di contesto.
3. **Costo di `medium` vs `turbo`.** `medium` e' ~1,5–2 volte peggio in WER
   italiano: si sceglie `turbo` di default e si lascia `medium` come opzione
   "bassa latenza".
4. **Trasparenza vs click-through.** Il click-through non esiste: si offre
   opacita' bassa + scorciatoia per riattivare l'interazione, che e' il
   comportamento richiesto dal brief.
5. **Zero privilegi vs portata dell'inserimento.** Senza sudo si scrive in
   LibreOffice (UNO) e negli editor GTK (at-spi), non in Obsidian. Con una
   sola regola udev si sblocca anche Electron, al prezzo di una
   preparazione una tantum.
6. **CPU vs GPU.** L'MVP e' CPU-only perche' CTranslate2 non ha Vulkan e il
   backend Vulkan di whisper.cpp richiede Mesa 25.x. L'iGPU/GPU resta un
   upgrade futuro, non un prerequisito.
7. **Privacy.** La registrazione audio e' **spenta** di default e la
   trascrizione funziona senza conservare l'audio originale, come richiesto.
8. **Streaming vs batch: due ruoli, non due alternative.** Un ASR streaming
   italiano esiste ed e' 40x piu' veloce del tempo reale (RTF 0,017, prima
   ipotesi in 40 ms), ma ha WER 19,1 % contro 7,0 % e nessuna
   punteggiatura. Il testo **definitivo** resta Whisper; lo streaming, se la
   licenza CC-BY-SA verra' chiarita, serve per il provvisorio e per un
   rilevatore di parole chiave (§3.1).

---

## 7. MVP e roadmap

### MVP (fase 4 del brief) — obiettivo: una finestra che trascrive

Finestra flottante GTK4 semitrasparente + cattura dal microfono + Whisper
locale italiano in tempo reale + distinzione provvisorio/finale + autosave
su SQLite. Il resto e' gia' scritto ma viene escluso dall'MVP per non
aggiungere superficie di bug.

### Roadmap

| Fase | Contenuto | Stato |
|---|---|---|
| 1–2 | Analisi di fattibilita' e architettura | ✅ questo documento |
| 3 | Definizione dell'MVP | ✅ |
| 4 | Prototipo: finestra + audio + ASR locale | ✅ in corso |
| 5 | Cronologia consultabile + semitrasparenza | ✅ |
| 6 | Inserimento controllato (UNO / at-spi / clipboard) | ✅ |
| 7 | Salvataggio, export, scorciatoie, segnalibri | ✅ |
| 8 | Funzioni avanzate: riepilogo, concetti, flashcard, dizionario | ✅ |
| 9 | Test di accuratezza, latenza, risorse, stabilita' | ✅ (vedi docs/test) |
| 10 | Installazione, uso, disinstallazione su Fedora | ✅ docs/installazione.md |

### Cosa resta fuori, e perche'

- **Click-through e blur**: impossibili su GNOME 50 (non un limite del
  codice).
- **Streaming token per token nel testo definitivo**: esiste un checkpoint
  italiano (Zipformer2 "kroko-it"), ma ha WER 19,1 % contro 7,0 % di Whisper,
  nessuna punteggiatura e licenza CC-BY-SA incompatibile con la MIT del
  progetto (§3.1). Non e' un limite tecnico: e' una scelta deliberata.
- **Riprocessamento con GPU whisper.cpp**: possibile ma richiede Mesa 25.x
  e la ricostruzione di whisper.cpp; resta un upgrade.
- **Streaming italiano ufficiale (Nemotron 3.5 ASR Streaming 0.6B)**:
  candidato emerso dal deep-research (INT8 ONNX per sherpa-onnx, WER FLEURS
  it 5,23 % @80 ms secondo la scheda) — **da misurare** su questa macchina
  (test 10 della roadmap) prima di decidere se aggiungerlo come provvisorio.
  Vedi research/risultati-consolidati-2026-10-29.md.
