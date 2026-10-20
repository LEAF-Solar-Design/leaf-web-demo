"""Map only a verified demo context to its current, provisioned platform actor."""
from dataclasses import dataclass
from uuid import UUID

import platform_link


class DemoActorUnavailable(Exception):
    """Infrastructure failed while resolving a verified demo actor."""


class DemoActorRefused(Exception):
    status_code = 403
    code = "demo_actor_refused"

    def __init__(self):
        super().__init__(self.code)


@dataclass(frozen=True)
class DemoActor:
    binding: object
    org_id: UUID
    project_id: UUID
    member_role: str
    server_tenant: str

    @property
    def binding_id(self):
        return self.binding.binding_id


def is_demo_context(context):
    from deps import TenantContext
    return isinstance(context, TenantContext) and context.verified_demo is True


def resolve_demo_actor(context) -> DemoActor | None:
    if not is_demo_context(context):
        return None
    import demo_sessions
    if not demo_sessions.is_enabled():
        raise DemoActorRefused()

    def lookup(conn):
        # A refreshed durable record, not a cached actor, gates every observation.
        session = conn.execute(
            "SELECT subject, tenant_id, org_id FROM demo_sessions "
            "WHERE subject = %s AND revoked_at IS NULL AND expires_at > now()",
            (context.subject,)).fetchone()
        if (session is None or session["tenant_id"] != str(context)
                or str(session["org_id"]) != str(context.org_id)):
            raise DemoActorRefused()
        rows = conn.execute(
            "SELECT * FROM identity_bindings WHERE external_authority = 'leaf-demo' "
            "AND external_subject = %s AND status = 'active' AND revoked_at IS NULL",
            (context.subject,)).fetchall()
        if len(rows) != 1:
            raise DemoActorRefused()
        binding = IdentityBinding.from_row(rows[0])
        if str(binding.platform_tenant_id) != str(context.org_id):
            raise DemoActorRefused()
        org_id = binding.platform_tenant_id
        org = conn.execute(
            "SELECT org_id FROM orgs WHERE org_id = %s AND status = 'active' "
            "AND account_class = 'demo'", (org_id,)).fetchone()
        projects = conn.execute(
            "SELECT project_id FROM live_projects WHERE org_id = %s", (org_id,)).fetchall()
        if org is None or len(projects) != 1:
            raise DemoActorRefused()
        project_id = projects[0]["project_id"]
        members = conn.execute(
            "SELECT role FROM project_member_bindings WHERE org_id = %s "
            "AND project_id = %s AND binding_id = %s "
            "AND status = 'active' AND revoked_at IS NULL",
            (org_id, project_id, binding.binding_id)).fetchall()
        from leaf_platform.project_lifecycle import PROJECT_ROLES
        if len(members) != 1 or members[0]["role"] not in PROJECT_ROLES:
            raise DemoActorRefused()
        return DemoActor(binding, org_id, project_id, members[0]["role"], str(context))

    try:
        platform_link._ensure_platform_package()
        from leaf_platform.models import IdentityBinding

        return platform_link.platform_db().run_transaction(lookup, read_only=True)
    except DemoActorRefused:
        raise
    except Exception as exc:
        raise DemoActorUnavailable() from exc


def require_demo_project(actor, project_id, *, write=False):
    from leaf_platform.project_lifecycle import WRITE_ROLES
    if (str(project_id) != str(actor.project_id)
            or (write and actor.member_role not in WRITE_ROLES)):
        raise DemoActorRefused()
