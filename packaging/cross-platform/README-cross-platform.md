# Lecture Transcriber su Windows e macOS

Il **core** di Lecture Transcriber è cross-platform: lo stesso codice Python
(`faster-whisper`, `soxr`, `sounddevice`, sessioni SQLite, export TXT/MD/SRT)
gira identico su Linux, Windows e macOS. Ciò che è Linux-specific è l'**interfaccia
GTK/libadwaita**, che non ha build stabili di PyGObject su Windows/macOS.

Questo pacchetto produce un **binario headless** (riga di comando) di
Lecture Transcriber per Windows e macOS: la stessa qualità di trascrizione,
senza la UI grafica.

---

## Cosa ottieni

| File prodotto | Piattaforma | Cosa fa |
|---|---|---|
| `dist/lecture-transcriber.exe` | Windows | Trascrive un file audio (WAV), salva su SQLite, esporta TXT/MD/MD_TS/SRT |
| `dist/lecture-transcriber` | macOS | Idem |

Niente interfaccia grafica: è un comando da terminale. È pensato per
l'uso diretto (trascrivere una registrazione) e per essere richiamato
dagli script.

> **Perché non la UI GTK?** Su Windows non esiste un PyGObject nativo stabile
> per GTK4/libadwaita (solo MSYS2, non adatto alla distribuzione); su macOS
> gira ma è fragile per always-on-top e scorciatoie globali. Portare la UI è
> un lavoro a sé (una UI nativa minima, es. PySide6) — vedi il limite sotto.

---

## Requisiti

**Windows**
- Python 3.11+ da [python.org](https://python.org) (spunta *Add to PATH*).
- Microsoft Visual C++ Redistributable (richiesto da `ctranslate2`).

**macOS**
- Python 3.11+ (via [python.org](https://python.org) o Homebrew).
- Xcode Command Line Tools (`xcode-select --install`) per il toolchain.

I **modelli Whisper (~1,6 GB)** non sono inclusi nel binario: si scaricano al
primo utilizzo nella cartella dati della piattaforma (vedi *Dove va tutto*).

---

## Build

### Windows (PowerShell, dalla cartella del progetto)

```powershell
.\packaging\cross-platform\build-win.bat
```

### macOS

```bash
bash packaging/cross-platform/build-mac.sh
```

Entrambi: creano un virtualenv locale, installano le dipendenze e lanciano
PyInstaller. Il binario esce in `dist/`.

### Binario universale macOS (arm64 + x86_64)

Builda due volte (una su un Mac Apple Silicon, una su un Intel) e fai il merge:

```bash
lipo -create dist-arm/lecture-transcriber dist-x86/lecture-transcriber \
     -output lecture-transcriber-universal
```

---

## Uso

```bash
# aiuto
lecture-transcriber --help

# trascrivi un file (WAV; mono/stereo, nei formati PCM 8/16/32 bit)
lecture-transcriber lezione.wav

# con metadati e export in un colpo
lecture-transcriber lezione.wav --course "Analisi II" --professor "Rossi" \
        --export out.md --format md_ts

# dal microfono (Ctl+C per fermare)
lecture-transcriber --mic --course "Analisi II"

# scegli un modello diverso (default: large-v3-turbo)
lecture-transcriber lezione.wav --model medium
```

Formati export: `txt`, `md`, `md_ts` (markdown con timestamp), `srt`.

---

## Dove finiscono i dati (percorsi per piattaforma)

| Piattaforma | Dati (sessioni, preferenze) | Modelli | Cache |
|---|---|---|---|
| **Linux** | `~/.local/share/lecture-transcriber` | ‹stessa›/`models` | `~/.cache/lecture-transcriber` |
| **Windows** | `%APPDATA%\lecture-transcriber` | ‹stessa›\`models` | `%LOCALAPPDATA%\lecture-transcriber` |
| **macOS** | `~/Library/Application Support/lecture-transcriber` | ‹stessa›/`models` | `~/Library/Caches/lecture-transcriber` |

I percorsi rispettano le convenzioni native (implementato in
`src/lecture_transcriber/config.py`, funzione `_platform_home_dirs`).
Puoi forzare un percorso con le variabili `LT_DATA_DIR`, `LT_CONFIG_DIR`,
`LT_CACHE_DIR`, `LT_MODEL_DIR`.

---

## Limiti reali e note oneste

1. **Niente interfaccia grafica** su Windows/macOS in questo pacchetto.
   La trascrizione è via riga di comando. Una GUI nativa (PySide6) è
   l'evoluzione naturale ma è un lavoro separato e **non** è inclusa qui.
2. **Inserimento automatico nei documenti** (LibreOffice/at-spi/wl-copy):
   non disponibile nei binari. Su Windows/macOS il meccanismo at-spi non
   esiste; UNO per LibreOffice resterebbe, ma senza UI non c'è il pulsante
   che lo attiva. Per ora il testo si esporta, poi si incolla.
3. **Scorciatoie globali / sempre-in-primo-piano**: requisiti della GUI,
   non del core → non rilevanti per il binario headless.
4. **Microfono**: `sounddevice` usa PortAudio; su Windows parla con WASAPI,
   su macOS con CoreAudio. Dovrebbe funzionare senza configurazione.
5. **WeasyPrint/PDF**: non incluso (serve la UI). L'export è TXT/MD/SRT.

---

## Verifica dell'installazione (portabilità)

I test automatici del progetto girano su Linux; i percorsi cross-platform
sono coperti da test dedicati (`test_config.py::TestPlatformHomeDirs`,
`test_audio.py::TestVirtualNoiseNode`). Sul binario, verifica rapida:

```bash
lecture-transcriber --version
lecture-transcriber --help
```

Su macOS, se il binario apre il Firewall/Gatekeeper: `xattr -dr com.apple.quarantine
dist/lecture-transcriber` (o `spctl --add` dopo la firma).

---

## Risoluzione problemi

| Sintomo | Probabile causa | Rimedio |
|---|---|---|
| `.exe` non parte (manca DLL) | VC++ Redistributable mancante | installa dal sito Microsoft |
| Modello non trovato al primo uso | rete assente | scarica prima con `LT_MODEL_DIR=... python -c "from faster_whisper import WhisperModel; WhisperModel('large-v3-turbo')"` |
| Binario mac "danneggiato" | quarantine | `xattr -dr com.apple.quarantine <file>` |
| Microfono non rilevato | device index | usa `--micro` senza indice (default di sistema) |
