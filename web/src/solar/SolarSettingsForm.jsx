import React, { useEffect, useRef, useState } from 'react';
import {
  SETTINGS_FIELDS, DRAWING_UNITS, SOLAR_SETTINGS_REASONS, L2_COLLECTORS_NOTE,
  parseField, deriveFormState, buildSettingsParams, pyStrip,
  PROJECT_SETTINGS_REASONS, deriveProjectSettingsState, buildProjectSettingsRequest,
} from './solarSettingsModel.js';
import { createSolarDrawingBinding, sameSolarDrawingBinding, solarDrawingBindingKey } from './solarDrawingBinding.js';

const projectReasonCode = new RegExp('^[A-Za-z][A-Za-z0-9_]{0,127}$');
const projectJobId = new RegExp('^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$');
function projectFeedback(outcome, admission = null) {
  const code = admission?.error?.reason_code ?? outcome?.envelope?.error?.reason_code ?? outcome?.reason;
  return { outcome, admission, text: PROJECT_SETTINGS_REASONS.preview_refused,
    code: typeof code === 'string' && projectReasonCode.test(code) ? code : null };
}

function exactSettingsPreview(outcome, digest) {
  if (!outcome?.ok || !Array.isArray(outcome.data?.families)) return { accepted: false, admission: null };
  const rows = outcome.data.families.flatMap((family) => Array.isArray(family?.capabilities) ? family.capabilities : [])
    .filter((row) => row?.name === 'solar-settings');
  const accepted = (row) => {
    const view = row.availability;
    return row.catalog_digest === digest && view?.admission_checked === true
      && view.admission?.status_code === 200 && view.admission.error === null
      && ['entitled', 'engine_ready', 'implemented', 'input_ready', 'runnable'].every((key) => view[key] === true)
      && Array.isArray(view.refusal_reasons) && view.refusal_reasons.length === 0;
  };
  return { accepted: rows.length > 0 && rows.every(accepted),
    admission: rows.find((row) => !accepted(row))?.availability?.admission ?? null };
}

