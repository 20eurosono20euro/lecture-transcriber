"""Scorciatoie globali via GSettings (``media-keys``).

Percorso alternativo al portal ``org.freedesktop.portal.GlobalShortcuts``,
misurato su Fedora 44 / GNOME 50.6.

Il portal ha due limiti strutturali su GNOME 50:

* ``CreateSession`` pretende un **app-id**, risolto dal nome dello scope
  systemd del chiamante (``app-<app-id>-<rand>.scope``): un'applicazione
  lanciata da un terminale non ce l'ha e il portal risponde
  ``org.freedesktop.portal.Error.NotAllowed: An app id is required``;
* ``BindShortcuts`` apre il dialogo "Add Keyboard Shortcuts" di
  gnome-control-center e **aspetta la conferma dell'utente**: la risposta
  del portal non arriva finche' il dialogo non viene chiuso. Su GNOME 50
  la UI di rebind del portal non esiste (arriva solo con GNOME 51).

Il percorso GSettings evita entrambi i problemi:

* non serve alcun app-id: ``gnome-settings-daemon`` reagisce direttamente
  a ``org.gnome.settings-daemon.plugins.media-keys``;
* non compare alcun dialogo: la scorciatoia viene attivata lanciando un
  comando, che nel nostro caso e' una chiamata D-Bus all'applicazione.

Il rovescio della medaglia e' che la scorciatoia e' visibile nelle
Impostazioni di sistema sotto "Tastiera -> Scorciatoie personalizzate",
ed e' un'impostazione di sistema, non una preferenza dell'app.

Meccanismo (verificato in funzione):

1. si accoda il percorso della scorciatoia in
   ``org.gnome.settings-daemon.plugins.media-keys custom-keybindings``;
2. si impostano ``name``, ``command`` e ``binding`` nello schema child
   ``org.gnome.settings-daemon.plugins.media-keys.custom-keybinding``;
3. ``gnome-settings-daemon`` registra l'acceleratore e, alla pressione,
   esegue ``command``.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: Schema che tiene l'elenco delle scorciatoie personalizzate.
MEDIA_KEYS_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
#: Schema child con i dettagli di una singola scorciatoia.
CUSTOM_KEYBINDING_SCHEMA = (
    "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding"
)
#: Radice dei percorsi delle scorciatoie personalizzate.
CUSTOM_KEYBINDING_PATH = (
    "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"
)

#: Nome del bus D-Bus su cui l'applicazione espone l'attivazione delle
#: scorciatoie. Deve combaciare con ``APP_ID`` in ``config.py``.
APP_BUS_NAME = "it.lecturetranscriber.App"
APP_OBJECT_PATH = "/it/lecturetranscriber/App"
APP_INTERFACE = "it.lecturetranscriber.App"
APP_METHOD = "ActivateShortcut"

#: Gli identificatori logici delle scorciatoie dell'app. Servono a
#: distinguere le nostre scorciatoie da quelle personalizzate che
#: l'utente ha creato per conto proprio: queste ultime non vanno mai
#: toccate.
KNOWN_SHORTCUT_IDS = frozenset(
    {
        "toggle_listen",
        "toggle_insert",
        "toggle_transparent",
        "toggle_window",
        "toggle_always_on_top",
        "bookmark",
        "jump_to_latest",
    }
)


@dataclass(frozen=True)
class GSettingsShortcut:
    """Una scorciatoie registrata nel ``media-keys`` di GNOME."""

    id: str
    accelerator: str
    description: str

    @property
    def gsettings_path(self) -> str:
        return f"{CUSTOM_KEYBINDING_PATH}{self.id}/"

    @property
    def command(self) -> str:
        """Il comando lanciato da GNOME alla pressione della scorciatoia.

        Si preferisce la chiamata D-Bus all'applicazione: mantiene il
        processo unico e fa risparmiare l'avvio di un interprete Python
        a ogni pressione.
        """
        return (
            "gdbus call --session "
            f"--dest {APP_BUS_NAME} "
            f"--object-path {APP_OBJECT_PATH} "
            f"--method {APP_INTERFACE}.{APP_METHOD} {self.id}"
        )


def _gsettings(args: list[str]) -> tuple[int, str, str]:
    """Esegue ``gsettings`` e restituisce (codice, stdout, stderr)."""
    try:
        proc = subprocess.run(
            ["gsettings", *args],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:
        return 1, "", str(exc)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _is_valid_path(path: str) -> bool:
    """True se il percorso e' uno che ``media-keys`` puo' usare.

    ``gsd-media-keys`` chiama ``g_settings_new_with_path()`` senza
    validare il percorso: se il valore non e' un percorso GSettings
    valido, la funzione restituisce ``NULL`` e il plugin va in
    dereferenza di puntatore nullo, morendo con SIGSEGV.

    Il caso reale: ``gsettings get`` puo' stampare la lista come
    ``@as []``, e un parser che non scarta il prefisso finisce per
    scrivere la stringa ``@as []`` fra i percorsi delle scorciatoie.
    Il crash e' stato riprodotto: ``gsd-media-keys`` muore e con lui
    **tutte** le scorciatoie di GNOME, non solo le nostre.

    Per questo ogni percorso viene validato prima di essere scritto.
    """
    if not path:
        return False
    # deve essere un percorso assoluto di GSettings: /segmenti/nomi
    if not path.startswith("/"):
        return False
    if path == "/":
        return False
    # nessun spazio, nessun carattere di controllo
    if any(ch.isspace() or ord(ch) < 0x20 for ch in path):
        return False
    # i segmenti non possono essere vuoti
    if any(not segment for segment in path.strip("/").split("/")):
        return False
    # il percorso deve stare sotto la radice delle scorciatoie
    if not path.startswith(CUSTOM_KEYBINDING_PATH):
        return False
    # e deve avere almeno un nome dopo la radice
    return bool(path[len(CUSTOM_KEYBINDING_PATH):].strip("/"))


class GSettingsShortcutManager:
    """Registra le scorciatoie globali nel ``media-keys`` di GNOME.

    Ogni operazione non solleva mai: se ``gsettings`` non e' disponibile o
    la scrittura fallisce, il manager resta utilizzabile e segnala
    l'errore in :attr:`last_error`.
    """

    def __init__(self, app_bus_name: str = APP_BUS_NAME) -> None:
        self._app_bus_name = app_bus_name
        self._shortcuts: dict[str, GSettingsShortcut] = {}
        self._last_error: str | None = None
        self._owned: list[str] = []

    # -- stato -------------------------------------------------------------
    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def is_available(self) -> bool:
        """True se lo schema ``media-keys`` e' installato e utilizzabile."""
        code, out, _err = _gsettings(["list-keys", MEDIA_KEYS_SCHEMA])
        if code != 0:
            return False
        return "custom-keybindings" in out.split()

    def list_registered(self) -> list[tuple[str, str]]:
        """Coppie ``(id, acceleratore)`` registrate."""
        return [(s.id, s.accelerator) for s in self._shortcuts.values()]

    @staticmethod
    def existing_paths() -> list[str]:
        """I percorsi delle scorciatoie personalizzate gia' presenti.

        Serve a non sovrascrivere le scorciatoie personalizzate che
        l'utente ha creato per conto proprio: il manager deve aggiungere
        le proprie e lasciare stare le altre.
        """
        code, out, _err = _gsettings(["get", MEDIA_KEYS_SCHEMA, "custom-keybindings"])
        if code != 0:
            return []
        return [p for p in _parse_strv(out) if _is_valid_path(p)]

    # -- registrazione -----------------------------------------------------
    def register(self, callbacks: Mapping[str, str]) -> bool:
        """Registra le scorciatoie indicate.

        ``callbacks`` e' una mappa ``id -> descrizione``; il valore non e'
        un callback perche' l'attivazione avviene via D-Bus, non
        in-process.
        """
        if not self.is_available:
            self._last_error = (
                f"schema {MEDIA_KEYS_SCHEMA} non disponibile: "
                "le scorciatoie globali restano disattivate"
            )
            return False

        ok = True
        for sid, description in callbacks.items():
            if not self.register_single(sid, description):
                ok = False
        return ok

    def register_single(self, sid: str, description: str = "") -> bool:
        """Registra una singola scorciatoia.

        L'acceleratore viene letto da :meth:`accelerator_for`, che lo
        recupera dalle preferenze dell'applicazione.
        """
        accelerator = self.accelerator_for(sid)
        if not accelerator:
            self._last_error = f"nessun tasto configurato per {sid}"
            return False

        shortcut = GSettingsShortcut(
            id=sid, accelerator=accelerator, description=description or sid
        )
        path = shortcut.gsettings_path
        if not _is_valid_path(path):
            self._last_error = f"percorso di scorciatoia non valido: {path}"
            return False

        # 1. accodare il percorso
        code, out, err = _gsettings(["get", MEDIA_KEYS_SCHEMA, "custom-keybindings"])
        if code != 0:
            self._last_error = f"lettura custom-keybindings fallita: {err}"
            return False
        # I percorsi non validi vengono scartati: lasciarli dentro
        # farebbe crashare gsd-media-keys (vedi _is_valid_path).
        paths = [p for p in _parse_strv(out) if _is_valid_path(p)]
        if path not in paths:
            paths.append(path)
            code, _out, err = _gsettings(
                [
                    "set",
                    MEDIA_KEYS_SCHEMA,
                    "custom-keybindings",
                    "[" + ", ".join(f"'{p}'" for p in paths) + "]",
                ]
            )
            if code != 0:
                self._last_error = f"scrittura custom-keybindings fallita: {err}"
                return False
            self._owned.append(path)

        # 2. i dettagli della scorciatoia
        for key, value in (
            ("name", shortcut.description),
            ("command", shortcut.command),
            ("binding", accelerator),
        ):
            code, _out, err = _gsettings(
                ["set", f"{CUSTOM_KEYBINDING_SCHEMA}:{path}", key, value]
            )
            if code != 0:
                self._last_error = f"impostazione di {key} fallita: {err}"
                return False

        self._shortcuts[sid] = shortcut
        self._last_error = None
        return True

    def unregister(self) -> None:
        """Rimuove le scorciatoie registrate da questo manager.

        Si rimuovono i percorsi che questo manager ha scritto, piu' quelli
        che portano lo stesso prefisso dell'app: cosi' un manager nuovo
        (per esempio dopo un riavvio dell'applicazione) pulisce lo stesso
        insieme di scorciatoie, e non lascia orfoni nel ``media-keys``.
        """
        code, out, _err = _gsettings(["get", MEDIA_KEYS_SCHEMA, "custom-keybindings"])
        if code == 0:
            mine = set(self._owned)
            paths = [
                p
                for p in _parse_strv(out)
                if _is_valid_path(p) and p not in mine and not self._is_ours(p)
            ]
            _gsettings(
                [
                    "set",
                    MEDIA_KEYS_SCHEMA,
                    "custom-keybindings",
                    "[" + ", ".join(f"'{p}'" for p in paths) + "]",
                ]
            )
        self._owned = []
        self._shortcuts.clear()

    def _is_ours(self, path: str) -> bool:
        """True se il percorso appartiene a una scorciatoia dell'app.

        Le scorciatoie dell'app stanno sotto
        ``.../custom-keybindings/<id>/`` dove ``<id>`` e' uno degli
        identificatori logici; le scorciatoie personalizzate dell'utente
        hanno nomi arbitrari e non vanno toccate.
        """
        name = path.rstrip("/").rsplit("/", 1)[-1]
        return name in KNOWN_SHORTCUT_IDS

    # -- acceleratori ------------------------------------------------------
    def accelerator_for(self, sid: str) -> str:
        """Il tasto configurato per ``sid`` nelle preferenze dell'app."""
        try:
            from . import config as _config

            settings = _config.load_settings()
        except Exception:
            return ""
        return settings.shortcuts.get(sid, "")


