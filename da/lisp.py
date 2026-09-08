r"""da/lisp.py — the headless extraction LISP, verbatim from the proven extractor.

Source: C:/Users/ehaug/OneDrive/Documents/GitHub/utility-estimation/extracts/dwg_intake.py
The ONLY change vs. the local recipe: {OUT} is bound to the Activity's output
localName ("result.txt") so Design Automation uploads it as the Result parameter.
accoreconsole reads a .scr line-by-line, so every (progn ...) MUST stay on ONE line.
build_scr() returns the CRLF-joined script content DA writes to settings[script].
"""

# The output file the LISP writes == the Activity's Result parameter localName.
# DA runs accoreconsole in a working dir and uploads this file after the run.
OUT_LOCALNAME = "result.txt"

# accoreconsole 2026 measured 2026-09-07: 2,490 characters hangs the reader;
# 1,842 works. Keep every finished script line below that known-good length.
MAX_SCRIPT_LINE_CHARS = 1800

# Verbatim LISP block (each progn one line). {OUT} replaced at build time.
_LISP = r"""(setvar "CMDECHO" 0)
(progn (setq f (open "{OUT}" "w")) (setq lay (tblnext "LAYER" T)) (while lay (write-line (strcat "LAYER|" (cdr (assoc 2 lay))) f) (setq lay (tblnext "LAYER"))) (princ "LAYERS-DONE") (close f))
(progn (setq f (open "{OUT}" "a")) (setq ss (ssget "_X" (list (cons 0 "LWPOLYLINE") (cons 410 "Model")))) (if ss (progn (setq nn (sslength ss) i 0) (while (< i nn) (setq ed (entget (ssname ss i) (list "*")) layn (cdr (assoc 8 ed)) cl (cdr (assoc 70 ed)) el (cdr (assoc 38 ed)) nrm (cdr (assoc 210 ed)) hnd (cdr (assoc 5 ed))) (if (null el) (setq el 0.0)) (if (null nrm) (setq nrm (list 0.0 0.0 1.0))) (write-line (strcat "PL|" layn "|" (itoa (cond (cl cl)(T 0))) "|" (rtos el 2 3) "|" (rtos (car nrm) 2 6) "," (rtos (cadr nrm) 2 6) "," (rtos (caddr nrm) 2 6) "|" hnd) f) (foreach g ed (if (= 10 (car g)) (write-line (strcat "PV|" (rtos (cadr g) 2 3) "," (rtos (caddr g) 2 3)) f))) (setq xd (assoc -3 ed)) (if xd (foreach app (cdr xd) (progn (write-line (strcat "PX|" (car app)) f) (foreach pr (cdr app) (if (= 1000 (car pr)) (write-line (strcat "PXS|" (cdr pr)) f)))))) (setq i (1+ i))))) (princ "PL-DONE") (close f))
(progn (setq f (open "{OUT}" "a")) (setq ss (ssget "_X" (list (cons 0 "INSERT") (cons 410 "Model")))) (if ss (progn (setq nn (sslength ss) i 0) (while (< i nn) (setq ed (entget (ssname ss i)) nm (cdr (assoc 2 ed)) layn (cdr (assoc 8 ed)) ip (cdr (assoc 10 ed)) rot (cdr (assoc 50 ed)) nrm (cdr (assoc 210 ed)) sx (cdr (assoc 41 ed)) sy (cdr (assoc 42 ed)) sz (cdr (assoc 43 ed)) hnd (cdr (assoc 5 ed))) (if (null nrm) (setq nrm (list 0.0 0.0 1.0))) (write-line (strcat "IN|" nm "|" layn "|" (rtos (car ip) 2 3) "," (rtos (cadr ip) 2 3) "," (rtos (caddr ip) 2 3) "|" (rtos (cond (rot rot)(T 0.0)) 2 5) "|" (rtos (car nrm) 2 6) "," (rtos (cadr nrm) 2 6) "," (rtos (caddr nrm) 2 6) "|" (rtos (cond (sx sx)(T 1.0)) 2 4) "," (rtos (cond (sy sy)(T 1.0)) 2 4) "," (rtos (cond (sz sz)(T 1.0)) 2 4) "|" hnd) f) (setq i (1+ i))))) (princ "IN-DONE") (close f))
(progn (setq f (open "{OUT}" "a")) (setq ss (ssget "_X" (list (cons 0 "3DFACE") (cons 410 "Model")))) (if ss (progn (setq nn (sslength ss) i 0) (while (< i nn) (setq ed (entget (ssname ss i)) layn (cdr (assoc 8 ed))) (setq p1 (cdr (assoc 10 ed)) p2 (cdr (assoc 11 ed)) p3 (cdr (assoc 12 ed)) p4 (cdr (assoc 13 ed))) (write-line (strcat "F3|" layn "|" (rtos (car p1) 2 3) "," (rtos (cadr p1) 2 3) "," (rtos (caddr p1) 2 3) "|" (rtos (car p2) 2 3) "," (rtos (cadr p2) 2 3) "," (rtos (caddr p2) 2 3) "|" (rtos (car p3) 2 3) "," (rtos (cadr p3) 2 3) "," (rtos (caddr p3) 2 3) "|" (rtos (car p4) 2 3) "," (rtos (cadr p4) 2 3) "," (rtos (caddr p4) 2 3)) f) (setq i (1+ i))))) (princ "F3-DONE") (close f))
(progn (setq f (open "{OUT}" "a")) (setq ss (ssget "_X" (list (cons 0 "INSERT") (cons 2 "*PVBlock*") (cons 410 "Model")))) (if ss (progn (setq seen nil i 0 nn (sslength ss)) (while (and (< i nn) (< (length seen) 12)) (setq nm (cdr (assoc 2 (entget (ssname ss i))))) (if (not (member nm seen)) (progn (setq seen (cons nm seen)) (setq bdef (tblobjname "BLOCK" nm)) (if bdef (progn (write-line (strcat "BD|" nm) f) (setq be (entnext bdef) cnt 0) (while (and be (< cnt 60)) (setq bed (entget be) bt (cdr (assoc 0 bed)) pts "") (foreach gg bed (if (= 10 (car gg)) (setq pts (strcat pts (rtos (cadr gg) 2 3) "," (rtos (caddr gg) 2 3) ";")))) (if (/= pts "") (write-line (strcat "BDE|" bt "|" pts) f)) (setq be (entnext be) cnt (1+ cnt))))))) (setq i (1+ i))))) (princ "BD-DONE") (close f))
(progn (setq f (open "{OUT}" "a")) (setq gd (dictsearch (namedobjdict) "ACAD_GEOGRAPHICDATA")) (if (null gd) (write-line "GEO|none" f) (foreach pr gd (write-line (strcat "GEO|" (itoa (car pr)) "|" (cond ((= (type (cdr pr)) 'STR) (cdr pr)) ((= (type (cdr pr)) 'REAL) (rtos (cdr pr) 2 8)) ((= (type (cdr pr)) 'INT) (itoa (cdr pr))) ((= (type (cdr pr)) 'LIST) (strcat (rtos (car (cdr pr)) 2 6) "," (rtos (cadr (cdr pr)) 2 6) (if (caddr (cdr pr)) (strcat "," (rtos (caddr (cdr pr)) 2 6)) ""))) (T "?")) ) f))) (princ "GEO-DONE") (close f))
(progn (setq f (open "{OUT}" "a")) (setq idict (dictsearch (namedobjdict) "ACAD_IMAGE_DICT")) (if idict (progn (foreach pr idict (if (= 3 (car pr)) (write-line (strcat "IMGNAME|" (cdr pr)) f))) (foreach pr idict (if (= 350 (car pr)) (progn (setq ie (entget (cdr pr))) (if ie (write-line (strcat "IMG|" (cond ((cdr (assoc 1 ie)) (cdr (assoc 1 ie))) (T "?"))) f))))))) (princ "IMG-DONE") (close f))
{QUIT}"""

