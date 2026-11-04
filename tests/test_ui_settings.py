"""Collaudo dell'interfaccia: quello che i test delle altre parti non vedono.

Questi test sono nati da due difetti *reali* trovati provando il popover
delle preferenze a mano, entrambi invisibili agli altri collaudi perche'
nessuno costruisce la finestra:

* il gruppo "Aspetto" (opacita', dimensione del testo, scorrimento
  automatico, vista compatta, sempre in primo piano, click-through) veniva
  costruito riga per riga ma **non aggiunto alla pagina**: nessun errore,
  nessun avviso, le preferenze semplicemente non esistevano. Misurato: il
  pannello mostrava solo Modello, Acquisizione audio, Inserimento, Privacy.
* il popover non aveva una larghezza dichiarata, quindi GTK lo riduceva
  fino a far stare le etichette *ellisizzate*: misurato 183 px, con
  "Modello di riconoscimento" (191 px naturali) tagliata a 95 px e i valori
  dei menu a tendina -- "large-v3-turbo" -- a 14 px.

Il filo conduttore e' lo stesso: un widget che *esiste* nel codice non e'
un widget che l'utente puo' raggiungere. Qui si controlla l'albero
effettivo, non le chiamate.
"""

from __future__ import annotations

import unittest

from _bootstrap import ensure_paths

ensure_paths()

from lecture_transcriber import ui as uimod  # noqa: E402

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gio, GLib, Gtk

    _HAVE_GTK = True
except (ImportError, ValueError):  # pragma: no cover - dipende dall'ambiente
    _HAVE_GTK = False


def _labels(widget, out=None):
    """Raccoglie tutte le ``Gtk.Label`` visibili sotto ``widget``."""
    if out is None:
        out = []
    if isinstance(widget, Gtk.Label):
        out.append(widget)
    child = widget.get_first_child()
    while child is not None:
        _labels(child, out)
        child = child.get_next_sibling()
    return out


def _reachable_controls(widget, out=None, blocked=False):
    """Widget interattivi davvero raggiungibili sotto ``widget``.

    ``get_visible()`` su un widget dice se *quel* widget e' marcato
    visibile, non se e' mostrato: un figlio di un genitore nascosto
    continua a rispondere ``True``. Senza propagare lo stato degli
    antenati questo controllo passava anche con la barra intera nascosta
    — ed e' cosi' che il difetto della trappola in consultazione e'
    sopravvissuto a un primo collaudo.
    """
    if out is None:
        out = []
    child = widget.get_first_child()
    while child is not None:
        hidden = blocked or not child.get_visible()
        if isinstance(child, (Gtk.Button, Gtk.ToggleButton, Gtk.MenuButton)):
            out.append((child, not hidden))
        _reachable_controls(child, out, hidden)
        child = child.get_next_sibling()
    return out


@unittest.skipUnless(_HAVE_GTK, "serve GTK4/libadwaita")
class SettingsPopoverTest(unittest.TestCase):
    """Il pannello preferenze: contenuti raggiungibili e larghezza onesta."""

    @classmethod
    def setUpClass(cls) -> None:
        Adw.init()

    def setUp(self) -> None:
        from lecture_transcriber.app import (
            PipelineCallbacks,
            PipelineConfig,
            TranscriptionPipeline,
        )
        from lecture_transcriber.config import Settings

        self.pipeline = TranscriptionPipeline(
            PipelineConfig(settings=Settings()), callbacks=PipelineCallbacks()
        )
        # La finestra vuole una Gtk.Application: `Gtk.Application` va bene,
        # non serve un ciclo di vita completo per costruire i widget.
        app = Gtk.Application(application_id="it.lecturetranscriber.TestUi")
        self.window = uimod.MainWindow(app, self.pipeline)

    def test_popover_exists(self) -> None:
        """Il popover e' agganciato al pulsante: senza, le preferenze non si aprono."""
        popover = self.window.settings_button.get_popover()
        self.assertIsNotNone(popover)
        self.assertIsInstance(popover, Gtk.Popover)

    def test_every_group_reaches_the_page(self) -> None:
        """Ogni gruppo costruito deve stare nella pagina.

        Questo e' il difetto originario: il gruppo "Aspetto" era costruito
        (sei righe, con tanto di collegamenti ai gestori) ma non aggiunto,
        quindi nessuna di quelle preferenze era raggiungibile.
        """
        popover = self.window.settings_button.get_popover()
        page = popover.get_child()
        self.assertIsInstance(page, Adw.PreferencesPage)

        titles = []
        child = page.get_first_child()
        while child is not None:
            if isinstance(child, Adw.PreferencesGroup):
                titles.append(child.get_title())
            child = child.get_next_sibling()
        # Adw.PreferencesPage contiene uno ScrolledWindow: si scende di un livello
        if not titles:
            stack = [page.get_first_child()]
            while stack:
                node = stack.pop()
                while node is not None:
                    if isinstance(node, Adw.PreferencesGroup):
                        titles.append(node.get_title())
                    stack.append(node.get_first_child())
                    node = node.get_next_sibling()

        for expected in (
            "Modello di riconoscimento",
            "Acquisizione audio",
            "Aspetto",
            "Inserimento nel documento",
            "Privacy",
        ):
            self.assertIn(expected, titles)

    def test_aspetto_rows_are_reachable(self) -> None:
        """Le righe del gruppo Aspetto devono esistere *e* essere nella pagina."""
        popover = self.window.settings_button.get_popover()
        page = popover.get_child()

        def descendants(widget, out=None):
            if out is None:
                out = []
            child = widget.get_first_child()
            while child is not None:
                out.append(child)
                descendants(child, out)
                child = child.get_next_sibling()
            return out

        in_page = {id(w) for w in descendants(page)}
        for name in (
            "opacity_row",
            "font_row",
            "auto_scroll_row",
            "compact_row",
            "always_on_top_row",
            "click_through_row",
        ):
            row = getattr(self.window, name, None)
            self.assertIsNotNone(row, f"{name} non costruita")
            self.assertIn(
                id(row), in_page, f"{name} esiste ma non e' raggiungibile dal pannello"
            )

    def test_popover_is_wide_enough_for_its_labels(self) -> None:
        """La larghezza dichiarata deve superare la larghezza naturale dei titoli.

        Senza ``set_size_request`` GTK restringeva il pannello a 183 px:
        i titoli venivano ellisizzati e i valori dei menu tagliati.
        """
        popover = self.window.settings_button.get_popover()
        request = popover.get_size_request()
        width_request = request.width
        self.assertGreater(
            width_request,
            0,
            "il popover non ha una larghezza minima: GTK lo rimpicciolisce "
            "fino a ellisizzare le etichette",
        )
        # Le etichette che si *troncano* sono quelle su una riga sola: i
        # titoli di riga e di gruppo. Le descrizioni (il paragrafo della
        # sezione Privacy) vanno a capo e la loro larghezza naturale non
        # conta — misurata a 1075 px, molto piu' del pannello.
        longest = 0
        longest_text = ""
        for label in _labels(popover):
            text = label.get_text()
            if not text or text == "Search":
                continue
            if label.get_ellipsize() == 0:
                # ellipsize=NONE: il testo va a capo, non si tronca
                continue
            if label.get_wrap():
                continue
            natural = label.get_preferred_size().natural_size.width
            if natural > longest:
                longest = natural
                longest_text = text
        self.assertGreaterEqual(
            width_request,
            longest,
            "larghezza richiesta %d < titolo piu' largo %d (%r)"
            % (width_request, longest, longest_text),
        )

    def test_privacy_row_defaults_to_off(self) -> None:
        """La registrazione audio e' spenta per impostazione predefinita (brief §8)."""
        self.assertFalse(self.window.settings.record_audio)
        self.assertFalse(self.window.record_audio_row.get_active())


