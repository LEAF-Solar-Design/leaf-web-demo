r"""Fixed AutoLISP interpreter for the closed Leaf mutation plan format.

The plan starts with ``LEAF_MUTATION_PLAN|1`` and
``BASE_SHA256|<64hex>``. It then contains only ``REMOVE|<hexhandle>``,
``TRANSFORM|<hexhandle>|<nx>,<ny>,<nz>|<elevation>|<x>,<y>;...``, and
``ADD|<safe-layer>|<nx>,<ny>,<nz>|<elevation>|<x>,<y>;...`` lines. The script
never evaluates plan text and never loads code. Numeric conversion uses
``distof`` after a character allowlist check. The server has already lowered
additions to planar OCS coordinates, so this interpreter does no projection.
"""

PLAN_LOCALNAME = "mutation-plan.txt"
OUT_LOCALNAME = "output.dwg"
INTAKE_LOCALNAME = "output-intake.txt"

_LISP_LINES = (
    '(setvar "CMDECHO" 0)',
    '(setvar "FILEDIA" 0)',
    '(defun leaf-chars-ok (s allowed / i ok) (setq i 1 ok (> (strlen s) 0)) (while (and ok (<= i (strlen s))) (if (not (vl-string-search (substr s i 1) allowed)) (setq ok nil)) (setq i (1+ i))) ok)',
    '(defun leaf-find (s token start / i found) (setq i start) (while (and (<= i (strlen s)) (not found)) (if (= (substr s i 1) token) (setq found i) (setq i (1+ i)))) found)',
    '(defun leaf-split (s token / at start out) (setq start 1) (while (setq at (leaf-find s token start)) (setq out (cons (substr s start (- at start)) out) start (1+ at))) (reverse (cons (substr s start) out)))',
    '(defun leaf-hex-len-p (s n) (and (= (strlen s) n) (leaf-chars-ok (strcase s) "0123456789ABCDEF")))',
    '(defun leaf-handle-p (s) (and (<= (strlen s) 16) (leaf-chars-ok (strcase s) "0123456789ABCDEF")))',
    '(defun leaf-layer-p (s) (and (<= (strlen s) 64) (leaf-chars-ok s "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-$ ")))',
    '(defun leaf-number (s) (if (and (<= (strlen s) 32) (leaf-chars-ok s "0123456789+-.eE")) (distof s 2)))',
    '(defun leaf-vector3 (s / v x y z) (setq v (leaf-split s ",")) (if (= (length v) 3) (progn (setq x (leaf-number (nth 0 v)) y (leaf-number (nth 1 v)) z (leaf-number (nth 2 v))) (if (and x y z (not (and (equal x 0.0 0.000000001) (equal y 0.0 0.000000001) (equal z 0.0 0.000000001)))) (list x y z)))))',
    '(defun leaf-point2 (s / v x y) (setq v (leaf-split s ",")) (if (= (length v) 2) (progn (setq x (leaf-number (nth 0 v)) y (leaf-number (nth 1 v))) (if (and x y) (list x y)))))',
    '(defun leaf-add-op (v / raw pts p normal elevation ok) (if (and (= (length v) 5) (leaf-layer-p (nth 1 v)) (setq normal (leaf-vector3 (nth 2 v))) (setq elevation (leaf-number (nth 3 v)))) (progn (setq raw (leaf-split (nth 4 v) ";") ok (>= (length raw) 3)) (while (and ok raw) (setq p (leaf-point2 (car raw))) (if p (setq pts (cons p pts)) (setq ok nil)) (setq raw (cdr raw))) (if ok (list "ADD" (nth 1 v) normal elevation (reverse pts))))))',
    '(defun leaf-remove-op (v / e kind) (if (and (= (length v) 2) (leaf-handle-p (nth 1 v)) (setq e (handent (nth 1 v))) (setq kind (cdr (assoc 0 (entget e)))) (= kind "LWPOLYLINE")) (list "REMOVE" (strcase (nth 1 v)))))',
    '(defun leaf-count-code (data code / item count) (setq count 0) (foreach item data (if (= (car item) code) (setq count (1+ count)))) count)',
    '(defun leaf-transform-op (v / e data raw pts p normal elevation ok) (if (and (= (length v) 5) (leaf-handle-p (nth 1 v)) (setq e (handent (nth 1 v))) (= (cdr (assoc 0 (setq data (entget e)))) "LWPOLYLINE") (setq normal (leaf-vector3 (nth 2 v))) (setq elevation (leaf-number (nth 3 v)))) (progn (setq raw (leaf-split (nth 4 v) ";") ok (>= (length raw) 3)) (while (and ok raw) (setq p (leaf-point2 (car raw))) (if p (setq pts (cons p pts)) (setq ok nil)) (setq raw (cdr raw))) (setq pts (reverse pts)) (if (and ok (= (length pts) (leaf-count-code data 10))) (list "TRANSFORM" (strcase (nth 1 v)) normal elevation pts)))))',
    '(defun leaf-parse-line (line / v) (setq v (leaf-split line "|")) (cond ((= (car v) "REMOVE") (leaf-remove-op v)) ((= (car v) "TRANSFORM") (leaf-transform-op v)) ((= (car v) "ADD") (leaf-add-op v))))',
    '(defun leaf-read-plan (path / fh line base op ops ok) (setq fh (open path "r") ok (if fh T nil)) (if ok (progn (setq line (read-line fh)) (if (/= line "LEAF_MUTATION_PLAN|1") (setq ok nil)))) (if ok (progn (setq line (read-line fh)) (if line (setq base (leaf-split line "|")) (setq ok nil)) (if (not (and ok (= (length base) 2) (= (car base) "BASE_SHA256") (leaf-hex-len-p (nth 1 base) 64))) (setq ok nil)))) (while (and ok (setq line (read-line fh))) (setq op (leaf-parse-line line)) (if op (setq ops (cons op ops)) (setq ok nil))) (if fh (close fh)) (if (and ok ops) (reverse ops)))',
    '(defun leaf-ensure-layer (name) (if (not (tblsearch "LAYER" name)) (entmake (list (cons 0 "LAYER") (cons 100 "AcDbSymbolTableRecord") (cons 100 "AcDbLayerTableRecord") (cons 2 name) (cons 70 0) (cons 62 7) (cons 6 "Continuous")))))',
    '(defun leaf-apply-add (op / layer normal elevation pts data p) (setq layer (nth 1 op) normal (nth 2 op) elevation (nth 3 op) pts (nth 4 op)) (leaf-ensure-layer layer) (setq data (list (cons 0 "LWPOLYLINE") (cons 100 "AcDbEntity") (cons 8 layer) (cons 100 "AcDbPolyline") (cons 90 (length pts)) (cons 70 1) (cons 38 elevation) (cons 210 normal))) (foreach p pts (setq data (append data (list (cons 10 p))))) (entmakex data))',
    '(defun leaf-apply-transform (op / e data item code pts out saw38 saw210) (setq e (handent (nth 1 op)) data (entget e) pts (nth 4 op)) (foreach item data (setq code (car item)) (cond ((= code 10) (setq out (cons (cons 10 (car pts)) out) pts (cdr pts))) ((= code 38) (setq out (cons (cons 38 (nth 3 op)) out) saw38 T)) ((= code 210) (setq out (cons (cons 210 (nth 2 op)) out) saw210 T)) (T (setq out (cons item out))))) (setq out (reverse out)) (if (not saw38) (setq out (append out (list (cons 38 (nth 3 op)))))) (if (not saw210) (setq out (append out (list (cons 210 (nth 2 op)))))) (if (and (not pts) (entmod out)) (progn (entupd e) T)))',
    '(defun leaf-apply (op) (cond ((= (car op) "REMOVE") (entdel (handent (nth 1 op)))) ((= (car op) "TRANSFORM") (leaf-apply-transform op)) ((= (car op) "ADD") (leaf-apply-add op))))',
    '(setq leaf-ops (leaf-read-plan "mutation-plan.txt"))',
    '(if (not leaf-ops) (progn (princ "LEAF-MUTATION-PLAN-INVALID") (quit)))',
    '(command "_.UNDO" "_Begin")',
    '(foreach leaf-op leaf-ops (if (not (leaf-apply leaf-op)) (progn (command "_.UNDO" "_End") (command "_.UNDO" "_Back") (princ "LEAF-MUTATION-APPLY-FAILED") (quit))))',
    '(command "_.UNDO" "_End")',
    '(command "_.SAVEAS" "" "output.dwg")',
    '(command "_.QUIT" "_Y")',
)


def build_apply_scr() -> str:
    """Return one fixed mutation plus output-inspection AutoCAD script."""
    from lisp import build_extract_body

    mutation = "\r\n".join(_LISP_LINES[:-1]) + "\r\n"
    inspection = build_extract_body(INTAKE_LOCALNAME)
    return mutation + inspection + _LISP_LINES[-1] + "\r\n"
