"""Inserimento controllato della trascrizione nelle applicazioni esterne.

Restrizioni reali misurate su Fedora 44 / GNOME 50 / Wayland (vedi
docs/analisi-fattibilita.md):

* ``wtype`` e' rotto: mutter non espone i globali ``wlr-virtual-keyboard``
  (0 occorrenze nel binario), quindi non si puo' sintetizzare tasti via
  protocollo;
* ``ydotool`` funziona (uinput, indipendente dal compositor) ma richiede una
  volta sola ``sudo`` per la regola udev e il gruppo ``input``; e' l'unica
  strada che raggiunge Obsidian/Electron;
* ``at-spi`` scrive nelle applicazioni GTK (verificato: ``insert_text`` su
  un'entry GTK viva);
* l'API UNO scrive in LibreOffice Writer in modo ancorato al cursore
  (verificato: ``insertString`` su un documento swriter);
* il portal ``RemoteDesktop`` espone ``NotifyKeyboardKeysym`` (usata da
  Vocalinux per scrivere in Wayland senza uinput), ma su questa macchina
  ``RemoteDesktop.CreateSession`` **abbatte il daemon xdg-desktop-portal**
  (assertion in xdp-session.c:296) — non e' una via percorribile qui
  (ricrea: research/portal-remotedesktop-crash.txt);
* non esiste alcun portal di input-injection: ogni via scrive globalmente
  sulla superficie che ha il focus.

Per questo l'inserimento e' sempre condizionato da due gate: la modalita'
inserimento deve essere attiva e la finestra attiva deve essere quella
registrata quando l'inserimento e' stato abilitato.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from enum import Enum


class InsertMethod(str, Enum):
    AUTO = "auto"
    LIBREOFFICE = "libreoffice"
    ATSPI = "atspi"
    CLIPBOARD = "clipboard"
    YDOTOOL = "ydotool"
    NONE = "none"


@dataclass
class InsertResult:
    ok: bool
    method: str
    message: str = ""


def _has_uno() -> bool:
    try:
        import uno  # noqa: F401

        return True
    except Exception:
        return False


def _has_atspi() -> bool:
    try:
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi  # noqa: F401

        return True
    except Exception:
        return False


def capabilities() -> dict[str, bool]:
    """Quali metodi di inserimento sono disponibili su questo sistema."""
    ydo = shutil.which("ydotool")
    return {
        "libreoffice_uno": _has_uno(),
        "atspi": _has_atspi(),
        "clipboard": shutil.which("wl-copy") is not None,
        "ydotool": ydo is not None,
        "wtype": shutil.which("wtype") is not None,  # rotto su GNOME 50
    }


def active_window_title() -> str:
    """Titolo della finestra attiva, se ottenibile.

    Il metodo dell'oggetto ``Accessible`` e' ``get_state_set()``:
    ``get_state()`` non esiste in questa PyGObject (verificato su 3.52),
    quindi ogni finestra verrebbe saltata dal ``try/except`` e la funzione
    restituirebbe sempre ``""`` -- con il gate del focus che, non potendo
    verificare, lascerebbe passare tutto.
    """
    try:
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi

        desktop = Atspi.get_desktop(0)
        for i in range(desktop.get_child_count()):
            app = desktop.get_child_at_index(i)
            if app is None:
                continue
            for j in range(app.get_child_count()):
                win = app.get_child_at_index(j)
                if win is None:
                    continue
                try:
                    state = win.get_state_set()
                    if state.contains(Atspi.StateType.ACTIVE):
                        return win.get_name() or ""
                except Exception:
                    continue
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# LibreOffice via UNO
# ---------------------------------------------------------------------------
class LibreOfficeInserter:
    """Scrive in un documento Writer aperto, ancorandosi al cursore.

    UNO richiede che soffice sia gia' avviato con un socket di accettazione
    (``--accept=...``). L'applicazione prova a lanciarlo se non c'e'.
    """

    SOCKET = "socket,host=127.0.0.1,port=2002;urp;StarOffice.ComponentContext"

    def __init__(self) -> None:
        self._ctx = None
        self._desktop = None
        self._lock = threading.Lock()

    def connect(self, timeout: float = 10.0) -> bool:
        """Connessione a soffice; avvia il processo se necessario."""
        deadline = time.monotonic() + timeout
        with self._lock:
            if self._desktop is not None:
                return True
            try:
                import uno
                # ``PropertyValue`` non serve qui, ma importarlo prova che
                # l'API UNO sia completa: se manca, ``connect()`` fallisce
                # subito invece di scoprirlo a meta' inserimento.
                from com.sun.star.beans import PropertyValue  # noqa: F401
            except Exception:
                return False

            started = False
            while time.monotonic() < deadline:
                try:
                    local_ctx = uno.getComponentContext()
                    resolver = local_ctx.ServiceManager.createInstanceWithContext(
                        "com.sun.star.bridge.UnoUrlResolver", local_ctx
                    )
                    ctx = resolver.resolve(
                        f"uno:{self.SOCKET}",
                    )
                    smgr = ctx.ServiceManager
                    desktop = smgr.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
                    self._ctx, self._desktop = ctx, desktop
                    return True
                except Exception:
                    if not started:
                        started = True
                        try:
                            subprocess.Popen(
                                [
                                    "soffice",
                                    f"--accept=socket,host=127.0.0.1,port=2002;urp;",
                                    "--norestore",
                                    "--nodefault",
                                ],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                            )
                        except Exception:
                            return False
                    time.sleep(0.4)
            return False

    def insert(self, text: str) -> InsertResult:
        """Inserisce il testo nel documento attivo di Writer, al cursore."""
        if not self.connect():
            return InsertResult(False, "libreoffice", "Impossibile collegarsi a LibreOffice")
        with self._lock:
            try:
                # il documento attivo: se non ce n'e' nessuno, non si tocca nulla
                doc = self._desktop.getCurrentComponent()
                if doc is None:
                    return InsertResult(
                        False, "libreoffice", "Nessun documento Writer aperto"
                    )
                text_obj = doc.getText()
                cursor = text_obj.createTextCursor()
                cursor.gotoEnd(False)
                text_obj.insertString(cursor, text, False)
                return InsertResult(True, "libreoffice", "")
            except Exception as exc:
                return InsertResult(False, "libreoffice", f"Errore UNO: {exc}")


# ---------------------------------------------------------------------------
# at-spi: applicazioni GTK
# ---------------------------------------------------------------------------
class AtspiInserter:
    """Scrive in un campo di testo editabile di un'applicazione GTK.

    at-spi espone l'interfaccia EditableText solo se l'applicazione la
    implementa: vale per GTK (gnome-text-editor, gedit, i nostri dialoghi) ma
    non per Electron/Chromium, che espongono un albero di sola lettura.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def _find_editable(self):
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi

        desktop = Atspi.get_desktop(0)
        for i in range(desktop.get_child_count()):
            app = desktop.get_child_at_index(i)
            if app is None:
                continue
            for j in range(app.get_child_count()):
                win = app.get_child_at_index(j)
                if win is None:
                    continue
                node = self._find_editable_in(win)
                if node is not None:
                    return node
        return None

    def _find_editable_in(self, node):
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi

        try:
            iface = node.get_interfaces()
            # ``get_interfaces()`` restituisce una **lista di stringhe**
            # (verificato su PyGObject 3.52): i nomi sono "Accessible",
            # "EditableText", ... Usare ``iface.contains(...)`` solleverebbe
            # AttributeError, perche' le liste Python non hanno quel metodo.
            if iface and "EditableText" in iface and self._is_editable(node):
                return node
        except Exception:
            pass
        try:
            for k in range(node.get_child_count()):
                child = node.get_child_at_index(k)
                if child is None:
                    continue
                found = self._find_editable_in(child)
                if found is not None:
                    return found
        except Exception:
            pass
        return None

    @staticmethod
    def _is_editable(node) -> bool:
        """Vero se il nodo e' un campo editabile e mostrato, non una label.

        Misurato su GNOME 50: **~310 nodi** dell'albero at-spi espongono
        ``EditableText``, ma uno solo ha ``EDITABLE`` -- un campo nascosto
        nel pannello di gnome-shell, che sta con ``SHOWING=False`` (non
        e' effettivamente reso a schermo). Tutti gli altri sono etichette
        statiche di pannelli e menu, che accettano ``insert_text`` senza
        produrre nulla di visibile. Senza questo doppio filtro
        l'inserimento finirebbe nel pannello, non nel documento.
        """
        try:
            import gi

            gi.require_version("Atspi", "2.0")
            from gi.repository import Atspi

            state = node.get_state_set()
            if not state.contains(Atspi.StateType.EDITABLE):
                return False
            # un campo puo' essere marcato visibile ma non mostrato (e' il
            # caso del campo di gnome-shell): in quello stato non accetta
            # davvero input e la scrittura non avrebbe alcun effetto.
            return bool(state.contains(Atspi.StateType.SHOWING))
        except Exception:
            return False

    def insert(self, text: str) -> InsertResult:
        with self._lock:
            try:
                node = self._find_editable()
                if node is None:
                    return InsertResult(
                        False, "atspi", "Nessun campo di testo GTK attivo e modificabile"
                    )
                editable = node.get_editable_text()
                # alla fine del contenuto esistente
                offset = node.get_character_count()
                editable.insert_text(offset, text, len(text))
                return InsertResult(True, "atspi", "")
            except Exception as exc:
                return InsertResult(False, "atspi", f"Errore at-spi: {exc}")


