"""Punto d'ingresso a riga di comando.

``python -m lecture_transcriber`` avvia l'applicazione grafica.
``python -m lecture_transcriber --version`` stampa la versione.

Il requisito e' che l'applicazione si apra con un tap dalla sua icona.
Se una dipendenza manca, l'errore deve dire **quale** e **come
rimediare**: il messaggio generico "verifica PyGObject e GTK4" era
falso -- misurato, l'app non si apriva per ``soxr`` assente mentre il
messaggio incolpava GTK4.
"""

from __future__ import annotations

import sys


# Cosa serve per aprire la finestra e cosa serve invece solo per
# trascrivere. Le seconde non devono impedire l'avvio: cronologia,
# ricerca, esportazione e correzioni funzionano lo stesso.
_GUI_MODULES = ("gi",)
_ASR_MODULES = ("numpy", "sounddevice", "soxr", "faster_whisper")


def _missing(mods: tuple[str, ...]) -> list[str]:
    import importlib.util

    gone = []
    for name in mods:
        try:
            if importlib.util.find_spec(name) is None:
                gone.append(name)
        except (ImportError, ValueError):
            gone.append(name)
    return gone


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if "--version" in argv or "-V" in argv:
        from lecture_transcriber import __version__

        print(f"lecture-transcriber {__version__}")
        return 0

    gone = _missing(_GUI_MODULES)
    if gone:
        print(
            "Impossibile avviare l'interfaccia grafica: manca "
            + ", ".join(gone)
            + ".",
            file=sys.stderr,
        )
        print(
            "  Installa i pacchetti: sudo dnf install python3-gobject gtk4 libadwaita",
            file=sys.stderr,
        )
        return 1

    try:
        from lecture_transcriber.app import main as run_app
    except ImportError as exc:
        print(f"Impossibile avviare l'interfaccia grafica: {exc}", file=sys.stderr)
        print(
            "  Verifica che i pacchetti dell'interfaccia siano installati:\n"
            "    sudo dnf install python3-gobject gtk4 libadwaita",
            file=sys.stderr,
        )
        return 1

    # Le librerie di riconoscimento vocale mancanti NON bloccano l'avvio:
    # l'app si apre, e la trascrizione segnala l'errore con un rimedio.
    # Un tap dall'icona deve produrre una finestra, non un terminale.
    missing_asr = _missing(_ASR_MODULES)
    if missing_asr:
        from lecture_transcriber import config

        print(
            "lecture-transcriber: riconoscimento vocale non disponibile "
            "(manca: " + ", ".join(missing_asr) + ").",
            file=sys.stderr,
        )
        print(
            "  L'app si apre comunque: cronologia, ricerca, esportazione e\n"
            "  correzioni funzionano. Per trascrivere esegui:",
            file=sys.stderr,
        )
        # percorso valido per il layout in cui gira davvero: relativo al
        # clone, oppure /usr/share/... nel pacchetto RPM
        print(f"    {config.ensure_venv_hint()}", file=sys.stderr)

    return run_app(argv)


if __name__ == "__main__":
    raise SystemExit(main())
