# APS Design Automation — Flex concurrency-limit raise request

> **Status: SUBMITTED VIA APS ONBOARDING FALLBACK (2026-07-18).** Autodesk's
> logged-in Assistant confirmed that direct API tickets require an active ADN
> membership, which this account does not have. The complete concurrency request
> below was therefore submitted in the special-request field for an official APS
> Onboarding call with Peter Schlipf on **Thursday, 2026-07-23 at 5:30 AM Central**.
> Microsoft Bookings confirmed the appointment and will send the calendar receipt
> to `ehaug@leafautomation.ai`. This path does not issue a support ticket number.
> **App identity changed on 2026-07-19:** the clean-cutover app below replaced the
> inaccessible legacy app as the target of this request. At the onboarding call,
> ask Autodesk to apply any concurrency grant to the new client ID below, not the
> legacy `iBZF...` app referenced in the original booking text.
> **Updated request emailed 2026-07-19:** sent from
> `ehaug@leafautomation.ai` to
> `AutodeskPlatformServicesAPSSupport@autodesk.com`, requesting 25 concurrent
> WorkItems (10 then 25 acceptable) for the new app and asking Autodesk to attach
> it to the July 23 onboarding call. Awaiting reply/reference number.
>
> Send via: APS support ticket at https://aps.autodesk.com/en/support/get-help
> (category: Design Automation API), or your Autodesk account/partner contact.

---

**Subject:** Request to raise Design Automation WorkItem concurrency limit — app `czjIu4W9OK9fSoWAJ6lddfj00tv6tPooFdBqMfFbEKP2AbfV`

Hello APS Design Automation team,

We operate a hosted, multi-tenant CAD-automation product (Leaf) built on APS
Design Automation for AutoCAD. We are requesting a raise to the **WorkItem
concurrency limit** on our production app.

## 1. Account / app identity

| Field | Value |
|---|---|
| APS application | `Leaf Design Automation Production` (Developer Hub: `Leaf Automation APS`) |
| APS client ID / app nickname | `czjIu4W9OK9fSoWAJ6lddfj00tv6tPooFdBqMfFbEKP2AbfV` |
| Engine | `Autodesk.AutoCAD+26_0` (Design Automation for AutoCAD) |
| Region | `us-east` (`/da/us-east/v3`) |
| OSS bucket (persistent store) | `leaf-web-store-czjiu4w9ok9fsowa` (policy: **persistent**) |
| Billing | Design Automation Flex (pay-as-you-go) |
| Typical WorkItem | headless AutoCAD extract + read-only tools, **~2.4–4.1 engine-seconds**, **~$0.006–0.008/run** at the AutoCAD DA rate; new-app smoke on 2026-07-19: **2.43 engine-seconds / $0.0068** |

## 2. Current limit (observed)

Our app currently runs against the **default per-app WorkItem concurrency limit**.
In practice our submit path is throttled to a conservative in-app ceiling of
**`APS_MAX_CONCURRENCY = 1–2`** concurrent WorkItems to stay safely under that
limit, which serializes all tenants behind a single in-flight WorkItem.

The exact account-side limit was not exposed in the available APS console. The
request was submitted using the observed/assumed **1–2** figure; APS can confirm
the authoritative limit from the app id above.

## 3. Requested limit

We request a raise to **25 concurrent WorkItems** for this app (a staged raise to
10 first, then 25, is also acceptable if you prefer to ramp).

## 4. Justification (multi-tenant fair scheduling)

- **Multi-tenant load.** The app serves many independent customer tenants, each
  isolated by object-key prefix in the shared persistent OSS bucket above. We
  expect **20–50 active tenants** in the near term, each able to submit a short
  burst of runs. With a ceiling of 1–2, one tenant's run blocks every other
  tenant — unacceptable head-of-line blocking for an interactive product.
- **We already scheduler-limit ourselves.** We do **not** intend to flood the
  service. Our submit path is a **round-robin fair queue** with a hard global
  ceiling (`APS_MAX_CONCURRENCY`): a tenant's *k*-th WorkItem is never dispatched
  before every other tenant's *(k-1)*-th, and total in-flight WorkItems never
  exceed the configured ceiling. We will raise our internal ceiling **in lockstep
  with** whatever limit you grant — never above it.
- **Short, cheap WorkItems.** Each WorkItem is ~2.4–4.1 engine-seconds. A ceiling
  of 25 bounds our steady-state concurrent engine usage to well under a minute of
  aggregate engine time per wave, i.e. modest, bursty, pay-as-you-go load.
- **Orphan protection in place.** We cancel abandoned WorkItems (closed browser
  tab / expired session lease) via `DELETE /workitems/{id}`, so a raised limit
  will not translate into stuck or runaway billable WorkItems.

## 5. What a raise unblocks

With 25 concurrent WorkItems and our fair scheduler, up to 25 tenants can run
simultaneously with no cross-tenant blocking, while our internal ceiling +
per-tenant round-robin fairness keep any single tenant from monopolizing the pool.

Thank you — happy to provide app logs, sample WorkItem ids, or a call if useful.

Best regards,
Leaf — Design Automation integration team
