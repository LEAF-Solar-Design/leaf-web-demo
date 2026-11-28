"""Dependency dispositions for the existing local DXF reader.

This consumes decoded records only. It never follows external paths and does
not claim that a DWG converter preserved every original metadata record.
"""
from __future__ import annotations

import re


_INLINE_FONT = re.compile(r"\\[fF]")
_FONT_DELIMITER = re.compile(r"[|;]")
_LINE_ENTITIES = {"LINE", "LWPOLYLINE", "POLYLINE"}
_DEFAULT_LINETYPES = {"bylayer", "byblock", "continuous"}
_MATERIAL_MAPS = {
    3: ("diffuse", 72),
    4: ("specular", 77),
    6: ("reflection", 171),
    7: ("opacity", 175),
    8: ("bump", 179),
    9: ("refraction", 273),
}


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


def _material_texture_maps(pairs, start, end):
    in_material = False
    normal_map = False
    saw_diffuse_path = False
    previous = (None, None)
    for index in range(start, end):
        code, value = pairs[index]
        if code == 100:
            in_material = value == "AcDbMaterial"
        elif in_material and code in (271, 465):
            normal_map = True
        elif in_material and code in _MATERIAL_MAPS:
            channel, source_code = _MATERIAL_MAPS[code]
            if code == 3:
                # The normal map repeats the diffuse groups after its own header.
                if normal_map or saw_diffuse_path:
                    channel = "normal"
                saw_diffuse_path = True
            if value and (previous[0] != source_code or previous[1] == "1"):
                yield channel, value
        previous = code, value


def _light_web_path(pairs, start, end):
    boolean_count = None
    for index in range(start, end):
        code, value = pairs[index]
        if code == 100:
            boolean_count = 0 if value == "AcDbLight" else None
        elif code == 290 and boolean_count is not None:
            boolean_count += 1
            # AcDbLight writes status first, then the photometric web flag.
            if boolean_count == 2:
                if value == "1" and index + 1 < end and pairs[index + 1][0] == 300:
                    return pairs[index + 1][1]
                return None
    return None


def _dxf_int(value):
    try:
        return int(value)
    except ValueError:
        return None


def _linetype_shape_elements(pairs, start, end):
    # Group 49 starts each pattern element. A group 340 belongs only to
    # that element, even when a text or another shape element follows it.
    element_index = -1
    flags = 0
    shape_number = None
    handles = {}
    for index in range(start, end + 1):
        code, value = pairs[index] if index < end else (49, "")
        if code == 49:
            if element_index >= 0 and flags & 4:
                yield {"element_index": element_index, "shape_number": shape_number,
                       "style_handles": list(handles)}
            element_index += 1
            flags = 0
            shape_number = None
            handles = {}
        elif code == 74:
            flags = _dxf_int(value) or 0
        elif code == 75:
            shape_number = _dxf_int(value)
        elif code == 340 and value:
            handles[value.upper()] = None


def _resolve_shape_link(link, styles):
    handles = link["style_handles"]
    if not handles:
        return "missing_pointer", None
    if len(handles) > 1:
        return "ambiguous", None
    matches = styles.get(handles[0], [])
    if not matches:
        return "dangling", None
    if len(matches) > 1:
        return "ambiguous", None
    if not matches[0]:
        return "invalid_style", None
    return "resolved", matches[0]


