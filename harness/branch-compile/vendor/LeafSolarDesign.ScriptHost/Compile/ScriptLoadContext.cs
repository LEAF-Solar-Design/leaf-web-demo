#if NET
using System;
using System.Reflection;
using System.Runtime.Loader;

namespace LeafSolarDesign.ScriptHost.Compile
{
    internal sealed class ScriptLoadContext : AssemblyLoadContext
    {
        internal ScriptLoadContext() : base("tool-" + Guid.NewGuid().ToString("N"), isCollectible: true) { }

        protected override Assembly Load(AssemblyName assemblyName)
        {
            // All dependencies use the default context, including System.*, Microsoft.*, Autodesk.*,
            // Ac*, netstandard, mscorlib, Newtonsoft.Json and LeafSolarDesign.*. No private copies.
            return null;
        }
    }
}
#endif
