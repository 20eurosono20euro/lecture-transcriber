"""Sistema di design dell'interfaccia.

L'interfaccia si ispira al linguaggio visivo di Apple (HIG): superfici
sovrapposte con angoli ampi, una scala tipografica dichiarata, capsule
di stato, controllo segmentato, barra di comando mobile. Non e' una
copia: e' la traduzione di quelle regole nei widget che GTK4 mette a
disposizione.

Cosa si puo' fare e cosa no, verificato su GNOME 50 / Wayland:

* **Gli angoli arrotondati della finestra non si possono ottenere dal
  client.** Su Wayland e' il compositor a disegnare il telaio. Si
  arrotondano quindi le superfici *interne* (schede, barre, campi),
  che e' anche dove l'effetto si vede.
* **La sfocatura (materiale "often" di Apple) non esiste**: non c'e'
  un'API per sfocare cio' che sta dietro la finestra. Si ottiene la
  profondita' con la sovrapposizione di livelli alfa piu' un bordo
  sottile (hairline), che su sfondo scuro produce lo stesso effetto di
  separazione.
* **I nomi di colore del tema** (``@accent_color``, ``@dim_label_color``,
  ...) funzionano nel CSS di GTK4 ma **non** nei tag di
  ``Gtk.TextBuffer``: li' servono valori RGBA espliciti, risolti da
  :func:`resolve_named_color`.
* ``alpha()`` e' supportato, ed e' il modo corretto per ottenere le
  sovrapposizioni senza inseguire il tema chiaro/scuro a mano.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gtk  # noqa: E402

# ---------------------------------------------------------------------------
# gettoni (token)
# ---------------------------------------------------------------------------

#: Scala dei raggi. Apple usa angoli proporzionali alla dimensione della
#: superficie: piu' e' grande la superficie, piu' ampio il raggio.
RADIUS = {
    "sheet": "16px",  # pannelli che coprono (preferenze, barra laterale)
    "card": "12px",  # schede di contenuto
    "control": "8px",  # pulsanti, campi, righe
    "field": "6px",  # campi di testo nidificati
    "pill": "999px",  # capsule: segmentato, stati, livello microfono
}

#: Scala tipografica. I valori sono in ``px`` perche' e' l'unita' del CSS
#: di GTK; il rapporto fra i gradini riproduce quello di Apple, con il
#: corpo testo leggermente piu' grande per la lettura prolungata.
TYPE = {
    "large-title": ("1.55rem", 800, "0.01em"),
    "title-1": ("1.28rem", 700, "0em"),
    "title-2": ("1.05rem", 650, "0em"),
    "title-3": ("0.94rem", 600, "0.01em"),
    "headline": ("0.88rem", 600, "0.01em"),
    "body": ("0.82rem", 400, "0.012em"),
    "callout": ("0.78rem", 400, "0.012em"),
    "footnote": ("0.72rem", 400, "0.014em"),
    "caption": ("0.67rem", 550, "0.03em"),
}

#: Interlinea del transcript. Apple usa 1.4-1.5 per il corpo testo; per
#: una lettura lunga e con timestamp a inizio riga serve piu' aria.
TRANSCRIPT_LINE_HEIGHT = 1.58

#: Livelli di elevazione: (alfa dello sfondo, alfa del bordo, ombra).
#: L'ombra e' dichiarata ma GTK4 la applica solo dove il widget la
#: disegna; dove non e' supportata resta l'alfa e il bordo a fare il
#: lavoro di separazione.
ELEVATION = {
    "flat": ("alpha(currentColor, 0)", "alpha(currentColor, 0)", "none"),
    "card": (
        "alpha(currentColor, 0.045)",
        "alpha(currentColor, 0.09)",
        "0 1px 2px alpha(shade(@theme_bg_color, 0.4), 0.18)",
    ),
    "raised": (
        "alpha(currentColor, 0.07)",
        "alpha(currentColor, 0.12)",
        "0 2px 6px alpha(shade(@theme_bg_color, 0.4), 0.22)",
    ),
    "floating": (
        "alpha(@theme_bg_color, 0.78)",
        "alpha(currentColor, 0.14)",
        "0 6px 20px alpha(shade(@theme_bg_color, 0.4), 0.32)",
    ),
}


def _type_css() -> str:
    rules = []
    for name, (size, weight, tracking) in TYPE.items():
        rules.append(
            f".lt-type-{name} {{\n"
            f"    font-size: {size};\n"
            f"    font-weight: {weight};\n"
            f"    letter-spacing: {tracking};\n"
            f"}}"
        )
    return "\n".join(rules)


# ---------------------------------------------------------------------------
# foglio di stile
# ---------------------------------------------------------------------------

_CSS_TEMPLATE = """
/* ---- finestra ------------------------------------------------------- */
/* Il fondo della finestra resta trasparente **di proposito**: la
   superficie visibile e' la scheda interna (`.lt-window-surface`), che
   porta il colore di sfondo del tema e gli angoli arrotondati. Su
   Wayland il compositore disegna il telaio rettangolare della finestra e
   non si puo' arrotondarlo dal client: arrotondando la superficie
   interna con un piccolo margine, la finestra *sembra* una scheda
   mobile -- che e' anche l'effetto voluto.

   Attenzione: `set_opacity()` agisce sull'alpha dell'intera finestra,
   quindi senza una superficie interna l'opacita' non avrebbe nulla da
   sfumare e il testo finirebbe direttamente sul desktop (misurato: con
   `background-color: transparent` e nessuna superficie, la finestra
   era invisibile tranne il testo). */
