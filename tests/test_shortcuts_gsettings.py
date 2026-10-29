"""Test del percorso delle scorciatoie globali via GSettings (``media-keys``).

Il percorso GSettings e' quello che funziona davvero su GNOME 50: il
portal ``org.freedesktop.portal.GlobalShortcuts`` pretende un app-id e
apre un dialogo di conferma che blocca (misurato su Fedora 44 / GNOME
50.6).

I test non scrivono mai nelle impostazioni reali: ``gsettings`` viene
sostituito da un falso che mantiene lo stato in memoria.
"""

from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from lecture_transcriber import shortcuts_gsettings as sg


class FakeGSettings:
    """Doppione in memoria di ``gsettings``.

    Tiene lo stato per schema e per percorso, cosi' i test possono
    verificare cosa verrebbe scritto davvero nel sistema.
    """

    def __init__(self) -> None:
        # chiave -> valore grezzo
        self.values: dict[str, str] = {}
        # chiave -> codice di uscita
        self.failures: set[str] = set()
        self.calls: list[list[str]] = []

    def _key(self, args: list[str]) -> str:
        # "set <schema> <key> <value>" oppure "set <schema>:<path> <key> <value>"
        if args[0] == "set":
            return f"{args[1]}::{args[2]}"
        return f"{args[1]}::{args[2]}"

    def run(self, args: list[str]) -> tuple[int, str, str]:
        self.calls.append(list(args))
        verb = args[0]

        if verb == "list-keys":
            if "media-keys" in args[1]:
                return 0, "custom-keybindings\n", ""
            return 1, "", "schema non installato"

        if verb == "get":
            schema, key = args[1], args[2]
            if schema in self.failures or f"{schema}::{key}" in self.failures:
                return 1, "", "errore simulato"
            value = self.values.get(f"{schema}::{key}")
            if value is None:
                return 0, "@as []", ""
            return 0, value, ""

        if verb == "set":
            schema, key = args[1], args[2]
            if schema in self.failures or f"{schema}::{key}" in self.failures:
                return 1, "", "errore simulato"
            self.values[f"{schema}::{key}"] = args[3]
            return 0, "", ""

        return 1, "", f"verbo sconosciuto: {verb}"


def _patch(fake: FakeGSettings):
    return mock.patch.object(
        sg, "_gsettings", side_effect=lambda args: fake.run(list(args))
    )


class TestParseStrv(unittest.TestCase):
    """La rappresentazione delle liste di ``gsettings`` e' variabile."""

    def test_plain_list(self) -> None:
        self.assertEqual(sg._parse_strv("['a', 'b']"), ["a", "b"])

    def test_typed_prefix_is_stripped(self) -> None:
        # ``gsettings get`` a volte antepone il tipo della lista.
        self.assertEqual(sg._parse_strv("@as ['a']"), ["a"])
        self.assertEqual(sg._parse_strv("@as []"), [])

    def test_empty(self) -> None:
        self.assertEqual(sg._parse_strv("[]"), [])
        self.assertEqual(sg._parse_strv(""), [])

    def test_double_quotes(self) -> None:
        self.assertEqual(sg._parse_strv('["a", "b"]'), ["a", "b"])


class TestPathValidation(unittest.TestCase):
    """La validazione dei percorsi evita il crash di ``gsd-media-keys``.

    ``gsd-media-keys`` chiama ``g_settings_new_with_path()`` senza
    validare il percorso: con un percorso non valido la funzione
    restituisce ``NULL`` e il plugin va in dereferenza di puntatore
    nullo (SIGSEGV), portandosi dietro tutte le scorciatoie di GNOME.
    """

    def test_valid_path_is_accepted(self) -> None:
        self.assertTrue(
            sg._is_valid_path(
                "/org/gnome/settings-daemon/plugins/media-keys/"
                "custom-keybindings/toggle_listen/"
            )
        )

    def test_typed_list_artefact_is_rejected(self) -> None:
        """Il caso che ha fatto crashare gsd-media-keys."""
        self.assertFalse(sg._is_valid_path("@as []"))
        self.assertFalse(sg._is_valid_path("@as ['']"))

    def test_empty_and_root_are_rejected(self) -> None:
        self.assertFalse(sg._is_valid_path(""))
        self.assertFalse(sg._is_valid_path("/"))

    def test_relative_path_is_rejected(self) -> None:
        self.assertFalse(sg._is_valid_path("custom-keybindings/x/"))

    def test_path_outside_the_root_is_rejected(self) -> None:
        self.assertFalse(sg._is_valid_path("/org/gnome/other/thing/"))
        self.assertFalse(sg._is_valid_path("/etc/passwd"))

    def test_path_with_whitespace_is_rejected(self) -> None:
        self.assertFalse(sg._is_valid_path("/org/gnome/a b/"))
        self.assertFalse(sg._is_valid_path("/org/gnome/\tx/"))

    def test_path_with_empty_segment_is_rejected(self) -> None:
        self.assertFalse(sg._is_valid_path("/org//gnome/"))

    def test_trailing_root_only_is_rejected(self) -> None:
        self.assertFalse(
            sg._is_valid_path(
                "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"
            )
        )


