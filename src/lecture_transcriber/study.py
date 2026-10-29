"""Assistente di studio locale (opzionale) sulle lezioni trascritte.

Il modulo e' **facoltativo e spento per impostazione predefinita**: nessuna
funzione viene eseguita finche' l'utente non la richiama esplicitamente e non
attiva il modulo (:meth:`StudyAssistant.enable`). E' pensato per il
riassunto di una lezione gia' finita, non per il flusso di trascrizione.

Vincoli di progetto:

* **tutto locale**: il funzionamento di base non richiede alcun servizio
  remoto e non effettua chiamate di rete all'importazione del modulo. Se e'
  attivo un modello locale (server llama.cpp con API OpenAI-compatible su
  127.0.0.1) lo si puo' usare per migliorare i risultati, ma e' solo un
  *miglioramento*: il backend euristico funziona sempre, offline;
* **pluggable**: il contratto minimo e' :class:`StudyBackend`
  (``generate(task, prompt) -> str``). Ogni backend risponde in formati
  canonici separati da ``||``, cosi' l'assistente puo' interpretare
  l'output di un LLM locale o quello del backend euristico con lo stesso
  codice di parsing;
* **nessuna perdita**: le correzioni manuali del testo sono tenute
  *accanto* all'originale e sono reversibili.

Componenti:

``StudyConfig``
    Preferenze persistenti (attivo, backend, endpoint del modello locale).
``LocalLLMBackend``
    Client HTTP (solo stdlib) di un server llama.cpp-style su 127.0.0.1.
``HeuristicBackend``
    Backend di riserva, senza dipendenze: lavora sul testo della lezione con
    statistiche lessicali e pattern linguistici italiani.
``StudyAssistant``
    Facciata: sceglie il backend ed espone riassunto, concetti, definizioni,
    flashcard e quiz.
``CustomDictionary``
    Dizionario personale (termini tecnici, nomi propri) salvato in locale,
    con un prompt compatto da usare come ``initial_prompt`` di faster-whisper.
``TranscriptCorrector``
    Coppie (originale, correzione) per segmento, persistenti e reversibili.
"""

from __future__ import annotations

import json
import math
import random
import re
import time
import unicodedata
import urllib.error
import urllib.request
import zlib
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Protocol, Sequence, runtime_checkable

from . import config as _config
from .sessions import SessionStore

__all__ = [
    "Concept",
    "Correction",
    "CustomDictionary",
    "Definition",
    "DictionaryEntry",
    "Flashcard",
    "HeuristicBackend",
    "LocalLLMBackend",
    "QuizQuestion",
    "StudyAssistant",
    "StudyBackend",
    "StudyConfig",
    "StudyMaterial",
    "TranscriptCorrector",
    "load_study_config",
    "save_study_config",
    "session_transcript",
]


# ---------------------------------------------------------------------------
# percorsi locali
# ---------------------------------------------------------------------------
def _data_dir() -> Path:
    """Directory dati corrente (ricalcolata a ogni chiamata, per i test)."""
    return Path(_config.DATA_DIR)


def _default_dictionary_path() -> Path:
    return _data_dir() / "dictionary.json"


def _default_corrections_path() -> Path:
    return _data_dir() / "corrections.json"


def _default_config_path() -> Path:
    return _data_dir() / "study.json"


def _default_material_path(session_id: int) -> Path:
    return _data_dir() / "study" / f"session_{session_id}.json"


def _write_json_atomic(path: Path, payload: Any) -> None:
    """Scrive JSON in modo atomico (file temporaneo + replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(path)
    except OSError:
        # il disco puo' essere pieno o la directory sola-lettura: un
        # salvataggio fallito non deve interrompere chi stava usando
        # l'applicazione. Il file temporaneo resta, ma non e' un dato
        # che l'utente vede.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _read_json(path: Path) -> Any:
    """Legge un file JSON, tornando ``None`` se illeggibile o non-JSON.

    ``UnicodeDecodeError`` va catturata perche' *non* e' una
    sottoclasse di ``OSError`` ne' di ``JSONDecodeError``: un file
    salvato da un'altra applicazione (o da una versione con un'altra
    codifica) faceva propagare l'errore fino al costruttore del
    dizionario e delle correzioni, che chiamano questo metodo in
    ``__init__``. Misurato: con ``{"it": "caff\xe8"}`` (utf-8 non valido)
    ``PersonalDictionary(...)`` sollevava invece di ignorare il file.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # ValueError copre UnicodeDecodeError e JSONDecodeError
        return None


# ---------------------------------------------------------------------------
# tipi di studio
# ---------------------------------------------------------------------------
def _safe_int(value: Any, default: int = 0) -> int:
    """Converte in intero senza sollevare.

    I file JSON di questo modulo possono essere editati a mano o scritti da
    una versione diversa: ``int("dodici")`` o ``int(None)`` sollevano, e
    ``from_dict`` deve degradare, non crashare.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Come :func:`_safe_int`, per i numeri reali."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Concept:
    """Un concetto principale, con la frase della lezione che lo sostiene."""

    term: str
    evidence: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"term": self.term, "evidence": self.evidence}

    def __str__(self) -> str:
        return f"{self.term}: {self.evidence}" if self.evidence else self.term


@dataclass(frozen=True)
class Definition:
    """Una definizione estratta dal testo."""

    term: str
    definition: str
    evidence: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "term": self.term,
            "definition": self.definition,
            "evidence": self.evidence,
        }

    def __str__(self) -> str:
        return f"{self.term} — {self.definition}"


@dataclass(frozen=True)
class Flashcard:
    """Una flashcard: fronte e retro."""

    front: str
    back: str
    kind: str = "cloze"  # cloze | definizione | aperta

    def to_dict(self) -> dict[str, str]:
        return {"front": self.front, "back": self.back, "kind": self.kind}

    def __str__(self) -> str:
        return f"{self.front} -> {self.back}"


def _option_label(index: int) -> str:
    """Etichetta dell'opzione: ``A``, ``B``, ... ``Z``, ``AA``, ``AB``.

    ``chr(65 + n)`` andava bene solo fino alla ventiseiesima opzione:
    dalla ventisettesima in poi produceva ``[``, ``\\``, ``]`` —
    misurato, una domanda con 30 opzioni (output di un modello locale)
    mostrava ``[risposta: []]``. L'alternativa `AA` e' la convenzione dei
    fogli di lavoro, non un'invenzione.
    """
    index = int(index)
    if index < 0:
        index = 0
    label = ""
    index += 1
    while index > 0:
        index, rem = divmod(index - 1, 26)
        label = chr(65 + rem) + label
    return label


@dataclass(frozen=True)
class QuizQuestion:
    """Una domanda di riposo a scelta multipla."""

    question: str
    options: tuple[str, ...]
    answer: int  # indice 0-based nelle options
    explanation: str = ""

    @property
    def answer_text(self) -> str:
        if 0 <= self.answer < len(self.options):
            return self.options[self.answer]
        return ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "options": list(self.options),
            "answer": self.answer,
            "answer_text": self.answer_text,
            "explanation": self.explanation,
        }

    def __str__(self) -> str:
        opts = " ".join(f"{_option_label(i)}) {o}" for i, o in enumerate(self.options))
        return f"{self.question} {opts} [risposta: {_option_label(self.answer)}]"


@dataclass
class StudyMaterial:
    """Il pacchetto di studio completo di una lezione."""

    summary: str = ""
    concepts: list[Concept] = field(default_factory=list)
    definitions: list[Definition] = field(default_factory=list)
    flashcards: list[Flashcard] = field(default_factory=list)
    quiz: list[QuizQuestion] = field(default_factory=list)
    session_id: int | None = None
    backend: str = ""
    created_at: float = 0.0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "concepts": [c.to_dict() for c in self.concepts],
            "definitions": [d.to_dict() for d in self.definitions],
            "flashcards": [f.to_dict() for f in self.flashcards],
            "quiz": [q.to_dict() for q in self.quiz],
            "session_id": self.session_id,
            "backend": self.backend,
            "created_at": self.created_at,
            "warnings": list(self.warnings),
        }

    @classmethod
    def _items(cls, data: dict[str, Any], key: str) -> list[Any]:
        """Le voci di una lista, scartando quello che non e' un dict.

        ``from_dict`` legge un file che l'utente puo' aver editato a mano o
        che una versione precedente ha scritto in un altro formato: senza
        questo filtro ``data["concepts"] = ["una stringa"]`` sollevava
        ``AttributeError`` su ``.get`` — misurato: riaprire un materiale
        salvato crashava invece di degradare.
        """
        raw = data.get(key, [])
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, dict)]

    @classmethod
    def _strings(cls, data: dict[str, Any], key: str) -> list[str]:
        """Una lista di stringhe, o vuota se il campo non e' una lista."""
        raw = data.get(key, [])
        if not isinstance(raw, list):
            return []
        return [str(item) for item in raw]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StudyMaterial":
        return cls(
            summary=str(data.get("summary", "")),
            concepts=[
                Concept(str(c.get("term", "")), str(c.get("evidence", "")))
                for c in cls._items(data, "concepts")
            ],
            definitions=[
                Definition(
                    str(d.get("term", "")),
                    str(d.get("definition", "")),
                    str(d.get("evidence", "")),
                )
                for d in cls._items(data, "definitions")
            ],
            flashcards=[
                Flashcard(
                    str(f.get("front", "")),
                    str(f.get("back", "")),
                    str(f.get("kind", "cloze")),
                )
                for f in cls._items(data, "flashcards")
            ],
            quiz=[
                QuizQuestion(
                    str(q.get("question", "")),
                    tuple(str(o) for o in q.get("options", [])),
                    _safe_int(q.get("answer", 0)),
                    str(q.get("explanation", "")),
                )
                for q in cls._items(data, "quiz")
            ],
            session_id=data.get("session_id"),
            backend=str(data.get("backend", "")),
            created_at=_safe_float(data.get("created_at", 0.0)),
            warnings=cls._strings(data, "warnings"),
        )

    def save(self, path: Path | None = None) -> Path:
        """Salva il pacchetto su disco per rivederlo in seguito."""
        target = Path(path) if path else _default_material_path(self.session_id or 0)
        _write_json_atomic(target, self.to_dict())
        return target

    @classmethod
    def load(cls, path: Path) -> "StudyMaterial | None":
        data = _read_json(Path(path))
        if not isinstance(data, dict):
            return None
        return cls.from_dict(data)