window.lt-window {
    background-color: transparent;
}

.lt-window-surface {
    background-color: alpha(@theme_bg_color, 0.985);
    border: 1px solid alpha(currentColor, 0.12);
    border-radius: 16px;
    margin: 5px;
}

/* ---- superfici ------------------------------------------------------ */
.lt-surface {
    background-color: %(card_bg)s;
    border: 1px solid %(card_border)s;
    border-radius: %(card_radius)s;
}
.lt-surface-raised {
    background-color: %(raised_bg)s;
    border: 1px solid %(raised_border)s;
    border-radius: %(card_radius)s;
}
/* La barra di comando mobile: una capsula staccata dal contenuto. */
.lt-floating-bar {
    background-color: %(floating_bg)s;
    border: 1px solid %(floating_border)s;
    border-radius: %(pill_radius)s;
    padding: 5px 8px;
    box-shadow: %(floating_shadow)s;
}
/* La variante su due file (modalita' sopra, comandi sotto): la capsula
   non deve allargare la finestra, quindi il padding orizzontale si
   stringe al minimo per far entrare la riga piu' larga dei comandi nei
   420 px della finestra (misurato: una capsula a riga unica misurava
   590 px di minimo e costringeva la finestra ad allargarsi). */
.lt-floating-bar-vertical {
    padding: 5px 3px;
}

/* ---- capsule di stato ----------------------------------------------- */
.lt-capsule {
    border-radius: %(pill_radius)s;
    padding: 2px 9px;
    font-size: %(footnote)s;
    font-weight: 600;
    letter-spacing: 0.02em;
    background-color: alpha(currentColor, 0.10);
    color: @theme_fg_color;
}
.lt-capsule-accent {
    background-color: alpha(@accent_color, 0.16);
    color: @accent_color;
}
.lt-capsule-success {
    background-color: alpha(@success_color, 0.16);
    color: @success_color;
}
.lt-capsule-warning {
    background-color: alpha(@warning_color, 0.16);
    color: @warning_color;
}
.lt-capsule-error {
    background-color: alpha(@error_color, 0.16);
    color: @error_color;
}
.lt-capsule-quiet {
    background-color: alpha(currentColor, 0.07);
    color: alpha(@theme_fg_color, 0.65);
}
/* La pillola "torna all'ultimo testo": un pulsante galleggiante che
   ricompare quando l'utente scorre via dal fondo. Riusa i token della
   capsula accentata per restare coerente col resto dell'interfaccia. */
