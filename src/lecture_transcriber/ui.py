"""Interfaccia grafica GTK4/libadwaita.

La finestra e' pensata per restare appiccicata accanto a un editor di
appunti: piccola, translucida, sempre in primo piano quando si puo'. Due
modalita' di lettura, distinte in modo inequivocabile:

``consultazione``
    sola lettura, chrome minimo, semitrasparente: si legge la lezione
    mentre si prende appunti altrove.

``presa di appunti``
    finestra completa, con controlli, copia, inserimento, esportazione e
    preferenze.

Restrizioni reali della piattaforma (GNOME 50 / Wayland), gia' verificate:

* ``Gtk.Window`` non ha ``set_keep_above``: il sempre in primo piano si
  ottiene solo tramite un'estensione GNOME Shell che espone un'interfaccia
  D-Bus. Se l'estensione non e' installata la preferenza resta
  disponibile ma l'applicazione continua a funzionare;
* il click-through non e' implementabile dal cliente: non esiste un
  interruttore nel pannello preferenze per una funzione che non si puo'
  attivare. Il limite e' documentato in docs/analisi-fattibilita.md;
* lo sfondo sfocato non esiste: si usa la sola opacita'.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import config as _config  # noqa: E402
from . import theme  # noqa: E402
from .app import ListenerState  # noqa: E402

#: Interfaccia D-Bus dell'estensione GNOME Shell che espone il
#: "sempre in primo piano". Se l'estensione non e' installata la chiamata
#: fallisce e l'applicazione prosegue senza.
SHELL_BUS_NAME = "org.gnome.Shell.Extensions.LectureTranscriber"
SHELL_OBJECT_PATH = "/org/gnome/Shell/Extensions/LectureTranscriber"
SHELL_INTERFACE = "org.gnome.Shell.Extensions.LectureTranscriber"
SHELL_METHOD = "SetAlwaysOnTop"

#: Soglia minima di opacita': sotto questa il testo diventa illeggibile.
MIN_OPACITY = 0.25

#: Ritardo dichiarato all'utente fra la fine di un segmento e la sua
#: comparsa: la decodifica Whisper riempie sempre una finestra di 30 s e
#: quindi costa ~3 s indipendentemente dalla lunghezza del segmento.
DECODE_LATENCY_HINT = "latenza ~3 s"

MODE_CONSULT = "consultazione"
MODE_NOTES = "presa di appunti"

#: Tolleranza, in pixel, entro cui il viewport si considera "al fondo".
#: Presta in prestito da DeLive (``isAtBottom``, 30 px): senza una
#: tolleranza, un arrotondamento di un pixel mostrerebbe la pillola di
#: ritorno mentre l'utente sta leggendo proprio l'ultima riga.
REJOIN_TOLERANCE_PX = 30


def _within_rejoin_tolerance(
    value: float, upper: float, page_size: float
) -> bool:
    """True se il viewport sta entro :data:`REJOIN_TOLERANCE_PX` dal fondo.

    Prende i tre numeri del ``Gtk.Adjustment`` invece dell'aggiustamento
    stesso per una ragione misurata: in un ambiente senza display lo
    ``ScrolledWindow`` non ha altezza, quindi ricalcola ``upper`` e
    ``page_size`` dal contenuto non appena il test scrive ``value`` —
    misurato, ``set_value(500)`` tornava 8.0 e la sintetica posizione
    scritta dal test veniva sovrascritta. Con i numeri isolati la
    tolleranza si collauda per quella che e', e il cablaggio col widget
    vero si collauda a parte (``_update_rejoin``).
    """
    return value >= upper - page_size - REJOIN_TOLERANCE_PX


# Il foglio di stile e le primitive di design (capsule, controllo
# segmentato, scala tipografica) vivono in theme.py: qui resta solo la
# costruzione dei widget. I colori nominati del tema servono ai tag di
# Gtk.TextBuffer, che non accettano nomi ma solo valori RGBA espliciti
# (vedi theme.resolve_named_color).


def _clipboard_copy(text: str) -> bool:
    """Copia il testo negli appunti. True se la copia e' stata accettata."""
    display = Gdk.Display.get_default()
    if display is None or not text:
        return False
    try:
        provider = Gdk.ContentProvider.new_for_value(text)
        display.get_clipboard().set_content(provider)
        return True
    except Exception:
        return False


class ShellBridge:
    """Ponte verso l'estensione GNOME Shell per il sempre in primo piano.

    Ogni chiamata e' protetta: se l'estensione non e' installata il
    metodo restituisce ``False`` e l'applicazione prosegue.
    """

    def __init__(
        self,
        bus_name: str = SHELL_BUS_NAME,
        object_path: str = SHELL_OBJECT_PATH,
        interface: str = SHELL_INTERFACE,
        method: str = SHELL_METHOD,
    ) -> None:
        self.bus_name = bus_name
        self.object_path = object_path
        self.interface = interface
        self.method = method

    def available(self) -> bool:
        """True se l'estensione risulta registrata sul bus di sessione."""
        try:
            connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            reply = connection.call_sync(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "NameHasOwner",
                GLib.Variant("(s)", (self.bus_name,)),
                GLib.VariantType("(b)"),
                Gio.DBusCallFlags.NONE,
                1500,
                None,
            )
            return bool(reply.unpack()[0])
        except Exception:
            return False

    def set_always_on_top(self, enabled: bool) -> bool:
        """Attiva o disattiva il sempre in primo piano via D-Bus."""
        try:
            connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            connection.call_sync(
                self.bus_name,
                self.object_path,
                self.interface,
                self.method,
                GLib.Variant("(b)", (bool(enabled),)),
                None,
                Gio.DBusCallFlags.NONE,
                2000,
                None,
            )
            return True
        except Exception:
            return False

    def get_always_on_top(self) -> bool | None:
        """Stato del sempre in primo piano, se l'estensione risponde.

        ``None`` se l'estensione non e' installata o non e' abilitata.
        """
        try:
            connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            reply = connection.call_sync(
                self.bus_name,
                self.object_path,
                self.interface,
                "GetAlwaysOnTop",
                None,
                GLib.VariantType("(b)"),
                Gio.DBusCallFlags.NONE,
                2000,
                None,
            )
            return bool(reply.unpack()[0])
        except Exception:
            return None


