import { useEffect, useRef, useState } from 'react'
import SchemaForm, { defaultsOf } from '../components/SchemaForm.jsx'
import { solarFormKeys, solarView } from './solarView.js'
import { solarFlowPrefill, solarFlowStepId, PROJECT_EQUIPMENT_TOOL, deriveProjectEquipmentState,
  buildProjectEquipmentRequest, exactProjectEquipmentPreview, projectSolarFlowReason } from './solarFlowModel.js'
import { createSolarDrawingBinding, sameSolarDrawingBinding, solarDrawingBindingKey } from './solarDrawingBinding.js'
import SolarPresetForm from './SolarPresetForm.jsx'
import { PRESET_TOOL } from './solarPresetModel.js'
import returnToolFocus from './returnToolFocus.js'
import useEscapeOwner from '../lib/useEscapeOwner.js'

export default function SolarToolForm(props) {
  const { tool, onSubmit, onClose, presetListing, presetRevision, readIntake, drawingId = null, drawingVersion = null } = props
  if (Object.prototype.hasOwnProperty.call(props, 'solarBinding') || props.projectId != null) {
    return <ProjectSolarEquipmentForm {...props} />
  }
  if (tool?.name === PRESET_TOOL) {
    return <PresetSolarToolForm tool={tool} onSubmit={onSubmit} onClose={onClose}
      presetListing={presetListing} presetRevision={presetRevision} readIntake={readIntake}
      drawingId={drawingId} drawingVersion={drawingVersion} />
  }
  return <GenericSolarToolForm tool={tool} onSubmit={onSubmit} onClose={onClose}
    readIntake={readIntake} drawingId={drawingId} drawingVersion={drawingVersion} />
}

function ownsKey(record, key) {
  return record !== null && typeof record === 'object' && Object.prototype.hasOwnProperty.call(record, key)
}

const equipmentReasonCode = new RegExp('^[A-Za-z][A-Za-z0-9_]{0,127}$');
const equipmentJobId = new RegExp('^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$');
function equipmentFeedback(outcome, admission = null) {
  const code = [admission?.error?.reason_code, outcome?.envelope?.error?.reason_code, outcome?.reason].find((candidate) => typeof candidate === 'string' && equipmentReasonCode.test(candidate)) ?? null;
  return { outcome, admission, text: projectSolarFlowReason('preview_refused'),
    code: code };
}

