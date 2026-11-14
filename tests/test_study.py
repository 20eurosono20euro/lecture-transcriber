"""Test del modulo di studio.

Questo file nasce perche' ``study.py`` era l'unico modulo di produzione
senza **nessun** test: 27 dei 56 bug trovati nella caccia sistematica
stavano qui, e nessuno li aveva visti. I test sotto coprono i difetti
corretti (degrado silenzioso, marker case-sensitive, correzione che
perdeva l'originale, endpoint non-locale) e le proprieta' che devono
reggere per contratto.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _bootstrap  # noqa: F401,E402

from lecture_transcriber import study as S
from lecture_transcriber.study import (
    CustomDictionary,
    HeuristicBackend,
    LocalLLMBackend,
    StudyAssistant,
    StudyConfig,
    StudyMaterial,
    TranscriptCorrector,
)


LECTURE = (
    "La varianza e' definita come la dispersione dei dati attorno alla media. "
    "In statistica la deviazione standard misura quanto i valori si allontanano "
    "dalla media del campione. Il campione e' un sottoinsieme della popolazione. "
    "Per definizione la probabilita' e' il rapporto tra casi favorevoli e casi possibili. "
    "Significa che il modello ha convergito quando il gradiente si annulla."
)


class FakeLLM(LocalLLMBackend):
    """Backend LLM finto: restituisce sempre stringa vuota (server spento)."""

    name = "llm-finto"

    def __init__(self) -> None:
        super().__init__(allow_remote=False)

    def is_local(self) -> bool:
        return True

    def available(self, *, timeout: float = 1.5) -> bool:
        return False

    def generate(self, task: str, prompt: str) -> str:
        self.last_error = "server non raggiungibile"
        return ""


# ---------------------------------------------------------------------------
# il degrado all'euristico
# ---------------------------------------------------------------------------


class TestDegradation(unittest.TestCase):
    """Il LLM locale spento deve degradare su OGNI task, non solo la prima."""

    def setUp(self) -> None:
        self.assistant = StudyAssistant(
            StudyConfig(enabled=True, backend="local"), backend=FakeLLM()
        )

    def test_every_section_is_filled_when_the_llm_is_off(self) -> None:
        """study() chiama _run() cinque volte: tutte e cinque devono produrre.

        Prima ``_degraded`` era un latch: la prima task vuota alzava il
        flag e le altre quattro tornavano ``""`` — misurato: riassunto sì,
        concetti/definitions/flashcards/quiz tutti vuoti.
        """
        material = self.assistant.study(LECTURE)
        self.assertTrue(material.summary, "il riassunto non deve cadere")
        self.assertTrue(material.concepts, "i concetti non devono cadere")
        self.assertTrue(material.definitions, "le definizioni non devono cadere")
        self.assertTrue(material.flashcards, "le flashcards non devono cadere")

    def test_the_degradation_is_reported_as_a_warning(self) -> None:
        """L'avviso deve arrivare a material.warnings, non perdersi.

        ``_run`` azzerava ``last_error`` a ogni chiamata, quindi ``study()``
        non lo vedeva mai: l'utente riceveva quattro sezioni vuote e nessuna
        spiegazione.
        """
        material = self.assistant.study(LECTURE)
        self.assertTrue(material.warnings, "manca l'avviso di degrado")
        self.assertIn("server non raggiungibile", " ".join(material.warnings))

    def test_the_warning_is_not_repeated_for_every_task(self) -> None:
        """Cinque task, un solo avviso: l'accumulo non duplica."""
        material = self.assistant.study(LECTURE)
        self.assertEqual(len(material.warnings), 1)

    def test_a_single_task_still_reports_the_error(self) -> None:
        """Chiamare una sola task direttamente lascia l'errore visibile."""
        text = self.assistant.summarize(LECTURE)
        self.assertTrue(text)
        self.assertIsNotNone(self.assistant.last_error)


