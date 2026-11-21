using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.Linq;

namespace LeafSolarDesign.ScriptHost.Compile
{
    public sealed class CompileRequest
    {
        private readonly Dictionary<string, byte[]> files;
        public string PackageDigest { get; }
        public IReadOnlyDictionary<string, byte[]> Files => CopyFiles(files);
        public IReadOnlyList<string> Capabilities { get; }
        public ReferenceSet References { get; }

        public CompileRequest(string packageDigest, IReadOnlyDictionary<string, byte[]> files,
            IReadOnlyList<string> capabilities, ReferenceSet references)
        {
            if (packageDigest == null || packageDigest.Length != 64 ||
                packageDigest.Any(c => !(c >= '0' && c <= '9') && !(c >= 'a' && c <= 'f')))
                throw new ArgumentException("Digest must be 64 lowercase hexadecimal characters.", nameof(packageDigest));
            if (files == null) throw new ArgumentNullException(nameof(files));
            if (capabilities == null) throw new ArgumentNullException(nameof(capabilities));
            References = references ?? throw new ArgumentNullException(nameof(references));
            PackageDigest = packageDigest;
            this.files = new Dictionary<string, byte[]>(StringComparer.Ordinal);
            foreach (var pair in files)
            {
                if (string.IsNullOrEmpty(pair.Key) || pair.Value == null)
                    throw new ArgumentException("File names and bytes are required.", nameof(files));
                this.files.Add(pair.Key, (byte[])pair.Value.Clone());
            }
            if (capabilities.Any(c => c == null)) throw new ArgumentException("Null capability.", nameof(capabilities));
            Capabilities = Array.AsReadOnly(capabilities.ToArray());
        }

        private static IReadOnlyDictionary<string, byte[]> CopyFiles(Dictionary<string, byte[]> source)
        {
            return new ReadOnlyDictionary<string, byte[]>(source.ToDictionary(p => p.Key,
                p => (byte[])p.Value.Clone(), StringComparer.Ordinal));
        }
    }
}
