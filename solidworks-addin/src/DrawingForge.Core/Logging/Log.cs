using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;

namespace DrawingForge.Core.Logging
{
    public enum LogLevel
    {
        Debug = 0,
        Info = 1,
        Warning = 2,
        Error = 3
    }

    /// <summary>Where the add-in writes what it did.</summary>
    public interface ILog
    {
        void Write(LogLevel level, string message);
    }

    /// <summary>Convenience wrappers over <see cref="ILog"/>.</summary>
    public static class LogExtensions
    {
        public static void Debug(this ILog log, string message) { if (log != null) log.Write(LogLevel.Debug, message); }
        public static void Info(this ILog log, string message) { if (log != null) log.Write(LogLevel.Info, message); }
        public static void Warn(this ILog log, string message) { if (log != null) log.Write(LogLevel.Warning, message); }
        public static void Error(this ILog log, string message) { if (log != null) log.Write(LogLevel.Error, message); }

        public static void Error(this ILog log, string message, Exception ex)
        {
            if (log == null) return;
            log.Write(LogLevel.Error, message + " — " + Describe(ex));
        }

        /// <summary>
        /// Exception text that keeps the COM HRESULT, which is usually the only
        /// part of a SOLIDWORKS API failure that says anything useful.
        /// </summary>
        public static string Describe(Exception ex)
        {
            if (ex == null) return "(no exception)";
            StringBuilder sb = new StringBuilder();
            Exception current = ex;
            while (current != null)
            {
                if (sb.Length > 0) sb.Append(" <- ");
                sb.Append(current.GetType().Name).Append(": ").Append(current.Message);

                System.Runtime.InteropServices.COMException com =
                    current as System.Runtime.InteropServices.COMException;
                if (com != null)
                {
                    sb.Append(string.Format(CultureInfo.InvariantCulture, " (HRESULT 0x{0:X8})", com.ErrorCode));
                }
                current = current.InnerException;
            }
            return sb.ToString();
        }
    }

    /// <summary>Collects log lines in memory and optionally mirrors them to a file.</summary>
    public sealed class BufferedLog : ILog
    {
        private readonly List<string> _lines = new List<string>();
        private readonly object _gate = new object();
        private readonly string _path;

        public BufferedLog(string path = null, LogLevel minimum = LogLevel.Info)
        {
            _path = path;
            Minimum = minimum;
        }

        public LogLevel Minimum { get; set; }

        public void Write(LogLevel level, string message)
        {
            if (level < Minimum) return;

            string line = string.Format(CultureInfo.InvariantCulture, "{0:HH:mm:ss} {1,-7} {2}",
                DateTime.Now, level.ToString().ToUpperInvariant(), message);

            lock (_gate)
            {
                _lines.Add(line);
                if (!string.IsNullOrEmpty(_path))
                {
                    try
                    {
                        string folder = Path.GetDirectoryName(_path);
                        if (!string.IsNullOrEmpty(folder) && !Directory.Exists(folder))
                            Directory.CreateDirectory(folder);
                        File.AppendAllText(_path, line + Environment.NewLine);
                    }
                    catch (IOException)
                    {
                        // A log that cannot be written must not stop a run.
                    }
                    catch (UnauthorizedAccessException)
                    {
                    }
                }
            }
        }

        public IList<string> Lines
        {
            get { lock (_gate) { return new List<string>(_lines); } }
        }

        public override string ToString()
        {
            lock (_gate) { return string.Join(Environment.NewLine, _lines.ToArray()); }
        }
    }

    /// <summary>Sends every line to a callback; used to drive the progress dialog.</summary>
    public sealed class CallbackLog : ILog
    {
        private readonly Action<LogLevel, string> _sink;

        public CallbackLog(Action<LogLevel, string> sink)
        {
            if (sink == null) throw new ArgumentNullException("sink");
            _sink = sink;
        }

        public void Write(LogLevel level, string message)
        {
            _sink(level, message);
        }
    }

    /// <summary>Writes to several logs at once.</summary>
    public sealed class CompositeLog : ILog
    {
        private readonly IList<ILog> _logs;

        public CompositeLog(params ILog[] logs)
        {
            _logs = new List<ILog>();
            if (logs != null)
            {
                foreach (ILog log in logs)
                {
                    if (log != null) _logs.Add(log);
                }
            }
        }

        public void Write(LogLevel level, string message)
        {
            foreach (ILog log in _logs) log.Write(level, message);
        }
    }
}