class TestDisabledModule(unittest.TestCase):
    """Il modulo spento non deve fare nulla, tantomeno toccare la rete."""

    def test_study_with_the_module_off_does_nothing(self) -> None:
        assistant = StudyAssistant(StudyConfig(enabled=False, backend="auto"))
        material = assistant.study(LECTURE)
        self.assertEqual(material.summary, "")
        self.assertEqual(material.concepts, [])
        self.assertEqual(material.flashcards, [])
        self.assertTrue(
            any("disabilitato" in w.lower() for w in material.warnings),
            f"avviso mancante: {material.warnings}",
        )

    def test_backend_name_with_the_module_off_makes_no_probe(self) -> None:
        """backend_name non deve risolvere il backend (e quindi sondare la rete).

        ``_resolve_backend`` in modalita' ``auto`` chiama ``available()``,
        che aspetta fino a 1,5 s: misura: 1,5 s di attesa per una risposta
        che dice solo "niente da fare".
        """
        probes: list[int] = []

        class Counting(LocalLLMBackend):
            def available(self, *, timeout: float = 1.5) -> bool:
                probes.append(1)
                return False

        config = StudyConfig(enabled=False, backend="auto")
        assistant = StudyAssistant.__new__(StudyAssistant)
        assistant.config = config
        assistant.store = None
        assistant._backend = None
        assistant._forced = False
        assistant._degraded = False
        assistant.last_error = None
        assistant._errors = []
        assistant._resolve_backend = lambda: Counting()  # type: ignore[method-assign]

        name = assistant.backend_name
        self.assertEqual(name, "disabilitato")
        self.assertEqual(probes, [], "backend_name ha risolto il backend a modulo spento")

    def test_a_forced_backend_keeps_its_name_when_disabled(self) -> None:
        """Se il backend c'e' gia' (uso programmatico), il nome resta utile."""
        assistant = StudyAssistant(
            StudyConfig(enabled=False), backend=HeuristicBackend()
        )
        self.assertIn("heuristic", assistant.backend_name.lower())


# ---------------------------------------------------------------------------
# riconoscimento dei marker definizionali
# ---------------------------------------------------------------------------


class TestDefinitionMarkers(unittest.TestCase):
    """I marker si trovano anche a inizio frase e con l'elisione ASCII."""

    def test_a_marker_at_the_start_of_a_sentence_is_found(self) -> None:
        """'Per definizione ...' apriva la frase e non veniva riconosciuto.

        Il regex era case-sensitive: tutti i marker che possono iniziare una
        frase (per definizione, significa, si intende, ovvero) erano invisibili
        — misurato: ``_find_marker("Per definizione la varianza ...")``
        restituiva ``None``.
        """
        found = HeuristicBackend._find_marker("Per definizione la varianza e' la dispersione.")
        self.assertIsNotNone(found)

    def test_markers_are_found_in_any_case(self) -> None:
        for sentence in (
            "SIGNIFICA in maiuscolo.",
            "si intende il tempo di attesa.",
            "Ovvero il secondo termine.",
        ):
            with self.subTest(sentence=sentence):
                self.assertIsNotNone(HeuristicBackend._find_marker(sentence))

    def test_ascii_elision_keeps_the_definition_whole(self) -> None:
        """'e' definita come' matcha 16 caratteri per un marker da 15.

        La definizione va tagliata sulla lunghezza **matchata**, non su
        quella del marker canonico: misurato, tutti i 32 marker per tutte
        le varianti di elisione restituiscono la definizione intera.
        """
        out = HeuristicBackend().generate("definitions", LECTURE)
        self.assertIn("la dispersione dei dati", out)
        for line in out.splitlines():
            if "varianza" not in line:
                continue
            _, _, definition = line.partition("||")
            self.assertTrue(
                definition.strip().startswith("la dispersione"),
                f"definizione troncata: {definition!r}",
            )


# ---------------------------------------------------------------------------
# correzioni manuali
# ---------------------------------------------------------------------------