# ---------------------------------------------------------------------------
# backend: contratto e implementazioni
# ---------------------------------------------------------------------------
@runtime_checkable
class StudyBackend(Protocol):
    """Contratto minimo di un backend di studio.

    ``task`` e' uno fra ``summary``, ``concepts``, ``definitions``,
    ``flashcards``, ``quiz``. ``prompt`` contiene l'istruzione e la
    trascrizione. Il testo restituito usa il formato canonico della task
    (righe separate da ``||``).
    """

    name: str

    def generate(self, task: str, prompt: str) -> str:
        """Restituisce il testo generato per la task richiesta."""
        ...


#: Istruzioni date al backend LLM per ogni task.
TASK_INSTRUCTIONS: dict[str, str] = {
    "summary": (
        "Scrivi un riassunto in italiano della lezione, fra 4 e 8 frasi, "
        "solo con gli argomenti principali. Rispondi con il riassunto e "
        "nient'altro."
    ),
    "concepts": (
        "Elenca i concetti principali della lezione (al massimo 10). "
        "Una riga per concetto, nel formato esatto: termine || frase della "
        "lezione che spiega il termine."
    ),
    "definitions": (
        "Estrai le definizioni presenti nella lezione (al massimo 12). "
        "Una riga per definizione, nel formato esatto: termine || definizione."
    ),
    "flashcards": (
        "Crea 12 flashcard di ripasso. Una riga per flashcard, nel formato "
        "esatto: domanda || risposta breve."
    ),
    "quiz": (
        "Crea 5 domande di riposo a scelta multipla. Una riga per domanda, "
        "nel formato esatto: domanda || opzione A || opzione B || opzione C || "
        "opzione D || indice della risposta (0-3, zero-based)."
    ),
}

TRANSCRIPT_BEGIN = "### TRASCRIZIONE ###"
TRANSCRIPT_END = "### FINE TRASCRIZIONE ###"

_TRANSCRIPT_RE = re.compile(
    re.escape(TRANSCRIPT_BEGIN) + r"\n(.*?)\n" + re.escape(TRANSCRIPT_END), re.S
)


def build_prompt(task: str, transcript: str, *, max_chars: int = 12000) -> str:
    """Compone il prompt (istruzione + trascrizione delimitata)."""
    instruction = TASK_INSTRUCTIONS.get(task, "Analizza la lezione.")
    body = transcript or ""
    truncated = False
    if max_chars > 0 and len(body) > max_chars:
        body = body[:max_chars]
        truncated = True
    note = "\n[troncato: la lezione e' piu' lunga del limite]" if truncated else ""
    return f"{instruction}\n\n{TRANSCRIPT_BEGIN}\n{body}{note}\n{TRANSCRIPT_END}\n"


def extract_transcript(prompt: str) -> str:
    """Estrae la trascrizione da un prompt.

    Se i delimitatori completi non ci sono si ripiega sul testo che segue
    il marcatore d'inizio; se manca anche quello si usa il testo intero
    ripulito dai marcatori. In questo modo l'istruzione ("scrivi un
    riassunto") non finisce mai dentro la lezione analizzata.
    """
    text = prompt or ""
    match = _TRANSCRIPT_RE.search(text)
    if match:
        return match.group(1).strip()
    begin = text.find(TRANSCRIPT_BEGIN)
    if begin >= 0:
        tail = text[begin + len(TRANSCRIPT_BEGIN) :]
        end = tail.find(TRANSCRIPT_END)
        if end >= 0:
            tail = tail[:end]
        return tail.strip()
    cleaned = text.replace(TRANSCRIPT_BEGIN, " ").replace(TRANSCRIPT_END, " ")
    return re.sub(r"\n{2,}", "\n", cleaned).strip()


class LocalLLMBackend:
    """Backend che parla con un server LLM locale in stile llama.cpp.

    L'API e' quella OpenAI-compatible (``POST {endpoint}/chat/completions``),
    quindi funziona con ``llama-server``, ``llama.cpp`` e con server
    analoghi che espongono ``/v1``. Per motivi di riservatezza il backend
    **rifiuta** endpoint non locali, salvo esplicito ``allow_remote=True``:
    le trascrizioni delle lezioni non devono uscire dalla macchina.

    In caso di errore non solleva eccezioni: ``generate`` restituisce la
    stringa vuota e l'errore resta disponibile in :attr:`last_error`.
    """

    name = "local-llm"

    def __init__(
        self,
        endpoint: str = "http://127.0.0.1:8080/v1",
        model: str = "local-model",
        *,
        timeout: float = 60.0,
        temperature: float = 0.2,
        max_tokens: int = 800,
        allow_remote: bool = False,
    ) -> None:
        self.endpoint = (endpoint or "").rstrip("/")
        self.model = model
        self.timeout = timeout
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.allow_remote = allow_remote
        self.last_error: str | None = None

    # -- rete --------------------------------------------------------------
    def is_local(self) -> bool:
        """True se l'endpoint punta alla macchina locale.

        L'host vuoto **non** e' locale: era nell'insieme, e con lui
        passavano il test anche ``""``, ``"http://"``, ``"not-a-url"`` e
        — il caso che conta — ``"file:///etc/passwd"``, perche'
        ``urlparse`` non ci trova un hostname. Misurato: quel endpoint
        veniva classificato locale, superava il guardiano della privacy e
        ``urlopen`` avrebbe aperto il file. Un endpoint vuoto o illeggibile
        e' un endpoint **non** valido, non un permesso.

        Restano locali solo host esplicitamente di loopback. Gli indirizzi
        privati (192.168.x, 10.x) **non** lo sono: sono un'altra macchina
        della rete, e il §8 vuole che nessun dato esca dal computer.
        """
        try:
            from urllib.parse import urlparse

            parsed = urlparse(self.endpoint)
        except Exception:
            return False
        if parsed.scheme and parsed.scheme not in ("http", "https"):
            return False
        host = (parsed.hostname or "").lower()
        if not host:
            return False
        return host in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}

    def available(self, *, timeout: float = 1.5) -> bool:
        """Verifica se il server risponde (chiamata locale, mai all'import)."""
        if not self.endpoint:
            return False
        if not self.allow_remote and not self.is_local():
            self.last_error = (
                f"endpoint non locale ({self.endpoint}): rifiutato per privacy"
            )
            return False
        url = f"{self.endpoint}/models"
        try:
            request = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return 200 <= int(response.status) < 300
        except urllib.error.HTTPError as exc:
            # un /models non implementato non significa server morto
            self.last_error = f"HTTP {exc.code} su {url}"
            return exc.code in {404, 405}
        except Exception as exc:
            self.last_error = f"server non raggiungibile: {exc}"
            return False

    # -- generazione -------------------------------------------------------
    def generate(self, task: str, prompt: str) -> str:
        """Chiede al modello locale la task richiesta; stringa vuota su errore."""
        self.last_error = None
        if not self.endpoint:
            self.last_error = "endpoint non configurato"
            return ""
        if not self.allow_remote and not self.is_local():
            self.last_error = (
                f"endpoint non locale ({self.endpoint}): rifiutato per privacy"
            )
            return ""

        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Sei un assistente che studia trascrizioni di lezioni "
                        "universitarie in italiano. Rispondi in italiano e "
                        "rispetta esattamente il formato richiesto."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.endpoint}/chat/completions",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
        except Exception as exc:
            self.last_error = f"richiesta fallita: {exc}"
            return ""

        try:
            parsed = json.loads(body)
            content = parsed["choices"][0]["message"]["content"]
        except Exception as exc:
            self.last_error = f"risposta non interpretabile: {exc}"
            return ""
        if not isinstance(content, str):
            self.last_error = "risposta non testuale"
            return ""
        return content.strip()


# ---------------------------------------------------------------------------
# analisi lessicale del testo
# ---------------------------------------------------------------------------
#: Funzione grammaticali italiane: non sono concetti.
STOPWORDS: frozenset[str] = frozenset(
    """
    a ad affinche agl agli ahime ahime al alla alle allo altri altro anche
    ancora anni anno avere aveva avevano avra avrebbe c che chi ci cioe come
    comunque con concernente contro cosa cosi cui da dagl dagli dai dal dall
    dalla dalle dallo della delle degli dello dei del nel nei negli nell
    dentro di dietro dopo
    ella elle entro era erano essendo esse essere essi fa fare fatto fin
    finalmente fino fosse fra fu gli grande ha hai hanno ho i il in infatti
    insieme intanto invece io la le lei li lo loro lui ma me mentre mia mie
    mio modo molta molti molto ne nel nell nella nelle nello nei negli
    nemmeno nessuno niente no noi non nostra nostre nostro nulla o ogni oltre
    oppure ora ossia ovvero per perche percio pero piu poco poi prima proprio
    qua quale quali quando quanto quasi quel quella quelle quello questa
    queste questi questo qui quindi sara sarebbe se secondo sembra sempre
    senza si sia siamo solo sono sopra sotto sta stato stesso su sua sue
    sugli sui sul sull sulla sulle suo suoi tale tali tra tre troppo tu tua
    tue tuo tutti tutto un una uno va vi voi vostra vostre vostro e gia cio
    oggi pero gia cosi affinche attorno sopra sotto dentro fuori verso presso
    durante contro attraverso lungo ancora gia sempre mai quasi pero
    """.split()
)

#: Saluti e formule di apertura: non sono concetti.
GREETINGS: frozenset[str] = frozenset(
    """
    benvenuti benvenute benvenuto ciao buongiorno buonasera buon pomeriggio
    arrivederci arrivederci a presto grazie grazie mille
    """.split()
)

