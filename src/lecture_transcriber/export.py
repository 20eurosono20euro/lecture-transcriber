"""Esportazione della trascrizione in formati leggibili (TXT, Markdown, SRT).

L'esportazione e' il punto in cui la trascrizione esce dal database e diventa
un artefatto riusabile: appunti per Obsidian, testo da rileggere, sottotitoli
per rivedere una lezione. Nessuna operazione richiede la rete: tutto avviene
in locale e in modo deterministico a partire dai segmenti salvati.

Formati supportati:

``txt``
    Testo semplice con un'intestazione (corso, docente, data) e una riga per
    segmento con il timestamp iniziale. E' il formato piu' robusto: si
    rilegge in qualsiasi editor e non patisce la punteggiatura speciale.

``md``
    Markdown "pulito": un titolo, un blocco di metadati e un paragrafo per
    segmento, senza timestamp. Va bene per gli appunti.

``md_ts``
    Markdown con timestamp inline (``**[12:34]** testo``): permette di
    citare un momento preciso della lezione.

``srt``
    Sottotitoli con inizio/fine di ogni segmento. Usabile con i player
    video e con ``ffmpeg``.

Convenzioni comuni a tutti i formati:

* i segmenti con segnalibro sono preceduti dal marcatore ``★``;
* le note personali del segmento (``sessions.Segment.note``) compaiono in
  una riga separata con il marcatore ``✎``;
* i segmenti non finali (ipotesi provvisoria) sono esclusi per default:
  un'exportazione non deve contenere testo che il motore ASR non ha
  ancora consolidato.
"""

from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .sessions import Session, SessionStore

__all__ = [
    "BOOKMARK_MARKER",
    "NOTE_MARKER",
    "ExportError",
    "ExportedSegment",
    "Exporter",
    "format_duration",
    "format_srt_time",
    "format_timestamp",
    "safe_filename",
]

#: Marcatore dei segmenti con segnalibro (in tutti i formati).
BOOKMARK_MARKER = "★"

#: Marcatore delle note personali (in tutti i formati).
NOTE_MARKER = "✎"

#: Separatore fra i metadati nelle intestazioni.
_FIELD_SEP = " | "

_ILLEGAL_CHARS = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]')
_WHITESPACE = re.compile(r"\s+")
_DASHES = re.compile(r"-{2,}")
_ONLY_PUNCT = re.compile(r"^[\s.\-–—_]+$")


class ExportError(Exception):
    """Errore durante l'esportazione (sessione inesistente, formato ignoto)."""


@dataclass(frozen=True)
class ExportedSegment:
    """Un segmento pronto per l'esportazione.

    E' una copia immutabile della riga del database: l'export lavora su
    questi oggetti e non tocca mai la sessione.
    """

    index: int
    start: float
    end: float
    text: str
    bookmark: bool = False
    note: str = ""
    final: bool = True

    @property
    def timestamp(self) -> str:
        """Timestamp iniziale ``MM:SS`` (stesso formato di ``asr.TranscriptSegment``)."""
        return format_timestamp(self.start)


# ---------------------------------------------------------------------------
# formattazione dei singoli campi
# ---------------------------------------------------------------------------
def format_timestamp(seconds: float) -> str:
    """Minuti e secondi (``12:34``), con lo stesso criterio di ``asr.py``.

    Le lezioni lunghe superano i 60 minuti: il formato resta ``MM:SS`` con
    i minuti non troncati (``75:00``), coerente con l'anteprima mostrata
    dall'interfaccia durante la trascrizione.
    """
    total = max(0, int(seconds))
    return f"{total // 60:02d}:{total % 60:02d}"


