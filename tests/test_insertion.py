"""Test dei gate di inserimento del testo.

I gate sono la parte piu' delicata del modulo: la tastiera simulata e
gli appunti sono **globali**, quindi senza un controllo esplicito della
finestra attiva un segmento potrebbe finire in un'altra applicazione.
Questi test verificano che, disarmato o con il focus cambiato, il testo
**non** venga inserito.

Il percorso degli appunti viene esercitato solo se ``wl-copy`` esiste
(saltato altrimenti); i percorsi UNO / at-spi / ydotool non vengono
toccati perche' scriverebbero nelle applicazioni aperte dell'utente.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

# rende importabile ``lecture_transcriber`` (layout src/) e installa la
# shim di compatibilita' per soxr, anche sotto ``python -m unittest``
import _bootstrap  # noqa: F401,E402  (importa dopo l'inserimento nel path)

from lecture_transcriber import insertion
from lecture_transcriber.insertion import (
    InsertMethod,
    InsertResult,
    TextInserter,
    capabilities,
)


class TestCapabilities(unittest.TestCase):
    """capabilities() deve restituire una dict di bool."""

    def test_capabilities_returns_dict_of_bools(self) -> None:
        caps = capabilities()
        self.assertIsInstance(caps, dict)
        self.assertTrue(caps, "la dict non deve essere vuota")
        for key, value in caps.items():
            self.assertIsInstance(key, str)
            self.assertIsInstance(value, bool, f"{key} non e' un bool")

    def test_expected_keys_are_present(self) -> None:
        caps = capabilities()
        for key in ("libreoffice_uno", "atspi", "clipboard", "ydotool", "wtype"):
            self.assertIn(key, caps)

    def test_clipboard_capability_matches_the_system(self) -> None:
        caps = capabilities()
        self.assertEqual(caps["clipboard"], shutil.which("wl-copy") is not None)


class _WindowMixin:
    """Neutralizza il titolo della finestra reale nei test che non lo misurano.

    ``active_window_title()`` ora funziona davvero (prima era rotto e
    restituiva sempre ``""``): senza questo, un ``arm("Finestra di prova")``
    seguito da ``insert()`` verrebbe bloccato dal gate perche' la finestra
    attiva reale -- il terminale che esegue i test -- non e' "Finestra di
    prova". I test che misurano il gate del focus fanno il proprio mock
    esplicito.
    """

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(
            insertion, "active_window_title", return_value="Finestra di prova"
        )
        patcher.start()
        self.addCleanup(patcher.stop)


class TestGateDisarmed(_WindowMixin, unittest.TestCase):
    """Disarmato, il testo non deve essere inserito."""

    def setUp(self) -> None:
        super().setUp()
        self.inserter = TextInserter(method=InsertMethod.CLIPBOARD)

    def test_insert_is_skipped_when_disarmed(self) -> None:
        self.inserter.disarm()
        with mock.patch.object(
            insertion.ClipboardInserter, "insert", return_value=InsertResult(True, "clipboard")
        ) as mocked:
            result = self.inserter.insert("testo riservato")
        mocked.assert_not_called()
        self.assertFalse(result.ok)

    def test_disarm_is_the_default_state(self) -> None:
        """Alla costruzione l'inserter e' disarmato."""
        fresh = TextInserter(method=InsertMethod.CLIPBOARD)
        with mock.patch.object(
            insertion.ClipboardInserter, "insert", return_value=InsertResult(True, "clipboard")
        ) as mocked:
            fresh.insert("testo")
        mocked.assert_not_called()

    def test_armed_property_tracks_the_state(self) -> None:
        """La proprieta' `armed` riflette arm/disarm."""
        inserter = TextInserter(method=InsertMethod.CLIPBOARD)
        self.assertFalse(inserter.armed)
        inserter.arm("Finestra di prova")
        self.assertTrue(inserter.armed)
        inserter.disarm()
        self.assertFalse(inserter.armed)

    def test_disarm_then_arm_restores_the_insert(self) -> None:
        self.inserter.disarm()
        with mock.patch.object(
            insertion, "active_window_title", return_value="Finestra"
        ):
            self.inserter.arm()
        with mock.patch.object(
            insertion, "active_window_title", return_value="Finestra"
        ):
            with mock.patch.object(
                insertion.ClipboardInserter, "insert", return_value=InsertResult(True, "clipboard")
            ) as mocked:
                result = self.inserter.insert("testo")
        mocked.assert_called_once()
        self.assertTrue(result.ok)

    def test_empty_text_is_a_no_op(self) -> None:
        self.inserter.arm("Finestra di prova")
        with mock.patch.object(
            insertion.ClipboardInserter, "insert", return_value=InsertResult(True, "clipboard")
        ) as mocked:
            result = self.inserter.insert("")
        mocked.assert_not_called()
        self.assertTrue(result.ok)


