; bom-pdf 설치 프로그램(NSIS 3). 이 파일은 UTF-8 BOM 으로 저장해야 makensis 가 한글을 읽는다(위키 P18).
; 직접 부르지 말고 tools/build_installer.py 로 만든다:
;   makensis /V2 /DVERSION=0.1.0 /DSOURCE=<dist\bom-pdf> /DOUTFILE=<release\bom-pdf-Setup-0.1.0.exe> tools\installer.nsi
;
; - 이 사용자에게만 설치(관리자 권한 없음): %LOCALAPPDATA%\Programs\bom-pdf
; - 시작 메뉴 바로 가기, 바탕 화면 바로 가기(선택, 기본 켬), 프로그램 추가/제거 항목(HKCU)
; - PDF 파일 연결(선택, 기본 켬, HKCU 만): ProgID BomPdf.Document, .pdf\OpenWithProgids,
;   Capabilities + RegisteredApplications(설정 → 기본 앱 목록에 bom-pdf 가 보이게), SHChangeNotify.
;   기본 앱(UserChoice)은 쓰지 않는다 — Windows 는 사용자가 설정 앱에서 직접 고르게 한다. 마침 화면에서 그 화면을 연다.
; - 사용자 데이터(%APPDATA%\bom-pdf: 설정·주석·세션·로그)는 설치·제거 모두 건드리지 않는다.
; - 업그레이드: 같은 폴더에 덮어쓴다. 실행 중이면 닫아 달라고 묻고, 예전 _internal 은 지운 뒤 새로 넣는다(위키 P08·P09).
; - 점검용 /TEST: 바로 가기·프로그램 목록·파일 연결을 만들지 않는다(tools/check_installer.py 가 임시 폴더에 설치해 본다).
; - GNU AGPL v3. 소스 위치는 build_installer.py 가 /DSOURCE_URL 로 넘긴다.

Unicode true
SetCompressor /SOLID lzma
RequestExecutionLevel user
ManifestDPIAware true

!include "MUI2.nsh"
!include "FileFunc.nsh"
!include "LogicLib.nsh"
!include "Sections.nsh"

!define APP "bom-pdf"
!define EXE "bom-pdf.exe"
!define PUBLISHER "bom-pdf"
!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\bom-pdf"
!define PROGID "BomPdf.Document"
!define APP_KEY "Software\bom-pdf"
!define CAPS_KEY "Software\bom-pdf\Capabilities"
; RegisteredApplications 값 이름 — src/bom_pdf/ui/desktop.py REGISTERED_APP 과 같아야 한다
!define REG_APP "bom-pdf"
!define DEFAULT_APPS_URI "ms-settings:defaultapps?registeredAppUser=bom-pdf"
!define TEST_MARK "test-install.txt"
!define SHCNE_ASSOCCHANGED 0x08000000

Name "${APP} ${VERSION}"
OutFile "${OUTFILE}"
InstallDir "$LOCALAPPDATA\Programs\${APP}"
InstallDirRegKey HKCU "${UNINSTALL_KEY}" "InstallLocation"
BrandingText "${APP} ${VERSION} — GNU AGPL v3"
VIProductVersion "${VERSION}.0"
VIAddVersionKey /LANG=1042 "ProductName" "${APP}"
VIAddVersionKey /LANG=1042 "ProductVersion" "${VERSION}"
VIAddVersionKey /LANG=1042 "FileVersion" "${VERSION}"
VIAddVersionKey /LANG=1042 "CompanyName" "${PUBLISHER}"
VIAddVersionKey /LANG=1042 "FileDescription" "${APP} 설치"
VIAddVersionKey /LANG=1042 "LegalCopyright" "GNU AGPL v3 — ${SOURCE_URL}"

!define MUI_ICON "${SOURCE}\_internal\assets\bom-pdf.ico"
!define MUI_UNICON "${SOURCE}\_internal\assets\bom-pdf.ico"
!define MUI_WELCOMEFINISHPAGE_BITMAP "${__FILEDIR__}\..\assets\installer-side.bmp"
!define MUI_UNWELCOMEFINISHPAGE_BITMAP "${__FILEDIR__}\..\assets\installer-side.bmp"
!define MUI_ABORTWARNING
!define MUI_COMPONENTSPAGE_NODESC

!define MUI_WELCOMEPAGE_TITLE "bom-pdf ${VERSION} 설치"
!define MUI_WELCOMEPAGE_TEXT "법률문서용 PDF 리더/편집기를 이 사용자 계정에 설치합니다(관리자 권한 필요 없음).$\r$\n$\r$\n이 프로그램은 GNU AGPL v3 로 배포되는 자유 소프트웨어이며 보증 없이 제공됩니다. 소스 코드: ${SOURCE_URL}$\r$\n$\r$\n원본 PDF·사진은 고치지 않으며, 설정·주석·기록(%APPDATA%\bom-pdf)은 설치·제거 때 지우지 않습니다.$\r$\n$\r$\nbom-pdf 가 열려 있으면 먼저 닫아 주십시오."
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES

