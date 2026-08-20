# FRONT-DOOR — one indexable marketing surface

Adopted 2026-08-19 (operator decision D2, recorded in leaf_website
`docs/decisions/public-web-overhaul-20260819.md`). Grounding: the 2026-08-19
Search Console audit found this repo's SPA serving a full duplicate of the
marketing site — its own wordmark, positioning copy, "Start free trial"
button, and Auth0 login — indexable at leaf-platform-web.vercel.app and via
the browser routes of platform.leafdesign.ai, splitting link equity and the
billing funnel across two properties.

## The contract

1. **The one indexable front door is `https://www.leafautomation.ai`**
   (repo `leaf_website`). Every deployed surface of this repo's `web/` SPA is
   app-only.
2. **noindex everywhere, structurally.** `web/index.html` carries
   `<meta name="robots" content="noindex, nofollow">`; because every SPA route
   serves that one shell, the whole deployment is covered. `web/public/
   robots.txt` disallows all (nginx's `try_files $uri` serves it on ECS; the
   Vercel rewrite excludes it explicitly in `web/public/vercel.json`).
3. **Marketing scenes redirect.** On the app-only hosts
   (`leaf-platform-web.vercel.app`, `platform.leafdesign.ai`,
   `platform-staging.leafdesign.ai` — the `APP_ONLY_HOSTS` set in
   `web/src/site/SiteRoot.jsx`), scenes `site` and `sheets` redirect to the
   front door, path-preserved (`/sheets*` lands on the www sheets hub because
   the two surfaces use different sheet codes). Auth0's bare-origin callback
   and every `?demo/?fixture/?ops/?dev/?drawing` deep link boot scene `app`
   first (`bootWantsApp`), so they never reach the redirect. localhost and
   Vercel previews are deliberately NOT redirected — the site scenes stay
   viewable for development and review; the noindex still applies there.
4. **One trial entry.** `startTrial()` in `web/src/site/LandingCast.jsx` goes
   to `https://www.leafautomation.ai/get-started`, where Stripe checkout mints
   Auth0 `app_metadata.leaf` and the platform enforces entitlement
   (`server/entitlements.py`). The SPA never mints its own trial path.
   Existing-user sign-in (`web/src/auth.js`) is unchanged.
5. **The `web/src/site/**` source stays.** leaf_website's homepage is a port
   of this markup (see leaf_website `docs/port/architecture-contract.md`);
   this contract changes what the deployed SPA *serves*, never what the port
   reads.

## Durable follow-up (terraform, separately ceremonied)

The lasting version of §3 for `platform.leafdesign.ai` is turning the public
web service off at the infrastructure layer
(`leaf_platform_web_public_enabled` in leaf-automation-aws-terraform), which
removes the duplicate surface rather than redirecting it. That flag flip is a
terraform + operator ceremony and is NOT part of this contract's code change;
until it lands, the in-app redirect above is the enforced posture.

## Changing this contract

Adding a public alias that serves this SPA requires adding it to
`APP_ONLY_HOSTS` in the same PR — or a ratified decision to open a second
front door, which supersedes this file.
