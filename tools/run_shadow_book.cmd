@echo off
rem Track C forward shadow book (docs/RUNBOOKS.md).
rem Appends the next calendar session from the inbox; prints counts only.
rem %1 = shadow book directory, %2 = inbox directory (defaults below).
rem Exit 0 appended / already recorded / calendar exhausted, 2 refused.
setlocal
set "REPO=%~dp0.."
set PYTHONUTF8=1
set "PYTHONPATH=%REPO%\python\src"
set "OUT=%REPO%\data\shadow_book"
set "INBOX=%REPO%\data\shadow_book_inbox"
if not "%~1"=="" set "OUT=%~1"
if not "%~2"=="" set "INBOX=%~2"
if not exist "%OUT%" mkdir "%OUT%"
echo START %DATE% %TIME% >> "%OUT%\shadow_book.log"
python -m iap.paper.shadow_book run --config "%REPO%\configs\paper\shadow_book.json" --inbox "%INBOX%" --out "%OUT%" >> "%OUT%\shadow_book.log" 2>&1
set RC=%ERRORLEVEL%
echo EXIT %RC% >> "%OUT%\shadow_book.log"
exit /b %RC%
