#!/usr/bin/env python3
"""Smoke test per i moduli ``export`` e ``study``.

Crea una sessione SQLite temporanea con qualche segmento (con segnalibro e
nota), la esporta in tutti i formati e prova il backend euristico su un
testo di lezione italiano. Non richiede rete, modelli o servizi esterni.

    .venv/bin/python scripts/smoke_export_study.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from lecture_transcriber.export import Exporter, ExportError, safe_filename  # noqa: E402
from lecture_transcriber.sessions import SessionStore  # noqa: E402
from lecture_transcriber.study import (  # noqa: E402
    CustomDictionary,
    HeuristicBackend,
    LocalLLMBackend,
    StudyAssistant,
    StudyConfig,
    TranscriptCorrector,
    build_prompt,
    session_transcript,
)

LEZIONE = """
Oggi parliamo di inferenza statistica. La probabilita' e' una misura della
certezza di un evento. Il teorema di Bayes permette di aggiornare le
probabilita' a priorio quando arriva nuova evidenza.
Il teorema di Bayes e' definito come la regola che lega la probabilita'
condizionata di un evento alla probabilita' a priori.
La varianza e' definita come la misura della dispersione di una variabile
casuale intorno alla sua media. Uno scarto quadratico medio e' indicato come
la radice quadrata della varianza.
La probabilita' condizionata significa la probabilita' di un evento sapendo
che un altro evento si e' verificato.
Il teorema di Bayes si usa in diagnostica medica, nei filtri antispam e
nella classificazione automatica. La probabilita' a posteriori e' il termine
usato per la probabilita' aggiornata.
"""


def banner(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


class FakeLLMServer:
    """Server finto con API OpenAI-compatible, per provare LocalLLMBackend.

    Risponde a ``GET /v1/models`` e a ``POST /v1/chat/completions``. Sta
    in ascolto su 127.0.0.1 su una porta libera: nessun accesso alla rete
    esterna.
    """

    def __init__(self) -> None:
        self.requests: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass  # silenzia il log su stderr

            def _send(self, payload: dict) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802 (nome imposto da http.server)
                if self.path.endswith("/models"):
                    self._send({"data": [{"id": "fake"}]})
                else:
                    self.send_error(404)

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode("utf-8")
                outer.requests.append(raw)
                content = "MODELLO LOCALE: riassunto di prova."
                self._send(
                    {
                        "choices": [{"message": {"role": "assistant", "content": content}}]
                    }
                )

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> "FakeLLMServer":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def build_store(tmp: Path) -> tuple[SessionStore, int]:
    """Crea una sessione di prova con segnalibri e note."""
    store = SessionStore(tmp / "sessions.db")
    session_id = store.create_session(
        "Analisi Statistica I", "Prof. Maria Rossi", "2026-10-20"
    )
    segments = [
        (0.0, 7.4, "Benvenuti alla lezione di analisi statistica."),
        (7.4, 15.2, "Oggi introduciamo il teorema di Bayes e la probabilita' condizionata."),
        (15.2, 24.8, "Il teorema di Bayes e' definito come la regola che aggiorna la probabilita'."),
        (24.8, 33.0, "La varianza e' definita come la dispersione attorno alla media."),
    ]
    for idx, (start, end, text) in enumerate(segments):
        store.add_segment(session_id, idx, start, end, text)

    rows = store.get_segments(session_id)
    store.set_bookmark(int(rows[1]["id"]), True)
    store.set_note(int(rows[1]["id"]), "Rivedere la formula")
    store.set_note(int(rows[3]["id"]), "Chiedere il MODE della varianza")
    return store, session_id


def test_export(tmp: Path) -> None:
    banner("1. ESPORTAZIONE")
    store, session_id = build_store(tmp)

    exporter = Exporter(store)
    out_dir = tmp / "export"
    out_dir.mkdir(parents=True, exist_ok=True)

    for fmt in ("txt", "md", "md_ts", "srt"):
        path = exporter.export(session_id, fmt, out_dir / f"prova.{fmt}")
        size = path.stat().st_size
        print(f"  {fmt:6s} -> {path.name} ({size} byte)")
        assert path.exists() and size > 0, f"export {fmt} vuoto"

    default_path = exporter.export_default(session_id, out_dir)
    print(f"  default (md_ts) -> {default_path.name}")
    assert default_path.suffix == ".md", "il default deve essere un .md"
    assert "-ts.md" in default_path.name, "il default deve distinguerlo dal .md"

    banner("1a. TXT")
    text = exporter.to_text(session_id)
    print(text)
    assert "[00:00]" in text, "il TXT deve avere i timestamp"
    assert "★" in text, "il TXT deve marcare i segnalibri"
    assert "Rivedere la formula" in text, "il TXT deve includere le note"
    assert "Analisi Statistica I" in text, "il TXT deve avere l'intestazione"

    banner("1b. MD con timestamp")
    md_ts = exporter.to_markdown_ts(session_id)
    print(md_ts)
    assert "**[00:00]**" in md_ts, "il MD_ts deve avere i timestamp inline"
    assert md_ts.startswith("# "), "il MD deve iniziare con un titolo"
    assert "**Corso:**" in md_ts, "il MD deve avere i metadati"
    assert "✎ nota:" in md_ts, "il MD deve includere le note"

    banner("1c. SRT")
    srt = exporter.to_srt(session_id)
    print(srt)
    assert srt.startswith("1\n00:00:00,000 --> 00:00:07,400"), "SRT malformato"
    assert srt.count("-->") == 4, "il SRT deve avere 4 cue"

    banner("1d. Nome file sicuro")
    name = safe_filename("Analisi/Statistica I", "Prof. Mario Rossi", "22/10/2026")
    print(f"  'Analisi/Statistica I' + 'Prof. Mario Rossi' + '22/10/2026'")
    print(f"    -> {name}")
    assert "/" not in name and "\\" not in name, "il nome non deve contenere slash"
    assert "Analisi" in name and "Rossi" in name, "il nome deve conservare le parti"
    ascii_name = safe_filename("Perché è così", "", "", keep_accents=False)
    print(f"  'Perche e cosi' senza accenti -> {ascii_name}")
    assert ascii_name.isascii(), "senza accenti il nome deve essere ASCII"

    banner("1e. Formato ignoto")
    try:
        exporter.export(session_id, "pdf", out_dir / "x.pdf")
        raise AssertionError("doveva sollevare ExportError")
    except ExportError as exc:
        print(f"  ExportError come previsto: {exc}")

    store.close()


def test_study(tmp: Path) -> None:
    banner("2. BACKEND EURISTICO (offline, senza modelli)")
    backend = HeuristicBackend()

    prompt_summary = build_prompt("summary", LEZIONE)
    print("\n-- riassunto --")
    print(backend.generate("summary", prompt_summary))

    print("\n-- concetti --")
    raw = backend.generate("concepts", prompt_summary)
    print(raw)

    print("\n-- definizioni --")
    raw = backend.generate("definitions", prompt_summary)
    print(raw)

    print("\n-- flashcard --")
    raw = backend.generate("flashcards", prompt_summary)
    for line in raw.splitlines():
        print("  " + line)

    print("\n-- quiz --")
    raw = backend.generate("quiz", prompt_summary)
    for line in raw.splitlines():
        print("  " + line)

    # verifiche: il backend euristico deve produrre risultati usabili
    concepts = backend.generate("concepts", prompt_summary).splitlines()
    definitions = backend.generate("definitions", prompt_summary).splitlines()
    flashcards = backend.generate("flashcards", prompt_summary).splitlines()
    quiz = backend.generate("quiz", prompt_summary).splitlines()
    assert len(concepts) >= 3, "servono almeno 3 concetti"
    assert len(definitions) >= 3, "servono almeno 3 definizioni"
    assert len(flashcards) >= 3, "servono almeno 3 flashcard"
    assert len(quiz) >= 1, "serve almeno 1 domanda di quiz"
    assert any("teorema di bayes" in line.lower() for line in concepts), (
        "il concetto 'teorema di Bayes' deve comparire"
    )
    assert any("varianza" in line.lower() for line in definitions), (
        "la definizione della varianza deve comparire"
    )
    assert all("||" in line for line in concepts + definitions), (
        "il formato canonico deve essere 'termine || resto'"
    )
    print("\n  verifiche superate.")


def test_assistant(tmp: Path) -> None:
    banner("3. STUDY ASSISTANT (backend euristico forzato)")
    store, session_id = build_store(tmp)
    config = StudyConfig(enabled=True, backend="heuristic")
    assistant = StudyAssistant(config, store=store)

    print(f"  enabled={assistant.enabled}  backend={assistant.backend_name}")
    print(f"  is_available={assistant.is_available}")
    assert assistant.is_available, "il backend euristico e' sempre disponibile"

    text = session_transcript(store, session_id)
    material = assistant.study(text, session_id=session_id)
    print(f"  riepilogo: {len(material.concepts)} concetti, "
          f"{len(material.definitions)} definizioni, "
          f"{len(material.flashcards)} flashcard, {len(material.quiz)} domande")
    for concept in material.concepts[:3]:
        print(f"    concetto: {concept.term}")
    for definition in material.definitions[:3]:
        print(f"    definizione: {definition.term} -> {definition.definition[:60]}")
    assert material.summary, "il riassunto non deve essere vuoto"
    assert material.quiz, "devono esserci domande di quiz"

    banner("3a. MODULO SPENTO (default)")
    off = StudyAssistant(StudyConfig(), store=store)
    empty = off.study(text)
    print(f"  enabled={off.enabled}  avvisi: {empty.warnings}")
    assert not off.enabled, "il modulo deve essere spento per impostazione predefinita"
    assert empty.warnings, "il modulo spento deve avvisare"

    banner("3b. BACKEND LLM LOCALE (server assente: degrada senza errori)")
    local = StudyAssistant(
        StudyConfig(enabled=True, backend="local", llm_endpoint="http://127.0.0.1:9/v1"),
        store=store,
    )
    print(f"  backend risolto: {local.backend_name}")
    summary = local.summarize(text)
    print(f"  riassunto degradato ({len(summary)} caratteri), ultimo errore: {local.last_error}")
    assert summary, "il backend locale assente deve degradare all'euristico"
    assert local.last_error, "l'errore di rete deve essere registrato"

    banner("3c. BACKEND LLM LOCALE (server finto: deve essere usato)")
    with FakeLLMServer() as server:
        fake = StudyAssistant(
            StudyConfig(
                enabled=True,
                backend="auto",
                llm_endpoint=f"http://127.0.0.1:{server.port}/v1",
                llm_model="fake",
            ),
            store=store,
        )
        print(f"  backend risolto: {fake.backend_name}")
        print(f"  is_available: {fake.is_available}")
        assert fake.backend_name == "local-llm", "con il server attivo serve il LLM locale"
        print(f"  riassunto dal modello: {fake.summarize(text)[:80]}")
        assert "MODELLO LOCALE" in fake.summarize(text), "deve arrivare la risposta del server"
        print(f"  richieste ricevute: {server.requests}")

    banner("3d. ENDPOINT NON LOCALE RIFIUTATO (privacy)")
    remote = LocalLLMBackend(endpoint="https://api.example.com/v1")
    print(f"  is_local: {remote.is_local()}")
    assert not remote.is_local(), "un endpoint remoto non e' locale"
    assert remote.generate("summary", "x") == "", "il backend deve rifiutare l'invio"
    assert remote.last_error, "deve spiegare il rifiuto"
    print(f"  errore: {remote.last_error}")
    store.close()


def test_dictionary_and_corrections(tmp: Path) -> None:
    banner("4. DIZIONARIO PERSONALIZZATO")
    dictionary = CustomDictionary(tmp / "dictionary.json")
    for term in ("teorema di Bayes", "Markov", "entropia", "onnxruntime"):
        dictionary.add(term)
    print(f"  voci: {dictionary.terms}")
    assert len(dictionary) == 4, "devono esserci 4 voci"
    assert not dictionary.add("Markov"), "un termine duplicato non deve essere aggiunto"
    assert "markov" in dictionary, "la ricerca deve essere case-insensitive"

    prompt = dictionary.hotwords_prompt()
    print(f"  hotwords_prompt ({len(prompt)} caratteri): {prompt!r}")
    assert len(prompt) <= 200, "il prompt deve stare entro 200 caratteri"
    assert "teorema di Bayes" in prompt, "il prompt deve contenere i termini"

    # il limite deve troncare senza spezzare i termini
    long_dict = CustomDictionary(tmp / "dictionary2.json")
    for i in range(40):
        long_dict.add(f"termine-tecnico-lunghissimo-{i:02d}")
    bounded = long_dict.hotwords_prompt()
    print(f"  prompt limitato ({len(bounded)} caratteri): {bounded[:80]}...")
    assert len(bounded) <= 200, "il prompt troncato deve stare entro 200 caratteri"
    assert not bounded.endswith("-"), "il troncamento non deve spezzare un termine"

    print(f"  asr_initial_prompt: {dictionary.asr_initial_prompt('contesto precedente')!r}")
    assert dictionary.asr_initial_prompt("") == prompt, "senza contesto resta il prompt"
    empty = CustomDictionary(tmp / "dictionary3.json")
    assert empty.asr_initial_prompt("") is None, "senza termini il prompt e' None"

    print(f"  rimozione 'Markov': {dictionary.remove('Markov')} -> {dictionary.terms}")
    assert "Markov" not in dictionary, "il termine deve essere stato rimosso"

    # persistenza
    dictionary.save()
    reloaded = CustomDictionary(tmp / "dictionary.json")
    print(f"  ricaricato dal disco: {reloaded.terms}")
    assert reloaded.terms == dictionary.terms, "il dizionario deve sopravvivere al riavvio"

    banner("5. CORREZIONI MANUALI (originale conservato)")
    store, session_id = build_store(tmp)
    corrector = TranscriptCorrector(tmp / "corrections.json", store=store)
    rows = store.get_segments(session_id)
    segment_id = int(rows[2]["id"])
    original = rows[2]["text"]
    correction = corrector.correct(
        segment_id, "Il teorema di Bayes e' definito come la regola che aggiorna la probabilita' a posteriori."
    )
    print(f"  originale : {original}")
    print(f"  corretto  : {correction.corrected}")
    print(f"  coppia conservata: {corrector.get(segment_id) is not None}")
    assert correction.original == original, "l'originale deve restare in chiaro"
    assert correction.corrected != original, "la correzione deve valere"
    assert segment_id in corrector.mapping(), "la mappa deve contenere il segmento"
    assert "a posteriori" in corrector.session_text(session_id), (
        "il testo della sessione deve usare la correzione"
    )

    # la correzione deve sopravvivere al riavvio
    reloaded_corr = TranscriptCorrector(tmp / "corrections.json", store=store)
    assert reloaded_corr.get(segment_id) is not None, "la correzione deve essere persistita"
    print("  correzione ricaricata dal disco: OK")

    corrector.revert(segment_id)
    print(f"  dopo revert, attive: {len(corrector.active())}")
    assert not corrector.active(), "il revert deve disattivare la correzione"
    assert corrector.get(segment_id) is not None, "il revert deve conservare la traccia"
    assert corrector.get(segment_id).original == original, (
        "il revert deve conservare l'originale"
    )
    assert original in corrector.session_text(session_id), (
        "dopo il revert il testo originale torna"
    )
    print(f"  testo sessione con correzioni: {corrector.session_text(session_id)[:120]}...")
    store.close()


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="lt-smoke-") as tmpdir:
        tmp = Path(tmpdir)
        test_export(tmp)
        test_study(tmp)
        test_assistant(tmp)
        test_dictionary_and_corrections(tmp)
    print()
    print("Smoke test completato senza errori.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
