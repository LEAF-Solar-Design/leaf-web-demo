/**
 * Auth0 PRE-USER-REGISTRATION Action — SELF-SERVE signup variant (PROPOSED).
 * ---------------------------------------------------------------------------
 * Concern 1 (Leaf PLATFORM identity). This is the self-serve counterpart to the
 * production B2B gate at:
 *   leaf_website/docs/auth0-actions/pre-user-registration-bypass-for-invitations.js
 *
 * DIFF vs the production action (review before deploying):
 *   - PROD: maintains a BLOCKED_DOMAINS list (gmail/yahoo/outlook/...) and calls
 *           api.access.deny(...) for personal-email signups UNLESS the user has
 *           a pending invitation (checked via WEBSITE_API_URL + AUTH0_ACTION_SECRET).
 *   - SELF-SERVE (this file): personal-email signups are ALLOWED by default —
 *           no BLOCKED_DOMAINS denial. The invitation check is retained ONLY to
 *           ATTACH org metadata for invited users (never to gate access), and it
 *           is best-effort: a missing config or network error does NOT block
 *           signup (fail-open, the opposite of PROD's fail-closed).
 *   - An OPTIONAL, default-EMPTY disposable/abuse domain list remains as a knob
 *           the operator can populate later if self-serve is abused.
 *
 * OPERATOR: this RELAXES the live B2B signup gate. Deploying it (Auth0 Dashboard
 * -> Actions -> Pre User Registration flow) permits anyone to self-register.
 * Confirm intent + MAU/plan headroom before deploying. See contract/AUTH.md.
 *
 * Secrets (optional, only for invitation metadata attach):
 *   WEBSITE_API_URL, AUTH0_ACTION_SECRET  (+ dependency axios@1.6.0)
 */

'use strict';

// OPTIONAL abuse guard — default EMPTY (self-serve open). Populate to block
// disposable-email providers if signup abuse appears.
const BLOCKED_DOMAINS = [];

exports.onExecutePreUserRegistration = async (event, api) => {
  const email = (event.user && event.user.email ? event.user.email : '').toLowerCase();
  if (!email) {
    api.access.deny('invalid_email', 'Email address is required');
    return;
  }
  const domain = email.split('@')[1];

  // Only the explicit abuse list is denied (empty by default => everyone allowed).
  if (BLOCKED_DOMAINS.includes(domain)) {
    api.access.deny('signup_blocked', 'This email domain is not allowed');
    return;
  }

  // Best-effort invitation metadata attach (NEVER gates access in self-serve).
  const websiteUrl = event.secrets && event.secrets.WEBSITE_API_URL;
  const actionSecret = event.secrets && event.secrets.AUTH0_ACTION_SECRET;
  if (websiteUrl && actionSecret) {
    try {
      const axios = require('axios');
      const response = await axios.get(`${websiteUrl}/api/invitations/check`, {
        params: { email },
        headers: { Authorization: `Bearer ${actionSecret}` },
        timeout: 5000,
      });
      const data = response.data || {};
      if (data.hasInvitation) {
        api.user.setUserMetadata('invited_to_org', data.organizationName);
        api.user.setUserMetadata('invited_role', data.role);
      }
    } catch (err) {
      // Fail-OPEN: log and allow signup regardless (self-serve).
      const msg = err instanceof Error ? err.message : String(err);
      console.log('invitation check skipped (self-serve allows anyway):', msg);
    }
  }
  // Allow signup.
};
