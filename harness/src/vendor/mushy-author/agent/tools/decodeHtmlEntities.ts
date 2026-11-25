/** Parser errors for which an operator-entity retry is worth attempting. */
export const CSHARP_PARSER_ERROR_CODES: ReadonlySet<string> = new Set([
  "CS1001", "CS1002", "CS1003", "CS1010", "CS1022", "CS1026", "CS1031", "CS1041",
  "CS1513", "CS1514", "CS1515", "CS1519", "CS1525", "CS1526", "CS1547", "CS7017",
  "CS8124", "CS8641", "CS8803",
]);

type Entity = { end: number; value?: string; forbidden: boolean };
function entityAt(source: string, start: number): Entity | null {
  if (source[start] !== "&") return null;
  let end = start + 1;
  const numeric = source[end] === "#";
  if (numeric) end++;
  const nameStart = end;
  while (end < source.length && (numeric ? /[^\s&;"'\/]/.test(source[end]) : /[A-Za-z0-9]/.test(source[end]))) end++;
  if (end === nameStart || source[end] !== ";") return null;
  const name = source.slice(nameStart, end);
  let value: string | undefined;
  if (numeric) {
    const hex = name[0] === "x" || name[0] === "X";
    const digits = hex ? name.slice(1) : name;
    if (digits.length >= 1 && digits.length <= 4 && (hex ? /^[0-9a-fA-F]+$/ : /^[0-9]+$/).test(digits)) {
      const code = Number.parseInt(digits, hex ? 16 : 10);
      if (code === 60 || code === 62 || code === 38) value = String.fromCharCode(code);
    }
  } else value = name === "lt" ? "<" : name === "gt" ? ">" : name === "amp" ? "&" : undefined;
  return { end: end + 1, value, forbidden: numeric ? value === undefined : ["quot", "apos", "nbsp"].includes(name) };
}

function hasEntity(text: string): boolean {
  for (let i = 0; i < text.length; i++) if (text[i] === "&" && entityAt(text, i)) return true;
  return false;
}

/** One open bracket in code, with the semicolons directly inside it that do not end an operator entity. */
type Bracket = { open: string; forHeader: boolean; semicolons: number };
/** name is absent for a numeric entity, which is never legal C# and needs no proof. */
type Replacement = { start: number; end: number; value: string; name?: string; bracket?: Bracket };
type Lexed = { tokens: string[]; replacements: Replacement[]; names: Set<string>; balanced: boolean };
const entityNames: ReadonlySet<string> = new Set(["lt", "gt", "amp"]);
const closing: Readonly<Record<string, string>> = { "(": ")", "[": "]", "{": "}" };
const identifierStart = /[\p{L}\p{Nl}_]/u;
const identifierPart = /[\p{L}\p{Nl}\p{Mn}\p{Mc}\p{Nd}\p{Pc}\p{Cf}]/u;
const identifierFormat = /\p{Cf}/u;
function newline(char: string): boolean {
  return char === "\n" || char === "\r" || char === "\u0085" || char === "\u2028" || char === "\u2029";
}
function identifierChar(source: string, i: number): { char: string; end: number } {
  if (source[i] === "\\" && (source[i + 1] === "u" || source[i + 1] === "U")) {
    const count = source[i + 1] === "u" ? 4 : 8;
    const digits = source.slice(i + 2, i + 2 + count);
    if (digits.length === count && /^[0-9a-fA-F]+$/.test(digits)) {
      const code = Number.parseInt(digits, 16);
      if (code <= 0x10ffff) return { char: String.fromCodePoint(code), end: i + 2 + count };
    }
  }
  const char = String.fromCodePoint(source.codePointAt(i)!);
  return { char, end: i + char.length };
}

/** End of the identifier token at start, or start when there is none. Records lt, gt and amp by C# identity. */
function identifierEnd(source: string, start: number, names: Set<string>): number {
  const first = identifierChar(source, source[start] === "@" && start + 1 < source.length ? start + 1 : start);
  if (!identifierStart.test(first.char)) return start;
  let name = first.char;
  let i = first.end;
  while (i < source.length) {
    const next = identifierChar(source, i);
    if (!identifierPart.test(next.char)) break;
    if (!identifierFormat.test(next.char)) name += next.char;
    i = next.end;
  }
  if (entityNames.has(name)) names.add(name);
  return i;
}

const MAX_HOLE_DEPTH = 16;

/** End of an interpolation hole whose code starts at start, or -1 when it cannot be followed. */
function holeEnd(source: string, start: number, braces: number, multiline: boolean, depth: number, names: Set<string>): number {
  if (depth > MAX_HOLE_DEPTH) return -1;
  const open: string[] = [];
  let i = start;
  while (i < source.length) {
    const char = source[i];
    // Comments, escapes and directives are not followed inside a hole.
    if (char === "\\" || char === "#" || (char === "/" && (source[i + 1] === "/" || source[i + 1] === "*"))) return -1;
    if (newline(char) && !multiline) return -1;
    const literal = literalEnd(source, i, multiline, depth, names);
    if (literal < 0) return -1;
    if (literal > 0) { i = literal; continue; }
    if (char === "$") return -1;
    if (open.length === 0 && (char === "}" || char === ":")) {
      let end = i;
      if (char === ":") {
        // A format clause is plain text up to the closing braces.
        if (source[i + 1] === ":") return -1;
        for (end = i + 1; source[end] !== "}"; end++) {
          if (end >= source.length || newline(source[end]) || '"\\{'.includes(source[end])) return -1;
        }
        // A longer run may escape a brace in the format, which compilers read differently.
        if (source[end + braces] === "}") return -1;
      }
      for (let n = 0; n < braces; n++) if (source[end + n] !== "}") return -1;
      return end + braces;
    }
    if (char === "(" || char === "[" || char === "{") open.push(char);
    else if (char === ")" || char === "]" || char === "}") {
      if (closing[open.pop() ?? ""] !== char) return -1;
    } else {
      const end = identifierEnd(source, i, names);
      if (end > i) { i = end; continue; }
    }
    i++;
  }
  return -1;
}

/** End of the literal at start, 0 when none starts there, or -1 when it cannot be followed. */
function literalEnd(source: string, start: number, multiline: boolean, depth: number, names: Set<string>): number {
  let quote = start;
  let dollars = 0;
  let verbatim = false;
  if (source[quote] === "@") {
    verbatim = true;
    quote++;
    if (source[quote] === "$") { dollars++; quote++; }
  } else {
    while (source[quote] === "$") { dollars++; quote++; }
    if (source[quote] === "@") { verbatim = true; quote++; }
  }
  const delimiter = source[quote];
  if (delimiter !== '"' && !(delimiter === "'" && quote === start)) return 0;
  let quotes = 1;
  if (!verbatim && delimiter === '"') while (source[quote + quotes] === '"') quotes++;
  const raw = quotes >= 3;
  if (dollars > 1 && !raw) return -1;
  const lines = multiline && (verbatim || raw);
  let i = quote + (raw ? quotes : 1);
  while (i < source.length) {
    const char = source[i];
    if (newline(char) && !lines) return -1;
    if (char === delimiter) {
      if (raw) {
        let end = i;
        while (source[end] === '"') end++;
        if (end - i >= quotes) return end;
        i = end;
        continue;
      }
      if (!verbatim || source[i + 1] !== '"') return i + 1;
      i += 2;
      continue;
    }
    if (!raw && !verbatim && char === "\\") {
      if (i + 1 < source.length && newline(source[i + 1])) return -1;
      i += 2;
      continue;
    }
    if (dollars > 0 && (char === "{" || char === "}")) {
      let end = i;
      if (raw) {
        while (source[end] === char) end++;
        if (end - i < dollars) { i = end; continue; }
        if (end - i >= 2 * dollars) return -1;
      } else {
        if (source[i + 1] === char) { i += 2; continue; }
        end++;
      }
      if (char === "}") return -1;
      i = holeEnd(source, end, dollars, lines, depth + 1, names);
      if (i < 0) return -1;
      continue;
    }
    i++;
  }
  return -1;
}

/** Conservative C# literal/comment lexer. Interpolation holes it cannot follow fail closed. */
function lex(source: string, decoding: boolean, rejectEntities = false): Lexed | null {
  const tokens: string[] = [];
  const replacements: Replacement[] = [];
  const names = new Set<string>();
  const brackets: Bracket[] = [];
  let balanced = true;
  // True while the last code token, ignoring trivia, is the for keyword.
  let afterFor = false;
  // Record directive lines in a separate linear pass, including lines inside tokens.
  const directives: { start: number; first: number; end: number }[] = [];
  for (let start = 0; start < source.length;) {
    let end = start;
    while (end < source.length && !newline(source[end])) end++;
    let first = start;
    while (first < end && /\s/.test(source[first])) first++;
    if (source[first] === "#") directives.push({ start, first, end });
    start = end + 1;
  }
  let directive = 0;
  for (let i = 0; i < source.length;) {
    const start = i;
    while (directive < directives.length && directives[directive].end <= i) directive++;
    const line = directives[directive];
    if (line?.first === i) {
      // Directive text has no literals or block comments, so it is kept whole.
      const text = source.slice(i, line.end);
      // Conditionally skipped text cannot be lexed, so conditionals fail closed.
      if ((decoding && hasEntity(text)) || /^#\s*(?:if|elif|else|endif)\b/.test(text)) return null;
      tokens.push("directive:" + text);
      i = line.end;
      continue;
    }
    if (source.startsWith("//", i)) {
      i += 2;
      while (i < source.length && !newline(source[i])) i++;
      tokens.push("comment:" + source.slice(start, i));
      continue;
    }
    if (source.startsWith("/*", i)) {
      const end = source.indexOf("*/", i + 2);
      if (end < 0) return null;
      i = end + 2;
      tokens.push("comment:" + source.slice(start, i));
      continue;
    }
    const char = source[i];
    if (newline(char) || /\s/.test(char)) { i++; continue; }
    const forHeader = char === "(" && afterFor;
    afterFor = false;
    const literal = literalEnd(source, i, true, 0, names);
    if (literal < 0) return null;
    if (literal > 0) {
      i = literal;
      const text = source.slice(start, i);
      // Only decoding rejects these: rule 3 already rejects one that decoding would create.
      if (decoding && hasEntity(text)) return null;
      tokens.push("literal:" + text);
      continue;
    }
    if (char === "$") {
      // Consume an invalid dollar prefix once rather than rescanning its suffixes.
      while (source[i] === "$") i++;
      continue;
    }
    const bracket = brackets.at(-1);
    if (char === "&") {
      const entity = entityAt(source, i);
      if (entity) {
        if (rejectEntities) return null;
        // The line can still start with # when that # closes a block comment or literal.
        if (decoding && (entity.forbidden || (line !== undefined && line.start <= i))) return null;
        if (entity.value !== undefined) {
          replacements.push({ start: i, end: entity.end, value: entity.value,
            name: source[i + 1] === "#" ? undefined : source.slice(i + 1, entity.end - 1), bracket });
        } else if (bracket) bracket.semicolons++; // Any other &name; keeps its own semicolon.
        i = entity.end;
        continue;
      }
      i++;
      continue;
    }
    if (char === "(" || char === "[" || char === "{") brackets.push({ open: char, forHeader, semicolons: 0 });
    else if (char === ")" || char === "]" || char === "}") {
      if (closing[brackets.pop()?.open ?? ""] !== char) balanced = false;
    } else if (char === ";") {
      if (bracket) bracket.semicolons++;
    } else {
      const end = identifierEnd(source, i, names);
      if (end > i) {
        afterFor = source.slice(i, end) === "for";
        i = end;
        continue;
      }
    }
    i++;
  }
  return { tokens, replacements, names, balanced: balanced && brackets.length === 0 };
}

function skipSpace(source: string, i: number): number {
  while (i < source.length && /\s/.test(source[i])) i++;
  return i;
}

const typeName = /[\p{L}\p{Nl}_][\p{L}\p{Nl}\p{Mn}\p{Mc}\p{Nd}\p{Pc}]*/uy;
const statementKeywords: ReadonlySet<string> = new Set([
  "return", "throw", "await", "yield", "new", "else", "do", "in", "case", "goto", "var", "is", "as",
]);

/**
 * End of the type argument list, with its closing &gt;, that starts after an &lt; entity, or -1.
 * lists holds the result for every later &lt;, so no text is scanned twice.
 */
function typeListEnd(source: string, start: number, lists: ReadonlyMap<number, number>): number {
  let i = skipSpace(source, start);
  for (;;) {
    for (;;) {
      typeName.lastIndex = i;
      const name = typeName.exec(source)?.[0];
      if (name === undefined || statementKeywords.has(name)) return -1;
      i += name.length;
      if (source[i] !== ".") break;
      i++;
    }
    const nested = skipSpace(source, i);
    if (source.startsWith("&lt;", nested)) {
      const end = lists.get(nested) ?? -1;
      if (end < 0) return -1;
      i = skipSpace(source, end);
    }
    for (;;) {
      if (source[i] === "?") i++;
      else if (source.startsWith("[]", i)) i += 2;
      else if (source.startsWith("[,]", i)) i += 3;
      else break;
    }
    i = skipSpace(source, i);
    if (source.startsWith("&gt;", i)) return i + 4;
    if (source[i] !== ",") return -1;
    i = skipSpace(source, i + 1);
  }
}

/** Rule 5: no named entity may be a legal `E & name ;` when that name is an identifier in this source. */
function namedEntitiesProven(source: string, { replacements, names, balanced }: Lexed): boolean {
  if (names.size === 0) return true;
  const lists = new Map<number, number>();
  for (let n = replacements.length - 1; n >= 0; n--) {
    const { start, end, name, bracket } = replacements[n];
    if (name === "lt") lists.set(start, typeListEnd(source, end, lists));
    if (name === undefined || !names.has(name)) continue;
    // A semicolon is never legal directly inside [ or (, except the two of a for header.
    if (balanced && bracket && (bracket.open === "[" ||
        (bracket.open === "(" && (!bracket.forHeader || bracket.semicolons === 2)))) continue;
    if (name === "lt" && !names.has("gt") && (lists.get(start) ?? -1) >= 0) continue;
    return false;
  }
  return true;
}

/**
 * Decode once, only when literals and comments retain their original token text.
 * otherSources are compiled together with source and can declare lt, gt or amp. They are never decoded.
 */
export function decodeOperatorEntities(source: string, otherSources: readonly string[] = []): { source: string; entities: number } | null {
  const original = lex(source, true);
  if (!original || original.replacements.length === 0) return null;
  for (const other of otherSources) {
    // A source that cannot be lexed may declare any of the three names.
    for (const name of lex(other, false)?.names ?? entityNames) original.names.add(name);
  }
  if (!namedEntitiesProven(source, original)) return null;
  const chunks: string[] = [];
  let cursor = 0;
  for (const replacement of original.replacements) {
    chunks.push(source.slice(cursor, replacement.start), replacement.value);
    cursor = replacement.end;
  }
  chunks.push(source.slice(cursor));
  const decoded = chunks.join("");
  const checked = lex(decoded, false, true);
  if (!checked || original.tokens.length !== checked.tokens.length || original.tokens.some((token, i) => token !== checked.tokens[i])) return null;
  return { source: decoded, entities: original.replacements.length };
}
