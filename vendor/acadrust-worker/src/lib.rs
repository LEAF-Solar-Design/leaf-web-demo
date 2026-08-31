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

use acadrust::entities::{Entity, EntityType};
use acadrust::types::{Vector2, Vector3};
use acadrust::{CadDocument, DxfReader, DxfWriter};
use serde::Serialize;
use wasm_bindgen::prelude::*;

// ---------------------------------------------------------------------------
// Card F-3 (editing surface engine leg). Everything below is LEAF wrapper
// code: the crate stays unmodified and rev-pinned (the license review's
// tripwire), and every mutation goes through the crate's own public surface
// (entities_mut(), common_mut(), the public vertex Vecs). Contract shared by
// every exported mutation: bounds-checked, Result<_, JsValue> — never a
// panic across the wasm boundary; an out-of-range index or an unsupported
// entity kind is a typed JS error the worker folds into an editApplied
// refusal, and the document is NEVER half-mutated on a refused edit (each op
// validates before it writes).
//
// Index contract: `editableEntities` and every mutation address entities by
// their CURRENT position in document order. Any successful mutation may
// invalidate previously fetched indexes, so the UI must refresh its list
// from the edit response before issuing another edit — which is exactly what
// the existing editApplied message already carries.
// ---------------------------------------------------------------------------

/// True when this entity kind is one the editor can mutate (vertex-level
/// geometry through the crate's public fields). Everything else still
/// round-trips through the writer untouched — the whole-document model means
/// "unsupported" costs nothing and loses nothing.
fn editable(entity: &EntityType) -> bool {
    matches!(
        entity,
        EntityType::Line(_) | EntityType::LwPolyline(_) | EntityType::Polyline2D(_)
    )
}

