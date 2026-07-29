"""Safe, disk-backed catalog discovery for curated Leaf skill bundles."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Mapping, Optional


SKILL_NAME_RE = re.compile(
    r"^[a-z0-9][a-z0-9._-]{0,63}$", re.IGNORECASE | re.ASCII)
RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}
MAX_SKILL_FILE_BYTES = 256 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
MAX_SKILLS = 250
_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---", re.DOTALL)
_FRONTMATTER_FIELD_RE = re.compile(r"^(name|description)\s*:\s*(.*)$")


def _dedupe_key(name: str) -> str:
    """The collision key for case-only duplicates (`Probe` vs `probe`): one
    directory on Windows/macOS, two on Linux — ambiguous either way, so they
    fold to one key and the first wins. Extracted so the FOLD itself is a
    testable unit on every platform (a case-insensitive filesystem cannot even
    construct the on-disk pair)."""
    return name.lower()


def is_valid_skill_name(name: object) -> bool:
    """Return whether *name* is a safe, portable skill slug."""
    if not isinstance(name, str) or not SKILL_NAME_RE.fullmatch(name):
        return False
    if name.endswith("."):
        return False
    return name.split(".", 1)[0].lower() not in RESERVED_NAMES


def _contained_real_path(root_real: Path, child: Path) -> Optional[Path]:
    """Resolve *child* only when it remains below the real bundle root."""
    try:
        child_real = child.resolve(strict=True)
        child_real.relative_to(root_real)
    except (OSError, RuntimeError, ValueError):
        return None
    return child_real


def _is_multiply_linked(path: Path) -> bool:
    """Mirror the harness loader's hardlink refusal: a curated bundle's files
    are its own, and nlink > 1 means the file also lives somewhere else — the
    one escape real-path containment structurally cannot see. A stat that
    cannot prove exclusivity refuses."""
    try:
        return os.stat(path).st_nlink > 1
    except OSError:
        return True


def _read_manifest_tier(root: Path, root_real: Path) -> Optional[str]:
    # ONE try over everything — is_symlink and the containment resolve hit the
    # filesystem too, and json.loads on a hostile 64 KiB deeply-nested body
    # raises RecursionError, not JSONDecodeError. The route's contract is
    # never-500, so every failure here IS the answer "no valid tier".
    try:
        manifest_dir = root / ".claude-plugin"
        manifest = manifest_dir / "plugin.json"
        if manifest_dir.is_symlink() or manifest.is_symlink():
            return None
        manifest_real = _contained_real_path(root_real, manifest)
        if manifest_real is None:
            return None
        if not manifest_real.is_file() or _is_multiply_linked(manifest_real):
            return None
        source = _read_capped(manifest_real, MAX_MANIFEST_BYTES)
        if source is None:
            return None
        value = json.loads(source)
    except (OSError, RecursionError, ValueError):
        return None
    tier = value.get("leafTier") if isinstance(value, dict) else None
    return tier if tier in {"tenant-safe", "operator"} else None


def _read_capped(path: Path, limit: int) -> Optional[str]:
    """Read a UTF-8 text file only when one open proves it fits the limit."""
    try:
        with path.open("rb") as source:
            data = source.read(limit + 1)
        if len(data) > limit:
            return None
        return data.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _parse_frontmatter(source: str) -> Optional[tuple[str, str]]:
    match = _FRONTMATTER_RE.match(source)
    if match is None:
        return None
    name: Optional[str] = None
    description: Optional[str] = None
    for line in match.group(1).splitlines():
        field = _FRONTMATTER_FIELD_RE.match(line.strip())
        if field is None:
            continue
        value = field.group(2).strip().strip("\"'")
        if field.group(1) == "name" and name is None:
            name = value
        elif field.group(1) == "description" and description is None:
            description = value
    if not is_valid_skill_name(name):
        return None
    return name, description or ""


def discover_bundle(bundle_path: str, expected_tier: str) -> list[dict[str, str]]:
    """Return valid skills from exactly one correctly tiered bundle, or no skills."""
    if not bundle_path:
        return []
    try:
        root = Path(bundle_path)
        if root.is_symlink() or not root.is_dir():
            return []
        root_real = root.resolve(strict=True)
    except (OSError, RuntimeError):
        return []
    if _read_manifest_tier(root, root_real) != expected_tier:
        return []

    try:
        skills_root = root / "skills"
        if skills_root.is_symlink():
            return []
        skills_root_real = _contained_real_path(root_real, skills_root)
        if skills_root_real is None or not skills_root_real.is_dir():
            return []
    except OSError:
        return []

    found: list[dict[str, str]] = []
    seen: set[str] = set()
    try:
        with os.scandir(skills_root_real) as entries:
            for examined, entry in enumerate(entries, start=1):
                if len(found) >= MAX_SKILLS or examined > MAX_SKILLS * 2:
                    break
                if not is_valid_skill_name(entry.name):
                    continue
                key = _dedupe_key(entry.name)
                if key in seen:
                    continue
                try:
                    skill_dir = skills_root_real / entry.name
                    skill_file = skill_dir / "SKILL.md"
                    if skill_dir.is_symlink() or skill_file.is_symlink():
                        continue
                    skill_dir_real = _contained_real_path(skills_root_real, skill_dir)
                    skill_file_real = _contained_real_path(root_real, skill_file)
                    if skill_dir_real is None or skill_file_real is None:
                        continue
                    if not skill_dir_real.is_dir() or not skill_file_real.is_file():
                        continue
                    if _is_multiply_linked(skill_file_real):
                        continue
                    source = _read_capped(skill_file_real, MAX_SKILL_FILE_BYTES)
                except OSError:
                    continue
                if source is None:
                    continue
                parsed = _parse_frontmatter(source)
                if parsed is None or parsed[0] != entry.name:
                    continue
                seen.add(key)
                found.append({
                    "id": parsed[0],
                    "name": parsed[0],
                    "description": parsed[1],
                    "tier": expected_tier,
                })
    except OSError:
        return []
    return sorted(found, key=lambda skill: skill["name"].lower())


def catalog(env: Optional[Mapping[str, str]] = None, *, operator: bool = False) -> dict[str, list[dict[str, str]]]:
    """Build the caller-visible catalog from separate tenant and operator paths."""
    values = os.environ if env is None else env
    skills = discover_bundle(values.get("LEAF_SKILLS_BUNDLE_PATH", ""), "tenant-safe")
    if operator:
        skills.extend(discover_bundle(
            values.get("LEAF_SKILLS_OPERATOR_BUNDLE_PATH", ""), "operator"))
    return {"skills": skills}