@unittest.skipUnless(_HAVE_GTK, "serve GTK4/libadwaita")
class ModeReachabilityTest(unittest.TestCase):
    """La consultazione discreta deve essere una modalita', non una trappola.

    Difetto trovato provando la finestra: passando in consultazione si
    nascondeva la barra di comando *intera*, e con essa il commutatore
    Lettura/Appunti e il pulsante delle preferenze. Misurato: zero
    controlli raggiungibili, per uscire serviva sapere a memoria ``<Ctrl>m``.
    """

    @classmethod
    def setUpClass(cls) -> None:
        Adw.init()

    def setUp(self) -> None:
        from lecture_transcriber.app import (
            PipelineCallbacks,
            PipelineConfig,
            TranscriptionPipeline,
        )
        from lecture_transcriber.config import Settings

        self.pipeline = TranscriptionPipeline(
            PipelineConfig(settings=Settings()), callbacks=PipelineCallbacks()
        )
        app = Gtk.Application(application_id="it.lecturetranscriber.TestUi")
        self.window = uimod.MainWindow(app, self.pipeline)

    def _shown_controls(self):
        overlay = self.window.get_content()
        return [w for w, shown in _reachable_controls(overlay) if shown]

    def test_consult_mode_hides_the_commands(self) -> None:
        """In consultazione i comandi non si vedono: la modalita' e' discreta."""
        # si passa dalla strada dell'utente (il commutatore), non scrivendo
        # ``_mode`` a mano: cosi' il controllo segmentato e lo stato
        # interno rimangono d'accordo
        self.window.mode_segmented.set_active(uimod.MODE_CONSULT)
        self.assertEqual(self.window._mode, uimod.MODE_CONSULT)
        self.assertFalse(self.window.command_box.get_visible())
        # il contenitore dei comandi e' nascosto, quindi i suoi figli non
        # sono mostrati (anche se `get_visible()` su di loro resta True:
        # e' esattamente il tranello che questo test deve evitare)
        shown = self._shown_controls()
        self.assertNotIn(self.window.listen_button, shown)
        self.assertNotIn(self.window.settings_button, shown)

    def test_consult_mode_keeps_the_mode_switch(self) -> None:
        """Ma il commutatore di modalita' resta: e' l'unica uscita visibile."""
        self.window.mode_segmented.set_active(uimod.MODE_CONSULT)
        shown = self._shown_controls()
        self.assertGreater(
            len(shown),
            0,
            "in consultazione discreta non resta nessun controllo: "
            "l'utente e' bloccato dentro la modalita'",
        )
        # il commutatore e' dentro la barra, quindi i suoi pulsanti devono
        # essere fra i controlli mostrati
        segmented_buttons = list(self.window.mode_segmented._buttons.values())
        for button in segmented_buttons:
            self.assertIn(
                button,
                shown,
                "il commutatore Lettura/Appunti non e' raggiungibile in consultazione",
            )

    def test_notes_mode_shows_everything(self) -> None:
        """Nella presa di appunti i comandi ci sono tutti."""
        self.window._mode = uimod.MODE_NOTES
        self.window._apply_mode()
        shown = self._shown_controls()
        for name in (
            "listen_button",
            "stop_button",
            "settings_button",
            "copy_button",
            "export_button",
        ):
            self.assertIn(getattr(self.window, name), shown, f"{name} non mostrato")

    def test_compact_mode_hides_everything(self) -> None:
        """In compatto la finestra e' una striscia: nessun comando."""
        self.window.settings.compact = True
        self.window._apply_compact()
        shown = self._shown_controls()
        for name in ("listen_button", "settings_button", "copy_button"):
            self.assertNotIn(
                getattr(self.window, name), shown, f"{name} mostrato in compatto"
            )

    def test_round_trip_leaves_notes_mode(self) -> None:
        """Andare in consultazione e tornare indietro deve funzionare."""
        self.window.mode_segmented.set_active(uimod.MODE_CONSULT)
        # e' la strada che l'utente fa: preme "Appunti" sul commutatore
        self.window.mode_segmented.set_active(uimod.MODE_NOTES)
        self.assertEqual(self.window._mode, uimod.MODE_NOTES)
        shown = self._shown_controls()
        self.assertIn(self.window.listen_button, shown)

    def test_keyboard_shortcut_switches_mode(self) -> None:
        """La scorciatoia <Ctrl>m passa da una modalita' all'altra e torna."""
        before = self.window._mode
        self.window._toggle_mode()
        after = self.window._mode
        self.assertNotEqual(before, after)
        self.assertEqual(
            self.window.mode_segmented.active,
            after,
            "la scorciatoia deve tenere allineato il commutatore",
        )
        self.window._toggle_mode()
        self.assertEqual(self.window._mode, before)


