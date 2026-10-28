@echo off
REM ============================================================
REM  package_shipping.bat -- S6A verification package.
REM
REM  Why this is two steps and not one `BuildCookRun -cook`:
REM  on this host a single-step cook fails with a Zen oplog
REM  `HTTP NotFound` (see docs/p0_freeze_notes.md #6 and
REM  RELEASE_NOTES.md). The working recipe is cook-to-loose with
REM  `-SkipZenStore`, then stage/pak/archive with `-skipcook`.
REM
REM  Why `-NoUBA` on the stage step:
REM  the Unreal Build Accelerator session dir is ACL locked here; link.exe and
REM  the WriteMetadata step both exit 0 when run standalone with the identical
REM  .rsp and fail only under the executor. Deleting per-target intermediates
REM  does NOT fix it (QA proved that). So the executor is bypassed.
REM
REM  Why no `-build`/`-cook` in step 2:
REM  MCReplica-Win64-Shipping.exe is already newer than every
REM  file under project/Source, so there is nothing to compile.
REM  `-nocompile -nocompileeditor` makes that explicit instead
REM  of implicit.
REM
REM  PATH RESOLUTION (changed 10-28, drive migration):
REM  <HOME> is derived as %~dp0..\..\.. (= the root holding MC2UE5\ and UE\)
REM  and can be overridden with `set MC2UE5_HOME=...`. The old literals under
REM  the previous drive root still parsed after the move but pointed at a drive
REM  that no longer exists, so the cook died on a missing project file.
REM  tools\mcpaths.py holds the same mapping for python.
REM
REM  Usage:  package_shipping.bat [archive-dir]
REM ============================================================
setlocal

if "%MC2UE5_HOME%"=="" set "MC2UE5_HOME=%~dp0..\..\.."
pushd "%MC2UE5_HOME%" >nul 2>&1
if errorlevel 1 (
  echo ===ERROR cannot resolve MC2UE5_HOME from "%~dp0"===
  endlocal & exit /b 1
)
set "MC2UE5_HOME=%CD%"
popd >nul

set "UBA_ROOT=%MC2UE5_HOME%\UBA"
set "DOTNET_ROOT=%MC2UE5_HOME%\dotnet"
set "PATH=%DOTNET_ROOT%;%PATH%"
set "UE_SDKS_ROOT=%MC2UE5_HOME%\AutoSDK"
set "WindowsSDKDir=%MC2UE5_HOME%\WindowsKits"
set "WindowsSDKVersion=10.0.26100.0"

set "PY=%MC2UE5_HOME%\venv\Scripts\python.exe"
set "UE=%MC2UE5_HOME%\UE\UE_5.8\Engine"
set "PROJ=%~dp0..\project\MCReplica.uproject"
set "ARCHIVE=%~1"
if "%ARCHIVE%"=="" set "ARCHIVE=%MC2UE5_HOME%\MC2UE5\dist_verify"
set "LOGDIR=%MC2UE5_HOME%\MC2UE5\logs\s6a"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"

for %%I in ("%PROJ%") do set "PROJ=%%~fI"

echo ===ENV MC2UE5_HOME=%MC2UE5_HOME%===
echo ===ENV UE=%UE%===
echo ===ENV ARCHIVE=%ARCHIVE%===
echo ===ENV LOGDIR=%LOGDIR%===
if not exist "%PROJ%" ( echo ===ERROR uproject missing: %PROJ%=== & endlocal & exit /b 1 )
if not exist "%UE%\Binaries\Win64\UnrealEditor-Cmd.exe" ( echo ===ERROR engine missing: %UE%=== & endlocal & exit /b 1 )


REM C: loses the Windows SDK registry entry on reboot; re-register it.
if exist "%~dp0reg_sdk.py" "%PY%" "%~dp0reg_sdk.py" >nul 2>&1

REM NOTE: no '>' in any echo below. cmd parses a bare '>' as a redirect even
REM inside an echo, so a literal arrow here silently creates a *file* named
REM after the archive directory and then RunUAT cannot mkdir it.
echo ===STEP1 loose cook -SkipZenStore [log %LOGDIR%\cook1.log]===
"%UE%\Binaries\Win64\UnrealEditor-Cmd.exe" "%PROJ%" -run=Cook ^
  -TargetPlatform=Windows -unversioned -SkipZenStore ^
  -unattended -nopause -nosplash -nullrhi -stdout ^
  > "%LOGDIR%\cook1.log" 2>&1
echo ===COOK1_EXIT=%ERRORLEVEL%===

echo ===STEP2 stage+pak+archive [archive %ARCHIVE%]===
call "%UE%\Build\BatchFiles\RunUAT.bat" BuildCookRun ^
  -project="%PROJ%" -noP4 -platform=Win64 -clientconfig=Shipping ^
  -nocompile -nocompileeditor -skipcook ^
  -stage -pak -archive -archivedirectory="%ARCHIVE%" ^
  -utf8output -NoUBA -NoCodeSign -unattended -nopause -nosplash ^
  > "%LOGDIR%\cook2.log" 2>&1
echo ===COOK2_EXIT=%ERRORLEVEL%===

endlocal
