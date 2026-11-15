@echo off
if exist "%~dp0..\build\bin\Release\1919coind.exe" ( set "BIN=%~dp0..\build\bin\Release" ) else ( set "BIN=%~dp0..\build\bin" )
if not exist "%BIN%\1919coind.exe" ( echo [ERROR] 1919coind.exe not found - run build.bat first. & exit /b 1 )