# How the script ends. These are NOT interchangeable — measured against a real
# accoreconsole (AutoCAD 2026, the engine family DA runs) on 2026-07-24:
#
#   input      ending                     outcome
#   ---------  -------------------------  ---------------------------------
#   .dwg       QUIT _Y                    exits in 10.2s
#   .dxf       QUIT _Y                    HANGS forever
#   .dxf       QUIT _N                    HANGS forever
#   .dxf       mark-saved + QUIT          exits in 3.4s
#   .dwg       mark-saved + QUIT          exits in 3.8s, source bytes unchanged
#
# Why: opening a DXF makes AutoCAD build a NEW in-memory drawing, which counts
# as modified and has no .dwg on disk to save back to. QUIT therefore walks into
# a SAVEAS prompt ("Current file format: AutoCAD 2018 Drawing") and blocks on
# stdin, whichever way the discard question is answered. Under DA that is not a
# visible error — the job simply burns limitProcessingTimeSec (100s) and ends as
# a timeout, and DA uploads Result only after a clean exit, so the extraction
# output is thrown away even though the LISP already wrote it.
#
# CORRECTION (2026-08-24, real accoreconsole 2026 W.164.0.0, $0, reproduced
# independently twice — once against a dirtied real DWG, once running the
# ACTUAL extract_dxf_activity_spec script against a real DXF-opened drawing):
# QUIT_SAVED does NOT mark the document unmodified. `vlax-get-acad-object`
# returns nil in this headless/DA sandbox, so `vla-put-Saved` throws
# `; error: bad argument type: VLA-OBJECT nil` every time — the mark-saved
# call is a silent no-op, not a working safety mechanism. The paragraph above
# describing it as "marks the document unmodified first" was wrong about the
# mechanism.
#
# What IS true, and reproduces: QUIT_SAVED is the LAST line of the script, so
# when its command errors, accoreconsole has nothing left to execute and exits
# at script EOF regardless — clean exit, no hang, and the LISP's already-written
# Result output survives. The safety property is "the script ends here no
# matter what," not "the document is actually unmodified." Same reproduction
# also found `(setvar "DBMOD" 0)` does NOT work as a headless-safe substitute —
# AutoCAD rejects it outright (`; error: AutoCAD variable setting rejected:
# "DBMOD" 0`) — so that is not a viable replacement either.
#
# OPEN QUESTION, not resolved here: this same session's reproduction could NOT
# reproduce the ".dxf QUIT _Y HANGS forever" row above against a trivial
# synthetic DXF (single LINE, R12 header, `vendor/acadrust-worker/fixtures/
# one_line.dxf`), with or without an explicit DBMOD-dirtying command first —
# plain `(command "_.QUIT" "_Y")` also exited cleanly (~15-17s, "; error:
# Function cancelled", no hang). That does not overturn the original
# 2026-07-24 measurement above, which may have used more complex/realistic
# DXF content that AutoCAD tracks as modified differently — it is flagged here
# as unresolved so a future session tests QUIT_DEFAULT against a real guest
# DXF before drawing any conclusion about removing QUIT_SAVED.
#
# QUIT_SAVED is left in place pending that follow-up: it is harmless (errors
# into a clean EOF exit, same as QUIT_DEFAULT would), and swapping the LIVE
# LeafExtract+prod / LeafExtractDxf Activity's baked-in script requires a new
# Activity version + alias repoint (see client.extract_dxf_activity_spec /
# blank_lisp.activity_body_matches), which is not warranted to eliminate a
# no-op line with no measured behavioral difference.
QUIT_DEFAULT = '(command "_.QUIT" "_Y")'
QUIT_SAVED = ('(vl-load-com)'
              '(vla-put-Saved (vla-get-ActiveDocument (vlax-get-acad-object)) :vlax-true)'
              '(command "_.QUIT")')


