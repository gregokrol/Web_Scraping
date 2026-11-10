@echo off
setlocal EnableExtensions
title Web_Scraping installer
rem ===================================================================
rem  Web_Scraping - one-click installer for Windows 10/11
rem  Double-click this file. It can live anywhere (e.g. Downloads).
rem  - installs Python 3.12 with winget if needed (asks first)
rem  - downloads the code (git, or ZIP when git is missing)
rem  - creates .venv, installs libraries, copies example configs
rem  - runs the self-tests and puts a "Web Scraping" shortcut on the desktop
rem ===================================================================

set "BRANCH=claude/web-scraping-v3"
set "REPO=https://github.com/gregokrol/Web_Scraping.git"
set "ZIPURL=https://github.com/gregokrol/Web_Scraping/archive/refs/heads/claude/web-scraping-v3.zip"
set "TARGET=C:\Web_Scraping"
rem If this script sits inside the project folder, install right here
if exist "%~dp0main.py" set "TARGET=%~dp0"
if "%TARGET:~-1%"=="\" set "TARGET=%TARGET:~0,-1%"

echo.
echo  ==============================================
echo     Web_Scraping installer
echo  ==============================================
echo  Install folder: %TARGET%
echo.

rem ------------------------------------------------------------ 1. Python
where py >nul 2>nul
if errorlevel 1 goto no_python
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if errorlevel 1 goto no_python
for /f "delims=" %%V in ('py -3 --version') do echo  [ok] %%V
goto have_python

:no_python
echo  Python 3.11 or newer was not found.
where winget >nul 2>nul
if errorlevel 1 goto python_manual
choice /M " Install Python 3.12 now (winget)"
if errorlevel 2 goto python_manual
winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
echo.
echo  Python was installed. CLOSE this window and double-click install_windows.bat again.
pause
exit /b 0

:python_manual
echo  Please install Python from https://www.python.org/downloads/
echo  IMPORTANT: tick "Add python.exe to PATH" during setup, then run this file again.
pause
exit /b 1

:have_python
rem ------------------------------------------------------------ 2. Code
if exist "%TARGET%\main.py" goto update_code
where git >nul 2>nul
if errorlevel 1 goto zip_download
echo  Downloading the code with git...
git clone -b %BRANCH% %REPO% "%TARGET%"
if errorlevel 1 goto zip_download
goto have_code

:zip_download
echo  Downloading the code as ZIP...
set "UNZIP=%TEMP%\web_scraping_unzip"
if exist "%UNZIP%" rmdir /s /q "%UNZIP%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; Invoke-WebRequest -UseBasicParsing -Uri '%ZIPURL%' -OutFile '%TEMP%\web_scraping.zip'; Expand-Archive -Path '%TEMP%\web_scraping.zip' -DestinationPath '%UNZIP%' -Force"
if errorlevel 1 goto download_failed
for /d %%D in ("%UNZIP%\Web_Scraping-*") do xcopy /E /I /Y /Q "%%D" "%TARGET%" >nul
if not exist "%TARGET%\main.py" goto download_failed
goto have_code

:download_failed
echo  [error] Could not download the code. Check the internet connection and try again.
pause
exit /b 1

:update_code
if not exist "%TARGET%\.git" goto have_code
where git >nul 2>nul
if errorlevel 1 goto have_code
echo  Updating the existing copy...
git -C "%TARGET%" fetch origin %BRANCH%
git -C "%TARGET%" checkout %BRANCH%
git -C "%TARGET%" pull --ff-only origin %BRANCH%

:have_code
cd /d "%TARGET%"
echo  [ok] Code is in %CD%

rem ------------------------------------------------------------ 3. Virtual environment + libraries
if exist ".venv\Scripts\python.exe" goto have_venv
echo  Creating the virtual environment...
py -3 -m venv .venv
if errorlevel 1 goto venv_failed
:have_venv
set "PY=%CD%\.venv\Scripts\python.exe"
echo  Installing libraries - this can take a few minutes...
"%PY%" -m pip install --upgrade pip --quiet
"%PY%" -m pip install -r requirements-dev.txt --quiet
if errorlevel 1 goto pip_failed
echo  [ok] Libraries installed

choice /M " Also install support for JavaScript-heavy sites (Playwright, about 150 MB)"
if errorlevel 2 goto configs
"%PY%" -m pip install -r requirements-js.txt --quiet
"%PY%" -m playwright install chromium

:configs
rem ------------------------------------------------------------ 4. Config files (never overwritten)
if not exist affiliate_config.json copy affiliate_config.example.json affiliate_config.json >nul
if not exist sources.json copy sources.example.json sources.json >nul
if not exist automation.json copy automation.example.json automation.json >nul
echo  [ok] Config files ready: affiliate_config.json, sources.json, automation.json

rem ------------------------------------------------------------ 5. Self-test
echo  Running self-tests...
"%PY%" -m pytest -q -p no:cacheprovider
if errorlevel 1 (
  echo  [warning] Some tests failed. Copy the text above and send it to Claude.
) else (
  echo  [ok] All tests passed
)

rem ------------------------------------------------------------ 6. Desktop shortcut
rem opens the app window (pythonw = no CMD screen). start_windows.bat keeps the text menu.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\Web Scraping.lnk'); $s.TargetPath='%CD%\.venv\Scripts\pythonw.exe'; $s.Arguments=[char]34+'%CD%\app_window.pyw'+[char]34; $s.WorkingDirectory='%CD%'; $s.IconLocation='%CD%\affiliate_scraper\dashboard\static\app.ico'; $s.Description='Web Scraping - price tracking and deals'; $s.Save()" >nul 2>nul
if not errorlevel 1 echo  [ok] Shortcut "Web Scraping" created on the desktop

echo.
echo  ==============================================
echo   Done. Start with the "Web Scraping" desktop
echo   shortcut (app window). The text menu is
echo   still in start_windows.bat
echo  ==============================================
pause
exit /b 0

:venv_failed
echo  [error] Could not create the virtual environment.
pause
exit /b 1

:pip_failed
echo  [error] Installing libraries failed. Copy the text above and send it to Claude.
pause
exit /b 1
