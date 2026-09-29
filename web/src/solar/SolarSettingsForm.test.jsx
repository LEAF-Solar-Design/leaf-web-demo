import React from 'react';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import SolarSettingsForm from './SolarSettingsForm.jsx';
import { SOLAR_SETTINGS_REASONS } from './solarSettingsModel.js';

const SHA = 'a'.repeat(64);
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
  const promise = new Promise((resolveValue) => { resolvePromise = resolveValue; });
  return { promise, resolve: resolvePromise };
}

async function waitForMode(mode) {
  await waitFor(() => expect(screen.getByTestId('solar-settings-form').getAttribute('data-mode')).toBe(mode));
}

afterEach(cleanup);

const consequence = 'Re-size strings after this change';
const blankZip = 'ZIP code is blank. You can save setup, but string sizing is unavailable until you save a ZIP code.';
const clearingZip = 'Changing the ZIP code clears saved coordinates unless you enter both coordinates again.';
const changeInput = (label, value) => fireEvent.change(screen.getByLabelText(label), { target: { value } });
const applyButton = () => screen.getByRole('button', { name: 'Apply settings' });

describe('Solar project settings', () => {
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

  it('keeps L2 collectors off and says why', async () => {
    render(<SolarSettingsForm {...props()} />);
    await waitForMode('edit');
    const checkbox = screen.getByRole('checkbox', { name: 'L2 collectors' });
    expect(checkbox.disabled).toBe(true);
    expect(checkbox.checked).toBe(false);
    expect(screen.getByText(SOLAR_SETTINGS_REASONS.l2_collectors_unavailable)).toBeTruthy();
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
