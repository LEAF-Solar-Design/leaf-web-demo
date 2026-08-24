# Buying ADN membership: step-by-step walkthrough

**For:** Evan Haug, signing in as `ehaug@leafsolardesign.com`
**Goal:** an active ADN membership that unlocks member API support, so the
WorkItem concurrency raise in `docs/aps-concurrency-raise-request.md` can be
filed with the Design Automation team.
**Written:** 2026-07-26.

**What is verified here:** the URLs, the redirect, and the exact wording of the
pre-login screen (observed directly). The published pricing and the start-up
terms come from Autodesk's own membership page.
**What is not verified:** everything after sign-in. The application form, the
payment step, and the review time are behind a login I have not entered and will
not. Steps 3 onward describe what to expect and what to do when the screen does
not match.

---

## Before you start (5 minutes)

Have these to hand:

- **The Autodesk ID you want to sign in with.** You have chosen
  `ehaug@leafsolardesign.com`, the account that held the prior membership.
- **Exact legal name of the LLC** as filed. The form will likely ask for the
  registered entity, not the trading name.
- **Company address, phone, and website** (`leafdesign.ai`).
- **A payment method.** Card or invoice details, depending on what the order
  path offers.
- **The answer sheet** at the bottom of
  `docs/adn-membership-reinstate-email-draft.md`. Every field the form asks for
  is already written out there, including a company description for free-text
  boxes.
- **Cost expectation:** published list is **1,750 for 1 user**, 3,000 for 2 to 5,
  5,500 for 6 or more, currency varying by region.

### One thing to settle before you pay

The membership will sit on `ehaug@leafsolardesign.com`, but the APS production
app (`czjIu4W9OK9fSoWAJ6lddfj00tv6tPooFdBqMfFbEKP2AbfV`) and all live Design
Automation traffic sit on `ehaug@leafautomation.ai`. Support entitlement follows
the membership account.

This is very likely fine, because ADN membership is held by a **company**, not a
person, and both addresses are Leaf. But if it is not fine, you will discover it
at the worst moment: after paying, when you try to file a case about an app the
membership account does not own.

Two ways to de-risk it, both cheap:

1. During the application, list `ehaug@leafautomation.ai` as an additional
   contact or developer on the membership if the form allows it.
2. Ask the rep outright, in the same message that asks for the order path.
   That question is already in the draft email.

---

## Step 1: open the portal

Go to:

```
https://adndata.autodesk.io/sysinit.asp?SessionType=Applicant
```

This **301-redirects to `https://adn.autodesk.io`**. That is expected, not a
broken link. Either address lands in the same place.

You will see a page headed **"Autodesk Developer Network"** with the line:

> "Autodesk Developer Network is designed for software development businesses
> and teams that develop specialized solutions and integrations with Autodesk
> technology."

and then, twice on the page:

> "Click here to initiate or continue an ADN Membership Application"

followed by:

> "You need to sign-in to submit your application to the ADN program. Use the
> button below to sign-in."

with a **Sign In** button.

Note the phrase **"initiate or continue"**. Applications are resumable, so if you
started one previously it should pick up rather than force a restart.

---

## Step 2: sign in as `ehaug@leafsolardesign.com`

Use the **Sign In** button. This is a standard Autodesk ID login.

**Watch for these three outcomes:**

- **It signs in and shows a new blank application.** Normal. Continue to step 3.
- **It signs in and shows an existing or expired membership record.** Better.
  Look for a renew or reinstate option rather than starting fresh, because
  keeping the history on one company record is cleaner and may shorten review.
- **It refuses the address or shows no developer program at all.** Do not create
  a second Autodesk ID to work around it. Stop and email the rep instead
  (step 6), because a duplicate company record is exactly the mess that takes
  weeks to unpick.

---

## Step 3: complete the application

Fill it from the answer sheet in `docs/adn-membership-reinstate-email-draft.md`.
The values that matter:

- **Seats: 1.** This sets the price band.
- **Category: paid, standard.** Not the start-up category. Leaf already completed
  the no-charge start-up window under this same account, and claiming it again
  would be caught and would cost you credibility with the rep you are about to
  depend on.
- **Prior history: disclose it.** "Member under ehaug@leafsolardesign.com,
  start-up period completed, not renewed." They will see it anyway.
- **Primary support need: Design Automation API**, specifically WorkItem
  concurrency and quota changes. This routes you to the right support group.

---

## Step 4: payment

I cannot tell you what this screen looks like, and I cannot do this step for you.
Entering payment details is yours alone.

