using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text.Json;
using Autodesk.AutoCAD.ApplicationServices.Core;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.Geometry;
using Autodesk.AutoCAD.Runtime;

[assembly: ExtensionApplication(typeof(LeafWriteTools.LeafWriteApp))]
[assembly: CommandClass(typeof(LeafWriteTools.LeafWriteCommands))]

namespace LeafWriteTools
{
    public sealed class LeafWriteApp : IExtensionApplication
    {
        public void Initialize() { }
        public void Terminate() { }
    }

    public sealed class LeafWriteCommands
    {
        private const string PlanFile = "mutation-plan.json";
        private const string OutputFile = "output.dwg";
        private const string ReceiptFile = "receipt.json";
        private const int MaximumReceiptBytes = 262_144;

        [CommandMethod("LEAFAPPLYMUTATIONS", CommandFlags.Session)]
        public void ApplyMutations()
        {
            DeleteIfPresent(OutputFile);
            DeleteIfPresent(ReceiptFile);
            DeleteIfPresent(ReceiptFile + ".tmp");

            byte[] planBytes = File.ReadAllBytes(PlanFile);
            ValidatedMutationPlan plan = MutationPlanParser.Parse(planBytes);
            Database database = Application.DocumentManager.MdiActiveDocument.Database;
            Dictionary<string, string> addedHandles = new Dictionary<string, string>(StringComparer.Ordinal);

            using (Transaction transaction = database.TransactionManager.StartTransaction())
            {
                BlockTable blockTable = (BlockTable)transaction.GetObject(
                    database.BlockTableId, OpenMode.ForRead);
                BlockTableRecord modelSpace = (BlockTableRecord)transaction.GetObject(
                    blockTable[BlockTableRecord.ModelSpace], OpenMode.ForWrite);

                Dictionary<string, Entity> removals = ResolveEntities(
                    database, transaction, modelSpace, plan.Removed);
                Dictionary<string, Entity> transforms = ResolveEntities(
                    database, transaction, modelSpace, TransformHandles(plan.Transforms));

                foreach (string handle in plan.Removed)
                    removals[handle].UpgradeOpen();
                foreach (PolylineTransform transform in plan.Transforms)
                {
                    Entity entity = transforms[transform.Handle];
                    if (!(entity is Polyline) && !(entity is Polyline3d))
                        throw new MutationPlanException(
                            $"transform handle {transform.Handle} is not a Polyline or Polyline3d");
                    entity.UpgradeOpen();
                }

                Dictionary<string, ObjectId> layers = EnsureLayers(
                    database, transaction, plan.Added);

                foreach (PolylineTransform transform in plan.Transforms)
                    ApplyTransform(transaction, transforms[transform.Handle], transform);
                foreach (string handle in plan.Removed)
                    removals[handle].Erase();
                foreach (AddedPolyline addition in plan.Added)
                {
                    Entity entity = CreatePolyline(addition);
                    entity.LayerId = layers[addition.Layer];
                    modelSpace.AppendEntity(entity);
                    transaction.AddNewlyCreatedDBObject(entity, true);
                    addedHandles.Add(addition.LogicalHandle, entity.Handle.ToString());
                }
                transaction.Commit();
            }

            database.SaveAs(OutputFile, DwgVersion.Current);
            WriteReceipt(plan, addedHandles);
        }

        private static Dictionary<string, Entity> ResolveEntities(
            Database database,
            Transaction transaction,
            BlockTableRecord modelSpace,
            IEnumerable<string> handles)
        {
            Dictionary<string, Entity> result = new Dictionary<string, Entity>(
                StringComparer.OrdinalIgnoreCase);
            foreach (string handleText in handles)
            {
                long value = long.Parse(handleText, NumberStyles.HexNumber, CultureInfo.InvariantCulture);
                ObjectId objectId;
                try
                {
                    objectId = database.GetObjectId(false, new Handle(value), 0);
                }
                catch (Autodesk.AutoCAD.Runtime.Exception)
                {
                    throw new MutationPlanException($"unknown AutoCAD handle {handleText}");
                }
                Entity entity = transaction.GetObject(objectId, OpenMode.ForRead, false) as Entity;
                if (entity == null || entity.OwnerId != modelSpace.ObjectId || entity.IsErased)
                    throw new MutationPlanException(
                        $"handle {handleText} does not name a live model-space entity");
                result.Add(handleText, entity);
            }
            return result;
        }

        private static IEnumerable<string> TransformHandles(IEnumerable<PolylineTransform> transforms)
        {
            foreach (PolylineTransform transform in transforms)
                yield return transform.Handle;
        }