class TranscriptView:
    """La vista della trascrizione: cronologia scorrevole e ricercabile.

    Il testo provvisorio vive in un tag dedicato e, quando esiste un
    segmento finalizzato, continua l'ultima riga invece di comparire su
    una riga separata: quando il segmento reale arriva, il provvisorio
    viene rimosso e non restano le stesse parole su due righe (il
    "salto di riga" che si vedeva a ogni ridecodifica ~3 s).
    """

    @staticmethod
    def _theme_color(name: str, fallback: str) -> str:
        """Risolve un colore nominato del tema in un valore RGBA esplicito.

        I tag di ``Gtk.TextBuffer`` accettano solo colori concreti: un
        ``foreground="@accent_color"`` produce l'avviso "Don't know
        color". Il nome si risolve quindi chiedendo al motore CSS di GTK
        il valore corrente del tema; se il nome non e' conosciuto si
        ricade su un colore neutro esplicito.
        """
        return theme.resolve_named_color(name, fallback)

    def __init__(self, settings: _config.Settings) -> None:
        self.settings = settings
        # Gli offset sono l'unico riferimento sicuro a una posizione del
        # buffer: un ``Gtk.TextIter`` viene invalidato da qualunque
        # modifica del testo e andrebbe ricalcolato ogni volta.
        # Ogni riga e' (inizio_testo, fine_testo).
        self._rows: dict[int, tuple[int, int]] = {}
        self._partial_span: tuple[int, int] | None = None
        self._bookmarks: set[int] = set()

        self.buffer = Gtk.TextBuffer()
        # i tag si creano una volta sola: ``create_tag`` con lo stesso nome
        # ne crea uno nuovo e non riusabile, e applicarlo a un buffer
        # diverso da quello di origine genera un critical di GTK.
        #
        # I colori si prendono dal tema Adw corrente: i nomi tipo
        # "@accent_color" esistono solo in libadwaita 1.6 e non sono
        # riconosciuti dal CSS di GTK4, che segnalerebbe
        # "Don't know color '@accent_color'".
        accent = theme.accent_rgba(0.85)
        # "dim_label_color" non e' un nome conosciuto dal motore CSS di
        # GTK: "insensitive_fg_color" e' il grigio attenuato del tema e
        # serve esattamente per il testo provvisorio.
        dim = theme.resolve_named_color("insensitive_fg_color", "#9a9996")
        # Separatore fra segmenti: un filetto appena accennato. Nel testo
        # si realizza con un colore a bassissimo contrasto perche' i tag
        # non possono disegnare linee orizzontali.
        separator = theme.foreground_rgba(0.16)
        self.tag_timestamp = self.buffer.create_tag(
            "timestamp", foreground=accent
        )
        self.tag_partial = self.buffer.create_tag(
            "partial", style=2, foreground=dim
        )  # 2 = Pango italic
        self.tag_bookmark = self.buffer.create_tag("bookmark", weight=700)
        self.tag_separator = self.buffer.create_tag(
            "separator", foreground=separator
        )

        self.view = Gtk.TextView(
            buffer=self.buffer,
            editable=False,
            cursor_visible=False,
            wrap_mode=Gtk.WrapMode.WORD,
            top_margin=4,
            bottom_margin=4,
            left_margin=4,
            right_margin=4,
            hexpand=True,
            vexpand=True,
        )
        self.view.add_css_class("lt-transcript")

        self.scroller = Gtk.ScrolledWindow(
            hexpand=True,
            vexpand=True,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
        )
        self.scroller.set_child(self.view)
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self._pending_rows: list[tuple[int, float, float, str, bool]] = []

    # -- scrittura ---------------------------------------------------------
    def add_segment(self, index: int, start: float, text: str, bookmark: bool = False) -> None:
        """Aggiunge (o sostituisce) un segmento finalizzato.

        Ogni segmento e' una "riga di lettura" composta da tre parti:
        il tempo in una cifra tabellare piccola, il testo nel corpo di
        lettura piu' grande, e un filetto di separazione. E' la stessa
        struttura che Apple usa nelle liste di note e trascrizioni: il
        dato secondario (il tempo) sta in una colonna stretta e non
        compete con il testo.
        """
        self.clear_partial()
        self._rows.pop(index, None)

        end = self.buffer.get_end_iter()

        stamp = f"{int(start // 60):02d}:{int(start % 60):02d}"
        self.buffer.insert_with_tags(end, f"{stamp}  ", self.tag_timestamp)
        if bookmark:
            self.buffer.insert_with_tags(end, "★ ", self.tag_bookmark)
        text_start = self.buffer.get_end_iter().get_offset()
        self.buffer.insert(end, text)
        text_end = self.buffer.get_end_iter().get_offset()
        # Il separatore e' un trattino a basso contrasto, non una riga
        # vuota: occupa una riga ma non crea un buco nella lettura.
        self.buffer.insert_with_tags(end, "\n\n", self.tag_separator)

        self._rows[index] = (text_start, text_end)
        if bookmark:
            self._bookmarks.add(index)

    def set_partial(self, text: str) -> None:
        """Mostra (o toglie) l'ipotesi provvisoria.

        L'ipotesi e' scritta in coda all'ultimo segmento finalizzato,
        subito dopo il suo testo e prima del separatore, NON come riga
        nuova in fondo: quando il segmento finale arriva le stesse parole
        non ricompaiono una riga piu' sopra (misurato: un salto di riga
        duplicato a ogni ridecodifica del segmento aperto, ~3 s).
        """
        self.clear_partial()
        if not text:
            return
        # la riga piu' recente: il suo span termina appena prima del
        # separatore, quindi ``text_end`` e' l'offset dove va il prossimo
        # pezzo di testo (la coda visiva della riga).
        if self._rows:
            index = max(self._rows)
            start = self._rows[index][1]
            delimiter = " "
        else:
            start = self.buffer.get_end_iter().get_offset()
            delimiter = ""
        it = self.buffer.get_iter_at_offset(start)
        self.buffer.insert_with_tags(it, f"{delimiter}{text}", self.tag_partial)
        self.buffer.insert(it, "\n")
        # Lo span deve comprendere anche il terminatore di riga: se
        # restasse fuori, cancellare l'ipotesi lascerebbe un a capo
        # orfano nel buffer. L'ipotesi vive ORA dentro l'ultima riga:
        # cancellarla riporta il testo al punto esatto in cui era.
        finish = start + len(delimiter) + len(text) + 1
        self._partial_span = (start, finish)

    def clear_partial(self) -> None:
        """Rimuove la riga provvisoria, se presente."""
        if self._partial_span is None:
            return
        start, end = self._partial_span
        self._partial_span = None
        begin = self.buffer.get_iter_at_offset(start)
        stop = self.buffer.get_iter_at_offset(end)
        self.buffer.delete(begin, stop)

    def clear(self) -> None:
        self.buffer.set_text("")
        self._rows.clear()
        self._bookmarks.clear()
        self._partial_span = None

    # -- lettura -----------------------------------------------------------
    def full_text(self) -> str:
        return self.buffer.get_text(
            self.buffer.get_start_iter(), self.buffer.get_end_iter(), False
        )

    def text_for(self, index: int) -> str:
        span = self._rows.get(index)
        if span is None:
            return ""
        begin = self.buffer.get_iter_at_offset(span[0])
        stop = self.buffer.get_iter_at_offset(span[1])
        return self.buffer.get_text(begin, stop, False)

    def set_bookmark(self, index: int, bookmark: bool) -> None:
        if bookmark:
            self._bookmarks.add(index)
        else:
            self._bookmarks.discard(index)

    def bookmarks(self) -> list[int]:
        return sorted(self._bookmarks)

    def last_line(self) -> str:
        """L'ultimo testo trascritto, senza il timestamp.

        Il timestamp e' una comodita' di lettura nella cronologia, ma
        chi copia "l'ultimo segmento" vuole il testo, non ``"01:05  La
        varianza..."``. Toglierlo qui lo toglie per tutti gli usi.
        """
        # Con un'ipotesi provvisoria presente, la lettura si ferma prima
        # dello span del provvisorio: il testo da copiare e' quello
        # finalizzato, non la coda in corsivo (che cambia a ogni
        # ridecodifica). Non si tocca il buffer: l'ipotesi resta in vista.
        if self._partial_span is not None:
            begin = self.buffer.get_start_iter()
            stop = self.buffer.get_iter_at_offset(self._partial_span[0])
            text = self.buffer.get_text(begin, stop, False)
        else:
            text = self.full_text()
        text = text.strip()
        if not text:
            return ""
        for line in reversed(text.splitlines()):
            line = line.strip()
            if not line:
                continue
            # il formato scritto da add_segment e' "MM:SS  testo"
            head, sep, rest = line.partition("  ")
            if sep and head and head[0].isdigit() and ":" in head:
                return rest.strip()
            return line
        return ""

    def scroll_to_end(self) -> None:
        end = self.buffer.get_end_iter()
        self.view.scroll_to_iter(end, 0.0, False, 0.0, 0.0)

    def scroll_to_index(self, index: int) -> None:
        """Porta in vista il segmento ``index``, se presente."""
        span = self._rows.get(index)
        if span is None:
            return
        it = self.buffer.get_iter_at_offset(span[0])
        self.view.scroll_to_iter(it, 0.0, True, 0.0, 0.2)