class TestShortcutDefinition(unittest.TestCase):
    """La definizione di una scorciatoia e il comando che la attiva."""

    def test_path_uses_id(self) -> None:
        s = sg.GSettingsShortcut("bookmark", "<Ctrl>B", "Segnalibro")
        self.assertTrue(
            s.gsettings_path.endswith("/custom-keybindings/bookmark/"),
            s.gsettings_path,
        )

    def test_command_calls_the_app(self) -> None:
        s = sg.GSettingsShortcut("toggle_listen", "<Ctrl>A", "")
        cmd = s.command
        self.assertIn(f"--dest {sg.APP_BUS_NAME}", cmd)
        self.assertIn(f"--object-path {sg.APP_OBJECT_PATH}", cmd)
        self.assertIn(f"{sg.APP_INTERFACE}.{sg.APP_METHOD}", cmd)
        self.assertTrue(cmd.endswith("toggle_listen"), cmd)

    def test_app_identifiers_match_config(self) -> None:
        """Il comando scritto in gsettings deve puntare all'app reale."""
        from lecture_transcriber import config

        self.assertEqual(sg.APP_BUS_NAME, config.APP_ID)
        # il percorso oggetto e' la versione con "/" del nome bus
        self.assertEqual(
            sg.APP_OBJECT_PATH, "/" + config.APP_ID.replace(".", "/")
        )
        self.assertEqual(sg.APP_INTERFACE, config.APP_ID)


