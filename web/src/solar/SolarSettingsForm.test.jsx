import React, { StrictMode } from 'react';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import SolarSettingsForm from './SolarSettingsForm.jsx';
import { L2_COLLECTORS_NOTE, SOLAR_SETTINGS_REASONS, PROJECT_SETTINGS_REASONS, deriveProjectSettingsState } from './solarSettingsModel.js';
import { solarSettingsRunFeedback } from './solarSettingsWire.js';
import { createSolarProjectClient } from './solarProjectClient.js';

const SHA = 'a'.repeat(64);

const projectClients = [];
afterEach(() => { cleanup(); for (const client of projectClients.splice(0)) client.dispose(); });
const projectBinding = { kind: 'project', organization_id: '11111111-1111-1111-1111-111111111111',
  project_id: '22222222-2222-2222-2222-222222222222', drawing_id: '33333333-3333-3333-3333-333333333333',
  input_version_id: '44444444-4444-4444-4444-444444444444' };
const projectDigest = 'sha256:' + 'd'.repeat(64); // Synthetic transport digest.
const projectJob = '55555555-5555-5555-5555-555555555555';
function canonicalView(binding = projectBinding, intake = IE.intake, context = {}) {
  return { context: { organization_id: binding.organization_id, project_id: binding.project_id,
    drawing_id: binding.drawing_id, input_version_id: binding.input_version_id,
    head_version_id: binding.input_version_id, is_head: true, intake_sha256: SHA, ...context }, intake };
}
function previewRow(changes = {}, availability = {}) {
  return { name: 'solar-settings', catalog_digest: projectDigest,
    availability: { admission_checked: true, admission: { status_code: 200, error: null },
      entitled: true, engine_ready: true, implemented: true, input_ready: true, runnable: true,
      refusal_reasons: [], ...availability }, ...changes };
}
const previewData = (rows = [previewRow()]) => ({ families: [{ capabilities: rows }] });
const projectResponse = (data, status = 200) => ({ ok: status >= 200 && status < 300, status, json: async () => data });
function projectHarness(overrides = {}) {
  const harness = { contextResponses: [], previewResponses: [], submitResponses: [], intake: IE.intake,
    contextChanges: {}, preview: previewData(), run: { job_id: projectJob, status: 'submitted' }, ...overrides };
  const fetchImpl = vi.fn(async (url, options) => {
    const binding = harness.real.getSnapshot().binding;
    if (url.includes('/context?')) return harness.contextResponses.shift()
      ?? projectResponse(canonicalView(binding, harness.intake, harness.contextChanges));
    if (url.includes('/api/capabilities?')) return harness.previewResponses.shift() ?? projectResponse(harness.preview);
    if (url.endsWith('/checkout')) {
      const identity = { organization_id: binding.organization_id, project_id: binding.project_id, drawing_id: binding.drawing_id };
      if (options.method === 'DELETE') return projectResponse({ ...identity, released: true, checkout: null });
      return projectResponse({ ...identity, acquired: true, checkout: { holder: 'synthetic editor',
        acquired: '2026-01-01T00:00:00Z', expires: '2099-01-01T00:00:00Z', fence: '1' },
        checkout_capability: 'lco1.' + 'c'.repeat(64) });
    }
    if (url.endsWith('/api/run')) return harness.submitResponses.shift() ?? projectResponse(harness.run, 202);
    throw new Error('Unexpected project request');
  });
  const real = createSolarProjectClient({ fetchImpl, authHeaders: () => ({ Authorization: 'Bearer fake' }) });
  real.setScope(projectBinding, 'epoch-1');
  projectClients.push(real);
  harness.real = real;
  harness.fetchImpl = fetchImpl;
  const client = { ...real };
  for (const name of ['readContext', 'prepareRun', 'catalog', 'submitRun', 'captureCatalogScope',
    'setScope', 'dispose', 'acquireCheckout', 'releaseCheckout']) client[name] = vi.fn((...args) => real[name](...args));
  harness.unsubscribes = [];
  client.subscribe = vi.fn((listener) => {
    const unsubscribe = vi.fn(real.subscribe(listener));
    harness.unsubscribes.push(unsubscribe);
    return unsubscribe;
  });
  harness.client = client;
  let sequence = 0;
  harness.props = props({ solarBinding: projectBinding, authorityKey: 'epoch-1', projectClient: client,
    catalogDigest: projectDigest, newProjectIntentKey: vi.fn(() => 'synthetic-intent-' + ++sequence),
    onProjectSubmit: vi.fn((intent, options) => real.submitRun(intent, { signal: options.signal })) });
  return harness;
}
async function editProject(harness, label = 'MPPT count', value = '4') {
  const view = render(<SolarSettingsForm {...harness.props} />);
  await waitForMode(harness.intake === IG.intake ? 'initialize' : 'edit');
  changeInput(label, value);
  return view;
}
async function readyProject(label = 'Apply settings') {
  await waitFor(() => expect(screen.getByRole('button', { name: label }).disabled).toBe(false));
}
async function settleProject() { await act(async () => { await Promise.resolve(); await Promise.resolve(); }); }
function rerenderProject(view, harness, changes) {
  harness.props = { ...harness.props, ...changes };
  harness.real.setScope(harness.props.solarBinding, harness.props.authorityKey);
  view.rerender(<SolarSettingsForm {...harness.props} />);
}
const previewCalls = (harness) => harness.fetchImpl.mock.calls.filter(([url]) => url.includes('/api/capabilities?'));

