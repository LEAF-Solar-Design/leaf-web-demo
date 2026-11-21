using System;

namespace LeafSolarDesign.ScriptHost.Package
{
    public sealed class PackageFormatException : Exception
    {
        // Fails closed on null rule or message; identifies the rejected transport rule.
        public PackageFormatException(string rule, string message) : base(message ?? throw new ArgumentNullException(nameof(message)))
        {
            Rule = rule ?? throw new ArgumentNullException(nameof(rule));
        }

        public string Rule { get; }
    }
}