export function ProjectSolarEquipmentForm(props) {
  const { solarBinding, authorityKey, projectClient, catalogDigest, newProjectIntentKey, onProjectSubmit, busy, onClose, tool, surface = 'tool' } = props;
  let binding = null;
  let bindingKey = null;
  try { binding = createSolarDrawingBinding(solarBinding); bindingKey = solarDrawingBindingKey(binding); } catch { /* local refusal */ }
  const latest = useRef(props);
  latest.current = props;
  const owner = useRef(null);
  const sequence = useRef(0);
  if (!owner.current || owner.current.bindingKey !== bindingKey || owner.current.authorityKey !== authorityKey
      || owner.current.client !== projectClient) {
    if (owner.current) {
      owner.current.alive = false;
      for (const controller of owner.current.controllers) controller.abort();
    }
    owner.current = { id: ++sequence.current, bindingKey, binding, authorityKey, client: projectClient, alive: true, generation: 0,
      controllers: new Set(), candidateControllers: new Set(), predicates: new Set(), candidate: null, armed: null, lock: null,
      digest: catalogDigest, formValid: true };
  }
  const scope = owner.current;
  if (scope.digest !== catalogDigest) {
    scope.generation += 1;
    scope.armed = null;
    scope.digest = catalogDigest;
    for (const controller of scope.candidateControllers) controller.abort();
  }
  const [loaded, setLoaded] = useState(null);
  const schema = { ...tool?.params, properties: Object.fromEntries(Object.entries(tool?.params?.properties || {})
    .filter(([key]) => ['equipment', 'assignments', 'cancel', 'preview'].includes(key))) };
  const [draft, setDraft] = useState(null);
  const values = draft?.scope === scope ? draft.values : defaultsOf(schema);
  const [formValid, setFormValid] = useState(true);
  const [publication, setPublication] = useState(null);
  const [feedback, setFeedback] = useState(null);
  const [refresh, setRefresh] = useState(0);
  const [submitting, setSubmitting] = useState(false);
  const supported = tool?.name === PROJECT_EQUIPMENT_TOOL;
  const dependencies = supported && binding && typeof authorityKey === 'string' && authorityKey.trim().length > 0
    && new TextEncoder().encode(authorityKey).byteLength <= 256
    && projectClient && ['readContext', 'prepareRun', 'catalog', 'captureCatalogScope', 'subscribe', 'getSnapshot']
      .every((key) => typeof projectClient[key] === 'function');
  const available = dependencies && typeof newProjectIntentKey === 'function' && typeof onProjectSubmit === 'function';
  const isScopeCurrent = () => {
    const current = latest.current;
    const snapshot = projectClient?.getSnapshot?.();
    return scope.alive && owner.current === scope && current.projectClient === scope.client
      && current.authorityKey === scope.authorityKey && sameSolarDrawingBinding(current.solarBinding, scope.binding)
      && sameSolarDrawingBinding(snapshot?.binding, scope.binding) && snapshot?.authorityKey === scope.authorityKey;
  };
  const operation = (candidate = true) => {
    const generation = scope.generation;
    const digest = catalogDigest;
    const predicate = projectClient.captureCatalogScope();
    const controller = new AbortController();
    scope.controllers.add(controller);
    if (candidate) scope.candidateControllers.add(controller);
    scope.predicates.add(predicate);
    return { signal: controller.signal, checkoutCurrent: predicate, isCurrent: () => isScopeCurrent() && !controller.signal.aborted && predicate()
      && (!candidate || (scope.generation === generation && latest.current.catalogDigest === digest)),
    abort: () => controller.abort(),
    finish: () => {
      scope.controllers.delete(controller);
      scope.candidateControllers.delete(controller);
      scope.predicates.delete(predicate);
    } };
  };

  useEffect(() => {
    scope.alive = true;
    setLoaded(null);
    setDraft(null);
    setFormValid(true);
    scope.formValid = true;
    setPublication(null);
    setFeedback(null);
    setSubmitting(false);
    if (!available) return () => { scope.alive = false; };
    const snapshot = projectClient.getSnapshot();
    if (!sameSolarDrawingBinding(snapshot?.binding, binding) || snapshot?.authorityKey !== authorityKey) {
      setLoaded({ scope, state: { mode: 'refused', reason: 'binding_invalid' } });
      return () => { scope.alive = false; };
    }
    // A scope change that aborts the context read before it settles starts the read again, so the form
    // never stays at "Reading the drawing." after a checkout rotation.
    let read = null;
    let contextSettled = false;
    const startRead = () => {
      const current = operation(false);
      read = current;
      (async () => {
        try {
          const outcome = await projectClient.readContext({ signal: current.signal });
          if (!current.isCurrent()) return;
          contextSettled = true;
          setLoaded({ scope, state: outcome.ok ? deriveProjectEquipmentState({ binding, contextView: outcome.data })
            : { mode: 'refused', reason: 'context_unreadable' } });
          if (!outcome.ok) setFeedback({ scope, generation: scope.generation, ...equipmentFeedback(outcome) });
        } catch {
          if (current.isCurrent()) {
            contextSettled = true;
            setLoaded({ scope, state: { mode: 'refused', reason: 'context_unreadable' } });
          }
        } finally { current.finish(); }
      })();
    };
    const unsubscribe = projectClient.subscribe(() => {
      if (!scope.alive || owner.current !== scope) return;
      if (!isScopeCurrent() || (scope.previewCurrent && !scope.previewCurrent())
          || (scope.armed && !scope.armed.isCurrent()) || [...scope.predicates].some((predicate) => !predicate())) {
        scope.armed = null;
        scope.generation += 1;
        for (const controller of scope.controllers) controller.abort();
        scope.predicates.clear();
        setPublication(null);
        setRefresh((value) => value + 1);
        scope.previewCurrent = null;
      }
      // Checked on every notification, not only on an invalidation: a scope that leaves and returns
      // before the context settles (A, B, A) cleared the predicates on the way out, so the return
      // alone would never restart the aborted read.
      if (!contextSettled && isScopeCurrent() && (!read || read.signal.aborted)) startRead();
    });
    startRead();
    return () => {
      scope.alive = false;
      scope.generation += 1;
      scope.armed = null;
      read.abort();
      for (const controller of scope.controllers) controller.abort();
      unsubscribe();
    };
  }, [scope, Boolean(available)]);

  const state = !supported ? { mode: 'refused', reason: 'tool_unsupported' } : !binding ? { mode: 'refused', reason: 'binding_invalid' }
    : !available ? { mode: 'refused', reason: 'submit_unavailable' }
      : loaded?.scope === scope ? loaded.state : null;
  const editable = state?.mode === 'edit';
  const result = editable && formValid && scope.formValid ? buildProjectEquipmentRequest({ binding, state, values,
    catalogDigest, idempotencyKey: 'candidate-validation' }) : null;
  const fingerprint = result?.ok ? JSON.stringify(result.request) : null;
  useEffect(() => {
    scope.armed = null;
    setPublication(null);
    if (!fingerprint || !available || !isScopeCurrent()) return undefined;
    const preview = operation();
    if (!preview.isCurrent()) { preview.finish(); return undefined; }
    let candidate = scope.candidate;
    try {
      if (!candidate || candidate.fingerprint !== fingerprint) {
        // Read the factory through the latest props: a new factory function alone is not a new candidate.
        const key = latest.current.newProjectIntentKey();
        const built = buildProjectEquipmentRequest({ binding, state, values,
          catalogDigest, idempotencyKey: key });
        if (!built.ok) {
          if (preview.isCurrent()) setPublication({ scope, generation: scope.generation, reason: built.reason });
          preview.finish();
          return undefined;
        }
        const intent = projectClient.prepareRun(built.request);
        candidate = { fingerprint, intent };
        scope.candidate = candidate;
      }
    } catch {
      if (preview.isCurrent()) setPublication({ scope, generation: scope.generation, reason: 'request_invalid' });
      preview.finish();
      return undefined;
    }
    scope.previewCurrent = preview.checkoutCurrent;
    (async () => {
      try {
        const outcome = await projectClient.catalog({ intents: [candidate.intent], signal: preview.signal });
        if (!preview.isCurrent()) return;
        const exact = exactProjectEquipmentPreview(outcome, catalogDigest);
        if (exact.accepted) {
          // Keep the predicate after settlement: checkout rotation invalidates an armed preview too.
          scope.armed = { candidate, generation: scope.generation, isCurrent: preview.isCurrent };
          setPublication({ scope, generation: scope.generation, reason: null });
          setFeedback(scope.retry?.candidate === candidate
            ? { scope, generation: scope.generation, ...scope.retry.feedback } : null);
        } else {
          setPublication({ scope, generation: scope.generation, reason: 'preview_refused' });
          setFeedback({ scope, generation: scope.generation, ...equipmentFeedback(outcome, exact.admission) });
        }
      } catch {
        if (preview.isCurrent()) {
          setPublication({ scope, generation: scope.generation, reason: 'preview_refused' });
          setFeedback({ scope, generation: scope.generation, ...equipmentFeedback(null) });
        }
      } finally { preview.finish(); }
    })();
    return () => preview.abort();
  }, [scope, fingerprint, available, refresh]);

  function change(setter, update) {
    scope.generation += 1;
    scope.armed = null;
    for (const controller of scope.candidateControllers) controller.abort();
    setPublication(null);
    setFeedback(null);
    setter(update);
    setRefresh((value) => value + 1);
  }
  const armed = scope.armed && scope.armed.generation === scope.generation && scope.armed.isCurrent()
    && scope.armed.candidate.fingerprint === fingerprint;
  const reason = state?.mode === 'refused' ? state.reason : !available ? 'submit_unavailable'
    : !isScopeCurrent() ? 'scope_stale' : busy || submitting ? 'run_in_progress' : !formValid || !scope.formValid ? 'invalid_value' : result && !result.ok ? result.reason
      : !armed ? (publication?.scope === scope && publication.generation === scope.generation
        ? publication.reason || 'preview_required' : 'preview_required') : null;
  const runMessage = feedback?.scope === scope && feedback.generation === scope.generation ? feedback : null;
  async function submit(event) {
    event?.preventDefault();
    if (reason || !scope.formValid || !armed || scope.lock || !isScopeCurrent() || !scope.armed?.isCurrent()
        || scope.armed.generation !== scope.generation || scope.armed.candidate.fingerprint !== fingerprint) return;
    const accepted = scope.armed;
    const pending = operation();
    const lock = {};
    scope.lock = lock;
    setSubmitting(true);
    try {
      const outcome = await projectClient.readContext({ signal: pending.signal });
      if (!pending.isCurrent() || scope.armed !== accepted) return;
      const checked = outcome.ok ? deriveProjectEquipmentState({ binding, contextView: outcome.data })
        : { mode: 'refused', reason: 'context_unreadable' };
      if (checked.mode === 'refused' || checked.mode !== state.mode || checked.intakeSha256 !== state.intakeSha256
          || checked.rev !== state.rev || !sameSolarDrawingBinding(checked.binding, state.binding)) {
        scope.armed = null;
        if (!outcome.ok) setFeedback({ scope, generation: scope.generation, ...equipmentFeedback(outcome) });
        setPublication({ scope, generation: scope.generation,
          reason: checked.reason === 'not_current_head' ? 'not_current_head' : 'scope_stale' });
        return;
      }
      if (!pending.isCurrent() || latest.current.busy || typeof latest.current.onProjectSubmit !== 'function') return;
      const result = await latest.current.onProjectSubmit(accepted.candidate.intent,
        { binding, authorityKey, signal: pending.signal, isCurrent: pending.isCurrent });
      if (!pending.isCurrent()) return;
      if (result?.ok && result.status === 202 && result.data?.status === 'submitted'
          && typeof result.data.job_id === 'string' && equipmentJobId.test(result.data.job_id)) {
        scope.retry = null;
        setFeedback({ scope, generation: scope.generation, outcome: result, text: 'Equipment request submitted.', code: null });
      } else {
        const message = equipmentFeedback(result);
        scope.retry = { candidate: accepted.candidate, feedback: message };
        setFeedback({ scope, generation: scope.generation, ...message });
      }
    } catch {
      if (pending.isCurrent()) {
        const message = equipmentFeedback(null);
        scope.retry = { candidate: accepted.candidate, feedback: message };
        setFeedback({ scope, generation: scope.generation, ...message });
      }
    } finally {
      if (scope.lock === lock) {
        scope.lock = null;
        if (isScopeCurrent()) setSubmitting(false);
      }
      pending.finish();
    }
  }
  function cancel() {
    onClose?.();
    if (surface === 'step') {
      const button = typeof document !== 'undefined' ? document.getElementById(solarFlowStepId(tool?.name)) : null;
      button?.focus();
    } else returnToolFocus(tool?.name);
  }
  const formRef = useRef(null);
  useEscapeOwner(surface === 'step' ? 'solar-step-editor' : 'solar-tool-form', true, cancel,
    { layer: 'edit', scope: formRef, scoped: true });
  function invalidate() {
    scope.generation += 1;
    scope.armed = null;
    for (const controller of scope.candidateControllers) controller.abort();
    setPublication(null);
    setFeedback(null);
  }
  function validity(valid) {
    if (scope.formValid !== valid) invalidate();
    scope.formValid = valid;
    setFormValid(valid);
  }
  const text = tool?.label || tool?.name || 'Equipment';
  return (
    <section ref={formRef} id={surface === 'step' ? 'solar-step-editor' : 'solar-tool-form'}
      className={surface === 'step' ? 'solar-step-editor tool-body' : 'solar-tool-form tool-body'}
      aria-label={`${text} parameters`} data-testid="project-solar-equipment-form" data-mode={state?.mode ?? 'loading'}>
      <h3>{text}</h3>
      {state === null && <p role="status">Reading the drawing.</p>}
      {editable && <p>Graph revision {state.rev}</p>}
      {reason && <p role="status" data-testid="project-solar-equipment-reason">{projectSolarFlowReason(reason)}</p>}
      {runMessage && <p role="status" data-testid="project-solar-equipment-feedback">
        {runMessage.text}{runMessage.code ? ` (${runMessage.code})` : ''}
      </p>}
      {editable && <>
        <div onChangeCapture={invalidate}>
          <SchemaForm key={scope.id} schema={schema} values={values}
            onChange={(next) => change(setDraft, { scope, values: next })} onValidityChange={validity} />
        </div>
        <button type="button" className="chip-act" disabled={Boolean(reason)} onClick={submit}>Review &amp; run</button>
        {runMessage && runMessage.outcome?.ok !== true && <button type="button" className="chip-act"
          disabled={Boolean(reason)} onClick={submit}>Retry</button>}
        <button type="button" className="chip-act" disabled={submitting} onClick={() => {
          invalidate();
          setRefresh((value) => value + 1);
        }}>Refresh preview</button>
      </>}
      <button type="button" className="chip-act" onClick={cancel}>Cancel</button>
    </section>
  );
}

