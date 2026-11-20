# Test: accuratezza, latenza, risorse, stabilita'

Tutti i numeri qui sotto sono **misurati su questa macchina**
(Fedora 44, GNOME 50.6 Wayland, 16 core, AMD RX 9070, 7,8 GiB liberi).

---

## 1. Test automatici

```bash
cd ~/lecture-transcriber
.venv/bin/python -m pytest tests/ -q
```

**Risultato: 364 test e 17 subtest, tutti verdi.**

| File | Copre |
|---|---|
| `test_asr.py` | segmentatore: confine sui silenzi, scarto dei segmenti troppo brevi, `max_segment_s`, `flush()` |
| `test_sessions.py` | SQLite: creazione sessione, segmenti, ricerca, segnalibro, nota, cancellazione con cascade |
| `test_config.py` | round-trip delle preferenze, chiavi sconosciute ignorate, scorciatoie **unite** e non sostituite |
| `test_insertion.py` | gate dell'inserimento: disarmato non scrive, focus cambiato non scrive, capacita' come dict di bool, **filtro EDITABLE/SHOWING di at-spi**, **lettura del titolo della finestra attiva** |
| `test_pipeline_lazy.py` | **caricamento differito del modello**, richiesta di ascolto pendente durante il caricamento, **l'audio scartato viene detto a fine sessione** |
| `test_audio.py` | cattura audio, buffering, gestione degli errori del dispositivo |
| `test_recorder.py` | **la registrazione audio opzionale**: WAV PCM 16 bit mono a 16 kHz valido e rileggibile, cartella creata solo al primo uso, ampiezza conservata, campioni fuori scala *clippati* e non avvolti, NaN/Inf azzerati, `close()` idempotente. E il legame con la pipeline: la cattura alimenta il file, la pausa **non** lo fa crescere, un errore di scrittura avvisa senza fermare la trascrizione |
| `test_pdf_export.py` | l'export PDF: HTML impaginato dai segmenti finali (timestamp, segnalibri, note), escaping dei caratteri speciali, metadati in intestazione, e il messaggio chiaro quando WeasyPrint manca (saltato se la libreria non e' installata) |
| `test_shortcuts_gsettings.py` | percorso GSettings: parsing della lista, **validazione dei percorsi**, registrazione, rimozione, non toccare le scorciatoie dell'utente |
| `test_app_shortcuts.py` | consegna della scorciatoia all'azione, GSettings preferito sul portal, interfaccia D-Bus coerente con `config.APP_ID` |
| `test_ui_settings.py` | **il pannello preferenze: ogni gruppo costruito deve stare nella pagina, larghezza sufficiente per le etichette**, la consultazione discreta deve restare una modalita' e non una trappola (il commutatore Lettura/Appunti raggiungibile), registro audio spento per default. **Anche: il tasto del sempre-in-primo-piano** (esiste, e' raggiungibile in tutte e tre le modalita', torna spento e non tocca la preferenza se l'estensione manca), la **pillola di ritorno** e la **seconda capsula di stato** sul transcript, l'export SRT con **anteprima prima di scrivere** |
| `test_transcript_view.py` | la **vista del transcript**: l'ipotesi provvisoria scritta *in place* in coda all'ultimo segmento (non come riga nuova), `text_for()` che esclude il provvisorio, niente newline orfano dopo una sequenza, `last_line()` col testo committato |
| `test_study.py` (esteso) | robustezza del modulo di studio: **file JSON non UTF-8 ignorato invece di far sollevare il costruttore**, estrazione del termine con la testa del sintagma, spezzatura in frasi con le abbreviazioni, **campi vuoti che non fanno scivolare l'indice della risposta** |
| `test_study_cli.py` | **il punto d'ingresso che mancava al modulo di studio** (`scripts/study_session.py`): elenco delle lezioni, materiale in Markdown su stdout e su file, JSON scritto nella cartella dati e rileggibile, **lezione inesistente e lezione vuota che escono con codice 1 invece di un traceback**, nessun argomento = errore d'uso, e il renderer che mostra tutte le sezioni con la risposta del quiz segnata |
| `test_launch_without_asr.py` | **il lancio con un tap senza numpy/soxr/sounddevice/faster-whisper**: il pacchetto si importa, `pipeline.open()` sopravvive, "ascolta" segnala l'errore invece di morire o tacere. Gira in un sotto-processo con `MetaPathFinder` che blocca i moduli prima dell'import |

