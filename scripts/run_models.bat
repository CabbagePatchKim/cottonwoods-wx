@echo off
rem Run by Windows Task Scheduler (task "Cottonwoods models", every 3 hours).
rem Archives every new 00/06/12/18z run of every model in config archive.models, then writes the model
rem comparison report (data\reports\models_latest.txt) and the website feed. Output is appended to data\logs\models.log.
rem
rem Works from any copy of the project: it runs the code in the folder it sits in. The PRODUCTION copy (an approved
rem release, see CLAUDE.md "Website feed") has a data_dir.txt naming the shared data folder, so every copy uses one
rem archive. Without data_dir.txt the copy's own data\ folder is used.
rem After each step a "----- <script> exit N (time)" line shows how far a pass got.
rem NEVER edit this file while a pass is running: cmd reads it as it goes and loses its place.
cd /d "%~dp0.."
set COTTONWOODS_DATA=
if exist data_dir.txt set /p COTTONWOODS_DATA=<data_dir.txt
if not defined COTTONWOODS_DATA set COTTONWOODS_DATA=%CD%\data
rem Import this copy's code, not whichever copy is pip-installed
set PYTHONPATH=%CD%\src
set PYTHONUTF8=1
set PY="C:\Users\patri\Documents\GIS_Class_Documents\NR6960\python.exe"
set LOG="%COTTONWOODS_DATA%\logs\models.log"
if not exist "%COTTONWOODS_DATA%\logs" mkdir "%COTTONWOODS_DATA%\logs"
echo ===== %DATE% %TIME% (%CD%) >> %LOG%
%PY% scripts\archive_forecasts.py >> %LOG% 2>&1
echo ----- archive_forecasts.py exit %ERRORLEVEL% (%TIME%) >> %LOG%
rem runs archived by an older derivation are derived again once (archive/forecasts.DERIVE_VERSION)
%PY% scripts\rederive_archive.py >> %LOG% 2>&1
echo ----- rederive_archive.py exit %ERRORLEVEL% (%TIME%) >> %LOG%
rem SNOTEL (+ NOAA) observations every pass, so a storm's end (12 h dry) is noticed and verified promptly
%PY% scripts\archive_obs.py --days 3 >> %LOG% 2>&1
echo ----- archive_obs.py exit %ERRORLEVEL% (%TIME%) >> %LOG%
%PY% scripts\storms.py >> %LOG% 2>&1
echo ----- storms.py exit %ERRORLEVEL% (%TIME%) >> %LOG%
%PY% scripts\model_report.py >> %LOG% 2>&1
echo ----- model_report.py exit %ERRORLEVEL% (%TIME%) >> %LOG%
rem Website feed: refuses (one "NOT PUBLISHED" line) unless this copy is an owner-approved release
%PY% scripts\publish_feed.py >> %LOG% 2>&1
echo ----- publish_feed.py exit %ERRORLEVEL% (%TIME%) >> %LOG%
