r"""da/write_lisp.py — the headless DWG-MUTATE LISP for the drawing.write spike.

Mirror of da/lisp.py's CRLF discipline: accoreconsole reads a .scr line-by-line,
so every (progn ...) MUST stay on ONE line. build_write_scr() returns the
CRLF-joined script content the LeafWriteProbe Activity writes to settings[script].

The recipe proves the additive/subtractive drawing-edit path in ONE run:
  1. CMDECHO off.
  2. DELETE one pre-existing model-space LWPOLYLINE (entdel of the first hit).
  3. MAKE a probe layer "LEAF_WRITE_PROBE".
  4. ADD a closed 4-vertex LWPOLYLINE on that layer via entmake.
  5. SAVEAS the mutated drawing to the Activity's Result localName "output.dwg".
  6. QUIT.

Order is load-bearing: DELETE first, ADD second, then SAVE. The layer must be
made BEFORE the entmake (entmake does not auto-create layers). Re-extracting
output.dwg then shows: LEAF_WRITE_PROBE present in the layer table + at least one
polyline on it (the ADD), and exactly one original polyline handle gone (the
DELETE) — verified by da/write_spike.py against a re-extract of the input.
"""

# The output DWG the script writes == the Activity's Result parameter localName.
OUT_LOCALNAME = "output.dwg"

# The probe layer the ADD step creates (asserted present in the output extract,
# absent from the input extract). Kept in sync with da/write_spike.PROBE_LAYER.
PROBE_LAYER = "LEAF_WRITE_PROBE"

# Verbatim mutate LISP (each progn one line). {OUT} / {LAYER} replaced at build.
_WRITE_LISP = r"""(setvar "CMDECHO" 0)
(progn (setq ss (ssget "_X" (list (cons 0 "LWPOLYLINE") (cons 410 "Model")))) (if ss (entdel (ssname ss 0))) (princ "DELETE-DONE"))
(command "_.-LAYER" "_Make" "{LAYER}" "")
(progn (entmake (list (cons 0 "LWPOLYLINE") (cons 100 "AcDbEntity") (cons 8 "{LAYER}") (cons 100 "AcDbPolyline") (cons 90 4) (cons 70 1) (cons 10 (list 0.0 0.0)) (cons 10 (list 10.0 0.0)) (cons 10 (list 10.0 10.0)) (cons 10 (list 0.0 10.0)))) (princ "ADD-DONE"))
(command "_.SAVEAS" "" "{OUT}")
(command "_.QUIT" "_Y")"""


def build_write_scr(out_localname: str = OUT_LOCALNAME, layer: str = PROBE_LAYER) -> str:
    """Return the .scr content (CRLF line endings) for the LeafWriteProbe Activity.

    Mirrors da/lisp.build_scr: join every progn-line with \r\n + a trailing newline.
    """
    body = _WRITE_LISP.replace("{OUT}", out_localname).replace("{LAYER}", layer)
    return body.replace("\n", "\r\n") + "\r\n"
