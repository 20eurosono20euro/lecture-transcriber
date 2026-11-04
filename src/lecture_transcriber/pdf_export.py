"""Esportazione della trascrizione in PDF leggibile.

Il pulsante "Salva lezione" nella barra superiore salva la lezione come
un file PDF nella cartella Documents dell'utente. Questo modulo prende i
dati dalla **stessa** fonte degli altri formati — :class:`export.Exporter`
— e li impagina con `WeasyPrint`: solo i segmenti finali, con segnalibri
e note, e un'intestazione con i metadati della sessione.

Perche' WeasyPrint e non un widget di stampa di GTK? ``Gtk.PrintOperation``
esiste ma apre un dialogo interattivo ed espone opzioni che qui non
interessano. Il requisito e' una pagina pulita con un tap: WeasyPrint
renderizza l'HTML preparato dal codice direttamente su ``~/Documents``,
senza dialogo.

La dipendenza ``python3-weasyprint`` e' pacchettizzata in Fedora 44, quindi
entra nel GRUPPO 1 delle dipendenze (``Requires:`` nello spec, controllo
in ``install.sh``). Se pero' l'app parte senza di essa — perche' il sistema
non l'ha — il resto dell'applicazione funziona e il tasto segnala un errore
chiaro invece di non aprire.
"""

from __future__ import annotations

import html
from pathlib import Path

from .export import BOOKMARK_MARKER, NOTE_MARKER, Exporter, format_duration, format_timestamp
from .sessions import Session, SessionStore

__all__ = [
    "PdfExportError",
    "WeasyPrintMissingError",
    "render_pdf_html",
    "save_pdf",
]

#: Colore di accento dei timestamp, coerente con il tema (accent GNOME).
_ACCENT = "#3584e4"

#: CSS del documento: tipografia pulita, timestamp in una colonna stretta
#: sul margine sinistro, segnalibri in evidenza prima del testo.
_CSS = """
@page {
    size: A4;
    margin: 18mm 16mm 20mm 16mm;
    @bottom-right {
        content: "Pagina " counter(page);
        font-family: 'Cantarell', 'DejaVu Sans', sans-serif;
        font-size: 8.5pt;
        color: #777;
    }
}
* { box-sizing: border-box; }
body {
    font-family: 'Cantarell', 'DejaVu Sans', sans-serif;
    color: #1f2020;
    margin: 0;
    line-height: 1.45;
}
header {
    border-bottom: 2px solid %(accent)s;
    padding-bottom: 10pt;
    margin-bottom: 14pt;
}
h1 { font-size: 17pt; margin: 0 0 4pt; }
.meta { font-size: 9.5pt; color: #555; margin: 0; }
.meta span { display: inline-block; margin-right: 16pt; }
.lesson { font-size: 10pt; }
ul.segments { list-style: none; padding: 0; margin: 0; }
.segments li {
    display: flex;
    gap: 10pt;
    padding: 7pt 0;
    border-bottom: 0.5pt solid #e6e6e6;
    page-break-inside: avoid;
}
.time {
    width: 38pt;
    flex: 0 0 38pt;
    font-family: 'DejaVu Sans Mono', 'Cantarell', monospace;
    font-size: 8.5pt;
    font-weight: 600;
    color: %(accent)s;
    padding-top: 1.5pt;
}
.body { flex: 1; font-size: 11pt; }
.mark { color: #b8860b; font-weight: 600; }
.note {
    margin-top: 3pt;
    font-size: 9.5pt;
    color: #555;
    font-style: italic;
}
.partial { color: #999; font-style: italic; }
footer { margin-top: 12pt; font-size: 8.5pt; color: #888; }
""" % {"accent": _ACCENT}


class PdfExportError(Exception):
    """Errore durante la creazione del PDF."""


class WeasyPrintMissingError(PdfExportError):
    """WeasyPrint non e' installato: non si puo' creare il PDF."""


def _import_weasyprint():
    """Importa WeasyPrint, o solleva un errore chiaro se manca."""
    try:
        from weasyprint import HTML  # noqa: PLC0415
        from weasyprint.text.ffi import pango  # noqa: F401, PLC0415
    except Exception:  # noqa: BLE001 - qualunque fallimento di import
        raise WeasyPrintMissingError(
            "Per salvare il PDF serve WeasyPrint: sudo dnf install python3-weasyprint"
        ) from None
    return HTML


def _escape(text: str) -> str:
    return html.escape(text or "")


def _segment_li(index: int, start: float, text: str, bookmark: bool, note: str) -> str:
    time = format_timestamp(start)
    mark = f' <span class="mark">{_escape(BOOKMARK_MARKER)}</span>' if bookmark else ""
    body = _escape(text)
    note_html = (
        f'<div class="note">{_escape(NOTE_MARKER)} nota: {_escape(note)}</div>'
        if note
        else ""
    )
    return (
        f"<li>"
        f'<div class="time">{time}</div>'
        f'<div class="body">{mark}{" " if mark else ""}{body}{note_html}</div>'
        f"</li>"
    )


def _render_body(session: Session, segments) -> str:
    """I segmenti come lista: timestamp, segnalibro, testo, nota."""
    items = []
    for seg in segments:
        items.append(
            _segment_li(seg.index, seg.start, seg.text, seg.bookmark, seg.note)
        )
    return "\n".join(items)


def render_pdf_html(session: Session, segments, duration: float) -> str:
    """Prepara l'HTML del PDF a partire da sessione e segmenti.

    ``segments`` sono gli :class:`export.ExportedSegment` gia' raccolti
    (solo finali), come fa l'export degli altri formati. ``duration`` e'
    la durata calcolata sull'ultimo segmento.
    """
    title = session.title
    meta = (
        f'<div class="meta">'
        f'<span><b>Corso:</b> {_escape(session.course) or "—"}</span>'
        f'<span><b>Docente:</b> {_escape(session.professor) or "—"}</span>'
        f'<span><b>Data:</b> {_escape(session.date) or "—"}</span>'
        f'<span><b>Durata:</b> {format_duration(duration)}</span>'
        f'<span><b>Segmenti:</b> {len(segments)}</span>'
        f"</div>"
    )
    body = _render_body(session, segments)
    return f"""<!doctype html>
<html lang="it"><head><meta charset="utf-8"><style>{_CSS}</style></head>
<body>
  <header>
    <h1>{_escape(title)}</h1>
    {meta}
  </header>
  <ul class="segments">
    {body}
  </ul>
</body></html>"""


def _collect(store: SessionStore, session_id: int):
    """Sessione e segmenti finali, dalla stessa fonte degli altri export."""
    exporter = Exporter(store)
    session, segments = exporter._collect(session_id, include_partial=False)
    return session, segments


def save_pdf(store: SessionStore, session_id: int, path: Path) -> Path:
    """Scrive il PDF della lezione in ``path``.

    Ritorna il percorso scritto. Se WeasyPrint manca, solleva
    :class:`WeasyPrintMissingError`; per ogni altro problema,
    :class:`PdfExportError`.
    """
    HTML = _import_weasyprint()
    session, segments = _collect(store, session_id)
    if not segments:
        raise PdfExportError("La lezione non contiene ancora alcuna trascrizione")
    duration = max(seg.end for seg in segments)
    html_text = render_pdf_html(session, segments, duration)
    path = Path(path)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    try:
        HTML(string=html_text).write_pdf(str(path))
    except Exception as exc:  # noqa: BLE001 - errori di rendering da segnalare come tali
        raise PdfExportError(f"Creazione del PDF fallita: {exc}") from exc
    return path