function PresetSolarToolForm({ tool, onSubmit, onClose, presetListing, presetRevision, readIntake, drawingId, drawingVersion }) {
  const binding = useRef(null)
  const counter = useRef(0)
  const [readState, setReadState] = useState(null)
  if (!binding.current || binding.current.tool !== tool || binding.current.drawingId !== drawingId
    || binding.current.drawingVersion !== drawingVersion || binding.current.readIntake !== readIntake) {
    binding.current = { tool, drawingId, drawingVersion, readIntake, key: `${drawingId}:${drawingVersion}:${++counter.current}` }
  }
  const bound = binding.current
  const matches = readState?.key === bound.key
  const reading = typeof readIntake === 'function' && (!matches || readState.pending)
  const autoRevision = typeof readIntake === 'function' && matches ? readState.autoRevision : null

  useEffect(() => {
    if (typeof readIntake !== 'function') return undefined
    let current = true
    setReadState({ key: bound.key, pending: true, autoRevision: null })
    Promise.resolve()
      .then(() => readIntake(drawingId, drawingVersion))
      .then((result) => {
        if (!current || binding.current !== bound || result === null || typeof result !== 'object'
          || ![Object.prototype, null].includes(Object.getPrototypeOf(result)) || result.version !== drawingVersion) return
        const prefill = solarFlowPrefill(tool, result.intake?.solar_design_graph?.rev)
        if (!ownsKey(prefill, 'expected_rev')) return
        setReadState({ key: bound.key, pending: true,
          autoRevision: Object.freeze({ key: bound.key, value: prefill.expected_rev }) })
      })
      .catch(() => {})
      .finally(() => {
        if (current && binding.current === bound) setReadState((previous) => ({ ...previous, pending: false }))
      })
    return () => { current = false }
  }, [tool, drawingId, drawingVersion, readIntake])

  return <SolarPresetForm tool={tool} onSubmit={onSubmit} onClose={onClose}
    listing={presetListing} revision={presetRevision} autoRevision={autoRevision} reading={reading} />
}

