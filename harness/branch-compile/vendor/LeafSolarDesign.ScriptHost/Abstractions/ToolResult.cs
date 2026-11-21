using System;
using Newtonsoft.Json.Linq;

namespace LeafSolarDesign.ScriptHost
{
    /// <summary>
    /// ToJson is the frozen manifest v1 section 7 invocation envelope; never add keys to it. Host diagnostics go
    /// in ToReceiptJson.
    /// </summary>
    public sealed class ToolResult
    {
        private readonly JToken result;

        public string Tool { get; }
        public string Version { get; }
        public int Generation { get; }
        public string PackageDigest { get; }
        public ToolDecision Decision { get; }
        public bool Ok { get; }
        public JToken Result => result?.DeepClone();
        public string Error { get; }
        public long TimingMs { get; }

        public ToolResult(string tool, string version, string packageDigest, int generation,
            ToolDecision decision, bool ok, JToken result, string error, long timingMs)
        {
            if (generation < 1)
                throw new ArgumentOutOfRangeException(nameof(generation), generation, "Generation must be at least 1.");
            if (ok != (decision == ToolDecision.Accept && error == null))
                throw new ArgumentException("Ok must match an accept decision with no error.", nameof(ok));

            Tool = tool;
            Version = version;
            Generation = generation;
            PackageDigest = packageDigest;
            Decision = decision;
            Ok = ok;
            this.result = result?.DeepClone();
            Error = error;
            TimingMs = timingMs;
        }

        public JObject ToJson()
        {
            return new JObject
            {
                ["ok"] = Ok,
                ["tool"] = Tool,
                ["version"] = Version,
                ["generation"] = Generation,
                ["result"] = Result ?? JValue.CreateNull(),
                ["error"] = Ok ? (JToken)JValue.CreateNull() : new JObject
                {
                    ["code"] = ToolDecisions.Code(Decision),
                    ["message"] = Error ?? ToolDecisions.Code(Decision)
                },
                ["timing_ms"] = TimingMs
            };
        }

        public JObject ToReceiptJson()
        {
            var json = ToJson();
            json["package_digest"] = PackageDigest;
            json["decision"] = ToolDecisions.Code(Decision);
            return json;
        }
    }
}