# W4g-3: the mutation Activity's same-WorkItem inspection also reports the
# kinds the browser engine writes (LINE, CIRCLE, ARC), each with its handle,
# so a plan's effects on them can be verified and the new version's intake
# carries them (intake_parse reads LN / CI / AR). These blocks are appended
# ONLY to the mutation Activity's inspect script (build_scr(...,
# extra_blocks=MUTATION_INSPECT_BLOCKS)); the LeafExtract Activities keep the
# byte-identical script above, so nothing there needs re-provisioning.
# CIRCLE / ARC centres are reported in OCS with the normal (as PL does);
# ARC angles come out of entget in radians and are written in degrees.
MUTATION_INSPECT_BLOCKS = (
    '(progn (setq f (open "{OUT}" "a")) (setq ss (ssget "_X" (list (cons 0 "LINE") (cons 410 "Model")))) (if ss (progn (setq nn (sslength ss) i 0) (while (< i nn) (setq ed (entget (ssname ss i)) layn (cdr (assoc 8 ed)) p1 (cdr (assoc 10 ed)) p2 (cdr (assoc 11 ed)) hnd (cdr (assoc 5 ed))) (write-line (strcat "LN|" layn "|" (rtos (car p1) 2 3) "," (rtos (cadr p1) 2 3) "," (rtos (caddr p1) 2 3) "|" (rtos (car p2) 2 3) "," (rtos (cadr p2) 2 3) "," (rtos (caddr p2) 2 3) "|" hnd) f) (setq i (1+ i))))) (princ "LN-DONE") (close f))',
    '(progn (setq f (open "{OUT}" "a")) (setq ss (ssget "_X" (list (cons 0 "CIRCLE") (cons 410 "Model")))) (if ss (progn (setq nn (sslength ss) i 0) (while (< i nn) (setq ed (entget (ssname ss i)) layn (cdr (assoc 8 ed)) c (cdr (assoc 10 ed)) r (cdr (assoc 40 ed)) nrm (cdr (assoc 210 ed)) hnd (cdr (assoc 5 ed))) (if (null nrm) (setq nrm (list 0.0 0.0 1.0))) (write-line (strcat "CI|" layn "|" (rtos (car c) 2 3) "," (rtos (cadr c) 2 3) "," (rtos (caddr c) 2 3) "|" (rtos r 2 3) "|" (rtos (car nrm) 2 6) "," (rtos (cadr nrm) 2 6) "," (rtos (caddr nrm) 2 6) "|" hnd) f) (setq i (1+ i))))) (princ "CI-DONE") (close f))',
    '(progn (setq f (open "{OUT}" "a")) (setq ss (ssget "_X" (list (cons 0 "ARC") (cons 410 "Model")))) (if ss (progn (setq nn (sslength ss) i 0) (while (< i nn) (setq ed (entget (ssname ss i)) layn (cdr (assoc 8 ed)) c (cdr (assoc 10 ed)) r (cdr (assoc 40 ed)) a1 (cdr (assoc 50 ed)) a2 (cdr (assoc 51 ed)) nrm (cdr (assoc 210 ed)) hnd (cdr (assoc 5 ed))) (if (null nrm) (setq nrm (list 0.0 0.0 1.0))) (write-line (strcat "AR|" layn "|" (rtos (car c) 2 3) "," (rtos (cadr c) 2 3) "," (rtos (caddr c) 2 3) "|" (rtos r 2 3) "|" (rtos (* 180.0 (/ a1 pi)) 2 6) "|" (rtos (* 180.0 (/ a2 pi)) 2 6) "|" (rtos (car nrm) 2 6) "," (rtos (cadr nrm) 2 6) "," (rtos (caddr nrm) 2 6) "|" hnd) f) (setq i (1+ i))))) (princ "AR-DONE") (close f))',
    # W4g-7b-03s: the three common properties (colour, linetype, lineweight)
    # for every entity of a kind the contract can target (LINE, LWPOLYLINE,
    # CIRCLE, ARC, INSERT; W4g-7b-04s-c added DIMENSION so a styled DIMENSION
    # target is verifiable too), reported AFTER the geometry sequences above and
    # BEFORE the BK catalogue block below (the legacy parser never sees an EP
    # row between a PL and its PV rows, since EP only follows a full pass).
    # ACI defaults to 256 (ByLayer), true colour to "~" (absent), linetype to
    # ByLayer, lineweight to -1 (ByLayer) — the same defaults entget reports
    # for an unset group. A 24-bit true colour (group 420) is decoded to
    # r,g,b inline (no separate helper needed before its first use).
    '(progn (setq f (open "{OUT}" "a")) (setq ss (ssget "_X" (list (cons -4 "<OR") (cons 0 "LINE") (cons 0 "LWPOLYLINE") (cons 0 "CIRCLE") (cons 0 "ARC") (cons 0 "INSERT") (cons 0 "DIMENSION") (cons -4 "OR>") (cons 410 "Model")))) (if ss (progn (setq nn (sslength ss) i 0) (while (< i nn) (setq ed (entget (ssname ss i)) hnd (cdr (assoc 5 ed)) aci (cdr (assoc 62 ed)) tc (cdr (assoc 420 ed)) lt (cdr (assoc 6 ed)) lw (cdr (assoc 370 ed)) enc "") (if (null aci) (setq aci 256)) (if (null lt) (setq lt "ByLayer")) (if (null lw) (setq lw -1)) (foreach ch (vl-string->list lt) (setq enc (strcat enc (cond ((= ch 37) "%25") ((= ch 124) "%7C") ((= ch 13) "%0D") ((= ch 10) "%0A") (T (chr ch)))))) (write-line (strcat "EP|" hnd "|" (itoa aci) "|" (cond (tc (strcat (itoa (lsh (logand tc 16711680) -16)) "," (itoa (lsh (logand tc 65280) -8)) "," (itoa (logand tc 255)))) (T "~")) "|" enc "|" (itoa lw)) f) (setq i (1+ i))))) (princ "EP-DONE") (close f))',
)