function ProjectSettingsForContext(props) {
  const { solarBinding, authorityKey, projectClient, catalogDigest, newProjectIntentKey, onProjectSubmit, busy, onClose } = props;
  let binding = null;
  let bindingKey = null;
  try { binding = createSolarDrawingBinding(solarBinding); bindingKey = solarDrawingBindingKey(binding); } catch { /* local refusal */ }
  const latest = useRef(props);
  latest.current = props;
  const owner = useRef(null);
  if (!owner.current || owner.current.bindingKey !== bindingKey || owner.current.authorityKey !== authorityKey
      || owner.current.client !== projectClient) {
    if (owner.current) {
      owner.current.alive = false;
      for (const controller of owner.current.controllers) controller.abort();
    }
    owner.current = { bindingKey, binding, authorityKey, client: projectClient, alive: true, generation: 0,
      controllers: new Set(), candidateControllers: new Set(), predicates: new Set(), candidate: null, armed: null, lock: null,
      digest: catalogDigest };
  }
  const scope = owner.current;
  if (scope.digest !== catalogDigest) {
    scope.generation += 1;
    scope.armed = null;
    scope.digest = catalogDigest;
    for (const controller of scope.candidateControllers) controller.abort();
  }
  const [loaded, setLoaded] = useState(null);
  const [drafts, setDrafts] = useState({});
  const [projectDrafts, setProjectDrafts] = useState({});
  const [units, setUnits] = useState({ drawing_units: '', elevation_datum: 'unknown', crs: '' });
  const [publication, setPublication] = useState(null);
  const [feedback, setFeedback] = useState(null);
  const [refresh, setRefresh] = useState(0);
  const [submitting, setSubmitting] = useState(false);
  const dependencies = binding && typeof authorityKey === 'string' && authorityKey.trim().length > 0
    && projectClient && ['readContext', 'prepareRun', 'catalog', 'captureCatalogScope', 'subscribe', 'getSnapshot']
      .every((key) => typeof projectClient[key] === 'function');
  const available = dependencies && typeof newProjectIntentKey === 'function' && typeof onProjectSubmit === 'function';
  const isScopeCurrent = () => {
    const current = latest.current;
    return scope.alive && owner.current === scope && current.projectClient === scope.client
      && current.authorityKey === scope.authorityKey && sameSolarDrawingBinding(current.solarBinding, scope.binding);
  };
  const operation = (candidate = true) => {
    const generation = scope.generation;
    const digest = catalogDigest;
    const predicate = projectClient.captureCatalogScope();
    const controller = new AbortController();
    scope.controllers.add(controller);
    if (candidate) scope.candidateControllers.add(controller);
    scope.predicates.add(predicate);
    return { signal: controller.signal, isCurrent: () => isScopeCurrent() && !controller.signal.aborted && predicate()
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
    setDrafts({});
    setProjectDrafts({});
    setUnits({ drawing_units: '', elevation_datum: 'unknown', crs: '' });
    setPublication(null);
    setFeedback(null);
    setSubmitting(false);
    if (!dependencies) return () => { scope.alive = false; };
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
          setLoaded({ scope, state: outcome.ok ? deriveProjectSettingsState({ binding, contextView: outcome.data })
            : { mode: 'refused', reason: 'context_unreadable' } });
          if (!outcome.ok) setFeedback({ scope, generation: scope.generation, ...projectFeedback(outcome) });
        } catch {
          if (current.isCurrent()) {
            contextSettled = true;
            setLoaded({ scope, state: { mode: 'refused', reason: 'context_unreadable' } });
          }
        } finally { current.finish(); }
      })();
    };
    const unsubscribe = projectClient.subscribe(() => {
      if (!isScopeCurrent()) return;
      if ((scope.armed && !scope.armed.isCurrent()) || [...scope.predicates].some((predicate) => !predicate())) {
        scope.armed = null;
        scope.generation += 1;
        for (const controller of scope.controllers) controller.abort();
        setPublication(null);
        setRefresh((value) => value + 1);
        if (!contextSettled) startRead();
      }
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
  }, [scope, Boolean(dependencies)]);

  const state = !binding ? { mode: 'refused', reason: 'binding_invalid' }
    : !dependencies ? { mode: 'refused', reason: 'submit_unavailable' }
      : loaded?.scope === scope ? loaded.state : null;
  const editable = state?.mode === 'edit' || state?.mode === 'initialize';
  const result = editable ? buildProjectSettingsRequest({ binding, state, drafts, units, projectDrafts,
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
        const built = buildProjectSettingsRequest({ binding, state, drafts, units, projectDrafts,
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
    (async () => {
      try {
        const outcome = await projectClient.catalog({ intents: [candidate.intent], signal: preview.signal });
        if (!preview.isCurrent()) return;
        const exact = exactSettingsPreview(outcome, catalogDigest);
        if (exact.accepted) {
          // Keep the predicate after settlement: checkout rotation invalidates an armed preview too.
          scope.armed = { candidate, generation: scope.generation, isCurrent: preview.isCurrent };
          setPublication({ scope, generation: scope.generation, reason: null });
          setFeedback(null);
        } else {
          setPublication({ scope, generation: scope.generation, reason: 'preview_refused' });
          setFeedback({ scope, generation: scope.generation, ...projectFeedback(outcome, exact.admission) });
        }
      } catch {
        if (preview.isCurrent()) {
          setPublication({ scope, generation: scope.generation, reason: 'preview_refused' });
          setFeedback({ scope, generation: scope.generation, ...projectFeedback(null) });
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
    : busy || submitting ? 'run_in_progress' : result && !result.ok ? result.reason
      : !armed ? (publication?.scope === scope && publication.generation === scope.generation
        ? publication.reason || 'preview_required' : 'preview_required') : null;
  const runMessage = feedback?.scope === scope && feedback.generation === scope.generation ? feedback : null;
  const effectiveZip = editable ? pyStrip(projectDrafts.zip_code ?? state.project.zip_code) : '';
  const zipChanged = editable && effectiveZip !== pyStrip(state.project.zip_code);
  const coordinateChange = editable ? buildSettingsParams(state, {}, units,
    Object.fromEntries(Object.entries(projectDrafts).filter(([key]) => key === 'latitude' || key === 'longitude'))) : null;
  const coordinatesUnchanged = coordinateChange?.reason === 'no_changes';
  const hasChanges = editable && (SETTINGS_FIELDS.some(({ key }) => {
    const parsed = parseField(key, drafts[key]);
    return parsed.ok && parsed.value !== state.settings[key];
  }) || Object.keys(projectDrafts).some((key) => {
    const patch = buildSettingsParams(state, {}, units, key === 'latitude' || key === 'longitude'
      ? Object.fromEntries(Object.entries(projectDrafts).filter(([field]) => field === 'latitude' || field === 'longitude'))
      : { [key]: projectDrafts[key] });
    return patch.ok || ['units_required', 'invalid_elevation_datum', 'invalid_crs'].includes(patch.reason);
  }));

  async function submit(event) {
    event.preventDefault();
    if (!isScopeCurrent()) return;
    if (reason || !armed || scope.lock || !scope.armed?.isCurrent()
        || scope.armed.generation !== scope.generation || scope.armed.candidate.fingerprint !== fingerprint) {
      setFeedback({ scope, generation: scope.generation, ...projectFeedback({ ok: false,
        reason: scope.lock ? 'SIP_R7_FORM_LOCKED' : 'SIP_R7_FORM_NOT_ARMED' }) });
      return;
    }
    const accepted = scope.armed;
    const pending = operation();
    const lock = {};
    scope.lock = lock;
    setSubmitting(true);
    try {
      const outcome = await projectClient.readContext({ signal: pending.signal });
      if (!pending.isCurrent()) return;
      if (scope.armed !== accepted) {
        setFeedback({ scope, generation: scope.generation, ...projectFeedback({ ok: false,
          reason: latest.current.busy ? 'SIP_R7_FORM_BUSY'
            : typeof latest.current.onProjectSubmit !== 'function' ? 'SIP_R7_FORM_NO_SUBMIT'
              : 'SIP_R7_FORM_NOT_ARMED' }) });
        return;
      }
      const checked = outcome.ok ? deriveProjectSettingsState({ binding, contextView: outcome.data })
        : { mode: 'refused', reason: 'context_unreadable' };
      if (checked.mode === 'refused' || checked.mode !== state.mode || checked.intakeSha256 !== state.intakeSha256
          || checked.rev !== state.rev || !sameSolarDrawingBinding(checked.binding, state.binding)) {
        scope.armed = null;
        setFeedback({ scope, generation: scope.generation,
          ...projectFeedback({ ...outcome, reason: 'SIP_R7_FORM_CONTEXT_STALE' }) });
        setPublication({ scope, generation: scope.generation,
          reason: checked.reason === 'not_current_head' ? 'not_current_head' : 'scope_stale' });
        return;
      }
      if (!pending.isCurrent()) return;
      if (latest.current.busy || typeof latest.current.onProjectSubmit !== 'function') {
        setFeedback({ scope, generation: scope.generation, ...projectFeedback({ ok: false,
          reason: latest.current.busy ? 'SIP_R7_FORM_BUSY' : 'SIP_R7_FORM_NO_SUBMIT' }) });
        return;
      }
      const result = await latest.current.onProjectSubmit(accepted.candidate.intent,
        { binding, authorityKey, signal: pending.signal, isCurrent: pending.isCurrent })
        || { ok: false, reason: 'SIP_R7_WORKSPACE_NO_RESULT' };
      if (!pending.isCurrent()) return;
      if (result?.ok && result.status === 202 && result.data?.status === 'submitted'
          && typeof result.data.job_id === 'string' && projectJobId.test(result.data.job_id)) {
        setFeedback({ scope, generation: scope.generation, outcome: result, text: 'Solar settings submitted.', code: null });
      } else setFeedback({ scope, generation: scope.generation, ...projectFeedback(result) });
    } catch {
      if (pending.isCurrent()) setFeedback({ scope, generation: scope.generation,
        ...projectFeedback({ ok: false, reason: 'SIP_R7_FORM_ERROR' }) });
    } finally {
      if (scope.lock === lock) {
        scope.lock = null;
        if (isScopeCurrent()) setSubmitting(false);
      }
      pending.finish();
    }
  }
  return (
    <section aria-label="Solar settings" data-testid="solar-settings-form" data-mode={state?.mode ?? 'loading'}>
      {state === null && <p role="status" data-testid="solar-settings-status">Reading the drawing.</p>}
      {state?.mode === 'edit' && <p>Graph revision {state.rev}</p>}
      {state?.mode === 'initialize' && <p>{PROJECT_SETTINGS_REASONS.graphless}</p>}
      {reason && <p role="status" data-testid="solar-settings-reason">{PROJECT_SETTINGS_REASONS[reason] ?? SOLAR_SETTINGS_REASONS[reason]}</p>}
      {runMessage && typeof runMessage === 'object' && typeof runMessage.text === 'string' && runMessage.text.length > 0 && (
        <p className="solar-settings-run" role="status" data-testid="solar-settings-run">{runMessage.text}{runMessage.code ? ` (${runMessage.code})` : ''}</p>
      )}
      {editable && (
        <form className="params" onSubmit={submit} noValidate>
          {[
            ['name', 'Project name'], ['zip_code', 'ZIP code'],
            ['latitude', 'Latitude'], ['longitude', 'Longitude'],
          ].map(([key, label]) => (
            <label className="param" key={key}>
              <span>{label}</span>
              <input type="text" value={projectDrafts[key] ?? String(state.project[key] ?? '')}
                onChange={(event) => {
                  const text = event.target.value;
                  change(setProjectDrafts, (previous) => ({ ...previous, [key]: text }));
                }} />
            </label>
          ))}
          <button type="button" onClick={() => change(setProjectDrafts, (previous) => ({ ...previous, latitude: '', longitude: '' }))}>Clear coordinates</button>
          {!effectiveZip && <p>ZIP code is blank. You can save setup. Cloud string sizing needs a saved ZIP code.</p>}
          {zipChanged && coordinatesUnchanged && <p>Changing the ZIP code clears saved coordinates unless you enter both coordinates again.</p>}
          {hasChanges && <p>Re-size strings after this change</p>}
          {SETTINGS_FIELDS.map(({ key, label, kind }) => {
            const hasDraft = Object.prototype.hasOwnProperty.call(drafts, key);
            if (kind === 'boolean') {
              return (
                <div className="param" key={key}>
                  <label>
                    <input
                      type="checkbox"
                      checked={(hasDraft ? drafts[key] : state.settings[key]) === true}
                      onChange={(event) => {
                        const checked = event.target.checked === true;
                        change(setDrafts, (previous) => ({ ...previous, [key]: checked }));
                      }}
                    />{label}
                  </label>
                  <span>{L2_COLLECTORS_NOTE}</span>
                </div>
              );
            }
            return (
              <label className="param" key={key}>
                <span>{label}</span>
                <input
                  type="text"
                  value={hasDraft ? drafts[key] : String(state.settings[key])}
                  aria-invalid={hasDraft && !parseField(key, drafts[key]).ok ? 'true' : undefined}
                  onChange={(event) => {
                    const text = event.target.value;
                    change(setDrafts, (previous) => ({ ...previous, [key]: text }));
                  }}
                />
              </label>
            );
          })}
          {state.mode === 'initialize' && (
            <fieldset className="params">
              <legend>Drawing coordinates</legend>
              <label className="param">
                <span>Drawing units</span>
                <select value={units.drawing_units} onChange={(event) => change(setUnits, { ...units, drawing_units: event.target.value })}>
                  <option value=""></option>
                  {DRAWING_UNITS.map((unit) => <option key={unit} value={unit}>{unit}</option>)}
                </select>
              </label>
              <label className="param">
                <span>Elevation datum</span>
                <input type="text" value={units.elevation_datum} onChange={(event) => change(setUnits, { ...units, elevation_datum: event.target.value })} />
              </label>
              <label className="param">
                <span>Coordinate system</span>
                <input type="text" value={units.crs} onChange={(event) => change(setUnits, { ...units, crs: event.target.value })} />
              </label>
            </fieldset>
          )}
          <button type="submit" disabled={Boolean(reason)}>{state.mode === 'edit' ? 'Apply settings' : 'Start Solar design'}</button>
        </form>
      )}
      {editable && <button type="button" disabled={submitting} onClick={() => {
        scope.generation += 1;
        scope.armed = null;
        setPublication(null);
        setRefresh((value) => value + 1);
      }}>Refresh preview</button>}
      <button type="button" onClick={() => onClose?.()}>Close</button>
    </section>
  );
}


export default function SolarSettingsForm(props) {
  const { context, readIntake, readVersions, checkoutHeld, busy, onSubmit, onClose, runMessage,
    solarBinding, authorityKey, projectClient, catalogDigest, newProjectIntentKey, onProjectSubmit } = props;
  if (Object.prototype.hasOwnProperty.call(props, 'solarBinding') || context?.projectId != null || context?.project_id != null) {
    return <ProjectSettingsForContext solarBinding={solarBinding} authorityKey={authorityKey}
      projectClient={projectClient} catalogDigest={catalogDigest} newProjectIntentKey={newProjectIntentKey}
      onProjectSubmit={onProjectSubmit} busy={busy} onClose={onClose} />;
  }
  return (
    <SettingsForContext
      key={JSON.stringify([context?.drawingId, context?.drawingVersion])}
      context={context}
      readIntake={readIntake}
      readVersions={readVersions}
      checkoutHeld={checkoutHeld}
      busy={busy}
      onSubmit={onSubmit}
      onClose={onClose}
      runMessage={runMessage}
    />
  );
}

function SettingsForContext({ context, readIntake, readVersions, checkoutHeld, busy, onSubmit, onClose, runMessage }) {
  const [views, setViews] = useState(null);
  const [drafts, setDrafts] = useState({});
  const [projectDrafts, setProjectDrafts] = useState({});
  const submitLock = useRef(false);
  const [units, setUnits] = useState({ drawing_units: '', elevation_datum: 'unknown', crs: '' });
  const drawingId = context?.drawingId;
  const drawingVersion = context?.drawingVersion;

  useEffect(() => {
    submitLock.current = false;
  });

  useEffect(() => {
    let current = true;
    setViews(null);
    Promise.all([
      Promise.resolve().then(() => readIntake(drawingId, drawingVersion)),
      Promise.resolve().then(() => readVersions(drawingId)),
    ]).then(([intakeView, versionsView]) => {
      if (current) setViews({ intakeView, versionsView });
    }).catch(() => {
      if (current) setViews({ intakeView: null, versionsView: null });
    });
    return () => { current = false; };
  }, [drawingId, drawingVersion, readIntake, readVersions]);

  const state = views === null ? null : deriveFormState({ context, ...views });
  const result = state ? buildSettingsParams(state, drafts, units, projectDrafts) : null;
  const reason = state?.mode === 'refused' ? state.reason
    : busy ? 'run_in_progress'
      : !checkoutHeld ? 'checkout_required'
        : result && !result.ok ? result.reason : null;
  const editable = state?.mode === 'edit' || state?.mode === 'initialize';
  const effectiveZip = editable ? pyStrip(projectDrafts.zip_code ?? state.project.zip_code) : '';
  const zipChanged = editable && effectiveZip !== pyStrip(state.project.zip_code);
  const coordinateChange = editable ? buildSettingsParams(state, {}, units,
    Object.fromEntries(Object.entries(projectDrafts).filter(([key]) => key === 'latitude' || key === 'longitude'))) : null;
  const coordinatesUnchanged = coordinateChange?.reason === 'no_changes';
  const hasChanges = editable && (
    SETTINGS_FIELDS.some(({ key }) => {
      const parsed = parseField(key, drafts[key]);
      return parsed.ok && parsed.value !== state.settings[key];
    }) || Object.keys(projectDrafts).some((key) => {
      const change = buildSettingsParams(state, {}, units, key === 'latitude' || key === 'longitude'
        ? Object.fromEntries(Object.entries(projectDrafts).filter(([field]) => field === 'latitude' || field === 'longitude'))
        : { [key]: projectDrafts[key] });
      return change.ok || ['units_required', 'invalid_elevation_datum', 'invalid_crs'].includes(change.reason);
    })
  );

  function submit(event) {
    event.preventDefault();
    if (!reason && result?.ok && !submitLock.current) {
      submitLock.current = true;
      onSubmit(result.params);
    }
  }

  return (
    <section aria-label="Solar settings" data-testid="solar-settings-form" data-mode={state?.mode ?? 'loading'}>
      {state === null && <p role="status" data-testid="solar-settings-status">Reading the drawing.</p>}
      {state?.mode === 'edit' && <p>Graph revision {state.rev}</p>}
      {reason && <p role="status" data-testid="solar-settings-reason">{SOLAR_SETTINGS_REASONS[reason]}</p>}
      {runMessage && typeof runMessage === 'object' && typeof runMessage.text === 'string' && runMessage.text.length > 0 && (
        <p className="solar-settings-run" role="status" data-testid="solar-settings-run">{runMessage.text}{runMessage.code ? ` (${runMessage.code})` : ''}</p>
      )}
      {editable && (
        <form className="params" onSubmit={submit} noValidate>
          {[
            ['name', 'Project name'], ['zip_code', 'ZIP code'],
            ['latitude', 'Latitude'], ['longitude', 'Longitude'],
          ].map(([key, label]) => (
            <label className="param" key={key}>
              <span>{label}</span>
              <input type="text" value={projectDrafts[key] ?? String(state.project[key] ?? '')}
                onChange={(event) => {
                  const text = event.target.value;
                  setProjectDrafts((previous) => ({ ...previous, [key]: text }));
                }} />
            </label>
          ))}
          <button type="button" onClick={() => setProjectDrafts((previous) => ({ ...previous, latitude: '', longitude: '' }))}>Clear coordinates</button>
          {!effectiveZip && <p>ZIP code is blank. You can save setup. Cloud string sizing needs a saved ZIP code.</p>}
          {zipChanged && coordinatesUnchanged && <p>Changing the ZIP code clears saved coordinates unless you enter both coordinates again.</p>}
          {hasChanges && <p>Re-size strings after this change</p>}
          {SETTINGS_FIELDS.map(({ key, label, kind }) => {
            const hasDraft = Object.prototype.hasOwnProperty.call(drafts, key);
            if (kind === 'boolean') {
              return (
                <div className="param" key={key}>
                  <label>
                    <input
                      type="checkbox"
                      checked={(hasDraft ? drafts[key] : state.settings[key]) === true}
                      onChange={(event) => {
                        const checked = event.target.checked === true;
                        setDrafts((previous) => ({ ...previous, [key]: checked }));
                      }}
                    />{label}
                  </label>
                  <span>{L2_COLLECTORS_NOTE}</span>
                </div>
              );
            }
            return (
              <label className="param" key={key}>
                <span>{label}</span>
                <input
                  type="text"
                  value={hasDraft ? drafts[key] : String(state.settings[key])}
                  aria-invalid={hasDraft && !parseField(key, drafts[key]).ok ? 'true' : undefined}
                  onChange={(event) => {
                    const text = event.target.value;
                    setDrafts((previous) => ({ ...previous, [key]: text }));
                  }}
                />
              </label>
            );
          })}
          {state.mode === 'initialize' && (
            <fieldset className="params">
              <legend>Drawing coordinates</legend>
              <label className="param">
                <span>Drawing units</span>
                <select value={units.drawing_units} onChange={(event) => setUnits({ ...units, drawing_units: event.target.value })}>
                  <option value=""></option>
                  {DRAWING_UNITS.map((unit) => <option key={unit} value={unit}>{unit}</option>)}
                </select>
              </label>
              <label className="param">
                <span>Elevation datum</span>
                <input type="text" value={units.elevation_datum} onChange={(event) => setUnits({ ...units, elevation_datum: event.target.value })} />
              </label>
              <label className="param">
                <span>Coordinate system</span>
                <input type="text" value={units.crs} onChange={(event) => setUnits({ ...units, crs: event.target.value })} />
              </label>
            </fieldset>
          )}
          <button type="submit" disabled={Boolean(reason)}>{state.mode === 'edit' ? 'Apply settings' : 'Start Solar design'}</button>
        </form>
      )}
      <button type="button" onClick={() => onClose()}>Close</button>
    </section>
  );
}
