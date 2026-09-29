"""Store immutable SolarEdge PDF sources bound to a tenant, project and drawing.

Each artifact binds the head revision, graph digest and content digest. Duplicate
content at the same head is idempotent; later heads share the immutable blob.
Input is bounded to 16 MiB and 50 pages, with eight distinct sources per drawing.
Only the page tree is inspected. No electrical entity, graph revision or Solve
state is created. Validation, storage and source readback fail closed.
"""
import write_loop
import store
import hashlib
import solar_artifacts
from leaf_cloud_client import canonical_bytes
from solar_design_graph import GraphValidationError
from solar_graph_context import resolve_graph_context

SOURCE_TOOL = "solar-solaredge-pdf-source"
SOURCE_KIND = "solaredge-pdf"
SOURCE_MEDIA_TYPE = "application/pdf"
SOURCE_FILENAME = "solaredge-source.pdf"
RESULT_SCHEMA = "leaf.solar-import-source.v1"
SLOT_SCHEMA = "leaf.solar-import-slot.v1"
MAX_IMPORT_PDF_BYTES = 16_777_216
MAX_IMPORT_PAGES = 50
MAX_SOURCES_PER_DRAWING = 8


class ImportSourceError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _artifact_refusal(code):
    return {
        "ARTIFACT_WRITES_DRAINED": "IMPORT_WRITES_DRAINED",
        "ARTIFACT_STORE_UNAVAILABLE": "IMPORT_STORE_UNAVAILABLE",
        "ARTIFACT_CONFLICT": "IMPORT_SOURCE_CONFLICT",
        "ARTIFACT_NOT_FOUND": "IMPORT_SOURCE_NOT_FOUND",
        "ARTIFACT_ID_INVALID": "IMPORT_SOURCE_ID_INVALID",
        "ARTIFACT_CORRUPT": "IMPORT_SOURCE_CORRUPT",
        "ARTIFACT_VERIFY_MISMATCH": "IMPORT_SOURCE_CORRUPT",
    }.get(code, "IMPORT_SOURCE_INVALID")


def inspect_solaredge_pdf(data) -> int:
    if type(data) is not bytes or not data:
        raise ImportSourceError("IMPORT_PDF_EMPTY")
    if len(data) > MAX_IMPORT_PDF_BYTES:
        raise ImportSourceError("IMPORT_PDF_TOO_LARGE")
    if not data[:1024].lstrip().startswith(b"%PDF-"):
        raise ImportSourceError("IMPORT_NOT_A_PDF")
    import io
    import itertools
    from pdfminer.pdfdocument import PDFDocument, PDFEncryptionError
    from pdfminer.pdfparser import PDFParser
    from pdfminer.pdfpage import PDFPage

    try:
        doc = PDFDocument(PDFParser(io.BytesIO(data)))
        if doc.encryption is not None:
            raise ImportSourceError("IMPORT_PDF_ENCRYPTED")
        pages = sum(1 for _ in itertools.islice(PDFPage.create_pages(doc), MAX_IMPORT_PAGES + 1))
    except ImportSourceError:
        raise
    except PDFEncryptionError:
        raise ImportSourceError("IMPORT_PDF_ENCRYPTED") from None
    except (RecursionError, MemoryError, Exception):
        raise ImportSourceError("IMPORT_PDF_MALFORMED") from None
    if pages == 0:
        raise ImportSourceError("IMPORT_PDF_NO_PAGES")
    if pages > MAX_IMPORT_PAGES:
        raise ImportSourceError("IMPORT_PDF_TOO_MANY_PAGES")
    return pages


def _slot_key(tenant_id, drawing_id, n):
    return store.drawing_prefix(tenant_id, drawing_id) + f"/imports/solaredge/slot-{n}.json"


