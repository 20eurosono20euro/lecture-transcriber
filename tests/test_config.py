"""Test delle preferenze (round-trip, chiavi ignote, scorciatoie).

Il modulo ``config`` usa percorsi globali (``CONFIG_DIR``,
``SETTINGS_FILE``); i test li sostituiscono con directory temporanee,
cosi' le preferenze reali dell'utente non vengono mai toccate.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

# rende importabile ``lecture_transcriber`` (layout src/) e installa la
# shim di compatibilita' per soxr, anche sotto ``python -m unittest``
import _bootstrap  # noqa: F401,E402  (importa dopo l'inserimento nel path)

from lecture_transcriber import config as config_module
from lecture_transcriber.config import Settings, load_settings, save_settings


class ConfigTestCase(unittest.TestCase):
    """Base: reindirizza CONFIG_DIR e SETTINGS_FILE in una dir temporanea."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="lt-config-")
        self.config_dir = Path(self._tmp.name) / "config"
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.settings_file = self.config_dir / "settings.json"

        self._patchers = [
            mock.patch.object(config_module, "CONFIG_DIR", self.config_dir),
            mock.patch.object(config_module, "SETTINGS_FILE", self.settings_file),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self) -> None:
        for p in self._patchers:
            p.stop()
        self._tmp.cleanup()


class TestRoundTrip(ConfigTestCase):
    """Salvataggio e ricaricamento delle preferenze."""

    def test_save_then_load_returns_the_same_settings(self) -> None:
        original = Settings(
            model_name="small",
            compute_type="int8",
            language="it",
            opacity=0.5,
            font_size=18,
            insert_enabled=True,
            record_audio=True,
        )
        save_settings(original)

        loaded = load_settings()

        self.assertEqual(loaded.model_name, "small")
        self.assertEqual(loaded.opacity, 0.5)
        self.assertEqual(loaded.font_size, 18)
        self.assertTrue(loaded.insert_enabled)
        self.assertTrue(loaded.record_audio)

    def test_saved_file_is_valid_json(self) -> None:
        save_settings(Settings(model_name="medium"))
        raw = json.loads(self.settings_file.read_text(encoding="utf-8"))
        self.assertEqual(raw["model_name"], "medium")
        self.assertIn("shortcuts", raw)

    def test_save_is_atomic_and_leaves_no_temp_file(self) -> None:
        save_settings(Settings())
        leftovers = list(self.config_dir.glob("*.tmp"))
        self.assertEqual(leftovers, [], "resta un file temporaneo")

    def test_load_without_file_returns_defaults(self) -> None:
        self.assertFalse(self.settings_file.exists())
        loaded = load_settings()
        self.assertEqual(loaded.model_name, Settings().model_name)
        self.assertFalse(loaded.insert_enabled)
        self.assertFalse(loaded.record_audio)

    def test_load_with_corrupted_file_returns_defaults(self) -> None:
        self.settings_file.write_text("{ questo non e' json", encoding="utf-8")
        loaded = load_settings()
        self.assertEqual(loaded.model_name, Settings().model_name)

    def test_load_with_empty_file_returns_defaults(self) -> None:
        self.settings_file.write_text("", encoding="utf-8")
        loaded = load_settings()
        self.assertEqual(loaded.model_name, Settings().model_name)

    def test_save_creates_the_config_directory(self) -> None:
        with mock.patch.object(config_module, "CONFIG_DIR", Path(self._tmp.name) / "nuovo"):
            save_settings(Settings())
        self.assertTrue((Path(self._tmp.name) / "nuovo").is_dir())