@unittest.skipUnless(_HAVE_GTK, "serve GTK4/libadwaita")
class ListenButtonTest(unittest.TestCase):
    """Un solo tasto per avvio e pausa, e dalla pausa si deve poter riprendere.

    Difetto trovato dall'utente: in pausa non si poteva riprendere
    l'ascolto. C'erano tre bottoni (avvia, pausa, stop) e ``_on_state``
    abilitava il bottone pausa **solo** in ascolto, quindi da PAUSED
    entrambi quelli utili erano disabilitati: misurato, l'unica via
    d'uscita era terminare la lezione e ricominciare. Il codice che
    riprendeva esisteva gia' ma era irraggiungibile.

    Ora un solo pulsante: l'icona dice lo stato, il click fa l'opposto.
    """

    @classmethod
    def setUpClass(cls) -> None:
        Adw.init()

    def setUp(self) -> None:
        from lecture_transcriber.app import (
            PipelineCallbacks,
            PipelineConfig,
            TranscriptionPipeline,
        )
        from lecture_transcriber.config import Settings

        self.pipeline = TranscriptionPipeline(
            PipelineConfig(settings=Settings()), callbacks=PipelineCallbacks()
        )
        app = Gtk.Application(application_id="it.lecturetranscriber.TestBtn")
        self.window = uimod.MainWindow(app, self.pipeline)

    def _set(self, state) -> None:
        self.pipeline.state = state
        self.window._on_state(state, "")

    def test_there_is_only_one_listen_button(self) -> None:
        """Un bottono, non tre: la barra non deve offrire scelte morte."""
        self.assertTrue(hasattr(self.window, "listen_button"))
        self.assertFalse(hasattr(self.window, "start_button"))
        self.assertFalse(hasattr(self.window, "pause_button"))

    def test_the_icon_shows_play_when_idle(self) -> None:
        from lecture_transcriber.app import ListenerState

        self._set(ListenerState.IDLE)
        self.assertEqual(self.window.listen_button.get_icon_name(), "media-playback-start")
        self.assertTrue(self.window.listen_button.get_sensitive())

    def test_the_icon_shows_pause_while_listening(self) -> None:
        from lecture_transcriber.app import ListenerState

        self._set(ListenerState.LISTENING)
        self.assertEqual(self.window.listen_button.get_icon_name(), "media-playback-pause")

    def test_the_icon_shows_play_again_when_paused(self) -> None:
        from lecture_transcriber.app import ListenerState

        self._set(ListenerState.PAUSED)
        self.assertEqual(self.window.listen_button.get_icon_name(), "media-playback-start")

    def test_the_button_is_usable_while_paused(self) -> None:
        """Il cuore del difetto: da PAUSED il tasto deve essere premibile."""
        from lecture_transcriber.app import ListenerState

        self._set(ListenerState.PAUSED)
        self.assertTrue(
            self.window.listen_button.get_sensitive(),
            "in pausa il tasto e' disabilitato: non si puo' riprendere",
        )

    def test_clicking_while_paused_resumes(self) -> None:
        from lecture_transcriber.app import ListenerState

        self._set(ListenerState.PAUSED)
        self.window._on_listen_clicked(None)
        self.assertEqual(self.pipeline.state, ListenerState.LISTENING)

    def test_clicking_while_listening_pauses(self) -> None:
        from lecture_transcriber.app import ListenerState

        self._set(ListenerState.LISTENING)
        self.window._on_listen_clicked(None)
        self.assertEqual(self.pipeline.state, ListenerState.PAUSED)

    def test_clicking_when_idle_starts(self) -> None:
        from lecture_transcriber.app import ListenerState

        self._set(ListenerState.IDLE)
        # senza motore ne' segmenter, start() chiama open() e segnala
        # l'errore: non deve sollevare
        self.window._on_listen_clicked(None)
        self.assertNotEqual(self.pipeline.state, ListenerState.LISTENING)


