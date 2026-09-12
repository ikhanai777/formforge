using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text;

namespace DrawingForge.Core.Reporting
{
    /// <summary>
    /// A small write-only JSON emitter.
    /// </summary>
    /// <remarks>
    /// Reports are written, never parsed back, so a full JSON library would be
    /// a dependency bought for nothing — and this assembly is loaded into the
    /// SOLIDWORKS process, where every dependency is a risk. Null values are
    /// dropped rather than written as null, which keeps the report readable.
    /// </remarks>
    public sealed class JsonWriter
    {
        private readonly StringBuilder _sb = new StringBuilder();
        private readonly Stack<bool> _firstInScope = new Stack<bool>();
        private int _indent;

        public void BeginObject()
        {
            WriteSeparatorIfNeeded();
            _sb.Append('{');
            _firstInScope.Push(true);
            _indent++;
        }

        public void EndObject()
        {
            _indent--;
            bool empty = _firstInScope.Count > 0 && _firstInScope.Pop();
            if (!empty) NewLine();
            _sb.Append('}');
            MarkWritten();
        }

        public void BeginArray()
        {
            WriteSeparatorIfNeeded();
            _sb.Append('[');
            _firstInScope.Push(true);
            _indent++;
        }

        public void EndArray()
        {
            _indent--;
            bool empty = _firstInScope.Count > 0 && _firstInScope.Pop();
            if (!empty) NewLine();
            _sb.Append(']');
            MarkWritten();
        }

        /// <summary>Writes a property name; the caller writes the value next.</summary>
        public void PropertyName(string name)
        {
            WriteSeparatorIfNeeded();
            _sb.Append(Quote(name)).Append(": ");
            // The value that follows belongs to this property, not a new element.
            _pendingValue = true;
        }

        public void Property(string name, string value)
        {
            if (value == null) return;
            PropertyName(name);
            _sb.Append(Quote(value));
            MarkWritten();
        }

        public void Property(string name, bool value)
        {
            PropertyName(name);
            _sb.Append(value ? "true" : "false");
            MarkWritten();
        }

        public void Property(string name, int value)
        {
            PropertyName(name);
            _sb.Append(value.ToString(CultureInfo.InvariantCulture));
            MarkWritten();
        }

        public void Property(string name, double value)
        {
            PropertyName(name);
            if (double.IsNaN(value) || double.IsInfinity(value)) _sb.Append("null");
            else _sb.Append(value.ToString("R", CultureInfo.InvariantCulture));
            MarkWritten();
        }

        public void StringArray(IEnumerable<string> values)
        {
            BeginArray();
            if (values != null)
            {
                foreach (string value in values)
                {
                    WriteSeparatorIfNeeded();
                    _sb.Append(Quote(value ?? string.Empty));
                    MarkWritten();
                }
            }
            EndArray();
        }

        public override string ToString()
        {
            return _sb.ToString();
        }

        // ---- internals -------------------------------------------------------

        private bool _pendingValue;

        private void WriteSeparatorIfNeeded()
        {
            if (_pendingValue)
            {
                _pendingValue = false;
                return;
            }

            if (_firstInScope.Count == 0) return;

            bool first = _firstInScope.Pop();
            if (!first) _sb.Append(',');
            _firstInScope.Push(false);
            NewLine();
        }

        private void MarkWritten()
        {
            // A scalar value consumes the pending-property state; a closing
            // brace has already had it consumed by the matching opener.
            _pendingValue = false;
            if (_firstInScope.Count == 0) return;
            _firstInScope.Pop();
            _firstInScope.Push(false);
        }

        private void NewLine()
        {
            _sb.Append('\n');
            _sb.Append(' ', _indent * 2);
        }

        /// <summary>JSON string escaping, including the control characters.</summary>
        public static string Quote(string value)
        {
            StringBuilder sb = new StringBuilder(value.Length + 2);
            sb.Append('"');
            foreach (char c in value)
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
                        if (c < ' ')
                            sb.Append("\\u").Append(((int)c).ToString("x4", CultureInfo.InvariantCulture));
                        else
                            sb.Append(c);
                        break;
                }
            }
            sb.Append('"');
            return sb.ToString();
        }
    }
}
