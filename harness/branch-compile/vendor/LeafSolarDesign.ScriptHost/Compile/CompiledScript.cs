using System;
using System.Threading;
using System.Threading.Tasks;
using Microsoft.CodeAnalysis.Scripting;

namespace LeafSolarDesign.ScriptHost.Compile
{
    public sealed class CompiledScript : IDisposable
    {
        private ScriptRunner<object> runner;
        public string PackageDigest { get; }
        public bool IsDisposed => Volatile.Read(ref runner) == null;
        public Func<object, CancellationToken, Task<object>> Entry => Run;
#if NET
        internal ScriptLoadContext LoadContext { get; private set; }
#endif

        internal CompiledScript(string digest, Func<object[], Task<object>> factory
#if NET
            , ScriptLoadContext context
#endif
            )
        {
            PackageDigest = digest;
            runner = (globals, token) =>
            {
                token.ThrowIfCancellationRequested();
                if (!(globals is ToolGlobals)) throw new ArgumentException("Globals must be ToolGlobals.", nameof(globals));
                return factory(new object[] { globals, null });
            };
#if NET
            LoadContext = context;
#endif
        }

        private Task<object> Run(object globals, CancellationToken token)
        {
            var current = Volatile.Read(ref runner);
            if (current == null) throw new ObjectDisposedException(nameof(CompiledScript));
            return current(globals, token);
        }

        public void Dispose()
        {
            if (Interlocked.Exchange(ref runner, null) == null) return;
#if NET
            var context = LoadContext;
            LoadContext = null;
            context?.Unload();
#endif
        }
    }
}
