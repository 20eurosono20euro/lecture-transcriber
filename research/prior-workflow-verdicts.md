# Verified findings from workflow wf_a4573fc7-b65 (feasibility research, prior session)

Source: transcript of workflow `wf_a4573fc7-b65` (feasibility research).
The original journal lived in a per-session directory and is not
redistributable; the verified findings are reproduced here so they
survive outside that session.

**Verification status caveat:** all 4 research agents completed successfully (48 claims).
The adversarial-verify phase then launched 32 verifier agents; **29 died on
`API Error: 402 available tokens exhausted`** (upstream free-tier quota on the local
proxy), not on a logic error. Only 3 verifiers returned a verdict. The 45 claims
marked UNVERIFIED below are therefore UNTESTED, not disproven — the claims are
carried forward as research leads, and the deep-research workflow is re-verifying
the dimensions it was scoped to.

## audio-pipewire  (13 claims)


### SURVIVED (2)

- **OK** — On this Fedora 44 box, PipeWire 1.6.9 runs as server with pipewire-pulse + wireplumber all 'active', and libportaudio.so.2 is installed but neither the 'sounddevice' nor 'pyaudio' Python module is importable into python3.13, while pip 26.0.1 is bound to python3.14 by default — the two-Python pitfall.
  - _every operative assertion verifies exactly on this box_
- **OK** — The single physical capture source here is 'alsa_input.pci-0000_0e_00.6.analog-stereo' (Realtek ALC897 / Ryzen HD Audio Controller) advertising s32le 2ch 48000Hz, with media.class Audio/Source and a 'Front Mic' + 'Front Mic Boost' ALSA mixer path for input gain.
  - _verified live on this machine_

### REFUTED (1)

- **KILLED** — ALSA capture works without any new daemon and arecord/amixer are already present, but direct ALSA bypasses PipeWire's global rate/format conversion so you must request the device's native 48000 Hz and resample yourself.
  - _first half true and verified; second half materially misleading_

### UNVERIFIED (10)

- **UNVERIFIED** — PipeWire docs state its 'adapter' SPA node performs sample-rate and format conversion automatically between the device native format and the app's stream, so a 16 kHz mono capture is resampled by the server
  - _verifier agent died: 402 available tokens exhausted_
- **UNVERIFIED** — PipeWire defaults node.suspend-on-idle=true (seen in /usr/share/pipewire/minimal.conf), so an idle source sits SUSPENDED and only wakes on stream open — keep a continuous stream to avoid wake latency.
  - _verifier agent died: 402 available tokens exhausted_
- **UNVERIFIED** — PortAudio's Linux backends (ALSA, JACK, Pulse) route through PulseAudio here; on this machine PulseAudio is provided by pipewire-pulse, so sounddevice opens the PipeWire server through the libpulse shim and inherits its auto-resampling.
  - _verifier agent died: 402 available tokens exhausted_
- **UNVERIFIED** — Exact-rate matching avoids all resampling cost and quantization: request exactly 48 kHz from the device via the app, then downmix-to-mono and resample to 16 kHz in-process with soxr (the highest-quality CPP/sox native resampler).
  - _verifier agent died: 402 available tokens exhausted_
- **UNVERIFIED** — Input-level/VU readout needs no extra API: PCM arriving in the app stream is already the live signal (pre the VU meter is at the PipeWire source node, not accessible to apps), so compute RMS/peak per block in numpy directly.
  - _verifier agent died: 402 available tokens exhausted_
- **UNVERIFIED** — continuous streaming without overruns: use a PortAudio callback (or sounddevice stream callback) so the rt thread blocks-not-busy, buffer of ~128-512 frames at 48 kHz, and a separate Python consumer thread draining a bounded queue for ASR chunks.
  - _verifier agent died: 402 available tokens exhausted_
- **UNVERIFIED** — hotplug and default-device change are watched in PipeWire through WirePlumber events (device 'added'/'removed'/default set) via the object manager, and in PortAudio only through the host API's device list callback — polling pactl list sources or wpctl status is a practical fallback.
  - _verifier agent died: 402 available tokens exhausted_
