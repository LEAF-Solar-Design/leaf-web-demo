using System.Collections.Generic;

namespace LeafSolarDesign.ScriptHost
{
    /// <summary>
    /// Paths are checked against the package's declared file globs.
    /// </summary>
    public interface IToolFiles
    {
        string ReadText(string relativePath);
        void WriteText(string relativePath, string text);
        IReadOnlyList<string> List(string relativeGlob);
    }
}
