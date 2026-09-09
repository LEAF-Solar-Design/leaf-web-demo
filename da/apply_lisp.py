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
    '(progn (setq leaf-apply-ok T) (command "_.UNDO" "_Begin"))',
    '(foreach leaf-op leaf-ops (if (not (leaf-apply leaf-op)) (progn (setq leaf-apply-ok nil) (command "_.UNDO" "_End") (command "_.UNDO" "_Back") (princ "LEAF-MUTATION-APPLY-FAILED") (quit))))',
    '(command "_.UNDO" "_End")',
    '(if (and leaf-ops leaf-apply-ok) (command "_.SAVEAS" "" "output.dwg"))',
    '(command "_.QUIT" "_Y")',
)


def build_apply_scr() -> str:
    """Return the fixed CRLF AutoCAD mutation script."""
    return "\r\n".join(_LISP_LINES) + "\r\n"


_INSERT_LISP_LINES = (
    '(defun leaf-addinsert-op (v / layer name pt rot scale) (if (and (= (length v) 6) (setq layer (nth 1 v) name (nth 2 v)) (<= (strlen layer) 255) (leaf-chars-ok layer "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-$ ") (> (strlen name) 0) (<= (strlen name) 255) (/= (substr name 1 1) "*") (not (vl-string-search (chr 13) name)) (not (vl-string-search (chr 10) name)) (tblsearch "BLOCK" name) (setq pt (leaf-point3 (nth 3 v))) (setq rot (leaf-number (nth 4 v))) (setq scale (leaf-point3 (nth 5 v))) (/= (car scale) 0.0) (/= (cadr scale) 0.0) (/= (caddr scale) 0.0)) (list "ADDINSERT" layer name pt rot scale)))',
    '(defun leaf-apply-addinsert (op / layer name pt rot sx sy sz) (setq layer (nth 1 op) name (nth 2 op) pt (nth 3 op) rot (nth 4 op) sx (car (nth 5 op)) sy (cadr (nth 5 op)) sz (caddr (nth 5 op))) (if (tblsearch "BLOCK" name) (progn (leaf-ensure-layer layer) (entmakex (list (cons 0 "INSERT") (cons 100 "AcDbEntity") (cons 8 layer) (cons 100 "AcDbBlockReference") (cons 2 name) (cons 10 pt) (cons 41 sx) (cons 42 sy) (cons 43 sz) (cons 50 (/ (* rot pi) 180.0)))))))',
)


_DIMENSION_LISP_LINES = (
    '(defun leaf-adddimlinear-op (v / layer style def1 def2 dimline rot) (if (and (= (length v) 7) (setq layer (nth 1 v) style (nth 2 v)) (<= (strlen layer) 255) (leaf-chars-ok layer "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-$ ") (> (strlen style) 0) (<= (strlen style) 255) (leaf-chars-ok style "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-$ ") (tblsearch "DIMSTYLE" style) (setq def1 (leaf-point3 (nth 3 v))) (setq def2 (leaf-point3 (nth 4 v))) (setq dimline (leaf-point3 (nth 5 v))) (setq rot (leaf-number (nth 6 v)))) (list "ADDDIMLINEAR" layer style def1 def2 dimline rot)))',
    '(defun leaf-adddimaligned-op (v / layer style def1 def2 dimline) (if (and (= (length v) 6) (setq layer (nth 1 v) style (nth 2 v)) (<= (strlen layer) 255) (leaf-chars-ok layer "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-$ ") (> (strlen style) 0) (<= (strlen style) 255) (leaf-chars-ok style "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-$ ") (tblsearch "DIMSTYLE" style) (setq def1 (leaf-point3 (nth 3 v))) (setq def2 (leaf-point3 (nth 4 v))) (setq dimline (leaf-point3 (nth 5 v)))) (list "ADDDIMALIGNED" layer style def1 def2 dimline)))',
    '(defun leaf-apply-adddimlinear (op / layer style def1 def2 dimline rot) (setq layer (nth 1 op) style (nth 2 op) def1 (nth 3 op) def2 (nth 4 op) dimline (nth 5 op) rot (nth 6 op)) (if (tblsearch "DIMSTYLE" style) (progn (leaf-ensure-layer layer) (entmakex (list (cons 0 "DIMENSION") (cons 100 "AcDbEntity") (cons 8 layer) (cons 100 "AcDbDimension") (cons 10 dimline) (cons 70 32) (cons 3 style) (cons 210 (list 0.0 0.0 1.0)) (cons 100 "AcDbAlignedDimension") (cons 13 def1) (cons 14 def2) (cons 50 (leaf-deg2rad rot)) (cons 100 "AcDbRotatedDimension"))))))',
    '(defun leaf-apply-adddimaligned (op / layer style def1 def2 dimline) (setq layer (nth 1 op) style (nth 2 op) def1 (nth 3 op) def2 (nth 4 op) dimline (nth 5 op)) (if (tblsearch "DIMSTYLE" style) (progn (leaf-ensure-layer layer) (entmakex (list (cons 0 "DIMENSION") (cons 100 "AcDbEntity") (cons 8 layer) (cons 100 "AcDbDimension") (cons 10 dimline) (cons 70 33) (cons 3 style) (cons 210 (list 0.0 0.0 1.0)) (cons 100 "AcDbAlignedDimension") (cons 13 def1) (cons 14 def2))))))',
)