class _FakeShell:
    """Ponte verso l'estensione GNOME Shell, con risposta comandata.

    Non e' un ``mock.MagicMock``: registra le chiamate e risponde quello
    che il test decide, cosi' si vede *quante* volte l'app ha provato a
    parlare con l'estensione e con quali argomenti.
    """

    def __init__(self, works: bool = True) -> None:
        self.works = works
        self.calls: list[bool] = []
        self.state = False

    def set_always_on_top(self, enabled: bool) -> bool:
        self.calls.append(bool(enabled))
        if self.works:
            self.state = bool(enabled)
        return self.works

    def get_always_on_top(self):
        return self.state if self.works else None

    def available(self) -> bool:
        return self.works


@unittest.skipUnless(_HAVE_GTK, "serve GTK4/libadwaita")
class AlwaysOnTopButtonTest(unittest.TestCase):
    """Il sempre-in-primo-piano deve essere un tasto, non solo una preferenza.

    Il difetto di partenza: "sempre in primo piano" era raggiungibile solo
    dall'interruttore dentro il popover delle preferenze, quindi servivano
    due tap piu' la voglia di aprire un pannello per la cosa che la
    finestra fa' di piu' caratteristico — stare sopra l'editor di appunti.

    Questi test guardano l'albero dei widget *reale*, senza mock della
    finestra, per la stessa ragione per cui furono scritti quelli della
    consultazione: un widget che esiste nel codice non e' un widget che
    l'utente puo' raggiungere.
    """

    @classmethod
    def setUpClass(cls) -> None:
        Adw.init()

    def setUp(self) -> None:
        from lecture_transcriber.app import (
            PipelineCallbacks,
            PipelineConfig,
            TranscriptionPipeline,
        )
        from lecture_transcriber.config import Settings

        self.pipeline = TranscriptionPipeline(
            PipelineConfig(settings=Settings()), callbacks=PipelineCallbacks()
        )
        app = Gtk.Application(application_id="it.lecturetranscriber.TestPin")
        self.window = uimod.MainWindow(app, self.pipeline)
        self.shell = _FakeShell(works=True)
        self.window._shell = self.shell

    def _shown(self):
        overlay = self.window.get_content()
        return [w for w, shown in _reachable_controls(overlay) if shown]

    # -- esiste e si vede --------------------------------------------------
    def test_the_button_exists(self) -> None:
        self.assertTrue(hasattr(self.window, "pin_button"))
        self.assertIsInstance(self.window.pin_button, Gtk.ToggleButton)

    def test_the_button_is_reachable_in_notes_mode(self) -> None:
        self.assertIn(self.window.pin_button, self._shown())

    def test_the_button_is_reachable_in_consult_mode(self) -> None:
        """Il cuore della scelta di posizione.

        In consultazione discreta la riga dei comandi sparisce: un tasto
        messo la' sarebbe irraggiungibile proprio mentre si rilegge una
        lezione con la finestra sopra l'editor. La barra superiore invece
        non viene nascosta da nessuna modalita'.
        """
        self.window.mode_segmented.set_active(uimod.MODE_CONSULT)
        self.assertIn(
            self.window.pin_button,
            self._shown(),
            "in consultazione discreta il tasto del sempre-in-primo-piano "
            "non e' raggiungibile",
        )

    def test_the_button_is_reachable_in_compact_mode(self) -> None:
        self.window.settings.compact = True
        self.window._apply_compact()
        self.assertIn(
            self.window.pin_button,
            self._shown(),
            "in vista compatta il tasto del sempre-in-primo-piano non e' "
            "raggiungibile",
        )

    def test_the_icon_exists_in_the_icon_theme(self) -> None:
        """L'icona deve esistere davvero, non solo nel codice.

        ``document-export-symbolic`` era usato dal pulsante di export e
        **non esiste** nel tema icone di Fedora 44: il pulsante restava
        disegnato con l'icona di ripiego. Verificato con
        ``Gtk.IconTheme.has_icon``, non dato per buono.
        """
        from gi.repository import Gdk

        theme_icons = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        self.assertTrue(
            theme_icons.has_icon(self.window.pin_button.get_icon_name()),
            "l'icona %r non esiste nel tema: il pulsante resta vuoto"
            % self.window.pin_button.get_icon_name(),
        )

    def test_every_button_icon_exists_in_the_theme(self) -> None:
        """Nessun pulsante puo' restare con l'icona di ripiego.

        Il difetto che questo test cattura e' gia' successo: il pulsante di
        export usava ``document-export-symbolic``, che nel tema icone di
        Fedora 44 non esiste (misurato con ``Gtk.IconTheme.has_icon``:
        risponde ``False``). GTK non si lamenta — disegna ``image-missing``
        — quindi l'unico modo di accorgersene e' chiedere al tema.
        """
        from gi.repository import Gdk

        theme_icons = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())

        def nodes(widget):
            child = widget.get_first_child()
            while child is not None:
                yield child
                yield from nodes(child)
                child = child.get_next_sibling()

        missing = []
        for widget in nodes(self.window.get_content()):
            if not isinstance(
                widget, (Gtk.Button, Gtk.ToggleButton, Gtk.MenuButton)
            ):
                continue
            icon = widget.get_icon_name()
            if icon and not theme_icons.has_icon(icon):
                missing.append((type(widget).__name__, icon))
        self.assertEqual(
            missing,
            [],
            "pulsanti la cui icona non esiste nel tema (restano vuoti): %r"
            % missing,
        )

    def test_the_button_does_not_widen_the_command_bar(self) -> None:
        """La finestra deve restare stretta: 420 px.

        Misurato: la riga comandi sta a 402 px naturali e aggiungere un
        bottone la porta a 444 px, costringendo la finestra ad allargarsi —
        lo stesso difetto che aveva obbligato la barra su due file. Il
        tasto sta quindi nella barra superiore, che ha ~109 px di residuo.
        """
        command_width = self.window.command_box.get_preferred_size()[1].width
        self.assertLessEqual(
            command_width,
            self.window.settings.width,
            "la riga comandi misura %d px e la finestra ne ha %d: si allarga"
            % (command_width, self.window.settings.width),
        )
        top_width = self.window._top_bar.get_preferred_size()[1].width
        self.assertLessEqual(
            top_width,
            self.window.settings.width,
            "la barra superiore misura %d px, piu' dei %d px della finestra"
            % (top_width, self.window.settings.width),
        )

    # -- fa quello che dice ------------------------------------------------
    def test_pressing_it_asks_the_extension(self) -> None:
        self.window.pin_button.set_active(False)
        self.window.pin_button.set_active(True)
        self.assertIn(True, self.shell.calls)

    def test_turning_it_off_saves_the_preference(self) -> None:
        self.window.settings.always_on_top = True
        self.window._sync_pin_button()
        self.window.pin_button.set_active(False)
        self.assertFalse(self.window.settings.always_on_top)
        self.assertIn(False, self.shell.calls)

    def test_the_preference_row_follows_the_button(self) -> None:
        """Le due viste della stessa preferenza non devono contraddirsi."""
        self.window.pin_button.set_active(False)
        self.assertFalse(self.window.always_on_top_row.get_active())
        self.window.pin_button.set_active(True)
        self.assertTrue(self.window.always_on_top_row.get_active())

    def test_the_button_follows_the_preference_row(self) -> None:
        """E anche il percorso inverso."""
        self.window.always_on_top_row.set_active(False)
        self.assertFalse(self.window.pin_button.get_active())
        self.window.always_on_top_row.set_active(True)
        self.assertTrue(self.window.pin_button.get_active())

    def test_the_state_comes_from_settings_not_from_the_extension(self) -> None:
        """``GetAlwaysOnTop`` mente quando la finestra non e' aperta.

        Misurato: con nessuna finestra dell'app sullo schermo l'estensione
        risponde ``false`` anche se la preferenza e' accesa. Lo stato
        mostrato deve venire dalla preferenza.
        """
        self.window.settings.always_on_top = True
        self.shell.state = False  # l'estensione dice "spento"
        self.window._sync_pin_button()
        self.assertTrue(
            self.window.pin_button.get_active(),
            "il tasto segue l'estensione invece della preferenza",
        )

    # -- quando l'estensione non c'e' --------------------------------------
    def test_without_the_extension_the_button_springs_back(self) -> None:
        self.window._shell = _FakeShell(works=False)
        self.window.settings.always_on_top = True
        self.window._sync_pin_button()
        self.window.pin_button.set_active(False)  # l'utente prova a spegnerlo
        self.assertTrue(
            self.window.pin_button.get_active(),
            "il tasto resta spento dopo un tentativo fallito: l'interfaccia "
            "mostra uno stato che non esiste",
        )

    def test_without_the_extension_the_preference_is_untouched(self) -> None:
        """Non si salva una preferenza che non ha effetto.

        Il percorso della riga salvava ``always_on_top=True`` anche quando
        la chiamata D-Bus era fallita: al riavvio la finestra non sarebbe
        stata sopra le altre nonostante l'interruttore acceso.
        """
        self.window._shell = _FakeShell(works=False)
        self.window.settings.always_on_top = True
        self.window.pin_button.set_active(False)
        self.assertTrue(self.window.settings.always_on_top)

    def test_without_the_extension_nothing_is_disabled(self) -> None:
        """Riprovare deve restare possibile.

        Il percorso della riga faceva ``set_sensitive(False)``: misurato,
        dopo un tentativo a vuoto l'interruttore restava spento per sempre,
        e installare l'estensione in seguito non serviva a niente.
        """
        self.window._shell = _FakeShell(works=False)
        self.window.settings.always_on_top = True
        self.window.always_on_top_row.set_active(False)
        self.window.always_on_top_row.set_active(True)
        self.assertTrue(self.window.always_on_top_row.get_sensitive())
        self.assertTrue(self.window.pin_button.get_sensitive())

    def test_retrying_after_installing_the_extension_works(self) -> None:
        """Il tasto non e' un interruttore usa-e-getta."""
        self.window._shell = _FakeShell(works=False)
        self.window.settings.always_on_top = True
        self.window.pin_button.set_active(False)  # fallisce, torna acceso
        # ora l'estensione c'e'
        self.window._shell = _FakeShell(works=True)
        self.window.pin_button.set_active(False)  # riprova: deve riuscire
        self.assertFalse(self.window.settings.always_on_top)
        self.assertFalse(self.shell.state if False else self.window.pin_button.get_active())


