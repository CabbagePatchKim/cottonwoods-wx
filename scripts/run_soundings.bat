@echo off
rem Run by Windows Task Scheduler (task "Cottonwoods soundings", 8 am and 8 pm daily).
rem Collects the latest Salt Lake City balloon soundings and appends the output to data\logs\soundings.log.
cd /d "C:\Users\patri\Documents\anaconda_projects\cottonwood_snow_models\cottonwoods-wx2\cottonwoods-wx"
if not exist data\logs mkdir data\logs
set PYTHONUTF8=1
echo ===== %DATE% %TIME% >> data\logs\soundings.log
"C:\Users\patri\Documents\GIS_Class_Documents\NR6960\python.exe" scripts\archive_soundings.py >> data\logs\soundings.log 2>&1
