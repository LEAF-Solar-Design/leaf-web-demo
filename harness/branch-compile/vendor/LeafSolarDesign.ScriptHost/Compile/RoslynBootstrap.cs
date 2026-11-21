using System;
using System.IO;
using System.Reflection;
#if NET
using System.Runtime.Loader;
#endif

namespace LeafSolarDesign.ScriptHost.Compile
{
    public static class RoslynBootstrap
    {
        private static readonly object Sync = new object();
        private static bool installed;

        public static void EnsureInstalled()
        {
            lock (Sync)
            {
                if (installed) return;
#if NET
                AssemblyLoadContext.Default.Resolving += (context, name) => Resolve(name);
#else
                AppDomain.CurrentDomain.AssemblyResolve += (sender, args) =>
                {
                    try { return Resolve(new AssemblyName(args.Name)); }
                    catch { return null; }
                };
#endif
                installed = true;
            }
        }

        private static Assembly Resolve(AssemblyName name)
        {
            try
            {
                string simple = name.Name;
                if (string.IsNullOrEmpty(simple) || simple.IndexOfAny(new[] { '/', '\\', ':' }) >= 0) return null;
                if (!simple.StartsWith("Microsoft.CodeAnalysis", StringComparison.Ordinal) &&
                    simple != "System.Collections.Immutable" && simple != "System.Reflection.Metadata" &&
                    simple != "System.Runtime.CompilerServices.Unsafe" && simple != "System.Memory" &&
                    simple != "System.Buffers" && simple != "System.Numerics.Vectors" &&
                    simple != "System.Text.Encoding.CodePages" && simple != "System.Threading.Tasks.Extensions" &&
                    simple != "Newtonsoft.Json") return null;
                string path = Path.Combine(Path.GetDirectoryName(typeof(RoslynBootstrap).Assembly.Location), simple + ".dll");
                if (!File.Exists(path)) return null;
#if NET
                return AssemblyLoadContext.Default.LoadFromAssemblyPath(path);
#else
                return Assembly.LoadFrom(path);
#endif
            }
            catch { return null; }
        }
    }
}
