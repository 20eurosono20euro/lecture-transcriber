"""Test del punto d'ingresso a riga di comando del modulo di studio.

Difetto coperto: :mod:`lecture_transcriber.study` era completo e collaudato
(70 test) ma **nessun percorso utente lo raggiungeva** -- ne' la GUI ne' una
CLI lo importavano. Una funzione che esiste e che nessuno puo' chiamare e'
una funzione assente, con l'aggravante che la roadmap la segnava come fatta.

I test verificano l'ingresso vero (``scripts/study_session.py``), non il
modulo: che legga la sessione dal database, che produca materiale, che i due
formati siano leggibili, e che un identificativo sbagliato esca con un
codice distinto invece di stampare un traceback.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _bootstrap  # noqa: F401,E402

from lecture_transcriber import config as configmod  # noqa: E402
from lecture_transcriber.sessions import SessionStore  # noqa: E402
from lecture_transcriber.study import StudyMaterial  # noqa: E402

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "study_session.py"


def _load_script():
    """Importa lo script per percorso (``scripts/`` non e' un pacchetto)."""
    spec = importlib.util.spec_from_file_location("study_session_cli", _SCRIPT)
    if spec is None or spec.loader is None:  # pragma: no cover - difesa
        raise RuntimeError(f"impossibile caricare {_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StudySessionCliTest(unittest.TestCase):
    """La CLI produce il materiale della lezione, o dice perche' non puo'."""

    LEZIONE = (
        ("La probabilita' e' una misura della certezza di un evento.", 0.0, 4.0),
        (
            "Il teorema di Bayes permette di aggiornare la probabilita' a "
            "posteriori partendo dalla probabilita' a priori.",
            4.0,
            9.0,
        ),
        ("La varianza misura la dispersione dei valori attorno alla media.", 9.0, 13.0),
    )

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        patcher = self._patch_data_dir(self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cli = _load_script()
        self.session_id = self._make_session()

    @staticmethod
    def _patch_data_dir(path: Path):
        """Sostituisce ``config.DATA_DIR``: lo script lo legge a ogni chiamata."""
        import unittest.mock as mock

        return mock.patch.object(configmod, "DATA_DIR", path)

    def _make_session(self) -> int:
        store = SessionStore(self.dir / "sessions.db")
        try:
            sid = store.create_session("Analisi II", "Rossi", "2026-11-19")
            for index, (text, start, end) in enumerate(self.LEZIONE):
                store.add_segment(sid, index, start, end, text, True)
            store.end_session(sid)
        finally:
            store.close()
        return sid

    def _run(self, *argv: object) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = self.cli.main([str(a) for a in argv])
            except SystemExit as exc:  # argparse chiama exit() sui suoi errori
                code = int(exc.code or 0)
        return code, out.getvalue(), err.getvalue()

    # -- elenco ------------------------------------------------------------
    def test_list_names_the_saved_lessons(self) -> None:
        code, out, _err = self._run("--list")
        self.assertEqual(code, 0)
        self.assertIn("Analisi II", out)
        self.assertRegex(out, r"\b3 segmenti")

    def test_list_on_an_empty_store_says_so_and_fails(self) -> None:
        """Nessuna lezione non e' un successo: chi automatizza deve accorgersene."""
        with self._patch_data_dir(self.dir / "vuoto"):
            code, _out, err = self._run("--list")
        self.assertEqual(code, 1)
        self.assertIn("Nessuna lezione", err)

    # -- materiale ---------------------------------------------------------
    def test_markdown_goes_to_stdout(self) -> None:
        code, out, _err = self._run(self.session_id, "--format", "md")
        self.assertEqual(code, 0)
        self.assertIn("# Materiale di studio", out)
        self.assertIn("Analisi II", out)
        # il backend euristico non richiede rete ne' modelli
        self.assertIn("heuristic", out)

    def test_markdown_file_is_written_where_asked(self) -> None:
        path = self.dir / "uscita" / "studio.md"
        code, out, _err = self._run(
            self.session_id, "--format", "md", "-o", path
        )
        self.assertEqual(code, 0)
        self.assertTrue(path.is_file(), "il file non e' stato scritto")
        self.assertIn("Materiale di studio", path.read_text(encoding="utf-8"))
        self.assertIn(str(path), out)

    def test_json_is_written_to_the_data_dir_by_default(self) -> None:
        code, out, _err = self._run(self.session_id)
        self.assertEqual(code, 0)
        path = self.dir / "study" / f"session_{self.session_id}.json"
        self.assertTrue(path.is_file(), "il JSON non e' stato scritto")
        self.assertIn(str(path), out)
        material = StudyMaterial.load(path)
        assert material is not None, "il JSON scritto non e' rileggibile"
        self.assertEqual(material.session_id, self.session_id)

    def test_json_content_is_valid_and_reflects_the_lesson(self) -> None:
        path = self.dir / "materiale.json"
        code, _out, _err = self._run(self.session_id, "-o", path)
        self.assertEqual(code, 0)
        material = StudyMaterial.load(path)
        assert material is not None, "il JSON scritto non e' rileggibile"
        body = " ".join(
            [material.summary]
            + [c.term for c in material.concepts]
            + [f.front for f in material.flashcards]
        ).lower()
        self.assertIn("probabilita", body)

    # -- errori ------------------------------------------------------------
    def test_a_missing_lesson_exits_1_without_a_traceback(self) -> None:
        code, _out, err = self._run(999)
        self.assertEqual(code, 1)
        self.assertIn("inesistente", err)
        self.assertNotIn("Traceback", err)

    def test_an_empty_lesson_exits_1(self) -> None:
        store = SessionStore(self.dir / "sessions.db")
        try:
            sid = store.create_session("Vuota", "", "")
            store.end_session(sid)
        finally:
            store.close()
        code, _out, err = self._run(sid)
        self.assertEqual(code, 1)
        self.assertIn("non ha ancora trascrizione", err)

    def test_no_arguments_is_a_usage_error(self) -> None:
        code, _out, err = self._run()
        self.assertNotEqual(code, 0)
        self.assertIn("--list", err)

    def test_markdown_renderer_covers_every_section(self) -> None:
        """Il Markdown deve mostrare tutte le sezioni, non solo il riassunto."""
        from lecture_transcriber.study import (
            Concept,
            Definition,
            Flashcard,
            QuizQuestion,
        )

        material = StudyMaterial(
            summary="Un riassunto.",
            concepts=[Concept("Bayes", "la frase che lo sostiene")],
            definitions=[Definition("Varianza", "la dispersione")],
            flashcards=[Flashcard("Fronte", "Retro")],
            quiz=[QuizQuestion("Domanda?", ("A", "B"), 1, "perche' B")],
            session_id=1,
            backend="heuristic",
        )
        text = self.cli.render_markdown(material, title="Analisi II")
        for heading in (
            "## Riassunto",
            "## Concetti principali",
            "## Definizioni",
            "## Flashcard",
            "## Quiz",
        ):
            self.assertIn(heading, text)
        # la risposta del quiz e' segnata, non solo elencata
        self.assertIn("✓", text)
        self.assertIn("B) B", text)


if __name__ == "__main__":
    unittest.main()
