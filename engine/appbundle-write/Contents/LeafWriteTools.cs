// ===========================================================================
// LeafWriteTools.cs  -  Leaf drawing.write COMPILED productization path (skeleton)
// ---------------------------------------------------------------------------
// The C# / ObjectARX equivalent of the LISP mutate spike in da/write_lisp.py.
// The LISP spike (da/write_spike.py + LeafWriteProbe Activity) is the PROOF of
// capability; this bundle is the later productization path for when a write tool
// wants real .NET / full ObjectARX access (transactions, geometry libs, error
// handling) instead of a headless .scr.
//
// One CommandMethod: LEAFWRITEPROBE. Same load-bearing order as the LISP recipe:
//   1. Erase() one pre-existing model-space Polyline (the DELETE).
//   2. Ensure layer LEAF_WRITE_PROBE exists (the LayerTable Make).
//   3. Append a closed Polyline on LEAF_WRITE_PROBE (the ADD).
//   4. Database.SaveAs("output.dwg") — the Activity's Result localName.
// Re-extracting output.dwg then shows the probe layer + polyline (ADD) and
// exactly one original polyline handle gone (DELETE) — identical assertions to
// the LISP spike.
//
// Pattern mirrors ../appbundle/Contents/LeafDaTools.cs (assembly attrs,
// Transaction / BlockTable / ModelSpace idioms). Managed AutoCAD refs are
// Private=false: the DA engine supplies acdbmgd/accoremgd at runtime.
//
// SKELETON: deliberately NOT built here (no guarantee the AutoCAD refs resolve
// on this host). The LISP spike is the demonstrated capability — see BUILD.md.
// ===========================================================================
using System;
using System.Diagnostics;
using System.IO;
using Autodesk.AutoCAD.ApplicationServices.Core;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.Geometry;
using Autodesk.AutoCAD.Runtime;

[assembly: ExtensionApplication(typeof(LeafWriteTools.LeafWriteApp))]
[assembly: CommandClass(typeof(LeafWriteTools.LeafWriteCommands))]

namespace LeafWriteTools
{
    public class LeafWriteApp : IExtensionApplication
    {
        public void Initialize() { LeafWriteCommands.Log("LeafWriteTools loaded"); }
        public void Terminate() { }
    }

    public class LeafWriteCommands
    {
        private const string ProbeLayer = "LEAF_WRITE_PROBE";
        private const string OutFile = "output.dwg";   // Activity Result localName
        private const string LogFile = "LeafWriteTools.log";

        internal static void Log(string msg)
        {
            try { File.AppendAllText(LogFile, $"[{DateTime.Now:HH:mm:ss.fff}] {msg}\r\n"); } catch { }
        }

        // ---- drawing.write probe (engine_op: write_probe) ------------------
        [CommandMethod("LEAFWRITEPROBE")]
        public void WriteProbe()
        {
            var sw = Stopwatch.StartNew();
            var db = Application.DocumentManager.MdiActiveDocument.Database;
            using (var tr = db.TransactionManager.StartTransaction())
            {
                var bt = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
                var ms = (BlockTableRecord)tr.GetObject(bt[BlockTableRecord.ModelSpace],
                                                        OpenMode.ForWrite);

                // 1) DELETE one pre-existing model-space polyline.
                foreach (ObjectId id in ms)
                {
                    if (tr.GetObject(id, OpenMode.ForRead) is Polyline pl)
                    {
                        var w = (Polyline)tr.GetObject(id, OpenMode.ForWrite);
                        w.Erase();
                        Log($"deleted polyline handle {pl.Handle}");
                        break;   // exactly one
                    }
                }

                // 2) Ensure the probe layer exists.
                var lt = (LayerTable)tr.GetObject(db.LayerTableId, OpenMode.ForRead);
                ObjectId layerId;
                if (lt.Has(ProbeLayer))
                {
                    layerId = lt[ProbeLayer];
                }
                else
                {
                    var wlt = (LayerTable)tr.GetObject(db.LayerTableId, OpenMode.ForWrite);
                    using (var ltr = new LayerTableRecord { Name = ProbeLayer })
                    {
                        layerId = wlt.Add(ltr);
                        tr.AddNewlyCreatedDBObject(ltr, true);
                    }
                }

                // 3) ADD a closed 4-vertex polyline on the probe layer.
                using (var probe = new Polyline())
                {
                    probe.AddVertexAt(0, new Point2d(0.0, 0.0), 0, 0, 0);
                    probe.AddVertexAt(1, new Point2d(10.0, 0.0), 0, 0, 0);
                    probe.AddVertexAt(2, new Point2d(10.0, 10.0), 0, 0, 0);
                    probe.AddVertexAt(3, new Point2d(0.0, 10.0), 0, 0, 0);
                    probe.Closed = true;
                    probe.LayerId = layerId;
                    ms.AppendEntity(probe);
                    tr.AddNewlyCreatedDBObject(probe, true);
                    Log($"added probe polyline handle {probe.Handle}");
                }

                tr.Commit();
            }

            // 4) SAVEAS output.dwg (keep current DWG version).
            db.SaveAs(OutFile, DwgVersion.Current);
            Log($"saved {OutFile} in {sw.ElapsedMilliseconds} ms");
        }
    }
}
