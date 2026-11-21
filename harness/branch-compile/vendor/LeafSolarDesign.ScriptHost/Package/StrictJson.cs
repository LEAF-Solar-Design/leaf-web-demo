using System;

namespace LeafSolarDesign.ScriptHost.Package
{
    // Json.NET accepts JavaScript extensions; recognize RFC 8259 first, in one bounded-depth pass.
    internal sealed class StrictJson
    {
        private readonly string text;
        private int position;

        internal StrictJson(string text) { this.text = text; }

        internal void ReadDocument()
        {
            for (var i = 0; i < text.Length; i++)
            {
                if (char.IsHighSurrogate(text[i]))
                {
                    if (i + 1 == text.Length || !char.IsLowSurrogate(text[i + 1])) Invalid("unpaired surrogate");
                    i++;
                }
                else if (char.IsLowSurrogate(text[i])) Invalid("unpaired surrogate");
            }
            ReadValue(0);
            Whitespace();
            if (position != text.Length) Invalid("trailing content");
        }

        private void ReadValue(int depth)
        {
            Whitespace();
            if (position == text.Length) Invalid("expected value");
            var c = text[position];
            if (c == '{' || c == '[')
            {
                if (depth >= 64) Invalid("nesting exceeds 64");
                position++;
                var isObject = c == '{';
                var close = isObject ? '}' : ']';
                Whitespace();
                if (Take(close)) return;
                do
                {
                    if (isObject)
                    {
                        Whitespace();
                        ReadString();
                        Whitespace();
                        if (!Take(':')) Invalid("expected colon");
                    }
                    ReadValue(depth + 1);
                    Whitespace();
                    if (Take(close)) return;
                    if (!Take(',')) Invalid("expected comma or closing delimiter");
                } while (true);
            }
            if (c == '"') { ReadString(); return; }
            if (c == 't') { Literal("true"); return; }
            if (c == 'f') { Literal("false"); return; }
            if (c == 'n') { Literal("null"); return; }
            Take('-');
            if (!Take('0'))
            {
                if (position == text.Length || text[position] < '1' || text[position] > '9') Invalid("invalid value or number");
                Digits();
            }
            if (Take('.')) RequiredDigits();
            if (Take('e') || Take('E'))
            {
                if (!Take('+')) Take('-');
                RequiredDigits();
            }
        }

        private void ReadString()
        {
            if (!Take('"')) Invalid("expected double-quoted string");
            while (position < text.Length)
            {
                var c = text[position++];
                if (c == '"') return;
                if (c < 0x20) Invalid("control character in string");
                if (c != '\\') continue;
                if (position == text.Length) Invalid("incomplete escape");
                c = text[position++];
                if (c == 'u')
                {
                    var code = HexQuad();
                    if (code >= 0xd800 && code <= 0xdbff)
                    {
                        if (!Take('\\') || !Take('u')) Invalid("unpaired surrogate escape");
                        var low = HexQuad();
                        if (low < 0xdc00 || low > 0xdfff) Invalid("unpaired surrogate escape");
                    }
                    else if (code >= 0xdc00 && code <= 0xdfff) Invalid("unpaired surrogate escape");
                }
                else if (c != '"' && c != '\\' && c != '/' && c != 'b' && c != 'f' && c != 'n' && c != 'r' && c != 't')
                    Invalid("invalid string escape");
            }
            Invalid("unterminated string");
        }

        private int HexQuad()
        {
            var value = 0;
            for (var i = 0; i < 4; i++)
            {
                if (position == text.Length) Invalid("incomplete Unicode escape");
                var c = text[position++];
                var digit = c >= '0' && c <= '9' ? c - '0'
                    : c >= 'a' && c <= 'f' ? c - 'a' + 10
                    : c >= 'A' && c <= 'F' ? c - 'A' + 10 : -1;
                if (digit < 0) Invalid("invalid Unicode escape");
                value = value * 16 + digit;
            }
            return value;
        }

        private void Literal(string value)
        {
            foreach (var c in value)
                if (!Take(c)) Invalid("invalid literal");
        }

        private void RequiredDigits()
        {
            var start = position;
            Digits();
            if (position == start) Invalid("expected digit");
        }

        private void Digits()
        {
            while (position < text.Length && text[position] >= '0' && text[position] <= '9') position++;
        }

        private void Whitespace()
        {
            while (position < text.Length && (text[position] == ' ' || text[position] == '\t'
                || text[position] == '\n' || text[position] == '\r')) position++;
        }

        private bool Take(char c)
        {
            if (position == text.Length || text[position] != c) return false;
            position++;
            return true;
        }

        private static void Invalid(string reason) { throw new FormatException(reason); }
    }
}