#: Verbi frequenti (e forme più diffuse): esclusi dai concetti.
#:
#: Sono le forme che compaiono davvero nelle lezioni italiane: senza
#: questo elenco i termini chiave si riempiono di coppie come
#: "regola che lega" o "varianza e' definita", dove la seconda parola e'
#: un verbo e non un concetto.
GENERIC_VERBS: frozenset[str] = frozenset(
    """
    abbia abbiamo avere avuto avendo abbiavo
    sia siamo siano essere stato stata stati state fossero
    face facciamo fanno fare fatto facendo fecero
    dico diciamo dicono dire detto dicendo disse
    vado andiamo vanno andare andato andava
    vengo veniamo vengono venire venuto
    diamo danno dare dato dando dava
    sappiamo sanno sapere sa
    possiamo possono potere potuto puo
    dobbiamo devono dovere dovuto deve
    vogliamo vogliono volere voluto vuole
    metto mettiamo mettono mettere messo mette
    prendo prendiamo prendono prendere preso prende
    uso usiamo usano usare usato usa
    permette permettono permettere permesso
    introduciamo introduce introducono introdurre introdotto
    aggiorna aggiornano aggiornare aggiornata aggiorniamo
    significa significano significare significa
    indica indicano indicare indicato indica
    chiamiamo chiama chiamano chiamare chiamato chiama
    definiamo definisce definiscono definire definito definita
    capiamo capisce capiscono capire capito
    vediamo vede vedono vedere visto vede
    guardiamo guarda guardano guardare
    restiamo resta restare
    torniamo torna tornare
    arriviamo arriva arrivare
    finiamo finisce finire finito
    continuiamo continua continuare
    ricordiamo ricorda ricordare
    spieghiamo spiega spiegano spiegare spiegato
    dimostriamo dimostra dimostrano dimostrare dimostrato
    notiamo nota notano notare noto nota
    supponiamo suppone supporre supposto
    assumiamo assume assumere
    concludiamo conclude concludere concluso
    analizziamo analizza analizzare analisi analizziamo
    consideriamo considera considerare considerato
    calcoliamo calcola calcolare calcolo
    misuriamo misura misurare misura misurato
    stimiamo stima stimare stima stimato
    confrontiamo confronta confrontare
    parliamo parla parlano parlare parlato
    studiamo studia studiare
    osserviamo osserva osservare osservato
    ricaviamo ricava ricavare
    aggiungiamo aggiunge aggiungono aggiungere aggiunto
    deriviamo deriva derivano derivare derivato
    seguiamo segue seguire seguito
    applichiamo applica applicare applicato
    deduciamo deduce dedurre dedotto
    chiediamo chiede chiedere chiesto
    mostriamo mostra mostrare mostrato
    legano lega legare legato
    serve servono servire servito
    aiuta aiutano aiutare aiuto
    migliora migliorano migliorare miglioramento
    include includono includere incluso
    contiene contengono contenere contenuto
    consiste consistono consistere
    dipende dipendono dipendere dipendente
    segue seguono seguire
    vale valgono valere valido
    vuol vuole
    puo puo
    converge convergono convergere convergo
    """.split()
)

#: Abbreviazioni che NON segnano la fine di una frase.
ABBREVIATIONS: frozenset[str] = frozenset(
    """
    sig prof dott ing avv arch ecc es pag sez vol n nr ca p mo
    all fra
    """.split()
)

#: Suffissi tipici dei verbi: una parola che li ha non e' un concetto.
VERB_SUFFIXES: tuple[str, ...] = ("iamo", "ando", "endo", "ire")

#: Parole che possono stare *fra* le due parti di una collocazione
#: ("teorema *di* Bayes"). Piu' stretta di STOPWORDS: se ammettessimo
#: qualsiasi funzione, "diagnostica medica, *nei* filtri" diventerebbe
#: un unico termine.
BRIDGE_WORDS: frozenset[str] = frozenset(
    [
        "di",
        "del",
        "della",
        "dei",
        "delle",
        "dello",
        "a",
        "al",
        "alla",
        "in",
        "per",
        "con",
        "e",
        "e",
    ]
)

#: Pattern che introducono una definizione.
DEFINITION_MARKERS: tuple[str, ...] = (
    "è definito come",
    "è definita come",
    "è indicato come",
    "è indicata come",
    "viene definito come",
    "viene definita come",
    "si definisce come",
    "si definisce",
    "è chiamato",
    "è chiamata",
    "detto anche",
    "detta anche",
    "noto come",
    "nota come",
    "denominato",
    "denominata",
    "significa",
    "significano",
    "ovvero",
    "ossia",
    "per definizione",
    "consiste in",
    "consistono in",
    "corrisponde a",
    "corrispondono a",
    "vale a dire",
    "si intende",
    "si intendono",
    "si parla di",
    "indica",
    "indicano",
    "chiamiamo",
)

#: Come scrivere un markeraccentato in modo che matchi anche la versione
#: ASCII usata spesso nelle trascrizioni ("e' definito" invece di "e' ...").
_ELISION_PATTERNS: dict[str, str] = {
    "è": r"(?:è|e'|é|e\")",
    "é": r"(?:é|e')",
    "à": r"(?:à|a')",
    "ò": r"(?:ò|o')",
    "ù": r"(?:ù|u')",
    "ì": r"(?:ì|i')",
}

#: Elementi da togliere all'inizio di un termine.
_LEADING_TRIM: frozenset[str] = frozenset(
    [
        "il",
        "lo",
        "la",
        "le",
        "i",
        "gli",
        "un",
        "uno",
        "una",
        "questo",
        "questa",
        "questi",
        "queste",
        "quel",
        "quella",
        "quei",
        "quelle",
        "l",
        "un",
        "che",
        "come",
        "quale",
        "cosa",
        "dove",
        "quando",
        "perché",
    ]
)

#: Elementi da togliere alla fine di un termine.
_TRAILING_TRIM: frozenset[str] = frozenset(
    [
        "di",
        "a",
        "al",
        "alla",
        "ai",
        "dei",
        "del",
        "della",
        "delle",
        "in",
        "per",
        "con",
        "da",
        "dal",
        "dalla",
        "su",
        "sul",
        "sulla",
        "tra",
        "fra",
        "e",
        "o",
        "ma",
        "che",
        "cui",
        "se",
        "non",
        "come",
        "dove",
        "quando",
        "ossia",
        "quindi",
        "cioè",
    ]
)

#: Copule e verbi ausiliari da togliere in coda a un termine.
_TRAILING_COPULA: frozenset[str] = frozenset(
    ["e", "e'", "sono", "si", "ci", "è", "siamo", "hanno", "abbiamo"]
)

