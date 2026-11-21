using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Runtime.CompilerServices;
using Newtonsoft.Json.Linq;

namespace LeafSolarDesign.ScriptHost.Compile
{
    public sealed class ReferenceSet
    {
        public IReadOnlyList<string> Paths { get; }

        private ReferenceSet(IEnumerable<string> paths)
        {
            if (paths == null) throw new ArgumentNullException(nameof(paths));
            Paths = Array.AsReadOnly(paths.Select(p =>
            {
                if (string.IsNullOrWhiteSpace(p)) throw new ArgumentException("Reference path is empty.", nameof(paths));
                return Path.GetFullPath(p);
            }).Distinct(StringComparer.OrdinalIgnoreCase).ToArray());
        }

        public static ReferenceSet FromPaths(IEnumerable<string> paths) => new ReferenceSet(paths);

        public static IReadOnlyList<string> PlatformPaths()
        {
            var paths = new List<string>();
#if NET
            var names = new HashSet<string>(new[] { "System.Runtime", "System.Private.CoreLib", "netstandard", "mscorlib",
                "System.Linq", "System.Collections", "System.Collections.Concurrent", "System.Text.RegularExpressions",
                "System.Runtime.Extensions", "System.ComponentModel.Primitives", "System.Threading",
                "System.Threading.Tasks", "System.Memory", "System.Private.Uri" }, StringComparer.OrdinalIgnoreCase);
            var tpa = (string)AppContext.GetData("TRUSTED_PLATFORM_ASSEMBLIES") ?? "";
            paths.AddRange(tpa.Split(Path.PathSeparator).Where(p => names.Contains(Path.GetFileNameWithoutExtension(p))));
#else
            paths.Add(typeof(object).Assembly.Location);
            paths.Add(typeof(Uri).Assembly.Location);
            paths.Add(typeof(Enumerable).Assembly.Location);
#endif
            return Array.AsReadOnly(paths.ToArray());
        }

        // Keep AutoCAD type resolution out of FromPaths and PlatformPaths and their callers' JIT paths.
        [MethodImpl(MethodImplOptions.NoInlining)]
        public static ReferenceSet ForCurrentProcess()
        {
            var paths = new List<string>(PlatformPaths());
            paths.Add(typeof(JObject).Assembly.Location);
            paths.Add(typeof(ToolGlobals).Assembly.Location);
            string database = typeof(Autodesk.AutoCAD.DatabaseServices.Database).Assembly.Location;
            paths.Add(database);
            paths.Add(typeof(Autodesk.AutoCAD.EditorInput.Editor).Assembly.Location);
            string acmgd = Path.Combine(Path.GetDirectoryName(database), "acmgd.dll");
            if (File.Exists(acmgd)) paths.Add(acmgd);
            return FromPaths(paths);
        }
    }
}
