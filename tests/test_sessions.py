"""Test dell'archivio delle sessioni (SQLite su file temporaneo).

Copre: creazione, aggiunta segmenti, ricerca, segnalibro, nota,
cancellazione con cascata. Ogni test usa un file SQLite proprio in
``tmp_path`` (pytest) o in una directory temporanea (unittest), cosi'
nessuno stato persiste fra un test e l'altro.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# rende importabile ``lecture_transcriber`` (layout src/) e installa la
# shim di compatibilita' per soxr, anche sotto ``python -m unittest``
import _bootstrap  # noqa: F401,E402  (importa dopo l'inserimento nel path)

from lecture_transcriber.sessions import SessionStore


class SessionStoreTestCase(unittest.TestCase):
    """Base con un archivio temporaneo gia' pronto."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="lt-sessions-")
        self.db_path = Path(self._tmp.name) / "sessions.db"
        self.store = SessionStore(self.db_path)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def make_session(self, course: str = "Fisica", professor: str = "Rossi") -> int:
        return self.store.create_session(course, professor)


class TestCreateSession(SessionStoreTestCase):
    """Creazione e lettura delle sessioni."""

    def test_create_session_returns_id(self) -> None:
        sid = self.store.create_session("Analisi", "Verdi")
        self.assertIsInstance(sid, int)
        self.assertGreater(sid, 0)

    def test_session_fields_are_stored(self) -> None:
        sid = self.store.create_session("Analisi", "Verdi", date="2026-10-22")
        session = self.store.get_session(sid)
        assert session is not None
        self.assertEqual(session.course, "Analisi")
        self.assertEqual(session.professor, "Verdi")
        self.assertEqual(session.date, "2026-10-22")
        self.assertIsNotNone(session.started_at)
        self.assertIsNone(session.ended_at)

    def test_default_date_is_today(self) -> None:
        sid = self.store.create_session("Analisi", "Verdi")
        session = self.store.get_session(sid)
        assert session is not None
        self.assertRegex(session.date, r"^\d{4}-\d{2}-\d{2}$")

    def test_get_missing_session_returns_none(self) -> None:
        self.assertIsNone(self.store.get_session(9999))

    def test_end_session_sets_timestamp(self) -> None:
        sid = self.make_session()
        self.store.end_session(sid)
        session = self.store.get_session(sid)
        assert session is not None
        self.assertIsNotNone(session.ended_at)

    def test_list_sessions_is_newest_first(self) -> None:
        first = self.store.create_session("Prima", "A")
        second = self.store.create_session("Seconda", "B")
        sessions = self.store.list_sessions()
        self.assertEqual(len(sessions), 2)
        self.assertEqual(sessions[0].id, second)
        self.assertEqual(sessions[1].id, first)

    def test_session_title(self) -> None:
        sid = self.store.create_session("Fisica", "Rossi")
        session = self.store.get_session(sid)
        assert session is not None
        self.assertEqual(session.title, "Fisica — Rossi")

    def test_session_title_without_professor(self) -> None:
        sid = self.store.create_session("Fisica", "")
        session = self.store.get_session(sid)
        assert session is not None
        self.assertEqual(session.title, "Fisica")


class TestSegments(SessionStoreTestCase):
    """Aggiunta e lettura dei segmenti."""

    def setUp(self) -> None:
        super().setUp()
        self.sid = self.make_session()
        self.store.add_segment(self.sid, 0, 0.0, 3.5, "Buongiorno a tutti")
        self.store.add_segment(self.sid, 1, 3.5, 7.0, "Il sistema funziona in locale")

    def test_segments_are_returned_in_order(self) -> None:
        rows = self.store.get_segments(self.sid)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["idx"], 0)
        self.assertEqual(rows[1]["idx"], 1)
        self.assertEqual(rows[0]["text"], "Buongiorno a tutti")

    def test_segment_timestamps_are_stored(self) -> None:
        row = self.store.get_segments(self.sid)[0]
        self.assertAlmostEqual(row["t_start"], 0.0)
        self.assertAlmostEqual(row["t_end"], 3.5)

    def test_final_flag_is_stored(self) -> None:
        self.store.add_segment(self.sid, 2, 7.0, 9.0, "Ipotesi", final=False)
        rows = self.store.get_segments(self.sid)
        self.assertEqual(rows[0]["final"], 1)
        self.assertEqual(rows[2]["final"], 0)

    def test_segments_of_other_session_are_isolated(self) -> None:
        other = self.make_session("Altro", "Corso")
        self.assertEqual(self.store.get_segments(other), [])
        self.assertEqual(len(self.store.get_segments(self.sid)), 2)


class TestSearch(SessionStoreTestCase):
    """Ricerca testuale fra i segmenti di una sessione."""

    def setUp(self) -> None:
        super().setUp()
        self.sid = self.make_session()
        self.store.add_segment(self.sid, 0, 0.0, 3.0, "Benvenuti alla lezione")
        self.store.add_segment(self.sid, 1, 3.0, 6.0, "Oggi parliamo del modello")
        self.store.add_segment(self.sid, 2, 6.0, 9.0, "Il modello e' accurato")

    def test_search_finds_matching_segments(self) -> None:
        rows = self.store.search(self.sid, "modello")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["idx"], 1)

    def test_search_is_case_insensitive(self) -> None:
        rows = self.store.search(self.sid, "MODELLO")
        self.assertEqual(len(rows), 2)

    def test_search_matches_substring(self) -> None:
        rows = self.store.search(self.sid, "lezione")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["idx"], 0)

    def test_empty_query_returns_nothing(self) -> None:
        self.assertEqual(self.store.search(self.sid, ""), [])

    def test_no_match_returns_nothing(self) -> None:
        self.assertEqual(self.store.search(self.sid, "xyzzy"), [])

    def test_search_is_scoped_to_the_session(self) -> None:
        other = self.make_session("Altro", "Docente")
        self.store.add_segment(other, 0, 0.0, 1.0, "modello")
        rows = self.store.search(self.sid, "modello")
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row["session_id"], self.sid)


