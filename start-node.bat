@echo off
setlocal
cd /d "%~dp0"
call "%~dp0scripts\_bin.bat"
REM Data dir: %LOCALAPPDATA%\1919coin   (P2P port 11451, RPC port 11450)
REM Add peers with:  start-node.bat -addnode=IP:11451
"%BIN%\1919coind.exe" -printtoconsole -listen=1 %*
