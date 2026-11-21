using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.Linq;

namespace LeafSolarDesign.ScriptHost.Package
{
    public sealed class ToolPackage
    {
        private readonly SortedDictionary<string, byte[]> files;

        internal ToolPackage(string dirName, IDictionary<string, byte[]> files)
        {
            if (dirName == null) throw new ArgumentNullException(nameof(dirName));
            if (files == null) throw new ArgumentNullException(nameof(files));
            DirName = dirName;
            this.files = Copy(files);
            TotalBytes = this.files.Values.Sum(b => (long)b.Length);
        }

        public string DirName { get; }
        public long TotalBytes { get; }

        public IReadOnlyDictionary<string, byte[]> Files => new ReadOnlyDictionary<string, byte[]>(Copy(files));

        private static SortedDictionary<string, byte[]> Copy(IDictionary<string, byte[]> source)
        {
            var result = new SortedDictionary<string, byte[]>(StringComparer.Ordinal);
            foreach (var kv in source)
            {
                if (kv.Value == null) throw new ArgumentNullException(nameof(source), "File contents cannot be null.");
                result.Add(kv.Key, (byte[])kv.Value.Clone());
            }
            return result;
        }
    }
}
