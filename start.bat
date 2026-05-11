@echo off
echo ========================================
echo   Student Email Extractor - Starting...
echo ========================================
echo.

pip install -r requirements.txt --quiet

echo.
echo Starting server at http://localhost:8001
echo Open your browser and go to: http://localhost:8001
echo.
start "" "http://localhost:8001"
python server.py
pause
