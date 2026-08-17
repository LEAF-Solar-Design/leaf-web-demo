# APS Design Automation — Flex concurrency-limit raise request

> **Status: NOT YET DELIVERED TO THE APS TECHNICAL TEAM (corrected 2026-07-26).**
> The 2026-07-23 onboarding call connected us with Autodesk **sales** by accident.
> We have never met the APS technical team, so this request has not been made to
> anyone who can act on it. Do not record this as "awaiting Autodesk": the next
> step is ours, which is to reach the APS Design Automation technical team through
> a channel that can grant a concurrency raise. The submission history below is
> kept for the record. A drafted (unsent) approach to the ADN representative,
> which is the queue that can actually action this, is in
> `docs/adn-membership-reinstate-email-draft.md`.
>
> **Re-verified 2026-08-17** against this repo's own state: the ADN draft is still
> headed "DRAFT ONLY ... nothing has been sent, nothing purchased", and no APS or
> ADN doc has changed on `main` since `461c6074` (2026-07-26). So nothing has been
> delivered in the interim and this status still holds. It rots only when the ADN
> membership is actually purchased or a technical-team channel actually opens, both
> of which are operator actions; whoever takes one, correct this block in the same
> change. This correction itself sat uncommitted in a working tree for three weeks
> while `main` told readers the opposite, which is the failure this stamp exists to
> prevent.
>
> **Operator decision 2026-08-17: one app, and the raise is PARKED, not dropped.**
> All Design Automation runs stay on the existing single APS app named below, "at
> least for now with minimal demo traffic". Two consequences, and they point in
> opposite directions, so do not collapse them:
>
> 1. Consolidating on one app does NOT raise the ceiling, so this request stays
>    valid. Our own enforcement is a single global ceiling across every tenant
>    (`APS_MAX_CONCURRENCY` in `da/queue.py`, default **1**), and it moves only when
>    Autodesk grants a raise, never by buying anything. Nothing in this repo ties
>    Flex token spend or a subscription tier to the ceiling.
> 2. At demo-scale traffic a ceiling of 1 is tolerable, so this is no longer
>    urgent. It is PARKED. Revisit when concurrent demand actually exceeds the
>    ceiling, which is the trigger, not a date.
>
> **Ceiling scope is UNCONFIRMED on the Autodesk side, and this document and the
> code word it differently.** `da/queue.py` calls it the "account Flex cap ... across
> ALL tenants" while the request text below calls it the "default per-app WorkItem
> concurrency limit". Nobody here has confirmed which Autodesk actually enforces.
> It does not change what to do: if the cap is per-account, one app cannot lift it;
> if it is per-app, funnelling every tenant through one app makes it bite harder.
> Either way the raise is the only relief. Do not quote either wording as settled.
>
> **Before paying for ADN, use the free channel.** `docs/adn-purchase-walkthrough.md`
> records a no-membership fallback: the "Ask the APS Expert" call, every other
> Wednesday at 11 AM ET, where APS engineers can confirm whether a concurrency
> change must go through ADN at all. That answers the one question the ADN purchase
> is currently premised on, at zero cost. It cannot itself grant the raise.
>
> **History: submitted via APS onboarding fallback (2026-07-18).** Autodesk's
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
> it to the July 23 onboarding call. No reply and no reference number ever came
> back, and the call itself reached sales, not the technical team.
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
