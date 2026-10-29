"""Scorciatoie globali via il portal ``org.freedesktop.portal.GlobalShortcuts``.

**Questo percorso e' il secondario.** Su GNOME 50 il percorso primario e'
quello via GSettings (``shortcuts_gsettings.py``), che non ha i tre
problemi descritti sotto. Il portal resta come alternativa per i desktop
dove il ``media-keys`` non e' disponibile.

Restrizioni reali misurate su Fedora 44 / GNOME Shell 50.6 / Wayland:

* il portal ``GlobalShortcuts`` e' attivo, ma **richiede un app id**:
  ``CreateSession`` risponde ``org.freedesktop.portal.Error.NotAllowed: An
  app id is required`` se il chiamante non e' in uno scope systemd
  ``app-<app-id>.<pid>.scope`` il cui id corrisponda a un file ``.desktop``
  installato (in ``~/.local/share/applications`` o in
  ``/usr/share/applications``) con un ``Exec`` **esistente**; e' il
  backend GNOME a risolvere l'app id dal cgroup del chiamante;
* il backend **aborta** (SIGABRT, ``xdp_session_initable_init: assertion
  failed: (session->token != NULL)``) se ``CreateSession`` non riceve
  ``session_handle_token``: il token va sempre inviato;
* il formato del trigger e' il campo stringa ``preferred_trigger`` della
  specifica xdg-desktop-portal, non una lista di keycode: il backend GNOME
  50 normalizza la stringa dividendola su ``+`` e confronta i token
  modificatori con ``strcmp`` (corsivo esatto) contro ``CTRL``, ``SHIFT``,
  ``NUM``, ``LOGO`` e il tasto Super;
* ``BindShortcuts`` **apre un dialogo di conferma** ("Add Keyboard
  Shortcuts" di gnome-control-center) e la risposta del portal non
  arriva finche' il dialogo non viene chiuso;
* una sessione puo' fare un solo bind: un secondo ``BindShortcuts`` sulla
  stessa sessione fallisce con "Session already has bound shortcuts";
* su GNOME 50 la UI di rebind del portal (``ConfigureShortcuts``) non
  esiste: l'app deve fornire la propria UI e il portal si limita a
  consegnare il segnale ``Activated``.

Il modulo non deve mai far cadere l'applicazione: se il portal manca o
una registrazione fallisce si registra un avviso e l'app prosegue.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

logger = logging.getLogger(__name__)

# -- costanti del portal ----------------------------------------------------

PORTAL_BUS_NAME = "org.freedesktop.portal.Desktop"
PORTAL_OBJECT_PATH = "/org/freedesktop/portal/desktop"
PORTAL_INTERFACE = "org.freedesktop.portal.GlobalShortcuts"
SESSION_INTERFACE = "org.freedesktop.portal.Session"
REQUEST_INTERFACE = "org.freedesktop.portal.Request"

#: Modificatori accettati dal backend GNOME 50, nella forma ``<nome>``
#: che il backend produce internamente.
CANONICAL_MODIFIERS = ("<ctrl>", "<shift>", "<alt>", "<mod2>", "<super>")

#: Traduzione dalla sintassi GTK (usata da ``config.Settings.shortcuts``)
#: alla forma canonica attesa dal parser GTK del backend GNOME.
#: La grafia conta: ``gtk_accelerator_parse`` rifiuta ``<ctrl>`` e
#: accetta solo ``<Ctrl>``.
GTK_MODIFIER_TO_PORTAL: dict[str, str] = {
    "<ctrl>": "<Ctrl>",
    "<control>": "<Ctrl>",
    "<primary>": "<Primary>",
    "<shift>": "<Shift>",
    "<alt>": "<Alt>",
    "<mod2>": "<Mod2>",
    "<super>": "<Super>",
    "<meta>": "<Super>",
}

#: Descrizioni leggibili in italiano per gli identificatori logici.
DEFAULT_DESCRIPTIONS: dict[str, str] = {
    "toggle_listen": "Avvia/ferma la trascrizione",
    "toggle_insert": "Attiva/disattiva l'inserimento nel documento",
    "toggle_transparent": "Modalita' semitrasparente",
    "toggle_window": "Mostra/nascondi la finestra",
    "bookmark": "Crea un segnalibro",
    "jump_to_latest": "Torna al testo piu' recente",
}

#: Acceleratori proposti al portal. Sono i **preferiti**, non vincolanti:
#: il backend di GNOME puo' riassegnarli, e l'utente puo' cambiarli.
#: Scelti per non collidere con le scorciatoie di GNOME e di LibreOffice.
DEFAULT_ACCELERATORS: dict[str, str] = {
    "toggle_listen": "<Ctrl><Super>L",
    "toggle_insert": "<Ctrl><Super>I",
    "toggle_transparent": "<Ctrl><Super>T",
    "toggle_window": "<Ctrl><Super>W",
    "bookmark": "<Ctrl><Super>B",
    "jump_to_latest": "<Ctrl><Super>J",
}

#: Sostituzioni dei nomi dei tasti non alfanumerici, che il portal
#: pretende in forma canonica (``plus`` invece di ``+``).
_KEY_ALIASES: dict[str, str] = {
    "+": "plus",
    "-": "minus",
    " ": "space",
}

#: Grafia canonica GTK dei tasti speciali con underscore o doppia
#: maiuscola, indicizzata per nome minuscolo. ``gtk_accelerator_parse``
#: è case sensitive: ``Kp_enter`` viene rifiutato, ``KP_Enter`` no.
_SPECIAL_KEYS: dict[str, str] = {
    "kp_enter": "KP_Enter",
    "kp_space": "KP_Space",
    "kp_tab": "KP_Tab",
    "kp_f1": "KP_F1",
    "kp_f2": "KP_F2",
    "kp_f3": "KP_F3",
    "kp_f4": "KP_F4",
    "kp_home": "KP_Home",
    "kp_left": "KP_Left",
    "kp_up": "KP_Up",
    "kp_right": "KP_Right",
    "kp_down": "KP_Down",
    "kp_page_up": "KP_Page_Up",
    "kp_page_down": "KP_Page_Down",
    "kp_end": "KP_End",
    "kp_begin": "KP_Begin",
    "kp_insert": "KP_Insert",
    "kp_delete": "KP_Delete",
    "kp_0": "KP_0",
    "kp_1": "KP_1",
    "kp_2": "KP_2",
    "kp_3": "KP_3",
    "kp_4": "KP_4",
    "kp_5": "KP_5",
    "kp_6": "KP_6",
    "kp_7": "KP_7",
    "kp_8": "KP_8",
    "kp_9": "KP_9",
    "kp_multiply": "KP_Multiply",
    "kp_add": "KP_Add",
    "kp_subtract": "KP_Subtract",
    "kp_decimal": "KP_Decimal",
    "kp_divide": "KP_Divide",
    "kp_equal": "KP_Equal",
    "page_up": "Page_Up",
    "page_down": "Page_Down",
    "num_lock": "Num_Lock",
    "scroll_lock": "Scroll_Lock",
    "caps_lock": "Caps_Lock",
    "shift_lock": "Shift_Lock",
    "iso_level3_shift": "ISO_Level3_Shift",
    "iso_left_tab": "ISO_Left_Tab",
    "multi_key": "Multi_key",
}


@dataclass(frozen=True)
class Shortcut:
    """Una scorciatoia registrata, con i dati necessari per ri-pubblicarla."""

    id: str
    accelerator: str
    description: str

    @property
    def trigger(self) -> str:
        """La stringa ``preferred_trigger`` da consegnare al portal."""
        return to_portal_trigger(self.accelerator)


# ---------------------------------------------------------------------------
# Conversione degli acceleratori
# ---------------------------------------------------------------------------


def split_accelerator(accelerator: str) -> tuple[list[str], str]:
    """Scompone un acceleratore GTK in modificatori e tasto.

    Accetta sia la forma GTK (``<Ctrl><Super><Shift>A``) sia la forma XDG
    con il ``+`` come separatore (``<Ctrl>+<Super>+<Shift>+A``). La forma
    con il ``+`` e' quella che il backend del portal sa dividere da solo.

    Attenzione al separatore: con ``<Ctrl>+<Super>+A`` l'ultimo ``+`` sta
    **dentro** la parte che segue l'ultimo modificatore, perche' lo split
    semplice su ``+`` lascia i ``+`` dei modificatori nella coda —
    misura: ``split_accelerator("<Ctrl>+<Super>+F13")`` restituiva la
    chiave ``'+++F13'``, e ``normalize_accelerator`` produceva
    ``'<Ctrl><Super>+++f13'``, che ``gtk_accelerator_parse`` rifiuta.
    """
    text = accelerator.strip()
    if not text:
        return [], ""

    if "+" in text and not text.startswith("<"):
        parts = [p.strip() for p in text.split("+") if p.strip()]
        if not parts:
            return [], ""
        return parts[:-1], parts[-1]

    mods: list[str] = []
    key = ""
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "<":
            end = text.find(">", i)
            if end < 0:
                key += text[i:]
                break
            mods.append(text[i : end + 1])
            i = end + 1
        elif text[i] == "+":
            # separatore XDG nella forma mista: si salta, non e' parte del
            # tasto. La coda che resta (compresi eventuali ``+`` residui)
            # viene ripulita da ``normalize_accelerator``.
            i += 1
        else:
            key += text[i]
            i += 1
    return mods, key


def normalize_accelerator(accelerator: str) -> str:
    """Riporta un acceleratore alla forma canonica del portal.

    Il backend GNOME 50 non accetta la sintassi XDG (``CTRL+a``): il
    trigger arriva al provider di gnome-control-center, che lo smonta con
    ``gtk_accelerator_parse_with_keycode()``. Quel parser e' **case
    sensitive** e riconosce solo i nomi canonici GTK: ``<Ctrl>``,
    ``<Shift>``, ``<Alt>``, ``<Super>``, ``<Primary>``, ``<Meta>``.

    Verificato con ``Gtk.accelerator_parse()`` su GTK 4.20:

        '<Ctrl><Super><Shift>F13'  -> ok=True   (key=F13, mask=...)
        '<ctrl><super><shift>f13'  -> ok=False  (nomi minuscoli: rifiutati)
        '<ctrl>+<super>+<shift>+f13' -> ok=False (i '+' non vanno nel trigger)

    Quindi il trigger deve conservare la grafia dei modificatori e
    l'iniziale maiuscola del tasto.
    """
    mods, key = split_accelerator(accelerator)
    if not key:
        return ""

    canon: list[str] = []
    for mod in mods:
        mapped = GTK_MODIFIER_TO_PORTAL.get(mod.lower())
        if mapped is None:
            # forma senza parentesi angolari: "Ctrl+Super+L" e
            # "CTRL+SUPER+F13" arrivano dall'utente o da un file di
            # configurazione. Senza questo ramo "ctrl" (la forma piu'
            # comune!) non era in mappa e veniva **scartata in silenzio**:
            # misurato, "Ctrl+Super+L" diventava "<Super>L", cioe' una
            # scorciatoia diversa da quella chiesta e in conflitto con
            # GNOME. Uno shortcut che perde un modificatore non e' una
            # scorciatoia piu' comoda: e' un tasto che parte quando non
            # dovrebbe.
            stripped = mod.strip("<>").lower()
            if stripped in ("ctrl", "control", "primary", "cmd", "command"):
                mapped = "<Ctrl>"
            elif stripped in ("meta", "super", "logo", "win", "windows"):
                mapped = "<Super>"
            elif stripped == "shift":
                mapped = "<Shift>"
            elif stripped in ("alt", "altgr", "mod1"):
                mapped = "<Alt>"
            elif stripped == "mod2":
                mapped = "<Mod2>"
            else:
                logger.debug("Modificatore sconosciuto ignorato: %r", mod)
                continue
        if mapped not in canon:
            canon.append(mapped)

    key = key.strip()
    # i '+' residui della forma mista ("<Ctrl>+<Super>+F13" lasciava
    # "+++F13"): non fanno parte del nome del tasto
    key = key.lstrip("+").strip()
    key = _KEY_ALIASES.get(key, key)
    # il tasto e' una lettera: l'iniziale va maiuscola, il resto minuscolo
    if len(key) == 1 and key.isalpha():
        key = key.upper()
    elif len(key) > 1:
        # i tasti speciali hanno una grafia canonica che NON e' solo
        # "iniziale maiuscola": ``KP_Enter`` e ``Page_Up`` contengono un
        # underscore e una seconda maiuscola. Scriverli come
        # ``Kp_enter``/``Page_up`` fa fallire ``gtk_accelerator_parse`` —
        # misurato: quelle scorciatoie venivano rifiutate in silenzio.
        canonical = _SPECIAL_KEYS.get(key.lower())
        if canonical is not None:
            key = canonical
        elif key.isalpha():
            # nomi senza underscore o cifi: Return, Escape, F13...
            key = key[0].upper() + key[1:].lower()

    if not canon:
        return key
    return "".join([*canon, key])


def to_portal_trigger(accelerator: str) -> str:
    """Converte un acceleratore nella stringa ``preferred_trigger``."""
    return normalize_accelerator(accelerator)


def find_conflicts(
    accelerators: Mapping[str, str],
) -> list[tuple[str, str]]:
    """Trova le coppie di identificatori che si conflittano.

    Restituisce l'elenco delle coppie ``(id_a, id_b)`` che condividono la
    stessa scorciatoia normalizzata.
    """
    seen: dict[str, str] = {}
    conflicts: list[tuple[str, str]] = []
    for sid, accel in accelerators.items():
        norm = normalize_accelerator(accel)
        if not norm:
            continue
        if norm in seen:
            conflicts.append((seen[norm], sid))
        else:
            seen[norm] = sid
    return conflicts


# ---------------------------------------------------------------------------
# Client del portal
# ---------------------------------------------------------------------------


class PortalUnavailable(RuntimeError):
    """Il portal ``GlobalShortcuts`` non e' raggiungibile."""


