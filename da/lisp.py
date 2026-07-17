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
(command "_.QUIT" "_Y")"""


def build_scr(out_localname: str = OUT_LOCALNAME) -> str:
    """Return the .scr content (CRLF line endings) for the extract Activity."""
    body = _LISP.replace("{OUT}", out_localname)
    # accoreconsole scripts are CRLF; join every progn-line with \r\n and a trailing newline
    return body.replace("\n", "\r\n") + "\r\n"