class _FakeExporter:
    """Sostituisce il vero Exporter senza mai toccare il disco.

    Non e' un ``mock.MagicMock``: registra quello che la finestra chiede
    (``render``) e quello che scrive (``export``), e risponde un testo
    comandato — cosi' si vede *se* e *quando* la finestra scrive.
    """

    def __init__(self) -> None:
        self.rendered = "contenuto di prova\n"
        self.export_calls: list[tuple[str, str]] = []

    def render(self, session_id, fmt, *, include_partial=False) -> str:
        return self.rendered

    def export(self, session_id, fmt, path) -> None:
        self.export_calls.append((fmt, str(path)))


class _SpyDialog:
    """FileDialog finto che non apre nulla e risponde un percorso comandato."""

    def __init__(self, path: str = "/tmp/export.txt") -> None:
        self.path = path
        self.initial_name = None

    def set_title(self, title: str) -> None:
        pass

    def set_accept_label(self, label: str) -> None:
        pass

    def set_initial_name(self, name: str) -> None:
        self.initial_name = name

    def save(self, *args) -> None:
        pass

    def save_finish(self, result):
        class _FakeFile:
            def get_path(self) -> str:
                return self_path

        self_path = self.path
        return _FakeFile()


@unittest.skipUnless(_HAVE_GTK, "serve GTK4/libadwaita")
class ExportMenuTest(unittest.TestCase):
    """L'esportazione SRT dev'essere raggiungibile dal menu e dall'anteprima.

    Il difetto di partenza: export.py conosce gia' il formato ``srt`` e lo
    sa rendere (``render`` + ``_render_srt``), ma il menu della finestra
    offriva solo txt/md/md_ts e la mappa dei suffissi ignorava ``srt``:
    un formato che esiste nel codice non e' un formato che l'utente puo'
    scegliere. E scrivere a colpo secco il contenuto, senza anteprima,
    metteva il file su disco prima ancora di guardarlo.
    """

    @classmethod
    def setUpClass(cls) -> None:
        Adw.init()

    def setUp(self) -> None:
        from lecture_transcriber.app import (
            PipelineCallbacks,
            PipelineConfig,
            TranscriptionPipeline,
        )
        from lecture_transcriber.config import Settings

        self.pipeline = TranscriptionPipeline(
            PipelineConfig(settings=Settings()), callbacks=PipelineCallbacks()
        )
        app = Gtk.Application(application_id="it.lecturetranscriber.TestSrt")
        self.window = uimod.MainWindow(app, self.pipeline)
        self.pipeline._exporter = _FakeExporter()
        self.pipeline.session.session_id = 1

    def _find(self, widget, kind):
        """Primo discendente di ``widget`` di tipo ``kind``, o ``None``."""
        if isinstance(widget, kind):
            return widget
        child = widget.get_first_child()
        while child is not None:
            found = self._find(child, kind)
            if found is not None:
                return found
            child = child.get_next_sibling()
        return None

    def _export_targets(self) -> list[str]:
        menu = self.window.export_button.get_menu_model()
        variant_type = GLib.VariantType.new("s")
        targets = []
        for i in range(menu.get_n_items()):
            variant = menu.get_item_attribute_value(i, "target", variant_type)
            if variant is not None:
                targets.append(variant.unpack())
        return targets

    def test_export_menu_contains_srt(self) -> None:
        """Il menu dell'esportazione deve offrire i sottotitoli .srt."""
        menu = self.window.export_button.get_menu_model()
        self.assertIsInstance(menu, Gio.Menu)
        self.assertIn("srt", self._export_targets())
        # il formato dichiarato al menu e quello della lista interna coincidono
        self.assertIn("srt", self.window._export_formats)

    def test_suffix_map_has_srt(self) -> None:
        """Il nome proposto dal dialogo deve finire in .srt, non cadere su .txt."""
        spy = _SpyDialog()
        original = uimod.Gtk.FileDialog
        uimod.Gtk.FileDialog = lambda: spy
        try:
            self.window.export("srt")
        finally:
            uimod.Gtk.FileDialog = original
        self.assertEqual(spy.initial_name, "trascrizione.srt")
        self.assertEqual(self.window._pending_export_fmt, "srt")

    def test_export_ready_builds_a_preview(self) -> None:
        """Dopo il dialogo appare un'anteprima con il testo da scrivere."""
        self.pipeline.exporter.rendered = (
            "1\n00:00:00,000 --> 00:00:02,000\nCiao mondo.\n\n"
        )
        self.window._on_export_ready(_SpyDialog(path="/tmp/lezione.srt"), None)
        preview = getattr(self.window, "_export_preview", None)
        self.assertIsNotNone(preview)
        self.assertIsInstance(preview, Adw.PreferencesWindow)
        self.assertEqual(preview.get_title(), "Anteprima esportazione")
        # il testo anticipato e' davvero nel buffer, in sola lettura
        text_view = self._find(preview, Gtk.TextView)
        self.assertIsNotNone(text_view)
        self.assertFalse(text_view.get_editable())
        buffer = text_view.get_buffer()
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
        self.assertIn("Ciao mondo", text)
        confirm = self._find(preview, Gtk.Button)
        self.assertIsNotNone(confirm)
        self.assertEqual(confirm.get_label(), "Conferma salvataggio")

    def test_dismissing_the_preview_writes_nothing(self) -> None:
        """Chiudere l'anteprima senza confermare non deve scrivere nulla."""
        self.window._on_export_ready(_SpyDialog(path="/tmp/lezione.srt"), None)
        self.window._export_preview.destroy()
        self.assertEqual(self.pipeline.exporter.export_calls, [])

    def test_confirming_the_preview_writes_and_closes(self) -> None:
        """La conferma e' la sola strada che mette il file su disco."""
        self.window._pending_export_fmt = "srt"
        self.window._on_export_ready(_SpyDialog(path="/tmp/lezione.srt"), None)
        confirm = self._find(self.window._export_preview, Gtk.Button)
        confirm.emit("clicked")
        self.assertEqual(
            self.pipeline.exporter.export_calls, [("srt", "/tmp/lezione.srt")]
        )
        self.assertIsNone(getattr(self.window, "_export_preview", None))


