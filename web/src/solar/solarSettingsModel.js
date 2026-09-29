export const SOLAR_SETTINGS_TOOL = 'solar-settings';

export const SETTINGS_FIELDS = Object.freeze([
  { key: 'panel_layer_contains', label: 'Panel layer contains', kind: 'string', max: 4096 },
  { key: 'panel_group_layer', label: 'Panel group layer', kind: 'string', max: 4096 },
  { key: 'string_layer', label: 'String layer', kind: 'string', max: 4096 },
  { key: 'home_run_layer', label: 'Home run layer', kind: 'string', max: 4096 },
  { key: 'panels_in_sequence', label: 'Panels in sequence', kind: 'integer', max: 1000000 },
  { key: 'num_mppt', label: 'MPPT count', kind: 'integer', max: 1000000 },
  { key: 'strings_per_mppt', label: 'Strings per MPPT', kind: 'integer', max: 1000000 },
  { key: 'optimizer_ratio', label: 'Optimizer ratio', kind: 'ratio' },
  { key: 'use_l2_collectors', label: 'L2 collectors', kind: 'fixed' },
  { key: 'panel_group_number', label: 'Panel group number', kind: 'integer', max: 1000000 },
  { key: 'string_number', label: 'String number', kind: 'integer', max: 1000000 },
  { key: 'inverter_number', label: 'Inverter number', kind: 'integer', max: 1000000 },
  { key: 'mppt_letter', label: 'MPPT letter', kind: 'string', max: 4096 },
].map((field) => Object.freeze(field)));

export const SEED_SETTINGS = Object.freeze({
  panel_layer_contains: 'Panel',
  panel_group_layer: 'Panel Group',
  string_layer: 'String',
  home_run_layer: 'HomeRun',
  panels_in_sequence: 0,
  num_mppt: 0,
  strings_per_mppt: 0,
  optimizer_ratio: 1,
  use_l2_collectors: false,
  panel_group_number: 1,
  string_number: 1,
  inverter_number: 1,
  mppt_letter: 'A',
});

export const DRAWING_UNITS = Object.freeze(['m', 'mm', 'cm', 'km', 'in', 'ft', 'yd']);
export const IDENTITY_WCS_TO_UCS = Object.freeze([1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]);

export const SOLAR_SETTINGS_REASONS = Object.freeze({
  drawing_version_required: 'Open a saved drawing version before changing Solar settings.',
  project_scope_unsupported: 'This form does not change Solar settings inside a project yet. Open the drawing on its own.',
  drawing_unreadable: 'This drawing version could not be read. Reload the drawing and try again.',
  not_current_head: 'Solar settings change only on the newest drawing version. Open the head version first.',
  licensed_graph_commit_required: 'This drawing keeps its Solar design in AutoCAD, so its settings change there.',
  graph_unreadable: 'The Solar design stored with this drawing cannot be read, so it cannot be edited here.',
  version_digest_unavailable: 'This version has no recorded content digest, so a Solar design cannot start from it.',
  invalid_value: 'A value is outside its allowed range. Check the marked field.',
  no_changes: 'Change at least one project field or setting before you submit.',
  project_name_required: 'Enter a project name before saving this change.',
  invalid_project_zip: 'Enter a five-digit ZIP code or ZIP+4, or leave it blank.',
  invalid_project_coordinates: 'Enter both coordinates within their allowed ranges, or clear both.',
  invalid_project_request: 'Check the project fields and their length limits.',
  units_required: 'Choose the drawing units before starting a Solar design.',
  invalid_elevation_datum: 'Name the elevation datum in 1 to 4096 characters.',
  invalid_crs: 'Keep the coordinate system name to 4096 characters or fewer.',
  l2_collectors_unavailable: 'L2 collectors are not supported yet, so this setting stays off.',
  checkout_required: 'Take the drawing checkout before changing Solar settings.',
  run_in_progress: 'A run is in progress. Wait for it to finish.',
});

const owns = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
const PYTHON_WHITESPACE = String.raw`[\u0009-\u000D\u001C-\u001F\u0020\u0085\u00A0\u1680\u2000-\u200A\u2028\u2029\u202F\u205F\u3000]`;
const PYTHON_STRIP = new RegExp(`^${PYTHON_WHITESPACE}+|${PYTHON_WHITESPACE}+$`, 'g');

