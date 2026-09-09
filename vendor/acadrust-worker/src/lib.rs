//! wasm-bindgen wrapper for acadrust (MPL-2.0), day 3 of the CAD engine spike.
//!
//! The exported surface mirrors ../bindings.mjs (the JS-native stand-in the
//! day-2 test actually runs) 1:1 — same three names via `js_name`, same call
//! structure (`parseDxf` returns a document handle, `writeDxf` takes it,
//! `bytesEqual` compares byte buffers) — so swapping the stand-in for the
//! compiled `pkg/acadrust_worker.js` really is the one-line import change in
//! worker-entry.mjs, plus the `.entities` accessor below which the stand-in
//! exposes as a plain array and this wrapper exposes as a getter returning a
//! JS array of `{type, layer, start, end}` objects of the same shape.
//!
//! Bytes in from JS, bytes out to JS — exactly the shape `wasm_bindgen`'s
//! `&[u8]` / `Vec<u8>` marshalling wants; no `std::fs` anywhere on this
//! call path (in-memory `DxfReader::from_reader` / `DxfWriter::write_to_vec`
//! per day 1's inventory).
//!
//! OQ-3 RESOLVED (day 3, against the real crate at the pinned rev in
//! Cargo.toml — C:\tmp\spike-day3-c6477dd5-crate, HEAD
//! 18500466e7e4392ef830fdc59cede75fa3794f2b). Corrections made against the
//! day-2 draft, each verified by reading the real source, not guessed:
//!
//! 1. `DxfReader::from_reader(reader)` returns `Result<DxfReader>`, NOT a
//!    document. `DxfReader` is a builder/config handle; the actual parse
//!    happens in a separate consuming `.read(self) -> Result<CadDocument>`
//!    call (src/io/dxf/reader.rs:60,158). Day 2's draft stored the
//!    `DxfReader` itself in `ParsedDxf.inner: acadrust::Document` — wrong
//!    type entirely (`acadrust::Document` does not exist; the real type is
//!    `acadrust::CadDocument`, src/document.rs:1002) and missing the `.read()`
//!    call. Fixed: `ParsedDxf.inner` is now `CadDocument`, produced by
//!    chaining `.from_reader(..)?.read()?`.
//! 2. `DxfWriter` is not called as an associated function taking `&CadDocument`
//!    as day 2 wrote (`DxfWriter::write_to_vec(&doc.inner)`). It is a
//!    constructed value borrowing the document: `DxfWriter::new(&CadDocument)
//!    -> DxfWriter<'a>` (src/io/dxf/writer/mod.rs:29), then
//!    `.write_to_vec(&self) -> Result<Vec<u8>>` is an instance method
//!    (mod.rs:71), not associated-fn-with-doc-arg. Fixed:
//!    `DxfWriter::new(&doc.inner).write_to_vec()`.
//! 3. `CadDocument::entities()` returns `impl Iterator<Item = &EntityType>`
//!    (src/document.rs:2675) where `EntityType` is a 40+-variant enum
//!    (src/entities/mod.rs:405), not a type with an `.as_line()` method (no
//!    such method exists anywhere in the crate — day 2's guess). The correct
//!    match is `if let EntityType::Line(line) = e { ... }` against the enum
//!    variant directly (src/entities/mod.rs:409, `Line(Line)`).
//! 4. `.layer()` is defined on the `Entity` trait (src/entities/mod.rs:168),
//!    implemented per concrete entity struct (e.g. `impl Entity for Line`,
//!    src/entities/line.rs:74-89) — NOT a method on `EntityType` itself (day
//!    2 wrote `e.layer()` where `e: &EntityType`, which does not compile:
//!    `EntityType` has no such inherent method). Once matched down to the
//!    concrete `&Line`, `line.layer()` resolves via the `Entity` trait, which
//!    must be in scope (`use acadrust::entities::Entity;`).
//! 5. `Line.start` / `Line.end` are `Vector3` structs with public `.x`/`.y`/
//!    `.z` f64 fields (src/entities/line.rs:9-20, confirmed via
//!    `Vector3::new(x, y, z)` call sites elsewhere in the same file) — this
//!    part of day 2's draft was already correct, carried forward unchanged.
//!
//! 6. (found only by running the REAL compiled wasm in Node, not by static
//!    reading — see docs/ACADRUST-SPIKE-DAY3.md "First real-wasm run,
//!    unfixed") `serde_wasm_bindgen::to_value` with its DEFAULT `Serializer`
//!    converts a Rust map/struct into a JS `Map` instance, not a plain
//!    object — `JSON.stringify` on a `Map` prints `{}`, which is exactly the
//!    empty-object symptom the first real run showed for every entity.
//!    Fixed by serializing with `Serializer::json_compatible()`
//!    (`serialize_maps_as_objects: true`), the crate's own documented
//!    plain-object mode, instead of the bare `to_value` free function.
//!
//! Executed against the real compiled wasm in Node — see
//! docs/ACADRUST-SPIKE-DAY3.md for the round-trip evidence.
//!
//! W4d (the Draw group) split every operation into a CORE that returns a
//! plain `Result<_, String>` refusal code and a thin `#[wasm_bindgen]` export
//! that maps it to a `JsValue` at the boundary. `JsValue` cannot be built off
//! wasm32, so this is what lets the refusal paths run under native
//! `cargo test` (the tests at the bottom of this file) instead of only in a
//! browser. The exported names and semantics are unchanged.

use acadrust::entities::{Arc as ArcEntity, Circle, Dimension, DimensionAligned, DimensionLinear, Entity, EntityType, Line, LwPolyline, Text, Point, Ellipse, Insert};
use acadrust::entities::MultiLeader;
use acadrust::types::{Color, Handle, LineWeight, Transform, Vector2, Vector3};
use acadrust::{CadDocument, DxfReader, DxfWriter};
use acadrust::objects::{AssociativeData, Dictionary, Group, ObjectType};
use acadrust::io::dxf::{DxfStreamWriter, DxfTextWriter};
use serde::Serialize;
use std::cell::Cell;
use std::collections::{HashMap, HashSet};
use wasm_bindgen::prelude::*;

#[wasm_bindgen]
extern "C" {
    #[wasm_bindgen(js_namespace = Reflect, js_name = set)]
    fn set_projection_field(target: &JsValue, key: &JsValue, value: &JsValue) -> bool;
}

// ---------------------------------------------------------------------------
// Card F-3 (editing surface engine leg). Everything below is Leaf Automation wrapper
// code: the crate stays unmodified and rev-pinned (the license review's
// tripwire), and every mutation goes through the crate's own public surface
// (entities_mut(), common_mut(), the public vertex Vecs, add_entity()).
// Contract shared by every exported mutation: bounds-checked, refused with a
// typed code — never a panic across the wasm boundary; an out-of-range index
// or an unsupported entity kind is a typed JS error the worker folds into an
// editApplied refusal, and the document is NEVER half-mutated on a refused
// edit (each op validates before it writes).
//
// Index contract: `editableEntities` and every mutation address entities by
// their CURRENT position in document order. Any successful mutation may
// invalidate previously fetched indexes, so the UI must refresh its list
// from the edit response before issuing another edit — which is exactly what
// the existing editApplied message already carries. A create returns the new
// entity's HANDLE, the identity that survives the write/re-parse (an index
// does not), and the projection carries every entity's handle for the lookup.
// ---------------------------------------------------------------------------

/// The refusal type inside the wrapper: a stable code string. Converted to a
/// `JsValue` only at the exported boundary (see `js_err`).
type Refusal = String;

fn refuse<T>(code: &str) -> Result<T, Refusal> {
    Err(code.to_string())
}

fn js_err(refusal: Refusal) -> JsValue {
    JsValue::from_str(&refusal)
}

/// JavaScript numbers cannot represent every DXF u64 handle. Keep the
/// engine identity lossless at the wasm boundary as a canonical decimal
/// string, including for handles above Number.MAX_SAFE_INTEGER.
fn handle_id(value: u64) -> String {
    value.to_string()
}

/// True when this entity kind is one the editor can mutate through the
/// crate's public fields: vertex-level geometry for LINE / LWPOLYLINE /
/// POLYLINE, centre-level geometry for CIRCLE / ARC (W4d Draw group: what
/// the ribbon can create it must also be able to delete, move and re-layer;
/// their single "vertex" is the centre, and vertex insert/delete is refused
/// by kind). Everything else still round-trips through the writer untouched —
/// the whole-document model means "unsupported" costs nothing and loses
/// nothing.
fn editable(entity: &EntityType) -> bool {
    matches!(
        entity,
        EntityType::Line(_)
            | EntityType::LwPolyline(_)
            | EntityType::Polyline2D(_)
            | EntityType::Circle(_)
            | EntityType::Arc(_)
            | EntityType::Text(_)
            | EntityType::Point(_)
            | EntityType::Ellipse(_)
    )
}

fn kind_name(entity: &EntityType) -> &'static str {
    match entity {
        EntityType::Line(_) => "LINE",
        EntityType::LwPolyline(_) => "LWPOLYLINE",
        EntityType::Polyline2D(_) => "POLYLINE",
        EntityType::Circle(_) => "CIRCLE",
        EntityType::Arc(_) => "ARC",
        EntityType::Text(_) => "TEXT",
        // W4g-4b: the reference's Draw column, engine-backed now.
        EntityType::Point(_) => "POINT",
        EntityType::Ellipse(_) => "ELLIPSE",
        EntityType::Insert(_) => "INSERT",
        // W4g-7b-04c: a DIMENSION projects under its own type name regardless
        // of dimtype (LINEAR / ALIGNED / OTHER); see dimension_dimtype_of.
        EntityType::Dimension(_) => "DIMENSION",
        EntityType::MultiLeader(_) => "MLEADER",
        _ => "OTHER",
    }
}

fn vertices_of(entity: &EntityType) -> Vec<[f64; 3]> {
    match entity {
        EntityType::Line(line) => vec![
            [line.start.x, line.start.y, line.start.z],
            [line.end.x, line.end.y, line.end.z],
        ],
        EntityType::LwPolyline(poly) => poly
            .vertices
            .iter()
            .map(|v| [v.location.x, v.location.y, poly.elevation])
            .collect(),
        EntityType::Polyline2D(poly) => poly
            .vertices
            .iter()
            .map(|v| [v.location.x, v.location.y, v.location.z])
            .collect(),
        // The centre is the one point a circle or arc is addressed by.
        EntityType::Circle(c) => vec![[c.center.x, c.center.y, c.center.z]],
        EntityType::Arc(a) => vec![[a.center.x, a.center.y, a.center.z]],
        EntityType::Text(t) => vec![[t.insertion_point.x, t.insertion_point.y, t.insertion_point.z]],
        // W4g-4b: a POINT is its location; an ELLIPSE is addressed by its centre
        // (the axis and ratio ride beside it in the projection).
        EntityType::MultiLeader(m) => m.context.leader_roots.iter()
            .flat_map(|root| root.lines.iter().flat_map(|line| line.points.iter())
                .chain(std::iter::once(&root.connection_point).filter(move |p| root.lines.last().and_then(|line| line.points.last()) != Some(*p))))
            .map(|p| [p.x, p.y, p.z]).collect(),
        EntityType::Point(p) => vec![[p.location.x, p.location.y, p.location.z]],
        EntityType::Ellipse(e) => vec![[e.center.x, e.center.y, e.center.z]],
        _ => Vec::new(),
    }
}

/// W4g-6d: a polyline's bulge per vertex (tan of a quarter of the segment's
/// included angle, positive counter-clockwise, 0 straight), so the client can
/// SEE a curved segment (refuse the verbs whose maths is on chords, draw the
/// arc on the canvas) and carry every bulge back through set_vertices. `None`
/// for every other kind, so a consumer that ignores it sees the old shape.
fn bulges_of(entity: &EntityType) -> Option<Vec<f64>> {
    match entity {
        EntityType::LwPolyline(poly) => Some(poly.vertices.iter().map(|v| v.bulge).collect()),
        EntityType::Polyline2D(poly) => Some(poly.vertices.iter().map(|v| v.bulge).collect()),
        _ => None,
    }
}

fn closed_of(entity: &EntityType) -> bool {
    match entity {
        EntityType::LwPolyline(poly) => poly.is_closed,
        EntityType::Polyline2D(poly) => poly.flags.is_closed(),
        _ => false,
    }
}

/// W4f: a circle or arc is drawn from its centre, radius and sweep; the
/// projection carried only the centre before, so the viewer could not show
/// the engine document. `None` for every other kind (JSON null), and the
/// angles come out in DEGREES, the same unit the create operands take.
fn text_of(entity: &EntityType) -> Option<String> {
    match entity {
        EntityType::Text(t) => Some(t.value.clone()),
        EntityType::MultiLeader(m) => m.text().map(str::to_string),
        _ => None,
    }
}

fn height_of(entity: &EntityType) -> Option<f64> {
    match entity {
        EntityType::Text(t) => Some(t.height),
        EntityType::MultiLeader(m) => Some(if m.context.text_height > 0.0 { m.context.text_height } else { m.text_height }),
        _ => None,
    }
}

fn rotation_deg_of(entity: &EntityType) -> Option<f64> {
    match entity {
        EntityType::Text(t) => Some(t.rotation.to_degrees()),
        EntityType::Insert(i) => Some(i.rotation.to_degrees()),
        // W4g-7b-04c: LINEAR's own rotation; ALIGNED has none (0, its
        // dimension line always runs parallel to def1-def2).
        EntityType::Dimension(Dimension::Linear(d)) => Some(d.rotation.to_degrees()),
        EntityType::Dimension(Dimension::Aligned(_)) => Some(0.0),
        _ => None,
    }
}

// W4g-7b-04c: the DIMENSION projection. Linear / Aligned carry their own
// definition points, the dimension-line point (`base.definition_point` —
// the field the writer actually emits at group 10, never the per-variant
// `definition_point` field the crate keeps but never reads on write), the
// style and the COMPUTED measurement (`measurement()`, never the cached
// `actual_measurement`). Every other dimension kind (Radius, Diameter,
// Angular, Ordinate, Arc, LargeRadial) projects as 'OTHER': visible by
// handle, refused for editing, never dropped (the 05c rule).
fn dimension_dimtype_of(entity: &EntityType) -> Option<&'static str> {
    match entity {
        EntityType::Dimension(Dimension::Linear(_)) => Some("LINEAR"),
        EntityType::Dimension(Dimension::Aligned(_)) => Some("ALIGNED"),
        EntityType::Dimension(_) => Some("OTHER"),
        _ => None,
    }
}

fn dimension_def1_of(entity: &EntityType) -> Option<[f64; 2]> {
    match entity {
        EntityType::Dimension(Dimension::Linear(d)) => Some([d.first_point.x, d.first_point.y]),
        EntityType::Dimension(Dimension::Aligned(d)) => Some([d.first_point.x, d.first_point.y]),
        _ => None,
    }
}

fn dimension_def2_of(entity: &EntityType) -> Option<[f64; 2]> {
    match entity {
        EntityType::Dimension(Dimension::Linear(d)) => Some([d.second_point.x, d.second_point.y]),
        EntityType::Dimension(Dimension::Aligned(d)) => Some([d.second_point.x, d.second_point.y]),
        _ => None,
    }
}

fn dimension_dimline_of(entity: &EntityType) -> Option<[f64; 2]> {
    match entity {
        EntityType::Dimension(dim @ (Dimension::Linear(_) | Dimension::Aligned(_))) => {
            let p = dim.base().definition_point;
            Some([p.x, p.y])
        }
        _ => None,
    }
}

fn dimension_style_of(entity: &EntityType) -> Option<String> {
    match entity {
        EntityType::Dimension(dim @ (Dimension::Linear(_) | Dimension::Aligned(_))) => Some(dim.base().style_name.clone()),
        _ => None,
    }
}

fn dimension_measurement_of(entity: &EntityType) -> Option<f64> {
    match entity {
        EntityType::Dimension(dim @ (Dimension::Linear(_) | Dimension::Aligned(_))) => Some(dim.measurement()),
        _ => None,
    }
}

/// W4g-4b: an ELLIPSE's major-axis endpoint RELATIVE to its centre and its
/// minor-to-major ratio, so the client can draw it; `None` for every other kind.
fn major_axis_of(entity: &EntityType) -> Option<[f64; 2]> {
    match entity {
        EntityType::Ellipse(e) => Some([e.major_axis.x, e.major_axis.y]),
        _ => None,
    }
}

fn ratio_of(entity: &EntityType) -> Option<f64> {
    match entity {
        EntityType::Ellipse(e) => Some(e.minor_axis_ratio),
        _ => None,
    }
}

fn radius_of(entity: &EntityType) -> Option<f64> {
    match entity {
        EntityType::Circle(c) => Some(c.radius),
        EntityType::Arc(a) => Some(a.radius),
        _ => None,
    }
}

fn sweep_deg_of(entity: &EntityType) -> Option<(f64, f64)> {
    match entity {
        EntityType::Arc(a) => Some((a.start_angle.to_degrees(), a.end_angle.to_degrees())),
        _ => None,
    }
}

// W4g-7b-03c: colour, linetype and lineweight. ACI is 0..256 (256 ByLayer, 0
// ByBlock); a true colour (Color::Rgb) still carries a nearest-neighbour ACI
// here so a consumer that only reads `aci` never sees a hole, but `trueColor`
// is the field that decides whether the entity is actually true-coloured.
fn aci_of(color: &Color) -> i64 {
    (color.approximate_index() as i64).clamp(0, 256)
}

fn true_color_of(color: &Color) -> Option<[u8; 3]> {
    color.true_color_rgb().map(|(r, g, b)| [r, g, b])
}

// EntityCommon.linetype's empty string means ByLayer (see has_linetype());
// the projection always spells it out.
fn linetype_of(entity: &EntityType) -> String {
    let name = &entity.common().linetype;
    if name.is_empty() { "ByLayer".to_string() } else { name.clone() }
}

fn lineweight_of(entity: &EntityType) -> i64 {
    entity.common().line_weight.value() as i64
}

// W4g-7b-01c: block definitions share the crate's flat entity storage, but
// their children are not independent model-space geometry or edit targets.
const BLOCK_CHILD_CAP: usize = 60;
// W4g-7b-05c-2: ERASE stays allowed on a placed INSERT or DIMENSION (the
// contract carries `removed` for any kind); every other geometry verb
// refuses by kind. Both sentences gate through entity_mut only, never
// editable_at/delete_entity_core, so delete_entity_core never sees them.
const INSERT_NOT_EDITABLE: &str = "an INSERT is placed, not edited, in this round";
const DIMENSION_NOT_EDITABLE: &str = "a dimension is placed, not edited, in this round";
const MLEADER_NOT_EDITABLE: &str = "a mleader is placed, not edited, in this round";

fn block_children(document: &CadDocument) -> HashSet<Handle> {
    document.block_records.iter()
        .filter(|b| !b.is_model_space() && b.name != "*Paper_Space")
        .flat_map(|b| b.entity_handles.iter().copied())
        .collect()
}

// One dependency walk shared by the projection and the create boundary.
fn dimension_defining_handles(document: &CadDocument) -> HashMap<Handle, HashSet<Handle>> {
    let mut defining = HashMap::<Handle, HashSet<Handle>>::new();
    let mut reactors = HashMap::<Handle, Handle>::new();
    for entity in document.entities() {
        if let EntityType::Dimension(dim) = entity {
            let handle = entity.common().handle;
            reactors.insert(handle, handle);
            defining.entry(handle).or_default().extend(document.block_records.iter()
                .filter(|b| b.name == dim.base().block_name)
                .flat_map(|b| b.entity_handles.iter().copied()));
        }
    }
    for object in document.objects.values() {
        if let ObjectType::Associative(object) = object {
            if let AssociativeData::DimensionAssociation(assoc) = &object.data {
                reactors.insert(object.handle, assoc.dimension);
                let handles = defining.entry(assoc.dimension).or_default();
                handles.insert(assoc.dimension);
                for reference in assoc.references.iter().flatten() {
                    handles.extend(reference.xrefs.iter().copied());
                    handles.extend(reference.intersection_objects.iter().copied());
                }
            }
        }
    }
    for entity in document.entities() {
        for reactor in &entity.common().reactors {
            if let Some(dimension) = reactors.get(reactor) {
                defining.entry(*dimension).or_default().insert(entity.common().handle);
            }
        }
    }
    defining
}

fn block_base(document: &CadDocument, block: &acadrust::tables::BlockRecord) -> [f64; 3] {
    let base = match document.get_entity(block.block_entity_handle) {
        Some(EntityType::Block(marker)) => marker.base_point,
        // Newly allocated system records may have no marker yet.
        _ => block.base_point,
    };
    [base.x, base.y, base.z]
}

fn entity_record(index: usize, entity: &EntityType, can_edit: bool) -> serde_json::Value {
    let mut record = serde_json::json!({
        "index": index,
        "handle": handle_id(entity.common().handle.value()),
        "type": kind_name(entity),
        "layer": entity.common().layer.clone(),
        "closed": closed_of(entity),
        "editable": can_edit && editable(entity),
        "vertices": vertices_of(entity),
        "bulges": bulges_of(entity),
        "radius": radius_of(entity),
        "text": text_of(entity),
        "height": height_of(entity),
        "rotationDeg": rotation_deg_of(entity),
        "startDeg": sweep_deg_of(entity).map(|(start, _)| start),
        "endDeg": sweep_deg_of(entity).map(|(_, end)| end),
        "majorAxis": major_axis_of(entity),
        "ratio": ratio_of(entity),
        "aci": aci_of(&entity.common().color),
        "trueColor": true_color_of(&entity.common().color),
        "linetype": linetype_of(entity),
        "lineweight": lineweight_of(entity),
        "normal": normal_of(entity),
        // W4g-7b-04c: null for every kind but DIMENSION; OTHER dimtypes carry
        // only "dimtype" (the rest stay null, per the projection contract).
        "dimtype": dimension_dimtype_of(entity),
        "def1": dimension_def1_of(entity),
        "def2": dimension_def2_of(entity),
        "dimline": dimension_dimline_of(entity),
        "style": dimension_style_of(entity),
        "measurement": dimension_measurement_of(entity),
    });
    if let EntityType::LwPolyline(poly) = entity {
        if poly.constant_width != 0.0 {
            record["constantWidth"] = serde_json::json!(poly.constant_width);
        }
        if poly.vertices.iter().any(|v| v.start_width != 0.0) {
            record["startWidths"] = serde_json::json!(poly.vertices.iter().map(|v| v.start_width).collect::<Vec<_>>());
        }
        if poly.vertices.iter().any(|v| v.end_width != 0.0) {
            record["endWidths"] = serde_json::json!(poly.vertices.iter().map(|v| v.end_width).collect::<Vec<_>>());
        }
    }
    if let EntityType::MultiLeader(m) = entity {
        let p = m.context.text_location;
        record["textLocation"] = serde_json::json!([p.x, p.y, p.z]);
        record["arrow"] = serde_json::json!(m.arrowhead_size);
        record["dogleg"] = serde_json::json!(m.dogleg_length);
    }
    if let EntityType::Insert(insert) = entity {
        record["kind"] = serde_json::json!("REFERENCE");
        record["name"] = serde_json::json!(insert.block_name);
        record["ip"] = serde_json::json!([insert.insert_point.x, insert.insert_point.y, insert.insert_point.z]);
        record["scale"] = serde_json::json!([insert.x_scale(), insert.y_scale(), insert.z_scale()]);
        record["columns"] = serde_json::json!(insert.column_count);
        record["rows"] = serde_json::json!(insert.row_count);
        record["columnSpacing"] = serde_json::json!(insert.column_spacing);
        record["rowSpacing"] = serde_json::json!(insert.row_spacing);
    }
    record
}

fn projected_entities(document: &CadDocument) -> Vec<serde_json::Value> {
    let children = block_children(document);
    let defining = dimension_defining_handles(document);
    let defined: HashSet<Handle> = defining.values().flatten().copied().collect();
    document.entities().enumerate()
        .filter(|(_, e)| !children.contains(&e.common().handle))
        .map(|(index, e)| {
            let mut record = entity_record(index, e, true);
            let model_space = !document.block_records.iter()
                .any(|block| block.handle == e.common().owner_handle && !block.is_model_space());
            if let EntityType::MultiLeader(m) = e {
                let style = document.objects.values().find_map(|object| match object {
                    ObjectType::MultiLeaderStyle(style) if Some(style.handle) == m.style_handle => Some(style.name.as_str()),
                    _ => None,
                }).unwrap_or("");
                record["style"] = serde_json::json!(style);
            }
            record["modelSpace"] = serde_json::json!(model_space);
            if defined.contains(&e.common().handle) { record["dimensionDefined"] = serde_json::json!(true); }
            if let EntityType::Dimension(_) = e {
                let mut handles: Vec<String> = defining.get(&e.common().handle).into_iter()
                    .flatten().map(|h| handle_id(h.value())).collect();
                handles.sort();
                record["definingHandles"] = serde_json::json!(handles);
            }
            record
        })
        .collect()
}

// Use the actual writer's group sequence, including inline VERTEX/ATTRIB/
// SEQEND records. Handles and owners identify records but are not geometry.
fn written_block_children(document: &CadDocument) -> HashMap<Handle, String> {
    let mut records = HashMap::<Handle, String>::new();
    let bytes = match DxfWriter::new(document).write_to_vec() { Ok(bytes) => bytes, Err(_) => return records };
    let text = match std::str::from_utf8(&bytes) { Ok(text) => text, Err(_) => return records };
    let lines: Vec<_> = text.lines().collect();
    let code = |i: usize| lines.get(i).and_then(|line| line.trim().parse::<i32>().ok());
    let children = block_children(document);
    let mut in_blocks = false;
    let mut active = None;
    let mut i = 0;
    while i + 1 < lines.len() {
        if code(i) != Some(0) { i += 2; continue; }
        let mut end = i + 2;
        while end + 1 < lines.len() && code(end) != Some(0) { end += 2; }
        let kind = lines[i + 1];
        if kind == "SECTION" {
            in_blocks = code(i + 2) == Some(2) && lines.get(i + 3) == Some(&"BLOCKS");
            active = None;
        } else if kind == "ENDSEC" {
            in_blocks = false;
            active = None;
        } else if in_blocks {
            if matches!(kind, "BLOCK" | "ENDBLK") {
                active = None;
            } else {
                let handle = (i + 2..end).step_by(2).find(|&pos| code(pos) == Some(5))
                    .and_then(|pos| u64::from_str_radix(lines[pos + 1].trim(), 16).ok()).map(Handle::new);
                if let Some(handle) = handle.filter(|handle| children.contains(handle)) {
                    active = Some(handle);
                    records.entry(handle).or_default();
                } else if !matches!(kind, "VERTEX" | "ATTRIB" | "SEQEND") {
                    active = None;
                }
                if let Some(handle) = active {
                    let record = records.entry(handle).or_default();
                    for pos in (i..end).step_by(2) {
                        if matches!(code(pos), Some(5 | 330)) { continue; }
                        record.push_str(lines[pos]);
                        record.push('\n');
                        record.push_str(lines[pos + 1]);
                        record.push('\n');
                    }
                }
            }
        }
        i = end;
    }
    records
}

// FNV-1a over length-delimited canonical records in membership order. Only
// kinds absent from the writer's output fall back to full-field Debug.
fn block_digest(document: &CadDocument, block: &acadrust::tables::BlockRecord, base_unknown: bool,
    written: &HashMap<Handle, String>) -> String {
    let mut hash = 0xcbf29ce484222325u64;
    let mut feed = |value: String| {
        for byte in (value.len() as u64).to_le_bytes().iter().chain(value.as_bytes()) {
            hash = (hash ^ u64::from(*byte)).wrapping_mul(0x100000001b3);
        }
    };
    let mut base = Vec::new();
    for (axis, coordinate) in block_base(document, block).iter().enumerate() {
        let _ = DxfTextWriter::new(&mut base).write_double(10 + axis as i32 * 10, *coordinate);
    }
    feed(String::from_utf8(base).unwrap());
    feed(format!("{base_unknown}:{:?}:{}", block.flags, block.xref_path));
    for handle in &block.entity_handles {
        match (written.get(handle), document.get_entity(*handle)) {
            (Some(record), _) => feed(record.clone()),
            (_, Some(entity)) => feed(format!("{entity:?}")),
            _ => feed("missing".to_string()),
        }
    }
    format!("{hash:016x}")
}

fn block_catalogue(document: &CadDocument, bases_unknown: bool, unknown_bases: &HashSet<String>) -> Vec<serde_json::Value> {
    if !document.block_records.iter().any(|b| !b.is_model_space() && !b.is_paper_space()) { return Vec::new(); }
    let written = written_block_children(document);
    document.block_records.iter()
        .filter(|b| !b.is_model_space() && !b.is_paper_space())
        .map(|block| {
            let base_unknown = bases_unknown || unknown_bases.contains(&block.name);
            let mut complete = !base_unknown && block.entity_handles.len() <= BLOCK_CHILD_CAP && !block.flags.has_attributes;
            let mut children = Vec::new();
            for handle in block.entity_handles.iter().take(BLOCK_CHILD_CAP) {
                match document.get_entity(*handle) {
                    Some(entity) if matches!(entity, EntityType::Line(_) | EntityType::LwPolyline(_)
                        | EntityType::Polyline2D(_) | EntityType::Circle(_) | EntityType::Arc(_) | EntityType::Text(_)) => {
                        let mut child = entity_record(0, entity, false);
                        // Keep entity_record's decimal handle for identity-based plan lowering.
                        child.as_object_mut().unwrap().remove("index");
                        children.push(child);
                    }
                    _ => complete = false,
                }
            }
            serde_json::json!({ "name": block.name, "base": block_base(document, block), "children": children,
                "complete": complete, "baseUnknown": base_unknown, "digest": block_digest(document, block, base_unknown, &written) })
        })
        .collect()
}

// W4g-7b-03c: the LTYPE table's names for the Properties panel's linetype
// select, sorted case-insensitively and bounded so a pathological drawing
// cannot hand the client an unbounded list. ByLayer / ByBlock / Continuous
// are the crate's own always-created defaults (CadDocument::initialize_defaults),
// but a hand-built or heavily edited document could in principle omit one, so
// the catalogue guarantees them defensively.
const LINETYPE_CATALOGUE_CAP: usize = 200;

fn linetypes_catalogue(document: &CadDocument) -> (Vec<String>, bool) {
    let mut names: Vec<String> = document.line_types.iter().map(|lt| lt.name.clone()).collect();
    for required in ["ByLayer", "ByBlock", "Continuous"] {
        if !names.iter().any(|n| n.eq_ignore_ascii_case(required)) {
            names.push(required.to_string());
        }
    }
    names.sort_by(|a, b| a.to_lowercase().cmp(&b.to_lowercase()));
    names.dedup_by(|a, b| a.eq_ignore_ascii_case(b));
    let truncated = names.len() > LINETYPE_CATALOGUE_CAP;
    names.truncate(LINETYPE_CATALOGUE_CAP);
    let defaults = ["ByLayer", "ByBlock", "Continuous"];
    for required in defaults {
        if !names.iter().any(|n| n.eq_ignore_ascii_case(required)) {
            let index = names.iter().rposition(|n| !defaults.iter().any(|d| n.eq_ignore_ascii_case(d)))
                .expect("bounded catalogue has a non-default name");
            names[index] = required.to_string();
        }
    }
    names.sort_by(|a, b| a.to_lowercase().cmp(&b.to_lowercase()));
    (names, truncated)
}