_WORD_RE = re.compile(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'’\-]*")
_NUM_RE = re.compile(r"^\d+$")
_BULLET_RE = re.compile(r"^\s*(?:[-*•–—]|\d+[.)])\s*")
_PUNCT_EDGE_RE = re.compile(r"^[^A-Za-zÀ-ÿ0-9]+|[^A-Za-zÀ-ÿ0-9]+$", re.UNICODE)
#: Timestamp iniziale "[MM:SS]" o "[HH:MM:SS]": va tolto prima dell'analisi.
_TIMESTAMP_RE = re.compile(r"\[(?:\d{1,2}:)?\d{1,2}:\d{2}(?:[.,]\d{1,3})?\]")
_ARTICLES_PREFIX: tuple[str, ...] = (
    "dell'",
    "nell'",
    "sull'",
    "coll'",
    "pell'",
    "all'",
    "l'",
    "un'",
    "d'",
)


def _fold(word: str) -> str:
    """Normalizza una parola per il confronto: niente accenti, niente apostrofi.

    L'apostrofo finale italiano ("probabilita'", "un'") e' un segno di
    elisione, non parte della parola: viene rimosso.
    """
    decomposed = unicodedata.normalize("NFD", word.lower())
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    stripped = stripped.replace("'", "").replace("’", "").replace('"', "")
    return stripped


def _marker_regex(marker: str) -> re.Pattern[str]:
    """Compila un marker in un pattern che tollera l'elisione ASCII.

    ``re.IGNORECASE`` non e' un vezzo: senza, tutti i marker che possono
    aprire una frase ("Per definizione...", "Significa che...", "Si
    intende...", "Ovvero...") non venivano mai riconosciuti — misurato:
    ``_find_marker("Per definizione la varianza e' ...")`` restituiva
    ``None``. Una frase su cinque in una trascrizione inizia con il
    marker, perche' l'ASR spezza la frase li'.
    """
    parts: list[str] = []
    for char in marker:
        parts.append(_ELISION_PATTERNS.get(char, re.escape(char)))
    body = "".join(parts).replace(r"\ ", r"\s+")
    return re.compile(
        r"(?<![A-Za-zÀ-ÿ])" + body + r"(?![A-Za-zÀ-ÿ])", re.IGNORECASE
    )


_DEFINITION_RES: tuple[re.Pattern[str], ...] = tuple(
    _marker_regex(marker) for marker in DEFINITION_MARKERS
)


def _strip_timestamps(text: str) -> str:
    """Toglie i timestamp "[MM:SS]" dal testo (marcano l'inizio dei segmenti)."""
    return _TIMESTAMP_RE.sub(" ", text or "")


def _split_sentences(text: str) -> list[str]:
    """Segmenta il testo in frasi, rispettando le abbreviazioni italiane."""
    flat = re.sub(r"\s+", " ", _strip_timestamps(text or "")).strip()
    if not flat:
        return []
    sentences: list[str] = []
    # Due cursori, non uno. ``pending`` e' dove inizia la frase da
    # consegnare; ``probe`` e' da dove si misurano le parole per la regola
    # delle abbreviazioni. Divergono quando un punto risulta essere
    # un'abbreviazione: il testo dell'abbreviazione deve restare nella
    # frase successiva (con un solo cursore arretrato al punto, "Il Prof.
    # Rossi arriva." diventava ". Rossi arriva."), ma le parole vanno
    # misurate **dopo** di essa — con un solo cursore la sigla precedente
    # entrava nel conto e una sigla in una frase decideva per quella dopo.
    # Su input realistici le due versioni danno lo stesso risultato
    # (verificato), quindi questo e' un irrobustimento, non la correzione
    # di un difetto misurato: i test in ``test_study.py`` fissano il
    # comportamento, non una regressione.
    pending = 0
    probe = 0
    for match in re.finditer(r"([.!?]+)(?=\s|$)", flat):
        before = flat[probe : match.start()]
        words = [_fold(w) for w in _WORD_RE.findall(before)]
        if match.group(1) == "." and _looks_like_abbreviation(words):
            probe = match.end()
            continue  # abbreviazione: non e' fine frase
        chunk = flat[pending : match.end()].strip()
        if chunk:
            sentences.append(chunk)
        pending = match.end()
        probe = match.end()
    tail = flat[pending:].strip()
    if tail:
        sentences.append(tail)
    return sentences


def _looks_like_abbreviation(words: list[str]) -> bool:
    """True se l'ultima parola e' un'abbreviazione (Sig. / Prof. / ecc.)."""
    if not words:
        return False
    last = words[-1]
    if last in ABBREVIATIONS:
        return True
    # sigle brevi: "c.a", "a.c", "n.p"
    if len(last) <= 3 and len(words) >= 2 and len(words[-2]) <= 3:
        return True
    return False


def _words(text: str) -> list[str]:
    """Token alfabetici minuscoli, con articoli elisi normalizzati."""
    out: list[str] = []
    for raw in _WORD_RE.findall(text or ""):
        word = raw.lower()
        for prefix in _ARTICLES_PREFIX:
            if word.startswith(prefix) and len(word) > len(prefix) + 1:
                word = word[len(prefix) :]
                break
        out.append(word)
    return out


def _folded_words(text: str) -> list[str]:
    """Token normalizzati (senza accenti né apostrofi)."""
    return [_fold(w) for w in _words(text)]


def _is_content(word: str) -> bool:
    """Un token e' un termine di contenuto se puo' essere un concetto."""
    if len(word) < 3 or _NUM_RE.match(word):
        return False
    folded = _fold(word)
    if folded in STOPWORDS or folded in GENERIC_VERBS or folded in GREETINGS:
        return False
    if folded.endswith(VERB_SUFFIXES):
        return False
    return any(ch.isalpha() for ch in word)


def _content_words(text: str) -> list[str]:
    return [w for w in _words(text) if _is_content(w)]


def _looks_like_noun(word: str) -> bool:
    """True se il termine ha l'aspetto di un sostantivo (non di un verbo).

    La regola e' negativa: si escludono i verbi noti e le forme verbali
    riconoscibili dal suffisso. Il resto e' un termine potenziale.
    """
    folded = _fold(word)
    if folded in GENERIC_VERBS:
        return False
    if folded.endswith(VERB_SUFFIXES):
        return False
    return True


def _key_terms(
    sentences: Sequence[str],
    limit: int = 12,
    keep: Iterable[str] = (),
) -> list[tuple[tuple[str, ...], int]]:
    """Termini chiave (parole e coppie) con la loro frequenza.

    Restituisce coppie ``(sequenza di parole, frequenza)``. Le coppie di
    parole ("teorema di Bayes") sono preferite alle parole singole: sono
    concetti, non ripetizioni.

    Una coppia e' accettata se:

    * compare almeno due volte; oppure
    * e' **esclusiva**: la seconda parola compare in tutto il testo solo
      in quella posizione ("condizionata" sta sempre dopo "probabilita'").
      In questo caso l'accoppiamento non e' casuale e il termine e' un
      concetto, anche se la coppia appare una volta sola.

    Una parola singola viene accettata solo se compare almeno due volte:
    vista una volta sola e' spesso un aggettivo di passaggio ("nuova").
    I termini in ``keep`` (di norma quelli estratti dalle definizioni)
    sono sempre conservati: sono gia' ancorati a una frase dichiarativa.
    """
    keep_list = [term for term in keep if term and term.strip()]
    keep_folded = {_fold(term) for term in keep_list}
    keep_sequences: set[tuple[str, ...]] = {
        tuple(_fold(w) for w in term.split()) for term in keep_list
    }

    unigram: Counter[str] = Counter()
    bigram: Counter[tuple[str, ...]] = Counter()
    for sentence in sentences:
        tokens = _folded_words(sentence)
        for token in tokens:
            if _is_content(token):
                unigram[token] += 1
        # le coppie non possono attraversare una virgola: "medica, nei
        # filtri" non e' un termine
        for a, b in _content_pairs(sentence):
            middle = tokens[a:b]
            if any(m not in BRIDGE_WORDS for m in middle):
                continue
            bigram[(tokens[a], tokens[b])] += 1

    # candidati: (sequenza, frequenza, peso)
    candidates: list[tuple[tuple[str, ...], int, float]] = []
    for sequence, count in bigram.most_common(limit * 3):
        exclusive = unigram[sequence[-1]] <= count
        if count >= 2:
            # una coppia di parole e' piu' informativa della parola singola:
            # "teorema di Bayes" vale piu' di "teorema"
            candidates.append((sequence, count, count * 1.6))
        elif exclusive:
            # la seconda parola sta sempre con la prima: e' un collocation
            candidates.append((sequence, count, count * 2.5))
    for word, count in unigram.most_common(limit * 3):
        if count < 2 and word not in keep_folded:
            continue
        if not _looks_like_noun(word):
            continue
        candidates.append(((word,), count, float(count)))

    # i termini delle definizioni entrano per primi: sono gia' verificati
    for term in keep_list:
        words = tuple(term.split())
        if words:
            candidates.append((words, 1, 1000.0))

    candidates.sort(key=lambda item: (-item[2], -len(item[0]), item[0]))

    # selezione: una sequenza lunga "copre" le sue parole piu' corte, cosi'
    # "teorema di Bayes" esclude i termini "teorema" e "Bayes" presi da solo
    selected: list[tuple[tuple[str, ...], int]] = []
    covered: set[str] = set()
    for sequence, count, _weight in candidates:
        if not sequence:
            continue
        folded = [_fold(word) for word in sequence]
        if any(word in covered for word in folded):
            continue
        selected.append((sequence, count))
        covered.update(folded)
        if len(selected) >= limit:
            break
    return selected


def _strip_leading_marker(sentence: str) -> str:
    return _BULLET_RE.sub("", sentence.strip())


def _clean_word(word: str) -> str:
    """Toglie la punteggiatura ai bordi, conservando l'apostrofo.

    In italiano l'apostrofo finale ("probabilita'") e' il segno di
    elisione della vocale: si conserva, perche' e' parte della scrittura
    della lezione.
    """
    stripped = _PUNCT_EDGE_RE.sub("", word).strip()
    if stripped and word.rstrip().endswith(("'", "’")):
        stripped += "'"
    return stripped


def _trim_term(tokens: list[str]) -> list[str]:
    """Ripulisce una sequenza di parole per estrarre un termine."""
    words = [_clean_word(t) for t in tokens]
    words = [w for w in words if w]
    # via le copule in coda
    while words and _fold(words[-1]) in _TRAILING_COPULA:
        words.pop()
    # via le preposizioni in coda
    while words and _fold(words[-1]) in _TRAILING_TRIM:
        words.pop()
    # via gli articoli e le subordinate in testa. Il ciclo **alterna** le
    # due ripuliture e si ripete finche' qualcosa cambia: togliere un verbo
    # in testa puo' mettere a nudo una subordinata ("oggi vediamo che la
    # varianza" -> "che la varianza"), e ripulire quella puo' mettere a
    # nudo un altro verbo. Con le due passate separate — misurato —
    # "che" sopravviveva perche' il taglio dei verbi avveniva *dopo* quello
    # degli articoli.
    changed = True
    while changed and words:
        changed = False
        # articolo eliso: "L'emivita'" -> "emivita'"
        if (
            len(words[0]) > 2
            and words[0][0].lower() == "l"
            and words[0][1] == "'"
        ):
            words[0] = words[0][2:]
            changed = True
            if not words[0]:
                words.pop(0)
                continue
        folded = _fold(words[0])
        if folded in _LEADING_TRIM or folded in STOPWORDS:
            words.pop(0)
            changed = True
            continue
        # via un eventuale verbo iniziale ("oggi vediamo che la varianza")
        if not _looks_like_noun(words[0]):
            words.pop(0)
            changed = True
            continue
    # Il limite va preso dalla **testa**, non dalla coda. Le ripuliture
    # sopra tolgono articoli e verbi dall'inizio proprio perche' la prima
    # parola sopravvissuta e' la testa del sintagma; prendere le ultime
    # cinque (``words[-5:]``) la scartava di nuovo. Misurato:
    # "il Teorema del limite centrale per la stima della varianza"
    # produceva "per la stima della varianza" — il soggetto perso, tre
    # preposizioni al suo posto.
    words = words[:5]
    # E ora si ripulisce la coda: tagliare a cinque puo' lasciare in fondo
    # una preposizione o una subordinata ("Teorema del limite centrale per").
    while words and (
        _fold(words[-1]) in _TRAILING_COPULA or _fold(words[-1]) in _TRAILING_TRIM
    ):
        words.pop()
    return words


def _extract_term_before(sentence: str, marker_start: int) -> str:
    """Termine che precede il pattern definizionale."""
    head = sentence[:marker_start]
    # taglia sull'ultima virgola: il soggetto del termine e' l'ultimo
    # sintagma prima del predicato
    head = re.split(r"[,;:]|\s[-–]\s", head)[-1]
    return " ".join(_trim_term(head.split()))


def _fallback_term(sentence: str) -> str:
    """Termine di riserva: le prime parole di contenuto della frase."""
    words = [w for w in _words(sentence) if _is_content(w)]
    if not words:
        return ""
    return " ".join(words[:2])


#: Prefissi che introducono un sinonimo: "chiamata *anche* matrice di Jacobi".
#:
#: ``"o"`` **non** ci appartiene: in italiano introduce un'alternativa, non
#: un sinonimo ("zero significa o assenza di valore" — la definizione e'
#: "o assenza di valore", non "assenza di valore"). Toglierlo da qui era
#: un falso positivo: misurato, quella frase perdeva la congiunzione e la
#: definizione risultante cambiava significato. I verbi che introducono un
#: sinonimo con "o" sono ``ovvero`` e ``ossia``, che stanno in
#: ``DEFINITION_MARKERS`` e vengono gia' tolti dal marcatore.
_ALIAS_PREFIXES: tuple[str, ...] = (
    "anche",
    "detto anche",
    "detta anche",
    "anche detto",
    "anche detta",
    "alias",
)


def _strip_alias(text: str) -> str:
    """Toglie il prefisso di sinonimo da una definizione.

    "La matrice di iterazione e' chiamata anche matrice di Jacobi" dice che
    la matrice di iterazione *si chiama anche* matrice di Jacobi: la parte
    che segue "anche" e' un sinonimo, non la definizione.
    """
    stripped = text.strip()
    lowered = stripped.lower()
    for prefix in _ALIAS_PREFIXES:
        if lowered.startswith(prefix + " "):
            remainder = stripped[len(prefix) :].strip(" ,;:")
            # il sinonimo e' un nome proprio di frase: si tiene fino alla
            # prima congiunzione
            for conjunction in (" e ", " ed ", " che ", " per "):
                index = remainder.lower().find(conjunction)
                if index > 0:
                    remainder = remainder[:index]
            return remainder.strip(" ,;:")
    return stripped


def _content_pairs(sentence: str) -> list[tuple[int, int]]:
    """Coppie di posizioni (i, j) di parole di contenuto correlate.

    Le due parole devono stare nella stessa proposizione: una virgola o un
    punto e virgola fra le due le separa ("diagnostica medica, nei filtri
    antispam" non e' una coppia).
    """
    matches = list(_WORD_RE.finditer(sentence))
    if not matches:
        return []
    positions = [
        index
        for index, match in enumerate(matches)
        if _is_content(match.group(0))
    ]
    pairs: list[tuple[int, int]] = []
    for first, second in zip(positions, positions[1:]):
        gap = sentence[matches[first].end() : matches[second].start()]
        if any(char in gap for char in ",;:!?"):
            continue
        pairs.append((first, second))
    return pairs


def _find_phrase(sentence: str, words: Sequence[str]) -> tuple[str, int, int]:
    """Trova nel testo la sequenza di parole, con le maiuscole originali.

    Restituisce ``(frase, inizio, fine)``; la frase e' vuota se la sequenza
    non compare. Le parole ammesse in mezzo sono solo le "ponte" ("teorema
    *di* Bayes") e la sequenza non puo' attraversare una virgola: "medica,
    nei filtri" non e' un termine unico.
    """
    if not words:
        return "", 0, 0
    matches = list(_WORD_RE.finditer(sentence))
    if not matches:
        return "", 0, 0
    folded = [_fold(m.group(0)) for m in matches]
    target = [_fold(w) for w in words]
    for start in range(len(matches)):
        if folded[start] != target[0]:
            continue
        matched = 0
        end_index = start
        for index in range(start, len(matches)):
            if index > start and _crosses_punctuation(sentence, matches, start, index):
                break  # la sequenza non puo' superare una virgola
            token = folded[index]
            if token == target[matched]:
                matched += 1
                end_index = index
                if matched == len(target):
                    break
            elif _fold(matches[index].group(0)) in BRIDGE_WORDS:
                continue  # parola ponte in mezzo: si salta
            else:
                break
        if matched == len(target):
            begin = matches[start].start()
            finish = matches[end_index].end()
            return sentence[begin:finish], begin, finish
    return "", 0, 0


def _crosses_punctuation(
    sentence: str, matches: Sequence[re.Match[str]], start: int, index: int
) -> bool:
    """True se fra due parole c'e' una virgola (o un altro separatore forte)."""
    gap = sentence[matches[start].end() : matches[index].start()]
    return any(char in gap for char in ",;:.!?")


class HeuristicBackend:
    """Backend euristico: nessun modello, nessuna rete, solo testo.

    Produce risultati utili a partire dal solo testo della lezione:

    * **riassunto**: le frasi piu' rappresentative, in ordine di apparizione;
    * **concetti**: i termini piu' frequenti (parole e coppie), ciascuno
      con la frase della lezione che li contiene;
    * **definizioni**: le frasi che seguono un pattern definizionale
      italiano ("e' definito come", "significa", "chiamato", ...);
    * **flashcard**: cloze sulle parole chiave piu' frequenti e carte
      "che cos'e'" sulle definizioni;
    * **quiz**: domande a scelta multipla costruite dalle definizioni e
      dalle parole chiave.

    Il formato di uscita e' canonico (``termine || resto``) ed e' lo stesso
    usato dai backend LLM, cosi' l'assistente li interpreta allo stesso
    modo.
    """

    name = "heuristic"

    def __init__(
        self,
        *,
        max_concepts: int = 10,
        max_definitions: int = 12,
        max_flashcards: int = 12,
        max_quiz: int = 5,
    ) -> None:
        self.max_concepts = max_concepts
        self.max_definitions = max_definitions
        self.max_flashcards = max_flashcards
        self.max_quiz = max_quiz

    # -- API ---------------------------------------------------------------
    def generate(self, task: str, prompt: str) -> str:
        text = extract_transcript(prompt)
        sentences = _split_sentences(text)
        if not sentences:
            return ""
        if task == "summary":
            return self._summary(sentences)
        if task == "concepts":
            return self._concepts(sentences)
        if task == "definitions":
            return self._definitions(sentences)
        if task == "flashcards":
            return self._flashcards(sentences)
        if task == "quiz":
            return self._quiz(sentences)
        return ""

    # -- task --------------------------------------------------------------
    def _definition_terms(self, sentences: Sequence[str]) -> list[str]:
        """Termini estratti dalle frasi definitorie (gia' ancorati al testo)."""
        terms: list[str] = []
        for line in self._definitions(sentences).splitlines():
            parts = [p.strip() for p in line.split("||")]
            if parts and parts[0]:
                terms.append(parts[0])
        return terms

    def _summary(self, sentences: Sequence[str]) -> str:
        ranked = self._rank_sentences(sentences)
        chosen = ranked[: max(1, min(4, len(sentences)))]
        # Le frasi scelte vanno rimesse nell'**ordine di lettura**, non in
        # quello del punteggio ne' tantomeno alfabetico: un riassunto di
        # una lezione si legge come la lezione. Con ``top.sort()`` —
        # misurato — "Il teorema del limite centrale..." finiva prima de
        # "La varianza campionaria...", anche se la varianza era stata
        # spiegata prima.
        top = [sentences[i] for i in sorted(chosen)]
        terms = [
            self._term_label(sentences, sequence)
            for sequence, _ in _key_terms(
                sentences, limit=8, keep=self._definition_terms(sentences)
            )
        ]
        terms = [t for t in terms if t]
        lines = ["Riassunto automatico (backend euristico):", ""]
        lines.extend(f"- {s}" for s in top)
        if terms:
            lines.append("")
            lines.append("Parole chiave: " + ", ".join(terms))
        return "\n".join(lines)

    def _concepts(self, sentences: Sequence[str]) -> str:
        out: list[str] = []
        ranked = self._rank_sentences(sentences)
        keep = self._definition_terms(sentences)
        for sequence, _count in _key_terms(
            sentences, limit=self.max_concepts, keep=keep
        ):
            label = self._term_label(sentences, sequence)
            if not label:
                continue
            evidence = ""
            best = -1.0
            for index in ranked:
                if not self._contains_term(sentences[index], sequence):
                    continue
                score = self._score_sentence(sentences[index], sentences)
                if score > best:
                    best = score
                    evidence = sentences[index]
            out.append(f"{label} || {evidence}")
        return "\n".join(out)

    def _definitions(self, sentences: Sequence[str]) -> str:
        out: list[str] = []
        seen: set[str] = set()
        for sentence in sentences:
            found = self._find_marker(sentence)
            if found is None:
                continue
            marker, start = found
            term = _extract_term_before(sentence, start)
            if not term:
                term = _fallback_term(sentence)
            if not term or _fold(term) in seen:
                continue
            definition = sentence[start + len(marker) :].strip(" ,;:")
            definition = _strip_alias(definition)
            if not definition:
                definition = sentence.strip()
            definition = definition.rstrip(". ")
            seen.add(_fold(term))
            out.append(f"{term} || {definition}")
            if len(out) >= self.max_definitions:
                break
        return "\n".join(out)

    def _flashcards(self, sentences: Sequence[str]) -> str:
        cards: list[str] = []
        ranked = self._rank_sentences(sentences)
        # 1) cloze sulle parole chiave: si cancella la parola piu' significativa
        keep = self._definition_terms(sentences)
        for sequence, _count in _key_terms(
            sentences, limit=self.max_concepts, keep=keep
        ):
            for index in ranked:
                sentence = sentences[index]
                phrase, begin, finish = _find_phrase(sentence, sequence)
                if not phrase:
                    continue
                cards.append(
                    f"{sentence[:begin]}{'____'}{sentence[finish:]} || {phrase}"
                )
                break
        # 2) carte "che cos'e'" sulle definizioni
        for line in self._definitions(sentences).splitlines():
            parts = [p.strip() for p in line.split("||")]
            if len(parts) < 2 or not parts[0]:
                continue
            cards.append(f"Che cos'e' {parts[0]}? || {parts[1]}")
        return "\n".join(cards[: self.max_flashcards])

    def _quiz(self, sentences: Sequence[str]) -> str:
        definitions: list[tuple[str, str]] = []
        for line in self._definitions(sentences).splitlines():
            parts = [p.strip() for p in line.split("||")]
            if len(parts) >= 2 and parts[0] and parts[1]:
                definitions.append((parts[0], parts[1]))

        questions: list[str] = []

        # 1) domande di definizione: i distrattori sono le altre definizioni
        for term, definition in definitions[: self.max_quiz]:
            distractors = [
                other_def for other_term, other_def in definitions if other_term != term
            ][:3]
            if len(distractors) < 1:
                continue  # senza distrattori la domanda sarebbe triviale
            options, answer = _shuffled([definition, *distractors], 0, seed=term)
            questions.append(
                " || ".join([f"Che cos'e' {term}?", *options, str(answer)])
            )

        # 2) cloze a scelta multipla sulle parole chiave
        ranked = self._rank_sentences(sentences)
        terms = [
            (sequence, self._term_label(sentences, sequence))
            for sequence, _ in _key_terms(
                sentences, limit=12, keep=self._definition_terms(sentences)
            )
        ]
        terms = [(sequence, label) for sequence, label in terms if label]
        for sequence, label in terms:
            if len(questions) >= self.max_quiz:
                break
            distractors = [
                other_label
                for _other_sequence, other_label in terms
                if other_label != label
            ][:3]
            if len(distractors) < 2:
                continue
            cloze = ""
            for index in ranked:
                phrase, begin, finish = _find_phrase(sentences[index], sequence)
                if not phrase:
                    continue
                cloze = sentences[index][:begin] + "____" + sentences[index][finish:]
                break
            if not cloze:
                continue
            options, answer = _shuffled([label, *distractors], 0, seed=label)
            questions.append(
                " || ".join([f"Completate la frase: {cloze}", *options, str(answer)])
            )
        return "\n".join(questions[: self.max_quiz])

    # -- supporto ----------------------------------------------------------
    @staticmethod
    def _find_marker(sentence: str) -> tuple[str, int] | None:
        """Primo pattern definizionale presente nella frase."""
        best: tuple[str, int] | None = None
        for pattern in _DEFINITION_RES:
            match = pattern.search(sentence)
            if match is None:
                continue
            if best is None or match.start() < best[1]:
                best = (match.group(0), match.start())
        return best

    @staticmethod
    def _contains_term(sentence: str, sequence: Sequence[str]) -> bool:
        return bool(_find_phrase(sentence, sequence)[0])

    @staticmethod
    def _term_label(sentences: Sequence[str], sequence: Sequence[str]) -> str:
        """Etichetta leggibile del termine (con le maiuscole originali)."""
        for sentence in sentences:
            phrase, _begin, _finish = _find_phrase(sentence, sequence)
            if phrase:
                return phrase
        return " ".join(sequence)

    def _score_sentence(self, sentence: str, sentences: Sequence[str]) -> float:
        content = _content_words(sentence)
        if not content:
            return 0.0
        frequency = Counter()
        for other in sentences:
            frequency.update(_content_words(other))
        score = sum(frequency[w] for w in content) / math.sqrt(len(content))
        if self._find_marker(sentence) is not None:
            score *= 1.35
        return score

    def _rank_sentences(self, sentences: Sequence[str]) -> list[int]:
        """Indici delle frasi, dal piu' rilevante al meno rilevante."""
        frequency = Counter()
        for sentence in sentences:
            frequency.update(_content_words(sentence))
        scored: list[tuple[float, int]] = []
        for index, sentence in enumerate(sentences):
            content = _content_words(sentence)
            if not content:
                continue
            score = sum(frequency[w] for w in content) / math.sqrt(len(content))
            if self._find_marker(sentence) is not None:
                score *= 1.35
            # bonus lieve per l'apertura: spesso introduce il tema
            if index < 3:
                score *= 1.1
            scored.append((score, index))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [index for _score, index in scored]


def _shuffled(options: Sequence[str], answer: int, *, seed: str) -> tuple[list[str], int]:
    """Ordina le opzioni in modo *deterministico* (stesso seed = stesso ordine)."""
    rnd = random.Random(zlib.crc32(seed.encode("utf-8")))
    indexed = list(enumerate(options))
    rnd.shuffle(indexed)
    new_options = [text for _i, text in indexed]
    positions = [i for i, (orig, _t) in enumerate(indexed) if orig == answer]
    return new_options, (positions[0] if positions else 0)


# ---------------------------------------------------------------------------
# configurazione del modulo di studio
# ---------------------------------------------------------------------------
@dataclass
class StudyConfig:
    """Preferenze del modulo di studio.

    ``enabled`` e' **False** per impostazione predefinita: il modulo resta
    spento finche' l'utente non lo attiva. ``backend`` puo' valere:

    ``auto``
        usa il modello locale se raggiungibile, altrimenti l'euristico;
    ``heuristic``
        forza il backend euristico (funziona sempre, offline);
    ``local``
        forza il modello locale; se non risponde si degrada all'euristico.
    """

    enabled: bool = False
    backend: str = "auto"
    llm_endpoint: str = "http://127.0.0.1:8080/v1"
    llm_model: str = "local-model"
    llm_timeout: float = 60.0
    allow_remote: bool = False
    max_transcript_chars: int = 12000

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StudyConfig":
        known = set(cls.__dataclass_fields__)
        clean = {k: v for k, v in (data or {}).items() if k in known}
        config = cls(**clean)  # type: ignore[arg-type]
        if config.backend not in {"auto", "heuristic", "local"}:
            config.backend = "auto"
        return config


def load_study_config(path: Path | None = None) -> StudyConfig:
    """Legge la configurazione; torna ai default se il file manca o e' rotto."""
    target = Path(path) if path else _default_config_path()
    data = _read_json(target)
    if not isinstance(data, dict):
        return StudyConfig()
    return StudyConfig.from_dict(data)


def save_study_config(config: StudyConfig, path: Path | None = None) -> Path:
    """Salva la configurazione del modulo di studio."""
    target = Path(path) if path else _default_config_path()
    _write_json_atomic(target, config.to_dict())
    return target


# ---------------------------------------------------------------------------
# dizionario personalizzato
# ---------------------------------------------------------------------------
@dataclass
class DictionaryEntry:
    """Una voce del dizionario personale."""

    term: str
    kind: str = "term"  # term | name | acronym
    note: str = ""
    added_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DictionaryEntry":
        return cls(
            term=str(data.get("term", "")),
            kind=str(data.get("kind", "term")),
            note=str(data.get("note", "")),
            added_at=float(data.get("added_at", 0.0)),
        )


class CustomDictionary:
    """Dizionario personale di termini tecnici e nomi propri.

    Serve a due scopi:

    * **riconoscimento**: :meth:`hotwords_prompt` restituisce una stringa
      compatta (di norma entro ~200 caratteri) da usare come
      ``initial_prompt`` di faster-whisper, che spinge il modello a
      riconoscere quei termini;
    * **correzione**: le voci sono anche un aiuto per la revisione manuale.

    Il dizionario e' salvato in locale (``DATA_DIR/dictionary.json``) ed e'
    modificabile a runtime.
    """

    def __init__(self, path: Path | None = None, *, max_prompt_chars: int = 200) -> None:
        self.path = Path(path) if path else _default_dictionary_path()
        self.max_prompt_chars = max_prompt_chars
        self._entries: list[DictionaryEntry] = []
        self.load()

    # -- persistenza -------------------------------------------------------
    def load(self) -> None:
        """Ricarica il dizionario dal disco (ignora i file corrotti)."""
        data = _read_json(self.path)
        if isinstance(data, dict):
            raw = data.get("entries", [])
        elif isinstance(data, list):
            raw = data
        else:
            raw = []
        self._entries = []
        for item in raw:
            if isinstance(item, dict):
                entry = DictionaryEntry.from_dict(item)
                if entry.term:
                    self._entries.append(entry)
            elif isinstance(item, str) and item.strip():
                self._entries.append(
                    DictionaryEntry(term=item.strip(), added_at=time.time())
                )

    def save(self) -> Path:
        """Salva il dizionario su disco."""
        payload = {
            "version": 1,
            "entries": [entry.to_dict() for entry in self._entries],
        }
        _write_json_atomic(self.path, payload)
        return self.path

    # -- API ---------------------------------------------------------------
    def add(self, term: str, kind: str = "term", note: str = "") -> bool:
        """Aggiunge un termine. Ritorna False se era gia' presente."""
        cleaned = (term or "").strip()
        if not cleaned:
            return False
        for entry in self._entries:
            if entry.term.lower() == cleaned.lower():
                return False
        self._entries.append(
            DictionaryEntry(
                term=cleaned, kind=kind or "term", note=note or "", added_at=time.time()
            )
        )
        self.save()
        return True

    def remove(self, term: str) -> bool:
        """Rimuove un termine. Ritorna False se non era presente."""
        cleaned = (term or "").strip().lower()
        before = len(self._entries)
        self._entries = [e for e in self._entries if e.term.lower() != cleaned]
        if len(self._entries) != before:
            self.save()
            return True
        return False

    def clear(self) -> None:
        """Svuota il dizionario."""
        self._entries = []
        self.save()

    def list(self) -> list[DictionaryEntry]:
        """Tutte le voci, nell'ordine di inserimento."""
        return list(self._entries)

    @property
    def terms(self) -> list[str]:
        """I soli termini, come stringhe."""
        return [entry.term for entry in self._entries]

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, term: object) -> bool:
        if not isinstance(term, str):
            return False
        needle = term.strip().lower()
        return any(entry.term.lower() == needle for entry in self._entries)

    # -- uso nel riconoscimento -------------------------------------------
    def hotwords_prompt(self, max_chars: int | None = None) -> str:
        """Prompt compatto per faster-whisper (``initial_prompt``).

        Restituisce i termini separati da virgola, troncati al limite di
        caratteri senza spezzare l'ultimo termine. Se il dizionario e'
        vuoto la stringa e' vuota: in quel caso ``None`` e' il valore
        corretto da passare a faster-whisper.
        """
        limit = self.max_prompt_chars if max_chars is None else max_chars
        if not self._entries or limit <= 0:
            return ""
        parts: list[str] = []
        length = 0
        for entry in self._entries:
            piece = entry.term.strip()
            if not piece:
                continue
            extra = len(piece) + (2 if parts else 0)
            # un termine piu' lungo del limite viene **saltato**, non
            # usato come termine della lista: con ``break`` il primo
            # termine troppo lungo escludeva per sempre tutti quelli dopo,
            # anche cortissimi -- misurato: un solo termine da 500
            # caratteri azzerava il prompt con limite 200.
            if length + extra > limit:
                continue
            parts.append(piece)
            length += extra
        return ", ".join(parts)

    def asr_initial_prompt(self, previous_text: str = "") -> str | None:
        """Prompt da passare a faster-whisper, unendo hotwords e contesto.

        Restituisce ``None`` se non c'e' niente da dire: in quel caso
        faster-whisper usa il comportamento predefinito.
        """
        hotwords = self.hotwords_prompt()
        context = (previous_text or "").strip()
        if not hotwords and not context:
            return None
        if not hotwords:
            return context
        if not context:
            return hotwords
        budget = max(0, self.max_prompt_chars - len(hotwords) - 2)
        return f"{hotwords}, {context[-budget:]}" if budget else hotwords


