; ============================================================================
; count_by_layer.lsp  -  engine_op: count_by_layer   (capability: drawing.read)
; ----------------------------------------------------------------------------
; DA-runnable tool. Counts every model-space entity per layer and writes a
; Result envelope (CONTRACT.md section 3) to result.json in the working dir.
; The DA Activity runs it via a script:  (load "count_by_layer.lsp")
; Pure-python oracle: engine/selfcheck.py -> mock_count_by_layer().
; ============================================================================
(defun leaf-json-str (s) (strcat "\"" s "\""))

(defun c:LEAFTOOL ( / t0 ss nn i ed layn pair lst f entry first res dt)
  (setq t0 (getvar "MILLISECS"))
  (setq lst nil)
  (setq ss (ssget "_X" (list (cons 410 "Model"))))
  (if ss
    (progn
      (setq nn (sslength ss) i 0)
      (while (< i nn)
        (setq ed (entget (ssname ss i)) layn (cdr (assoc 8 ed)))
        (setq pair (assoc layn lst))
        (if pair
          (setq lst (subst (cons layn (1+ (cdr pair))) pair lst))
          (setq lst (cons (cons layn 1) lst)))
        (setq i (1+ i)))))
  ; build result.counts object
  (setq res "" first T)
  (foreach p lst
    (setq res (strcat res (if first "" ",") (leaf-json-str (car p)) ":" (itoa (cdr p))))
    (setq first nil))
  (setq dt (- (getvar "MILLISECS") t0))
  (setq f (open "result.json" "w"))
  (write-line
    (strcat
      "{\"ok\":true,\"tool\":\"count-by-layer\",\"version\":\"1.0.0\","
      "\"result\":{\"counts\":{" res "}},"
      "\"overlay\":null,"
      "\"timing_ms\":" (itoa (fix dt)) ","
      "\"cost\":null,\"error\":null}")
    f)
  (close f)
  (princ "LEAFTOOL-DONE")
  (princ))

; auto-run when loaded headlessly under DA
(c:LEAFTOOL)
