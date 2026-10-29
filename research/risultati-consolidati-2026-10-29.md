# Convergenza: ASR streaming italiano, portal e app concorrenti

Rapporto consolidato (2026-10-29). Tre livelli di prova distinti:

- **MISURATO qui** (questa macchina, Fedora 44): i numeri da me eseguito dal vivo.
- **salvato** (deep-research 2026-10-22): claim estratti da fonti primarie ma con
  **verifica avversaria fallita** — tutti i pannelli verifier sono morti su errori
  di infrastruttura (85 agenti su 400); questi claim sono **estratti, non
  verificati**. Segnati uno a uno.
- **prior** (workflow di fattibilità 2026-10-22): 48 claim, 1 solo verificato.

---

## A. RemoteDesktop portal: un sentiero di inserimento testo che SU QUESTA macchina CRASHA

**MISURATO qui (due riproduzioni identiche, 2026-10-22 e 2026-10-29):**
`RemoteDesktop.CreateSession` **abbattono l'intero `xdg-desktop-portal`**:

```
xdg-desktop-portal:ERROR:../src/xdp-session.c:296:xdp_session_initable_init:
  assertion failed: (session->token != NULL)
Bail out!  -> core-dump (status 6/ABRT) -> systemd riavvia il portal
```

Rilevante perché il deep-research ha scoperto che **Vocalinux** (app di dettatura
Linux, AGPL-3.0) usa `NotifyKeyboardKeysym` del portal **RemoteDesktop** per
scrivere testo nelle app Wayland **senza** `/dev/uinput` (claim salvato [52]).
Ma su **questa** macchina quel percorso **non è percorribile**: la strada che
Vocalinux usa fa crashare il portal. Verifica diretta che **GlobalShortcuts** lo
stesso giorno funziona (errore pulito `NotAllowed: An app id is required`, il
portal resta vivo). Check:

```
GlobalShortcuts.CreateSession -> clean error, portal attivo
RemoteDesktop.CreateSession  -> CRASH (assertion, portal morto)
```

Conclusione per il progetto: **non adottare RemoteDesktop-based injection né su
questa macchina né come "unica via"** — dipenderesti da un portal rotto. Resta
la matrice attuale (UNO / at-spi / clipboard / ydotool). Se in futuro si
volesse il RemoteDesktop, va testato un fix (aggiornamento
xdg-desktop-portal-gnome, o Workaround: chiamare CreateSession con entrambi i
token) e capita la root cause prima di integrarlo.

Prove: `research/portal-remotedesktop-crash.txt`.

---

## B. Streaming italiano: la risposta definitiva (mancava al workflow prior)

**MISURATO qui (2026-10-22, `research/streaming-asr-italiano-misure.md`):**
Zipformer2 community `-it-kroko` su sherpa-onnx 1.13.8, voce sintetica:

| | streaming Zipformer (Kroko-IT) | faster-whisper turbo int8 |
|---|---|---|
| WER medio | **19,1 %** | **7,0 %** |
| CER medio | 8,2 % | 1,5 % |
| RTF | **0,017** | 0,685 |
| time-to-first-partial | **0,04 s** | n/d |
| punteggiatura/casing | no | sì |
| modello | 124 MB | ~1,6 GB |