# ---------------------------------------------------------------------------
# correzioni manuali della trascrizione
# ---------------------------------------------------------------------------
@dataclass
class Correction:
    """Una correzione manuale: originale e versione corretta affiancate."""

    segment_id: int
    original: str
    corrected: str
    created_at: float = 0.0

    @property
    def is_reverted(self) -> bool:
        return not self.corrected.strip()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Correction":
        return cls(
            segment_id=int(data.get("segment_id", 0)),
            original=str(data.get("original", "")),
            corrected=str(data.get("corrected", "")),
            created_at=float(data.get("created_at", 0.0)),
        )

    def __str__(self) -> str:
        return f"[{self.segment_id}] {self.original} -> {self.corrected}"


class TranscriptCorrector:
    """Correzioni manuali, reversibili e persistenti.

    Per ogni segmento corretto vengono conservati **sia** il testo
    riconosciuto dall'ASR **sia** la correzione: nessuna informazione
    viene persa e :meth:`revert` riporta il segmento al testo originale.
    """

    def __init__(
        self, path: Path | None = None, *, store: SessionStore | None = None
    ) -> None:
        self.path = Path(path) if path else _default_corrections_path()
        self.store = store
        self._corrections: dict[int, Correction] = {}
        # id_segmento -> testo originale, per non rifare la stessa query
        self._original_cache: dict[int, str] = {}
        self.load()

    # -- persistenza -------------------------------------------------------
    def load(self) -> None:
        """Ricarica le correzioni dal disco."""
        data = _read_json(self.path)
        self._corrections = {}
        # la cache puntava ai testi del giro precedente: svuotarla, o una
        # sessione riaperta dopo una trascrizione nuova vedrebbe id riusati
        self._original_cache.clear()
        items: Iterable[Any]
        if isinstance(data, dict):
            raw = data.get("corrections", {})
            items = raw.values() if isinstance(raw, dict) else raw
        elif isinstance(data, list):
            items = data
        else:
            items = []
        for item in items:
            if not isinstance(item, dict):
                continue
            correction = Correction.from_dict(item)
            if correction.segment_id:
                self._corrections[correction.segment_id] = correction

    def save(self) -> Path:
        """Salva le correzioni su disco."""
        payload = {
            "version": 1,
            "corrections": [c.to_dict() for c in self._corrections.values()],
        }
        _write_json_atomic(self.path, payload)
        return self.path

    # -- API ---------------------------------------------------------------
    def correct(
        self, segment_id: int, new_text: str, original: str | None = None
    ) -> Correction:
        """Registra (ed eventualmente corregge) il testo di un segmento.

        Il testo originale viene da ``original`` se fornito, altrimenti
        viene cercato nel ``SessionStore`` (se collegato). Senza store ne'
        l'uno ne' l'altro, l'originale gia' salvato per quel segmento ha
        la precedenza: senza questa precedenza, correggere due volte lo
        stesso segmento sovrascriveva la correzione esistente con
        ``original=""`` e :meth:`revert` non aveva piu' nulla da
        ripristinare — misurato: ``[42] -> testo corretto di nuovo`` con
        ``original=''``.

        Una correzione vuota equivale a un revert.
        """
        key = int(segment_id)
        if original is None:
            original = self._lookup_original(key)
        existing = self._corrections.get(key)
        if not (original or "").strip() and existing is not None:
            # nessuna fonte esterna: l'originale salvato e' l'unico che abbiamo
            original = existing.original
        text = (new_text or "").strip()
        if not text:
            reverted = self.revert(key)
            if reverted is not None:
                return reverted
            return Correction(
                segment_id=key, original=original, corrected=""
            )
        correction = Correction(
            segment_id=key,
            original=original,
            corrected=text,
            created_at=time.time(),
        )
        self._corrections[key] = correction
        self.save()
        return correction

    def revert(self, segment_id: int) -> Correction | None:
        """Riporta il segmento al testo originale (conserva la traccia)."""
        key = int(segment_id)
        existing = self._corrections.get(key)
        if existing is None:
            return None
        existing.corrected = ""
        existing.created_at = time.time()
        self.save()
        return existing

    def all(self) -> list[Correction]:
        """Tutte le correzioni registrate, ordinate per id di segmento."""
        return [self._corrections[key] for key in sorted(self._corrections)]

    def active(self) -> list[Correction]:
        """Solo le correzioni attive (non ancora reversate)."""
        return [c for c in self.all() if c.corrected.strip()]

    def get(self, segment_id: int) -> Correction | None:
        """Correzione di un segmento, se presente."""
        return self._corrections.get(int(segment_id))

    def mapping(self) -> dict[int, str]:
        """Mappa ``id_segmento -> testo corretto`` (solo correzioni attive)."""
        return {c.segment_id: c.corrected for c in self.active()}

    def corrected_text(self, segment_id: int) -> str | None:
        """Testo corretto di un segmento, se esiste."""
        correction = self.get(segment_id)
        if correction is None or not correction.corrected.strip():
            return None
        return correction.corrected

    def session_text(self, session_id: int, *, timestamps: bool = True) -> str:
        """Testo della sessione con le correzioni applicate."""
        if self.store is None:
            raise ValueError("TranscriptCorrector senza SessionStore: usa .mapping()")
        pieces: list[str] = []
        for row in self.store.get_segments(session_id):
            original = (row["text"] or "").strip()
            if not original:
                continue
            text = self.corrected_text(int(row["id"])) or original
            if timestamps:
                start = float(row["t_start"])
                pieces.append(f"[{int(start // 60):02d}:{int(start % 60):02d}] {text}")
            else:
                pieces.append(text)
        return " ".join(pieces)

    # -- interni -----------------------------------------------------------
    def _lookup_original(self, segment_id: int) -> str:
        """Cerca il testo originale del segmento nel database.

        ``SessionStore`` non ha una ricerca per id di segmento, quindi la
        scansione passa per tutte le sessioni. Il risultato viene messo in
        cache: correggere dieci segmenti della stessa sessione faceva dieci
        scansioni complete del database (ogni ``get_segments`` e' una
        query con tutti i segmenti della sessione).
        """
        key = int(segment_id)
        if key in self._original_cache:
            return self._original_cache[key]
        found = ""
        if self.store is not None:
            for session in self.store.list_sessions():
                for row in self.store.get_segments(session.id):
                    if int(row["id"]) == key:
                        found = (row["text"] or "").strip()
                        break
                if found:
                    break
        # solo i risultati positivi in cache: un segmento che non c'era
        # ancora (transcrizione in corso) verra' cercato di nuovo
        if found:
            self._original_cache[key] = found
        return found


