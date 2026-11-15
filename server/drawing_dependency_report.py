"""Dependency dispositions for the existing local DXF reader.

This consumes decoded records only. It never follows external paths and does
not claim that a DWG converter preserved every original metadata record.
"""
from __future__ import annotations


def from_pairs(pairs):
    dependencies = {}

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
        while end < len(pairs) and pairs[end][0] != 0:
            groups.setdefault(*pairs[end])
            end += 1
        kind = kind.upper()
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
        i = end
    return {"schema": "leaf.drawing-dependencies.v1",
            "scope": "local_dxf_records", "external_files_opened": False,
            "dependencies": [dependencies[key] for key in sorted(dependencies)],
            "coverage_note": "Lists dependency records preserved in the local DXF conversion; raw DWG metadata coverage requires an independent inventory."}
