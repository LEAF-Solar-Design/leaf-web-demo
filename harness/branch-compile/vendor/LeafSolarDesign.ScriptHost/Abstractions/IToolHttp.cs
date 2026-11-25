using System;
using System.Threading;
using System.Threading.Tasks;

namespace LeafSolarDesign.ScriptHost
{
    /// <summary>
    /// The URI host must match the package's declared hosts.
    /// </summary>
    public interface IToolHttp
    {
        Task<string> GetStringAsync(Uri uri, CancellationToken ct);
    }
}
