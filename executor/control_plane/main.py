"""Development entry point. Production must inject PostgreSQL and Redis clients."""
from wsgiref.simple_server import make_server

from .api import application
from .service import ControlPlane
from .store import InMemoryStore


class DevelopmentCoordination:
    def check_available(self): pass
    def acquire_claim_lock(self, *_): return "development"
    def release_claim_lock(self, *_): pass
    def publish_hint(self, *_): pass


class DevelopmentRuntime:
    def assign(self, _, __): return {"state": "ready"}
    def release(self, _, __): pass


if __name__ == "__main__":
    store = InMemoryStore()
    service = ControlPlane(store, DevelopmentRuntime(), coordination=DevelopmentCoordination())
    service.register_host("executor-local-001", "http://127.0.0.1:8088", ["slot-1"])
    service.readiness("executor-local-001", "slot-1", True, "sha256:" + "0" * 64, "sha256:" + "0" * 64)
    print("development control plane listening on http://127.0.0.1:8090")
    make_server("127.0.0.1", 8090, application(service, allow_unauthenticated=True)).serve_forever()