// W4g-7b-04c: the DIMSTYLE table's names for the create-dimension style
// select, sorted case-insensitively and bounded exactly like linetypes_catalogue.
// 'Standard' is the crate's own always-created default
// (CadDocument::initialize_defaults), guaranteed defensively here too.
const DIMSTYLE_CATALOGUE_CAP: usize = 200;

// Group 173 is narrowed by the crate's style reader. Retain source facts by
// handle, with absent 173 read as 0; unscannable documents have no known value.
fn scan_mlstyle_segments(bytes: &[u8]) -> HashMap<Handle, i32> {
    let mut segments = HashMap::new();
    if bytes.len() > 16 * 1024 * 1024 || bytes.starts_with(b"AutoCAD Binary DXF") { return segments; }
    let mut pairs = bytes.split(|b| *b == b'\n');
    let mut objects = false;
    let mut section = false;
    let mut style = false;
    let mut handle = None;
    let mut count = None;
    while let (Some(code), Some(value)) = (pairs.next(), pairs.next()) {
        let code = std::str::from_utf8(code).ok().and_then(|s| s.trim().parse::<i32>().ok());
        let value = std::str::from_utf8(value).unwrap_or("").trim();
        if code == Some(0) {
            if style {
                if let Some(h) = handle { segments.insert(h, count.unwrap_or(0)); }
            }
            style = objects && value == "MLEADERSTYLE";
            handle = None;
            count = None;
            section = value == "SECTION";
            if section || value == "ENDSEC" { objects = false; }
        } else if section && code == Some(2) {
            objects = value == "OBJECTS";
            section = false;
        } else if style {
            match code {
                Some(5) => handle = u64::from_str_radix(value, 16).ok().map(Handle::new),
                Some(173) => count = value.parse::<i32>().ok(),
                _ => {},
            }
        }
    }
    if style {
        if let Some(h) = handle { segments.insert(h, count.unwrap_or(0)); }
    }
    segments
}

fn mlstyles_catalogue(document: &CadDocument, segments: &HashMap<Handle, i32>) -> Vec<serde_json::Value> {
    let mut styles: Vec<_> = document.objects.values().filter_map(|object| {
        let ObjectType::MultiLeaderStyle(style) = object else { return None; };
        let textstyle = document.text_styles.iter()
            .find(|s| Some(s.handle) == style.text_style_handle)
            .map(|s| s.name.as_str()).unwrap_or("Standard");
        Some(serde_json::json!({
            "name": style.name, "textstyle": textstyle, "height": style.text_height,
            "arrow": style.arrowhead_size, "dogleg": style.landing_distance,
            "gap": style.landing_gap, "segments": segments.get(&style.handle),
        }))
    }).collect();
    styles.sort_by(|a, b| a["name"].as_str().cmp(&b["name"].as_str()));
    styles
}

fn dimstyles_catalogue(document: &CadDocument) -> Vec<String> {
    let mut names: Vec<String> = document.dim_styles.iter().map(|s| s.name.clone()).collect();
    if !names.iter().any(|n| n.eq_ignore_ascii_case("Standard")) {
        names.push("Standard".to_string());
    }
    names.sort_by(|a, b| a.to_lowercase().cmp(&b.to_lowercase()));
    names.dedup_by(|a, b| a.eq_ignore_ascii_case(b));
    names.truncate(DIMSTYLE_CATALOGUE_CAP);
    names
}

// The pinned DXF reader records block handles but discards the BLOCK marker.
// Retain its base in the wrapper so the post-write pass has the source value.
fn retain_block_bases(document: &mut CadDocument, bytes: &[u8]) -> Result<HashSet<String>, Refusal> {
    let mut unknown: HashSet<String> = document.block_records.iter()
        .filter(|b| !b.is_model_space() && !b.is_paper_space()).map(|b| b.name.clone()).collect();
    if bytes.starts_with(b"AutoCAD Binary DXF") { return Ok(unknown); }
    // Decode each text line like the crate: UTF-8, then byte-to-char Latin-1.
    // Coordinates and handles do not depend on the description/name encoding.
    let lines: Vec<String> = bytes.split(|byte| *byte == b'\n').map(|line| {
        let line = line.strip_suffix(b"\r").unwrap_or(line);
        std::str::from_utf8(line).map(str::to_string).unwrap_or_else(|_| line.iter().map(|&byte| char::from(byte)).collect())
    }).collect();
    let code = |i: usize| lines.get(i).and_then(|s| s.trim().parse::<i32>().ok());
    let mut in_blocks = false;
    let mut i = 0;
    while i + 1 < lines.len() {
        if code(i) == Some(0) && lines[i + 1] == "SECTION" {
            in_blocks = code(i + 2) == Some(2) && lines.get(i + 3).map(String::as_str) == Some("BLOCKS");
        } else if code(i) == Some(0) && lines[i + 1] == "ENDSEC" {
            in_blocks = false;
        } else if in_blocks && code(i) == Some(0) && lines[i + 1] == "BLOCK" {
            let mut marker_handle = None;
            let mut base = [0.0; 3];
            let mut end = i + 2;
            while end + 1 < lines.len() && code(end) != Some(0) {
                match code(end) {
                    Some(5) => marker_handle = u64::from_str_radix(lines[end + 1].trim(), 16).ok().map(Handle::new),
                    Some(10 | 20 | 30) => {
                        let axis = (code(end).unwrap() / 10 - 1) as usize;
                        base[axis] = lines[end + 1].trim().parse::<f64>()
                            .map_err(|_| "block_base_not_finite".to_string())?;
                        if !base[axis].is_finite() { return refuse("block_base_not_finite"); }
                    }
                    _ => {}
                }
                end += 2;
            }
            if let Some(handle) = marker_handle.filter(|handle| !handle.is_null()) {
                let matches: Vec<String> = document.block_records.iter()
                    .filter(|block| block.block_entity_handle == handle).map(|block| block.name.clone()).collect();
                if matches.len() == 1 {
                    let name = &matches[0];
                    let block = document.block_records.get(name).unwrap();
                    let handle = block.block_entity_handle;
                    let owner = block.handle;
                    let base = Vector3::new(base[0], base[1], base[2]);
                    if let Some(EntityType::Block(marker)) = document.get_entity_mut(handle) {
                        marker.base_point = base;
                    } else if document.get_entity(handle).is_none() {
                        let mut marker = acadrust::entities::Block::new(name, base);
                        marker.common.handle = handle;
                        marker.common.owner_handle = owner;
                        let marker_handle = document.add_entity(EntityType::Block(marker))
                            .map_err(|e| format!("block_base_retention_failed:{e}"))?;
                        document.block_records.get_mut(name).unwrap().block_entity_handle = marker_handle;
                    } else {
                        return refuse("block_marker_handle_collision");
                    }
                    document.block_records.get_mut(name).unwrap().base_point = base;
                    unknown.remove(name);
                }
            }
            i = end;
            continue;
        }
        i += 2;
    }
    Ok(unknown)
}

// Count definitions on raw bytes, before any name decoding or table lookup.
// System and anonymous names start with '*'; all other BLOCKs count once.
fn raw_block_definition_count(bytes: &[u8]) -> Result<usize, Refusal> {
    let mut count = 0;
    let mut in_blocks = false;
    let mut section_record = false;
    let mut block_record = false;
    let mut visit = |code: i32, value: &[u8]| {
        if code == 0 {
            if value == b"ENDSEC" { in_blocks = false; }
            section_record = value == b"SECTION";
            block_record = in_blocks && value == b"BLOCK";
        } else if code == 2 {
            if section_record {
                in_blocks = value == b"BLOCKS";
                section_record = false;
            } else if block_record {
                if !value.starts_with(b"*") { count += 1; }
                block_record = false;
            }
        }
    };
    if !bytes.starts_with(b"AutoCAD Binary DXF") {
        let mut lines = bytes.split(|byte| *byte == b'\n');
        while let (Some(code), Some(value)) = (lines.next(), lines.next()) {
            if let Some(code) = std::str::from_utf8(code).ok().and_then(|code| code.trim().parse().ok()) {
                visit(code, value.strip_suffix(b"\r").unwrap_or(value));
            }
        }
        return Ok(count);
    }
    // Binary names need the same preflight. Use the crate's public group-type
    // mapping to skip values, including pre-R13 single-byte group codes.
    use acadrust::io::dxf::GroupCodeValueType as ValueType;
    let mut at = 22usize;
    let single_byte = bytes.get(at) == Some(&0) && bytes.get(at + 1).map_or(false, |b| (0x20..0x7f).contains(b));
    while at < bytes.len() {
        let code = if single_byte && bytes[at] != 255 {
            let code = i32::from(bytes[at]);
            at += 1;
            code
        } else {
            if single_byte { at += 1; }
            let pair = bytes.get(at..at + 2).ok_or("block_name_scan_truncated")?;
            at += 2;
            i32::from(i16::from_le_bytes([pair[0], pair[1]]))
        };
        let size = match ValueType::from_raw_code(code) {
            ValueType::String | ValueType::Handle | ValueType::None => {
                let length = bytes[at..].iter().position(|b| *b == 0).ok_or("block_name_scan_truncated")?;
                visit(code, &bytes[at..at + length]);
                length + 1
            }
            ValueType::Double | ValueType::Point3D | ValueType::Int64 => 8,
            ValueType::Int32 => 4,
            ValueType::Int16 | ValueType::Byte => 2,
            ValueType::Bool => 1,
            ValueType::BinaryData => 1 + usize::from(*bytes.get(at).ok_or("block_name_scan_truncated")?),
        };
        at = at.checked_add(size).filter(|end| *end <= bytes.len()).ok_or("block_name_scan_truncated")?;
    }
    Ok(count)
}

fn validate_block_names(document: &CadDocument, definitions: usize) -> Result<(), Refusal> {
    let retained = document.block_records.iter().filter(|block| !block.name.starts_with('*')).count();
    if definitions != retained {
        return Err(format!("block definitions collapsed on load: {definitions} in the file, {retained} retained"));
    }
    let mut names = HashMap::<String, String>::new();
    for block in document.block_records.iter().filter(|block| !block.name.starts_with('*')) {
        if let Some(previous) = names.insert(block.name.to_uppercase(), block.name.clone()) {
            return Err(format!("block names collide case-insensitively: {previous}, {}", block.name));
        }
    }
    Ok(())
}

fn parse_dxf_core(bytes: &[u8]) -> Result<ParsedDxf, Refusal> {
    let definitions = raw_block_definition_count(bytes)?;
    let mut inner = DxfReader::from_reader(std::io::Cursor::new(bytes.to_vec()))
        .map_err(|e| e.to_string())?.read().map_err(|e| e.to_string())?;
    validate_block_names(&inner, definitions)?;
    let unknown_block_bases = retain_block_bases(&mut inner, bytes)?;
    Ok(ParsedDxf { group_names: group_names(&inner), inner, block_base_patched: Cell::new(false), block_bases_unknown: bytes.starts_with(b"AutoCAD Binary DXF"), unknown_block_bases, mlstyle_segments: scan_mlstyle_segments(bytes) })
}

// Validate all BLOCK layouts before emitting any replacement. Keep every byte
// outside the three coordinate values, including the original line endings.
// Formatting delegates to the pinned crate's public writer, not a copied formatter.
fn patch_block_bases(document: &CadDocument, bytes: Vec<u8>) -> (Vec<u8>, bool) {
    let text = match std::str::from_utf8(&bytes) { Ok(text) => text, Err(_) => return (bytes, false) };
    let lines: Vec<&str> = text.split_inclusive('\n').collect();
    if lines.len() % 2 != 0 { return (bytes, false); }
    let value = |i: usize| lines[i].trim_end_matches(['\r', '\n']);
    let code = |i: usize| value(i).trim().parse::<i32>().ok();
    let bases: HashMap<Handle, [f64; 3]> = document.block_records.iter()
        .map(|b| (b.block_entity_handle, block_base(document, b))).collect();
    let mut replacements = HashMap::new();
    let mut in_blocks = false;
    let mut i = 0;
    while i + 1 < lines.len() {
        if code(i) == Some(0) && value(i + 1) == "SECTION" {
            in_blocks = i + 3 < lines.len() && code(i + 2) == Some(2) && value(i + 3) == "BLOCKS";
        } else if code(i) == Some(0) && value(i + 1) == "ENDSEC" {
            in_blocks = false;
        } else if in_blocks && code(i) == Some(0) && value(i + 1) == "BLOCK" {
            let mut end = i + 2;
            while end + 1 < lines.len() && code(end) != Some(0) { end += 2; }
            let names: Vec<usize> = (i + 2..end).step_by(2).filter(|&at| code(at) == Some(2)).collect();
            if names.len() != 1 { return (bytes, false); }
            let at = names[0];
            if at + 9 >= end || [70, 10, 20, 30].iter().enumerate().any(|(j, expected)| code(at + 2 + j * 2) != Some(*expected)) {
                return (bytes, false);
            }
            if (i + 2..end).step_by(2).filter(|&pos| matches!(code(pos), Some(10 | 20 | 30))).count() != 3 {
                return (bytes, false);
            }
            let handle = (i + 2..end).step_by(2).find(|&pos| code(pos) == Some(5))
                .and_then(|pos| u64::from_str_radix(value(pos + 1).trim(), 16).ok()).map(Handle::new);
            let base = match handle.and_then(|handle| bases.get(&handle)) { Some(base) => base, None => return (bytes, false) };
            for (axis, coordinate) in base.iter().enumerate() {
                let mut formatted = Vec::new();
                if DxfTextWriter::new(&mut formatted).write_double(10, *coordinate).is_err() { return (bytes, false); }
                let formatted = match String::from_utf8(formatted) { Ok(s) => s, Err(_) => return (bytes, false) };
                let number = match formatted.lines().nth(1) { Some(s) => s, None => return (bytes, false) };
                let pos = at + 5 + axis * 2;
                let ending = &lines[pos][value(pos).len()..];
                replacements.insert(pos, format!("{number}{ending}"));
            }
            i = end;
            continue;
        }
        i += 2;
    }
    let mut patched = Vec::with_capacity(bytes.len());
    for (i, line) in lines.iter().enumerate() {
        patched.extend_from_slice(replacements.get(&i).map(String::as_str).unwrap_or(line).as_bytes());
    }
    (patched, true)
}

// W4d Draw group. Creation goes through the crate's own `add_entity`, which
// allocates the handle and routes the entity into model space; the wrapper
// only validates and builds the entity. Every create refuses BEFORE it
// touches the document (non-finite coordinates, a non-positive radius, a
// zero-sweep arc, a degenerate line, an odd or oversized point list).
const MAX_CREATED_VERTICES: usize = 100_000;

/// The most copies one ARRAY may add. Matches the store's own create bound,
/// so a plan the client refuses cannot arrive here either.
const MAX_ARRAY_COPIES: usize = 1_000;

/// The most characters one TEXT may carry. A DXF group value is one line;
/// the client refuses the same number so a long paste never reaches here.
const MAX_TEXT_CHARS: usize = 1024;

fn all_finite(values: &[f64]) -> bool {
    values.iter().all(|v| v.is_finite())
}

/// The layer a created entity lands on: trimmed, bounded, defaulting to the
/// always-present `0` when empty (a create with no layer typed is a normal
/// gesture; an empty layer NAME on an existing entity is not — set_entity_layer
/// keeps refusing that).
fn created_layer(layer: &str) -> Result<String, Refusal> {
    let trimmed = layer.trim();
    if trimmed.is_empty() {
        return Ok("0".to_string());
    }
    if trimmed.len() > 255 {
        return refuse("layer_name_too_long");
    }
    Ok(trimmed.to_string())
}

/// Opaque parsed-document handle: the Rust twin of the plain object
/// bindings.mjs's `parseDxf` returns. Crosses the boundary by reference;
/// JS only ever reads the `entities` getter or hands it back to `writeDxf`.
#[wasm_bindgen]
pub struct ParsedDxf {
    inner: CadDocument,
    group_names: Vec<(String, Handle)>,
    block_base_patched: Cell<bool>,
    block_bases_unknown: bool,
    unknown_block_bases: HashSet<String>,
    mlstyle_segments: HashMap<Handle, i32>,
}

fn group_dictionary(document: &CadDocument) -> Option<Handle> {
    match document.objects.get(&document.header.named_objects_dict_handle) {
        Some(ObjectType::Dictionary(root)) => root.get("ACAD_GROUP"),
        _ => None,
    }
}

fn group_names(document: &CadDocument) -> Vec<(String, Handle)> {
    match group_dictionary(document).and_then(|h| document.objects.get(&h)) {
        Some(ObjectType::Dictionary(dict)) => dict.entries.clone(),
        _ => Vec::new(),
    }
}

fn projected_groups(document: &CadDocument) -> Vec<serde_json::Value> {
    let mut names = group_names(document);
    names.sort_by(|a, b| a.0.cmp(&b.0));
    names.into_iter().filter_map(|(name, handle)| {
        match document.objects.get(&handle) {
            Some(ObjectType::Group(group)) => Some(serde_json::json!({
                "id": handle_id(handle.value()), "name": name,
                "memberIds": group.entities.iter().map(|h| handle_id(h.value())).collect::<Vec<_>>(),
                "unnamed": group.unnamed, "selectable": group.selectable,
                "description": group.description,
            })),
            _ => None,
        }
    }).collect()
}

// ---- the cores: every operation, natively testable ------------------------
fn normal_of(entity: &EntityType) -> [f64; 3] {
    let n = match entity {
        EntityType::Line(e) => e.normal,
        EntityType::LwPolyline(e) => e.normal,
        EntityType::Circle(e) => e.normal,
        EntityType::Arc(e) => e.normal,
        _ => Vector3::new(0.0, 0.0, 1.0),
    };
    [n.x, n.y, n.z]
}

impl ParsedDxf {
    fn create_block_core(&mut self, name: &str, base: [f64; 3], member_handles: &[String], layer_of_insert: &str) -> Result<String, Refusal> {
        if self.block_bases_unknown {
            return refuse("block_bases_unknown: the drawing's block bases are unknown after a binary load; save as ASCII DXF first");
        }
        let name = name.trim();
        if name.is_empty() || name.len() > 255 || name.starts_with('*')
            || name.bytes().any(|b| !(0x20..=0x7e).contains(&b) || b"<>/\\\":;?*|,=`".contains(&b)) {
            return refuse("block_name_invalid: use a new printable block name without reserved punctuation");
        }
        if self.inner.block_records.iter().any(|b| b.name.trim().eq_ignore_ascii_case(name)) {
            return refuse("block_name_exists: a block with this name already exists");
        }
        if !all_finite(&base) || base[2] != 0.0 { return refuse("block_base_invalid: the base must be a finite XY point"); }
        if !layer_of_insert.is_empty() && layer_of_insert != "0" { return refuse("block_insert_layer: the replacement INSERT must use layer 0"); }
        if member_handles.is_empty() || member_handles.len() > 60 { return refuse("block_member_count: select 1 to 60 committed entities"); }
        let mut handles = HashSet::new();
        let mut children = Vec::new();
        let defining: HashSet<Handle> = dimension_defining_handles(&self.inner).values().flatten().copied().collect();
        for id in member_handles {
            let entity = self.inner.entities().find(|e| handle_id(e.common().handle.value()) == *id)
                .ok_or_else(|| "block_member_missing: every member must exist in the drawing".to_string())?;
            let common = entity.common();
            if !handles.insert(common.handle) { return refuse("block_member_duplicate: each member must be distinct"); }
            if !matches!(entity, EntityType::Line(_) | EntityType::LwPolyline(_) | EntityType::Circle(_) | EntityType::Arc(_)) {
                return refuse("block_member_kind: only LINE, straight LWPOLYLINE, CIRCLE and ARC can become block children");
            }
            if self.inner.block_records.iter().any(|b| !b.is_model_space() && (b.handle == common.owner_handle || b.entity_handles.contains(&common.handle)))
                || common.entity_mode == Some(1) {
                return refuse("block_member_space: every member must be in model space");
            }
            if normal_of(entity) != [0.0, 0.0, 1.0] { return refuse("block_member_normal: every member must have normal +Z"); }
            if let EntityType::LwPolyline(poly) = entity {
                if poly.vertices.iter().any(|v| v.bulge != 0.0) { return refuse("block_member_bulge: polyline segments must be straight"); }
                if poly.constant_width != 0.0 || poly.vertices.iter().any(|v| v.start_width != 0.0 || v.end_width != 0.0) {
                    return refuse("block_member_width: polyline widths must be zero");
                }
            }
            if aci_of(&common.color) == 0 || linetype_of(entity).eq_ignore_ascii_case("ByBlock") || lineweight_of(entity) == -2 {
                return refuse("block_member_byblock: members must not use ByBlock properties");
            }
            if self.inner.objects.values().any(|o| matches!(o, ObjectType::Group(g) if g.entities.contains(&common.handle))) {
                return refuse("block_member_group: ungroup members before creating a block");
            }
            if defining.contains(&common.handle) {
                return refuse("block_member_dimension: a dimension defining entity cannot become a block child");
            }
            children.push(common.handle);
        }
        // All changes are staged. Even an internal insertion failure leaves self byte-identical.
        let mut next = self.inner.clone();
        let mut block = acadrust::tables::BlockRecord::new(name);
        block.handle = next.allocate_handle();
        block.block_entity_handle = next.allocate_handle();
        block.block_end_handle = next.allocate_handle();
        block.base_point = Vector3::new(base[0], base[1], base[2]);
        let owner = block.handle;
        next.block_records.add(block).map_err(|e| format!("block_create_failed:{e}"))?;
        for handle in children {
            let mut child = next.remove_entity(handle).ok_or_else(|| "block_member_missing".to_string())?;
            // remove_entity leaves membership for undo; reparenting must prune it.
            for old_owner in next.block_records.iter_mut() {
                old_owner.entity_handles.retain(|member| *member != handle);
            }
            child.common_mut().owner_handle = owner;
            child.common_mut().entity_mode = None;
            child.common_mut().reactors.clear();
            child.common_mut().xdictionary_handle = None;
            next.add_entity(child).map_err(|e| format!("block_create_failed:{e}"))?;
        }
        let insert = Insert::new(name, Vector3::new(base[0], base[1], base[2]));
        let inserted = next.add_entity(EntityType::Insert(insert)).map_err(|e| format!("block_create_failed:{e}"))?;
        self.inner = next;
        Ok(handle_id(inserted.value()))
    }

    fn create_group_core(&mut self, name: &str, member_indices: &[usize]) -> Result<String, Refusal> {
        let name = name.trim();
        if name.is_empty() || name.len() > 255
            || name.bytes().any(|b| !(0x20..=0x7e).contains(&b) || b"<>/\\\":;?*|,=`".contains(&b)) {
            return refuse("group_name_invalid");
        }
        let uppercase_name = name.to_ascii_uppercase();
        let name = uppercase_name.as_str();
        if self.group_names.iter().any(|(n, _)| n.eq_ignore_ascii_case(name)) {
            return refuse("group_name_exists");
        }
        let mut members = Vec::new();
        for &index in member_indices {
            let entity = self.editable_at(index).map_err(|_| "group_member_not_editable".to_string())?;
            if !editable(entity) && !matches!(entity, EntityType::Insert(_) | EntityType::Dimension(_)) {
                return refuse("group_member_not_editable");
            }
            let handle = entity.common().handle;
            if !members.contains(&handle) { members.push(handle); }
            let owner = entity.common().owner_handle;
            if self.inner.block_records.iter().any(|block| block.handle == owner && !block.is_model_space()) {
                return refuse("group_member_not_editable");
            }
        }
        if members.len() < 2 { return refuse("group_needs_two_members"); }
        let root_handle = self.inner.header.named_objects_dict_handle;
        if !matches!(self.inner.objects.get(&root_handle), Some(ObjectType::Dictionary(_))) {
            return refuse("group_dictionary_missing");
        }
        let dictionary_handle = match group_dictionary(&self.inner) {
            Some(handle) if matches!(self.inner.objects.get(&handle), Some(ObjectType::Dictionary(_))) => handle,
            Some(_) => return refuse("group_dictionary_missing"),
            None => {
                let handle = self.inner.allocate_handle();
                let mut dictionary = Dictionary::new();
                dictionary.handle = handle;
                dictionary.owner = root_handle;
                self.inner.objects.insert(handle, ObjectType::Dictionary(dictionary));
                if let Some(ObjectType::Dictionary(root)) = self.inner.objects.get_mut(&root_handle) {
                    root.add_entry("ACAD_GROUP", handle);
                }
                handle
            }
        };
        let handle = self.inner.allocate_handle();
        let mut group = Group::new(name);
        group.handle = handle;
        group.owner = dictionary_handle;
        group.entities = members.clone();
        self.inner.objects.insert(handle, ObjectType::Group(group));
        self.group_names.push((name.to_string(), handle));
        if let Some(ObjectType::Dictionary(dictionary)) = self.inner.objects.get_mut(&dictionary_handle) {
            dictionary.add_entry(name, handle);
        }
        for entity in self.inner.entities_mut() {
            if members.contains(&entity.common().handle) {
                entity.common_mut().reactors.push(handle);
            }
        }
        Ok(handle_id(handle.value()))
    }

    fn ungroup_core(&mut self, name: &str) -> Result<(), Refusal> {
        let handle = group_names(&self.inner).iter()
            .find(|(n, _)| n.eq_ignore_ascii_case(name.trim())).map(|(_, h)| *h)
            .ok_or_else(|| "group_not_found".to_string())?;
        if let Some(dict_handle) = group_dictionary(&self.inner) {
            if let Some(ObjectType::Dictionary(dictionary)) = self.inner.objects.get_mut(&dict_handle) {
                dictionary.entries.retain(|(_, h)| *h != handle);
                dictionary.hard_owner_entries.retain(|n| !n.eq_ignore_ascii_case(name.trim()));
            }
        }
        self.inner.objects.remove(&handle);
        self.group_names.retain(|(_, h)| *h != handle);
        for entity in self.inner.entities_mut() {
            entity.common_mut().reactors.retain(|h| *h != handle);
        }
        Ok(())
    }

    fn editable_at(&self, index: usize) -> Result<&EntityType, Refusal> {
        let entity = self.inner.entities().nth(index)
            .ok_or_else(|| "entity_index_out_of_range".to_string())?;
        if block_children(&self.inner).contains(&entity.common().handle) {
            return refuse("block_child_not_editable");
        }
        Ok(entity)
    }

    fn entity_mut(&mut self, index: usize) -> Result<&mut EntityType, Refusal> {
        let entity = self.editable_at(index)?;
        // W4g-7b-05c-2: every geometry verb goes through entity_mut (delete
        // does not, see delete_entity_core), so gating here refuses an
        // INSERT or a DIMENSION selection for all of them by kind.
        if matches!(entity, EntityType::Insert(_)) {
            return refuse(INSERT_NOT_EDITABLE);
        }
        if matches!(entity, EntityType::Dimension(_)) {
            return refuse(DIMENSION_NOT_EDITABLE);
        }
        if matches!(entity, EntityType::MultiLeader(_)) {
            return refuse(MLEADER_NOT_EDITABLE);
        }
        let handle = entity.common().handle;
        self.inner
            .entities_mut()
            .find(|entity| entity.common().handle == handle)
            .ok_or_else(|| "entity_handle_not_found".to_string())
    }

    fn delete_entity_core(&mut self, index: usize) -> Result<(), Refusal> {
        let (handle, is_editable) = {
            let entity = self.editable_at(index)?;
            // W4g-7b-04c/05c-2: a DIMENSION or an INSERT is not
            // geometry-editable (editable() stays false so the projection's
            // "editable" flag is honest) but its DELETE is allowed, so this
            // is the one place that admits either.
            (entity.common().handle, editable(entity) || matches!(entity, EntityType::Dimension(_) | EntityType::Insert(_) | EntityType::MultiLeader(_)))
        };
        if !is_editable {
            return refuse("entity_kind_not_editable");
        }
        self.remove_entity_and_repair_groups(handle)
    }

    fn remove_entity_and_repair_groups(&mut self, handle: Handle) -> Result<(), Refusal> {
        self.inner
            .remove_entity(handle)
            .map(|_| ())
            .ok_or_else(|| "entity_handle_not_found".to_string())?;
        let mut empty = Vec::new();
        for (name, group_handle) in group_names(&self.inner) {
            if let Some(ObjectType::Group(group)) = self.inner.objects.get_mut(&group_handle) {
                group.entities.retain(|h| *h != handle);
                if group.entities.is_empty() { empty.push(name); }
            }
        }
        for name in empty { self.ungroup_core(&name)?; }
        Ok(())
    }

    fn translate_entity_core(&mut self, index: usize, dx: f64, dy: f64) -> Result<(), Refusal> {
        if !dx.is_finite() || !dy.is_finite() {
            return refuse("delta_not_finite");
        }
        match self.entity_mut(index)? {
            EntityType::Line(line) => {
                line.start = Vector3::new(line.start.x + dx, line.start.y + dy, line.start.z);
                line.end = Vector3::new(line.end.x + dx, line.end.y + dy, line.end.z);
                Ok(())
            }
            EntityType::LwPolyline(poly) => {
                for v in poly.vertices.iter_mut() {
                    v.location = Vector2::new(v.location.x + dx, v.location.y + dy);
                }
                Ok(())
            }
            EntityType::Polyline2D(poly) => {
                for v in poly.vertices.iter_mut() {
                    v.location =
                        Vector3::new(v.location.x + dx, v.location.y + dy, v.location.z);
                }
                Ok(())
            }
            EntityType::Circle(c) => {
                c.center = Vector3::new(c.center.x + dx, c.center.y + dy, c.center.z);
                Ok(())
            }
            EntityType::Arc(a) => {
                a.center = Vector3::new(a.center.x + dx, a.center.y + dy, a.center.z);
                Ok(())
            }
            EntityType::Text(t) => {
                // W4g-5d, kimi on #1028: MOVE is the one verb hand-rolled as a
                // match, and a TEXT was editable everywhere but here, so a placed
                // text armed MOVE and the engine then refused it with a false
                // sentence. The insertion point moves; value, height and rotation
                // stay exactly what the drafter set.
                // The crate's own translate, never a hand move of one field: an
                // aligned or fit text (common in real DXF) carries a second
                // alignment point that moves with the insertion point.
                t.translate(Vector3::new(dx, dy, 0.0));
                Ok(())
            }
            // W4g-4b: a POINT moves its location, an ELLIPSE its centre (the
            // axis is relative to the centre and rides along unchanged).
            EntityType::Point(p) => {
                p.location = Vector3::new(p.location.x + dx, p.location.y + dy, p.location.z);
                Ok(())
            }
            EntityType::Ellipse(el) => {
                el.center = Vector3::new(el.center.x + dx, el.center.y + dy, el.center.z);
                Ok(())
            }
            _ => refuse("entity_kind_not_editable"),
        }
    }

