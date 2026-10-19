"""Provision durable demo rows on the mint caller's transaction connection."""
from __future__ import annotations

from typing import NamedTuple
import uuid


class DemoTenant(NamedTuple):
    session_id: uuid.UUID
    org_id: uuid.UUID
    tenant_id: str
    binding_id: uuid.UUID
    platform_user_id: uuid.UUID
    project_id: uuid.UUID
    membership_id: uuid.UUID


def provision_demo_tenant(conn, session) -> DemoTenant:
    """No commit, connection acquisition, drawing, artifact or blob writes."""
    tenant = DemoTenant(session.id, session.org_id, session.tenant_id,
                        uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    conn.execute(
        "INSERT INTO orgs (org_id, name, tier, account_class) "
        "VALUES (%s, %s, 'demo_live', 'demo')",
        (tenant.org_id, "Leaf demo " + session.id.hex[:8]))
    conn.execute(
        "INSERT INTO identity_bindings (binding_id, external_authority, external_subject, "
        "platform_tenant_id, platform_user_id, role) "
        "VALUES (%s, 'leaf-demo', %s, %s, %s, 'read_only')",
        (tenant.binding_id, session.subject, tenant.org_id, tenant.platform_user_id))
    conn.execute(
        "INSERT INTO projects (project_id, org_id, name) VALUES (%s, %s, 'Sample rooftop')",
        (tenant.project_id, tenant.org_id))
    conn.execute(
        "INSERT INTO project_authority_modes (org_id, project_id, authority_mode, selected_by) "
        "VALUES (%s, %s, 'postgres_canonical', 'server')",
        (tenant.org_id, tenant.project_id))
    conn.execute(
        "INSERT INTO project_member_bindings "
        "(membership_id, org_id, project_id, binding_id, role, invited_by_binding_id) "
        "VALUES (%s, %s, %s, %s, 'editor', %s)",
        (tenant.membership_id, tenant.org_id, tenant.project_id,
         tenant.binding_id, tenant.binding_id))
    conn.execute(
        "INSERT INTO broker_tenants (tenant_id, tier, disabled) VALUES (%s, 'demo_live', false)",
        (tenant.tenant_id,))
    return tenant
