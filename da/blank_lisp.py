#!/usr/bin/env python3
r"""da/blank_lisp.py — the blank-DWG CREATE recipe (pure, no network, no creds).

Sibling of da/write_lisp.py. Where write_lisp MUTATES an input drawing, this
module CREATES one from nothing: the DA Activity built on this script takes NO
HostDwg input parameter at all, so no customer bytes and no OSS input object
are involved on the create leg.

WHY NO INPUT IS NEEDED (measured, not assumed)
----------------------------------------------
`accoreconsole.exe /s <script>` with NO `/i` boots on the engine's own
`acad.dwt` and lands at a live command prompt. Confirmed 2026-08-24 against a
real AutoCAD 2026 accoreconsole (engine family Autodesk.AutoCAD+26_0, the
pinned APS engine): the banner reads "Drawing created using acad.dwt from
AutoCAD profile", the script ran, and SAVEAS produced a valid 31,726-byte DWG.

ORDER IS LOAD-BEARING
---------------------
  1. MAKE the run-scoped marker layer. Two jobs at once:
       (a) PROVENANCE - the marker is unique per run, so a read round-trip that
           returns some OTHER drawing's bytes cannot be mistaken for success.
       (b) it dirties the database (DBMOD != 0), so SAVEAS never stops on an
           "already saved" prompt and hangs the WorkItem to its timeout.
  1b. OPTIONALLY draw one witness POINT on that layer (`witness=True`). Needed
      only when the read oracle counts ENTITIES rather than reading the layer
      table - see WITNESS_POINT below. Off by default: the proven spike's
      recipe is unchanged.
  2. SAVEAS an EXPLICIT format to output.dwg (the Activity's Result localName).
     Explicit beats "" (current): the empty answer inherits whatever the engine
     image defaults to, which is a silent contract with the engine version.
  3. QUIT.

Nothing here allocates per entity, opens a socket, or reads a credential: it is
string construction only, so it is fully unit-testable offline.
"""
from __future__ import annotations

import re
import uuid

# The Activity's Result localName. Must match blank_activity_spec()'s parameter.
OUT_LOCALNAME = "output.dwg"

# The Activity's Script localName. The per-run .scr is delivered as an ARGUMENT
# (not a baked-in setting) so one Activity version serves every unique marker.
SCRIPT_LOCALNAME = "run.scr"

# SAVEAS format answer. "2018" is the DWG format alias proven live on
# accoreconsole 2026 (see module docstring). Never "" — see step 2 above.
SAVEAS_FORMAT = "2018"

# Marker layer prefix. The suffix is per-run entropy, so the full name is the
# provenance token the read leg asserts on.
MARKER_PREFIX = "LEAF_BLANK_"

# Model-space witness entity, drawn on the marker layer right after the layer
# MAKE (which also makes that layer current). Emitted only when witness=True.
#
# WHY A BARE LAYER IS NOT ENOUGH FOR EVERY READ ORACLE (measured 2026-08-24 on
# a real AutoCAD 2026 accoreconsole, $0, not inferred):
#   engine/tools/count_by_layer.lsp - the BROKER's read witness - counts model
#   space ENTITIES grouped by layer, via (ssget "_X" (list (cons 410 "Model"))).
#   A marker layer carrying no entity is therefore INVISIBLE to it. Same recipe,
#   same engine, one variable: with the point below the read returned
#   counts={"LEAF_BLANK_AABBCCDD1122":1}; without it, counts={}.
#   da/client.py extract() reads the layer TABLE, so the spike sees the marker
#   either way and does not need this.
# One POINT is the cheapest entity that closes the gap: no prompts beyond the
# coordinate, no linetype or block dependency, negligible bytes.
WITNESS_POINT = "0,0"

# AutoCAD layer names forbid < > / \ " : ; ? * | , = ` and control chars. We are
# far stricter than that on purpose: A-Z 0-9 and underscore only, so the name
# can never need quoting inside the .scr and can never terminate a LISP string.
_MARKER_RE = re.compile(r"^[A-Z0-9_]{1,255}$")

# A DWG the engine wrote from acad.dwt with one added layer is ~30 KB. Anything
# far below that is not a drawing (a truncated download, an error page, an empty
# PUT). Fails closed rather than registering rubbish as drawing version 1.
MIN_PLAUSIBLE_DWG_BYTES = 4096


def new_marker_layer() -> str:
    """A fresh run-scoped marker layer name, e.g. LEAF_BLANK_9F3A1C7E2B04.

    Entropy comes from uuid4, NOT from a clock: a timestamp at 1-second
    granularity cannot distinguish two runs in the same second, and this token's
    whole job is to be unforgeable by a concurrent run. (The scratch-key shape
    that had that exact flaw was fixed at source in da/client.py by #782,
    e16f1d78, which now mints a uuid-derived per-run nonce. This marker is
    defense in depth over that, not the only guard.)
    """
    return f"{MARKER_PREFIX}{uuid.uuid4().hex[:12].upper()}"


def validate_marker(marker: str) -> str:
    """Return `marker` if it is a safe layer name, else raise. Fails closed."""
    if not isinstance(marker, str) or not _MARKER_RE.match(marker):
        raise ValueError(
            f"unsafe marker layer name {marker!r}: expected ^[A-Z0-9_]{{1,255}}$"
        )
    return marker


