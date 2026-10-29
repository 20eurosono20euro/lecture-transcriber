"""Configurazione e percorsi dell'applicazione.

Tutto lo stato persistente sta in ``~/.local/share/lecture-transcriber`` (Linux
XDG) e i modelli in ``~/.local/share/lecture-transcriber/models``. Entrambi i
percorsi sono configurabili via variabili d'ambiente per il collaudo.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

APP_ID = "it.lecturetranscriber.App"
APP_NAME = "Lecture Transcriber"

XDG_DATA = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
XDG_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
XDG_CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))

DATA_DIR = Path(os.environ.get("LT_DATA_DIR", XDG_DATA / "lecture-transcriber"))
CONFIG_DIR = Path(os.environ.get("LT_CONFIG_DIR", XDG_CONFIG / "lecture-transcriber"))
CACHE_DIR = Path(os.environ.get("LT_CACHE_DIR", XDG_CACHE / "lecture-transcriber"))
MODEL_DIR = Path(os.environ.get("LT_MODEL_DIR", DATA_DIR / "models"))

SETTINGS_FILE = CONFIG_DIR / "settings.json"
SESSIONS_FILE = DATA_DIR / "sessions.json"


def _default_data_dir() -> Path:
    return Path(os.environ.get("LT_DATA_DIR", DATA_DIR))


@dataclass
class Settings:
    """Preferenze utente, serializzate in JSON.

    I campi hanno default sensati: la registrazione audio è disattivata e
    l'inserimento nel documento è spento, come richiesto dalle specifiche.
    """

    # Modello ASR
    model_name: str = "large-v3-turbo"
    compute_type: str = "int8"
    device: str = "cpu"
    language: str = "it"

    # Acquisizione audio
    input_device: int | None = None  # None = sorgente predefinita di PipeWire
    sample_rate: int = 48000
    target_rate: int = 16000
    vad_silence_ms: int = 700
    vad_min_speech_ms: int = 250
    max_segment_s: float = 15.0

    # Interfaccia
    opacity: float = 0.85
    font_size: int = 14
    always_on_top: bool = True
    click_through: bool = False
    auto_scroll: bool = True
    x_pos: int | None = None
    y_pos: int | None = None
    width: int = 420
    height: int = 520
    compact: bool = False

    # Inserimento nel documento
    insert_enabled: bool = False
    insert_paragraphs: bool = True  # paragrafi separati vs testo continuo
    insert_target: str = "auto"  # auto | libreoffice | atspi | clipboard

    # Privacy
    record_audio: bool = False  # disattivata per impostazione predefinita
    keep_audio: bool = False

    # Scorciatoie (identificatori logici -> tasti)
    shortcuts: dict[str, str] = field(
        default_factory=lambda: {
            "toggle_listen": "<Ctrl><Super><Shift>A",
            "toggle_insert": "<Ctrl><Super><Shift>I",
            "toggle_transparent": "<Ctrl><Super><Shift>T",
            "toggle_window": "<Ctrl><Super><Shift>V",
            "bookmark": "<Ctrl><Super><Shift>B",
            "jump_to_latest": "<Ctrl><Super><Shift>L",
        }
    )

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        known = {f for f in cls.__dataclass_fields__}
        clean = {k: v for k, v in data.items() if k in known}
        s = cls(**clean)
        # le scorciatoie vanno unite, non sostituite: quelle nuove devono
        # comunque essere presenti se il file è di una versione precedente
        base = cls().shortcuts
        merged = s.shortcuts if isinstance(s.shortcuts, dict) else {}
        base.update(merged)
        s.shortcuts = base
        # un campo col tipo sbagliato non deve attivare un'opzione da solo:
        # "record_audio": "sì" (o "no", o "0") e' una stringa, quindi
        # veritiera — misurato: con quel valore nel file la registrazione
        # audio partiva, e il §8 la vuole spenta per impostazione
        # predefinita. Si riportano ai default i campi booleani non booleani.
        for field in cls.__dataclass_fields__.values():
            if field.type in (bool, "bool"):
                value = getattr(s, field.name)
                if not isinstance(value, bool):
                    setattr(s, field.name, field.default)
        return s


def load_settings() -> Settings:
    """Legge le preferenze, tornando ai default se il file manca o è rotto."""
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return Settings()
    # un JSON valido non e' necessariamente un oggetto: '[1,2,3]',
    # '"testo"', 'null' e '42' parsano benissimo. Passarli a from_dict
    # sollevava AttributeError su .items() e l'app non partiva —
    # misurato: crash al lancio con un file scritto a mano o troncato.
    if not isinstance(data, dict):
        return Settings()
    try:
        return Settings.from_dict(data)
    except (TypeError, ValueError):
        return Settings()


def save_settings(settings: Settings) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_FILE.with_suffix(".json.tmp")
    tmp.write_text(settings.to_json(), encoding="utf-8")
    tmp.replace(SETTINGS_FILE)  # scrittura atomica


def ensure_dirs() -> None:
    for d in (DATA_DIR, CONFIG_DIR, CACHE_DIR, MODEL_DIR):
        d.mkdir(parents=True, exist_ok=True)
