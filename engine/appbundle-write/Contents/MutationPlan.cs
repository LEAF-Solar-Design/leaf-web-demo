using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace LeafWriteTools
{
    public sealed class MutationPlanException : Exception
    {
        public MutationPlanException(string message) : base(message) { }
    }

    public sealed class MutationPoint
    {
        public double X { get; }
        public double Y { get; }
        public double Z { get; }

        internal MutationPoint(double x, double y, double z)
        {
            X = x;
            Y = y;
            Z = z;
        }
    }

    public sealed class AddedPolyline
    {
        public string LogicalHandle { get; }
        public string Layer { get; }
        public IReadOnlyList<MutationPoint> Points { get; }
        public bool IsThreeDimensional { get; }

        internal AddedPolyline(string logicalHandle, string layer, List<MutationPoint> points)
        {
            LogicalHandle = logicalHandle;
            Layer = layer;
            Points = points.AsReadOnly();
            IsThreeDimensional = points.Any(point => Math.Abs(point.Z - points[0].Z) > 1e-9);
        }
    }

    public sealed class PolylineTransform
    {
        public string Handle { get; }
        public double Dx { get; }
        public double Dy { get; }
        public double RotationDegrees { get; }

        internal PolylineTransform(string handle, double dx, double dy, double rotationDegrees)
        {
            Handle = handle;
            Dx = dx;
            Dy = dy;
            RotationDegrees = rotationDegrees;
        }
    }

    public sealed class ValidatedMutationPlan
    {
        public const string Schema = "leaf.mutation-plan.v1";

        public string Sha256 { get; }
        public IReadOnlyList<AddedPolyline> Added { get; }
        public IReadOnlyList<string> Removed { get; }
        public IReadOnlyList<PolylineTransform> Transforms { get; }

        internal ValidatedMutationPlan(
            string sha256,
            List<AddedPolyline> added,
            List<string> removed,
            List<PolylineTransform> transforms)
        {
            Sha256 = sha256;
            Added = added.AsReadOnly();
            Removed = removed.AsReadOnly();
            Transforms = transforms.AsReadOnly();
        }
    }

    public static class MutationPlanParser
    {
        public const int MaximumPlanBytes = 1_048_576;
        public const int MaximumOperations = 10_000;
        public const int MaximumAddedPolylines = 2_000;
        public const int MaximumPointsPerPolyline = 10_000;
        public const int MaximumTotalPoints = 100_000;
        public const double MaximumCoordinateMagnitude = 1_000_000_000.0;
        public const double MaximumTranslationMagnitude = 10_000.0;

        private static readonly Regex LogicalHandlePattern = new Regex(
            @"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$",
            RegexOptions.CultureInvariant);
        private static readonly Regex DwgHandlePattern = new Regex(
            @"^[0-9A-Fa-f]{1,16}$",
            RegexOptions.CultureInvariant);
        private static readonly Regex LayerPattern = new Regex(
            @"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,62}$",
            RegexOptions.CultureInvariant);

        public static ValidatedMutationPlan Parse(byte[] exactBytes)
        {
            if (exactBytes == null || exactBytes.Length == 0)
                throw new MutationPlanException("mutation plan is empty");
            if (exactBytes.Length > MaximumPlanBytes)
                throw new MutationPlanException("mutation plan exceeds the byte limit");

            JsonDocument document;
            try
            {
                document = JsonDocument.Parse(exactBytes, new JsonDocumentOptions
                {
                    AllowTrailingCommas = false,
                    CommentHandling = JsonCommentHandling.Disallow,
                    MaxDepth = 16,
                });
            }
            catch (JsonException exception)
            {
                throw new MutationPlanException($"mutation plan is not strict JSON: {exception.Message}");
            }

            using (document)
            {
                JsonElement root = document.RootElement;
                RequireObject(root, "mutation plan");
                RequireExactFields(root, "mutation plan", "schema", "added", "removed", "transforms");
                if (RequireString(root, "schema", "mutation plan") != ValidatedMutationPlan.Schema)
                    throw new MutationPlanException("mutation plan schema is unsupported");

                List<AddedPolyline> added = ParseAdded(RequireArray(root, "added", "mutation plan"));
                List<string> removed = ParseRemoved(RequireArray(root, "removed", "mutation plan"));
                List<PolylineTransform> transforms = ParseTransforms(
                    RequireArray(root, "transforms", "mutation plan"));

                int operations = checked(added.Count + removed.Count + transforms.Count);
                if (operations == 0)
                    throw new MutationPlanException("mutation plan is a no-op");
                if (operations > MaximumOperations)
                    throw new MutationPlanException("mutation plan exceeds the operation limit");

                HashSet<string> occupied = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
                foreach (AddedPolyline item in added)
                    AddUnique(occupied, item.LogicalHandle, "logical handle");
                foreach (string handle in removed)
                    AddUnique(occupied, handle, "conflicting handle");
                foreach (PolylineTransform transform in transforms)
                    AddUnique(occupied, transform.Handle, "conflicting handle");

                string digest = Convert.ToHexString(SHA256.HashData(exactBytes)).ToLowerInvariant();
                return new ValidatedMutationPlan(digest, added, removed, transforms);
            }
        }

        private static List<AddedPolyline> ParseAdded(JsonElement array)
        {
            if (array.GetArrayLength() > MaximumAddedPolylines)
                throw new MutationPlanException("added polylines exceed the limit");
            List<AddedPolyline> result = new List<AddedPolyline>();
            int totalPoints = 0;
            foreach (JsonElement item in array.EnumerateArray())
            {
                RequireObject(item, "added polyline");
                RequireExactFields(item, "added polyline", "handle", "layer", "closed", "pts");
                string handle = RequireString(item, "handle", "added polyline");
                if (!LogicalHandlePattern.IsMatch(handle))
                    throw new MutationPlanException("added polyline handle is unsafe");
                string layer = RequireString(item, "layer", "added polyline");
                if (!LayerPattern.IsMatch(layer))
                    throw new MutationPlanException("added polyline layer is unsafe");
                JsonElement closed = RequireProperty(item, "closed", "added polyline");
                if (closed.ValueKind != JsonValueKind.True)
                    throw new MutationPlanException("added polyline must be closed");

                JsonElement points = RequireArray(item, "pts", "added polyline");
                if (points.GetArrayLength() < 3 || points.GetArrayLength() > MaximumPointsPerPolyline)
                    throw new MutationPlanException("added polyline point count is outside limits");
                totalPoints = checked(totalPoints + points.GetArrayLength());
                if (totalPoints > MaximumTotalPoints)
                    throw new MutationPlanException("mutation plan exceeds the total point limit");
                List<MutationPoint> parsedPoints = new List<MutationPoint>();
                foreach (JsonElement point in points.EnumerateArray())
                {
                    if (point.ValueKind != JsonValueKind.Array || point.GetArrayLength() != 3)
                        throw new MutationPlanException("each added polyline point must contain exactly x, y, z");
                    JsonElement.ArrayEnumerator values = point.EnumerateArray();
                    values.MoveNext(); double x = RequireFiniteNumber(values.Current, "point x", MaximumCoordinateMagnitude);
                    values.MoveNext(); double y = RequireFiniteNumber(values.Current, "point y", MaximumCoordinateMagnitude);
                    values.MoveNext(); double z = RequireFiniteNumber(values.Current, "point z", MaximumCoordinateMagnitude);
                    parsedPoints.Add(new MutationPoint(x, y, z));
                }
                RequireNonDegenerate(parsedPoints);
                result.Add(new AddedPolyline(handle, layer, parsedPoints));
            }
            return result;
        }

        private static List<string> ParseRemoved(JsonElement array)
        {
            List<string> result = new List<string>();
            HashSet<string> seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (JsonElement item in array.EnumerateArray())
            {
                if (item.ValueKind != JsonValueKind.String)
                    throw new MutationPlanException("removed handles must be strings");
                string handle = item.GetString();
                if (handle == null || !DwgHandlePattern.IsMatch(handle))
                    throw new MutationPlanException("removed handle is not an AutoCAD handle");
                AddUnique(seen, handle, "removed handle");
                result.Add(handle.ToUpperInvariant());
            }
            return result;
        }

        private static List<PolylineTransform> ParseTransforms(JsonElement array)
        {
            List<PolylineTransform> result = new List<PolylineTransform>();
            HashSet<string> seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (JsonElement item in array.EnumerateArray())
            {
                RequireObject(item, "transform");
                RequireExactFields(item, "transform", "handle", "dx", "dy", "rotation_deg");
                string handle = RequireString(item, "handle", "transform");
                if (!DwgHandlePattern.IsMatch(handle))
                    throw new MutationPlanException("transform handle is not an AutoCAD handle");
                AddUnique(seen, handle, "transform handle");
                double dx = RequireFiniteNumber(
                    RequireProperty(item, "dx", "transform"), "transform dx", MaximumTranslationMagnitude);
                double dy = RequireFiniteNumber(
                    RequireProperty(item, "dy", "transform"), "transform dy", MaximumTranslationMagnitude);
                double rotation = RequireFiniteNumber(
                    RequireProperty(item, "rotation_deg", "transform"), "transform rotation", 360.0);
                if (Math.Abs(dx) <= 1e-12 && Math.Abs(dy) <= 1e-12 && Math.Abs(rotation) <= 1e-12)
                    throw new MutationPlanException("transform is a no-op");
                result.Add(new PolylineTransform(handle.ToUpperInvariant(), dx, dy, rotation));
            }
            return result;
        }

        private static void RequireNonDegenerate(List<MutationPoint> points)
        {
            MutationPoint origin = points[0];
            for (int i = 1; i < points.Count - 1; i++)
            {
                double ax = points[i].X - origin.X;
                double ay = points[i].Y - origin.Y;
                double az = points[i].Z - origin.Z;
                for (int j = i + 1; j < points.Count; j++)
                {
                    double bx = points[j].X - origin.X;
                    double by = points[j].Y - origin.Y;
                    double bz = points[j].Z - origin.Z;
                    double cx = ay * bz - az * by;
                    double cy = az * bx - ax * bz;
                    double cz = ax * by - ay * bx;
                    if (Math.Sqrt(cx * cx + cy * cy + cz * cz) > 1e-9)
                        return;
                }
            }
            throw new MutationPlanException("added polyline points are degenerate");
        }

        private static double RequireFiniteNumber(JsonElement value, string name, double magnitude)
        {
            if (value.ValueKind != JsonValueKind.Number || !value.TryGetDouble(out double number)
                || double.IsNaN(number) || double.IsInfinity(number))
                throw new MutationPlanException($"{name} must be a finite JSON number");
            if (Math.Abs(number) > magnitude)
                throw new MutationPlanException($"{name} exceeds {magnitude.ToString(CultureInfo.InvariantCulture)}");
            return number == 0.0 ? 0.0 : number;
        }

        private static JsonElement RequireProperty(JsonElement owner, string name, string context)
        {
            if (!owner.TryGetProperty(name, out JsonElement value))
                throw new MutationPlanException($"{context} is missing {name}");
            return value;
        }

        private static JsonElement RequireArray(JsonElement owner, string name, string context)
        {
            JsonElement value = RequireProperty(owner, name, context);
            if (value.ValueKind != JsonValueKind.Array)
                throw new MutationPlanException($"{context}.{name} must be an array");
            return value;
        }

        private static string RequireString(JsonElement owner, string name, string context)
        {
            JsonElement value = RequireProperty(owner, name, context);
            if (value.ValueKind != JsonValueKind.String || string.IsNullOrEmpty(value.GetString()))
                throw new MutationPlanException($"{context}.{name} must be a nonempty string");
            return value.GetString();
        }

        private static void RequireObject(JsonElement value, string context)
        {
            if (value.ValueKind != JsonValueKind.Object)
                throw new MutationPlanException($"{context} must be an object");
        }

        private static void RequireExactFields(JsonElement value, string context, params string[] expected)
        {
            HashSet<string> allowed = new HashSet<string>(expected, StringComparer.Ordinal);
            HashSet<string> seen = new HashSet<string>(StringComparer.Ordinal);
            foreach (JsonProperty property in value.EnumerateObject())
            {
                if (!allowed.Contains(property.Name))
                    throw new MutationPlanException($"{context} contains unknown field {property.Name}");
                if (!seen.Add(property.Name))
                    throw new MutationPlanException($"{context} contains duplicate field {property.Name}");
            }
            if (!allowed.SetEquals(seen))
                throw new MutationPlanException($"{context} does not contain the exact required fields");
        }

        private static void AddUnique(HashSet<string> values, string value, string context)
        {
            if (!values.Add(value))
                throw new MutationPlanException($"duplicate or {context}: {value}");
        }
    }
}