export function pyStrip(text) {
  return text.replace(PYTHON_STRIP, '');
}

const plainObject = (value) => value !== null && typeof value === 'object'
  && (Object.getPrototypeOf(value) === Object.prototype || Object.getPrototypeOf(value) === null);

function validString(value, minimum = 0) {
  if (typeof value !== 'string') return false;
  if (/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/.test(value)) return false;
  const length = [...value].length;
  return length >= minimum && length <= 4096;
}

function validValue(kind, value) {
  if (kind === 'string') return validString(value);
  if (kind === 'integer') return Number.isInteger(value) && value >= 0 && value <= 1000000;
  if (kind === 'ratio') return typeof value === 'number' && Number.isFinite(value) && value > 0 && value < 1e15;
  return kind === 'fixed' && value === false;
}

const EMPTY_PROJECT = Object.freeze({ name: '', zip_code: '', latitude: null, longitude: null });
const PROJECT_KEYS = Object.keys(EMPTY_PROJECT);
const validCoordinate = (value, limit) => value === null
  || (typeof value === 'number' && Number.isFinite(value) && Math.abs(value) <= limit);

function validProject(project) {
  return plainObject(project) && validString(project.name) && validString(project.zip_code)
    && [...project.zip_code].length <= 10
    && validCoordinate(project.latitude, 90) && validCoordinate(project.longitude, 180)
    && (project.latitude === null) === (project.longitude === null);
}

function projectChanges(saved, drafts) {
  const fail = (reason) => ({ ok: false, reason });
  if (!plainObject(drafts) || Object.keys(drafts).some((key) => !PROJECT_KEYS.includes(key))) {
    return fail('invalid_project_request');
  }
  const changes = {};
  for (const key of ['name', 'zip_code']) {
    if (!owns(drafts, key)) continue;
    const raw = drafts[key];
    if (!validString(raw) || (key === 'zip_code' && [...raw].length > 10)) return fail('invalid_project_request');
    const value = pyStrip(raw);
    if (key === 'name' && !value) return fail('project_name_required');
    if (key === 'zip_code' && value && !/^[0-9]{5}(-[0-9]{4})?$/.test(value)) return fail('invalid_project_zip');
    if (value !== saved[key]) changes[key] = value;
  }
  if (owns(drafts, 'latitude') || owns(drafts, 'longitude')) {
    const values = {};
    for (const [key, limit] of [['latitude', 90], ['longitude', 180]]) {
      if (!owns(drafts, key)) {
        values[key] = saved[key];
        continue;
      }
      if (typeof drafts[key] !== 'string') return fail('invalid_project_coordinates');
      const text = drafts[key].trim();
      if (text !== '' && !/^-?\d+(\.\d+)?$/.test(text)) return fail('invalid_project_coordinates');
      const value = text === '' ? null : Number(text);
      if (!validCoordinate(value, limit)) return fail('invalid_project_coordinates');
      values[key] = value;
    }
    if ((values.latitude === null) !== (values.longitude === null)) return fail('invalid_project_coordinates');
    if (values.latitude !== saved.latitude || values.longitude !== saved.longitude) Object.assign(changes, values);
  }
  return { ok: true, changes };
}

export function parseField(key, text) {
  const field = SETTINGS_FIELDS.find((item) => item.key === key);
  if (!field || field.kind === 'fixed' || typeof text !== 'string') return { ok: false };
  if (field.kind === 'string') return validString(text) ? { ok: true, value: text } : { ok: false };
  const trimmed = text.trim();
  const pattern = field.kind === 'integer' ? /^[0-9]{1,7}$/ : /^(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)$/;
  if (!pattern.test(trimmed)) return { ok: false };
  const value = Number(trimmed);
  return validValue(field.kind, value) ? { ok: true, value } : { ok: false };
}

