"""Bound JSON container nesting before recursive decoding."""


def within_depth_budget(raw, maximum):
    # Structural characters are ASCII even when UTF-8 strings contain multibyte text.
    depth = 0
    in_string = escaped = False
    for byte in raw:
        if in_string:
            if escaped:
                escaped = False
            elif byte == ord("\\"):
                escaped = True
            elif byte == ord('"'):
                in_string = False
        elif byte == ord('"'):
            in_string = True
        elif byte in b"[{":
            depth += 1
            if depth > maximum:
                return False
        elif byte in b"]}":
            depth -= 1
    return True