class MainWindow(Adw.ApplicationWindow):
    """La finestra fluttuante dell'applicazione."""

    def __init__(self, app: Adw.Application, pipeline: TranscriptionPipeline) -> None:
        super().__init__(
            application=app,
            title="Trascrizione lezioni",
            default_width=pipeline.settings.width,
            default_height=pipeline.settings.height,
        )
        self.pipeline = pipeline
        self.settings = pipeline.settings
        self._shell = ShellBridge()
        self._mode = MODE_NOTES
        self._search_query = ""
        self._search_rows: list[int] = []
        self._search_view_active = False
        self._auto_scroll = self.settings.auto_scroll
        # Il "segui il fondo" non e' la preferenza di scorrimento ma uno
        # stato derivato dalla posizione reale del viewport: si segue fin
        # quando il bordo del testo e' dentro una tolleranza di 30 px dal
        # fondo. Eredita DeLive: se l'utente scorre via, si smette di
        # seguire e compare la pillola di ritorno.
        self._following = True
        # Traccia se un'ipotesi provvisoria e' in vista: serve alla
        # seconda capsula di stato ("trascrivendo") accanto al transcript.
        self._partial_on_screen = False
        self._closing = False

        self.set_css_classes(["lt-window"])
        self._install_css()
        self.apply_opacity()
        self._apply_font_size()

        self.view = TranscriptView(self.settings)

        self._build_ui()
        self._apply_mode()
        self._connect_pipeline()
        self._install_shortcuts()

    # -- costruzione -------------------------------------------------------
    def _install_css(self) -> None:
        """Installa il foglio di stile e registra l'esito.

        GTK scarta un CSS non valido in silenzio: senza questo controllo
        la finestra resterebbe senza stile e sembrerebbe un difetto del
        widget invece che del foglio di stile.
        """
        if not theme.install():
            logging.getLogger(__name__).warning(
                "foglio di stile dell'interfaccia non applicato"
            )

    def _build_ui(self) -> None:
        self.toast_overlay = Adw.ToastOverlay()
        self.set_content(self.toast_overlay)

        # La struttura e' a tre livelli sovrapposti, come nelle app Apple:
        #   * barra superiore: identita' della sessione e stato, fissa;
        #   * area di lettura: l'unica cosa che scorre;
        #   * barra di comando mobile: una capsula staccata dal contenuto,
        #     che galleggia sopra il transcript invece di occupare una
        #     riga propria.
        # `Adw.ToolbarView` serve solo come contenitore: le barre vere
        # sono widget normali dentro un `Gtk.Box`, per poterle posizionare
        # e animare liberamente.
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        root.add_css_class("lt-window-surface")
        self.toast_overlay.set_child(root)

        self._build_top_bar()
        self._build_reading_area()
        self._build_status_strip()
        self._build_command_bar()

        root.append(self._top_bar)
        root.append(self._reading_area)
        root.append(self._status_strip)
        root.append(self.search_bar)
        root.append(self._command_bar)

    # -- barra superiore ---------------------------------------------------
    def _build_top_bar(self) -> None:
        """Barra superiore: titolo, capsula di stato, sempre-in-primo-piano.

        In consultazione discreta questa barra si riduce al minimo
        indispensabile (stato del microfono e niente altro), perche' il
        requisito e' che la finestra sia leggibile e discretta.

        Qui sta anche il **tasto del sempre-in-primo-piano**, ed e' una
        scelta misurata, non estetica. Due conti:

        * **raggiungibilita'**: la barra superiore e' l'unica superficie di
          comando che *non* viene nascosta da nessuna modalita'. La riga
          dei comandi sparisce in consultazione discreta e in vista
          compatta, quindi un tasto messo la' sarebbe irraggiungibile
          proprio quando serve di piu' — rileggere una lezione con la
          finestra sopra l'editor di appunti. Verificato: ``_top_bar``
          resta visibile in tutti e tre gli stati.
        * **larghezza**: la riga comandi misura 402 px naturali e la
          finestra ne ha 420 (misurato): aggiungere un bottone da 38 px la
          porta a 444 px e la finestra si allarga — lo stesso difetto che
          aveva costretto la barra su due file. La barra superiore sta a
          299 px, con ~109 px di residuo: il tasto ci sta senza stringere
          niente.

        Il tasto e' un ``Gtk.ToggleButton`` perche' il suo stato *e'*
        l'informazione: acceso = la finestra sta sopra le altre. Lo stato
        mostrato viene da ``settings.always_on_top`` e non da
        ``GetAlwaysOnTop``: misurato, con nessuna finestra dell'app aperta
        l'estensione risponde ``false`` anche se la preferenza e' accesa,
        quindi quel metodo non e' una fonte attendibile per l'interfaccia.
        """
        self.top_bar = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
            margin_top=10,
            margin_bottom=6,
            margin_start=14,
            margin_end=14,
        )

        title = Gtk.Label(
            label="Trascrizione lezioni",
            xalign=0.0,
            ellipsize=3,
        )
        title.add_css_class("lt-type-title-3")
        self.title_label = title
        self.top_bar.append(title)

        # capsula di stato dell'ascolto: sostituisce l'icona del
        # microfono perche' un'icona da sola non dice se il microfono
        # sta registrando, e' in pausa o e' spento. La modalita' NON ha
        # una capsula sua: la dicono il controllo segmentato (sempre
        # visibile) e il suffisso del titolo, tre indicatori per la
        # stessa informazione erano rumore.
        self.state_capsule = theme.capsule("inattivo", "quiet")
        self.top_bar.append(self.state_capsule)

        spacer = Gtk.Box(hexpand=True)
        self.top_bar.append(spacer)

        # Salva lezione come PDF in Documents: nell'angolo in alto, sempre
        # visibile (anche in consultazione), perche' salvare il lavoro fatto
        # non deve mai essere nascosto dietro un menù.
        self.save_lesson_button = Gtk.Button(
            icon_name="document-save-symbolic",
            tooltip_text=(
                "Salva la lezione come PDF nella cartella Documenti (Documents)"
            ),
        )
        self.save_lesson_button.add_css_class("lt-primary")
        self.save_lesson_button.connect("clicked", self._on_save_lesson)
        self.top_bar.append(self.save_lesson_button)

        # -- sempre in primo piano: un tasto, non una preferenza --
        # ``view-pin-symbolic`` esiste nel tema icone (verificato), a
        # differenza di ``document-export-symbolic`` usato piu' sotto, che
        # nel tema non c'e'.
        self.pin_button = Gtk.ToggleButton(
            icon_name="view-pin-symbolic",
            tooltip_text=(
                "Tieni la finestra sopra le altre "
                "(richiede l'estensione GNOME Shell «Lecture Transcriber»)"
            ),
        )
        self.pin_button.add_css_class("lt-icon-button")
        self.pin_button.set_active(self.settings.always_on_top)
        self.pin_button.connect("toggled", self._on_pin_toggled)
        self.top_bar.append(self.pin_button)

        # tempo trascorso della sessione, in cifre tabellari: compare
        # solo mentre si ascolta
        self.elapsed_label = Gtk.Label(label="", xalign=1.0)
        self.elapsed_label.add_css_class("lt-type-caption")
        self.elapsed_label.add_css_class("lt-numeric")
        self.elapsed_label.add_css_class("lt-dim")
        self.elapsed_label.set_visible(False)
        self.top_bar.append(self.elapsed_label)

        self._top_bar = self.top_bar

    # -- area di lettura ---------------------------------------------------
    def _build_reading_area(self) -> None:
        """L'area che scorre: metadati, segnalibri, vista compatta, transcript."""
        stack = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        stack.set_vexpand(True)

        # riga dei metadati della sessione: campi nudi dentro una
        # superficie, non tre entry staccate
        self._build_meta_fields(stack)

        # pannello dei segnalibri: una scheda sovrapposta al transcript,
        # rivelata dal pulsante a stella. Non e' una barra in cima alla
        # finestra perche' leggere un segnalibro e' un'azione di
        # consultazione, e il contesto resta il transcript.
        self.bookmark_panel = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=2,
            margin_top=4,
            margin_bottom=4,
            margin_start=14,
            margin_end=14,
            visible=False,
        )
        self.bookmark_panel.add_css_class("lt-surface-raised")
        self.bookmark_title = Gtk.Label(
            label="Segnalibri", xalign=0.0, margin_top=6, margin_start=10
        )
        self.bookmark_title.add_css_class("lt-type-caption")
        self.bookmark_title.add_css_class("lt-dim")
        self.bookmark_panel.append(self.bookmark_title)
        self.bookmark_list = Gtk.ListBox(selection_mode=0)
        self.bookmark_list.add_css_class("lt-bookmark-list")
        scroller = Gtk.ScrolledWindow(
            max_content_height=150,
            propagate_natural_height=False,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
        )
        scroller.set_child(self.bookmark_list)
        self.bookmark_panel.append(scroller)
        stack.append(self.bookmark_panel)

        # riga compatta: solo l'ultima riga, esposta al posto della
        # cronologia quando la vista e' ridotta
        self.compact_label = Gtk.Label(
            label="nessuna trascrizione",
            xalign=0.0,
            ellipsize=3,
            max_width_chars=60,
            margin_top=6,
            margin_bottom=6,
            margin_start=14,
            margin_end=14,
            wrap=False,
        )
        self.compact_label.add_css_class("lt-type-body")
        self.compact_label.set_visible(False)
        stack.append(self.compact_label)

        self.view.scroller.set_vexpand(True)

        # L'overlay tiene la pillola di ritorno e la seconda capsula di
        # stato sopra il transcript, senza rubare spazio alla colonna.
        self.scroll_overlay = Gtk.Overlay()
        self.scroll_overlay.set_child(self.view.scroller)
        self._build_transcript_overlay_children()
        stack.append(self.scroll_overlay)

        self._reading_area = stack

    def _build_transcript_overlay_children(self) -> None:
        """Figli dell'overlay: pillola di ritorno e capsula di stato.

        La pillola di ritorno e' un ``Gtk.Revealer`` che compare solo
        quando si smette di seguire il fondo (vedi ``_update_rejoin``): la
        tolleranza si valuta in idle, dopo che il contenuto e' stato
        misurato, altrimenti la capsula sfarfalla a ogni segmento.
        La seconda capsula di stato riflette la stessa mappa
        ``_STATE_CAPSULE`` della barra superiore piu' lo stato
        "trascrivendo", quando un'ipotesi provvisoria e' in vista.
        """
        self.rejoin_revealer = Gtk.Revealer(
            transition_type=Gtk.RevealerTransitionType.CROSSFADE,
            transition_duration=150,
        )
        self.rejoin_button = Gtk.Button(label="Torna all'ultimo testo")
        self.rejoin_button.add_css_class("lt-rejoin")
        self.rejoin_button.connect("clicked", self._on_rejoin)
        self.rejoin_revealer.set_child(self.rejoin_button)
        # In GTK4 i figli dell'overlay si allineano con le loro proprieta'
        # halign/valign (non esiste set_overlay_halign).
        self.rejoin_revealer.set_halign(Gtk.Align.END)
        self.rejoin_revealer.set_valign(Gtk.Align.END)
        self.scroll_overlay.add_overlay(self.rejoin_revealer)
        self.rejoin_revealer.set_margin_end(14)
        self.rejoin_revealer.set_margin_bottom(14)

        self.transcript_state_capsule = theme.capsule("pronto", "quiet")
        self.transcript_state_capsule.set_halign(Gtk.Align.END)
        self.transcript_state_capsule.set_valign(Gtk.Align.START)
        self.scroll_overlay.add_overlay(self.transcript_state_capsule)
        self.transcript_state_capsule.set_margin_start(8)
        self.transcript_state_capsule.set_margin_top(8)

        # Lo stato del viewport (dentro la tolleranza?) si valuta dopo il
        # disegno: al widget serve la posizione del vadjustment, che e'
        # aggiornata soltanto quando GTK ha misurato il nuovo contenuto.
        adj = self.view.scroller.get_vadjustment()
        adj.connect("value-changed", lambda *_: self._schedule_rejoin_update())

    def _build_meta_fields(self, parent: Gtk.Box) -> None:
        """Campi dei metadati, raggruppati in una capsula unica."""
        self.meta_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=6,
            margin_top=6,
            margin_bottom=4,
            margin_start=14,
            margin_end=14,
        )
        self.meta_box.add_css_class("lt-surface")

        self.course_entry = Gtk.Entry(
            placeholder_text="Corso", hexpand=True, has_frame=False
        )
        self.course_entry.set_text(self.pipeline.session.course)
        self.course_entry.connect("changed", self._on_meta_changed)
        self.meta_box.append(self.course_entry)

        sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        sep.add_css_class("lt-dim")
        self.meta_box.append(sep)

        self.professor_entry = Gtk.Entry(
            placeholder_text="Docente", hexpand=True, has_frame=False
        )
        self.professor_entry.set_text(self.pipeline.session.professor)
        self.professor_entry.connect("changed", self._on_meta_changed)
        self.meta_box.append(self.professor_entry)

        sep2 = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        sep2.add_css_class("lt-dim")
        self.meta_box.append(sep2)

        self.date_entry = Gtk.Entry(
            placeholder_text="Data", width_chars=10, has_frame=False
        )
        self.date_entry.set_text(self.pipeline.session.date)
        self.date_entry.connect("changed", self._on_meta_changed)
        self.meta_box.append(self.date_entry)

        parent.append(self.meta_box)

    # -- striscia di stato -------------------------------------------------
    def _build_status_strip(self) -> None:
        """Striscia fra transcript e barra di comando.

        Contiene il livello del microfono, lo stato della pipeline e
        l'esito dell'inserimento. E' una striscia e non una barra in
        fondo alla finestra perche' in fondo finirebbe sotto la barra di
        comando mobile: qui sta subito sopra il testo che descrive.
        """
        strip = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
            margin_top=2,
            margin_bottom=2,
            margin_start=14,
            margin_end=14,
        )

        # misuratore di livello: una capsula sottile, non una barra
        self.level_bar = Gtk.LevelBar(
            min_value=0.0,
            max_value=0.3,
            width_request=70,
            valign=Gtk.Align.CENTER,
        )
        self.level_bar.add_css_class("lt-level")
        strip.append(self.level_bar)

        self.status_label = Gtk.Label(
            label="pronto",
            xalign=0.0,
            hexpand=True,
            ellipsize=3,  # Pango.EllipsizeMode.END
        )
        self.status_label.add_css_class("lt-type-footnote")
        self.status_label.add_css_class("lt-dim")
        strip.append(self.status_label)

        self.insert_status = Gtk.Label(
            label="",
            xalign=1.0,
            ellipsize=3,
        )
        self.insert_status.add_css_class("lt-type-footnote")
        self.insert_status.add_css_class("lt-dim")
        strip.append(self.insert_status)

        self._status_strip = strip

    # -- barra di comando --------------------------------------------------
    def _build_command_bar(self) -> None:
        """La barra mobile: una capsula con i comandi, sopra il testo.

        Sta in fondo ma **non** e' una barra del contenitore: e' un
        widget con margine, arrotondato, che sembra appoggiato sul
        transcript. Cosi' in consultazione discreta i comandi si
        nascondono con una riga e la lettura guadagna lo spazio.

        Attenzione a *cosa* si nasconde. Il controllo segmentato resta
        sempre visibile, anche in consultazione: e' l'unico modo per
        uscire da quella modalita' senza sapere a memoria la scorciatoia
        ``<Ctrl>m``. Con la barra intera nascosta, chi entrava in
        consultazione discreta restava bloccato dentro — misurato: zero
        controlli raggiungibili, neppure le preferenze, perche' il
        pulsante che le apre sta in questa barra. La barra si divide
        quindi in due meta': la modalita' (sempre) e i comandi (solo in
        presa di appunti).
        """
        outer = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            margin_top=4,
            margin_bottom=10,
            margin_start=8,
            margin_end=8,
        )

        # La capsula e' **verticale**, in due file: sopra il commutatore
        # di modalita', sotto i comandi. Prima era un'unica riga che
        # misurava 590 px di minimo — piu' della finestra (420 px), quindi
        # la finestra si allargava per forza (misurato). Su due file il
        # massimo e' la riga dei comandi (~400 px) e la finestra resta
        # stretta, com'era nelle intenzioni di chi prende appunti accanto
        # a un editor.
        bar = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=4,
            halign=Gtk.Align.CENTER,
        )
        bar.add_css_class("lt-floating-bar")
        bar.add_css_class("lt-floating-bar-vertical")
        self.command_bar = bar

        # -- controllo segmentato della modalita' --
        self.mode_segmented = theme.SegmentedControl(
            [
                (MODE_CONSULT, "Lettura"),
                (MODE_NOTES, "Appunti"),
            ],
            MODE_NOTES,
        )
        self.mode_segmented.connect_changed(self._on_mode_changed)
        self.mode_control = self.mode_segmented
        bar.append(self.mode_segmented)

        # I comandi vivono in un contenitore proprio, riga 2 della capsula:
        # in consultazione si nasconde questo, non la barra, cosi' il
        # commutatore resta.
        commands = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=4
        )
        self.command_box = commands
        bar.append(commands)

        sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        sep.add_css_class("lt-dim")
        self.mode_sep = sep
        commands.append(sep)

        # -- pulsanti di ascolto --
        # Un solo pulsante per avvio e pausa. Avevamo tre bottoni (avvia,
        # pausa, stop) e in pausa **non si poteva riprendere**: il tasto
        # pausa era abilitato solo in ascolto, quindi l'unica via d'uscita
        # era terminare la lezione e ricominciare — misurato, e l'utente
        # lo ha trovato da solo. Il codice di resume esisteva gia'
        # (``_on_pause_clicked`` gestiva PAUSED) ma era irraggiungibile.
        # Ora l'icona dice lo stato e il click fa l'opposto: play quando
        # si puo' avviare o riprendere, pausa quando si sta ascoltando.
        self.listen_button = theme.icon_button(
            "media-playback-start-symbolic",
            "Avvia l'ascolto",
            self._on_listen_clicked,
        )
        commands.append(self.listen_button)

        self.stop_button = theme.icon_button(
            "media-playback-stop-symbolic",
            "Termina la lezione",
            self._on_stop_clicked,
        )
        commands.append(self.stop_button)

        # -- inserimento nel documento --
        # Il pulsante resta distinto perche' e' l'unico comando che
        # modifica qualcosa fuori da questa finestra: acceso, deve essere
        # riconoscibile a colpo d'occhio.
        self.insert_button = Gtk.ToggleButton(
            icon_name="edit-paste-symbolic",
            tooltip_text="Inserisci la trascrizione nel documento attivo",
        )
        self.insert_button.add_css_class("lt-icon-button")
        self.insert_button.connect("toggled", self._on_insert_toggled)
        commands.append(self.insert_button)

        sep2 = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        sep2.add_css_class("lt-dim")
        commands.append(sep2)

        # -- segnalibro: toggle, perche' apre e chiude l'elenco --
        self.bookmark_button = Gtk.ToggleButton(
            icon_name="non-starred-symbolic",
            tooltip_text="Mostra i segnalibri",
        )
        self.bookmark_button.add_css_class("lt-icon-button")
        self.bookmark_button.connect(
            "toggled", self._on_bookmark_filter_toggled
        )
        commands.append(self.bookmark_button)

        self.search_button = theme.icon_button(
            "system-search-symbolic",
            "Cerca nella trascrizione",
            self._toggle_search,
        )
        commands.append(self.search_button)

        self.jump_button = theme.icon_button(
            "go-down-symbolic",
            "Vai all'ultimo testo",
            self.jump_to_latest,
        )
        commands.append(self.jump_button)

        # -- copia, esporta, preferenze --
        # Nessun separatore qui: questi comandi completano la "barra
        # strumenti" e un terzo filetto in una barra gia' piena di icone
        # aggiungeva linee, non chiarezza.
        self.copy_button = Gtk.MenuButton(
            icon_name="edit-copy-symbolic",
            tooltip_text="Copia negli appunti",
        )
        self.copy_button.add_css_class("lt-icon-button")
        self._build_copy_menu()
        commands.append(self.copy_button)

        # ``document-export-symbolic`` NON esiste nel tema icone di Fedora
        # 44 (verificato con Gtk.IconTheme.has_icon: risponde False), quindi
        # il pulsante restava disegnato con l'icona "image-missing" di
        # ripiego — un quadrato vuoto al posto del punto esclamativo
        # dell'export. ``document-send-symbolic`` c'e' e ha lo stesso
        # significato ("manda fuori").
        self.export_button = Gtk.MenuButton(
            icon_name="document-send-symbolic",
            tooltip_text="Esporta la trascrizione",
        )
        self.export_button.add_css_class("lt-icon-button")
        self._build_export_menu()
        commands.append(self.export_button)

        self.settings_button = Gtk.MenuButton(
            icon_name="preferences-system-symbolic",
            tooltip_text="Preferenze",
        )
        self.settings_button.add_css_class("lt-icon-button")
        self._build_settings_popover()
        commands.append(self.settings_button)

        outer.append(bar)
        self._command_bar = outer

        # campo di ricerca: una riga propria, rivelata dal pulsante.
        # Tenerla sempre in mezzo alla barra — come faceva la versione
        # precedente — rubava spazio ai comandi e costringeva la barra a
        # una larghezza fissa.
        self.search_bar = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=6,
            margin_top=4,
            margin_start=14,
            margin_end=14,
            visible=False,
        )
        self.search_entry = Gtk.SearchEntry(
            placeholder_text="Cerca nella trascrizione...",
            hexpand=True,
        )
        self.search_entry.set_search_delay(200)
        self.search_entry.connect("search-changed", self._on_search_changed)
        self.search_bar.append(self.search_entry)
        self.search_close = theme.icon_button(
            "window-close-symbolic", "Chiudi la ricerca", self._toggle_search
        )
        self.search_bar.append(self.search_close)

    def _toggle_search(self, _button=None) -> None:
        """Mostra o nasconde la riga di ricerca."""
        visible = not self.search_bar.get_visible()
        self.search_bar.set_visible(visible)
        if visible:
            self.search_entry.grab_focus()
        else:
            self.search_entry.set_text("")
            self._on_search_changed(self.search_entry)

    def _build_copy_menu(self) -> None:
        menu = Gio.Menu()
        for label, target in (
            ("Testo selezionato", "selection"),
            ("Ultimo segmento", "last"),
            ("Trascrizione completa", "all"),
        ):
            item = Gio.MenuItem.new(label, None)
            item.set_action_and_target_value(
                "app.copy", GLib.Variant("s", target)
            )
            menu.append_item(item)
        self.copy_button.set_menu_model(menu)

    def _build_export_menu(self) -> None:
        menu = Gio.Menu()
        for label, fmt in (
            ("Testo semplice (.txt)", "txt"),
            ("Markdown (.md)", "md"),
            ("Markdown con timestamp (.md)", "md_ts"),
            ("Sottotitoli (.srt)", "srt"),
        ):
            item = Gio.MenuItem.new(label, None)
            item.set_action_and_target_value(
                "app.export", GLib.Variant("s", fmt)
            )
            menu.append_item(item)
        self.export_button.set_menu_model(menu)
        self._export_formats = ("txt", "md", "md_ts", "srt")

    def _build_settings_popover(self) -> None:
        # Il pannello e' ancorato a un pulsante dentro la barra mobile, in
        # fondo a una finestra alta 520 px: GTK misura un `Gtk.Popover` sul
        # contenuto e, senza un vincolo, lo restringe finche' le etichette
        # ci stanno *ellisizzate*. Misurato: 183 px di larghezza, con
        # "Modello di riconoscimento" (191 px naturali) tagliata a 95 px e i
        # valori dei menu a tendina -- "large-v3-turbo" -- a 14 px. La
        # larghezza va quindi dichiarata, e va tenuta sotto i 400 px per non
        # superare la finestra su schermi piccoli.
        popover = Gtk.Popover()
        popover.set_size_request(340, -1)
        page = Adw.PreferencesPage()
        popover.set_child(page)

        # -- modello --
        group = Adw.PreferencesGroup(title="Modello di riconoscimento")
        models = ["large-v3-turbo", "large-v3", "medium", "small", "base"]
        self.model_row = Adw.ComboRow(title="Modello")
        self.model_row.set_model(Gtk.StringList.new(models))
        try:
            self.model_row.set_selected(models.index(self.settings.model_name))
        except ValueError:
            self.model_row.set_selected(0)
        self.model_row.connect("notify::selected", self._on_model_changed)
        group.add(self.model_row)
        page.add(group)

        # -- audio --
        group = Adw.PreferencesGroup(title="Acquisizione audio")
        self.device_row = Adw.ComboRow(title="Microfono")
        self._device_indices: list[int | None] = []
        self._refresh_devices()
        self.device_row.connect("notify::selected", self._on_device_changed)
        group.add(self.device_row)
        page.add(group)

        # -- aspetto --
        group = Adw.PreferencesGroup(title="Aspetto")
        self.opacity_row = Adw.SpinRow(
            title="Opacità",
            subtitle="Trasparenza della finestra",
            adjustment=Gtk.Adjustment(
                value=self.settings.opacity,
                lower=MIN_OPACITY,
                upper=1.0,
                step_increment=0.05,
                page_increment=0.1,
            ),
        )
        self.opacity_row.set_digits(2)
        self.opacity_row.connect("notify::value", self._on_opacity_changed)
        group.add(self.opacity_row)

        self.font_row = Adw.SpinRow(
            title="Dimensione del testo",
            adjustment=Gtk.Adjustment(
                value=self.settings.font_size,
                lower=8,
                upper=32,
                step_increment=1,
                page_increment=2,
            ),
        )
        self.font_row.connect("notify::value", self._on_font_changed)
        group.add(self.font_row)

        self.auto_scroll_row = Adw.SwitchRow(title="Scorrimento automatico")
        self.auto_scroll_row.set_active(self.settings.auto_scroll)
        self.auto_scroll_row.connect("notify::active", self._on_auto_scroll_changed)
        group.add(self.auto_scroll_row)

        self.compact_row = Adw.SwitchRow(
            title="Vista compatta",
            subtitle="Mostra solo stato e'ultima riga",
        )
        self.compact_row.set_active(self.settings.compact)
        self.compact_row.connect("notify::active", self._on_compact_changed)
        group.add(self.compact_row)

        self.always_on_top_row = Adw.SwitchRow(
            title="Sempre in primo piano",
            subtitle="Richiede l'estensione GNOME Shell",
        )
        self.always_on_top_row.set_active(self.settings.always_on_top)
        self.always_on_top_row.connect("notify::active", self._on_always_on_top_changed)
        group.add(self.always_on_top_row)
        page.add(group)

        # -- inserimento --
        group = Adw.PreferencesGroup(title="Inserimento nel documento")
        self.insert_paragraphs_row = Adw.SwitchRow(
            title="Paragrafi separati",
            subtitle="Off: testo continuo",
        )
        self.insert_paragraphs_row.set_active(self.settings.insert_paragraphs)
        self.insert_paragraphs_row.connect(
            "notify::active", self._on_insert_paragraphs_changed
        )
        group.add(self.insert_paragraphs_row)

        self.insert_method_row = Adw.ComboRow(title="Metodo di inserimento")
        methods = ["auto", "libreoffice", "atspi", "clipboard", "ydotool", "none"]
        self.insert_method_row.set_model(Gtk.StringList.new(methods))
        try:
            self.insert_method_row.set_selected(methods.index(self.settings.insert_target))
        except ValueError:
            self.insert_method_row.set_selected(0)
        self.insert_method_row.connect("notify::selected", self._on_insert_method_changed)
        group.add(self.insert_method_row)
        page.add(group)

        # -- privacy --
        group = Adw.PreferencesGroup(
            title="Privacy",
            description="La registrazione audio è disattivata per impostazione "
            "predefinita. Se la attivi, l'audio viene scritto su disco in "
            "chiaro (WAV nella cartella dei dati): consumo spazio e impatti "
            "sulla privacy.",
        )
        self.record_audio_row = Adw.SwitchRow(
            title="Registra l'audio",
            subtitle="Salva la lezione in WAV nella cartella dei dati",
        )
        self.record_audio_row.set_active(self.settings.record_audio)
        self.record_audio_row.connect("notify::active", self._on_record_audio_changed)
        group.add(self.record_audio_row)
        page.add(group)

        self.settings_button.set_popover(popover)

    def _refresh_devices(self) -> None:
        from .audio import list_input_devices

        labels = ["Predefinito di sistema"]
        indices: list[int | None] = [None]
        try:
            for dev in list_input_devices():
                suffix = " (predefinito)" if dev.is_default else ""
                labels.append(f"{dev.name}{suffix}")
                indices.append(dev.index)
        except Exception:
            pass
        self._device_indices = indices
        self.device_row.set_model(Gtk.StringList.new(labels))
        current = self.settings.input_device
        if current in indices:
            self.device_row.set_selected(indices.index(current))
        else:
            self.device_row.set_selected(0)

    # -- pipeline ----------------------------------------------------------
    def _connect_pipeline(self) -> None:
        cb = self.pipeline.cb
        cb.on_segment = lambda seg: GLib.idle_add(
            self._on_segment, seg.index, seg.start, seg.text
        )
        cb.on_partial = lambda text: GLib.idle_add(self._on_partial, text)
        cb.on_level = lambda rms, peak: GLib.idle_add(self._on_level, rms, peak)
        cb.on_state = lambda st, msg: GLib.idle_add(self._on_state, st, msg)
        cb.on_insert_result = lambda res: GLib.idle_add(self._on_insert_result, res)
        cb.on_error = lambda msg: GLib.idle_add(self._on_error, msg)

    # -- callback dalla pipeline -------------------------------------------
    def _on_segment(self, index: int, start: float, text: str) -> bool:
        self.view.add_segment(index, start, text)
        # un segmento finalizzato sostituisce l'ipotesi provvisoria
        self._partial_on_screen = False
        if self._following:
            self.view.scroll_to_end()
        self._update_transcript_capsule()
        self._schedule_rejoin_update()
        self._refresh_compact_label()
        return False

    def _on_partial(self, text: str) -> bool:
        self.view.set_partial(text)
        self._partial_on_screen = bool(text)
        if self._following:
            self.view.scroll_to_end()
        self._update_transcript_capsule()
        self._schedule_rejoin_update()
        self._refresh_compact_label()
        return False

    def _on_rejoin(self, _button=None) -> None:
        """Torna a seguire il fondo: il click sulla pillola.
        """
        self._following = True
        self.view.scroll_to_end()
        self._schedule_rejoin_update()
        return False

    def _schedule_rejoin_update(self) -> bool:
        """Valuta la tolleranza di ritorno in idle.

        Il valore del ``vadjustment`` riflette il *nuovo* contenuto solo
        dopo che GTK lo ha misurato: chiamare ``_update_rejoin`` qui
        dentro, a testo appena scritto, mostrerebbe la pillola a ogni
        segmento (il viewport non e' ancora stato spinto al fondo). Si
        rimanda di un giro d'eventi, quando anche l'ultima
        ``scroll_to_end`` e' stata computata.
        """
        GLib.idle_add(self._update_rejoin)
        return False

    def _update_rejoin(self) -> bool:
        """Mostra/nascondi la pillola di ritorno in base al viewport.

        Si segue il fondo finche' il valore del ``vadjustment`` sta entro
        30 px dal massimo; oltre quella tolleranza la pillola compare. La
        valutazione va fatta su upper e page_size del vadjustment, non su
        un confronto col testo, perche' e' la posizione *realmente*
        scorsa che conta.

        Il confronto vero e' in :func:`_within_rejoin_tolerance`: sta
        fuori perche' e' pura aritmetica su tre numeri, e va collaudata
        come tale (vedi il test della tolleranza).
        """
        adj = self.view.scroller.get_vadjustment()
        if adj is None:
            return False
        at_bottom = _within_rejoin_tolerance(
            adj.get_value(), adj.get_upper(), adj.get_page_size()
        )
        if at_bottom:
            self._following = True
        else:
            self._following = False
        self.rejoin_revealer.set_reveal_child(not at_bottom)
        return False

    #: Testo alternativo della seconda capsula quando si sta trascrivendo.
    _TRANSCRIBING = ("trascrivendo", "warning")

    def _update_transcript_capsule(self) -> None:
        """Aggiorna la seconda capsula accanto al transcript.

        Segue la stessa mappa ``_STATE_CAPSULE`` della barra superiore,
        ma mentre un'ipotesi provvisoria e' in vista e la pipeline e'
        in ascolto mostra lo stato dedicato "trascrivendo" (ambra).
        """
        listening = self.pipeline.state == ListenerState.LISTENING
        if self._partial_on_screen and listening:
            text, style = self._TRANSCRIBING
        else:
            state = self.pipeline.state
            text, style = self._STATE_CAPSULE.get(state, ("pronto", "quiet"))
        cap = self.transcript_state_capsule
        cap.set_text(text)
        for cls in (
            "lt-capsule-quiet",
            "lt-capsule-accent",
            "lt-capsule-success",
            "lt-capsule-warning",
            "lt-capsule-error",
        ):
            cap.remove_css_class(cls)
        cap.add_css_class(f"lt-capsule-{style}")

    def _on_level(self, rms: float, peak: float) -> bool:
        # un livello non finito (campione corrotio a monte) farebbe
        # congelare la LevelBar: min(nan, 0.3) e' nan, e set_value(nan)
        # viene scartato in silenzio da GLib
        if not math.isfinite(rms):
            return False
        self.level_bar.set_value(min(rms, 0.3))
        return False

    #: Testo e stile della capsula di stato per ogni stato della pipeline.
    _STATE_CAPSULE = {
        ListenerState.IDLE: ("pronto", "quiet"),
        ListenerState.LOADING: ("caricamento", "warning"),
        ListenerState.LISTENING: ("microfono attivo", "success"),
        ListenerState.PAUSED: ("in pausa", "warning"),
        ListenerState.STOPPED: ("sessione conclusa", "quiet"),
        ListenerState.ERROR: ("errore", "error"),
    }

    def _on_state(self, state: ListenerState, message: str) -> bool:
        listening = state == ListenerState.LISTENING
        paused = state == ListenerState.PAUSED
        # Il tasto unico segue lo stato: in ascolto mostra la pausa (il
        # click sospende), in pausa mostra play (il click riprende). Le
        # icone sono ``media-playback-*`` e non ``*-symbolic`` perche'
        # ``Gtk.Button(icon_name=...)`` aggiunge il suffisso da solo:
        # passargli ``media-playback-start-symbolic`` produiva il nome
        # ``media-playback-start-symbolic-symbolic``, che il tema non
        # risolve -- misurato, il pulsante restava senza icona.
        if listening:
            self.listen_button.set_icon_name("media-playback-pause")
            self.listen_button.set_tooltip_text("Sospendi l'ascolto")
            # Abilitato anche in LOADING: la pressione resta in attesa e
            # viene onorata da ``_on_model_ready`` (vedi
            # ``_start_when_ready``), quindi il bottone non deve fingere
            # di essere morto mentre il modello carica.
            self.listen_button.set_sensitive(True)
        elif paused:
            self.listen_button.set_icon_name("media-playback-start")
            self.listen_button.set_tooltip_text("Riprendi l'ascolto")
            self.listen_button.set_sensitive(True)
        else:
            self.listen_button.set_icon_name("media-playback-start")
            self.listen_button.set_tooltip_text("Avvia l'ascolto")
            self.listen_button.set_sensitive(
                state in (ListenerState.IDLE, ListenerState.STOPPED)
            )
        self.stop_button.set_sensitive(
            state
            in (ListenerState.LISTENING, ListenerState.PAUSED, ListenerState.LOADING)
        )

        # La capsula sostituisce l'icona del microfono: dice a parole cosa
        # sta succedendo, ed e' l'unica parte della barra superiore che
        # resta visibile in consultazione discreta.
        text, style = self._STATE_CAPSULE.get(state, ("pronto", "quiet"))
        # Il messaggio mostra l'errore, ma anche l'audio perso a fine
        # sessione: senza, la trascrizione perde parole e niente lo dice.
        # La capsula e' l'unico elemento che resta visibile in
        # consultazione discreta, quindi e' il posto giusto.
        if message and state in (ListenerState.ERROR, ListenerState.STOPPED):
            text = message
            if state == ListenerState.STOPPED:
                style = "warning"
        self.state_capsule.set_text(text)
        self.state_capsule.remove_css_class("lt-capsule-quiet")
        self.state_capsule.remove_css_class("lt-capsule-accent")
        self.state_capsule.remove_css_class("lt-capsule-success")
        self.state_capsule.remove_css_class("lt-capsule-warning")
        self.state_capsule.remove_css_class("lt-capsule-error")
        self.state_capsule.add_css_class(f"lt-capsule-{style}")
        if state == ListenerState.LISTENING:
            self.state_capsule.set_tooltip_text(
                f"Il microfono sta registrando \u00b7 {DECODE_LATENCY_HINT}"
            )
        else:
            self.state_capsule.set_tooltip_text(message or text)

        # Il tempo trascorso compare solo mentre si ascolto: una cifra
        # ferma a zero sarebbe rumore. ``set_visible`` va chiamato in
        # entrambi i rami: nascondere senza mai rimostrare lasciava il
        # contatore invisibile per tutta la sessione (misurato: dopo
        # LISTENING l'etichetta restava hidden).
        self.elapsed_label.set_visible(listening)
        if listening:
            self._start_elapsed_timer()
        else:
            self._stop_elapsed_timer()
        self._update_transcript_capsule()
        self.status_label.set_text(
            {
                ListenerState.LISTENING: f"in ascolto \u00b7 {DECODE_LATENCY_HINT}",
                ListenerState.PAUSED: "in pausa",
                ListenerState.LOADING: "caricamento modello...",
                ListenerState.ERROR: message or "errore",
                ListenerState.STOPPED: "sessione conclusa",
            }.get(state, "pronto")
        )
        return False

    # -- tempo di sessione -------------------------------------------------
    def _start_elapsed_timer(self) -> None:
        """Avvia il contatore del tempo di ascolto (un tick al secondo)."""
        self._stop_elapsed_timer()
        self._elapsed_at = GLib.get_monotonic_time()
        self._elapsed_timer = GLib.timeout_add_seconds(1, self._tick_elapsed)
        self._tick_elapsed()

    def _stop_elapsed_timer(self) -> None:
        timer = getattr(self, "_elapsed_timer", None)
        self._elapsed_timer = None
        if timer is not None:
            GLib.source_remove(timer)
        self._elapsed_at = None

    def _tick_elapsed(self) -> bool:
        started = getattr(self, "_elapsed_at", None)
        if started is None:
            self._elapsed_at = started = GLib.get_monotonic_time()
        seconds = int((GLib.get_monotonic_time() - started) / 1_000_000)
        minutes, secs = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            self.elapsed_label.set_text(f"{hours}:{minutes:02d}:{secs:02d}")
        else:
            self.elapsed_label.set_text(f"{minutes:02d}:{secs:02d}")
        return True

    def _on_insert_result(self, result) -> bool:
        text = result.message or ("inserito" if result.ok else "inserimento fallito")
        self.insert_status.set_text(text)
        self.insert_status.set_tooltip_text(result.message or "")
        if result.ok:
            self.toast_overlay.add_toast(Adw.Toast.new(text))
        return False

    def _on_error(self, message: str) -> bool:
        self.toast_overlay.add_toast(Adw.Toast.new(message))
        return False

    # -- copia e segnalibri -------------------------------------------------
    def copy(self, target: str = "selection") -> None:
        """Copia negli appunti: selezione, ultimo segmento o tutto."""
        if target == "selection":
            buffer = self.view.view.get_buffer()
            selection = buffer.get_selection_bounds()
            if not selection:
                self.toast_overlay.add_toast(
                    Adw.Toast.new("Nessun testo selezionato")
                )
                return
            text = buffer.get_text(selection[0], selection[1], False)
        elif target == "last":
            text = self.view.last_line()
        else:
            text = self.view.full_text()

        if not text.strip():
            self.toast_overlay.add_toast(Adw.Toast.new("Niente da copiare"))
            return
        if _clipboard_copy(text):
            self.toast_overlay.add_toast(Adw.Toast.new("Copiato negli appunti"))
        else:
            self.toast_overlay.add_toast(
                Adw.Toast.new("Copia non riuscita: appunti non disponibili")
            )

    def _on_bookmark_filter_toggled(self, button: Gtk.ToggleButton) -> None:
        show = button.get_active()
        self.bookmark_panel.set_visible(show)
        button.set_icon_name(
            "starred-symbolic" if show else "non-starred-symbolic"
        )
        if show:
            self._refresh_bookmark_list()
        else:
            self.bookmark_list.remove_all()

    def _refresh_bookmark_list(self) -> None:
        """Ricostruisce l'elenco dei segmenti con segnalibro."""
        self.bookmark_list.remove_all()
        store = self.pipeline.store
        session_id = self.pipeline.session.session_id
        if store is None or session_id is None:
            return
        rows = store.get_bookmarks(session_id)
        if not rows:
            label = Gtk.Label(label="Nessun segnalibro", margin_top=6, margin_bottom=6)
            label.add_css_class("dim-label")
            self.bookmark_list.append(label)
            return
        for row in rows:
            text = str(row["text"])
            stamp = f"{int(row['t_start'] // 60):02d}:{int(row['t_start'] % 60):02d}"
            btn = Gtk.Button(
                label=f"{stamp}  {text[:60]}",
                halign=Gtk.Align.START,
                tooltip_text=text,
            )
            btn.set_css_classes(["flat"])
            btn.connect("clicked", self._on_bookmark_clicked, int(row["idx"]))
            self.bookmark_list.append(btn)

    def _on_bookmark_clicked(self, _button, index: int) -> None:
        self.view.scroll_to_index(index)

    def toggle_bookmark(self, index: int) -> None:
        """Mette o toglie il segnalibro su un segmento."""
        if index <= 0:
            return
        store = self.pipeline.store
        session_id = self.pipeline.session.session_id
        if store is None or session_id is None:
            return
        for row in store.get_segments(session_id):
            if int(row["idx"]) == index:
                new_state = not int(row["bookmark"])
                store.set_bookmark(int(row["id"]), new_state)
                self.view.set_bookmark(index, new_state)
                if self.bookmark_button.get_active():
                    self._refresh_bookmark_list()
                return

    # -- azioni della finestra ---------------------------------------------
    def _on_listen_clicked(self, _button) -> None:
        """Un solo tasto per avvio e pausa: l'icona mostra cosa fara' il click."""
        state = self.pipeline.state
        if state == ListenerState.LISTENING:
            self.pipeline.pause()
        elif state == ListenerState.PAUSED:
            self.pipeline.resume()
        else:
            self.pipeline.start()

    def _on_stop_clicked(self, _button) -> None:
        self.pipeline.stop()

    def _on_insert_toggled(self, button: Gtk.ToggleButton) -> None:
        enabled = button.get_active()
        self.settings.insert_enabled = enabled
        self.pipeline.set_insert_enabled(
            enabled, own_window=self.get_title() or ""
        )
        if enabled:
            self.toast_overlay.add_toast(
                Adw.Toast.new(
                    "Inserimento attivo: passa all'applicazione delle note "
                    "perche' il testo finisca li'"
                )
            )
        else:
            self.insert_status.set_text("")

    def _on_mode_changed(self, key: str) -> None:
        """Il controllo segmentato ha cambiato modalita'."""
        self._mode = key
        self._apply_mode()

    def _apply_mode(self) -> None:
        """Applica la modalita' a tutta la finestra.

        Le due modalita' devono essere distinguibili **a colpo d'occhio**,
        non solo da un'etichetta: nella consultazione la barra di comando
        si nasconde, i metadati spariscono e la finestra diventa piu'
        trasparente. Restano l'indicatore del microfono e la capsula di
        stato, perche' sapere se il microfono e' attivo non e' mai
        secondario.
        """
        consult = self._mode == MODE_CONSULT
        # i comandi spariscono: la finestra si legge, non si pilota.
        # Si nasconde il contenitore dei comandi, **non** la barra: il
        # commutatore di modalita' deve restare, altrimenti non c'e' modo
        # di tornare alla presa di appunti senza conoscere ``<Ctrl>m``.
        self.command_box.set_visible(not consult)
        self.meta_box.set_visible(not consult)
        self.mode_sep.set_visible(not consult)
        self.search_bar.set_visible(False)
        if consult:
            self.apply_opacity()
        self.title_label.set_text(
            "Trascrizione lezioni — consultazione" if consult else "Trascrizione lezioni"
        )
        self.set_title(
            "Trascrizione lezioni — consultazione" if consult else "Trascrizione lezioni"
        )

    def jump_to_latest(self, _button=None) -> None:
        self._following = True
        self.view.scroll_to_end()
        self._schedule_rejoin_update()

    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        self._search_query = entry.get_text().strip()
        self._apply_search_highlight()

    def _apply_search_highlight(self) -> None:
        """Evidenzia i segmenti che contengono la query di ricerca.

        La ricerca usa :meth:`SessionStore.search`, quindi filtra su
        quello che e' davvero stato salvato: i segmenti non ancora
        finalizzati non sono ricercabili.
        """
        query = self._search_query
        store = self.pipeline.store
        session_id = self.pipeline.session.session_id

        self._search_rows = []
        if not query or store is None or session_id is None:
            self._search_view_active = False
            self.status_label.set_text(self._default_status())
            return

        rows = store.search(session_id, query)
        self._search_rows = [int(r["idx"]) for r in rows]
        self._search_view_active = True
        self.status_label.set_text(
            f"{len(self._search_rows)} risultati per «{query}»"
        )
        if self._search_rows:
            self.view.scroll_to_index(self._search_rows[0])

    def _default_status(self) -> str:
        if self.pipeline.state == ListenerState.LISTENING:
            return f"in ascolto · {DECODE_LATENCY_HINT}"
        return "pronto"

    def _on_meta_changed(self, _entry) -> None:
        self.pipeline.set_session_info(
            self.course_entry.get_text().strip(),
            self.professor_entry.get_text().strip(),
            self.date_entry.get_text().strip(),
        )

    # -- preferenze --------------------------------------------------------
    def _on_model_changed(self, row: Adw.ComboRow, _pspec) -> None:
        model = row.get_selected_item().get_string()
        self.settings.model_name = model
        self.toast_overlay.add_toast(
            Adw.Toast.new(f"Modello impostato su «{model}»: si riavvia la sessione")
        )

    def _on_device_changed(self, row: Adw.ComboRow, _pspec) -> None:
        idx = row.get_selected()
        if 0 <= idx < len(self._device_indices):
            self.settings.input_device = self._device_indices[idx]

    def _on_opacity_changed(self, row: Adw.SpinRow, _pspec) -> None:
        value = float(row.get_value())
        self.settings.opacity = value
        self.apply_opacity()

    def apply_opacity(self) -> None:
        value = max(float(self.settings.opacity), MIN_OPACITY)
        if self._mode == MODE_CONSULT:
            value = min(value, 0.75)
        try:
            self.set_opacity(value)
        except Exception:
            pass

    def _on_font_changed(self, row: Adw.SpinRow, _pspec) -> None:
        size = int(row.get_value())
        self.settings.font_size = size
        self._apply_font_size()

    def _apply_font_size(self) -> None:
        """Applica la dimensione del testo alla vista trascrizione."""
        size = int(self.settings.font_size)
        provider = Gtk.CssProvider()
        try:
            provider.load_from_data(
                f"textview.lt-transcript {{ font-size: {size}pt; }}".encode("utf-8")
            )
        except Exception:
            return
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

    def _on_auto_scroll_changed(self, row: Adw.SwitchRow, _pspec) -> None:
        self._auto_scroll = row.get_active()
        self.settings.auto_scroll = self._auto_scroll
        if self._auto_scroll:
            self.view.scroll_to_end()

    def _on_compact_changed(self, row: Adw.SwitchRow, _pspec) -> None:
        self.settings.compact = row.get_active()
        self._apply_compact()

    def _apply_compact(self) -> None:
        """Vista compatta: solo stato e ultima riga.

        In compatto spariscono i metadati e la barra di comando: resta il
        transcript (o la sua ultima riga) e la capsula di stato, che e'
        l'informazione che non puo' mancare mai.
        """
        compact = self.settings.compact
        self.view.scroller.set_visible(not compact)
        self.meta_box.set_visible(not compact)
        self._command_bar.set_visible(not compact)
        self.command_box.set_visible(not compact)
        self.compact_label.set_visible(compact)
        if compact:
            self.set_default_size(self.settings.width, 120)
            self._refresh_compact_label()
        else:
            self.set_default_size(self.settings.width, self.settings.height)

    def _refresh_compact_label(self) -> None:
        """Aggiorna la riga compatta con l'ultimo testo disponibile."""
        if not self.settings.compact:
            return
        text = self.view.last_line()
        if not text:
            text = "nessuna trascrizione"
        self.compact_label.set_text(text)
        self.compact_label.set_tooltip_text(text)

    def _on_pin_toggled(self, button: Gtk.ToggleButton) -> None:
        """Il tasto del sempre-in-primo-piano: acceso = sopra le altre.

        Due differenze dal percorso della preferenza, entrambe volute:

        * se l'estensione non c'e' il tasto **torna spento** e la
          preferenza non viene toccata. Il percorso della riga salvava
          ``always_on_top=True`` anche quando la chiamata D-Bus era
          fallita, lasciando una preferenza che promette qualcosa che non
          succede — e al riavvio la finestra non sarebbe sopra le altre
          nonostante l'interruttore acceso;
        * non disabilita niente. Il percorso della riga faceva
          ``row.set_sensitive(False)``: misurato, dopo un solo tentativo a
          vuoto l'utente non poteva piu' toccare l'interruttore, e se
          l'estensione veniva installata dopo restava spento senza
          possibilita' di accenderlo. Il tasto invece riprova a ogni
          pressione: installare l'estensione e premere di nuovo funziona.

        Lo stato mostrato viene dalla preferenza, che e' la sola fonte
        affidabile (``GetAlwaysOnTop`` mente quando la finestra non e'
        aperta — misurato).
        """
        enabled = button.get_active()
        if self._shell.set_always_on_top(enabled):
            self.settings.always_on_top = enabled
            self._sync_always_on_top_rows()
            return
        # l'estensione non risponde: si torna allo stato di prima, senza
        # salvare una preferenza che non ha effetto
        button.handler_block_by_func(self._on_pin_toggled)
        button.set_active(self.settings.always_on_top)
        button.handler_unblock_by_func(self._on_pin_toggled)
        self.toast_overlay.add_toast(
            Adw.Toast.new(
                "Sempre in primo piano non disponibile: manca l'estensione "
                "GNOME Shell «Lecture Transcriber»"
            )
        )

    def _sync_always_on_top_rows(self) -> None:
        """Allinea l'interruttore delle preferenze al tasto della barra.

        Sono due viste della stessa preferenza: se cambia da una parte
        l'altra deve seguire, altrimenti l'utente vede due stati diversi
        per la stessa cosa e non sa quale sia quello vero.
        """
        row = getattr(self, "always_on_top_row", None)
        if row is None:
            return
        active = self.settings.always_on_top
        row.handler_block_by_func(self._on_always_on_top_changed)
        row.set_active(active)
        row.set_sensitive(True)
        row.handler_unblock_by_func(self._on_always_on_top_changed)

    def _on_always_on_top_changed(self, row: Adw.SwitchRow, _pspec) -> None:
        enabled = row.get_active()
        if self._shell.set_always_on_top(enabled):
            self.settings.always_on_top = enabled
            self._sync_pin_button()
            return
        # l'estensione non c'e': la preferenza resta salvata ma non ha effetto
        # (non si disabilita la riga: rimettendo a posto l'estensione deve
        # restare possibile riprovare, misurato il difetto opposto)
        row.set_sensitive(True)
        self.toast_overlay.add_toast(
            Adw.Toast.new(
                "Sempre in primo piano non disponibile: manca l'estensione "
                "GNOME Shell «Lecture Transcriber»"
            )
        )

    def _sync_pin_button(self) -> None:
        """Allinea il tasto della barra alla preferenza."""
        button = getattr(self, "pin_button", None)
        if button is None:
            return
        active = self.settings.always_on_top
        button.handler_block_by_func(self._on_pin_toggled)
        button.set_active(active)
        button.handler_unblock_by_func(self._on_pin_toggled)

    def apply_always_on_top(self) -> bool:
        """Chiede all'estensione GNOME Shell il sempre in primo piano.

        Ritorna False se l'estensione non e' installata: la finestra
        continua a funzionare, ma senza restare sopra le altre.
        """
        if not self.settings.always_on_top:
            return True
        if self._shell.set_always_on_top(True):
            self._sync_pin_button()
            return True
        # Non si disabilita ne' la riga ne' il tasto: la finestra continua
        # a funzionare e l'utente puo' riprovare dopo aver installato
        # l'estensione.
        self.always_on_top_row.set_sensitive(True)
        self.toast_overlay.add_toast(
            Adw.Toast.new(
                "Sempre in primo piano non disponibile: manca l'estensione "
                "GNOME Shell «Lecture Transcriber»"
            )
        )
        return False

    def _on_insert_paragraphs_changed(self, row: Adw.SwitchRow, _pspec) -> None:
        self.settings.insert_paragraphs = row.get_active()
        self.pipeline.set_insert_paragraphs(self.settings.insert_paragraphs)

    def _on_insert_method_changed(self, row: Adw.ComboRow, _pspec) -> None:
        from .insertion import InsertMethod

        value = row.get_selected_item().get_string()
        self.settings.insert_target = value
        try:
            self.pipeline.set_insert_method(InsertMethod(value))
        except ValueError:
            pass

    def _on_record_audio_changed(self, row: Adw.SwitchRow, _pspec) -> None:
        enabled = row.get_active()
        self.settings.record_audio = enabled
        # vale anche a sessione in corso: la pipeline apre o chiude il file
        # adesso, cosi' il comportamento segue subito la casella invece di
        # aspettare il prossimo "ascolta".
        try:
            self.pipeline.set_record_audio(enabled)
        except Exception as exc:
            self._on_error(f"Registrazione audio non disponibile: {exc}")
            return
        if enabled:
            path = self.pipeline.recording_path
            if path is not None:
                detail = f"Audio salvato in {path}"
            elif self.pipeline.is_listening:
                detail = "Registrazione audio attiva: l'audio verrà scritto su disco"
            else:
                detail = "Registrazione audio attiva: parte con il prossimo ascolto"
            self.toast_overlay.add_toast(Adw.Toast.new(detail))

    # -- scorciatoie --------------------------------------------------------
    def _install_shortcuts(self) -> None:
        """Scorciatoie locali. Quelle globali dipendono da shortcuts.py."""
        controller = Gtk.ShortcutController()
        for accel, callback in (
            ("<Ctrl>f", self._focus_search),
            ("<Ctrl>l", self.jump_to_latest),
            ("<Ctrl>i", self._toggle_insert),
            ("<Ctrl>m", self._toggle_mode),
            ("<Ctrl>b", self._toggle_bookmark),
            ("<Ctrl>c", lambda *_a: self.copy("selection")),
        ):
            trigger = Gtk.ShortcutTrigger.parse_string(accel)
            if trigger is None:
                continue
            controller.add_shortcut(
                Gtk.Shortcut(trigger=trigger, action=Gtk.CallbackAction.new(callback))
            )
        self.add_controller(controller)

    def _focus_search(self) -> None:
        self.search_entry.grab_focus()

    def _toggle_bookmark(self) -> None:
        index = self.pipeline.segment_index
        if index > 0:
            self.toggle_bookmark(index)

    def _toggle_insert(self) -> None:
        self.insert_button.set_active(not self.insert_button.get_active())

    def _toggle_mode(self) -> None:
        self.mode_segmented.set_active(
            MODE_CONSULT if self._mode == MODE_NOTES else MODE_NOTES
        )

    # -- esportazione -------------------------------------------------------
    def export(self, fmt: str) -> None:
        """Esporta la sessione corrente nel formato richiesto."""
        exporter = self.pipeline.exporter
        session_id = self.pipeline.session.session_id
        if exporter is None or session_id is None:
            self.toast_overlay.add_toast(
                Adw.Toast.new("Nessuna sessione da esportare")
            )
            return
        suffix = {
            "txt": ".txt",
            "md": ".md",
            "md_ts": "-ts.md",
            "srt": ".srt",
        }.get(fmt, ".txt")
        dialog = Gtk.FileDialog()
        dialog.set_title("Esporta la trascrizione")
        dialog.set_initial_name(f"trascrizione{suffix}")
        dialog.set_accept_label("Esporta")
        self._pending_export_fmt = fmt
        dialog.save(self, None, self._on_export_ready)

    def _on_export_ready(self, dialog: Gtk.FileDialog, result) -> None:
        fmt = getattr(self, "_pending_export_fmt", "txt")
        self._pending_export_fmt = ""
        try:
            file = dialog.save_finish(result)
        except Exception:
            return
        path = Path(file.get_path() or "")
        exporter = self.pipeline.exporter
        session_id = self.pipeline.session.session_id
        if exporter is None or session_id is None:
            return
        try:
            text = exporter.render(session_id, fmt)
        except Exception:
            self.toast_overlay.add_toast(Adw.Toast.new("Export fallito"))
            return
        self._show_export_preview(fmt, path, exporter, session_id, text)

    def _show_export_preview(self, fmt, path, exporter, session_id, text) -> None:
        """Mostra il testo prima di scriverlo: la conferma resta esplicita.

        Scrivere a colpo secco e' un difetto chiuso: nessun controllo di
        quel che finisce nel file. Ora il contenuto anticipato compare in
        una finestra di sola lettura, monospace, e la scrittura avviene
        solo premendo "Conferma salvataggio". Chiudere la finestra senza
        confermare non scrive nulla e non mostra toast. Una sola anteprima
        per volta: se ne riapre un'altra, la precedente viene chiusa.
        """
        current = getattr(self, "_export_preview", None)
        if current is not None:
            current.destroy()
        preview = Adw.PreferencesWindow()
        preview.set_title("Anteprima esportazione")
        preview.set_default_size(380, 420)

        buffer = Gtk.TextBuffer()
        buffer.set_text(text)
        text_view = Gtk.TextView()
        text_view.set_buffer(buffer)
        text_view.set_editable(False)
        text_view.set_cursor_visible(False)
        text_view.set_monospace(True)
        scroller = Gtk.ScrolledWindow()
        scroller.set_child(text_view)
        scroller.set_vexpand(True)

        confirm = Gtk.Button(label="Conferma salvataggio")
        confirm.add_css_class("suggested-action")
        confirm.connect(
            "clicked", self._on_export_confirm, preview, fmt, path, exporter, session_id
        )

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.append(scroller)
        box.append(confirm)
        preview.set_content(box)
        self._export_preview = preview
        preview.present()

    def _on_export_confirm(
        self, _button, preview, fmt, path, exporter, session_id
    ) -> None:
        """Chiude l'anteprima e scrive il file: la sola strada di scrittura."""
        preview.destroy()
        if getattr(self, "_export_preview", None) is preview:
            self._export_preview = None
        try:
            exporter.export(session_id, fmt, path)
        except Exception as exc:
            self.toast_overlay.add_toast(Adw.Toast.new(f"Export fallito: {exc}"))
            return
        self.toast_overlay.add_toast(Adw.Toast.new(f"Salvato in {path.name}"))

    # -- salva lezione in PDF ----------------------------------------------
    def _on_save_lesson(self, _button) -> None:
        """Salva la lezione come PDF in ~/Documents, senza chiedere dove.

        E' il tasto della barra superiore: un tap, un file pronto da
        consegnare o rivedere. Il nome deriva da corso, docente e data
        (via ``safe_filename``), perche' sia unico e riconoscibile.
        """
        store = self.pipeline.store
        session_id = self.pipeline.session.session_id
        if store is None or session_id is None:
            self.toast_overlay.add_toast(
                Adw.Toast.new("Niente da salvare: nessuna lezione aperta")
            )
            return
        course = self.pipeline.session.course or ""
        professor = self.pipeline.session.professor or ""
        date = self.pipeline.session.date or ""
        from .export import safe_filename as _safe_name

        base_name = _safe_name(course, professor, date)
        documents = Path.home() / "Documents"
        path = documents / f"{base_name}.pdf"
        try:
            from . import pdf_export

            path = pdf_export.save_pdf(store, session_id, path)
        except Exception as exc:
            self.toast_overlay.add_toast(Adw.Toast.new(f"Salvataggio fallito: {exc}"))
            return
        self.toast_overlay.add_toast(
            Adw.Toast.new(f"Lezione salvata in {path}")
        )

    # -- ciclo di vita ------------------------------------------------------
    def do_close_request(self) -> bool:
        """Salva le preferenze e chiude la sessione."""
        self._save_settings()
        try:
            self.pipeline.close()
        except Exception:
            pass
        return False

    def _save_settings(self) -> None:
        try:
            _config.save_settings(self.settings)
        except Exception:
            pass

    def apply_position(self) -> None:
        """Posiziona la finestra in alto a destra sul monitor corrente.

        GTK4 non espone un'API di posizionamento della finestra: su
        Wayland il cliente non puo' spostare la propria finestra. Si usa
        pero' la gravita' del toplevel, che il compositor rispetta
        (xdg_toplevel.set_gravity): con ``NORTH_EAST`` la finestra viene
        piazzata nell'angolo in alto a destra del monitor.
        """
        display = Gdk.Display.get_default()
        if display is None:
            return
        monitor = self._current_monitor(display)
        if monitor is None:
            return
        rect = monitor.get_geometry()
        width = self.get_width() or self.settings.width
        height = self.get_height() or self.settings.height

        # con la gravita' il compositor fa il posizionamento: noi ci
        # limitiamo a restringere la finestra perche' resti nell'angolo.
        try:
            surface = self.get_surface()
            if surface is not None:
                surface.set_gravity(Gdk.Gravity.NORTH_EAST)
        except Exception:
            pass
        self.set_default_size(min(width, rect.width), min(height, rect.height))

    def _current_monitor(self, display: Gdk.Display) -> Gdk.Monitor | None:
        """Il monitor che contiene il puntatore, o il primo disponibile."""
        try:
            seat = display.get_default_seat()
            pointer = seat.get_pointer() if seat is not None else None
            if pointer is not None:
                surface = pointer.get_surface_at_position()
                if surface is not None:
                    monitor = display.get_monitor_at_surface(surface)
                    if monitor is not None:
                        return monitor
        except Exception:
            pass
        try:
            monitors = display.get_monitors()
            if monitors:
                return monitors[0]
        except Exception:
            pass
        return None


def run_application(argv: list[str] | None = None) -> int:
    """Avvia l'applicazione grafica e restituisce il codice di uscita.

    La classe applicativa sta in :mod:`lecture_transcriber.app`: qui
    ci sono solo i widget. Il collegamento e' in un solo punto, cosi'
    l'interfaccia resta riutilizzabile anche da una modalita' headless.
    """
    _config.ensure_dirs()
    from .app import main as run_main

    return run_main(argv)
