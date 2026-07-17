"""Built-in tool files (the FILE is the tool).

Each module exposes `run(intake, params) -> (result, overlay)` and IS the
implementation the dynamic loader executes for the corresponding registry
built-in. The count/measure/highlight built-ins delegate to the shared
pure-python primitives in `tools_fallback` so their §3 result/overlay stay
byte-identical to the frozen baseline; `list_layers` is a self-contained
reference implementation of a built-in outside the original 3-op family.
"""