Difetto reale scoperto: `enable_endpoint_detection=True` di sherpa-onnx 1.13.8
**crasha** su questo modello (`features.cc:GetFrames:188 256+269>279`); la
segmentazione va fatta col VAD proprio (già presente nell'app) e ogni stream va
chiuso con ~2 s di silenzio.

**Salvato (deep-research, NON verificato):** esiste oggi un'alternativa
ufficiale e seria per lo streaming italiano in locale — NVIDIA
**Nemotron 3.5 ASR Streaming 0.6B** (FastConformer-CacheAware-RNNT, 600 M),
pubblicato come **INT8 ONNX in 4 chunk** (80/160/560/1120 ms) per sherpa-onnx
([60][61][64][70][73]):
- FLEURS it WER 5,23 % @80 ms → 4,25 % @1120 ms ([70]);
- granularità selezionabile a runtime via `att_context_size` ([71]);
- RTF CPU misurato *solo* sul modello inglese 560 ms: 0,16 su 1 thread ([62]);
- nessun WER/CER italiano nel modello ufficiale sherpa ([63]);
- percorso offline completo: GGUF q8_0 + NeMo-Speech.cpp, `--language it-IT` ([73]).

Se questa strada regge alla verifica locale (scarica il tarball ONNX e misura
RTF + WER su 16 core col mio `bench_it_streaming.py`), sarebbe **il** candidato
per il provvisorio: streaming vero, italiano ufficiale, INT8, CPU-only, senza i
problemi di licenza del txt di Kroko. **Da fare come test 10 della roadmap** —
con la stessa disciplina di misura già usata.

**Confronto con quanto già misurato**: nemotron @80 ms = 5,23 % vs kroko Zipformer
19,1 % (voce sintetica). Due modelli, due epoche: il confronto onesto va fatto
dopo la misura, non prima.

---

## C. App concorrenti (salvato — verifica singola su questo host)

| App | Licenza | Streaming | Testo nel focus | nota (salvato) |
|---|---|---|---|---|
| **Vocalinux** | AGPL-3.0 | no (batch VOSK + whisper.cpp) [54] | Synchronous **RemoteDesktop Keysym** [52] — MA il portal crasha su questo host (A) | overlay GTK3 layer-shell [53] |
| **Buzz** | MIT | no batch 5 s [57] | no | piano Qt, niente overlay [56] |
| **Handy** | MIT [20] | no [21] | xdotool/wtype/ydotool [23] | gtk-layer-shell [24] |
| **hyprwhspr** | ? | parziale (NeMo/Phonon solo en)[42] | ydotool /dev/uinput [40][43] | hotkey evdev |
| **Voxtype** | MIT | no (push-to-talk) | ydotool [46] | OSD layer-shell rotto su GNOME [47] |
| **Whishper** | AGPL | no (batch) [31] | — | web UI, CUDA only [32][33] |

Claim 52 è il più importante perché tocca l'architettura: se fosse vero senza
condizioni, `insertion.py` potrebbe avere une quarta via. **Non lo è su questo
host** (il portal crasha). Lo stato reale: "su GNOME/Wayland non esiste una via
portale affidabile oggi; le app serie (Vocalinux) la tentano ma si scontrano
con portal roto o layer-shell assente".

---

## D. GlobalShortcuts: versione e UI di rebinding (salvato)

- [25] Il backend GNOME 50 di xdg-desktop-portal-gnome exporta GlobalShortcuts a
  **version 1**; la 2 è solo su main (branch 51.1) — coerente con quanto già
  verificato localmente prima.
- [26][27] `ConfigureShortcuts` (v2) aggiunta su xdg-desktop-portal-gnome da
  commit b104d4db (MR 264, 2026-07-03) e su xdg-desktop-portal da 1.21.0
  (commit a78b5b393).
- [15][16][17] L'interfaccia standard GlobalShortcuts è a **version 2** e
  ConfigureShortcuts è fire-and-forget (nessun modo di conoscere il rebinding
  fatto dall'utente).
- [3] Da portal 1.21.0 serve un **AppID valido** — il progetto RPM ce l'ha,
  un Flatpak deve dichiararel'identità correttamente.

Impatto sul progetto: la scelta GSettings-media-keys resta giusta per GNOME 50.
su GNOME 51 si potrà usare il portal con rebinding; su essa macchina, ora.

---

## E. Flatpak vs RPM (salvato)

- [58] Il manifest ufficiale di Buzz chiede `--socket=wayland --talk-name=...`
  capture raw PipeWire/PulseAudio e DRM, **non** il portal: conferma che per
  audio con [80] il ScreenCast (v6) non ha audio/mic — serve il socket raw o il
  permesso `--device=all`.
- [82] Un'app sandboxata raggiunge PipeWire via portal → fd a un remote
  PipeWire ristretto → `pw_context_connect_fd`; non un socket grezzo.
- [83] `persist_mode` può rendere persistenti le autorizzazioni di cattura
  (2 = finché non revocate esplicitamente).
- [84] La sicurezza è imposta dal backend, non dall'interfaccia.

→ Coerente con la roadmap: **RPM primary, Flatpak secondario** (§4.4 del
brief). Un Flatpak con audio richiede il socket raw, che erode il senso stesso
del sandbox: per l'audio la strada pulita è il portal, che però (A) su questa
macchina il sottoportale RemoteDesktop crasha e il ScreenCast non fa audio.

---

## F. Accuratezza Whisper italiano (salvato)

- [75] Open ASR Leaderboard: large-v3 it WER 5,31 (covost) / 6,50 (MLS) / 2,26
  (FLEURS); turbo 6,84 / 6,63 / 3,28 → **turbo peggiore di large-v3 su tutti e
  tre i corpora**, coerente con la mia misura (7,0 % turbo vs ~4-5 % large-v3
  atteso da HF).
- [76] il leaderboard multilingual ha solo large-v3 e large-v3-turbo tra i
  whisper-family: niente WER italiano per medium/small/distil.
- [79] tutti e tre i corpora italiani sono **lettura pulita** (no lecture).
- [68][65][66] **mTEDx** è l'unico corpus lecture-style italiano: 107 h, 53k
  utt; Kaldi ibrido 16,4 %; Transformer da zero 48,0 %; **gesto riferimento:
  16,4 % non va confrontato coi 2-7 % di Whisper su lettura pulita**.
- [74] Il WER Nemotron è su lettura pulita soltanto; nulla su lecture.

---

## G. La foto finale (cosa è solido, cosa è salvato)

**Solido (misurato o verificato qui)**
1. Il claim "non esiste ASR streaming italiano" è falso: esiste Kroko Zipformer2
   (19,1 % WER, RTF 0,017, TTFP 0,04 s) — misurato.
2. Whisper turbo 7,0 % WER / 1,5 % CER su stesso audio — misurato.
3. Endpoint detection sherpa-onnx crasha su modello kroko; VAD proprio + flush
   silenzio — misurato.
4. `RemoteDesktop.CreateSession` crasha l'intero portal su questo host —
   riprodotto 2×.
5. at-spi / UNO verificati dal vivo; GlobalShortcuts v1 su GNOME 50, no
   ConfigureShortcuts; InputCapture senza tastiera — verificato prima.
6. wtype rotto (no wlr-virtual-keyboard), layer-shell non implementato, blur
   GNOME 51+ — verificato.

**Salvato (da fonti primarie, verifica avversaria NON eseguita)**
7. Nemotron 3.5 ASR Streaming 0.6B it: WER FLEURS 5,23 %, INT8 ONNX per
   sherpa-onnx, 4 chunk, RTF CPU 0,16 (en 560 ms, 1 thread).
8. Vocalinux usa RemoteDesktop Keysym per scrivere in Wayland (inattivo su
   questo host: (4)).
9. Leaderboard leggibile: il matrisetto it è solo lettura pulita; mTEDx è
   l'unico corpus lecture; nessun WER lecture per Whisper.
10. Flatpak audio → socket raw PipeWire / `--device=all`, non il portal.

**Prossimo passo consigliato (test 10 della roadmap)**: scaricare gli INT8
ONNX Nemotron it, misurarli con `bench_it_streaming.py` e `eval_it_streaming.py`
(quantizzando il tarball nello stesso format usato da sherpa), verificare se
la strada "streaming vero a WER basso" regge su 16 core — e solo allora
decidere se il dettaglio di licenza worth it.
