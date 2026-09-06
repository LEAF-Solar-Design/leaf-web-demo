# Leaf platform front door

The platform front door is the guest entry to the Leaf workspace. The
operator rulings of 2026-07-26 define it as a guest-first sandbox with demand
capture and bring-your-own-key (BYO-key) use. This contract replaces the
previous description of a paid trial funnel.

## Guest entry

- The app-host root renders the landing cover for signed-out visitors,
  including on `leaf-platform-web.vercel.app`, `platform.leafdesign.ai`, and
  `platform-staging.leafdesign.ai`.
- The cover's workspace buttons, Solve action, and T shortcut open
  `/try?demo=1` on the same origin. No login or payment is required to try.
- Guest entry uses a page load because SiteRoot and ToolCast read demo mode
  at module initialization. A client-side recast from a bare root would not
  enable the existing local demo.
- The sandbox uses the existing sample drawing and client-side tools. It
  does not promise live cloud execution or change cloud data.

## Demand capture and BYO key

No payment processor exists. There is no Stripe integration, payment
checkout, card collection, subscription purchase, or checkout-minted trial
in this front-door contract.

Unratified pricing appears as **PRICING PLACEHOLDER**, never an invented
price, trial duration, or plan promise. The landing pricing tab focuses the
interest form instead of directing visitors to a plan purchase.

The only monetization call to action is **Register interest**. The form uses
the existing `submitDemandCapture` client and `POST /api/demand` endpoint.
It records an email and interest in BYO-key use, reports success only after
the endpoint succeeds, and offers retry after failure. It neither charges
the visitor nor grants entitlement. Registering interest is optional and
never blocks the sandbox.

BYO-key use keeps the operator's provider credentials and provider credit
separate from platform identity. The front door collects no key and makes
no claim to sell provider credit or enable live execution through a purchase.

## Existing workspace and authentication

Signed-in workspace entry keeps the existing client-side `/try` navigation
and query preservation. Existing `/app` and `/ty` console routes, legacy
deep links, and authentication callback deferral remain unchanged.
`web/src/site/authBoot.js` owns those boot decisions. Authentication is for
existing account-backed work, not a prerequisite for the guest sandbox.

## Marketing and indexing

`https://www.leafautomation.ai` remains the indexable marketing site.
The SPA retains its existing noindex shell and robots policy. App-host roots
stay in the app so visitors can try the sandbox; only the sheets scene on
the listed app hosts redirects to the marketing sheets hub. Local and
preview sheets remain available for development. This front-door change
does not change public aliases or infrastructure.
