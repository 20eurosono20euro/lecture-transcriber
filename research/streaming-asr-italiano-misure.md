# Streaming ASR per l'italiano: misure locali (2026-10-22)

Tutte le misure qui sotto sono **eseguite su questa macchina**
(Fedora 44, GNOME 50.6 Wayland, 16 core, 14 GiB RAM) e sono riproducibili
con gli script in `scripts/`. Non sono citazioni: sono numeri misurati.

## La premessa: la claim "non esiste un ASR streaming italiano" è FALSA

Il workflow di fattibilità precedente (wf_a4573fc7-b65) sosteneva:

> "sherpa-onnx has NO streaming ASR model for Italian: its online
> transducer/Zipformer models cover only Chinese, English, Korean, French,
> and bilingual zh-en"

Questa claim **non regge**, per due motivi distinti:

1. **L'indice ufficiale** di sherpa-onnx elenca davvero solo `en`, `zh`, `ko`,
   `fr`, `de`, `es` per gli streaming Zipformer — lì la claim è corretta.
   Verificato su <https://k2-fsa.github.io/sherpa/onnx/pretrained_models/online-transducer/index.html>
   e con l'API HuggingFace su `csukuangfj/sherpa-onnx-streaming-zipformer-*`
   (33 modelli, lingue: `ctc de en es fr zh` — **nessuna `it`**).

2. **Esistono però modelli italiani**, pubblicati dalla comunità e non
   presenti nell'indice ufficiale:
   - `kouhxp/sherpa-onnx-streaming-zipformer-it-kroko` (creato 2026-06-15)
   - `R4kSo1997/sherpa-onnx-streaming-zipformer-it-kroko-2025-08-06`
     (creato 2026-05-26)

   Il tarball scaricato contiene `encoder.onnx`, `decoder.onnx`,
   `joiner.onnx` e un `tokens.txt` **italiano** (650 token SentencePiece con
   parole italiane: `▁di`, `▁che`, `▁in`…). Il file-modello contiene
   l'intestazione:

   ```json
   {"id": "5130c68a-...", "type": "zipformer2", "free": true,
    "language": {"id": 5, "name": "Italian", "iso": "IT"}}
   ```

   L'origine è **Kroko ASR** (<https://huggingface.co/Banafo/Kroko-ASR>),
   licenza **CC-BY-SA-4.0** per i modelli community, engine Apache-2.0.

> **Attenzione ai due rischi:** il README upstream dichiara il metadata
> `license_name: test` (un placeholder, non una licenza reale) e la pagina
> elenca fra le lingue supportate solo en/fr/de/es/pt — **non `it`**, benché il
> file `Kroko-IT-Community-128-L-Streaming-001.data` esista. I modelli hanno
> **0 download e 0 like**: nessuno li ha usati in produzione. Non c'è un
> WER pubblicato per l'italiano da parte del produttore.

### La licenza è ambigua — va risolta prima diShipping

Il file `LICENSE` del repository Kroko-ASR **scaricato è di 0 byte**
(`content-length: 0` su `resolve/main/LICENSE`), e il metadata dichiara
`license_name: test`. Il corpo del README dice:

> "Apache-2.0 engine. Models licensed separately (CC-BY-SA community or
> commercial OEM)."

e il README del modello repackageato dichiara:

> "Repackaged from Banafo/Kroko-ASR (CC-BY-SA-4.0) to the sherpa-onnx
> streaming Zipformer transducer layout."

Quindi **tre fonti dicono cose diverse** (LICENSE vuota / `test` /
CC-BY-SA-4.0). Per un progetto con licenza MIT questa è un ostacolo reale:
**CC-BY-SA è copyleft**, quindi un modello CC-BY-SA non può semplicemente
entrare in un'applicazione MIT senza deroghe o licenza separata. Inoltre
**CC-BY-SA-4.0 richiede attribuzione e la condivisione agli stessi termini**:
un modello così non è "un componente" ma un'opera incorporata.