!define MUI_FINISHPAGE_TITLE "설치를 마쳤습니다"
!define MUI_FINISHPAGE_TEXT "bom-pdf 를 기본 PDF 프로그램으로 쓰려면 아래 첫 칸을 켠 채 [마침]을 누르십시오. Windows 설정의 bom-pdf 화면이 열리면 .pdf 를 누르고 bom-pdf 를 고른 뒤 [기본값으로 설정]을 누르면 됩니다.$\r$\n$\r$\n나중에 하려면 bom-pdf 의 [파일] → [기본 PDF 프로그램으로 지정…]을 쓰십시오."
!define MUI_FINISHPAGE_SHOWREADME ""
!define MUI_FINISHPAGE_SHOWREADME_TEXT "기본 PDF 프로그램 지정 화면 열기 (Windows 설정)"
!define MUI_FINISHPAGE_SHOWREADME_FUNCTION OpenDefaultApps
!define MUI_FINISHPAGE_RUN "$INSTDIR\${EXE}"
!define MUI_FINISHPAGE_RUN_TEXT "bom-pdf 실행"
!define MUI_FINISHPAGE_RUN_NOTCHECKED
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "Korean"

Var TestMode

Function .onInit
  StrCpy $TestMode "0"
  ${GetParameters} $0
  ClearErrors
  ${GetOptions} $0 "/TEST" $1
  ${IfNot} ${Errors}
    StrCpy $TestMode "1"
  ${EndIf}
  ; O6: Windows 11(빌드 22000 이상)에서만 설치한다. /WINBUILD=<번호> 는 점검용(다른 빌드인 척) — check_installer.py
  ReadRegStr $2 HKLM "SOFTWARE\Microsoft\Windows NT\CurrentVersion" "CurrentBuildNumber"
  ClearErrors
  ${GetOptions} $0 "/WINBUILD=" $1
  ${IfNot} ${Errors}
    StrCpy $2 $1
  ${EndIf}
  ${If} $2 == ""
    StrCpy $2 "0"
  ${EndIf}
  ${If} $2 < 22000
    MessageBox MB_OK|MB_ICONSTOP "bom-pdf 는 Windows 11 에서만 쓸 수 있습니다.$\r$\n이 PC 는 Windows 빌드 $2 입니다(Windows 11 은 22000 이상).$\r$\n$\r$\n설치를 멈춥니다. 아무것도 바뀌지 않았습니다." /SD IDOK
    SetErrorLevel 3
    Quit
  ${EndIf}
FunctionEnd

; 실행 중인 EXE 는 쓰기로 열 수 없다 — 열어 보고 막히면 닫아 달라고 한다(렌더 작업자도 같은 EXE).
!macro WAIT_NOT_RUNNING UN
Function ${UN}WaitNotRunning
  ${If} ${FileExists} "$INSTDIR\${EXE}"
    retry:
      ClearErrors
      FileOpen $0 "$INSTDIR\${EXE}" a
      ${If} ${Errors}
        MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "bom-pdf 가 실행 중입니다.$\r$\n열린 bom-pdf 창을 모두 닫은 뒤 [다시 시도]를 누르세요." /SD IDCANCEL IDRETRY retry
        Abort
      ${EndIf}
      FileClose $0
  ${EndIf}
FunctionEnd
!macroend
!insertmacro WAIT_NOT_RUNNING ""
!insertmacro WAIT_NOT_RUNNING "un."

Section "bom-pdf (필수)" SecMain
  SectionIn RO
  SetOutPath "$INSTDIR"
  ${If} ${FileExists} "$INSTDIR\${EXE}"
    Call WaitNotRunning
    RMDir /r "$INSTDIR\_internal"   ; 예전 판의 남는 파일을 지우고 새로 넣는다
  ${EndIf}
  File /r "${SOURCE}\*.*"
  WriteUninstaller "$INSTDIR\uninstall.exe"

  ${If} $TestMode == "1"
    FileOpen $0 "$INSTDIR\${TEST_MARK}" w
    FileClose $0
  ${Else}
    CreateShortCut "$SMPROGRAMS\${APP}.lnk" "$INSTDIR\${EXE}" "" "$INSTDIR\${EXE}" 0 SW_SHOWNORMAL "" "PDF 리더/편집기"
    WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayName" "${APP}"
    WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayVersion" "${VERSION}"
    WriteRegStr HKCU "${UNINSTALL_KEY}" "Publisher" "${PUBLISHER}"
    WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayIcon" "$INSTDIR\${EXE}"
    WriteRegStr HKCU "${UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
    WriteRegStr HKCU "${UNINSTALL_KEY}" "UninstallString" '"$INSTDIR\uninstall.exe"'
    WriteRegStr HKCU "${UNINSTALL_KEY}" "QuietUninstallString" '"$INSTDIR\uninstall.exe" /S'
    WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoModify" 1
    WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoRepair" 1
    ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
    WriteRegDWORD HKCU "${UNINSTALL_KEY}" "EstimatedSize" $0
  ${EndIf}
SectionEnd

