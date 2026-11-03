@echo off
rem Browser CLI internal RC launcher (unsigned test build; NOT a public release).
rem Uses a dedicated internal profile, never the user's normal Chrome/Browser CLI
rem profile. Do not add --no-sandbox or automation flags.
setlocal
set "CHROME=D:\c\src\out\WinRelease\chrome.exe"
if defined RC_CHROME set "CHROME=%RC_CHROME%"
if not exist "%CHROME%" (
  echo Internal RC chrome.exe not found: "%CHROME%"
  echo Set RC_CHROME to the unified 001-038 out/WinRelease/chrome.exe.
  exit /b 2
)
set "PROFILE=%LOCALAPPDATA%\BrowserCLI-RC-Internal"
if defined RC_PROFILE set "PROFILE=%RC_PROFILE%"
start "" "%CHROME%" --user-data-dir="%PROFILE%" --no-first-run --no-default-browser-check --enable-features=NetworkServiceSandbox about:blank
endlocal
