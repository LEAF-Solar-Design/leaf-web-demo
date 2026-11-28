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

    internal sealed class ToolCancelledException : Exception
    {
        internal ToolCancelledException() : base("stopped with Esc") { }
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

        internal static Scope Begin(Stopwatch stopwatch, TimeSpan deadline, Func<bool> userBreak = null)
        {
            var scope = new Scope(current.Value, stopwatch, deadline, userBreak);
            current.Value = scope;
            return scope;
        }

        internal static void Tick()
        {
            var scope = current.Value;
            if (scope == null) return;
            scope.ThrowIfExceeded();
            if ((++scope.Steps & 255) == 0)
            {
                scope.PollUserBreak();
                if (scope.Steps >= StepCeiling || scope.Stopwatch.Elapsed >= scope.Deadline)
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
            private readonly int owner = Environment.CurrentManagedThreadId;
            private Func<bool> userBreak;
            private TimeSpan nextPoll;
            internal readonly Stopwatch Stopwatch;
            internal readonly TimeSpan Deadline;
            internal long Steps;
            internal Exception Failure;

            internal Scope(Scope previous, Stopwatch stopwatch, TimeSpan deadline, Func<bool> userBreak)
            {
                this.previous = previous;
                Stopwatch = stopwatch;
                Deadline = deadline;
                this.userBreak = userBreak;
                nextPoll = stopwatch.Elapsed + TimeSpan.FromMilliseconds(100);
                var remaining = deadline - stopwatch.Elapsed;
                timer = new Timer(state =>
                {
                    var scope = (Scope)state;
                    Interlocked.CompareExchange(ref scope.Failure, new ToolBudgetExceededException(), null);
                }, this, remaining > TimeSpan.Zero ? remaining : TimeSpan.Zero, Timeout.InfiniteTimeSpan);
            }

            internal bool PollUserBreak()
            {
                if (Volatile.Read(ref Failure) is ToolCancelledException) return true;
                if (userBreak == null || Environment.CurrentManagedThreadId != owner || Stopwatch.Elapsed < nextPoll)
                    return false;
                nextPoll = Stopwatch.Elapsed + TimeSpan.FromMilliseconds(100);
                try
                {
                    if (!userBreak()) return false;
                    Interlocked.CompareExchange(ref Failure, new ToolCancelledException(), null);
                    return Volatile.Read(ref Failure) is ToolCancelledException;
                }
                catch { userBreak = null; return false; }
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
