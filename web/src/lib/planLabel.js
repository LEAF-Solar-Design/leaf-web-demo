const PLAN_LABELS = {
  hosted_free: 'Free',
  hosted_starter: 'Starter',
  hosted_pro: 'Pro',
  self_hosted: 'Self-hosted',
  demo: 'Demo',
  demo_live: 'Demo',
  admin: 'Admin',
  restricted: 'Restricted',
  guest: 'Guest',
}

export function planLabel(tier) {
  if (tier == null || tier === '') return ''
  return Object.hasOwn(PLAN_LABELS, tier) ? PLAN_LABELS[tier] : tier
}
