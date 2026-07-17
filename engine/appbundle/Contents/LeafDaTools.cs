// ===========================================================================
// LeafDaTools.cs  -  Leaf Web Demo COMPILED-tool option (Lane B, step 4)
// ---------------------------------------------------------------------------
// The kind:"appbundle" alternative to the LISP scripts in ../tools/. Same three
// engine_ops, implemented in C# against acdbmgd/accoremgd, loaded into the
// headless AutoCAD DA engine as a NETLOAD AppBundle (see PackageContents.xml).
// Each CommandMethod does its read-only work and writes result.json (Result
// envelope, CONTRACT.md section 3) to the DA working directory, which the
// Activity uploads. Pure-python oracle for all three: ../selfcheck.py.
//
// Pattern lifted from C:\tmp\hot-script-tier\spike\SpikeCommands.cs:
//   [assembly: ExtensionApplication] + [assembly: CommandClass],
//   Transaction / BlockTable / ModelSpace idioms, file-based logging.
// Roslyn hot-compile is NOT needed here (these are pre-compiled), so the spike's
// AssemblyResolve shim is dropped; keep it only if you later NETLOAD Roslyn.
//
// SKELETON: illustrative, not required to fully build for the demo (BUILD.md).
// ===========================================================================
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text;
using Autodesk.AutoCAD.ApplicationServices.Core;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.Geometry;
using Autodesk.AutoCAD.Runtime;

[assembly: ExtensionApplication(typeof(LeafDaTools.LeafApp))]
[assembly: CommandClass(typeof(LeafDaTools.LeafCommands))]

namespace LeafDaTools
{
    public class LeafApp : IExtensionApplication
    {
        public void Initialize() { LeafCommands.Log("LeafDaTools loaded"); }
        public void Terminate() { }
    }

    public class LeafCommands
    {
        // In DA the working dir is the WorkItem sandbox; result.json is a declared
        // output. params.json (optional) is a declared input the Activity stages.
        private const string ResultFile = "result.json";
        private const string ParamsFile = "params.json";
        private const string LogFile = "LeafDaTools.log";

        internal static void Log(string msg)
        {
            try { File.AppendAllText(LogFile, $"[{DateTime.Now:HH:mm:ss.fff}] {msg}\r\n"); } catch { }
        }

        // ---- count-by-layer (engine_op: count_by_layer) --------------------
        [CommandMethod("LEAFCOUNT")]
        public void CountByLayer()
        {
            var sw = Stopwatch.StartNew();
            var counts = new Dictionary<string, int>();
            var db = Application.DocumentManager.MdiActiveDocument.Database;
            using (var tr = db.TransactionManager.StartTransaction())
            {
                var bt = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
                var ms = (BlockTableRecord)tr.GetObject(bt[BlockTableRecord.ModelSpace], OpenMode.ForRead);
                foreach (ObjectId id in ms)
                {
                    var ent = tr.GetObject(id, OpenMode.ForRead) as Entity;
                    if (ent == null) continue;
                    counts.TryGetValue(ent.Layer, out int c);
                    counts[ent.Layer] = c + 1;
                }
                tr.Commit();
            }
            var sb = new StringBuilder();
            bool first = true;
            foreach (var kv in counts)
            {
                sb.Append(first ? "" : ",").Append(Json(kv.Key)).Append(':').Append(kv.Value);
                first = false;
            }
            WriteEnvelope("count-by-layer",
                $"{{\"counts\":{{{sb}}}}}", null, sw.ElapsedMilliseconds);
        }

        // ---- measure-panel-area (engine_op: measure_panel_area) ------------
        // UNITS ASSUMPTION: 1 drawing unit = 1 inch; sqft = in2 / 144.
        [CommandMethod("LEAFAREA")]
        public void MeasurePanelArea()
        {
            var sw = Stopwatch.StartNew();
            double totalIn2 = 0.0;
            var byLayer = new Dictionary<string, double>();
            var db = Application.DocumentManager.MdiActiveDocument.Database;
            using (var tr = db.TransactionManager.StartTransaction())
            {
                var bt = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
                var ms = (BlockTableRecord)tr.GetObject(bt[BlockTableRecord.ModelSpace], OpenMode.ForRead);
                foreach (ObjectId id in ms)
                {
                    if (tr.GetObject(id, OpenMode.ForRead) is Polyline pl && pl.Closed)
                    {
                        double a = Math.Abs(pl.Area);        // AutoCAD's own area, in2
                        totalIn2 += a;
                        byLayer.TryGetValue(pl.Layer, out double s);
                        byLayer[pl.Layer] = s + a;
                    }
                }
                tr.Commit();
            }
            var sb = new StringBuilder();
            bool first = true;
            foreach (var kv in byLayer)
            {
                sb.Append(first ? "" : ",").Append(Json(kv.Key)).Append(':').Append(Num(kv.Value / 144.0));
                first = false;
            }
            var result = $"{{\"area_sqft\":{Num(totalIn2 / 144.0)},\"by_layer_sqft\":{{{sb}}}," +
                         "\"units_assumption\":\"1 drawing unit = 1 inch; sqft = in2 / 144\"}";
            WriteEnvelope("measure-panel-area", result, null, sw.ElapsedMilliseconds);
        }