# ---------------------------------------------------------------------------
# la facciata: StudyAssistant
# ---------------------------------------------------------------------------
class StudyAssistant:
    """Assistente di studio su una lezione gia' trascritta.

    Il backend viene scelto in base a :class:`StudyConfig`:

    * ``auto``: se un modello locale e' configurato e raggiungibile lo si
      usa, altrimenti si ricade sull'euristico;
    * ``local``: il modello locale e' obbligatorio, ma se non risponde
      l'assistente si degrada comunque all'euristico (e lo segnala);
    * ``heuristic``: solo analisi lessicale, sempre disponibile offline.

    Il modulo resta **spento** finche' :attr:`enabled` non e' True: nessuna
    attivita' parte da sola.
    """

    def __init__(
        self,
        config: StudyConfig | None = None,
        *,
        backend: StudyBackend | None = None,
        store: SessionStore | None = None,
    ) -> None:
        self.config = config or load_study_config()
        self.store = store
        self._backend: StudyBackend | None = backend
        self._forced = backend is not None
        self._degraded = False
        self.last_error: str | None = None
        # avvisi accumulati da _run(): sopravvivono tra una task e l'altra
        # di study(), che poi li mette in material.warnings.
        self._errors: list[str] = []

    # -- stato -------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        """True se il modulo di studio e' attivo."""
        return bool(self.config.enabled)

    def enable(self) -> None:
        """Attiva il modulo di studio (persistito)."""
        self.config.enabled = True
        save_study_config(self.config)

    def disable(self) -> None:
        """Disattiva il modulo di studio (persistito)."""
        self.config.enabled = False
        save_study_config(self.config)

    @property
    def backend(self) -> StudyBackend:
        """Il backend in uso (risolto alla prima richiesta)."""
        if self._backend is None:
            self._backend = self._resolve_backend()
        return self._backend

    @property
    def backend_name(self) -> str:
        """Nome del backend in uso.

        Con il modulo **spento** non risolve nulla: ``_resolve_backend()``
        in modalita' ``auto`` sonda il server LLM locale con un timeout di
        1,5 s, e ``study()`` chiede ``backend_name`` proprio nel ramo
        "modulo spento" — misurato: 1,5 s di attesa di rete per una
        risposta che dice solo "niente da fare". Se il backend e' stato
        forzato dall'esterno (test, uso programmatico) il nome c'e' gia'.
        """
        if not self.enabled:
            if self._backend is not None:
                return getattr(self._backend, "name", type(self._backend).__name__)
            return "disabilitato"
        return getattr(self.backend, "name", type(self.backend).__name__)

    @property
    def is_available(self) -> bool:
        """True se l'assistente puo' produrre risultati.

        Con il backend euristico e' sempre True (funziona offline); con un
        modello locale e' True solo se il server risponde.
        """
        backend = self.backend
        probe = getattr(backend, "available", None)
        if callable(probe):
            return bool(probe())
        return True

    @property
    def local_llm_available(self) -> bool:
        """True se un server LLM locale e' raggiungibile."""
        return self._make_llm_backend().available()

    # -- API ---------------------------------------------------------------
    def summarize(self, text: str) -> str:
        """Riassunto della lezione."""
        return self._run("summary", text)

    def key_concepts(self, text: str) -> list[Concept]:
        """Concetti principali, con la frase che li sostiene."""
        return _parse_concepts(self._run("concepts", text))

    def definitions(self, text: str) -> list[Definition]:
        """Definizioni estratte dal testo."""
        return _parse_definitions(self._run("definitions", text))

    def flashcards(self, text: str) -> list[Flashcard]:
        """Flashcard di ripasso."""
        return _parse_flashcards(self._run("flashcards", text))

    def quiz(self, text: str) -> list[QuizQuestion]:
        """Domande di riposo a scelta multipla."""
        return _parse_quiz(self._run("quiz", text))

    def study(self, text: str, session_id: int | None = None) -> StudyMaterial:
        """Esegue tutte le attivita' e restituisce il pacchetto completo."""
        if not self.enabled:
            return StudyMaterial(
                session_id=session_id,
                backend=self.backend_name,
                created_at=time.time(),
                warnings=["Modulo studio disabilitato: nessuna attivita' eseguita."],
            )
        material = StudyMaterial(
            session_id=session_id, backend=self.backend_name, created_at=time.time()
        )
        material.summary = self.summarize(text)
        material.concepts = self.key_concepts(text)
        material.definitions = self.definitions(text)
        material.flashcards = self.flashcards(text)
        material.quiz = self.quiz(text)
        # gli avvisi di _run() sono stati accumulati, non azzerati da ogni
        # task: arrivano tutti qui (misurato: senza questo, 4 sezioni su 5
        # tornavano vuote e warnings restava []).
        material.warnings.extend(self._drain_errors())
        return material

    def study_session(self, session_id: int) -> StudyMaterial:
        """Analizza una sessione gia' trascritta."""
        if self.store is None:
            raise ValueError("StudyAssistant senza SessionStore")
        text = session_transcript(self.store, session_id)
        return self.study(text, session_id=session_id)

    # -- interni -----------------------------------------------------------
    def _make_llm_backend(self) -> LocalLLMBackend:
        return LocalLLMBackend(
            endpoint=self.config.llm_endpoint,
            model=self.config.llm_model,
            timeout=self.config.llm_timeout,
            allow_remote=self.config.allow_remote,
        )

    def _resolve_backend(self) -> StudyBackend:
        if self._forced and self._backend is not None:
            return self._backend
        choice = (self.config.backend or "auto").lower()
        if choice == "heuristic":
            return HeuristicBackend()
        llm = self._make_llm_backend()
        if choice == "local":
            return llm
        # auto: LLM locale se c'e', altrimenti euristico
        if llm.is_local() and llm.available():
            return llm
        return HeuristicBackend()

    def _run(self, task: str, text: str) -> str:
        """Esegue una task sul backend, degradando all'euristico se serve.

        Il degrado all'euristico vale **per ogni task**, non per la prima
        soltanto: il flag ``_degraded`` serviva a evitare di riprovare il
        LLM a ogni chiamata, ma :meth:`study` chiama ``_run`` cinque volte
        di fila e quindi quattro sezioni su cinque restavano vuote senza
        alcun avviso (misurato: riassunto sì, concetti/definitions/
        flashcards/quiz tutti vuoti e ``warnings`` vuoto).

        ``last_error`` non viene piu' azzerato a ogni ``_run``: era
        proprio quell'azzeramento a cancellare l'avviso prima che
        ``study()`` lo leggesse, percio' l'errore viene accumulato in
        ``_errors`` e riversato in ``study()``. L'errore che interrompe
        la generazione (il backend fallisce) e' tenuto in ``last_error``.
        """
        if not (text or "").strip():
            return ""
        prompt = build_prompt(task, text, max_chars=self.config.max_transcript_chars)
        backend = self.backend
        try:
            output = backend.generate(task, prompt)
        except Exception as exc:  # pragma: no cover - difesa di rete
            output = ""
            self.last_error = f"backend {getattr(backend, 'name', '?')} fallito: {exc}"
        if output.strip():
            return output.strip()
        # Il LLM locale ha risposto vuoto: degrada all'euristico, che e'
        # offline e produce sempre qualcosa. L'avviso viene accumulato per
        # non essere perso dalle chiamate successive.
        if isinstance(backend, LocalLLMBackend):
            self._degraded = True
            self._add_error(backend.last_error or "modello locale non raggiungibile")
            fallback = HeuristicBackend()
            try:
                return fallback.generate(task, prompt).strip()
            except Exception as exc:
                self._add_error(f"backend euristico fallito: {exc}")
                return ""
        return output.strip()

    def _add_error(self, message: str) -> None:
        """Accumula un avviso, evitando i duplicati esatti."""
        if message and message not in self._errors:
            self._errors.append(message)
        self.last_error = message

    def _drain_errors(self) -> list[str]:
        """Restituisce gli avvisi accumulati e azzera la lista."""
        errors = list(self._errors)
        self._errors.clear()
        return errors