@unittest.skipUnless(_HAVE_GTK, "serve GTK4/libadwaita")
class RejoinAndStateTest(unittest.TestCase):
    """La pillola di ritorno e la seconda capsula di stato sul transcript.

    Due difetti che questo collaudo deve sorvegliare:

    * lo scorrimento automatico era una preferenza globale: se attiva, un
      nuovo segmento strappava il viewport in fondo anche quando l'utente
      era tornato indietro a rileggere — impossibile seguire il testo con
      le dita. Ora e' uno stato *derivato* ``_following``: si segue finche'
      il viewport sta dentro una tolleranza di 30 px dal fondo, oltre si
      smette e compare una pillola "Torna all'ultimo testo".
    * lo stato viveva solo nella barra superiore: mentre un'ipotesi
      provvisoria scorre (non finalizzata) niente diceva che il microfono
      sta ancora "trascrivendo". Qui si controlla la seconda capsula.

    I test guidano il ``vadjustment`` a mano, perche' in un ambiente senza
    display ``scroll_to_end()`` non sposta davvero il valore del viewport
    (misurato: resta 0.0): l'unico modo deterministico di esercitare la
    tolleranza e' scrivere la posizione del viewport direttamente.

    Attenzione pero': su uno ``ScrolledWindow`` non realizzato (altezza 0)
    quei valori non restano quelli scritti — misurato, ``set_value(500)``
    tornava 8.0, perche' GTK ricalcola ``upper`` e ``page_size`` dal
    contenuto. Percio' la tolleranza si collauda in due pezzi: l'aritmetica
    pura su :func:`_within_rejoin_tolerance`, e il cablaggio col widget
    (legge l'aggiustamento, muove la pillola) attraverso un sostituto
    dell'aggiustamento. Non e' un mock della finestra: il widget, il
    ``Revealer``, lo stato ``_following`` e il gestore del click sono
    quelli veri.
    """

    @classmethod
    def setUpClass(cls) -> None:
        Adw.init()

    def setUp(self) -> None:
        from lecture_transcriber.app import (
            PipelineCallbacks,
            PipelineConfig,
            TranscriptionPipeline,
        )
        from lecture_transcriber.config import Settings

        self.pipeline = TranscriptionPipeline(
            PipelineConfig(settings=Settings()), callbacks=PipelineCallbacks()
        )
        app = Gtk.Application(application_id="it.lecturetranscriber.TestUi")
        self.window = uimod.MainWindow(app, self.pipeline)

    def _process_idle(self) -> None:
        """Svuota gli idle programmer (quelli di ``_schedule_rejoin_update``)."""
        while GLib.MainContext.default().pending():
            GLib.MainContext.default().iteration(False)

    def _put_viewport(self, value: float, upper: float, page: float) -> None:
        """Sostituisce la lettura dell'aggiustamento con numeri scelti.

        ``_update_rejoin`` chiede ``value/upper/page_size`` al
        ``vadjustment``: qui si mette in mezzo un oggetto con quei tre
        numeri, e si lascia che il resto del codice giri com'e'. Cosi' il
        cablaggio vero (``_following``, il ``Revealer``) viene esercitato
        con una posizione deterministica, senza dipendere da un display.
        """
        window = self.window

        class _Viewport:
            def get_value(self_inner):
                return value

            def get_upper(self_inner):
                return upper

            def get_page_size(self_inner):
                return page

        scroller = window.view.scroller
        scroller.get_vadjustment = lambda: _Viewport()
        try:
            window._update_rejoin()
        finally:
            del scroller.get_vadjustment

    def _scroll_away(self) -> None:
        """Porta il viewport lontano dal fondo, dentro il collaudo."""
        self._put_viewport(500.0, 1000.0, 200.0)

    def _back_to_bottom(self) -> None:
        """Riporta il viewport in fondo, dentro la tolleranza."""
        self._put_viewport(800.0, 1000.0, 200.0)

    def _capsule_style(self) -> str:
        classes = self.window.transcript_state_capsule.get_css_classes()
        return next(
            (c for c in classes if c.startswith("lt-capsule-")), ""
        )

    def test_rejoin_pill_exists_and_is_hidden_by_default(self) -> None:
        """(a) La pillola c'e', ma non compare finche' si segue il fondo."""
        self.assertIsNotNone(self.window.rejoin_button)
        self.assertEqual(
            self.window.rejoin_button.get_label(), "Torna all'ultimo testo"
        )
        self.assertTrue(self.window.rejoin_revealer.get_child() is self.window.rejoin_button)
        self.assertFalse(self.window.rejoin_revealer.get_reveal_child())
        self.assertTrue(self.window._following)

    def test_scrolling_away_reveals_the_pill(self) -> None:
        """(b) Oltre la tolleranza di 30 px il viewport smette di seguire."""
        self._scroll_away()
        self.assertFalse(self.window._following)
        self.assertTrue(self.window.rejoin_revealer.get_reveal_child())

    def test_within_tolerance_keeps_following(self) -> None:
        """Dentro i 30 px di tolleranza la pillola non deve comparire."""
        self._put_viewport(790.0, 1000.0, 200.0)  # 1000-200-10: dentro
        self.assertTrue(self.window._following)
        self.assertFalse(self.window.rejoin_revealer.get_reveal_child())

    def test_clicking_rejoin_resumes_following(self) -> None:
        """(c) Il click riprende a seguire e il fondo nasconde la pillola."""
        self._scroll_away()
        self.assertTrue(self.window.rejoin_revealer.get_reveal_child())
        self.window._on_rejoin(None)
        # il click ripristina lo stato di seguito a prescindere
        self.assertTrue(self.window._following)
        # e il viewport tornato in tolleranza nasconde la pillola
        self._back_to_bottom()
        self.assertTrue(self.window._following)
        self.assertFalse(self.window.rejoin_revealer.get_reveal_child())

    def test_transcript_state_capsule_reflects_the_state_map(self) -> None:
        """(d) La seconda capsula segue la stessa mappa della barra superiore."""
        from lecture_transcriber.app import ListenerState

        self.pipeline.state = ListenerState.IDLE
        self.window._update_transcript_capsule()
        self.assertEqual(
            self.window.transcript_state_capsule.get_text(),
            self.window._STATE_CAPSULE[ListenerState.IDLE][0],
        )
        self.pipeline.state = ListenerState.LISTENING
        self.window._update_transcript_capsule()
        self.assertEqual(
            self.window.transcript_state_capsule.get_text(),
            self.window._STATE_CAPSULE[ListenerState.LISTENING][0],
        )

    def test_transcribing_state_shown_while_partial_and_listening(self) -> None:
        """La capsula vira su "trascrivendo" quando c'e' un'ipotesi in vista."""
        from lecture_transcriber.app import ListenerState

        self.pipeline.state = ListenerState.LISTENING
        self.window._on_partial("ipotesi provvisoria")
        self.assertEqual(
            self.window.transcript_state_capsule.get_text(), "trascrivendo"
        )
        self.assertEqual(self._capsule_style(), "lt-capsule-warning")
        # un segmento finalizzato sostituisce l'ipotesi: lo stato torna alla mappa
        self.window._on_segment(0, 0.0, "testo finalizzato")
        self.assertEqual(
            self.window.transcript_state_capsule.get_text(),
            self.window._STATE_CAPSULE[ListenerState.LISTENING][0],
        )

    def test_transcribing_not_shown_when_idle(self) -> None:
        """Un'ipotesi in vista senza ascolto non e' "trascrivendo"."""
        from lecture_transcriber.app import ListenerState

        self.pipeline.state = ListenerState.PAUSED
        self.window._on_partial("ipotesi in pausa")
        self.assertNotEqual(
            self.window.transcript_state_capsule.get_text(), "trascrivendo"
        )

    def test_pill_hides_once_back_at_bottom(self) -> None:
        """(e) Tornato al fondo, dentro la tolleranza, la pillola sparisce."""
        self._scroll_away()
        self.assertTrue(self.window.rejoin_revealer.get_reveal_child())
        self._back_to_bottom()
        self.assertTrue(self.window._following)
        self.assertFalse(self.window.rejoin_revealer.get_reveal_child())

    # -- la tolleranza, come aritmetica pura ------------------------------
    def test_tolerance_boundary_is_inclusive(self) -> None:
        """Esattamente a 30 px dal fondo si sta *seguendo*: il confronto e' >=."""
        # upper 1000, page 200 -> il fondo e' 800; 800 - 30 = 770
        self.assertTrue(
            uimod._within_rejoin_tolerance(770.0, 1000.0, 200.0),
            "esattamente sulla soglia dei 30 px il viewport segue ancora",
        )
        self.assertFalse(
            uimod._within_rejoin_tolerance(769.9, 1000.0, 200.0),
            "un pixel sotto la soglia e la pillola deve comparire",
        )

    def test_tolerance_empty_transcript_is_at_bottom(self) -> None:
        """Niente da scorrere: il viewport (0,0,0) sta al fondo.

        Senza questo, la pillola comparirebbe subito all'avvio, prima che
        una sola riga sia stata trascritta.
        """
        self.assertTrue(uimod._within_rejoin_tolerance(0.0, 0.0, 0.0))

    def test_tolerance_when_content_shorter_than_viewport(self) -> None:
        """Contenuto piu' corto del viewport (upper < page): si segue."""
        self.assertTrue(uimod._within_rejoin_tolerance(0.0, 150.0, 200.0))


if __name__ == "__main__":
    unittest.main()
