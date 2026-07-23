# Public host redirect contract

The frozen contract is [`contract/PUBLIC-HOSTS.json`](../contract/PUBLIC-HOSTS.json).
It covers the HTTP entry points for the apex, `www`, and held `platform`
aliases. Each entry must return the exact recorded redirect status and
`Location`: one same-host hop from HTTP to HTTPS.

The probe rejects HTTP 200, multiple or missing `Location` headers, loops,
off-host targets, insecure targets, unexpected status or location values, and
responses over the body limit. It never follows the redirect. This contract
does not assert the application response behind the HTTPS target, so it does
not release or change the public platform alias hold.

Run the offline contract check, which is the default:

```text
python scripts/probe-public-hosts.py
```

Run the optional live, read-only check:

```text
python scripts/probe-public-hosts.py --live
```

Live mode returns nonzero when any host violates the contract. Both modes emit
bounded JSON with precise check names. No mode reads credentials or contains a
DNS, Cloudflare, Vercel, deployment, or alias mutation path.
