#!/usr/bin/env python3
"""Genera gli screenshot del README come mockup SVG fedele alla vera UI.

Non possiamo catturare la finestra reale senza un display vivo, e un
mockup *inventato* tradirebbe la UI. Questo script costruisce invece una
SVG che riusa i **valori letterali** del foglio di stile reale
(``lecture_transcriber.theme``) e i colori del tema libadwaita risolti
da GTK. Il risultato e' una ricostruzione pixel-fedele della finestra
in "presa di appunti" e in "consultazione discreta".

Le icone sono quelle della barra mobile reale (``media-playback-start``,
``media-playback-stop``, ecc.) disegnate come path SVG monochromi, come
fa il tema iconico symbolic di GNOME.

Uso:
    PYTHONPATH=src .venv/bin/python scripts/render_mockup.py [outdir]
    # scrive outdir/notes.svg, outdir/notes.png (via headless chromium)
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

try:
    from lecture_transcriber import theme  # type: ignore
except Exception:  # pragma: no cover
    theme = None  # type: ignore

OUT_DEFAULT = Path(__file__).resolve().parent.parent / "docs" / "shots"

# ---------------------------------------------------------------------------
# valori reali del tema libadwaita su Fedora 44 (modalita' scura), misurati
# ---------------------------------------------------------------------------

DARK_BG = "rgb(34,34,38)"
DARK_FG = "rgb(255,255,255)"
ACCENT = "rgb(129,208,255)"
SUCCESS = "rgb(120,233,171)"
WARNING = "rgb(255,194,82)"
ERROR = "rgb(255,147,140)"

INK = DARK_FG
SURFACE = "#222226"        # alpha(@theme_bg_color,.985) su bg scuro
FG_DIM = "rgba(255,255,255,0.55)"


def rgba(fg: str, a: float) -> str:
    m = re.match(r"rgb\((\d+),\s*(\d+),\s*(\d+)\)", fg)
    if not m:
        return fg
    r, g, b = (int(x) for x in m.groups())
    return f"rgba({r},{g},{b},{a})"


# ---------------------------------------------------------------------------
# icone symbolic monochromi (path SVG)
# ---------------------------------------------------------------------------

ICONS = {
    "play": '<path d="M8 5v14l11-7z"/>',
    "pause": '<path d="M7 5h4v14H7zM13 5h4v14h-4z"/>',
    "stop": '<rect x="6" y="6" width="12" height="12" rx="1"/>',
    "paste": '<path d="M14 4h-2a1 1 0 0 0-2 0H8v3h8V4zM6 4H5a1 1 0 0 0-1 1v14a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1V5a1 1 0 0 0-1-1h-1v5H6V4z"/>',
    "star": '<path d="M12 3l2.7 5.5 6 .9-4.3 4.2 1 6L12 17l-5.4 2.6 1-6L3.3 9.4l6-.9z"/>',
    "search": '<path d="M10.5 3a7.5 7.5 0 0 1 5.7 12.3l4.5 4.5-1.4 1.4-4.5-4.5A7.5 7.5 0 1 1 10.5 3zm0 2a5.5 5.5 0 1 0 0 11 5.5 5.5 0 0 0 0-11z"/>',
    "down": '<path d="M12 4v14l-6-6-1.4 1.4L12 21l7.4-7.6L18 12l-6 6V4z"/>',
    "copy": '<path d="M9 4a1 1 0 0 1 1-1h9a1 1 0 0 1 1 1v11a1 1 0 0 1-1 1h-2v-2h1V5h-8v1h-2V4zM6 7h9a1 1 0 0 1 1 1v11a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V8a1 1 0 0 1 1-1zm0 2v9h7V9H6z"/>',
    "export": '<path d="M6 3h12a1 1 0 0 1 1 1v11h-2V5H7v13h4v2H6a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1zM14 12l2 2 3-3-1.4-1.4L16 11V6h2v5l1.6-1.4L21 11l-3 3-2-2z"/>',
    "prefs": '<path d="M10 4a1 1 0 0 1 1 1v1h9a1 1 0 0 1 0 2h-9v1a1 1 0 0 1-2 0V8H4a1 1 0 0 1 0-2h5V5a1 1 0 0 1 1-1zm1 6a1 1 0 0 1 1 1v1h8a1 1 0 0 1 0 2h-8v1a1 1 0 0 1-2 0v-1H4a1 1 0 0 1 0-2h6v-1a1 1 0 0 1 1-1z"/>',
}


def _icon(name: str, size: int = 16) -> str:
    """Icona symbolic come elemento SVG monchromo."""
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" '
        f'fill="currentColor" aria-hidden="true">{ICONS[name]}</svg>'
    )


# ---------------------------------------------------------------------------
# costruzione della finestra (struttura reale di ui.py)
# ---------------------------------------------------------------------------

def window(mode: str) -> str:
    is_consult = mode == "consult"

    meta = ""
    if not is_consult:
        meta = (
            f'<g font-family="Cantarell" font-size="11" fill="{FG_DIM}">'
            '<rect x="34" y="92" width="352" height="34" rx="12" '
            f'fill="rgba(255,255,255,0.05)" stroke="rgba(255,255,255,0.09)"/>'
            f'<text x="48" y="113">Corso di Analisi</text>'
            f'<text x="196" y="113">Prof. Rossi</text>'
            f'<text x="310" y="113">29/10</text></g>'
        )

    transcript = [
        ("00:00", False, "Oggi introduciamo la convergenza di successioni."),
        ("00:32", False, "Definizione: una successione converge a L se ..."),
        ("01:05", True, "Il criterio di Cauchy e’ equivalente alla convergenza."),
        ("01:40", False, "Dimostriamo il teorema del confronto direttamente ..."),
        ("02:12", False, "La varianza di una variabile aleatoria si calcola come ..."),
    ]
    ty = 148
    trows = ""
    for stamp, bm, text in transcript:
        star = f'<text x="78" y="{ty}" font-size="12" fill="{WARNING}">★</text>' if bm else ""
        tx = 88 if bm else 76
        trows += (
            f'<text x="34" y="{ty}" font-family="Cantarell" font-size="10" '
            f'font-weight="600" fill="{rgba(ACCENT,0.85)}">{stamp}</text>'
            f'{star}'
            f'<text x="{tx}" y="{ty}" font-family="Cantarell" font-size="13" '
            f'fill="{INK}">{text}</text>'
            f'<line x1="34" y1="{ty+8}" x2="386" y2="{ty+8}" stroke="rgba(255,255,255,0.10)" stroke-width="1"/>'
        )
        ty += 24
    # ipotesi provvisoria
    trows += (
        f'<text x="34" y="{ty}" font-family="Cantarell" font-size="10" font-weight="600" '
        f'fill="{rgba(ACCENT,0.85)}">02:44</text>'
        f'<text x="76" y="{ty}" font-family="Cantarell" font-size="13" font-style="italic" '
        f'fill="rgba(255,255,255,0.55)">il teorema segue per induzione sulla ...</text>'
    )

    # icone barra di comando
    def ico(nm, x, active=False):
        bg = "rgba(255,255,255,0.09)" if active else "none"
        return (
            f'<g transform="translate({x},0)">'
            f'<circle cx="14" cy="14" r="14" fill="{bg}"/>'
            f'<g transform="translate(6,6)">{_icon(nm)}</g></g>'
        )

    bar = ""
    if not is_consult:
        bar = (
            '<g font-family="Cantarell" font-size="11" font-weight="600">'
            # capsula segmentata
            '<rect x="48" y="296" width="150" height="26" rx="13" fill="rgba(255,255,255,0.07)"/>'
            f'<rect x="50" y="298" width="72" height="22" rx="11" fill="{SURFACE}" stroke="none"/>'
            f'<text x="86" y="314" text-anchor="middle" fill="{INK}">Lettura</text>'
            f'<text x="150" y="314" text-anchor="middle" fill="rgba(255,255,255,0.7)">Appunti</text>'
            '</g>'
            # comandi
            f'<line x1="212" y1="300" x2="212" y2="318" stroke="rgba(255,255,255,0.1)"/>'
            f'{ico("play",222,True)}{ico("stop",250)}{ico("paste",278)}'
            f'<line x1="308" y1="300" x2="308" y2="318" stroke="rgba(255,255,255,0.1)"/>'
            f'{ico("star",318)}{ico("search",346)}{ico("down",374)}'
            f'<line x1="404" y1="300" x2="404" y2="318" stroke="rgba(255,255,255,0.1)"/>'
            f'{ico("copy",414)}{ico("export",442)}{ico("prefs",470)}'
        )

    # barra superiore e striscia di stato
    mode_txt = "consultazione discreta" if is_consult else "presa di appunti assistita"
    mode_class_fill = "rgba(255,255,255,0.07)" if is_consult else rgba(ACCENT, 0.16)
    mode_txt_fill = rgba(INK, 0.65) if is_consult else ACCENT

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="420" height="348" viewBox="0 0 420 348" font-family="Cantarell, system-ui, sans-serif">
  <defs>
    <linearGradient id="stage" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#1b1b20"/><stop offset="1" stop-color="#101013"/>
    </linearGradient>
  </defs>
  <!-- stage -->
  <rect width="420" height="348" fill="url(#stage)"/>
  <!-- finestra -->
  <g>
    <rect x="0" y="10" width="420" height="338" rx="16" fill="{SURFACE}" stroke="rgba(255,255,255,0.12)" stroke-width="1"/>

    <!-- barra superiore -->
    <g font-family="Cantarell">
      <text x="20" y="40" font-size="13" font-weight="600" fill="{INK}">Trascrizione lezioni{' — consultazione' if is_consult else ''}</text>
      <g><rect x="280" y="26" width="118" height="20" rx="10" fill="{mode_class_fill}"/>
        <text x="339" y="40" text-anchor="middle" font-size="9" font-weight="600" fill="{mode_txt_fill}">{mode_txt}</text></g>
      <g><rect x="404" y="26" width="68" height="20" rx="10" fill="rgba(120,233,171,0.16)"/>
        <text x="438" y="40" text-anchor="middle" font-size="9" font-weight="600" fill="{SUCCESS}">microfono attivo</text></g>
      <text x="20" y="40" font-size="9" fill="{FG_DIM}"/>
    </g>
    <text x="392" y="40" font-family="Cantarell" font-size="10" font-weight="550" text-anchor="end" fill="{FG_DIM}">02:44</text>

    {meta}

    <!-- transcript -->
    <g>{trows}</g>

    <!-- striscia di stato -->
    <g>
      <rect x="34" y="272" width="70" height="4" rx="2" fill="rgba(255,255,255,0.12)"/>
      <rect x="34" y="272" width="44" height="4" rx="2" fill="{SUCCESS}"/>
      <text x="112" y="276" font-size="10" fill="{FG_DIM}">in ascolto · latenza ~3 s</text>
    </g>

    <!-- barra mobile -->
    <g>
      {bar}
    </g>
  </g>
</svg>
"""


