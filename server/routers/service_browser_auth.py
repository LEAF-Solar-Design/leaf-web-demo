"""Visible service-principal login; all identity authority stays in deps/auth."""
import html
import secrets

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from uuid import UUID

import deps
import service_browser_sessions as sessions

router = APIRouter(prefix="/api/auth/agent", tags=["service browser login"])


def response(content, code=200):
    return JSONResponse(content, status_code=code, headers={"Cache-Control": "no-store"})


def set_cookie(res, name, value, ttl):
    res.set_cookie(name, value, max_age=ttl, secure=True, httponly=True,
                   samesite="strict", path="/")


def clear_cookie(res, name):
    res.delete_cookie(name, path="/", secure=True, httponly=True, samesite="strict")


@router.get("/login", response_class=HTMLResponse)
def login(request: Request):
    cfg = sessions.config()
    sessions.local_request(request, cfg)
    nonce, script_nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    challenge = sessions.store.create(cfg, nonce)
    # All substitutions are server-generated, escaped text (never provider data).
    body = """<!doctype html><html><head><meta charset="utf-8"><title>Service admin login</title></head>
<body><h1>Studio service admin login</h1><p>Approve this challenge using the Studio agent CLI.</p>
<p>Origin: <strong>ORIGIN</strong></p><p>Public challenge: <code>CHALLENGE</code></p>
<p id="status">Waiting for service approval (up to 120 seconds).</p>
<script nonce="NONCE">
const deadline = Date.now() + 120000;
async function poll() {
  if (Date.now() >= deadline) { document.getElementById('status').textContent = 'Challenge expired. Reload to begin again.'; return; }
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), Math.max(1, deadline - Date.now()));
  try {
    const r = await fetch('/api/auth/agent/challenge/CHALLENGE/poll', {
      method: 'POST', credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal: controller.signal});
    if (!r.ok) throw new Error('denied');
    const result = await r.json();
    if (result.redirect) {
      const target = new URL(result.redirect, location.origin);
      if (target.origin !== location.origin || !result.redirect.startsWith('/?')) throw new Error('denied');
      location.replace(target.href); return;
    }
    setTimeout(poll, 1000);
  } catch (_) { document.getElementById('status').textContent = 'Login unavailable. Reload to begin again.'; }
  finally { clearTimeout(timeout); }
}
poll();
</script></body></html>"""
    body = body.replace("ORIGIN", html.escape(cfg.origin)).replace(
        "CHALLENGE", html.escape(challenge)).replace("NONCE", html.escape(script_nonce, quote=True))
    res = HTMLResponse(body, headers={
        "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; script-src 'nonce-" + script_nonce +
        "'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"})
    set_cookie(res, sessions.PENDING_COOKIE, nonce, 120)
    return res


@router.get("/challenge/{challenge}/status")
def status(challenge: str, request: Request):
    cfg = sessions.config()
    sessions.local_request(request, cfg)
    try:
        challenge = str(UUID(challenge))
    except ValueError:
        # A public lookup is not authority. Do not reflect arbitrary input in
        # validation errors or hit the database for malformed identifiers.
        return response({"exists": False, "pending": False, "expired": False,
                         "environment": cfg.environment, "origin": cfg.origin})
    return response(sessions.store.status(cfg, challenge))


class Approval(BaseModel):
    challenge_id: UUID


@router.post("/approve")
def approve(body: Approval, request: Request, tenant=Depends(deps.require_tenant)):
    cfg = sessions.config()
    sessions.local_request(request, cfg)
    token, subject, exp = sessions.authorize(request, tenant)
    sessions.store.approve(cfg, str(body.challenge_id), token, subject, exp)
    return response({"approved": True, "challenge_id": str(body.challenge_id),
                     "environment": cfg.environment, "origin": cfg.origin})


@router.post("/challenge/{challenge}/poll")
def poll(challenge: UUID, request: Request):
    cfg = sessions.config()
    sessions.local_request(request, cfg, origin_required=True)
    nonce = sessions.cookie_value(request, sessions.PENDING_COOKIE)
    result = sessions.store.consume(cfg, str(challenge), nonce)
    if result is None:
        state = sessions.store.status(cfg, str(challenge))
        return response({"pending": state["pending"], "expired": state["expired"]},
                        200 if state["pending"] else 410)
    binding, secret = result
    res = response({"redirect": "/?service_session=" + binding})
    set_cookie(res, sessions.SESSION_COOKIE, secret, 900)
    clear_cookie(res, sessions.PENDING_COOKIE)
    return res


def joint_session(request, tenant):
    row = getattr(request.state, "service_browser_session", None)
    if not row:
        sessions.deny()
    sessions.authorize(request, tenant)
    if row["subject"] != tenant.subject:
        sessions.deny()
    return row


@router.get("/session")
def session(request: Request, tenant=Depends(deps.require_tenant)):
    cfg = sessions.config()
    sessions.local_request(request, cfg)
    row = joint_session(request, tenant)
    return response({"authenticated": True, "identity": {"subject": row["subject"],
                     "sub": tenant.subject, "iss": "https://leafautomation.us.auth0.com/",
                     "org_id": tenant.org_id,
                     "principal_type": "service_admin", "tenant_id": str(tenant)},
                     "expires_at": row["expires_at"].isoformat(),
                     "binding_id": str(row["binding_id"])})


def require_logout_origin(request: Request):
    # Enforce CSRF before resolving any identity, including cookie-only callers.
    cfg = sessions.config()
    sessions.local_request(request, cfg, origin_required=True)


@router.post("/logout", dependencies=[Depends(require_logout_origin)])
def logout(request: Request, tenant=Depends(deps.require_tenant)):
    cfg = sessions.config()
    sessions.local_request(request, cfg, origin_required=True)
    row = joint_session(request, tenant)
    sessions.store.revoke(cfg, str(row["binding_id"]),
                          sessions.cookie_value(request, sessions.SESSION_COOKIE))
    res = response({"authenticated": False})
    clear_cookie(res, sessions.SESSION_COOKIE)
    return res