    fn move_vertex_core(
        &mut self,
        index: usize,
        vertex_index: usize,
        dx: f64,
        dy: f64,
    ) -> Result<(), Refusal> {
        if !dx.is_finite() || !dy.is_finite() {
            return refuse("delta_not_finite");
        }
        match self.entity_mut(index)? {
            EntityType::Line(line) => match vertex_index {
                0 => {
                    line.start =
                        Vector3::new(line.start.x + dx, line.start.y + dy, line.start.z);
                    Ok(())
                }
                1 => {
                    line.end = Vector3::new(line.end.x + dx, line.end.y + dy, line.end.z);
                    Ok(())
                }
                _ => refuse("vertex_index_out_of_range"),
            },
            EntityType::LwPolyline(poly) => {
                let v = poly
                    .vertices
                    .get_mut(vertex_index)
                    .ok_or_else(|| "vertex_index_out_of_range".to_string())?;
                v.location = Vector2::new(v.location.x + dx, v.location.y + dy);
                Ok(())
            }
            EntityType::Polyline2D(poly) => {
                let v = poly
                    .vertices
                    .get_mut(vertex_index)
                    .ok_or_else(|| "vertex_index_out_of_range".to_string())?;
                v.location = Vector3::new(v.location.x + dx, v.location.y + dy, v.location.z);
                Ok(())
            }
            EntityType::Circle(c) if vertex_index == 0 => {
                c.center = Vector3::new(c.center.x + dx, c.center.y + dy, c.center.z);
                Ok(())
            }
            EntityType::Arc(a) if vertex_index == 0 => {
                a.center = Vector3::new(a.center.x + dx, a.center.y + dy, a.center.z);
                Ok(())
            }
            EntityType::Circle(_) | EntityType::Arc(_) => refuse("vertex_index_out_of_range"),
            _ => refuse("entity_kind_not_editable"),
        }
    }

    fn add_vertex_after_core(
        &mut self,
        index: usize,
        vertex_index: usize,
        x: f64,
        y: f64,
    ) -> Result<(), Refusal> {
        if !x.is_finite() || !y.is_finite() {
            return refuse("coordinate_not_finite");
        }
        match self.entity_mut(index)? {
            EntityType::LwPolyline(poly) => {
                if vertex_index >= poly.vertices.len() {
                    return refuse("vertex_index_out_of_range");
                }
                poly.vertices.insert(
                    vertex_index + 1,
                    acadrust::entities::LwVertex::from_coords(x, y),
                );
                Ok(())
            }
            EntityType::Polyline2D(poly) => {
                if vertex_index >= poly.vertices.len() {
                    return refuse("vertex_index_out_of_range");
                }
                poly.vertices.insert(
                    vertex_index + 1,
                    acadrust::entities::Vertex2D::new(Vector3::new(x, y, poly.elevation)),
                );
                Ok(())
            }
            EntityType::Line(_) => refuse("line_has_fixed_endpoints"),
            EntityType::Circle(_) | EntityType::Arc(_) => refuse("entity_kind_has_no_vertex_list"),
            _ => refuse("entity_kind_not_editable"),
        }
    }

    fn delete_vertex_core(&mut self, index: usize, vertex_index: usize) -> Result<(), Refusal> {
        match self.entity_mut(index)? {
            EntityType::LwPolyline(poly) => {
                if vertex_index >= poly.vertices.len() {
                    return refuse("vertex_index_out_of_range");
                }
                if poly.vertices.len() <= 2 {
                    return refuse("polyline_needs_two_vertices");
                }
                poly.vertices.remove(vertex_index);
                Ok(())
            }
            EntityType::Polyline2D(poly) => {
                if vertex_index >= poly.vertices.len() {
                    return refuse("vertex_index_out_of_range");
                }
                if poly.vertices.len() <= 2 {
                    return refuse("polyline_needs_two_vertices");
                }
                poly.vertices.remove(vertex_index);
                Ok(())
            }
            EntityType::Line(_) => refuse("line_has_fixed_endpoints"),
            EntityType::Circle(_) | EntityType::Arc(_) => refuse("entity_kind_has_no_vertex_list"),
            _ => refuse("entity_kind_not_editable"),
        }
    }

    // ----------------------------------------------------------------------
    // W4g-6: the two geometry primitives the intersection verbs (TRIM,
    // EXTEND, FILLET, CHAMFER) lower to. The browser computes the new shape
    // from the crossing; the engine only replaces an entity's OWN geometry,
    // so one verb is one batch of these plus the existing creates and
    // deletes. Both refuse BEFORE the document is touched.
    // ----------------------------------------------------------------------

    /// Replaces the geometry of the entity at `index` with the flat
    /// `[x0, y0, x1, y1, ...]` list: a LINE takes exactly two distinct
    /// points, a LWPOLYLINE / POLYLINE2D takes 2..MAX_CREATED_VERTICES and
    /// the closed flag. `bulges` is empty (every segment straight) or exactly
    /// one finite value per point (W4g-6d: a corner fillet writes one, and a
    /// caller that read the projection carries the others back unchanged);
    /// widths the old vertices carried go with them. Refuses before it
    /// touches the document.
    fn set_vertices_core(&mut self, index: usize, points: &[f64], closed: bool, bulges: &[f64]) -> Result<(), Refusal> {
        if points.len() % 2 != 0 {
            return refuse("points_not_pairs");
        }
        let count = points.len() / 2;
        if count < 2 {
            return refuse("polyline_needs_two_vertices");
        }
        if count > MAX_CREATED_VERTICES {
            return refuse("polyline_too_many_vertices");
        }
        if !all_finite(points) {
            return refuse("coordinate_not_finite");
        }
        if !bulges.is_empty() && bulges.len() != count {
            return refuse("bulges_not_per_vertex");
        }
        if !all_finite(bulges) {
            return refuse("bulge_not_finite");
        }
        let bulge_at = |i: usize| -> f64 { if bulges.is_empty() { 0.0 } else { bulges[i] } };
        match self.entity_mut(index)? {
            EntityType::Line(line) => {
                if count != 2 {
                    return refuse("line_has_fixed_endpoints");
                }
                if points[0] == points[2] && points[1] == points[3] {
                    return refuse("line_zero_length");
                }
                line.start = Vector3::new(points[0], points[1], line.start.z);
                line.end = Vector3::new(points[2], points[3], line.end.z);
                Ok(())
            }
            EntityType::LwPolyline(poly) => {
                poly.vertices = points
                    .chunks_exact(2)
                    .enumerate()
                    .map(|(i, p)| acadrust::entities::LwVertex::with_bulge(Vector2::new(p[0], p[1]), bulge_at(i)))
                    .collect();
                poly.is_closed = closed;
                Ok(())
            }
            EntityType::Polyline2D(poly) => {
                let elevation = poly.elevation;
                poly.vertices = points
                    .chunks_exact(2)
                    .enumerate()
                    .map(|(i, p)| {
                        let mut v = acadrust::entities::Vertex2D::new(Vector3::new(p[0], p[1], elevation));
                        v.bulge = bulge_at(i);
                        v
                    })
                    .collect();
                poly.flags.set_closed(closed);
                Ok(())
            }
            EntityType::Circle(_) | EntityType::Arc(_) => refuse("entity_kind_has_no_vertex_list"),
            _ => refuse("entity_kind_not_editable"),
        }
    }

    /// Replaces an ARC's centre, radius and sweep (degrees, counter-clockwise
    /// from start to end, as the DXF stores them). Refuses a non-positive
    /// radius, a zero sweep, and every other kind (a CIRCLE has no sweep to
    /// set; a TRIM of a circle deletes it and creates the arc).
    fn set_arc_core(
        &mut self,
        index: usize,
        cx: f64,
        cy: f64,
        radius: f64,
        start_deg: f64,
        end_deg: f64,
    ) -> Result<(), Refusal> {
        if !all_finite(&[cx, cy, radius, start_deg, end_deg]) {
            return refuse("coordinate_not_finite");
        }
        if radius <= 0.0 {
            return refuse("radius_not_positive");
        }
        if ((end_deg - start_deg) % 360.0).abs() < 1e-9 {
            return refuse("arc_sweep_zero");
        }
        match self.entity_mut(index)? {
            EntityType::Arc(a) => {
                a.center = Vector3::new(cx, cy, a.center.z);
                a.radius = radius;
                a.start_angle = start_deg.to_radians();
                a.end_angle = end_deg.to_radians();
                Ok(())
            }
            EntityType::Circle(_) => refuse("circle_has_no_sweep"),
            _ => refuse("entity_kind_not_an_arc"),
        }
    }
    fn set_entity_layer_core(&mut self, index: usize, layer: &str) -> Result<(), Refusal> {
        let trimmed = layer.trim();
        if trimmed.is_empty() {
            return refuse("layer_name_empty");
        }
        if trimmed.len() > 255 {
            return refuse("layer_name_too_long");
        }
        let entity = self.entity_mut(index)?;
        if !editable(&*entity) {
            return refuse("entity_kind_not_editable");
        }
        entity.common_mut().layer = trimmed.to_string();
        Ok(())
    }

    // W4g-7b-03c: a property (colour, linetype, lineweight) is not geometry.
    // The INSERT-not-editable refusal exists for the geometry verbs; a
    // reference's own colour/linetype/lineweight lives on its EntityCommon
    // exactly like any other entity's, so property ops accept it. A block
    // child is still refused: it is not independent model-space state.
    fn property_target_at(&self, index: usize) -> Result<&EntityType, Refusal> {
        let entity = self.inner.entities().nth(index)
            .ok_or_else(|| "entity_index_out_of_range".to_string())?;
        if block_children(&self.inner).contains(&entity.common().handle) {
            return refuse("block_child_not_editable");
        }
        if matches!(entity, EntityType::MultiLeader(_)) {
            return refuse(MLEADER_NOT_EDITABLE);
        }
        if !editable(entity) && !matches!(entity, EntityType::Insert(_)) {
            return refuse("entity_kind_not_editable");
        }
        Ok(entity)
    }

    fn property_target_mut(&mut self, index: usize) -> Result<&mut EntityType, Refusal> {
        let handle = self.property_target_at(index)?.common().handle;
        self.inner
            .entities_mut()
            .find(|entity| entity.common().handle == handle)
            .ok_or_else(|| "entity_handle_not_found".to_string())
    }

    /// aci: 0..=256 (256 ByLayer, 0 ByBlock, 1..=255 an AutoCAD Color Index).
    /// Setting an ACI replaces the whole `Color` value, which clears any
    /// true colour the entity carried, exactly as AutoCAD does.
    fn set_entity_color_core(&mut self, index: usize, aci: i32) -> Result<(), Refusal> {
        if !(0..=256).contains(&aci) {
            return refuse("color_index_out_of_range");
        }
        let entity = self.property_target_mut(index)?;
        entity.common_mut().color = Color::from_index(aci as i16);
        Ok(())
    }

    /// The name must be in the LTYPE table (case-insensitively); the table's
    /// own spelling is stored, never the caller's casing.
    fn set_entity_linetype_core(&mut self, index: usize, name: &str) -> Result<(), Refusal> {
        let resolved = self.inner.line_types.get(name)
            .map(|lt| lt.name.clone())
            .ok_or_else(|| format!("linetype_not_loaded:{name}"))?;
        let entity = self.property_target_mut(index)?;
        entity.common_mut().linetype = resolved;
        Ok(())
    }

    /// weight must be one of the crate's LineWeight enumeration: -3 Default,
    /// -2 ByBlock, -1 ByLayer, or one of the 24 standard 1/100mm values.
    fn set_entity_lineweight_core(&mut self, index: usize, weight: i32) -> Result<(), Refusal> {
        const VALID: [i32; 24] = [
            0, 5, 9, 13, 15, 18, 20, 25, 30, 35, 40, 50,
            53, 60, 70, 80, 90, 100, 106, 120, 140, 158, 200, 211,
        ];
        let resolved = match weight {
            -3 => LineWeight::Default,
            -2 => LineWeight::ByBlock,
            -1 => LineWeight::ByLayer,
            v if VALID.contains(&v) => LineWeight::Value(v as i16),
            _ => return refuse(&format!("lineweight_not_valid:{weight}")),
        };
        let entity = self.property_target_mut(index)?;
        entity.common_mut().line_weight = resolved;
        Ok(())
    }

    /// Adds a validated entity through the crate's own add_entity and returns
    /// its handle value (the identity that survives the write/re-parse).
    fn add_created(&mut self, mut entity: EntityType, layer: &str) -> Result<String, Refusal> {
        let layer = created_layer(layer)?;
        entity.common_mut().layer = layer;
        let handle = self
            .inner
            .add_entity(entity)
            .map_err(|e| format!("create_failed:{e}"))?;
        Ok(handle_id(handle.value()))
    }

    // ----------------------------------------------------------------------
    // W4g-4: the reference's Modify verbs the crate already carries. Every op
    // validates its operands BEFORE touching the document, refuses the same
    // kinds translate refuses, and names its refusal with the store's codes.
    // A verb that creates (copy, mirror-with-source, explode) hands the new
    // handle(s) back so the client can select what it drew.
    // ----------------------------------------------------------------------

    /// The entity at `index` as an owned clone with a NULL handle, so
    /// `add_entity` allocates a fresh one (a clone that kept its handle would
    /// overwrite the original in the document's map).
    fn cloned_for_create(&self, index: usize) -> Result<(EntityType, String), Refusal> {
        let entity = self.editable_at(index)?;
        // COPY / MIRROR-with-source refuse placed kinds with the same
        // sentences as entity_mut, before the generic editable-kind check.
        if matches!(entity, EntityType::Insert(_)) {
            return refuse(INSERT_NOT_EDITABLE);
        }
        if matches!(entity, EntityType::Dimension(_)) {
            return refuse(DIMENSION_NOT_EDITABLE);
        }
        if matches!(entity, EntityType::MultiLeader(_)) {
            return refuse(MLEADER_NOT_EDITABLE);
        }
        if !editable(entity) {
            return refuse("entity_kind_not_editable");
        }
        let layer = entity.common().layer.clone();
        let mut copy = entity.clone();
        copy.as_entity_mut().set_handle(Handle::NULL);
        copy.common_mut().reactors.clear();
        Ok((copy, layer))
    }

    /// COPY: a clone of the entity displaced by (dx, dy). The source stays.
    fn copy_entity_core(&mut self, index: usize, dx: f64, dy: f64) -> Result<String, Refusal> {
        if !all_finite(&[dx, dy]) {
            return refuse("delta_not_finite");
        }
        let (mut copy, layer) = self.cloned_for_create(index)?;
        copy.translate(Vector3::new(dx, dy, 0.0));
        self.add_created(copy, &layer)
    }

    /// MIRROR about the line (x1, y1)-(x2, y2). With `keep_source` the
    /// mirrored copy is a new entity (its handle is returned); without it the
    /// entity is mirrored in place and the answer is empty.
    fn mirror_entity_core(
        &mut self,
        index: usize,
        x1: f64,
        y1: f64,
        x2: f64,
        y2: f64,
        keep_source: bool,
    ) -> Result<String, Refusal> {
        if !all_finite(&[x1, y1, x2, y2]) {
            return refuse("coordinate_not_finite");
        }
        if x1 == x2 && y1 == y2 {
            return refuse("mirror_line_zero_length");
        }
        let transform = Transform::from_mirror_line(Vector3::new(x1, y1, 0.0), Vector3::new(x2, y2, 0.0));
        if keep_source {
            let (mut copy, layer) = self.cloned_for_create(index)?;
            copy.apply_mirror(&transform);
            return self.add_created(copy, &layer);
        }
        let entity = self.entity_mut(index)?;
        if !editable(entity) {
            return refuse("entity_kind_not_editable");
        }
        entity.apply_mirror(&transform);
        Ok(String::new())
    }

    /// ROTATE about the base point (cx, cy) by `deg` counter-clockwise.
    fn rotate_entity_core(&mut self, index: usize, cx: f64, cy: f64, deg: f64) -> Result<(), Refusal> {
        if !all_finite(&[cx, cy, deg]) {
            return refuse("coordinate_not_finite");
        }
        // translate(-c) then rotate then translate(+c): `then` applies self first.
        let transform = Transform::from_translation(Vector3::new(-cx, -cy, 0.0))
            .then(&Transform::from_rotation(Vector3::new(0.0, 0.0, 1.0), deg.to_radians()))
            .then(&Transform::from_translation(Vector3::new(cx, cy, 0.0)));
        let entity = self.entity_mut(index)?;
        if !editable(entity) {
            return refuse("entity_kind_not_editable");
        }
        entity.apply_transform(&transform);
        Ok(())
    }

    /// SCALE about the base point (cx, cy) by `factor` (strictly positive).
    fn scale_entity_core(&mut self, index: usize, cx: f64, cy: f64, factor: f64) -> Result<(), Refusal> {
        if !all_finite(&[cx, cy, factor]) {
            return refuse("coordinate_not_finite");
        }
        if factor <= 0.0 {
            return refuse("scale_not_positive");
        }
        let transform = Transform::from_scaling_with_origin(
            Vector3::new(factor, factor, factor),
            Vector3::new(cx, cy, 0.0),
        );
        let entity = self.entity_mut(index)?;
        if !editable(entity) {
            return refuse("entity_kind_not_editable");
        }
        entity.apply_transform(&transform);
        Ok(())
    }

    /// EXPLODE: a polyline becomes its segments (lines, arcs for bulges);
    /// the source is removed. Returns the new handles in document order.
    /// Only LWPOLYLINE and POLYLINE explode, and the kind is checked BEFORE
    /// the crate is asked: a LINE has nothing to explode into, and the
    /// crate's explode of a CIRCLE or ARC yields one "part" that is the same
    /// geometry (a circle comes back as a 0..2pi arc, which the writer emits
    /// as 50=0 / 51=360 and readers draw as nothing), so an empty-parts guard
    /// alone would let EXPLODE erase a circle (kimi on #1010). The parts are
    /// added before the source is removed, so a refused part never strands a
    /// document with its source gone.
    fn explode_entity_core(&mut self, index: usize) -> Result<Vec<String>, Refusal> {
        let (handle, layer, parts) = {
            let entity = self.editable_at(index)?;
            if matches!(entity, EntityType::Insert(_)) {
                return refuse(INSERT_NOT_EDITABLE);
            }
            if matches!(entity, EntityType::Dimension(_)) {
                return refuse(DIMENSION_NOT_EDITABLE);
            }
            if matches!(entity, EntityType::MultiLeader(_)) {
                return refuse(MLEADER_NOT_EDITABLE);
            }
            if !editable(entity) {
                return refuse("entity_kind_not_editable");
            }
            if !matches!(entity, EntityType::LwPolyline(_) | EntityType::Polyline2D(_)) {
                return refuse("entity_not_explodable");
            }
            let parts = entity.explode();
            if parts.is_empty() {
                return refuse("entity_not_explodable");
            }
            if parts.len() > MAX_CREATED_VERTICES {
                return refuse("explode_too_many_parts");
            }
            (entity.common().handle, entity.common().layer.clone(), parts)
        };
        let mut handles = Vec::with_capacity(parts.len());
        for mut part in parts {
            part.as_entity_mut().set_handle(Handle::NULL);
            part.common_mut().reactors.clear();
            handles.push(self.add_created(part, &layer)?);
        }
        self.remove_entity_and_repair_groups(handle)?;
        Ok(handles)
    }

    // ----------------------------------------------------------------------
    // W4g-5b: ARRAY. One engine operation, never N client-side copies. Every
    // applied edit re-parses the whole document and hands the bytes back (125
    // ms parse + 73 ms write on the 2,345-entity demo head), so a 10 x 10
    // array built as client copies would cost about 20 seconds and 100 undo
    // snapshots. Inside the engine it is one parse, one write, one snapshot.
    // The source is cloned ONCE and each copy clones that clone, so the cost
    // is linear in the copies and never re-walks the entity list.
    // ----------------------------------------------------------------------

    /// ARRAY, rectangular: `rows` x `cols` positions of the entity at
    /// `index`, spaced `row_gap` in y and `col_gap` in x. The source holds
    /// position (0, 0) and is not one of the copies, so a 2 x 3 array adds
    /// five entities. Refuses before the document is touched.
    fn array_rect_core(
        &mut self,
        index: usize,
        rows: usize,
        cols: usize,
        row_gap: f64,
        col_gap: f64,
    ) -> Result<Vec<String>, Refusal> {
        if rows == 0 || cols == 0 {
            return refuse("array_count_not_positive");
        }
        let positions = rows
            .checked_mul(cols)
            .ok_or_else(|| "array_too_many_copies".to_string())?;
        let copies = positions - 1;
        if copies == 0 {
            return refuse("array_count_not_positive");
        }
        if copies > MAX_ARRAY_COPIES {
            return refuse("array_too_many_copies");
        }
        if !all_finite(&[row_gap, col_gap]) {
            return refuse("coordinate_not_finite");
        }
        if row_gap == 0.0 && col_gap == 0.0 {
            // Every copy would land exactly on the source: a pile, not an array.
            return refuse("array_spacing_zero");
        }
        let (source, layer) = self.cloned_for_create(index)?;
        let mut handles = Vec::with_capacity(copies);
        for r in 0..rows {
            for c in 0..cols {
                if r == 0 && c == 0 {
                    continue;
                }
                let mut copy = source.clone();
                copy.translate(Vector3::new(col_gap * c as f64, row_gap * r as f64, 0.0));
                handles.push(self.add_created(copy, &layer)?);
            }
        }
        Ok(handles)
    }

    /// ARRAY, polar: `count` positions of the entity at `index` swept
    /// `total_deg` about (cx, cy). `count` counts the source, so a count of 4
    /// over 360 degrees adds three copies at 90-degree steps. The rotation
    /// composes exactly the way ROTATE does, so a polar array of an already
    /// rotated entity stays exact.
    fn array_polar_core(
        &mut self,
        index: usize,
        count: usize,
        cx: f64,
        cy: f64,
        total_deg: f64,
    ) -> Result<Vec<String>, Refusal> {
        if count < 2 {
            return refuse("array_count_not_positive");
        }
        if count - 1 > MAX_ARRAY_COPIES {
            return refuse("array_too_many_copies");
        }
        if !all_finite(&[cx, cy, total_deg]) {
            return refuse("coordinate_not_finite");
        }
        if total_deg == 0.0 {
            return refuse("array_sweep_zero");
        }
        // "Angle to fill" cannot fill more than one turn. Past 360 the sweep
        // wraps and copies start landing on the source: a count of 3 over 720
        // gives a step of 360, so BOTH copies sit exactly on the original as
        // invisible duplicates. That is the same fault array_spacing_zero
        // already refuses for the rectangular form, so it is refused here too
        // rather than silently drawn.
        if total_deg.abs() > 360.0 {
            return refuse("array_sweep_past_full_turn");
        }
        // A full turn shares its first and last position, so the step divides
        // by count there and by count - 1 for an open sweep.
        let full_turn = (total_deg.abs() - 360.0).abs() < 1e-9;
        let divisor = if full_turn { count } else { count - 1 } as f64;
        let step = total_deg / divisor;
        let (source, layer) = self.cloned_for_create(index)?;
        let mut handles = Vec::with_capacity(count - 1);
        for k in 1..count {
            let mut copy = source.clone();
            let transform = Transform::from_translation(Vector3::new(-cx, -cy, 0.0))
                .then(&Transform::from_rotation(
                    Vector3::new(0.0, 0.0, 1.0),
                    (step * k as f64).to_radians(),
                ))
                .then(&Transform::from_translation(Vector3::new(cx, cy, 0.0)));
            copy.apply_transform(&transform);
            handles.push(self.add_created(copy, &layer)?);
        }
        Ok(handles)
    }

    // ----------------------------------------------------------------------
    // W4g-5d: TEXT, single-line. The crate carries Text (value, insertion
    // point, height, rotation in radians) and the writer emits it, so the
    // engine's job is to validate and add; the drafter's height and angle are
    // the DXF's own fields, so a round trip keeps them exactly. The intake the
    // server keeps for a text carries layer, point and value only (DXF 1/10/
    // 20, not 40/50), so the projection below carries height and rotation
    // itself: what the browser drew is what the browser can read back.
    // ----------------------------------------------------------------------

    /// A bounded text leader using the document's MLEADERSTYLE values.
    fn create_mleader_core(
        &mut self, x1: f64, y1: f64, x2: f64, y2: f64,
        text: &str, style: &str, layer: &str,
    ) -> Result<String, Refusal> {
        if !all_finite(&[x1, y1, x2, y2]) { return refuse("coordinate_not_finite"); }
        let quantum = |v: f64| format!("{v:.3}").parse::<f64>().unwrap();
        if quantum(x1) == quantum(x2) && quantum(y1) == quantum(y2) {
            return refuse("the two points coincide at the drawing precision (0.001)");
        }
        if text.contains('^') { return refuse("text_caret"); }
        if text.is_empty() { return refuse("text_empty"); }
        if text.chars().count() > 256 { return refuse("text_too_long"); }
        if text.chars().any(|c| c.is_control()) { return refuse("text_control_character"); }
        let style = self.inner.objects.values().find_map(|object| match object {
            ObjectType::MultiLeaderStyle(s) if s.name.eq_ignore_ascii_case(style) => Some(s.clone()),
            _ => None,
        }).ok_or_else(|| "mleader_style_unknown".to_string())?;
        let text_location = Vector3::new(
            x2 + style.landing_distance + style.landing_gap, y2 + style.text_height / 2.0, 0.0);
        if !all_finite(&[text_location.x, text_location.y]) { return refuse("coordinate_not_finite"); }
        let landing = Vector3::new(x2, y2, 0.0);
        let mut m = MultiLeader::with_text(text, text_location, vec![Vector3::new(x1, y1, 0.0)]);
        m.common.handle = Handle::NULL;
        m.style_handle = Some(style.handle);
        m.text_style_handle = style.text_style_handle.or_else(|| self.inner.text_styles.iter()
            .find(|s| s.name.eq_ignore_ascii_case("Standard")).map(|s| s.handle));
        m.context.text_style_handle = m.text_style_handle;
        m.text_height = style.text_height;
        m.context.text_height = style.text_height;
        m.arrowhead_size = style.arrowhead_size;
        m.context.arrowhead_size = style.arrowhead_size;
        m.context.landing_gap = style.landing_gap;
        m.dogleg_length = style.landing_distance;
        m.context.leader_roots[0].connection_point = landing;
        m.context.leader_roots[0].landing_distance = style.landing_distance;
        m.context.leader_roots[0].direction = Vector3::new(1.0, 0.0, 0.0);
        m.context.leader_roots[0].lines[0].arrowhead_size = style.arrowhead_size;
        self.add_created(EntityType::MultiLeader(m), layer)
    }

    /// TEXT at (x, y), `height` drawing units tall, rotated `rotation_deg`
    /// counter-clockwise, reading `value`. Refuses before the document is
    /// touched: a non-finite number, a height that is not strictly positive,
    /// an empty value, a value over MAX_TEXT_CHARS, or a value carrying a
    /// control character (a DXF group value is one line; a newline inside it
    /// would split the record and the writer would emit a broken file).
    fn create_text_core(
        &mut self,
        x: f64,
        y: f64,
        height: f64,
        rotation_deg: f64,
        value: &str,
        layer: &str,
    ) -> Result<String, Refusal> {
        if !all_finite(&[x, y, height, rotation_deg]) {
            return refuse("coordinate_not_finite");
        }
        if height <= 0.0 {
            return refuse("text_height_not_positive");
        }
        let trimmed = value.trim_end_matches(['\r', '\n']);
        if trimmed.is_empty() {
            return refuse("text_empty");
        }
        if trimmed.chars().count() > MAX_TEXT_CHARS {
            return refuse("text_too_long");
        }
        if trimmed.chars().any(|c| c.is_control()) {
            return refuse("text_control_character");
        }
        let text = Text::with_value(trimmed, Vector3::new(x, y, 0.0))
            .with_height(height)
            .with_rotation(rotation_deg.to_radians());
        self.add_created(EntityType::Text(text), layer)
    }

    /// W4g-4b POINT: one location. Refuses a non-finite coordinate before
    /// the document is touched.
    fn create_point_core(&mut self, x: f64, y: f64, layer: &str) -> Result<String, Refusal> {
        if !all_finite(&[x, y]) {
            return refuse("coordinate_not_finite");
        }
        self.add_created(EntityType::Point(Point::from_coords(x, y, 0.0)), layer)
    }

    /// W4g-4b ELLIPSE: the centre, the major-axis endpoint RELATIVE to the
    /// centre (non-zero) and the minor-to-major ratio in (0, 1]; a full
    /// ellipse (the crate's default parameters). Refuses before it writes.
    fn create_ellipse_core(&mut self, cx: f64, cy: f64, ax: f64, ay: f64, ratio: f64, layer: &str) -> Result<String, Refusal> {
        if !all_finite(&[cx, cy, ax, ay, ratio]) {
            return refuse("coordinate_not_finite");
        }
        if ax == 0.0 && ay == 0.0 {
            return refuse("ellipse_axis_zero");
        }
        if ratio <= 0.0 || ratio > 1.0 {
            return refuse("ellipse_ratio_out_of_range");
        }
        let ellipse = Ellipse::from_center_axes(Vector3::new(cx, cy, 0.0), Vector3::new(ax, ay, 0.0), ratio);
        self.add_created(EntityType::Ellipse(ellipse), layer)
    }

    // ----------------------------------------------------------------------
    // W4g-7b-02c INSERT: a reference to an existing, complete block
    // definition. The block itself is never touched here — 01c owns reading
    // and cataloguing definitions; this is the one new way to ADD a
    // reference to one.
    // ----------------------------------------------------------------------

    /// INSERT of the block named `name` at (x, y), scaled (sx, sy, sz) and
    /// rotated `rotation_deg` counter-clockwise, on `layer`. Refuses BEFORE
    /// touching the document: a non-finite operand, a zero scale component,
    /// an empty or `*`-prefixed name (anonymous blocks are never insertable
    /// by name), a name with no matching record (case-insensitive, the
    /// 01c-d lookup rule), or a record `block_incomplete:<name>` for an
    /// unknown base, more than BLOCK_CHILD_CAP children, or an attribute
    /// definition (ATTDEF) among them. The Insert's `block_name` takes the
    /// CATALOGUE'S spelling, never the typed one, so a save that reads the
    /// name back always matches the definition it names.
    ///
    /// W4g-7b-02c-e: this check is NOT the whole of "complete". The
    /// catalogue's own `complete` flag (`block_catalogue`, 01c) is the
    /// stricter authority: it also marks a definition incomplete for a
    /// child kind this crate does not support projecting, which this check
    /// has no way to see (it counts and inspects flags, never child kinds).
    /// So the store (engineSession.js's buildCreatePayload) refuses on the
    /// catalogue's `complete`/`baseUnknown` fields before a typed name ever
    /// reaches this method, and this method's own refusal is the backstop
    /// for a caller (a script replay, a future direct wasm call) that
    /// skipped that check, never the primary gate a drafter sees.
    fn create_insert_core(
        &mut self,
        name: &str,
        x: f64,
        y: f64,
        rotation_deg: f64,
        sx: f64,
        sy: f64,
        sz: f64,
        layer: &str,
    ) -> Result<String, Refusal> {
        if !all_finite(&[x, y, rotation_deg, sx, sy, sz]) {
            return refuse("coordinate_not_finite");
        }
        if sx == 0.0 || sy == 0.0 || sz == 0.0 {
            return refuse("insert_scale_zero");
        }
        let trimmed = name.trim();
        if trimmed.is_empty() || trimmed.starts_with('*') {
            return refuse("insert_name_invalid");
        }
        let upper = trimmed.to_uppercase();
        // W4g-7b-02c-f: trimmed to trimmed. A record whose own DXF name
        // carries incidental whitespace still matches a typed name that
        // strips it, the same rule the store (engineSession.js) and the
        // ghost (pointPicking.js) apply on their side of this comparison.
        let block = match self.inner.block_records.iter()
            .find(|b| !b.is_model_space() && !b.is_paper_space() && b.name.trim().to_uppercase() == upper) {
            Some(block) => block,
            None => return refuse(&format!("block_not_defined:{trimmed}")),
        };
        let base_unknown = self.block_bases_unknown || self.unknown_block_bases.contains(&block.name);
        let incomplete = base_unknown
            || block.entity_handles.len() > BLOCK_CHILD_CAP
            || block.flags.has_attributes;
        if incomplete {
            return refuse(&format!("block_incomplete:{}", block.name));
        }
        let block_name = block.name.clone();
        let insert = Insert::new(block_name, Vector3::new(x, y, 0.0))
            .with_scale(sx, sy, sz)
            .with_rotation(rotation_deg.to_radians());
        self.add_created(EntityType::Insert(insert), layer)
    }