class TestCorrector(unittest.TestCase):
    """La correzione manuale non deve mai perdere il testo originale."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "corrections.json"

    def test_correcting_twice_without_a_store_keeps_the_original(self) -> None:
        """Senza store, ``_lookup_original`` torna '' e sovrascriveva.

        Misurato: la seconda correzione salvava ``original=''`` e ``revert``
        non aveva piu' nulla da ripristinare.
        """
        first = TranscriptCorrector(self.path)
        first.correct(42, "testo corretto", original="testo ASR originale")

        second = TranscriptCorrector(self.path)
        result = second.correct(42, "testo corretto di nuovo")
        self.assertEqual(result.original, "testo ASR originale")

    def test_revert_still_works_after_a_second_correction(self) -> None:
        TranscriptCorrector(self.path).correct(42, "primo", original="originale")
        corrector = TranscriptCorrector(self.path)
        corrector.correct(42, "secondo")
        reverted = corrector.revert(42)
        self.assertIsNotNone(reverted)
        self.assertEqual(reverted.original, "originale")
        self.assertEqual(reverted.corrected, "")
        self.assertEqual(corrector.mapping(), {})

    def test_an_explicit_original_wins_over_the_saved_one(self) -> None:
        """Chi passa l'originale a mano sa cosa sta facendo."""
        TranscriptCorrector(self.path).correct(7, "x", original="salvato")
        corrector = TranscriptCorrector(self.path)
        result = corrector.correct(7, "y", original="fornito")
        self.assertEqual(result.original, "fornito")

    def test_an_empty_correction_is_a_revert(self) -> None:
        corrector = TranscriptCorrector(self.path)
        corrector.correct(1, "testo", original="originale")
        result = corrector.correct(1, "")
        self.assertTrue(result.is_reverted)
        self.assertEqual(corrector.mapping(), {})

    def test_an_unknown_segment_reverts_to_nothing(self) -> None:
        corrector = TranscriptCorrector(self.path)
        self.assertIsNone(corrector.revert(999))

    def test_corrections_survive_a_reload(self) -> None:
        corrector = TranscriptCorrector(self.path)
        corrector.correct(3, "corretto", original="originale")
        reloaded = TranscriptCorrector(self.path)
        self.assertEqual(reloaded.corrected_text(3), "corretto")


# ---------------------------------------------------------------------------
# il guardiano della privacy
# ---------------------------------------------------------------------------


class TestEndpointIsLocal(unittest.TestCase):
    """Solo il loopback esplicito passa; l'host vuoto non e' un permesso."""

    def test_loopback_addresses_are_local(self) -> None:
        for endpoint in (
            "http://127.0.0.1:8080/v1",
            "http://localhost/v1",
            "http://[::1]/v1",
        ):
            with self.subTest(endpoint=endpoint):
                self.assertTrue(LocalLLMBackend(endpoint).is_local())

    def test_an_empty_or_unparseable_endpoint_is_not_local(self) -> None:
        """Era nell'insieme dei locali: con lui passavano anche 'file://'.

        Misurato: ``LocalLLMBackend("file:///etc/passwd").is_local()``
        tornava True, superava il guardiano e ``urlopen`` avrebbe aperto
        il file.
        """
        for endpoint in ("", "http://", "http:///v1", "not-a-url"):
            with self.subTest(endpoint=endpoint):
                self.assertFalse(LocalLLMBackend(endpoint).is_local())

    def test_a_non_http_scheme_is_not_local(self) -> None:
        self.assertFalse(LocalLLMBackend("file:///etc/passwd").is_local())

    def test_a_private_network_address_is_not_local(self) -> None:
        """192.168.x e' un'altra macchina della rete, non questo computer."""
        self.assertFalse(LocalLLMBackend("http://192.168.77.5/v1").is_local())

    def test_a_remote_endpoint_is_refused_for_privacy(self) -> None:
        backend = LocalLLMBackend("https://api.example.com/v1")
        self.assertFalse(backend.is_local())
        self.assertEqual(backend.generate("summary", "testo"), "")
        self.assertIn("privacy", (backend.last_error or "").lower())


# ---------------------------------------------------------------------------
# dizionario personalizzato
# ---------------------------------------------------------------------------