class TestBookmarksAndNotes(SessionStoreTestCase):
    """Segnalibri e note."""

    def setUp(self) -> None:
        super().setUp()
        self.sid = self.make_session()
        self.store.add_segment(self.sid, 0, 0.0, 3.0, "Primo segmento")
        self.store.add_segment(self.sid, 1, 3.0, 6.0, "Secondo segmento")
        self.rows = self.store.get_segments(self.sid)

    def test_set_bookmark_and_read_back(self) -> None:
        self.store.set_bookmark(self.rows[1]["id"], True)
        bookmarks = self.store.get_bookmarks(self.sid)
        self.assertEqual(len(bookmarks), 1)
        self.assertEqual(bookmarks[0]["idx"], 1)
        self.assertEqual(bookmarks[0]["bookmark"], 1)

    def test_bookmark_can_be_removed(self) -> None:
        self.store.set_bookmark(self.rows[0]["id"], True)
        self.store.set_bookmark(self.rows[0]["id"], False)
        self.assertEqual(self.store.get_bookmarks(self.sid), [])

    def test_bookmarks_are_scoped_to_the_session(self) -> None:
        other = self.make_session("Altro", "Docente")
        self.store.add_segment(other, 0, 0.0, 1.0, "Test")
        other_row = self.store.get_segments(other)[0]
        self.store.set_bookmark(other_row["id"], True)
        self.assertEqual(self.store.get_bookmarks(self.sid), [])
        self.assertEqual(len(self.store.get_bookmarks(other)), 1)

    def test_set_note_is_persisted(self) -> None:
        self.store.set_note(self.rows[0]["id"], "Da rivedere")
        row = self.store.get_segments(self.sid)[0]
        self.assertEqual(row["note"], "Da rivedere")

    def test_note_can_be_overwritten(self) -> None:
        self.store.set_note(self.rows[0]["id"], "Prima")
        self.store.set_note(self.rows[0]["id"], "Dopo")
        row = self.store.get_segments(self.sid)[0]
        self.assertEqual(row["note"], "Dopo")

    def test_default_note_is_empty(self) -> None:
        row = self.store.get_segments(self.sid)[0]
        self.assertEqual(row["note"], "")


class TestDeleteSession(SessionStoreTestCase):
    """La cancellazione deve rimuovere anche i segmenti (cascata)."""

    def setUp(self) -> None:
        super().setUp()
        self.sid = self.make_session()
        self.store.add_segment(self.sid, 0, 0.0, 3.0, "Primo")
        self.store.add_segment(self.sid, 1, 3.0, 6.0, "Secondo")

    def test_session_is_removed(self) -> None:
        self.store.delete_session(self.sid)
        self.assertIsNone(self.store.get_session(self.sid))
        self.assertEqual(self.store.list_sessions(), [])

    def test_segments_are_removed_with_the_session(self) -> None:
        self.store.delete_session(self.sid)
        self.assertEqual(self.store.get_segments(self.sid), [])

    def test_cascade_is_enforced_at_the_database_level(self) -> None:
        """La FOREIGN KEY con ON DELETE CASCADE pulisce i segmenti."""
        self.store._conn.execute("PRAGMA foreign_keys = ON")
        self.store.delete_session(self.sid)
        remaining = self.store._conn.execute(
            "SELECT COUNT(*) FROM segments WHERE session_id=?", (self.sid,)
        ).fetchone()[0]
        self.assertEqual(remaining, 0)

    def test_deleting_one_session_leaves_the_other(self) -> None:
        other = self.make_session("Altro", "Docente")
        self.store.add_segment(other, 0, 0.0, 1.0, "Sopravvive")

        self.store.delete_session(self.sid)

        self.assertIsNotNone(self.store.get_session(other))
        self.assertEqual(len(self.store.get_segments(other)), 1)

    def test_deleting_unknown_session_is_safe(self) -> None:
        self.store.delete_session(9999)
        self.assertEqual(len(self.store.list_sessions()), 1)


class TestPersistence(SessionStoreTestCase):
    """I dati sopravvivono alla riapertura del database."""

    def test_data_survives_reopen(self) -> None:
        sid = self.make_session("Chimica", "Neri")
        self.store.add_segment(sid, 0, 0.0, 2.0, "Reazione chimica")
        self.store.close()

        reopened = SessionStore(self.db_path)
        try:
            sessions = reopened.list_sessions()
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0].course, "Chimica")
            rows = reopened.get_segments(sid)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["text"], "Reazione chimica")
        finally:
            reopened.close()
        # tearDown richiude di nuovo: harmless, ma lo evitiamo
        self._closed = True

    def tearDown(self) -> None:
        if not getattr(self, "_closed", False):
            self.store.close()
        self._tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
