@echo off
rem Sends the owner's hand entries (data\entries\, e.g. Alta Collins storm totals) to the cloud bucket, so the cloud
rem passes use them. Only those files are sent; nothing else in the bucket is touched. Double-click, or run in a terminal.
cd /d "%~dp0.."
set PYTHONUTF8=1
rem boto3 (bucket access) lives in its own folder so the main Python environment is left alone
set PYTHONPATH=%CD%\src;%USERPROFILE%\.cwx_pylib
"C:\Users\patri\Documents\GIS_Class_Documents\NR6960\python.exe" scripts\bucket_sync.py push --only entries/storm_totals.csv --env-file "%USERPROFILE%\.cwx_r2.env"
pause
