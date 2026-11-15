@echo off
setlocal
cd /d "%~dp0"
call "%~dp0scripts\_bin.bat"
if not exist "%BIN%\1919coin-qt.exe" ( echo [ERROR] 1919coin-qt.exe not found - run "build.bat gui" first. & exit /b 1 )
REM The GUI also runs the RPC server, so mine.bat works while it is open.
"%BIN%\1919coin-qt.exe" -listen=1 %*
