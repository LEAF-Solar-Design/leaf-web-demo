# DRAFT: ADN reinstatement email + application answer sheet (not sent)

> **Status: DRAFT ONLY, 2026-07-26. Nothing has been sent, nothing purchased.**
> Sending, applying, and paying are operator actions.
>
> **Decision recorded (operator, 2026-07-26): buy the membership.** Leaf held ADN
> under `ehaug@leafsolardesign.com`, completed the no-charge start-up period, and
> chose not to renew. The new paid membership goes on `ehaug@leafautomation.ai`,
> the Autodesk ID tied to the live APS production app. Published pricing is 1,750
> for 1 user, 3,000 for 2 to 5, 5,500 for 6 or more, currency varying by region.
> Membership runs January to December and is not prorated, which is why the email
> asks what a July purchase actually covers before money moves.
>
> **Purpose of the membership:** it unlocks ADN member API support, which is the
> queue that can action the WorkItem concurrency raise in
> `docs/aps-concurrency-raise-request.md`. That request attaches to this email so
> it is queued the moment membership is active.
>
> **To:** Eswar K, ADN representative for US and Canada, `eswar.k@autodesk.com`
> (published at https://aps.autodesk.com/autodesk-developer-programs/adn/contact-adn)
> **From:** `ehaug@leafautomation.ai`
> **Attach:** `docs/aps-concurrency-raise-request.md`
> **Application portal:** https://adndata.autodesk.io/sysinit.asp?SessionType=Applicant
> (answer sheet at the bottom of this file)

---

**Subject:** Reinstating ADN membership (1 seat) and filing a Design Automation concurrency raise

Hello Eswar,

I am the founder of Leaf Automation, a one-person US company building a hosted,
multi-tenant CAD automation product on APS Design Automation for AutoCAD. We are
live on Design Automation Flex in `us-east` against engine
`Autodesk.AutoCAD+26_0`, with real paid WorkItems running in production today.

We previously held ADN membership under `ehaug@leafsolardesign.com` through the
no-charge start-up period and did not renew when it ended. We would like to come
back as a paying member, one developer seat, under
**`ehaug@leafautomation.ai`**, which is the Autodesk ID tied to our current APS
production app.

Could you send the purchase details, or point me at the right order path? Two
things I would like to get right before paying:

1. Membership runs January to December without proration. Buying in late July,
   does the fee cover through December 2026 only, or does a purchase this late in
   the year extend into 2027? If waiting a few months materially changes what we
   get for the money, I would rather know now.
2. Should I submit a fresh application at the ADN portal, or can the prior
   membership record be reused so the history stays on one company?

**What we need the membership for.** We need a raise to the WorkItem concurrency
limit on our production app, and ADN member API support is, as I understand it,
the queue that handles limit changes. The full write-up is attached. In short:

- **App:** `Leaf Design Automation Production`, client ID
  `czjIu4W9OK9fSoWAJ6lddfj00tv6tPooFdBqMfFbEKP2AbfV`, region `us-east`,
  engine `Autodesk.AutoCAD+26_0`, billing Design Automation Flex.
- **Today:** our submit path is capped at 1 to 2 concurrent WorkItems to stay
  safely under the default per-app limit, so every tenant queues behind one
  in-flight WorkItem.
- **Asking for:** 25 concurrent WorkItems, staged to 10 first and 25 later if you
  prefer to ramp.
- **Load profile, measured not estimated:** each WorkItem is a headless AutoCAD
  extract running about 2.4 to 4.1 engine-seconds at roughly $0.006 to $0.008 per
  run. Our 2026-07-19 smoke on the current app measured 2.43 engine-seconds at
  $0.0068.
- **We rate-limit ourselves:** dispatch is a round-robin fair queue with a hard
  global ceiling, a tenant's k-th WorkItem never goes out before every other
  tenant's (k-1)-th, and we raise our internal ceiling only in lockstep with
  whatever is granted, never above it. Abandoned WorkItems are cancelled through
  `DELETE /workitems/{id}`, so a higher ceiling does not become stuck billable
  work.

Worth saying plainly: this raise increases our Design Automation consumption
rather than reducing it. A ceiling of 1 to 2 is what currently caps how much we
can put through the service, because one tenant's run blocks every other tenant.

One last question so I file it correctly once membership is active: is the change
request form the right instrument for WorkItem concurrency specifically, or does
concurrency go through a different form or team?

Happy to share app logs, WorkItem ids, or take a call at your convenience.

Best regards,

Evan Haug
Leaf Automation
ehaug@leafautomation.ai

---

## ADN application answer sheet

For https://adndata.autodesk.io/sysinit.asp?SessionType=Applicant. Copy-paste
ready. Field names on the form may differ slightly from the labels below.

| Field | Value |
|---|---|
| Company | Leaf Automation |
| Entity type | Limited liability company (LLC) |
| Country | United States |
| Autodesk ID / primary contact email | ehaug@leafautomation.ai |
| Primary contact | Evan Haug, founder |
| Developer seats requested | 1 |
| Membership category | Paid, standard (not start-up: that period is complete) |
| Prior ADN history | Member under ehaug@leafsolardesign.com; start-up period completed; not renewed |
| Autodesk technologies used | Design Automation for AutoCAD, Object Storage Service, Model Derivative |
| Products developed | Hosted multi-tenant CAD automation platform for solar and electrical design |
| Deployment | Commercial SaaS, live production traffic |
| Current Autodesk spend | Design Automation Flex, pay-as-you-go |
| Primary support need | Design Automation API, WorkItem concurrency and quota changes |

**Company description (for a free-text field):**

> Leaf Automation builds a hosted, multi-tenant CAD automation platform on
> Autodesk Platform Services. Customers upload drawings and run automated
> AutoCAD workflows through APS Design Automation, with results returned to a
> web application. We run in production on Design Automation Flex and need
> higher WorkItem concurrency to serve tenants without head-of-line blocking.

> Seat count, entity type, and prior-membership history confirmed by the operator
> on 2026-07-26. If the form asks for the exact registered name, use the name on
> the LLC filing rather than the short form above.
