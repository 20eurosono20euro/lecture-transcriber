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
    from gi.repository import Adw, Gtk

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


if __name__ == "__main__":
    unittest.main()