.lt-rejoin {
    background-color: alpha(@accent_color, 0.16);
    color: @accent_color;
    border: 0;
    border-radius: 999px;
    padding: 6px 12px;
    font-size: %(footnote)s;
    font-weight: 600;
}
.lt-rejoin:hover {
    background-color: alpha(@accent_color, 0.26);
}

/* ---- controllo segmentato ------------------------------------------- */
/* Due pulsanti piatti dentro una capsula: quello attivo prende il fondo
   accentato. E' il controllo che iOS usa per scegliere fra due modi. */
.lt-segmented {
    background-color: alpha(currentColor, 0.07);
    border-radius: %(pill_radius)s;
    padding: 2px;
}
.lt-segmented > button {
    border-radius: %(pill_radius)s;
    padding: 3px 14px;
    font-size: %(footnote)s;
    font-weight: 600;
    letter-spacing: 0.01em;
    background: none;
    box-shadow: none;
    border: none;
    color: alpha(@theme_fg_color, 0.7);
    min-height: 0;
}
.lt-segmented > button:checked {
    background-color: @theme_bg_color;
    color: @theme_fg_color;
    box-shadow: 0 1px 3px alpha(shade(@theme_bg_color, 0.4), 0.28);
}

/* ---- pulsanti ------------------------------------------------------- */
/* I pulsanti della barra mobile sono icone nude con bersaglio da 28px:
   nessuna cornice, solo il bersaglio. */
.lt-icon-button {
    border-radius: %(pill_radius)s;
    padding: 5px;
    min-width: 28px;
    min-height: 28px;
    background: none;
    box-shadow: none;
    border: none;
    color: alpha(@theme_fg_color, 0.78);
    transition: 120ms ease;
}
.lt-icon-button:hover {
    background-color: alpha(currentColor, 0.09);
    color: @theme_fg_color;
}
.lt-icon-button:active {
    background-color: alpha(currentColor, 0.15);
}
/* Il bersaglio da 28px e' troppo piccolo per un anello di focus pieno:
   si disegna un anello sottile dentro il quadrato, senza far crescere
   il pulsante. GTK dipinge gli anelli di focus solo quando si naviga
   con la tastiera (non a ogni click), quindi qui non c'e' rischio di
   "doppio anello". */
.lt-icon-button:focus-visible {
    outline: 2px solid alpha(@accent_color, 0.65);
    outline-offset: -2px;
    background-color: alpha(currentColor, 0.06);
}
/* Un pulsante a icona che e' un interruttore (il sempre-in-primo-piano)
   deve dire il proprio stato a colpo d'occhio, non solo al passaggio del
   mouse: spento e' un'icona grigia, acceso prende il colore accentato e un
   fondo tinto. Senza questa regola i due stati erano distinguibili solo
   dall'icona, che in un tema symbolic monocromo e' la stessa. */
.lt-icon-button:checked {
    background-color: alpha(@accent_color, 0.16);
    color: @accent_color;
}
.lt-icon-button:checked:hover {
    background-color: alpha(@accent_color, 0.22);
    color: @accent_color;
}
.lt-icon-button:checked:focus-visible {
    outline: 2px solid alpha(@accent_color, 0.65);
    outline-offset: -2px;
}
/* Quando il comando non e' disponibile il pulsante non deve sembrare
   rotto: si attenua e resta premibile (il messaggio lo da' il toast). */
.lt-icon-button:disabled {
    color: alpha(@theme_fg_color, 0.3);
}
/* Il pulsante primario: capsula piena, l'unico elemento fortemente
   colorato della finestra. */