describe('Canonical Solar settings form through the merged project client', () => {
  it.each([true, false])('W1C-31 reports the busy state when submission is unavailable (busy=%s)', async (busy) => {
    const h = projectHarness();
    render(<SolarSettingsForm {...h.props} busy={busy} onProjectSubmit={undefined} />);
    await waitForMode('edit');
    const inProgress = 'A run is in progress. Wait for it to finish.';
    const unavailable = 'Project settings submission is not available in this view.';
    expect(screen.getByText(busy ? inProgress : unavailable)).toBeTruthy();
    expect(screen.queryByText(busy ? unavailable : inProgress)).toBeNull();
    expect(applyButton().disabled).toBe(true);
  });

  it('W1C16 traces each Start click from entry through a successful submission', async () => {
    const h = projectHarness({ intake: IG.intake });
    await editProject(h, 'Panels in sequence', '3');
    changeInput('Drawing units', 'ft');
    await readyProject('Start Solar design');
    const section = screen.getByTestId('solar-settings-form');
    expect(section.getAttribute('data-submit-seq')).toBe('0');
    expect(section.hasAttribute('data-submit-exit')).toBe(false);
    for (const seq of [1, 2]) {
      const pending = deferred();
      h.contextResponses.push(pending.promise);
      fireEvent.click(screen.getByRole('button', { name: 'Start Solar design' }));
      expect(section.getAttribute('data-submit-seq')).toBe(String(seq));
      expect(section.getAttribute('data-submit-exit')).toBe('entered');
      await act(async () => { pending.resolve(projectResponse(canonicalView(projectBinding, IG.intake))); });
      await waitFor(() => expect(section.getAttribute('data-submit-exit')).toBe('submitted'));
      await readyProject('Start Solar design');
    }
  });

  it.each(['not_armed', 'locked'])('W1C16 traces the existing %s top guard', async (exit) => {
    const h = projectHarness();
    let current = true;
    h.client.captureCatalogScope.mockImplementation(() => () => current);
    const pending = deferred();
    h.props.onProjectSubmit = vi.fn(() => pending.promise);
    await editProject(h);
    await readyProject();
    const form = applyButton().closest('form');
    if (exit === 'not_armed') current = false;
    act(() => {
      fireEvent.submit(form);
      if (exit === 'locked') fireEvent.submit(form);
    });
    const section = screen.getByTestId('solar-settings-form');
    expect(section.getAttribute('data-submit-seq')).toBe(exit === 'locked' ? '2' : '1');
    expect(section.getAttribute('data-submit-exit')).toBe(exit);
    expect(screen.getByTestId('solar-settings-run').textContent).toContain(`SIP_R7_FORM_${exit.toUpperCase()}`);
    if (exit === 'locked') {
      await waitFor(() => expect(h.props.onProjectSubmit).toHaveBeenCalledTimes(1));
      await act(async () => { pending.resolve({ ok: true, status: 202,
        data: { job_id: projectJob, status: 'submitted' } }); });
      // The older accepted run may publish feedback, but cannot overwrite the newer click's trace.
      expect(section.getAttribute('data-submit-exit')).toBe('locked');
      expect(screen.getByTestId('solar-settings-run').textContent).toBe('Solar settings submitted.');
    } else expect(h.props.onProjectSubmit).not.toHaveBeenCalled();
  });

  it.each(['context', 'result', 'error'])('W1C16 explains a superseded %s while its scope survives', async (stage) => {
    const h = projectHarness();
    const pending = deferred();
    if (stage !== 'context') h.props.onProjectSubmit = vi.fn(() => pending.promise);
    let current = true;
    h.client.captureCatalogScope.mockImplementation(() => () => current);
    await editProject(h);
    await readyProject();
    if (stage === 'context') h.client.readContext.mockImplementationOnce(() => pending.promise);
    fireEvent.click(applyButton());
    if (stage !== 'context') await waitFor(() => expect(h.props.onProjectSubmit).toHaveBeenCalledTimes(1));
    current = false;
    await act(async () => {
      if (stage === 'error') pending.reject(new Error('superseded transport'));
      else pending.resolve(stage === 'context' ? { ok: true, data: canonicalView() }
        : { ok: true, status: 202, data: { job_id: projectJob, status: 'submitted' } });
    });
    const code = `SIP_R7_FORM_SUPERSEDED_${stage.toUpperCase()}`;
    expect(screen.getByTestId('solar-settings-run').textContent).toContain(code);
    expect(screen.getByTestId('solar-settings-form').getAttribute('data-submit-exit')).toBe(`superseded_${stage}`);
    if (stage === 'context') expect(h.props.onProjectSubmit).not.toHaveBeenCalled();

    // A successful preview calls setFeedback(null); the trace still explains the previous click.
    current = true;
    fireEvent.click(screen.getByRole('button', { name: 'Refresh preview' }));
    await readyProject();
    expect(screen.getByTestId('solar-settings-run').textContent).toContain(code);
    const next = deferred();
    h.client.readContext.mockImplementationOnce(() => next.promise);
    fireEvent.click(applyButton());
    expect(screen.queryByTestId('solar-settings-run')).toBeNull();
    expect(screen.getByTestId('solar-settings-form').getAttribute('data-submit-seq')).toBe('2');
    expect(screen.getByTestId('solar-settings-form').getAttribute('data-submit-exit')).toBe('entered');
    current = false;
    await act(async () => { next.resolve({ ok: true, data: canonicalView() }); });
  });

  it('W1C16 explains a predicate that expires immediately before the handoff', async () => {
    const h = projectHarness();
    await editProject(h);
    await readyProject();
    const predicate = vi.fn().mockReturnValueOnce(true).mockReturnValue(false);
    h.client.captureCatalogScope.mockImplementationOnce(() => predicate);
    fireEvent.click(applyButton());
    await waitFor(() => expect(screen.getByTestId('solar-settings-form').getAttribute('data-submit-exit'))
      .toBe('superseded_handoff'));
    expect(screen.getByTestId('solar-settings-run').textContent).toContain('SIP_R7_FORM_SUPERSEDED_HANDOFF');
    expect(h.props.onProjectSubmit).not.toHaveBeenCalled();
  });

  it('W1C16 explains a replaced scope at handler entry and commits the current scope', async () => {
    const h = projectHarness();
    const binding = { ...projectBinding };
    h.props.solarBinding = binding;
    await editProject(h);
    await readyProject();
    const subscriptions = h.client.subscribe.mock.calls.length;
    const reads = h.client.readContext.mock.calls.length;
    // Change the latest binding without a render: the on-screen handler still owns the old scope.
    binding.input_version_id = '66666666-6666-6666-6666-666666666666';
    fireEvent.click(applyButton());
    expect(screen.getByTestId('solar-settings-form').getAttribute('data-submit-exit')).toBe('scope_replaced');
    expect(screen.getByTestId('solar-settings-form').getAttribute('data-submit-seq')).toBe('1');
    expect(screen.getByTestId('solar-settings-run').textContent).toContain('SIP_R7_FORM_SCOPE_REPLACED');
    // The forced render installs a new scope, whose snapshot refuses the mismatched client binding.
    await waitForMode('refused');
    expect(h.client.getSnapshot().binding).toEqual(projectBinding);
    expect(h.client.subscribe).toHaveBeenCalledTimes(subscriptions);
    expect(h.client.readContext).toHaveBeenCalledTimes(reads);
    expect(h.props.onProjectSubmit).not.toHaveBeenCalled();
  });

  it.each([
    [undefined, 'SIP_R7_WORKSPACE_NO_RESULT'],
    [{ ok: false, status: null, reason: 'SIP_R7_WORKSPACE_NOT_ADMITTED', envelope: null }, 'SIP_R7_WORKSPACE_NOT_ADMITTED'],
  ])('W1C15 reports the project submit result %j in run feedback', async (outcome, code) => {
    const h = projectHarness({ intake: IG.intake });
    h.props.onProjectSubmit = vi.fn().mockResolvedValue(outcome);
    await editProject(h, 'Panels in sequence', '3');
    changeInput('Drawing units', 'ft');
    await readyProject('Start Solar design');
    fireEvent.click(screen.getByRole('button', { name: 'Start Solar design' }));
    await waitFor(() => expect(screen.getByTestId('solar-settings-run').textContent).toContain(code));
    expect(h.props.onProjectSubmit).toHaveBeenCalledTimes(1);
    expect(h.client.submitRun).not.toHaveBeenCalled();
  });

  it.each([
    [{ busy: true }, 'SIP_R7_FORM_BUSY'],
    [{ onProjectSubmit: undefined }, 'SIP_R7_FORM_NO_SUBMIT'],
  ])('W1C15 reports a post-context guard %j', async (changes, code) => {
    const h = projectHarness();
    const view = await editProject(h);
    await readyProject();
    const pending = deferred();
    h.contextResponses.push(pending.promise);
    fireEvent.click(applyButton());
    await waitFor(() => expect(h.client.readContext).toHaveBeenCalledTimes(2));
    view.rerender(<SolarSettingsForm {...h.props} {...changes} />);
    await act(async () => { pending.resolve(projectResponse(canonicalView())); });
    await waitFor(() => expect(screen.getByTestId('solar-settings-run').textContent).toContain(code));
    expect(h.props.onProjectSubmit).not.toHaveBeenCalled();
  });

  it('W1C15 reports a preview that expires between enabling Start and the click', async () => {
    const h = projectHarness({ intake: IG.intake });
    let current = true;
    h.client.captureCatalogScope.mockImplementation(() => () => current);
    await editProject(h, 'Panels in sequence', '3');
    changeInput('Drawing units', 'ft');
    await readyProject('Start Solar design');
    current = false;
    fireEvent.click(screen.getByRole('button', { name: 'Start Solar design' }));
    expect(screen.getByTestId('solar-settings-run').textContent).toContain('SIP_R7_FORM_NOT_ARMED');
    expect(h.props.onProjectSubmit).not.toHaveBeenCalled();
  });

  it('W1C15 reports a duplicate enabled submit as locked while preserving the accepted run', async () => {
    const h = projectHarness();
    const pending = deferred();
    h.props.onProjectSubmit = vi.fn(() => pending.promise);
    await editProject(h);
    await readyProject();
    const form = applyButton().closest('form');
    act(() => { fireEvent.submit(form); fireEvent.submit(form); });
    expect(screen.getByTestId('solar-settings-run').textContent).toContain('SIP_R7_FORM_LOCKED');
    await waitFor(() => expect(h.props.onProjectSubmit).toHaveBeenCalledTimes(1));
    await act(async () => { pending.resolve({ ok: true, status: 202,
      data: { job_id: projectJob, status: 'submitted' } }); });
    expect(screen.getByTestId('solar-settings-run').textContent).toBe('Solar settings submitted.');
  });

  it('R8-06 refuses invalid canonical bindings without client or legacy calls', async () => {
    const accessor = { ...projectBinding };
    const get = vi.fn(() => projectBinding.input_version_id);
    Object.defineProperty(accessor, 'input_version_id', { enumerable: true, get });
    const missing = { ...projectBinding };
    delete missing.drawing_id;
    for (const solarBinding of [undefined, null, missing, { ...projectBinding, input_version_id: 3 },
      { ...projectBinding, extra: true }, accessor]) {
      const h = projectHarness();
      render(<SolarSettingsForm {...h.props} solarBinding={solarBinding} />);
      await waitForMode('refused');
      expect(screen.getByTestId('solar-settings-reason').textContent).toBe(PROJECT_SETTINGS_REASONS.binding_invalid);
      expect(deriveProjectSettingsState({ binding: solarBinding, contextView: canonicalView() }))
        .toEqual({ mode: 'refused', reason: 'binding_invalid' });
      expect(h.fetchImpl).not.toHaveBeenCalled();
      expect(h.props.readIntake).not.toHaveBeenCalled();
      expect(h.props.readVersions).not.toHaveBeenCalled();
      expect(h.client.prepareRun).not.toHaveBeenCalled();
      cleanup();
    }
    expect(get).not.toHaveBeenCalled();
  });
  it('R8-07 rejects mismatched and malformed context at the R7 and model boundaries', async () => {
    const other = '66666666-6666-6666-6666-666666666666';
    for (const changes of [
      { organization_id: other }, { project_id: other }, { drawing_id: other }, { input_version_id: other },
      { head_version_id: 3 }, { is_head: 'true' }, { head_version_id: other, is_head: true },
    ]) {
      const h = projectHarness({ contextChanges: changes });
      render(<SolarSettingsForm {...h.props} />);
      await waitForMode('refused');
      expect(screen.getByTestId('solar-settings-reason').textContent).toBe(PROJECT_SETTINGS_REASONS.context_unreadable);
      expect(deriveProjectSettingsState({ binding: projectBinding, contextView: canonicalView(projectBinding, IE.intake, changes) }))
        .toEqual({ mode: 'refused', reason: 'context_unreadable' });
      const outcome = await h.client.readContext.mock.results[0].value;
      if (!(changes.head_version_id === other && changes.is_head === true)) {
        expect(outcome.reason).toBe('SIP_R7_RESPONSE_INVALID');
      }
      expect(h.client.prepareRun).not.toHaveBeenCalled();
      expect(h.props.onProjectSubmit).not.toHaveBeenCalled();
      cleanup();
    }
  });
  it('R8-08 refuses historical context before preview or handoff', async () => {
    const h = projectHarness({ contextChanges: { head_version_id: '66666666-6666-6666-6666-666666666666', is_head: false } });
    render(<SolarSettingsForm {...h.props} />);
    await waitForMode('refused');
    expect(screen.getByTestId('solar-settings-reason').textContent).toBe(SOLAR_SETTINGS_REASONS.not_current_head);
    expect(screen.queryByRole('button', { name: 'Apply settings' })).toBeNull();
    expect(h.client.catalog).not.toHaveBeenCalled();
    expect(h.props.onProjectSubmit).not.toHaveBeenCalled();
  });
  it('R8-13 arms only the captured exact accepted preview and hands off the same frozen intent', async () => {
    const pending = deferred();
    const h = projectHarness({ previewResponses: [pending.promise] });
    h.props.checkoutHeld = false;
    await editProject(h);
    await waitFor(() => expect(h.client.catalog).toHaveBeenCalledTimes(1));
    expect(applyButton().disabled).toBe(true);
    expect(screen.getByTestId('solar-settings-reason').textContent).toBe(PROJECT_SETTINGS_REASONS.preview_required);
    const intent = h.client.catalog.mock.calls[0][0].intents[0];
    expect(Object.isFrozen(intent)).toBe(true);
    expect(Object.keys(intent)).toEqual(['binding', 'authorityKey', 'tool', 'params', 'catalogDigest', 'idempotencyKey']);
    await act(async () => { pending.resolve(projectResponse(previewData())); });
    await readyProject();
    fireEvent.click(applyButton());
    await waitFor(() => expect(h.props.onProjectSubmit).toHaveBeenCalledTimes(1));
    expect(h.props.onProjectSubmit.mock.calls[0][0]).toBe(intent);
    const guard = h.props.onProjectSubmit.mock.calls[0][1];
    expect(guard.binding).toEqual(projectBinding);
    expect(guard.authorityKey).toBe('epoch-1');
    expect(guard.signal.aborted).toBe(false);
    expect(guard.isCurrent()).toBe(true);
    expect(h.client.readContext).toHaveBeenCalledTimes(2);
  });
  it('R8-14 refuses outer success without exact admission or with contradictory matching rows', async () => {
    const cases = [
      [previewRow({}, { admission_checked: false })],
      [previewRow({}, { admission: { status_code: 200 } })],
      [previewRow({}, { admission: { status_code: 409, error: null } })],
      [previewRow({}, { refusal_reasons: ['graph_seed_required'] })],
      [previewRow({}, { refusal_reasons: ['project_request_required'] })],
      [previewRow({ catalog_digest: 'sha256:' + 'e'.repeat(64) })],
      [], [previewRow(), previewRow({}, { runnable: false })],
      ...['entitled', 'engine_ready', 'implemented', 'input_ready', 'runnable'].map((key) => [previewRow({}, { [key]: false })]),
    ];
    for (const rows of cases) {
      const h = projectHarness({ preview: previewData(rows) });
      await editProject(h);
      await waitFor(() => expect(screen.getByTestId('solar-settings-reason').textContent).toBe(PROJECT_SETTINGS_REASONS.preview_refused));
      expect(applyButton().disabled).toBe(true);
      fireEvent.click(applyButton());
      expect(h.props.onProjectSubmit).not.toHaveBeenCalled();
      cleanup();
    }
  });
  it('R8-15 preserves nested graph and L2 preview refusals in sanitized HTTP 200 data', async () => {
    for (const code of ['INVALID_SEED_PARENT', 'GRAPH_NOT_EMBEDDED', 'GRAPH_ALREADY_EMBEDDED',
      'SOURCE_HASH_MISMATCH', 'STALE_GRAPH_REVISION', 'DESIGN_PRESET_L2_EQUIPMENT_PRESENT', 'DUPLICATE_EQUIPMENT_NUMBER']) {
      const admission = { status_code: 409, error: { reason_code: code, error_code: 'BAD_PARAMS',
        message: 'Canonical Solar request was refused.', authorization: 'hidden transport field' } };
      const h = projectHarness({ preview: previewData([previewRow({}, { admission, runnable: false })]) });
      await editProject(h);
      await waitFor(() => expect(screen.getByTestId('solar-settings-run').textContent)
        .toBe(PROJECT_SETTINGS_REASONS.preview_refused + ' (' + code + ')'));
      const outcome = await h.client.catalog.mock.results[0].value;
      expect(outcome.status).toBe(200);
      expect(outcome).not.toHaveProperty('reason');
      const error = outcome.data.families[0].capabilities[0].availability.admission.error;
      expect(outcome.data.families[0].capabilities[0].availability.admission.status_code).toBe(409);
      expect(error.reason_code).toBe(code);
      expect(error).not.toHaveProperty('authorization');
      expect(Object.isFrozen(error)).toBe(true);
      expect(applyButton().disabled).toBe(true);
      expect(h.props.onProjectSubmit).not.toHaveBeenCalled();
      cleanup();
    }
  });
  it('R8-16 keeps the R7 fallback transport reason and absent server reason for digest refusal', async () => {
    const envelope = { error: { error_code: 'BAD_PARAMS',
      message: 'catalog tool changed or confirmation digest is missing; refresh tools and confirm again',
      authorization: 'hidden field' }, detail: { preserved: true } };
    const h = projectHarness({ previewResponses: [projectResponse(envelope, 409)] });
    await editProject(h);
    await waitFor(() => expect(screen.getByTestId('solar-settings-run').textContent).toContain('(HTTP_ERROR)'));
    const outcome = await h.client.catalog.mock.results[0].value;
    expect(outcome).toEqual({ ok: false, status: 409, reason: 'HTTP_ERROR',
      envelope: { error: { error_code: 'BAD_PARAMS', message: envelope.error.message }, detail: { preserved: true } } });
    expect(outcome.envelope.error).not.toHaveProperty('reason_code');
    expect(h.props.onProjectSubmit).not.toHaveBeenCalled();
  });
  it('R8-17 preserves checkout and historical new-write refusal statuses and codes', async () => {
    for (const [code, status] of [['SIP_R1_CHECKOUT_REQUIRED', 409], ['SIP_R1_CHECKOUT_EXPIRED', 409],
      ['SIP_R1_CHECKOUT_DENIED', 403], ['SIP_R1_CHECKOUT_STALE', 409], ['SIP_R1_STALE_VERSION', 409]]) {
      const envelope = { error: { reason_code: code, error_code: status === 403 ? 'FORBIDDEN' : 'BAD_PARAMS',
        message: 'Project Solar admission unavailable.' }, detail: { preserved: true }, authorization: 'hidden x' };
      const h = projectHarness({ previewResponses: [projectResponse(envelope, status)] });
      await editProject(h);
      await waitFor(() => expect(screen.getByTestId('solar-settings-run').textContent).toContain('(' + code + ')'));
      const outcome = await h.client.catalog.mock.results[0].value;
      expect(outcome.status).toBe(status);
      expect(outcome.reason).toBe(code);
      expect(outcome.envelope.error).toEqual(envelope.error);
      expect(outcome.envelope.detail).toEqual({ preserved: true });
      expect(outcome.envelope).not.toHaveProperty('authorization');
      expect(applyButton().disabled).toBe(true);
      expect(h.props.onProjectSubmit).not.toHaveBeenCalled();
      cleanup();
    }
  });
  it('R8-18 invalidates armed previews synchronously for params digest and key changes', async () => {
    const h = projectHarness();
    const view = await editProject(h);
    await readyProject();
    const first = h.client.catalog.mock.calls[0][0].intents[0];
    const paramsPreview = deferred();
    h.previewResponses.push(paramsPreview.promise);
    changeInput('MPPT count', '5');
    expect(applyButton().disabled).toBe(true);
    fireEvent.click(applyButton());
    expect(h.props.onProjectSubmit).not.toHaveBeenCalled();
    await waitFor(() => expect(h.client.catalog).toHaveBeenCalledTimes(2));
    await act(async () => { paramsPreview.resolve(projectResponse(previewData())); });
    await readyProject();
    const second = h.client.catalog.mock.calls[1][0].intents[0];
    expect(second.idempotencyKey).not.toBe(first.idempotencyKey);
    const digestPreview = deferred();
    h.previewResponses.push(digestPreview.promise);
    const digest = 'sha256:' + 'e'.repeat(64);
    view.rerender(<SolarSettingsForm {...h.props} catalogDigest={digest} />);
    expect(applyButton().disabled).toBe(true);
    await waitFor(() => expect(h.client.catalog).toHaveBeenCalledTimes(3));
    await act(async () => { digestPreview.resolve(projectResponse(previewData([previewRow({ catalog_digest: digest })]))); });
    await readyProject();
    const third = h.client.catalog.mock.calls[2][0].intents[0];
    expect(third.catalogDigest).toBe(digest);
    expect(third.idempotencyKey).not.toBe(second.idempotencyKey);
    // A new key factory alone is not a new candidate: two parent renders with fresh inline factories leave
    // the armed preview in place and issue no catalog request.
    view.rerender(<SolarSettingsForm {...h.props} catalogDigest={digest} newProjectIntentKey={() => 'replacement-key'} />);
    view.rerender(<SolarSettingsForm {...h.props} catalogDigest={digest} newProjectIntentKey={() => 'replacement-key'} />);
    expect(applyButton().disabled).toBe(false);
    expect(h.client.catalog).toHaveBeenCalledTimes(3);
    // The next candidate takes its key from the factory current at that moment.
    const keyPreview = deferred();
    h.previewResponses.push(keyPreview.promise);
    changeInput('MPPT count', '6');
    expect(applyButton().disabled).toBe(true);
    await waitFor(() => expect(h.client.catalog).toHaveBeenCalledTimes(4));
    expect(h.client.catalog.mock.calls[3][0].intents[0].idempotencyKey).toBe('replacement-key');
    expect(h.props.onProjectSubmit).not.toHaveBeenCalled();
    await act(async () => { keyPreview.resolve(projectResponse(previewData([previewRow({ catalog_digest: digest })]))); });
  });
  it('R8-19 discards old context successes and failures across every binding epoch and client change', async () => {
    const other = '66666666-6666-6666-6666-666666666666';
    for (const kind of ['organization_id', 'project_id', 'drawing_id', 'input_version_id', 'authorityKey', 'client']) {
      for (const failed of [false, true]) {
        const late = deferred();
        const h = projectHarness({ contextResponses: [late.promise] });
        const view = render(<SolarSettingsForm {...h.props} />);
        await waitFor(() => expect(h.client.readContext).toHaveBeenCalledTimes(1));
        const next = projectHarness({ intake: { ...IE.intake, solar_design_graph: { rev: 7, settings: { ...S, num_mppt: 8 }, project: P } } });
        const binding = kind in projectBinding ? { ...projectBinding, [kind]: other } : projectBinding;
        const epoch = kind === 'authorityKey' ? 'epoch-2' : 'epoch-1';
        next.real.setScope(binding, epoch);
        const nextProps = { ...h.props, solarBinding: binding, authorityKey: epoch,
          projectClient: kind === 'client' ? next.client : h.client };
        if (kind !== 'client') {
          h.intake = next.intake;
          h.real.setScope(binding, epoch);
        }
        view.rerender(<SolarSettingsForm {...nextProps} />);
        await waitForMode('edit');
        expect(screen.getByText('Graph revision 7')).toBeTruthy();
        const oldSignal = h.client.readContext.mock.calls[0][0].signal;
        expect(oldSignal.aborted).toBe(true);
        await act(async () => { late.resolve(projectResponse(failed ? { error: { reason_code: 'OLD_READ_FAILURE' } } : canonicalView(), failed ? 500 : 200)); });
        expect(screen.getByLabelText('MPPT count').value).toBe('8');
        expect(screen.queryByText('Graph revision 2')).toBeNull();
        expect(screen.queryByTestId('solar-settings-run')).toBeNull();
        cleanup();
      }
    }
  });
  it('R8-20 discards preview and submission settlements across scope changes including A to B to A', async () => {
    const other = '66666666-6666-6666-6666-666666666666';
    for (const stage of ['preview', 'submission']) {
      for (const kind of ['organization_id', 'project_id', 'drawing_id', 'input_version_id', 'authorityKey', 'client', 'ABA']) {
        for (const failed of [false, true]) {
          const late = deferred();
          const h = projectHarness(stage === 'preview' ? { previewResponses: [late.promise] } : {});
          if (stage === 'submission') h.props.onProjectSubmit = vi.fn(() => late.promise);
          const view = await editProject(h);
          if (stage === 'preview') await waitFor(() => expect(h.client.catalog).toHaveBeenCalledTimes(1));
          else {
            await readyProject();
            fireEvent.click(applyButton());
            await waitFor(() => expect(h.props.onProjectSubmit).toHaveBeenCalledTimes(1));
          }
          const next = kind === 'client' ? projectHarness() : h;
          const binding = kind in projectBinding || kind === 'ABA'
            ? { ...projectBinding, [kind === 'ABA' ? 'input_version_id' : kind]: other } : projectBinding;
          const epoch = kind === 'authorityKey' ? 'epoch-2' : 'epoch-1';
          next.real.setScope(binding, epoch);
          view.rerender(<SolarSettingsForm {...h.props} solarBinding={binding} authorityKey={epoch} projectClient={next.client} />);
          await waitForMode('edit');
          if (kind === 'ABA') {
            h.real.setScope(projectBinding, 'epoch-1');
            view.rerender(<SolarSettingsForm {...h.props} />);
            await waitForMode('edit');
          }
          await act(async () => {
            late.resolve(stage === 'preview' ? projectResponse(failed ? { error: { reason_code: 'OLD_PREVIEW_FAILURE' } } : previewData(), failed ? 409 : 200)
              : failed ? { ok: false, status: 409, reason: 'OLD_SUBMIT_FAILURE', envelope: { error: { reason_code: 'OLD_SUBMIT_FAILURE' } } }
                : { ok: true, status: 202, data: { job_id: projectJob, status: 'submitted' } });
          });
          expect(screen.getByLabelText('MPPT count').value).toBe('2');
          expect(applyButton().disabled).toBe(true);
          expect(screen.queryByTestId('solar-settings-run')).toBeNull();
          expect(h.props.onProjectSubmit).toHaveBeenCalledTimes(stage === 'submission' ? 1 : 0);
          cleanup();
        }
      }
    }
  }, 20000);
  it('R8-21 uses the R7 captured predicate after result production and before continuation', async () => {
    for (const stage of ['context', 'preview']) {
      const h = projectHarness();
      let captured = false;
      const method = stage === 'context' ? 'readContext' : 'catalog';
      h.client[method].mockImplementationOnce(async (...args) => {
        const outcome = await h.real[method](...args);
        expect(outcome.ok).toBe(true);
        h.real.setScope(null, null);
        captured = true;
        return outcome;
      });
      render(<SolarSettingsForm {...h.props} />);
      if (stage === 'preview') {
        await waitForMode('edit');
        changeInput('MPPT count', '4');
      }
      await waitFor(() => expect(captured).toBe(true));
      await settleProject();
      expect(screen.queryByTestId('solar-settings-run')).toBeNull();
      expect(h.props.onProjectSubmit).not.toHaveBeenCalled();
      if (stage === 'context') expect(screen.queryByLabelText('MPPT count')).toBeNull();
      else expect(applyButton().disabled).toBe(true);
      cleanup();
    }
  });
  it('R8-33 restarts an initial context read that a checkout rotation aborted', async () => {
    for (const failed of [false, true]) {
      const late = deferred();
      const h = projectHarness({ contextResponses: [late.promise] });
      const view = render(<SolarSettingsForm {...h.props} />);
      await waitFor(() => expect(h.client.readContext).toHaveBeenCalledTimes(1));
      const first = h.client.readContext.mock.calls[0][0].signal;
      expect(screen.getByTestId('solar-settings-form').getAttribute('data-mode')).toBe('loading');
      await act(async () => { await h.real.acquireCheckout(); });
      expect(first.aborted).toBe(true);
      await waitForMode('edit');
      const reads = h.client.readContext.mock.calls.length;
      expect(reads).toBeGreaterThanOrEqual(2);
      for (const [options] of h.client.readContext.mock.calls.slice(0, -1)) expect(options.signal.aborted).toBe(true);
      await act(async () => {
        late.resolve(failed ? projectResponse({ error: { error_code: 'SIP_R1_INTERNAL' } }, 500)
          : projectResponse(canonicalView(projectBinding, IG.intake)));
      });
      view.rerender(<SolarSettingsForm {...h.props} />);
      await settleProject();
      expect(screen.getByTestId('solar-settings-form').getAttribute('data-mode')).toBe('edit');
      expect(h.client.readContext).toHaveBeenCalledTimes(reads);
      // Once the context has settled, a later rotation re-previews but never reads the context again.
      changeInput('MPPT count', '4');
      await waitFor(() => expect(h.client.catalog).toHaveBeenCalledTimes(1));
      await act(async () => { await h.real.releaseCheckout(); });
      await waitFor(() => expect(h.client.catalog.mock.calls.length).toBeGreaterThan(1));
      expect(h.client.readContext).toHaveBeenCalledTimes(reads);
      cleanup();
    }
  });
  it('R8-22 clears checkout-stale previews and repreviews the unchanged intent and key', async () => {
    for (const pending of [false, true]) {
      const late = deferred();
      const h = projectHarness(pending ? { previewResponses: [late.promise] } : {});
      await editProject(h);
      await waitFor(() => expect(h.client.catalog).toHaveBeenCalledTimes(1));
      if (!pending) await readyProject();
      const intent = h.client.catalog.mock.calls[0][0].intents[0];
      const refreshed = deferred();
      h.previewResponses.push(refreshed.promise);
      await act(async () => { await h.real.acquireCheckout(); });
      expect(applyButton().disabled).toBe(true);
      await waitFor(() => expect(h.client.catalog).toHaveBeenCalledTimes(2));
      expect(h.client.catalog.mock.calls[1][0].intents[0]).toBe(intent);
      expect(h.props.newProjectIntentKey).toHaveBeenCalledTimes(1);
      if (pending) await act(async () => { late.resolve(projectResponse(previewData())); });
      expect(applyButton().disabled).toBe(true);
      await act(async () => { refreshed.resolve(projectResponse(previewData())); });
      await readyProject();
      const calls = previewCalls(h);
      const query = new URL(calls[1][0], 'https://synthetic.test').searchParams;
      expect(JSON.parse(query.get('project_runs'))['solar-settings'].idempotency_key).toBe(intent.idempotencyKey);
      cleanup();
    }
  });
  it('R8-23 rereads head digest mode and revision before handoff without retargeting', async () => {
    const moved = { head_version_id: '66666666-6666-6666-6666-666666666666', is_head: false };
    for (const [view, reason] of [
      [canonicalView(projectBinding, IE.intake, moved), 'not_current_head'],
      [canonicalView(projectBinding, IE.intake, { intake_sha256: 'e'.repeat(64) }), 'scope_stale'],
      [canonicalView(projectBinding, IG.intake), 'scope_stale'],
      [canonicalView(projectBinding, { ...IE.intake, solar_design_graph: { rev: 3, settings: S, project: P } }), 'scope_stale'],
    ]) {
      const h = projectHarness();
      await editProject(h);
      await readyProject();
      h.contextResponses.push(projectResponse(view));
      fireEvent.click(applyButton());
      await waitFor(() => expect(screen.getByTestId('solar-settings-reason').textContent)
        .toBe(PROJECT_SETTINGS_REASONS[reason] ?? SOLAR_SETTINGS_REASONS[reason]));
      expect(applyButton().disabled).toBe(true);
      expect(h.props.onProjectSubmit).not.toHaveBeenCalled();
      expect(h.client.prepareRun).toHaveBeenCalledTimes(1);
      cleanup();
    }
    const h = projectHarness();
    await editProject(h);
    await readyProject();
    const envelope = { error: { reason_code: 'SIP_R1_INTAKE_DIGEST_MISMATCH', error_code: 'INTERNAL',
      message: 'Project Solar admission unavailable.' } };
    h.contextResponses.push(projectResponse(envelope, 500));
    fireEvent.click(applyButton());
    await waitFor(() => expect(screen.getByTestId('solar-settings-run').textContent).toContain('SIP_R1_INTAKE_DIGEST_MISMATCH'));
    const outcome = await h.client.readContext.mock.results[1].value;
    expect(outcome.status).toBe(500);
    expect(outcome.envelope).toEqual(envelope);
    expect(h.props.onProjectSubmit).not.toHaveBeenCalled();
  });
  it('R8-24 locks before render and stays locked through unrelated renders until callback settlement', async () => {
    const pending = deferred();
    const h = projectHarness();
    h.props.onProjectSubmit = vi.fn(() => pending.promise);
    const view = await editProject(h);
    await readyProject();
    const form = applyButton().closest('form');
    act(() => { fireEvent.submit(form); fireEvent.submit(form); });
    await waitFor(() => expect(h.props.onProjectSubmit).toHaveBeenCalledTimes(1));
    view.rerender(<SolarSettingsForm {...h.props} newProjectIntentKey={() => 'unused-key'}
      runMessage={{ text: 'Uncorrelated legacy feedback' }} />);
    fireEvent.submit(form);
    expect(h.props.onProjectSubmit).toHaveBeenCalledTimes(1);
    expect(applyButton().disabled).toBe(true);
    expect(screen.queryByText('Uncorrelated legacy feedback')).toBeNull();
    expect(h.props.newProjectIntentKey).toHaveBeenCalledTimes(1);
    await act(async () => { pending.resolve(null); });
    await readyProject();
    fireEvent.click(applyButton());
    await waitFor(() => expect(h.props.onProjectSubmit).toHaveBeenCalledTimes(2));
    expect(h.props.onProjectSubmit.mock.calls[1][0]).toBe(h.props.onProjectSubmit.mock.calls[0][0]);
  });
  it('R8-25 hands real R7 edit and seed intents to the canonical run transport', async () => {
    for (const seed of [false, true]) {
      const h = projectHarness({ intake: seed ? IG.intake : IE.intake });
      await h.real.acquireCheckout();
      await editProject(h, seed ? 'Panels in sequence' : 'MPPT count', seed ? '3' : '4');
      if (seed) {
        expect(screen.getByText(PROJECT_SETTINGS_REASONS.graphless)).toBeTruthy();
        expect(screen.queryByRole('button', { name: 'Apply settings' })).toBeNull();
        expect(screen.getByRole('button', { name: 'Start Solar design' }).disabled).toBe(true);
        changeInput('Drawing units', 'ft');
      }
      await readyProject(seed ? 'Start Solar design' : 'Apply settings');
      const intent = h.client.catalog.mock.calls.at(-1)[0].intents[0];
      fireEvent.click(screen.getByRole('button', { name: seed ? 'Start Solar design' : 'Apply settings' }));
      await waitFor(() => expect(screen.getByTestId('solar-settings-run').textContent).toBe('Solar settings submitted.'));
      expect(h.props.onProjectSubmit.mock.calls[0][0]).toBe(intent);
      const call = h.fetchImpl.mock.calls.find(([url]) => url === '/api/run');
      const request = JSON.parse(call[1].body);
      expect(request).toEqual({ tool: 'solar-settings', params: intent.params,
        dwg: projectBinding.input_version_id, catalog_digest: projectDigest });
      expect(request.params.drawing_id).toBe(projectBinding.drawing_id);
      expect(request.params.expected_rev).toBe(seed ? 0 : 2);
      expect(request).not.toHaveProperty('dwg_version');
      expect(request).not.toHaveProperty('expected_drawing_head');
      expect(request.params).not.toHaveProperty('project_id');
      expect(request.params).not.toHaveProperty('checkout_capability');
      expect(call[1].headers).toMatchObject({ 'X-Org-Id': projectBinding.organization_id,
        'X-Tenant-Id': projectBinding.organization_id, 'X-Project-Id': projectBinding.project_id,
        'Idempotency-Key': intent.idempotencyKey, 'X-Checkout-Capability': 'lco1.' + 'c'.repeat(64) });
      if (seed) expect(request.params.initialize.source_intake_sha256).toBe(SHA);
      expect(h.props.onSubmit).not.toHaveBeenCalled();
      cleanup();
    }
  });
  it('R8-26 reports only R7-validated submitted outcomes and never claims applied settings', async () => {
    for (const data of [{ job_id: projectJob, status: 'submitted' }, { job_id: projectJob },
      { status: 'submitted' }, { job_id: 'not-a-uuid', status: 'submitted' }, { job_id: projectJob, status: 'completed' }]) {
      const h = projectHarness({ run: data });
      await editProject(h);
      await readyProject();
      fireEvent.click(applyButton());
      await waitFor(() => expect(screen.getByTestId('solar-settings-run')).toBeTruthy());
      const outcome = await h.props.onProjectSubmit.mock.results[0].value;
      if (data.job_id === projectJob && data.status === 'submitted') {
        expect(outcome.ok).toBe(true);
        expect(screen.getByTestId('solar-settings-run').textContent).toBe('Solar settings submitted.');
      } else {
        expect(outcome).toEqual({ ok: false, status: null, reason: 'SIP_R7_RESPONSE_INVALID', envelope: null });
        expect(screen.getByTestId('solar-settings-run').textContent).toContain('SIP_R7_RESPONSE_INVALID');
      }
      expect(screen.queryByText('Solar settings applied')).toBeNull();
      expect(h.props.solarBinding.input_version_id).toBe(projectBinding.input_version_id);
      cleanup();
    }
  });
  it('R8-27 retains the exact retry intent and key after uncertainty and checkout loss', async () => {
    const h = projectHarness();
    await h.real.acquireCheckout();
    const uncertain = deferred();
    h.props.onProjectSubmit = vi.fn()
      .mockImplementationOnce(() => uncertain.promise)
      .mockImplementation((intent, options) => h.real.submitRun(intent, { signal: options.signal }));
    await editProject(h);
    await readyProject();
    const intent = h.client.catalog.mock.calls[0][0].intents[0];
    fireEvent.click(applyButton());
    await waitFor(() => expect(h.props.onProjectSubmit).toHaveBeenCalledTimes(1));
    await act(async () => { uncertain.resolve({ ok: false, status: null, reason: 'SIP_R7_TIMEOUT', envelope: null }); });
    await waitFor(() => expect(screen.getByTestId('solar-settings-run').textContent).toContain('SIP_R7_TIMEOUT'));
    expect(h.props.onProjectSubmit).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: 'Refresh preview' }));
    await readyProject();
    expect(h.client.catalog.mock.calls.at(-1)[0].intents[0]).toBe(intent);
    await act(async () => { await h.real.releaseCheckout(); });
    await readyProject();
    expect(h.client.catalog.mock.calls.at(-1)[0].intents[0]).toBe(intent);
    expect(h.props.newProjectIntentKey).toHaveBeenCalledTimes(1);
    fireEvent.click(applyButton());
    await waitFor(() => expect(screen.getByTestId('solar-settings-run').textContent).toBe('Solar settings submitted.'));
    expect(h.props.onProjectSubmit.mock.calls[1][0]).toBe(intent);
    expect(h.props.onProjectSubmit).toHaveBeenCalledTimes(2);
    const run = h.fetchImpl.mock.calls.find(([url]) => url === '/api/run');
    expect(run[1].headers['Idempotency-Key']).toBe(intent.idempotencyKey);
    expect(run[1].headers).not.toHaveProperty('X-Checkout-Capability');
  });
  it('R8-28 aborts and unsubscribes owned StrictMode work without owning the shared client', async () => {
    const pending = deferred();
    const h = projectHarness({ contextResponses: [pending.promise] });
    const view = render(<StrictMode><SolarSettingsForm {...h.props} /></StrictMode>);
    await waitForMode('edit');
    const signals = h.client.readContext.mock.calls.map(([options]) => options.signal);
    expect(signals.length).toBeGreaterThanOrEqual(2);
    expect(signals[0].aborted).toBe(true);
    view.unmount();
    expect(signals.every((signal) => signal.aborted)).toBe(true);
    expect(h.unsubscribes.every((unsubscribe) => unsubscribe.mock.calls.length === 1)).toBe(true);
    await act(async () => { pending.resolve(projectResponse(canonicalView())); });
    for (const method of ['setScope', 'dispose', 'acquireCheckout', 'releaseCheckout']) expect(h.client[method]).not.toHaveBeenCalled();
    expect(h.props.onProjectSubmit).not.toHaveBeenCalled();
    render(<StrictMode><SolarSettingsForm {...h.props} /></StrictMode>);
    await waitForMode('edit');
    expect(screen.queryByTestId('solar-settings-run')).toBeNull();
  });
  it('R8-29 refuses missing canonical dependencies and never falls back to legacy loaders or submission', async () => {
    for (const changes of [{ projectClient: undefined }, { newProjectIntentKey: undefined },
      { onProjectSubmit: undefined }, { solarBinding: undefined, context: { ...C, projectId: projectBinding.project_id } }]) {
      const h = projectHarness();
      const supplied = { ...h.props, ...changes };
      render(<SolarSettingsForm {...supplied} />);
      await waitFor(() => expect(screen.getByTestId('solar-settings-reason').textContent)
        .toBe(changes.solarBinding === undefined && Object.hasOwn(changes, 'solarBinding')
          ? PROJECT_SETTINGS_REASONS.binding_invalid : PROJECT_SETTINGS_REASONS.submit_unavailable));
      const button = screen.queryByRole('button', { name: 'Apply settings' });
      if (button) { expect(button.disabled).toBe(true); fireEvent.click(button); }
      expect(h.props.readIntake).not.toHaveBeenCalled();
      expect(h.props.readVersions).not.toHaveBeenCalled();
      expect(h.props.onSubmit).not.toHaveBeenCalled();
      expect(h.props.onProjectSubmit).not.toHaveBeenCalled();
      expect(h.client.prepareRun).not.toHaveBeenCalled();
      cleanup();
    }
    for (const [changes, reason] of [
      [{ catalogDigest: undefined }, 'catalog_digest_required'],
      [{ catalogDigest: 'not-a-digest' }, 'catalog_digest_required'],
      [{ newProjectIntentKey: () => '' }, 'idempotency_key_required'],
      [{ newProjectIntentKey: () => 'é'.repeat(129) }, 'idempotency_key_required'],
    ]) {
      const h = projectHarness();
      h.props = { ...h.props, ...changes };
      await editProject(h);
      await waitFor(() => expect(screen.getByTestId('solar-settings-reason').textContent).toBe(PROJECT_SETTINGS_REASONS[reason]));
      expect(applyButton().disabled).toBe(true);
      expect(h.client.catalog).not.toHaveBeenCalled();
      expect(h.client.prepareRun).not.toHaveBeenCalled();
      expect(h.props.onProjectSubmit).not.toHaveBeenCalled();
      cleanup();
    }
  });
});


