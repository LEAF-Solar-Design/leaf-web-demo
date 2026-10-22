# APS Design Automation — Flex concurrency-limit raise request

> **Status: RESOLVED IN WRITING 2026-09-09. Autodesk says Design Automation has NO fixed
> WorkItem concurrency limit and no raise process exists, so this request will NOT be filed
> (stamp corrected 2026-09-09).** Madhukar Moogala (Autodesk, Gmail `1a08497a802e0b7d`, echoing
> Julia Brun `1a08220848f47758`): WorkItems queue and the service scales workers; nothing is a
> concurrency setting tied to the client_id; the only per-client limits are 100 Activities and
> 100 AppBundles (DA rate-limits doc). Our ceiling of 1 is our own env cap, `APS_MAX_CONCURRENCY`
> in `da/queue.py` (default 1); lifting it is a deploy-time env change, staged 10 then 25.
> The 2026-09-09 onboarding call was CANCELED by Autodesk 2026-09-08T17:46Z (`1a082210a46bbcbb`);
> Madhu offers a follow-up call instead. Reply draft: `docs/aps-madhu-reply-draft-20260909.md`.
> A Gmail draft of that reply now sits in the thread (draft `1a0a973a947cb94d`, created
> 2026-09-16T09:02Z) and is NOT SENT as of 2026-09-21 (still in Drafts); sending it is an operator action.
> Superseded facts, do not reinstate: the 2026-07-23 Peter Schlipf onboarding slot is
> past and produced no ceiling answer, and the direct email alias has not replied since
> 2026-07-19 despite a third nudge sent 2026-09-02 (thread `19f7b19edf45b8bd`).
> **Current route.** Shruthi ArunKumar (Autodesk ADN) directed the concurrency question
> to https://aps.autodesk.com/get-help on 2026-09-03 (thread `19fa4d9252cc2aa7`).
> That filing was never made and is now MOOT (2026-09-09): the written answer above means
> there is nothing to file.
> **Former channel (call CANCELED, answered by email instead).** APS Onboarding call with Julia Brun, Wednesday **2026-09-09,
> 8:00-8:30 AM Central** (9:00 AM Eastern), Microsoft Teams, booked under "Learn more
> about APS pricing" (Gmail `1a0658a73ae03d45`). Ask: (a) does raising Design Automation
> WorkItem concurrency to ~25 require ADN membership or is it a separate commercial line,
> (b) is the limit per-app or per-account, (c) what does ~25 cost and what is the lead time.
> **ADN invoice PAID and membership ACTIVE, confirmed by Autodesk 2026-09-21T07:47Z.** Payment notice 2026DEVR7357, US$1,500.00
> (50% prorated for calendar 2026; the older $1,750 figure is superseded), signed and
> returned 2026-09-02, submitted to Autodesk order processing 2026-09-03. Autodesk invoice
> 9863264012 arrived 2026-09-05T01:04Z (Gmail `1a06f1818c33ef20`, PDF attached): subtotal
> US$1,500.00 plus US$120.00 tax = **US$1,620.00 due 2026-10-04** (net 30, sales order
> 7550280587, item ADN STANDARD 2-5PK DISC50). Paying it is an OPERATOR action; Shruthi ArunKumar gave the
> route on 2026-09-07 (Gmail `1a079f17cba5c178`): accounts.autodesk.com > Billing and Orders
> > Invoices and Credit Memos > Pay, then email her so she can verify the payment status.
> PAID 2026-09-17 ~09:16Z by domestic wire from the operator's Banc of California account to
> Autodesk Inc. at Citibank N.A., reference `5152452773 9863264012` (operator pressed Transmit;
> bank confirmation number not captured). Reply to Shruthi sent the same morning (Gmail
> `1a0aea6f31032096`, follow-up `1a0aeaff87a06483`, cc Eswar K). CONFIRMED 2026-09-21T07:47Z:
> Shruthi ArunKumar wrote that Autodesk received the payment and activated the ADN Membership 2026,
> with ADN Extranet access at https://adn.autodesk.io/ (Gmail `1a0c2efb1f0d311c`, thread `19fa4d9252cc2aa7`).
> Lane `aps-adn-spend-watch` RETIRED 2026-09-21 on that evidence (both terminal halves met).
> The Madhu reply above is still unsent and is no longer tracked by any lane.
> **Trigger has FIRED.** The operator reported demand above `APS_MAX_CONCURRENCY=1`
> in writing on 2026-09-02 ("~25 over the next few weeks"). This request is no longer parked.
> **App identity:** the clean-cutover app `czjIu4W9...` below is the target, not the
> legacy `iBZF...` app referenced in the original July booking text.
>
> Send via: NOT SENT. Superseded 2026-09-09 by Autodesk's written answer (see status). Kept for the record.
> (category: Design Automation API), or the 2026-09-09 onboarding call.

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