    fn create_line_core(&mut self, x1: f64, y1: f64, x2: f64, y2: f64, layer: &str) -> Result<String, Refusal> {
        if !all_finite(&[x1, y1, x2, y2]) {
            return refuse("coordinate_not_finite");
        }
        if x1 == x2 && y1 == y2 {
            return refuse("line_zero_length");
        }
        self.add_created(
            EntityType::Line(Line::from_coords(x1, y1, 0.0, x2, y2, 0.0)),
            layer,
        )
    }

    fn create_circle_core(&mut self, cx: f64, cy: f64, radius: f64, layer: &str) -> Result<String, Refusal> {
        if !all_finite(&[cx, cy, radius]) {
            return refuse("coordinate_not_finite");
        }
        if radius <= 0.0 {
            return refuse("radius_not_positive");
        }
        self.add_created(
            EntityType::Circle(Circle::from_coords(cx, cy, 0.0, radius)),
            layer,
        )
    }

    fn create_arc_core(
        &mut self,
        cx: f64,
        cy: f64,
        radius: f64,
        start_deg: f64,
        end_deg: f64,
        layer: &str,
    ) -> Result<String, Refusal> {
        if !all_finite(&[cx, cy, radius, start_deg, end_deg]) {
            return refuse("coordinate_not_finite");
        }
        if radius <= 0.0 {
            return refuse("radius_not_positive");
        }
        if ((end_deg - start_deg) % 360.0).abs() < 1e-9 {
            return refuse("arc_sweep_zero");
        }
        self.add_created(
            EntityType::Arc(ArcEntity::from_center_radius_angles(
                Vector3::new(cx, cy, 0.0),
                radius,
                start_deg.to_radians(),
                end_deg.to_radians(),
            )),
            layer,
        )
    }

    fn create_polyline_core(&mut self, points: &[f64], closed: bool, layer: &str, bulges: &[f64]) -> Result<String, Refusal> {
        if points.len() % 2 != 0 {
            return refuse("points_not_pairs");
        }
        let count = points.len() / 2;
        if count < 2 {
            return refuse("polyline_needs_two_vertices");
        }
        if count > MAX_CREATED_VERTICES {
            return refuse("polyline_too_many_vertices");
        }
        if !all_finite(points) {
            return refuse("coordinate_not_finite");
        }
        if !bulges.is_empty() && bulges.len() != count {
            return refuse("bulges_not_per_vertex");
        }
        if !all_finite(bulges) {
            return refuse("bulge_not_finite");
        }
        let vertices: Vec<Vector2> = points
            .chunks_exact(2)
            .map(|p| Vector2::new(p[0], p[1]))
            .collect();
        let mut poly = LwPolyline::from_points(vertices);
        poly.is_closed = closed;
        if !bulges.is_empty() {
            poly.vertices = points
                .chunks_exact(2)
                .enumerate()
                .map(|(i, p)| acadrust::entities::LwVertex::with_bulge(Vector2::new(p[0], p[1]), bulges[i]))
                .collect();
        }
        self.add_created(EntityType::LwPolyline(poly), layer)
    }

    /// W4g-7b-04c: LINEAR / ALIGNED dimension creation. Refuses BEFORE the
    /// document is touched, in this order: a non-finite operand
    /// (`coordinate_not_finite`), coincident definition points
    /// (`dimension_points_coincide`), a rotation on ALIGNED
    /// (`dimension_rotation_not_allowed`), a style absent from the DIMSTYLE
    /// table by case-insensitive lookup (`dimstyle_not_loaded:<name>`; the
    /// table's own spelling is stored), an unknown dimtype
    /// (`dimension_type_not_supported`).
    ///
    /// `base.definition_point` is set to the DIMLINE point the caller gave:
    /// dimension.rs's writer (`write_dimension_base`) emits DXF group 10
    /// from `base.definition_point`, never from the per-variant
    /// `definition_point` field the Linear/Aligned structs also carry —
    /// that field is written nowhere, so setting it would be silently lost.
    /// `block_name` is left at its default empty string (`DimensionBase::new`);
    /// the writer emits group 2 unconditionally (`write_string(2,
    /// &base.block_name)`), so an empty block_name round-trips as an empty
    /// value rather than a fabricated block name — AutoCAD supplies the
    /// anonymous block itself.
    fn create_dimension_core(
        &mut self,
        dimtype: &str,
        x1: f64,
        y1: f64,
        x2: f64,
        y2: f64,
        dx: f64,
        dy: f64,
        rotation_deg: f64,
        style: &str,
        layer: &str,
    ) -> Result<String, Refusal> {
        if !all_finite(&[x1, y1, x2, y2, dx, dy, rotation_deg]) {
            return refuse("coordinate_not_finite");
        }
        if x1 == x2 && y1 == y2 {
            return refuse("dimension_points_coincide");
        }
        // W4g-7b-04c-3 F3b: the crate is the boundary (the store already
        // normalizes before it posts), so a raw rotation reaching this call
        // directly (-90, 450, 1e9) is normalized here too, before the F3a
        // projection test below and before the LINEAR constructor, so the
        // projection's own rotationDeg reads back normalized.
        let rotation_deg = rotation_deg.rem_euclid(360.0);
        // W4g-7b-04c-3 F3a: a LINEAR whose rotation is perpendicular to
        // def1-def2 projects both definition points onto the same foot, a
        // zero-length dimension line; refused before any write, the same way
        // a coincident pair already is.
        if dimtype == "LINEAR" {
            let rad = rotation_deg.to_radians();
            let projection = (x2 - x1) * rad.cos() + (y2 - y1) * rad.sin();
            if projection.abs() < 1e-9 {
                return refuse("dimension_projection_zero");
            }
        }
        if dimtype == "ALIGNED" && rotation_deg != 0.0 {
            return refuse("dimension_rotation_not_allowed");
        }
        let trimmed_style = style.trim();
        let resolved_style = self.inner.dim_styles.get(trimmed_style)
            .map(|s| s.name.clone())
            .ok_or_else(|| format!("dimstyle_not_loaded:{trimmed_style}"))?;
        let p1 = Vector3::new(x1, y1, 0.0);
        let p2 = Vector3::new(x2, y2, 0.0);
        let dimline = Vector3::new(dx, dy, 0.0);
        let entity = match dimtype {
            "LINEAR" => {
                let mut dim = DimensionLinear::rotated(p1, p2, rotation_deg.to_radians());
                dim.base.definition_point = dimline;
                dim.base.style_name = resolved_style;
                EntityType::Dimension(Dimension::Linear(dim))
            }
            "ALIGNED" => {
                let mut dim = DimensionAligned::new(p1, p2);
                dim.base.definition_point = dimline;
                dim.base.style_name = resolved_style;
                EntityType::Dimension(Dimension::Aligned(dim))
            }
            _ => return refuse("dimension_type_not_supported"),
        };
        self.add_created(entity, layer)
    }
}

// ---- the exported boundary: thin, JsValue only here -------------------------
#[wasm_bindgen]
impl ParsedDxf {
    #[wasm_bindgen(js_name = createGroup)]
    pub fn create_group(&mut self, name: &str, ids: Vec<String>) -> Result<String, JsValue> {
        let indices = ids.iter().map(|id| {
            self.inner.entities().position(|e| handle_id(e.common().handle.value()) == *id)
                .ok_or_else(|| "group_member_not_editable".to_string())
        }).collect::<Result<Vec<_>, _>>().map_err(js_err)?;
        self.create_group_core(name, &indices).map_err(js_err)
    }

    #[wasm_bindgen(js_name = createBlock)]
    pub fn create_block(&mut self, name: &str, bx: f64, by: f64, handles_json: &str) -> Result<String, JsValue> {
        let handles: Vec<String> = serde_json::from_str(handles_json)
            .map_err(|_| JsValue::from_str("block_members_invalid: members must be entity handles"))?;
        self.create_block_core(name, [bx, by, 0.0], &handles, "0").map_err(js_err)
    }

    #[wasm_bindgen(js_name = ungroup)]
    pub fn ungroup(&mut self, name: &str) -> Result<(), JsValue> {
        self.ungroup_core(name).map_err(js_err)
    }

    /// Mirrors the stand-in's `parsed.entities` array: one
    /// `{type, layer, start, end}` object per LINE entity, in document order.
    #[wasm_bindgen(getter)]
    pub fn entities(&self) -> Result<JsValue, JsValue> {
        let children = block_children(&self.inner);
        let list: Vec<serde_json::Value> = self
            .inner
            .entities()
            .filter(|e| !children.contains(&e.common().handle))
            .filter_map(|e| match e {
                EntityType::Line(line) => Some(serde_json::json!({
                    "type": "LINE",
                    "layer": line.layer().to_string(),
                    "start": [line.start.x, line.start.y, line.start.z],
                    "end": [line.end.x, line.end.y, line.end.z],
                })),
                EntityType::MultiLeader(_) => Some(entity_record(0, e, false)),
                _ => None,
            })
            .collect();
        // json_compatible(): plain JS objects, not Map instances (see module
        // doc, correction 6) — the shape bindings.mjs's stand-in and the
        // day-2 test's .toEqual({...}) assertions both require.
        list.serialize(&serde_wasm_bindgen::Serializer::json_compatible())
            .map_err(|e| JsValue::from_str(&e.to_string()))
    }

    /// Model-space projection with read-only INSERT references. Keep the array
    /// API (map/find consumers) and attach the additive block catalogue to it.
    #[wasm_bindgen(js_name = editableEntities)]
    pub fn editable_entities(&self) -> Result<JsValue, JsValue> {
        let serializer = serde_wasm_bindgen::Serializer::json_compatible();
        let list = projected_entities(&self.inner).serialize(&serializer)
            .map_err(|e| JsValue::from_str(&e.to_string()))?;
        let groups = projected_groups(&self.inner).serialize(&serializer)
            .map_err(|e| JsValue::from_str(&e.to_string()))?;
        if !set_projection_field(&list, &JsValue::from_str("groups"), &groups) {
            return Err(JsValue::from_str("group_projection_failed"));
        }
        let blocks = block_catalogue(&self.inner, self.block_bases_unknown, &self.unknown_block_bases).serialize(&serializer)
            .map_err(|e| JsValue::from_str(&e.to_string()))?;
        if !set_projection_field(&list, &JsValue::from_str("blocks"), &blocks) {
            return Err(JsValue::from_str("block_catalogue_projection_failed"));
        }
        let (names, truncated) = linetypes_catalogue(&self.inner);
        let linetypes = names.serialize(&serializer)
            .map_err(|e| JsValue::from_str(&e.to_string()))?;
        if !set_projection_field(&list, &JsValue::from_str("linetypes"), &linetypes) {
            return Err(JsValue::from_str("linetype_catalogue_projection_failed"));
        }
        if !set_projection_field(&list, &JsValue::from_str("linetypesTruncated"), &JsValue::from_bool(truncated)) {
            return Err(JsValue::from_str("linetype_catalogue_projection_failed"));
        }
        let mlstyles = mlstyles_catalogue(&self.inner, &self.mlstyle_segments).serialize(&serializer)
            .map_err(|e| JsValue::from_str(&e.to_string()))?;
        if !set_projection_field(&list, &JsValue::from_str("mlstyles"), &mlstyles) {
            return Err(JsValue::from_str("mleader_style_catalogue_projection_failed"));
        }
        // W4g-7b-04c: the DIMSTYLE catalogue, beside blocks/linetypes.
        let dimstyles = dimstyles_catalogue(&self.inner).serialize(&serializer)
            .map_err(|e| JsValue::from_str(&e.to_string()))?;
        if !set_projection_field(&list, &JsValue::from_str("dimstyles"), &dimstyles) {
            return Err(JsValue::from_str("dimstyle_catalogue_projection_failed"));
        }
        Ok(list)
    }

    /// Status of the last wrapper write, false until a write has succeeded.
    #[wasm_bindgen(getter, js_name = blockBasePatched)]
    pub fn block_base_patched(&self) -> bool {
        self.block_base_patched.get()
    }

    #[wasm_bindgen(getter, js_name = blockBasesUnknown)]
    pub fn block_bases_unknown(&self) -> bool {
        self.block_bases_unknown
    }

    #[wasm_bindgen(setter, js_name = blockBasesUnknown)]
    pub fn set_block_bases_unknown(&mut self, unknown: bool) {
        self.block_bases_unknown = unknown;
    }

    // Writing cannot recover an unmatched marker or a binary input's base.
    #[wasm_bindgen(js_name = inheritBlockBaseUnknowns)]
    pub fn inherit_block_base_unknowns(&mut self, previous: &ParsedDxf) {
        self.block_bases_unknown |= previous.block_bases_unknown;
        self.unknown_block_bases.extend(previous.unknown_block_bases.iter().cloned());
    }

    // Source counts survive the crate's lossy three-valued enum round trip.
    // Newly scanned handles remain; an already known source value wins.
    #[wasm_bindgen(js_name = inheritMlstyleSegments)]
    pub fn inherit_mlstyle_segments(&mut self, previous: &ParsedDxf) {
        self.mlstyle_segments.extend(previous.mlstyle_segments.iter().map(|(h, n)| (*h, *n)));
    }

    /// Deletes the entity at `index` (current document order) via the
    /// crate's own remove_entity(handle). Refuses out-of-range and
    /// non-editable kinds BEFORE touching the document.
    #[wasm_bindgen(js_name = deleteEntity)]
    pub fn delete_entity(&mut self, index: usize) -> Result<(), JsValue> {
        self.delete_entity_core(index).map_err(js_err)
    }

    /// Translates every vertex of the entity at `index` by (dx, dy).
    #[wasm_bindgen(js_name = translateEntity)]
    pub fn translate_entity(&mut self, index: usize, dx: f64, dy: f64) -> Result<(), JsValue> {
        self.translate_entity_core(index, dx, dy).map_err(js_err)
    }

    /// Moves ONE vertex of the entity at `index` by (dx, dy). For a LINE,
    /// vertex 0 is the start and vertex 1 the end; for a CIRCLE or ARC,
    /// vertex 0 is the centre.
    #[wasm_bindgen(js_name = moveVertex)]
    pub fn move_vertex(
        &mut self,
        index: usize,
        vertex_index: usize,
        dx: f64,
        dy: f64,
    ) -> Result<(), JsValue> {
        self.move_vertex_core(index, vertex_index, dx, dy).map_err(js_err)
    }

    /// Inserts a vertex AFTER `vertex_index` on a polyline at (x, y).
    /// Refused for LINE (a line has exactly two endpoints by definition) and
    /// for CIRCLE / ARC (a centre is not a vertex list).
    #[wasm_bindgen(js_name = addVertexAfter)]
    pub fn add_vertex_after(
        &mut self,
        index: usize,
        vertex_index: usize,
        x: f64,
        y: f64,
    ) -> Result<(), JsValue> {
        self.add_vertex_after_core(index, vertex_index, x, y).map_err(js_err)
    }

    /// Deletes one vertex of a polyline. Refused when it would leave fewer
    /// than two vertices (that is entity deletion, an explicit separate op),
    /// and refused for LINE for the same fixed-endpoints reason as add.
    #[wasm_bindgen(js_name = deleteVertex)]
    pub fn delete_vertex(&mut self, index: usize, vertex_index: usize) -> Result<(), JsValue> {
        self.delete_vertex_core(index, vertex_index).map_err(js_err)
    }

    /// Reassigns the entity at `index` to `layer` via the crate's own
    /// EntityCommon. Bounded name; the empty string is refused (DXF layer
    /// names cannot be empty, and an empty write would be silent data rot).
    #[wasm_bindgen(js_name = setEntityLayer)]
    pub fn set_entity_layer(&mut self, index: usize, layer: &str) -> Result<(), JsValue> {
        self.set_entity_layer_core(index, layer).map_err(js_err)
    }

    /// W4g-7b-03c: sets the ACI colour index (0..=256; 256 ByLayer, 0
    /// ByBlock). Accepts INSERT references, not only geometry-editable kinds.
    #[wasm_bindgen(js_name = setEntityColor)]
    pub fn set_entity_color(&mut self, index: usize, aci: i32) -> Result<(), JsValue> {
        self.set_entity_color_core(index, aci).map_err(js_err)
    }

    /// W4g-7b-03c: sets the linetype by name (must be loaded in the LTYPE
    /// table, case-insensitively). Accepts INSERT references.
    #[wasm_bindgen(js_name = setEntityLinetype)]
    pub fn set_entity_linetype(&mut self, index: usize, name: &str) -> Result<(), JsValue> {
        self.set_entity_linetype_core(index, name).map_err(js_err)
    }

    /// W4g-7b-03c: sets the lineweight (the crate's 1/100mm enumeration, or
    /// -1 ByLayer / -2 ByBlock / -3 Default). Accepts INSERT references.
    #[wasm_bindgen(js_name = setEntityLineweight)]
    pub fn set_entity_lineweight(&mut self, index: usize, weight: i32) -> Result<(), JsValue> {
        self.set_entity_lineweight_core(index, weight).map_err(js_err)
    }

    /// W4g-4 COPY: a displaced clone of the entity at `index`; returns the
    /// new entity's handle. Refuses a non-finite delta and read-only kinds.
    /// W4g-4b: a POINT at (x, y) on `layer`; refuses before it writes.
    #[wasm_bindgen(js_name = createPoint)]
    pub fn create_point(&mut self, x: f64, y: f64, layer: &str) -> Result<String, JsValue> {
        self.create_point_core(x, y, layer).map_err(js_err)
    }

    /// W4g-4b: an ELLIPSE at (cx, cy) with the major-axis endpoint (ax, ay)
    /// relative to the centre and the minor-to-major ratio; refuses before it
    /// writes.
    #[wasm_bindgen(js_name = createEllipse)]
    pub fn create_ellipse(&mut self, cx: f64, cy: f64, ax: f64, ay: f64, ratio: f64, layer: &str) -> Result<String, JsValue> {
        self.create_ellipse_core(cx, cy, ax, ay, ratio, layer).map_err(js_err)
    }

    /// W4g-7b-02c: INSERT of the block named `name` at (x, y), scaled
    /// (sx, sy, sz) and rotated `rotation_deg`, on `layer`. Refuses a
    /// non-finite operand, a zero scale, an invalid or undefined name, or
    /// an incomplete definition, before it writes.
    #[wasm_bindgen(js_name = createInsert)]
    pub fn create_insert(&mut self, name: &str, x: f64, y: f64, rotation_deg: f64, sx: f64, sy: f64, sz: f64, layer: &str) -> Result<String, JsValue> {
        self.create_insert_core(name, x, y, rotation_deg, sx, sy, sz, layer).map_err(js_err)
    }

    /// W4g-6: replaces the geometry of a LINE (two points) or a polyline
    /// (2..MAX_CREATED_VERTICES points plus the closed flag) from a flat
    /// `[x0, y0, x1, y1, ...]` list; `bulges` empty or one per point
    /// (W4g-6d). Refuses before it writes.
    #[wasm_bindgen(js_name = setVertices)]
    pub fn set_vertices(&mut self, index: usize, points: &[f64], closed: bool, bulges: &[f64]) -> Result<(), JsValue> {
        self.set_vertices_core(index, points, closed, bulges).map_err(js_err)
    }

    /// W4g-6: replaces an ARC's centre, radius and sweep (degrees). Refuses
    /// before it writes.
    #[wasm_bindgen(js_name = setArc)]
    pub fn set_arc(
        &mut self,
        index: usize,
        cx: f64,
        cy: f64,
        radius: f64,
        start_deg: f64,
        end_deg: f64,
    ) -> Result<(), JsValue> {
        self.set_arc_core(index, cx, cy, radius, start_deg, end_deg).map_err(js_err)
    }

    #[wasm_bindgen(js_name = copyEntity)]
    pub fn copy_entity(&mut self, index: usize, dx: f64, dy: f64) -> Result<String, JsValue> {
        self.copy_entity_core(index, dx, dy).map_err(js_err)
    }

    /// W4g-4 MIRROR about the line (x1, y1)-(x2, y2). `keep_source` true
    /// returns the mirrored copy's handle; false mirrors in place and
    /// returns an empty string. Refuses a zero-length line.
    #[wasm_bindgen(js_name = mirrorEntity)]
    pub fn mirror_entity(
        &mut self,
        index: usize,
        x1: f64,
        y1: f64,
        x2: f64,
        y2: f64,
        keep_source: bool,
    ) -> Result<String, JsValue> {
        self.mirror_entity_core(index, x1, y1, x2, y2, keep_source).map_err(js_err)
    }

    /// W4g-4 ROTATE about (cx, cy) by `deg` counter-clockwise.
    #[wasm_bindgen(js_name = rotateEntity)]
    pub fn rotate_entity(&mut self, index: usize, cx: f64, cy: f64, deg: f64) -> Result<(), JsValue> {
        self.rotate_entity_core(index, cx, cy, deg).map_err(js_err)
    }

    /// W4g-4 SCALE about (cx, cy) by a strictly positive `factor`.
    #[wasm_bindgen(js_name = scaleEntity)]
    pub fn scale_entity(&mut self, index: usize, cx: f64, cy: f64, factor: f64) -> Result<(), JsValue> {
        self.scale_entity_core(index, cx, cy, factor).map_err(js_err)
    }

    /// W4g-4 EXPLODE: the entity's segments as new entities (handles in
    /// document order); the source is removed. Refused for kinds with
    /// nothing to explode into.
    #[wasm_bindgen(js_name = explodeEntity)]
    pub fn explode_entity(&mut self, index: usize) -> Result<JsValue, JsValue> {
        let handles = self.explode_entity_core(index).map_err(js_err)?;
        handles
            .serialize(&serde_wasm_bindgen::Serializer::json_compatible())
            .map_err(|e| JsValue::from_str(&e.to_string()))
    }


    /// ARRAY, rectangular: rows x cols positions spaced row_gap in y and
    /// col_gap in x, the source holding the first. One engine operation for
    /// the whole array, so one parse, one write and one undo step. Returns
    /// the new handles in the order they were added.
    #[wasm_bindgen(js_name = arrayRectEntity)]
    pub fn array_rect_entity(
        &mut self,
        index: usize,
        rows: usize,
        cols: usize,
        row_gap: f64,
        col_gap: f64,
    ) -> Result<JsValue, JsValue> {
        let handles = self
            .array_rect_core(index, rows, cols, row_gap, col_gap)
            .map_err(js_err)?;
        handles
            .serialize(&serde_wasm_bindgen::Serializer::json_compatible())
            .map_err(|e| JsValue::from_str(&e.to_string()))
    }

    /// ARRAY, polar: count positions swept total_deg about (cx, cy), the
    /// source holding the first. A full turn shares its first and last
    /// position, so its step divides by count rather than count - 1.
    #[wasm_bindgen(js_name = arrayPolarEntity)]
    pub fn array_polar_entity(
        &mut self,
        index: usize,
        count: usize,
        cx: f64,
        cy: f64,
        total_deg: f64,
    ) -> Result<JsValue, JsValue> {
        let handles = self
            .array_polar_core(index, count, cx, cy, total_deg)
            .map_err(js_err)?;
        handles
            .serialize(&serde_wasm_bindgen::Serializer::json_compatible())
            .map_err(|e| JsValue::from_str(&e.to_string()))
    }

    /// TEXT at (x, y), `height` tall, rotated `rotation_deg`, reading `value`
    /// on `layer` (empty = `0`). Refuses a non-finite number, a height that is
    /// not positive, an empty or over-long value and any control character.
    #[wasm_bindgen(js_name = createText)]
    pub fn create_text(&mut self, x: f64, y: f64, height: f64, rotation_deg: f64, value: &str, layer: &str) -> Result<String, JsValue> {
        self.create_text_core(x, y, height, rotation_deg, value, layer).map_err(js_err)
    }

    /// Creates a LINE from (x1, y1) to (x2, y2) on `layer` (empty = `0`).
    /// Refuses non-finite coordinates and a zero-length line. Returns the
    /// new entity's handle.
    #[wasm_bindgen(js_name = createLine)]
    pub fn create_line(&mut self, x1: f64, y1: f64, x2: f64, y2: f64, layer: &str) -> Result<String, JsValue> {
        self.create_line_core(x1, y1, x2, y2, layer).map_err(js_err)
    }

    /// Creates a CIRCLE at (cx, cy) with `radius` on `layer`. Refuses a
    /// non-finite centre or a radius that is not strictly positive.
    #[wasm_bindgen(js_name = createCircle)]
    pub fn create_circle(&mut self, cx: f64, cy: f64, radius: f64, layer: &str) -> Result<String, JsValue> {
        self.create_circle_core(cx, cy, radius, layer).map_err(js_err)
    }

    /// Creates an ARC at (cx, cy) with `radius` from `start_deg` to `end_deg`
    /// (degrees, counter-clockwise, the DXF convention) on `layer`. Refuses a
    /// non-finite input, a non-positive radius and a zero sweep.
    #[wasm_bindgen(js_name = createArc)]
    pub fn create_arc(
        &mut self,
        cx: f64,
        cy: f64,
        radius: f64,
        start_deg: f64,
        end_deg: f64,
        layer: &str,
    ) -> Result<String, JsValue> {
        self.create_arc_core(cx, cy, radius, start_deg, end_deg, layer).map_err(js_err)
    }

    /// Creates an LWPOLYLINE from a flat `[x0, y0, x1, y1, ...]` point list on
    /// `layer`. Refuses an odd list, fewer than two points, more than
    /// MAX_CREATED_VERTICES points (bounded allocation), or any non-finite
    /// coordinate — all before the document is touched.
    #[wasm_bindgen(js_name = createPolyline)]
    pub fn create_polyline(&mut self, points: &[f64], closed: bool, layer: &str, bulges: &[f64]) -> Result<String, JsValue> {
        self.create_polyline_core(points, closed, layer, bulges).map_err(js_err)
    }

    /// Creates a LINEAR or ALIGNED DIMENSION from (x1, y1) to (x2, y2), with
    /// the dimension line through (dx, dy); `rotation_deg` applies to LINEAR
    /// only (ALIGNED refuses a non-zero value). See create_dimension_core
    /// for the exact refusal order.
    #[wasm_bindgen(js_name = createMleader)]
    pub fn create_mleader(
        &mut self, x1: f64, y1: f64, x2: f64, y2: f64,
        text: &str, style: &str, layer: &str,
    ) -> Result<String, JsValue> {
        self.create_mleader_core(x1, y1, x2, y2, text, style, layer).map_err(js_err)
    }

    #[wasm_bindgen(js_name = createDimension)]
    #[allow(clippy::too_many_arguments)]
    pub fn create_dimension(
        &mut self,
        dimtype: &str,
        x1: f64,
        y1: f64,
        x2: f64,
        y2: f64,
        dx: f64,
        dy: f64,
        rotation_deg: f64,
        style: &str,
        layer: &str,
    ) -> Result<String, JsValue> {
        self.create_dimension_core(dimtype, x1, y1, x2, y2, dx, dy, rotation_deg, style, layer).map_err(js_err)
    }
}

/// Parses DXF bytes into a document handle. Returns a JS-thrown error
/// (never panics across the wasm/JS boundary) on a malformed document —
/// mirrors the boundary's own "validate or drop, never throw into the UI"
/// contract at the message layer one level up.
///
/// `bytes.to_vec()` gives `Cursor<Vec<u8>>` an owned, effectively-`'static`
/// buffer, satisfying `DxfReader::from_reader`'s `R: Read + Seek + 'static`
/// bound — a borrowed `Cursor<&[u8]>` over the wasm-bindgen `&[u8]` argument
/// does not satisfy `'static` and does not compile.
#[wasm_bindgen(js_name = parseDxf)]
pub fn parse_dxf(bytes: &[u8]) -> Result<ParsedDxf, JsValue> {
    parse_dxf_core(bytes).map_err(js_err)
}

/// Re-serializes a parsed document via `DxfWriter::new(&doc).write_to_vec()`.
/// The caller (the worker's message handler) parses the output again and
/// compares against the input for the byte/entity comparison the day-2
/// oracle asks for.
#[wasm_bindgen(js_name = writeDxf)]
pub fn write_dxf(doc: &ParsedDxf) -> Result<Vec<u8>, JsValue> {
    let bytes = DxfWriter::new(&doc.inner)
        .write_to_vec()
        .map_err(|e| JsValue::from_str(&e.to_string()))?;
    let (bytes, patched) = if doc.block_bases_unknown { (bytes, false) } else { patch_block_bases(&doc.inner, bytes) };
    doc.block_base_patched.set(patched && doc.unknown_block_bases.is_empty());
    Ok(bytes)
}

/// Byte-for-byte comparison, the stand-in's `bytesEqual` twin.
#[wasm_bindgen(js_name = bytesEqual)]
pub fn bytes_equal(a: &[u8], b: &[u8]) -> bool {
    a == b
}

// ---------------------------------------------------------------------------
// Native tests (`cargo test` in this crate; it builds as an rlib too).
//
// Writer spike (W4d, Draw group): a PROGRAMMATICALLY CREATED entity must
// survive add_entity -> write -> re-parse. The upstream crate's own round-trip
// tests only ever round-trip PARSED files, so "the document model accepts a
// new entity" said nothing about whether the WRITER serializes one that never
// came from a reader. These tests are that proof, on the always-present layer
// `0` so a missing LAYER record cannot be the failure, then the wrapper's own
// create surface and its refusals, through the cores (no JsValue off wasm32).
// ---------------------------------------------------------------------------
#[cfg(test)]
mod created_entity_roundtrip {
    use super::*;

