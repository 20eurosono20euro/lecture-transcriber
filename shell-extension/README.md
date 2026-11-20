# Estensione GNOME Shell — Lecture Transcriber

## A che cosa serve

Il protocollo **xdg-shell** (usato da Wayland) **non prevede** una richiesta
di *always-on-top*. Un client non può chiedere al compositore di tenere la
propria finestra sopra le altre: il protocollo espone solo
`set_fullscreen`, `set_maximized`, `set_minimized`, `set_activatable`,
`set_movable`, `set_resizable`.

Su GNOME 50 inoltre **non esiste** il protocollo `wlr-layer-shell`
implementato da Mutter, quindi `gtk4-layer-shell` non funziona: non si può
trasformare la finestra in un layer surface.

L'unica via per avere una finestra "sopra tutto" su GNOME/Wayland è che
**il GNOME Shell stesso**, che è il compositor, modifichi la posizione della
finestra nello stack. È esattamente ciò che fa questa estensione.

## Cosa fa

Espone un metodo D-Bus che l'applicazione chiama per attivare o disattivare
il sempre-in-primo-piano della propria finestra:

| | |
|---|---|
| Nome del bus | `org.gnome.Shell.Extensions.LectureTranscriber` |
| Oggetto | `/org/gnome/Shell/Extensions/LectureTranscriber` |
| Interfaccia | `org.gnome.Shell.Extensions.LectureTranscriber` |
| Metodo | `SetAlwaysOnTop(b on_top)` |

Il metodo `GetAlwaysOnTop() -> b` restituisce lo stato attuale.

Internamente usa `Meta.Window.make_above()` e `Meta.Window.unmake_above()`,
disponibili in Mutter 18 (GNOME 50).

L'estensione **non tocca** desktop, dock e finestre di dialogo: i dialoghi di
sistema devono restare utilizzabili.

## Installazione

```bash
./install.sh
```

Lo script copia l'estensione in
`~/.local/share/gnome-shell/extensions/lecture-transcriber@localhost/`.
Poi **riavvia la sessione GNOME** (logout e login, oppure `Alt+F2` → `r`
per riavviare la shell su X11) e abilitala:

```bash
gnome-extensions enable lecture-transcriber@localhost
gnome-extensions list --enabled | grep lecture-transcriber
```

Su Wayland il riavvio della shell non e' possibile: serve un logout e un
login completi.

## Verifica

L'applicazione funziona **anche senza** l'estensione: se il metodo D-Bus
non e' raggiungibile, il sempre-in-primo-piano semplicemente non viene
attivato e l'app prosegue. Per verificare che l'estensione sia viva:

```bash
gdbus call --session --dest org.gnome.Shell.Extensions.LectureTranscriber \
  --object-path /org/gnome/Shell/Extensions/LectureTranscriber \
  --method org.gnome.Shell.Extensions.LectureTranscriber.GetAlwaysOnTop
```

Se risponde `false` senza errori, l'estensione e' attiva.

## Limitazione nota

L'estensione mette "sopra tutto" **tutte** le finestre dell'applicazione
riconosciute per app-id contenente `lecture` o per titolo contenente
`lecture transcriber` / `trascrizione`. Se l'app ha piu' finestre, vengono
tutte elevate. Non e' possibile distinguere la finestra principale per
app-id, perche' `Shell.WindowTracker` non e' sempre popolato su Wayland.

## Licenza

GPL-2.0-or-later, come tutte le estensioni GNOME Shell.