# ---------------------------------------------------------------------------
# parsing dell'output dei backend
# ---------------------------------------------------------------------------
def _split_fields(line: str) -> list[str]:
    """Divide una riga canonica nei suoi campi.

    I campi **vuoti restano**: scartarli faceva scivolare tutti i
    successivi di un posto. Misurato: ``"Domanda || A || || B || 2"``
    (opzioni ``A``, vuota, ``B``; risposta ``2`` = ``B``) dava
    ``options=('A','B')`` con ``answer=2``, cioe' **fuori range**, e la
    domanda rispondeva ``A`` — la risposta sbagliata presentata come
    giusta. Stesso difetto in :func:`_parse_definitions`, dove
    ``"Termine || || def"`` (definizione assente, evidenza ``def``)
    usava ``def`` come definizione.
    """
    line = _strip_leading_marker(line).strip()
    if not line:
        return []
    if "||" in line:
        parts = [part.strip() for part in line.split("||")]
    elif "|" in line:
        parts = [part.strip() for part in line.split("|")]
    else:
        parts = [line]
    return parts


def _parse_concepts(raw: str) -> list[Concept]:
    concepts: list[Concept] = []
    for line in (raw or "").splitlines():
        if not line.strip():
            continue
        parts = _split_fields(line)
        if not parts:
            continue
        term = parts[0].strip(" -*•:;")
        if not term:
            continue
        evidence = parts[1] if len(parts) > 1 else ""
        concepts.append(Concept(term=term, evidence=evidence))
    return concepts