class TestFocusMismatch(unittest.TestCase):
    """Con il focus cambiato, il testo non deve essere inserito."""

    def setUp(self) -> None:
        self.inserter = TextInserter(method=InsertMethod.CLIPBOARD)

    def test_insert_is_skipped_when_focus_does_not_match(self) -> None:
        self.inserter.arm("Finestra di prova")
        with mock.patch.object(
            insertion, "active_window_title", return_value="Un'altra finestra"
        ):
            with mock.patch.object(
                insertion.ClipboardInserter,
                "insert",
                return_value=InsertResult(True, "clipboard"),
            ) as mocked:
                result = self.inserter.insert("testo riservato")
        mocked.assert_not_called()
        self.assertFalse(result.ok)
        self.assertIn("Finestra attiva", result.message)

    def test_force_bypasses_the_focus_gate(self) -> None:
        self.inserter.arm("Finestra di prova")
        with mock.patch.object(
            insertion, "active_window_title", return_value="Un'altra finestra"
        ):
            with mock.patch.object(
                insertion.ClipboardInserter,
                "insert",
                return_value=InsertResult(True, "clipboard"),
            ) as mocked:
                result = self.inserter.insert("testo forzato", force=True)
        mocked.assert_called_once()
        self.assertTrue(result.ok)

    def test_matching_focus_allows_the_insert(self) -> None:
        self.inserter.arm("Finestra di prova")
        with mock.patch.object(
            insertion, "active_window_title", return_value="Finestra di prova"
        ):
            with mock.patch.object(
                insertion.ClipboardInserter,
                "insert",
                return_value=InsertResult(True, "clipboard"),
            ) as mocked:
                result = self.inserter.insert("testo")
        mocked.assert_called_once()
        self.assertTrue(result.ok)

    def test_focus_matches_is_true_without_a_registered_window(self) -> None:
        """Senza finestra registrata non c'e' nulla da confrontare."""
        self.inserter.disarm()
        self.assertTrue(self.inserter.focus_matches())

    def test_focus_matches_when_title_is_unavailable(self) -> None:
        """Se il titolo non e' ottenibile il gate **chiude**, non apre.

        Prima assumeva "stessa finestra" e tornava True: con at-spi che
        non risponde il titolo torna "" e il testo finiva nella finestra
        sbagliata. Un inserimento saltato si recupera premendo di nuovo
        il tasto, una nota di lezione scritta in un altro documento no.
        """
        self.inserter.arm("Finestra di prova")
        with mock.patch.object(insertion, "active_window_title", return_value=""):
            self.assertFalse(self.inserter.focus_matches())

    def test_insert_is_refused_when_title_is_unavailable(self) -> None:
        """Il gate chiuso blocca anche l'inserimento, non solo il confronto."""
        self.inserter.arm("Finestra di prova")
        with mock.patch.object(insertion, "active_window_title", return_value=""):
            with mock.patch.object(
                insertion.ClipboardInserter,
                "insert",
                return_value=InsertResult(True, "clipboard"),
            ) as mocked:
                result = self.inserter.insert("testo")
        mocked.assert_not_called()
        self.assertFalse(result.ok)

    def test_focus_comparison_ignores_surrounding_spaces(self) -> None:
        self.inserter.arm("Finestra di prova")
        with mock.patch.object(
            insertion, "active_window_title", return_value="  Finestra di prova  "
        ):
            self.assertTrue(self.inserter.focus_matches())

    def test_focus_mismatch_is_detected(self) -> None:
        self.inserter.arm("Finestra di prova")
        with mock.patch.object(
            insertion, "active_window_title", return_value="Altra cosa"
        ):
            self.assertFalse(self.inserter.focus_matches())


