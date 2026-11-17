"""Dependency dispositions for the existing local DXF reader.

This consumes decoded records only. It never follows external paths and does
not claim that a DWG converter preserved every original metadata record.
"""
from __future__ import annotations

import re


_INLINE_FONT = re.compile(r"\\[fF]")
_FONT_DELIMITER = re.compile(r"[|;]")


def _inline_fonts(text):
    position = 0
    while control := _INLINE_FONT.search(text, position):
        delimiter = _FONT_DELIMITER.search(text, control.end())
        if delimiter is None:
            break
        name = text[control.end():delimiter.start()]
        if name and (delimiter.group() == "|" or name.lower().endswith((".shx", ".ttf", ".otf"))):
            yield name
        # Consume the entire control once, including a literal path. An
        # unterminated suffix is searched once rather than once per prefix.
        position = delimiter.end()


def from_pairs(pairs, *, literal_text=None):
    dependencies = {}
    # Full drawings require owning sections. Bare decoded-record fragments keep
    # their existing API behavior, but ENDSEC/EOF still close their scope.
    section = None if any(c == 0 and v.upper() == "SECTION" for c, v in pairs) else "__FRAGMENT__"
    owners = {"BLOCK": {"BLOCKS"}, "STYLE": {"TABLES"},
              "IMAGEDEF": {"OBJECTS"}, "PDFDEFINITION": {"OBJECTS"},
              "DWFDEFINITION": {"OBJECTS"}, "DGNDEFINITION": {"OBJECTS"},
              "ACAD_PROXY_ENTITY": {"ENTITIES", "BLOCKS"}, "PROXY_ENTITY": {"ENTITIES", "BLOCKS"},
              "ACAD_PROXY_OBJECT": {"OBJECTS"}, "PROXY_OBJECT": {"OBJECTS"}}
    inline_entities = {"MTEXT", "MULTILEADER", "TABLE", "ACAD_TABLE"}
    owners.update({kind: {"ENTITIES", "BLOCKS"} for kind in inline_entities})

    def add(kind, name, disposition, **details):
        if name:
            dependencies.setdefault((kind, name), {
                "kind": kind, "name": name, "disposition": disposition, **details})

    i = 0
    while i < len(pairs):
        code, kind = pairs[i]
        if code != 0:
            i += 1
            continue
        end = i + 1
        groups = {}
        text_parts = []
        inline_record = kind.upper() in inline_entities
        while end < len(pairs) and pairs[end][0] != 0:
            groups.setdefault(*pairs[end])
            if inline_record and pairs[end][0] in (1, 3, 304):
                value = pairs[end][1] if literal_text is None else literal_text.get(end, pairs[end][1])
                text_parts.append(value)
            end += 1
        kind = kind.upper()
        if kind == "EOF":
            break
        if kind == "SECTION":
            section = groups.get(2, "").upper()
            i = end
            continue
        if kind == "ENDSEC":
            section = None
            i = end
            continue
        if section != "__FRAGMENT__" and section not in owners.get(kind, set()):
            i = end
            continue
        if kind == "BLOCK":
            try:
                external = int(groups.get(70, "0")) & 12
            except ValueError:
                external = False
            if external:
                add("xref", groups.get(1) or groups.get(2), "detached",
                    reason="External drawings are not followed by the local reader.")
        elif kind == "STYLE":
            try:
                shape = bool(int(groups.get(70, "0")) & 1)
            except ValueError:
                shape = False
            for code in (3, 4):
                name = groups.get(code, "")
                font_kind = "shape_font" if shape or code == 4 else "font"
                add(font_kind, name, "font remapped", remapped_to="sans-serif")
        elif kind == "IMAGEDEF":
            add("image", groups.get(1) or groups.get(5), "image omitted")
        elif kind in ("PDFDEFINITION", "DWFDEFINITION", "DGNDEFINITION"):
            add("underlay", groups.get(1) or groups.get(2) or groups.get(5), "underlay omitted")
        elif kind in ("ACAD_PROXY_ENTITY", "ACAD_PROXY_OBJECT", "PROXY_ENTITY", "PROXY_OBJECT"):
            add("proxy", groups.get(5) or groups.get(1) or kind, "proxy omitted")
        elif kind in inline_entities:
            # Font controls can span repeated DXF text chunks. Read them in
            # record order; a first-value map would lose split font names.
            for name in _inline_fonts("".join(text_parts)):
                add("font", name, "font remapped", remapped_to="sans-serif")
        i = end
    return {"schema": "leaf.drawing-dependencies.v1",
            "scope": "local_dxf_records", "external_files_opened": False,
            "dependencies": [dependencies[key] for key in sorted(dependencies)],
            "coverage_note": "Lists dependency records preserved in the local DXF conversion; raw DWG metadata coverage requires an independent inventory."}