- **UNVERIFIED** — device enumeration is cleanest through pw-cli/pw-dump or libwireplumber for wired quality, but pactl list sources short is trivial and stable on pipewire-pulse and enumerates sources, monitors and suspend state.
  - _verifier agent died: 402 available tokens exhausted_
- **UNVERIFIED** — recommended Fedora dnf set: portaudio-devel (enables sounddevice+pyaudio from source), pipewire-devel + wireplumber-devel + pipewire-utils (native Rust API + wpctl/pw-cli), python3-pip, and soxr via pip; no root needed for userspace device enumeration or app capture.
  - _verifier agent died: 402 available tokens exhausted_
- **UNVERIFIED** — Python is the pragmatic MVP capture layer — sounddevice gives working blocks with minimal code — but Rust with libpipewire is the more robust choice if you need live hotplug, default-node-change callbacks and true zero-copy without the pthread-rt/Python-GIL tension.
  - _verifier agent died: 402 available tokens exhausted_

## asr-local  (12 claims)


### UNVERIFIED (12)

- **UNVERIFIED** — sherpa-onnx has NO streaming ASR model for Italian: its online transducer/Zipformer models cover only Chinese, English, Korean, French, and bilingual zh-en (verified from the sherpa-onnx pretrained-models index).
  - _verifier agent died: 402 available tokens exhausted_
- **UNVERIFIED** — OpenAI Whisper large-v3 reaches roughly 4-5% WER on Italian Common Voice 15 and roughly 6-8% on Italian FLEURS, per the HF model card/community evals, with Italian a mid-tier language for the model.
  - _verifier agent died: 402 available tokens exhausted_
- **UNVERIFIED** — faster-whisper (CTranslate2, MIT) claims up to 4x faster than openai/whisper at the same accuracy and ships an integrated Silero VAD filter, both verified in its README.
  - _verifier agent died: 402 available tokens exhausted_
- **UNVERIFIED** — whisper.cpp exposes a cross-vendor Vulkan build via -DGGML_VULKAN=1, and recent ggml added AMD GFX12/RDNA4 (Navi48) device IDs, requiring Mesa 25.x on Linux; RDNA4 Vulkan is not yet a zero-config turnkey path.
  - _verifier agent died: 402 available tokens exhausted_
- **UNVERIFIED** — CTranslate2 (faster-whisper) has no Vulkan backend (CUDA/ROCm only), so on this Radeon Navi48 box GPU acceleration is practical only through whisper.cpp's ggml Vulkan backend.
  - _verifier agent died: 402 available tokens exhausted_
- **UNVERIFIED** — Vosk Italian models are Apache-2.0: vosk-model-small-it-0.22 is 48MB with WER 16.88 (CV)/25.87 (MLS)/17.01 (mtedx); vosk-model-it-0.22 is 1.2GB with WER 8.10/15.68/11.23 but needs up to 16GB RAM (verified from alphacephei.com/vosk/models).
  - _verifier agent died: 402 available tokens exhausted_
- **UNVERIFIED** — OpenAI Whisper natively emits punctuation and casing in Italian at every model size; streaming Zipformer/transducer models and Vosk emit raw tokens without punctuation.
  - _verifier agent died: 402 available tokens exhausted_
- **UNVERIFIED** — distil-whisper large-v3 is English-only and unsuitable for Italian.
  - _verifier agent died: 402 available tokens exhausted_
- **UNVERIFIED** — whisper large-v3-turbo (~809M params) is multilingual and roughly 8x faster than large-v3 on GPU, and is available as ggml-large-v3-turbo in whisper.cpp and 'turbo' in faster-whisper.
  - _verifier agent died: 402 available tokens exhausted_
- **UNVERIFIED** — faster-whisper large-v2 int8 on GPU transcribed a 13-min clip in 59s vs openai/whisper 2m23s (about 2.4x), per the faster-whisper README benchmark.
  - _verifier agent died: 402 available tokens exhausted_