### Cinque bug reali trovati dai test e dalle verifiche dal vivo

1. **`wl-copy` bloccava il processo.** `wl-copy` fork-a in background per
   servire le richieste future degli appunti; con `capture_output=True` la
   pipe restava aperta e `subprocess.run` scadeva il timeout dopo 5 s.
   Corretto usando `stdout=DEVNULL`. Il percorso clipboard era **completamente
   rotto** prima della correzione.
2. **L'inserter era armato per default.** Un `TextInserter` appena creato
   poteva scrivere nel documento senza che l'utente avesse autorizzato
   nulla. Ora la costruzione e' **disarmata** e serve un `arm()` esplicito:
   difesa in profondita' contro il requisito "non deve modificare il documento
   in modo imprevisto".
3. **at-spi scriveva nel pannello di GNOME.** `_find_editable_in()` accettava
   il primo nodo che espone `EditableText` -- ma nell'albero at-spi di GNOME 50
   **~310 nodi** la espongono, e il primo in ordine di visita e' una label
   nascosta del pannello. Il testo sarebbe finito la', non nel documento.
   Trovato per ispezione e confermato con una sonda: **un solo nodo** su 310 ha
   davvero `EDITABLE`, e quello sta con `SHOWING=False`, quindi non e'
   raggiungibile. Ora il filtro richiede `EDITABLE` **e** `SHOWING`.
   Due bug collegati, trovati nella stessa ispezione: `iface.contains(...)`
   su una lista Python (AttributeError inghiottito da un `except` muto) e
   `Atspi.State` invece di `Atspi.StateType`. Entrambi uccidevano silenziosamente
   l'intero percorso at-spi.