_MLEADER_LISP_LINES = (
    '(vl-load-com)',
    '(defun leaf-mlstyle (style / d) (if (setq d (dictsearch (namedobjdict) "ACAD_MLEADERSTYLE")) (dictsearch (cdr (assoc -1 d)) style)))',
    '(defun leaf-mltext-p (text / c ok) (setq ok (and (> (strlen text) 0) (<= (strlen text) 256))) (foreach c (vl-string->list text) (if (or (< c 32) (> c 126) (member c (list 37 92 124))) (setq ok nil))) ok)',
    '(defun leaf-addmleader-op (v / layer style p1 p2 text) (if (and (= (length v) 6) (setq layer (nth 1 v) style (nth 2 v) text (nth 5 v)) (<= (strlen layer) 255) (leaf-chars-ok layer "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-$ ") (> (strlen style) 0) (<= (strlen style) 255) (leaf-chars-ok style "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-$ ") (leaf-mlstyle style) (setq p1 (leaf-point3 (nth 3 v))) (setq p2 (leaf-point3 (nth 4 v))) (= (caddr p1) 0.0) (= (caddr p2) 0.0) (not (equal p1 p2 0.000000001)) (leaf-mltext-p text)) (list "ADDMLEADER" layer style p1 p2 text)))',
    '(defun leaf-apply-addmleader (op / layer style p1 p2 text oldlayer oldstyle oldmleaderlayer before result caught layerdata) (setq layer (nth 1 op) style (nth 2 op) p1 (nth 3 op) p2 (nth 4 op) text (nth 5 op) layerdata (tblsearch "LAYER" layer)) (if (and (leaf-mlstyle style) (not (and layerdata (= 1 (logand 1 (cdr (assoc 70 layerdata))))))) (progn (setq oldlayer (getvar "CLAYER") oldstyle (getvar "CMLEADERSTYLE") oldmleaderlayer (getvar "MLEADERLAYER")) (setq caught (vl-catch-all-apply (function (lambda () (leaf-ensure-layer layer) (setvar "CLAYER" layer) (setvar "CMLEADERSTYLE" style) (setvar "MLEADERLAYER" ".") (setq before (entlast)) (setq p1 (trans p1 0 1) p2 (trans p2 0 1)) (command "_.MLEADER" p1 p2 text))) nil)) (setvar "CLAYER" oldlayer) (setvar "CMLEADERSTYLE" oldstyle) (setvar "MLEADERLAYER" oldmleaderlayer) (setq result (entlast)) (if (and (not (vl-catch-all-error-p caught)) result (not (equal result before)) (= (cdr (assoc 0 (entget result))) "MULTILEADER")) result))))',
)


