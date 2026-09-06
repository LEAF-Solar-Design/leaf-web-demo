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
)


# Both mutation contracts currently share this tuple (including the V3 alias).
# Keep the catalogue out of _LISP so LeafExtract remains byte-identical.
MUTATION_INSPECT_BLOCKS += (
    " ".join(r'''(progn
      (defun leaf-bk-point (p precision)
        (if (null p) (setq p (list 0.0 0.0 0.0)))
        (strcat (rtos (car p) 2 precision) "," (rtos (cadr p) 2 precision) ","
          (rtos (cond ((caddr p) (caddr p)) (T 0.0)) 2 precision)))
      (defun leaf-bk-angle (a)
        (rtos (* 180.0 (/ (cond (a a) (T 0.0)) pi)) 2 6))
      (defun leaf-bk-child (name ed / kind layer body normal points value)
        (setq kind (cdr (assoc 0 ed)) layer (cdr (assoc 8 ed)))
        (if (null layer) (setq layer "0"))
        (cond
          ((= kind "LINE")
            (setq body (strcat (leaf-bk-point (cdr (assoc 10 ed)) 3) "|"
              (leaf-bk-point (cdr (assoc 11 ed)) 3))))
          ((= kind "LWPOLYLINE")
            (setq normal (cdr (assoc 210 ed)) points "")
            (if (null normal) (setq normal (list 0.0 0.0 1.0)))
            (foreach g ed (if (= (car g) 10)
              (setq points (strcat points (rtos (cadr g) 2 3) "," (rtos (caddr g) 2 3) ";"))))
            (setq body (strcat (itoa (logand 1 (cond ((cdr (assoc 70 ed))) (T 0)))) "|"
              (leaf-bk-point normal 6) "|"
              (rtos (cond ((cdr (assoc 38 ed))) (T 0.0)) 2 3) "|" points)))
          ((member kind (list "CIRCLE" "ARC"))
            (setq body (strcat (leaf-bk-point (cdr (assoc 10 ed)) 3) "|" (rtos (cdr (assoc 40 ed)) 2 3)))
            (if (= kind "ARC") (setq body (strcat body "|"
              (leaf-bk-angle (cdr (assoc 50 ed))) "|" (leaf-bk-angle (cdr (assoc 51 ed)))))))
          ((= kind "TEXT")
            (setq value (cond ((cdr (assoc 1 ed))) (T "")))
            (setq value (substr (vl-string-translate "|\r\n" "   " value) 1 512))
            (setq body (strcat (leaf-bk-point (cdr (assoc 10 ed)) 3) "|"
              (rtos (cdr (assoc 40 ed)) 2 3) "|" (leaf-bk-angle (cdr (assoc 50 ed))) "|" value)))
          (T (setq body kind kind "OTHER" layer "")))
        (strcat "BKE|" name "|" kind "|" body "|" layer))
      (princ))'''.splitlines()),
    " ".join(r'''(progn
      (setq f (open "{OUT}" "a") bk (tblnext "BLOCK" T) total 0)
      (while bk
        (setq name (cdr (assoc 2 bk)))
        (if (and name (/= (substr name 1 1) "*") (= 0 (logand 1 (cdr (assoc 70 bk)))))
          (progn
            (setq total (1+ total))
            (if (<= total 200)
              (progn
                (setq be (entnext (tblobjname "BLOCK" name)) cnt 0 complete 1 rows nil)
                (while (and be (/= (cdr (assoc 0 (entget be))) "ENDBLK"))
                  (setq bed (entget be) kind (cdr (assoc 0 bed)) cnt (1+ cnt))
                  (if (or (> cnt 60) (not (member kind (list "LINE" "LWPOLYLINE" "CIRCLE" "ARC" "TEXT"))))
                    (setq complete 0))
                  (if (<= cnt 60) (setq rows (cons (leaf-bk-child name bed) rows)))
                  (setq be (entnext be)))
                (if (null be) (setq complete 0))
                (write-line (strcat "BK|" name "|" (leaf-bk-point (cdr (assoc 10 bk)) 3) "|"
                  (itoa cnt) "|" (itoa complete)) f)
                (foreach row (reverse rows) (write-line row f))))))
        (setq bk (tblnext "BLOCK")))
      (if (> total 200) (write-line (strcat "BKCAP|" (itoa total)) f))
      (princ "BK-DONE") (close f))'''.splitlines()),
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
        if any('"BK|"' in block for block in extra_blocks):
            # The mutation round trip uses DXF degrees; leave legacy extraction
            # (whose IN rotation was radians) and its provisioned bytes alone.
            lisp = lisp.replace(
                '(rtos (cond (rot rot)(T 0.0)) 2 5)',
                '(rtos (* 180.0 (/ (cond (rot rot)(T 0.0)) pi)) 2 6)')
        lisp = lisp.replace("{QUIT}", "\n".join(extra_blocks) + "\n{QUIT}")
    body = lisp.replace("{OUT}", out_localname).replace("{QUIT}", quit_form)
    # accoreconsole scripts are CRLF; join every progn-line with \r\n and a trailing newline
    return body.replace("\n", "\r\n") + "\r\n"