        private static Dictionary<string, ObjectId> EnsureLayers(
            Database database,
            Transaction transaction,
            IEnumerable<AddedPolyline> additions)
        {
            LayerTable layerTable = (LayerTable)transaction.GetObject(
                database.LayerTableId, OpenMode.ForRead);
            Dictionary<string, ObjectId> result = new Dictionary<string, ObjectId>(
                StringComparer.OrdinalIgnoreCase);
            foreach (AddedPolyline addition in additions)
            {
                if (result.ContainsKey(addition.Layer))
                    continue;
                if (layerTable.Has(addition.Layer))
                {
                    result.Add(addition.Layer, layerTable[addition.Layer]);
                    continue;
                }
                if (!layerTable.IsWriteEnabled)
                    layerTable.UpgradeOpen();
                using (LayerTableRecord layer = new LayerTableRecord { Name = addition.Layer })
                {
                    ObjectId layerId = layerTable.Add(layer);
                    transaction.AddNewlyCreatedDBObject(layer, true);
                    result.Add(addition.Layer, layerId);
                }
            }
            return result;
        }

        private static Entity CreatePolyline(AddedPolyline addition)
        {
            if (addition.IsThreeDimensional)
            {
                Point3dCollection points = new Point3dCollection();
                foreach (MutationPoint point in addition.Points)
                    points.Add(new Point3d(point.X, point.Y, point.Z));
                return new Polyline3d(Poly3dType.SimplePoly, points, true);
            }

            Polyline polyline = new Polyline(addition.Points.Count);
            polyline.SetDatabaseDefaults();
            polyline.Elevation = addition.Points[0].Z;
            for (int index = 0; index < addition.Points.Count; index++)
            {
                MutationPoint point = addition.Points[index];
                polyline.AddVertexAt(index, new Point2d(point.X, point.Y), 0.0, 0.0, 0.0);
            }
            polyline.Closed = true;
            return polyline;
        }

        private static void ApplyTransform(
            Transaction transaction,
            Entity entity,
            PolylineTransform transform)
        {
            Point3d centroid = VertexCentroid(transaction, entity);
            if (Math.Abs(transform.RotationDegrees) > 1e-12)
            {
                entity.TransformBy(Matrix3d.Rotation(
                    transform.RotationDegrees * Math.PI / 180.0,
                    Vector3d.ZAxis,
                    centroid));
            }
            if (Math.Abs(transform.Dx) > 1e-12 || Math.Abs(transform.Dy) > 1e-12)
            {
                entity.TransformBy(Matrix3d.Displacement(
                    new Vector3d(transform.Dx, transform.Dy, 0.0)));
            }
        }

        private static Point3d VertexCentroid(Transaction transaction, Entity entity)
        {
            List<Point3d> points = new List<Point3d>();
            if (entity is Polyline polyline)
            {
                for (int index = 0; index < polyline.NumberOfVertices; index++)
                    points.Add(polyline.GetPoint3dAt(index));
            }
            else if (entity is Polyline3d polyline3d)
            {
                foreach (ObjectId vertexId in polyline3d)
                {
                    PolylineVertex3d vertex = (PolylineVertex3d)transaction.GetObject(
                        vertexId, OpenMode.ForRead);
                    points.Add(vertex.Position);
                }
            }
            if (points.Count < 2)
                throw new MutationPlanException($"handle {entity.Handle} has too few vertices");
            double x = 0.0, y = 0.0, z = 0.0;
            foreach (Point3d point in points)
            {
                x += point.X;
                y += point.Y;
                z += point.Z;
            }
            return new Point3d(x / points.Count, y / points.Count, z / points.Count);
        }

        private static void WriteReceipt(
            ValidatedMutationPlan plan,
            IReadOnlyDictionary<string, string> addedHandles)
        {
            object receipt = new
            {
                schema = "leaf.mutation-receipt.v1",
                ok = true,
                plan_sha256 = plan.Sha256,
                counts = new
                {
                    added = plan.Added.Count,
                    removed = plan.Removed.Count,
                    transformed = plan.Transforms.Count,
                },
                logical_to_dwg_handles = addedHandles,
            };
            byte[] bytes = JsonSerializer.SerializeToUtf8Bytes(receipt, new JsonSerializerOptions
            {
                WriteIndented = false,
            });
            if (bytes.Length > MaximumReceiptBytes)
                throw new MutationPlanException("mutation receipt exceeds the byte limit");
            string temporary = ReceiptFile + ".tmp";
            File.WriteAllBytes(temporary, bytes);
            File.Move(temporary, ReceiptFile, true);
        }

        private static void DeleteIfPresent(string path)
        {
            if (File.Exists(path))
                File.Delete(path);
        }
    }
}
