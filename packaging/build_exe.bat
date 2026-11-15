@echo off
rem Builds dist\PiVault.exe (Windows). Double-click this file or run it from anywhere.
cd /d "%~dp0.."
py -m pip install -r requirements.txt pyinstaller || goto :fail
py -m PyInstaller --onefile --name PiVault --icon packaging\pivault.ico --paths server --add-data "webui;webui" server\server.py || goto :fail
echo.
echo Done. Your program is dist\PiVault.exe
echo Copy it to the computer that will run the server and double-click it.
echo It asks for an invite code and who can connect the first time.
pause
exit /b 0
:fail
echo.
echo The build failed. Read the messages above.
pause
exit /b 1
