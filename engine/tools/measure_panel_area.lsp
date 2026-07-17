; ============================================================================
; measure_panel_area.lsp  -  engine_op: measure_panel_area  (cap: drawing.read)
; ----------------------------------------------------------------------------
; DA-runnable tool. Sums the enclosed area of every CLOSED LWPOLYLINE, total and
; per-layer, and writes a Result envelope (CONTRACT.md section 3) to result.json.
;
; UNITS ASSUMPTION (noted in the envelope result): 1 drawing unit = 1 inch, so
; square feet = area_in2 / 144. On the golden sample a panel is 77 x 38.5 units
; = 20.57 sqft, which is a real module footprint -> confirms inches.
;
; area_in2 comes from AutoCAD's own (vlax-curve-getArea) when available, else the
; shoelace formula over the DXF-10 vertices (same math as the python oracle).
; Pure-python oracle: engine/selfcheck.py -> mock_measure_panel_area().
; ============================================================================
(vl-load-com)

(defun leaf-shoelace (pts / n i a x1 y1 x2 y2)
  (setq n (length pts) i 0 a 0.0)
  (while (< i n)
    (setq x1 (car (nth i pts)) y1 (cadr (nth i pts)))
    (setq x2 (car (nth (rem (1+ i) n) pts)) y2 (cadr (nth (rem (1+ i) n) pts)))
    (setq a (+ a (- (* x1 y2) (* x2 y1))))
    (setq i (1+ i)))
  (/ (abs a) 2.0))

(defun c:LEAFTOOL ( / t0 ss nn i en ed layn cl pts g a2 lst pair total res first f dt)
  (setq t0 (getvar "MILLISECS"))
  (setq lst nil total 0.0)
  (setq ss (ssget "_X" (list (cons 0 "LWPOLYLINE") (cons 410 "Model"))))
  (if ss
    (progn
      (setq nn (sslength ss) i 0)
      (while (< i nn)
        (setq en (ssname ss i) ed (entget en) layn (cdr (assoc 8 ed)) cl (cdr (assoc 70 ed)))
        (if (and cl (= 1 (logand cl 1)))       ; closed flag
          (progn
            (setq a2 (vl-catch-all-apply 'vlax-curve-getArea (list en)))
            (if (vl-catch-all-error-p a2)
              (progn                            ; fallback: shoelace over vertices
                (setq pts nil)
                (foreach g ed (if (= 10 (car g)) (setq pts (cons (cdr g) pts))))
                (setq a2 (leaf-shoelace (reverse pts)))))
            (setq total (+ total a2))
            (setq pair (assoc layn lst))
            (if pair
              (setq lst (subst (cons layn (+ (cdr pair) a2)) pair lst))
              (setq lst (cons (cons layn a2) lst)))))
        (setq i (1+ i)))))
  ; per-layer sqft object
  (setq res "" first T)
  (foreach p lst
    (setq res (strcat res (if first "" ",") "\"" (car p) "\":" (rtos (/ (cdr p) 144.0) 2 3)))
    (setq first nil))
  (setq dt (- (getvar "MILLISECS") t0))
  (setq f (open "result.json" "w"))
  (write-line
    (strcat
      "{\"ok\":true,\"tool\":\"measure-panel-area\",\"version\":\"1.0.0\","
      "\"result\":{\"area_sqft\":" (rtos (/ total 144.0) 2 3) ","
      "\"by_layer_sqft\":{" res "},"
      "\"units_assumption\":\"1 drawing unit = 1 inch; sqft = in2 / 144\"},"
      "\"overlay\":null,"
      "\"timing_ms\":" (itoa (fix dt)) ","
      "\"cost\":null,\"error\":null}")
    f)
  (close f)
  (princ "LEAFTOOL-DONE")
  (princ))

(c:LEAFTOOL)
