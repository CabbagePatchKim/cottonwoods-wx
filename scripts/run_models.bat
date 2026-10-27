@echo off
rem Run by Windows Task Scheduler (task "Cottonwoods models", every 3 hours).
rem Archives every new 00/06/12/18z run of every model in config archive.models, then writes the model
rem comparison report (data\reports\models_latest.txt). Output is appended to data\logs\models.log.
cd /d "C:\Users\patri\Documents\anaconda_projects\cottonwood_snow_models\cottonwoods-wx2\cottonwoods-wx"
if not exist data\logs mkdir data\logs
set PYTHONUTF8=1
echo ===== %DATE% %TIME% >> data\logs\models.log
"C:\Users\patri\Documents\GIS_Class_Documents\NR6960\python.exe" scripts\archive_forecasts.py >> data\logs\models.log 2>&1
rem SNOTEL (+ NOAA) observations every pass, so a storm's end (12 h dry) is noticed and verified promptly
"C:\Users\patri\Documents\GIS_Class_Documents\NR6960\python.exe" scripts\archive_obs.py --days 3 >> data\logs\models.log 2>&1
"C:\Users\patri\Documents\GIS_Class_Documents\NR6960\python.exe" scripts\storms.py >> data\logs\models.log 2>&1
"C:\Users\patri\Documents\GIS_Class_Documents\NR6960\python.exe" scripts\model_report.py >> data\logs\models.log 2>&1
