"""The chain-facing seam used by physical producers.

Producers use only project_id, parent, source_sink, publish and
unchanged_drawing_version. LegacySource deliberately keeps the head reader's
default version selection, including its versioned-physical fallback policy.
"""
from typing import Protocol

import solar_artifacts
import solar_physical_head as ph
from solar_graph_context import resolve_graph_context


class PhysicalSource(Protocol):
    def project_id(self) -> str: ...
    def parent(self) -> tuple[dict | None, dict | None]: ...
    def source_sink(self, tool: str, request_sha256: str): ...
    def publish(self, document: dict) -> dict: ...
    def unchanged_drawing_version(self) -> dict | None: ...


class LegacySource:
    def __init__(self, backend, tenant_id, drawing_id, project_id, *, holder=None, fence=None):
        self.backend = backend
        self.tenant_id = tenant_id
        self.drawing_id = drawing_id
        self.requested_project = project_id
        self.context = None
        self.holder = holder
        self.fence = fence

    def project_id(self):
        self.context = resolve_graph_context(self.backend, self.tenant_id,
            self.drawing_id, "head", project_id=self.requested_project)
        return self.context["project_id"]

    def parent(self):
        return ph.load_physical_head(self.backend, self.tenant_id, self.drawing_id,
                                     project_id=self.context["project_id"])

    def source_sink(self, tool, request_sha256):
        return solar_artifacts.ArtifactSink(self.backend, self.tenant_id, self.drawing_id,
                                            self.context, tool, request_sha256, False)

    def publish(self, document):
        result = ph.publish_physical_state(self.backend, self.tenant_id, self.drawing_id,
                                         document, project_id=self.context["project_id"],
                                         holder=self.holder, fence=self.fence)
        return dict(result, drawing_version=result.get("drawing_version"))

    def unchanged_drawing_version(self):
        return None