def format_duration(seconds: float) -> str:
    """Durata in ``MM:SS`` oppure ``H:MM:SS`` se supera l'ora."""
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def format_srt_time(seconds: float) -> str:
    """Timestamp SRT ``HH:MM:SS,mmm``."""
    total = max(0.0, float(seconds))
    millis = int(round(total * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def safe_filename(
    course: str = "",
    professor: str = "",
    date: str = "",
    *,
    max_len: int = 120,
    keep_accents: bool = True,
) -> str:
    """Nome di file sicuro derivato dalla sessione.

    Le parti (corso, docente, data) vengono pulite e unite con ``-``.
    Slash, backslash, due punti e caratteri di controllo diventano ``-``
    (una data come ``22/10/2026`` non puo' creare directory). Le accenti
    italiane vengono *conservate*: sono leggibili e valide nei nomi di
    file. Con ``keep_accents=False`` si ottiene la variante solo ASCII
    ("perché" -> "perche"); l'apostrofo di elisione resta invariato,
    perche' fa parte della scrittura della lezione.
    """

    parts: list[str] = []
    for raw in (course, professor, date):
        text = (raw or "").strip()
        if not text:
            continue
        text = unicodedata.normalize("NFC", text)
        if not keep_accents:
            decomposed = unicodedata.normalize("NFD", text)
            text = unicodedata.normalize(
                "NFC", "".join(c for c in decomposed if not unicodedata.combining(c))
            )
        text = _ILLEGAL_CHARS.sub("-", text)
        text = _WHITESPACE.sub("-", text)
        text = _DASHES.sub("-", text)
        text = text.strip(" -")
        if not text or _ONLY_PUNCT.match(text):
            continue
        parts.append(text)

    name = "-".join(parts) if parts else "lezione"
    if len(name) > max_len:
        name = name[:max_len].rsplit("-", 1)[0].strip(" -")
    return name or "lezione"


# ---------------------------------------------------------------------------
# il trasformatore
# ---------------------------------------------------------------------------
class Exporter:
    """Esporta la trascrizione di una sessione in uno dei formati supportati.

    La classe non mantiene stato proprio: tutto viene riletto dal
    ``SessionStore`` al momento dell'esportazione, cosi' si puo' riesportare
    una sessione dopo aver aggiunto segnalibri o note.
    """

    #: Formati accettati da :meth:`export`.
    FORMATS: tuple[str, ...] = ("txt", "md", "md_ts", "srt")

    #: Estensione predefinita per ciascun formato.
    EXTENSIONS: dict[str, str] = {
        "txt": ".txt",
        "md": ".md",
        "md_ts": "-ts.md",
        "srt": ".srt",
    }

    #: Formato usato da :meth:`export_default`.
    DEFAULT_FORMAT = "md_ts"

    def __init__(self, store: SessionStore) -> None:
        self.store = store

    # -- API pubblica ------------------------------------------------------
    def export(self, session_id: int, fmt: str, path: Path) -> Path:
        """Scrive la trascrizione in ``path`` nel formato ``fmt``.

        ``fmt`` e' uno fra ``txt``, ``md``, ``md_ts``, ``srt``. Le directory
        mancanti vengono create. Ritorna il percorso scritto.
        """
        key = (fmt or "").strip().lower()
        if key not in self.FORMATS:
            raise ExportError(
                f"Formato sconosciuto: {fmt!r} (supportati: {', '.join(self.FORMATS)})"
            )

        content = self.render(session_id, key)
        path = Path(path)
        if path.parent and not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def export_default(self, session_id: int, directory: Path) -> Path:
        """Esporta nel formato predefinito (``md_ts``) dentro ``directory``.

        Il nome del file e' derivato da corso, docente e data con
        :func:`safe_filename`.
        """
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        name = self.session_filename(session_id, self.DEFAULT_FORMAT)
        return self.export(session_id, self.DEFAULT_FORMAT, directory / name)

    def render(self, session_id: int, fmt: str = "md_ts", *, include_partial: bool = False) -> str:
        """Restituisce il testo dell'esportazione senza scriverlo su disco."""
        key = (fmt or "").strip().lower()
        if key not in self.FORMATS:
            raise ExportError(
                f"Formato sconosciuto: {fmt!r} (supportati: {', '.join(self.FORMATS)})"
            )
        session, segments = self._collect(session_id, include_partial)
        if key == "txt":
            return self._render_text(session, segments)
        if key == "md":
            return self._render_markdown(session, segments)
        if key == "md_ts":
            return self._render_markdown_ts(session, segments)
        return self._render_srt(segments)

    def to_text(self, session_id: int, *, include_partial: bool = False) -> str:
        """Trascrizione in testo semplice, con timestamp e intestazione."""
        session, segments = self._collect(session_id, include_partial)
        return self._render_text(session, segments)

    def to_markdown(self, session_id: int, *, include_partial: bool = False) -> str:
        """Trascrizione in Markdown, un paragrafo per segmento."""
        session, segments = self._collect(session_id, include_partial)
        return self._render_markdown(session, segments)

    def to_markdown_ts(self, session_id: int, *, include_partial: bool = False) -> str:
        """Trascrizione in Markdown con timestamp inline."""
        session, segments = self._collect(session_id, include_partial)
        return self._render_markdown_ts(session, segments)

    def to_srt(self, session_id: int, *, include_partial: bool = False) -> str:
        """Trascrizione in sottotitoli SRT."""
        _, segments = self._collect(session_id, include_partial)
        return self._render_srt(segments)

    def session_filename(self, session_id: int, fmt: str = "md_ts") -> str:
        """Nome di file consigliato per la sessione nel formato ``fmt``."""
        session = self._require_session(session_id)
        base = safe_filename(session.course, session.professor, session.date)
        suffix = self.EXTENSIONS.get(fmt, ".txt")
        if suffix.startswith("."):
            return f"{base}{suffix}"
        return f"{base}{suffix}"

    # -- interni -----------------------------------------------------------
    def _require_session(self, session_id: int) -> Session:
        session = self.store.get_session(session_id)
        if session is None:
            raise ExportError(f"Sessione inesistente: {session_id}")
        return session

    def _collect(
        self, session_id: int, include_partial: bool
    ) -> tuple[Session, list[ExportedSegment]]:
        session = self._require_session(session_id)
        rows = self.store.get_segments(session_id)
        segments: list[ExportedSegment] = []
        for row in rows:
            final = bool(int(row["final"])) if row["final"] is not None else True
            if not final and not include_partial:
                continue
            text = (row["text"] or "").strip()
            if not text:
                continue
            segments.append(
                ExportedSegment(
                    index=int(row["idx"]),
                    start=float(row["t_start"]),
                    end=float(row["t_end"]),
                    text=text,
                    bookmark=bool(int(row["bookmark"])) if row["bookmark"] is not None else False,
                    note=(row["note"] or "").strip(),
                    final=final,
                )
            )
        return session, segments

    def _duration(self, segments: Sequence[ExportedSegment]) -> float:
        if not segments:
            return 0.0
        return max(seg.end for seg in segments)

    def _metadata(self, session: Session, segments: Sequence[ExportedSegment]) -> dict[str, str]:
        return {
            "corso": session.course or "—",
            "docente": session.professor or "—",
            "data": session.date or "—",
            "durata": format_duration(self._duration(segments)),
            "segmenti": str(len(segments)),
            "esportato": time.strftime("%Y-%m-%d %H:%M"),
        }

    def _header_lines(self, session: Session, segments: Sequence[ExportedSegment]) -> list[str]:
        meta = self._metadata(session, segments)
        return [
            f"Lezione: {session.title}",
            f"Corso: {meta['corso']}",
            f"Docente: {meta['docente']}",
            f"Data: {meta['data']}",
            f"Durata: {meta['durata']}",
            f"Segmenti: {meta['segmenti']}",
            f"Esportato: {meta['esportato']}",
        ]

    def _render_text(self, session: Session, segments: Sequence[ExportedSegment]) -> str:
        lines = self._header_lines(session, segments)
        lines.append("=" * 60)
        lines.append("")
        for seg in segments:
            prefix = f"{BOOKMARK_MARKER} " if seg.bookmark else ""
            lines.append(f"[{seg.timestamp}] {prefix}{seg.text}")
            if seg.note:
                lines.append(f"    {NOTE_MARKER} nota: {seg.note}")
        lines.append("")
        return "\n".join(lines)

    def _render_markdown(self, session: Session, segments: Sequence[ExportedSegment]) -> str:
        meta = self._metadata(session, segments)
        lines = [
            f"# {session.title}",
            "",
            f"- **Corso:** {meta['corso']}",
            f"- **Docente:** {meta['docente']}",
            f"- **Data:** {meta['data']}",
            f"- **Durata:** {meta['durata']}",
            f"- **Segmenti:** {meta['segmenti']}",
            f"- **Esportato:** {meta['esportato']}",
            "",
            "---",
            "",
        ]
        for seg in segments:
            prefix = f"{BOOKMARK_MARKER} " if seg.bookmark else ""
            lines.append(f"{prefix}{seg.text}")
            if seg.note:
                lines.append("")
                lines.append(f"> {NOTE_MARKER} nota: {seg.note}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _render_markdown_ts(self, session: Session, segments: Sequence[ExportedSegment]) -> str:
        meta = self._metadata(session, segments)
        lines = [
            f"# {session.title}",
            "",
            f"- **Corso:** {meta['corso']}",
            f"- **Docente:** {meta['docente']}",
            f"- **Data:** {meta['data']}",
            f"- **Durata:** {meta['durata']}",
            f"- **Segmenti:** {meta['segmenti']}",
            f"- **Esportato:** {meta['esportato']}",
            "",
            "---",
            "",
        ]
        for seg in segments:
            prefix = f"{BOOKMARK_MARKER} " if seg.bookmark else ""
            lines.append(f"**[{seg.timestamp}]** {prefix}{seg.text}")
            if seg.note:
                lines.append("")
                lines.append(f"> {NOTE_MARKER} nota: {seg.note}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _render_srt(self, segments: Sequence[ExportedSegment]) -> str:
        blocks: list[str] = []
        for number, seg in enumerate(segments, start=1):
            body = [f"{BOOKMARK_MARKER} {seg.text}" if seg.bookmark else seg.text]
            if seg.note:
                body.append(f"{NOTE_MARKER} nota: {seg.note}")
            blocks.append(
                f"{number}\n"
                f"{format_srt_time(seg.start)} --> {format_srt_time(seg.end)}\n"
                + "\n".join(body)
            )
        return "\n\n".join(blocks) + ("\n" if blocks else "")