class TestUnknownKeys(ConfigTestCase):
    """Le chiavi sconosciute vengono ignorate, non fanno fallire il caricamento."""

    def test_unknown_keys_are_ignored(self) -> None:
        self.settings_file.write_text(
            json.dumps(
                {
                    "model_name": "tiny",
                    "chiave_inesistente": 42,
                    "un'altra_ignota": {"a": 1},
                }
            ),
            encoding="utf-8",
        )
        loaded = load_settings()
        self.assertEqual(loaded.model_name, "tiny")
        self.assertFalse(hasattr(loaded, "chiave_inesistente"))

    def test_unknown_keys_do_not_break_the_round_trip(self) -> None:
        self.settings_file.write_text(
            json.dumps({"model_name": "tiny", "ignota": 1}), encoding="utf-8"
        )
        loaded = load_settings()
        save_settings(loaded)
        raw = json.loads(self.settings_file.read_text(encoding="utf-8"))
        self.assertNotIn("ignota", raw)
        self.assertEqual(raw["model_name"], "tiny")

    def test_from_dict_filters_unknown_keys(self) -> None:
        s = Settings.from_dict({"font_size": 22, "campo_ignoto": "x"})
        self.assertEqual(s.font_size, 22)
        self.assertNotIn("campo_ignoto", s.to_json())

    def test_known_keys_with_wrong_type_do_not_crash(self) -> None:
        """Un tipo sbagliato non deve far fallire il caricamento."""
        self.settings_file.write_text(
            json.dumps({"font_size": "ventidue"}), encoding="utf-8"
        )
        loaded = load_settings()
        self.assertIsInstance(loaded, Settings)


class TestShortcuts(ConfigTestCase):
    """Le scorciatoie vengono unite, non sostituite."""

    def test_defaults_include_all_logical_shortcuts(self) -> None:
        s = Settings()
        for key in ("toggle_listen", "toggle_insert", "bookmark"):
            self.assertIn(key, s.shortcuts)

    def test_shortcuts_are_merged_not_replaced(self) -> None:
        """Una scorciatoia nuova non deve cancellare le altre."""
        self.settings_file.write_text(
            json.dumps({"shortcuts": {"toggle_listen": "<Ctrl><Alt><F1>"}}),
            encoding="utf-8",
        )
        loaded = load_settings()
        self.assertEqual(loaded.shortcuts["toggle_listen"], "<Ctrl><Alt><F1>")
        # le altre devono sopravvivere
        self.assertIn("toggle_insert", loaded.shortcuts)
        self.assertIn("bookmark", loaded.shortcuts)
        self.assertEqual(
            loaded.shortcuts["toggle_insert"], Settings().shortcuts["toggle_insert"]
        )

    def test_merged_shortcuts_are_saved_back(self) -> None:
        self.settings_file.write_text(
            json.dumps({"shortcuts": {"nuovo_tasto": "<Ctrl>K"}}, ensure_ascii=False),
            encoding="utf-8",
        )
        loaded = load_settings()
        save_settings(loaded)
        raw = json.loads(self.settings_file.read_text(encoding="utf-8"))
        self.assertEqual(raw["shortcuts"]["nuovo_tasto"], "<Ctrl>K")
        self.assertIn("toggle_listen", raw["shortcuts"])

    def test_empty_shortcuts_dict_keeps_the_defaults(self) -> None:
        s = Settings.from_dict({"shortcuts": {}})
        self.assertEqual(s.shortcuts, Settings().shortcuts)

    def test_missing_shortcuts_key_keeps_the_defaults(self) -> None:
        s = Settings.from_dict({"model_name": "tiny"})
        self.assertEqual(s.shortcuts, Settings().shortcuts)


class TestEnsureDirs(unittest.TestCase):
    """ensure_dirs() crea le directory previste."""

    def test_ensure_dirs_creates_all_directories(self) -> None:
        with tempfile.TemporaryDirectory(prefix="lt-dirs-") as tmp:
            root = Path(tmp)
            data = root / "data"
            cfg = root / "cfg"
            cache = root / "cache"
            models = root / "models"
            patches = [
                mock.patch.object(config_module, "DATA_DIR", data),
                mock.patch.object(config_module, "CONFIG_DIR", cfg),
                mock.patch.object(config_module, "CACHE_DIR", cache),
                mock.patch.object(config_module, "MODEL_DIR", models),
            ]
            for p in patches:
                p.start()
            try:
                config_module.ensure_dirs()
                for d in (data, cfg, cache, models):
                    self.assertTrue(d.is_dir(), f"{d} non e' stato creato")
            finally:
                for p in patches:
                    p.stop()