def _parse_definitions(raw: str) -> list[Definition]:
    definitions: list[Definition] = []
    for line in (raw or "").splitlines():
        if not line.strip():
            continue
        parts = _split_fields(line)
        if not parts:
            continue
        term = parts[0].strip(" -*•:;")
        definition = parts[1] if len(parts) > 1 else ""
        if not term:
            continue
        definitions.append(
            Definition(term=term, definition=definition, evidence=line.strip())
        )
    return definitions


def _parse_flashcards(raw: str) -> list[Flashcard]:
    cards: list[Flashcard] = []
    for line in (raw or "").splitlines():
        if not line.strip():
            continue
        parts = _split_fields(line)
        if not parts:
            continue
        front = parts[0].strip(" -*•:;")
        back = parts[1] if len(parts) > 1 else ""
        if not front:
            continue
        kind = "cloze" if "____" in front else "aperta"
        cards.append(Flashcard(front=front, back=back, kind=kind))
    return cards


def _parse_quiz(raw: str) -> list[QuizQuestion]:
    questions: list[QuizQuestion] = []
    for line in (raw or "").splitlines():
        if not line.strip():
            continue
        parts = _split_fields(line)
        if len(parts) < 2:
            continue
        question = parts[0].strip(" -*•:;")
        options = tuple(parts[1:-1]) if len(parts) > 2 else tuple(parts[1:])
        answer = 0
        if len(parts) >= 3:
            try:
                answer = int(parts[-1])
            except ValueError:
                answer = 0
        if not question or not options:
            continue
        # Opzioni duplicate vanno tolte, e l'indice della risposta va
        # ricalcolato di conseguenza. Con ``answer=0`` su
        # "positiva || positiva || negativa" il quiz mostrava la stessa
        # risposta due volte: qualunque opzione si scegliesse, era giusta
        # — misurato. Togliendo i duplicati, se la risposta giusta
        # sopravvive resta corretta; se era un duplicato, torna alla
        # prima opzione rimasta.
        unique: list[str] = []
        for option in options:
            if option not in unique:
                unique.append(option)
        if len(unique) != len(options):
            correct = options[answer] if answer < len(options) else options[0]
            options = tuple(unique)
            answer = unique.index(correct) if correct in unique else 0
        if answer >= len(options):
            answer = 0
        # Opzioni vuote non sono opzioni: il modello locale a volte ne
        # emette una per riempire la riga. Toglierle **dopo** aver
        # riparato l'indice, e ripararlo di nuovo, altrimenti una riga
        # "D || A || || B || 2" (risposta 2 = "B") perderebbe la B e
        # risponderebbe "A".
        nonempty = [o for o in options if o]
        if len(nonempty) != len(options):
            if 0 <= answer < len(options) and options[answer]:
                correct = options[answer]
                options = tuple(nonempty)
                answer = (
                    nonempty.index(correct) if correct in nonempty else 0
                )
            else:
                options = tuple(nonempty)
                answer = 0
        if not options:
            continue
        questions.append(QuizQuestion(question=question, options=options, answer=answer))
    return questions


# ---------------------------------------------------------------------------
# utilita'
# ---------------------------------------------------------------------------
def session_transcript(
    store: SessionStore, session_id: int, *, timestamps: bool = True
) -> str:
    """Trascrizione di una sessione come testo semplice.

    Con ``timestamps=True`` ogni segmento e' preceduto dal suo
    ``[MM:SS]``: i timestamp aiutano sia il LLM a citare i momenti della
    lezione sia l'euristico (che li ignora).
    """
    pieces: list[str] = []
    for row in store.get_segments(session_id):
        text = (row["text"] or "").strip()
        if not text:
            continue
        if timestamps:
            start = float(row["t_start"])
            pieces.append(f"[{int(start // 60):02d}:{int(start % 60):02d}] {text}")
        else:
            pieces.append(text)
    return " ".join(pieces)
