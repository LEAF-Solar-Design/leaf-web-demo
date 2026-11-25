export default function PlanToggle({ pressed, onToggle }) {
  return (
    <button
      type="button"
      aria-pressed={pressed}
      aria-keyshortcuts="Shift+Tab"
      title="Plan first (Shift+Tab in the prompt)"
      onClick={onToggle}
      style={{
        border: '1px solid var(--well-accent)',
        borderRadius: 999,
        padding: '2px 10px',
        fontFamily: 'var(--font-mono)',
        fontSize: 'var(--leaf-size-caption)',
        cursor: 'pointer',
        color: pressed ? 'var(--leaf-on-accent)' : 'var(--well-text)',
        background: pressed ? 'var(--well-accent)' : 'transparent',
      }}
    >Plan</button>
  )
}