# ---------------------------------------------------------------------------
# ydotool: simulazione tastiera (unica via verso Electron)
# ---------------------------------------------------------------------------
class YdotoolInserter:
    """Simula la tastiera via uinput.

    Richiede la preparazione una tantum: regola udev per /dev/uinput e
    appartenenza al gruppo ``input`` (vedi docs/installazione.md).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def insert(self, text: str) -> InsertResult:
        if shutil.which("ydotool") is None:
            return InsertResult(False, "ydotool", "ydotool non installato")
        with self._lock:
            try:
                # ydotool accetta una sequenza di tasti, non testo UTF-8 libero:
                # per il percorso Electron si usa il clipboard + ctrl+v
                # (stdout devnull: wl-copy fork-a in background e la pipe
                # farebbe scadere il timeout)
                proc = subprocess.run(
                    ["wl-copy", "--type", "text/plain"],
                    input=text.encode("utf-8"),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                )
                if proc.returncode != 0:
                    return InsertResult(False, "ydotool", "wl-copy fallito")
                subprocess.run(
                    ["ydotool", "key", "ctrl+v"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                )
                return InsertResult(True, "ydotool", "")
            except subprocess.TimeoutExpired:
                return InsertResult(False, "ydotool", "Timeout di ydotool/wl-copy")
            except Exception as exc:
                return InsertResult(False, "ydotool", f"Errore ydotool: {exc}")


# ---------------------------------------------------------------------------
# clipboard: sempre disponibile, nessun privilegio
# ---------------------------------------------------------------------------
class ClipboardInserter:
    """Copia il testo negli appunti e lascia l'incollaggio all'utente."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def insert(self, text: str) -> InsertResult:
        if shutil.which("wl-copy") is None:
            return InsertResult(False, "clipboard", "wl-copy non installato")
        with self._lock:
            try:
                # wl-copy fork-a in background per servire le richieste future
                # degli appunti: se si cattura l'output con una pipe, il
                # processo padre aspetta la fine della pipe e scade il timeout.
                # Per questo stdout/stderr vanno devnullati.
                proc = subprocess.run(
                    ["wl-copy", "--type", "text/plain"],
                    input=text.encode("utf-8"),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                )
                if proc.returncode != 0:
                    return InsertResult(False, "clipboard", "wl-copy fallito")
                return InsertResult(
                    True, "clipboard", "Testo copiato: incolla con Ctrl+V"
                )
            except Exception as exc:
                return InsertResult(False, "clipboard", f"Errore clipboard: {exc}")