class TestRegistration(unittest.TestCase):
    """Registrazione e rimozione delle scorciatoie."""

    def setUp(self) -> None:
        self.fake = FakeGSettings()
        self.patcher = _patch(self.fake)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.manager = sg.GSettingsShortcutManager()

    def test_is_available(self) -> None:
        self.assertTrue(self.manager.is_available)

    def test_register_writes_path_and_details(self) -> None:
        with mock.patch.object(
            self.manager, "accelerator_for", return_value="<Ctrl><Super>A"
        ):
            ok = self.manager.register_single("toggle_listen", "Ascolta")

        self.assertTrue(ok, self.manager.last_error)
        paths = sg._parse_strv(
            self.fake.values["org.gnome.settings-daemon.plugins.media-keys::custom-keybindings"]
        )
        self.assertEqual(len(paths), 1)
        self.assertIn("toggle_listen", paths[0])

        base = "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding"
        prefix = f"{base}:{paths[0]}"
        self.assertEqual(self.fake.values[f"{prefix}::name"], "Ascolta")
        self.assertEqual(self.fake.values[f"{prefix}::binding"], "<Ctrl><Super>A")
        self.assertIn("ActivateShortcut", self.fake.values[f"{prefix}::command"])

    def test_register_keeps_user_shortcuts(self) -> None:
        """Le scorciatoie personalizzate dell'utente non vanno toccate."""
        user_path = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/mio/"
        self.fake.values[
            "org.gnome.settings-daemon.plugins.media-keys::custom-keybindings"
        ] = f"['{user_path}']"

        with mock.patch.object(
            self.manager, "accelerator_for", return_value="<Ctrl>A"
        ):
            self.manager.register_single("toggle_listen", "Ascolta")

        paths = sg._parse_strv(
            self.fake.values["org.gnome.settings-daemon.plugins.media-keys::custom-keybindings"]
        )
        self.assertIn(user_path, paths)
        self.assertEqual(len(paths), 2)

    def test_unregister_removes_only_ours(self) -> None:
        user_path = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/mio/"
        self.fake.values[
            "org.gnome.settings-daemon.plugins.media-keys::custom-keybindings"
        ] = f"['{user_path}']"

        with mock.patch.object(
            self.manager, "accelerator_for", return_value="<Ctrl>A"
        ):
            self.manager.register_single("toggle_listen", "Ascolta")

        self.manager.unregister()

        paths = sg._parse_strv(
            self.fake.values["org.gnome.settings-daemon.plugins.media-keys::custom-keybindings"]
        )
        self.assertEqual(paths, [user_path])

    def test_unregister_cleans_orphans_from_previous_run(self) -> None:
        """Un manager nuovo deve ripulire le scorciatoie dell'app."""
        stale = (
            "/org/gnome/settings-daemon/plugins/media-keys/"
            "custom-keybindings/toggle_listen/"
        )
        self.fake.values[
            "org.gnome.settings-daemon.plugins.media-keys::custom-keybindings"
        ] = f"['{stale}']"

        self.manager.unregister()

        paths = sg._parse_strv(
            self.fake.values["org.gnome.settings-daemon.plugins.media-keys::custom-keybindings"]
        )
        self.assertEqual(paths, [])

    def test_failure_is_reported_not_raised(self) -> None:
        self.fake.failures.add("org.gnome.settings-daemon.plugins.media-keys")
        with mock.patch.object(
            self.manager, "accelerator_for", return_value="<Ctrl>A"
        ):
            ok = self.manager.register_single("toggle_listen", "Ascolta")
        self.assertFalse(ok)
        self.assertIsNotNone(self.manager.last_error)

    def test_missing_accelerator_fails(self) -> None:
        with mock.patch.object(self.manager, "accelerator_for", return_value=""):
            ok = self.manager.register_single("toggle_listen", "Ascolta")
        self.assertFalse(ok)
        self.assertIn("toggle_listen", self.manager.last_error or "")

    def test_invalid_path_in_system_is_scrubbed(self) -> None:
        """Una lista corrotta non deve essere riscritta cosi' com'e'.

        Se nel ``media-keys`` c'e' un percorso non valido (per esempio
        il residuo ``@as []`` di un parser errato), riscrivere la lista
        cosi' com'e' farebbe crashare ``gsd-media-keys``.
        """
        self.fake.values[
            "org.gnome.settings-daemon.plugins.media-keys::custom-keybindings"
        ] = "['@as []']"

        with mock.patch.object(
            self.manager, "accelerator_for", return_value="<Ctrl>A"
        ):
            ok = self.manager.register_single("toggle_listen", "Ascolta")

        self.assertTrue(ok, self.manager.last_error)
        written = self.fake.values[
            "org.gnome.settings-daemon.plugins.media-keys::custom-keybindings"
        ]
        self.assertNotIn("@as", written)
        paths = sg._parse_strv(written)
        self.assertTrue(all(sg._is_valid_path(p) for p in paths), paths)

    def test_unregister_scrubs_invalid_paths(self) -> None:
        self.fake.values[
            "org.gnome.settings-daemon.plugins.media-keys::custom-keybindings"
        ] = "['@as []', '/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/mio/']"

        self.manager.unregister()

        written = self.fake.values[
            "org.gnome.settings-daemon.plugins.media-keys::custom-keybindings"
        ]
        self.assertNotIn("@as", written)
        paths = sg._parse_strv(written)
        self.assertTrue(all(sg._is_valid_path(p) for p in paths), paths)


class TestAcceleratorLookup(unittest.TestCase):
    """L'acceleratore arriva dalle preferenze dell'applicazione."""

    def test_reads_from_settings(self) -> None:
        manager = sg.GSettingsShortcutManager()
        from lecture_transcriber import config

        expected = config.Settings().shortcuts.get("toggle_listen", "")
        self.assertEqual(manager.accelerator_for("toggle_listen"), expected)

    def test_unknown_id_is_empty(self) -> None:
        manager = sg.GSettingsShortcutManager()
        self.assertEqual(manager.accelerator_for("inesistente"), "")


class TestRealEnvironment(unittest.TestCase):
    """Verifica sull'ambiente reale: leggere, non scrivere."""

    def test_schema_is_installed(self) -> None:
        """Il sistema deve avere lo schema ``media-keys``."""
        manager = sg.GSettingsShortcutManager()
        self.assertTrue(
            manager.is_available,
            "lo schema org.gnome.settings-daemon.plugins.media-keys "
            "non e' installato: le scorciatoie globali non funzioneranno",
        )

    def test_gsettings_binary_exists(self) -> None:
        result = subprocess.run(
            ["gsettings", "--version"], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