fn kind_name(entity: &EntityType) -> &'static str {
    match entity {
        EntityType::Line(_) => "LINE",
        EntityType::LwPolyline(_) => "LWPOLYLINE",
        EntityType::Polyline2D(_) => "POLYLINE",
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

fn err(message: &str) -> JsValue {
    JsValue::from_str(message)
}

/// Opaque parsed-document handle: the Rust twin of the plain object
/// bindings.mjs's `parseDxf` returns. Crosses the boundary by reference;
/// JS only ever reads the `entities` getter or hands it back to `writeDxf`.
#[wasm_bindgen]
pub struct ParsedDxf {
    inner: CadDocument,
}

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
    /// `{index, type, layer, closed, editable, vertices}`. Non-editable kinds
    /// appear with `editable: false` and empty vertices — they are listed so
    /// the count the UI shows is the truth about the document, and they
    /// round-trip through the writer untouched (whole-document model: nothing
    /// is dropped by being uneditable).
    #[wasm_bindgen(js_name = editableEntities)]
    pub fn editable_entities(&self) -> Result<JsValue, JsValue> {
        let list: Vec<serde_json::Value> = self
            .inner
            .entities()
            .enumerate()
            .map(|(index, e)| {
                serde_json::json!({
                    "index": index,
                    "type": kind_name(e),
                    "layer": e.common().layer.clone(),
                    "closed": closed_of(e),
                    "editable": editable(e),
                    "vertices": vertices_of(e),
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
        let (handle, is_editable) = {
            let entity = self
                .inner
                .entities()
                .nth(index)
                .ok_or_else(|| err("entity_index_out_of_range"))?;
            (entity.common().handle, editable(entity))
        };
        if !is_editable {
            return Err(err("entity_kind_not_editable"));
        }
        self.inner
            .remove_entity(handle)
            .map(|_| ())
            .ok_or_else(|| err("entity_handle_not_found"))
    }

    /// Translates every vertex of the entity at `index` by (dx, dy).
    #[wasm_bindgen(js_name = translateEntity)]
    pub fn translate_entity(&mut self, index: usize, dx: f64, dy: f64) -> Result<(), JsValue> {
        if !dx.is_finite() || !dy.is_finite() {
            return Err(err("delta_not_finite"));
        }
        let entity = self
            .inner
            .entities_mut()
            .nth(index)
            .ok_or_else(|| err("entity_index_out_of_range"))?;
        match entity {
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
            _ => Err(err("entity_kind_not_editable")),
        }
    }

    /// Moves ONE vertex of the entity at `index` by (dx, dy). For a LINE,
    /// vertex 0 is the start and vertex 1 the end.
    #[wasm_bindgen(js_name = moveVertex)]
    pub fn move_vertex(
        &mut self,
        index: usize,
        vertex_index: usize,
        dx: f64,
        dy: f64,
    ) -> Result<(), JsValue> {
        if !dx.is_finite() || !dy.is_finite() {
            return Err(err("delta_not_finite"));
        }
        let entity = self
            .inner
            .entities_mut()
            .nth(index)
            .ok_or_else(|| err("entity_index_out_of_range"))?;
        match entity {
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
                _ => Err(err("vertex_index_out_of_range")),
            },
            EntityType::LwPolyline(poly) => {
                let v = poly
                    .vertices
                    .get_mut(vertex_index)
                    .ok_or_else(|| err("vertex_index_out_of_range"))?;
                v.location = Vector2::new(v.location.x + dx, v.location.y + dy);
                Ok(())
            }
            EntityType::Polyline2D(poly) => {
                let v = poly
                    .vertices
                    .get_mut(vertex_index)
                    .ok_or_else(|| err("vertex_index_out_of_range"))?;
                v.location = Vector3::new(v.location.x + dx, v.location.y + dy, v.location.z);
                Ok(())
            }
            _ => Err(err("entity_kind_not_editable")),
        }
    }

    /// Inserts a vertex AFTER `vertex_index` on a polyline at (x, y).
    /// Refused for LINE (a line has exactly two endpoints by definition).
    #[wasm_bindgen(js_name = addVertexAfter)]
    pub fn add_vertex_after(
        &mut self,
        index: usize,
        vertex_index: usize,
        x: f64,
        y: f64,
    ) -> Result<(), JsValue> {
        if !x.is_finite() || !y.is_finite() {
            return Err(err("coordinate_not_finite"));
        }
        let entity = self
            .inner
            .entities_mut()
            .nth(index)
            .ok_or_else(|| err("entity_index_out_of_range"))?;
        match entity {
            EntityType::LwPolyline(poly) => {
                if vertex_index >= poly.vertices.len() {
                    return Err(err("vertex_index_out_of_range"));
                }
                poly.vertices.insert(
                    vertex_index + 1,
                    acadrust::entities::LwVertex::from_coords(x, y),
                );
                Ok(())
            }
            EntityType::Polyline2D(poly) => {
                if vertex_index >= poly.vertices.len() {
                    return Err(err("vertex_index_out_of_range"));
                }
                poly.vertices.insert(
                    vertex_index + 1,
                    acadrust::entities::Vertex2D::new(Vector3::new(x, y, poly.elevation)),
                );
                Ok(())
            }
            EntityType::Line(_) => Err(err("line_has_fixed_endpoints")),
            _ => Err(err("entity_kind_not_editable")),
        }
    }

    /// Deletes one vertex of a polyline. Refused when it would leave fewer
    /// than two vertices (that is entity deletion, an explicit separate op),
    /// and refused for LINE for the same fixed-endpoints reason as add.
    #[wasm_bindgen(js_name = deleteVertex)]
    pub fn delete_vertex(&mut self, index: usize, vertex_index: usize) -> Result<(), JsValue> {
        let entity = self
            .inner
            .entities_mut()
            .nth(index)
            .ok_or_else(|| err("entity_index_out_of_range"))?;
        match entity {
            EntityType::LwPolyline(poly) => {
                if vertex_index >= poly.vertices.len() {
                    return Err(err("vertex_index_out_of_range"));
                }
                if poly.vertices.len() <= 2 {
                    return Err(err("polyline_needs_two_vertices"));
                }
                poly.vertices.remove(vertex_index);
                Ok(())
            }
            EntityType::Polyline2D(poly) => {
                if vertex_index >= poly.vertices.len() {
                    return Err(err("vertex_index_out_of_range"));
                }
                if poly.vertices.len() <= 2 {
                    return Err(err("polyline_needs_two_vertices"));
                }
                poly.vertices.remove(vertex_index);
                Ok(())
            }
            EntityType::Line(_) => Err(err("line_has_fixed_endpoints")),
            _ => Err(err("entity_kind_not_editable")),
        }
    }

    /// Reassigns the entity at `index` to `layer` via the crate's own
    /// EntityCommon. Bounded name; the empty string is refused (DXF layer
    /// names cannot be empty, and an empty write would be silent data rot).
    #[wasm_bindgen(js_name = setEntityLayer)]
    pub fn set_entity_layer(&mut self, index: usize, layer: &str) -> Result<(), JsValue> {
        let trimmed = layer.trim();
        if trimmed.is_empty() {
            return Err(err("layer_name_empty"));
        }
        if trimmed.len() > 255 {
            return Err(err("layer_name_too_long"));
        }
        let entity = self
            .inner
            .entities_mut()
            .nth(index)
            .ok_or_else(|| err("entity_index_out_of_range"))?;
        if !editable(&*entity) {
            return Err(err("entity_kind_not_editable"));
        }
        entity.common_mut().layer = trimmed.to_string();
        Ok(())
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
