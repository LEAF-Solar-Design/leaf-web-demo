import React, { useEffect, useState } from 'react';
import {
  SETTINGS_FIELDS, DRAWING_UNITS, SOLAR_SETTINGS_REASONS,
  parseField, deriveFormState, buildSettingsParams,
} from './solarSettingsModel.js';

export default function SolarSettingsForm({ context, readIntake, readVersions, checkoutHeld, busy, onSubmit, onClose, runMessage }) {
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
  const [units, setUnits] = useState({ drawing_units: '', elevation_datum: 'unknown', crs: '' });
  const drawingId = context?.drawingId;
  const drawingVersion = context?.drawingVersion;

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
  const result = state ? buildSettingsParams(state, drafts, units) : null;
  const reason = state?.mode === 'refused' ? state.reason
    : busy ? 'run_in_progress'
      : !checkoutHeld ? 'checkout_required'
        : result && !result.ok ? result.reason : null;
  const editable = state?.mode === 'edit' || state?.mode === 'initialize';

  function submit(event) {
    event.preventDefault();
    if (!reason && result?.ok) onSubmit(result.params);
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
          {SETTINGS_FIELDS.map(({ key, label, kind }) => {
            if (kind === 'fixed') {
              return (
                <div className="param" key={key}>
                  <label><input type="checkbox" checked={false} disabled />{label}</label>
                  <span>{SOLAR_SETTINGS_REASONS.l2_collectors_unavailable}</span>
                </div>
              );
            }
            const hasDraft = Object.prototype.hasOwnProperty.call(drafts, key);
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
