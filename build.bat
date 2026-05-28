@echo off
echo ========================================
echo  Building TOKENBANK...
echo ========================================

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo Python not found! Please install Python 3.8+ first.
    pause
    exit /b 1
)

pip install pyinstaller -q
pip install -r requirements.txt -q

echo.
echo Compiling .exe ...
echo.

pyinstaller --noconfirm --onefile --windowed ^
    --name TOKENBANK ^
    --hidden-import webview ^
    --hidden-import webview.platforms.winforms ^
    --hidden-import webview.platforms.edgechromium ^
    --hidden-import clr ^
    --hidden-import pystray._win32 ^
    --hidden-import PIL ^
    --hidden-import PIL._tkinter_finder ^
    --collect-data webview ^
    app.py

if %errorlevel% neq 0 (
    echo.
    echo Build FAILED!
    pause
    exit /b 1
)

echo.
echo ========================================
echo  Build complete!
echo  Output: dist\TOKENBANK.exe
echo  You can copy this single .exe to any
echo  Windows PC - no Python needed!
echo ========================================
pause