def _parse_strv(value: str) -> list[str]:
    """Scompone la rappresentazione GSettings di una lista di stringhe.

    ``gsettings get`` stampa una lista Python-like (``['a', 'b']``), ma
    a volte la precede con il tipo (``@as []``): quel prefisso va
    scartato, altrimenti finirebbe nel risultato come se fosse un
    percorso di scorciatoia.

    Il prefisso '@as ' era la causa di un crash di ``gsd-media-keys``:
    la stringa ``@as []`` finiva scritta fra i percorsi delle
    scorciatoie, ``g_settings_new_with_path`` restituiva ``NULL`` e il
    plugin andava in dereferenza di puntatore nullo, morendo con
    SIGSEGV e portandosi dietro **tutte** le scorciatoie di GNOME.

    Lo split sulla virgola era un secondo difetto, piu' subdolo: un
    percorso che contenga una virgola viene spezzato in due, e il primo
    frammento e' di per se' un percorso valido sotto la radice — misurato:
    ``'.../custom-keybindings/toggle,listen/'`` tornava come
    ``'.../custom-keybindings/toggle'``, che ``_is_valid_path`` accetta.
    La lista letta non corrispondeva piu' a quella scritta. Percio' lo
    split segue le virgolette: una virgola dentro una stringa quotata non
    separa.
    """
    text = value.strip()
    if text.startswith("@"):
        # '@as [...]' -> '[...]'
        _, _, text = text.partition(" ")
        text = text.strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    if not text.strip():
        return []
    out: list[str] = []
    current: list[str] = []
    quote: str | None = None
    for ch in text:
        if quote is not None:
            if ch == quote:
                quote = None
            else:
                current.append(ch)
        elif ch in ("'", '"'):
            quote = ch
        elif ch == ",":
            item = "".join(current).strip()
            if item:
                out.append(item)
            current = []
        else:
            current.append(ch)
    item = "".join(current).strip()
    if item:
        out.append(item)
    return out
