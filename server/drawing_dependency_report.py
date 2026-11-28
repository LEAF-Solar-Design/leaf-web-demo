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
    plot_setups = {}
    plot_layouts = []
    # Full drawings require owning sections. Bare decoded-record fragments keep
    # their existing API behavior, but ENDSEC/EOF still close their scope.
    section = None if any(c == 0 and v.upper() == "SECTION" for c, v in pairs) else "__FRAGMENT__"
    owners = {"BLOCK": {"BLOCKS"}, "STYLE": {"TABLES"},
              "IMAGEDEF": {"OBJECTS"}, "PDFDEFINITION": {"OBJECTS"},
              "DWFDEFINITION": {"OBJECTS"}, "DGNDEFINITION": {"OBJECTS"},
              "PLOTSETTINGS": {"OBJECTS"}, "LAYOUT": {"OBJECTS"},
              "ACAD_PROXY_ENTITY": {"ENTITIES", "BLOCKS"}, "PROXY_ENTITY": {"ENTITIES", "BLOCKS"},
              "ACAD_PROXY_OBJECT": {"OBJECTS"}, "PROXY_OBJECT": {"OBJECTS"}}
    inline_entities = {"MTEXT", "MULTILEADER", "TABLE", "ACAD_TABLE"}
    owners.update({kind: {"ENTITIES", "BLOCKS"} for kind in inline_entities | {"TOLERANCE"}})

    def add(kind, name, disposition, **details):
        if name:
            dependencies.setdefault((kind, name), {
                "kind": kind, "name": name, "disposition": disposition, **details})

    def plot_resources(groups, *, layout=False):
        device = groups.get(2) or None
        resources = {"plot_device": None if layout and device == "None" else device,
                     "plot_style": groups.get(7) or None}
        for kind, name in resources.items():
            add(kind, name, "reported")
        return resources

    i = 0
    while i < len(pairs):
        code, kind = pairs[i]
        if code != 0:
            i += 1
            continue
        kind = kind.upper()
        end = i + 1
        groups = {}
        subclasses = {}
        subclass = None
        text_parts = []
        plot_record = kind in {"LAYOUT", "PLOTSETTINGS"}
        inline_record = kind in inline_entities
        while end < len(pairs) and pairs[end][0] != 0:
            tag, value = pairs[end]
            groups.setdefault(tag, value)
            if plot_record:
                if tag == 100:
                    subclass = subclasses.setdefault(value, {})
                elif subclass is not None:
                    subclass.setdefault(tag, value)
            if inline_record and tag in (1, 3, 304):
                text_parts.append(value if literal_text is None else literal_text.get(end, value))
            end += 1
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
        elif kind == "PLOTSETTINGS":
            settings = subclasses.get("AcDbPlotSettings")
            resources = plot_resources(groups if settings is None else settings)
            if section == "OBJECTS" and settings and settings.get(1):
                plot_setups.setdefault(settings[1].casefold(), []).append(resources)
        elif kind == "LAYOUT" and section == "OBJECTS":
            layout = subclasses.get("AcDbLayout", {}).get(1)
            settings = subclasses.get("AcDbPlotSettings")
            if layout and layout.casefold() != "model" and settings is not None:
                plot_layouts.append({"layout": layout, "page_setup": settings.get(1) or None,
                                     "layout_resources": plot_resources(settings, layout=True)})
        elif kind in ("PDFDEFINITION", "DWFDEFINITION", "DGNDEFINITION"):
            add("underlay", groups.get(1) or groups.get(2) or groups.get(5), "underlay omitted")
        elif kind in ("ACAD_PROXY_ENTITY", "ACAD_PROXY_OBJECT", "PROXY_ENTITY", "PROXY_OBJECT"):
            add("proxy", groups.get(5) or groups.get(1) or kind, "proxy omitted")
        elif kind in inline_entities or kind == "TOLERANCE":
            # Font controls can span repeated DXF text chunks. Read them in
            # record order; a first-value map would lose split font names.
            # A tolerance frame stores its content in group 1 and its dimension style in group 3.
            text = groups.get(1, "") if kind == "TOLERANCE" else "".join(text_parts)
            for name in _inline_fonts(text):
                add("font", name, "font remapped", remapped_to="sans-serif")
        i = end
    report = {"schema": "leaf.drawing-dependencies.v1",
            "scope": "local_dxf_records", "external_files_opened": False,
            "dependencies": [dependencies[key] for key in sorted(dependencies)],
            "coverage_note": "Lists dependency records preserved in the local DXF conversion; raw DWG metadata coverage requires an independent inventory."}
    # A layout stores its own plot values. The setup name links reported
    # records and does not establish runtime inheritance or printer availability.
    for binding in plot_layouts:
        name = binding["page_setup"]
        matches = plot_setups.get(name.casefold(), []) if name else []
        if len(matches) == 1:
            status = "matched"
        elif matches:
            status = "ambiguous"
        else:
            status = "unresolved" if name else "direct"
        binding["setup_status"] = status
        binding["setup_resources"] = matches[0] if status == "matched" else None
    if plot_layouts:
        report["plot_layout_bindings"] = plot_layouts
    return report
