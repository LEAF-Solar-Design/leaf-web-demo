import { describe, expect, it } from "vitest";
import { CSHARP_PARSER_ERROR_CODES, decodeOperatorEntities } from "../src/vendor/mushy-author/agent/tools/decodeHtmlEntities.js";

describe("decodeOperatorEntities", () => {
  it.each([
    ["if (p.X &lt; sMinX)", "if (p.X < sMinX)", 1],
    ["List&lt;List&lt;double[]&gt;&gt;", "List<List<double[]>>", 4],
    ["a &amp;&amp; b", "a && b", 2],
    ["x &gt;= y", "x >= y", 1],
    ["&lt; &gt; &amp; &#60; &#62; &#38; &#x3c; &#X3E; &#x26;", "< > & < > & < > &", 9],
    ["&#0060; &#0062; &#0038; &#x003C; &#X003e; &#x0026;", "< > & < > &", 6],
    ["&amp;lt;", "&lt;", 1],
    ["\ufeff\ufffd\r\nx &lt; y\r\n", "\ufeff\ufffd\r\nx < y\r\n", 1],
    ["// &lt; &quot; &#34;\nx &lt; y /* &apos; &#xA; */", "// &lt; &quot; &#34;\nx < y /* &apos; &#xA; */", 1],
    ["x &lt; y; // keep // &gt;\n", "x < y; // keep // &gt;\n", 1],
    ["x /* &lt; */ &gt; y", "x /* &lt; */ > y", 1],
    ["x /&lt;* y; x *&gt;/ y", "x /<* y; x *>/ y", 2],
    ["x &lt;/* &gt; */ y", "x </* &gt; */ y", 1],
    ['var s = "safe \\"text\\""; x &lt; y;', 'var s = "safe \\"text\\""; x < y;', 1],
    ['var s = @"safe ""text"""; x &lt; y;', 'var s = @"safe ""text"""; x < y;', 1],
    ['var s = $"safe {x}"; a &lt; b;', 'var s = $"safe {x}"; a < b;', 1],
    ['var s = """raw " quote"""; a &lt; b;', 'var s = """raw " quote"""; a < b;', 1],
    ['var s = $$"""raw {{x}} text"""; a &lt; b;', 'var s = $$"""raw {{x}} text"""; a < b;', 1],
    ['var s = $"{area / 144.0:F1} ft"; a &lt; b;', 'var s = $"{area / 144.0:F1} ft"; a < b;', 1],
    ['var s = $"""{area / 2}"""; a &lt; b;', 'var s = $"""{area / 2}"""; a < b;', 1],
    ['var s = $"{F("nested")}"; a &lt; b;', 'var s = $"{F("nested")}"; a < b;', 1],
    ['var s = $"{ $"nested {x}" }"; a &lt; b;', 'var s = $"{ $"nested {x}" }"; a < b;', 1],
    ["var s = $\"{F('}')}\"; a &lt; b;", "var s = $\"{F('}')}\"; a < b;", 1],
    ["var s = $\"\"\"{F('}')}\"\"\"; a &lt; b;", "var s = $\"\"\"{F('}')}\"\"\"; a < b;", 1],
    ['var s = $@"{F(@"a""}", "}")}\n"; a &lt; b;', 'var s = $@"{F(@"a""}", "}")}\n"; a < b;', 1],
    ['var s = $$"""{{F("""} raw""")}} {x} {{{y}}}"""; a &lt; b;', 'var s = $$"""{{F("""} raw""")}} {x} {{{y}}}"""; a < b;', 1],
    ['var s = $"{{{x}}} {new[] { 1 }[0],5:0.0} {(b ? "}" : "{")} {d:MM/dd}"; a &lt; b;',
      'var s = $"{{{x}}} {new[] { 1 }[0],5:0.0} {(b ? "}" : "{")} {d:MM/dd}"; a < b;', 1],
    ["int gt = 1; x &lt; y", "int gt = 1; x < y", 1],
    ["int amp = 1; x &lt; y", "int amp = 1; x < y", 1],
    ["int lt = 1; x &gt; y &amp;&amp; z", "int lt = 1; x > y && z", 3],
    ["int lt = 1; var r = a &#60; b &#x3C; c;", "int lt = 1; var r = a < b < c;", 2],
    ["/*\n# &lt; */\na &lt; b", "/*\n# &lt; */\na < b", 1],
    ["&custom; a &lt; b", "&custom; a < b", 1],
    ["&constructor; a &lt; b", "&constructor; a < b", 1],
  ])("decodes once and preserves tokens: %s", (source, expected, entities) => {
    expect(decodeOperatorEntities(source as string)).toEqual({ source: expected, entities });
  });

  it.each([
    "a < b", "// &lt;\nreturn 1;", "/* &lt; */", '"&lt;"',
    'var s = "&lt;"; a &gt; b;', "char c = '&amp;'; a &lt; b;",
    'var s = @"&gt;"; a &lt; b;', 'var s = $"&lt; {x}"; a &gt; b;',
    'var s = $@"&lt; {x}"; a &gt; b;', 'var s = @$"&lt; {x}"; a &gt; b;',
    'var s = """&lt;"""; a &gt; b;', 'var s = """"raw """ &lt;""""; a &gt; b;',
    'var s = $$"""{{x}} &lt;"""; a &gt; b;',
    'var s = $"{F("&lt;")}"; a &gt; b;', 'var s = $"{ $"nested {a &lt; b}" }"; a &gt; b;', "var s = $\"{F('&lt;')}\"; a &gt; b;",
    'var s = $"{F("unterminated)}"; a &lt; b;', 'var s = $"{F("x"]}"; a &lt; b;', 'var s = $"{x"; a &lt; b;',
    'var s = $"lone } brace"; a &lt; b;', 'var s = $"{x:F1}}}"; a &lt; b;', 'var s = $"{global::X}"; a &lt; b;',
    'var s = $"{x:"}"}"; a &lt; b;', 'var s = $"{\nx}"; a &lt; b;', 'var s = $"{@"\n"}"; a &lt; b;',
    'var s = $$"""{{{{x}}}}"""; a &lt; b;', 'var s = $$"""{{x}"""; a &lt; b;', 'var s = $$"{{x}}"; a &lt; b;',
    'var s = "bad\\\nescape"; a &lt; b;',
    'var s = $$"""{{F("""&lt;""")}}"""; a &gt; b;',
    'var s = "x &quot; + evil + &quot; y"; a &lt; b;',
    'var s = "&custom;"; a &lt; b;',
    'var s = "unterminated; a &lt; b;', "'unterminated; a &lt; b;",
    'var s = @"unterminated; a &lt; b;', 'var s = """unterminated; a &lt; b;',
    "/* unterminated &lt;", "x &lt; y; /* unterminated",
    "&quot; a &lt; b", "&apos; a &lt; b", "&nbsp; a &lt; b",
    "&#34; a &lt; b", "&#39; a &lt; b", "&#x22; a &lt; b", "&#X27; a &lt; b",
    "&#10; a &lt; b", "&#xA; a &lt; b", "&#65; a &lt; b", "&#-1; a &lt; b",
    "&#00060;", "&#x0003c;", "&#xZZ; a &lt; b",
    ' #load "lib/x.csx" // &lt;\n', '#load &lt;path&gt;\nx &lt; y',
    "\t#if x &gt; y\nx &lt; y\n#endif", "#if &custom;\nx &lt; y\n#endif",
    "#region &quot;\nx &lt; y\n#endregion", "/*\n# */ x &lt; y", '#load "lib/x.csx" // &amp;\nx &lt; y',
    "#if DEBUG\n/*\n#endif\nx &lt; y", "# else\nx &lt; y", '#region /*\nvar s = "&lt;"; // */\nx &lt; y',
    'var s = $"{x /* } " */}"; a &lt; b;', 'var s = $@"{x // }\n}"; a &lt; b;', 'var s = $"{x:\\"}"; a &lt; b;',
    'var s = $"""{x /* } */}"""; a &lt; b;',
    "int lt = 1; x &lt; y", "int gt = 1; x &gt; y", "int amp = 1; x &amp; y", 'var s = $"{lt.Name}"; x &lt; y',
    "int @lt = 1; x &lt; y", "int \\u006ct = 1; x &lt; y",
    "int l\u200ct = 1; x &lt; y", 'var s = "bad\u2028literal"; a &lt; b;',
    '// comment\u2028var s = "&lt;";\nx &lt; y', "// comment\u0085#if x &lt; y\n#endif",
  ])("refuses ambiguous or non-operator source: %s", source => {
    expect(decodeOperatorEntities(source)).toBeNull();
  });

  const declared = "var lt = (LayerTable)x;\n";

  it.each([
    ["if (p.X &lt; sMinX) sMinX = p.X;", 1],
    ["if (p.X &lt; 1.0 /* ; */ || s == \";\" || q &lt; r) { }", 2],
    ["for (int i = 0; i &lt; n; i++) { }", 1],
    ["for /* trivia */\n(int i = 0; i &lt; n &amp;&amp; j &lt; m; i++) { }", 4],
    ["for (int i = F(a &lt; b); ; i++) { }", 1],
    ["foreach (var q in a[b &lt; c]) { }", 1],
    ["var q = a[b &lt; c];", 1],
    ["int @for = 1; var q = @for(a &lt; b);", 1],
    ["var a = new List&lt;PolyData&gt;();", 2],
    ["List&lt;double[]&gt; best = null;", 2],
    ["Dictionary&lt;string, List&lt;double[]&gt;&gt; d = null;", 4],
    ["Dictionary&lt; System.String ,\n List&lt; int? &gt; [] &gt; d = null;", 4],
    ["Dictionary&lt;int[,], double?[]&gt; d = null;", 2],
    ["f(() => { var q = new List&lt;int&gt;(); });", 2],
    ["var q = a &#60; b;", 1],
    ["var q = a &gt; b;", 1],
  ])("proves every lt entity while lt is declared: %s", (body, entities) => {
    const source = declared + (body as string);
    expect(decodeOperatorEntities(source)).toEqual({ entities,
      source: source.replace(/&lt;|&#60;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&") });
  });

  it.each([
    "var r = a &lt; b;", "var r = flags &lt;\nb++; if (x {", "f(() => { var r = a &lt; b; });",
    "var q = new X { A = b &lt; c };", "if (p &lt; q) { } var r = a &lt; b;", "if (p &lt; q) { } }", "if (p &lt; q] { }",
    "for (; i &lt; n) { }", "for (;;; i &lt; n) { }", "for (i &lt; n) { }", "for (i &custom; i &lt; n) { }",
    "for // trivia\n(; i &lt; n) { }", "for (i = 0; F(i); G(() => { var r = a &lt; b; })) { }",
    "int gt = 1; var r = a &lt; b &gt; c;", "int gt = 1; List&lt;double[]&gt; best = null;",
    "x = a &lt; return &gt;", "x = a &lt; b, await &gt;", "x = a &lt; List&lt;new&gt; &gt;",
    "List&lt;double[]> best = null;", "List&lt;double[]&#62; best = null;", "List&lt;a .b&gt; c;", "List&lt;a. b&gt; c;",
    "List&lt;a []&gt; c;", "List&lt;int[,,]&gt; c;", "List&lt;@return&gt; c;", "List&lt;retu‌rn&gt; c;",
    "List&lt;&gt; c;", "List&lt;a,&gt; c;", "List&lt;a b&gt; c;", "List&lt;1&gt; c;", "List&lt;a/**/&gt; c;", "List&lt;a",
    "List&lt;List&lt;a&gt; c;", 'var s = $"{lt.Name}"; var r = a &lt; b;',
  ])("refuses an unproven lt entity while lt is declared: %s", body => {
    expect(decodeOperatorEntities(declared + body)).toBeNull();
  });

  it.each([
    ["int lt = 1;", "var r = a &lt; b;"], ["int gt = 1;", "var r = a &gt; b;"], ["int amp = 1;", "var r = a &amp; b;"],
    ["int \\u006ct = 1;", "var r = a &lt; b;"],
    ["int gt = 1;", "var lt = 1; List&lt;double[]&gt; best = null;"],
    // A lib that cannot be lexed counts as containing all three names.
    ['var s = "unterminated;', "var r = a &lt; b;"], ["/* unterminated", "var r = a &gt; b;"],
    ["#if DEBUG\n#endif", "var r = a &amp; b;"],
  ])("counts identifiers declared in a lib: %s", (lib, body) => {
    expect(decodeOperatorEntities(body)).not.toBeNull();
    expect(decodeOperatorEntities(body, ["// safe", lib])).toBeNull();
  });

  it("still decodes proven entities and ignores names outside lib code", () => {
    const decoded = { source: "var r = a < b;", entities: 1 };
    for (const lib of ["int other = 1;", '// int lt\nvar s = "lt &lt; gt";', "int gt = 1;", "var r = a &lt; b;"]) {
      expect(decodeOperatorEntities("var r = a &lt; b;", [lib])).toEqual(decoded);
    }
    expect(decodeOperatorEntities("if (a &lt; b) { }", ["int lt = 1;"])).toEqual({ source: "if (a < b) { }", entities: 1 });
    expect(decodeOperatorEntities("var r = a &#60; b;", ["/* unterminated"])).toEqual(decoded);
    expect(decodeOperatorEntities("var r = a < b;", ["var r = a &lt; b;"])).toBeNull();
  });

  it("follows sixteen nested interpolation holes and no more", () => {
    const nested = (depth: number) => 'var s = ' + '$"{'.repeat(depth) + "x" + '}"'.repeat(depth) + "; a &lt; b;";
    expect(decodeOperatorEntities(nested(16))).toEqual({ source: nested(16).replace("&lt;", "<"), entities: 1 });
    expect(decodeOperatorEntities(nested(17))).toBeNull();
    expect(decodeOperatorEntities(nested(200_000))).toBeNull();
  });

  it("proves long nested and failing type lists in linear time", () => {
    const start = performance.now();
    const depth = 100_000;
    const good = declared + "A&lt;".repeat(depth) + "T" + "&gt;".repeat(depth) + " x;";
    expect(decodeOperatorEntities(good)?.entities).toBe(2 * depth);
    expect(decodeOperatorEntities(declared + "A&lt;".repeat(depth))).toBeNull();
    expect(decodeOperatorEntities(declared + "a &lt; " + "T, ".repeat(depth) + "1;")).toBeNull();
    expect(performance.now() - start).toBeLessThan(5_000);
  });

  it("leaves directives without entities and comment entities intact", () => {
    const source = '#load "lib/x.csx"\n#region don\'t /* " unpaired\n// &quot;\nx &lt; y\n#endregion\n#pragma warning disable';
    expect(decodeOperatorEntities(source)).toEqual({ source: source.replace("&lt;", "<"), entities: 1 });
  });

  it("handles four MiB of ampersands in linear time", () => {
    const source = "&".repeat(4 * 1024 * 1024);
    const start = performance.now();
    expect(decodeOperatorEntities(source)).toBeNull();
    expect(performance.now() - start).toBeLessThan(20_000);
  }, 60_000);

  it("does not rescan long invalid interpolation prefixes", () => {
    const source = "$".repeat(4 * 1024 * 1024);
    const start = performance.now();
    expect(decodeOperatorEntities(source)).toBeNull();
    expect(performance.now() - start).toBeLessThan(2_000);
  });

  it("exports only the fixed parser error set", () => {
    expect([...CSHARP_PARSER_ERROR_CODES]).toEqual([
      "CS1001", "CS1002", "CS1003", "CS1010", "CS1022", "CS1026", "CS1031", "CS1041",
      "CS1513", "CS1514", "CS1515", "CS1519", "CS1525", "CS1526", "CS1547", "CS7017",
      "CS8124", "CS8641", "CS8803",
    ]);
  });
});
