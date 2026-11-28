using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.RegularExpressions;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace LeafSolarDesign.ScriptHost.Package
{
    public static class PackageValidator
    {
        private static readonly Regex NameRx = new Regex("^[a-z][a-z0-9]*(-[a-z0-9]+)*$");
        private static readonly Regex SemverRx = new Regex("^(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)$");
        private static readonly Regex Rfc3339Rx = new Regex("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\\.[0-9]+)?Z$");
        private static readonly Regex CsxNameRx = new Regex("^[a-z0-9][a-z0-9-]*\\.csx$");
        private static readonly Regex CapabilityRx = new Regex("^(drawing\\.(read|write)|files\\.(read|write):.+|net\\.http:.+)$");
        private static readonly Regex LoadRx = new Regex("^\\s*#load\\s+\"([^\"]+)\"\\s*$");
        private static readonly Regex RefRx = new Regex("^\\s*#r($|[\\s\"])");

        // Fails closed on null packages and manifest/layout/directive violations V1-V17, including 64 files and 4 MiB.
        public static PackageValidationResult Validate(ToolPackage package)
        {
            if (package == null) throw new ArgumentNullException(nameof(package));
            var failures = new List<string>();
            var manifest = Validate(package.Files, package.DirName, failures);
            return new PackageValidationResult(failures, manifest);
        }

        private static JObject Validate(IReadOnlyDictionary<string, byte[]> files, string pkgDirName, List<string> failures)
        {
            // V16: layout
            if (files.Count > 64) failures.Add("V16: more than 64 files");
            if (files.Values.Sum(b => (long)b.Length) > 4 * 1024 * 1024) failures.Add("V16: package exceeds 4 MiB");
            foreach (var path in files.Keys)
            {
                var ok = path == "tool.json" || path == "main.csx"
                      || (path.StartsWith("lib/") && path.Count(c => c == '/') == 1 && CsxNameRx.IsMatch(path.Substring(4)));
                if (!ok) failures.Add($"V16: disallowed file '{path}'");
            }

            // V1: manifest present, UTF-8, no BOM, single JSON object
            if (!files.TryGetValue("tool.json", out var manifestBytes)) { failures.Add("V1: tool.json missing"); return null; }
            if (manifestBytes.Length >= 3 && manifestBytes[0] == 0xEF && manifestBytes[1] == 0xBB && manifestBytes[2] == 0xBF)
                { failures.Add("V1: tool.json has a BOM"); return null; }
            JToken tok;
            try
            {
                var json = new UTF8Encoding(false, true).GetString(manifestBytes);
                new StrictJson(json).ReadDocument();
                using (var jr = new JsonTextReader(new StringReader(json))
                       { DateParseHandling = DateParseHandling.None, FloatParseHandling = FloatParseHandling.Decimal, MaxDepth = 64 })
                    tok = JToken.ReadFrom(jr);
                CheckJsonTypes(tok);
            }
            catch (Exception ex) { failures.Add("V1: tool.json parse error: " + ex.Message); return null; }
            if (!(tok is JObject m)) { failures.Add("V1: tool.json is not a JSON object"); return null; }

            // V2: closed top-level key set
            var required = new[] { "name", "version", "description", "entry", "params", "returns", "capabilities", "hosts", "timeout_ms", "idempotent", "provenance", "review" };
            var allowed = new HashSet<string>(required.Concat(new[] { "family" }), StringComparer.Ordinal);
            foreach (var p in m.Properties())
                if (!allowed.Contains(p.Name)) failures.Add($"V2: unknown key '{p.Name}'");
            foreach (var r in required)
                if (m[r] == null) failures.Add($"V2: missing required key '{r}'");
            if (failures.Count > 0) return m;

            // V3-V15
            var name = StringValue(m["name"]) ?? "";
            if (!NameRx.IsMatch(name) || name.Length < 3 || name.Length > 64) failures.Add("V3: name grammar");
            if (name != pkgDirName) failures.Add($"V3: package dir '{pkgDirName}' != name '{name}'");
            if (!SemverRx.IsMatch(StringValue(m["version"]) ?? "")) failures.Add("V4: version not strict semver");
            var desc = StringValue(m["description"]) ?? "";
            if (desc.Length < 1 || desc.Length > 500) failures.Add("V5: description length");
            if (StringValue(m["entry"]) != "main.csx") failures.Add("V6: entry must be main.csx");
            if (!files.ContainsKey("main.csx")) failures.Add("V6: main.csx missing");
            if (!(m["params"] is JObject prm) || StringValue(prm["type"]) != "object") failures.Add("V7: params must be an object schema with type=object");
            if (!(m["returns"] is JObject)) failures.Add("V7: returns must be an object");
            if (m["capabilities"] is JArray caps)
            {
                foreach (var c in caps)
                    if (c.Type != JTokenType.String || !CapabilityRx.IsMatch((string)c)) failures.Add($"V8: bad capability '{c}'");
            }
            else failures.Add("V8: capabilities must be an array");
            if (m["family"] != null)
            {
                var fam = StringValue(m["family"]) ?? "";
                if (!NameRx.IsMatch(fam) || fam.Length < 3 || fam.Length > 64) failures.Add("V9: family grammar");
            }
            if (m["hosts"] is JObject hosts)
            {
                var hkeys = hosts.Properties().Select(p => p.Name).ToList();
                if (!hkeys.OrderBy(k => k, StringComparer.Ordinal).SequenceEqual(new[] { "min_host_api", "min_release" }))
                    failures.Add("V10: hosts closed set {min_release, min_host_api}");
                if (!IntegerInRange(hosts["min_release"], 2018, 2030))
                    failures.Add("V10: min_release out of range");
                if (!IntegerInRange(hosts["min_host_api"], 1, long.MaxValue))
                    failures.Add("V10: min_host_api must be integer >= 1");
            }
            else failures.Add("V10: hosts must be an object");
            if (!IntegerInRange(m["timeout_ms"], 1, 600000))
                failures.Add("V11: timeout_ms integer 1..600000");
            if (m["idempotent"]?.Type != JTokenType.Boolean) failures.Add("V12: idempotent boolean");
            if (m["provenance"] is JObject prov)
            {
                var pkeys = prov.Properties().Select(p => p.Name).OrderBy(k => k, StringComparer.Ordinal).ToList();
                if (!pkeys.SequenceEqual(new[] { "author", "created", "modified", "session" }))
                    failures.Add("V13: provenance closed set {author, session, created, modified}");
                if (StringValue(prov["author"]) != "agent" && StringValue(prov["author"]) != "human") failures.Add("V13: author enum");
                // Gate parity: provenance.session deliberately has no length bound (K0 item 4).
                if (string.IsNullOrEmpty(StringValue(prov["session"]))) failures.Add("V13: session nonempty");
                if (!Rfc3339Rx.IsMatch(StringValue(prov["created"]) ?? "")) failures.Add("V13: created RFC3339 UTC");
                if (!Rfc3339Rx.IsMatch(StringValue(prov["modified"]) ?? "")) failures.Add("V13: modified RFC3339 UTC");
            }
            else failures.Add("V13: provenance must be an object");
            if (m["review"] is JObject rev)
            {
                if (!rev.Properties().Select(p => p.Name).SequenceEqual(new[] { "status" }))
                    failures.Add("V14: review closed set {status}");
                var st = StringValue(rev["status"]);
                if (st != "unreviewed" && st != "reviewed" && st != "rejected") failures.Add("V14: review.status enum");
            }
            else failures.Add("V14: review must be an object");
            CheckIntegersOnly(m, "$", failures);
            CheckPicks(m, failures);

            // V17: script directives
            foreach (var kv in files.Where(f => f.Key.EndsWith(".csx")))
            {
                string text;
                try { text = new UTF8Encoding(false, true).GetString(kv.Value); }
                catch (DecoderFallbackException)
                {
                    // Vector v17-invalid-utf8 overrides the appendix's uncaught decoder exception.
                    failures.Add($"V17: '{kv.Key}' is not valid UTF-8");
                    continue;
                }
                foreach (var line in text.Split('\n'))
                {
                    var l = line.TrimEnd('\r');
                    if (RefRx.IsMatch(l)) failures.Add($"V17: #r directive in '{kv.Key}'");
                    var lm = LoadRx.Match(l);
                    if (lm.Success)
                    {
                        var target = lm.Groups[1].Value;
                        if (!target.StartsWith("lib/") || !CsxNameRx.IsMatch(target.Substring(4)))
                            failures.Add($"V17: #load target '{target}' not lib/<file>.csx in '{kv.Key}'");
                        else if (!files.ContainsKey(target))
                            failures.Add($"V17: #load target '{target}' missing");
                    }
                }
            }
            return m;
        }

        // V18: x-leaf-pick object-pick inputs (host API 2). Top-level params properties only; at most
        // PickSpec.MaximumPicks; each on a "type": "string" property with a well-formed spec; a package that
        // declares one needs hosts.min_host_api >= PickSpec.FirstHostApi so an older plugin refuses it cleanly.
        private static void CheckPicks(JObject m, List<string> failures)
        {
            if (!(m["params"] is JObject prm) || !(prm["properties"] is JObject properties)) return;
            var picks = 0;
            foreach (var property in properties.Properties())
            {
                if (!(property.Value is JObject schema) || schema.Property(PickSpec.Keyword) == null) continue;
                picks++;
                if (schema["type"]?.Type != JTokenType.String || (string)schema["type"] != "string")
                    failures.Add($"V18: pick input '{property.Name}' must have type string");
                if (!PickSpec.TryParse(schema[PickSpec.Keyword], out _, out var why))
                    failures.Add($"V18: pick input '{property.Name}': {why}");
            }
            if (picks > PickSpec.MaximumPicks) failures.Add($"V18: more than {PickSpec.MaximumPicks} pick inputs");
            if (picks > 0 && !IntegerInRange((m["hosts"] as JObject)?["min_host_api"], PickSpec.FirstHostApi, long.MaxValue))
                failures.Add($"V18: pick inputs need hosts.min_host_api >= {PickSpec.FirstHostApi}");
        }

        private static void CheckJsonTypes(JToken token)
        {
            switch (token.Type)
            {
                case JTokenType.Object:
                case JTokenType.Array:
                case JTokenType.Property:
                    foreach (var child in token.Children()) CheckJsonTypes(child);
                    break;
                case JTokenType.String:
                case JTokenType.Integer:
                case JTokenType.Float:
                case JTokenType.Boolean:
                case JTokenType.Null:
                    break;
                default:
                    throw new FormatException("unsupported JSON token: " + token.Type);
            }
        }

        private static string StringValue(JToken token)
        {
            // Malformed JSON field shapes and BigInteger scalars must report rules, not conversion exceptions.
            var value = (token as JValue)?.Value;
            return value is IFormattable formatted ? formatted.ToString(null, CultureInfo.InvariantCulture) : value?.ToString();
        }

        private static bool IntegerInRange(JToken token, long minimum, long maximum)
        {
            // TryParse rejects integers outside Int64 without narrowing or throwing (successor c).
            return token?.Type == JTokenType.Integer
                && long.TryParse(StringValue(token), NumberStyles.Integer, CultureInfo.InvariantCulture, out var value)
                && value >= minimum && value <= maximum;
        }

        private static void CheckIntegersOnly(JToken token, string path, List<string> failures)
        {
            switch (token.Type)
            {
                case JTokenType.Float:
                    failures.Add($"V15: non-integer number at {path}");
                    break;
                case JTokenType.Object:
                    foreach (var p in ((JObject)token).Properties()) CheckIntegersOnly(p.Value, path + "." + p.Name, failures);
                    break;
                case JTokenType.Array:
                    var i = 0;
                    foreach (var child in (JArray)token) CheckIntegersOnly(child, path + "[" + i++ + "]", failures);
                    break;
                case JTokenType.Property:
                    CheckIntegersOnly(((JProperty)token).Value, path, failures);
                    break;
                case JTokenType.String:
                case JTokenType.Integer:
                case JTokenType.Boolean:
                case JTokenType.Null:
                    break;
                default:
                    failures.Add("V1: unsupported JSON token: " + token.Type);
                    break;
            }
        }
    }

    /// <summary>
    /// One <c>x-leaf-pick</c> declaration (R2-11 contract v1): the command-line prompt, the DXF entity names the
    /// picker accepts, whether a polyline must be closed, and whether a single matching pickfirst entity is used
    /// without prompting. <see cref="TryParse"/> is the one parser for the package validator (V18) and the
    /// catalog's input list, so publish and run agree. It fails closed on any unknown key or malformed value.
    /// </summary>
    public sealed class PickSpec
    {
        public const string Keyword = "x-leaf-pick";
        public const int MaximumPicks = 8;
        public const int MaximumPromptLength = 120;
        public const int MaximumDxfNames = 16;
        /// <summary>The first host API (HostApi.Version) that prompts for pick inputs.</summary>
        public const int FirstHostApi = 2;

        /// <summary>The DXF entity names a pick may name; each maps to an AutoCAD entity class in the picker.</summary>
        public static readonly IReadOnlyCollection<string> KnownDxfNames = new HashSet<string>(StringComparer.Ordinal)
        {
            "LWPOLYLINE", "POLYLINE", "LINE", "ARC", "CIRCLE", "INSERT", "TEXT", "MTEXT", "HATCH", "POINT",
            "SPLINE", "ELLIPSE", "SOLID", "3DFACE", "3DSOLID", "REGION", "RAY", "XLINE", "DIMENSION", "LEADER",
            "MULTILEADER"
        };

        private PickSpec(string prompt, IReadOnlyList<string> dxf, bool closed, bool implied)
        {
            Prompt = prompt;
            Dxf = dxf;
            Closed = closed;
            Implied = implied;
        }

        public string Prompt { get; }
        public IReadOnlyList<string> Dxf { get; }
        public bool Closed { get; }
        public bool Implied { get; }

        /// <summary>True for the DXF names whose entities can be open or closed polylines.</summary>
        public static bool IsPolyline(string dxfName) => dxfName == "LWPOLYLINE" || dxfName == "POLYLINE";

        public static bool TryParse(JToken token, out PickSpec spec, out string error)
        {
            spec = null;
            error = null;
            if (!(token is JObject value)) { error = "x-leaf-pick must be an object"; return false; }
            foreach (var property in value.Properties())
                if (property.Name != "prompt" && property.Name != "dxf" && property.Name != "closed" && property.Name != "implied")
                { error = "x-leaf-pick has unknown key '" + property.Name + "'"; return false; }
            var prompt = value["prompt"];
            if (prompt?.Type != JTokenType.String || !PrintablePrompt((string)prompt))
            { error = "prompt must be 1 to " + MaximumPromptLength + " printable characters"; return false; }
            if (!(value["dxf"] is JArray names) || names.Count < 1 || names.Count > MaximumDxfNames)
            { error = "dxf must list 1 to " + MaximumDxfNames + " DXF entity names"; return false; }
            var dxf = new List<string>(names.Count);
            foreach (var name in names)
            {
                if (name.Type != JTokenType.String || !KnownDxfNames.Contains((string)name))
                { error = "dxf has an unsupported entity name '" + name.ToString(Formatting.None) + "'"; return false; }
                if (dxf.Contains((string)name)) { error = "dxf repeats '" + (string)name + "'"; return false; }
                dxf.Add((string)name);
            }
            foreach (var flag in new[] { "closed", "implied" })
                if (value[flag] != null && value[flag].Type != JTokenType.Boolean)
                { error = flag + " must be true or false"; return false; }
            spec = new PickSpec((string)prompt, dxf.AsReadOnly(), value["closed"] != null && (bool)value["closed"],
                value["implied"] != null && (bool)value["implied"]);
            return true;
        }

        private static bool PrintablePrompt(string text)
        {
            if (text.Length < 1 || text.Length > MaximumPromptLength || text.Trim().Length == 0) return false;
            for (var i = 0; i < text.Length; i++)
            {
                if (char.IsHighSurrogate(text[i]) && i + 1 < text.Length && char.IsLowSurrogate(text[i + 1])) { i++; continue; }
                switch (CharUnicodeInfo.GetUnicodeCategory(text[i]))
                {
                    case UnicodeCategory.Control:
                    case UnicodeCategory.Format:
                    case UnicodeCategory.LineSeparator:
                    case UnicodeCategory.ParagraphSeparator:
                    case UnicodeCategory.Surrogate:
                    case UnicodeCategory.OtherNotAssigned:
                    case UnicodeCategory.PrivateUse:
                        return false;
                }
            }
            return true;
        }
    }
}