# ---------------------------------------------------------------------------
# facciata
# ---------------------------------------------------------------------------
class TextInserter:
    """Sceglie il metodo e applica i gate di sicurezza.

    I gate sono deliberati: la tastiera simulata e il clipboard sono globali,
    quindi senza un controllo esplicito della finestra attiva un segmento
    potrebbe finire in un'altra applicazione.
    """

    def __init__(self, method: InsertMethod = InsertMethod.AUTO) -> None:
        self.method = method
        self._lo = LibreOfficeInserter()
        self._atspi = AtspiInserter()
        self._ydo = YdotoolInserter()
        self._clip = ClipboardInserter()
        self._expected_window: str | None = None
        # l'inserimento e' disarmato finche' l'utente non lo abilita: e' una
        # difesa in profondita', cosi' nessun percorso di codice puo' scrivere
        # nel documento senza un'esplicita autorizzazione
        self._armed = False
        self._lock = threading.Lock()

    # -- gate --------------------------------------------------------------
    @property
    def armed(self) -> bool:
        with self._lock:
            return self._armed

    def arm(self, expected_window: str | None = None) -> None:
        """Autorizza l'inserimento e registra la finestra di riferimento."""
        with self._lock:
            self._armed = True
            self._expected_window = expected_window or active_window_title()

    def disarm(self) -> None:
        with self._lock:
            self._armed = False
            self._expected_window = None

    def focus_matches(self) -> bool:
        """True se la finestra attiva e' quella registrata.

        Se il titolo della finestra attiva **non si puo' leggere** la
        risposta e' ``False``, non ``True``: prima assumeva "stessa
        finestra" per non bloccare l'inserimento, e quel assume-permesso
        era esattamente il buco nel gate — misurato: con at-spi che non
        risponde (sessione senza bus, finestra che si sta chiudendo) il
        titolo torna ``""``, e il testo veniva scritto nella finestra
        sbagliata. Il gate della privacy deve chiudere, non aprire: un
        inserimento saltato e' recuperabile premendo di nuovo il tasto,
        una nota di lezione finita in un altro documento no.
        """
        with self._lock:
            expected = self._expected_window
        if not expected:
            return True
        current = active_window_title()
        if not current:
            return False
        return expected.strip() == current.strip()

    # -- inserimento -------------------------------------------------------
    def insert(self, text: str, force: bool = False) -> InsertResult:
        if not text:
            return InsertResult(True, self.method.value, "Testo vuoto")
        if not force:
            if not self.armed:
                return InsertResult(
                    False, self.method.value, "Inserimento non autorizzato: nessuna finestra registrata"
                )
            if not self.focus_matches():
                return InsertResult(
                    False, self.method.value, "Finestra attiva cambiata: inserimento saltato"
                )

        method = self.method
        if method == InsertMethod.NONE:
            return InsertResult(False, "none", "Inserimento disattivato")

        if method == InsertMethod.AUTO:
            caps = capabilities()
            if caps.get("libreoffice_uno") and self._looks_like_libreoffice():
                return self._lo.insert(text)
            if caps.get("atspi") and self._looks_like_gtk():
                return self._atspi.insert(text)
            if caps.get("clipboard") and self._can_reach_target():
                return self._clip.insert(text)
            return InsertResult(
                False,
                "auto",
                "Nessun metodo di inserimento disponibile per la finestra attiva",
            )

        if method == InsertMethod.LIBREOFFICE:
            return self._lo.insert(text)
        if method == InsertMethod.ATSPI:
            return self._atspi.insert(text)
        if method == InsertMethod.YDOTOOL:
            return self._ydo.insert(text)
        if method == InsertMethod.CLIPBOARD:
            return self._clip.insert(text)
        return InsertResult(False, "none", "Metodo sconosciuto")

    def _looks_like_libreoffice(self) -> bool:
        title = active_window_title().lower()
        return "libreoffice" in title or "writer" in title or "odt" in title

    def _looks_like_gtk(self) -> bool:
        title = active_window_title().lower()
        if not title:
            return False
        bad = ("obsidian", "electron", "code", "chromium", "chrome")
        return not any(b in title for b in bad)

    def _can_reach_target(self) -> bool:
        """Vero se sappiamo qual e' la finestra attiva.

        Il ripiego sugli appunti sovrascrive il clipboard di **qualunque**
        applicazione sia in primo piano, senza chiedere nulla: se non
        riusciamo nemmeno a leggere il titolo della finestra attiva, non
        sappiamo dove finira' il prossimo incolla dell'utente — misurato:
        con il titolo illeggibile il ramo AUTO cadeva sugli appunti e il
        testo sostituiva quello che l'utente aveva copiato altrove.
        Meglio non inserire e dirlo.
        """
        return bool(active_window_title())