_PROPERTY_LISP_LINES = (
    '(setq leaf-created nil)',
    '(defun leaf-target-p (s / tail) (setq tail (substr s 3)) (cond ((= (substr s 1 2) "H:") (leaf-handle-p tail)) ((= (substr s 1 2) "A:") (and (<= (strlen tail) 6) (leaf-chars-ok tail "0123456789")))))',
    '(defun leaf-target (s / n e) (if (leaf-target-p s) (progn (if (= (substr s 1 2) "H:") (setq e (handent (substr s 3))) (progn (setq n (atoi (substr s 3))) (if (< n (length leaf-created)) (setq e (nth n leaf-created))))) (if (and e (member (cdr (assoc 0 (entget e))) (list "LINE" "LWPOLYLINE" "CIRCLE" "ARC" "INSERT" "DIMENSION"))) e))))',
    '(defun leaf-weight-p (n) (and n (member n (list -3 -2 -1 0 5 9 13 15 18 20 25 30 35 40 50 53 60 70 80 90 100 106 120 140 158 200 211))))',
    '(defun leaf-color-value (s / n) (setq n (leaf-number s)) (if (and n (= n (fix n)) (>= n 0) (<= n 256)) (fix n)))',
    '(defun leaf-weight-value (s / n) (setq n (leaf-number s)) (if (and n (= n (fix n)) (leaf-weight-p (fix n))) (fix n)))',
    '(defun leaf-linetype-value (s) (if (and (> (strlen s) 0) (<= (strlen s) 255) (leaf-chars-ok s "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-$ ") (tblsearch "LTYPE" s)) s))',
    '(defun leaf-property-op (v / tag value) (setq tag (car v)) (if (and (= (length v) 3) (leaf-target-p (nth 1 v))) (progn (setq value (nth 2 v)) (cond ((= tag "SETLINETYPE") (setq value (leaf-linetype-value value))) ((= tag "SETCOLOR") (setq value (leaf-color-value value))) ((= tag "SETLINEWEIGHT") (setq value (leaf-weight-value value)))) (if value (list tag (nth 1 v) value)))))',
    '(defun leaf-apply-setcolor (op / e data out item) (if (setq e (leaf-target (nth 1 op))) (progn (setq data (entget e)) (foreach item data (if (not (member (car item) (list 420 430))) (setq out (cons item out)))) (setq out (reverse out)) (if (assoc 62 out) (setq out (subst (cons 62 (nth 2 op)) (assoc 62 out) out)) (setq out (append out (list (cons 62 (nth 2 op)))))) (if (entmod out) (progn (entupd e) T)))))',
    '(defun leaf-apply-setlinetype (op / e out) (if (and (tblsearch "LTYPE" (nth 2 op)) (setq e (leaf-target (nth 1 op)))) (progn (setq out (entget e)) (if (assoc 6 out) (setq out (subst (cons 6 (nth 2 op)) (assoc 6 out) out)) (setq out (append out (list (cons 6 (nth 2 op)))))) (if (entmod out) (progn (entupd e) T)))))',
    '(defun leaf-apply-setlineweight (op / e out) (if (and (leaf-weight-p (nth 2 op)) (setq e (leaf-target (nth 1 op)))) (progn (setq out (entget e)) (if (assoc 370 out) (setq out (subst (cons 370 (nth 2 op)) (assoc 370 out) out)) (setq out (append out (list (cons 370 (nth 2 op)))))) (if (entmod out) (progn (entupd e) T)))))',
)


