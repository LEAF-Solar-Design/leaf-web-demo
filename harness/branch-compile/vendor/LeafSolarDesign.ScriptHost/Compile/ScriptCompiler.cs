using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;

namespace LeafSolarDesign.ScriptHost.Compile
{
    public sealed class ScriptCompiler
    {
        private readonly object sync = new object();
        private readonly Dictionary<string, CompiledScript> cache = new Dictionary<string, CompiledScript>(StringComparer.Ordinal);
#if NETFRAMEWORK
        private readonly int generationCap;
        private int generations;
#endif

        public ScriptCompiler(int net48GenerationCap = 32)
        {
            if (net48GenerationCap < 1) throw new ArgumentOutOfRangeException(nameof(net48GenerationCap));
#if NETFRAMEWORK
            generationCap = net48GenerationCap;
#endif
            RoslynBootstrap.EnsureInstalled();
        }

        public CompileResult Compile(CompileRequest r)
        {
            if (r == null) throw new ArgumentNullException(nameof(r));
            lock (sync)
            {
                if (cache.TryGetValue(r.PackageDigest, out var cached))
                {
                    if (!cached.IsDisposed) return Accept(cached);
                    cache.Remove(r.PackageDigest);
                }
#if NETFRAMEWORK
                if (generations >= generationCap)
                    return new CompileResult(ToolDecision.CompileFailed,
                        new[] { $"generation cap reached ({generationCap}); restart AutoCAD to load more tools" }, generationCapReached: true);
#endif
                using (var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(60)))
                {
                    try { return CompileCore(r, timeout.Token); }
                    catch (OperationCanceledException) { return Failed("compile timed out"); }
                    catch (Exception ex) when (ex is IOException || ex is ArgumentException || ex is BadImageFormatException || ex is TypeLoadException)
                    { return Failed("main.csx(1,1): compile: " + ex.Message); }
                }
            }
        }

        private CompileResult CompileCore(CompileRequest r, CancellationToken token)
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
            // No metadata resolver: #r cannot expand the caller's explicit reference set.
            var compilation = CSharpCompilation.CreateScriptCompilation("Tool_" + Guid.NewGuid().ToString("N"), tree,
                r.References.Paths.Select(p => MetadataReference.CreateFromFile(p)), options,
                returnType: typeof(object), globalsType: typeof(ToolGlobals));
            var errors = compilation.GetDiagnostics(token).Where(d => d.Severity == DiagnosticSeverity.Error).ToArray();
            var findings = ApiScanner.Scan(compilation, r.Capabilities, token);
            // Policy violations (including unsafe syntax) remain violations when binding also fails.
            if (findings.Count != 0) return new CompileResult(ToolDecision.CapabilityViolation, findings);
            if (errors.Length != 0) return new CompileResult(ToolDecision.CompileFailed, errors.Select(Format));
            // Loaded trees cannot be replaced individually in a script compilation. Rewrite
            // every resolved source, then re-resolve #load against the instrumented text.
            // The fixed friend identity allows internal guards without a public tool API;
            // assemblies remain isolated by LoadFromStream / Assembly.Load(byte[]).
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
                returnType: typeof(object), globalsType: typeof(ToolGlobals));
            using (var stream = new MemoryStream())
            {
                var emitted = compilation.Emit(stream, cancellationToken: token);
                if (!emitted.Success) return new CompileResult(ToolDecision.CompileFailed,
                    emitted.Diagnostics.Where(d => d.Severity == DiagnosticSeverity.Error).Select(Format));
                token.ThrowIfCancellationRequested();
                var entry = compilation.GetEntryPoint(token);
#if NET
                var context = new ScriptLoadContext();
                try
                {
                    stream.Position = 0;
                    var assembly = context.LoadFromStream(stream);
#else
                    var assembly = Assembly.Load(stream.ToArray());
                    generations++;
#endif
                    var method = assembly.GetType(entry.ContainingType.MetadataName, true).GetMethod(entry.MetadataName,
                        BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static);
                    // Bind the emitted submission factory ourselves. Script.CreateDelegate would
                    // load another copy through Roslyn's loader rather than this collectible context.
                    var factory = (Func<object[], Task<object>>)method.CreateDelegate(typeof(Func<object[], Task<object>>));
                    var script = new CompiledScript(r.PackageDigest, factory
#if NET
                        , context
#endif
                        );
                    // Only successfully loaded scripts are cached; failed compiles are never cached.
                    cache.Add(r.PackageDigest, script);
                    return Accept(script);
#if NET
                }
                catch { context.Unload(); throw; }
#endif
            }
        }

        private static string Format(Diagnostic diagnostic)
        {
            var span = diagnostic.Location.GetLineSpan();
            return $"{(string.IsNullOrEmpty(span.Path) ? "main.csx" : span.Path)}({span.StartLinePosition.Line + 1},{span.StartLinePosition.Character + 1}): {diagnostic.Id}: {diagnostic.GetMessage()}";
        }

        private static CompileResult Failed(string message) => new CompileResult(ToolDecision.CompileFailed, new[] { message });
        private static CompileResult Accept(CompiledScript script) => new CompileResult(ToolDecision.Accept, Array.Empty<string>(), script);
    }
}
