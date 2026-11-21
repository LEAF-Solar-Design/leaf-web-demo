using System;
using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using Newtonsoft.Json.Linq;

namespace LeafSolarDesign.ScriptHost.Package
{
    public static class PackageIdentity
    {
        // Fails closed on null packages; hashes exact file bytes and ordinal-sorted paths per CONTRACT 5.1.
        public static string Digest(ToolPackage package)
        {
            if (package == null) throw new ArgumentNullException(nameof(package));
            var sb = new StringBuilder();
            using (var sha = SHA256.Create())
            {
                foreach (var kv in package.Files)
                    sb.Append(kv.Key).Append('\t').Append(kv.Value.Length.ToString(CultureInfo.InvariantCulture)).Append('\t')
                        .Append(HexLower(sha.ComputeHash(kv.Value))).Append('\n');
                return HexLower(sha.ComputeHash(new UTF8Encoding(false).GetBytes(sb.ToString())));
            }
        }

        // Fails closed on null arguments; clones manifest values and binds introspection to exact package bytes.
        public static JObject Introspection(JObject manifest, ToolPackage package)
        {
            if (manifest == null) throw new ArgumentNullException(nameof(manifest));
            if (package == null) throw new ArgumentNullException(nameof(package));
            var fileArray = new JArray();
            using (var sha = SHA256.Create())
                foreach (var kv in package.Files)
                    fileArray.Add(new JObject
                    {
                        ["path"] = kv.Key,
                        ["sha256"] = HexLower(sha.ComputeHash(kv.Value)),
                        ["size"] = kv.Value.Length
                    });
            var intro = new JObject { ["contract_version"] = 1 };
            foreach (var key in new[] { "name", "version", "description", "entry", "params", "returns", "capabilities", "hosts", "timeout_ms", "idempotent", "provenance", "review" })
                intro[key] = manifest[key].DeepClone();
            intro["files"] = fileArray;
            intro["package_digest"] = Digest(package);
            if (manifest["family"] != null) intro["family"] = manifest["family"].DeepClone();
            return intro;
        }

        // Fails closed on null arguments or non-canonical manifest values per CONTRACT 5.2 and 5.3.
        public static string IntrospectionJson(JObject manifest, ToolPackage package)
        {
            if (manifest == null) throw new ArgumentNullException(nameof(manifest));
            if (package == null) throw new ArgumentNullException(nameof(package));
            return CanonicalJson.Serialize(Introspection(manifest, package));
        }

        private static string HexLower(byte[] bytes)
        {
            var sb = new StringBuilder(bytes.Length * 2);
            foreach (var b in bytes) sb.Append(b.ToString("x2", CultureInfo.InvariantCulture));
            return sb.ToString();
        }
    }
}