class TestCustomDictionary(unittest.TestCase):
    def setUp(self) -> None:
        self.path = Path(tempfile.mkdtemp()) / "dict.json"
        self.dictionary = CustomDictionary(self.path)

    def test_add_and_find_a_term(self) -> None:
        # ``terms`` conserva la grafia originale: la maiuscola serve a
        # faster-whisper per riconoscere il nome proprio nel prompt.
        self.assertTrue(self.dictionary.add("Trasformata di Fourier"))
        self.assertIn("Trasformata di Fourier", self.dictionary.terms)
        # il confronto per contenuto pero' ignora maiuscole e spazi
        self.assertIn("trasformata di fourier", self.dictionary)

    def test_add_is_idempotent(self) -> None:
        self.assertTrue(self.dictionary.add("varianza"))
        self.assertFalse(self.dictionary.add("varianza"))
        self.assertEqual(len(self.dictionary), 1)

    def test_remove_works_and_reports_misses(self) -> None:
        self.dictionary.add("varianza")
        self.assertTrue(self.dictionary.remove("varianza"))
        self.assertFalse(self.dictionary.remove("varianza"))

    def test_an_empty_term_is_refused(self) -> None:
        self.assertFalse(self.dictionary.add("   "))
        self.assertEqual(len(self.dictionary), 0)

    def test_terms_survive_a_reload(self) -> None:
        self.dictionary.add("autovalore")
        reloaded = CustomDictionary(self.path)
        self.assertIn("autovalore", reloaded.terms)

    def test_hotwords_prompt_respects_the_limit(self) -> None:
        for i in range(60):
            self.dictionary.add(f"termine moltissimo lungo numero {i}")
        prompt = self.dictionary.hotwords_prompt(max_chars=200)
        self.assertLessEqual(len(prompt), 200 + 20)

    def test_a_single_oversized_term_does_not_block_the_others(self) -> None:
        """Un termine piu' lungo del limite non deve azzerare il prompt.

        Misurato: il prompt si riempiva fino al primo termine troppo lungo,
        e tutti quelli dopo — anche cortissimi — venivano esclusi per sempre.
        """
        self.dictionary.add("x" * 500)
        self.dictionary.add("varianza")
        prompt = self.dictionary.hotwords_prompt(max_chars=200)
        self.assertIn("varianza", prompt)

    def test_a_non_utf8_file_is_ignored_not_fatal(self) -> None:
        """Un file salvato con un'altra codifica non deve far sollevare nulla.

        ``UnicodeDecodeError`` non e' una ``OSError`` ne' una
        ``JSONDecodeError``: finche' non la si catturava, il costruttore
        del dizionario — che carica in ``__init__`` — sollevava e
        l'applicazione non si apriva. Misurato con ``{"entries":
        [{"term": "caff\xe8"}]}``.
        """
        self.path.write_bytes(b'{"entries": [{"term": "caff\xe8"}]}')
        try:
            dictionary = CustomDictionary(self.path)
        except Exception as exc:  # pragma: no cover - il test deve passare
            self.fail(f"CustomDictionary ha sollevato: {exc!r}")
        self.assertEqual(len(dictionary), 0)

    def test_a_non_utf8_corrections_file_is_ignored_not_fatal(self) -> None:
        """Stessa cosa per le correzioni, che caricano allo stesso modo."""
        path = Path(tempfile.mkdtemp()) / "corrections.json"
        path.write_bytes(b'{"corrections": [{"wrong": "caff\xe8", "right": "x"}]}')
        try:
            corrector = TranscriptCorrector(path)
        except Exception as exc:  # pragma: no cover - il test deve passare
            self.fail(f"TranscriptCorrector ha sollevato: {exc!r}")
        self.assertEqual(corrector._corrections, {})


# ---------------------------------------------------------------------------
# materiale di studio: persistenza e robustezza
# ---------------------------------------------------------------------------


class TestStudyMaterial(unittest.TestCase):
    def test_from_dict_survives_missing_and_junk_fields(self) -> None:
        data = {
            "session_id": 1,
            "summary": "riassunto",
            "concepts": ["non un dict", {"term": "t", "evidence": "e"}],
            "definitions": None,
            "warnings": "non una lista",
        }
        material = StudyMaterial.from_dict(data)
        self.assertEqual(material.summary, "riassunto")
        self.assertEqual(len(material.concepts), 1)
        self.assertEqual(material.concepts[0].term, "t")

    def test_round_trip_through_json(self) -> None:
        original = StudyMaterial(session_id=3, backend="euristico")
        original.summary = "un riassunto"
        original.warnings = ["avviso"]
        path = Path(tempfile.mkdtemp()) / "material.json"
        original.save(path)
        loaded = StudyMaterial.load(path)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.summary, "un riassunto")
        self.assertEqual(loaded.warnings, ["avviso"])

    def test_load_of_a_missing_file_returns_none(self) -> None:
        self.assertIsNone(StudyMaterial.load(Path("/non/esiste.json")))


# ---------------------------------------------------------------------------
# parsing dell'output dei backend
# ---------------------------------------------------------------------------


