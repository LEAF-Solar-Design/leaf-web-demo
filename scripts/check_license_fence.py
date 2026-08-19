#!/usr/bin/env python3
"""License fence for the web bundle and vendored sources (card C1-1).

Scope (documented in docs/CAD-ENGINE-LICENSE-FENCE.md, keep both in step):
  * SCAN_ROOTS = ("web", "vendor") relative to the repo root. This is the
    "web bundle inputs" (web/) and "vendored sources" (vendor/) the oracle
    names — NOT the whole repo. server/dwg_convert.py and deploy/Dockerfile.app
    carry a documented, load-bearing GPL-3.0+ dependency (libredwg, consumed
    strictly across a subprocess boundary, never shipped to end users); that
    is server-side, not web-bundle, and is intentionally out of this fence's
    scope. Widening SCAN_ROOTS to the whole repo would red-flag that
    already-adjudicated, correct usage.
  * Three deny rules:
      1. OpenCADStudio identifiers/paths, any spelling/casing/separator.
      2. A GPL-3.0 license header or SPDX id, anywhere in scope.
      3. "acadrust" (the MPL-2.0 CAD engine) referenced from OUTSIDE
         ALLOWED_ACADRUST_PREFIX, unless the reference is the exact legal
         Worker-spawn shape `new Worker(new URL('<path with acadrust>',
         import.meta.url))` — the only way this codebase loads a worker
         script (see web/src/cad/engineWorker.js). Inside the prefix,
         acadrust source is expected and always allowed.
  * SELF_EXCLUDED_PATHS is exactly the fence's own 3 files (this script, its
    workflow, its doc) — they necessarily spell out these identifiers in
    prose/patterns and would self-match. This set must stay exactly these 3
    paths: widening it to cover a real vendored/source directory is the
    evasion hole the negative-control tests in this file guard against
    (see test_self_exclusion_is_exactly_the_three_fence_files).

Exit-code contract (deliberately NOT delegated to `grep`'s own exit code,
which is inverted from what CI wants — 0 means "found a match", 1 means
"no match" — under `set -e` that reads a clean tree as failure):
  * scan_tree() returns a plain Python list; main() does `if violations:
    return 1 else: return 0` explicitly. Both directions are covered by
    test_exit_code_semantics_both_directions.

Self-test mode: `python scripts/check_license_fence.py --self-test` runs the
embedded unittest suite. Every fixture is built in a tempfile.TemporaryDirectory
at test-run time and never touches the working tree — a fixture committed
under a scanned path would either permanently red every clean PR, or (if
excluded) become a second, broader evasion hole in SELF_EXCLUDED_PATHS.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parent.parent

SCAN_ROOTS: tuple[str, ...] = ("web", "vendor")

# Pruned by directory name at any depth. node_modules is third-party npm
# registry code (a different, out-of-scope licensing question from a
# deliberately-vendored/copied source drop); .git and __pycache__ are VCS/
# tooling internals. Deliberately does NOT prune dist/build: the built web
# bundle is exactly where trap-class "header stripped by minification"
# content lives (see module docstring, "built bundle" note under trap 1 of
# the trapsheet), and the workflow builds the bundle before scanning so it is
# present on disk. NOTE this is a deliberate, documented, narrow prune list —
# NOT the git-grep-skips-untracked-vendored-dirs failure mode: os.walk below
# reads the real filesystem, including untracked and .gitignore'd files, for
# every directory that is not in this set.
PRUNE_DIRNAMES = frozenset({".git", "node_modules", "__pycache__"})

# The fence's own 3 files. Exactly these — see module docstring.
SELF_EXCLUDED_PATHS = frozenset({
    "scripts/check_license_fence.py",
    ".github/workflows/license-fence.yml",
    "docs/CAD-ENGINE-LICENSE-FENCE.md",
})

# vendor/acadrust-worker/ is the only place MPL-2.0 acadrust source may live.
# Trailing slash is load-bearing: without it, "vendor/acadrust-worker-staging/"
# would also satisfy a naive startswith() check.
ALLOWED_ACADRUST_PREFIX = "vendor/acadrust-worker/"

# Cap on bytes read per file: bounds memory on an unexpectedly huge blob
# while comfortably covering real bundle/vendor files (bundle JS is typically
# single-digit MB; a license header or identifier is always near the start of
# a file in any case a legitimate dependency would ship).
MAX_SCAN_BYTES = 50 * 1024 * 1024

# --- Deny patterns --------------------------------------------------------
# Bytes patterns throughout: files are read in binary mode so a stripped-text
# .wasm/minified blob is scanned the same as source (grep -I / "Binary file
# matches" would silently swallow exactly this case).

OPENCADSTUDIO_RE = re.compile(rb"open[-_\s]{0,2}cad[-_\s]{0,2}studio", re.IGNORECASE)

# The oracle denies "identifiers/paths" — code symbols and file/import paths —
# not the bare product name appearing as English prose (e.g. a comment or
# test description explicitly disclaiming use, which this repo's own
# card-C1-6 code legitimately does: "No OpenCADStudio source ... is used
# here"). A match is code-shaped (and therefore a violation) unless BOTH the
# byte immediately before and the byte immediately after it are whitespace or
# ordinary sentence punctuation — any identifier/path/string-literal
# character (letter, digit, _, -, /, \, quote, backtick) on either side means
# it is glued to a token, not standing alone as a word.
_PROSE_SAFE_NEIGHBORS = frozenset(b" \t\r\n.,;:!?()")

def _is_prose_mention(data: bytes, start: int, end: int) -> bool:
    before_ok = start == 0 or data[start - 1] in _PROSE_SAFE_NEIGHBORS
    after_ok = end >= len(data) or data[end] in _PROSE_SAFE_NEIGHBORS
    return before_ok and after_ok

# Negative lookbehind for a preceding letter excludes LGPL-3.0 / AGPL-3.0
# (whose "GPL-3.0" substring is directly preceded by a letter). Matches the
# bare id and the SPDX -only/-or-later suffixes; does NOT match GPL-2.0.
GPL3_SPDX_RE = re.compile(
    rb"(?<![A-Za-z])GPL-3\.0(-only|-or-later)?(?![A-Za-z0-9.])", re.IGNORECASE
)

# Full boilerplate header phrase, requiring "Version 3" within a bounded
# window after it (real GPL-3 boilerplate always states the version near the
# top; this also keeps the pattern from matching a GPL-2-only header, which
# is out of scope here). Bounded span, not unbounded ".*", for the same
# not-ReDoS-on-a-huge-blob reason as MAX_SCAN_BYTES above.
GPL3_HEADER_RE = re.compile(
    rb"GNU\s+GENERAL\s+PUBLIC\s+LICENSE.{0,300}?Version\s+3",
    re.IGNORECASE | re.DOTALL,
)

ACADRUST_RE = re.compile(rb"acadrust", re.IGNORECASE)

# The one legal shape: new Worker(new URL('<...acadrust...>', import.meta.url)).
# Group 2 is the quoted literal span; an ACADRUST_RE hit is legal only if it
# falls inside that span for some match of this pattern.
WORKER_SPAWN_RE = re.compile(
    rb"new\s+Worker\s*\(\s*new\s+URL\s*\(\s*(['\"`])((?:(?!\1).){0,300}?)\1"
    rb"\s*,\s*import\.meta\.url\s*\)",
    re.IGNORECASE | re.DOTALL,
)


class Violation:
    __slots__ = ("kind", "path", "line", "detail")

    def __init__(self, kind: str, path: str, line: int, detail: str):
        self.kind = kind
        self.path = path
        self.line = line
        self.detail = detail

    def __eq__(self, other):
        if not isinstance(other, Violation):
            return NotImplemented
        return (self.kind, self.path, self.line, self.detail) == (
            other.kind, other.path, other.line, other.detail,
        )

    def __repr__(self):
        return f"Violation({self.kind!r}, {self.path!r}, {self.line!r}, {self.detail!r})"

    def format(self) -> str:
        return f"{self.kind}: {self.path}:{self.line}: {self.detail}"


def _is_under_prefix(relpath: str, prefix: str) -> bool:
    assert prefix.endswith("/")
    return relpath == prefix[:-1] or relpath.startswith(prefix)

def _is_self_excluded(relpath: str) -> bool:
    return relpath in SELF_EXCLUDED_PATHS

def _line_at(data: bytes, offset: int) -> int:
    return data.count(b"\n", 0, offset) + 1

def _iter_scan_files(root: Path, scan_roots=SCAN_ROOTS):
    for scan_root in scan_roots:
        base = root / scan_root
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in PRUNE_DIRNAMES]
            for name in filenames:
                full = Path(dirpath) / name
                rel = PurePosixPath(full.relative_to(root).as_posix())
                relpath = str(rel)
                if _is_self_excluded(relpath):
                    continue
                yield full, relpath


def _scan_file(full: Path, relpath: str) -> list[Violation]:
    try:
        data = full.read_bytes()[:MAX_SCAN_BYTES]
    except OSError:
        return []

    violations: list[Violation] = []

    relpath_bytes = relpath.encode("utf-8", "surrogateescape")
    for m in OPENCADSTUDIO_RE.finditer(relpath_bytes):
        violations.append(Violation(
            "opencadstudio_identifier", relpath, 0,
            f"matched {m.group(0)!r} in the file path itself",
        ))

    for m in OPENCADSTUDIO_RE.finditer(data):
        if _is_prose_mention(data, m.start(), m.end()):
            continue
        violations.append(Violation(
            "opencadstudio_identifier", relpath, _line_at(data, m.start()),
            f"matched {m.group(0)!r}",
        ))

    for m in GPL3_SPDX_RE.finditer(data):
        violations.append(Violation(
            "gpl3_license_header", relpath, _line_at(data, m.start()),
            f"SPDX-style GPL-3.0 id {m.group(0)!r}",
        ))
    for m in GPL3_HEADER_RE.finditer(data):
        violations.append(Violation(
            "gpl3_license_header", relpath, _line_at(data, m.start()),
            "GNU GENERAL PUBLIC LICENSE ... Version 3 boilerplate",
        ))

    acadrust_matches = list(ACADRUST_RE.finditer(data))
    if acadrust_matches and not _is_under_prefix(relpath, ALLOWED_ACADRUST_PREFIX):
        legal_spans = [m.span(2) for m in WORKER_SPAWN_RE.finditer(data)]
        for m in acadrust_matches:
            start, end = m.span()
            legal = any(lo <= start and end <= hi for lo, hi in legal_spans)
            if not legal:
                violations.append(Violation(
                    "acadrust_outside_prefix", relpath, _line_at(data, m.start()),
                    f"'acadrust' referenced outside {ALLOWED_ACADRUST_PREFIX} "
                    "and not inside a legal Worker(new URL(...import.meta.url)) spawn",
                ))

    return violations


def scan_tree(root: Path, scan_roots=SCAN_ROOTS) -> list[Violation]:
    root = Path(root)
    violations: list[Violation] = []
    for full, relpath in _iter_scan_files(root, scan_roots):
        violations.extend(_scan_file(full, relpath))
    return violations


def _run_scan(root: str) -> int:
    violations = scan_tree(Path(root))
    if violations:
        for v in violations:
            print(v.format())
        print(f"license fence: {len(violations)} violation(s)")
        return 1
    print("license fence: clean")
    return 0


def _run_self_test() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


def main(argv: list[str]) -> int:
    if "--self-test" in argv:
        return _run_self_test()
    root = argv[0] if argv else "."
    return _run_scan(root)


# --- Embedded self-tests ---------------------------------------------------
# Every fixture below is built under a tempfile.TemporaryDirectory and is
# never written into the repo tree.

def _write(root: Path, relpath: str, content: bytes) -> None:
    full = root / relpath
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(content)


class LicenseFenceSelfTest(unittest.TestCase):
    def test_self_exclusion_is_exactly_the_three_fence_files(self):
        # Locks the exclusion set narrow: widening it to a real source
        # directory is the evasion hole trap this fence exists to avoid.
        self.assertEqual(SELF_EXCLUDED_PATHS, {
            "scripts/check_license_fence.py",
            ".github/workflows/license-fence.yml",
            "docs/CAD-ENGINE-LICENSE-FENCE.md",
        })

    def test_negative_control_bundle_violation_is_red(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "web/dist/app.js", b"var x = 'OpenCADStudio-runtime';\n")
            violations = scan_tree(root)
            self.assertTrue(any(v.kind == "opencadstudio_identifier" for v in violations))

    def test_negative_control_vendored_violation_is_red(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "vendor/some-lib/LICENSE", (
                b"GNU GENERAL PUBLIC LICENSE\n"
                b"Version 3, 29 June 2007\n"
            ))
            violations = scan_tree(root)
            self.assertTrue(any(v.kind == "gpl3_license_header" for v in violations))

    def test_positive_control_clean_tree_is_green(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "web/src/App.jsx", b"export default function App() { return null }\n")
            _write(root, "vendor/acadrust-worker/engine.js", b"// MPL-2.0 acadrust engine glue\n")
            violations = scan_tree(root)
            self.assertEqual(violations, [])

    def test_acadrust_allowed_inside_prefix_any_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "vendor/acadrust-worker/engine.js", (
                b"// acadrust core, MPL-2.0\nimport { acadrustInit } from './core.js'\n"
            ))
            violations = scan_tree(root)
            self.assertEqual(violations, [])

    def test_acadrust_legal_worker_spawn_from_main_bundle_is_green(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "web/src/main.js", (
                b"const w = new Worker(new URL('../../vendor/acadrust-worker/engine.js', "
                b"import.meta.url), { type: 'module' })\n"
            ))
            violations = scan_tree(root)
            self.assertEqual(violations, [])

    def test_acadrust_direct_import_from_main_bundle_is_red(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "web/src/main.js", (
                b"import { run } from '../../vendor/acadrust-worker/engine.js'\n"
            ))
            violations = scan_tree(root)
            self.assertTrue(any(v.kind == "acadrust_outside_prefix" for v in violations))

    def test_prefix_trailing_slash_boundary_lookalike_dir_is_red(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "vendor/acadrust-worker-staging/engine.js", (
                b"import { run } from './core.js' // acadrust\n"
            ))
            violations = scan_tree(root)
            self.assertTrue(any(v.kind == "acadrust_outside_prefix" for v in violations))

    def test_gpl_bare_substring_no_false_positive_on_lgpl_agpl_gpl2(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "vendor/some-lib/LICENSE", (
                b"SPDX-License-Identifier: LGPL-3.0-only\n"
                b"SPDX-License-Identifier: AGPL-3.0-or-later\n"
                b"SPDX-License-Identifier: GPL-2.0-only\n"
            ))
            violations = scan_tree(root)
            self.assertEqual(violations, [])

    def test_gpl_spdx_variant_detected_when_header_boilerplate_is_stripped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "web/dist/vendored.min.js", b"//SPDX-License-Identifier: GPL-3.0-or-later\n")
            violations = scan_tree(root)
            self.assertTrue(any(v.kind == "gpl3_license_header" for v in violations))

    def test_opencadstudio_variant_casing_and_separators(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "web/dist/a.js", b"var x = 'opencadstudio';\n")
            _write(root, "web/dist/b.js", b"var y = 'Open-CAD-Studio';\n")
            violations = scan_tree(root)
            hit_files = {v.path for v in violations if v.kind == "opencadstudio_identifier"}
            self.assertEqual(hit_files, {"web/dist/a.js", "web/dist/b.js"})

    def test_opencadstudio_bare_prose_mention_is_not_a_violation(self):
        # Real case from this repo (card C1-6, web/src/cad/EditSurface.jsx):
        # a comment disclaiming use of OpenCADStudio source mentions the bare
        # product name as English prose, not as a copied identifier or path.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "web/src/cad/EditSurface.jsx", (
                b"// No OpenCADStudio source, assets, or dependency is used here.\n"
                b"// (OpenCADStudio interaction model, reimplemented natively.)\n"
            ))
            violations = scan_tree(root)
            self.assertEqual(violations, [])

    def test_opencadstudio_identifier_glued_to_other_tokens_is_a_violation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "web/src/x.js", b"import OpenCADStudioEngine from './x'\n")
            violations = scan_tree(root)
            self.assertTrue(any(v.kind == "opencadstudio_identifier" for v in violations))

    def test_opencadstudio_path_component_is_a_violation_even_with_clean_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "vendor/opencadstudio-widget/index.js", b"export default 1\n")
            violations = scan_tree(root)
            self.assertTrue(any(
                v.kind == "opencadstudio_identifier" and v.line == 0 for v in violations
            ))

    def test_binary_blob_is_scanned_not_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "web/dist/engine.wasm", b"\x00\x61\x73\x6d\x00OpenCADStudio\x00\x01\x02")
            violations = scan_tree(root)
            self.assertTrue(any(v.kind == "opencadstudio_identifier" for v in violations))

    def test_git_directory_excluded_but_untracked_sibling_still_scanned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "web/.git/COMMIT_EDITMSG", b"var x = 'OpenCADStudio'\n")
            _write(root, "web/src/untracked_leftover.js", b"var x = 'OpenCADStudio'\n")
            violations = scan_tree(root)
            paths = {v.path for v in violations}
            self.assertNotIn("web/.git/COMMIT_EDITMSG", paths)
            self.assertIn("web/src/untracked_leftover.js", paths)

    def test_exit_code_semantics_both_directions(self):
        with tempfile.TemporaryDirectory() as tmp:
            clean = Path(tmp) / "clean"
            _write(clean, "web/src/App.jsx", b"export default 1\n")
            self.assertEqual(len(scan_tree(clean)), 0)

            dirty = Path(tmp) / "dirty"
            _write(dirty, "web/src/App.jsx", b"var x = 'OpenCADStudio'\n")
            self.assertGreater(len(scan_tree(dirty)), 0)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
