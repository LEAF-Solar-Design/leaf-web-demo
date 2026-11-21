import { useEffect, useRef, useState } from 'react'
import { sendAnswers } from '../../converse.js'
import { track } from '../../telemetry.js'
import './inlineAuthoring.css'

function Question({ question, answer, onChange, disabled }) {
  const [focusAt, setFocusAt] = useState(0)
  const options = [...question.options, { label: 'Other' }]
  const isOther = answer?.other !== undefined
  const choose = (index) => {
    if (index === options.length - 1) {
      onChange({ other: answer?.other ?? '' })
      return
    }
    const { label } = options[index]
    const labels = answer?.labels || []
    onChange({ labels: !question.multiSelect ? [label] : labels.includes(label)
      ? labels.filter((choice) => choice !== label) : [...labels, label] })
  }
  const move = (event) => {
    if (event.key === 'Enter') {
      event.preventDefault()
      event.target.click()
      return
    }
    const offset = { ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1 }[event.key]
    if (!offset) return
    event.preventDefault()
    const buttons = [...event.currentTarget.querySelectorAll('button')]
    buttons[(buttons.indexOf(event.target) + offset + buttons.length) % buttons.length].focus()
  }
  return <fieldset disabled={disabled}>
    <legend><span className="inline-question-header">{question.header}</span> {question.question}</legend>
    <div className="inline-choices" onKeyDown={move}>
      {options.map((option, index) => <button type="button" key={index}
        aria-pressed={index === options.length - 1 ? isOther : !!answer?.labels?.includes(option.label)}
        tabIndex={index === focusAt ? 0 : -1} onFocus={() => setFocusAt(index)} onClick={() => choose(index)}>
        {option.label}{option.description && <small>{option.description}</small>}
      </button>)}
    </div>
    {isOther && <label>Other answer for {question.header}
      <input type="text" maxLength={500} value={answer.other} onChange={(e) => onChange({ other: e.target.value })} />
    </label>}
  </fieldset>
}

export default function QuestionsCard({ sessionId, questionId, questions, closed, answers, disabled, onAnswered }) {
  const [draft, setDraft] = useState({})
  const [sent, setSent] = useState(null)
  const [sending, setSending] = useState(false)
  const [error, setError] = useState('')
  const shown = useRef(false)
  const count = questions.length
  useEffect(() => {
    if (!shown.current) track('plan.questions_shown', { count })
    shown.current = true
  }, [count])
  const recorded = answers || sent
  const complete = questions.every(({ id }) => draft[id]?.labels?.length || draft[id]?.other?.trim())
  const submit = async () => {
    if (closed || recorded || sending || disabled || !complete) return
    const payload = Object.fromEntries(questions.map(({ id }) => [id, draft[id].other !== undefined
      ? { other: draft[id].other.trim() } : draft[id]]))
    setSending(true)
    setError('')
    try {
      const result = await sendAnswers(sessionId, questionId, payload)
      setSent(payload)
      if (result.turn_id) onAnswered?.(result.turn_id)
      track('plan.questions_answered', { count, other_used: Object.values(payload).some((a) => 'other' in a) })
    } catch (e) {
      setError(e.secretRefused ? e.message : 'Could not send your answers. Try again.')
    } finally {
      setSending(false)
    }
  }
  return <section className="inline-authoring" aria-label="Planning questions" data-converse-role="questions">
    {questions.map((question) => closed || recorded
      ? <div key={question.id}><span className="inline-question-header">{question.header}</span> {question.question}
        <p>{recorded?.[question.id]?.other || recorded?.[question.id]?.labels?.join(', ') || 'No answer recorded'}</p></div>
      : <Question key={question.id} question={question} answer={draft[question.id]} disabled={sending || disabled}
        onChange={(value) => setDraft((prev) => ({ ...prev, [question.id]: value }))} />)}
    {!closed && !recorded && <button type="button" className="chip-act" disabled={!complete || sending || disabled}
      onClick={submit}>{sending ? 'Sending…' : 'Submit'}</button>}
    {error && !closed && <p role="alert">{error}</p>}
  </section>
}
