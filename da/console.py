"""Shared local AutoCAD console discovery for the engine canaries."""
from __future__ import annotations

import os
import re
from pathlib import Path


def resolve_accoreconsole(root=Path(r"C:\Program Files\Autodesk")):
    pattern = str(root / "AutoCAD <year>" / "accoreconsole.exe")
    override = os.environ.get("LEAF_ACCORECONSOLE")
    if override is not None:
        console = Path(override)
        if not override or not console.is_file():
            raise ValueError(f"LEAF_ACCORECONSOLE refuses missing executable: {override!r}")
        return console, f"LEAF_ACCORECONSOLE resolved {console}"
    years = []
    candidates = []
    for directory in root.glob("AutoCAD *"):
        match = re.fullmatch(r"AutoCAD ([0-9]{4})", directory.name)
        if match is None or not directory.is_dir():
            continue
        year = int(match.group(1))
        years.append(year)
        console = directory / "accoreconsole.exe"
        if console.is_file():
            candidates.append((year, console))
    tried = (
        f"LEAF_ACCORECONSOLE unset; tried {pattern}; "
        f"years seen: {', '.join(map(str, sorted(years))) or 'none'}"
    )
    if candidates:
        console = max(candidates)[1]
        return console, f"resolved {console}; {tried}"
    return None, f"no AutoCAD console found; {tried}"