Section "바탕 화면 바로 가기" SecDesktop
  ${If} $TestMode != "1"
    CreateShortCut "$DESKTOP\${APP}.lnk" "$INSTDIR\${EXE}" "" "$INSTDIR\${EXE}" 0 SW_SHOWNORMAL "" "PDF 리더/편집기"
  ${EndIf}
SectionEnd

Section "PDF 파일 연결 (기본 앱 후보로 등록)" SecAssoc
  ${If} $TestMode != "1"
    ; ProgID — 탐색기에서 여러 파일을 고르고 Enter 를 누르면 파일마다 이 명령이 실행되고, 단일 인스턴스가 한 창의 탭으로 모은다
    WriteRegStr HKCU "Software\Classes\${PROGID}" "" "PDF 문서"
    WriteRegStr HKCU "Software\Classes\${PROGID}" "FriendlyTypeName" "PDF 문서 (bom-pdf)"
    WriteRegStr HKCU "Software\Classes\${PROGID}\DefaultIcon" "" '"$INSTDIR\${EXE}",0'
    WriteRegStr HKCU "Software\Classes\${PROGID}\shell" "" "open"
    WriteRegStr HKCU "Software\Classes\${PROGID}\shell\open\command" "" '"$INSTDIR\${EXE}" "%1"'
    WriteRegStr HKCU "Software\Classes\.pdf\OpenWithProgids" "${PROGID}" ""
    ; "연결 프로그램" 목록용 응용 프로그램 키
    WriteRegStr HKCU "Software\Classes\Applications\${EXE}" "FriendlyAppName" "${APP}"
    WriteRegStr HKCU "Software\Classes\Applications\${EXE}\DefaultIcon" "" '"$INSTDIR\${EXE}",0'
    WriteRegStr HKCU "Software\Classes\Applications\${EXE}\SupportedTypes" ".pdf" ""
    WriteRegStr HKCU "Software\Classes\Applications\${EXE}\shell\open\command" "" '"$INSTDIR\${EXE}" "%1"'
    ; 설정 → 기본 앱 목록
    WriteRegStr HKCU "${CAPS_KEY}" "ApplicationName" "${APP}"
    WriteRegStr HKCU "${CAPS_KEY}" "ApplicationDescription" "PDF 리더/편집기"
    WriteRegStr HKCU "${CAPS_KEY}" "ApplicationIcon" "$INSTDIR\${EXE},0"
    WriteRegStr HKCU "${CAPS_KEY}\FileAssociations" ".pdf" "${PROGID}"
    WriteRegStr HKCU "Software\RegisteredApplications" "${REG_APP}" "${CAPS_KEY}"
    System::Call 'shell32::SHChangeNotify(i ${SHCNE_ASSOCCHANGED}, i 0, p 0, p 0)'
  ${EndIf}
SectionEnd

Function OpenDefaultApps
  ${If} ${SectionIsSelected} ${SecAssoc}
  ${AndIf} $TestMode != "1"
    ExecShell "open" "${DEFAULT_APPS_URI}"
  ${EndIf}
FunctionEnd

Section "Uninstall"
  Call un.WaitNotRunning
  ; 점검 설치(/TEST)로 만든 것이면 바로 가기·프로그램 목록·파일 연결은 진짜 설치본 것이므로 건드리지 않는다.
  ${IfNot} ${FileExists} "$INSTDIR\${TEST_MARK}"
    Delete "$SMPROGRAMS\${APP}.lnk"
    Delete "$DESKTOP\${APP}.lnk"
    DeleteRegKey HKCU "${UNINSTALL_KEY}"
    ; 파일 연결: 이 설치가 만든 키·값만 지운다(.pdf 키와 다른 앱의 OpenWithProgids 값은 그대로).
    ; 사용자가 고른 기본 앱(UserChoice)은 건드리지 않는다 — Windows 가 다음 열기 때 다른 앱을 묻는다.
    DeleteRegKey HKCU "Software\Classes\${PROGID}"
    DeleteRegValue HKCU "Software\Classes\.pdf\OpenWithProgids" "${PROGID}"
    DeleteRegKey HKCU "Software\Classes\Applications\${EXE}"
    DeleteRegValue HKCU "Software\RegisteredApplications" "${REG_APP}"
    DeleteRegKey HKCU "${CAPS_KEY}"
    DeleteRegKey /ifempty HKCU "${APP_KEY}"
    System::Call 'shell32::SHChangeNotify(i ${SHCNE_ASSOCCHANGED}, i 0, p 0, p 0)'
  ${EndIf}
  RMDir /r "$INSTDIR\_internal"
  Delete "$INSTDIR\${EXE}"
  Delete "$INSTDIR\README-내부전용.txt"   ; 0.1.5 이전 판
  Delete "$INSTDIR\README.txt"
  Delete "$INSTDIR\LICENSE"
  Delete "$INSTDIR\THIRD_PARTY_NOTICES.md"
  Delete "$INSTDIR\${TEST_MARK}"
  Delete "$INSTDIR\uninstall.exe"
  RMDir "$INSTDIR"
SectionEnd
