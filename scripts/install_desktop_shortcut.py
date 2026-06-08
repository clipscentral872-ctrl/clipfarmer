"""Create a desktop shortcut for the clipfarmer dashboard.

Run this ONCE. It places a "clipfarmer dashboard.lnk" on your Windows
desktop that, when double-clicked, regenerates the dashboard with fresh
data and opens it in your default browser.

Usage:
    python scripts/install_desktop_shortcut.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    desktop = _find_desktop()
    if not desktop:
        print("Could not locate your Desktop folder. Aborting.")
        return 1

    # 1. Write a launcher .bat in the project so the shortcut just points at it.
    bat_path = PROJECT_ROOT / "clipfarmer-dashboard.bat"
    venv_python = PROJECT_ROOT / ".venv" / "Scripts" / "pythonw.exe"
    fallback_python = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    py_exe = venv_python if venv_python.exists() else fallback_python

    bat_path.write_text(
        f"""@echo off
cd /d "{PROJECT_ROOT}"
start "" "{py_exe}" "scripts\\dashboard.py"
""",
        encoding="utf-8",
    )
    print(f"Wrote launcher: {bat_path}")

    # 2. Create the .lnk on the desktop via PowerShell + WScript.Shell COM.
    lnk_path = desktop / "clipfarmer dashboard.lnk"
    icon_hint = _find_icon()

    ps_script = f"""
$WshShell = New-Object -comObject WScript.Shell
$shortcut = $WshShell.CreateShortcut("{lnk_path}")
$shortcut.TargetPath = "{bat_path}"
$shortcut.WorkingDirectory = "{PROJECT_ROOT}"
$shortcut.WindowStyle = 7
$shortcut.Description = "Open the clipfarmer dashboard"
"""
    if icon_hint:
        ps_script += f'$shortcut.IconLocation = "{icon_hint}"\n'
    ps_script += "$shortcut.Save()\n"

    r = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", ps_script],
        capture_output=True, text=True, timeout=20,
    )
    if r.returncode != 0:
        print("PowerShell shortcut creation failed:")
        print(r.stderr)
        return 1

    if not lnk_path.exists():
        print(f"PowerShell ran but {lnk_path} was not created.")
        return 1

    print(f"\n✅ Desktop shortcut created: {lnk_path}")
    print("\nDouble-click it any time to refresh + open the dashboard in your browser.")
    print("(There will be a tiny console flash, then your browser opens.)")
    return 0


def _find_desktop() -> Path | None:
    # Prefer the OneDrive desktop if it exists, else the local profile desktop.
    candidates = [
        Path(os.environ.get("OneDrive", "")) / "Desktop" if os.environ.get("OneDrive") else None,
        Path.home() / "OneDrive" / "Desktop",
        Path.home() / "Desktop",
    ]
    for c in candidates:
        if c and c.exists() and c.is_dir():
            return c
    return None


def _find_icon() -> str | None:
    # No icon file shipped; fall back to a built-in Windows icon (orange bullet).
    # imageres.dll,154 is a graph/chart icon on most Win10/11 systems.
    sys32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "imageres.dll"
    if sys32.exists():
        return f"{sys32},154"
    return None


if __name__ == "__main__":
    sys.exit(main())