class ShortcutManager:
    """Registra le scorciatoie globali presso il portal.

    Uso tipico::

        manager = ShortcutManager()
        manager.register({
            "toggle_listen": "<Ctrl><Super><Shift>A",
            ...
        })

    I callback ricevuti dal segnale ``Activated`` vengono eseguiti sul
    main loop di GLib (``GLib.idle_add``), cosi' il codice GTK puo'
    girare dentro il callback.
    """

    def __init__(self, app_id: str = "it.lecturetranscriber.App") -> None:
        self._app_id = app_id
        self._conn: Gio.DBusConnection | None = None
        self._session: str | None = None
        self._callbacks: dict[str, Callable[[], None]] = {}
        self._shortcuts: dict[str, Shortcut] = {}
        self._available = False
        self._subscription: int = 0
        self._response_subscription: int = 0
        self._lock = threading.RLock()
        self._token_counter = 0
        #: risposte ``Response`` del portal, indicizzate per request path
        self._responses: dict[str, tuple[int, dict[str, Any]]] = {}
        #: motivo per cui le scorciatoie non sono attive, se non lo sono
        self._last_error: str | None = None

    # -- stato -------------------------------------------------------------
    @property
    def available(self) -> bool:
        """Vero se il portal e' raggiungibile e la sessione e' attiva."""
        return self._available

    @property
    def last_error(self) -> str | None:
        """Motivo del mancato funzionamento, da mostrare all'utente.

        Senza questo il fallimento del portal era muto: misura, il
        chiamante non aveva modo di distinguere "portal assente" da
        "scorciatoie registrate" e l'utente restava senza tasti.
        """
        return self._last_error

    def is_available(self) -> bool:
        """Vero se il portal e' disponibile (vedi :attr:`available`)."""
        return self._available

    @property
    def session_path(self) -> str | None:
        """Il percorso oggetto della sessione del portal, se attiva."""
        return self._session

    # -- ciclo di vita -----------------------------------------------------
    def register(
        self,
        callbacks: Mapping[str, Callable[[], None]],
        accelerators: Mapping[str, str] | None = None,
    ) -> None:
        """Registra le scorciatoie indicate e i relativi callback.

        ``callbacks`` associa un id alla funzione da chiamare;
        ``accelerators`` (facoltativo) associa lo stesso id al tasto
        desiderato, e in sua mancanza si usano :data:`DEFAULT_ACCELERATORS`.

        I due mappami sono tenuti **separati**: la versione precedente
        iterava ``callbacks.items()`` passando le funzioni dove si
        aspettava una stringa, e ``to_portal_trigger(<function>)``
        sollevava ``AttributeError`` — misurato: ``'function' object has
        no attribute 'strip'``. Con il portal disponibile il crash usciva
        da qui, contraddicendo la promessa "non solleva mai", e la
        sessione del portal restava attiva senza scorciatoie.

        Non solleva mai: se il portal non e' disponibile registra un
        avviso e lascia l'applicazione usabile.
        """
        with self._lock:
            self._callbacks = dict(callbacks)
            self._shortcuts = {}
            accel_map = dict(accelerators or {})

            if not self._connect():
                return

            session = self._create_session()
            if session is None:
                self._available = False
                return
            self._session = session
            self._available = True
            self._subscribe_activated()

            for sid in self._callbacks:
                accelerator = accel_map.get(sid) or DEFAULT_ACCELERATORS.get(sid, "")
                description = DEFAULT_DESCRIPTIONS.get(sid, sid)
                self._register_one(sid, accelerator, description)

            conflicts = find_conflicts(
                {s.id: s.accelerator for s in self._shortcuts.values()}
            )
            if conflicts:
                logger.warning(
                    "Scorciatoie in conflitto: %s",
                    ", ".join(f"{a} / {b}" for a, b in conflicts),
                )

    def unregister(self) -> None:
        """Rilascia la sessione del portal e cancella i callback."""
        with self._lock:
            if self._subscription and self._conn is not None:
                try:
                    self._conn.signal_unsubscribe(self._subscription)
                except Exception:  # pragma: no cover - difensivo
                    pass
                self._subscription = 0

            if self._response_subscription and self._conn is not None:
                try:
                    self._conn.signal_unsubscribe(self._response_subscription)
                except Exception:  # pragma: no cover - difensivo
                    pass
                self._response_subscription = 0
            self._responses.clear()

            if self._session and self._conn is not None:
                try:
                    # ``org.freedesktop.portal.Session`` espone ``Close``,
                    # non ``Destroy`` (verificato in
                    # /usr/share/dbus-1/interfaces/org.freedesktop.portal.Session.xml).
                    # Chiamare ``Destroy`` fa fallire la chiamata con
                    # UnknownMethod: la sessione del portal non veniva
                    # rilasciata e restavano appesi i bind delle
                    # scorciatoie — misurato: ogni riavvio dell'app
                    # lasciava una sessione orfana.
                    self._conn.call_sync(
                        PORTAL_BUS_NAME,
                        self._session,
                        SESSION_INTERFACE,
                        "Close",
                        None,
                        None,
                        Gio.DBusCallFlags.NONE,
                        4000,
                        None,
                    )
                except Exception as exc:  # pragma: no cover - difensivo
                    logger.debug("Rilascio della sessione portal fallito: %s", exc)

            self._session = None
            self._available = False
            self._callbacks.clear()
            self._shortcuts.clear()

    # API comoda per ``with``/chiusione dell'app
    def close(self) -> None:
        """Alias di :meth:`unregister`."""
        self.unregister()

    def __enter__(self) -> "ShortcutManager":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.unregister()

    # -- API pubblica ------------------------------------------------------
    def register_single(
        self, id: str, accelerator: str, description: str
    ) -> bool:
        """Registra (o sostituisce) una singola scorciatoia a runtime.

        Restituisce ``True`` se la registrazione e' andata a buon fine.
        """
        with self._lock:
            if not self._available or self._session is None:
                logger.warning(
                    "Impossibile registrare %s: il portal non e' disponibile", id
                )
                return False
            ok = self._register_one(id, accelerator, description)
            return ok

    def list_registered(self) -> list[tuple[str, str]]:
        """Elenco ``(id, acceleratore)`` delle scorciatoie registrate."""
        with self._lock:
            return [(s.id, s.accelerator) for s in self._shortcuts.values()]

    def list_triggers(self) -> list[tuple[str, str]]:
        """Elenco ``(id, preferred_trigger)`` come consegnato al portal."""
        with self._lock:
            return [(s.id, s.trigger) for s in self._shortcuts.values()]

    # -- interni -----------------------------------------------------------
    def _connect(self) -> bool:
        """Apre la connessione al bus di sessione e verifica il portal."""
        if self._conn is not None:
            return True
        try:
            self._conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except Exception as exc:
            self._last_error = f"bus di sessione non disponibile: {exc}"
            logger.warning(
                "Bus di sessione D-Bus non disponibile: %s. "
                "Le scorciatoie globali restano disattivate.",
                exc,
            )
            self._conn = None
            return False

        try:
            self._conn.call_sync(
                PORTAL_BUS_NAME,
                PORTAL_OBJECT_PATH,
                "org.freedesktop.DBus.Introspectable",
                "Introspect",
                None,
                None,
                Gio.DBusCallFlags.NONE,
                3000,
                None,
            )
        except Exception as exc:
            self._last_error = (
                f"portal {PORTAL_BUS_NAME} non raggiungibile: {exc}"
            )
            logger.warning(
                "Il portal %s non e' raggiungibile (%s): "
                "le scorciatoie globali restano disattivate.",
                PORTAL_BUS_NAME,
                exc,
            )
            self._conn = None
            return False

        self._subscribe_responses()
        return True

    def _subscribe_responses(self) -> None:
        """Sottoscrive **permanentemente** al segnale ``Response``.

        La sottoscrizione deve esistere *prima* della chiamata che genera
        la risposta: il backend del portal risponde a ``CreateSession``
        in modo quasi immediato, e D-Bus scarta i segnali sui quali
        nessuno e' iscritto al momento della consegna. Senza questa
        sottoscrizione permanente la richiesta resterebbe in attesa per
        sempre.
        """
        if self._conn is None or self._response_subscription:
            return
        assert self._conn is not None

        def on_response(
            _conn: Gio.DBusConnection,
            _sender: str | None,
            path: str,
            _iface: str,
            _member: str,
            params: GLib.Variant,
        ) -> None:
            if not path.startswith("/org/freedesktop/portal/desktop/request/"):
                return
            try:
                code, payload = params.unpack()
            except Exception:  # pragma: no cover - difensivo
                return
            with self._lock:
                self._responses[path] = (int(code), _unpack(payload))

        self._response_subscription = self._conn.signal_subscribe(
            PORTAL_BUS_NAME,
            REQUEST_INTERFACE,
            "Response",
            None,
            None,
            Gio.DBusSignalFlags.NONE,
            on_response,
        )

    def _next_token(self, prefix: str) -> str:
        with self._lock:
            self._token_counter += 1
            return f"{prefix}{self._token_counter}"

    def _create_session(self) -> str | None:
        """Crea la sessione del portal e ne restituisce il percorso.

        ``session_handle_token`` e' obbligatorio: senza il backend
        GNOME termina con un assertion fallito (``SIGABRT``).
        """
        assert self._conn is not None
        token = self._next_token("ltsess")
        # stesso principio di _register_one: dizionario di Variants dentro
        # la tupla, non una 'a{sv}' pre-costruita.
        options = GLib.Variant(
            "(a{sv})",
            (
                {
                    "handle_token": GLib.Variant("s", token),
                    "session_handle_token": GLib.Variant("s", token),
                },
            ),
        )
        try:
            reply = self._conn.call_sync(
                PORTAL_BUS_NAME,
                PORTAL_OBJECT_PATH,
                PORTAL_INTERFACE,
                "CreateSession",
                options,
                GLib.VariantType.new("(o)"),
                Gio.DBusCallFlags.NONE,
                8000,
                None,
            )
        except Exception as exc:
            self._last_error = f"CreateSession del portal fallito: {exc}"
            logger.warning(
                "CreateSession del portal fallito (%s). "
                "Su GNOME il chiamante deve stare in uno scope systemd "
                "'app-<app-id>.<pid>.scope' con un .desktop installato: "
                "le scorciatoie globali restano disattivate.",
                exc,
            )
            return None

        request_path = reply.unpack()[0]
        results = self._await_request(request_path, timeout=10.0)
        if results is None:
            self._last_error = "il portal non ha risposto a CreateSession"
            logger.warning(
                "Il portal non ha risposto a CreateSession: "
                "le scorciatoie globali restano disattivate."
            )
            return None

        code, payload = results
        if code != 0:
            self._last_error = f"CreateSession ha restituito il codice {code}"
            logger.warning(
                "CreateSession del portal ha restituito il codice %s: "
                "le scorciatoie globali restano disattivate.",
                code,
            )
            return None

        session = payload.get("session_handle")
        if not session:
            self._last_error = "CreateSession non ha restituito session_handle"
            logger.warning("CreateSession non ha restituito session_handle.")
            return None
        return str(session)

    def _await_request(
        self, request_path: str, timeout: float = 10.0
    ) -> tuple[int, dict[str, Any]] | None:
        """Attende la risposta ``Response`` del portal per una richiesta.

        La sottoscrizione al segnale ``Response`` e' **permanente** (vedi
        :meth:`_connect`): il backend risponde a ``CreateSession" in modo
        quasi immediato e una sottoscrizione creata dopo la chiamata
        perderebbe il segnale, perche' D-Bus scarta i segnali sui quali
        nessuno e' iscritto al momento della consegna.
        """
        assert self._conn is not None
        deadline = time.monotonic() + timeout

        with self._lock:
            found = self._responses.pop(request_path, None)
        if found is not None:
            return found

        loop = GLib.MainLoop()

        def poll() -> bool:
            with self._lock:
                ready = request_path in self._responses
            if ready:
                loop.quit()
            return True

        def expire() -> bool:
            loop.quit()
            return False

        poller = GLib.timeout_add(50, poll)
        ticker = GLib.timeout_add(int(timeout * 1000), expire)
        try:
            loop.run()
        finally:
            ctx = GLib.MainContext.default()
            for source in (poller, ticker):
                if ctx.find_source_by_id(source) is not None:
                    GLib.source_remove(source)

        with self._lock:
            return self._responses.pop(request_path, None)

    def _register_one(self, sid: str, accelerator: str, description: str) -> bool:
        """Registra una singola scorciatoia presso il portal."""
        assert self._conn is not None
        trigger = to_portal_trigger(accelerator)
        if not trigger:
            logger.warning("Acceleratore non valido per %s: %r", sid, accelerator)
            return False

        shortcut = Shortcut(id=sid, accelerator=accelerator, description=description)
        with self._lock:
            self._shortcuts[sid] = shortcut

        # Costruzione delle varianti D-Bus.
        #
        # PyGObject accetta i valori nidi in forma "Python" dentro la
        # tupla del messaggio, non come GLib.Variant gia' costruiti: passare
        # una 'a{sv}' gia' costruita dentro una tupla solleva
        # "KeyError: 0" (verificato su PyGObject 3.52 / Fedora 44).
        # Le forme che funzionano sono state provate contro il portal
        # vivo: tupla con dizionario, lista di tuple, VariantBuilder con
        # dizionario dentro la tupla.
        options = {"handle_token": GLib.Variant("s", self._next_token("ltbind"))}
        shortcuts = [
            (
                sid,
                {
                    "description": GLib.Variant("s", description),
                    "preferred_trigger": GLib.Variant("s", trigger),
                },
            )
        ]
        message = GLib.Variant(
            "(oa(sa{sv})sa{sv})",
            (self._session or "", shortcuts, "", options),
        )
        try:
            reply = self._conn.call_sync(
                PORTAL_BUS_NAME,
                PORTAL_OBJECT_PATH,
                PORTAL_INTERFACE,
                "BindShortcuts",
                message,
                GLib.VariantType.new("(o)"),
                Gio.DBusCallFlags.NONE,
                8000,
                None,
            )
        except Exception as exc:
            logger.warning("Registrazione della scorciatoia %s fallita: %s", sid, exc)
            return False

        request_path = reply.unpack()[0]
        results = self._await_request(request_path, timeout=10.0)
        if results is None:
            logger.warning(
                "Il portal non ha risposto alla registrazione di %s.", sid
            )
            return False
        code, _payload = results
        if code != 0:
            logger.warning(
                "BindShortcuts ha restituito il codice %s per %s.", code, sid
            )
            return False
        logger.info("Scorciatoia registrata: %s -> %s", sid, trigger)
        return True

    def _subscribe_activated(self) -> None:
        """Ascolta il segnale ``Activated`` del portal."""
        assert self._conn is not None

        def on_activated(
            _conn: Gio.DBusConnection,
            _sender: str | None,
            _path: str,
            _iface: str,
            _member: str,
            params: GLib.Variant,
        ) -> None:
            try:
                _session, shortcut_id, _timestamp, _options = params.unpack()
            except Exception:  # pragma: no cover - difensivo
                return
            self._dispatch(str(shortcut_id))

        self._subscription = self._conn.signal_subscribe(
            PORTAL_BUS_NAME,
            PORTAL_INTERFACE,
            "Activated",
            None,
            None,
            Gio.DBusSignalFlags.NONE,
            on_activated,
        )

    def _dispatch(self, shortcut_id: str) -> None:
        """Esegue il callback sul main loop di GLib."""
        with self._lock:
            callback = self._callbacks.get(shortcut_id)
        if callback is None:
            logger.debug("Nessun callback per la scorciatoia %s", shortcut_id)
            return

        def run() -> bool:
            try:
                callback()
            except Exception:
                logger.exception(
                    "Errore nel callback della scorciatoia %s", shortcut_id
                )
            return GLib.SOURCE_REMOVE

        GLib.idle_add(run, priority=GLib.PRIORITY_DEFAULT)


