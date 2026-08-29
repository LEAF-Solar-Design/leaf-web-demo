from app import app
from route_flatten import iter_leaf_routes


def test_project_conversation_crud_routes_are_mounted() -> None:
    routes = {(path, method)
              for path, route in iter_leaf_routes(app.routes)
              for method in (getattr(route, "methods", None) or ())}

    assert ("/api/projects/{project_id}/conversations", "POST") in routes
    assert ("/api/projects/{project_id}/conversations", "GET") in routes
