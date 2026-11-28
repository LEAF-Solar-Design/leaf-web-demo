using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text;
using System.Threading;
using LeafSolarDesign.ScriptHost;
using LeafSolarDesign.ScriptHost.Compile;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;

namespace LeafAutomation.ToolCheck.Net48
{
    internal static class Net48Compiler
    {
        public static CompileResult Compile(CompileRequest r)
        {
            if (r == null) throw new ArgumentNullException(nameof(r));
            using (var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(60)))
            {
                try { return CompileCore(r, timeout.Token); }
                catch (OperationCanceledException) { return Failed("compile timed out"); }
                catch (Exception ex) when (ex is IOException || ex is ArgumentException || ex is BadImageFormatException || ex is TypeLoadException)
                { return Failed("main.csx(1,1): compile: " + ex.Message); }
            }
        }

        private static CompileResult CompileCore(CompileRequest r, CancellationToken token)
        {
            var files = r.Files;
            if (files.Count > 64 || files.Sum(p => (long)p.Value.Length) > 4L * 1024 * 1024)
                return Failed("main.csx(1,1): compile: package exceeds 64 files or 4 MiB");
            var sources = new Dictionary<string, string>(StringComparer.Ordinal);
            var utf8 = new UTF8Encoding(false, true);
            foreach (var file in files.Where(p => p.Key.EndsWith(".csx", StringComparison.Ordinal)))
            {
                token.ThrowIfCancellationRequested();
                try { sources.Add(file.Key, utf8.GetString(file.Value)); }
                catch (DecoderFallbackException) { return Failed(file.Key + "(1,1): compile: invalid UTF-8"); }
            }
            if (!sources.TryGetValue("main.csx", out var main)) return Failed("main.csx(1,1): compile: entry source missing");
            var tree = CSharpSyntaxTree.ParseText(main, new CSharpParseOptions(kind: SourceCodeKind.Script), "main.csx", cancellationToken: token);
            var options = new CSharpCompilationOptions(OutputKind.DynamicallyLinkedLibrary,
                usings: new[] { "System", "System.Linq" }, allowUnsafe: true,
                sourceReferenceResolver: new InMemorySourceResolver(sources));
            // Resolve script types by the target assembly identities, without executing them.
            using var metadata = new MetadataLoadContext(new PathAssemblyResolver(r.References.Paths), "mscorlib");
            var returnType = metadata.CoreAssembly.GetType("System.Object", true);
            var globalsPath = r.References.Paths.First(p =>
                string.Equals(Path.GetFileName(p), "LeafSolarDesign.ScriptHost.dll", StringComparison.OrdinalIgnoreCase));
            var globalsType = metadata.LoadFromAssemblyPath(globalsPath).GetType("LeafSolarDesign.ScriptHost.ToolGlobals", true);
            // No metadata resolver: #r cannot expand the explicit reference set.
            var compilation = CSharpCompilation.CreateScriptCompilation("Tool_" + Guid.NewGuid().ToString("N"), tree,
                r.References.Paths.Select(p => MetadataReference.CreateFromFile(p)), options,
                returnType: returnType, globalsType: globalsType);
            var errors = compilation.GetDiagnostics(token).Where(d => d.Severity == DiagnosticSeverity.Error).ToArray();
            var findings = ApiScanner.Scan(compilation, r.Capabilities, token);
            if (findings.Count != 0) return new CompileResult(ToolDecision.CapabilityViolation, findings);
            if (errors.Length != 0) return new CompileResult(ToolDecision.CompileFailed, errors.Select(Format));
            foreach (var sourceTree in compilation.SyntaxTrees)
            {
                token.ThrowIfCancellationRequested();
                sources[sourceTree.FilePath] = new BudgetRewriter(compilation.GetSemanticModel(sourceTree))
                    .Visit(sourceTree.GetRoot(token)).ToFullString();
            }
            tree = CSharpSyntaxTree.ParseText(sources["main.csx"], (CSharpParseOptions)tree.Options,
                "main.csx", cancellationToken: token);
            compilation = CSharpCompilation.CreateScriptCompilation("LeafSolarDesign.BudgetedTool", tree,
                compilation.References, options.WithSourceReferenceResolver(new InMemorySourceResolver(sources)),
                returnType: returnType, globalsType: globalsType);
            using (var stream = new MemoryStream())
            {
                var emitted = compilation.Emit(stream, cancellationToken: token);
                if (!emitted.Success) return new CompileResult(ToolDecision.CompileFailed,
                    emitted.Diagnostics.Where(d => d.Severity == DiagnosticSeverity.Error).Select(Format));
                token.ThrowIfCancellationRequested();
                return new CompileResult(ToolDecision.Accept, Array.Empty<string>());
            }
        }

        private static string Format(Diagnostic diagnostic)
        {
            var span = diagnostic.Location.GetLineSpan();
            return $"{(string.IsNullOrEmpty(span.Path) ? "main.csx" : span.Path)}({span.StartLinePosition.Line + 1},{span.StartLinePosition.Character + 1}): {diagnostic.Id}: {diagnostic.GetMessage()}";
        }

        private static CompileResult Failed(string message) => new CompileResult(ToolDecision.CompileFailed, new[] { message });
    }
}