export function deriveFormState({ context, intakeView, versionsView }) {
  const refuse = (reason) => ({ mode: 'refused', reason });
  if (typeof context?.drawingId !== 'string' || context.drawingId.length === 0
      || !Number.isInteger(context.drawingVersion) || context.drawingVersion < 1) {
    return refuse('drawing_version_required');
  }
  if (context.projectId != null) return refuse('project_scope_unsupported');
  if (!plainObject(intakeView) || intakeView.version !== context.drawingVersion || !plainObject(intakeView.intake)) {
    return refuse('drawing_unreadable');
  }
  if (!plainObject(versionsView) || !Number.isInteger(versionsView.head) || !Array.isArray(versionsView.versions)) {
    return refuse('drawing_unreadable');
  }
  const row = versionsView.versions.find((item) => item?.v === context.drawingVersion);
  if (!row) return refuse('drawing_unreadable');
  if (versionsView.head !== context.drawingVersion) return refuse('not_current_head');
  if (typeof row.note === 'string' && row.note.startsWith('solar-bundle:')) return refuse('licensed_graph_commit_required');
  const intake = intakeView.intake;
  const hasGraph = owns(intake, 'solar_design_graph');
  const hasDigest = owns(intake, 'solar_design_graph_sha256');
  if (hasGraph !== hasDigest) return refuse('graph_unreadable');
  const identity = { drawingId: context.drawingId, version: context.drawingVersion };
  if (hasGraph) {
    const graph = intake.solar_design_graph;
    if (!plainObject(graph) || !Number.isInteger(graph.rev) || graph.rev < 0 || !plainObject(graph.settings) || !validProject(graph.project)
        || SETTINGS_FIELDS.some(({ key, kind }) => !owns(graph.settings, key) || !validValue(kind, graph.settings[key]))) {
      return refuse('graph_unreadable');
    }
    const settings = Object.fromEntries(SETTINGS_FIELDS.map(({ key }) => [key, graph.settings[key]]));
    const project = Object.fromEntries(PROJECT_KEYS.map((key) => [key, graph.project[key]]));
    return { mode: 'edit', ...identity, rev: graph.rev, settings, project };
  }
  if (typeof row.sha256 !== 'string' || !/^[0-9a-f]{64}$/.test(row.sha256)) return refuse('version_digest_unavailable');
  return { mode: 'initialize', ...identity, sourceIntakeSha256: row.sha256, settings: { ...SEED_SETTINGS }, project: { ...EMPTY_PROJECT } };
}

export function buildSettingsParams(state, drafts = {}, units = {}, projectDrafts = {}) {
  if (state.mode === 'refused') return { ok: false, reason: state.reason };
  const changes = {};
  for (const { key, kind } of SETTINGS_FIELDS) {
    if (!owns(drafts, key)) continue;
    if (kind === 'fixed') return { ok: false, reason: 'l2_collectors_unavailable', field: key };
    const parsed = parseField(key, drafts[key]);
    if (!parsed.ok) return { ok: false, reason: 'invalid_value', field: key };
    if (parsed.value !== state.settings[key]) changes[key] = parsed.value;
  }
  const project = projectChanges(state.project, projectDrafts);
  if (!project.ok) return project;
  const patches = {
    ...(Object.keys(changes).length ? { changes } : {}),
    ...(Object.keys(project.changes).length ? { project_changes: project.changes } : {}),
  };
  if (Object.keys(patches).length === 0) return { ok: false, reason: 'no_changes' };
  if (state.mode === 'edit') return { ok: true, params: { expected_rev: state.rev, ...patches } };
  if (!DRAWING_UNITS.includes(units?.drawing_units)) return { ok: false, reason: 'units_required' };
  if (!validString(units.elevation_datum, 1)) return { ok: false, reason: 'invalid_elevation_datum' };
  if (!validString(units.crs)) return { ok: false, reason: 'invalid_crs' };
  return {
    ok: true,
    params: {
      expected_rev: 0,
      ...patches,
      initialize: {
        schema_version: 1,
        source_intake_sha256: state.sourceIntakeSha256,
        units: {
          drawing_units: units.drawing_units,
          wcs_to_ucs: [...IDENTITY_WCS_TO_UCS],
          elevation_datum: units.elevation_datum,
          crs: units.crs === '' ? null : units.crs,
        },
      },
    },
  };
}
