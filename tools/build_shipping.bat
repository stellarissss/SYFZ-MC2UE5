@echo off
REM ============================================================
REM  build_shipping.bat -- compile the Win64 Shipping target.
REM
REM  This is the *compile* step only. It is separate from
REM  package_shipping.bat (cook + stage + pak), because the two fail for
REM  unrelated reasons and mixing them makes the failure ambiguous:
REM  a cook failure and a link failure look the same from UAT.
REM
REM  PATH RESOLUTION (changed 10-28, drive migration)
REM  ------------------------------------------------
REM  These paths used to be hardcoded literals under the old Q: drive (the venv,
REM  the engine, the SDK roots, the UBA dir). When the tree moved to another
REM  drive those literals still *parsed* -- a drive letter is only a prefix -- so
REM  nothing failed fast. The build died later with a misleading
REM  "Exited with error code 1" and no other diagnostic.
REM
REM  <HOME> is now derived from this script's own location:
REM
REM      %~dp0                                  <HOME>\MC2UE5\repo\tools\
REM      %~dp0..\..\..                          <HOME>
REM
REM  and can be overridden with one variable:
REM
REM      set MC2UE5_HOME=D:\somewhere
REM
REM  tools\mcpaths.py holds the same mapping for python. Keep them in step.
REM
REM  Env: UBT discovers Visual Studio via the registry and C: loses the Windows
REM  SDK entry on reboot, so the SDK root is re-registered first by
REM  tools\reg_sdk.py.
REM
REM  -NoUBA is required on this host: the Unreal Build Accelerator session
REM  dir is ACL locked, and the executor fails where the same steps pass
REM  standalone. See docs/p0_freeze_notes.md.
REM
REM  Usage:  build_shipping.bat [config]      (default Shipping)
REM ============================================================
setlocal

REM --- <HOME> = the root that holds MC2UE5\, UE\, AutoSDK\, venv\ -----------
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
set "UAT=%MC2UE5_HOME%\UE\UE_5.8\Engine\Build\BatchFiles\Build.bat"
set "PROJ=%~dp0..\project\MCReplica.uproject"
set "CFG=%~1"
if "%CFG%"=="" set "CFG=Shipping"
set "LOGDIR=%MC2UE5_HOME%\MC2UE5\logs\s6a"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"

for %%I in ("%PROJ%") do set "PROJ=%%~fI"

echo ===ENV MC2UE5_HOME=%MC2UE5_HOME%===
echo ===ENV PY=%PY%===
echo ===ENV UAT=%UAT%===
if not exist "%PY%"  ( echo ===ERROR venv python missing: %PY%===  & endlocal & exit /b 1 )
if not exist "%UAT%" ( echo ===ERROR Build.bat missing: %UAT%===  & endlocal & exit /b 1 )

if exist "%~dp0reg_sdk.py" "%PY%" "%~dp0reg_sdk.py" >nul 2>&1

echo ===BUILD MCReplica Win64 %CFG%===
call "%UAT%" MCReplica Win64 %CFG% "%PROJ%" -WaitMutex -NoUBA
echo ===BUILD_EXIT=%ERRORLEVEL%===

endlocal
