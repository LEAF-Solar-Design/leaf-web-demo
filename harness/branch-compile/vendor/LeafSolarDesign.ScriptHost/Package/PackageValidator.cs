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
}
