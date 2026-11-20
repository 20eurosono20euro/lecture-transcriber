"""Configurazione e percorsi dell'applicazione.

Tutto lo stato persistente sta in ``~/.local/share/lecture-transcriber`` (Linux
XDG) e i modelli in ``~/.local/share/lecture-transcriber/models``. Entrambi i
percorsi sono configurabili via variabili d'ambiente per il collaudo.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field, asdict
from pathlib import Path

APP_ID = "it.lecturetranscriber.App"
APP_NAME = "Lecture Transcriber"


def _platform_home_dirs(platform: str, env: Mapping[str, str], home: Path):
    r"""Sceglie le cartelle base di dati, configurazione e cache per l'OS.

    Cross-platform (verificato contro le convenzioni ufficiali):

    * **Linux/altro**: XDG. ``~/.local/share`` per i dati, ``~/.config`` per
      la configurazione, ``~/.cache`` per la cache. Rispettano le variabili
      ``XDG_DATA_HOME``/``XDG_CONFIG_HOME``/``XDG_CACHE_HOME`` (sostituiscono
      il default, non lo aggiungono).
    * **Windows**: ``%APPDATA%`` per i dati e la configurazione,
      ``%LOCALAPPDATA%`` per la cache. Su Windows la "Application Data" di
      ogni utente e' l'analogo di ``~/.config``; i dati che devono essere
      sincronizzati con il profilo vanno li' (sessioni, preferenze), mentre
      la cache no (puo' essere ricreata).
    * **macOS**: ``~/Library/Application Support`` per i dati e la
      configurazione, ``~/Library/Caches`` per la cache — il percorso
      *canonico* che Foundation/``FileManager`` usano e che Apple
      raccomanda (``NSApplicationSupportDirectory``/``NSCachesDirectory``).

    La funzione e' pura (nessun accesso a ``os.environ``/``Path.home()``
    reale): riceve tutto come parametri, cosi' i test possono simulare un
    altro sistema operativo senza toccare la macchina che gira.
    """
    if platform.startswith("win"):
        appdata = env.get("APPDATA")
        localappdata = env.get("LOCALAPPDATA")
        # Se uno dei due manca (es. in un ambiente minimale), si crolla
        # su un percorso sensato dentro la home invece di un path vuoto.
        base_data = Path(appdata) if appdata else home / "AppData" / "Roaming"
        base_cache = (
            Path(localappdata) if localappdata else home / "AppData" / "Local"
        )
        return {
            "data": base_data,
            "config": base_data,
            "cache": base_cache,
        }
    if platform == "darwin":
        return {
            "data": home / "Library" / "Application Support",
            "config": home / "Library" / "Application Support",
            "cache": home / "Library" / "Caches",
        }
    # Linux e tutto il resto: XDG.
    xdg_data = env.get("XDG_DATA_HOME")
    xdg_config = env.get("XDG_CONFIG_HOME")
    xdg_cache = env.get("XDG_CACHE_HOME")
    return {
        "data": Path(xdg_data) if xdg_data else home / ".local" / "share",
        "config": Path(xdg_config) if xdg_config else home / ".config",
        "cache": Path(xdg_cache) if xdg_cache else home / ".cache",
    }


# Calcolati all'import come prima, ma ora consapevoli della piattaforma.
_platform = _platform_home_dirs(
    sys.platform, os.environ, Path.home()
)

XDG_DATA = _platform["data"]
XDG_CONFIG = _platform["config"]
XDG_CACHE = _platform["cache"]

DATA_DIR = Path(os.environ.get("LT_DATA_DIR", XDG_DATA / "lecture-transcriber"))
CONFIG_DIR = Path(os.environ.get("LT_CONFIG_DIR", XDG_CONFIG / "lecture-transcriber"))
CACHE_DIR = Path(os.environ.get("LT_CACHE_DIR", XDG_CACHE / "lecture-transcriber"))
MODEL_DIR = Path(os.environ.get("LT_MODEL_DIR", DATA_DIR / "models"))

SETTINGS_FILE = CONFIG_DIR / "settings.json"

#: Sottocartella dei dati dove finiscono le registrazioni audio opzionali
#: (``Settings.record_audio``). Sta sotto ``DATA_DIR`` perche' e' dato
#: dell'utente come le trascrizioni, non cache.
AUDIO_DIR = DATA_DIR / "audio"


def ensure_venv_hint() -> str:
    """Percorso utilizzabile di ``ensure-venv.sh`` in *questo* layout.

    Il rimedio che l'app mostra quando mancano le librerie di
    riconoscimento vocale era un percorso relativo (``scripts/ensure-venv.sh``)
    oppure, peggio, un percorso assoluto fisso
    (``/usr/share/lecture-transcriber/scripts/ensure-venv.sh``): il primo
    non e' valido per chi lancia l'app da un'icona (la cwd non e' il
    clone), il secondo non esiste in un clone o in un'installazione per
    utente. Qui si sceglie il primo candidato che **esiste davvero**:

    * ``$LT_APP_DIR/scripts/ensure-venv.sh``, se la variabile e' impostata
      (utile per installazioni spostate);
    * ``/usr/share/lecture-transcriber/scripts/ensure-venv.sh`` (RPM);
    * ``<radice del progetto>/scripts/ensure-venv.sh`` (clone, sia per
      utente sia in sviluppo).

    Se nessuno esiste (pacchetto installato in modo inusuale) si torna al
    nome relativo: e' comunque il comando giusto da eseguire dal clone.
    """
    candidates: list[Path] = []
    app_dir = os.environ.get("LT_APP_DIR")
    if app_dir:
        candidates.append(Path(app_dir) / "scripts" / "ensure-venv.sh")
    candidates.append(
        Path("/usr/share/lecture-transcriber/scripts/ensure-venv.sh")
    )
    # config.py sta in <radice>/src/lecture_transcriber/: parents[2] e' la
    # radice del progetto quando si lavora da un clone.
    candidates.append(
        Path(__file__).resolve().parents[2] / "scripts" / "ensure-venv.sh"
    )
    for candidate in candidates:
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    return "scripts/ensure-venv.sh"


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
    auto_scroll: bool = True
    width: int = 420
    height: int = 520
    compact: bool = False

    # Inserimento nel documento
    insert_enabled: bool = False
    insert_paragraphs: bool = True  # paragrafi separati vs testo continuo
    insert_target: str = "auto"  # auto | libreoffice | atspi | clipboard

    # Privacy
    #: Scrive l'audio della sessione in ``AUDIO_DIR/<sessione>.wav``.
    #: Spenta per impostazione predefinita (brief §8).
    record_audio: bool = False

    # Scorciatoie (identificatori logici -> tasti)
    shortcuts: dict[str, str] = field(
        default_factory=lambda: {
            "toggle_listen": "<Ctrl><Super><Shift>A",
            "toggle_insert": "<Ctrl><Super><Shift>I",
            "toggle_transparent": "<Ctrl><Super><Shift>T",
            "toggle_window": "<Ctrl><Super><Shift>V",
            "toggle_always_on_top": "<Ctrl><Super><Shift>P",
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
    # ``AUDIO_DIR`` si crea solo quando si registra (privacy: nessuna
    # cartella audio vuota per chi non usa la funzione).
