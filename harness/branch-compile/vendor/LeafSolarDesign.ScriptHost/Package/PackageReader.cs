using System;
using System.Collections.Generic;
using System.IO;
using System.IO.Compression;
using System.Linq;

namespace LeafSolarDesign.ScriptHost.Package
{
    public static class PackageReader
    {
        // Fails closed on null inputs; copies bytes without validating manifest or layout.
        public static ToolPackage FromFiles(string dirName, IDictionary<string, byte[]> files)
        {
            if (dirName == null) throw new ArgumentNullException(nameof(dirName));
            if (files == null) throw new ArgumentNullException(nameof(files));
            return new ToolPackage(dirName, files);
        }

        // Fails closed on >5 MiB archive bytes (4 MiB + header headroom), bounding the directory before opening, and on unsafe paths, roots, >256 entries, >64 files or >4 MiB expanded data.
        public static ToolPackage ReadZip(Stream zip)
        {
            if (zip == null) throw new ArgumentNullException(nameof(zip));
            try
            {
                using (var transport = new MemoryStream())
                using (var archive = OpenBoundedArchive(zip, transport))
                {
                    if (archive.Entries.Count > 256) throw Format("V16", "more than 256 archive entries");
                    var names = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
                    var fileEntries = new List<ZipArchiveEntry>();
                    string root = null;
                    foreach (var entry in archive.Entries)
                    {
                        var name = entry.FullName;
                        var directory = name.EndsWith("/", StringComparison.Ordinal);
                        var path = directory ? name.Substring(0, name.Length - 1) : name;
                        if (path.Length == 0 || name.StartsWith("/", StringComparison.Ordinal)
                            || name.Contains("\\") || name.Contains(":")
                            || path.Split('/').Any(s => s.Length == 0 || s == "." || s == ".."))
                            throw Format("V16", "unsafe archive path '" + name + "'");
                        if (!names.Add(name)) throw Format("V16", "duplicate archive path '" + name + "'");
                        if (directory) continue;
                        fileEntries.Add(entry);
                    }
                    foreach (var entry in fileEntries)
                    {
                        var slash = entry.FullName.IndexOf('/');
                        if (slash < 0) throw Format("V3", "transport archive must contain exactly one top-level directory");
                        var top = entry.FullName.Substring(0, slash);
                        if (root != null && root != top)
                            throw Format("V3", "transport archive must contain exactly one top-level directory");
                        root = top;
                    }
                    if (root == null) throw Format("V3", "transport archive must contain exactly one top-level directory");
                    if (fileEntries.Count > 64) throw Format("V16", "more than 64 files");

                    const int limit = 4 * 1024 * 1024;
                    var buffer = new byte[8192];
                    long total = 0;
                    var files = new SortedDictionary<string, byte[]>(StringComparer.Ordinal);
                    foreach (var entry in fileEntries)
                    {
                        using (var input = entry.Open())
                        using (var output = new MemoryStream())
                        {
                            int count;
                            while ((count = input.Read(buffer, 0, (int)Math.Min(buffer.Length, limit - total + 1))) != 0)
                            {
                                total += count;
                                if (total > limit) throw Format("V16", "package exceeds 4 MiB");
                                output.Write(buffer, 0, count);
                            }
                            files.Add(entry.FullName.Substring(root.Length + 1), output.ToArray());
                        }
                    }
                    return new ToolPackage(root, files);
                }
            }
            catch (InvalidDataException ex)
            {
                throw Format("V16", "invalid transport archive: " + ex.Message);
            }
        }

        private static ZipArchive OpenBoundedArchive(Stream zip, MemoryStream transport)
        {
            const int archiveLimit = 5 * 1024 * 1024;
            var buffer = new byte[8192];
            int count;
            while ((count = zip.Read(buffer, 0, (int)Math.Min(buffer.Length, archiveLimit - transport.Length + 1))) != 0)
            {
                if (transport.Length + count > archiveLimit) throw Format("V16", "archive exceeds 5 MiB");
                transport.Write(buffer, 0, count);
            }
            transport.Position = 0;
            return new ZipArchive(transport, ZipArchiveMode.Read, true);
        }

        private static PackageFormatException Format(string rule, string message) => new PackageFormatException(rule, message);
    }
}
