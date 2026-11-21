using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.Geometry;

using (var tr = Ctx.Db.TransactionManager.StartTransaction())
{
    var bt = (BlockTable)tr.GetObject(Ctx.Db.BlockTableId, OpenMode.ForRead);
    var ms = (BlockTableRecord)tr.GetObject(bt[BlockTableRecord.ModelSpace], OpenMode.ForWrite);
    var line = new Line(new Point3d(0, 0, 0), new Point3d(10, 0, 0));
    ms.AppendEntity(line);
    tr.AddNewlyCreatedDBObject(line, true);
    tr.Commit();
}
return new { appended = 1 };
