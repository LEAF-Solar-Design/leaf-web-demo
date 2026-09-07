import { useCallback, useEffect, useRef, useState } from 'react'
import * as api from './api.js'

const empty = () => ({ status: 'idle', refreshing: false, error: null, errorAction: null,
  campaigns: [], selectedId: null, selected: null, questions: [], answers: {}, pending: {},
  execution: null, executionLoading: false, executionError: null, completion: null,
  enrollments: [], allowedMachines: [], enrollmentError: null,
  capabilities: [], capabilityError: null, submissions: {}, invocationResults: {}, recoveryUnavailable: false })
const newKey = () => globalThis.crypto?.randomUUID?.() || `k-${Date.now()}-${Math.random().toString(16).slice(2)}`
const submissionScope = (project, campaign, enrollment) => `leaf.campaign.invocation:${project}:${campaign}:${enrollment}`

export default function useCampaigns(projectId, { enabled = true } = {}) {
  const scope = `${projectId || ''}:${enabled}`
  const [snapshot, setSnapshot] = useState(() => ({ scope, ...empty() }))
  const contextRef = useRef(null)
  const generationRef = useRef(0)
  const draftKeyRef = useRef(null)
  const releaseKeysRef = useRef(new Map())
  const questionKeyRef = useRef(null)
  const submissionsRef = useRef(new Map())
  const resultsRef = useRef(new Map())
  const storageUnavailableRef = useRef(false)
  const readSubmission = useCallback(key => {
    if (submissionsRef.current.has(key)) return submissionsRef.current.get(key)
    try {
      const raw = sessionStorage.getItem(key)
      if (!raw) return null
      const value = JSON.parse(raw)
      if (!value?.idempotencyKey || !value?.effectiveCatalogDigest) throw new Error('Invalid pending submission')
      submissionsRef.current.set(key, value)
      return value
    } catch {
      storageUnavailableRef.current = true
      return null
    }
  }, [])
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

  const load = useCallback(async ({ refresh = true, preferredId = context.selectedId, onChoicesError } = {}) => {
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
      let completion = null
      let questions = []
      let enrollment = { enrollments: [], allowed_machines: [] }
      let enrollmentError = null
      let capabilities = []
      let capabilityError = null
      if (selectedId) {
        const [detail, rows, hosts, tools] = await Promise.all([
          api.getCampaign(projectId, selectedId), api.listQuestions(projectId, selectedId),
          api.listEnrollments(projectId, selectedId).catch(error => { enrollmentError = error; return null }),
          api.listCapabilities(projectId, selectedId).catch(error => { capabilityError = error; return null }),
        ])
        if (!live()) return null
        if (enrollmentError || capabilityError) onChoicesError?.(enrollmentError || capabilityError)
        selected = detail.campaign
        completion = detail.completion !== undefined ? detail.completion
          : detail.campaign?.completion !== undefined ? detail.campaign.completion
            : context.selectedId === selectedId ? context.completion ?? null : null
        questions = rows.questions || []
        if (hosts) enrollment = hosts.enrollment
        capabilities = tools?.capabilities || []
      }
      const answers = {}
      for (const question of questions) {
        if (question.answer != null) answers[question.question_id] = question.answer
      }
      const changedSelection = context.selectedId !== selectedId
      context.selectedId = selectedId
      const submissions = {}
      const invocationResults = {}
      for (const row of enrollment.enrollments) {
        const key = submissionScope(projectId, selectedId, row.enrollment_id)
        const pending = readSubmission(key)
        if (pending) submissions[row.enrollment_id] = pending
        const result = resultsRef.current.get(key)
        if (result) invocationResults[row.enrollment_id] = result
      }
      context.capabilities = capabilities
      context.enrollments = enrollment.enrollments
      context.completion = completion
      update({ campaigns, selectedId, selected, completion, questions, answers, status: 'ready', refreshing: false,
        capabilities, capabilityError, submissions, invocationResults, recoveryUnavailable: storageUnavailableRef.current,
        enrollments: enrollment.enrollments, allowedMachines: enrollment.allowed_machines, enrollmentError,
        executionLoading: !!selectedId, ...(changedSelection || !selectedId ? { execution: null, executionError: null } : {}) })
      if (selectedId) {
        try {
          const result = await api.getExecution(projectId, selectedId)
          if (!live()) return null
          completion = result.completion !== undefined ? result.completion
            : result.execution?.completion !== undefined ? result.execution.completion : completion
          context.completion = completion
          update({ execution: result.execution, completion, executionLoading: false, executionError: null })
        } catch (executionError) {
          if (!live()) return null
          update({ executionError, executionLoading: false })
        }
      }
      return { campaigns, selected, questions }
    } catch (error) {
      if (live()) onChoicesError?.(error)
      if (live()) update({ error, errorAction: 'load', refreshing: false, executionLoading: false, ...(!refresh ? { status: 'error' } : {}) })
      return null
    }
  }, [context, current, enabled, projectId, readSubmission, update])

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
    context.capabilities = []
    context.enrollments = []
    context.completion = null
    questionKeyRef.current = null
    update({ selectedId: id, selected: null, questions: [], answers: {}, pending: {}, error: null, errorAction: null,
      enrollments: [], allowedMachines: [], enrollmentError: null,
      capabilities: [], capabilityError: null, submissions: {}, invocationResults: {}, recoveryUnavailable: false,
      execution: null, executionLoading: false, executionError: null, completion: null })
    return load({ preferredId: id })
  }, [context, current, load, update])

  const mutate = useCallback(async (action, operation, preferredId) => {
    if (!enabled || !projectId || !current() || context.locks[action]) return null
    const view = context.view
    const selectedId = context.selectedId
    const locks = context.locks
    locks[action] = true
    update({ pending: { ...locks }, error: null, errorAction: null })
    try {
      const result = await operation()
      if (!current(view) || context.selectedId !== selectedId) return null
      if (action !== 'submit' && result?.completion !== undefined) {
        context.completion = result.completion
        update({ completion: result.completion })
      }
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
      if (current(view) && context.selectedId === selectedId) setSnapshot(previous => error?.status === 409 && error?.code === 'catalog_drift'
        && previous.errorAction === 'load' ? previous : { ...previous, error, errorAction: action })
      throw error
    } finally {
      delete locks[action]
      if (current(view)) update({ pending: { ...locks } })
    }
  }, [context, current, enabled, load, projectId, update])

  const submit = useCallback(({ title, prompt, mode, finish }) => {
    const fingerprint = JSON.stringify([projectId, title, prompt, mode, finish])
    if (draftKeyRef.current?.fingerprint !== fingerprint) draftKeyRef.current = { fingerprint, key: newKey() }
    const idempotencyKey = draftKeyRef.current.key
    return mutate('submit', () => api.submitCampaign({ projectId, title, prompt, idempotencyKey,
      ...(mode === undefined ? {} : { mode, finish }) }), result => result.campaign.campaign_id)
  }, [mutate, projectId])
  const createRelease = useCallback(finish => {
    const id = context.selectedId
    if (!id) return Promise.resolve(null)
    const signature = JSON.stringify([projectId, id, 'finish', finish])
    if (!releaseKeysRef.current.has(signature)) releaseKeysRef.current.set(signature, newKey())
    const idempotencyKey = releaseKeysRef.current.get(signature)
    return mutate('release', async () => {
      const result = await api.createRelease(projectId, id, { finish, idempotencyKey })
      releaseKeysRef.current.delete(signature)
      return result
    })
  }, [context, mutate, projectId])
  const transitionRelease = useCallback(action => {
    const id = context.selectedId
    const releaseId = context.completion?.release?.release_id
    return id && releaseId ? mutate('release', () => api.transitionRelease(projectId, id, releaseId, action)) : Promise.resolve(null)
  }, [context, mutate, projectId])
  const retryReleaseStage = useCallback(stage => {
    const id = context.selectedId
    const releaseId = context.completion?.release?.release_id
    return id && releaseId ? mutate('release', () => api.retryReleaseStage(projectId, id, releaseId, stage)) : Promise.resolve(null)
  }, [context, mutate, projectId])
  const downloadReleaseArtifact = useCallback(async artifact => {
    const id = context.selectedId
    const releaseId = context.completion?.release?.release_id
    if (!id || !releaseId || !enabled || !current() || context.locks.download) return null
    const view = context.view
    const generation = generationRef.current
    const locks = context.locks
    const live = () => current(view) && generationRef.current === generation && context.selectedId === id
      && context.completion?.release?.release_id === releaseId
    locks.download = true
    update({ pending: { ...locks } })
    try {
      // Recheck current delivery state before accessing previously accepted bytes.
      const latest = await api.getRelease(projectId, id, releaseId)
      if (!live()) return null
      const completion = latest?.completion
      if (!completion || completion.release?.release_id !== releaseId) throw new Error('Current release verification is unavailable. Reload the release.')
      context.completion = completion
      update({ completion })
      const checks = completion.release.contract?.required_checks || []
      const coverage = completion.coverage || []
      if (['failed', 'unavailable'].includes(completion.current_verification?.status)
          || completion.release.status !== 'finished' || !checks.length
          || checks.some(check => !coverage.some(row => row.check_id === check.check_id && row.status === 'passed'))) {
        throw new Error('Current release verification does not permit this download. Reload the release.')
      }
      const currentArtifact = completion.deliverables?.find(row => row.name === artifact.name
        && row.sha256 === artifact.sha256 && row.byte_count === artifact.byte_count)
      if (!currentArtifact) throw new Error('This output changed. Reload the release before downloading.')
      const result = await api.downloadReleaseArtifact(projectId, id, releaseId, currentArtifact)
      return live() ? result : null
    } catch (error) {
      if (!live()) return null
      throw error
    } finally {
      delete locks.download
      if (current(view)) update({ pending: { ...locks } })
    }
  }, [context, current, enabled, projectId, update])
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
  const enroll = useCallback((machine, capability) => {
    const id = context.selectedId
    return id ? mutate('enroll', () => capability === undefined
      ? api.requestEnrollment(projectId, id, machine)
      : api.requestEnrollment(projectId, id, machine, capability)) : Promise.resolve(null)
  }, [context, mutate, projectId])
  const enableEnrollment = useCallback(enrollmentId => {
    const id = context.selectedId
    return id ? mutate(`enrollment:${enrollmentId}`, () => api.enableEnrollment(projectId, id, enrollmentId)) : Promise.resolve(null)
  }, [context, mutate, projectId])
  const revokeEnrollment = useCallback(enrollmentId => {
    const id = context.selectedId
    return id ? mutate(`enrollment:${enrollmentId}`, () => api.revokeEnrollment(projectId, id, enrollmentId)) : Promise.resolve(null)
  }, [context, mutate, projectId])
  const bindPublication = useCallback((enrollmentId, changeSetId) => {
    const id = context.selectedId
    if (!id || !context.capabilities?.some(tool => tool.change_set_id === changeSetId)) return Promise.resolve(null)
    return mutate(`enrollment:${enrollmentId}`, () => api.bindPublication(projectId, id, enrollmentId, changeSetId))
  }, [context, mutate, projectId])
  const invokeCapability = useCallback(enrollmentId => {
    const id = context.selectedId
    if (!id || !enabled || !current()) return Promise.resolve(null)
    const view = context.view
    const key = submissionScope(projectId, id, enrollmentId)
    return mutate(`enrollment:${enrollmentId}`, async () => {
      let submission = readSubmission(key)
      if (!submission) {
        const row = context.enrollments?.find(item => item.enrollment_id === enrollmentId)
        const digest = row?.capability_link?.effective_catalog_digest
        if (row?.state !== 'enabled' || !digest) throw new Error('Reload the published capability before use.')
        submission = { idempotencyKey: newKey(), effectiveCatalogDigest: digest }
        submissionsRef.current.set(key, submission)
        try { sessionStorage.setItem(key, JSON.stringify(submission)) }
        catch { storageUnavailableRef.current = true }
      }
      setSnapshot(previous => ({ ...previous, submissions: { ...previous.submissions, [enrollmentId]: submission },
        recoveryUnavailable: storageUnavailableRef.current }))
      let result
      try {
        result = await api.invokeCapability(projectId, id, enrollmentId, submission)
      } catch (error) {
        if (error?.status !== 409 || error?.code !== 'catalog_drift') throw error
        const matches = value => value?.idempotencyKey === submission.idempotencyKey
          && value?.effectiveCatalogDigest === submission.effectiveCatalogDigest
        if (matches(submissionsRef.current.get(key))) {
          submissionsRef.current.set(key, null)
          try {
            const stored = sessionStorage.getItem(key)
            if (stored && matches(JSON.parse(stored))) sessionStorage.removeItem(key)
          } catch { storageUnavailableRef.current = true }
          if (current(view)) setSnapshot(previous => {
            const submissions = { ...previous.submissions }
            if (matches(submissions[enrollmentId])) delete submissions[enrollmentId]
            return { ...previous, submissions, recoveryUnavailable: storageUnavailableRef.current }
          })
        }
        let refreshError = null
        if (current(view)) await load({ preferredId: id, onChoicesError: failure => { refreshError = failure } })
        const message = 'The published tool changed. No job was submitted. Review its publication binding before using it again.'
          + (refreshError ? ' Current choices could not be refreshed.' : '')
        throw Object.assign(new Error(message), error, { message })
      }
      if (!/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(result?.invocation?.job_id || '')) {
        throw new Error('Submission outcome unknown. Recover submission to retrieve its job.')
      }
      resultsRef.current.set(key, result.invocation)
      submissionsRef.current.set(key, null)
      try { sessionStorage.removeItem(key) } catch { storageUnavailableRef.current = true }
      if (current(view)) setSnapshot(previous => {
        const submissions = { ...previous.submissions }
        delete submissions[enrollmentId]
        return { ...previous, submissions, invocationResults: { ...previous.invocationResults, [enrollmentId]: result.invocation },
          recoveryUnavailable: storageUnavailableRef.current }
      })
      return result
    })
  }, [context, current, enabled, load, mutate, projectId, readSubmission])
  return { ...(snapshot.scope === scope ? snapshot : empty()), select, submit, ask, answer, refetch,
    enroll, enableEnrollment, revokeEnrollment, bindPublication, invokeCapability,
    createRelease, transitionRelease, retryReleaseStage, downloadReleaseArtifact }
}