4. **Il gate del focus era una gabola.** `active_window_title()` chiamava
   `win.get_state()`, metodo che **non esiste** in PyGObject 3.52 (quello
   giusto e' `get_state_set()`). Ogni finestra finiva nel `try/except`, la
   funzione restituiva sempre `""` -- e il gate, non potendo verificare,
   lasciava passare **tutto**: il controllo "non scrivere se l'utente ha
   cambiato finestra" non era attivo. Questo e' il bug piu' insidioso dei
   cinque perche' neutralizzava una difesa di sicurezza invece di rompere
   una funzione. Verificato dal vivo: ora restituisce
   `'nota_tit.txt (/tmp) - Text Editor'`.
5. **Il modello Whisper si caricava all'apertura della finestra.** `open()`
   chiamava `load_async()`, quindi aprire l'app anche solo per consultare una
   sessione vecchia costava **1,1 GB di RSS** misurati. Il requisito §8 dice
   "poche risorse quando inattivo". Ora il caricamento parte al primo
   "ascolta": misurato **139 MB** a finestra aperta, modello scaricato.
   Nella stessa correzione, un secondo difetto: se l'utente premeva "ascolta"
   durante il caricamento, `start()` usciva senza far nulla e la pressione
   andava **perduta** (nessuno onorava la richiesta). Ora resta in attesa e
   viene onorata da `_on_model_ready`, nel main loop via `GLib.idle_add`.

### Due bug trovati dopo: l'app non si apriva comunque

6. **`numpy` bloccava l'apertura della finestra.** `__main__` promette a
   video "L'app si apre comunque: cronologia, ricerca, esportazione e
   correzioni funzionano" — ed era **falso**: `app`, `asr` e `audio`
   importavano `numpy` in testa al modulo, quindi senza numpy
   `from lecture_transcriber.app import TranscriptionPipeline` sollevava
   `ImportError` e `__main__` cadeva sul proprio `try`. Il primo avvio
   (venv non ancora creato da `ensure-venv.sh`) non poteva aprire niente.
7. **`Segmenter.__init__` chiamava `np.zeros`.** Differire l'import non
   bastava: `np` restava `None` e il primo `np.zeros` diventava
   ``AttributeError: 'NoneType' object has no attribute 'zeros'``, che
   risaliva da `do_activate` fino a `app.run()`. Misurato dal vivo:
   `main()` stampava la promessa, poi moriva — **la finestra non
   compariva mai**, e l'utente non poteva consultare la cronologia ne'
   esportare. Ora `asr.require_asr_libs()` trasforma l'assenza in un
   `RuntimeError` che dice cosa installare, `open()` lo cattura e lo mette
   in `error_message`, e `start()`/`_ensure_model()` lo riportano invece di
   tacere.

Il filo conduttente dei due: **un errore lontano dalla sua causa**. Il
sintomo era una finestra che non si apriva; la causa stava in un import in
testa a un modulo. Per questo `test_launch_without_asr.py` gira in un
sotto-processo con un `MetaPathFinder` che rende i moduli *genuinamente*
assenti prima dell'import — nello stesso interprete i moduli veri sono
gia' in `sys.modules` e il test non proverebbe niente. Verificato che la
rete sia reale: rimettendo l'import in `app.py` 4 test su 6 falliscono.

### Due bug nel modulo di studio

8. **Un campo vuoto faceva scivolare l'indice della risposta del quiz.**
   `_split_fields` scartava i campi vuoti, quindi una riga con un'opzione
   in piu' spostava di un posto tutto il resto. Misurato:
   ``"Domanda || A || || B || 2"`` — la risposta giusta e' ``B``, indice
   ``2`` — dava ``options=('A','B')`` con ``answer=2``, cioe' **fuori
   range**: la domanda rispondeva ``A``. Il sintomo peggiore non e'
   l'errore, e' che la risposta sbagliata veniva presentata come giusta.
   Stesso difetto in `_parse_definitions`, dove ``"Termine || || def"``
   (definizione assente, ``def`` e' l'evidenza) usava ``def`` come
   definizione. Ora i campi vuoti restano al loro posto e le opzioni
   vuote vengono tolte **dopo** aver riparato l'indice.

9. **``"o"`` era trattato come prefisso di sinonimo.** In italiano ``o``
   introduce un'alternativa, non un sinonimo: misurato, "zero significa o
   assenza di valore" dava la definizione "assenza di valore", con la
   congiunzione — e il senso della frase — perduti. I sinonimi introdotti
   da "o" si dicono ``ovvero`` e ``ossia``, che sono gia' marcatori e
   vengono tolti dal marcatore. Rimosso da ``_ALIAS_PREFIXES``.

**Nota su come questi bug passavano i test.** I bug 3 e 4 erano invisibili
perche' i test usavano `mock`: `_find_editable` veniva sostituito con un
nodo finto e `active_window_title` con una stringa. I mock verificavano che
il codice *chiamasse* le cose giuste, ma non che le chiamate *funzionassero*
-- e il nome del metodo era sbagliato in un punto che nessun mock toccava.
La lezione e' che un `try/except` muto attorno a una libreria esterna
converte ogni errore in "niente da fare", e i test con mock non lo vedono.
Per questo `test_insertion.py` include ora sia test con nodi finti sia
verifiche dal vivo (sezione 6), e i finti sono costruiti con
`mock.MagicMock(spec=[...])`: se il codice chiama un metodo che il finto non
ha, il test fallisce invece di lasciar passare.

---

## 2. Accuratezza su italiano reale

Audio di prova generato con **espeak-ng** (`-v it`), non rumore simulato:
8 frasi, 58 s, silenzi di 1,0 s fra le frasi.

```bash
.venv/bin/python scripts/make_test_audio.py /tmp/lt_test_italian.wav
.venv/bin/python scripts/test_asr_italian.py --audio /tmp/lt_test_italian.wav
```

| Metrica | Valore |
|---|---|
| Segmenti rilevati | 9 (8 frasi + finale) |
| Confini su silenzio ≥ 0,7 s | **9/9** |
| Similarita' testuale (difflib) | **0,988** |
| **WER approssimato** | **1,0 %** (1 errore su 97 parole) |
| Picco RSS | 1,68 GiB |
| Caricamento modello | 2,95 s (dalla cache locale) |

Gli errori residui sono tipici del modello, non della pipeline:

- `settecento` → `700` (normalizzazione numerica);
- `puntinatura` → `punteggiatura` (accento).

**Caveat onesto:** espeak-ng e' una voce robotica. Il WER reale su un
professore con accento, in aula rumorosa, sara' peggiore. Questo test
dimostra la **correttezza della pipeline** (segmentazione, decodifica,
punteggiatura, contesto), non l'accuratezza assoluta. La misura definitiva
richiede una lezione vera — e' il test 9 della roadmap.

---

## 3. Latenza

Misurata con `large-v3-turbo` int8, CPU, 16 thread:

| Durata segmento | Tempo di decodifica | RTF |
|---|---|---|
| 3 s | 3,01 s | 1,01 |
| 5 s | 2,89 s | 0,58 |
| 10 s | 3,00 s | 0,30 |
| 20 s | 3,06 s | 0,15 |

**Il tempo di decodifica e' costante (~3 s)**: faster-whisper riempie sempre
l'audio fino alla finestra di 30 s. Nessun flag lo riduce
(`condition_on_previous_text`, `vad_filter`, `no_speech_threshold`,
`without_timestamps` danno tutti 2,98–3,06 s).

Conseguenze reali:

- la latenza di finalizzazione e' **~3 s** piu' l'attesa del VAD;
- il testo provvisorio non puo' girare in loop (3 s di CPU per volta): si
  decodifica solo nei primi secondi del segmento;
- per scendere sotto i 3 s serve un **modello piu' piccolo** (`medium`), non
  un'ottimizzazione dei parametri.

**Nota sulla concorrenza:** misurando il benchmark mentre altri processi
saturation il processore, la decodifica sale a 5,5 s. La latenza reale
**dipende dal carico della CPU**: con il processore libero sono ~3 s.

---

## 4. Consumo di risorse

| Risorsa | Valore misurato |
|---|---|
| RAM all'apertura della finestra, modello **non** caricato | **139 MB** |
| RAM (picco RSS, modello caricato) | 1,68 GiB |
| CPU durante la decodifica | ~375 % (≈4 core su 16) |
| Modello su disco | 1,6 GiB (cache HuggingFace) |
| Disco per una lezione di 1 h | ~40 KiB di SQLite |

Il consumo a riposo e' ora davvero trascurabile: il modello viene caricato
**al primo "ascolta"**, non all'avvio. Prima della correzione aprire la
finestra costava 1,1 GB di RSS (misurato), in violazione del requisito
"poche risorse quando inattivo".

---

## 5. Stabilita' e gestione degli errori

| Situazione | Comportamento |
|---|---|
| Microfono assente o negato | `AudioCapture.start()` solleva, l'app mostra l'errore e resta viva |
| Microfono scollegato a meta' lezione | il flusso audio si interrompe, l'ultimo testo resta salvato |
| Stream sospeso da PipeWire | si tiene uno stream continuo, quindi niente latenza di riattivazione |
| Coda di audio piena (ASR lento) | l'overrun viene **contato**, non si blocca il thread realtime e non si perde silenziosamente |
| Modello non scaricato | errore esplicito, l'app resta utilizzabile per consultare le sessioni |
| Nessun server LLM locale | il backend euristico prende il posto, senza errori |
| LibreOffice non in esecuzione | viene lanciato in background; se non c'e' un documento aperto, **non si scrive** |
| Focus cambiato durante l'inserimento | il segmento viene **saltato** con avviso |

---

## 6. Test di inserimento del testo

Verificati dal vivo su questa macchina:

| Percorso | Esito |
|---|---|
| LibreOffice Writer via UNO | ✅ connessione reale a `soffice` (0,8 s) e testo inserito **riletto dal documento** |
| Writer senza documento aperto | ✅ rifiutato correttamente ("Nessun documento Writer aperto") |
| at-spi su app GTK | ✅ **verificato end-to-end su `gnome-text-editor`**: testo passato da `at# nota di prova` a `at# nota di prova OK-FINALE`, riletto dal documento vivo |
| at-spi senza campi editabili | ✅ rifiutato ("Nessun campo di testo GTK attivo e modificabile") — nessuno scritto nel pannello GNOME |
| Obsidian / Electron | ⚠️ serve `ydotool` (non installato di default) |
| `wtype` | ❌ mutter non espone i globali necessari |

**Nota su come e' stato letto il testo:** `Atspi.Accessible.get_text()` in
PyGObject 3.52 e' deprecato e la sua firma `(self, arg1)` nasconde il metodo
D-Bus `Text.get_text(start, end)` -- chiamarlo con due argomenti solleva
`TypeError`. La lettura funzionante e' `get_text().get_string_at_offset(0,
Atspi.TextGranularity.LINE)`, che restituisce un `Atspi.TextRange` con
`.content`. Non serve all'inserimento (che usa `EditableText`), ma serve a
**verificarlo**.

---

## 7. Cosa non e' stato testato

- **Always-on-top**: richiede l'estensione GNOME Shell, che va installata e
  abilitata dall'utente; il meccanismo D-Bus e' implementato ma la verifica
  end-to-end richiede un riavvio della sessione. L'estensione chiede anche
  `stick()`/`unstick()` accanto a `make_above()`: `make_above()` da solo
  tiene la finestra sopra le altre **solo nello spazio di lavoro corrente**,
  cambiando workspace non e' piu' sopra niente. Verificato che `stick`,
  `unstick` e `on_all_workspaces` esistano nel typelib Meta-18 di mutter
  (grep binario), ma non che reggano un cambio di workspace dal vivo.
- **Click-through e blur**: impossibili su GNOME 50, non c'e' nulla da testare.
- **Scorciatoie globali**: la catena e' stata verificata end-to-end *eseguendo
  il comando* che `gnome-settings-daemon` eseguirebbe alla pressione del tasto
  (`gdbus call ... ActivateShortcut <id>`), non premendo fisicamente il tasto:
  la risposta e' arrivata e l'app ha risposto. Resta da provare la pressione
  fisica della combinazione, che dipende dal grab dell'acceleratore fatto da
  `gsd-media-keys` (e dal fatto che `gsd-media-keys` sia vivo: se il servizio
  e' crashed, non parte nessuna scorciatoia di GNOME).
- **at-spi su altre applicazioni GTK**: verificato su `gnome-text-editor`; il
  meccanismo e' generico (primo campo `EDITABLE`+`SHOWING` nell'albero), ma non
  e' stato provato su gedit, GNOME Builder o altri toolkit. Il caso Electron
  resta escluso per costruzione (espone un albero di sola lettura).
- **at-spi dall'applicazione reale**: la verifica end-to-end e' stata fatta
  lanciando l'inserimento da uno script separato mentre l'editor era aperto.
  Manca la prova con il flusso completo dell'app (microfono → ASR →
  inserimento), che richiede una lezione vera.
- **Precisione su parlato reale in aula**: richiede una registrazione vera.

### 7.1 Verifiche specifiche sulle scorciatoie

| Cosa | Come | Esito |
|---|---|---|
| Registrazione nel `media-keys` | `gsettings get ... custom-keybindings` | ✅ i 7 percorsi scritti |
| Contenuto di una scorciatoia | `gsettings get ...custom-keybinding:<path> binding` | ✅ `<Ctrl><Super><Shift>A` |
| Comando generato | `gsettings get ... command` | ✅ `gdbus call ... ActivateShortcut <id>` |
| Interfaccia D-Bus esposta | `gdbus introspect --dest it.lecturetranscriber.App --object-path /it/lecturetranscriber/App` | ✅ `it.lecturetranscriber.App.ActivateShortcut(s)` |
| Chiamata alla scorciatoia | `gdbus call ... ActivateShortcut toggle_window` | ✅ risposta `()` |
| Rimozione alla chiusura | quit dell'app, poi `gsettings get` | ✅ lista svuotata |
| Non tocca le scorciatoie dell'utente | `unregister()` con una voce `mio/` | ✅ la voce resta |
| Percorso non valido | `['@as []']` nella lista | ✅ ripulito, `gsd-media-keys` sopravvive |

Il caso del percorso non valido merita una riga: `gsd-media-keys` chiama
`g_settings_new_with_path()` senza validare il percorso, e con un percorso
non valido va in dereferenza di puntatore nullo — **SIGSEGV**, verificato
(rc=-11). Il crash e' stato causato da un parser che non scartava il
prefisso `@as ` che `gsettings get` a volte stampa. Ora il percorso viene
validato prima di ogni scrittura (`_is_valid_path`), e i test coprono il
caso.