_GROUP_LISP_LINES = (
    '(progn (setq leaf-ca (open "created-handles.txt" "w")) (if leaf-ca (close leaf-ca)))',
    '(defun leaf-group-name-p (s / i ok c) (setq i 1 ok (and (> (strlen s) 0) (<= (strlen s) 255) (/= (substr s 1 1) " ") (/= (substr s (strlen s) 1) " "))) (while (and ok (<= i (strlen s))) (setq c (ascii (substr s i 1))) (if (or (< c 32) (> c 126) (member c (list 60 62 47 92 34 58 59 63 42 124 44 61 96))) (setq ok nil)) (setq i (1+ i))) ok)',
    '(defun leaf-group-op (v / members ok m) (if (and (member (car v) (list "ADDGROUP" "REMOVEGROUP")) (leaf-group-name-p (nth 1 v))) (if (= (car v) "REMOVEGROUP") (if (= (length v) 2) v) (if (= (length v) 3) (progn (setq members (leaf-split (nth 2 v) ";") ok (>= (length members) 2)) (foreach m members (if (not (leaf-target-p m)) (setq ok nil))) (if ok (list "ADDGROUP" (nth 1 v) members)))))))',
    '(defun leaf-group-dictionary (create / found e) (setq found (dictsearch (namedobjdict) "ACAD_GROUP")) (if found (cdr (assoc -1 found)) (if create (progn (setq e (entmakex (list (cons 0 "DICTIONARY") (cons 100 "AcDbDictionary") (cons 281 1)))) (if e (dictadd (namedobjdict) "ACAD_GROUP" e))))))',
    '(defun leaf-group-member (s / e d) (if (leaf-target-p s) (progn (setq e (if (= (substr s 1 2) "H:") (handent (substr s 3)) (nth (atoi (substr s 3)) leaf-created))) (if e (progn (setq d (entget e)) (if (and (member (cdr (assoc 0 d)) (list "LINE" "LWPOLYLINE" "CIRCLE" "ARC" "TEXT" "INSERT" "DIMENSION" "POINT" "ELLIPSE")) (= (cdr (assoc 410 d)) "Model")) e))))))',
    '''(defun leaf-addgroup-op (op / members m e ok acad-group group name) (setq ok T name (nth 1 op)) (foreach m (nth 2 op) (setq e (leaf-group-member m)) (if (or (null e) (member e members)) (setq ok nil)) (setq members (append members (list e)))) (if (and ok (leaf-group-name-p name) (>= (length members) 2) (setq acad-group (leaf-group-dictionary T)) (not (dictsearch acad-group name))) (progn (setq group (entmakex (append '((0 . "GROUP") (100 . "AcDbGroup") (300 . "") (70 . 0) (71 . 1)) (mapcar '(lambda (e) (cons 340 e)) members)))) (if group (dictadd acad-group name group)))))''',
    '(defun leaf-removegroup-op (op / acad-group name) (setq acad-group (leaf-group-dictionary nil) name (nth 1 op)) (if (and acad-group (dictsearch acad-group name)) (dictremove acad-group name)))',
    '(defun leaf-record-created (e / fh) (setq fh (open "created-handles.txt" "a")) (if fh (progn (write-line (strcat "CA|" (itoa (length leaf-created)) "|" (cdr (assoc 5 (entget e)))) fh) (close fh) T)))',
)


_BLOCK_DEPENDENCY_LISP_LINES = (
    '(defun leaf-bd-dimension-p (ed / todo seen pair e data kind found steps) (setq todo nil seen nil found nil steps 0) (foreach pair ed (if (and (member (car pair) (list 330 340 350 360)) (= (type (cdr pair)) (quote ENAME))) (setq todo (cons (cdr pair) todo)))) (while (and todo (not found) (< steps 256)) (setq e (car todo) todo (cdr todo) steps (1+ steps)) (if (not (member e seen)) (progn (setq seen (cons e seen) data (entget e) kind (cdr (assoc 0 data))) (if (= kind "DIMENSION") (setq found T)) (if (member kind (list "DIMASSOC" "DICTIONARY" "XRECORD")) (foreach pair data (if (and (member (car pair) (list 330 331 340 350 360)) (= (type (cdr pair)) (quote ENAME))) (setq todo (cons (cdr pair) todo)))))))) (or found todo))',
)

