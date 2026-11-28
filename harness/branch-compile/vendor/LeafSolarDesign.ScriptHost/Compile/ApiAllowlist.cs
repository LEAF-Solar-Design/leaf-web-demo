using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using Newtonsoft.Json.Linq;

namespace LeafSolarDesign.ScriptHost.Compile
{
    /// <summary>The embedded, digest-pinned positive API policy.</summary>
    public static class ApiAllowlist
    {
        public const string ReflectionEmit = "System.Reflection.Emit";
        public const string Diagnostics = "System.Diagnostics";
        public const string RuntimeLoader = "System.Runtime.Loader";
        public const string IO = "System.IO";
        public const string Net = "System.Net";
        public const string AppDomain = "System.AppDomain";
        public const string Marshal = "System.Runtime.InteropServices.Marshal";
        public const string DllImport = "System.Runtime.InteropServices.DllImportAttribute";
        public const string Files = "LeafSolarDesign.ScriptHost.IToolFiles";
        public const string Http = "LeafSolarDesign.ScriptHost.IToolHttp";
        internal static readonly string[] ToolFacingTypes = {
            "LeafSolarDesign.ScriptHost.ToolGlobals",
            "LeafSolarDesign.ScriptHost.IToolContext",
            "LeafSolarDesign.ScriptHost.IToolLog",
            Files, Http,
            "LeafSolarDesign.ScriptHost.HostApi"
        };
        internal const string Digest = "716d548887ce792f4f9950d0ba8b0e531b3d7f9b80b7399cf3fcd59d622a3285";
        internal const string ResourceName = "LeafSolarDesign.ScriptHost.api-allowlist.v2.json";
        private static readonly Lazy<ApiPolicy> policy = new Lazy<ApiPolicy>(Load);
        internal static ApiPolicy Policy => policy.Value;

        private static ApiPolicy Load()
        {
            try
            {
                using (var stream = typeof(ApiAllowlist).Assembly.GetManifestResourceStream(ResourceName))
                {
                    if (stream == null) return ApiPolicy.Unavailable("missing resource");
                    using (var bytes = new MemoryStream())
                    {
                        stream.CopyTo(bytes);
                        return ApiPolicy.Parse(bytes.ToArray());
                    }
                }
            }
            catch (Exception ex) { return ApiPolicy.Unavailable("resource load: " + ex.Message); }
        }
    }

    internal sealed class ApiPolicy
    {
        internal string Error { get; private set; }
        internal string Digest { get; private set; }
        // Policy sets are built once by the thread-safe lazy loader.
        internal HashSet<string> AllowedNamespaces, AllowedTypes, AllowedMembers, IdentityTypes;
        internal HashSet<string> DeniedNamespaces, DeniedTypes, DeniedMembers, MemberTypes;
        internal Dictionary<string, string> CapabilityGatedMembers;

        internal static ApiPolicy Unavailable(string reason) => new ApiPolicy { Error = reason };

        internal static ApiPolicy Parse(byte[] bytes)
        {
            try
            {
                if (bytes == null) return Unavailable("missing resource");
                byte[] normalized = bytes.Where(b => b != 13).ToArray();
                string digest;
                using (var sha = SHA256.Create())
                    digest = BitConverter.ToString(sha.ComputeHash(normalized)).Replace("-", "").ToLowerInvariant();
                var json = JObject.Parse(new UTF8Encoding(false, true).GetString(normalized));
                if ((string)json["schema"] != "leaf.tool-api-allowlist.v2") return Unavailable("wrong schema");
                var result = new ApiPolicy {
                    Digest = digest,
                    AllowedNamespaces = ReadSet(json, "allowed_namespaces"),
                    AllowedTypes = ReadSet(json, "allowed_types"),
                    AllowedMembers = ReadSet(json, "allowed_members"),
                    IdentityTypes = ReadSet(json, "identity_types"),
                    DeniedNamespaces = ReadSet(json, "denied_namespaces"),
                    DeniedTypes = ReadSet(json, "denied_types"),
                    DeniedMembers = ReadSet(json, "denied_members"),
                    CapabilityGatedMembers = ((JObject)json["capability_gated_members"]).Properties()
                        .ToDictionary(p => p.Name, p => (string)p.Value, StringComparer.Ordinal)
                };
                var allowed = new HashSet<string>(result.AllowedNamespaces, StringComparer.Ordinal);
                allowed.UnionWith(result.AllowedTypes);
                allowed.UnionWith(result.AllowedMembers);
                allowed.UnionWith(result.IdentityTypes);
                allowed.UnionWith(result.CapabilityGatedMembers.Keys);
                if (allowed.Overlaps(result.DeniedNamespaces) || allowed.Overlaps(result.DeniedTypes) ||
                    allowed.Overlaps(result.DeniedMembers)) return Unavailable("allow/deny overlap");
                if (digest != ApiAllowlist.Digest) return Unavailable("digest mismatch");
                result.MemberTypes = new HashSet<string>(result.AllowedMembers.Concat(result.CapabilityGatedMembers.Keys)
                    .Select(n => n.Substring(0, n.EndsWith("..ctor", StringComparison.Ordinal) ? n.Length - 6 : n.LastIndexOf('.'))),
                    StringComparer.Ordinal);
                return result;
            }
            catch (Exception ex) { return Unavailable("parse failure: " + ex.Message); }
        }

        private static HashSet<string> ReadSet(JObject json, string key)
        {
            var array = json[key] as JArray ?? throw new FormatException("missing " + key);
            if (array.Any(v => v.Type != JTokenType.String || string.IsNullOrEmpty((string)v)))
                throw new FormatException("invalid " + key);
            return new HashSet<string>(array.Values<string>(), StringComparer.Ordinal);
        }
    }
}
