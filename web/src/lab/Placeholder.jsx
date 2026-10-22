export default function Placeholder({ title, owner, state }) {
  return (
    <section className="lab-placeholder" aria-label={title} data-state={state}>
      <h2>{title}</h2>
      <p>{owner}</p>
      <p role="status">{state === 'unavailable' ? 'Unavailable' : state}</p>
    </section>
  )
}
