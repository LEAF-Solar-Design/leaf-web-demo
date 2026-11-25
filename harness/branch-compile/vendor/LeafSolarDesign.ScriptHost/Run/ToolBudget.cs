using System;
using System.Diagnostics;
using System.Runtime.CompilerServices;
using System.Threading;

// Only compiler-produced submissions need this internal API. Original source is scanned
// before rewriting, including references to this namespace and assembly attributes.
[assembly: InternalsVisibleTo("LeafSolarDesign.BudgetedTool")]

namespace LeafSolarDesign.ScriptHost.Run
{
    internal sealed class ToolBudgetExceededException : Exception
    {
        internal ToolBudgetExceededException() : base("execution budget exceeded") { }
    }

    internal static class ToolBudget
    {
        // Contract: at most 100,000,000 instrumented steps per run, independent of
        // timeout_ms. The clock and ceiling are sampled every 256 steps; a latched
        // failure is checked on EVERY step, including catch-block entry. A one-shot
        // timer also latches the deadline, ending the run at its first instrumented
        // step after the deadline however far apart steps are. No allocation on the Tick path.
        internal const long StepCeiling = 100000000;
        private static readonly AsyncLocal<Scope> current = new AsyncLocal<Scope>();

        internal static Scope Begin(Stopwatch stopwatch, TimeSpan deadline)
        {
            var scope = new Scope(current.Value, stopwatch, deadline);
            current.Value = scope;
            return scope;
        }

        internal static void Tick()
        {
            var scope = current.Value;
            if (scope == null) return;
            scope.ThrowIfExceeded();
            if ((++scope.Steps & 255) == 0 &&
                (scope.Steps >= StepCeiling || scope.Stopwatch.Elapsed >= scope.Deadline))
            {
                Interlocked.CompareExchange(ref scope.Failure, new ToolBudgetExceededException(), null);
                scope.ThrowIfExceeded();
            }
        }

        [MethodImpl(MethodImplOptions.NoInlining)]
        internal static void Enter()
        {
            var scope = current.Value;
            if (scope == null) return;
            Tick();
            try { RuntimeHelpers.EnsureSufficientExecutionStack(); }
            catch (InsufficientExecutionStackException ex)
            {
                Interlocked.CompareExchange(ref scope.Failure, ex, null);
                scope.ThrowIfExceeded();
            }
        }

        internal sealed class Scope : IDisposable
        {
            private readonly Scope previous;
            private readonly Timer timer;
            internal readonly Stopwatch Stopwatch;
            internal readonly TimeSpan Deadline;
            internal long Steps;
            internal Exception Failure;

            internal Scope(Scope previous, Stopwatch stopwatch, TimeSpan deadline)
            {
                this.previous = previous;
                Stopwatch = stopwatch;
                Deadline = deadline;
                var remaining = deadline - stopwatch.Elapsed;
                timer = new Timer(state =>
                {
                    var scope = (Scope)state;
                    Interlocked.CompareExchange(ref scope.Failure, new ToolBudgetExceededException(), null);
                }, this, remaining > TimeSpan.Zero ? remaining : TimeSpan.Zero, Timeout.InfiniteTimeSpan);
            }

            internal void ThrowIfExceeded()
            {
                var failure = Volatile.Read(ref Failure);
                if (failure != null) throw failure;
            }

            public void Dispose()
            {
                timer.Dispose();
                current.Value = previous;
            }
        }
    }
}
