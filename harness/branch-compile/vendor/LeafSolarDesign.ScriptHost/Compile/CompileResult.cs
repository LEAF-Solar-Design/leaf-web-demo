using System;
using System.Collections.Generic;
using System.Linq;

namespace LeafSolarDesign.ScriptHost.Compile
{
    public sealed class CompileResult
    {
        public ToolDecision Decision { get; }
        public IReadOnlyList<string> Diagnostics { get; }
        public CompiledScript Script { get; }
        public bool GenerationCapReached { get; }

        internal CompileResult(ToolDecision decision, IEnumerable<string> diagnostics,
            CompiledScript script = null, bool generationCapReached = false)
        {
            Decision = decision;
            Diagnostics = Array.AsReadOnly(diagnostics.ToArray());
            Script = decision == ToolDecision.Accept ? script : null;
            GenerationCapReached = generationCapReached;
        }
    }
}
