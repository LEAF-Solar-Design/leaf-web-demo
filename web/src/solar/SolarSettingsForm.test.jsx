import React from 'react';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import SolarSettingsForm from './SolarSettingsForm.jsx';
import { SOLAR_SETTINGS_REASONS } from './solarSettingsModel.js';

const SHA = 'a'.repeat(64);
const C = { drawingId: 'd1', drawingVersion: 3, projectId: null };
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
    polylines: [], solar_design_graph: { rev: 2, settings: S },
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

describe('Solar settings form', () => {
  it('reads the intake and versions for the context version and shows the graph revision', async () => {
    const supplied = props();
    render(<SolarSettingsForm {...supplied} />);
    expect(screen.getByTestId('solar-settings-status').textContent).toBe('Reading the drawing.');
    await waitForMode('edit');
    expect(supplied.readIntake).toHaveBeenCalledWith('d1', 3);
    expect(supplied.readVersions).toHaveBeenCalledWith('d1');
    expect(screen.getByText('Graph revision 2')).toBeTruthy();
    expect(screen.getByRole('region', { name: 'Solar settings' })).toBeTruthy();
    expect(screen.getAllByRole('textbox')).toHaveLength(12);
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
      intake: { ...IE.intake, solar_design_graph: { rev: 2, settings: { ...S, optimizer_ratio: 5e-7 } } },
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
      intake: { ...IE.intake, solar_design_graph: { rev: 7, settings: { ...S, num_mppt: 8 } } },
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
