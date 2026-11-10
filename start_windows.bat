@echo off
setlocal EnableExtensions
rem UTF-8 console so Hebrew search words typed in the menu reach Python intact
chcp 65001 >nul
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
echo    5   Open the app window (dashboard)
echo    6   Run the full automation now
echo    7   Show deals waiting for approval
echo    8   Approve deals
echo    9   Publish approved deals to Telegram
echo   10   Check the Telegram connection
echo   11   Schedule the automation  (once or twice a day)
echo   12   Edit settings  (automation / sources / affiliate)
echo   13   Open the results folder
echo   14   Run the self-tests
echo   15   Find my Telegram channel id  (private channels)
echo   16   Add product links to track
echo   17   Show / remove tracked product links
echo   18   Post a test message in the channel
echo   19   Add an automatic search  (e.g. "puzzle 1000")
echo   20   Show / remove automatic searches
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
if "%CH%"=="16" goto addtarget
if "%CH%"=="17" goto listtargets
if "%CH%"=="18" goto tgchannel
if "%CH%"=="19" goto addsearch
if "%CH%"=="20" goto listsearches
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
rem the app window runs on its own (no console) and stops by itself after its window closes
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0app_window.pyw"
goto menu

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

:addtarget
set "URL="
set /p "URL=  Paste a product link (Enter = finish): "
if "%URL%"=="" goto menu
"%PY%" main.py targets add "%URL%"
goto addtarget

:listtargets
"%PY%" main.py targets list
set "N="
set /p "N=  Number to remove (Enter = back): "
if "%N%"=="" goto menu
"%PY%" main.py targets remove %N%
goto done

:tgchannel
"%PY%" main.py telegram-check --test-channel
goto done

:addsearch
set "Q="
set /p "Q=  Search words (Enter = finish): "
if "%Q%"=="" goto menu
"%PY%" main.py searches add "%Q%"
goto addsearch

:listsearches
"%PY%" main.py searches list
set "N="
set /p "N=  Number to remove (Enter = back): "
if "%N%"=="" goto menu
"%PY%" main.py searches remove %N%
goto done

:schedule
set "T="
set /p "T=  First run time, HH:MM (Enter = 08:13): "
if "%T%"=="" set "T=08:13"
choice /C 12 /M "  1 = once a day, 2 = twice a day (every 12 hours)"
if errorlevel 2 (
  "%PY%" main.py schedule --every twice-daily --at %T% --hidden --create
) else (
  "%PY%" main.py schedule --every daily --at %T% --hidden --create
)
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
