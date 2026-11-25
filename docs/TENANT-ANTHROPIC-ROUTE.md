# Per-tenant Anthropic-compatible model route (staging only)

Added by #3621 (R-H1). A tenant's authoring and chat turns can run on an Anthropic-compatible endpoint other
than api.anthropic.com, chosen per tenant by a provider record. Production carries no
`LEAF_TENANT_PROVIDER_HOSTS` and no provider records, so the route is inert there.

## Record

One JSON record per tenant in the harness grants directory, read by `FileTenantProviderStore`:

```json
{
  "provider": "anthropic_compatible",
  "base_url": "https://llm-mini.leafdesign.ai",
  "model": "inkling-2-mini",
  "api_key_env": "LEAF_PROVIDER_KEY_MINI",
  "label": "Mac mini (staging walk)",
  "turn_timeout_s": 300
}
```

- `base_url` is https, has no port and no `/v1`. The Claude CLI appends `/v1/messages`.
- The host must be listed in `LEAF_TENANT_PROVIDER_HOSTS`.
- `api_key_env` names an env var matching `^LEAF_PROVIDER_KEY_[A-Z0-9_]+$`. The value is read server-side and is
  never logged.

## Behaviour

- `TenantAnthropicGrantProvider` returns an `api_key` grant carrying `baseUrl` and `model`. The SDK child gets
  `ANTHROPIC_BASE_URL`, `ANTHROPIC_MODEL` and `ANTHROPIC_SMALL_FAST_MODEL` after the env scrub.
- A record that cannot be honoured (host not allowlisted, key missing) fails closed with
  `TenantProviderConfigError`. It never falls back to another credential.
- `envScrub.ts` removes `LEAF_PROVIDER_KEY_*` and other `*_KEY_*` provider credentials from every SDK child, so
  no tenant subprocess sees a provider key it was not granted.
- An `openai_compatible` record still routes to the OpenAI runner; an `anthropic_compatible` record does not.

## Rollback

Delete the tenant's record. The tenant returns to its own linked Claude account on the next turn.