_BLOCK_DEFINITION_LISP_LINES = (
    '(setq leaf-pending-definitions nil)',
    '(setq leaf-pending-children nil)',
    '(defun leaf-bd-name-p (name / ok c) (setq ok (and (> (strlen name) 0) (<= (strlen name) 255) (/= (substr name 1 1) "*"))) (foreach c (vl-string->list name) (if (or (< c 32) (> c 126) (= c 124)) (setq ok nil))) ok)',
    '(defun leaf-bc-index (s) (if (and (<= (strlen s) 6) (leaf-chars-ok s "0123456789")) (atoi s)))',
    '(defun leaf-bc-geometry (v / kind layer op flag) (setq kind (nth 3 v) layer (nth 4 v)) (cond ((and (= kind "LINE") (= (length v) 10)) (leaf-addline-op (list "ADDLINE" layer (nth 5 v) (nth 6 v)))) ((and (= kind "CIRCLE") (= (length v) 10)) (leaf-addcircle-op (list "ADDCIRCLE" layer (nth 5 v) (nth 6 v)))) ((and (= kind "ARC") (= (length v) 12)) (leaf-addarc-op (list "ADDARC" layer (nth 5 v) (nth 6 v) (nth 7 v) (nth 8 v)))) ((and (= kind "LWPOLYLINE") (= (length v) 12) (setq flag (leaf-flag01 (nth 5 v)))) (setq op (if (= flag 1) (leaf-add-op (list "ADD" layer (nth 6 v) (nth 7 v) (nth 8 v))) (leaf-addopen-op (list "ADDOPEN" layer (nth 6 v) (nth 7 v) (nth 8 v))))) (if (and op (equal (nth 2 op) (list 0.0 0.0 1.0) 0.000001)) op))))',
    '(defun leaf-blockchild-op (v / name idx key op aci lt weight n) (setq n (length v)) (if (and (>= n 10) (setq name (nth 1 v)) (leaf-bd-name-p name) (not (assoc (strcase name) leaf-pending-definitions)) (setq idx (leaf-bc-index (nth 2 v))) (not (assoc (setq key (list (strcase name) idx)) leaf-pending-children)) (setq op (leaf-bc-geometry v)) (setq aci (leaf-color-value (nth (- n 3) v))) (setq lt (leaf-linetype-value (nth (- n 2) v))) (setq weight (leaf-weight-value (nth (1- n) v)))) (progn (setq leaf-pending-children (cons (cons key (list op aci lt weight)) leaf-pending-children)) (list "BLOCKCHILD" name idx))))',
    '(defun leaf-bc-complete-p (/ entry key definition ok) (setq ok T) (foreach entry leaf-pending-children (setq key (car entry) definition (assoc (car key) leaf-pending-definitions)) (if (not (and definition (member (strcat "C:" (itoa (cadr key))) (nth 2 definition)))) (setq ok nil))) ok)',
    '(defun leaf-bd-group-p (ed / pair data found) (foreach pair ed (if (and (= (car pair) 330) (= (type (cdr pair)) (quote ENAME))) (progn (setq data (entget (cdr pair))) (if (= (cdr (assoc 0 data)) "GROUP") (setq found T))))) found)',
    '(defun leaf-bd-member-data (h / e ed kind ok pair) (if (and (leaf-handle-p h) (setq e (handent h)) (setq ed (entget e))) (progn (setq kind (cdr (assoc 0 ed)) ok (and (member kind (list "LINE" "LWPOLYLINE" "CIRCLE" "ARC")) (= (cdr (assoc 410 ed)) "Model") (or (null (assoc 210 ed)) (equal (cdr (assoc 210 ed)) (list 0.0 0.0 1.0) 0.000001)) (not (equal (cdr (assoc 62 ed)) 0)) (/= (strcase (cond ((cdr (assoc 6 ed))) (T "BYLAYER"))) "BYBLOCK") (not (equal (cdr (assoc 370 ed)) -2)) (not (leaf-bd-dimension-p ed)) (not (leaf-bd-group-p ed)))) (if (= kind "LWPOLYLINE") (foreach pair ed (if (and (member (car pair) (list 40 41 42 43)) (/= (cdr pair) 0.0)) (setq ok nil)))) (if ok ed))))',
    '(defun leaf-bd-ref (name ref / h idx) (setq h (substr ref 3)) (cond ((and (= (substr ref 1 2) "H:") (or (leaf-bd-member-data h) (and (leaf-handle-p h) (handent h) (= (cdr (assoc 0 (entget (handent h)))) "POLYLINE")))) (strcat "H:" (strcase h))) ((and (= (substr ref 1 2) "C:") (setq idx (leaf-bc-index h)) (assoc (list (strcase name) idx) leaf-pending-children)) (strcat "C:" (itoa idx)))))',
    '(defun leaf-blockdef-op (v / name base raw refs ref value ok) (if (and (= (length v) 4) (setq name (nth 1 v)) (leaf-bd-name-p name) (not (tblsearch "BLOCK" name)) (not (assoc (strcase name) leaf-pending-definitions)) (setq base (leaf-point3 (nth 2 v)))) (progn (setq raw (leaf-split (nth 3 v) ";") ok (and (>= (length raw) 1) (<= (length raw) 60))) (foreach ref raw (setq value (leaf-bd-ref name ref)) (if (and value (not (member value refs))) (setq refs (append refs (list value))) (setq ok nil))) (if ok (progn (setq leaf-pending-definitions (cons (list (strcase name) base refs) leaf-pending-definitions)) (list "ADDBLOCKDEF" name base refs))))))',
    '(defun leaf-bd-clean (ed / out pair depth) (setq depth 0) (foreach pair ed (cond ((= (car pair) 102) (if (= (cdr pair) "}") (setq depth (max 0 (1- depth))) (setq depth (1+ depth)))) ((and (= depth 0) (not (member (car pair) (list -1 5 330 360 350 340 67 410)))) (setq out (cons pair out))))) (reverse out))',
    '(defun leaf-bd-create-child (ed) (entmake ed))',
    '(defun leaf-bc-data (declaration / op tag data p aci lt weight) (setq op (car declaration) aci (cadr declaration) lt (nth 2 declaration) weight (nth 3 declaration) tag (car op)) (setq data (list (cons 0 (cond ((member tag (list "ADD" "ADDOPEN")) "LWPOLYLINE") ((= tag "ADDLINE") "LINE") ((= tag "ADDCIRCLE") "CIRCLE") (T "ARC"))) (cons 100 "AcDbEntity") (cons 8 (nth 1 op)))) (if (/= aci 256) (setq data (append data (list (cons 62 aci))))) (if (/= (strcase lt) "BYLAYER") (setq data (append data (list (cons 6 lt))))) (if (/= weight -1) (setq data (append data (list (cons 370 weight))))) (append data (leaf-bc-shape op)))',
    '(defun leaf-bc-shape (op / tag data p) (setq tag (car op)) (cond ((= tag "ADDLINE") (list (cons 100 "AcDbLine") (cons 10 (nth 2 op)) (cons 11 (nth 3 op)))) ((member tag (list "ADD" "ADDOPEN")) (setq data (list (cons 100 "AcDbPolyline") (cons 90 (length (nth 4 op))) (cons 70 (if (= tag "ADD") 1 0)) (cons 38 (nth 3 op)) (cons 210 (nth 2 op)))) (foreach p (nth 4 op) (setq data (append data (list (cons 10 p))))) data) (T (setq data (list (cons 100 "AcDbCircle") (cons 10 (nth 2 op)) (cons 40 (nth 3 op)) (cons 210 (list 0.0 0.0 1.0)))) (if (= tag "ADDARC") (setq data (append data (list (cons 100 "AcDbArc") (cons 50 (leaf-deg2rad (nth 4 op))) (cons 51 (leaf-deg2rad (nth 5 op))))))) data)))',
    '(defun leaf-bd-ref-data (name ref / ed declaration) (if (= (substr ref 1 2) "H:") (if (setq ed (leaf-bd-member-data (substr ref 3))) (leaf-bd-clean ed)) (if (setq declaration (assoc (list (strcase name) (atoi (substr ref 3))) leaf-pending-children)) (leaf-bc-data (cdr declaration)))))',
    '(defun leaf-bd-entmake (ed / result) (setq result (vl-catch-all-apply (quote entmake) (list ed))) (if (not (vl-catch-all-error-p result)) result))',
    '(defun leaf-bd-apply-child (ed / result) (setq result (vl-catch-all-apply (function (lambda () (leaf-ensure-layer (cdr (assoc 8 ed))) (leaf-bd-create-child ed))) nil)) (if (not (vl-catch-all-error-p result)) result))',
    # Residual: straight classic POLYLINE passes frozen intake validation but fails apply preflight, so VERTEX/SEQEND are never copied header-only.
    '(defun leaf-addblockdef-op (op / name base refs children ed ref ok begun ended) (setq name (nth 1 op) base (nth 2 op) refs (nth 3 op) ok (not (tblsearch "BLOCK" name))) (foreach ref refs (setq ed (leaf-bd-ref-data name ref)) (if (and ed (/= (cdr (assoc 0 ed)) "POLYLINE")) (setq children (append children (list ed))) (setq ok nil))) (if ok (progn (setq begun (leaf-bd-entmake (list (cons 0 "BLOCK") (cons 2 name) (cons 70 0) (cons 10 base) (cons 8 "0"))) ok begun) (foreach ed children (if ok (setq ok (leaf-bd-apply-child ed)))) (if begun (progn (setq ended (leaf-bd-entmake (list (cons 0 "ENDBLK") (cons 8 "0")))) (setq ok (and ok ended)))) (if ok (setq leaf-pending-definitions (vl-remove (assoc (strcase name) leaf-pending-definitions) leaf-pending-definitions))))) ok)',
)


