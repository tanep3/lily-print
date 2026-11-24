@echo off
cd /d %~dp0
echo Building Lily Print exe...
pip install lxd02-printer tkinterdnd2 pyinstaller
if errorlevel 1 goto error
pyinstaller --onefile --windowed --name LilyPrint --collect-all tkinterdnd2 --noconfirm lily_print.py
if errorlevel 1 goto error
echo.
echo Done: dist\LilyPrint.exe
echo (lily_print_config.json will be created next to the exe)
pause
exit /b 0
:error
echo.
echo Build failed. Please check the error messages above.
pause
exit /b 1
