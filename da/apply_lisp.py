r"""Fixed AutoLISP interpreter for the closed Leaf mutation plan format.

The plan starts with ``LEAF_MUTATION_PLAN|1`` or ``LEAF_MUTATION_PLAN|2`` and
``BASE_SHA256|<64hex>``. Version 1 lines (catalog tools): ``REMOVE|<hex>``,
``TRANSFORM|<hex>|<nx>,<ny>,<nz>|<elevation>|<x>,<y>;...`` and
``ADD|<safe-layer>|<nx>,<ny>,<nz>|<elevation>|<x>,<y>;...`` (a closed
LWPOLYLINE). Version 2 lines (W4g-3, the browser engine's saves):
``ADDOPEN|<layer>|<normal>|<elev>|<pts>`` (an open LWPOLYLINE, >= 2 points),
``ADDLINE|<layer>|<x1>,<y1>,<z1>|<x2>,<y2>,<z2>``,
``ADDCIRCLE|<layer>|<cx>,<cy>,<cz>|<r>``,
``ADDARC|<layer>|<cx>,<cy>,<cz>|<r>|<start_deg>|<end_deg>``,
``RELAYER|<hex>|<layer>``,
``SETPOINTS|<hex>|<closed 0|1>|<normal>|<elev>|<pts>`` (a LINE takes exactly
two points and closed 0; an LWPOLYLINE takes any count, its vertex list is
rebuilt), ``SETCIRCLE|<hex>|<centre>|<r>`` and
``SETARC|<hex>|<centre>|<r>|<start_deg>|<end_deg>``. REMOVE covers
LWPOLYLINE, LINE, CIRCLE and ARC. The script never evaluates plan text and
never loads code. Numeric conversion uses ``distof`` after a character
allowlist check. The server has already lowered polyline geometry to planar
OCS coordinates; a LINE's endpoints come back to world coordinates through
AutoCAD's own ``trans`` from the plan's normal, and circle and arc centres are
world coordinates on the +z plane (the server refuses tilted ones). Angles on
the plan are degrees; the interpreter converts to the radians entmake takes.
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
    '(defun leaf-flag01 (s) (cond ((= s "0") 0) ((= s "1") 1)))',
    '(defun leaf-deg2rad (d) (* pi (/ d 180.0)))',
    '(defun leaf-vector3 (s / v x y z) (setq v (leaf-split s ",")) (if (= (length v) 3) (progn (setq x (leaf-number (nth 0 v)) y (leaf-number (nth 1 v)) z (leaf-number (nth 2 v))) (if (and x y z (not (and (equal x 0.0 0.000000001) (equal y 0.0 0.000000001) (equal z 0.0 0.000000001)))) (list x y z)))))',
    '(defun leaf-point3 (s / v x y z) (setq v (leaf-split s ",")) (if (= (length v) 3) (progn (setq x (leaf-number (nth 0 v)) y (leaf-number (nth 1 v)) z (leaf-number (nth 2 v))) (if (and x y z) (list x y z)))))',
    '(defun leaf-point2 (s / v x y) (setq v (leaf-split s ",")) (if (= (length v) 2) (progn (setq x (leaf-number (nth 0 v)) y (leaf-number (nth 1 v))) (if (and x y) (list x y)))))',
    '(defun leaf-points2 (s least / raw pts p ok) (setq raw (leaf-split s ";") ok (>= (length raw) least)) (while (and ok raw) (setq p (leaf-point2 (car raw))) (if p (setq pts (cons p pts)) (setq ok nil)) (setq raw (cdr raw))) (if ok (reverse pts)))',
    '(defun leaf-radius (s / r) (setq r (leaf-number s)) (if (and r (> r 0.0)) r))',
    '(defun leaf-kind (h / e) (if (and (leaf-handle-p h) (setq e (handent h))) (cdr (assoc 0 (entget e)))))',
    '(defun leaf-add-op (v / raw pts p normal elevation ok) (if (and (= (length v) 5) (leaf-layer-p (nth 1 v)) (setq normal (leaf-vector3 (nth 2 v))) (setq elevation (leaf-number (nth 3 v)))) (progn (setq raw (leaf-split (nth 4 v) ";") ok (>= (length raw) 3)) (while (and ok raw) (setq p (leaf-point2 (car raw))) (if p (setq pts (cons p pts)) (setq ok nil)) (setq raw (cdr raw))) (if ok (list "ADD" (nth 1 v) normal elevation (reverse pts))))))',
    '(defun leaf-addopen-op (v / pts normal elevation) (if (and (= (length v) 5) (leaf-layer-p (nth 1 v)) (setq normal (leaf-vector3 (nth 2 v))) (setq elevation (leaf-number (nth 3 v))) (setq pts (leaf-points2 (nth 4 v) 2))) (list "ADDOPEN" (nth 1 v) normal elevation pts)))',
    '(defun leaf-addline-op (v / p1 p2) (if (and (= (length v) 4) (leaf-layer-p (nth 1 v)) (setq p1 (leaf-point3 (nth 2 v))) (setq p2 (leaf-point3 (nth 3 v))) (not (equal p1 p2 0.000000001))) (list "ADDLINE" (nth 1 v) p1 p2)))',
    '(defun leaf-addcircle-op (v / c r) (if (and (= (length v) 4) (leaf-layer-p (nth 1 v)) (setq c (leaf-point3 (nth 2 v))) (setq r (leaf-radius (nth 3 v)))) (list "ADDCIRCLE" (nth 1 v) c r)))',
    '(defun leaf-addarc-op (v / c r a b) (if (and (= (length v) 6) (leaf-layer-p (nth 1 v)) (setq c (leaf-point3 (nth 2 v))) (setq r (leaf-radius (nth 3 v))) (setq a (leaf-number (nth 4 v))) (setq b (leaf-number (nth 5 v))) (not (equal a b 0.000000001))) (list "ADDARC" (nth 1 v) c r a b)))',
    '(defun leaf-remove-op (v / kind) (if (and (= (length v) 2) (setq kind (leaf-kind (nth 1 v))) (member kind (list "LWPOLYLINE" "LINE" "CIRCLE" "ARC"))) (list "REMOVE" (strcase (nth 1 v)))))',
    '(defun leaf-count-code (data code / item count) (setq count 0) (foreach item data (if (= (car item) code) (setq count (1+ count)))) count)',
    '(defun leaf-transform-op (v / e data raw pts p normal elevation ok) (if (and (= (length v) 5) (leaf-handle-p (nth 1 v)) (setq e (handent (nth 1 v))) (= (cdr (assoc 0 (setq data (entget e)))) "LWPOLYLINE") (setq normal (leaf-vector3 (nth 2 v))) (setq elevation (leaf-number (nth 3 v)))) (progn (setq raw (leaf-split (nth 4 v) ";") ok (>= (length raw) 3)) (while (and ok raw) (setq p (leaf-point2 (car raw))) (if p (setq pts (cons p pts)) (setq ok nil)) (setq raw (cdr raw))) (setq pts (reverse pts)) (if (and ok (= (length pts) (leaf-count-code data 10))) (list "TRANSFORM" (strcase (nth 1 v)) normal elevation pts)))))',
    '(defun leaf-relayer-op (v / kind) (if (and (= (length v) 3) (setq kind (leaf-kind (nth 1 v))) (member kind (list "LWPOLYLINE" "LINE" "CIRCLE" "ARC")) (leaf-layer-p (nth 2 v))) (list "RELAYER" (strcase (nth 1 v)) (nth 2 v))))',
    '(defun leaf-setpoints-op (v / kind closed normal elevation pts) (if (and (= (length v) 6) (setq kind (leaf-kind (nth 1 v))) (member kind (list "LWPOLYLINE" "LINE")) (setq closed (leaf-flag01 (nth 2 v))) (setq normal (leaf-vector3 (nth 3 v))) (setq elevation (leaf-number (nth 4 v))) (setq pts (leaf-points2 (nth 5 v) 2)) (or (= kind "LWPOLYLINE") (and (= (length pts) 2) (= closed 0))) (or (= closed 0) (>= (length pts) 3))) (list "SETPOINTS" (strcase (nth 1 v)) closed normal elevation pts)))',
    '(defun leaf-setcircle-op (v / c r) (if (and (= (length v) 4) (= (leaf-kind (nth 1 v)) "CIRCLE") (setq c (leaf-point3 (nth 2 v))) (setq r (leaf-radius (nth 3 v)))) (list "SETCIRCLE" (strcase (nth 1 v)) c r)))',
    '(defun leaf-setarc-op (v / c r a b) (if (and (= (length v) 6) (= (leaf-kind (nth 1 v)) "ARC") (setq c (leaf-point3 (nth 2 v))) (setq r (leaf-radius (nth 3 v))) (setq a (leaf-number (nth 4 v))) (setq b (leaf-number (nth 5 v))) (not (equal a b 0.000000001))) (list "SETARC" (strcase (nth 1 v)) c r a b)))',
    '(defun leaf-parse-line (line / v) (setq v (leaf-split line "|")) (cond ((= (car v) "REMOVE") (leaf-remove-op v)) ((= (car v) "TRANSFORM") (leaf-transform-op v)) ((= (car v) "ADD") (leaf-add-op v)) ((= (car v) "ADDOPEN") (leaf-addopen-op v)) ((= (car v) "ADDLINE") (leaf-addline-op v)) ((= (car v) "ADDCIRCLE") (leaf-addcircle-op v)) ((= (car v) "ADDARC") (leaf-addarc-op v)) ((= (car v) "RELAYER") (leaf-relayer-op v)) ((= (car v) "SETPOINTS") (leaf-setpoints-op v)) ((= (car v) "SETCIRCLE") (leaf-setcircle-op v)) ((= (car v) "SETARC") (leaf-setarc-op v))))',
    '(defun leaf-read-plan (path / fh line base op ops ok) (setq fh (open path "r") ok (if fh T nil)) (if ok (progn (setq line (read-line fh)) (if (not (member line (list "LEAF_MUTATION_PLAN|1" "LEAF_MUTATION_PLAN|2"))) (setq ok nil)))) (if ok (progn (setq line (read-line fh)) (if line (setq base (leaf-split line "|")) (setq ok nil)) (if (not (and ok (= (length base) 2) (= (car base) "BASE_SHA256") (leaf-hex-len-p (nth 1 base) 64))) (setq ok nil)))) (while (and ok (setq line (read-line fh))) (setq op (leaf-parse-line line)) (if op (setq ops (cons op ops)) (setq ok nil))) (if fh (close fh)) (if (and ok ops) (reverse ops)))',
    '(defun leaf-ensure-layer (name) (if (not (tblsearch "LAYER" name)) (entmake (list (cons 0 "LAYER") (cons 100 "AcDbSymbolTableRecord") (cons 100 "AcDbLayerTableRecord") (cons 2 name) (cons 70 0) (cons 62 7) (cons 6 "Continuous")))))',
    '(defun leaf-apply-poly (layer normal elevation pts closed / data p) (leaf-ensure-layer layer) (setq data (list (cons 0 "LWPOLYLINE") (cons 100 "AcDbEntity") (cons 8 layer) (cons 100 "AcDbPolyline") (cons 90 (length pts)) (cons 70 closed) (cons 38 elevation) (cons 210 normal))) (foreach p pts (setq data (append data (list (cons 10 p))))) (entmakex data))',
    '(defun leaf-apply-add (op) (leaf-apply-poly (nth 1 op) (nth 2 op) (nth 3 op) (nth 4 op) 1))',
    '(defun leaf-apply-addopen (op) (leaf-apply-poly (nth 1 op) (nth 2 op) (nth 3 op) (nth 4 op) 0))',
    '(defun leaf-apply-addline (op) (leaf-ensure-layer (nth 1 op)) (entmakex (list (cons 0 "LINE") (cons 100 "AcDbEntity") (cons 8 (nth 1 op)) (cons 100 "AcDbLine") (cons 10 (nth 2 op)) (cons 11 (nth 3 op)))))',
    '(defun leaf-apply-addcircle (op) (leaf-ensure-layer (nth 1 op)) (entmakex (list (cons 0 "CIRCLE") (cons 100 "AcDbEntity") (cons 8 (nth 1 op)) (cons 100 "AcDbCircle") (cons 10 (nth 2 op)) (cons 40 (nth 3 op)) (cons 210 (list 0.0 0.0 1.0)))))',
    '(defun leaf-apply-addarc (op) (leaf-ensure-layer (nth 1 op)) (entmakex (list (cons 0 "ARC") (cons 100 "AcDbEntity") (cons 8 (nth 1 op)) (cons 100 "AcDbCircle") (cons 10 (nth 2 op)) (cons 40 (nth 3 op)) (cons 210 (list 0.0 0.0 1.0)) (cons 100 "AcDbArc") (cons 50 (leaf-deg2rad (nth 4 op))) (cons 51 (leaf-deg2rad (nth 5 op))))))',
    '(defun leaf-apply-transform (op / e data item code pts out saw38 saw210) (setq e (handent (nth 1 op)) data (entget e) pts (nth 4 op)) (foreach item data (setq code (car item)) (cond ((= code 10) (setq out (cons (cons 10 (car pts)) out) pts (cdr pts))) ((= code 38) (setq out (cons (cons 38 (nth 3 op)) out) saw38 T)) ((= code 210) (setq out (cons (cons 210 (nth 2 op)) out) saw210 T)) (T (setq out (cons item out))))) (setq out (reverse out)) (if (not saw38) (setq out (append out (list (cons 38 (nth 3 op)))))) (if (not saw210) (setq out (append out (list (cons 210 (nth 2 op)))))) (if (and (not pts) (entmod out)) (progn (entupd e) T)))',
    '(defun leaf-apply-relayer (op / e data out) (leaf-ensure-layer (nth 2 op)) (setq e (handent (nth 1 op)) data (entget e)) (setq out (subst (cons 8 (nth 2 op)) (assoc 8 data) data)) (if (entmod out) (progn (entupd e) T)))',
    '(defun leaf-apply-setpoints (op / e data kind item code head saw38 saw90 saw70 n pts normal elev closed out) (setq e (handent (nth 1 op)) data (entget e) kind (cdr (assoc 0 data)) closed (nth 2 op) normal (nth 3 op) elev (nth 4 op) pts (nth 5 op) n (length pts)) (cond ((= kind "LINE") (if (and (= n 2) (= closed 0)) (progn (setq out (subst (cons 10 (trans (list (car (car pts)) (cadr (car pts)) elev) normal 0)) (assoc 10 data) data)) (setq out (subst (cons 11 (trans (list (car (cadr pts)) (cadr (cadr pts)) elev) normal 0)) (assoc 11 out) out)) (if (entmod out) (progn (entupd e) T))))) ((= kind "LWPOLYLINE") (foreach item data (setq code (car item)) (cond ((member code (list 10 40 41 42 210)) nil) ((= code 90) (setq head (cons (cons 90 n) head) saw90 T)) ((= code 70) (setq head (cons (cons 70 closed) head) saw70 T)) ((= code 38) (setq head (cons (cons 38 elev) head) saw38 T)) (T (setq head (cons item head))))) (setq head (reverse head)) (if (not saw90) (setq head (append head (list (cons 90 n))))) (if (not saw70) (setq head (append head (list (cons 70 closed))))) (if (not saw38) (setq head (append head (list (cons 38 elev))))) (foreach p pts (setq head (append head (list (cons 10 p))))) (setq head (append head (list (cons 210 normal)))) (if (entmod head) (progn (entupd e) T)))))',
    '(defun leaf-apply-setcircle (op / e data out) (setq e (handent (nth 1 op)) data (entget e)) (setq out (subst (cons 10 (nth 2 op)) (assoc 10 data) data)) (setq out (subst (cons 40 (nth 3 op)) (assoc 40 out) out)) (if (entmod out) (progn (entupd e) T)))',
    '(defun leaf-apply-setarc (op / e data out) (setq e (handent (nth 1 op)) data (entget e)) (setq out (subst (cons 10 (nth 2 op)) (assoc 10 data) data)) (setq out (subst (cons 40 (nth 3 op)) (assoc 40 out) out)) (setq out (subst (cons 50 (leaf-deg2rad (nth 4 op))) (assoc 50 out) out)) (setq out (subst (cons 51 (leaf-deg2rad (nth 5 op))) (assoc 51 out) out)) (if (entmod out) (progn (entupd e) T)))',
    '(defun leaf-apply (op) (cond ((= (car op) "REMOVE") (entdel (handent (nth 1 op)))) ((= (car op) "TRANSFORM") (leaf-apply-transform op)) ((= (car op) "ADD") (leaf-apply-add op)) ((= (car op) "ADDOPEN") (leaf-apply-addopen op)) ((= (car op) "ADDLINE") (leaf-apply-addline op)) ((= (car op) "ADDCIRCLE") (leaf-apply-addcircle op)) ((= (car op) "ADDARC") (leaf-apply-addarc op)) ((= (car op) "RELAYER") (leaf-apply-relayer op)) ((= (car op) "SETPOINTS") (leaf-apply-setpoints op)) ((= (car op) "SETCIRCLE") (leaf-apply-setcircle op)) ((= (car op) "SETARC") (leaf-apply-setarc op))))',
    '(setq leaf-ops (leaf-read-plan "mutation-plan.txt"))',
    '(if (not leaf-ops) (progn (princ "LEAF-MUTATION-PLAN-INVALID") (quit)))',
    '(command "_.UNDO" "_Begin")',
    '(foreach leaf-op leaf-ops (if (not (leaf-apply leaf-op)) (progn (command "_.UNDO" "_End") (command "_.UNDO" "_Back") (princ "LEAF-MUTATION-APPLY-FAILED") (quit))))',
    '(command "_.UNDO" "_End")',
    '(command "_.SAVEAS" "" "output.dwg")',
    '(command "_.QUIT" "_Y")',
)


def build_apply_scr() -> str:
    """Return the fixed CRLF AutoCAD mutation script."""
    return "\r\n".join(_LISP_LINES) + "\r\n"