- **UNVERIFIED** — whisper.cpp ggml model sizes: large-v3 fp16 ~2.9GB / int8(Q8) ~1.6GB; large-v3-turbo fp16 ~1.6GB / int8 ~0.85GB; medium fp16 ~1.5GB / int8 ~0.8GB; small fp16 ~466MB / int8 ~250MB.
  - _verifier agent died: 402 available tokens exhausted_
- **UNVERIFIED** — A persistent whisper.cpp process bound through its C API (whisper-rs crate or static lib) is the only binding that gives in-process partial-result streaming; faster-whisper Python is chunked (feed audio, get text) though it supports VAD and condition_on_previous_text.
  - _verifier agent died: 402 available tokens exhausted_

## wayland-ui  (13 claims)


### UNVERIFIED (13)

- **UNVERIFIED** — On GNOME 50 Wayland, a GTK4 window has NO always-on-top client API because xdg-shell has no set_always_on_top request and no always_on_top state value (verified against the xdg-shell spec).
  - _verifier agent died: 402 available tokens exhausted_
- **UNVERIFIED** — Mutter does not implement wlr-layer-shell (long-open GNOME/mutter issue #1984), so gtk4-layer-shell cannot govern stacking on GNOME; it targets wlroots compositors.
  - _verifier agent died: 402 available tokens exhausted_
- **UNVERIFIED** — GTK3 exposes set_keep_above/set_keep_below but mutter ignores such X11/global hints on Wayland, so GTK3 keep-above is not a working always-on-top path on GNOME Wayland.
  - _verifier agent died: 402 available tokens exhausted_
- **UNVERIFIED** — The only reliable always-on-top for a window inside GNOME 50 Wayland is compositor-level: GNOME Shell's built-in Always-on-Top window menu or a gnome-shell extension driving Meta.Window APIs.
  - _verifier agent died: 402 available tokens exhausted_
- **UNVERIFIED** — GTK4 Window.set_opacity works on GNOME 50 giving a semi-transparent window via client-side alpha on an rgba visual (confirmed locally on gtk4 4.22.5).
  - _verifier agent died: 402 available tokens exhausted_
- **UNVERIFIED** — Client-requestable background blur is NOT available on GNOME 50: mutter's ext-background-effect-v1 blur support only landed in GNOME 51 (mutter MR #5071, merged 2026-07-03).
  - _verifier agent died: 402 available tokens exhausted_
- **UNVERIFIED** — Click-through/input passthrough is impossible on native GNOME Wayland in mutter 50; the only escape is an Xwayland-window + X11 input-shape shim, which is a flaky legacy fallback requiring XWayland to own the window.
  - _verifier agent died: 402 available tokens exhausted_
- **UNVERIFIED** — The GlobalShortcuts portal IS working on Fedora 44 GNOME 50: /usr/libexec/gnome-control-center-global-shortcuts-provider is installed and xdg-desktop-portal-gnome 50.0 wires org.freedesktop.impl.portal.GlobalShortcuts/GlobalShortcutsSession (matches merged MR #208, 2025-02-26).
  - _verifier agent died: 402 available tokens exhausted_
- **UNVERIFIED** — The GlobalShortcuts ConfigureShortcuts rebinding UI landed in GNOME 51 (xdg-desktop-portal-gnome MR #264, merged 2026-07-03); GNOME 50 supports create/bind/list/activated but not the portal reboot UI.
  - _verifier agent died: 402 available tokens exhausted_
- **UNVERIFIED** — The InputCapture portal is served on GNOME 50 (mutter support since GNOME 45, MR #2628) and xdg-desktop-portal-gnome 50.0 wires it to org.gnome.Mutter.InputCapture.
  - _verifier agent died: 402 available tokens exhausted_
- **UNVERIFIED** — Local probes confirmed: Fedora 44, GNOME Shell 50.6/mutter 50.6 Wayland, gtk4 4.22.5, libadwaita 1.9.4, xdg-desktop-portal 1.22.1, Xwayland 24.1.14 present, gjs present, pipewire 1.6.9 present but pipewire-pulse NOT installed, gtk4-layer-shell NOT installed.
  - _verifier agent died: 402 available tokens exhausted_
- **UNVERIFIED** — System /usr/bin/python3 has python3-gobject 3.56.3 but the user's primary ~/.venv does NOT include gi/PyGObject; the app must run on system Python or add python3-gobject to its venv.
  - _verifier agent died: 402 available tokens exhausted_
- **UNVERIFIED** — Autostart via ~/.config/autostart/*.desktop (Type=Application) is loaded reliably by gnome-session on GNOME Wayland; no admin rights are needed.
  - _verifier agent died: 402 available tokens exhausted_

## text-insertion  (10 claims)


### UNVERIFIED (10)

- **UNVERIFIED** — on Fedora 44 GNOME 50 (mutter 50.6), the wlr-virtual-keyboard Wayland globals (zwp_virtual_keyboard_v1, wlr_virtual_keyboard_manager_v1) are NOT exported by mutter (verified by exact string scan of /usr/lib64/libmutter-18.so.0.0.0, 0 hits), so wtype has no protocol to attach to on stock GNOME 50 and needs a GNOME Shell extension or a compositor that implements the protocol
  - _verifier agent died: 402 available tokens exhausted_
- **UNVERIFIED** — ydotool is compositor-independent (kernel uinput) and DOES work on Wayland, but on this machine /dev/uinput is mode crw------- root:root, no udev rule exists for it, and the user is not in the 'input' group, so it requires a one-time sudo udev rule (KERNEL==uinput, GROUP=input, MODE=0660) plus usermod -aG input, or running ydotoold as root
  - _verifier agent died: 402 available tokens exhausted_
- **UNVERIFIED** — at-spi text injection into GTK apps is REAL and was verified live: via python3.14 gi.repository.Atspi, insert_text(0,'Hello-ATSPI',1) succeeded on a running GTK zenity entry and readback confirmed the text was inserted and appended to existing content
  - _verifier agent died: 402 available tokens exhausted_
- **UNVERIFIED** — LibreOffice UNO insertion is REAL and was verified live: from python3.14 uno, a socket-accepted headless soffice swriter document accepted text.insertString(cursor, 'inserted-at-end'), proving the UNO same-user path works on this machine
  - _verifier agent died: 402 available tokens exhausted_
- **UNVERIFIED** — the system python3.13 does NOT have PyGObject (import gi fails), but /usr/bin/python3.14 DOES ship gi.repository.Atspi and libreoffice uno bindings (/usr/lib64/python3.14/site-packages), so the transcription app must run its injection code with python3.14
  - _verifier agent died: 402 available tokens exhausted_
- **UNVERIFIED** — Obsidian and other Electron/Chromium apps do not expose an editable at-spi text node by default (Chromium's ATK tree is read-only/opt-in), so neither UNO nor at-spi set_text_contents can write into them; only real keyboard simulation (uinput/ydotool or a GNOME shell virtual device) reaches them
  - _verifier agent died: 402 available tokens exhausted_
- **UNVERIFIED** — there is no input-injection portal in xdg-desktop-portal: org.freedesktop.portal.InputCapture is capture/pass-through only (remote-desktop style), never programmatic key synthesis, so no portal session API can type for the app
  - _verifier agent died: 402 available tokens exhausted_
- **UNVERIFIED** — wl-clipboard (wl-copy/wl-paste) is installed and functional on this system and works without any privileges or extra installs, making clipboard+manual paste the zero-setup baseline
  - _verifier agent died: 402 available tokens exhausted_
- **UNVERIFIED** — a GNOME Shell extension can inject keystrokes/text into the focused surface by compositor-native means (clutter virtual input devices exist in mutter/libmutter-18 as clutter_seat_create_virtual_device), but it must be authored and installed by the user via ~/.local/share/gnome-shell/extensions and is the least 'no-admin' portable option
  - _verifier agent died: 402 available tokens exhausted_
- **UNVERIFIED** — the app must never write unless insert-mode is on and focus is verified, because uinput (ydotool) and shell-extension injection are global: they deliver keystrokes to whatever surface has focus, with no per-app targeting
  - _verifier agent died: 402 available tokens exhausted_