class TestPlatformHomeDirs(unittest.TestCase):
    """La scelta delle cartelle base e' consapevole della piattaforma.

    Le cartelle vengono calcolate da ``_platform_home_dirs`` (pura), quindi
    un test puo' simulare Windows/macOS senza toccare la macchina reale. Su
    Linux (la piattaforma di sviluppo) il default deve restare quello XDG
    di sempre: e' il comportamento che i test e gli utenti si aspettano.
    """

    def _dirs(self, platform: str, env: dict | None = None, home: str = "/home/utente"):
        return config_module._platform_home_dirs(
            platform, env or {}, Path(home)
        )

    def test_linux_default_is_xdg(self) -> None:
        d = self._dirs("linux")
        self.assertEqual(d["data"], Path("/home/utente/.local/share"))
        self.assertEqual(d["config"], Path("/home/utente/.config"))
        self.assertEqual(d["cache"], Path("/home/utente/.cache"))

    def test_linux_respects_xdg_env_overrides(self) -> None:
        d = self._dirs(
            "linux",
            {
                "XDG_DATA_HOME": "/tmp/xdg/data",
                "XDG_CONFIG_HOME": "/tmp/xdg/config",
                "XDG_CACHE_HOME": "/tmp/xdg/cache",
            },
        )
        self.assertEqual(d["data"], Path("/tmp/xdg/data"))
        self.assertEqual(d["config"], Path("/tmp/xdg/config"))
        self.assertEqual(d["cache"], Path("/tmp/xdg/cache"))

    def test_windows_uses_appdata(self) -> None:
        d = self._dirs(
            "win32",
            {"APPDATA": r"C:\Users\Marco\AppData\Roaming",
             "LOCALAPPDATA": r"C:\Users\Marco\AppData\Local"},
        )
        self.assertEqual(d["data"], Path(r"C:\Users\Marco\AppData\Roaming"))
        self.assertEqual(d["config"], Path(r"C:\Users\Marco\AppData\Roaming"))
        self.assertEqual(d["cache"], Path(r"C:\Users\Marco\AppData\Local"))

    def test_windows_falls_back_when_env_missing(self) -> None:
        # Su una macchina Linux non esiste ``WindowsPath``: la funzione
        # riceve un ``Path`` generico e continua con quello. Il punto da
        # verificare e' la SCELTA della cartella (AppData/Roaming per i
        # dati, AppData/Local per la cache), non i separatori: si controlla
        # che il percorso finisca con i segmenti giusti in modo OS-agnostico.
        d = self._dirs("win32", {}, home=r"C:\Users\Marco")
        self.assertEqual(d["data"].name, "Roaming")
        self.assertEqual(d["data"].parent.name, "AppData")
        self.assertEqual(d["cache"].name, "Local")
        self.assertEqual(d["cache"].parent.name, "AppData")
        # e che il fallback usi comunque la home passata
        self.assertIn("Marco", str(d["data"]))

    def test_macos_uses_application_support(self) -> None:
        d = self._dirs("darwin", {}, "/Users/Marco")
        self.assertEqual(
            d["data"], Path("/Users/Marco/Library/Application Support")
        )
        self.assertEqual(
            d["config"], Path("/Users/Marco/Library/Application Support")
        )
        self.assertEqual(d["cache"], Path("/Users/Marco/Library/Caches"))

    def test_platform_dirs_function_is_pure(self) -> None:
        # La funzione non deve leggere l'ambiente reale: ricevendo la
        # stessa mappa, deve dare lo stesso risultato anche se l'ambiente
        # del processo cambia (e' la proprieta' che rende i test di
        # portabilita' deterministici).
        env = {"XDG_DATA_HOME": "/tmp/xdg/data"}
        first = config_module._platform_home_dirs("linux", env, Path("/home/u"))
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": "/altrove"}, clear=False):
            second = config_module._platform_home_dirs("linux", env, Path("/home/u"))
        self.assertEqual(first, second)
        self.assertEqual(first["data"], Path("/tmp/xdg/data"))


class TestToJson(unittest.TestCase):
    """Serializzazione delle preferenze."""

    def test_to_json_is_parseable(self) -> None:
        raw = json.loads(Settings().to_json())
        self.assertIn("model_name", raw)
        self.assertIn("shortcuts", raw)

    def test_to_json_keeps_unicode(self) -> None:
        s = Settings()
        s.shortcuts["prova"] = "<Ctrl>àè"
        self.assertIn("àè", s.to_json())


if __name__ == "__main__":
    unittest.main()
