@echo off
REM ===========================================================================
REM  Costruisce il binario di Lecture Transcriber per Windows.
REM
REM  Prodotto: lecture-transcriber.exe — un eseguibile onefile headless che
REM  trascrive file audio da riga di comando (nessuna GUI GTK: la GUI su
REM  Windows non esiste come build stabile di PyGObject; il core ASR e'
REM  portabile e qui si impacchetta).
REM
REM  Uso, da questo file (packaging\cross-platform) lanciato dal root del
REM  progetto oppure con PowerShell:

REM  REQUISITI (una volta sola):
REM    * Python 3.11+ da python.org (spunta "Add to PATH")
REM    * Microsoft Visual C++ Redistributable (per ctranslate2)
REM
REM  Da PowerShell, nella cartella del progetto:
REM      .\packaging\cross-platform\build-win.bat
REM ===========================================================================
setlocal
cd /d "%~dp0..\.."

echo ==^> Creo il virtualenv
if not exist ".venv-win" (
    python -m venv .venv-win
) else (
    echo    (esiste gia')
)

echo ==^> Installo le dipendenze runtime
.\.venv-win\Scripts\python -m pip install --upgrade pip wheel setuptools
.\.venv-win\Scripts\python -m pip install numpy sounddevice soxr faster-whisper huggingface-hub pyinstaller

echo ==^> Build PyInstaller (onefile, headless)
.\.venv-win\Scripts\pyinstaller --noconfirm --clean --specpath packaging\cross-platform packaging\cross-platform\lecture-transcriber.spec

echo.
echo ==^> Fatto: dist\lecture-transcriber.exe
echo.
echo Prova:
echo     dist\lecture-transcriber.exe --help
echo     dist\lecture-transcriber.exe mio.wav --course "Analisi" --export out.md

endlocal