    #[test]
    fn create_block_keeps_coordinates_and_refuses_without_changing_bytes() {
        let mut doc = empty_doc();
        let line = doc.create_line_core(12.0, 23.0, 17.0, 23.0, "0").unwrap();
        let circle = doc.create_circle_core(11.0, 24.0, 2.0, "0").unwrap();
        let members = vec![line, circle];
        let before = DxfWriter::new(&doc.inner).write_to_vec().unwrap();
        let mut late_failure = members.clone();
        late_failure.push("4294967295".to_string());
        assert_eq!(code(doc.create_block_core("Late", [10.0, 20.0, 0.0], &late_failure, "0")),
            "block_member_missing: every member must exist in the drawing");
        assert_eq!(before, DxfWriter::new(&doc.inner).write_to_vec().unwrap());
        for name in ["bad<name", "", "*anonymous"] {
            assert!(doc.create_block_core(name, [10.0, 20.0, 0.0], &members, "0").is_err());
            assert_eq!(before, DxfWriter::new(&doc.inner).write_to_vec().unwrap());
        }
        let inserted = doc.create_block_core("B", [10.0, 20.0, 0.0], &members, "0").unwrap();
        let projection = projected_entities(&doc.inner);
        assert_eq!(projection.len(), 1);
        assert_eq!(projection[0]["handle"], inserted);
        assert_eq!(projection[0]["type"], "INSERT");
        let definitions = block_catalogue(&doc.inner, false, &doc.unknown_block_bases);
        assert_eq!(definitions[0]["base"], serde_json::json!([10.0, 20.0, 0.0]));
        assert_eq!(definitions[0]["children"][0]["vertices"], serde_json::json!([[12.0, 23.0, 0.0], [17.0, 23.0, 0.0]]));
        let after = DxfWriter::new(&doc.inner).write_to_vec().unwrap();
        assert!(doc.create_block_core("b", [10.0, 20.0, 0.0], &members, "0").is_err());
        assert_eq!(after, DxfWriter::new(&doc.inner).write_to_vec().unwrap());
        let (written, _) = patch_block_bases(&doc.inner, after);
        let back = parse_dxf_core(&written).unwrap();
        let restored = block_catalogue(&back.inner, false, &back.unknown_block_bases);
        assert_eq!(restored[0]["base"], definitions[0]["base"]);
        assert_eq!(restored[0]["children"][0]["vertices"], definitions[0]["children"][0]["vertices"]);
        assert_eq!(projected_entities(&back.inner).len(), 1);
    }

    #[test]
    fn create_block_retains_created_and_loaded_handles_and_prunes_old_membership() {
        let mut doc = parse_dxf_core(b"0\nSECTION\n2\nENTITIES\n0\nCIRCLE\n5\n11\n8\n0\n10\n4\n20\n2\n40\n1\n0\nENDSEC\n0\nEOF\n").unwrap();
        let circle = "17".to_string();
        let line = doc.create_line_core(0.0, 0.0, 3.0, 0.0, "0").unwrap();
        let members = vec![line.clone(), circle.clone()];
        let original_handles: Vec<Handle> = members.iter().map(|id| Handle::new(id.parse().unwrap())).collect();
        let old_owner = doc.inner.block_records.iter().find(|b| b.is_model_space()).unwrap().handle;
        for handle in &original_handles {
            assert!(doc.inner.block_records.iter().find(|b| b.handle == old_owner).unwrap().entity_handles.contains(handle));
        }
        let inserted = doc.create_block_core("B", [1.0, 1.0, 0.0], &members, "0").unwrap();
        let projection = projected_entities(&doc.inner);
        assert_eq!(projection.len(), 1);
        assert_eq!(projection[0]["handle"], inserted);
        assert_eq!(projection[0]["type"], "INSERT");
        let block = doc.inner.block_records.get("B").unwrap();
        assert_eq!(block.entity_handles, original_handles);
        for handle in &original_handles {
            assert_eq!(doc.inner.get_entity(*handle).unwrap().common().handle, *handle);
            assert_eq!(doc.inner.get_entity(*handle).unwrap().common().owner_handle, block.handle);
            assert!(!doc.inner.block_records.iter().find(|b| b.handle == old_owner).unwrap().entity_handles.contains(handle));
        }
        let catalogue = block_catalogue(&doc.inner, false, &doc.unknown_block_bases);
        assert_eq!(catalogue[0]["children"][0]["handle"], line);
        assert_eq!(catalogue[0]["children"][1]["handle"], circle);
        let bytes = DxfWriter::new(&doc.inner).write_to_vec().unwrap();
        let (bytes, _) = patch_block_bases(&doc.inner, bytes);
        let back = parse_dxf_core(&bytes).unwrap();
        let restored = block_catalogue(&back.inner, false, &back.unknown_block_bases);
        assert_eq!(restored[0]["children"], catalogue[0]["children"]);
        assert_eq!(projected_entities(&back.inner).len(), 1);
        let model_space = back.inner.block_records.iter().find(|b| b.is_model_space()).unwrap();
        assert!(original_handles.iter().all(|handle| !model_space.entity_handles.contains(handle)));
    }

    #[test]
    fn create_block_keeps_a_moved_members_handle_and_geometry() {
        let mut doc = empty_doc();
        let line = doc.create_line_core(0.0, 0.0, 3.0, 0.0, "0").unwrap();
        doc.translate_entity_core(0, 2.0, -1.0).unwrap();
        doc.create_block_core("B", [1.0, 1.0, 0.0], &[line.clone()], "0").unwrap();
        let catalogue = block_catalogue(&doc.inner, false, &doc.unknown_block_bases);
        assert_eq!(catalogue[0]["children"][0]["handle"], line);
        assert_eq!(catalogue[0]["children"][0]["vertices"], serde_json::json!([[2.0, -1.0, 0.0], [5.0, -1.0, 0.0]]));
    }

    #[test]
    fn create_block_failure_after_reparenting_one_child_leaves_original_bytes() {
        let mut doc = empty_doc();
        let line = doc.create_line_core(0.0, 0.0, 3.0, 0.0, "0").unwrap();
        let circle = doc.create_circle_core(4.0, 2.0, 1.0, "0").unwrap();
        // A corrupt identity passes member lookup but cannot be removed by its
        // flat-storage key. The circle is already reparented on the staged clone.
        doc.inner.get_entity_mut(Handle::new(line.parse().unwrap())).unwrap().common_mut().handle = Handle::new(0xffff);
        let before = DxfWriter::new(&doc.inner).write_to_vec().unwrap();
        assert_eq!(code(doc.create_block_core("B", [1.0, 1.0, 0.0], &[circle, "65535".to_string()], "0")), "block_member_missing");
        assert_eq!(before, DxfWriter::new(&doc.inner).write_to_vec().unwrap());
        assert!(doc.inner.block_records.get("B").is_none());
    }

    #[test]
    fn create_block_refuses_dimassoc_sources_from_the_real_projection() {
        let bytes = b"0\nSECTION\n2\nHEADER\n9\n$ACADVER\n1\nAC1027\n0\nENDSEC\n0\nSECTION\n2\nENTITIES\n0\nLINE\n5\n10\n102\n{ACAD_REACTORS\n330\n30\n102\n}\n8\n0\n10\n12\n20\n23\n11\n17\n21\n23\n0\nDIMENSION\n5\n20\n8\n0\n70\n0\n10\n0\n20\n5\n13\n0\n23\n0\n14\n5\n24\n0\n0\nENDSEC\n0\nSECTION\n2\nOBJECTS\n0\nDIMASSOC\n5\n30\n100\nAcDbDimAssoc\n330\n20\n90\n1\n70\n0\n71\n0\n1\nAcDbOsnapPointRef\n72\n1\n331\n10\n73\n1\n91\n0\n40\n0\n10\n12\n20\n23\n30\n0\n75\n0\n0\nENDSEC\n0\nEOF\n";
        let mut doc = parse_dxf_core(bytes).unwrap();
        assert!(doc.inner.objects.values().any(|o| matches!(o, ObjectType::Associative(a)
            if matches!(&a.data, AssociativeData::DimensionAssociation(d) if d.dimension == Handle::new(0x20)
                && d.references.iter().flatten().any(|r| r.xrefs.contains(&Handle::new(0x10)))))));
        let projection = projected_entities(&doc.inner);
        let dim = projection.iter().find(|e| e["handle"] == "32").unwrap();
        assert!(dim["definingHandles"].as_array().unwrap().contains(&serde_json::json!("16")));
        for with_reactor in [true, false] {
            if !with_reactor { doc.inner.get_entity_mut(Handle::new(0x10)).unwrap().common_mut().reactors.clear(); }
            let before = DxfWriter::new(&doc.inner).write_to_vec().unwrap();
            assert_eq!(code(doc.create_block_core("B", [10.0, 20.0, 0.0], &["16".to_string()], "0")),
                "block_member_dimension: a dimension defining entity cannot become a block child");
            assert_eq!(before, DxfWriter::new(&doc.inner).write_to_vec().unwrap());
        }
    }

    #[test]
    fn create_block_refuses_widths_before_changing_bytes() {
        for kind in 0..3 {
            let mut doc = empty_doc();
            let id = doc.create_polyline_core(&[0.0, 0.0, 5.0, 0.0], false, "0", &[]).unwrap();
            assert!(projected_entities(&doc.inner)[0]["constantWidth"].is_null());
            assert!(projected_entities(&doc.inner)[0]["startWidths"].is_null());
            for entity in doc.inner.entities_mut() {
                if let EntityType::LwPolyline(poly) = entity {
                    match kind {
                        0 => poly.constant_width = 2.0,
                        1 => poly.vertices[0].start_width = 2.0,
                        _ => poly.vertices[0].end_width = 2.0,
                    }
                }
            }
            let projection = projected_entities(&doc.inner);
            match kind {
                0 => assert_eq!(projection[0]["constantWidth"], 2.0),
                1 => assert_eq!(projection[0]["startWidths"][0], 2.0),
                _ => assert_eq!(projection[0]["endWidths"][0], 2.0),
            }
            let before = DxfWriter::new(&doc.inner).write_to_vec().unwrap();
            assert_eq!(code(doc.create_block_core("B", [0.0, 0.0, 0.0], &[id], "0")),
                "block_member_width: polyline widths must be zero");
            assert_eq!(before, DxfWriter::new(&doc.inner).write_to_vec().unwrap());
        }
    }

    #[test]
    fn named_group_roundtrip_collision_ungroup_and_deletion_repair() {
        let mut doc = empty_doc();
        let line = doc.create_line_core(0.0, 0.0, 3.0, 0.0, "0").unwrap();
        let circle = doc.create_circle_core(4.0, 2.0, 1.0, "0").unwrap();
        let geometry = projected_entities(&doc.inner);
        let handle = doc.create_group_core(" rack ", &[0, 1, 0]).unwrap();
        assert_eq!(projected_entities(&doc.inner), geometry);
        let expected = serde_json::json!({"id": handle, "name": "RACK", "memberIds": [line, circle], "unnamed": false, "selectable": true, "description": ""});
        assert_eq!(projected_groups(&doc.inner), vec![expected.clone()]);
        doc = rewrite(&doc);
        assert_eq!(projected_groups(&doc.inner), vec![expected]);
        assert_eq!(code(doc.create_group_core("rack", &[0, 1])), "group_name_exists");
        assert_eq!(code(doc.create_group_core("ONE", &[0, 0])), "group_needs_two_members");
        doc.ungroup_core("rack").unwrap();
        assert_eq!(projected_entities(&doc.inner), geometry);
        assert!(projected_groups(&doc.inner).is_empty());
        assert!(doc.inner.entities().all(|entity| entity.common().reactors.is_empty()));
        doc.create_group_core("RACK", &[0, 1]).unwrap();
        doc.delete_entity_core(1).unwrap();
        assert_eq!(projected_groups(&doc.inner)[0]["memberIds"], serde_json::json!([line]));
        doc.delete_entity_core(0).unwrap();
        assert!(projected_groups(&doc.inner).is_empty());
        assert!(group_names(&doc.inner).is_empty());
        assert_eq!(code(doc.ungroup_core("RACK")), "group_not_found");
    }

    #[test]
    fn group_dictionary_syntax_refuses_before_mutation() {
        let mut doc = empty_doc();
        doc.create_line_core(0.0, 0.0, 3.0, 0.0, "0").unwrap();
        doc.create_circle_core(4.0, 2.0, 1.0, "0").unwrap();
        let before = DxfWriter::new(&doc.inner).write_to_vec().unwrap();
        for name in ["A/B", "RA*CK", "A<B", "A>B", "A\\B", "A\"B", "A:B", "A;B", "A?B", "A|B", "A,B", "A=B", "A`B"] {
            assert_eq!(code(doc.create_group_core(name, &[0, 1])), "group_name_invalid", "{name}");
            assert_eq!(DxfWriter::new(&doc.inner).write_to_vec().unwrap(), before);
        }
    }

    #[test]
    fn group_explode_repairs_members_and_parts_have_no_reactors_after_reparse() {
        let mut doc = empty_doc();
        let poly = doc.create_polyline_core(&[0.0, 0.0, 4.0, 0.0, 4.0, 3.0], false, "0", &[]).unwrap();
        let line = doc.create_line_core(10.0, 0.0, 13.0, 0.0, "0").unwrap();
        doc.create_group_core("rack", &[0, 1]).unwrap();
        let parts = doc.explode_entity_core(0).unwrap();
        let back = rewrite(&doc);
        assert_eq!(projected_groups(&back.inner)[0]["memberIds"], serde_json::json!([line]));
        assert!(back.inner.entities().all(|entity| handle_id(entity.common().handle.value()) != poly));
        for part in parts {
            let entity = back.inner.entities().find(|entity| handle_id(entity.common().handle.value()) == part).unwrap();
            assert!(entity.common().reactors.is_empty());
        }
    }

    #[test]
    fn group_copy_has_no_reactor_and_does_not_join_members_after_reparse() {
        let mut doc = empty_doc();
        doc.create_line_core(0.0, 0.0, 3.0, 0.0, "0").unwrap();
        doc.create_circle_core(4.0, 2.0, 1.0, "0").unwrap();
        doc.create_group_core("rack", &[0, 1]).unwrap();
        let groups = projected_groups(&doc.inner);
        let copy = doc.copy_entity_core(0, 2.0, 3.0).unwrap();
        let back = rewrite(&doc);
        assert_eq!(projected_groups(&back.inner), groups);
        let copied = back.inner.entities().find(|entity| handle_id(entity.common().handle.value()) == copy).unwrap();
        assert!(copied.common().reactors.is_empty());
        assert!(back.inner.entities().filter(|entity| handle_id(entity.common().handle.value()) != copy)
            .all(|entity| entity.common().reactors.len() == 1));
    }

    const EPS: f64 = 1e-9;

    fn near(a: f64, b: f64) -> bool {
        (a - b).abs() < EPS
    }

    fn empty_doc() -> ParsedDxf {
        ParsedDxf { inner: CadDocument::new(), group_names: Vec::new(), block_base_patched: Cell::new(false), block_bases_unknown: false, unknown_block_bases: HashSet::new(), mlstyle_segments: HashMap::new() }
    }