class TestMethodSelection(_WindowMixin, unittest.TestCase):
    """Il metodo scelto e' quello richiesto."""

    def test_none_method_never_inserts(self) -> None:
        inserter = TextInserter(method=InsertMethod.NONE)
        inserter.arm("Finestra di prova")
        result = inserter.insert("testo")
        self.assertFalse(result.ok)
        self.assertEqual(result.method, "none")

    def test_explicit_clipboard_method_uses_the_clipboard(self) -> None:
        inserter = TextInserter(method=InsertMethod.CLIPBOARD)
        inserter.arm("Finestra di prova")
        with mock.patch.object(
            insertion.ClipboardInserter, "insert", return_value=InsertResult(True, "clipboard")
        ) as mocked:
            result = inserter.insert("testo")
        mocked.assert_called_once_with("testo")
        self.assertTrue(result.ok)

    def test_auto_without_any_method_fails_cleanly(self) -> None:
        inserter = TextInserter(method=InsertMethod.AUTO)
        inserter.arm("Finestra di prova")
        with mock.patch.object(insertion, "capabilities", return_value={}):
            with mock.patch.object(
                insertion, "active_window_title", return_value="Finestra"
            ):
                result = inserter.insert("testo")
        self.assertFalse(result.ok)

    def test_auto_does_not_fall_back_to_clipboard_on_unreadable_title(self) -> None:
        """Titolo illeggibile: niente appunti, e lo si dice.

        Il ripiego sugli appunti sovrascrive il clipboard di qualunque
        applicazione sia in primo piano. Se non sappiamo nemmeno qual e'
        la finestra attiva, non sappiamo dove finira' il prossimo incolla
        dell'utente: meglio non inserire.
        """
        inserter = TextInserter(method=InsertMethod.AUTO)
        inserter.arm("Finestra di prova")
        with mock.patch.object(
            insertion, "capabilities", return_value={"clipboard": True}
        ):
            with mock.patch.object(
                insertion, "active_window_title", return_value=""
            ):
                with mock.patch.object(
                    insertion.ClipboardInserter,
                    "insert",
                    return_value=InsertResult(True, "clipboard"),
                ) as mocked:
                    result = inserter.insert("testo")
        mocked.assert_not_called()
        self.assertFalse(result.ok)
        self.assertIn("saltato", result.message)

    def test_insert_result_defaults(self) -> None:
        result = InsertResult(True, "clipboard")
        self.assertEqual(result.message, "")