def from_pairs(pairs, *, literal_text=None):
    dependencies = {}
    texture_maps = {}
    web_lights = {}
    plot_setups = {}
    plot_resource_setups = {}
    plot_layouts = []
    linetype_shape_links = []
    linetype_names = set()
    line_linetypes = {}
    styles = {}
    # Full drawings require owning sections. Bare decoded-record fragments keep
    # their existing API behavior, but ENDSEC/EOF still close their scope.
    section = None if any(c == 0 and v.upper() == "SECTION" for c, v in pairs) else "__FRAGMENT__"
    owners = {"BLOCK": {"BLOCKS"}, "STYLE": {"TABLES"}, "LTYPE": {"TABLES"},
              "IMAGEDEF": {"OBJECTS"}, "MATERIAL": {"OBJECTS"}, "PDFDEFINITION": {"OBJECTS"},
              "DWFDEFINITION": {"OBJECTS"}, "DGNDEFINITION": {"OBJECTS"},
              "PLOTSETTINGS": {"OBJECTS"}, "LAYOUT": {"OBJECTS"},
              "ACAD_PROXY_ENTITY": {"ENTITIES", "BLOCKS"}, "PROXY_ENTITY": {"ENTITIES", "BLOCKS"},
              "ACAD_PROXY_OBJECT": {"OBJECTS"}, "PROXY_OBJECT": {"OBJECTS"}}
    inline_entities = {"MTEXT", "MULTILEADER", "TABLE", "ACAD_TABLE"}
    owners.update({kind: {"ENTITIES", "BLOCKS"} for kind in inline_entities | _LINE_ENTITIES | {"TOLERANCE", "LIGHT"}})

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
            shape = bool((_dxf_int(groups.get(70, "0")) or 0) & 1)
            if handle := groups.get(5):
                styles.setdefault(handle.upper(), []).append(groups.get(3) if shape else None)
            for code in (3, 4):
                name = groups.get(code, "")
                font_kind = "shape_font" if shape or code == 4 else "font"
                add(font_kind, name, "font remapped", remapped_to="sans-serif")
        elif kind == "LTYPE":
            if name := groups.get(2):
                linetype_names.add(name.casefold())
            for element in _linetype_shape_elements(pairs, i + 1, end):
                linetype_shape_links.append({"linetype": groups.get(2) or None,
                                             "linetype_handle": groups.get(5) or None,
                                             **element})
        elif kind in _LINE_ENTITIES:
            if name := groups.get(6):
                line_linetypes.setdefault(name, []).append({
                    "entity_type": kind, "handle": groups.get(5) or None,
                })
        elif kind == "IMAGEDEF":
            add("image", groups.get(1) or groups.get(5), "image omitted")
        elif kind == "MATERIAL":
            handle = groups.get(5) or ""
            for channel, path in _material_texture_maps(pairs, i + 1, end):
                add("material_texture", path, "texture omitted")
                texture_maps.setdefault(path, set()).add((handle, channel))
        elif kind == "LIGHT":
            path = _light_web_path(pairs, i + 1, end)
            if path:
                add("photometric_web", path, "reported")
                handles = web_lights.setdefault(path, set())
                if handle := groups.get(5):
                    handles.add(handle)
        elif kind == "PLOTSETTINGS":
            settings = subclasses.get("AcDbPlotSettings")
            resources = plot_resources(groups if settings is None else settings)
            if section == "OBJECTS" and settings and settings.get(1):
                plot_setups.setdefault(settings[1].casefold(), []).append(resources)
                for resource_kind, name in resources.items():
                    if name:
                        plot_resource_setups.setdefault((resource_kind, name), set()).add(settings[1])
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
    for name, references in line_linetypes.items():
        normalized = name.casefold()
        if normalized not in linetype_names and normalized not in _DEFAULT_LINETYPES:
            add("linetype", name, "missing definition", references=references)
    for path, references in texture_maps.items():
        dependencies["material_texture", path]["material_maps"] = [
            {"material_handle": handle or None, "channel": channel}
            for handle, channel in sorted(references)
        ]
    for path, handles in web_lights.items():
        dependencies["photometric_web", path]["light_handles"] = sorted(handles)
    for key, setup_names in plot_resource_setups.items():
        dependencies[key]["page_setups"] = sorted(setup_names)
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
    if linetype_shape_links:
        for link in linetype_shape_links:
            link["status"], link["font"] = _resolve_shape_link(link, styles)
        report["linetype_shape_links"] = linetype_shape_links
    return report