class TestParsing(unittest.TestCase):
    def test_a_single_pipe_does_not_split_a_line_with_commas(self) -> None:
        """Una pipe nella lezione non deve spezzare la riga in due campi."""
        raw = "concetto || la frase, con una | pipe in mezzo"
        concepts = S._parse_concepts(raw)
        self.assertEqual(len(concepts), 1)
        self.assertIn("|", concepts[0].evidence)

    def test_an_empty_quiz_option_does_not_shift_the_answer(self) -> None:
        """Un'opzione vuota faceva slittare l'indice della risposta giusta."""
        raw = "Domanda? || a || || c || 0"
        questions = S._parse_quiz(raw)
        if questions:
            self.assertEqual(questions[0].answer, 0)

    def test_an_out_of_range_answer_does_not_produce_garbage(self) -> None:
        """chr(65+n) con n fuori range produce caratteri senza senso."""
        raw = "Domanda? || a || b || c || 9"
        questions = S._parse_quiz(raw)
        if questions:
            self.assertLessEqual(questions[0].answer, 2)


class TestAliasPrefixes(unittest.TestCase):
    """Il prefisso di sinonimo non deve mangiare una congiunzione.

    ``"o"`` era in ``_ALIAS_PREFIXES``: in italiano introduce
    un'alternativa, non un sinonimo. Misurato: "zero significa o assenza
    di valore" dava la definizione "assenza di valore" — la congiunzione
    persa, e con lei il senso della frase. I sinonimi introdotti da "o"
    si dicono ``ovvero``/``ossia``, che sono marcatori e vengono tolti
    dal marcatore, non da qui.
    """

    def test_a_leading_o_is_not_treated_as_an_alias(self) -> None:
        assistant = S.StudyAssistant()
        definitions = assistant.definitions("Zero significa o assenza di valore.")
        self.assertEqual(len(definitions), 1)
        self.assertEqual(definitions[0].definition, "o assenza di valore")

    def test_a_real_alias_prefix_is_still_stripped(self) -> None:
        self.assertEqual(S._strip_alias("anche matrice di Jacobi"), "matrice di Jacobi")
        self.assertEqual(
            S._strip_alias("detto anche tensore di curvatura"),
            "tensore di curvatura",
        )

    def test_ovvero_is_handled_by_the_marker_not_the_prefix(self) -> None:
        assistant = S.StudyAssistant()
        definitions = assistant.definitions("La norma e ovvero modulo.")
        self.assertEqual(definitions[0].definition, "modulo")


class TestTermExtraction(unittest.TestCase):
    """L'estrazione del termine: la testa del sintagma deve sopravvivere.

    Il difetto originario prendeva le ultime cinque parole (``words[-5:]``)
    dopo aver ripulito la testa: per un soggetto lungo restavano solo le
    preposizioni finali. Misurato: "il Teorema del limite centrale per la
    stima della varianza" dava "per la stima della varianza".
    """

    def _term(self, phrase: str) -> str:
        return " ".join(S._trim_term(phrase.split()))

    def test_the_head_noun_survives_a_long_subject(self) -> None:
        term = self._term("il Teorema del limite centrale per la stima della varianza")
        self.assertTrue(term.startswith("Teorema"), term)

    def test_a_leading_article_is_dropped(self) -> None:
        self.assertEqual(self._term("la varianza"), "varianza")

    def test_an_elided_article_is_stripped(self) -> None:
        # "L'emivita' del carbonio" -> "emivita' del carbonio"
        term = self._term("L'emivita' del carbonio")
        self.assertFalse(term.startswith("L'"), term)
        self.assertTrue(term.startswith("emivita"), term)

    def test_a_leading_verb_exposes_the_subordinate_which_is_also_dropped(self) -> None:
        # togliere il verbo mette a nudo "che", che va tolto a sua volta
        term = self._term("oggi vediamo che la varianza campionaria")
        self.assertEqual(term, "varianza campionaria")

    def test_the_term_does_not_end_on_a_preposition(self) -> None:
        for phrase in (
            "il Teorema del limite centrale per la stima della varianza",
            "la formula di calcolo del per",
            "il coefficiente di correlazione tra",
        ):
            term = self._term(phrase)
            last = term.rsplit(" ", 1)[-1]
            self.assertNotIn(
                S._fold(last),
                S._TRAILING_TRIM | S._TRAILING_COPULA,
                f"{phrase!r} termina con {last!r}",
            )

    def test_an_empty_input_stays_empty(self) -> None:
        self.assertEqual(self._term(""), "")

    def test_a_single_word_is_kept(self) -> None:
        self.assertEqual(self._term("caffeina"), "caffeina")

    def test_the_term_is_capped_at_five_words(self) -> None:
        term = self._term("uno due tre quattro cinque sei sette otto")
        self.assertLessEqual(len(term.split()), 5)