class TestClipboardPath(_WindowMixin, unittest.TestCase):
    """Il percorso degli appunti, solo se ``wl-copy`` e' installato."""

    def setUp(self) -> None:
        super().setUp()
        self.inserter = TextInserter(method=InsertMethod.CLIPBOARD)
        self.inserter.arm("Finestra di prova")

    def _skip_if_no_wl_copy(self) -> None:
        if shutil.which("wl-copy") is None:
            self.skipTest("wl-copy non installato: percorso degli appunti saltato")

    def test_clipboard_without_wl_copy_fails_cleanly(self) -> None:
        with mock.patch.object(shutil, "which", return_value=None):
            result = self.inserter.insert("testo")
        self.assertFalse(result.ok)
        self.assertIn("wl-copy", result.message)

    def test_clipboard_copies_the_text(self) -> None:
        """Verifica end-to-end col vero wl-copy, poi rilegge con wl-paste.

        Il contenuto originale degli appunti viene salvato e ripristinato,
        per non lasciare tracce sulla sessione dell'utente. Il wl-copy
        che resta in vita a servire gli appunti viene chiuso con --clear
        a fine test (vedi _restore_clipboard).
        """
        self._skip_if_no_wl_copy()
        if shutil.which("wl-paste") is None:
            self.skipTest("wl-paste non installato: impossibile rileggere")

        original = self._paste(timeout=3)
        self.addCleanup(self._restore_clipboard, original)

        payload = "Testo di prova per gli appunti — lezione transcriber"
        result = self.inserter.insert(payload)

        self.assertTrue(result.ok, result.message)
        self.assertEqual(self._paste(timeout=3).decode("utf-8"), payload)

        # ``wl-copy`` fork-a e resta vivo a servire gli appunti: e' il suo
        # comportamento normale, ma dopo un test non deve lasciare processi
        # in giro. ``--clear`` libera il possesso degli appunti.
        self.addCleanup(self._clear_clipboard)

    @staticmethod
    def _clear_clipboard() -> None:
        try:
            subprocess.run(
                ["wl-copy", "--clear"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    @staticmethod
    def _paste(timeout: float = 3) -> bytes:
        """Legge gli appunti; se sono vuoti restituisce ``b""``.

        ``wl-paste`` resta appeso quando nessuno possiede gli appunti:
        non e' un errore, e' l'assenza di contenuto. Il timeout breve
        distingue i due casi senza aspettare secondi preziosi.
        """
        try:
            return subprocess.run(
                ["wl-paste", "--no-newline"],
                capture_output=True,
                timeout=timeout,
            ).stdout
        except subprocess.TimeoutExpired:
            return b""

    def _restore_clipboard(self, content: bytes) -> None:
        """Rimette negli appunti il contenuto che c'era prima del test.

        Se erano vuoti non si lancia nulla: un ``wl-copy`` con input vuoto
        fork-a e resterebbe vivo a possedere gli appunti, lasciando un
        processo orfano dopo la suite. Per svuotarli davvero si usa
        ``wl-copy --clear``.
        """
        try:
            if content:
                subprocess.run(
                    ["wl-copy", "--type", "text/plain"],
                    input=content,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                )
            else:
                subprocess.run(
                    ["wl-copy", "--clear"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                )
        except (OSError, subprocess.SubprocessError):
            pass

    def test_clipboard_is_actually_invoked(self) -> None:
        """Il subprocess wl-copy viene davvero eseguito."""
        self._skip_if_no_wl_copy()
        with mock.patch.object(
            insertion.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)
        ) as mocked:
            result = self.inserter.insert("testo")
        mocked.assert_called_once()
        args = mocked.call_args[0][0]
        self.assertEqual(args[0], "wl-copy")
        self.assertTrue(result.ok)


class TestActiveWindowTitle(unittest.TestCase):
    """active_window_title() non deve mai sollevare."""

    def test_active_window_title_returns_a_string(self) -> None:
        title = insertion.active_window_title()
        self.assertIsInstance(title, str)

    def test_active_window_title_survives_a_failing_gi(self) -> None:
        with mock.patch.dict(sys.modules, {"gi": None}):
            title = insertion.active_window_title()
        self.assertIsInstance(title, str)

    def test_active_window_title_reads_the_state_set(self) -> None:
        """Deve usare ``get_state_set()``, non ``get_state()``.

        ``get_state()`` non esiste in PyGObject 3.52: chiamandolo, ogni
        finestra finisce nel ``try/except`` e la funzione restituisce
        sempre ``""``. Il gate del focus, non potendo verificare,
        lascerebbe passare tutto. La finestra finta qui non ha
        ``get_state``, quindi se il codice tornasse a quel nome il test
        fallirebbe con AttributeError.
        """
        window = mock.MagicMock(spec=["get_state_set", "get_name"])
        window.get_state_set.return_value.contains.return_value = True
        window.get_name.return_value = "Finestra di prova"
        self.assertFalse(
            hasattr(window, "get_state"),
            "il finto non deve esporre get_state: e' il nome sbagliato",
        )

        # attiva il percorso reale con un desktop finto
        desktop = mock.MagicMock()
        desktop.get_child_count.return_value = 1
        desktop.get_child_at_index.return_value = mock.MagicMock(
            get_child_count=lambda: 1,
            get_child_at_index=lambda _i: window,
        )
        fake_atspi = mock.MagicMock()
        fake_atspi.get_desktop.return_value = desktop
        fake_atspi.StateType.ACTIVE = "active"
        with mock.patch.dict(
            sys.modules, {"gi.repository": mock.MagicMock(Atspi=fake_atspi)}
        ):
            title = insertion.active_window_title()

        self.assertEqual(title, "Finestra di prova")

    def test_active_window_title_ignores_inactive_windows(self) -> None:
        """Una finestra non attiva non deve essere restituita."""
        window = mock.MagicMock(spec=["get_state_set", "get_name"])
        window.get_state_set.return_value.contains.return_value = False
        window.get_name.return_value = "Finestra inattiva"

        desktop = mock.MagicMock()
        desktop.get_child_count.return_value = 1
        desktop.get_child_at_index.return_value = mock.MagicMock(
            get_child_count=lambda: 1,
            get_child_at_index=lambda _i: window,
        )
        fake_atspi = mock.MagicMock()
        fake_atspi.get_desktop.return_value = desktop
        with mock.patch.dict(
            sys.modules, {"gi.repository": mock.MagicMock(Atspi=fake_atspi)}
        ):
            title = insertion.active_window_title()

        self.assertEqual(title, "")


# ---------------------------------------------------------------------------
# at-spi: i tre bug trovati per ispezione su GNOME 50
# ---------------------------------------------------------------------------
class FakeState:
    """Stato at-spi finto: solo i flag che ci interessano.

    L'enum reale di PyGObject ha ``value_name`` =
    ``ATSPI_STATE_EDITABLE``: il confronto usa quella forma, cosi' il
    doppione resta allineato all'enum di sistema.
    """

    def __init__(self, **flags: bool) -> None:
        self._flags = flags

    def contains(self, state_type) -> bool:
        name = getattr(state_type, "value_name", None)
        if name is None:
            name = str(getattr(state_type, "value", state_type))
        key = name.removeprefix("ATSPI_STATE_").lower()
        return bool(self._flags.get(key, False))


class FakeNode:
    """Nodo dell'albero at-spi finto.

    Riproduce la forma reale misurata su PyGObject 3.52:
    ``get_interfaces()`` restituisce una **lista di stringhe**, non un
    oggetto con ``.contains()``.
    """

    def __init__(self, interfaces=(), children=(), state=None) -> None:
        self._interfaces = list(interfaces)
        self._children = list(children)
        self._state = state or FakeState()

    def get_interfaces(self):
        return self._interfaces

    def get_child_count(self):
        return len(self._children)

    def get_child_at_index(self, index):
        return self._children[index]

    def get_state_set(self):
        return self._state


class _FakeAtspi:
    """Sostituto di ``gi.repository.Atspi`` per i test."""

    class StateType:
        EDITABLE = "editable"
        SHOWING = "showing"
        ACTIVE = "active"


class TestAtspiEditableFilter(unittest.TestCase):
    """Il filtro EDITABLE/SHOWING: il bug che scriveva nel pannello GNOME.

    Misurato su GNOME 50: ~310 nodi espongono ``EditableText`` ma uno
    solo e' davvero modificabile, e sta con ``SHOWING=False``. Senza il
    doppio filtro l'inseritore avrebbe scritto nel pannello.
    """

    def _editable_state(self, **flags) -> FakeState:
        base = {"editable": True, "showing": True}
        base.update(flags)
        return FakeState(**base)

    def test_label_without_editable_is_rejected(self) -> None:
        """Una label con EditableText ma senza EDITABLE non va bene."""
        node = FakeNode(
            interfaces=["Accessible", "EditableText"],
            state=FakeState(editable=False, showing=True),
        )
        self.assertFalse(insertion.AtspiInserter._is_editable(node))

    def test_editable_but_not_showing_is_rejected(self) -> None:
        """Il campo del pannello GNOME: EDITABLE ma non mostrato."""
        node = FakeNode(
            interfaces=["Accessible", "EditableText"],
            state=self._editable_state(showing=False),
        )
        self.assertFalse(insertion.AtspiInserter._is_editable(node))

    def test_editable_and_showing_is_accepted(self) -> None:
        node = FakeNode(
            interfaces=["Accessible", "EditableText"],
            state=self._editable_state(),
        )
        self.assertTrue(insertion.AtspiInserter._is_editable(node))

    def test_a_node_without_editabletext_interface_is_rejected(self) -> None:
        """Lo stato non basta: serve anche l'interfaccia ``EditableText``.

        ``_is_editable`` guarda solo lo stato; il controllo
        sull'interfaccia sta in ``_find_editable_in``. Il test esercita la
        combinazione reale, che e' quella che gira nel programma.
        """
        node = FakeNode(interfaces=["Accessible"], state=self._editable_state())
        with mock.patch.dict(
            sys.modules, {"gi.repository": mock.MagicMock(Atspi=_FakeAtspi)}
        ):
            found = insertion.AtspiInserter()._find_editable_in(node)
        self.assertIsNone(found)

    def test_editable_state_alone_is_enough_for_is_editable(self) -> None:
        """``_is_editable`` guarda lo stato, non l'interfaccia.

        Se il nodo espone ``EditableText`` ma ha uno stato non editabile,
        viene rifiutato; se lo stato e' editabile, ``_is_editable`` dice
        si' -- l'interfaccia la verifica chi cerca nell'albero.
        """
        node = FakeNode(interfaces=[], state=self._editable_state())
        self.assertTrue(insertion.AtspiInserter._is_editable(node))

    def test_a_failing_node_does_not_raise(self) -> None:
        """get_state_set() che solleva deve dare False, non un'eccezione."""

        class Broken(FakeNode):
            def get_state_set(self):
                raise RuntimeError("nodo morto")

        self.assertFalse(insertion.AtspiInserter._is_editable(Broken()))


class TestAtspiTreeWalk(unittest.TestCase):
    """La ricerca nell'albero: lista di stringhe, non ``.contains()``."""

    def _patch_atspi(self):
        patcher = mock.patch.dict(
            sys.modules, {"gi.repository": mock.MagicMock(Atspi=_FakeAtspi)}
        )
        return patcher

    def test_returns_none_when_nothing_is_editable(self) -> None:
        root = FakeNode(interfaces=["Accessible"], children=[])
        self.assertIsNone(insertion.AtspiInserter()._find_editable_in(root))

    def test_skips_labels_and_finds_the_editable_descendant(self) -> None:
        """Riproduce la forma reale: label ovunque, un campo in fondo."""
        field = FakeNode(
            interfaces=["Accessible", "EditableText"],
            state=FakeState(editable=True, showing=True),
        )
        label = FakeNode(interfaces=["Accessible", "EditableText"])
        root = FakeNode(interfaces=["Accessible"], children=[label, field])

        with self._patch_atspi():
            found = insertion.AtspiInserter()._find_editable_in(root)
        self.assertIs(found, field)

    def test_interfaces_are_compared_as_a_plain_list(self) -> None:
        """``"EditableText" in iface`` non deve chiamare ``.contains()``.

        Se il codice tornasse a ``iface.contains(...)`` la lista Python
        solleverebbe AttributeError, inghiottito da ``except``: la
        ricerca morirebbe in silenzio. Questo test fallisce in quel caso.
        """
        seen = {}

        class Recording(FakeNode):
            def get_interfaces(self):
                seen["called"] = True
                return ["Accessible", "EditableText"]

        node = Recording(state=FakeState(editable=True, showing=True))
        with self._patch_atspi():
            found = insertion.AtspiInserter()._find_editable_in(node)
        self.assertTrue(seen.get("called"), "get_interfaces() non e' stato chiamato")
        self.assertIs(found, node)

    def test_a_child_that_raises_does_not_stop_the_search(self) -> None:
        field = FakeNode(
            interfaces=["Accessible", "EditableText"],
            state=FakeState(editable=True, showing=True),
        )

        class Exploding(FakeNode):
            def get_child_count(self):
                raise RuntimeError("nodo irraggiungibile")

        root = FakeNode(interfaces=["Accessible"], children=[Exploding(), field])
        with self._patch_atspi():
            found = insertion.AtspiInserter()._find_editable_in(root)
        self.assertIs(found, field)

    def test_an_editable_ancestor_wins_over_a_deeper_label(self) -> None:
        """La ricerca si ferma al primo nodo valido, non al piu' profondo."""
        editable = FakeNode(
            interfaces=["Accessible", "EditableText"],
            state=FakeState(editable=True, showing=True),
            children=[FakeNode(interfaces=["Accessible", "EditableText"])],
        )
        with self._patch_atspi():
            found = insertion.AtspiInserter()._find_editable_in(editable)
        self.assertIs(found, editable)


class TestAtspiInsert(unittest.TestCase):
    """insert() con un albero finto: niente applicazioni reali toccate."""

    def _inserter_with(self, node):
        inserter = insertion.AtspiInserter()
        inserter._find_editable = lambda: node
        return inserter

    def test_insert_fails_cleanly_when_no_field_is_found(self) -> None:
        result = self._inserter_with(None).insert("testo")
        self.assertFalse(result.ok)
        self.assertEqual(result.method, "atspi")

    def test_insert_writes_at_the_end_of_the_existing_text(self) -> None:
        written = {}

        class FakeEditable:
            def insert_text(self, offset, text, length):
                written.update(offset=offset, text=text, length=length)

        node = mock.MagicMock()
        node.get_character_count.return_value = 15
        node.get_editable_text.return_value = FakeEditable()

        result = self._inserter_with(node).insert(" coda")

        self.assertTrue(result.ok, result.message)
        self.assertEqual(written, {"offset": 15, "text": " coda", "length": 5})

    def test_a_failing_insert_is_reported_not_raised(self) -> None:
        node = mock.MagicMock()
        node.get_character_count.side_effect = RuntimeError("campo scomparso")
        result = self._inserter_with(node).insert("testo")
        self.assertFalse(result.ok)
        self.assertIn("at-spi", result.message)


if __name__ == "__main__":
    unittest.main()
