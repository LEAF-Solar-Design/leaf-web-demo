# Deploy kinds

Open `/app/lab/deploys` to inspect the eight kinds. Only kinds 1 to 3 read tenant ledger state. Kinds 4 to 8 show a Handoff badge and require evidence from their own release surface.

| Kind | Trigger | Rail | Receipt | Rollback | Live state |
| --- | --- | --- | --- | --- | --- |
| 1. Revision publish | Publish in the save bar | Tenant ledger head and published revision | Publish receipt id | Restore the prior revision as a new revision | Yes |
| 2. Read-only activation | Request activation | Tenant ledger published revision; SEL-312 handoff | Activation evidence arrives with SEL-312 | Activation rollback through SEL-312 | Yes |
| 3. Widget publish | Request publication | Tenant ledger published revision; SEL-309 handoff | Widget publication arrives with SEL-309 | Widget rollback through SEL-309 | Yes |
| 4. Studio release | Approved release | Staging alias: native ECS, forward-normal, observe_only. Platform production: leaf-platform-production, operator_yes through release-train promote | Target surface release-train receipt | release-train rollback | No |
| 5. Ops UI and API release | Separate UI or API release | ops-dashboard: Vercel preview to production alias, observe_only. kashbot-api: ECR image to ECS task definition, standing_authorization under OPERATOR-WISHES, recorded in the registry | Separate receipts for each surface | release-train rollback for the affected surface | No |
| 6. Runner release | Approved rollout | Runner release-train surface | Runner LIVE receipt | Previous LIVE revision | No |
| 7. Trading lifecycle | Requested action, recorded as its own fact | Runner outcome, recorded as its own fact | Observed revision, recorded as its own fact | Request a compensating action and observe the runner outcome | No |
| 8. Native renderer | Approved internal TestFlight release | Internal TestFlight; production exposure is separate | Internal TestFlight receipt | Prior internal build; review production exposure separately | No |

The current ledger client retains head, published and lastSaved but does not retain the publish receipt id. The view reports that gap explicitly. Do not substitute a saved revision hash for a publish receipt. Activation and widget evidence remain handoffs until SEL-312 and SEL-309 supply them.

## State legend

| Label | Evidence |
| --- | --- |
| requested | AUTHORIZED pending. An authorized request is not activation evidence. |
| published | Tenant ledger publish receipt. Publication does not prove running state. |
| running | PROMOTING with activation evidence. |
| observed | LIVE receipt. |

Release-train progression is BUILT, PROVEN, ELIGIBLE, AUTHORIZED, PROMOTING, LIVE. Its receipt path pattern is:

```text
~/.claude/program/release-train/receipts/<surface>/<surface>__<candidate>__<utc>__<to_state>.json
```

## Production prerequisites

`migrate-leaf-platform-production-db.yml` runs before any migration-bearing image, even when the Lab rail is off. Obtain its successful workflow receipt before promotion.

Never deploy production with `deploy-web.py` without `--preview`. The script defaults to production and adds `--prod` unless `--preview` is supplied. Use the preview flag for previews; platform production promotion goes through release-train promote on `leaf-platform-production`.

## Registry drift found while writing this

Release-train owner: the plan names `leaf-web-demo-production`, but that surface is retired in favour of `leaf-platform-production`. Update the plan to the registered ECR native manifest to ECS leaf-platform surface, with `operator_yes` authorization.

Release-train owner: kashbot-api is `standing_authorization`, not `operator_yes`. Its standing authorization comes from OPERATOR-WISHES and is recorded in the registry. Keep ops-dashboard's `observe_only` status separate from that API authorization.
