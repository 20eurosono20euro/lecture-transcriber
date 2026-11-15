# -*- mode: python ; coding: utf-8 -*-
#
# Spec PyInstaller per il binario **headless/CLI** di Lecture Transcriber,
# pensato per Windows e macOS (il core e' portabile; la GUI GTK resta
# Linux-only e non viene impacchettata qui).
#
#   uso (sulla macchina bersaglio, dentro un venv con le dipendenze):
#     pip install pyinstaller
#     pyinstaller packaging/cross-platform/lecture-transcriber.spec
#
# Prodotto: un singolo eseguibile (onefile) chiamato
#   lecture-transcriber.exe  (Windows)
#   lecture-transcriber      (macOS)
# che trascrive un file audio e/o usa DETTA CLI senza interfaccia grafica.
#
# I modelli Whisper NON entrano nel bundle: pesano ~1,6 GB e si scaricano
# al primo utilizzo nella cartella dati della piattaforma (vedi
# config.py -> _platform_home_dirs). Il binario li trova da solo.
#
# La GUI (src/lecture_transcriber/ui.py, theme.py, app.py) e' esclusa
# esplicitamente: dipende da PyGObject/GTK che non esiste come wheel
# stabile su Windows/macOS.
#
# ATTENZIONE alle path: PyInstaller espone SPECPATH (directory assoluta
# dello spec) e la usa come ancora -- le path relative dentro Analysis()
# sarebbero risolte rispetto alla cwd al momento del build, che puo'
# cambiare a seconda di come si lancia pyinstaller. Radice del progetto
# calcolata da SPECPATH rende il build indipendente dalla cwd.

import os

# 2 livelli sopra: cross-platform -> packaging -> radice del progetto
ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir, os.pardir))

a = Analysis(
    [os.path.join(ROOT, "scripts", "transcribe_file.py")],
    pathex=[os.path.join(ROOT, "src")],
    binaries=[],
    datas=[],
    hiddenimports=[
        "ctranslate2",
        "tokenizers",
        "huggingface_hub",
        # soxr espone piu' moduli binari
        "soxr",
        "soxr._h_soxr",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "gi",
        "cairo",
        "PyGObject",
        "weasyprint",
        # la GUI non deve essere impacchettata
        "lecture_transcriber.ui",
        "lecture_transcriber.theme",
        "lecture_transcriber.app",
        "lecture_transcriber.shortcuts",
        "lecture_transcriber.shortcuts_gsettings",
        "lecture_transcriber.insertion",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="lecture-transcriber",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
