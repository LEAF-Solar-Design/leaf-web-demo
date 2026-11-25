using System.Threading;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.EditorInput;
using Newtonsoft.Json.Linq;

namespace LeafSolarDesign.ScriptHost
{
    public interface IToolContext
    {
        Document Doc { get; }
        Database Db { get; }
        Editor Ed { get; }
        JObject Args { get; }
        IToolLog Log { get; }
        CancellationToken Cancel { get; }
        T Service<T>();
    }
}