def _claim_slot(backend, tenant_id, drawing_id, content_sha256) -> int:
    mine = canonical_bytes({"schema": SLOT_SCHEMA, "kind": SOURCE_KIND,
                            "content_sha256": content_sha256})
    for n in range(MAX_SOURCES_PER_DRAWING):
        key = _slot_key(tenant_id, drawing_id, n)
        try:
            try:
                existing = backend.get(key)
            except KeyError:
                try:
                    backend.put_if_absent_or_verify(key, mine)
                    return n
                except store.ImmutableConflict:
                    existing = backend.get(key)
            if existing == mine:
                return n
        except (OSError, RuntimeError, ValueError):
            raise ImportSourceError("IMPORT_STORE_UNAVAILABLE") from None
    raise ImportSourceError("IMPORT_QUOTA_EXCEEDED")


def import_solaredge_source(backend, tenant_id, drawing_id, data, *, project_id=None) -> dict:
    if write_loop.drawing_mutations_refusal() is not None:
        raise ImportSourceError("IMPORT_WRITES_DRAINED")
    if project_id is not None and not (isinstance(project_id, str) and 1 <= len(project_id) <= 100):
        raise ImportSourceError("IMPORT_PROJECT_ID_INVALID")
    page_count = inspect_solaredge_pdf(data)
    try:
        context = resolve_graph_context(backend, tenant_id, drawing_id, "head", project_id=project_id)
    except GraphValidationError as exc:
        code = {"PROJECT_MISMATCH": "IMPORT_PROJECT_MISMATCH",
                "GRAPH_CONTEXT_UNAVAILABLE": "IMPORT_DRAWING_NOT_FOUND"}.get(exc.code, "IMPORT_GRAPH_REQUIRED")
        raise ImportSourceError(code) from None
    except (OSError, RuntimeError):
        raise ImportSourceError("IMPORT_STORE_UNAVAILABLE") from None
    content_sha256 = hashlib.sha256(data).hexdigest()
    sink = solar_artifacts.ArtifactSink(backend, tenant_id, drawing_id, context,
                                        SOURCE_TOOL, content_sha256, False)
    try:
        prepared = sink.prepare(solar_artifacts.ArtifactOutput(
            {"page_count": page_count}, SOURCE_MEDIA_TYPE, SOURCE_FILENAME, data))
    except GraphValidationError as exc:
        raise ImportSourceError(_artifact_refusal(exc.code)) from None
    # The immutable store has no delete: a reservation survives storage failure.
    # Retrying the same PDF reuses its slot. Distinct PDFs that fail after
    # reservation can exhaust the quota until storage recovers and those same
    # files are retried. A refused upload stores no artifact; its reservation
    # is not covered by that guarantee.
    _claim_slot(backend, tenant_id, drawing_id, content_sha256)
    try:
        ref = sink.finish(prepared)
    except GraphValidationError as exc:
        raise ImportSourceError(_artifact_refusal(exc.code)) from None
    return {"schema": RESULT_SCHEMA, "kind": SOURCE_KIND, "drawing_id": drawing_id,
            "project_id": context["project_id"], "source_version": context["resolved_version"],
            "graph_sha256": context["graph_sha256"], "page_count": page_count, "source": ref}


def load_import_source(backend, tenant_id, drawing_id, artifact_id, *, project_id):
    try:
        meta, content = solar_artifacts.read_artifact(backend, tenant_id, drawing_id, artifact_id)
    except GraphValidationError as exc:
        raise ImportSourceError(_artifact_refusal(exc.code)) from None
    if (meta["tool"] != SOURCE_TOOL or meta["media_type"] != SOURCE_MEDIA_TYPE
            or meta["filename"] != SOURCE_FILENAME):
        raise ImportSourceError("IMPORT_SOURCE_KIND_MISMATCH")
    if meta["project_id"] != project_id:
        raise ImportSourceError("IMPORT_PROJECT_MISMATCH")
    if meta["request_sha256"] != meta["content_sha256"]:
        raise ImportSourceError("IMPORT_SOURCE_CORRUPT")
    return meta, content
