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

use acadrust::entities::{Arc as ArcEntity, Circle, Entity, EntityType, Line, LwPolyline, Text};
use acadrust::types::{Handle, Transform, Vector2, Vector3};
use acadrust::{CadDocument, DxfReader, DxfWriter};
use serde::Serialize;
use wasm_bindgen::prelude::*;

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
        _ => Vec::new(),
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
        _ => None,
    }
}

fn height_of(entity: &EntityType) -> Option<f64> {
    match entity {
        EntityType::Text(t) => Some(t.height),
        _ => None,
    }
}

fn rotation_deg_of(entity: &EntityType) -> Option<f64> {
    match entity {
        EntityType::Text(t) => Some(t.rotation.to_degrees()),
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
}

// ---- the cores: every operation, natively testable ------------------------
impl ParsedDxf {
    fn entity_mut(&mut self, index: usize) -> Result<&mut EntityType, Refusal> {
        self.inner
            .entities_mut()
            .nth(index)
            .ok_or_else(|| "entity_index_out_of_range".to_string())
    }

    fn delete_entity_core(&mut self, index: usize) -> Result<(), Refusal> {
        let (handle, is_editable) = {
            let entity = self
                .inner
                .entities()
                .nth(index)
                .ok_or_else(|| "entity_index_out_of_range".to_string())?;
            (entity.common().handle, editable(entity))
        };
        if !is_editable {
            return refuse("entity_kind_not_editable");
        }
        self.inner
            .remove_entity(handle)
            .map(|_| ())
            .ok_or_else(|| "entity_handle_not_found".to_string())
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
        let entity = self
            .inner
            .entities()
            .nth(index)
            .ok_or_else(|| "entity_index_out_of_range".to_string())?;
        if !editable(entity) {
            return refuse("entity_kind_not_editable");
        }
        let layer = entity.common().layer.clone();
        let mut copy = entity.clone();
        copy.as_entity_mut().set_handle(Handle::NULL);
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
            let entity = self
                .inner
                .entities()
                .nth(index)
                .ok_or_else(|| "entity_index_out_of_range".to_string())?;
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
            handles.push(self.add_created(part, &layer)?);
        }
        self.inner
            .remove_entity(handle)
            .ok_or_else(|| "entity_handle_not_found".to_string())?;
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

    fn create_polyline_core(&mut self, points: &[f64], closed: bool, layer: &str) -> Result<String, Refusal> {
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
        let vertices: Vec<Vector2> = points
            .chunks_exact(2)
            .map(|p| Vector2::new(p[0], p[1]))
            .collect();
        let mut poly = LwPolyline::from_points(vertices);
        poly.is_closed = closed;
        self.add_created(EntityType::LwPolyline(poly), layer)
    }
}

// ---- the exported boundary: thin, JsValue only here -------------------------
#[wasm_bindgen]
impl ParsedDxf {
    /// Mirrors the stand-in's `parsed.entities` array: one
    /// `{type, layer, start, end}` object per LINE entity, in document order.
    #[wasm_bindgen(getter)]
    pub fn entities(&self) -> Result<JsValue, JsValue> {
        let list: Vec<serde_json::Value> = self
            .inner
            .entities()
            .filter_map(|e| match e {
                EntityType::Line(line) => Some(serde_json::json!({
                    "type": "LINE",
                    "layer": line.layer().to_string(),
                    "start": [line.start.x, line.start.y, line.start.z],
                    "end": [line.end.x, line.end.y, line.end.z],
                })),
                _ => None,
            })
            .collect();
        // json_compatible(): plain JS objects, not Map instances (see module
        // doc, correction 6) — the shape bindings.mjs's stand-in and the
        // day-2 test's .toEqual({...}) assertions both require.
        list.serialize(&serde_wasm_bindgen::Serializer::json_compatible())
            .map_err(|e| JsValue::from_str(&e.to_string()))
    }

    /// F-3 editor projection: EVERY entity in document order, each as
    /// `{index, handle, type, layer, closed, editable, vertices}`. Non-editable
    /// kinds appear with `editable: false` and empty vertices — they are
    /// listed so the count the UI shows is the truth about the document, and
    /// they round-trip through the writer untouched (whole-document model:
    /// nothing is dropped by being uneditable).
    #[wasm_bindgen(js_name = editableEntities)]
    pub fn editable_entities(&self) -> Result<JsValue, JsValue> {
        let list: Vec<serde_json::Value> = self
            .inner
            .entities()
            .enumerate()
            .map(|(index, e)| {
                serde_json::json!({
                    "index": index,
                    // The handle is the identity that survives a write/re-parse
                    // (the index does not: a delete renumbers). A create returns
                    // it; the worker finds the new entity by it afterwards.
                    "handle": handle_id(e.common().handle.value()),
                    "type": kind_name(e),
                    "layer": e.common().layer.clone(),
                    "closed": closed_of(e),
                    "editable": editable(e),
                    "vertices": vertices_of(e),
                    // W4f: circles and arcs are drawable from these; null
                    // for every other kind, so a consumer that ignores them
                    // sees the exact shape it saw before.
                    "radius": radius_of(e),
                    // W4g-5d: a TEXT carries its own value, height and
                    // rotation (degrees); null for every other kind.
                    "text": text_of(e),
                    "height": height_of(e),
                    "rotationDeg": rotation_deg_of(e),
                    "startDeg": sweep_deg_of(e).map(|(start, _)| start),
                    "endDeg": sweep_deg_of(e).map(|(_, end)| end),
                })
            })
            .collect();
        list.serialize(&serde_wasm_bindgen::Serializer::json_compatible())
            .map_err(|e| JsValue::from_str(&e.to_string()))
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

    /// W4g-4 COPY: a displaced clone of the entity at `index`; returns the
    /// new entity's handle. Refuses a non-finite delta and read-only kinds.
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
    pub fn create_polyline(&mut self, points: &[f64], closed: bool, layer: &str) -> Result<String, JsValue> {
        self.create_polyline_core(points, closed, layer).map_err(js_err)
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
    let inner = DxfReader::from_reader(std::io::Cursor::new(bytes.to_vec()))
        .map_err(|e| JsValue::from_str(&e.to_string()))?
        .read()
        .map_err(|e| JsValue::from_str(&e.to_string()))?;
    Ok(ParsedDxf { inner })
}

/// Re-serializes a parsed document via `DxfWriter::new(&doc).write_to_vec()`.
/// The caller (the worker's message handler) parses the output again and
/// compares against the input for the byte/entity comparison the day-2
/// oracle asks for.
#[wasm_bindgen(js_name = writeDxf)]
pub fn write_dxf(doc: &ParsedDxf) -> Result<Vec<u8>, JsValue> {
    DxfWriter::new(&doc.inner)
        .write_to_vec()
        .map_err(|e| JsValue::from_str(&e.to_string()))
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

    const EPS: f64 = 1e-9;

    fn near(a: f64, b: f64) -> bool {
        (a - b).abs() < EPS
    }

    fn empty_doc() -> ParsedDxf {
        ParsedDxf { inner: CadDocument::new() }
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
        ParsedDxf { inner: reparse(&doc.inner) }
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
            .create_polyline_core(&[0.0, 0.0, 4.0, 0.0, 4.0, 3.0], true, "Outline")
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
        assert_eq!(code(doc.create_polyline_core(&[0.0, 0.0, 1.0], false, "")), "points_not_pairs");
        assert_eq!(code(doc.create_polyline_core(&[0.0, 0.0], false, "")), "polyline_needs_two_vertices");
        assert_eq!(code(doc.create_polyline_core(&[0.0, 0.0, 1.0, f64::NAN], false, "")), "coordinate_not_finite");
        let too_many = vec![0.0; (MAX_CREATED_VERTICES + 1) * 2];
        assert_eq!(code(doc.create_polyline_core(&too_many, false, "")), "polyline_too_many_vertices");
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
        doc.create_polyline_core(&[0.0, 0.0, 1.0, 0.0], false, "").expect("two-vertex polyline");
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
        doc.create_polyline_core(&[0.0, 0.0, 4.0, 0.0, 4.0, 3.0], true, "Outline").expect("polyline");
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
            .create_polyline_core(&[0.0, 0.0, 4.0, 0.0, 4.0, 3.0], true, "P")
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
        doc.create_polyline_core(&[20.0, 0.0, 24.0, 0.0, 24.0, 3.0], true, "P").unwrap();
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
