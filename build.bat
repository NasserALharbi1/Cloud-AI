@echo off
setlocal
echo ===================================================
echo   Building Cloud AI Agent single-file executable
echo ===================================================

cd /d "%~dp0"

python -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --onefile ^
    --name cloud ^
    --icon "cloud.ico" ^
    --add-data "cloud.ico;." ^
    --add-data "cloud.png;." ^
    --add-data "cloud_agent/tui/styles.tcss;cloud_agent/tui" ^
    --hidden-import "cloud_agent.plugins.gmail" ^
    --hidden-import "cloud_agent.plugins.gcal" ^
    --hidden-import "cloud_agent.plugins.notion" ^
    --hidden-import "cloud_agent.plugins.mcp_client" ^
    --hidden-import "cloud_agent.tui.gui_app" ^
    --hidden-import "keyring.backends.Windows" ^
    --hidden-import "apscheduler.triggers.interval" ^
    --exclude-module "PySide6.QtWebEngineCore" ^
    --exclude-module "PySide6.QtWebEngineWidgets" ^
    --exclude-module "PySide6.QtPdf" ^
    --exclude-module "PySide6.QtQml" ^
    --exclude-module "PySide6.QtQuick" ^
    --collect-all textual ^
    cloud.py

if errorlevel 1 (
    echo [ERROR] PyInstaller build failed.
    exit /b 1
)

copy /Y "%~dp0dist\cloud.exe" "%~dp0cloud.exe" >nul
ie4uinit.exe -ClearIconCache >nul 2>&1
ie4uinit.exe -show >nul 2>&1
echo.
echo [SUCCESS] Built single executable at: %~dp0cloud.exe and %~dp0dist\cloud.exe
endlocal