const C = { drawingId: 'd1', drawingVersion: 3, projectId: null };
const P = { name: 'Roof A', zip_code: '44224', latitude: 41, longitude: -81 };
const S = {
  panel_layer_contains: 'Panel', panel_group_layer: 'Panel Group',
  string_layer: 'String', home_run_layer: 'HomeRun', panels_in_sequence: 3,
  num_mppt: 2, strings_per_mppt: 0, optimizer_ratio: 1, use_l2_collectors: false,
  panel_group_number: 1, string_number: 1, inverter_number: 1, mppt_letter: 'A',
  global_string_sizing_confirmed: false, extra: {},
};
const IE = {
  version: 3, head: 3, latest: 3,
  intake: {
    polylines: [], solar_design_graph: { rev: 2, settings: S, project: P },
    solar_design_graph_sha256: 'b'.repeat(64),
  },
};
const IG = { version: 3, head: 3, latest: 3, intake: { polylines: [] } };
const V = {
  drawing_id: 'd1', head: 3, latest: 3,
  versions: [
    { v: 2, parent: 1, sha256: 'c'.repeat(64), note: null },
    { v: 3, parent: 2, sha256: SHA, note: null },
  ],
};

function props(overrides = {}) {
  return {
    context: C, readIntake: vi.fn().mockResolvedValue(IE), readVersions: vi.fn().mockResolvedValue(V),
    checkoutHeld: true, busy: false, onSubmit: vi.fn(), onClose: vi.fn(), ...overrides,
  };
}