# Both mutation contracts currently share this tuple (including the V3 alias).
# Keep the catalogue out of _LISP so LeafExtract remains byte-identical.
MUTATION_INSPECT_BLOCKS += (
    r'''(defun leaf-bk-point (p precision) (if (null p) (setq p (list 0.0 0.0 0.0))) (strcat (rtos (car p) 2 precision) "," (rtos (cadr p) 2 precision) "," (rtos (cond ((caddr p) (caddr p)) (T 0.0)) 2 precision)))''',
    r'''(defun leaf-bk-angle (a) (rtos (* 180.0 (/ (cond (a a) (T 0.0)) pi)) 2 6))''',
    r'''(defun leaf-bk-encode (value / result ch) (setq result "") (foreach ch (vl-string->list value) (setq result (strcat result (cond ((= ch 37) "%25") ((= ch 124) "%7C") ((= ch 13) "%0D") ((= ch 10) "%0A") (T (chr ch)))))) result)''',
    r'''(defun leaf-bk-child (name ed / kind layer body normal points value) (setq kind (cdr (assoc 0 ed)) layer (cdr (assoc 8 ed))) (if (null layer) (setq layer "0")) (setq layer (leaf-bk-encode layer)) (cond ((= kind "LINE") (setq body (strcat (leaf-bk-point (cdr (assoc 10 ed)) 3) "|" (leaf-bk-point (cdr (assoc 11 ed)) 3)))) ((= kind "LWPOLYLINE") (setq normal (cdr (assoc 210 ed)) points "") (if (null normal) (setq normal (list 0.0 0.0 1.0))) (foreach g ed (if (= (car g) 10) (setq points (strcat points (rtos (cadr g) 2 3) "," (rtos (caddr g) 2 3) ";")))) (setq body (strcat (itoa (logand 1 (cond ((cdr (assoc 70 ed))) (T 0)))) "|" (leaf-bk-point normal 6) "|" (rtos (cond ((cdr (assoc 38 ed))) (T 0.0)) 2 3) "|" points))) ((member kind (list "CIRCLE" "ARC")) (setq normal (cdr (assoc 210 ed))) (if (null normal) (setq normal (list 0.0 0.0 1.0))) (setq body (strcat (leaf-bk-point (cdr (assoc 10 ed)) 3) "|" (rtos (cdr (assoc 40 ed)) 2 3))) (if (= kind "ARC") (setq body (strcat body "|" (leaf-bk-angle (cdr (assoc 50 ed))) "|" (leaf-bk-angle (cdr (assoc 51 ed)))))) (setq body (strcat body "|" (leaf-bk-point normal 6)))) ((= kind "TEXT") (setq value (cond ((cdr (assoc 1 ed))) (T ""))) (setq value (substr (vl-string-translate "|\r\n" "   " value) 1 512)) (setq body (strcat (leaf-bk-point (cdr (assoc 10 ed)) 3) "|" (rtos (cdr (assoc 40 ed)) 2 3) "|" (leaf-bk-angle (cdr (assoc 50 ed))) "|" value))) (T (setq body kind kind "OTHER" layer ""))) (strcat "BKE|" (leaf-bk-encode name) "|" kind "|" body "|" layer))''',
    r'''(princ)''',
    r'''(progn (setq f (open "{OUT}" "a") bk (tblnext "BLOCK" T) total 0) (while bk (setq name (cdr (assoc 2 bk))) (if (and name (/= (substr name 1 1) "*") (= 0 (logand 1 (cdr (assoc 70 bk))))) (progn (setq total (1+ total)) (if (<= total 200) (progn (setq be (entnext (tblobjname "BLOCK" name)) cnt 0 complete 1 rows nil) (while (and be (/= (cdr (assoc 0 (entget be))) "ENDBLK")) (setq bed (entget be) kind (cdr (assoc 0 bed)) cnt (1+ cnt)) (if (or (> cnt 60) (not (member kind (list "LINE" "LWPOLYLINE" "CIRCLE" "ARC" "TEXT")))) (setq complete 0)) (if (<= cnt 60) (setq rows (cons (leaf-bk-child name bed) rows))) (setq be (entnext be))) (write-line (strcat "BK|" (leaf-bk-encode name) "|" (leaf-bk-point (cdr (assoc 10 bk)) 3) "|" (itoa cnt) "|" (itoa complete)) f) (foreach row (reverse rows) (write-line row f)))))) (setq bk (tblnext "BLOCK"))) (if (> total 200) (write-line (strcat "BKCAP|" (itoa total)) f)) (princ "BK-DONE") (close f))''',
)