Two things to check before you confirm any charge:

1. **What period the fee covers.** Membership runs **January to December and is
   not prorated**. A purchase in late July may buy five months at the full annual
   price. Ask, or read the order terms, before confirming. If the fee does not
   extend into 2027, the honest question is whether the concurrency raise can
   wait until January. Only you can weigh that against the product schedule.
2. **The renewal setting.** If auto-renew is on by default, decide deliberately
   rather than discovering it next January.

If no payment step appears and the application simply submits for review, that is
normal for programs with an approval gate. Expect Autodesk to come back with an
order or invoice.

---

## Step 5: after activation, verify before you celebrate

Membership is only useful if it actually produces a support queue. Confirm:

1. Sign in to the ADN support portal and check you can **open an API support
   case**. If you cannot, membership is active but the entitlement is not, and
   that is a rep question.
2. Confirm the case tool lets you reference the APS app on the
   `leafautomation.ai` ID. This is the cross-account question from the top of
   this guide, now answerable for real.

Then file the raise: attach `docs/aps-concurrency-raise-request.md`, ask for 25
concurrent WorkItems with a staged 10-then-25 fallback, and cite the measured
load profile (2.4 to 4.1 engine-seconds per WorkItem, roughly $0.006 to $0.008
per run). Autodesk's guidance is explicit that arbitrary large numbers are
rejected, so lead with the measurements.

---

## Step 6: if you get stuck at any step

Contact the ADN representative for US and Canada directly:

- **Eswar K**, `eswar.k@autodesk.com`, +353 876901284
  (published at https://aps.autodesk.com/autodesk-developer-programs/adn/contact-adn)

The draft email in `docs/adn-membership-reinstate-email-draft.md` is written for
exactly this. Sending it **before** you start the application is also a
reasonable order of operations, since the rep can confirm the price, the coverage
period, and whether reinstatement beats a fresh application.

A free fallback that needs no membership: the **"Ask the APS Expert"** call, every
other Wednesday at 11 AM ET, where APS engineers can confirm whether a
concurrency change really must go through ADN.
Registration: https://autodesk.zoom.us/meeting/register/69qpfOuhSGKykxKzHBTeYg

---

## Observed flow (verified 2026-07-26, steps 1 and 2 only)

Driven in a real browser up to the sign-in wall. Nothing was typed, no account
was touched.

- `https://adndata.autodesk.io/sysinit.asp?SessionType=Applicant` **301-redirects
  to `https://adn.autodesk.io`**. The `sysinit.asp` path is legacy.
- **There is no pre-login application form.** The two "Click here to initiate or
  continue an ADN Membership Application" links and both "Sign In" buttons all
  resolve to the same target: `/user/login?state=/application`. Sign-in gates
  everything, so nothing can be filled in or previewed beforehand.
- Clicking the header "Sign In" in-page did not navigate (the page behaves as a
  single-page app). Going straight to
  `https://adn.autodesk.io/user/login?state=/application` worked.
- That URL redirects to **`https://signin.autodesk.com`**, titled "Sign in -
  Autodesk". The screen offers: an **Email** field with a **Next** button, and
  four alternatives, **Continue with Google / Apple / Microsoft / Facebook**.
  There is also "New to Autodesk? Create account" and a "Get help" link.
- **Pick the method the account was created with.** If
  `ehaug@leafsolardesign.com` is a Google Workspace address, "Continue with
  Google" is more likely to reach the existing record than the email path, and
  landing in the wrong place risks creating a duplicate identity.
- The `state=/application` parameter should carry you to the application
  immediately after authentication, rather than dropping you on the portal home.
- Chinese and Japanese variants exist as separate links
  (`/user/login?state=/application?lng=cn` and `?lng=jp`).

Stopped here deliberately. The next screen is the first one that needs the
operator.

## Quick reference

| Item | Value |
|---|---|
| Portal | https://adndata.autodesk.io/sysinit.asp?SessionType=Applicant (redirects to https://adn.autodesk.io) |
| Sign in as | ehaug@leafsolardesign.com |
| Seats | 1 |
| Published price, 1 user | 1,750 (currency varies by region) |
| Membership term | January to December, not prorated |
| Category | Paid, standard (start-up window already used) |
| Rep | Eswar K, eswar.k@autodesk.com, +353 876901284 |
| Answer sheet | docs/adn-membership-reinstate-email-draft.md |
| The request this unlocks | docs/aps-concurrency-raise-request.md |
