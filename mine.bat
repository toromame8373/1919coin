@echo off
setlocal enableextensions
cd /d "%~dp0"
call "%~dp0scripts\_bin.bat"
set "CLI=%BIN%\1919coin-cli.exe -rpcclienttimeout=0"
REM Threads: set INMU_MINE_THREADS=N before running to limit CPU use (default: all).
REM The first block needs ~1 minute extra to build the 2 GiB RandomX dataset.

%CLI% createwallet miner >nul 2>&1
%CLI% loadwallet miner >nul 2>&1
set "ADDR=%~1"
if "%ADDR%"=="" (
  for /f "usebackq delims=" %%a in (`"%BIN%\1919coin-cli.exe" -rpcwallet=miner getnewaddress`) do set "ADDR=%%a"
)
echo Mining to %ADDR%   (Ctrl+C to stop)
:loop
%CLI% generatetoaddress 1 %ADDR% 1000000000
if errorlevel 1 ( echo RPC error - is start-node.bat running? & timeout /t 5 >nul )
goto loop