**Azione richiesta prima di scegliere questo modello:** chiedere a Kroko
(kroko.ai, Discord) la licenza effettiva del modello **IT** e se esiste una
deroga per uso commerciale. In alternativa scegliere un percorso con
licenza chiara (Whisper → MIT, Vosk → Apache-2.0) e trattare lo Zipformer
come esperimento.

## Misure: streaming Zipformer vs Whisper batch, stesso audio, stessi riferimenti

Audio sintetico italiano (espeak-ng `-v it`), 16 kHz mono, 5 frasi identiche
per entrambi i motori.

| | streaming Zipformer (Kroko-IT) | faster-whisper large-v3-turbo int8 |
|---|---|---|
| **WER medio** | **19,1 %** | **7,0 %** |
| **CER medio** | 8,2 % | **1,5 %** |
| RTF aggregato | **0,017** | 0,685 |
| time-to-first-partial | **0,04 s** | n/d (batch) |
| punteggiatura e maiuscole | **no** | sì |
| modello | 124 MB (encoder 148 MB ONNX) | ~1,6 GB |

Riproduzione:

```bash
.venv/bin/python scripts/eval_it_streaming.py <model_dir> 4
.venv/bin/python scripts/eval_it_whisper.py
```

### Cosa significa davvero

- **Lo streaming Zipformer è 40× più veloce del tempo reale** (RTF 0,017) e
  restituisce il primo ipotesi in **40 millisecondi**. Questo è vero streaming:
  l'ipotesi provvisoria appare mentre il professore parla.
- **Ma è molto meno accurato**: WER 19 % contro 7 %. E non produce
  punteggiatura né maiuscole.
- **Il vantaggio di Whisper è che l'accuratezza si compra col prezzo di
  aspettare**: RTF 0,685 significa che per ogni secondo di audio servono
  ~0,7 s di CPU. Su una finestra di 30 s sono ~21 s di attesa, che è il
  motivo per cui il tempo di latenza reale dichiarato dal progetto è ~3 s
  e non subito.
- Nota: il WER qui è su **voce sintetica**, che è più facile di un
  professore reale. I numeri reali in aula saranno peggiori per entrambi.

## Difetto reale trovato: l'endpoint detection di sherpa-onnx 1.13.8 va in crash

Con `enable_endpoint_detection=True` il processo **aborta** su questo modello:

```
/project/sherpa-onnx/csrc/features.cc:GetFrames:188 256 + 269 > 279
```

Ho isolato la causa con una matrice di test: il fallimento dipende **solo**
da `enable_endpoint_detection=True` (con `rule3` a 5/10/20/30 s, con e senza
`reset()`, con chunk da 0,4 s o 1 s). Senza endpoint detection tutto
funziona. Quindi:

- **non usare l'endpoint detection integrato** con i modelli Zipformer2
  repackageati; segmentare con un **VAD proprio** (l'app ne ha già uno a
  inviluppo RMS) e alimentare un segmento per stream;
- chiudere ogni stream con **silenzio finale** (~2 s): un trasduttore
  committa la coda dell'utto solo quando "sente" il silenzio, e senza quel
  flush la trascrizione resta troncata (misurato: "convoluzionali" → "con evo").

## Implicazione per l'architettura

Non sono due alternative equivalenti, ma due ruoli diversi:

- **Zipformer streaming** → il testo provvisorio che scorre mentre parli
  (latenza 40 ms), e un **rivelatore di parole chiave** a bassissimo costo.
- **Whisper batch** → il testo definitivo da mostrare e da inserire nel
  documento (accuratezza 7 % WER, punteggiatura, maiuscole).

Un'architettura ibrida — streaming per il provvisorio, Whisper per il
definitivo — è misurabile su questa macchina e resta sotto i ~6 GiB di RAM
(124 MB + 1,6 GB). Va però verificato che il tempo totale di CPU dei due
motori insieme stia nei 16 core durante una lezione reale.