try:
    from .apply_lisp import _BLOCK_DEPENDENCY_LISP_LINES
except ImportError:
    from apply_lisp import _BLOCK_DEPENDENCY_LISP_LINES


MUTATION_INSPECT_BLOCKS += _BLOCK_DEPENDENCY_LISP_LINES + (
    '(defun leaf-bkep (name ordinal ed / aci lt lw tc colour) (setq aci (cond ((cdr (assoc 62 ed))) (T 256)) lt (cond ((cdr (assoc 6 ed))) (T "ByLayer")) lw (cond ((cdr (assoc 370 ed))) (T -1)) tc (cdr (assoc 420 ed)) colour "~") (if tc (setq colour (strcat (itoa (lsh (logand tc 16711680) -16)) "," (itoa (lsh (logand tc 65280) -8)) "," (itoa (logand tc 255))))) (write-line (strcat "BKEP|" (leaf-bk-encode name) "|" (itoa ordinal) "|" (itoa aci) "|" (leaf-bk-encode lt) "|" (itoa lw) "|" colour) f))',
    '(progn (setq f (open "{OUT}" "a") bk (tblnext "BLOCK" T) bn-display 0) (write-line "BKEPC|1" f) (while bk (setq name (cdr (assoc 2 bk))) (if (and (/= (substr name 1 1) "*") (= 0 (logand 1 (cdr (assoc 70 bk))))) (progn (setq bn-display (1+ bn-display)) (if (<= bn-display 200) (progn (setq be (entnext (tblobjname "BLOCK" name)) ordinal 0) (while (and be (< ordinal 60) (/= (cdr (assoc 0 (entget be))) "ENDBLK")) (leaf-bkep name ordinal (entget be)) (setq ordinal (1+ ordinal) be (entnext be))))))) (setq bk (tblnext "BLOCK"))) (close f))',
    '(progn (setq f (open "{OUT}" "a") ss (ssget "_X" (list (cons 0 "LINE,LWPOLYLINE,CIRCLE,ARC") (cons 410 "Model"))) i 0) (if ss (repeat (sslength ss) (setq ed (entget (ssname ss i)) i (1+ i) bulged 0 wide 0 normal (cond ((cdr (assoc 210 ed))) (T (list 0.0 0.0 1.0)))) (if (= (cdr (assoc 0 ed)) "LWPOLYLINE") (foreach pair ed (if (and (= (car pair) 42) (/= (cdr pair) 0.0)) (setq bulged 1)) (if (and (member (car pair) (list 40 41 43)) (/= (cdr pair) 0.0)) (setq wide 1)))) (write-line (strcat "BM|" (cdr (assoc 5 ed)) "|" (cdr (assoc 0 ed)) "|" (leaf-bk-point normal 6) "|" (itoa bulged) "|" (if (leaf-bd-dimension-p ed) "1" "0") "|" (itoa wide)) f))) (close f))',
)



