// SPDX-License-Identifier: GPL-2.0-or-later
//
// Estensione GNOME Shell per Lecture Transcriber.
//
// Su Wayland il protocollo xdg-shell non prevede una richiesta di
// "always-on-top": il client non puo' chiedere al compositore di tenere
// la propria finestra sopra le altre. L'unica via e' che il GNOME Shell
// stesso, in qualita' di compositor, modifichi la posizione della finestra
// nello stack.
//
// L'estensione espone un metodo D-Bus:
//
//   Bus name:  org.gnome.Shell.Extensions.LectureTranscriber
//   Object:    /org/gnome/Shell/Extensions/LectureTranscriber
//   Interface: org.gnome.Shell.Extensions.LectureTranscriber
//   Method:    SetAlwaysOnTop(b on_top) -> ()
//
// L'applicazione lo chiama per attivare o disattivare il "sopra tutto"
// della propria finestra.
//
// Oltre al sopra-tutto si chiede anche stick(): make_above() da solo
// vale solo nello spazio di lavoro corrente, quindi cambiando workspace
// la finestra non e' piu' sopra niente. stick() la fa seguire su tutti
// gli spazi di lavoro (lo stesso ordine di chiamate di Firefox PIP).

import Gio from 'gi://Gio';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const DBUS_NAME = 'org.gnome.Shell.Extensions.LectureTranscriber';
const DBUS_PATH = '/org/gnome/Shell/Extensions/LectureTranscriber';

/* Finestre da non toccare: desktop, dock e dialoghi (l'utente deve
 * poter interagire con i dialoghi di sistema). */
const NEVER_ABOVE = [
    Meta.WindowType.DESKTOP,
    Meta.WindowType.DOCK,
    Meta.WindowType.DIALOG,
    Meta.WindowType.MODAL_DIALOG,
    Meta.WindowType.SPLASHSCREEN,
];

/* L'interfaccia D-Bus va dichiarata a mano: le firme non sono
 * inferite da wrapJSObject. */
const DBUS_IFACE = `
<node>
  <interface name="org.gnome.Shell.Extensions.LectureTranscriber">
    <method name="SetAlwaysOnTop">
      <arg type="b" name="on_top" direction="in"/>
    </method>
    <method name="GetAlwaysOnTop">
      <arg type="b" name="on_top" direction="out"/>
    </method>
  </interface>
</node>`;

export default class LectureTranscriberExtension extends Extension {
    enable() {
        this._dbusImpl = Gio.DBusExportedObject.wrapJSObject(DBUS_IFACE, this);

        /* L'esportazione va fatta SOLO quando il nome sul bus e' nostro:
         * esportare prima di possedere il nome fallisce. Per questo la
         * chiamata sta nell'handler di bus_own_name; il primo argomento
         * non serve (il nome e' gia' noto). */
        this._dbusOwner = Gio.bus_own_name(
            Gio.BusType.SESSION, DBUS_NAME,
            Gio.BusNameOwnerFlags.NONE,
            () => this._export(),   // nome acquisito: ora si puo' esportare
            null,                   // nome perso: nulla da fare
            () => this._export());  // ricalcolo: riesporta
    }

    disable() {
        /* va riportato in ordine il normale: togliere la finestra dal
         * livello "sopra tutto" prima di disattivare l'estensione */
        this._resetWindows();

        if (this._dbusOwner !== null) {
            Gio.bus_unown_name(this._dbusOwner);
            this._dbusOwner = null;
        }
        if (this._dbusImpl !== null) {
            try {
                this._dbusImpl.unexport();
            } catch (e) {
                // gia' stato tolto
            }
            this._dbusImpl = null;
        }
    }

    _export() {
        if (this._dbusImpl === null)
            return;
        try {
            this._dbusImpl.export(Gio.DBus.session, DBUS_PATH);
        } catch (e) {
            logError(e, 'LectureTranscriber: esportazione D-Bus fallita');
        }
    }

    /* -- metodi D-Bus ---------------------------------------------------- */

    SetAlwaysOnTop(onTop) {
        for (const window of this._candidateWindows()) {
            try {
                if (onTop) {
                    if (!window.above)
                        window.make_above();
                    /* stick(): la finestra segue l'utente su tutti gli
                     * spazi di lavoro. make_above() da solo la tiene
                     * sopra le altre *nello spazio corrente*: passando
                     * ad un altro workspace la finestra non c'e' piu' e
                     * il sopra-tutto non serve a niente (prestato da
                     * Firefox PIP, che fa le tre chiamate insieme).
                     *
                     * E' un'API interna di Meta.Window, non coperta da
                     * nessun protocollo: se una versione di GNOME non la
                     * espone, il try/catch la lascia cadere e
                     * make_above() resta il minimo garantito. */
                    if (!window.on_all_workspaces)
                        window.stick();
                } else {
                    if (window.on_all_workspaces)
                        window.unstick();
                    if (window.above)
                        window.unmake_above();
                }
            } catch (e) {
                logError(e, 'LectureTranscriber: always-on-top non applicabile');
            }
        }
    }

    GetAlwaysOnTop() {
        const windows = this._candidateWindows();
        if (windows.length === 0)
            return false;
        return windows.some(w => w.above);
    }

    /* -- helpers --------------------------------------------------------- */

    /* Le finestre candidate sono quelle della nostra applicazione. La
     * ricerca per app-id fallisce se l'applicazione non ha ancora
     * registrato il proprio WM_CLASS, quindi si accetta anche qualsiasi
     * finestra il cui titolo contenga il nome dell'app. */
    _candidateWindows() {
        const result = [];
        for (const actor of global.get_window_actors()) {
            const window = actor.meta_window;
            if (!window)
                continue;
            if (NEVER_ABOVE.includes(window.get_window_type()))
                continue;
            if (window.is_skip_taskbar())
                continue;

            const app = Shell.WindowTracker.get_default().get_window_app(window);
            const appId = app ? app.get_id() : null;
            if (appId && appId.toLowerCase().includes('lecture')) {
                result.push(window);
            } else if (this._titleLooksOurs(window)) {
                result.push(window);
            }
        }
        return result;
    }

    _titleLooksOurs(window) {
        const title = window.get_title();
        if (!title)
            return false;
        const t = title.toLowerCase();
        return t.includes('lecture transcriber') || t.includes('trascrizione');
    }

    _resetWindows() {
        for (const window of this._candidateWindows()) {
            try {
                if (window.on_all_workspaces)
                    window.unstick();
                if (window.above)
                    window.unmake_above();
            } catch (e) {
                // la finestra potrebbe essere gia' stata distrutta
            }
        }
    }
}
