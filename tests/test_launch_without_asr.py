"""Il lancio con un tap non deve dipendere dalle librerie di trascrizione.

Il requisito del progetto e' esplicito: l'applicazione si apre con un tap
dalla sua icona, e le librerie di riconoscimento vocale mancanti non
impediscono l'avvio — cronologia, ricerca, esportazione e correzioni
devono continuare a funzionare. ``__main__`` lo promette a video:

    "L'app si apre comunque: cronologia, ricerca, esportazione e
     correzioni funzionano."

La promessa era **falsa**. ``app``, ``asr`` e ``audio`` importavano
``numpy`` in testa al modulo, quindi un interprete senza numpy (venv non
ancora pronto, installazione minimale, ambiente rotto) moriva con
``ImportError: No module named 'numpy'`` al primo
``from lecture_transcriber.app import TranscriptionPipeline`` — e
``__main__`` cascava a sua volta su quel ``try``. Misurato: la finestra
non si apriva affatto, e l'utente non aveva modo di consultare la
cronologia ne' esportare. Stessa categoria di difetto di ``soxr`` e
``sounddevice``, che erano gia' stati resi differiti: numpy era rimasto
fuori perche' "tanto c'e' sempre".

Il test gira in un **sotto-processo** con un ``MetaPathFinder`` che
blocca numpy, soxr, sounddevice e faster_whisper prima dell'import. Non
si puo' fare nello stesso interprete: i moduli veri sono gia' in
``sys.modules`` e il blocco deve esisterre *prima* che il pacchetto
venga toccato per la prima volta.

Non si costruisce la finestra: servirebbero un display e una
``Gtk.Application``, e il punto del test e' l'import — se il pacchetto
non si importa, la finestra non esiste proprio. Che la finestra si
costruisca davvero lo verificano i test di ``test_ui_settings.py``,
nell'ambiente dove GTK c'e'.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import unittest
from unittest import mock

from _bootstrap import ROOT, SRC_DIR, ensure_paths

ensure_paths()

_BLOCKED = ("numpy", "soxr", "sounddevice", "faster_whisper")

# Codice eseguito nel sotto-processo. Il percorso dei sorgenti viene
# iniettato con ``str.replace`` e non con la formattazione ``%``: il
# bambino contiene operatori ``%`` propri, e ``%``-formattarlo qui li
# farebbe interpretare al padre (misurato: ``TypeError: not enough
# arguments for format string``).
_CHILD = textwrap.dedent(
    """
    import sys

    BLOCKED = ("numpy", "soxr", "sounddevice", "faster_whisper")


    class _Block:
        \"\"\"Il modulo non esiste, e non deve esistere: si prova a
        importarlo, l'interprete deve rispondere ImportError.\"\"\"

        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in BLOCKED:
                raise ImportError("module %r bloccato dal test" % name)
            return None


    sys.meta_path.insert(0, _Block())
    for name in list(sys.modules):
        if name.split(".")[0] in BLOCKED:
            del sys.modules[name]

    sys.path.insert(0, "@@SRC@@")

    from lecture_transcriber.app import TranscriptionPipeline
    from lecture_transcriber.ui import MainWindow
    print("import-ok")

    # Il percorso vero, non solo l'import: ``do_activate`` costruisce la
    # finestra e chiama ``pipeline.open()``, che costruisce ``Segmenter``
    # -- e ``Segmenter.__init__`` faceva ``np.zeros``. Differire l'import
    # di numpy non bastava: con ``np = None`` quello era un
    # ``AttributeError`` che risaliva fino a ``app.run()`` e la finestra
    # non compariva, mentre ``__main__`` aveva gia' promesso "L'app si
    # apre comunque". Misurato: ``main()`` stampava la promessa e poi
    # moriva. Ora l'assenza diventa un errore di stato con il rimedio.
    from lecture_transcriber.app import (
        PipelineCallbacks,
        PipelineConfig,
        TranscriptionPipeline,
    )

    pipe = TranscriptionPipeline(PipelineConfig(), PipelineCallbacks())
    pipe.open()
    assert pipe.error_message, "con numpy assente open() deve segnalare l'errore"
    assert "ensure-venv.sh" in pipe.error_message, pipe.error_message
    print("open-ok")

    # "ascolta" con le librerie assente non deve ne' tacere ne' esplodere
    pipe.start()
    assert pipe.state == "error", pipe.state
    print("start-ok")

    # L'errore deve arrivare quando si prova a usare il microfono, non
    # prima: e' un messaggio che dice cosa installare, non un
    # ModuleNotFoundError che sembra un difetto del programma.
    from lecture_transcriber.audio import AudioCapture

    try:
        AudioCapture()
    except RuntimeError as exc:
        assert "ensure-venv.sh" in str(exc), str(exc)
        print("actionable-ok")
    except ImportError as exc:
        raise AssertionError(
            "l'errore deve essere leggibile, non un ImportError: %s" % exc
        )
    else:
        raise AssertionError("senza sounddevice la cattura non puo' partire")
    """
).replace("@@SRC@@", str(SRC_DIR))


def _run_blocked() -> subprocess.CompletedProcess[str]:
    """Esegue il bambino con le librerie bloccate, catturando l'output."""
    return subprocess.run(
        [sys.executable, "-I", "-c", _CHILD],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(ROOT),
    )