function deferred() {
  let resolvePromise;
  let rejectPromise;
  const promise = new Promise((resolveValue, rejectValue) => {
    resolvePromise = resolveValue;
    rejectPromise = rejectValue;
  });
  return { promise, resolve: resolvePromise, reject: rejectPromise };
}

async function waitForMode(mode) {
  await waitFor(() => expect(screen.getByTestId('solar-settings-form').getAttribute('data-mode')).toBe(mode));
}

afterEach(cleanup);

const consequence = 'Re-size strings after this change';
const blankZip = 'ZIP code is blank. You can save setup. Cloud string sizing needs a saved ZIP code.';
const clearingZip = 'Changing the ZIP code clears saved coordinates unless you enter both coordinates again.';
const changeInput = (label, value) => fireEvent.change(screen.getByLabelText(label), { target: { value } });
const applyButton = () => screen.getByRole('button', { name: 'Apply settings' });
const l2 = () => screen.getByRole('checkbox', { name: 'L2 collectors' });
const withMode = (mode) => ({
  ...IE,
  intake: { ...IE.intake, solar_design_graph: { rev: 2, settings: { ...S, use_l2_collectors: mode }, project: P } },
});
const refusedEnvelope = (code) => ({
  ok: false,
  reason_code: code,
  error: {
    actor: 'user', error_code: 'BAD_PARAMS', message: code,
    next_action: 'Review the inputs, correct them, and submit again.', retry_class: 'after_action', retryable: false,
  },
});

