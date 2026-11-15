; 1919coin Windows インストーラー (NSIS)
;
; 使い方:
;   1. build.bat gui でビルドする
;   2. build\bin の *.exe を installer\staging\ にコピーする
;   3. このファイルを右クリック → Compile NSIS Script
;      (または コマンドラインで makensis 1919coin.nsi)
;   → 1919coin-1.0.0-win64-setup.exe ができる
;
; 仕様:
;   - 管理者権限なしでインストールできる (ユーザーフォルダへ入れる)
;   - アンインストールしてもデータフォルダ (%LOCALAPPDATA%\1919coin) は消さない
;     (ウォレットとブロックチェーンが入っているため)

!include "MUI2.nsh"

!ifndef VERSION
  !define VERSION "1.0.0"
!endif
!define APPNAME "1919coin"

Name "${APPNAME} ${VERSION}"
OutFile "1919coin-${VERSION}-win64-setup.exe"
Unicode True
RequestExecutionLevel user
InstallDir "$LOCALAPPDATA\Programs\1919coin"
InstallDirRegKey HKCU "Software\1919coin" "InstallDir"
SetCompressor /SOLID lzma

!define MUI_ICON "..\share\pixmaps\bitcoin.ico"
!define MUI_UNICON "..\share\pixmaps\bitcoin.ico"
!define MUI_ABORTWARNING

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "..\COPYING"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_RUN "$INSTDIR\1919coin-qt.exe"
!define MUI_FINISHPAGE_RUN_TEXT "1919coin を起動する"
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "Japanese"
!insertmacro MUI_LANGUAGE "English"

VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "1919coin"
VIAddVersionKey "FileDescription" "1919coin セットアップ"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "LegalCopyright" "1919coin project"

Section "1919coin 本体" SecMain
  SectionIn RO
  SetOutPath "$INSTDIR"
  File "staging\*.exe"
  WriteUninstaller "$INSTDIR\Uninstall.exe"

  CreateDirectory "$SMPROGRAMS\1919coin"
  CreateShortcut "$SMPROGRAMS\1919coin\1919coin.lnk" "$INSTDIR\1919coin-qt.exe"
  CreateShortcut "$SMPROGRAMS\1919coin\アンインストール.lnk" "$INSTDIR\Uninstall.exe"

  WriteRegStr HKCU "Software\1919coin" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\1919coin" "DisplayName" "1919coin ${VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\1919coin" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\1919coin" "DisplayIcon" "$INSTDIR\1919coin-qt.exe"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\1919coin" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\1919coin" "Publisher" "1919coin"
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\1919coin" "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\1919coin" "NoRepair" 1
SectionEnd

Section "デスクトップにショートカットを作る" SecDesktop
  CreateShortcut "$DESKTOP\1919coin.lnk" "$INSTDIR\1919coin-qt.exe"
SectionEnd

Section "Uninstall"
  Delete "$INSTDIR\*.exe"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"

  Delete "$DESKTOP\1919coin.lnk"
  Delete "$SMPROGRAMS\1919coin\1919coin.lnk"
  Delete "$SMPROGRAMS\1919coin\アンインストール.lnk"
  RMDir "$SMPROGRAMS\1919coin"

  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\1919coin"
  DeleteRegKey HKCU "Software\1919coin"

  ; データフォルダ (%LOCALAPPDATA%\1919coin) は意図的に残す。
  ; ウォレットを消してしまう事故を防ぐため。
SectionEnd