def build_apply_scr_v3() -> str:
    """Extend the frozen interpreter only for the separate v3 Activity."""
    lines = []
    for line in _LISP_LINES:
        if line.startswith("(defun leaf-parse-line "):
            lines.extend(_BLOCK_DEPENDENCY_LISP_LINES)
            lines.extend(_BLOCK_DEFINITION_LISP_LINES)
            lines.extend(s.replace('(tblsearch "BLOCK" name)',
                         '(or (tblsearch "BLOCK" name) (assoc (strcase name) leaf-pending-definitions))')
                         if s.startswith("(defun leaf-addinsert-op ") else s
                         for s in _INSERT_LISP_LINES)
            lines.extend(_DIMENSION_LISP_LINES)
            lines.extend(_MLEADER_LISP_LINES)
            lines.extend(_PROPERTY_LISP_LINES)
            lines.extend(_GROUP_LISP_LINES)
            line = line.replace('(cond ', '(cond ((= (car v) "ADDBLOCKDEF") (leaf-blockdef-op v)) ', 1)
            line = line.replace('(cond ', '(cond ((= (car v) "BLOCKCHILD") (leaf-blockchild-op v)) ', 1)
            line = line.replace('(cond ', '(cond ((member (car v) (list "ADDGROUP" "REMOVEGROUP")) (leaf-group-op v)) ', 1)
            line = line.replace('(cond ', '(cond ((member (car v) (list "SETCOLOR" "SETLINETYPE" "SETLINEWEIGHT")) (leaf-property-op v)) ', 1)
            line = line.replace(
                '((= (car v) "ADDARC")',
                '((= (car v) "ADDMLEADER") (leaf-addmleader-op v)) ((= (car v) "ADDINSERT") (leaf-addinsert-op v)) ((= (car v) "ADDDIMLINEAR") (leaf-adddimlinear-op v)) ((= (car v) "ADDDIMALIGNED") (leaf-adddimaligned-op v)) ((= (car v) "ADDARC")',
                1,
            )
        elif line.startswith("(defun leaf-remove-op "):
            # v3-only: existing dimensions and multileaders may be removed.
            line = line.replace(
                '(list "LWPOLYLINE" "LINE" "CIRCLE" "ARC")',
                '(append (list "LWPOLYLINE" "LINE" "CIRCLE" "ARC" "DIMENSION") (list "MULTILEADER"))',
                1,
            )
        elif line.startswith("(defun leaf-apply "):
            # Capture entmakex results only while applying adds, never while
            # parsing the plan (A: targets do not exist during that pass).
            line = line.replace('(defun leaf-apply (op)', '(defun leaf-apply-one (op)', 1)
            line = line.replace('(cond ', '(cond ((= (car op) "ADDBLOCKDEF") (leaf-addblockdef-op op)) ', 1)
            line = line.replace('(cond ', '(cond ((= (car op) "ADDGROUP") (leaf-addgroup-op op)) ((= (car op) "REMOVEGROUP") (leaf-removegroup-op op)) ', 1)
            line = line.replace('(cond ', '(cond ((= (car op) "SETCOLOR") (leaf-apply-setcolor op)) ((= (car op) "SETLINETYPE") (leaf-apply-setlinetype op)) ((= (car op) "SETLINEWEIGHT") (leaf-apply-setlineweight op)) ', 1)
            line = line.replace(
                '((= (car op) "ADDARC")',
                '((= (car op) "ADDMLEADER") (leaf-apply-addmleader op)) ((= (car op) "ADDINSERT") (leaf-apply-addinsert op)) ((= (car op) "ADDDIMLINEAR") (leaf-apply-adddimlinear op)) ((= (car op) "ADDDIMALIGNED") (leaf-apply-adddimaligned op)) ((= (car op) "ADDARC")',
                1,
            )
            lines.append(line)
            line = '(defun leaf-apply (op / result) (setq result (leaf-apply-one op)) (if (and result (member (car op) (list "ADD" "ADDOPEN" "ADDLINE" "ADDCIRCLE" "ADDARC" "ADDINSERT" "ADDDIMLINEAR" "ADDDIMALIGNED" "ADDMLEADER"))) (if (leaf-record-created result) (setq leaf-created (append leaf-created (list result))) (setq result nil))) result)'
        elif line == '(progn (setq leaf-apply-ok T) (command "_.UNDO" "_Begin"))':
            line = '(progn (command "_.UNDO" "_Mark") (setq leaf-apply-ok T))'
        elif line.startswith('(foreach leaf-op leaf-ops '):
            line = '(foreach leaf-op leaf-ops (if (and leaf-apply-ok (not (leaf-apply leaf-op))) (progn (setq leaf-apply-ok nil) (command "_.UNDO" "_Back") (princ "LEAF-MUTATION-APPLY-FAILED"))))'
        elif line == '(command "_.UNDO" "_End")':
            continue
        elif line.startswith("(defun leaf-read-plan "):
            line = line.replace('(setq fh (open path "r")', '(setq leaf-pending-definitions nil leaf-pending-children nil) (setq fh (open path "r")', 1)
            line = line.replace('(setq ops (cons op ops))', '(if (/= (car op) "BLOCKCHILD") (setq ops (cons op ops)))', 1)
            line = line.replace('(if (and ok ops)', '(if (and ok ops (leaf-bc-complete-p))', 1)
            line = line.replace(
                '(list "LEAF_MUTATION_PLAN|1" "LEAF_MUTATION_PLAN|2")',
                '(list "LEAF_MUTATION_PLAN|1" "LEAF_MUTATION_PLAN|2" "LEAF_MUTATION_PLAN|3")',
                1,
            )
        lines.append(line)
    from lisp import MAX_SCRIPT_LINE_CHARS
    if any(len(line) > MAX_SCRIPT_LINE_CHARS for line in lines):
        raise ValueError("apply script line exceeds MAX_SCRIPT_LINE_CHARS")
    return "\r\n".join(lines) + "\r\n"
