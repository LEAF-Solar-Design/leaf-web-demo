"""Dependency dispositions for the existing local DXF reader.

This consumes decoded records only. It never follows external paths and does
not claim that a DWG converter preserved every original metadata record.
"""
from __future__ import annotations

import re
from collections import Counter


_INLINE_FONT = re.compile(r"\\[fF]")
_FONT_DELIMITER = re.compile(r"[|;]")
_LINE_ENTITIES = {"LINE", "LWPOLYLINE", "POLYLINE"}
_PANEL_OUTLINES = {"LWPOLYLINE", "POLYLINE"}
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
    match = matches[0]
    if not match["shape_file"] or not match["font"]:
        return "invalid_style", None
    return "resolved", match["font"]


def _note_style_link(pairs, start, end, groups):
    # The entity-level text style follows the nested MULTILEADER context.
    after_context = False
    content = None
    handles = {}
    for index in range(start, end):
        code, value = pairs[index]
        if (code, value) == (301, "}"):
            after_context = True
        elif after_context and code == 172 and content is None:
            content = _dxf_int(value)
        elif after_context and code == 343 and value and value != "0":
            handles[value.upper()] = None
    if content != 2 or not groups.get(5):
        return None
    layout = groups.get(410)
    if not layout and groups.get(67, "0") == "0":
        layout = "Model"
    return {
        "note_handle": groups[5],
        "layer": groups.get(8) or "0",
        "layout": layout,
        "style_handles": list(handles),
    }


def _resolve_note_style(link, styles):
    handles = link["style_handles"]
    matches = [row for handle in handles for row in styles.get(handle, [])]
    if len(handles) > 1 or len(matches) > 1:
        status = "ambiguous"
    elif not matches:
        status = "missing_style"
    elif matches[0]["shape_file"] or not matches[0]["font"]:
        status = "missing_font"
    else:
        status = "resolved"
    return {"status": status, "definitions": matches}


def _table_style_fields(pairs, start, end):
    subclass = None
    handles = {}
    for index in range(start, end):
        code, value = pairs[index]
        if code == 100:
            subclass = value
        elif subclass == "AcDbTable":
            if code == 171:
                break
            if code == 342 and value and value != "0":
                handles[value.upper()] = None
    return list(handles)


def _table_style_cycles(tables, styles):
    usage = Counter(table["table_handle"] for table in tables)
    edges = {}
    for table in tables:
        handle, targets = table["table_handle"], table["style_handles"]
        if usage[handle] == 1 and handle not in styles and len(targets) == 1:
            edges[handle] = targets[0]
    cycles = set()
    visited = set()
    for handle in edges:
        path = {}
        target = handle
        while target in edges and target not in visited and target not in path:
            path[target] = len(path)
            target = edges[target]
        if target in path:
            cycles.update(list(path)[path[target]:])
        visited.update(path)
    return cycles, usage


def _resolve_table_styles(tables, styles):
    cycles, usage = _table_style_cycles(tables, styles)
    for table in tables:
        handles = table["style_handles"]
        definitions = [definition for handle in handles for definition in styles.get(handle, [])]
        if len(handles) > 1 or len(definitions) + sum(usage[h] for h in handles) > 1:
            status = "ambiguous"
        elif table["table_handle"] in cycles:
            status = "cyclic"
        elif definitions:
            status = "resolved"
        elif any(usage[h] for h in handles):
            status = "invalid_style"
        else:
            status = "missing_style"
        table.update(status=status, definitions=definitions)


def _resolve_tolerance_font(frame, dimension_styles, styles, handle_types):
    name = frame["dimension_style"] or ""
    definitions = dimension_styles.get(name.casefold(), [])
    handles = list(dict.fromkeys(handle for definition in definitions for handle in definition))
    font = None
    if len(definitions) > 1 or len(handles) > 1:
        status = "ambiguous"
    elif not handles:
        status = "missing_style"
    else:
        handle = handles[0]
        targets = styles.get(handle, [])
        kinds = handle_types.get(handle, [])
        if len(kinds) > 1 or len(targets) > 1:
            status = "ambiguous"
        elif not targets:
            status = "wrong_type" if kinds else "missing_style"
        elif targets[0]["shape_file"]:
            status = "wrong_type"
        else:
            font = targets[0]["font"]
            status = "resolved" if font else "missing_font"
    return {
        **frame, "style_handles": handles, "status": status, "font": font,
        "uses_second_font": (any(glyph.casefold() != font.casefold() for glyph in frame["glyph_fonts"])
                             if font else None),
    }