class LaunchWithoutASRLibrariesTest(unittest.TestCase):
    """Un tap deve aprire la finestra anche senza numpy/soxr/sd/faster-whisper."""

    def test_the_app_imports_without_the_transcription_libraries(self) -> None:
        proc = _run_blocked()
        self.assertEqual(
            proc.returncode,
            0,
            "l'import del pacchetto e' fallito:\n"
            + (proc.stderr or proc.stdout or "(nessun output)"),
        )
        self.assertIn("import-ok", proc.stdout)

    def test_the_missing_library_is_reported_when_it_is_needed(self) -> None:
        """L'errore, quando arriva, deve dire come rimediare."""
        proc = _run_blocked()
        self.assertIn("actionable-ok", proc.stdout, proc.stderr)

    def test_the_pipeline_opens_without_numpy(self) -> None:
        """``open()`` deve sopravvivere a numpy assente.

        Il percorso e' quello vero di ``do_activate``: costruire la
        finestra e chiamare ``pipeline.open()``. Con l'import differito
        ma il codice non difensivo, ``open()`` sollevava
        ``AttributeError: 'NoneType' object has no attribute 'zeros'`` e
        la finestra non si presentava — la promessa di ``__main__``
        ("L'app si apre comunque") era falsa.
        """
        proc = _run_blocked()
        self.assertIn("open-ok", proc.stdout, proc.stderr)

    def test_listening_without_numpy_reports_instead_of_dying(self) -> None:
        """"ascolta" senza librerie: errore di stato, non eccezione ne' silenzio."""
        proc = _run_blocked()
        self.assertIn("start-ok", proc.stdout, proc.stderr)

    def test_audio_capture_reports_the_missing_library(self) -> None:
        """Nel processo principale, con le librerie nascoste a mano.

        Qui i moduli veri ci sono: si nascondono gli attributi del
        modulo, che e' l'unica cosa che ``_require_audio_libs`` guarda.
        Cosi' il test non si autocancella quando l'ambiente e' completo
        — una prova che ``skipTest`` su un env sano e' una prova che non
        prova niente.
        """
        from lecture_transcriber import audio as audiomod

        with mock.patch.object(audiomod, "soxr", None), mock.patch.object(
            audiomod, "sd", None
        ):
            with self.assertRaises(RuntimeError) as caught:
                audiomod.AudioCapture()
        message = str(caught.exception)
        self.assertIn("ensure-venv.sh", message)
        self.assertIn("Librerie audio mancanti", message)

    def test_the_message_names_the_missing_library(self) -> None:
        """Non basta "manca qualcosa": deve dire *cosa* manca."""
        from lecture_transcriber import audio as audiomod

        with mock.patch.object(audiomod, "soxr", None), mock.patch.object(
            audiomod, "sd", None
        ):
            with self.assertRaises(RuntimeError) as caught:
                audiomod.AudioCapture()
        self.assertIn("soxr", str(caught.exception))
        self.assertIn("sounddevice", str(caught.exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
