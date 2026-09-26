import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import {
  SETTINGS_FIELDS, SEED_SETTINGS, DRAWING_UNITS, IDENTITY_WCS_TO_UCS,
  SOLAR_SETTINGS_REASONS, deriveFormState, buildSettingsParams,
} from './solarSettingsModel.js';

const SHA = 'a'.repeat(64);
const C = { drawingId: 'd1', drawingVersion: 3, projectId: null };
const settings = {
  panel_layer_contains: 'Panel', panel_group_layer: 'Panel Group',
  string_layer: 'String', home_run_layer: 'HomeRun', panels_in_sequence: 3,
  num_mppt: 2, strings_per_mppt: 0, optimizer_ratio: 1, use_l2_collectors: false,
  panel_group_number: 1, string_number: 1, inverter_number: 1, mppt_letter: 'A',
};
const S = { ...settings, global_string_sizing_confirmed: false, extra: {} };
const G = { rev: 2, settings: S };
const IE = {
  version: 3, head: 3, latest: 3,
  intake: { polylines: [], solar_design_graph: G, solar_design_graph_sha256: 'b'.repeat(64) },
};
const IG = { version: 3, head: 3, latest: 3, intake: { polylines: [] } };
const V = {
  drawing_id: 'd1', head: 3, latest: 3,
  versions: [
    { v: 2, parent: 1, sha256: 'c'.repeat(64), note: null },
    { v: 3, parent: 2, sha256: SHA, note: null },
  ],
};
const E = { mode: 'edit', drawingId: 'd1', version: 3, rev: 2, settings };
const I = {
  mode: 'initialize', drawingId: 'd1', version: 3, sourceIntakeSha256: SHA,
  settings: { ...settings, panels_in_sequence: 0, num_mppt: 0 },
};
const R = { ...E, settings: { ...settings, optimizer_ratio: 5e-7 } };
const U = { drawing_units: 'ft', elevation_datum: 'unknown', crs: '' };
const derive = (intakeView = IE, versionsView = V, context = C) => deriveFormState({ context, intakeView, versionsView });
const refused = (reason) => ({ mode: 'refused', reason });
const invalid = (field) => ({ ok: false, reason: 'invalid_value', field });
const edited = (changes) => ({ ok: true, params: { expected_rev: 2, changes } });
const withRow = (changes) => ({ ...V, versions: [V.versions[0], { ...V.versions[1], ...changes }] });
const withGraph = (changes) => ({ ...IE, intake: { ...IE.intake, solar_design_graph: { ...G, ...changes } } });