class TestSentenceSplitting(unittest.TestCase):
    """La spezzatura in frasi, con le abbreviazioni italiane.

    Questi test fissano il comportamento corretto; non riproducono un
    difetto misurato. Il sospetto era che la regola delle sigle brevi
    ("c.a", "a.c") guardasse parole di frasi gia' chiuse, perche' le
    contava su tutto il testo precedente al punto: provato su una dozzina
    di input, le due versioni danno sempre lo stesso risultato. Restano
    utili come rete: se la spezzatura si rompe, si vede qui.
    """

    def test_a_plain_sentence_splits(self) -> None:
        self.assertEqual(
            S._split_sentences("prima frase. seconda frase."),
            ["prima frase.", "seconda frase."],
        )

    def test_a_known_abbreviation_does_not_split(self) -> None:
        got = S._split_sentences("Il Prof. Rossi arriva. Poi tace.")
        self.assertEqual(got, ["Il Prof. Rossi arriva.", "Poi tace."])

    def test_the_abbreviation_text_is_not_dropped(self) -> None:
        """"Prof." deve restare nel testo, non diventare ". Rossi arriva"."""
        got = S._split_sentences("Il Prof. Rossi arriva.")
        self.assertEqual(got, ["Il Prof. Rossi arriva."])
        self.assertFalse(got[0].startswith("."), got)

    def test_a_later_sentence_is_not_decided_by_an_earlier_sigla(self) -> None:
        # la sigla "a.c." sta nella prima frase; la seconda finisce con
        # "risultato" e deve chiudere comunque
        got = S._split_sentences(
            "abbiamo visto ieri la formula generale. e poi a.c. il risultato."
        )
        self.assertEqual(len(got), 2, got)
        self.assertTrue(got[1].endswith("risultato."), got)

    def test_the_first_sentence_keeps_its_abbreviation(self) -> None:
        got = S._split_sentences("a.c. La varianza campionaria stima. Fine.")
        self.assertEqual(got[0], "a.c. La varianza campionaria stima.", got)
        self.assertEqual(got[1], "Fine.", got)

    def test_a_sentence_without_punctuation_is_returned_whole(self) -> None:
        self.assertEqual(
            S._split_sentences("una frase sola"), ["una frase sola"]
        )

    def test_empty_text_yields_no_sentences(self) -> None:
        self.assertEqual(S._split_sentences(""), [])
        self.assertEqual(S._split_sentences("   "), [])


    def test_the_summary_keeps_the_reading_order(self) -> None:
        """Il riassunto si legge nell'ordine in cui e' stata data la lezione.

        Con ``top.sort()`` le frasi finivano in ordine alfabetico:
        misurato, "Il teorema del limite centrale..." precedeva "La
        varianza campionaria..." anche se la varianza era stata spiegata
        prima.
        """
        sentences = [
            "La varianza campionaria misura la dispersione dei dati.",
            "Il teorema del limite centrale descrive la convergenza.",
            "La deviazione standard e' la radice della varianza.",
            "Una popolazione e' l'insieme di tutti gli elementi.",
        ]
        backend = S.HeuristicBackend()
        summary = backend._summary(sentences)
        bullets = [l[2:] for l in summary.splitlines() if l.startswith("- ")]
        self.assertEqual(bullets, sentences, bullets)


class TestQuizOptions(unittest.TestCase):
    """Opzioni duplicate: la risposta giusta non deve comparire due volte."""

    def test_duplicate_options_are_removed(self) -> None:
        raw = "Quanto vale la varianza? || positiva || positiva || negativa || 0"
        questions = S._parse_quiz(raw)
        self.assertEqual(len(questions), 1)
        self.assertEqual(len(set(questions[0].options)), len(questions[0].options))

    def test_the_answer_index_survives_the_dedup(self) -> None:
        raw = "Q? || positiva || negativa || positiva || 2"
        questions = S._parse_quiz(raw)
        q = questions[0]
        self.assertEqual(q.options[q.answer], "positiva")

    def test_a_question_without_duplicates_is_untouched(self) -> None:
        raw = "Qual e' la capitale? || Roma || Milano || Torino || 0"
        questions = S._parse_quiz(raw)
        self.assertEqual(questions[0].options, ("Roma", "Milano", "Torino"))
        self.assertEqual(questions[0].answer, 0)

    def test_all_identical_options_collapse_to_one(self) -> None:
        raw = "Q? || a || a || a || 0"
        questions = S._parse_quiz(raw)
        self.assertEqual(questions[0].options, ("a",))


