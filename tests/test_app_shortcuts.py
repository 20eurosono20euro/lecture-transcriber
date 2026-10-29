"""Test del collegamento fra le scorciatoie globali e l'applicazione.

Il meccanismo misurato su GNOME 50 e' questo:

    gnome-settings-daemon (media-keys)
        -> esegue il comando scritto in gsettings
            -> ``gdbus call ... it.lecturetranscriber.App.ActivateShortcut <id>``
                -> l'applicazione attiva l'azione corrispondente

I test coprono l'ultimo anello (la consegna) e la forma del comando
(che viene confrontata con l'interfaccia realmente esportata).
"""

from __future__ import annotations

import unittest
from unittest import mock

from lecture_transcriber import app as appmod
from lecture_transcriber import config, shortcuts_gsettings as sg


def _make_app():
    """Costruisce l'oggetto applicativo senza avviare il ciclo GTK.

    ``Gio.Application.__init__`` va' rifatto a mano perche' non vogliamo
    registrarci sul bus durante i test.
    """
    cls = appmod._make_application_class()
    instance = cls.__new__(cls)
    instance.window = None
    instance._shortcuts = None
    instance._gsettings_shortcuts = None
    instance._dbus_registration = None
    return instance


class TestShortcutDispatch(unittest.TestCase):
    """La consegna di una scorciatoia all'azione corrispondente."""

    def setUp(self) -> None:
        from gi.repository import GLib

        self._glib = GLib
        self.app = _make_app()
        self.recorded: list[str] = []
        for name in (
            "_toggle_listen",
            "_toggle_insert",
            "_toggle_opacity",
            "_toggle_window",
            "_bookmark_last",
            "_jump_to_latest",
        ):
            setattr(self.app, name, self._recorder(name))

    def _recorder(self, name: str):
        def handler(*_args, **_kwargs):
            self.recorded.append(name)

        return handler

    def _activate(self, shortcut_id: str) -> None:
        self.app._on_activate_shortcut(
            None, self._glib.Variant("s", shortcut_id)
        )

    def test_known_shortcut_is_dispatched(self) -> None:
        self._activate("toggle_listen")
        self.assertEqual(self.recorded, ["_toggle_listen"])

    def test_every_known_id_maps_to_a_handler(self) -> None:
        for sid in sg.KNOWN_SHORTCUT_IDS:
            with self.subTest(sid=sid):
                self.recorded.clear()
                self._activate(sid)
                self.assertEqual(len(self.recorded), 1)

    def test_unknown_id_is_ignored(self) -> None:
        self._activate("inesistente")
        self.assertEqual(self.recorded, [])

    def test_none_parameter_is_ignored(self) -> None:
        self.app._on_activate_shortcut(None, None)
        self.assertEqual(self.recorded, [])

    def test_handler_failure_does_not_propagate(self) -> None:
        """Un'azione che fallisce non deve far cadere l'applicazione."""
        def boom():
            raise RuntimeError("errore simulato")

        self.app._toggle_listen = boom
        self._activate("toggle_listen")  # non deve sollevare


class TestCommandMatchesInterface(unittest.TestCase):
    """Il comando scritto in gsettings deve puntare a un'interfaccia reale."""

    def test_command_targets_the_app_interface(self) -> None:
        shortcut = sg.GSettingsShortcut("toggle_listen", "<Ctrl>A", "")
        self.assertIn(config.APP_ID, shortcut.command)
        self.assertIn(sg.APP_METHOD, shortcut.command)

    def test_object_path_is_the_application_path(self) -> None:
        """``GApplication`` espone gli oggetti in ``/<app-id con / >``."""
        self.assertEqual(
            sg.APP_OBJECT_PATH, "/" + config.APP_ID.replace(".", "/")
        )

    def test_interface_name_is_the_application_id(self) -> None:
        """L'interfaccia esportata ha lo stesso nome del bus name."""
        self.assertEqual(sg.APP_INTERFACE, config.APP_ID)


class TestGSettingsPreferredOverPortal(unittest.TestCase):
    """Il percorso GSettings ha la precedenza sul portal."""

    def test_gsettings_used_when_available(self) -> None:
        app = _make_app()
        fake_manager = mock.MagicMock()
        fake_manager.register.return_value = True

        with mock.patch.dict(
            "sys.modules",
            {"lecture_transcriber.shortcuts_gsettings": mock.MagicMock(
                GSettingsShortcutManager=lambda: fake_manager
            )},
        ):
            with mock.patch(
                "lecture_transcriber.shortcuts.ShortcutManager"
            ) as portal:
                app._install_global_shortcuts()

        self.assertIs(app._gsettings_shortcuts, fake_manager)
        fake_manager.register.assert_called_once()
        # il portal non deve essere nemmeno istanziato
        portal.assert_not_called()

    def test_portal_used_as_fallback(self) -> None:
        app = _make_app()
        fake_manager = mock.MagicMock()
        fake_manager.register.return_value = False

        fake_portal = mock.MagicMock()
        with mock.patch.dict(
            "sys.modules",
            {"lecture_transcriber.shortcuts_gsettings": mock.MagicMock(
                GSettingsShortcutManager=lambda: fake_manager
            )},
        ):
            with mock.patch(
                "lecture_transcriber.shortcuts.ShortcutManager",
                return_value=fake_portal,
            ):
                app._install_global_shortcuts()

        self.assertIsNone(app._gsettings_shortcuts)
        self.assertIs(app._shortcuts, fake_portal)
        fake_portal.register.assert_called_once()

    def test_all_six_shortcuts_are_registered(self) -> None:
        app = _make_app()
        fake_manager = mock.MagicMock()
        fake_manager.register.return_value = True

        with mock.patch.dict(
            "sys.modules",
            {"lecture_transcriber.shortcuts_gsettings": mock.MagicMock(
                GSettingsShortcutManager=lambda: fake_manager
            )},
        ):
            app._install_global_shortcuts()

        registered = fake_manager.register.call_args[0][0]
        self.assertEqual(set(registered), set(sg.KNOWN_SHORTCUT_IDS))

    def test_registered_ids_match_the_dispatchable_ones(self) -> None:
        """Gli id registrati e quelli che la D-Bus sa attivare coincidono.

        Se i due elenchi divergessero, una scorciatoia registrata
        arriverebbe all'app e non farebbe nulla.
        """
        app = _make_app()
        app._toggle_listen = lambda: None
        app._toggle_insert = lambda: None
        app._toggle_opacity = lambda: None
        app._toggle_window = lambda: None
        app._bookmark_last = lambda: None
        app._jump_to_latest = lambda: None

        fake_manager = mock.MagicMock()
        fake_manager.register.return_value = True
        with mock.patch.dict(
            "sys.modules",
            {"lecture_transcriber.shortcuts_gsettings": mock.MagicMock(
                GSettingsShortcutManager=lambda: fake_manager
            )},
        ):
            app._install_global_shortcuts()

        registered = set(fake_manager.register.call_args[0][0])
        dispatchable = set(app._shortcut_handlers())
        self.assertEqual(registered, dispatchable)
        self.assertEqual(dispatchable, set(sg.KNOWN_SHORTCUT_IDS))


if __name__ == "__main__":
    unittest.main()