.lt-primary {
    border-radius: %(pill_radius)s;
    padding: 5px 16px;
    font-size: %(footnote)s;
    font-weight: 650;
    letter-spacing: 0.01em;
    background-color: @accent_color;
    color: @theme_selected_fg_color;
    border: none;
    box-shadow: none;
    min-height: 28px;
}
.lt-primary:hover {
    background-color: shade(@accent_color, 1.08);
}
.lt-primary:active {
    background-color: shade(@accent_color, 0.94);
}
.lt-primary.suggested-action {
    background-color: @accent_color;
}
.lt-destructive {
    border-radius: %(pill_radius)s;
    padding: 5px 14px;
    font-size: %(footnote)s;
    font-weight: 600;
    background-color: alpha(@error_color, 0.14);
    color: @error_color;
    border: none;
    box-shadow: none;
    min-height: 28px;
}
.lt-destructive:hover {
    background-color: alpha(@error_color, 0.22);
}
/* Interruttore acceso/spento: capsula sottile, lo stile dei toggle iOS. */
.lt-toggle {
    border-radius: %(pill_radius)s;
    padding: 4px 12px;
    font-size: %(footnote)s;
    font-weight: 600;
    background-color: alpha(currentColor, 0.07);
    color: alpha(@theme_fg_color, 0.7);
    border: none;
    box-shadow: none;
    min-height: 26px;
    transition: 120ms ease;
}
.lt-toggle:hover {
    background-color: alpha(currentColor, 0.11);
}
.lt-toggle:checked {
    background-color: alpha(@accent_color, 0.16);
    color: @accent_color;
}
.lt-toggle:checked:hover {
    background-color: alpha(@accent_color, 0.22);
}

/* ---- transcript ----------------------------------------------------- */
.lt-transcript {
    padding: 2px 14px 8px 14px;
    /* L'interlinea e' la costante dichiarata sopra: 1.58x il corpo di
       lettura, perche' le righe hanno timestamp e segnalibri a inizio
       riga e hanno bisogno di aria (vedi TRANSCRIPT_LINE_HEIGHT). */
    line-height: %(line_height)s;
}
.lt-transcript text {
    background: none;
}
.lt-transcript-row-time {
    color: alpha(@accent_color, 0.85);
    font-size: %(caption)s;
    font-weight: 600;
    letter-spacing: 0.03em;
}
.lt-transcript-row-text {
    font-size: 1rem;
}
.lt-partial {
    color: alpha(@theme_fg_color, 0.55);
    font-style: italic;
}
/* Il separatore fra segmenti e' un filetto appena accennato: guidare
   l'occhio senza spezzare il flusso di lettura. */
.lt-transcript-separator {
    color: alpha(currentColor, 0.10);
}

/* ---- elenco segnalibri ---------------------------------------------- */
/* Le righe dell'elenco segnalibri sono pulsanti piatti dentro il
   pannello: anche qui bersaglio comodo e hover appena accennato. */
.lt-bookmark-list {
    background: none;
}
.lt-bookmark-list > row {
    border-radius: %(control_radius)s;
    margin: 1px 6px;
    padding: 4px 6px;
}
.lt-bookmark-list > row:hover {
    background-color: alpha(currentColor, 0.06);
}
.lt-bookmark-list > row:focus-visible {
    outline: 2px solid alpha(@accent_color, 0.6);
    outline-offset: -2px;
}

/* ---- barra di scorrimento ------------------------------------------ */
/* La scrollbar del transcript segue la palette: un cursore sottile e
   arrotondato, visibile solo quando serve (overlay). */
.lt-transcript scrollbar {
    background: none;
}
.lt-transcript scrollbar > range > trough {
    background: none;
    border-radius: %(pill_radius)s;
}
.lt-transcript scrollbar > range > trough > slider {
    background-color: alpha(currentColor, 0.18);
    border-radius: %(pill_radius)s;
    min-width: 5px;
    min-height: 5px;
}
.lt-transcript scrollbar > range > trough > slider:hover {
    background-color: alpha(currentColor, 0.28);
}

/* ---- campi ---------------------------------------------------------- */
.lt-field {
    border-radius: %(field_radius)s;
    background-color: alpha(currentColor, 0.05);
    border: 1px solid alpha(currentColor, 0.08);
    padding: 3px 8px;
    font-size: %(footnote)s;
    min-height: 0;
}
.lt-field:focus-within {
    border-color: alpha(@accent_color, 0.55);
    background-color: alpha(currentColor, 0.07);
}

