; ============================================================================
; highlight_panels_near_edge.lsp
;   engine_op: highlight_panels_near_edge   (capability: drawing.read)
; ----------------------------------------------------------------------------
; DA-runnable tool. Params (layer, distance) arrive as a params.json input file
; the Activity stages next to the drawing:
;     {"layer": "Panels", "distance": 200}
; Computes the drawing extents (bbox over all model-space LWPOLYLINEs), then for
; each closed polyline on <layer> whose CENTROID is within <distance> of any of
; the four extents edges, emits its handle into overlay.highlight_handles.
; Writes a Result envelope (CONTRACT.md section 3) to result.json.
;
; distance is in drawing units (inches on the golden sample; default 200in).
; Pure-python oracle: engine/selfcheck.py -> mock_highlight_panels_near_edge().
; ============================================================================
(vl-load-com)

; --- tiny params.json reader: pulls "layer" (string) and "distance" (number) ---
(defun leaf-read-params ( / f line txt p q lay dist)
  (setq lay "Panels" dist 200.0)
  (if (findfile "params.json")
    (progn
      (setq f (open "params.json" "r") txt "")
      (while (setq line (read-line f)) (setq txt (strcat txt line)))
      (close f)
      ; layer
      (setq p (vl-string-search "\"layer\"" txt))
      (if p (progn
        (setq q (vl-string-search "\"" txt (+ p 8)))       ; opening quote of value
        (if q (progn
          (setq q (1+ q))
          (setq p (vl-string-search "\"" txt q))
          (if p (setq lay (substr txt (1+ q) (- p q))))))))
      ; distance
      (setq p (vl-string-search "\"distance\"" txt))
      (if p (progn
        (setq p (+ p 10))
        (while (and (< p (strlen txt))
                    (member (substr txt (1+ p) 1) '(" " ":" "\t")))
          (setq p (1+ p)))
        (setq q p)
        (while (and (< q (strlen txt))
                    (not (member (substr txt (1+ q) 1) '("," "}" " " "\t"))))
          (setq q (1+ q)))
        (setq dist (atof (substr txt (1+ p) (- q p))))))))
  (list lay dist))

(defun c:LEAFTOOL ( / t0 prm lay dist ss nn i en ed layn cl pts g cx cy npt
                     minx miny maxx maxy d handles first res f dt)
  (setq t0 (getvar "MILLISECS"))
  (setq prm (leaf-read-params) lay (car prm) dist (cadr prm))
  ; --- pass 1: overall extents over ALL polylines ---
  (setq minx nil)
  (setq ss (ssget "_X" (list (cons 0 "LWPOLYLINE") (cons 410 "Model"))))
  (if ss
    (progn
      (setq nn (sslength ss) i 0)
      (while (< i nn)
        (setq ed (entget (ssname ss i)))
        (foreach g ed
          (if (= 10 (car g))
            (progn
              (setq cx (cadr g) cy (caddr g))
              (if (null minx) (setq minx cx maxx cx miny cy maxy cy)
                (progn
                  (if (< cx minx) (setq minx cx)) (if (> cx maxx) (setq maxx cx))
                  (if (< cy miny) (setq miny cy)) (if (> cy maxy) (setq maxy cy)))))))
        (setq i (1+ i)))))
  ; --- pass 2: centroid distance to nearest extents edge ---
  (setq handles nil)
  (if (and ss minx)
    (progn
      (setq nn (sslength ss) i 0)
      (while (< i nn)
        (setq en (ssname ss i) ed (entget en) layn (cdr (assoc 8 ed)) cl (cdr (assoc 70 ed)))
        (if (and (= layn lay) cl (= 1 (logand cl 1)))
          (progn
            (setq cx 0.0 cy 0.0 npt 0)
            (foreach g ed
              (if (= 10 (car g))
                (setq cx (+ cx (cadr g)) cy (+ cy (caddr g)) npt (1+ npt))))
            (if (> npt 0)
              (progn
                (setq cx (/ cx npt) cy (/ cy npt))
                (setq d (min (- cx minx) (- maxx cx) (- cy miny) (- maxy cy)))
                (if (<= d dist)
                  (setq handles (cons (cdr (assoc 5 ed)) handles)))))))
        (setq i (1+ i)))))
  ; --- build overlay.highlight_handles array ---
  (setq res "" first T)
  (foreach h (reverse handles)
    (setq res (strcat res (if first "" ",") "\"" h "\"") first nil))
  (setq dt (- (getvar "MILLISECS") t0))
  (setq f (open "result.json" "w"))
  (write-line
    (strcat
      "{\"ok\":true,\"tool\":\"highlight-panels-near-edge\",\"version\":\"1.0.0\","
      "\"result\":{\"layer\":\"" lay "\",\"distance\":" (rtos dist 2 3) ","
      "\"matched\":" (itoa (length handles)) ","
      "\"extents\":{\"min\":[" (rtos minx 2 3) "," (rtos miny 2 3) "],"
      "\"max\":[" (rtos maxx 2 3) "," (rtos maxy 2 3) "]}},"
      "\"overlay\":{\"highlight_handles\":[" res "]},"
      "\"timing_ms\":" (itoa (fix dt)) ","
      "\"cost\":null,\"error\":null}")
    f)
  (close f)
  (princ "LEAFTOOL-DONE")
  (princ))

(c:LEAFTOOL)