MUTATION_INSPECT_BLOCKS += (
    '(defun leaf-gr-backlink (member group / item reactors target data) (setq data (entget member)) (foreach item data (cond ((= (car item) 102) (setq reactors (= (cdr item) "{ACAD_REACTORS"))) ((and reactors (= (car item) 330)) (setq target (cdr (assoc 5 (entget (cdr item))))) (if (= target group) (write-line (strcat "GM|" (cdr (assoc 5 data)) "|" group) f))))))',
    '(defun leaf-gr-members (data / result item h) (setq result "") (foreach item data (if (= (car item) 340) (progn (setq h (cdr (assoc 5 (entget (cdr item))))) (if h (setq result (strcat result (if (= result "") "" ";") h)))))) result)',
    '(defun leaf-gr-row (name e owner / data h item) (setq data (entget e) h (cdr (assoc 5 data))) (if (= (cdr (assoc 0 data)) "GROUP") (progn (write-line (strcat "GR|" h "|" (leaf-bk-encode name) "|" owner "|" (itoa (cdr (assoc 70 data))) "|" (itoa (cdr (assoc 71 data))) "|" (leaf-gr-members data)) f) (foreach item data (if (= (car item) 340) (leaf-gr-backlink (cdr item) h))))))',
    '(progn (setq f (open "{OUT}" "a") gr-dict (dictsearch (namedobjdict) "ACAD_GROUP")) (write-line "GRC|1" f) (write-line "MEC|1" f) (if gr-dict (progn (setq gr-e (cdr (assoc -1 gr-dict)) gr-owner (cdr (assoc 5 (entget gr-e))) gr-name nil) (foreach gr-pair (entget gr-e) (cond ((= (car gr-pair) 3) (setq gr-name (cdr gr-pair))) ((and gr-name (member (car gr-pair) (list 350 360))) (leaf-gr-row gr-name (cdr gr-pair) gr-owner) (setq gr-name nil)))))) (close f))',
    '(progn (setq f (open "{OUT}" "a") gr-ca (open "created-handles.txt" "r")) (if gr-ca (progn (while (setq gr-line (read-line gr-ca)) (write-line gr-line f)) (close gr-ca))) (close f))',
)