def to_png(svg: str, out: Path) -> Path:
    import shutil
    import urllib.parse

    shells = [
        Path.home() / ".cache/ms-playwright/chromium_headless_shell-1248/chrome-headless-shell-linux64/chrome-headless-shell",
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
    ]
    shell = next((s for s in shells if s and Path(s).exists()), None)
    png = out.with_suffix(".png")
    if shell:
        tmp = png.with_suffix(".tmp.png")
        url = "data:image/svg+xml;charset=utf-8," + urllib.parse.quote(svg)
        subprocess.run(
            [str(shell), "--headless", "--disable-gpu", "--no-sandbox",
             f"--screenshot={tmp}", "--window-size=420,348",
             "--force-device-scale-factor=2", url],
            capture_output=True,
        )
        if tmp.exists():
            tmp.replace(png)
    return png


def main() -> int:
    outdir = Path(sys.argv[1]) if len(sys.argv) > 1 else OUT_DEFAULT
    outdir.mkdir(parents=True, exist_ok=True)
    for mode in ("notes", "consult"):
        svg = window(mode)
        fn = outdir / f"{mode}.svg"
        fn.write_text(svg)
        png = to_png(svg, fn)
        print(f"mockup {mode}: {fn} ({len(svg)} B) -> {png.name}"
              + (" (png)" if png.exists() else " (niente png: nessun chromium)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