/* ---- livello microfono ---------------------------------------------- */
.lt-level trough {
    min-height: 4px;
    border-radius: %(pill_radius)s;
    background-color: alpha(currentColor, 0.12);
}
.lt-level block {
    border-radius: %(pill_radius)s;
    background-color: @success_color;
}
.lt-level block.filled {
    background-color: @success_color;
}
.lt-level block.empty {
    background-color: alpha(currentColor, 0.12);
}

/* ---- barra laterale ------------------------------------------------- */
.lt-sidebar {
    background-color: alpha(currentColor, 0.035);
    border-right: 1px solid alpha(currentColor, 0.08);
}
.lt-sidebar-row {
    border-radius: %(control_radius)s;
    padding: 6px 8px;
    margin: 1px 6px;
}
.lt-sidebar-row:selected {
    background-color: alpha(@accent_color, 0.16);
    color: @accent_color;
}

/* ---- testo attenuato ------------------------------------------------- */
.lt-dim {
    color: alpha(@theme_fg_color, 0.55);
}
.lt-dimmer {
    color: alpha(@theme_fg_color, 0.4);
}
.lt-numeric {
    font-variant-numeric: tabular-nums;
    letter-spacing: 0.02em;
}
"""


def _fill(template: str) -> str:
    return template % {
        "card_bg": ELEVATION["card"][0],
        "card_border": ELEVATION["card"][1],
        "raised_bg": ELEVATION["raised"][0],
        "raised_border": ELEVATION["raised"][1],
        "floating_bg": ELEVATION["floating"][0],
        "floating_border": ELEVATION["floating"][1],
        "floating_shadow": ELEVATION["floating"][2],
        "card_radius": RADIUS["card"],
        "control_radius": RADIUS["control"],
        "field_radius": RADIUS["field"],
        "pill_radius": RADIUS["pill"],
        "line_height": str(TRANSCRIPT_LINE_HEIGHT),
        "footnote": TYPE["footnote"][0],
        "caption": TYPE["caption"][0],
    }


CSS = _fill(_CSS_TEMPLATE) + "\n" + _type_css() + "\n"


def install() -> bool:
    """Installa il foglio di stile sul display corrente.

    Restituisce ``True`` se il CSS e' stato accettato: un CSS con un
    errore di sintassi viene scartato in silenzio da GTK e la finestra
    resterebbe senza stile, quindi il fallimento va saperlo.
    """
    provider = Gtk.CssProvider()
    try:
        provider.load_from_data(CSS.encode("utf-8"))
    except Exception:
        return False
    display = Gdk.Display.get_default()
    if display is None:
        return False
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    return True


# ---------------------------------------------------------------------------
# colori per i tag di TextBuffer
# ---------------------------------------------------------------------------

_CACHE: dict[str, str] = {}


def resolve_named_color(name: str, fallback: str) -> str:
    """Risolve un nome di colore del tema in un RGBA esplicito.

    I tag di ``Gtk.TextBuffer`` accettano solo colori concreti: un
    ``foreground="@accent_color"`` produce l'avviso "Don't know color".
    Il nome si risolve chiedendo al motore CSS di GTK il valore corrente
    del tema, cosi' il transcript segue il tema chiaro/scuro senza
    duplicate.

    Il risultato si memoizza: ``lookup_color`` crea un ``Gtk.Label`` a
    ogni chiamata e questa funzione viene usata dentro il ciclo di
    aggiornamento del testo.
    """
    cached = _CACHE.get(name)
    if cached is not None:
        return cached
    value = fallback
    try:
        probe = Gtk.Label()
        ok, rgba = probe.get_style_context().lookup_color(name)
        if ok and rgba is not None:
            value = rgba.to_string()
    except Exception:
        pass
    _CACHE[name] = value
    return value


def accent_rgba(alpha: float = 1.0) -> str:
    """Colore accentato con alpha, come stringa RGBA per i tag."""
    base = resolve_named_color("accent_color", "#3584e4")
    try:
        rgba = Gdk.RGBA()
        if rgba.parse(base):
            rgba.alpha = alpha
            return rgba.to_string()
    except Exception:
        pass
    return base


def foreground_rgba(alpha: float = 1.0) -> str:
    """Colore di primo piano con alpha, per il testo provvisorio."""
    base = resolve_named_color("theme_fg_color", "#ffffff")
    try:
        rgba = Gdk.RGBA()
        if rgba.parse(base):
            rgba.alpha = alpha
            return rgba.to_string()
    except Exception:
        pass
    return base


# ---------------------------------------------------------------------------
# componenti
# ---------------------------------------------------------------------------


class SegmentedControl(Gtk.Box):
    """Controllo segmentato in stile iOS: due o piu' modi, uno attivo.

    Un `Gtk.ToggleButton` singolo non dice *quale* dei due modi e'
    attivo: si vede solo "premuto / non premuto". Il controllo segmentato
    mostra entrambe le etichette e quale e' selezionata, che e' quello
    che la specifica chiede ("la modalita' attiva deve essere sempre
    chiara").
    """

    def __init__(self, options: list[tuple[str, str]], active: str) -> None:
        """``options`` e' una lista di (chiave, etichetta)."""
        super().__init__()
        self.add_css_class("lt-segmented")
        self._keys: list[str] = []
        self._buttons: dict[str, Gtk.ToggleButton] = {}
        self._handlers: list = []
        # Il primo pulsante creato e' il riferimento del gruppo: da li'
        # in poi tutti gli altri si agganciano a lui. Un CheckButton non
        # va bene -- ``set_group`` pretende un ToggleButton -- e comunque
        # non serve: il raggruppamento e' una proprieta' dei ToggleButton.
        self._group: Gtk.ToggleButton | None = None

        for key, label in options:
            button = Gtk.ToggleButton(label=label)
            if self._group is None:
                self._group = button
            else:
                button.set_group(self._group)
            button.set_active(key == active)
            self._keys.append(key)
            self._buttons[key] = button
            self.append(button)

    def connect_changed(self, callback) -> None:
        self._handlers.append(callback)
        for key, button in self._buttons.items():
            button.connect(
                "toggled",
                lambda b, k=key: self._emit(k) if b.get_active() else None,
            )

    def _emit(self, key: str) -> None:
        for callback in list(self._handlers):
            callback(key)

    @property
    def active(self) -> str:
        for key, button in self._buttons.items():
            if button.get_active():
                return key
        return self._keys[0] if self._keys else ""

    def set_active(self, key: str) -> None:
        button = self._buttons.get(key)
        if button is not None and not button.get_active():
            button.set_active(True)


def capsule(
    text: str, style: str = "quiet", tooltip: str | None = None
) -> Gtk.Label:
    """Capsula di stato: etichetta arrotondata con sfondo tinto."""
    label = Gtk.Label(label=text)
    label.add_css_class("lt-capsule")
    label.add_css_class(f"lt-capsule-{style}")
    if tooltip:
        label.set_tooltip_text(tooltip)
    return label


def icon_button(
    icon_name: str, tooltip: str, on_click=None
) -> Gtk.Button:
    """Pulsante a icona nudo, con bersaglio da 28px."""
    button = Gtk.Button(icon_name=icon_name, tooltip_text=tooltip)
    button.add_css_class("lt-icon-button")
    if on_click is not None:
        button.connect("clicked", on_click)
    return button


def primary_button(text: str, on_click=None) -> Gtk.Button:
    button = Gtk.Button(label=text)
    button.add_css_class("lt-primary")
    if on_click is not None:
        button.connect("clicked", on_click)
    return button


def toggle_button(text: str, on_toggled=None) -> Gtk.ToggleButton:
    button = Gtk.ToggleButton(label=text)
    button.add_css_class("lt-toggle")
    if on_toggled is not None:
        button.connect("toggled", on_toggled)
    return button
