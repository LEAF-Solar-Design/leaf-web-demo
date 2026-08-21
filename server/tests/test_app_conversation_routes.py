from app import app


def test_project_conversation_crud_routes_are_mounted() -> None:
    routes = {(route.path, method) for route in app.routes for method in route.methods}

    assert ("/api/projects/{project_id}/conversations", "POST") in routes
    assert ("/api/projects/{project_id}/conversations", "GET") in routes