    fn kinds(doc: &ParsedDxf) -> Vec<&'static str> {
        doc.inner.entities().map(kind_name).collect()
    }

    fn handles(doc: &ParsedDxf) -> Vec<u64> {
        doc.inner.entities().map(|e| e.common().handle.value()).collect()
    }

    fn reparse(doc: &CadDocument) -> CadDocument {
        let bytes = DxfWriter::new(doc).write_to_vec().expect("writer serializes the document");
        assert!(!bytes.is_empty(), "writer produced no bytes");
        DxfReader::from_reader(std::io::Cursor::new(bytes))
            .expect("reader accepts the written bytes")
            .read()
            .expect("written bytes re-parse")
    }

    fn rewrite(doc: &ParsedDxf) -> ParsedDxf {
        ParsedDxf { inner: reparse(&doc.inner), group_names: group_names(&doc.inner), block_base_patched: Cell::new(false), block_bases_unknown: doc.block_bases_unknown, unknown_block_bases: doc.unknown_block_bases.clone(), mlstyle_segments: doc.mlstyle_segments.clone() }
    }

    fn code<T>(result: Result<T, Refusal>) -> String {
        match result {
            Ok(_) => "OK".to_string(),
            Err(code) => code,
        }
    }

    // ---- the wrapper's own create surface (W4d Draw group) ----------------

    #[test]
    fn wrapper_creates_land_in_model_space_and_survive_rewrite_by_handle() {
        let mut doc = empty_doc();
        let line = doc.create_line_core(0.0, 0.0, 10.0, 5.0, "").expect("line");
        let circle = doc.create_circle_core(3.0, 3.0, 1.5, "Panels").expect("circle");
        let arc = doc.create_arc_core(0.0, 0.0, 2.0, 0.0, 90.0, "").expect("arc");
        let poly = doc
            .create_polyline_core(&[0.0, 0.0, 4.0, 0.0, 4.0, 3.0], true, "Outline", &[])
            .expect("polyline");
        assert_eq!(kinds(&doc), vec!["LINE", "CIRCLE", "ARC", "LWPOLYLINE"]);
        let back = rewrite(&doc);
        assert_eq!(kinds(&back), vec!["LINE", "CIRCLE", "ARC", "LWPOLYLINE"]);
        let back_handles = handles(&back);
        for h in [line, circle, arc, poly] {
            let value = h.parse::<u64>().expect("wrapper returns a decimal handle");
            assert!(back_handles.contains(&value), "handle {h} survives the rewrite");
        }
        let layers: Vec<String> = back.inner.entities().map(|e| e.common().layer.clone()).collect();
        assert_eq!(layers, vec!["0", "Panels", "0", "Outline"]);
        assert!(editable(back.inner.entities().nth(1).unwrap()), "a created circle is editable");
        assert!(closed_of(back.inner.entities().nth(3).unwrap()), "the closed flag survives");
    }

    #[test]
    fn handle_ids_stay_distinct_above_javascript_safe_integer() {
        assert_eq!(handle_id(0x20_0000_0000_0000), "9007199254740992");
        assert_eq!(handle_id(0x20_0000_0000_0001), "9007199254740993");
        assert_ne!(handle_id(0x20_0000_0000_0000), handle_id(0x20_0000_0000_0001));
    }

    #[test]
    fn wrapper_creates_refuse_before_touching_the_document() {
        let mut doc = empty_doc();
        assert_eq!(code(doc.create_line_core(f64::NAN, 0.0, 1.0, 1.0, "")), "coordinate_not_finite");
        assert_eq!(code(doc.create_line_core(2.0, 2.0, 2.0, 2.0, "")), "line_zero_length");
        assert_eq!(code(doc.create_circle_core(0.0, 0.0, 0.0, "")), "radius_not_positive");
        assert_eq!(code(doc.create_circle_core(0.0, f64::INFINITY, 1.0, "")), "coordinate_not_finite");
        assert_eq!(code(doc.create_arc_core(0.0, 0.0, 1.0, 45.0, 405.0, "")), "arc_sweep_zero");
        assert_eq!(code(doc.create_arc_core(0.0, 0.0, -1.0, 0.0, 90.0, "")), "radius_not_positive");
        assert_eq!(code(doc.create_polyline_core(&[0.0, 0.0, 1.0], false, "", &[])), "points_not_pairs");
        assert_eq!(code(doc.create_polyline_core(&[0.0, 0.0], false, "", &[])), "polyline_needs_two_vertices");
        assert_eq!(code(doc.create_polyline_core(&[0.0, 0.0, 1.0, f64::NAN], false, "", &[])), "coordinate_not_finite");
        let too_many = vec![0.0; (MAX_CREATED_VERTICES + 1) * 2];
        assert_eq!(code(doc.create_polyline_core(&too_many, false, "", &[])), "polyline_too_many_vertices");
        let long_layer = "L".repeat(256);
        assert_eq!(code(doc.create_line_core(0.0, 0.0, 1.0, 1.0, &long_layer)), "layer_name_too_long");
        assert_eq!(doc.inner.entities().count(), 0, "no refusal touched the document");
    }

    #[test]
    fn created_circle_and_arc_take_centre_edits_and_refuse_vertex_list_edits() {
        let mut doc = empty_doc();
        doc.create_circle_core(1.0, 1.0, 2.0, "").expect("circle");
        doc.create_arc_core(5.0, 5.0, 1.0, 0.0, 180.0, "").expect("arc");
        doc.translate_entity_core(0, 2.0, 3.0).expect("circle translates");
        doc.move_vertex_core(1, 0, -1.0, -1.0).expect("arc centre moves as vertex 0");
        assert_eq!(code(doc.move_vertex_core(0, 1, 1.0, 1.0)), "vertex_index_out_of_range");
        assert_eq!(code(doc.add_vertex_after_core(0, 0, 1.0, 1.0)), "entity_kind_has_no_vertex_list");
        assert_eq!(code(doc.delete_vertex_core(1, 0)), "entity_kind_has_no_vertex_list");
        doc.set_entity_layer_core(0, "Moved").expect("circle re-layers");
        doc.delete_entity_core(1).expect("arc deletes");
        let back = rewrite(&doc);
        let circles: Vec<&Circle> = back
            .inner
            .entities()
            .filter_map(|e| if let EntityType::Circle(c) = e { Some(c) } else { None })
            .collect();
        assert_eq!(circles.len(), 1);
        assert!(near(circles[0].center.x, 3.0) && near(circles[0].center.y, 4.0));
        assert_eq!(circles[0].layer(), "Moved");
        assert_eq!(kinds(&back), vec!["CIRCLE"]);
    }

    #[test]
    fn existing_edit_refusals_still_carry_their_codes_through_the_cores() {
        let mut doc = empty_doc();
        doc.create_line_core(0.0, 0.0, 1.0, 1.0, "").expect("line");
        assert_eq!(code(doc.delete_entity_core(5)), "entity_index_out_of_range");
        assert_eq!(code(doc.translate_entity_core(0, f64::NAN, 0.0)), "delta_not_finite");
        assert_eq!(code(doc.move_vertex_core(0, 2, 1.0, 1.0)), "vertex_index_out_of_range");
        assert_eq!(code(doc.add_vertex_after_core(0, 0, 1.0, 1.0)), "line_has_fixed_endpoints");
        assert_eq!(code(doc.delete_vertex_core(0, 0)), "line_has_fixed_endpoints");
        assert_eq!(code(doc.set_entity_layer_core(0, "   ")), "layer_name_empty");
        doc.create_polyline_core(&[0.0, 0.0, 1.0, 0.0], false, "", &[]).expect("two-vertex polyline");
        assert_eq!(code(doc.delete_vertex_core(1, 0)), "polyline_needs_two_vertices");
    }

    // ---- the writer spike: the crate's own surface, primitive by primitive ----

    #[test]
    fn created_line_survives_write_and_reparse() {
        let mut doc = CadDocument::new();
        let handle = doc
            .add_entity(EntityType::Line(Line::from_coords(1.0, 2.0, 0.0, 11.0, 7.0, 0.0)))
            .expect("add_entity accepts a created line");
        assert!(!handle.is_null(), "add_entity allocates a handle");

        let back = reparse(&doc);
        let lines: Vec<&Line> = back
            .entities()
            .filter_map(|e| if let EntityType::Line(l) = e { Some(l) } else { None })
            .collect();
        assert_eq!(lines.len(), 1, "exactly one LINE after re-parse");
        let l = lines[0];
        assert_eq!(l.layer(), "0");
        assert!(near(l.start.x, 1.0) && near(l.start.y, 2.0), "start survives: {:?}", l.start);
        assert!(near(l.end.x, 11.0) && near(l.end.y, 7.0), "end survives: {:?}", l.end);
    }

    #[test]
    fn created_circle_survives_write_and_reparse() {
        let mut doc = CadDocument::new();
        doc.add_entity(EntityType::Circle(Circle::from_coords(5.0, -3.0, 0.0, 2.5)))
            .expect("add_entity accepts a created circle");
        let back = reparse(&doc);
        let circles: Vec<&Circle> = back
            .entities()
            .filter_map(|e| if let EntityType::Circle(c) = e { Some(c) } else { None })
            .collect();
        assert_eq!(circles.len(), 1);
        let c = circles[0];
        assert_eq!(c.layer(), "0");
        assert!(near(c.center.x, 5.0) && near(c.center.y, -3.0), "center survives: {:?}", c.center);
        assert!(near(c.radius, 2.5), "radius survives: {}", c.radius);
    }

    #[test]
    fn created_arc_survives_write_and_reparse() {
        let mut doc = CadDocument::new();
        let mut arc = ArcEntity::new();
        arc.center = Vector3::new(0.5, 0.25, 0.0);
        arc.radius = 4.0;
        arc.start_angle = 0.0;
        arc.end_angle = std::f64::consts::FRAC_PI_2;
        doc.add_entity(EntityType::Arc(arc)).expect("add_entity accepts a created arc");
        let back = reparse(&doc);
        let arcs: Vec<&ArcEntity> = back
            .entities()
            .filter_map(|e| if let EntityType::Arc(a) = e { Some(a) } else { None })
            .collect();
        assert_eq!(arcs.len(), 1);
        let a = arcs[0];
        assert_eq!(a.layer(), "0");
        assert!(near(a.center.x, 0.5) && near(a.center.y, 0.25), "center survives: {:?}", a.center);
        assert!(near(a.radius, 4.0), "radius survives: {}", a.radius);
        // Angles round-trip through the DXF degree representation; a
        // quarter turn must come back as a quarter turn.
        assert!((a.end_angle - a.start_angle - std::f64::consts::FRAC_PI_2).abs() < 1e-6,
            "sweep survives: {} -> {}", a.start_angle, a.end_angle);
    }

    // W4f: the projection's drawable fields for circles and arcs, in the
    // create operands' own units (degrees), null for every other kind.
    #[test]
    fn projection_carries_radius_and_sweep_for_circles_and_arcs_only() {
        let mut doc = empty_doc();
        doc.create_line_core(0.0, 0.0, 10.0, 5.0, "").expect("line");
        doc.create_circle_core(3.0, 3.0, 1.5, "Panels").expect("circle");
        doc.create_arc_core(0.0, 0.0, 2.0, 30.0, 120.0, "").expect("arc");
        doc.create_polyline_core(&[0.0, 0.0, 4.0, 0.0, 4.0, 3.0], true, "Outline", &[]).expect("polyline");
        let back = rewrite(&doc);
        let entities: Vec<&EntityType> = back.inner.entities().collect();
        assert_eq!(entities.len(), 4);
        assert_eq!(radius_of(entities[0]), None);
        assert_eq!(sweep_deg_of(entities[0]), None);
        assert!(near(radius_of(entities[1]).expect("circle radius"), 1.5));
        assert_eq!(sweep_deg_of(entities[1]), None);
        assert!(near(radius_of(entities[2]).expect("arc radius"), 2.0));
        let (start, end) = sweep_deg_of(entities[2]).expect("arc sweep");
        assert!((start - 30.0).abs() < 1e-6 && (end - 120.0).abs() < 1e-6, "sweep in degrees: {} -> {}", start, end);
        assert_eq!(radius_of(entities[3]), None);
        assert_eq!(sweep_deg_of(entities[3]), None);
    }

    #[test]
    fn created_lwpolyline_survives_write_and_reparse() {
        let mut doc = CadDocument::new();
        let mut poly = LwPolyline::from_points(vec![
            Vector2::new(0.0, 0.0),
            Vector2::new(10.0, 0.0),
            Vector2::new(10.0, 4.0),
        ]);
        poly.is_closed = true;
        doc.add_entity(EntityType::LwPolyline(poly)).expect("add_entity accepts a created polyline");
        let back = reparse(&doc);
        let polys: Vec<&LwPolyline> = back
            .entities()
            .filter_map(|e| if let EntityType::LwPolyline(p) = e { Some(p) } else { None })
            .collect();
        assert_eq!(polys.len(), 1);
        let p = polys[0];
        assert_eq!(p.layer(), "0");
        assert_eq!(p.vertices.len(), 3, "vertex count survives");
        assert!(p.is_closed, "closed flag survives");
        assert!(near(p.vertices[2].location.x, 10.0) && near(p.vertices[2].location.y, 4.0));
    }

    #[test]
    fn created_entities_coexist_with_parsed_ones() {
        // The Draw group adds to an IMPORTED document: a created line next to
        // a parsed one must both survive, in document order, with distinct
        // handles.
        let fixture = include_bytes!("../fixtures/one_line.dxf");
        let mut doc = DxfReader::from_reader(std::io::Cursor::new(fixture.to_vec()))
            .expect("fixture reader")
            .read()
            .expect("fixture parses");
        let before = doc.entities().count();
        let handle = doc
            .add_entity(EntityType::Line(Line::from_coords(-1.0, -1.0, 0.0, -2.0, -2.0, 0.0)))
            .expect("add to a parsed document");
        let back = reparse(&doc);
        assert_eq!(back.entities().count(), before + 1);
        let handles: Vec<u64> = back.entities().map(|e| e.common().handle.value()).collect();
        let mut dedup = handles.clone();
        dedup.sort_unstable();
        dedup.dedup();
        assert_eq!(dedup.len(), handles.len(), "handles stay unique after a create: {:?}", handles);
        assert!(handles.contains(&handle.value()), "the created handle is the one written");
    }

    fn verts(doc: &ParsedDxf, index: usize) -> Vec<[f64; 3]> {
        vertices_of(doc.inner.entities().nth(index).expect("entity at index"))
    }

    #[test]
    fn w4g4_copy_rotate_scale_and_mirror_move_the_right_points_and_keep_the_layer() {
        let mut doc = empty_doc();
        let original = doc.create_line_core(0.0, 0.0, 10.0, 0.0, "A").unwrap();
        // COPY: a second line displaced by (5, 5) on the same layer, new handle.
        let copy = doc.copy_entity_core(0, 5.0, 5.0).unwrap();
        assert_ne!(copy, original);
        assert_eq!(kinds(&doc), vec!["LINE", "LINE"]);
        let v = verts(&doc, 1);
        assert!(near(v[0][0], 5.0) && near(v[0][1], 5.0) && near(v[1][0], 15.0) && near(v[1][1], 5.0));
        assert_eq!(doc.inner.entities().nth(1).unwrap().common().layer, "A");
        // ROTATE the copy 90 deg CCW about its start (5, 5): the end (15, 5) -> (5, 15).
        doc.rotate_entity_core(1, 5.0, 5.0, 90.0).unwrap();
        let v = verts(&doc, 1);
        assert!(near(v[0][0], 5.0) && near(v[0][1], 5.0), "the base point stays: {:?}", v);
        assert!(near(v[1][0], 5.0) && near(v[1][1], 15.0), "the end rotated: {:?}", v);
        // SCALE x2 about (5, 5): the end (5, 15) -> (5, 25).
        doc.scale_entity_core(1, 5.0, 5.0, 2.0).unwrap();
        let v = verts(&doc, 1);
        assert!(near(v[1][0], 5.0) && near(v[1][1], 25.0), "the end scaled: {:?}", v);
        // MIRROR the original about the y axis, keeping the source: a third line (0,0)-(-10,0).
        let mirrored = doc.mirror_entity_core(0, 0.0, 0.0, 0.0, 1.0, true).unwrap();
        assert!(!mirrored.is_empty());
        assert_eq!(doc.inner.entities().count(), 3);
        let v = verts(&doc, 2);
        assert!(near(v[1][0], -10.0) && near(v[1][1], 0.0), "mirrored copy: {:?}", v);
        // MIRROR in place: the mirrored copy comes back to (10, 0), no new entity, empty answer.
        assert_eq!(doc.mirror_entity_core(2, 0.0, 0.0, 0.0, 1.0, false).unwrap(), "");
        assert_eq!(doc.inner.entities().count(), 3);
        let v = verts(&doc, 2);
        assert!(near(v[1][0], 10.0), "mirrored back: {:?}", v);
        // Every handle stays unique after the verbs.
        let mut hs = handles(&doc);
        hs.sort_unstable();
        hs.dedup();
        assert_eq!(hs.len(), 3);
    }

    #[test]
    fn w4g4_explode_replaces_a_polyline_with_its_segments_and_refuses_the_rest() {
        let mut doc = empty_doc();
        let poly = doc
            .create_polyline_core(&[0.0, 0.0, 4.0, 0.0, 4.0, 3.0], true, "P", &[])
            .unwrap();
        let parts = doc.explode_entity_core(0).unwrap();
        assert_eq!(parts.len(), 3, "a closed triangle explodes into three segments: {:?}", parts);
        assert!(kinds(&doc).iter().all(|k| *k == "LINE"), "{:?}", kinds(&doc));
        assert!(doc.inner.entities().all(|e| e.common().layer == "P"));
        let gone: u64 = poly.parse().unwrap();
        assert!(!handles(&doc).contains(&gone), "the source polyline is removed");
        let mut hs = handles(&doc);
        hs.sort_unstable();
        hs.dedup();
        assert_eq!(hs.len(), 3);
        // A line has nothing to explode into.
        assert_eq!(code(doc.explode_entity_core(0)), "entity_not_explodable");
    }

    #[test]
    fn w4g4_explode_refuses_a_circle_an_arc_and_a_line_by_kind_and_leaves_them_whole() {
        // kimi on #1010: the crate's explode of a CIRCLE returns one part, a
        // 0..2pi ARC the writer emits as 50=0 / 51=360 (a zero-span arc that
        // readers draw as nothing), so an empty-parts guard alone would let
        // EXPLODE erase a circle. The kind is refused before the crate is
        // asked, and the document is untouched: same kinds, same handles.
        let mut doc = empty_doc();
        doc.create_circle_core(5.0, 5.0, 2.0, "C").unwrap();
        doc.create_arc_core(0.0, 0.0, 3.0, 0.0, 90.0, "C").unwrap();
        doc.create_line_core(0.0, 0.0, 1.0, 1.0, "C").unwrap();
        let before = handles(&doc);
        assert_eq!(code(doc.explode_entity_core(0)), "entity_not_explodable");
        assert_eq!(code(doc.explode_entity_core(1)), "entity_not_explodable");
        assert_eq!(code(doc.explode_entity_core(2)), "entity_not_explodable");
        assert_eq!(kinds(&doc), vec!["CIRCLE", "ARC", "LINE"]);
        assert_eq!(handles(&doc), before);
        // And after a write + re-parse the circle is still a circle.
        let back = rewrite(&doc);
        assert_eq!(kinds(&back), vec!["CIRCLE", "ARC", "LINE"]);
    }

    #[test]
    fn w4g4_verbs_refuse_before_touching_the_document() {
        let mut doc = empty_doc();
        doc.create_line_core(0.0, 0.0, 10.0, 0.0, "A").unwrap();
        let before = rewrite(&doc).inner.entities().count();
        assert_eq!(code(doc.copy_entity_core(0, f64::NAN, 0.0)), "delta_not_finite");
        assert_eq!(code(doc.mirror_entity_core(0, 1.0, 1.0, 1.0, 1.0, true)), "mirror_line_zero_length");
        assert_eq!(code(doc.mirror_entity_core(0, f64::INFINITY, 0.0, 1.0, 1.0, false)), "coordinate_not_finite");
        assert_eq!(code(doc.rotate_entity_core(0, 0.0, 0.0, f64::NAN)), "coordinate_not_finite");
        assert_eq!(code(doc.scale_entity_core(0, 0.0, 0.0, 0.0)), "scale_not_positive");
        assert_eq!(code(doc.scale_entity_core(0, 0.0, 0.0, -2.0)), "scale_not_positive");
        assert_eq!(code(doc.rotate_entity_core(9, 0.0, 0.0, 1.0)), "entity_index_out_of_range");
        assert_eq!(code(doc.copy_entity_core(9, 1.0, 1.0)), "entity_index_out_of_range");
        assert_eq!(code(doc.explode_entity_core(9)), "entity_index_out_of_range");
        assert_eq!(doc.inner.entities().count(), before);
        let v = verts(&doc, 0);
        assert!(near(v[1][0], 10.0) && near(v[1][1], 0.0), "untouched: {:?}", v);
    }

    #[test]
    fn w4g4_verbs_survive_write_and_reparse() {
        let mut doc = empty_doc();
        doc.create_line_core(0.0, 0.0, 10.0, 0.0, "A").unwrap();
        doc.copy_entity_core(0, 0.0, 7.0).unwrap();
        doc.rotate_entity_core(1, 0.0, 7.0, 90.0).unwrap();
        doc.create_polyline_core(&[20.0, 0.0, 24.0, 0.0, 24.0, 3.0], true, "P", &[]).unwrap();
        doc.explode_entity_core(2).unwrap();
        let back = rewrite(&doc);
        assert_eq!(kinds(&back), vec!["LINE", "LINE", "LINE", "LINE", "LINE"]);
        let v = verts(&back, 1);
        assert!(near(v[1][0], 0.0) && near(v[1][1], 17.0), "rotated copy after re-parse: {:?}", v);
    }

    // ---- W4g-5b: ARRAY -----------------------------------------------------

    /// Every CIRCLE centre in document order, rounded to the writer's own
    /// precision so a re-parse compares equal.
    fn centres(doc: &ParsedDxf) -> Vec<(f64, f64)> {
        doc.inner
            .entities()
            .filter_map(|e| match e {
                EntityType::Circle(c) => Some((
                    (c.center.x * 1e6).round() / 1e6,
                    (c.center.y * 1e6).round() / 1e6,
                )),
                _ => None,
            })
            .collect()
    }

    #[test]
    fn w4g5b_rectangular_array_adds_every_position_but_the_source() {
        let mut doc = empty_doc();
        doc.create_circle_core(0.0, 0.0, 1.0, "P").unwrap();
        let handles_added = doc.array_rect_core(0, 2, 3, 10.0, 5.0).expect("2 x 3 array");
        // 2 x 3 positions, the source holds one, so five copies.
        assert_eq!(handles_added.len(), 5);
        assert_eq!(
            centres(&doc),
            vec![(0.0, 0.0), (5.0, 0.0), (10.0, 0.0), (0.0, 10.0), (5.0, 10.0), (10.0, 10.0)],
        );
        // Every copy is on the source's layer and survives write + re-parse.
        assert!(doc.inner.entities().all(|e| e.common().layer == "P"));
        assert_eq!(centres(&rewrite(&doc)).len(), 6);
    }

    #[test]
    fn w4g5b_polar_array_over_a_full_turn_divides_by_the_count() {
        let mut doc = empty_doc();
        doc.create_circle_core(10.0, 0.0, 1.0, "P").unwrap();
        let added = doc.array_polar_core(0, 4, 0.0, 0.0, 360.0).expect("polar array");
        assert_eq!(added.len(), 3);
        // A full turn shares its first and last position, so four positions
        // are 90 degrees apart and the fourth does NOT sit on the source.
        assert_eq!(centres(&doc), vec![(10.0, 0.0), (0.0, 10.0), (-10.0, 0.0), (-0.0, -10.0)]);
        // A full turn is exactly fillable in either direction; only a sweep
        // PAST one turn is refused, because there the copies wrap onto the
        // source.
        let mut clockwise = empty_doc();
        clockwise.create_circle_core(10.0, 0.0, 1.0, "P").unwrap();
        assert_eq!(clockwise.array_polar_core(0, 4, 0.0, 0.0, -360.0).unwrap().len(), 3);
        assert_eq!(centres(&clockwise), vec![(10.0, 0.0), (-0.0, -10.0), (-10.0, 0.0), (0.0, 10.0)]);
    }

    #[test]
    fn w4g5b_polar_array_over_an_open_sweep_divides_by_the_gaps() {
        let mut doc = empty_doc();
        doc.create_circle_core(10.0, 0.0, 1.0, "P").unwrap();
        doc.array_polar_core(0, 3, 0.0, 0.0, 180.0).expect("open sweep");
        // Three positions across 180 degrees are 90 degrees apart: the last
        // one lands exactly on the sweep's end.
        assert_eq!(centres(&doc), vec![(10.0, 0.0), (0.0, 10.0), (-10.0, 0.0)]);
    }

    #[test]
    fn w4g5b_array_refuses_before_touching_the_document() {
        let mut doc = empty_doc();
        doc.create_circle_core(0.0, 0.0, 1.0, "P").unwrap();
        let before = handles(&doc);
        assert_eq!(code(doc.array_rect_core(0, 0, 3, 1.0, 1.0)), "array_count_not_positive");
        assert_eq!(code(doc.array_rect_core(0, 3, 0, 1.0, 1.0)), "array_count_not_positive");
        // One row by one column is the source alone: no copy to make.
        assert_eq!(code(doc.array_rect_core(0, 1, 1, 1.0, 1.0)), "array_count_not_positive");
        assert_eq!(code(doc.array_rect_core(0, 40, 40, 1.0, 1.0)), "array_too_many_copies");
        assert_eq!(code(doc.array_rect_core(0, 2, 2, f64::NAN, 1.0)), "coordinate_not_finite");
        assert_eq!(code(doc.array_rect_core(0, 2, 2, 0.0, 0.0)), "array_spacing_zero");
        assert_eq!(code(doc.array_rect_core(9, 2, 2, 1.0, 1.0)), "entity_index_out_of_range");
        assert_eq!(code(doc.array_polar_core(0, 1, 0.0, 0.0, 90.0)), "array_count_not_positive");
        assert_eq!(code(doc.array_polar_core(0, 2000, 0.0, 0.0, 90.0)), "array_too_many_copies");
        assert_eq!(code(doc.array_polar_core(0, 4, f64::INFINITY, 0.0, 90.0)), "coordinate_not_finite");
        assert_eq!(code(doc.array_polar_core(0, 4, 0.0, 0.0, 0.0)), "array_sweep_zero");
        assert_eq!(code(doc.array_polar_core(0, 3, 0.0, 0.0, 720.0)), "array_sweep_past_full_turn");
        assert_eq!(code(doc.array_polar_core(0, 4, 0.0, 0.0, -720.0)), "array_sweep_past_full_turn");
        assert_eq!(code(doc.array_polar_core(9, 4, 0.0, 0.0, 90.0)), "entity_index_out_of_range");
        assert_eq!(handles(&doc), before, "a refused array adds nothing");
        assert_eq!(kinds(&doc), vec!["CIRCLE"]);
    }

    // ---- W4g-5d: TEXT ------------------------------------------------------

    #[test]
    fn w4g5d_text_creates_projects_and_survives_rewrite_with_its_own_fields() {
        let mut doc = empty_doc();
        let handle = doc.create_text_core(10.0, 20.0, 2.5, 30.0, "Panel A", "Notes").expect("text");
        assert!(!handle.is_empty());
        assert_eq!(kinds(&doc), vec!["TEXT"]);
        let e = doc.inner.entities().next().unwrap();
        assert!(editable(e), "a TEXT is editable (delete, move, copy, clipboard)");
        assert_eq!(text_of(e).as_deref(), Some("Panel A"));
        assert_eq!(height_of(e), Some(2.5));
        assert!((rotation_deg_of(e).unwrap() - 30.0).abs() < 1e-9);
        assert_eq!(vertices_of(e), vec![[10.0, 20.0, 0.0]]);
        assert_eq!(e.common().layer, "Notes");
        // Height and rotation are the DXF's own fields, so the re-parse keeps
        // them exactly; the intake the server keeps would not.
        let back = rewrite(&doc);
        let b = back.inner.entities().next().unwrap();
        assert_eq!(text_of(b).as_deref(), Some("Panel A"));
        assert_eq!(height_of(b), Some(2.5));
        assert!((rotation_deg_of(b).unwrap() - 30.0).abs() < 1e-6);
        assert_eq!(vertices_of(b), vec![[10.0, 20.0, 0.0]]);
    }

    #[test]
    fn w4g5d_text_refuses_before_touching_the_document() {
        let mut doc = empty_doc();
        doc.create_circle_core(0.0, 0.0, 1.0, "P").unwrap();
        let before = handles(&doc);
        assert_eq!(code(doc.create_text_core(f64::NAN, 0.0, 1.0, 0.0, "x", "")), "coordinate_not_finite");
        assert_eq!(code(doc.create_text_core(0.0, 0.0, 0.0, 0.0, "x", "")), "text_height_not_positive");
        assert_eq!(code(doc.create_text_core(0.0, 0.0, -1.0, 0.0, "x", "")), "text_height_not_positive");
        assert_eq!(code(doc.create_text_core(0.0, 0.0, 1.0, 0.0, "", "")), "text_empty");
        assert_eq!(code(doc.create_text_core(0.0, 0.0, 1.0, 0.0, "
", "")), "text_empty");
        let long = "a".repeat(MAX_TEXT_CHARS + 1);
        assert_eq!(code(doc.create_text_core(0.0, 0.0, 1.0, 0.0, &long, "")), "text_too_long");
        assert_eq!(code(doc.create_text_core(0.0, 0.0, 1.0, 0.0, "line one
line two", "")), "text_control_character");
        assert_eq!(code(doc.create_text_core(0.0, 0.0, 1.0, 0.0, "tab	here", "")), "text_control_character");
        assert_eq!(handles(&doc), before, "a refused text adds nothing");
        assert_eq!(kinds(&doc), vec!["CIRCLE"]);
        // Exactly the bound is accepted.
        let max = "b".repeat(MAX_TEXT_CHARS);
        assert!(doc.create_text_core(0.0, 0.0, 1.0, 0.0, &max, "").is_ok());
    }

    // ---- W4g-6: the geometry primitives behind TRIM / EXTEND / FILLET / CHAMFER

    /// The written bytes: a refusal must leave them identical, not merely the
    /// handles and kinds.
    fn engine_bytes(doc: &ParsedDxf) -> Vec<u8> {
        DxfWriter::new(&doc.inner).write_to_vec().expect("writer serializes the document")
    }

    #[test]
    fn w4g6_set_vertices_rewrites_a_line_and_a_polyline_and_survives_rewrite() {
        let mut doc = empty_doc();
        doc.create_line_core(0.0, 0.0, 10.0, 0.0, "A").unwrap();
        doc.create_polyline_core(&[0.0, 0.0, 10.0, 0.0, 10.0, 10.0, 0.0, 10.0], true, "B", &[]).unwrap();
        let before = handles(&doc);
        // A TRIM of the line at x = 4 keeps [0, 4].
        doc.set_vertices_core(0, &[0.0, 0.0, 4.0, 0.0], false, &[]).expect("a line takes two points");
        // A TRIM that opens the square at its last segment keeps three corners.
        doc.set_vertices_core(1, &[0.0, 0.0, 10.0, 0.0, 10.0, 10.0], false, &[]).expect("a polyline takes a list");
        let entities: Vec<&EntityType> = doc.inner.entities().collect();
        assert_eq!(vertices_of(entities[0]), vec![[0.0, 0.0, 0.0], [4.0, 0.0, 0.0]]);
        assert_eq!(vertices_of(entities[1]).len(), 3);
        assert!(!closed_of(entities[1]), "the closed flag follows the call");
        assert_eq!(entities[1].common().layer, "B", "the layer is not geometry");
        assert_eq!(handles(&doc), before, "geometry replacement keeps the handles");
        let back = rewrite(&doc);
        let again: Vec<&EntityType> = back.inner.entities().collect();
        assert_eq!(vertices_of(again[0]), vec![[0.0, 0.0, 0.0], [4.0, 0.0, 0.0]]);
        assert_eq!(vertices_of(again[1]).len(), 3);
        assert!(!closed_of(again[1]));
        // And back to closed, four corners: the flag is settable both ways.
        let mut back = back;
        back.set_vertices_core(1, &[0.0, 0.0, 10.0, 0.0, 10.0, 10.0, 0.0, 10.0], true, &[]).unwrap();
        assert!(closed_of(back.inner.entities().nth(1).unwrap()));
    }

    #[test]
    fn w4g4b_point_and_ellipse_create_move_transform_and_survive_rewrite() {
        let mut doc = empty_doc();
        let point = doc.create_point_core(3.0, 4.0, "P").expect("a point");
        let ellipse = doc.create_ellipse_core(10.0, 0.0, 5.0, 0.0, 0.5, "E").expect("an ellipse");
        assert_ne!(point, ellipse);
        let back = rewrite(&doc);
        let entities: Vec<&EntityType> = back.inner.entities().collect();
        assert_eq!(kind_name(entities[0]), "POINT");
        assert_eq!(kind_name(entities[1]), "ELLIPSE");
        assert!(editable(entities[0]) && editable(entities[1]), "both kinds are editable");
        assert_eq!(vertices_of(entities[0]), vec![[3.0, 4.0, 0.0]]);
        assert_eq!(vertices_of(entities[1]), vec![[10.0, 0.0, 0.0]]);
        assert_eq!(major_axis_of(entities[1]), Some([5.0, 0.0]));
        assert_eq!(ratio_of(entities[1]), Some(0.5));
        assert_eq!(major_axis_of(entities[0]), None);
        assert_eq!(entities[0].common().layer, "P");
        assert_eq!(entities[1].common().layer, "E");
        // MOVE moves the location and the centre; the axis is relative and stays.
        let mut back = back;
        back.translate_entity_core(0, 1.0, 1.0).unwrap();
        back.translate_entity_core(1, -10.0, 2.0).unwrap();
        let moved: Vec<&EntityType> = back.inner.entities().collect();
        assert_eq!(vertices_of(moved[0]), vec![[4.0, 5.0, 0.0]]);
        assert_eq!(vertices_of(moved[1]), vec![[0.0, 2.0, 0.0]]);
        assert_eq!(major_axis_of(moved[1]), Some([5.0, 0.0]));
        // ROTATE by 90 degrees about the ellipse's own centre turns the axis, not the centre.
        back.rotate_entity_core(1, 0.0, 2.0, 90.0).unwrap();
        let turned = back.inner.entities().nth(1).unwrap();
        let axis = major_axis_of(turned).unwrap();
        assert!((axis[0]).abs() < 1e-9 && (axis[1] - 5.0).abs() < 1e-9, "axis after rotation: {:?}", axis);
        assert_eq!(vertices_of(turned), vec![[0.0, 2.0, 0.0]]);
        // SCALE by 2 about the origin doubles the axis and the centre's distance.
        back.scale_entity_core(1, 0.0, 0.0, 2.0).unwrap();
        let bigger = back.inner.entities().nth(1).unwrap();
        let axis = major_axis_of(bigger).unwrap();
        assert!((axis[1] - 10.0).abs() < 1e-9, "axis after scale: {:?}", axis);
        assert_eq!(vertices_of(bigger), vec![[0.0, 4.0, 0.0]]);
        assert_eq!(ratio_of(bigger), Some(0.5), "a uniform scale keeps the ratio");
        // The written document reads back with the same kinds and numbers.
        let again = rewrite(&back);
        let list: Vec<&EntityType> = again.inner.entities().collect();
        assert_eq!(kind_name(list[1]), "ELLIPSE");
        assert_eq!(vertices_of(list[1]), vec![[0.0, 4.0, 0.0]]);
    }

    #[test]
    fn w4g4b_point_and_ellipse_refuse_before_touching_the_document() {
        let mut doc = empty_doc();
        doc.create_line_core(0.0, 0.0, 1.0, 0.0, "A").unwrap();
        let before = engine_bytes(&doc);
        assert_eq!(code(doc.create_point_core(f64::NAN, 0.0, "A")), "coordinate_not_finite");
        assert_eq!(code(doc.create_ellipse_core(0.0, 0.0, 0.0, 0.0, 0.5, "A")), "ellipse_axis_zero");
        assert_eq!(code(doc.create_ellipse_core(0.0, 0.0, 5.0, 0.0, 0.0, "A")), "ellipse_ratio_out_of_range");
        assert_eq!(code(doc.create_ellipse_core(0.0, 0.0, 5.0, 0.0, 1.5, "A")), "ellipse_ratio_out_of_range");
        assert_eq!(code(doc.create_ellipse_core(0.0, 0.0, 5.0, f64::INFINITY, 0.5, "A")), "coordinate_not_finite");
        assert_eq!(engine_bytes(&doc), before, "a refusal touches nothing");
        // A ratio of exactly 1 is a circle-shaped ellipse and legal.
        doc.create_ellipse_core(0.0, 0.0, 5.0, 0.0, 1.0, "A").expect("ratio 1 is legal");
    }

    #[test]
    fn w4g6d_set_vertices_carries_bulges_and_refuses_a_bad_list() {
        let mut doc = empty_doc();
        // A 10 x 10 square; the projection reports four straight vertices.
        doc.create_polyline_core(&[0.0, 0.0, 10.0, 0.0, 10.0, 10.0, 0.0, 10.0], true, "B", &[]).unwrap();
        let first = doc.inner.entities().next().unwrap();
        assert_eq!(bulges_of(first), Some(vec![0.0, 0.0, 0.0, 0.0]));
        // The corner at (10,10) filleted with r = 2: (8,10) carries tan(pi / 8) toward (10,8); five vertices, still closed.
        let b = (std::f64::consts::PI / 8.0).tan();
        let pts = [0.0, 0.0, 10.0, 0.0, 10.0, 8.0, 8.0, 10.0, 0.0, 10.0];
        let bad = [0.0, 0.0, 0.0, 0.0];
        assert_eq!(code(doc.set_vertices_core(0, &pts, true, &bad)), "bulges_not_per_vertex");
        assert_eq!(code(doc.set_vertices_core(0, &pts, true, &[0.0, 0.0, f64::NAN, 0.0, 0.0])), "bulge_not_finite");
        assert_eq!(bulges_of(doc.inner.entities().next().unwrap()), Some(vec![0.0; 4]), "a refusal touches nothing");
        doc.set_vertices_core(0, &pts, true, &[0.0, 0.0, b, 0.0, 0.0]).expect("one bulge per point");
        let back = rewrite(&doc);
        let poly = back.inner.entities().next().unwrap();
        assert_eq!(vertices_of(poly).len(), 5);
        assert!(closed_of(poly));
        let got = bulges_of(poly).unwrap();
        assert!((got[2] - b).abs() < 1e-12, "the bulge survives write + re-parse: {:?}", got);
        assert!(got.iter().enumerate().all(|(i, v)| i == 2 || *v == 0.0));
        // An empty list means every segment straight, whatever the polyline carried before.
        let mut back = back;
        back.set_vertices_core(0, &pts, true, &[]).unwrap();
        assert_eq!(bulges_of(back.inner.entities().next().unwrap()), Some(vec![0.0; 5]));
        // A LINE ignores an empty list and refuses a non-empty one only by count (two points, two bulges is the shape).
        back.create_line_core(0.0, 0.0, 5.0, 0.0, "A").unwrap();
        assert_eq!(bulges_of(back.inner.entities().nth(1).unwrap()), None, "a line has no bulge list");
        back.set_vertices_core(1, &[0.0, 0.0, 6.0, 0.0], false, &[0.0, 0.0]).expect("a line takes a per-point list too");
        assert_eq!(code(back.set_vertices_core(1, &[0.0, 0.0, 6.0, 0.0], false, &[0.0])), "bulges_not_per_vertex");
    }

    #[test]
    fn w4g6e_create_polyline_carries_bulges_and_refuses_a_bad_list() {
        let mut doc = empty_doc();
        // An open three-vertex polyline whose first segment is a semicircle (bulge 1).
        doc.create_polyline_core(&[0.0, 0.0, 10.0, 0.0, 10.0, 10.0], false, "A", &[1.0, 0.0, 0.0]).expect("one bulge per point");
        assert_eq!(bulges_of(doc.inner.entities().next().unwrap()), Some(vec![1.0, 0.0, 0.0]));
        // Refusals touch nothing: still one entity, its list unchanged.
        assert_eq!(code(doc.create_polyline_core(&[0.0, 0.0, 10.0, 0.0, 10.0, 10.0], false, "A", &[1.0])), "bulges_not_per_vertex");
        assert_eq!(code(doc.create_polyline_core(&[0.0, 0.0, 10.0, 0.0, 10.0, 10.0], false, "A", &[0.0, f64::NAN, 0.0])), "bulge_not_finite");
        assert_eq!(code(doc.create_polyline_core(&[0.0, 0.0, 10.0, 0.0, 10.0, 10.0], false, "A", &[0.0, f64::INFINITY, 0.0])), "bulge_not_finite");
        assert_eq!(doc.inner.entities().count(), 1, "a refusal creates nothing");
        // The bulge survives write + re-parse; the shape is otherwise the straight create's.
        let back = rewrite(&doc);
        let poly = back.inner.entities().next().unwrap();
        assert_eq!(vertices_of(poly).len(), 3);
        assert!(!closed_of(poly));
        let got = bulges_of(poly).unwrap();
        assert!((got[0] - 1.0).abs() < 1e-12 && got[1] == 0.0 && got[2] == 0.0, "the bulge survives write + re-parse: {:?}", got);
        // A closed square whose CLOSING segment (the last vertex's bulge) curves; an empty list is all straight.
        let mut doc = empty_doc();
        doc.create_polyline_core(&[0.0, 0.0, 10.0, 0.0, 10.0, 10.0, 0.0, 10.0], true, "B", &[0.0, 0.0, 0.0, -0.5]).unwrap();
        let back = rewrite(&doc);
        let sq = back.inner.entities().next().unwrap();
        assert!(closed_of(sq));
        let got = bulges_of(sq).unwrap();
        assert!((got[3] + 0.5).abs() < 1e-12 && got[0] == 0.0 && got[1] == 0.0 && got[2] == 0.0, "{:?}", got);
        let mut doc = empty_doc();
        doc.create_polyline_core(&[0.0, 0.0, 10.0, 0.0, 10.0, 10.0], false, "A", &[]).unwrap();
        assert_eq!(bulges_of(doc.inner.entities().next().unwrap()), Some(vec![0.0; 3]));
    }

    #[test]
    fn w4g6_set_vertices_refuses_before_touching_the_document() {
        let mut doc = empty_doc();
        doc.create_line_core(0.0, 0.0, 10.0, 0.0, "A").unwrap();
        doc.create_circle_core(5.0, 5.0, 2.0, "A").unwrap();
        doc.create_polyline_core(&[0.0, 0.0, 1.0, 0.0, 1.0, 1.0], false, "A", &[]).unwrap();
        let snapshot = engine_bytes(&doc);
        assert_eq!(code(doc.set_vertices_core(0, &[0.0, 0.0, 4.0], false, &[])), "points_not_pairs");
        assert_eq!(code(doc.set_vertices_core(0, &[0.0, 0.0], false, &[])), "polyline_needs_two_vertices");
        assert_eq!(code(doc.set_vertices_core(0, &[0.0, 0.0, f64::NAN, 0.0], false, &[])), "coordinate_not_finite");
        assert_eq!(code(doc.set_vertices_core(0, &[0.0, 0.0, 1.0, 0.0, 2.0, 0.0], false, &[])), "line_has_fixed_endpoints");
        assert_eq!(code(doc.set_vertices_core(0, &[3.0, 3.0, 3.0, 3.0], false, &[])), "line_zero_length");
        assert_eq!(code(doc.set_vertices_core(1, &[0.0, 0.0, 4.0, 0.0], false, &[])), "entity_kind_has_no_vertex_list");
        let too_many: Vec<f64> = vec![0.0; (MAX_CREATED_VERTICES + 1) * 2];
        assert_eq!(code(doc.set_vertices_core(2, &too_many, false, &[])), "polyline_too_many_vertices");
        assert_eq!(code(doc.set_vertices_core(9, &[0.0, 0.0, 1.0, 1.0], false, &[])), "entity_index_out_of_range");
        assert_eq!(engine_bytes(&doc), snapshot, "every refusal leaves the document byte-identical");
    }

    #[test]
    fn w4g6_set_arc_rewrites_and_refuses() {
        let mut doc = empty_doc();
        doc.create_arc_core(0.0, 0.0, 5.0, 0.0, 90.0, "A").unwrap();
        doc.create_circle_core(0.0, 0.0, 5.0, "A").unwrap();
        doc.create_line_core(0.0, 0.0, 1.0, 1.0, "A").unwrap();
        // A TRIM that keeps the arc's first 30 degrees, moved and shrunk.
        doc.set_arc_core(0, 1.0, 2.0, 3.0, 10.0, 40.0).expect("an arc takes a new sweep");
        let a = doc.inner.entities().next().unwrap();
        assert_eq!(vertices_of(a), vec![[1.0, 2.0, 0.0]]);
        assert!(near(radius_of(a).unwrap(), 3.0));
        let (s, e) = sweep_deg_of(a).unwrap();
        assert!(near(s, 10.0) && near(e, 40.0), "degrees in, degrees out: {} {}", s, e);
        let back = rewrite(&doc);
        let (s, e) = sweep_deg_of(back.inner.entities().next().unwrap()).unwrap();
        assert!(near(s, 10.0) && near(e, 40.0));
        let snapshot = engine_bytes(&doc);
        assert_eq!(code(doc.set_arc_core(0, 0.0, 0.0, 0.0, 0.0, 90.0)), "radius_not_positive");
        assert_eq!(code(doc.set_arc_core(0, 0.0, 0.0, 1.0, 30.0, 390.0)), "arc_sweep_zero");
        assert_eq!(code(doc.set_arc_core(0, f64::INFINITY, 0.0, 1.0, 0.0, 90.0)), "coordinate_not_finite");
        assert_eq!(code(doc.set_arc_core(1, 0.0, 0.0, 1.0, 0.0, 90.0)), "circle_has_no_sweep");
        assert_eq!(code(doc.set_arc_core(2, 0.0, 0.0, 1.0, 0.0, 90.0)), "entity_kind_not_an_arc");
        assert_eq!(code(doc.set_arc_core(9, 0.0, 0.0, 1.0, 0.0, 90.0)), "entity_index_out_of_range");
        assert_eq!(engine_bytes(&doc), snapshot, "every refusal leaves the document byte-identical");
    }

    #[test]
    fn w4g5d_text_moves_and_keeps_its_own_fields() {
        let mut doc = empty_doc();
        doc.create_text_core(10.0, 20.0, 2.5, 30.0, "Panel A", "Notes").unwrap();
        doc.translate_entity_core(0, 5.0, -7.0).expect("MOVE takes a TEXT");
        let e = doc.inner.entities().next().unwrap();
        assert_eq!(vertices_of(e), vec![[15.0, 13.0, 0.0]]);
        assert_eq!(text_of(e).as_deref(), Some("Panel A"));
        assert_eq!(height_of(e), Some(2.5));
        assert!((rotation_deg_of(e).unwrap() - 30.0).abs() < 1e-9);
        let back = rewrite(&doc);
        assert_eq!(vertices_of(back.inner.entities().next().unwrap()), vec![[15.0, 13.0, 0.0]]);
    }

    #[test]
    fn w4g5d_move_carries_an_aligned_texts_second_point_too() {
        // An aligned or fit text (common in real DXF) has an alignment point;
        // a hand move of the insertion point alone would tear it, which is
        // why MOVE delegates to the crate's own translate.
        let mut doc = empty_doc();
        let mut text = Text::with_value("Fit", Vector3::new(0.0, 0.0, 0.0)).with_height(1.0);
        text.alignment_point = Some(Vector3::new(10.0, 0.0, 0.0));
        doc.add_created(EntityType::Text(text), "N").unwrap();
        doc.translate_entity_core(0, 3.0, 4.0).unwrap();
        // Bound first: a match on the iterator's temporary as the tail
        // expression outlives `doc` (E0597).
        let moved = doc.inner.entities().next().unwrap();
        match moved {
            EntityType::Text(t) => {
                assert_eq!((t.insertion_point.x, t.insertion_point.y), (3.0, 4.0));
                let a = t.alignment_point.expect("alignment point kept");
                assert_eq!((a.x, a.y), (13.0, 4.0));
            }
            other => panic!("expected a TEXT, got {}", kind_name(other)),
        };
    }
}

#[cfg(test)]
mod block_definition_rows {
    use super::*;

    const LINE: &str = "0\nLINE\n5\n100\n8\n0\n10\n1\n20\n2\n30\n0\n11\n4\n21\n2\n31\n0\n";
    const CIRCLE: &str = "0\nCIRCLE\n5\n101\n8\n0\n10\n1\n20\n2\n30\n0\n40\n2\n";

    fn fixture(children: &str, with_insert: bool) -> Vec<u8> {
        let insert = if with_insert {
            "0\nINSERT\n5\n500\n8\nRefs\n2\nB\n10\n10\n20\n20\n30\n3\n41\n-2\n42\n3\n43\n4\n50\n90\n70\n2\n71\n1\n44\n10\n45\n4\n"
        } else { "" };
        format!("0\nSECTION\n2\nHEADER\n9\n$ACADVER\n1\nAC1027\n0\nENDSEC\n\
            0\nSECTION\n2\nBLOCKS\n0\nBLOCK\n5\n40\n8\n0\n2\nB\n70\n0\n10\n1\n20\n2\n30\n0\n\
            {children}0\nENDBLK\n5\n41\n8\n0\n0\nENDSEC\n\
            0\nSECTION\n2\nENTITIES\n{insert}0\nENDSEC\n0\nEOF\n").into_bytes()
    }

    fn parsed(bytes: Vec<u8>) -> ParsedDxf {
        parse_dxf_core(&bytes).unwrap()
    }

    #[test]
    fn w7b_02c_insert_creates_a_reference_that_reads_back_through_the_projection() {
        let mut doc = parsed(fixture(LINE, false));
        let h1 = doc.create_insert_core("B", 10.0, 20.0, 90.0, 2.0, 3.0, 1.0, "Refs").unwrap();
        let (written, block_base_patched) = patch_block_bases(&doc.inner, DxfWriter::new(&doc.inner).write_to_vec().unwrap());
        assert!(block_base_patched, "blockBasePatched");
        let back = parsed(written);
        let list = projected_entities(&back.inner);
        assert_eq!(list.len(), 1);
        let reference = &list[0];
        assert_eq!(reference["handle"], h1);
        assert_eq!(reference["type"], "INSERT");
        assert_eq!(reference["kind"], "REFERENCE");
        assert_eq!(reference["name"], "B");
        assert_eq!(reference["ip"], serde_json::json!([10.0, 20.0, 0.0]));
        assert_eq!(reference["rotationDeg"], 90.0);
        assert_eq!(reference["scale"], serde_json::json!([2.0, 3.0, 1.0]));
        assert_eq!(reference["layer"], "Refs");
        let blocks = block_catalogue(&back.inner, back.block_bases_unknown, &back.unknown_block_bases);
        assert_eq!(blocks[0]["base"], serde_json::json!([1.0, 2.0, 0.0]));

        // A second, identical insert gets a distinct handle.
        let h2 = doc.create_insert_core("b", 10.0, 20.0, 90.0, 2.0, 3.0, 1.0, "Refs").unwrap();
        assert_ne!(h1, h2);
        assert_eq!(projected_entities(&doc.inner).len(), 2);
    }

    #[test]
    fn w7b_02c_insert_refuses_before_touching_the_document() {
        let mut doc = parsed(fixture(LINE, false));
        let before = DxfWriter::new(&doc.inner).write_to_vec().unwrap();
        assert_eq!(doc.create_insert_core("B", f64::NAN, 20.0, 0.0, 1.0, 1.0, 1.0, "").unwrap_err(), "coordinate_not_finite");
        assert_eq!(doc.create_insert_core("B", 10.0, 20.0, 0.0, 0.0, 1.0, 1.0, "").unwrap_err(), "insert_scale_zero");
        assert_eq!(doc.create_insert_core("B", 10.0, 20.0, 0.0, 1.0, 0.0, 1.0, "").unwrap_err(), "insert_scale_zero");
        assert_eq!(doc.create_insert_core("", 10.0, 20.0, 0.0, 1.0, 1.0, 1.0, "").unwrap_err(), "insert_name_invalid");
        assert_eq!(doc.create_insert_core("   ", 10.0, 20.0, 0.0, 1.0, 1.0, 1.0, "").unwrap_err(), "insert_name_invalid");
        assert_eq!(doc.create_insert_core("*U1", 10.0, 20.0, 0.0, 1.0, 1.0, 1.0, "").unwrap_err(), "insert_name_invalid");
        assert_eq!(doc.create_insert_core("Nope", 10.0, 20.0, 0.0, 1.0, 1.0, 1.0, "").unwrap_err(), "block_not_defined:Nope");
        assert_eq!(DxfWriter::new(&doc.inner).write_to_vec().unwrap(), before, "every refusal leaves the document untouched, and the handles too");
        assert!(projected_entities(&doc.inner).is_empty());

        // An incomplete block (more children than the cap) refuses too, and touches nothing.
        let children: String = (0..61).map(|i| LINE.replace("100\n", &format!("{:X}\n", 0x100 + i))).collect();
        let mut many = parsed(fixture(&children, false));
        let many_before = DxfWriter::new(&many.inner).write_to_vec().unwrap();
        assert_eq!(many.create_insert_core("B", 0.0, 0.0, 0.0, 1.0, 1.0, 1.0, "").unwrap_err(), "block_incomplete:B");
        assert_eq!(DxfWriter::new(&many.inner).write_to_vec().unwrap(), many_before);
    }

    #[test]
    fn w7b_02c_f_insert_matches_a_padded_record_name_trimmed_to_trimmed() {
        let mut doc = parsed(fixture(LINE, false));
        // The DXF text path would trim group code 2, so push the record's own
        // spelling (trailing space) directly, the same way the 02c-f row for
        // the store and the ghost seed a definition with incidental whitespace.
        doc.inner.block_records.get_mut("B").unwrap().name = "Fixture ".to_string();
        let h1 = doc.create_insert_core("Fixture", 10.0, 20.0, 0.0, 1.0, 1.0, 1.0, "").unwrap();
        let list = projected_entities(&doc.inner);
        assert_eq!(list[0]["name"], "Fixture ", "the created INSERT carries the record's own spelling");
        let h2 = doc.create_insert_core(" fixture ", 10.0, 20.0, 0.0, 1.0, 1.0, 1.0, "").unwrap();
        assert_ne!(h1, h2);
        assert_eq!(doc.create_insert_core("Nope", 10.0, 20.0, 0.0, 1.0, 1.0, 1.0, "").unwrap_err(), "block_not_defined:Nope");
    }

    #[test]
    fn w7b_01c_children_are_owned_and_insert_fields_are_exact() {
        let mut doc = parsed(fixture(&format!("{LINE}{CIRCLE}"), true));
        let list = projected_entities(&doc.inner);
        assert_eq!(list.len(), 1);
        let reference = &list[0];
        assert_eq!(reference["handle"], "1280");
        assert_eq!(reference["type"], "INSERT");
        assert_eq!(reference["kind"], "REFERENCE");
        assert_eq!(reference["name"], "B");
        assert_eq!(reference["ip"], serde_json::json!([10.0, 20.0, 3.0]));
        assert_eq!(reference["scale"], serde_json::json!([-2.0, 3.0, 4.0]));
        assert_eq!(reference["columns"], 2);
        assert_eq!(reference["rows"], 1);
        assert_eq!(reference["columnSpacing"], 10.0);
        assert_eq!(reference["rowSpacing"], 4.0);
        assert_eq!(reference["rotationDeg"], 90.0);
        assert_eq!(reference["layer"], "Refs");
        assert_eq!(reference["editable"], false);
        let index = reference["index"].as_u64().unwrap() as usize;
        let before = DxfWriter::new(&doc.inner).write_to_vec().unwrap();
        assert_eq!(doc.translate_entity_core(index, 1.0, 2.0).unwrap_err(), INSERT_NOT_EDITABLE);
        assert_eq!(doc.copy_entity_core(index, 1.0, 2.0).unwrap_err(), INSERT_NOT_EDITABLE);
        let child_index = doc.inner.entities().position(|e| e.common().handle == Handle::new(0x100)).unwrap();
        assert_eq!(doc.delete_entity_core(child_index).unwrap_err(), "block_child_not_editable");
        assert_eq!(doc.translate_entity_core(child_index, 1.0, 2.0).unwrap_err(), "block_child_not_editable");
        assert_eq!(doc.copy_entity_core(child_index, 1.0, 2.0).unwrap_err(), "block_child_not_editable");
        assert_eq!(DxfWriter::new(&doc.inner).write_to_vec().unwrap(), before);
        // W4g-7b-05c-2: ERASE stays allowed on a placed INSERT, unlike every
        // other verb above; index is still valid, nothing before mutated it.
        assert!(doc.delete_entity_core(index).is_ok(), "delete is allowed on an INSERT unlike every other verb");
        assert!(doc.inner.get_entity(Handle::new(1280)).is_none());
        let no_insert = parsed(fixture(&format!("{LINE}{CIRCLE}"), false));
        assert!(projected_entities(&no_insert.inner).is_empty());
        let blocks = block_catalogue(&doc.inner, doc.block_bases_unknown, &doc.unknown_block_bases);
        assert_eq!(blocks.len(), 1);
        assert_eq!(blocks[0]["base"], serde_json::json!([1.0, 2.0, 0.0]));
        assert_eq!(blocks[0]["complete"], true);
        let children = blocks[0]["children"].as_array().unwrap();
        assert_eq!(children.len(), 2);
        assert!(children.iter().all(|child| child["editable"] == false));
        assert_eq!(children[0]["vertices"], serde_json::json!([[1.0, 2.0, 0.0], [4.0, 2.0, 0.0]]));
    }

    #[test]
    fn w7b_01c_catalogue_caps_children_and_marks_unsupported_definitions() {
        let children: String = (0..61).map(|i| LINE.replace("100\n", &format!("{:X}\n", 0x100 + i))).collect();
        let doc = parsed(fixture(&children, true));
        let blocks = block_catalogue(&doc.inner, doc.block_bases_unknown, &doc.unknown_block_bases);
        assert_eq!(blocks[0]["children"].as_array().unwrap().len(), BLOCK_CHILD_CAP);
        assert_eq!(blocks[0]["complete"], false);
        for unsupported in [
            "0\nINSERT\n5\n100\n8\n0\n2\nB\n10\n1\n20\n2\n30\n0\n",
            "0\nPOINT\n5\n100\n8\n0\n10\n1\n20\n2\n30\n0\n",
            "0\nATTDEF\n5\n100\n8\n0\n10\n1\n20\n2\n30\n0\n40\n1\n1\nvalue\n2\nTAG\n3\nprompt\n70\n0\n",
        ] {
            let doc = parsed(fixture(unsupported, true));
            let blocks = block_catalogue(&doc.inner, doc.block_bases_unknown, &doc.unknown_block_bases);
            assert_eq!(blocks[0]["complete"], false);
            assert!(blocks[0]["children"].as_array().unwrap().is_empty());
            assert_eq!(projected_entities(&doc.inner).len(), 1);
        }
    }

    #[test]
    fn w7b_01c_delete_then_move_resolves_the_validated_handle() {
        let model: String = (0..3).map(|i| format!(
            "0\nLINE\n5\n{:X}\n8\n0\n10\n{}\n20\n0\n30\n0\n11\n{}\n21\n0\n31\n0\n", 0x600 + i, 10 + i * 10, 11 + i * 10)).collect();
        let source = String::from_utf8(fixture(LINE, false)).unwrap().replace("2\nENTITIES\n", &format!("2\nENTITIES\n{model}"));
        let mut doc = parsed(source.into_bytes());
        let index = |doc: &ParsedDxf, handle: u64| doc.inner.entities().position(|e| e.common().handle == Handle::new(handle)).unwrap();
        let marker = doc.inner.block_records.get("B").unwrap().block_entity_handle;
        let untouched: Vec<_> = [Handle::new(0x100), Handle::new(0x601), marker].iter()
            .map(|h| format!("{:?}", doc.inner.get_entity(*h).unwrap())).collect();
        doc.delete_entity_core(index(&doc, 0x600)).unwrap();
        doc.translate_entity_core(index(&doc, 0x602), 5.0, 0.0).unwrap();
        assert!(doc.inner.get_entity(Handle::new(0x600)).is_none());
        assert_eq!(vertices_of(doc.inner.get_entity(Handle::new(0x602)).unwrap()), vec![[35.0, 0.0, 0.0], [36.0, 0.0, 0.0]]);
        for (i, handle) in [Handle::new(0x100), Handle::new(0x601), marker].iter().enumerate() {
            assert_eq!(format!("{:?}", doc.inner.get_entity(*handle).unwrap()), untouched[i]);
        }
    }

    #[test]
    fn w7b_01c_digest_covers_uncapped_and_unsupported_children_and_properties() {
        let children: String = (0..61).map(|i| LINE.replace("100\n", &format!("{:X}\n", 0x100 + i))).collect();
        let mut doc = parsed(fixture(&children, true));
        let before = block_catalogue(&doc.inner, false, &doc.unknown_block_bases);
        let (written, patched) = patch_block_bases(&doc.inner, DxfWriter::new(&doc.inner).write_to_vec().unwrap());
        assert!(patched);
        let back = parsed(written);
        assert_eq!(before, block_catalogue(&back.inner, false, &back.unknown_block_bases));
        doc.inner.get_entity_mut(Handle::new(0x100 + 60)).unwrap().translate(Vector3::new(5.0, 0.0, 0.0));
        let after = block_catalogue(&doc.inner, false, &doc.unknown_block_bases);
        assert_eq!(before[0]["children"], after[0]["children"]);
        assert_ne!(before[0]["digest"], after[0]["digest"]);
        assert_eq!(before[0]["digest"].as_str().unwrap().len(), 16);

        let mut doc = parsed(fixture("0\nPOINT\n5\n100\n8\n0\n10\n1\n20\n2\n30\n0\n", true));
        let before = block_catalogue(&doc.inner, false, &doc.unknown_block_bases);
        doc.inner.get_entity_mut(Handle::new(0x100)).unwrap().translate(Vector3::new(5.0, 0.0, 0.0));
        let after = block_catalogue(&doc.inner, false, &doc.unknown_block_bases);
        assert_eq!(before[0]["children"], after[0]["children"]);
        assert_ne!(before[0]["digest"], after[0]["digest"]);

        let mut doc = parsed(fixture(LINE, true));
        let before = block_catalogue(&doc.inner, false, &doc.unknown_block_bases);
        if let Some(EntityType::Line(line)) = doc.inner.get_entity_mut(Handle::new(0x100)) { line.thickness = 7.0; }
        assert_ne!(before[0]["digest"], block_catalogue(&doc.inner, false, &doc.unknown_block_bases)[0]["digest"]);
    }

    fn binary_fixture(ascii: &[u8]) -> Vec<u8> {
        let mut bytes = Vec::new();
        let mut writer = acadrust::io::dxf::DxfBinaryWriter::new(&mut bytes).unwrap();
        let source = std::str::from_utf8(ascii).unwrap();
        let lines: Vec<_> = source.lines().collect();
        for pair in lines.chunks_exact(2) {
            let code: i32 = pair[0].parse().unwrap();
            match code {
                10..=59 => writer.write_double(code, pair[1].parse().unwrap()).unwrap(),
                60..=79 => writer.write_i16(code, pair[1].parse().unwrap()).unwrap(),
                _ => writer.write_string(code, pair[1]).unwrap(),
            }
        }
        bytes
    }

    #[test]
    fn w7b_01c_case_collisions_refuse_before_membership_can_leak() {
        let source = String::from_utf8(fixture(LINE, true)).unwrap();
        let collision = source.replace("0\nENDSEC\n0\nSECTION\n2\nENTITIES", &format!(
            "0\nBLOCK\n5\n42\n8\n0\n2\nb\n70\n0\n10\n1\n20\n2\n30\n0\n{}0\nENDBLK\n5\n43\n8\n0\n0\nENDSEC\n0\nSECTION\n2\nENTITIES", LINE.replace("100\n", "101\n")));
        for bytes in [collision.as_bytes().to_vec(), binary_fixture(collision.as_bytes())] {
            assert_eq!(parse_dxf_core(&bytes).err().unwrap(), "block definitions collapsed on load: 2 in the file, 1 retained");
        }
        assert!(parse_dxf_core(source.as_bytes()).is_ok());
    }

    #[test]
    fn w7b_01c_binary_blocks_have_unknown_bases() {
        let doc = parsed(binary_fixture(&fixture(LINE, true)));
        assert!(doc.block_bases_unknown);
        let blocks = block_catalogue(&doc.inner, doc.block_bases_unknown, &doc.unknown_block_bases);
        assert_eq!(blocks[0]["baseUnknown"], true);
        assert_eq!(blocks[0]["complete"], false);
        assert_eq!(blocks[0]["children"].as_array().unwrap().len(), 1);
        assert_eq!(projected_entities(&doc.inner)[0]["type"], "INSERT");
    }

    #[test]
    fn create_block_after_binary_load_refuses_before_mutation() {
        let source = b"0\nSECTION\n2\nENTITIES\n0\nLINE\n5\n10\n8\n0\n10\n12\n20\n23\n11\n17\n21\n23\n0\nENDSEC\n0\nEOF\n";
        let mut doc = parsed(binary_fixture(source));
        assert!(doc.block_bases_unknown);
        let before = DxfWriter::new(&doc.inner).write_to_vec().unwrap();
        assert_eq!(doc.create_block_core("B", [10.0, 20.0, 0.0], &["16".to_string()], "0").unwrap_err(),
            "block_bases_unknown: the drawing's block bases are unknown after a binary load; save as ASCII DXF first");
        assert_eq!(before, DxfWriter::new(&doc.inner).write_to_vec().unwrap());
    }

    #[test]
    fn w7b_01c_decoded_name_collapses_are_counted_on_raw_bytes() {
        let source = String::from_utf8(fixture(LINE, true)).unwrap();
        let pair = source.replace("0\nENDSEC\n0\nSECTION\n2\nENTITIES", &format!(
            "0\nBLOCK\n5\n42\n8\n0\n2\nOTHER\n70\n0\n10\n1\n20\n2\n30\n0\n{}0\nENDBLK\n5\n43\n8\n0\n0\nENDSEC\n0\nSECTION\n2\nENTITIES", LINE.replace("100\n", "101\n")));
        let caret = pair.replace("2\nB\n", "2\nB^ B\n").replace("2\nOTHER\n", "2\nB^B\n");
        assert_eq!(raw_block_definition_count(caret.as_bytes()).unwrap(), 2);
        assert_eq!(parse_dxf_core(caret.as_bytes()).err().unwrap(), "block definitions collapsed on load: 2 in the file, 1 retained");

        // Binary string decoding is lossy UTF-8: distinct Latin-1 bytes become
        // the same retained name. Counting the raw definitions still sees two.
        let mut latin1 = binary_fixture(pair.replace("2\nOTHER\n", "2\nC\n").as_bytes());
        for i in 2..latin1.len() - 1 {
            if latin1[i - 2..i] == [2, 0] && latin1[i + 1] == 0 {
                if latin1[i] == b'B' { latin1[i] = 0xe9; }
                else if latin1[i] == b'C' { latin1[i] = 0xe8; }
            }
        }
        assert_eq!(raw_block_definition_count(&latin1).unwrap(), 2);
        assert_eq!(parse_dxf_core(&latin1).err().unwrap(), "block definitions collapsed on load: 2 in the file, 1 retained");
    }

    #[test]
    fn w7b_01c_text_bases_survive_latin1_descriptions_and_caret_names() {
        let source = String::from_utf8(fixture(LINE, true)).unwrap();
        let latin1: Vec<u8> = source.replace("2\nB\n70\n", "2\nB\n4\ncafé\n70\n").chars().map(|c| c as u8).collect();
        let caret = source.replace("2\nB\n", "2\nB^ B\n").into_bytes();
        for bytes in [latin1, caret] {
            let doc = parsed(bytes);
            assert!(doc.unknown_block_bases.is_empty());
            let blocks = block_catalogue(&doc.inner, false, &doc.unknown_block_bases);
            assert_eq!(blocks[0]["base"], serde_json::json!([1.0, 2.0, 0.0]));
            assert_eq!(blocks[0]["complete"], true);
            assert_eq!(blocks[0]["baseUnknown"], false);
            assert_eq!(projected_entities(&doc.inner).len(), 1);
            assert_eq!(projected_entities(&doc.inner)[0]["type"], "INSERT");
            let (written, patched) = patch_block_bases(&doc.inner, DxfWriter::new(&doc.inner).write_to_vec().unwrap());
            assert!(patched);
            let back = parsed(written);
            assert_eq!(blocks, block_catalogue(&back.inner, false, &back.unknown_block_bases));
        }
    }

    #[test]
    fn w7b_01c_unmatched_marker_marks_only_its_definition_unknown() {
        let source = String::from_utf8(fixture(LINE, true)).unwrap().replace("0\nENDSEC\n0\nSECTION\n2\nENTITIES", &format!(
            "0\nBLOCK\n5\n42\n8\n0\n2\nC\n70\n0\n10\n7\n20\n8\n30\n0\n{}0\nENDBLK\n5\n43\n8\n0\n0\nENDSEC\n0\nSECTION\n2\nENTITIES", LINE.replace("100\n", "101\n")));
        let mut doc = parsed(source.as_bytes().to_vec());
        doc.inner.block_records.get_mut("B").unwrap().block_entity_handle = Handle::new(0x99);
        doc.unknown_block_bases = retain_block_bases(&mut doc.inner, source.as_bytes()).unwrap();
        assert_eq!(doc.unknown_block_bases, HashSet::from(["B".to_string()]));
        let blocks = block_catalogue(&doc.inner, false, &doc.unknown_block_bases);
        let b = blocks.iter().find(|b| b["name"] == "B").unwrap();
        let c = blocks.iter().find(|b| b["name"] == "C").unwrap();
        assert_eq!(b["baseUnknown"], true);
        assert_eq!(b["complete"], false);
        assert_eq!(c["baseUnknown"], false);
        assert_eq!(c["complete"], true);
        assert_eq!(c["base"], serde_json::json!([7.0, 8.0, 0.0]));
        let (written, _) = patch_block_bases(&doc.inner, DxfWriter::new(&doc.inner).write_to_vec().unwrap());
        let mut back = parsed(written);
        back.inherit_block_base_unknowns(&doc);
        assert_eq!(back.unknown_block_bases, doc.unknown_block_bases);
    }

    #[test]
    fn w7b_01c_digest_uses_written_defaults_and_ignores_allocated_handles() {
        let doc = parsed(fixture(&LINE.replace("8\n0\n", "8\n0\n6\nByLayer\n"), true));
        assert_eq!(doc.inner.get_entity(Handle::new(0x100)).unwrap().common().linetype, "ByLayer");
        let before = block_catalogue(&doc.inner, false, &doc.unknown_block_bases);
        let (written, patched) = patch_block_bases(&doc.inner, DxfWriter::new(&doc.inner).write_to_vec().unwrap());
        assert!(patched);
        let mut back = parsed(written);
        assert_eq!(back.inner.get_entity(Handle::new(0x100)).unwrap().common().linetype, "");
        assert_eq!(before[0]["digest"], block_catalogue(&back.inner, false, &back.unknown_block_bases)[0]["digest"]);
        let renumbered = parsed(fixture(&LINE.replace("5\n100\n", "5\n900\n"), true));
        assert_eq!(before[0]["digest"], block_catalogue(&renumbered.inner, false, &renumbered.unknown_block_bases)[0]["digest"]);
        back.inner.get_entity_mut(Handle::new(0x100)).unwrap().translate(Vector3::new(5.0, 0.0, 0.0));
        assert_ne!(before[0]["digest"], block_catalogue(&back.inner, false, &back.unknown_block_bases)[0]["digest"]);
    }

    #[test]
    fn w7b_01c_array_spacing_matches_the_pinned_crate_expansion() {
        let line = EntityType::Line(Line::from_coords(0.0, 0.0, 0.0, 1.0, 0.0, 0.0));
        let insert = acadrust::entities::Insert::new("B", Vector3::new(10.0, 20.0, 0.0))
            .with_scale(2.0, 1.0, 1.0).with_array(2, 1, 10.0, 4.0);
        let expanded = insert.explode(&[line.clone()]);
        assert_eq!(vertices_of(&expanded[1]), vec![[20.0, 20.0, 0.0], [22.0, 20.0, 0.0]]);
        let rotated = insert.with_scale(-2.0, 3.0, 1.0).with_rotation(std::f64::consts::FRAC_PI_2).with_array(2, 2, 10.0, 4.0);
        let expanded = rotated.explode(&[line]);
        for (entity, [x, y]) in expanded.iter().zip([[10.0, 20.0], [20.0, 20.0], [10.0, 24.0], [20.0, 24.0]]) {
            let vertices = vertices_of(entity);
            assert!((vertices[0][0] - x).abs() < 1e-9 && (vertices[0][1] - y).abs() < 1e-9);
            assert!((vertices[1][0] - x).abs() < 1e-9 && (vertices[1][1] - (y - 2.0)).abs() < 1e-9);
        }
    }

    #[test]
    fn w7b_01c_base_patch_pins_the_writer_defect_and_preserves_other_bytes() {
        let doc = parsed(fixture(&format!("{LINE}{CIRCLE}"), true));
        let original = DxfWriter::new(&doc.inner).write_to_vec().unwrap();
        let unpatched = parsed(original.clone());
        assert_eq!(block_base(&unpatched.inner, unpatched.inner.block_records.get("B").unwrap()), [0.0; 3]);
        let (patched, ok) = patch_block_bases(&doc.inner, original.clone());
        assert!(ok);
        let back = parsed(patched.clone());
        assert_eq!(block_base(&back.inner, back.inner.block_records.get("B").unwrap()), [1.0, 2.0, 0.0]);
        let raw = String::from_utf8(original).unwrap();
        let actual = String::from_utf8(patched).unwrap();
        let prefix = "  2\r\nB\r\n 70\r\n     0\r\n";
        let old = format!("{prefix} 10\r\n0.0\r\n 20\r\n0.0\r\n 30\r\n0.0\r\n");
        let new = format!("{prefix} 10\r\n1.0\r\n 20\r\n2.0\r\n 30\r\n0.0\r\n");
        assert!(raw.contains(&old));
        assert_eq!(actual, raw.replacen(&old, &new, 1));
        // A drifted BLOCK layout refuses the entire pass, never a partial patch.
        let malformed = raw.replacen(&old, &format!("{prefix} 11\r\n0.0\r\n 20\r\n0.0\r\n 30\r\n0.0\r\n"), 1).into_bytes();
        let (unchanged, ok) = patch_block_bases(&doc.inner, malformed.clone());
        assert!(!ok);
        assert_eq!(unchanged, malformed);
    }
}

// ---------------------------------------------------------------------------
// W4g-7b-03c: colour, linetype and lineweight on the property setters and
// the projection. Native, off the cores directly (no JsValue off wasm32).
// ---------------------------------------------------------------------------
#[cfg(test)]
mod w4g_7b_03c_property_verbs {
    use super::*;

    fn empty_doc() -> ParsedDxf {
        ParsedDxf { inner: CadDocument::new(), group_names: Vec::new(), block_base_patched: Cell::new(false), block_bases_unknown: false, unknown_block_bases: HashSet::new(), mlstyle_segments: HashMap::new() }
    }

    fn code<T>(result: Result<T, Refusal>) -> String {
        match result {
            Ok(_) => "OK".to_string(),
            Err(code) => code,
        }
    }

    fn reparse(doc: &ParsedDxf) -> ParsedDxf {
        let bytes = DxfWriter::new(&doc.inner).write_to_vec().expect("writer serializes the document");
        let inner = DxfReader::from_reader(std::io::Cursor::new(bytes))
            .expect("reader accepts the written bytes")
            .read()
            .expect("written bytes re-parse");
        ParsedDxf { group_names: group_names(&inner), inner, block_base_patched: Cell::new(false), block_bases_unknown: doc.block_bases_unknown, unknown_block_bases: doc.unknown_block_bases.clone(), mlstyle_segments: doc.mlstyle_segments.clone() }
    }

    #[test]
    fn w4g_7b_03c_set_color_writes_the_aci_and_the_projection_reflects_it() {
        let mut doc = empty_doc();
        doc.create_line_core(0.0, 0.0, 10.0, 0.0, "").expect("line");
        doc.set_entity_color_core(0, 1).expect("aci 1 is valid");
        let record = &projected_entities(&doc.inner)[0];
        assert_eq!(record["aci"], 1);
        assert_eq!(record["trueColor"], serde_json::Value::Null);
        let back = reparse(&doc);
        assert_eq!(back.inner.entities().next().unwrap().common().color, Color::Index(1));
    }

    #[test]
    fn w4g_7b_03c_set_color_refuses_out_of_range_before_touching_the_document() {
        let mut doc = empty_doc();
        doc.create_line_core(0.0, 0.0, 10.0, 0.0, "").expect("line");
        assert_eq!(code(doc.set_entity_color_core(0, 300)), "color_index_out_of_range");
        assert_eq!(doc.inner.entities().next().unwrap().common().color, Color::ByLayer);
    }

    #[test]
    fn w4g_7b_03c_set_color_clears_a_true_colour_as_autocad_does() {
        let mut doc = empty_doc();
        doc.create_line_core(0.0, 0.0, 10.0, 0.0, "").expect("line");
        doc.entity_mut(0).unwrap().common_mut().color = Color::from_rgb(10, 20, 30);
        let record = &projected_entities(&doc.inner)[0];
        assert_eq!(record["trueColor"], serde_json::json!([10, 20, 30]));
        doc.set_entity_color_core(0, 1).expect("aci 1 is valid");
        let record = &projected_entities(&doc.inner)[0];
        assert_eq!(record["aci"], 1);
        assert_eq!(record["trueColor"], serde_json::Value::Null);
        // The 420 group is gone after write + re-parse.
        let back = reparse(&doc);
        assert!(!back.inner.entities().next().unwrap().common().color.is_true_color());
    }

    #[test]
    fn w4g_7b_03c_set_linetype_stores_the_tables_own_spelling() {
        let mut doc = empty_doc();
        doc.create_line_core(0.0, 0.0, 10.0, 0.0, "").expect("line");
        doc.inner.line_types.add(acadrust::tables::LineType::new("DASHED")).expect("linetype added");
        doc.set_entity_linetype_core(0, "dashed").expect("case-insensitive match");
        assert_eq!(doc.inner.entities().next().unwrap().common().linetype, "DASHED");
        let record = &projected_entities(&doc.inner)[0];
        assert_eq!(record["linetype"], "DASHED");
        // The 6 group survives write + re-parse with the table's own case.
        let back = reparse(&doc);
        assert_eq!(back.inner.entities().next().unwrap().common().linetype, "DASHED");
    }

    #[test]
    fn w4g_7b_03c_set_linetype_preserves_whitespace_identity() {
        let mut doc = empty_doc();
        doc.create_line_core(0.0, 0.0, 10.0, 0.0, "").expect("line");
        for name in ["ZZZ", "ZZZ "] {
            doc.inner.line_types.add(acadrust::tables::LineType::new(name)).expect("linetype added");
        }
        doc.set_entity_linetype_core(0, "ZZZ ").expect("exact trailing space");
        assert_eq!(doc.inner.entities().next().unwrap().common().linetype, "ZZZ ");
        doc.set_entity_linetype_core(0, "ZZZ").expect("exact bare name");
        assert_eq!(doc.inner.entities().next().unwrap().common().linetype, "ZZZ");
        assert_eq!(code(doc.set_entity_linetype_core(0, " ZZZ")), "linetype_not_loaded: ZZZ");
        assert_eq!(doc.inner.entities().next().unwrap().common().linetype, "ZZZ");
    }

    #[test]
    fn w4g_7b_03c_set_linetype_refuses_an_unloaded_name() {
        let mut doc = empty_doc();
        doc.create_line_core(0.0, 0.0, 10.0, 0.0, "").expect("line");
        assert_eq!(code(doc.set_entity_linetype_core(0, "Hidden2")), "linetype_not_loaded:Hidden2");
    }

    #[test]
    fn w4g_7b_03c_set_lineweight_accepts_the_enumeration_and_refuses_off_grid_values() {
        let mut doc = empty_doc();
        doc.create_line_core(0.0, 0.0, 10.0, 0.0, "").expect("line");
        doc.set_entity_lineweight_core(0, 25).expect("0.25mm is a standard value");
        let record = &projected_entities(&doc.inner)[0];
        assert_eq!(record["lineweight"], 25);
        // The 370 group survives write + re-parse.
        let back = reparse(&doc);
        assert_eq!(back.inner.entities().next().unwrap().common().line_weight, LineWeight::Value(25));
        assert_eq!(code(doc.set_entity_lineweight_core(0, 26)), "lineweight_not_valid:26");
        doc.set_entity_lineweight_core(0, -1).expect("-1 is ByLayer");
        assert_eq!(doc.inner.entities().next().unwrap().common().line_weight, LineWeight::ByLayer);
    }

    #[test]
    fn w4g_7b_03c_set_lineweight_refusal_leaves_the_document_untouched() {
        let mut doc = empty_doc();
        doc.create_line_core(0.0, 0.0, 10.0, 0.0, "").expect("line");
        doc.set_entity_lineweight_core(0, 25).expect("0.25mm is a standard value");
        assert_eq!(code(doc.set_entity_lineweight_core(0, 26)), "lineweight_not_valid:26");
        // The refusal happened before any write: the value from before it stands.
        assert_eq!(doc.inner.entities().next().unwrap().common().line_weight, LineWeight::Value(25));
    }

    #[test]
    fn w4g_7b_03c_linetypes_catalogue_is_sorted_bounded_and_always_carries_the_defaults() {
        let mut doc = empty_doc();
        doc.inner.line_types.add(acadrust::tables::LineType::new("ZIGZAG")).expect("linetype added");
        doc.inner.line_types.add(acadrust::tables::LineType::new("dashed")).expect("linetype added");
        let (names, truncated) = linetypes_catalogue(&doc.inner);
        assert!(!truncated);
        assert!(names.iter().any(|n| n.eq_ignore_ascii_case("ByLayer")));
        assert!(names.iter().any(|n| n.eq_ignore_ascii_case("ByBlock")));
        assert!(names.iter().any(|n| n.eq_ignore_ascii_case("Continuous")));
        assert!(names.iter().any(|n| n == "ZIGZAG"));
        let lowered: Vec<String> = names.iter().map(|n| n.to_lowercase()).collect();
        let mut sorted = lowered.clone();
        sorted.sort();
        assert_eq!(lowered, sorted, "the catalogue is sorted case-insensitively");
        assert!(names.len() <= LINETYPE_CATALOGUE_CAP);
    }

    #[test]
    fn w4g_7b_linetypes_catalogue_reports_truncation_and_retains_defaults() {
        let mut doc = empty_doc();
        let (names, truncated) = linetypes_catalogue(&doc.inner);
        assert_eq!(names.len(), 3);
        assert!(!truncated);
        for index in 0..198 {
            doc.inner.line_types.add(acadrust::tables::LineType::new(&format!("A{index:03}"))).expect("linetype added");
        }
        let (names, truncated) = linetypes_catalogue(&doc.inner);
        assert_eq!(names.len(), 200);
        assert!(truncated);
        for required in ["ByLayer", "ByBlock", "Continuous"] {
            assert!(names.iter().any(|n| n.eq_ignore_ascii_case(required)));
        }
        assert!(names.windows(2).all(|pair| pair[0].to_lowercase() <= pair[1].to_lowercase()));
    }

    #[test]
    fn w4g_7b_03c_property_setters_accept_an_insert_reference_but_not_a_block_child() {
        // ENTITIES precedes BLOCKS here (both are ordinary section reads, and
        // the crate resolves an INSERT's block name lazily, so this parses
        // fine): the crate pushes a BLOCK's children into the document's flat
        // entity vec at that block's ENDBLK, so an ENTITIES-first fixture
        // gives the INSERT index 0 and the LINE the next index, exercising
        // the gate on both without hardcoding the LINE's index.
        let bytes = "0\nSECTION\n2\nHEADER\n9\n$ACADVER\n1\nAC1027\n0\nENDSEC\n\
            0\nSECTION\n2\nENTITIES\n0\nINSERT\n5\n500\n8\nRefs\n2\nB\n10\n10\n20\n20\n30\n0\n0\nENDSEC\n\
            0\nSECTION\n2\nBLOCKS\n0\nBLOCK\n5\n40\n8\n0\n2\nB\n70\n0\n10\n1\n20\n2\n30\n0\n\
            0\nLINE\n5\n100\n8\n0\n10\n1\n20\n2\n30\n0\n11\n4\n21\n2\n31\n0\n0\nENDBLK\n5\n41\n8\n0\n0\nENDSEC\n0\nEOF\n"
            .as_bytes().to_vec();
        let mut doc = parse_dxf_core(&bytes).expect("fixture parses");
        doc.set_entity_color_core(0, 1).expect("an INSERT reference accepts a colour");
        let child_index = doc.inner.entities().position(|e| matches!(e, EntityType::Line(_)))
            .expect("block child present");
        assert_eq!(code(doc.set_entity_color_core(child_index, 1)), "block_child_not_editable");
    }

    // Required row (2026-09-07 04:25Z): an entity the browser session never
    // touches keeps its EXPLICIT 62/6/370/420 groups exactly, through parse
    // -> one unrelated edit elsewhere -> write -> re-parse. The plan route's
    // preflight (server/routers/drawings.py) compares the head's dense EP
    // record against the browser-written DXF for every entity the plan does
    // not name, so a silent drop here would desync that comparison.
    #[test]
    fn w4g_7b_03c_untouched_property_groups_survive_a_write_and_reparse() {
        let bytes = "0\nSECTION\n2\nHEADER\n9\n$ACADVER\n1\nAC1027\n0\nENDSEC\n\
            0\nSECTION\n2\nENTITIES\n\
            0\nLINE\n5\n64\n8\n0\n6\nContinuous\n62\n3\n370\n25\n10\n20\n20\n0\n30\n0\n11\n25\n21\n0\n31\n0\n\
            0\nLINE\n5\n65\n8\n0\n420\n660510\n10\n30\n20\n0\n30\n0\n11\n35\n21\n0\n31\n0\n\
            0\nENDSEC\n0\nEOF\n"
            .as_bytes().to_vec();
        let mut doc = parse_dxf_core(&bytes).expect("fixture with explicit property groups parses");
        let before = projected_entities(&doc.inner);
        let a = before.iter().find(|e| e["handle"] == "100").expect("entity 100 in the projection");
        assert_eq!(a["aci"], 3);
        assert_eq!(a["linetype"], "Continuous");
        assert_eq!(a["lineweight"], 25);
        let b = before.iter().find(|e| e["handle"] == "101").expect("entity 101 in the projection");
        assert_eq!(b["trueColor"], serde_json::json!([10, 20, 30]));
        // ONE unrelated edit, touching neither entity above.
        doc.create_line_core(50.0, 50.0, 60.0, 50.0, "0").expect("unrelated line");
        let back = reparse(&doc);
        let after = projected_entities(&back.inner);
        let a2 = after.iter().find(|e| e["handle"] == "100").expect("entity 100 survives the round trip");
        assert_eq!(a2["aci"], 3);
        assert_eq!(a2["linetype"], "Continuous");
        assert_eq!(a2["lineweight"], 25);
        assert_eq!(a2["trueColor"], serde_json::Value::Null);
        let b2 = after.iter().find(|e| e["handle"] == "101").expect("entity 101 survives the round trip");
        assert_eq!(b2["trueColor"], serde_json::json!([10, 20, 30]));
    }
}

#[cfg(test)]
mod w4g_7b_04c_dimension_rows {
    use super::*;
    use acadrust::entities::DimensionRadius;

    fn empty_doc() -> ParsedDxf {
        ParsedDxf { inner: CadDocument::new(), group_names: Vec::new(), block_base_patched: Cell::new(false), block_bases_unknown: false, unknown_block_bases: HashSet::new(), mlstyle_segments: HashMap::new() }
    }

    fn code<T>(result: Result<T, Refusal>) -> String {
        match result {
            Ok(_) => "OK".to_string(),
            Err(code) => code,
        }
    }

    fn reparse(doc: &ParsedDxf) -> ParsedDxf {
        let bytes = DxfWriter::new(&doc.inner).write_to_vec().expect("writer serializes the document");
        let inner = DxfReader::from_reader(std::io::Cursor::new(bytes))
            .expect("reader accepts the written bytes")
            .read()
            .expect("written bytes re-parse");
        ParsedDxf { group_names: group_names(&inner), inner, block_base_patched: Cell::new(false), block_bases_unknown: doc.block_bases_unknown, unknown_block_bases: doc.unknown_block_bases.clone(), mlstyle_segments: doc.mlstyle_segments.clone() }
    }

    fn near(a: f64, b: f64) -> bool {
        (a - b).abs() < 1e-9
    }

    // (0,0)-(3,4): ALIGNED measures the chord (5); LINEAR at 0deg the x
    // projection (3); LINEAR at 90deg the y projection (4). Never 5 for
    // either LINEAR case, which is what reading the cached
    // `actual_measurement` (always the chord) instead of `measurement()`
    // would wrongly report.
    #[test]
    fn measurement_is_computed_never_the_cached_actual_measurement() {
        let mut doc = empty_doc();
        let aligned = doc.create_dimension_core("ALIGNED", 0.0, 0.0, 3.0, 4.0, 1.5, 6.0, 0.0, "Standard", "").expect("aligned");
        let linear0 = doc.create_dimension_core("LINEAR", 0.0, 0.0, 3.0, 4.0, 1.5, 6.0, 0.0, "Standard", "").expect("linear 0");
        let linear90 = doc.create_dimension_core("LINEAR", 0.0, 0.0, 3.0, 4.0, 1.5, 6.0, 90.0, "Standard", "").expect("linear 90");
        let list = projected_entities(&doc.inner);
        let find = |h: &str| list.iter().find(|e| e["handle"] == h).unwrap().clone();
        let a = find(&aligned);
        let l0 = find(&linear0);
        let l90 = find(&linear90);
        assert_eq!(a["type"], "DIMENSION");
        assert_eq!(a["dimtype"], "ALIGNED");
        assert_eq!(a["editable"], false);
        assert!(near(a["measurement"].as_f64().unwrap(), 5.0));
        assert_eq!(l0["dimtype"], "LINEAR");
        assert!(near(l0["measurement"].as_f64().unwrap(), 3.0), "0deg projects the x component, never the 5.0 chord");
        assert_eq!(l90["dimtype"], "LINEAR");
        assert!(near(l90["measurement"].as_f64().unwrap(), 4.0), "90deg projects the y component, never the 5.0 chord");
        assert_eq!(a["def1"], serde_json::json!([0.0, 0.0]));
        assert_eq!(a["def2"], serde_json::json!([3.0, 4.0]));
        assert_eq!(a["dimline"], serde_json::json!([1.5, 6.0]));
        assert_eq!(a["style"], "Standard");
        assert_eq!(l0["rotationDeg"], 0.0);
        assert_eq!(l90["rotationDeg"], 90.0);
        assert_eq!(a["rotationDeg"], 0.0, "ALIGNED carries no rotation of its own");
    }

    // Moving the dimline (the definition point the wrapper never exposes for
    // editing) changes nothing in measurement(): it depends only on the two
    // definition points and (for LINEAR) the rotation.
    #[test]
    fn moving_the_dimline_point_changes_nothing_in_the_measurement() {
        let mut doc = empty_doc();
        let handle = doc.create_dimension_core("LINEAR", 0.0, 0.0, 3.0, 4.0, 1.5, 6.0, 0.0, "Standard", "").expect("linear");
        let before = projected_entities(&doc.inner)[0]["measurement"].as_f64().unwrap();
        // A raw crate-level mutation (never the wrapper's own edit surface,
        // which refuses a DIMENSION selection outright).
        for entity in doc.inner.entities_mut() {
            if let EntityType::Dimension(dim) = entity {
                dim.base_mut().definition_point = Vector3::new(99.0, -50.0, 0.0);
            }
        }
        let after = &projected_entities(&doc.inner)[0];
        assert_eq!(after["handle"], handle);
        assert_eq!(after["dimline"], serde_json::json!([99.0, -50.0]));
        assert!(near(after["measurement"].as_f64().unwrap(), before));
    }

    #[test]
    fn write_and_reparse_keeps_both_points_the_dimline_the_rotation_the_style_and_the_measurement() {
        let mut doc = empty_doc();
        doc.create_dimension_core("LINEAR", 0.0, 0.0, 3.0, 4.0, 1.5, 6.0, 90.0, "Standard", "Dims").expect("linear");
        let before = &projected_entities(&doc.inner)[0];
        let back = reparse(&doc);
        let after = &projected_entities(&back.inner)[0];
        assert_eq!(after["handle"], before["handle"]);
        assert_eq!(after["def1"], before["def1"]);
        assert_eq!(after["def2"], before["def2"]);
        assert_eq!(after["dimline"], before["dimline"]);
        assert_eq!(after["rotationDeg"], before["rotationDeg"]);
        assert_eq!(after["style"], before["style"]);
        assert!(near(after["measurement"].as_f64().unwrap(), before["measurement"].as_f64().unwrap()));
        assert!(near(after["measurement"].as_f64().unwrap(), 4.0));
    }

    #[test]
    fn create_dimension_core_refuses_before_touching_the_document_in_the_contract_order() {
        let mut doc = empty_doc();
        assert_eq!(code(doc.create_dimension_core("LINEAR", f64::NAN, 0.0, 3.0, 4.0, 1.5, 6.0, 0.0, "Standard", "")), "coordinate_not_finite");
        assert_eq!(code(doc.create_dimension_core("LINEAR", 1.0, 1.0, 1.0, 1.0, 1.5, 6.0, 0.0, "Standard", "")), "dimension_points_coincide");
        assert_eq!(code(doc.create_dimension_core("ALIGNED", 0.0, 0.0, 3.0, 4.0, 1.5, 6.0, 30.0, "Standard", "")), "dimension_rotation_not_allowed");
        assert_eq!(code(doc.create_dimension_core("LINEAR", 0.0, 0.0, 3.0, 4.0, 1.5, 6.0, 0.0, "Fancy", "")), "dimstyle_not_loaded:Fancy");
        assert_eq!(code(doc.create_dimension_core("RADIUS", 0.0, 0.0, 3.0, 4.0, 1.5, 6.0, 0.0, "Standard", "")), "dimension_type_not_supported");
        // The style lookup is case-insensitive and stores the table's own spelling.
        let handle = doc.create_dimension_core("LINEAR", 0.0, 0.0, 3.0, 4.0, 1.5, 6.0, 0.0, "standard", "").expect("case-insensitive style");
        assert_eq!(projected_entities(&doc.inner)[0]["style"], "Standard");
        // The style check runs BEFORE the dimtype check: an unknown dimtype
        // with an unloaded style still reports the style refusal.
        assert_eq!(code(doc.create_dimension_core("RADIUS", 0.0, 0.0, 3.0, 4.0, 1.5, 6.0, 0.0, "Fancy", "")), "dimstyle_not_loaded:Fancy");
        // Every refusal above left the one prior create's handle the only one present.
        assert_eq!(projected_entities(&doc.inner).len(), 1);
        assert_eq!(projected_entities(&doc.inner)[0]["handle"], handle);
    }

    // W4g-7b-04c-3 F3a: a LINEAR whose rotation is perpendicular to
    // def1-def2 projects to a zero-length dimension line; refused before any
    // write, the same way a coincident pair already is.
    #[test]
    fn create_dimension_core_refuses_a_linear_projection_of_zero_before_any_write() {
        let mut doc = empty_doc();
        assert_eq!(code(doc.create_dimension_core("LINEAR", 0.0, 0.0, 3.0, 0.0, 1.5, 6.0, 90.0, "Standard", "")), "dimension_projection_zero");
        assert!(projected_entities(&doc.inner).is_empty(), "the refused create wrote nothing");
        // The same two def points at a rotation that DOES project: accepted.
        let handle = doc.create_dimension_core("LINEAR", 0.0, 0.0, 3.0, 4.0, 1.5, 6.0, 90.0, "Standard", "").expect("this one projects (measurement 4)");
        assert!(near(projected_entities(&doc.inner)[0]["measurement"].as_f64().unwrap(), 4.0));
        assert_eq!(projected_entities(&doc.inner)[0]["handle"], handle);
    }

    // W4g-7b-04c-3 F3b: the crate normalizes a raw rotation into [0, 360)
    // before the F3a projection test and before the LINEAR constructor, so
    // the projection's own rotationDeg reads back normalized too.
    #[test]
    fn create_dimension_core_normalizes_the_rotation_into_0_360_before_the_projection_test_and_the_constructor() {
        let mut doc = empty_doc();
        let h90 = doc.create_dimension_core("LINEAR", 0.0, 0.0, 3.0, 4.0, 1.5, 6.0, 90.0, "Standard", "").expect("90");
        let h450 = doc.create_dimension_core("LINEAR", 0.0, 0.0, 3.0, 4.0, 1.5, 6.0, 450.0, "Standard", "").expect("450 normalizes to 90");
        let hneg90 = doc.create_dimension_core("LINEAR", 0.0, 0.0, 3.0, 4.0, 1.5, 6.0, -90.0, "Standard", "").expect("-90 normalizes to 270");
        let list = projected_entities(&doc.inner);
        let find = |h: &str| list.iter().find(|e| e["handle"] == h).unwrap().clone();
        let e90 = find(&h90);
        let e450 = find(&h450);
        let eneg90 = find(&hneg90);
        assert_eq!(e450["rotationDeg"], 90.0, "450 normalizes into [0, 360) before the constructor");
        assert!(near(e450["measurement"].as_f64().unwrap(), e90["measurement"].as_f64().unwrap()), "450 measures the same as 90");
        assert_eq!(eneg90["rotationDeg"], 270.0, "-90 normalizes into [0, 360) before the constructor");
    }

    // The 05c sentence, adopted for DIMENSION: every geometry verb refuses a
    // DIMENSION selection, but its DELETE is still allowed.
    #[test]
    fn every_geometry_verb_refuses_a_dimension_selection_but_delete_is_allowed() {
        let mut doc = empty_doc();
        doc.create_dimension_core("LINEAR", 0.0, 0.0, 3.0, 4.0, 1.5, 6.0, 0.0, "Standard", "").expect("linear");
        assert_eq!(code(doc.translate_entity_core(0, 1.0, 2.0)), DIMENSION_NOT_EDITABLE);
        assert_eq!(code(doc.move_vertex_core(0, 0, 1.0, 2.0)), DIMENSION_NOT_EDITABLE);
        assert_eq!(code(doc.copy_entity_core(0, 1.0, 2.0)), DIMENSION_NOT_EDITABLE);
        assert_eq!(code(doc.mirror_entity_core(0, 0.0, 0.0, 1.0, 0.0, false)), DIMENSION_NOT_EDITABLE);
        assert_eq!(code(doc.rotate_entity_core(0, 0.0, 0.0, 90.0)), DIMENSION_NOT_EDITABLE);
        assert_eq!(code(doc.scale_entity_core(0, 0.0, 0.0, 2.0)), DIMENSION_NOT_EDITABLE);
        assert_eq!(projected_entities(&doc.inner).len(), 1, "every refusal above left the handle untouched");
        assert!(doc.delete_entity_core(0).is_ok(), "delete is allowed on a DIMENSION unlike every other verb");
        assert!(projected_entities(&doc.inner).is_empty());
    }

    // A dimension kind the wrapper does not create (RADIUS here, added
    // directly through the crate's own add_entity as a loaded document
    // would carry one) still projects: as OTHER, visible by handle, its
    // edit refused, its removal allowed — the 05c rule extended to a kind
    // this record never creates.
    #[test]
    fn a_radius_dimension_projects_as_other_and_refuses_its_edit_but_not_its_removal() {
        let mut doc = empty_doc();
        let radius = DimensionRadius::new(Vector3::new(0.0, 0.0, 0.0), Vector3::new(5.0, 0.0, 0.0));
        doc.inner.add_entity(EntityType::Dimension(Dimension::Radius(radius))).expect("radius dimension added");
        let record = &projected_entities(&doc.inner)[0];
        assert_eq!(record["type"], "DIMENSION");
        assert_eq!(record["dimtype"], "OTHER");
        assert_eq!(record["editable"], false);
        assert_eq!(record["def1"], serde_json::Value::Null);
        assert_eq!(record["measurement"], serde_json::Value::Null);
        assert!(record["handle"].as_str().is_some(), "the handle is never dropped for an unsupported dimtype");
        assert_eq!(code(doc.translate_entity_core(0, 1.0, 0.0)), DIMENSION_NOT_EDITABLE);
        assert!(doc.delete_entity_core(0).is_ok());
        assert!(projected_entities(&doc.inner).is_empty());
    }

    #[test]
    fn mleader_create_roundtrip_refusals_and_erase() {
        let mut doc = empty_doc();
        for object in doc.inner.objects.values_mut() {
            if let ObjectType::MultiLeaderStyle(style) = object { style.text_height = 1.0; }
        }
        assert_eq!(code(doc.create_mleader_core(f64::NAN, 0.0, 3.0, 4.0, "Valve", "Standard", "")), "coordinate_not_finite");
        assert_eq!(code(doc.create_mleader_core(0.0, 0.0, 0.0, 0.0, "Valve", "Standard", "")), "the two points coincide at the drawing precision (0.001)");
        assert_eq!(code(doc.create_mleader_core(0.0, 0.0, 3.0, 4.0, "", "Standard", "")), "text_empty");
        assert_eq!(code(doc.create_mleader_core(0.0, 0.0, 3.0, 4.0, &"x".repeat(257), "Standard", "")), "text_too_long");
        assert_eq!(code(doc.create_mleader_core(0.0, 0.0, 3.0, 4.0, "Valve\n", "Standard", "")), "text_control_character");
        assert_eq!(code(doc.create_mleader_core(0.0, 0.0, 3.0, 4.0, "Valve", "Absent", "")), "mleader_style_unknown");
        assert!(projected_entities(&doc.inner).is_empty());
        let handle = doc.create_mleader_core(0.0, 0.0, 3.0, 4.0, "Valve", "Standard", "Leaders").unwrap();
        let before = projected_entities(&doc.inner)[0].clone();
        assert_eq!(before["handle"], handle);
        assert_eq!(before["type"], "MLEADER");
        assert_eq!(before["editable"], false);
        assert_eq!(before["vertices"], serde_json::json!([[0.0, 0.0, 0.0], [3.0, 4.0, 0.0]]));
        assert_eq!(before["text"], "Valve");
        assert_eq!(before["style"], "Standard");
        assert!(near(before["textLocation"][0].as_f64().unwrap(), 3.45));
        assert!(near(before["textLocation"][1].as_f64().unwrap(), 4.5));
        assert_eq!(before["height"], serde_json::json!(1.0));
        assert_eq!(before["arrow"], serde_json::json!(0.18));
        assert_eq!(before["dogleg"], serde_json::json!(0.36));
        let mut back = reparse(&doc);
        let after = projected_entities(&back.inner)[0].clone();
        for field in ["handle", "vertices", "text", "style", "layer", "height"] { assert_eq!(after[field], before[field]); }
        let entity = back.inner.entities().next().unwrap();
        let EntityType::MultiLeader(m) = entity else { panic!("expected a multileader"); };
        assert_eq!(m.context.leader_roots.len(), 1);
        let root = &m.context.leader_roots[0];
        assert_eq!(root.lines.len(), 1);
        assert_eq!(root.lines[0].points, vec![Vector3::new(0.0, 0.0, 0.0)]);
        assert_eq!(root.connection_point, Vector3::new(3.0, 4.0, 0.0));
        assert_eq!(vertices_of(entity), vec![[0.0, 0.0, 0.0], [3.0, 4.0, 0.0]]);
        assert_eq!(code(back.translate_entity_core(0, 1.0, 2.0)), MLEADER_NOT_EDITABLE);
        assert_eq!(code(back.copy_entity_core(0, 1.0, 2.0)), MLEADER_NOT_EDITABLE);
        assert_eq!(code(back.rotate_entity_core(0, 0.0, 0.0, 90.0)), MLEADER_NOT_EDITABLE);
        assert_eq!(code(back.scale_entity_core(0, 0.0, 0.0, 2.0)), MLEADER_NOT_EDITABLE);
        assert_eq!(code(back.explode_entity_core(0)), MLEADER_NOT_EDITABLE);
        assert_eq!(code(back.set_entity_layer_core(0, "Other")), MLEADER_NOT_EDITABLE);
        assert_eq!(code(back.set_entity_color_core(0, 1)), MLEADER_NOT_EDITABLE);
        assert!(back.delete_entity_core(0).is_ok());
        assert!(projected_entities(&back.inner).is_empty());
        let styles = mlstyles_catalogue(&doc.inner, &doc.mlstyle_segments);
        assert!(styles.iter().any(|s| s["name"] == "Standard" && s["segments"].is_null()));
    }

    #[test]
    fn mlstyle_segments_scans_only_objects_and_keeps_unknown_values_unknown() {
        let bytes = b"0\nSECTION\n2\nENTITIES\n0\nMLEADERSTYLE\n5\nFF\n173\n9\n0\nENDSEC\n0\nSECTION\n2\nOBJECTS\n0\nMLEADERSTYLE\n5\nA\n173\n1\n0\nMLEADERSTYLE\n173\n2\n5\nB\n0\nMLEADERSTYLE\n5\nC\n0\nENDSEC\n0\nEOF\n";
        let segments = scan_mlstyle_segments(bytes);
        assert_eq!(segments.len(), 3);
        assert_eq!(segments.get(&Handle::new(0xC)), Some(&0));
        assert_eq!(segments.get(&Handle::new(0xA)), Some(&1));
        assert_eq!(segments.get(&Handle::new(0xB)), Some(&2));
        assert!(scan_mlstyle_segments(b"AutoCAD Binary DXF\r\n").is_empty());
        assert!(scan_mlstyle_segments(b"").is_empty());
    }

    #[test]
    fn mleader_caret_precision_and_foreign_trailing_point() {
        let mut doc = empty_doc();
        for text in ["A^ B", "A^B"] {
            assert_eq!(code(doc.create_mleader_core(30.0, 23.0, 31.0, 23.0, text, "Standard", "")), "text_caret");
        }
        assert_eq!(code(doc.create_mleader_core(30.0, 23.0, 30.0004, 23.0, "Valve", "Standard", "")), "the two points coincide at the drawing precision (0.001)");
        assert!(projected_entities(&doc.inner).is_empty());
        doc.create_mleader_core(30.0, 23.0, 30.001, 23.0, "Valve", "Standard", "").unwrap();
        let entity = doc.inner.entities_mut().next().unwrap();
        let EntityType::MultiLeader(m) = entity else { panic!("leader"); };
        let root = &mut m.context.leader_roots[0];
        root.lines[0].points.push(root.connection_point);
        assert_eq!(vertices_of(entity), vec![[30.0, 23.0, 0.0], [30.001, 23.0, 0.0]]);
    }

    #[test]
    fn mlstyle_source_segments_survive_production_write_and_reparse() {
        let bytes = write_dxf(&empty_doc()).unwrap();
        let source = String::from_utf8(bytes).unwrap();
        let mut lines: Vec<_> = source.lines().map(str::to_string).collect();
        let objects = (0..lines.len().saturating_sub(3)).step_by(2)
            .find(|&i| lines[i].trim() == "0" && lines[i + 1] == "SECTION"
                && lines[i + 2].trim() == "2" && lines[i + 3] == "OBJECTS").unwrap();
        let start = (objects + 4..lines.len().saturating_sub(1)).step_by(2)
            .find(|&i| lines[i].trim() == "0" && lines[i + 1] == "MLEADERSTYLE").unwrap();
        let index = (start + 2..lines.len().saturating_sub(1)).step_by(2)
            .take_while(|&i| lines[i].trim() != "0")
            .find(|&i| lines[i].trim() == "173").unwrap();
        lines[index + 1] = "3".to_string();
        let mut doc = parse_dxf_core((lines.join("\n") + "\n").as_bytes()).unwrap();
        assert!(mlstyles_catalogue(&doc.inner, &doc.mlstyle_segments).iter().any(|s| s["segments"] == 3));
        doc.create_line_core(0.0, 0.0, 1.0, 1.0, "").unwrap();
        let mut back = parse_dxf_core(&write_dxf(&doc).unwrap()).unwrap();
        back.inherit_mlstyle_segments(&doc);
        assert!(mlstyles_catalogue(&back.inner, &back.mlstyle_segments).iter().any(|s| s["segments"] == 3));
        lines.drain(index..index + 2);
        let missing = parse_dxf_core((lines.join("\n") + "\n").as_bytes()).unwrap();
        assert!(mlstyles_catalogue(&missing.inner, &missing.mlstyle_segments).iter().any(|s| s["segments"] == 0));
    }

    #[test]
    fn dimstyles_catalogue_is_sorted_bounded_and_always_carries_standard() {
        let mut doc = empty_doc();
        doc.inner.dim_styles.add(acadrust::tables::DimStyle::new("Zeta")).unwrap();
        doc.inner.dim_styles.add(acadrust::tables::DimStyle::new("alpha")).unwrap();
        let names = dimstyles_catalogue(&doc.inner);
        assert_eq!(names, vec!["alpha".to_string(), "Standard".to_string(), "Zeta".to_string()]);
    }
}