describe('Solar project settings', () => {
  it('PJ17 omits the ZIP clearing warning for an untouched normalized blank ZIP', async () => {
    const intake = { ...IE, intake: { ...IE.intake, solar_design_graph: { rev: 2, settings: S, project: { ...P, zip_code: '\u0085' } } } };
    const supplied = props({ readIntake: vi.fn().mockResolvedValue(intake) });
    render(<SolarSettingsForm {...supplied} />);
    await waitForMode('edit');
    changeInput('MPPT count', '4');
    expect(screen.queryByText(clearingZip)).toBeNull();
    expect(applyButton().disabled).toBe(false);
    fireEvent.click(applyButton());
    expect(supplied.onSubmit).toHaveBeenCalledExactlyOnceWith({ expected_rev: 2, changes: { num_mppt: 4 } });
  });

  it('PJ16 keeps the ZIP clearing warning for normalized coordinate no-ops', async () => {
    const supplied = props();
    render(<SolarSettingsForm {...supplied} />);
    await waitForMode('edit');
    changeInput('ZIP code', '37601');
    expect(screen.getByText(clearingZip)).toBeTruthy();
    changeInput('Latitude', '41.0');
    changeInput('Longitude', '-81.0');
    expect(screen.getByText(clearingZip)).toBeTruthy();
    expect(applyButton().disabled).toBe(false);
    fireEvent.click(applyButton());
    expect(supplied.onSubmit).toHaveBeenCalledExactlyOnceWith({ expected_rev: 2, project_changes: { zip_code: '37601' } });
  });

  it('PJ1 initializes from project fields alone', async () => {
    const supplied = props({ readIntake: vi.fn().mockResolvedValue(IG) });
    render(<SolarSettingsForm {...supplied} />);
    await waitForMode('initialize');
    expect(screen.getByLabelText('Project name').value).toBe('');
    expect(screen.getByLabelText('Latitude').value).toBe('');
    expect(screen.getByText(blankZip)).toBeTruthy();
    changeInput('Project name', ' Roof A ');
    changeInput('ZIP code', '44224');
    changeInput('Drawing units', 'ft');
    expect(screen.getByText(consequence)).toBeTruthy();
    const button = screen.getByRole('button', { name: 'Start Solar design' });
    expect(button.disabled).toBe(false);
    fireEvent.click(button);
    expect(supplied.onSubmit).toHaveBeenCalledExactlyOnceWith({
      expected_rev: 0,
      initialize: {
        schema_version: 1, source_intake_sha256: SHA,
        units: {
          drawing_units: 'ft', wcs_to_ucs: [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
          elevation_datum: 'unknown', crs: null,
        },
      },
      project_changes: { name: 'Roof A', zip_code: '44224' },
    });
  });

  it.each([
    ['PJ2 name-only change', [['Project name', ' Roof B ']], { name: 'Roof B' }, null],
    ['PJ3 blank ZIP remains saveable', [['ZIP code', '']], { zip_code: '' }, blankZip],
    ['PJ4 ZIP-only change omits coordinates', [['ZIP code', '37601']], { zip_code: '37601' }, clearingZip],
    ['PJ6 combined coordinates and settings', [['Latitude', '40.5'], ['Longitude', '-80.25'], ['MPPT count', '4']], { latitude: 40.5, longitude: -80.25 }, null],
  ])('%s', async (_name, inputs, project_changes, message) => {
    const supplied = props();
    render(<SolarSettingsForm {...supplied} />);
    await waitForMode('edit');
    expect(screen.getByLabelText('Project name').value).toBe('Roof A');
    expect(screen.getByLabelText('ZIP code').value).toBe('44224');
    expect(screen.getByLabelText('Latitude').value).toBe('41');
    expect(screen.getByLabelText('Longitude').value).toBe('-81');
    for (const [label, value] of inputs) changeInput(label, value);
    if (message) expect(screen.getByText(message)).toBeTruthy();
    expect(screen.getByText(consequence)).toBeTruthy();
    expect(applyButton().disabled).toBe(false);
    fireEvent.click(applyButton());
    expect(supplied.onSubmit).toHaveBeenCalledExactlyOnceWith({
      expected_rev: 2, ...(inputs.some(([label]) => label === 'MPPT count') ? { changes: { num_mppt: 4 } } : {}), project_changes,
    });
  });

  it('PJ5 clears both coordinates explicitly', async () => {
    const supplied = props();
    render(<SolarSettingsForm {...supplied} />);
    await waitForMode('edit');
    fireEvent.click(screen.getByRole('button', { name: 'Clear coordinates' }));
    expect(screen.getByLabelText('Latitude').value).toBe('');
    expect(screen.getByLabelText('Longitude').value).toBe('');
    fireEvent.click(applyButton());
    expect(supplied.onSubmit).toHaveBeenCalledExactlyOnceWith({ expected_rev: 2, project_changes: { latitude: null, longitude: null } });
  });

  it('PJ7 refuses a blank supplied name and keeps the draft', async () => {
    const supplied = props();
    render(<SolarSettingsForm {...supplied} />);
    await waitForMode('edit');
    changeInput('Project name', '   ');
    expect(screen.getByTestId('solar-settings-reason').textContent).toBe('Enter a project name before saving this change.');
    expect(applyButton().disabled).toBe(true);
    fireEvent.click(applyButton());
    expect(supplied.onSubmit).not.toHaveBeenCalled();
    expect(screen.getByLabelText('Project name').value).toBe('   ');
  });

  it('PJ8 refuses malformed and overlength ZIP codes', async () => {
    const supplied = props();
    render(<SolarSettingsForm {...supplied} />);
    await waitForMode('edit');
    for (const [value, sentence] of [
      ['1234', 'Enter a five-digit ZIP code or ZIP+4, or leave it blank.'],
      ['12345-67890', 'Check the project fields and their length limits.'],
    ]) {
      changeInput('ZIP code', value);
      expect(screen.getByTestId('solar-settings-reason').textContent).toBe(sentence);
      expect(applyButton().disabled).toBe(true);
      fireEvent.click(applyButton());
    }
    expect(supplied.onSubmit).not.toHaveBeenCalled();
  });

  it('PJ9 counts name length in Unicode code points', async () => {
    const supplied = props();
    render(<SolarSettingsForm {...supplied} />);
    await waitForMode('edit');
    const name = '\u{1F600}'.repeat(4096);
    changeInput('Project name', name);
    expect(applyButton().disabled).toBe(false);
    fireEvent.click(applyButton());
    expect(supplied.onSubmit).toHaveBeenCalledExactlyOnceWith({ expected_rev: 2, project_changes: { name } });
    changeInput('Project name', name + '\u{1F600}');
    expect(screen.getByTestId('solar-settings-reason').textContent).toBe('Check the project fields and their length limits.');
    expect(applyButton().disabled).toBe(true);
    fireEvent.click(applyButton());
    expect(supplied.onSubmit).toHaveBeenCalledTimes(1);
  });

  it('PJ10 refuses out of range, half-empty and exponent coordinates', async () => {
    const supplied = props();
    render(<SolarSettingsForm {...supplied} />);
    await waitForMode('edit');
    for (const [latitude, longitude] of [['91', '0'], ['0', ''], ['1e2', '0']]) {
      changeInput('Latitude', latitude);
      changeInput('Longitude', longitude);
      expect(screen.getByTestId('solar-settings-reason').textContent).toBe('Enter both coordinates within their allowed ranges, or clear both.');
      expect(applyButton().disabled).toBe(true);
      fireEvent.click(applyButton());
    }
    expect(supplied.onSubmit).not.toHaveBeenCalled();
  });

  it('PJ11 normalized no-op has no sizing consequence', async () => {
    const supplied = props();
    render(<SolarSettingsForm {...supplied} />);
    await waitForMode('edit');
    changeInput('Project name', ' Roof A ');
    expect(screen.queryByText(consequence)).toBeNull();
    expect(applyButton().disabled).toBe(true);
    expect(screen.getByTestId('solar-settings-reason').textContent).toBe('Change at least one project field or setting before you submit.');
    fireEvent.click(applyButton());
    expect(supplied.onSubmit).not.toHaveBeenCalled();
  });

  it('PJ12 locks duplicate submissions before the parent renders', async () => {
    const supplied = props();
    render(<SolarSettingsForm {...supplied} />);
    await waitForMode('edit');
    changeInput('Project name', 'Roof B');
    act(() => {
      fireEvent.click(applyButton());
      fireEvent.click(applyButton());
    });
    expect(supplied.onSubmit).toHaveBeenCalledExactlyOnceWith({ expected_rev: 2, project_changes: { name: 'Roof B' } });
  });

  it('PJ13 refuses an incomplete saved coordinate pair', async () => {
    const intake = { ...IE, intake: { ...IE.intake, solar_design_graph: { rev: 2, settings: S, project: { ...P, name: 'A', longitude: null } } } };
    const supplied = props({ readIntake: vi.fn().mockResolvedValue(intake) });
    render(<SolarSettingsForm {...supplied} />);
    await waitForMode('refused');
    expect(screen.getByTestId('solar-settings-reason').textContent).toBe(SOLAR_SETTINGS_REASONS.graph_unreadable);
    expect(screen.queryAllByRole('textbox')).toHaveLength(0);
    expect(supplied.onSubmit).not.toHaveBeenCalled();
  });

  it('PJ14 allows applying again after confirmation dismissal and a parent render', async () => {
    const supplied = props();
    const view = render(<SolarSettingsForm {...supplied} />);
    await waitForMode('edit');
    changeInput('Project name', 'Roof B');
    fireEvent.click(applyButton());
    expect(supplied.onSubmit).toHaveBeenCalledExactlyOnceWith({ expected_rev: 2, project_changes: { name: 'Roof B' } });
    view.rerender(<SolarSettingsForm {...supplied} />);
    fireEvent.click(applyButton());
    expect(supplied.onSubmit).toHaveBeenCalledTimes(2);
    expect(supplied.onSubmit.mock.calls[1][0]).toEqual(supplied.onSubmit.mock.calls[0][0]);
  });

  it('releases the lock on settled busy state and failure without losing drafts', async () => {
    const supplied = props();
    const view = render(<SolarSettingsForm {...supplied} />);
    await waitForMode('edit');
    changeInput('Project name', 'Roof B');
    fireEvent.click(applyButton());
    view.rerender(<SolarSettingsForm {...supplied} busy />);
    view.rerender(<SolarSettingsForm {...supplied} busy={false} />);
    fireEvent.click(applyButton());
    expect(supplied.onSubmit).toHaveBeenCalledTimes(2);
    view.rerender(<SolarSettingsForm {...supplied} runMessage={{ text: 'Solar settings were not applied.', code: null }} />);
    expect(screen.getByLabelText('Project name').value).toBe('Roof B');
    fireEvent.click(applyButton());
    fireEvent.click(applyButton());
    expect(supplied.onSubmit).toHaveBeenCalledTimes(3);
  });

  it('releases the lock and resets project drafts when the drawing changes', async () => {
    const supplied = props();
    const view = render(<SolarSettingsForm {...supplied} />);
    await waitForMode('edit');
    changeInput('Project name', 'Roof B');
    fireEvent.click(applyButton());
    view.rerender(<SolarSettingsForm {...supplied} context={{ ...C, drawingId: 'd2' }} />);
    await waitForMode('edit');
    expect(screen.getByLabelText('Project name').value).toBe('Roof A');
    changeInput('Project name', 'Roof C');
    fireEvent.click(applyButton());
    expect(supplied.onSubmit).toHaveBeenCalledTimes(2);
    expect(supplied.onSubmit).toHaveBeenLastCalledWith({ expected_rev: 2, project_changes: { name: 'Roof C' } });
  });
});

describe('Solar settings form', () => {
  it('SF2 row29 the form shows its run message and keeps the drafts', async () => {
    const M = 'This saved drawing version cannot be used to start a Solar design.';
    const supplied = props();
    const view = render(<SolarSettingsForm {...supplied} />);
    await waitForMode('edit');
    fireEvent.change(screen.getByLabelText('MPPT count'), { target: { value: '4' } });
    view.rerender(<SolarSettingsForm {...supplied} runMessage={{ text: M, code: 'INVALID_SEED_PARENT' }} />);
    expect(screen.getByTestId('solar-settings-run').textContent).toBe(`${M} (INVALID_SEED_PARENT)`);
    expect(screen.getByTestId('solar-settings-run').getAttribute('role')).toBe('status');
    expect(screen.getByLabelText('MPPT count').value).toBe('4');
    expect(supplied.readIntake).toHaveBeenCalledTimes(1);
    expect(supplied.readVersions).toHaveBeenCalledTimes(1);
  });

  it('SF2 row39 no run message renders nothing new', async () => {
    const supplied = props();
    const view = render(<SolarSettingsForm {...supplied} />);
    await waitForMode('edit');
    expect(screen.queryByTestId('solar-settings-run')).toBeNull();
    view.rerender(<SolarSettingsForm {...supplied} runMessage={null} />);
    expect(screen.queryByTestId('solar-settings-run')).toBeNull();
    expect(screen.getByTestId('solar-settings-reason').textContent).toBe(SOLAR_SETTINGS_REASONS.no_changes);
  });

  it('reads the intake and versions for the context version and shows the graph revision', async () => {
    const supplied = props();
    render(<SolarSettingsForm {...supplied} />);
    expect(screen.getByTestId('solar-settings-status').textContent).toBe('Reading the drawing.');
    await waitForMode('edit');
    expect(supplied.readIntake).toHaveBeenCalledWith('d1', 3);
    expect(supplied.readVersions).toHaveBeenCalledWith('d1');
    expect(screen.getByText('Graph revision 2')).toBeTruthy();
    expect(screen.getByRole('region', { name: 'Solar settings' })).toBeTruthy();
    expect(screen.getAllByRole('textbox')).toHaveLength(16);
    expect(screen.getByLabelText('MPPT count').value).toBe('2');
    expect(screen.queryByLabelText('Drawing units')).toBeNull();
    expect(screen.getByRole('button', { name: 'Apply settings' }).disabled).toBe(true);
    expect(screen.getByTestId('solar-settings-reason').textContent).toBe(SOLAR_SETTINGS_REASONS.no_changes);
    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(supplied.onClose).toHaveBeenCalledTimes(1);
  });

  it('submits expected_rev and only the changed fields', async () => {
    const intake = {
      ...IE,
      intake: { ...IE.intake, solar_design_graph: { rev: 2, settings: { ...S, optimizer_ratio: 5e-7 }, project: P } },
    };
    const supplied = props({ readIntake: vi.fn().mockResolvedValue(intake) });
    render(<SolarSettingsForm {...supplied} />);
    await waitForMode('edit');
    expect(screen.getByLabelText('Optimizer ratio').value).toBe('5e-7');
    expect(screen.getByLabelText('Optimizer ratio').hasAttribute('aria-invalid')).toBe(false);
    fireEvent.change(screen.getByLabelText('MPPT count'), { target: { value: '4' } });
    expect(screen.queryByTestId('solar-settings-reason')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Apply settings' }));
    expect(supplied.onSubmit).toHaveBeenCalledTimes(1);
    expect(supplied.onSubmit).toHaveBeenCalledWith({ expected_rev: 2, changes: { num_mppt: 4 } });
  });

  it('offers the units step on a graphless head and submits initialize', async () => {
    const supplied = props({ readIntake: vi.fn().mockResolvedValue(IG) });
    render(<SolarSettingsForm {...supplied} />);
    await waitForMode('initialize');
    expect(screen.getByLabelText('Elevation datum').value).toBe('unknown');
    expect(screen.getByLabelText('Coordinate system').value).toBe('');
    expect([...screen.getByLabelText('Drawing units').options].map((option) => option.value))
      .toEqual(['', 'm', 'mm', 'cm', 'km', 'in', 'ft', 'yd']);
    fireEvent.change(screen.getByLabelText('Panels in sequence'), { target: { value: '3' } });
    expect(screen.getByTestId('solar-settings-reason').textContent).toBe(SOLAR_SETTINGS_REASONS.units_required);
    fireEvent.change(screen.getByLabelText('Drawing units'), { target: { value: 'ft' } });
    fireEvent.click(screen.getByRole('button', { name: 'Start Solar design' }));
    expect(supplied.onSubmit).toHaveBeenCalledTimes(1);
    expect(supplied.onSubmit).toHaveBeenCalledWith({
      expected_rev: 0,
      changes: { panels_in_sequence: 3 },
      initialize: {
        schema_version: 1, source_intake_sha256: SHA,
        units: {
          drawing_units: 'ft',
          wcs_to_ucs: [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
          elevation_datum: 'unknown', crs: null,
        },
      },
    });
  });

  it('disables submit with the checkout reason when the checkout is not held', async () => {
    const supplied = props({ checkoutHeld: false });
    const view = render(<SolarSettingsForm {...supplied} />);
    await waitForMode('edit');
    fireEvent.change(screen.getByLabelText('MPPT count'), { target: { value: '4' } });
    expect(screen.getByTestId('solar-settings-reason').textContent).toBe(SOLAR_SETTINGS_REASONS.checkout_required);
    const button = screen.getByRole('button', { name: 'Apply settings' });
    expect(button.disabled).toBe(true);
    fireEvent.click(button);
    expect(supplied.onSubmit).not.toHaveBeenCalled();
    view.rerender(<SolarSettingsForm {...supplied} busy />);
    expect(screen.getByTestId('solar-settings-reason').textContent).toBe(SOLAR_SETTINGS_REASONS.run_in_progress);
    expect(button.disabled).toBe(true);
  });

  it('shows the refusal sentence when the drawing cannot be read', async () => {
    const supplied = props({ readIntake: vi.fn().mockRejectedValue(new Error('read failed')), busy: true, checkoutHeld: false });
    render(<SolarSettingsForm {...supplied} />);
    await waitForMode('refused');
    expect(screen.getByTestId('solar-settings-reason').textContent).toBe(SOLAR_SETTINGS_REASONS.drawing_unreadable);
    expect(supplied.onSubmit).not.toHaveBeenCalled();
  });

  it('ignores a response for a drawing version that is no longer current', async () => {
    const oldIntake = deferred();
    const oldVersions = deferred();
    const nextIntake = {
      ...IE, version: 4, head: 4, latest: 4,
      intake: { ...IE.intake, solar_design_graph: { rev: 7, settings: { ...S, num_mppt: 8 }, project: P } },
    };
    const nextVersions = { ...V, head: 4, latest: 4, versions: [...V.versions, { v: 4, parent: 3, sha256: SHA, note: null }] };
    const supplied = props({
      readIntake: vi.fn().mockImplementationOnce(() => oldIntake.promise).mockResolvedValue(nextIntake),
      readVersions: vi.fn().mockImplementationOnce(() => oldVersions.promise).mockResolvedValue(nextVersions),
    });
    const view = render(<SolarSettingsForm {...supplied} />);
    await waitFor(() => expect(supplied.readIntake).toHaveBeenCalledWith('d1', 3));
    view.rerender(<SolarSettingsForm {...supplied} context={{ ...C, drawingVersion: 4 }} />);
    await waitForMode('edit');
    expect(screen.getByText('Graph revision 7')).toBeTruthy();
    await act(async () => {
      oldIntake.resolve(IE);
      oldVersions.resolve(V);
      await Promise.all([oldIntake.promise, oldVersions.promise]);
    });
    expect(screen.getByText('Graph revision 7')).toBeTruthy();
    expect(screen.queryByText('Graph revision 2')).toBeNull();
    expect(screen.getByLabelText('MPPT count').value).toBe('8');
    expect(supplied.readIntake).toHaveBeenLastCalledWith('d1', 4);
    fireEvent.change(screen.getByLabelText('MPPT count'), { target: { value: '9' } });
    view.rerender(<SolarSettingsForm {...supplied} context={{ ...C, drawingId: 'd2', drawingVersion: 4 }} />);
    await waitForMode('edit');
    expect(screen.getByLabelText('MPPT count').value).toBe('8');
    expect(screen.getByTestId('solar-settings-reason').textContent).toBe(SOLAR_SETTINGS_REASONS.no_changes);
  });

  it('L2F1 L2 collectors is an editable checkbox with its note', async () => {
    render(<SolarSettingsForm {...props()} />);
    await waitForMode('edit');
    expect(screen.getAllByRole('checkbox')).toHaveLength(1);
    expect(l2().disabled).toBe(false);
    expect(l2().checked).toBe(false);
    expect(screen.getByText(L2_COLLECTORS_NOTE)).toBeTruthy();
    expect(screen.queryByText('L2 collectors are not supported yet, so this setting stays off.')).toBeNull();
    expect(screen.getAllByRole('textbox')).toHaveLength(16);
  });

  it('L2F2 entering L2 submits only the mode', async () => {
    const supplied = props();
    render(<SolarSettingsForm {...supplied} />);
    await waitForMode('edit');
    fireEvent.click(l2());
    expect(l2().checked).toBe(true);
    expect(screen.getByText(consequence)).toBeTruthy();
    expect(screen.queryByTestId('solar-settings-reason')).toBeNull();
    fireEvent.click(applyButton());
    expect(supplied.onSubmit).toHaveBeenCalledExactlyOnceWith({ expected_rev: 2, changes: { use_l2_collectors: true } });
    expect(supplied.onSubmit.mock.calls[0][0].changes.use_l2_collectors).toBe(true);
  });

  it('L2F3 toggling back to the saved mode sends nothing', async () => {
    const supplied = props();
    render(<SolarSettingsForm {...supplied} />);
    await waitForMode('edit');
    fireEvent.click(l2());
    fireEvent.click(l2());
    expect(l2().checked).toBe(false);
    expect(screen.queryByText(consequence)).toBeNull();
    expect(screen.getByTestId('solar-settings-reason').textContent).toBe(SOLAR_SETTINGS_REASONS.no_changes);
    expect(applyButton().disabled).toBe(true);
    fireEvent.click(applyButton());
    expect(supplied.onSubmit).not.toHaveBeenCalled();
    changeInput('MPPT count', '4');
    fireEvent.click(applyButton());
    expect(supplied.onSubmit).toHaveBeenCalledExactlyOnceWith({ expected_rev: 2, changes: { num_mppt: 4 } });
  });

  it('L2F4 a saved L2 drawing shows the box checked and leaving sends false', async () => {
    const supplied = props({ readIntake: vi.fn().mockResolvedValue(withMode(true)) });
    render(<SolarSettingsForm {...supplied} />);
    await waitForMode('edit');
    expect(l2().checked).toBe(true);
    expect(applyButton().disabled).toBe(true);
    fireEvent.click(l2());
    expect(l2().checked).toBe(false);
    fireEvent.click(applyButton());
    expect(supplied.onSubmit).toHaveBeenCalledExactlyOnceWith({ expected_rev: 2, changes: { use_l2_collectors: false } });
    expect(supplied.onSubmit.mock.calls[0][0].changes.use_l2_collectors).toBe(false);
  });

  it('L2F5 the mode rides with another setting in field order', async () => {
    const supplied = props();
    render(<SolarSettingsForm {...supplied} />);
    await waitForMode('edit');
    fireEvent.click(l2());
    changeInput('MPPT letter', 'B');
    changeInput('MPPT count', '4');
    fireEvent.click(applyButton());
    expect(supplied.onSubmit).toHaveBeenCalledExactlyOnceWith({
      expected_rev: 2, changes: { num_mppt: 4, use_l2_collectors: true, mppt_letter: 'B' },
    });
    expect(Object.keys(supplied.onSubmit.mock.calls[0][0].changes)).toEqual(['num_mppt', 'use_l2_collectors', 'mppt_letter']);
  });

  // The two envelopes are the server's own answers, measured over POST /api/run?wait=1 on Forge main
  // 4fdb411a (HTTP 400, head unchanged): leaving L2 on a drawing with a combiner box and a central
  // inverter, and entering L2 on a drawing whose two inverters share a number.
  it.each([
    ['leaving with L2 equipment', true, 'DESIGN_PRESET_L2_EQUIPMENT_PRESENT'],
    ['entering with duplicate inverter numbers', false, 'DUPLICATE_EQUIPMENT_NUMBER'],
  ])('L2F6 the server refusal for %s shows through the run message and keeps the draft', async (_name, saved, code) => {
    const supplied = props({ readIntake: vi.fn().mockResolvedValue(withMode(saved)) });
    const view = render(<SolarSettingsForm {...supplied} />);
    await waitForMode('edit');
    fireEvent.click(l2());
    fireEvent.click(applyButton());
    expect(supplied.onSubmit).toHaveBeenCalledExactlyOnceWith({ expected_rev: 2, changes: { use_l2_collectors: !saved } });
    const runMessage = solarSettingsRunFeedback({
      association: { intentId: 'i1', drawingId: 'd1', drawingVersion: 3, envelope: refusedEnvelope(code) }, context: C,
    });
    expect(runMessage).toEqual({ text: 'Solar settings were not applied.', code });
    view.rerender(<SolarSettingsForm {...supplied} runMessage={runMessage} />);
    expect(screen.getByTestId('solar-settings-run').textContent).toBe(`Solar settings were not applied. (${code})`);
    expect(l2().checked).toBe(!saved);
    expect(screen.queryByTestId('solar-settings-reason')).toBeNull();
    expect(applyButton().disabled).toBe(false);
    expect(supplied.readIntake).toHaveBeenCalledTimes(1);
    fireEvent.click(applyButton());
    expect(supplied.onSubmit).toHaveBeenCalledTimes(2);
    expect(supplied.onSubmit.mock.calls[1][0]).toEqual(supplied.onSubmit.mock.calls[0][0]);
  });

  it('L2F7 a graphless head can start the design in L2 mode', async () => {
    const supplied = props({ readIntake: vi.fn().mockResolvedValue(IG) });
    render(<SolarSettingsForm {...supplied} />);
    await waitForMode('initialize');
    expect(l2().checked).toBe(false);
    fireEvent.click(l2());
    expect(screen.getByTestId('solar-settings-reason').textContent).toBe(SOLAR_SETTINGS_REASONS.units_required);
    changeInput('Drawing units', 'ft');
    fireEvent.click(screen.getByRole('button', { name: 'Start Solar design' }));
    expect(supplied.onSubmit).toHaveBeenCalledExactlyOnceWith({
      expected_rev: 0,
      changes: { use_l2_collectors: true },
      initialize: {
        schema_version: 1, source_intake_sha256: SHA,
        units: {
          drawing_units: 'ft', wcs_to_ucs: [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
          elevation_datum: 'unknown', crs: null,
        },
      },
    });
  });

  it('L2F8 the checkout and busy reasons still gate a mode change', async () => {
    const supplied = props({ checkoutHeld: false });
    const view = render(<SolarSettingsForm {...supplied} />);
    await waitForMode('edit');
    fireEvent.click(l2());
    expect(l2().checked).toBe(true);
    expect(screen.getByTestId('solar-settings-reason').textContent).toBe(SOLAR_SETTINGS_REASONS.checkout_required);
    expect(applyButton().disabled).toBe(true);
    fireEvent.click(applyButton());
    view.rerender(<SolarSettingsForm {...supplied} checkoutHeld busy />);
    expect(screen.getByTestId('solar-settings-reason').textContent).toBe(SOLAR_SETTINGS_REASONS.run_in_progress);
    fireEvent.click(applyButton());
    expect(supplied.onSubmit).not.toHaveBeenCalled();
    expect(l2().checked).toBe(true);
  });

  it.each([1, 'true', null])('L2F9 a served mode that is not a boolean refuses the form and renders no checkbox %j', async (mode) => {
    const supplied = props({ readIntake: vi.fn().mockResolvedValue(withMode(mode)) });
    render(<SolarSettingsForm {...supplied} />);
    await waitForMode('refused');
    expect(screen.getByTestId('solar-settings-reason').textContent).toBe(SOLAR_SETTINGS_REASONS.graph_unreadable);
    expect(screen.queryAllByRole('checkbox')).toHaveLength(0);
  });

  it('L2F10 the mode draft resets when the drawing changes', async () => {
    const supplied = props();
    const view = render(<SolarSettingsForm {...supplied} />);
    await waitForMode('edit');
    fireEvent.click(l2());
    expect(l2().checked).toBe(true);
    view.rerender(<SolarSettingsForm {...supplied} context={{ ...C, drawingId: 'd2' }} />);
    await waitForMode('edit');
    expect(l2().checked).toBe(false);
    expect(screen.getByTestId('solar-settings-reason').textContent).toBe(SOLAR_SETTINGS_REASONS.no_changes);
  });

  it('marks an out of range field invalid and names the reason', async () => {
    const supplied = props();
    render(<SolarSettingsForm {...supplied} />);
    await waitForMode('edit');
    const input = screen.getByLabelText('Panels in sequence');
    expect(input.hasAttribute('aria-invalid')).toBe(false);
    fireEvent.change(input, { target: { value: '1000001' } });
    expect(input.getAttribute('aria-invalid')).toBe('true');
    expect(screen.getByTestId('solar-settings-reason').textContent).toBe(SOLAR_SETTINGS_REASONS.invalid_value);
    expect(screen.getByRole('button', { name: 'Apply settings' }).disabled).toBe(true);
    expect(supplied.onSubmit).not.toHaveBeenCalled();
  });

  it('imports no transport module', () => {
    for (const filename of ['solarSettingsModel.js', 'SolarSettingsForm.jsx']) {
      const source = readFileSync(resolve(process.cwd(), 'src', 'solar', filename), 'utf8');
      for (const forbidden of ['api.js', 'fetch(', 'XMLHttpRequest', 'localStorage']) {
        expect(source).not.toContain(forbidden);
      }
    }
  });
});