def _unpack(value: Any) -> Any:
    """Trasforma ricorsivamente un ``GLib.Variant`` in valori Python."""
    if isinstance(value, GLib.Variant):
        return _unpack(value.unpack())
    if isinstance(value, dict):
        return {k: _unpack(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_unpack(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Auto-prova
# ---------------------------------------------------------------------------


def _self_test() -> int:
    """Registra una scorciatoia e attende che venga attivata."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    manager = ShortcutManager()
    fired = threading.Event()

    def on_shortcut() -> None:
        print(">>> Callback attivato: la scorciatoia globale funziona.")
        fired.set()

    manager.register({"toggle_listen": "<Ctrl><Super><Shift>F13"})

    if not manager.is_available():
        print(
            "Il portal GlobalShortcuts non e' disponibile su questa macchina: "
            "non e' possibile eseguire la prova.",
            file=sys.stderr,
        )
        manager.unregister()
        return 1

    print("Scorciatoie registrate:", manager.list_registered())
    print("Trigger consegnati al portal:", manager.list_triggers())
    print(
        "Premi Ctrl+Super+Shift+F13 (oppure Ctrl+C per uscire) "
        "per verificare l'attivazione."
    )

    stopping = threading.Event()

    def on_sigint(_signum: int, _frame: object) -> None:
        stopping.set()

    try:
        signal.signal(signal.SIGINT, on_sigint)
    except ValueError:  # pragma: no cover - solo nel thread principale
        pass

    while not stopping.is_set() and not fired.is_set():
        stopping.wait(0.2)

    if fired.is_set():
        print("Prova superata.")
        manager.unregister()
        return 0

    print("Uscita richiesta: nessuna attivazione ricevuta.", file=sys.stderr)
    manager.unregister()
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
