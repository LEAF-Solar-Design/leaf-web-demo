using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text;
using Newtonsoft.Json.Linq;

namespace LeafSolarDesign.ScriptHost.Package
{
    public static class CanonicalJson
    {
        // Fails closed on null and token types outside the contract's integer-only JSON domain.
        public static string Serialize(JToken token)
        {
            if (token == null) throw new ArgumentNullException(nameof(token));
            var sb = new StringBuilder();
            WriteCanonical(token, sb);
            return sb.ToString();
        }

        private static void WriteCanonical(JToken token, StringBuilder sb)
        {
            switch (token.Type)
            {
                case JTokenType.Object:
                    sb.Append('{');
                    var first = true;
                    foreach (var p in ((JObject)token).Properties().OrderBy(p => p.Name, Comparer<string>.Create(CompareCodePoints)))
                    {
                        if (!first) sb.Append(',');
                        first = false;
                        WriteJsonString(p.Name, sb);
                        sb.Append(':');
                        WriteCanonical(p.Value, sb);
                    }
                    sb.Append('}');
                    break;
                case JTokenType.Array:
                    sb.Append('[');
                    var firstArray = true;
                    foreach (var child in (JArray)token)
                    {
                        if (!firstArray) sb.Append(',');
                        firstArray = false;
                        WriteCanonical(child, sb);
                    }
                    sb.Append(']');
                    break;
                case JTokenType.Integer:
                    // Vectors beyond-int64 and beyond-int64-defaults override the gate's Int64 narrowing.
                    sb.Append(((IFormattable)((JValue)token).Value).ToString(null, CultureInfo.InvariantCulture));
                    break;
                case JTokenType.String:
                    WriteJsonString((string)token, sb);
                    break;
                case JTokenType.Boolean:
                    sb.Append((bool)token ? "true" : "false");
                    break;
                case JTokenType.Null:
                    sb.Append("null");
                    break;
                default:
                    throw new ArgumentException("non-canonical token type: " + token.Type, nameof(token));
            }
        }

        private static int CompareCodePoints(string left, string right)
        {
            // S1-d supplementary-key vectors follow runner.py's code point order, overriding the gate's UTF-16 order.
            var i = 0;
            var j = 0;
            while (i < left.Length && j < right.Length)
            {
                var a = char.IsSurrogatePair(left, i) ? char.ConvertToUtf32(left, i) : left[i];
                var b = char.IsSurrogatePair(right, j) ? char.ConvertToUtf32(right, j) : right[j];
                if (a != b) return a.CompareTo(b);
                i += a > 0xffff ? 2 : 1;
                j += b > 0xffff ? 2 : 1;
            }
            return (left.Length - i).CompareTo(right.Length - j);
        }

        private static void WriteJsonString(string value, StringBuilder sb)
        {
            sb.Append('"');
            foreach (var c in value)
            {
                switch (c)
                {
                    case '"': sb.Append("\\\""); break;
                    case '\\': sb.Append("\\\\"); break;
                    case '\b': sb.Append("\\b"); break;
                    case '\f': sb.Append("\\f"); break;
                    case '\n': sb.Append("\\n"); break;
                    case '\r': sb.Append("\\r"); break;
                    case '\t': sb.Append("\\t"); break;
                    default:
                        if (c < 0x20) sb.Append("\\u").Append(((int)c).ToString("x4", CultureInfo.InvariantCulture));
                        else sb.Append(c);
                        break;
                }
            }
            sb.Append('"');
        }
    }
}
