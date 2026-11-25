using System.Collections.Generic;
using System.Linq;
using Newtonsoft.Json.Linq;

namespace LeafSolarDesign.ScriptHost.Package
{
    public sealed class PackageValidationResult
    {
        private readonly JObject manifest;

        internal PackageValidationResult(IEnumerable<string> failures, JObject manifest)
        {
            Failures = System.Array.AsReadOnly(failures.ToArray());
            this.manifest = (JObject)manifest?.DeepClone();
            Ok = Failures.Count == 0 && manifest != null;
        }

        public bool Ok { get; }
        public IReadOnlyList<string> Failures { get; }
        public JObject Manifest => (JObject)manifest?.DeepClone();
    }
}
