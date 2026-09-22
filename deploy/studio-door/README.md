# Studio door: studio.leafautomation.ai

The customer-facing studio host. It is a Vercel project (`leaf-studio-door`) whose only
job is to rewrite every request to the studio origin, so the browser sees one origin
(`https://studio.leafautomation.ai`) while the app, its assets and its API are served
by the Cloudflare-fronted ALB at `platform.leafdesign.ai` (production). Until the
production candidate is promoted the door pointed at `platform-staging.leafdesign.ai`;
this repoint is the W4g alignment queue's R-07 and merges only on the operator's fresh
yes after R-03 (the read-only production smoke driver) is green on the candidate.
Rollback: set `destination` back to `https://platform-staging.leafdesign.ai/$1` and
redeploy, or `vercel rollback` to the previous deployment.

Why a rewrite and not DNS: the `leafautomation.ai` zone lives on Vercel DNS (mail,
MTA-STS and the Auth0 login domain sit there, so the nameservers never move), and the
studio ALB only accepts Cloudflare source addresses, so a CNAME straight at the ALB cannot
work. The rewrite keeps the API same-origin for the SPA (it calls its API by relative
path, `web/src/api.js` `API_BASE`), so no backend CORS change is needed.

What else the host needs, outside this directory:

- Auth0 SPA client `Leaf Web Platform` (`zkJjr0ZFtcyQjyJ8e4zdkdgzoMaVWt5O`) must list
  `https://studio.leafautomation.ai` in callbacks, web origins and logout URLs, because
  the SPA sends `redirect_uri = window.location.origin`.
- `leaf_website` sends its Try button here (staging branch first).

Deploy (CLI signed in as the team owner, from this directory):

    vercel deploy --prod --yes --scope team_LWjg4ghzDbsZrkNPaOnRwAx5

Repoint the door at a different studio origin by editing the `destination` in
`vercel.json` and redeploying. Rollback is the previous Vercel deployment
(`vercel rollback`), or removing the domain from the project.

Known limits: Vercel rewrites do not carry WebSockets. The studio client uses
`EventSource` (SSE) for job streams, which the door passes through; verify streaming after
any change with `curl -N https://studio.leafautomation.ai/api/health`.
