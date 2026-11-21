using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using Microsoft.CodeAnalysis;

namespace LeafSolarDesign.ScriptHost.Compile
{
    public sealed class InMemorySourceResolver : SourceReferenceResolver
    {
        private readonly Dictionary<string, byte[]> sources = new Dictionary<string, byte[]>(StringComparer.Ordinal);

        public InMemorySourceResolver(IReadOnlyDictionary<string, string> sources)
        {
            if (sources == null) throw new ArgumentNullException(nameof(sources));
            foreach (var pair in sources) this.sources.Add(pair.Key, Encoding.UTF8.GetBytes(pair.Value));
        }

        public override string NormalizePath(string path, string baseFilePath)
        {
            if (string.IsNullOrEmpty(path) || path.Contains("\\") || path.Contains(":") || path.StartsWith("/", StringComparison.Ordinal)) return null;
            string prefix = "";
            int slash = (baseFilePath ?? "").LastIndexOf('/');
            if (slash >= 0) prefix = baseFilePath.Substring(0, slash + 1);
            var parts = new List<string>();
            foreach (string part in (prefix + path).Split('/'))
            {
                if (part == "" || part == ".") continue;
                if (part == "..")
                {
                    if (parts.Count == 0) return null;
                    parts.RemoveAt(parts.Count - 1);
                }
                else parts.Add(part);
            }
            return string.Join("/", parts);
        }

        public override string ResolveReference(string path, string baseFilePath)
        {
            string normalized = NormalizePath(path, baseFilePath);
            return normalized != null && normalized.StartsWith("lib/", StringComparison.Ordinal) &&
                normalized.EndsWith(".csx", StringComparison.Ordinal) && sources.ContainsKey(normalized) ? normalized : null;
        }

        public override Stream OpenRead(string resolvedPath)
        {
            if (!sources.TryGetValue(resolvedPath, out var bytes)) throw new FileNotFoundException("Package source is missing.", resolvedPath);
            return new MemoryStream(bytes, false);
        }

        public override bool Equals(object other) => ReferenceEquals(this, other);
        public override int GetHashCode() => System.Runtime.CompilerServices.RuntimeHelpers.GetHashCode(this);
    }
}
