#!/usr/bin/env bash
# First-party verification of the claims carried over from workflow
# wf_a4573fc7-b65 (whose adversarial-verify phase died on upstream 402).
# Every line prints CONFIRMED / REFUTED / UNKNOWN plus the evidence.
# Run: bash scripts/probe_claims.sh
set -uo pipefail

ok()   { printf 'CONFIRMED  %s\n' "$1"; }
bad()  { printf 'REFUTED    %s\n' "$1"; }
unk()  { printf 'UNKNOWN    %s\n' "$1"; }
run()  { printf '\n--- %s\n' "$1"; }

echo "=========================================================="
echo " Environment"
echo "=========================================================="
run "versions"
printf '  %s\n' "$(. /etc/os-release; echo "$PRETTY_NAME")"
printf '  mutter=%s gnome-shell=%s\n' \
  "$(rpm -q mutter 2>/dev/null | cut -d- -f1-2)" \
  "$(rpm -q gnome-shell 2>/dev/null | cut -d- -f1-2)"
printf '  xdg-desktop-portal=%s portal-gnome=%s\n' \
  "$(rpm -q xdg-desktop-portal 2>/dev/null | cut -d- -f1-2)" \
  "$(rpm -q xdg-desktop-portal-gnome 2>/dev/null | cut -d- -f1-2)"
printf '  session=%s\n' "${XDG_SESSION_TYPE:-?}/${XDG_CURRENT_DESKTOP:-?}"

echo
echo "=========================================================="
echo " Wayland / GNOME 50"
echo "=========================================================="
run "wlr-virtual-keyboard globals exported by mutter"
n=$(strings /usr/lib64/libmutter-18.so.0.0.0 2>/dev/null | grep -c "virtual_keyboard")
if [ "$n" -eq 0 ]; then
  ok "CLAIM HOLDS: mutter exports NO wlr-virtual-keyboard global -> wtype cannot attach"
else
  bad "CLAIM FAILS: mutter DOES export virtual_keyboard ($n string hits)"
fi

run "wlr-layer-shell support in mutter"
n=$(strings /usr/lib64/libmutter-18.so.0.0.0 2>/dev/null | grep -c "layer_shell")
if [ "$n" -eq 0 ]; then
  ok "CLAIM HOLDS: mutter does NOT implement wlr-layer-shell -> gtk4-layer-shell cannot govern stacking"
else
  bad "CLAIM FAILS: mutter references layer_shell ($n hits)"
fi

run "GlobalShortcuts portal provider present"
if [ -x /usr/libexec/gnome-control-center-global-shortcuts-provider ]; then
  ok "gnome-control-center-global-shortcuts-provider IS installed and executable"
else
  bad "GlobalShortcuts provider NOT found"
fi

run "xdotool/ydotool/wtype/wl-clipboard presence"
for t in ydotool ydotoold wtype wl-copy wl-paste at-spi2-registryd; do
  printf '  %-20s %s\n' "$t" "$(command -v "$t" 2>/dev/null || echo '(absent)')"
done

run "/dev/uinput accessibility (ydotool prerequisite)"
if [ -e /dev/uinput ]; then
  printf '  %s\n' "$(stat -c '%A %U:%G' /dev/uinput)"
  if [ "$(stat -c '%U' /dev/uinput)" = "root" ] && ! id -nG | tr ' ' '\n' | grep -qx input; then
    ok "CLAIM HOLDS: /dev/uinput is root-owned and user not in 'input' group -> ydotool needs a udev rule + usermod"
  else
    bad "CLAIM FAILS: /dev/uinput reachable by this user without a udev rule"
  fi
else
  unk "/dev/uinput does not exist (uinput module not loaded)"
fi

echo
echo "=========================================================="
echo " Audio / PipeWire"
echo "=========================================================="
run "pipewire services"
for s in pipewire pipewire-pulse wireplumber; do
  printf '  %-16s %s\n' "$s" "$(systemctl --user is-active "$s" 2>/dev/null)"
done

run "capture sources"
pactl list sources short 2>/dev/null | grep -v '\.monitor' || unk "pactl unavailable"

run "python module availability (the two-Python pitfall)"
for m in gi sounddevice pyaudio numpy soxr; do
  a=$(python3.13 -c "import $m" 2>/dev/null && echo OK || echo no)
  b=$(python3.14 -c "import $m" 2>/dev/null && echo OK || echo no)
  printf '  %-14s py3.13=%-3s py3.14=%s\n' "$m" "$a" "$b"
done

echo
echo "=========================================================="
echo " Text insertion paths"
echo "=========================================================="
run "python3.14 has Atspi + LibreOffice UNO bindings"
python3.14 -c "import gi; gi.require_version('Atspi','2.0'); from gi.repository import Atspi; print('  Atspi: OK')" 2>&1 | head -2
python3.14 -c "import uno; print('  UNO  : OK')" 2>&1 | head -2

run "live at-spi text injection into a GTK entry"
if command -v zenity >/dev/null; then
  zenity --entry --title=probe >/dev/null 2>&1 &
  zpid=$!
  sleep 3
  out=$(python3.14 - <<'PY' 2>&1 | grep -v Deprecation
import gi
gi.require_version('Atspi', '2.0')
from gi.repository import Atspi

node = None
def find(n):
    global node
    try: c = n.get_child_count()
    except Exception: return
    for i in range(c):
        try:
            w = n.get_child_at_index(i); role = w.get_role_name()
        except Exception: continue
        if role in ('entry', 'text', 'password text') and w.get_editable_text() is not None:
            node = w
            return
        find(w)
        if node: return

d = Atspi.get_desktop(0)
for i in range(d.get_child_count()):
    a = d.get_child_at_index(i)
    if a.get_name() == 'zenity':
        find(a)
        break

if node is None:
    print('NO_EDITABLE_NODE_FOUND')
else:
    t = node.get_text()
    before = Atspi.Text.get_text(t, 0, -1)
    ok = node.insert_text(0, 'PROBE-ATSPI', 10)
    after = Atspi.Text.get_text(t, 0, -1)
    if ok and 'PROBE-ATSPI' in after:
        print('INJECTED_OK before=%r after=%r' % (before, after))
    else:
        print('INJECTION_FAILED ok=%s before=%r after=%r' % (ok, before, after))
PY
)
  kill "$zpid" 2>/dev/null
  case "$out" in
    *INJECTED_OK*)  ok "at-spi insert_text() wrote into a live GTK entry: ${out}" ;;
    *NO_EDITABLE*)  unk "no editable ATSPI node exposed by zenity (${out})" ;;
    *FAILED*)       bad "at-spi insert_text() did not land: ${out}" ;;
    *)              unk "at-spi probe inconclusive: ${out}" ;;
  esac
else
  unk "zenity absent - cannot run live at-spi probe"
fi

echo
echo "=========================================================="
echo " Packaging"
echo "=========================================================="
run "flatpak / rpm tooling"
printf '  flatpak=%s rpm-build=%s\n' \
  "$(command -v flatpak 2>/dev/null || echo '(absent)')" \
  "$(command -v rpmbuild 2>/dev/null || echo '(absent)')"

echo
echo "=========================================================="
echo " done"
echo "=========================================================="
