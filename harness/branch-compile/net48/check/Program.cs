using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using LeafSolarDesign.ScriptHost;
using LeafSolarDesign.ScriptHost.Compile;
using LeafSolarDesign.ScriptHost.Package;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace LeafAutomation.ToolCheck.Net48
{
    public static class Program
    {
        public static int Main(string[] args) => Run(args, Console.Out, Console.Error);

        public static int Run(string[] args, TextWriter stdout, TextWriter stderr)
        {
            string packagePath = null, jsonPath = null, digest = null, allowlist = null;
            JObject result;
            int exit;
            try
            {
                if (args == null) throw new ArgumentException("Arguments are required.");
                for (int i = 0; i < args.Length; i++)
                {
                    string option = args[i];
                    if ((option != "--package" && option != "--json-out") || i + 1 == args.Length
                        || string.IsNullOrWhiteSpace(args[i + 1]) || args[i + 1].StartsWith("--", StringComparison.Ordinal))
                        throw new ArgumentException("Usage: tool-check --package <zip> [--json-out <file>]");
                    string value = args[++i];
                    if (option == "--package" && packagePath == null) packagePath = value;
                    else if (option == "--json-out" && jsonPath == null) jsonPath = value;
                    else throw new ArgumentException("Duplicate option: " + option);
                }
                if (packagePath == null) throw new ArgumentException("Missing --package. Usage: tool-check --package <zip> [--json-out <file>]");
                if (jsonPath != null && string.Equals(Path.GetFullPath(packagePath), Path.GetFullPath(jsonPath), StringComparison.OrdinalIgnoreCase))
                {
                    jsonPath = null;
                    throw new ArgumentException("--json-out must differ from --package.");
                }

                allowlist = AllowlistDigest();
                ToolPackage package;
                using (var input = File.OpenRead(packagePath)) package = PackageReader.ReadZip(input);
                digest = PackageIdentity.Digest(package);
                var validation = PackageValidator.Validate(package);
                if (!validation.Ok)
                {
                    result = Report(ToolDecision.Malformed, validation.Failures, digest, allowlist);
                    exit = 1;
                }
                else
                {
                    var compiled = Net48Compiler.Compile(new CompileRequest(digest, package.Files,
                        validation.Manifest["capabilities"].Values<string>().ToArray(), References()));
                    try
                    {
                        // The CLI reports all compiler/scanner refusals as compile_failed.
                        bool accepted = compiled.Decision == ToolDecision.Accept;
                        result = Report(accepted ? ToolDecision.Accept : ToolDecision.CompileFailed,
                            compiled.Diagnostics, digest, allowlist);
                        exit = accepted ? 0 : 1;
                    }
                    finally { compiled.Script?.Dispose(); }
                }
            }
            catch (PackageFormatException ex)
            {
                result = Report(ToolDecision.Malformed, new[] { ex.Rule + ": " + ex.Message }, digest, allowlist);
                exit = 1;
            }
            catch (Exception ex) when (IsUsageOrIo(ex))
            {
                result = Report(ToolDecision.Malformed, new[] { ex.Message }, digest, allowlist);
                stderr.WriteLine(ex.Message);
                exit = 2;
            }

            string json = result.ToString(Formatting.None);
            if (jsonPath != null)
            {
                try { File.WriteAllText(jsonPath, json + Environment.NewLine, new UTF8Encoding(false)); }
                catch (Exception ex) when (IsUsageOrIo(ex))
                {
                    stderr.WriteLine(ex.Message);
                    json = Report(ToolDecision.Malformed, new[] { ex.Message }, digest, allowlist).ToString(Formatting.None);
                    exit = 2;
                }
            }
            stdout.WriteLine(json);
            return exit;
        }

        private static bool IsUsageOrIo(Exception ex) => ex is IOException || ex is UnauthorizedAccessException
            || ex is ArgumentException || ex is NotSupportedException || ex is System.Security.SecurityException;

        private static JObject Report(ToolDecision decision, IEnumerable<string> diagnostics, string digest, string allowlist)
        {
            return new JObject
            {
                ["schema"] = "leaf.tool-check.v1",
                ["ok"] = decision == ToolDecision.Accept,
                ["decision"] = ToolDecisions.Code(decision),
                ["diagnostics"] = new JArray(diagnostics.Take(50).Select(d => d.Length > 500 ? d.Substring(0, 500) : d)),
                ["package_digest"] = digest,
                ["allowlist_sha256"] = allowlist,
                ["framework"] = ".NETFramework,Version=v4.8 reference check on " + RuntimeInformation.FrameworkDescription
            };
        }

        private static string AllowlistDigest()
        {
            using var stream = typeof(ApiAllowlist).Assembly.GetManifestResourceStream("LeafSolarDesign.ScriptHost.api-allowlist.v2.json");
            if (stream == null) throw new IOException("Embedded compiler allowlist is missing.");
            using var bytes = new MemoryStream();
            stream.CopyTo(bytes);
            // ApiPolicy.Parse removes CR bytes before hashing the embedded policy.
            return Convert.ToHexString(SHA256.HashData(bytes.ToArray().Where(b => b != 13).ToArray())).ToLowerInvariant();
        }

        private static ReferenceSet References()
        {
            string root = Environment.GetEnvironmentVariable("NUGET_PACKAGES");
            if (string.IsNullOrEmpty(root)) root = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".nuget", "packages");
            string framework = Path.Combine(root, "microsoft.netframework.referenceassemblies.net48", "1.0.3", "build", ".NETFramework", "v4.8");
            var paths = new List<string>();
            foreach (string name in new[] { "mscorlib.dll", "System.dll", "System.Core.dll" })
                paths.Add(Path.Combine(framework, name));
            paths.Add(Path.Combine(root, "newtonsoft.json", "13.0.3", "lib", "net45", "Newtonsoft.Json.dll"));
            var records = JObject.Parse(File.ReadAllText(Path.Combine(AppContext.BaseDirectory, "acad-refs.lock.json")))["years"]["2023"];
            foreach (string name in new[] { "accoremgd", "acdbmgd", "acmgd" })
            {
                var record = records[name];
                paths.Add(Path.Combine(root, (string)record["package"], (string)record["version"], (string)record["path"]));
            }
            string globals = Environment.GetEnvironmentVariable("LEAF_BRANCH_COMPILE_NET48_GLOBALS");
            paths.Add(string.IsNullOrEmpty(globals)
                ? Path.Combine(AppContext.BaseDirectory, "net48-globals", "LeafSolarDesign.ScriptHost.dll") : globals);
            foreach (string path in paths)
                if (!File.Exists(path)) throw new FileNotFoundException("Missing NuGet reference DLL: " + path, path);
            return ReferenceSet.FromPaths(paths);
        }
    }
}
