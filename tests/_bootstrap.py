"""Bootstrap condiviso dai test.

Un solo compito: rendere importabile ``lecture_transcriber`` (layout
``src/``) anche quando i test vengono lanciati con ``python -m
unittest``, che non conosce l'opzione ``pythonpath`` di pytest.

Qui viveva anche uno shim che riaggiungeva ``soxr.Resample`` (rimosso in
soxr 1.x) a uso esclusivo dei test. È stato tolto: lo shim faceva
passare tutti i test di :mod:`test_audio` mentre
``AudioCapture(...)`` non si costruiva affatto fuori dai test — il
difetto era invisibile proprio perche' la correzione esisteva solo
dentro ``tests/``. Ora la compatibilita' sta nel codice di produzione
(:func:`lecture_transcriber.audio._make_resampler`), dove i test la
esercitano per davvero.
"""

from __future__ import annotations

import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
SRC_DIR = ROOT / "src"


def ensure_paths() -> None:
    """Aggiunge ``src/`` a ``sys.path`` se non c'e' gia'."""
    p = str(SRC_DIR)
    if p not in sys.path:
        sys.path.insert(0, p)


ensure_paths()
