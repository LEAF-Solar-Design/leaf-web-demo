import { canonicalJSON } from '../contract.js';

export const MAX_ROWS = 5000;
const own = (value, key) => Object.prototype.hasOwnProperty.call(value, key);
const refuse = () => { throw new Error('Test: operator input refused'); };
const rows = value => Array.isArray(value) ? value : refuse();
const number = value => typeof value === 'number' && Number.isFinite(value) ? value : refuse();
const count = (value, fallback) => value === undefined ? fallback : Number.isSafeInteger(value) && value >= 0 && value <= MAX_ROWS ? value : refuse();

// Paths are data, never JavaScript expressions or inherited property lookups.
export function field(value, path) {
  if (typeof path !== 'string' || !path.length) return refuse();
  for (const key of path.split('.')) {
    if (['__proto__', 'prototype', 'constructor'].includes(key)) return refuse();
    if (value === null || typeof value !== 'object' || !own(value, key)) return null;
    value = value[key];
  }
  return value;
}

function compare(a, b) {
  if (a === b) return 0;
  if (a === null) return -1;
  if (b === null) return 1;
  if (typeof a !== typeof b || !['number', 'string', 'boolean'].includes(typeof a)) return refuse();
  return a < b ? -1 : 1;
}

function predicate(value, settings) {
  const selected = field(value, settings.field);
  switch (settings.comparison || 'eq') {
    case 'eq': return canonicalJSON(selected) === canonicalJSON(settings.value);
    case 'ne': return canonicalJSON(selected) !== canonicalJSON(settings.value);
    case 'gt': return compare(selected, settings.value) > 0;
    case 'gte': return compare(selected, settings.value) >= 0;
    case 'lt': return compare(selected, settings.value) < 0;
    case 'lte': return compare(selected, settings.value) <= 0;
    default: return refuse();
  }
}

const operators = Object.freeze({
  constant: (_input, settings) => settings.value,
  identity: input => input.value,
  field: (input, settings) => field(input.value, settings.field),
  select: (input, settings) => rows(input.value).map(row => field(row, settings.field)),
  filter: (input, settings) => rows(input.value).filter(row => predicate(row, settings)),
  sort: (input, settings) => {
    if (settings.direction !== undefined && !['asc', 'desc'].includes(settings.direction)) return refuse();
    const direction = settings.direction === 'desc' ? -1 : 1;
    return [...rows(input.value)].sort((a, b) => direction * compare(field(a, settings.field), field(b, settings.field)));
  },
  take: (input, settings) => rows(input.value).slice(0, count(settings.count, MAX_ROWS)),
  count: input => rows(input.value).length,
  sum: (input, settings) => rows(input.value).reduce((sum, row) => number(sum + number(settings.field ? field(row, settings.field) : row)), 0),
  mean: (input, settings) => {
    const values = rows(input.value);
    return values.length ? operators.sum(input, settings) / values.length : null;
  },
  min: (input, settings) => {
    const values = rows(input.value).map(row => number(settings.field ? field(row, settings.field) : row));
    return values.length ? Math.min(...values) : null;
  },
  max: (input, settings) => {
    const values = rows(input.value).map(row => number(settings.field ? field(row, settings.field) : row));
    return values.length ? Math.max(...values) : null;
  },
  add: input => number(number(input.value) + number(input.auxiliary)),
  subtract: input => number(number(input.value) - number(input.auxiliary)),
  multiply: input => number(number(input.value) * number(input.auxiliary)),
  divide: input => number(number(input.value) / number(input.auxiliary)),
  latest: input => rows(input.value).at(-1) ?? null,
  window: (input, settings) => {
    const size = count(settings.count, MAX_ROWS);
    return size === 0 ? [] : rows(input.value).slice(-size);
  },
  collect: (input, settings) => rows(input.value).slice(0, count(settings.count, MAX_ROWS)),
});

export const OPERATOR_IDS = Object.freeze(Object.keys(operators));
export const STREAM_ADAPTERS = Object.freeze(['latest', 'window', 'collect']);
export function hasOperator(id) { return own(operators, id); }
export function runOperator(id, input = {}, settings = {}) {
  if (!hasOperator(id)) throw new Error('Test: operator unavailable in browser');
  return operators[id](input, settings);
}
