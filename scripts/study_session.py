#!/usr/bin/env python3
"""Materiale di studio di una lezione gia' trascritta, senza interfaccia.

Il modulo :mod:`lecture_transcriber.study` era completo e collaudato (70
test) ma **non lo raggiungeva nessun percorso utente**: ne' la GUI ne' una
CLI lo importavano, percio' la voce "riepilogo, concetti, flashcard,
dizionario" della roadmap restava una promessa. Questo script e' il punto
d'ingresso che mancava.

Non richiede rete, modelli o GTK: il modulo ``study`` importa solo
``config`` e ``sessions``, e il backend euristico lavora sul solo testo.
Con un server LLM locale configurato usa quello.

    scripts/study_session.py --list
    scripts/study_session.py 3                       # JSON in DATA_DIR/study/
    scripts/study_session.py 3 --format md           # su stdout, leggibile
    scripts/study_session.py 3 --format md -o out.md
    scripts/study_session.py 3 --backend heuristic

Esce con 0 se ha prodotto il materiale, 1 se la sessione non esiste o e'
vuota, 2 se il modulo non ha prodotto nulla.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from lecture_transcriber import config  # noqa: E402
from lecture_transcriber.sessions import SessionStore  # noqa: E402
from lecture_transcriber.study import (  # noqa: E402
    StudyAssistant,
    StudyConfig,
    StudyMaterial,
    session_transcript,
)

_OPTION_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _option_letter(index: int) -> str:
    """``A``, ``B``, ... ``Z``, ``AA``: la stessa convenzione di ``study``."""
    label = ""
    index += 1
    while index > 0:
        index, rem = divmod(index - 1, 26)
        label = _OPTION_LETTERS[rem] + label
    return label


def render_markdown(material: StudyMaterial, *, title: str = "") -> str:
    """Il pacchetto di studio come Markdown leggibile.

    ``to_dict()`` e' il formato per le macchine; questo e' quello che una
    persona legge (o incolla nei propri appunti).
    """
    lines: list[str] = []
    heading = title or (
        f"Lezione {material.session_id}" if material.session_id else "Lezione"
    )
    lines.append(f"# Materiale di studio — {heading}")
    lines.append("")
    lines.append(f"*Backend: {material.backend or 'n/d'}*")
    lines.append("")

    if material.summary:
        lines.append("## Riassunto")
        lines.append("")
        lines.append(material.summary)
        lines.append("")

    if material.concepts:
        lines.append("## Concetti principali")
        lines.append("")
        for concept in material.concepts:
            if concept.evidence:
                lines.append(f"- **{concept.term}** — {concept.evidence}")
            else:
                lines.append(f"- **{concept.term}**")
        lines.append("")

    if material.definitions:
        lines.append("## Definizioni")
        lines.append("")
        for definition in material.definitions:
            lines.append(f"- **{definition.term}** — {definition.definition}")
        lines.append("")

    if material.flashcards:
        lines.append("## Flashcard")
        lines.append("")
        for card in material.flashcards:
            lines.append(f"- **{card.front}** → {card.back}")
        lines.append("")

    if material.quiz:
        lines.append("## Quiz")
        lines.append("")
        for number, question in enumerate(material.quiz, start=1):
            lines.append(f"{number}. {question.question}")
            for index, option in enumerate(question.options):
                marker = "✓" if index == question.answer else " "
                lines.append(f"   - [{marker}] {_option_letter(index)}) {option}")
            if question.explanation:
                lines.append(f"   _{question.explanation}_")
            lines.append("")

    if material.warnings:
        lines.append("## Avvisi")
        lines.append("")
        for warning in material.warnings:
            lines.append(f"- {warning}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _print_sessions(store: SessionStore) -> int:
    sessions = store.list_sessions()
    if not sessions:
        print("Nessuna lezione salvata.", file=sys.stderr)
        return 1
    for session in sessions:
        count = len(store.get_segments(session.id))
        print(f"{session.id:>4}  {session.title}  ({count} segmenti)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Materiale di studio di una lezione trascritta",
    )
    parser.add_argument(
        "session_id",
        nargs="?",
        type=int,
        help="identificativo della lezione (vedi --list)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="elenca le lezioni salvate ed esce",
    )
    parser.add_argument(
        "--format",
        choices=["json", "md"],
        default="json",
        help="json (predefinito, salvato su disco) oppure md (su stdout)",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="percorso del file di uscita (default: cartella dati per json)",
    )
    parser.add_argument(
        "--backend",
        choices=["auto", "heuristic", "local"],
        default="auto",
        help="auto (predefinito): LLM locale se risponde, altrimenti euristico",
    )
    args = parser.parse_args(argv)

    store = SessionStore(config.DATA_DIR / "sessions.db")
    try:
        if args.list:
            return _print_sessions(store)
        if args.session_id is None:
            parser.error("serve un identificativo di lezione, oppure --list")
        if store.get_session(args.session_id) is None:
            print(f"Lezione {args.session_id} inesistente.", file=sys.stderr)
            return 1
        text = session_transcript(store, args.session_id)
        if not text.strip():
            print(
                f"La lezione {args.session_id} non ha ancora trascrizione.",
                file=sys.stderr,
            )
            return 1

        # Il modulo resta spento per default (``StudyConfig.enabled``): qui
        # lo si accende per *questa* esecuzione senza toccare la preferenza
        # salvata, perche' l'utente ha appena chiesto il materiale.
        assistant = StudyAssistant(
            StudyConfig(enabled=True, backend=args.backend), store=store
        )
        material = assistant.study(text, session_id=args.session_id)
        if not (
            material.summary
            or material.concepts
            or material.definitions
            or material.flashcards
            or material.quiz
        ):
            for warning in material.warnings:
                print(f"avviso: {warning}", file=sys.stderr)
            print("Nessun materiale prodotto.", file=sys.stderr)
            return 2

        session = store.get_session(args.session_id)
        title = session.title if session is not None else ""

        if args.format == "md":
            rendered = render_markdown(material, title=title)
            if args.output:
                path = Path(args.output)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(rendered, encoding="utf-8")
                print(f"Scritto in {path}")
            else:
                print(rendered, end="")
            for warning in material.warnings:
                print(f"avviso: {warning}", file=sys.stderr)
            return 0

        # JSON: il formato canonico, scritto dal modulo stesso.
        if args.output:
            path = Path(args.output)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(material.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        else:
            path = material.save()
        print(f"Scritto in {path}")
        for warning in material.warnings:
            print(f"avviso: {warning}", file=sys.stderr)
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