# W4g-7c-3s-1: MLEADER styles and entities precede the frozen DS/DM tail.
# The walk helpers use the row's dynamically scoped locals for one entget pass.
MUTATION_INSPECT_BLOCKS += (
    '(defun leaf-ml-textstyle (h / data) (if (and h (setq data (entget h))) (cond ((cdr (assoc 2 data))) (T "")) ""))',
    '(defun leaf-ms-row (name e / data) (setq data (entget e)) (write-line (strcat "MS|" (leaf-bk-encode name) "|" (leaf-bk-encode (leaf-ml-textstyle (cdr (assoc 342 data)))) "|" (rtos (cdr (assoc 45 data)) 2 5) "|" (rtos (cdr (assoc 44 data)) 2 5) "|" (rtos (cdr (assoc 43 data)) 2 5) "|" (rtos (cdr (assoc 42 data)) 2 5) "|" (itoa (cdr (assoc 173 data)))) f))',
    '(progn (setq f (open "{OUT}" "a") ml-dict (dictsearch (namedobjdict) "ACAD_MLEADERSTYLE") ml-names nil ml-name nil) (foreach ml-pair ml-dict (cond ((= (car ml-pair) 3) (setq ml-name (cdr ml-pair))) ((and ml-name (= (car ml-pair) 350)) (setq ml-names (cons (cons (cdr ml-pair) ml-name) ml-names)) (leaf-ms-row ml-name (cdr ml-pair)) (setq ml-name nil)))) (princ "MS-DONE") (close f))',
    '(defun leaf-ml-context (code value) (cond ((= code 41) (setq height value)) ((= code 140) (setq arrow value)) ((= code 12) (setq textpt value)) ((= code 304) (setq text value)) ((= code 171) (setq attachment value))))',
    '(defun leaf-ml-walk (data / pair code value) (foreach pair data (setq code (car pair) value (cdr pair)) (cond ((and (= code 300) (= value "CONTEXT_DATA{")) (setq state 1)) ((and (= state 1) (= code 302) (= value "LEADER{")) (setq state 2)) ((and (= state 2) (= code 304) (= value "LEADER_LINE{")) (setq state 3 branches (1+ branches))) ((and (= state 3) (= code 305) (= value "}")) (setq state 2)) ((and (= state 2) (= code 303) (= value "}")) (setq state 1)) ((and (= state 1) (= code 301) (= value "}")) (setq state 0 closed T)) ((= state 1) (leaf-ml-context code value)) ((= state 2) (cond ((= code 10) (setq landing value)) ((= code 11) (setq doglegdir value)) ((= code 40) (setq dogleg value)))) ((and (= state 3) (= code 10)) (setq vertices (strcat vertices (if (= vertices "") "" ";") (leaf-bk-point value 5)))) ((and (= state 0) closed) (cond ((and (= code 340) (null style)) (setq style (cdr (assoc value ml-names)))) ((= code 343) (setq textstyle (leaf-ml-textstyle value))) ((= code 172) (setq content value)))))))',
    '(defun leaf-ml-row (data / state closed branches vertices height arrow textpt text attachment landing doglegdir dogleg style textstyle content) (setq state 0 branches 0 vertices "") (leaf-ml-walk data) (if (and style (= branches 1) (= content 2) text (/= vertices "") landing) (write-line (strcat "ML|" (cdr (assoc 5 data)) "|" (leaf-bk-encode (cdr (assoc 8 data))) "|" (leaf-bk-encode (cond (style) (T ""))) "|" (leaf-bk-encode (cond (textstyle) (T ""))) "|" (rtos height 2 5) "|" (rtos arrow 2 5) "|" (rtos dogleg 2 5) "|" (itoa attachment) "|" vertices "|" (leaf-bk-point landing 5) "|" (leaf-bk-point doglegdir 5) "|" (leaf-bk-point textpt 5) "|" (leaf-bk-encode text)) f) (write-line "MLX|1" f)))',
    '(progn (setq f (open "{OUT}" "a") ss (ssget "_X" (list (cons 0 "MULTILEADER") (cons 410 "Model"))) i 0) (if ss (repeat (sslength ss) (leaf-ml-row (entget (ssname ss i))) (setq i (1+ i)))) (princ "ML-DONE") (close f))',
)


