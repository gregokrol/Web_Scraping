@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Web_Scraping
if not exist ".venv\Scripts\python.exe" (
  echo  Not installed yet - starting the installer...
  call "%~dp0install_windows.bat"
  if not exist ".venv\Scripts\python.exe" exit /b 1
)
set "PY=%~dp0.venv\Scripts\python.exe"

:menu
cls
echo.
echo  ==================================================
echo     Web_Scraping - affiliate deal finder
echo  ==================================================
echo    1   Scrape a product page or a whole store
echo    2   Search AliExpress          (needs ALIEXPRESS_* keys)
echo    3   Search Amazon              (needs AMAZON_* keys)
echo    4   Import a product feed      (XML / CSV file or URL)
echo    5   Open the dashboard in the browser
echo    6   Run the full automation now
echo    7   Show deals waiting for approval
echo    8   Approve deals
echo    9   Publish approved deals to Telegram
echo   10   Check the Telegram connection
echo   15   Find my Telegram channel id  (private channels)
echo   11   Schedule the automation every day
echo   12   Edit settings  (automation / sources / affiliate)
echo   13   Open the results folder
echo   14   Run the self-tests
echo    0   Exit
echo.
set "CH="
set /p "CH=  Choose a number: "
if "%CH%"=="1" goto scrape
if "%CH%"=="2" goto aliexpress
if "%CH%"=="3" goto amazon
if "%CH%"=="4" goto feed
if "%CH%"=="5" goto dashboard
if "%CH%"=="6" goto run
if "%CH%"=="7" goto alerts
if "%CH%"=="8" goto approve
if "%CH%"=="9" goto publish
if "%CH%"=="10" goto tgcheck
if "%CH%"=="11" goto schedule
if "%CH%"=="12" goto settings
if "%CH%"=="13" goto results
if "%CH%"=="14" goto tests
if "%CH%"=="15" goto tgfind
if "%CH%"=="0" exit /b 0
goto menu

:scrape
set "URL="
set /p "URL=  Product or store URL: "
if "%URL%"=="" goto menu
choice /M "  Is this a whole store (read its sitemap)"
if errorlevel 2 (
  "%PY%" main.py "%URL%" -v -c affiliate_config.json --sources sources.json
) else (
  "%PY%" main.py "%URL%" --discover sitemap --max-pages 50 -v -c affiliate_config.json --sources sources.json
)
goto done

:aliexpress
set "Q="
set /p "Q=  What to search on AliExpress: "
if "%Q%"=="" goto menu
"%PY%" main.py --aliexpress-search "%Q%" -c affiliate_config.json
goto done

:amazon
set "Q="
set /p "Q=  What to search on Amazon: "
if "%Q%"=="" goto menu
"%PY%" main.py --amazon-search "%Q%" -c affiliate_config.json
goto done

:feed
set "F="
set /p "F=  Feed file path or URL: "
if "%F%"=="" goto menu
"%PY%" main.py --feed "%F%" -c affiliate_config.json --sources sources.json
goto done

:dashboard
echo  The dashboard opens in your browser. Close this window or press Ctrl+C to stop it.
"%PY%" main.py dashboard --open
goto done

:run
"%PY%" main.py run --config automation.json
goto done

:alerts
"%PY%" main.py alerts --status pending
goto done

:approve
"%PY%" main.py alerts --status pending
set "IDS="
set /p "IDS=  Deal numbers to approve (e.g. 3 5 8): "
if "%IDS%"=="" goto menu
"%PY%" main.py approve %IDS%
goto done

:publish
"%PY%" main.py publish --telegram
goto done

:tgcheck
"%PY%" main.py telegram-check --test-admin
goto done

:tgfind
"%PY%" main.py telegram-check --find-channel
goto done

:schedule
set "T="
set /p "T=  Time to run every day, HH:MM (Enter = 07:43): "
if "%T%"=="" set "T=07:43"
"%PY%" main.py schedule --every daily --at %T% --create
goto done

:settings
start "" notepad automation.json
start "" notepad sources.json
start "" notepad affiliate_config.json
goto menu

:results
if not exist output mkdir output
start "" explorer output
goto menu

:tests
"%PY%" -m pytest -q -p no:cacheprovider
goto done

:done
echo.
pause
goto menu
