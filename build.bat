@echo off
setlocal enableextensions
cd /d "%~dp0"
echo === 1919coin build (Visual Studio 2026 / MSVC, static, x64) ===

REM ---- 1. Visual Studio developer environment (newest installed VS) ----
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
if not exist "%VSWHERE%" (
  echo [ERROR] vswhere.exe not found. Install Visual Studio 2026 with "Desktop development with C++".
  exit /b 1
)
set "VSINSTALL="
for /f "usebackq delims=" %%i in (`"%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do set "VSINSTALL=%%i"
if "%VSINSTALL%"=="" (
  echo [ERROR] No Visual Studio with the C++ x64 toolset found.
  exit /b 1
)
echo Using Visual Studio: %VSINSTALL%
call "%VSINSTALL%\VC\Auxiliary\Build\vcvars64.bat" >nul || exit /b 1

REM ---- 2. vcpkg (clone next to this script if VCPKG_ROOT is not set) ----
if not defined VCPKG_ROOT (
  if not exist "%~dp0.vcpkg\vcpkg.exe" (
    where git >nul 2>&1 || ( echo [ERROR] git is required to fetch vcpkg. & exit /b 1 )
    echo Cloning vcpkg ^(full clone is required for the manifest baseline^)...
    git clone https://github.com/microsoft/vcpkg.git "%~dp0.vcpkg" || exit /b 1
    call "%~dp0.vcpkg\bootstrap-vcpkg.bat" -disableMetrics || exit /b 1
  )
  set "VCPKG_ROOT=%~dp0.vcpkg"
)
echo Using vcpkg: %VCPKG_ROOT%

REM ---- 3. Configure + build ----
REM Parallelism (MSVC C1060 guard): ninja defaults to all cores, and the
REM heavy wallet files (receive.cpp etc.) can exhaust the compiler heap.
REM Default is 2 jobs. Override with a job count as the last argument,
REM e.g. build.bat gui 1  (or build.bat 1), or set CMAKE_BUILD_PARALLEL_LEVEL
REM before running. If C1060 still appears at 2, retry with 1.
if not defined CMAKE_BUILD_PARALLEL_LEVEL set "CMAKE_BUILD_PARALLEL_LEVEL=2"
if /i "%~1"=="gui" (
  if not "%~2"=="" set "CMAKE_BUILD_PARALLEL_LEVEL=%~2"
) else if not "%~1"=="" (
  set "CMAKE_BUILD_PARALLEL_LEVEL=%~1"
)
echo Build parallelism: %CMAKE_BUILD_PARALLEL_LEVEL% job(s) (override: build.bat [gui] N)
set "PRESET=yju-ninja"
if /i "%~1"=="gui" set "PRESET=yju-ninja-gui"
echo Preset: %PRESET%
cmake -B build --preset %PRESET% -DVCPKG_INSTALL_OPTIONS=--x-buildtrees-root=%SystemDrive%\ybt || exit /b 1
cmake --build build --config Release || exit /b 1

echo.
echo === Build finished ===
echo   build\bin\1919coind.exe
echo   build\bin\1919coin-cli.exe
if /i "%~1"=="gui" echo   build\bin\1919coin-qt.exe
echo Next: start-node.bat ^(or start-gui.bat^)  then  mine.bat
endlocal
