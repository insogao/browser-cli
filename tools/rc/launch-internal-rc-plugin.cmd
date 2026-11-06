@echo off
rem Browser CLI internal RC + BrowserPilot extension (debug/internal use only).
rem Requires: unified out/WinRelease chrome.exe, BrowserPilot dist/, and a
rem native host registered with: register.ps1 -IncludeChromium
rem Uses a dedicated profile; never the user's normal Chrome/Browser CLI profile.
setlocal
set "CHROME=D:\c\src\out\WinRelease\chrome.exe"
if defined RC_CHROME set "CHROME=%RC_CHROME%"
set "EXT=D:\work\BrowserPilot\dist"
if defined RC_EXTENSION_DIST set "EXT=%RC_EXTENSION_DIST%"
set "PROFILE=%LOCALAPPDATA%\BrowserCLI-RC-Internal-Plugin"
if defined RC_PROFILE set "PROFILE=%RC_PROFILE%"
if not exist "%CHROME%" (
  echo Internal RC chrome.exe not found: "%CHROME%"
  exit /b 2
)
if not exist "%EXT%\manifest.json" (
  echo BrowserPilot extension dist not found: "%EXT%"
  echo Build it with: cd D:\work\BrowserPilot ^&^& npm run build
  exit /b 2
)
start "" "%CHROME%" --user-data-dir="%PROFILE%" ^
  --load-extension="%EXT%" --disable-extensions-except="%EXT%" ^
  --no-first-run --no-default-browser-check ^
  --enable-features=NetworkServiceSandbox ^
  "chrome-extension://nnollghpaggbcdkkgoieneffnlijinio/popup.html"
endlocal