        // ---- highlight-panels-near-edge (engine_op: highlight_panels_near_edge)
        [CommandMethod("LEAFHIGHLIGHT")]
        public void HighlightNearEdge()
        {
            var sw = Stopwatch.StartNew();
            string layer; double distance;
            ReadParams(out layer, out distance);

            var handles = new List<string>();
            double minx = double.MaxValue, miny = double.MaxValue,
                   maxx = double.MinValue, maxy = double.MinValue;
            var db = Application.DocumentManager.MdiActiveDocument.Database;
            var polys = new List<Polyline>();
            using (var tr = db.TransactionManager.StartTransaction())
            {
                var bt = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
                var ms = (BlockTableRecord)tr.GetObject(bt[BlockTableRecord.ModelSpace], OpenMode.ForRead);
                // pass 1: extents over ALL polylines
                foreach (ObjectId id in ms)
                {
                    if (tr.GetObject(id, OpenMode.ForRead) is Polyline pl)
                    {
                        polys.Add(pl);
                        var ext = pl.GeometricExtents;
                        minx = Math.Min(minx, ext.MinPoint.X); miny = Math.Min(miny, ext.MinPoint.Y);
                        maxx = Math.Max(maxx, ext.MaxPoint.X); maxy = Math.Max(maxy, ext.MaxPoint.Y);
                    }
                }
                // pass 2: centroid distance to nearest extents edge
                foreach (var pl in polys)
                {
                    if (pl.Layer != layer || !pl.Closed) continue;
                    double cx = 0, cy = 0; int n = pl.NumberOfVertices;
                    for (int i = 0; i < n; i++) { var p = pl.GetPoint2dAt(i); cx += p.X; cy += p.Y; }
                    cx /= n; cy /= n;
                    double d = Math.Min(Math.Min(cx - minx, maxx - cx), Math.Min(cy - miny, maxy - cy));
                    if (d <= distance) handles.Add(pl.Handle.ToString());
                }
                tr.Commit();
            }
            var hs = string.Join(",", handles.Select(Json));
            var result = $"{{\"layer\":{Json(layer)},\"distance\":{Num(distance)},\"matched\":{handles.Count}," +
                         $"\"extents\":{{\"min\":[{Num(minx)},{Num(miny)}],\"max\":[{Num(maxx)},{Num(maxy)}]}}}}";
            var overlay = $"{{\"highlight_handles\":[{hs}]}}";
            WriteEnvelope("highlight-panels-near-edge", result, overlay, sw.ElapsedMilliseconds);
        }

        // ---- helpers -------------------------------------------------------
        private static void ReadParams(out string layer, out double distance)
        {
            layer = "Panels"; distance = 200.0;
            try
            {
                if (!File.Exists(ParamsFile)) return;
                var txt = File.ReadAllText(ParamsFile);
                var m = System.Text.RegularExpressions.Regex.Match(txt, "\"layer\"\\s*:\\s*\"([^\"]*)\"");
                if (m.Success) layer = m.Groups[1].Value;
                var d = System.Text.RegularExpressions.Regex.Match(txt, "\"distance\"\\s*:\\s*([-0-9.eE]+)");
                if (d.Success) distance = double.Parse(d.Groups[1].Value, CultureInfo.InvariantCulture);
            }
            catch (System.Exception ex) { Log("param parse failed: " + ex.Message); }
        }

        private static void WriteEnvelope(string tool, string resultJson, string overlayJson, long ms)
        {
            var env = new StringBuilder();
            env.Append("{\"ok\":true,\"tool\":").Append(Json(tool)).Append(",\"version\":\"1.0.0\",")
               .Append("\"result\":").Append(resultJson).Append(',')
               .Append("\"overlay\":").Append(overlayJson ?? "null").Append(',')
               .Append("\"timing_ms\":").Append(ms).Append(',')
               .Append("\"cost\":null,\"error\":null}");
            File.WriteAllText(ResultFile, env.ToString());
            Log($"{tool}: wrote {ResultFile} in {ms} ms");
        }

        private static string Json(string s) =>
            "\"" + (s ?? "").Replace("\\", "\\\\").Replace("\"", "\\\"") + "\"";

        private static string Num(double d) =>
            Math.Round(d, 3).ToString("0.###", CultureInfo.InvariantCulture);
    }
}