class TestOptionLabels(unittest.TestCase):
    """L'etichetta dell'opzione deve restare una lettera.

    ``chr(65 + n)`` regge fino alla ventiseiesima opzione: dalla
    ventisettesima produce ``[``, ``\\``, ``]``. Misurato, una domanda
    con 30 opzioni (output di un modello locale) mostrava
    ``[risposta: []]`` — l'indice era corretto, la resa no. Ora si passa
    ad ``AA``, come nei fogli di lavoro.
    """

    def test_the_first_twenty_six_are_single_letters(self) -> None:
        self.assertEqual([S._option_label(i) for i in range(26)], list("ABCDEFGHIJKLMNOPQRSTUVWXYZ"))

    def test_the_twenty_seventh_is_two_letters(self) -> None:
        self.assertEqual(S._option_label(26), "AA")
        self.assertEqual(S._option_label(27), "AB")

    def test_a_negative_index_does_not_produce_garbage(self) -> None:
        self.assertEqual(S._option_label(-3), "A")

    def test_the_rendered_answer_is_a_letter(self) -> None:
        raw = "D? || " + " || ".join(f"opzione {i}" for i in range(30)) + " || 26"
        question = S._parse_quiz(raw)[0]
        self.assertEqual(question.answer_text, "opzione 26")
        self.assertIn("[risposta: AA]", str(question))
        self.assertNotIn("[]", str(question))


class TestEmptyFieldsKeepTheirPlace(unittest.TestCase):
    """Un campo vuoto non deve far scivolare i successivi.

    ``_split_fields`` scartava i campi vuoti, quindi una riga con
    un'opzione in piu' — che il modello locale emette per riempire —
    spostava di un posto tutto quello che veniva dopo. Misurato:
    ``"Domanda || A || || B || 2"`` (la risposta giusta e' ``B``, indice
    2) dava ``options=('A','B')`` con ``answer=2``: fuori range, e la
    domanda finiva per rispondere ``A``. Il sintomo peggiore non e'
    l'errore, e' che la risposta sbagliata veniva presentata come
    giusta.
    """

    def test_an_empty_option_does_not_shift_the_answer(self) -> None:
        raw = "Domanda || A || || B || 2"
        questions = S._parse_quiz(raw)
        self.assertEqual(len(questions), 1)
        q = questions[0]
        self.assertEqual(q.options, ("A", "B"))
        self.assertEqual(q.answer, 1)
        self.assertEqual(q.answer_text, "B")

    def test_a_leading_empty_option_is_dropped_not_shifted(self) -> None:
        raw = "Domanda || || A || 0"
        questions = S._parse_quiz(raw)
        self.assertEqual(questions[0].options, ("A",))
        self.assertEqual(questions[0].answer_text, "A")

    def test_a_clean_question_is_unaffected(self) -> None:
        raw = "D || A || B || C || 2"
        q = S._parse_quiz(raw)[0]
        self.assertEqual(q.options, ("A", "B", "C"))
        self.assertEqual(q.answer, 2)
        self.assertEqual(q.answer_text, "C")

    def test_a_definition_is_not_taken_from_the_evidence_field(self) -> None:
        """"Termine || || def": la definizione manca, ``def`` e' l'evidenza."""
        definitions = S._parse_definitions("Teorema || || enunciato")
        self.assertEqual(len(definitions), 1)
        self.assertEqual(definitions[0].definition, "")
        self.assertEqual(definitions[0].evidence, "Teorema || || enunciato")

    def test_a_present_definition_is_still_read(self) -> None:
        definitions = S._parse_definitions("Teorema || || afferma che")
        self.assertEqual(definitions[0].definition, "")
        definitions = S._parse_definitions("Teorema || afferma che")
        self.assertEqual(definitions[0].definition, "afferma che")

    def test_a_flashcard_with_an_empty_back_is_kept(self) -> None:
        cards = S._parse_flashcards("Fronte || ")
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].front, "Fronte")
        self.assertEqual(cards[0].back, "")

    def test_split_fields_keeps_empty_positions(self) -> None:
        self.assertEqual(S._split_fields("a || || b"), ["a", "", "b"])
        self.assertEqual(S._split_fields("a | | b"), ["a", "", "b"])
        self.assertEqual(S._split_fields("solo"), ["solo"])


if __name__ == "__main__":
    unittest.main()