# W4g-7b-04s: the DIMSTYLE catalogue (DS) and every model-space rotated/
# aligned DIMENSION (DM), placed after the BK catalogue so both reuse its
# leaf-bk-point/leaf-bk-encode helpers instead of re-deriving them. An
# unsupported dimension subtype is counted, never refused (DMX). The
# measurement is ALWAYS the GEOMETRIC value computed from the definition
# points and the rotation (LINEAR: the projection of def2-def1 onto the
# rotation axis; ALIGNED: the plain distance) — never DXF group 42, which a
# dimstyle with DIMLFAC != 1 scales for on-screen display and would refuse
# every added dimension in such a drawing against server/mutation_plan.py's
# unscaled computation. F3 (opus round-one read of PR #1119): groups 13/14/10
# are WCS per the DXF spec (only 11/12/16 are OCS), so p1/p2/dl are reported
# exactly as entget returns them, with no `trans` (arbitrary-axis) call.
MUTATION_INSPECT_BLOCKS += (
    r'''(progn (setq f (open "{OUT}" "a")) (setq ds (tblnext "DIMSTYLE" T)) (while ds (write-line (strcat "DS|" (leaf-bk-encode (cdr (assoc 2 ds)))) f) (setq ds (tblnext "DIMSTYLE"))) (princ "DS-DONE") (close f))''',
    r'''(progn (setq f (open "{OUT}" "a")) (setq ss (ssget "_X" (list (cons 0 "DIMENSION") (cons 410 "Model")))) (if ss (progn (setq nn (sslength ss) i 0) (while (< i nn) (setq ed (entget (ssname ss i)) sub (logand (cdr (assoc 70 ed)) 15) p1 (cdr (assoc 13 ed)) p2 (cdr (assoc 14 ed)) dl (cdr (assoc 10 ed)) rot (cdr (assoc 50 ed)) style (cdr (assoc 3 ed)) layer (cdr (assoc 8 ed)) nrm (cdr (assoc 210 ed)) hnd (cdr (assoc 5 ed)) kind nil) (if (null nrm) (setq nrm (list 0.0 0.0 1.0))) (if (null style) (setq style "Standard")) (if (null layer) (setq layer "0")) (if (null rot) (setq rot 0.0)) (cond ((= sub 0) (setq kind "LINEAR")) ((= sub 1) (setq kind "ALIGNED"))) (if kind (progn (setq wp1 p1 wp2 p2 wdl dl) (setq meas (if (= kind "LINEAR") (abs (+ (* (- (car wp2) (car wp1)) (cos rot)) (* (- (cadr wp2) (cadr wp1)) (sin rot)))) (distance wp1 wp2))) (write-line (strcat "DM|" kind "|" (leaf-bk-encode layer) "|" (leaf-bk-point wp1 3) "|" (leaf-bk-point wp2 3) "|" (leaf-bk-point wdl 3) "|" (rtos (* 180.0 (/ rot pi)) 2 6) "|" (leaf-bk-encode style) "|" (leaf-bk-point nrm 6) "|" (rtos meas 2 3) "|" hnd) f)) (write-line (strcat "DMX|" hnd "|" (itoa sub)) f)) (setq i (1+ i))))) (princ "DM-DONE") (close f))''',
)



def build_scr(out_localname: str = OUT_LOCALNAME, *, quit_form: str = QUIT_DEFAULT,
              extra_blocks: tuple = ()) -> str:
    """Return the .scr content (CRLF line endings) for the extract Activity.

    quit_form defaults to the DWG-proven ending so the existing Activity's
    script is byte-identical to what is live today. DXF input REQUIRES
    QUIT_SAVED (see the table above) or the WorkItem hangs to timeout.
    extra_blocks (one progn per entry, `{OUT}` bound like the rest) are
    inserted before the quit form; with none given the output is unchanged.
    """
    lisp = _LISP
    if extra_blocks:
        # The IN record keeps its legacy unit (radians, unconverted) in EVERY
        # script that carries it: LeafExtract and the mutation-inspect variant
        # must report the SAME rotation for the SAME physical INSERT, or the
        # unchanged-INSERT verifier compares radians against degrees and
        # either rejects a genuine no-op or misses a real rotation. Degrees
        # are confined to the DM record, never to INSERT rotation.
        lisp = lisp.replace("{QUIT}", "\n".join(extra_blocks) + "\n{QUIT}")
    body = lisp.replace("{OUT}", out_localname).replace("{QUIT}", quit_form)
    # accoreconsole scripts are CRLF; join every progn-line with \r\n and a trailing newline
    script = body.replace("\n", "\r\n") + "\r\n"
    for line in script.splitlines():
        if len(line) > MAX_SCRIPT_LINE_CHARS:
            raise ValueError(
                f"Script line exceeds {MAX_SCRIPT_LINE_CHARS} characters: {line[:40]}")
    return script
