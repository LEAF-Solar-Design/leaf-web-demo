import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import {
  SETTINGS_FIELDS, SEED_SETTINGS, DRAWING_UNITS, IDENTITY_WCS_TO_UCS,
  SOLAR_SETTINGS_REASONS, L2_COLLECTORS_NOTE, deriveFormState, buildSettingsParams, parseField, pyStrip,
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
const project = { name: 'Roof A', zip_code: '44224', latitude: 41, longitude: -81 };
const G = { rev: 2, settings: S, project };
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
const E = { mode: 'edit', drawingId: 'd1', version: 3, rev: 2, settings, project };
const I = {
  mode: 'initialize', drawingId: 'd1', version: 3, sourceIntakeSha256: SHA,
  settings: { ...settings, panels_in_sequence: 0, num_mppt: 0 },
  project: { name: '', zip_code: '', latitude: null, longitude: null },
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
  it('PJ15 strips project text with Python whitespace parity', () => {
    expect(buildSettingsParams(E, {}, {}, { name: '\u0085' }))
      .toEqual({ ok: false, reason: 'project_name_required' });
    expect(buildSettingsParams(E, {}, {}, { zip_code: '\u008537601' }))
      .toEqual({ ok: true, params: { expected_rev: 2, project_changes: { zip_code: '37601' } } });
    expect(buildSettingsParams(E, {}, {}, { name: 'Roof A\u0085' }))
      .toEqual({ ok: false, reason: 'no_changes' });
    expect(buildSettingsParams(E, {}, {}, { name: 'Roof B\uFEFF' }))
      .toEqual({ ok: true, params: { expected_rev: 2, project_changes: { name: 'Roof B\uFEFF' } } });
    const whitespace = '\u0009\u000A\u000B\u000C\u000D\u001C\u001D\u001E\u001F\u0020\u0085\u00A0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200A\u2028\u2029\u202F\u205F\u3000';
    for (const character of whitespace) expect(pyStrip(`${character}Roof${character}`)).toBe('Roof');
    expect(pyStrip('\uFEFFRoof\uFEFF')).toBe('\uFEFFRoof\uFEFF');
    expect(pyStrip('Roof\u0085B')).toBe('Roof\u0085B');
  });

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
  it('B14 L2 collectors on is sent as a boolean', () => {
    const result = buildSettingsParams(E, { use_l2_collectors: true });
    expect(result).toEqual(edited({ use_l2_collectors: true }));
    expect(result.params.changes.use_l2_collectors).toBe(true);
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

// L2 collectors (sf-w2-settings-form-l2-toggle). Every server answer named here was measured over
// POST /api/run?wait=1 on Forge main 4fdb411a with the real solar-settings builtin (python -B).
const L2 = 'use_l2_collectors';
const EL2 = { ...E, settings: { ...settings, [L2]: true } };
// The W1 fixture's settings and project exactly as the server serves them (rev 0); the L2 fixtures
// (topology, central only, combiner only, composite, string-only L2) serve the same with the mode true.
const W1_SETTINGS = {
  panel_layer_contains: 'Panels', panel_group_layer: 'Groups', string_layer: 'Strings',
  home_run_layer: 'Homeruns', panels_in_sequence: 2, num_mppt: 1, strings_per_mppt: 2,
  optimizer_ratio: 1, use_l2_collectors: false, panel_group_number: 2, string_number: 3,
  inverter_number: 2, mppt_letter: 'A',
};
const W1_PROJECT = { name: 'Synthetic rooftop', zip_code: '00000', latitude: null, longitude: null };
const served = (mode) => ({
  ...IE,
  intake: {
    ...IE.intake,
    solar_design_graph: {
      rev: 0, settings: { ...W1_SETTINGS, [L2]: mode, global_string_sizing_confirmed: false, extra: {} },
      project: W1_PROJECT,
    },
  },
});

describe('Solar settings L2 collectors', () => {
  it('L2-1 the field is a boolean and the seed keeps it off', () => {
    expect(SETTINGS_FIELDS.find(({ key }) => key === L2)).toEqual({ key: L2, label: 'L2 collectors', kind: 'boolean' });
    expect(SETTINGS_FIELDS.filter(({ kind }) => kind === 'boolean').map(({ key }) => key)).toEqual([L2]);
    expect(SETTINGS_FIELDS.some(({ kind }) => kind === 'fixed')).toBe(false);
    expect(SEED_SETTINGS[L2]).toBe(false);
  });
  it('L2-2 leaving L2 is sent as false', () => {
    const result = buildSettingsParams(EL2, { [L2]: false });
    expect(result).toEqual(edited({ [L2]: false }));
    expect(result.params.changes[L2]).toBe(false);
  });
  it.each([[E, false], [EL2, true]])('L2-3 a draft equal to the saved mode sends nothing %#', (state, draft) => {
    expect(buildSettingsParams(state, { [L2]: draft })).toEqual({ ok: false, reason: 'no_changes' });
    expect(buildSettingsParams(state, { [L2]: draft, num_mppt: '4' })).toEqual(edited({ num_mppt: 4 }));
  });
  it('L2-4 the mode rides with other changes in field order', () => {
    const result = buildSettingsParams(E, { [L2]: true, mppt_letter: 'B', num_mppt: '4' });
    expect(result).toEqual(edited({ num_mppt: 4, [L2]: true, mppt_letter: 'B' }));
    expect(Object.keys(result.params.changes)).toEqual(['num_mppt', L2, 'mppt_letter']);
    expect(buildSettingsParams(E, { [L2]: true }, {}, { zip_code: '37601' })).toEqual({
      ok: true, params: { expected_rev: 2, changes: { [L2]: true }, project_changes: { zip_code: '37601' } },
    });
  });
  // The server answers INVALID_GRAPH_SCHEMA for 1, 0, 1.0, "true" and null (type(mode) is bool); the model never sends them.
  it.each([1, 0, 1.5, 'true', 'false', '', null, undefined, [], {}, NaN])('L2-5 a draft that is not a boolean is refused %j', (draft) => {
    expect(buildSettingsParams(E, { [L2]: draft })).toEqual(invalid(L2));
    expect(buildSettingsParams(EL2, { [L2]: draft })).toEqual(invalid(L2));
  });
  it('L2-6 an invalid earlier field is named before the mode', () => {
    expect(buildSettingsParams(E, { [L2]: 'true', num_mppt: 'x' })).toEqual(invalid('num_mppt'));
    expect(buildSettingsParams(E, { [L2]: 'true', mppt_letter: 'x'.repeat(4097) })).toEqual(invalid(L2));
  });
  it('L2-7 initialize sends the mode only when it is on', () => {
    const request = seedRequest();
    request.params.changes = { [L2]: true };
    const result = buildSettingsParams(I, { [L2]: true }, U);
    expect(result).toEqual(request);
    expect(JSON.stringify(result.params)).toBe(JSON.stringify(request.params));
    expect(buildSettingsParams(I, { [L2]: false }, U)).toEqual({ ok: false, reason: 'no_changes' });
    expect(buildSettingsParams(I, { [L2]: true }, { ...U, drawing_units: '' })).toEqual({ ok: false, reason: 'units_required' });
  });
  it('L2-8 a refused state passes its reason before the mode is read', () => {
    expect(buildSettingsParams(refused('not_current_head'), { [L2]: true })).toEqual({ ok: false, reason: 'not_current_head' });
    expect(buildSettingsParams(refused('graph_unreadable'), { [L2]: 'true' })).toEqual({ ok: false, reason: 'graph_unreadable' });
  });
  it('L2-9 a served L2 drawing opens for editing', () => {
    expect(derive(withGraph({ settings: { ...S, [L2]: true } }))).toEqual(EL2);
  });
  it.each([1, 0, 'true', 'false', null, undefined, [], {}])('L2-10 a served mode that is not a boolean is unreadable %j', (mode) => {
    expect(derive(withGraph({ settings: { ...S, [L2]: mode } }))).toEqual(refused('graph_unreadable'));
  });
  it('L2-11 a served graph without the mode is unreadable', () => {
    const missing = { ...S };
    delete missing[L2];
    expect(derive(withGraph({ settings: missing }))).toEqual(refused('graph_unreadable'));
  });
  it('L2-12 parseField takes a boolean only for the boolean field', () => {
    expect(parseField(L2, true)).toEqual({ ok: true, value: true });
    expect(parseField(L2, false)).toEqual({ ok: true, value: false });
    for (const text of ['true', 'false', '', 1, 0, null, undefined]) expect(parseField(L2, text)).toEqual({ ok: false });
    for (const key of ['num_mppt', 'optimizer_ratio', 'mppt_letter']) {
      expect(parseField(key, true)).toEqual({ ok: false });
      expect(parseField(key, false)).toEqual({ ok: false });
    }
    expect(parseField('unknown_field', true)).toEqual({ ok: false });
    expect(parseField('toString', true)).toEqual({ ok: false });
  });
  // Measured: W1 (L1, one untyped inverter) with this request commits version 2, the mode true and the
  // inverter typed string_inverter (graph digest 24acf04e...); two inverters sharing a number answer
  // DUPLICATE_EQUIPMENT_NUMBER with the head unchanged.
  it('L2-13 entering L2 on the served W1 drawing builds the request the server commits', () => {
    const state = derive(served(false));
    expect(state).toEqual({ mode: 'edit', drawingId: 'd1', version: 3, rev: 0, settings: W1_SETTINGS, project: W1_PROJECT });
    expect(buildSettingsParams(state, { [L2]: true })).toEqual({ ok: true, params: { expected_rev: 0, changes: { [L2]: true } } });
  });
  // Measured: this one request is refused with DESIGN_PRESET_L2_EQUIPMENT_PRESENT (HTTP 400, head unchanged)
  // on the topology, central-only, combiner-only and composite drawings, and commits version 2 on the
  // string-only L2 drawing (the inverter untyped again, digest 8d1e77aa...) and on an L2 drawing with no
  // inverter. The model builds the same request for all of them: the equipment rule is the server's.
  it('L2-14 leaving L2 on a served L2 drawing builds the one request the server decides', () => {
    const state = derive(served(true));
    expect(state).toEqual({
      mode: 'edit', drawingId: 'd1', version: 3, rev: 0, settings: { ...W1_SETTINGS, [L2]: true }, project: W1_PROJECT,
    });
    expect(buildSettingsParams(state, { [L2]: false })).toEqual({ ok: true, params: { expected_rev: 0, changes: { [L2]: false } } });
    expect(buildSettingsParams(state, { [L2]: true })).toEqual({ ok: false, reason: 'no_changes' });
  });
  it('L2-15 the note is one frozen sentence and the retired reason is gone', () => {
    expect(L2_COLLECTORS_NOTE).toBe('Turning L2 collectors off is refused while the design has a combiner box, a central inverter or an inverter linked to an L2 collector.');
    expect(Object.hasOwn(SOLAR_SETTINGS_REASONS, 'l2_collectors_unavailable')).toBe(false);
    expect(Object.values(SOLAR_SETTINGS_REASONS).some((sentence) => sentence.includes('not supported yet'))).toBe(false);
  });
});

const readRoot = (...parts) => readFileSync(resolve(process.cwd(), '..', ...parts), 'utf8');

describe('Solar project validation', () => {
  it.each([
    null, [], undefined, { ...project, name: 3 }, { ...project, zip_code: '1'.repeat(11) },
    { ...project, latitude: true }, { ...project, longitude: Infinity },
    { ...project, latitude: 91 }, { ...project, name: '\uD800' },
  ])('refuses malformed saved project %j', (value) => {
    expect(derive(withGraph({ project: value }))).toEqual(refused('graph_unreadable'));
  });
  it('accepts incomplete saved setup without defaulting coordinates', () => {
    const empty = { name: '', zip_code: '', latitude: null, longitude: null };
    expect(derive(withGraph({ project: empty }))).toEqual({ ...E, project: empty });
  });
  it.each([
    { name: ' '.repeat(4096) + 'A' }, { zip_code: ' 44224     ' },
    { name: '\uD800' }, { zip_code: '\uDC00' }, { extra: 'x' },
  ])('checks raw project strings and keys before normalization %j', (drafts) => {
    expect(buildSettingsParams(E, {}, U, drafts)).toEqual({ ok: false, reason: 'invalid_project_request' });
  });
  it.each(['+1', '.5', '1.', '1e1', 'NaN', 'Infinity', '1x'])('refuses nondecimal coordinate %s', (latitude) => {
    expect(buildSettingsParams(E, {}, U, { latitude })).toEqual({ ok: false, reason: 'invalid_project_coordinates' });
  });
  it('sends a complete pair when only one coordinate changes', () => {
    expect(buildSettingsParams(E, {}, U, { latitude: ' 40.5 ' })).toEqual({
      ok: true, params: { expected_rev: 2, project_changes: { latitude: 40.5, longitude: -81 } },
    });
  });
  it('accepts coordinate limits and ZIP+4', () => {
    expect(buildSettingsParams(E, {}, U, { zip_code: '12345-6789', latitude: '-90', longitude: '180' })).toEqual({
      ok: true, params: { expected_rev: 2, project_changes: { zip_code: '12345-6789', latitude: -90, longitude: 180 } },
    });
  });
  it('omits normalized coordinate no-ops from a ZIP change', () => {
    expect(buildSettingsParams(E, {}, U, { zip_code: '37601', latitude: '41.0', longitude: '-81.0' })).toEqual({
      ok: true, params: { expected_rev: 2, project_changes: { zip_code: '37601' } },
    });
  });
});

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
      // The graph schema admits L2 mode since the typed topology slice; the form sends true or false.
      boolean: { type: 'boolean' },
    };
    for (const { key, kind } of SETTINGS_FIELDS) {
      expect(Object.hasOwn(bounds, kind)).toBe(true);
      expect(properties[key]).toMatchObject(bounds[kind]);
    }
  });
  it('the mode rule the model mirrors is the builtin rule', () => {
    const builtin = readRoot('server', 'builtins', 'solar_settings.py');
    expect(builtin).toContain('mode_flip = type(mode) is bool and mode != settings["use_l2_collectors"]');
    expect(builtin).toContain('if mode_flip and not mode and leaves_l2_blocked(graph):');
    expect(builtin).toContain('raise GraphValidationError(L2_EQUIPMENT_PRESENT)');
    expect(readRoot('server', 'solar_preset_sync.py')).toContain('L2_EQUIPMENT_PRESENT = "DESIGN_PRESET_L2_EQUIPMENT_PRESENT"');
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
    expect(Object.keys(SOLAR_SETTINGS_REASONS)).toHaveLength(18);
    for (const sentence of Object.values(SOLAR_SETTINGS_REASONS)) {
      expect(typeof sentence).toBe('string');
      expect(sentence.length).toBeGreaterThanOrEqual(12);
    }
  });
});