def build_blank_scr(marker: str, out_localname: str = OUT_LOCALNAME,
                    saveas_format: str = SAVEAS_FORMAT,
                    witness: bool = False) -> str:
    """The complete .scr a blank-CREATE Activity runs headless.

    `marker` is validated before interpolation, so this cannot emit a script
    with an injected command line.

    `witness=True` adds one POINT on the marker layer. Set it when the read
    oracle counts entities (the broker's count-by-layer) rather than reading
    the layer table (the spike's client.extract). See WITNESS_POINT. Default
    False keeps the byte-for-byte recipe proven live on 2026-08-24.
    """
    validate_marker(marker)
    if not out_localname or '"' in out_localname:
        raise ValueError(f"unsafe out_localname {out_localname!r}")
    if not saveas_format or '"' in saveas_format:
        raise ValueError(f"unsafe saveas_format {saveas_format!r}")
    lines = [
        '(setvar "CMDECHO" 0)',
        # 1) marker layer: provenance token + guarantees DBMOD != 0
        f'(command "_.-LAYER" "_Make" "{marker}" "")',
        f'(progn (princ "LEAF-BLANK-MARKER={marker}") (princ))',
    ]
    if witness:
        # 1b) one entity ON the marker layer, so entity-counting read
        # oracles can see the marker at all. See WITNESS_POINT.
        lines += [
            f'(command "_.POINT" "{WITNESS_POINT}")',
            '(progn (princ "LEAF-BLANK-WITNESS") (princ))',
        ]
    return "\n".join(lines + [
        # 2) explicit-format SAVEAS to the Activity's Result localName
        f'(command "_.SAVEAS" "{saveas_format}" "{out_localname}")',
        '(progn (princ "LEAF-BLANK-SAVED") (princ))',
        # 3) quit
        '(command "_.QUIT" "_Y")',
        "",
    ])



def activity_body_matches(live: dict, want: dict) -> bool:
    """True when a LIVE activity body would RUN the same thing as `want`.

    Compares only the fields that decide execution. `description` and any
    APS-assigned bookkeeping drift harmlessly; `engine`, `commandLine` and
    `parameters` do not.

    This exists because "POST /activities returned 409, so it is already
    correct" is NOT idempotent provisioning: it keeps whatever body was
    uploaded first, forever. The body server/da/blank_dwg.py originally
    shipped could not produce a drawing at all, so a 409 that meant "fine"
    would have kept spending WorkItems on known-broken code. Callers should
    read the aliased version back, compare it here, and repoint the alias when
    this returns False.

    NOT strict equality on `parameters`, deliberately. A live GET of an activity
    version is APS's rendering of the body, and a server is free to echo back
    defaults a client never sent (`zip`, `ondemand`, a generated description on a
    parameter). Under `==` any such field reads as drift, so the caller would
    publish a NEW ACTIVITY VERSION AND REPOINT THE ALIAS ON EVERY SINGLE RUN -
    unbounded version growth on a shared APS account, and APS versions cannot be
    deleted. So instead:

      * the set of parameter NAMES must match exactly, which is what catches a
        removed `Script` or a stale leftover `HostDwg`; and
      * within each parameter, only the sub-keys WE SET are compared, so an
        APS-injected default is ignored while a change to `verb`, `localName` or
        `required` is still caught.

    A live readback on 2026-08-24 (session 510f244f, real APS, read-only) showed
    no injected sub-keys on this activity shape. This does not depend on that
    holding.

    LIMITATION, and it is the reason the .scr is a per-run ARGUMENT: the live
    version body carries NO `settings` key at all, so an embedded script is
    INVISIBLE here and drift in it can never be detected. Any activity that bakes
    its script into `settings` gets a drift check that silently always passes.
    Moving the script out of `settings` is what makes this guard functional, not
    merely tidier - do not "simplify" it back.

    Pure dict comparison: no HTTP, no credential, fully testable offline.
    """
    if not isinstance(live, dict) or not isinstance(want, dict):
        return False
    if live.get("engine") != want.get("engine"):
        return False
    if live.get("commandLine") != want.get("commandLine"):
        return False
    return _parameters_match(live.get("parameters"), want.get("parameters"))


def _parameters_match(live: object, want: object) -> bool:
    """Exact parameter NAME set; within each, only the sub-keys we set."""
    if not isinstance(live, dict) or not isinstance(want, dict):
        return live == want
    if set(live) != set(want):
        return False
    for name, wanted in want.items():
        found = live.get(name)
        if not isinstance(wanted, dict) or not isinstance(found, dict):
            if found != wanted:
                return False
            continue
        if any(found.get(key) != value for key, value in wanted.items()):
            return False
    return True

def blank_activity_spec(activity_id: str, engine: str,
                        out_localname: str = OUT_LOCALNAME,
                        script_localname: str = SCRIPT_LOCALNAME) -> dict:
    """The POST /activities body for the blank-CREATE Activity.

    Differs from every other Leaf Activity in the one structural way that IS
    T3-02: there is NO HostDwg parameter and no `/i` on the command line. The
    engine opens its own acad.dwt, so the create leg never references an OSS
    INPUT drawing object at all.

    The .scr arrives as a per-run ARGUMENT, not as a baked-in `settings`
    value. That is deliberate: the marker layer must be unique per run to be
    a provenance token, and baking it into the Activity would force a new
    Activity VERSION for every single create. One Activity version therefore
    serves every run.
    """
    return {
        "id": activity_id,
        "engine": engine,
        # No /i - the engine opens its own acad.dwt. See module docstring.
        "commandLine": [
            r'$(engine.path)\accoreconsole.exe /s "$(args[Script].path)"'
        ],
        "parameters": {
            "Script": {"verb": "get", "required": True, "localName": script_localname},
            "Result": {"verb": "put", "required": True, "localName": out_localname},
        },
        "description": "Leaf blank DWG creation (no input drawing; engine acad.dwt).",
    }