def from_pairs(pairs, *, literal_text=None):
    dependencies = {}
    texture_maps = {}
    web_lights = {}
    plot_setups = {}
    plot_resource_setups = {}
    plot_layouts = []
    layout_names = {}
    linetype_shape_links = []
    linetype_names = set()
    line_linetypes = {}
    styles = {}
    dimension_styles = {}
    tolerance_frames = []
    handle_types = {}
    has_tolerances = any(code == 0 and value.upper() == "TOLERANCE" for code, value in pairs)
    note_style_links = []
    table_style_links = []
    table_styles = {}
    material_definitions = {}
    panel_material_links = []
    # Full drawings require owning sections. Bare decoded-record fragments keep
    # their existing API behavior, but ENDSEC/EOF still close their scope.
    section = None if any(c == 0 and v.upper() == "SECTION" for c, v in pairs) else "__FRAGMENT__"
    owners = {"BLOCK": {"BLOCKS"}, "STYLE": {"TABLES"}, "LTYPE": {"TABLES"}, "DIMSTYLE": {"TABLES"},
              "IMAGEDEF": {"OBJECTS"}, "MATERIAL": {"OBJECTS"}, "PDFDEFINITION": {"OBJECTS"},
              "DWFDEFINITION": {"OBJECTS"}, "DGNDEFINITION": {"OBJECTS"},
              "TABLESTYLE": {"OBJECTS"}, "PLOTSETTINGS": {"OBJECTS"}, "LAYOUT": {"OBJECTS"},
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
        font_pointers = []
        capture_subclasses = kind in {"LAYOUT", "PLOTSETTINGS", "MATERIAL", "TABLESTYLE", "DIMSTYLE", "TOLERANCE"} | _PANEL_OUTLINES
        inline_record = kind in inline_entities
        while end < len(pairs) and pairs[end][0] != 0:
            tag, value = pairs[end]
            groups.setdefault(tag, value)
            if capture_subclasses:
                if tag == 100:
                    subclass = subclasses.setdefault(value, {})
                elif subclass is not None:
                    subclass.setdefault(tag, value)
                    if kind == "DIMSTYLE" and tag == 340 and value and subclass is subclasses.get("AcDbDimStyleTableRecord"):
                        font_pointers.append(value.upper())
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
        if has_tolerances and section in {"TABLES", "ENTITIES", "BLOCKS", "OBJECTS"}:
            record_handle = groups.get(105 if kind == "DIMSTYLE" else 5)
            if record_handle:
                handle_types.setdefault(record_handle.upper(), []).append(kind)
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
                handle = handle.upper()
                styles.setdefault(handle, []).append({
                    "style_handle": handle, "name": groups.get(2) or None,
                    "font": groups.get(3) or None, "big_font": groups.get(4) or None,
                    "shape_file": shape,
                })
            for code in (3, 4):
                name = groups.get(code, "")
                font_kind = "shape_font" if shape or code == 4 else "font"
                add(font_kind, name, "font remapped", remapped_to="sans-serif")
        elif kind == "DIMSTYLE" and section == "TABLES":
            fields = subclasses.get("AcDbDimStyleTableRecord", {})
            if name := fields.get(2):
                dimension_styles.setdefault(name.casefold(), []).append(font_pointers)
        elif kind == "TABLESTYLE" and section == "OBJECTS":
            fields = subclasses.get("AcDbTableStyle")
            if fields is not None and (handle := groups.get(5)):
                handle = handle.upper()
                table_styles.setdefault(handle, []).append({
                    "style_handle": handle,
                    "description": fields.get(3) or None,
                })
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
            # Restrict the report to explicitly linked, closed 2D outlines.
            if kind in _PANEL_OUTLINES and section == "ENTITIES" and groups.get(5):
                entity = subclasses.get("AcDbEntity", {})
                material = entity.get(347)
                polyline = subclasses.get("AcDbPolyline") or subclasses.get("AcDb2dPolyline", {})
                flags = _dxf_int(polyline.get(70, "0")) or 0
                model_space = entity.get(67, "0") == "0" and entity.get(410, "Model").casefold() == "model"
                if material and flags & 1 and model_space:
                    panel_material_links.append({
                        "entity_handle": groups[5], "layer": entity.get(8),
                        "material_handle": material.upper(),
                    })
        elif kind == "IMAGEDEF":
            add("image", groups.get(1) or groups.get(5), "image omitted")
        elif kind == "MATERIAL":
            handle = groups.get(5) or ""
            if section == "OBJECTS" and handle:
                material = subclasses.get("AcDbMaterial", {})
                material_definitions.setdefault(handle.upper(), []).append(material.get(1) or None)
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
            layout_fields = subclasses.get("AcDbLayout", {})
            layout = layout_fields.get(1)
            if layout and layout_fields.get(330):
                layout_names[layout_fields[330].upper()] = layout
            settings = subclasses.get("AcDbPlotSettings")
            if layout and layout.casefold() != "model" and settings is not None:
                plot_layouts.append({"layout": layout, "page_setup": settings.get(1) or None,
                                     "layout_resources": plot_resources(settings, layout=True)})
        elif kind in ("PDFDEFINITION", "DWFDEFINITION", "DGNDEFINITION"):
            add("underlay", groups.get(1) or groups.get(2) or groups.get(5), "underlay omitted")
        elif kind in ("ACAD_PROXY_ENTITY", "ACAD_PROXY_OBJECT", "PROXY_ENTITY", "PROXY_OBJECT"):
            add("proxy", groups.get(5) or groups.get(1) or kind, "proxy omitted")
        elif kind in inline_entities or kind == "TOLERANCE":
            if kind == "MULTILEADER" and section in {"ENTITIES", "BLOCKS"}:
                link = _note_style_link(pairs, i + 1, end, groups)
                if link is not None:
                    note_style_links.append((link, groups.get(330, "").upper(), section))
            if kind in {"ACAD_TABLE", "TABLE"} and section in {"ENTITIES", "BLOCKS"}:
                if handle := groups.get(5):
                    layout = groups.get(410)
                    if not layout and section == "ENTITIES" and groups.get(67, "0") == "0":
                        layout = "Model"
                    table_style_links.append(({
                        "table_handle": handle.upper(),
                        "layout": layout,
                        "style_handles": _table_style_fields(pairs, i + 1, end),
                    }, groups.get(330, "").upper()))
            # Font controls can span repeated DXF text chunks. Read them in
            # record order; a first-value map would lose split font names.
            # A tolerance frame stores its content in group 1 and its dimension style in group 3.
            text = groups.get(1, "") if kind == "TOLERANCE" else "".join(text_parts)
            inline_fonts = list(dict.fromkeys(_inline_fonts(text)))
            for name in inline_fonts:
                add("font", name, "font remapped", remapped_to="sans-serif")
            if kind == "TOLERANCE" and section in {"ENTITIES", "BLOCKS"} and groups.get(5):
                fields = subclasses.get("AcDbFcf")
                if fields is not None:
                    tolerance_frames.append({
                        "frame_handle": groups[5].upper(), "dimension_style": fields.get(3) or None,
                        "glyph_fonts": list(dict.fromkeys(_inline_fonts(fields.get(1, "")))),
                    })
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
    material_usage = Counter(link["material_handle"] for link in panel_material_links)
    for link in panel_material_links:
        handle = link["material_handle"]
        definitions = material_definitions.get(handle, [])
        if len(definitions) > 1:
            status = "ambiguous"
        elif not definitions:
            status = "missing"
        elif not definitions[0]:
            status = "invalid_definition"
        else:
            status = "shared" if material_usage[handle] > 1 else "resolved"
        link.update(status=status, definitions=definitions)
    notes = []
    for link, owner, note_section in note_style_links:
        layout = layout_names.get(owner)
        if layout is not None or note_section == "ENTITIES":
            notes.append({**link, "layout": layout or link["layout"], **_resolve_note_style(link, styles)})
    schedules = [{**link, "layout": layout_names.get(owner, link["layout"])}
                 for link, owner in table_style_links]
    _resolve_table_styles(schedules, table_styles)
    report = {"schema": "leaf.drawing-dependencies.v1",
            "explicit_line_linetypes_checked": True,
            "explicit_panel_material_links": panel_material_links,
            "note_style_links": notes,
            "table_style_links": schedules,
            "tolerance_font_links": [_resolve_tolerance_font(frame, dimension_styles, styles, handle_types)
                                     for frame in tolerance_frames],
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