function seedRequest(crs = null) {
  return {
    ok: true,
    params: {
      expected_rev: 0,
      changes: { panels_in_sequence: 3 },
      initialize: {
        schema_version: 1, source_intake_sha256: SHA,
        units: {
          drawing_units: 'ft',
          wcs_to_ucs: [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
          elevation_datum: 'unknown', crs,
        },
      },
    },
  };
}

describe('Solar settings model', () => {
  it('D1 edit state at the head version', () => {
    expect(derive()).toEqual(E);
  });
  it('D2 initialize state on a graphless head', () => {
    expect(derive(IG)).toEqual(I);
    expect(I.settings).toEqual(SEED_SETTINGS);
  });
  it('D3 refuses a missing drawing version', () => {
    expect(derive(IE, V, { ...C, drawingVersion: null })).toEqual(refused('drawing_version_required'));
  });
  it('D4 refuses project scope', () => {
    expect(derive(IE, V, { ...C, projectId: 'p1' })).toEqual(refused('project_scope_unsupported'));
  });
  it('D5 refuses an intake of another version', () => {
    expect(derive({ ...IE, version: 2 })).toEqual(refused('drawing_unreadable'));
  });
  it('D6 refuses a version that is not the head', () => {
    expect(derive(IE, { ...V, head: 4 })).toEqual(refused('not_current_head'));
  });
  it('D7 refuses a licensed bundle version', () => {
    expect(derive(IE, withRow({ note: 'solar-bundle:x' }))).toEqual(refused('licensed_graph_commit_required'));
  });
  it('D8 refuses a lone graph companion', () => {
    expect(derive({ ...IG, intake: { ...IG.intake, solar_design_graph: G } })).toEqual(refused('graph_unreadable'));
  });
  it('D9 refuses a graph without an integer revision', () => {
    expect(derive(withGraph({ rev: '2' }))).toEqual(refused('graph_unreadable'));
  });
  it('D10 refuses a graph missing a setting', () => {
    const missing = { ...S };
    delete missing.mppt_letter;
    expect(derive(withGraph({ settings: missing }))).toEqual(refused('graph_unreadable'));
  });
  it('D11 refuses a version without a digest', () => {
    expect(derive(IG, withRow({ sha256: 'ABC' }))).toEqual(refused('version_digest_unavailable'));
  });
  it('D12 refuses when the version row is absent', () => {
    expect(derive(IE, { ...V, versions: [V.versions[0]] })).toEqual(refused('drawing_unreadable'));
  });
  it('D13 accepts a served ratio in exponent form', () => {
    expect(derive(withGraph({ settings: { ...S, optimizer_ratio: 5e-7 } }))).toEqual(R);
  });
  it('B1 edit sends one changed integer', () => {
    expect(buildSettingsParams(E, { num_mppt: '4' })).toEqual(edited({ num_mppt: 4 }));
  });
  it('B2 edit refuses an unchanged value', () => {
    expect(buildSettingsParams(E, { num_mppt: '2' })).toEqual({ ok: false, reason: 'no_changes' });
  });
  it('B3 edit refuses empty drafts', () => {
    expect(buildSettingsParams(E, {})).toEqual({ ok: false, reason: 'no_changes' });
  });
  it('B4 integer above the maximum', () => {
    expect(buildSettingsParams(E, { panels_in_sequence: '1000001' })).toEqual(invalid('panels_in_sequence'));
  });
  it('B5 negative integer', () => {
    expect(buildSettingsParams(E, { panels_in_sequence: '-1' })).toEqual(invalid('panels_in_sequence'));
  });
  it('B6 fractional integer', () => {
    expect(buildSettingsParams(E, { panels_in_sequence: '2.5' })).toEqual(invalid('panels_in_sequence'));
  });
  it('B7 integer at the maximum', () => {
    expect(buildSettingsParams(E, { panels_in_sequence: ' 1000000 ' })).toEqual(edited({ panels_in_sequence: 1000000 }));
  });
  it('B8 ratio of zero', () => {
    expect(buildSettingsParams(E, { optimizer_ratio: '0' })).toEqual(invalid('optimizer_ratio'));
  });
  it('B9 fractional ratio', () => {
    expect(buildSettingsParams(E, { optimizer_ratio: '0.5' })).toEqual(edited({ optimizer_ratio: 0.5 }));
  });
  it('B10 exponent ratio', () => {
    expect(buildSettingsParams(E, { optimizer_ratio: '1e3' })).toEqual(invalid('optimizer_ratio'));
  });
  it('B11 string over 4096 code points', () => {
    expect(buildSettingsParams(E, { mppt_letter: 'x'.repeat(4097) })).toEqual(invalid('mppt_letter'));
  });
  it('B12 string at 4096 astral code points', () => {
    const text = '\u{1F600}'.repeat(4096);
    expect(buildSettingsParams(E, { mppt_letter: text })).toEqual(edited({ mppt_letter: text }));
  });
  it('B13 lone surrogate string', () => {
    expect(buildSettingsParams(E, { string_layer: 'Str\uD800' })).toEqual(invalid('string_layer'));
  });
  it('B14 L2 collectors refused', () => {
    expect(buildSettingsParams(E, { use_l2_collectors: true })).toEqual({
      ok: false, reason: 'l2_collectors_unavailable', field: 'use_l2_collectors',
    });
  });
  it('B15 two changes in field order', () => {
    const result = buildSettingsParams(E, { num_mppt: '4', panel_layer_contains: 'Module' });
    expect(result).toEqual(edited({ panel_layer_contains: 'Module', num_mppt: 4 }));
    expect(Object.keys(result.params.changes)).toEqual(['panel_layer_contains', 'num_mppt']);
  });
  it('B16 initialize builds the seed request', () => {
    const result = buildSettingsParams(I, { panels_in_sequence: '3' }, U);
    expect(result).toEqual(seedRequest());
    expect(JSON.stringify(result.params)).toBe(JSON.stringify(seedRequest().params));
  });
  it('B17 initialize without units', () => {
    expect(buildSettingsParams(I, { panels_in_sequence: '3' }, { ...U, drawing_units: '' }))
      .toEqual({ ok: false, reason: 'units_required' });
  });
  it('B18 initialize with an unknown unit', () => {
    expect(buildSettingsParams(I, { panels_in_sequence: '3' }, { ...U, drawing_units: 'furlong' }))
      .toEqual({ ok: false, reason: 'units_required' });
  });
  it('B19 initialize with an empty datum', () => {
    expect(buildSettingsParams(I, { panels_in_sequence: '3' }, { ...U, elevation_datum: '' }))
      .toEqual({ ok: false, reason: 'invalid_elevation_datum' });
  });
  it('B20 initialize keeps a named crs', () => {
    expect(buildSettingsParams(I, { panels_in_sequence: '3' }, { ...U, crs: 'EPSG:2227' }))
      .toEqual(seedRequest('EPSG:2227'));
  });
  it('B21 initialize with an oversize crs', () => {
    expect(buildSettingsParams(I, { panels_in_sequence: '3' }, { ...U, crs: 'x'.repeat(4097) }))
      .toEqual({ ok: false, reason: 'invalid_crs' });
  });
  it('B22 initialize refuses the seed default', () => {
    expect(buildSettingsParams(I, { panels_in_sequence: '0' }, U)).toEqual({ ok: false, reason: 'no_changes' });
  });
  it('B23 refused state passes its reason', () => {
    expect(buildSettingsParams(refused('not_current_head'), { num_mppt: '4' }, U))
      .toEqual({ ok: false, reason: 'not_current_head' });
  });
  it('B24 served exponent ratio stays out of the request', () => {
    expect(buildSettingsParams(R, { num_mppt: '4' })).toEqual(edited({ num_mppt: 4 }));
  });
});

const readRoot = (...parts) => readFileSync(resolve(process.cwd(), '..', ...parts), 'utf8');
const schema = () => JSON.parse(readRoot('contract', 'solar-design-graph.v1.schema.json'));

describe('Solar settings server parity', () => {
  it('field table matches the builtin EDITABLE set', () => {
    const source = readRoot('server', 'builtins', 'solar_settings.py');
    const start = source.indexOf('EDITABLE =');
    expect(start).toBeGreaterThanOrEqual(0);
    const body = source.slice(source.indexOf('{', start) + 1, source.indexOf('}', start));
    const names = [...body.matchAll(new RegExp(String.raw`["']([^"']+)["']`, 'g'))].map((match) => match[1]);
    expect(new Set(names)).toEqual(new Set(SETTINGS_FIELDS.map(({ key }) => key)));
  });
  it('field bounds match the graph schema settings definition', () => {
    const properties = schema().$defs.settings.properties;
    const omitted = new Set(['id', 'kind', 'rev', 'provenance', 'extra', 'validity', 'global_string_sizing_confirmed', 'voc_cold']);
    expect(Object.keys(properties).filter((key) => !omitted.has(key))).toEqual(SETTINGS_FIELDS.map(({ key }) => key));
    const bounds = {
      string: { type: 'string', maxLength: 4096 },
      integer: { type: 'integer', minimum: 0, maximum: 1000000 },
      ratio: { type: 'number', exclusiveMinimum: 0 },
      fixed: { const: false },
    };
    for (const { key, kind } of SETTINGS_FIELDS) expect(properties[key]).toMatchObject(bounds[kind]);
  });
  it('seed defaults match solar_graph_seed.new_empty_graph', () => {
    const source = readRoot('server', 'solar_graph_seed.py');
    const start = source.indexOf('settings.update(');
    expect(start).toBeGreaterThanOrEqual(0);
    const end = source.indexOf('voc_cold=', start);
    expect(end).toBeGreaterThan(start);
    const body = source.slice(start, end);
    const pairs = [...body.matchAll(new RegExp(String.raw`(\w+)\s*=\s*("[^"]*"|[0-9]+|False|True)`, 'g'))];
    const parsed = Object.fromEntries(pairs.map(([, key, value]) => [
      key, value === 'False' ? false : value === 'True' ? true : JSON.parse(value),
    ]));
    const keys = SETTINGS_FIELDS.map(({ key }) => key);
    expect(pairs).toHaveLength(14);
    expect(new Set(Object.keys(parsed))).toEqual(new Set([...keys, 'global_string_sizing_confirmed']));
    expect(parsed.global_string_sizing_confirmed).toBe(false);
    expect(Object.fromEntries(keys.map((key) => [key, parsed[key]]))).toEqual(SEED_SETTINGS);
  });
  it('unit names match the graph schema units enum', () => {
    expect(schema().$defs.units.properties.drawing_units.enum).toEqual(DRAWING_UNITS);
  });
  it('reason map is frozen and every sentence is at least 12 characters', () => {
    for (const value of [SETTINGS_FIELDS, ...SETTINGS_FIELDS, SEED_SETTINGS, DRAWING_UNITS, IDENTITY_WCS_TO_UCS, SOLAR_SETTINGS_REASONS]) {
      expect(Object.isFrozen(value)).toBe(true);
    }
    expect(Object.keys(SOLAR_SETTINGS_REASONS)).toHaveLength(15);
    for (const sentence of Object.values(SOLAR_SETTINGS_REASONS)) {
      expect(typeof sentence).toBe('string');
      expect(sentence.length).toBeGreaterThanOrEqual(12);
    }
  });
});
