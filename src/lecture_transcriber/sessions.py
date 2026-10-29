"""Gestione delle sessioni di lezione e della cronologia.

La cronologia deve restare consultabile senza interrompere la trascrizione:
per questo i segmenti vengono scritti su SQLite in modo incrementale (con
commit frequenti) e le query di ricerca non bloccano mai l'UI per piu' di
qualche millisecondo.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path


def _escape_like(text: str) -> str:
    """Neutralizza i metacaratteri di LIKE (``%``, ``_``) in una ricerca.

    Il carattere di escape va dichiarato nella query (``ESCAPE '\\'``) e
    deve essere esso stesso escapato per primo, altrimenti una barra
    rovescia nella ricerca rovescerebbe il carattere successivo.
    """
    return (
        text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )


@dataclass
class Session:
    id: int
    course: str
    professor: str
    date: str
    started_at: float
    ended_at: float | None = None

    @property
    def title(self) -> str:
        base = self.course or "Lezione"
        if self.professor:
            return f"{base} — {self.professor}"
        return base


class SessionStore:
    """Archivio locale delle sessioni in SQLite.

    Un'unica connessione protetta da un lock: SQLite in modalita WAL regge
    bene un writer (la trascrizione) e lettori (la cronologia) contemporanei.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            # ON DELETE CASCADE e' dichiarato nello schema, ma SQLite lo
            # applica solo se le foreign key sono attive sulla connessione:
            # il PRAGMA e' spento per default. Senza questa riga, cancellare
            # una sessione lasciava orfani tutti i suoi segmenti — misurato:
            # dopo delete_session() i segmenti restavano nel database e
            # ricomparivano nella cronologia come righe senza sessione.
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._create_schema()

    def _create_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    course TEXT NOT NULL DEFAULT '',
                    professor TEXT NOT NULL DEFAULT '',
                    date TEXT NOT NULL DEFAULT '',
                    started_at REAL NOT NULL,
                    ended_at REAL
                );
                CREATE TABLE IF NOT EXISTS segments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    idx INTEGER NOT NULL,
                    t_start REAL NOT NULL,
                    t_end REAL NOT NULL,
                    text TEXT NOT NULL,
                    final INTEGER NOT NULL DEFAULT 1,
                    bookmark INTEGER NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_segments_session
                    ON segments(session_id, idx);
                CREATE INDEX IF NOT EXISTS idx_segments_bookmark
                    ON segments(session_id, bookmark);
                """
            )
            self._conn.commit()

    # -- sessioni ----------------------------------------------------------
    def create_session(self, course: str, professor: str, date: str = "") -> int:
        started = time.time()
        if not date:
            date = time.strftime("%Y-%m-%d", time.localtime(started))
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO sessions (course, professor, date, started_at) VALUES (?,?,?,?)",
                (course, professor, date, started),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def end_session(self, session_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET ended_at=? WHERE id=?", (time.time(), session_id)
            )
            self._conn.commit()

    def list_sessions(self) -> list[Session]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM sessions ORDER BY started_at DESC"
            ).fetchall()
        return [
            Session(
                id=r["id"],
                course=r["course"],
                professor=r["professor"],
                date=r["date"],
                started_at=r["started_at"],
                ended_at=r["ended_at"],
            )
            for r in rows
        ]

    def get_session(self, session_id: int) -> Session | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
        if r is None:
            return None
        return Session(
            id=r["id"],
            course=r["course"],
            professor=r["professor"],
            date=r["date"],
            started_at=r["started_at"],
            ended_at=r["ended_at"],
        )

    def delete_session(self, session_id: int) -> None:
        """Cancellazione definitiva di una sessione e dei suoi segmenti."""
        with self._lock:
            self._conn.execute("DELETE FROM segments WHERE session_id=?", (session_id,))
            self._conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            self._conn.commit()

    # -- segmenti ----------------------------------------------------------
    def add_segment(
        self,
        session_id: int,
        idx: int,
        t_start: float,
        t_end: float,
        text: str,
        final: bool = True,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO segments (session_id, idx, t_start, t_end, text, final)"
                " VALUES (?,?,?,?,?,?)",
                (session_id, idx, t_start, t_end, text, 1 if final else 0),
            )
            self._conn.commit()

    def get_segments(self, session_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM segments WHERE session_id=? ORDER BY idx", (session_id,)
            ).fetchall()

    def search(self, session_id: int, query: str) -> list[sqlite3.Row]:
        """Ricerca full-text semplice (LIKE) fra i segmenti della sessione.

        ``_`` e ``%`` sono metacaratteri di LIKE: senza escape una
        ricerca contenente un underscore trovava **tutto** (misurato:
        ``search(sid, "_")`` restituiva ogni segmento, perche' ``_`` matcha
        un carattere qualunque). L'utente che cerca un termine con un
        underscore — nomi di variabili, formule, ``t_start`` — otteneva
        l'intera lezione invece delle righe che lo contengono.
        """
        if not query:
            return []
        pattern = f"%{_escape_like(query)}%"
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM segments WHERE session_id=? AND text LIKE ? ESCAPE '\\'"
                " ORDER BY idx",
                (session_id, pattern),
            ).fetchall()

    def set_bookmark(self, segment_id: int, bookmark: bool = True) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE segments SET bookmark=? WHERE id=?", (1 if bookmark else 0, segment_id)
            )
            self._conn.commit()

    def set_note(self, segment_id: int, note: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE segments SET note=? WHERE id=?", (note, segment_id))
            self._conn.commit()

    def get_bookmarks(self, session_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM segments WHERE session_id=? AND bookmark=1 ORDER BY idx",
                (session_id,),
            ).fetchall()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
