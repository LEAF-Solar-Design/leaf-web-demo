import { useCallback, useEffect, useRef, useState } from 'react'
import * as api from './api.js'

const empty = () => ({ status: 'idle', refreshing: false, error: null, errorAction: null,
  campaigns: [], selectedId: null, selected: null, questions: [], answers: {}, pending: {} })
const newKey = () => globalThis.crypto?.randomUUID?.() || `k-${Date.now()}-${Math.random().toString(16).slice(2)}`

export default function useCampaigns(projectId, { enabled = true } = {}) {
  const scope = `${projectId || ''}:${enabled}`
  const [snapshot, setSnapshot] = useState(() => ({ scope, ...empty() }))
  const contextRef = useRef(null)
  const generationRef = useRef(0)
  const draftKeyRef = useRef(null)
  const questionKeyRef = useRef(null)
  // Invalidate during render as well as cleanup, so no frame exposes the old project.
  if (contextRef.current?.scope !== scope) {
    contextRef.current = { scope, active: true, view: 0, selectedId: null, locks: {} }
    generationRef.current += 1
    draftKeyRef.current = null
    questionKeyRef.current = null
  }
  const context = contextRef.current
  const current = useCallback((view) => contextRef.current === context && context.active
    && (view === undefined || context.view === view), [context])
  const update = useCallback((patch) => {
    if (current()) setSnapshot(previous => ({ ...(previous.scope === scope ? previous : empty()), scope, ...patch }))
  }, [current, scope])

  const load = useCallback(async ({ refresh = true, preferredId = context.selectedId } = {}) => {
    if (!enabled || !projectId || !current()) return null
    const generation = ++generationRef.current
    const view = context.view
    const live = () => current(view) && generationRef.current === generation
    update({ ...(refresh ? { refreshing: true } : { status: 'loading' }), error: null, errorAction: null })
    try {
      const list = await api.listCampaigns(projectId)
      if (!live()) return null
      const campaigns = list.campaigns || []
      const selectedId = campaigns.some(row => row.campaign_id === preferredId)
        ? preferredId : campaigns[0]?.campaign_id || null
      let selected = null
      let questions = []
      if (selectedId) {
        const [detail, rows] = await Promise.all([
          api.getCampaign(projectId, selectedId), api.listQuestions(projectId, selectedId),
        ])
        if (!live()) return null
        selected = detail.campaign
        questions = rows.questions || []
      }
      const answers = {}
      for (const question of questions) {
        if (question.answer != null) answers[question.question_id] = question.answer
      }
      context.selectedId = selectedId
      update({ campaigns, selectedId, selected, questions, answers, status: 'ready', refreshing: false })
      return { campaigns, selected, questions }
    } catch (error) {
      if (live()) update({ error, errorAction: 'load', refreshing: false, ...(!refresh ? { status: 'error' } : {}) })
      return null
    }
  }, [context, current, enabled, projectId, update])

  useEffect(() => {
    context.active = true
    update(empty())
    load({ refresh: false })
    return () => { context.active = false; generationRef.current += 1 }
  }, [context, load, update])

  const select = useCallback((id) => {
    if (!current() || context.selectedId === id) return
    context.view += 1
    context.selectedId = id
    context.locks = {}
    questionKeyRef.current = null
    update({ selectedId: id, selected: null, questions: [], answers: {}, pending: {}, error: null, errorAction: null })
    return load({ preferredId: id })
  }, [context, current, load, update])

  const mutate = useCallback(async (action, operation, preferredId) => {
    if (!enabled || !projectId || !current() || context.locks[action]) return null
    const view = context.view
    const locks = context.locks
    locks[action] = true
    update({ pending: { ...locks }, error: null, errorAction: null })
    try {
      const result = await operation()
      if (!current(view)) return null
      if (action === 'submit') draftKeyRef.current = null
      if (action === 'ask') questionKeyRef.current = null
      // An answer receipt is server truth, never the operator's draft.
      if (action.startsWith('answer:') && result.answer != null) {
        const qid = action.slice(7)
        setSnapshot(previous => ({ ...previous, answers: { ...previous.answers, [qid]: result.answer } }))
      }
      await load({ preferredId: preferredId?.(result) ?? context.selectedId })
      return current(view) ? result : null
    } catch (error) {
      if (current(view)) update({ error, errorAction: action })
      throw error
    } finally {
      delete locks[action]
      if (current(view)) update({ pending: { ...locks } })
    }
  }, [context, current, enabled, load, projectId, update])

  const submit = useCallback(({ title, prompt }) => {
    const fingerprint = `${projectId}\n${title}\n${prompt}`
    if (draftKeyRef.current?.fingerprint !== fingerprint) draftKeyRef.current = { fingerprint, key: newKey() }
    const idempotencyKey = draftKeyRef.current.key
    return mutate('submit', () => api.submitCampaign({ projectId, title, prompt, idempotencyKey }), result => result.campaign.campaign_id)
  }, [mutate, projectId])
  const ask = useCallback(({ prompt }) => {
    const id = context.selectedId
    if (!id) return Promise.resolve(null)
    const fingerprint = `${projectId}\n${id}\n${prompt}`
    if (questionKeyRef.current?.fingerprint !== fingerprint) questionKeyRef.current = { fingerprint, key: newKey() }
    const questionKey = questionKeyRef.current.key
    return mutate('ask', () => api.askQuestion(projectId, id, { questionKey, prompt }))
  }, [context, mutate, projectId])
  const answer = useCallback((qid, text) => {
    const id = context.selectedId
    if (!id) return Promise.resolve(null)
    return mutate(`answer:${qid}`, () => api.answerQuestion(projectId, id, qid, text))
  }, [context, mutate, projectId])
  const refetch = useCallback(() => load(), [load])
  return { ...(snapshot.scope === scope ? snapshot : empty()), select, submit, ask, answer, refetch }
}
