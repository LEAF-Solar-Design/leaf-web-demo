//! wasm-bindgen wrapper for acadrust (MPL-2.0), day 2 of the CAD engine spike.
//!
//! The exported surface mirrors ../bindings.mjs (the JS-native stand-in the
//! day-2 test actually runs) 1:1 — same three names via `js_name`, same call
//! structure (`parseDxf` returns a document handle, `writeDxf` takes it,
//! `bytesEqual` compares byte buffers) — so swapping the stand-in for the
//! compiled `pkg/acadrust_worker.js` really is the one-line import change in
//! worker-entry.mjs, plus the `.entities` accessor below which the stand-in
//! exposes as a plain array and this wrapper exposes as a getter returning a
//! JS array of `{kind, start, end}` objects of the same shape.
//!
//! Bytes in from JS, bytes out to JS — exactly the shape `wasm_bindgen`'s
//! `&[u8]` / `Vec<u8>` marshalling wants; no `std::fs` anywhere on this
//! call path (in-memory `DxfReader::from_reader` / `DxfWriter::write_to_vec`
//! per day 1's inventory).
//!
//! OPEN QUESTION (carried, see docs/ACADRUST-SPIKE-DAY2.md OQ-3): the exact
//! acadrust method names/chaining below (`doc.entities()`, `Entity::as_line()`)
//! are written from day 1's structural notes (`document.rs`, `entities/`
//! modules), not confirmed against acadrust's real public API — no
//! toolchain in this workdir can resolve the crate to check. Verify and
//! adjust against the real crate before this crate is ever built for real;
//! it is not exercised by day 2's Node-run test (that runs bindings.mjs).

use wasm_bindgen::prelude::*;

/// Opaque parsed-document handle: the Rust twin of the plain object
/// bindings.mjs's `parseDxf` returns. Crosses the boundary by reference;
/// JS only ever reads the `entities` getter or hands it back to `writeDxf`.
#[wasm_bindgen]
pub struct ParsedDxf {
    inner: acadrust::Document,
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
            .filter_map(|e| {
                // Element shape mirrors bindings.mjs exactly: {type, layer,
                // start, end}. The layer accessor is OQ-3-carried like every
                // other acadrust name here (unverified without a toolchain).
                e.as_line().map(|line| {
                    serde_json::json!({
                        "type": "LINE",
                        "layer": e.layer().to_string(),
                        "start": [line.start.x, line.start.y, line.start.z],
                        "end": [line.end.x, line.end.y, line.end.z],
                    })
                })
            })
            .collect();
        serde_wasm_bindgen::to_value(&list).map_err(|e| JsValue::from_str(&e.to_string()))
    }
}

/// Parses DXF bytes into a document handle. Returns a JS-thrown error
/// (never panics across the wasm/JS boundary) on a malformed document —
/// mirrors the boundary's own "validate or drop, never throw into the UI"
/// contract at the message layer one level up.
#[wasm_bindgen(js_name = parseDxf)]
pub fn parse_dxf(bytes: &[u8]) -> Result<ParsedDxf, JsValue> {
    let inner = acadrust::io::dxf::DxfReader::from_reader(std::io::Cursor::new(bytes))
        .map_err(|e| JsValue::from_str(&e.to_string()))?;
    Ok(ParsedDxf { inner })
}

/// Re-serializes a parsed document via `DxfWriter::write_to_vec()`. The
/// caller (the worker's message handler) parses the output again and
/// compares against the input for the byte/entity comparison the day-2
/// oracle asks for.
#[wasm_bindgen(js_name = writeDxf)]
pub fn write_dxf(doc: &ParsedDxf) -> Result<Vec<u8>, JsValue> {
    acadrust::io::dxf::DxfWriter::write_to_vec(&doc.inner)
        .map_err(|e| JsValue::from_str(&e.to_string()))
}

/// Byte-for-byte comparison, the stand-in's `bytesEqual` twin.
#[wasm_bindgen(js_name = bytesEqual)]
pub fn bytes_equal(a: &[u8], b: &[u8]) -> bool {
    a == b
}
