# -*- coding: utf-8 -*-
"""mcpaths.py -- the one place that knows where this machine's directories are.

Why this file exists
--------------------
Until 10-08 every script carried the toolchain's absolute paths as string
literals (`Q:\\MC2UE5\\...`, `Q:\\UE\\UE_5.8\\...`). When the environment was
migrated to another drive every one of those literals silently pointed at a
drive letter that no longer exists, and the failure mode is the worst kind:
`os.path.join(HOME, "UE_5.8")` still produces a *valid-looking* path, so the
error surfaces much later as "editor exited 1" or "file not found" from a
sub-subprocess. This module makes the layout a single, testable fact.

The layout it assumes (true for both the old Q: drive and the current G: one)
---------------------------------------------------------------------------
    <HOME>/MC2UE5/repo        this git working copy        (REPO)
    <HOME>/MC2UE5/logs        all step logs                (LOGS)
    <HOME>/MC2UE5/dist_*      packaged builds
    <HOME>/venv               the python that has numpy/scipy/PIL (VENV)
    <HOME>/UE/UE_5.8          Unreal Engine 5.8.3           (UE_ROOT)
    <HOME>/AutoSDK            the SDK root UBT is pointed at
    <HOME>/WindowsKits        Windows SDK for the toolchain
    <HOME>/dotnet, <HOME>/UBA Build accelerator / dotnet root

`<HOME>` is derived from this file's own location -- ``tools/mcpaths.py`` ->
repo -> MC2UE5 -> HOME -- so **moving the tree needs no edit here**. Every
value can be overridden by an environment variable, which is how you point the
same checkout at a second engine install or a scratch log dir without touching
the repo:

    MC2UE5_HOME     <HOME>                    (the only one you normally need)
    MC_LOG_DIR      logs dir
    MC_UE_ROOT      engine root (the dir containing Engine/)
    MC_VENV_PY      python.exe with numpy/scipy/PIL

The old `<HOME>/MC2UE5/venv` is NOT usable: its pyvenv.cfg points at a base
interpreter that was removed during the migration, so it fails with
"did not find executable at ...". `<HOME>/venv` is the working one.

Usage from a host-side script (sys.path[0] is tools/, so a plain import works):

    from mcpaths import REPO, LOGS, UE_ROOT, EDITOR_CMD
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# repo -> MC2UE5 -> HOME. Derived, never typed.
_DERIVED_HOME = os.path.dirname(os.path.dirname(REPO))
HOME = os.environ.get("MC2UE5_HOME") or _DERIVED_HOME

LOGS = os.environ.get("MC_LOG_DIR") or os.path.join(os.path.dirname(REPO), "logs")
UE_ROOT = os.environ.get("MC_UE_ROOT") or os.path.join(HOME, "UE", "UE_5.8")

VENV_PY = (os.environ.get("MC_VENV_PY")
           or os.path.join(HOME, "venv", "Scripts", "python.exe"))

EDITOR_CMD = os.path.join(UE_ROOT, "Engine", "Binaries", "Win64",
                          "UnrealEditor-Cmd.exe")
UAT_BAT = os.path.join(UE_ROOT, "Engine", "Build", "BatchFiles", "RunUAT.bat")
BUILD_BAT = os.path.join(UE_ROOT, "Engine", "Build", "BatchFiles", "Build.bat")

# The toolchain roots the C++ build needs. Kept here so the .bat files and any
# python wrapper agree; the bat files default to the same derived <HOME>.
TOOLCHAIN = {
    "UE_SDKS_ROOT": os.path.join(HOME, "AutoSDK"),
    "WindowsSDKDir": os.path.join(HOME, "WindowsKits"),
    "WindowsSDKVersion": "10.0.26100.0",
    "DOTNET_ROOT": os.path.join(HOME, "dotnet"),
    "UBA_ROOT": os.path.join(HOME, "UBA"),
}

DIST_VERIFY = os.path.join(os.path.dirname(REPO), "dist_verify")


def check(verbose=True):
    """Return a list of (name, path, ok) for the things that must exist.

    Call this first in any long-running script. A migration that silently lost
    the engine should cost one line of output, not a forty-minute cook.
    """
    required = [
        ("repo", REPO),
        ("engine exe", EDITOR_CMD),
        ("venv python", VENV_PY),
        ("uproject", os.path.join(REPO, "project", "MCReplica.uproject")),
        ("voxel data", os.path.join(REPO, "voxel_data", "full", "overworld.bin")),
    ]
    rows = [(name, path, os.path.exists(path)) for name, path in required]
    if verbose:
        for name, path, ok in rows:
            print(("OK   " if ok else "MISS ") + name + ": " + path, flush=True)
    return rows


def describe():
    lines = [
        "HOME     " + HOME,
        "REPO     " + REPO,
        "LOGS     " + LOGS,
        "UE_ROOT  " + UE_ROOT,
        "VENV_PY  " + VENV_PY,
        "EDITOR   " + EDITOR_CMD,
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    print(describe())
    print()
    rows = check()
    sys.exit(0 if all(ok for _, _, ok in rows) else 1)