// expected_rev starts as the drawing's own graph revision, read from the intake
// of the version this form was opened on (the step editor's rule, solarFlowPrefill),
// and only while the drafter has not typed one. No loader, no prefill: the form is
// then exactly the schema defaults it always was.
function GenericSolarToolForm({ tool, onSubmit, onClose, readIntake, drawingId, drawingVersion }) {
  const { view } = solarView(tool)
  const schema = {
    ...tool.params,
    properties: Object.fromEntries(solarFormKeys(tool, view).map((key) => [key, tool.params.properties[key]])),
  }
  const [values, setValues] = useState(() => defaultsOf(schema))
  const [formValid, setFormValid] = useState(true)
  const revTouched = useRef(false)
  const revAutomatic = useRef(false)
  const revisionKey = useRef({ tool, drawingId, drawingVersion })
  const [readState, setReadState] = useState(null)
  const text = tool.label || tool.name
  const takesRev = ownsKey(schema.properties, 'expected_rev') && ownsKey(solarFlowPrefill(tool, 0), 'expected_rev')
  const reading = takesRev && typeof readIntake === 'function' && (!readState
    || readState.tool !== tool || readState.drawingId !== drawingId || readState.drawingVersion !== drawingVersion
    || readState.readIntake !== readIntake || readState.pending)

  useEffect(() => {
    const previousKey = revisionKey.current
    if (previousKey.tool !== tool || previousKey.drawingId !== drawingId || previousKey.drawingVersion !== drawingVersion) {
      if (revAutomatic.current) {
        const defaultRev = defaultsOf(schema).expected_rev
        setValues((previous) => {
          const next = { ...previous }
          if (defaultRev === undefined) delete next.expected_rev
          else next.expected_rev = defaultRev
          return next
        })
        revAutomatic.current = false
      }
      revisionKey.current = { tool, drawingId, drawingVersion }
    }
    if (!takesRev || typeof readIntake !== 'function') return undefined
    let current = true
    const key = { tool, drawingId, drawingVersion, readIntake }
    setReadState({ ...key, pending: true })
    Promise.resolve()
      .then(() => readIntake(drawingId, drawingVersion))
      .then((intake) => {
        if (intake === null || typeof intake !== 'object'
          || ![Object.prototype, null].includes(Object.getPrototypeOf(intake)) || intake.version !== drawingVersion) return
        const prefill = solarFlowPrefill(tool, intake.intake?.solar_design_graph?.rev)
        if (!current || revTouched.current || !ownsKey(prefill, 'expected_rev')) return
        revAutomatic.current = true
        setValues((previous) => ({ ...previous, expected_rev: prefill.expected_rev }))
      })
      .catch(() => {})
      .finally(() => { if (current) setReadState({ ...key, pending: false }) })
    return () => { current = false }
  }, [tool, takesRev, readIntake, drawingId, drawingVersion])

  function change(next, { changedKey } = {}) {
    if (changedKey === 'expected_rev') {
      revTouched.current = true
      revAutomatic.current = false
    }
    setValues(next)
  }

  function cancel() {
    onClose()
    returnToolFocus(tool.name)
  }

  // S27: Escape from inside the form cancels it through the one owner stack
  // (edit layer, scoped to the form as the old section handler was).
  const formRef = useRef(null)
  useEscapeOwner('solar-tool-form', true, cancel, { layer: 'edit', scope: formRef, scoped: true })

  return (
    <section
      ref={formRef}
      id="solar-tool-form"
      className="solar-tool-form tool-body"
      aria-label={`${text} parameters`}
    >
      <h3>{text}</h3>
      <SchemaForm schema={schema} values={values} onChange={change} onValidityChange={setFormValid} />
      <button type="button" className="chip-act" disabled={reading || !formValid}
        title={reading ? "Reading this drawing's design revision." : undefined}
        onClick={() => { if (!reading && formValid) { const armed = onSubmit(tool, values); if (armed?.runIntent?.intentId) onClose() } }}>
        Review & run
      </button>
      <button type="button" className="chip-act" onClick={cancel}>Cancel</button>
    </section>
  )
}
