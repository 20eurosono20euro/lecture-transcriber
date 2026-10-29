"""Configurazione pytest: rende il pacchetto ``src/`` importabile.

La stessa sistemazione e' replicata in ``tests/_bootstrap.py`` per chi
usa ``python -m unittest`` (che non legge questa configurazione).
"""

from __future__ import annotations

import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))
