#load "lib/geometry.csx"

using System.Collections.Generic;
using System.Linq;
using Autodesk.AutoCAD.DatabaseServices;
using Newtonsoft.Json.Linq;

int minGap = Ctx.Args.Value<int?>("min_gap_in") ?? 18;
string layer = Ctx.Args.Value<string>("layer") ?? "PANELS";
var offenders = new List<string>();

using (var tr = Ctx.Db.TransactionManager.StartTransaction())
{
    var bt = (BlockTable)tr.GetObject(Ctx.Db.BlockTableId, OpenMode.ForRead);
    var ms = (BlockTableRecord)tr.GetObject(bt[BlockTableRecord.ModelSpace], OpenMode.ForRead);
    foreach (ObjectId id in ms)
    {
        var ent = tr.GetObject(id, OpenMode.ForRead) as Entity;
        if (ent == null || ent.Layer != layer) continue;
        double gap = EdgeGapInches(ent.GeometricExtents);
        if (gap < minGap) offenders.Add(id.Handle.ToString());
    }
    tr.Commit();
}

Ctx.Log.Info($"panel-gap-check: {offenders.Count} offenders below {minGap}in on layer {layer}");
return new { offenders, min_gap_in = minGap, layer };
