using System;
using System.Collections.Generic;
using System.Globalization;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using DrawingForge.Core.Logging;

namespace DrawingForge.AddIn.Interop
{
    /// <summary>
    /// Late-bound calls into the SOLIDWORKS API.
    /// </summary>
    /// <remarks>
    /// Most of the API is stable enough to call directly, and this add-in does
    /// so wherever it can. A minority of it is not: SOLIDWORKS versions methods
    /// by suffix — <c>InsertModelAnnotations3</c>, <c>AutoBalloon5</c>,
    /// <c>CreateSectionViewAt5</c> — and a release both adds the new suffix and
    /// eventually drops the old one. Binding those at compile time means the
    /// add-in builds against exactly one SOLIDWORKS version and fails to load
    /// on every other.
    ///
    /// So the version-sensitive calls go through here: try each known name
    /// newest first, take whichever the installed release actually has, and
    /// report clearly when none of them exist rather than throwing an opaque
    /// <see cref="MissingMethodException"/> out of the middle of a batch run.
    ///
    /// The exact names and argument lists tried are listed in docs/api-notes.md
    /// so they can be checked against a target release without reading code.
    /// </remarks>
    internal static class SwDispatch
    {
        private const BindingFlags MethodFlags =
            BindingFlags.InvokeMethod | BindingFlags.Public | BindingFlags.Instance;

        private const BindingFlags GetFlags =
            BindingFlags.GetProperty | BindingFlags.Public | BindingFlags.Instance;

        private const BindingFlags SetFlags =
            BindingFlags.SetProperty | BindingFlags.Public | BindingFlags.Instance;

        /// <summary>
        /// Calls the first of <paramref name="methodNames"/> that exists on the
        /// target and accepts <paramref name="args"/>.
        /// </summary>
        /// <returns>True when one of them was called; the return value is in
        /// <paramref name="result"/>.</returns>
        public static bool TryInvoke(object target, string[] methodNames, object[] args,
                                     out object result, ILog log = null)
        {
            result = null;
            if (target == null || methodNames == null) return false;

            Type type = target.GetType();
            List<string> attempts = new List<string>();

            foreach (string name in methodNames)
            {
                try
                {
                    result = type.InvokeMember(name, MethodFlags, null, target, args,
                                               CultureInfo.InvariantCulture);
                    return true;
                }
                catch (MissingMethodException)
                {
                    attempts.Add(name + " (not present)");
                }
                catch (MissingMemberException)
                {
                    attempts.Add(name + " (not present)");
                }
                catch (ArgumentException ex)
                {
                    // Wrong argument count or an unconvertible argument: the
                    // method exists but this release takes a different list.
                    attempts.Add(name + " (argument mismatch: " + ex.Message + ")");
                }
                catch (TargetInvocationException ex)
                {
                    // The method ran and threw. That is a real failure inside
                    // SOLIDWORKS, not a missing overload, so stop here.
                    log.Warn(string.Format(CultureInfo.InvariantCulture,
                        "{0} threw inside SOLIDWORKS: {1}", name, LogExtensions.Describe(ex.InnerException ?? ex)));
                    return false;
                }
                catch (COMException ex)
                {
                    log.Warn(string.Format(CultureInfo.InvariantCulture,
                        "{0} failed: {1}", name, LogExtensions.Describe(ex)));
                    return false;
                }
            }

            log.Debug(string.Format(CultureInfo.InvariantCulture,
                "None of the expected methods are available on {0}: {1}",
                type.Name, string.Join(", ", attempts.ToArray())));
            return false;
        }

        /// <summary>Single-name convenience over <see cref="TryInvoke(object,string[],object[],out object,ILog)"/>.</summary>
        public static bool TryInvoke(object target, string methodName, object[] args,
                                     out object result, ILog log = null)
        {
            return TryInvoke(target, new[] { methodName }, args, out result, log);
        }

        /// <summary>
        /// Calls the first name that works, trying each argument list in turn.
        /// Used where a method both changed name and changed arity.
        /// </summary>
        public static bool TryInvokeAny(object target, string[] methodNames, object[][] argumentLists,
                                        out object result, ILog log = null)
        {
            result = null;
            if (argumentLists == null || argumentLists.Length == 0)
                return TryInvoke(target, methodNames, new object[0], out result, log);

            foreach (object[] args in argumentLists)
            {
                if (TryInvoke(target, methodNames, args, out result, log)) return true;
            }
            return false;
        }

        /// <summary>Reads a property, returning false when it does not exist.</summary>
        public static bool TryGet(object target, string propertyName, out object value, ILog log = null)
        {
            value = null;
            if (target == null) return false;

            try
            {
                value = target.GetType().InvokeMember(propertyName, GetFlags, null, target, null,
                                                      CultureInfo.InvariantCulture);
                return true;
            }
            catch (MissingMemberException)
            {
                log.Debug("Property " + propertyName + " is not available on " + target.GetType().Name + ".");
                return false;
            }
            catch (TargetInvocationException ex)
            {
                log.Warn("Reading " + propertyName + " failed: " + LogExtensions.Describe(ex.InnerException ?? ex));
                return false;
            }
            catch (COMException ex)
            {
                log.Warn("Reading " + propertyName + " failed: " + LogExtensions.Describe(ex));
                return false;
            }
        }

        /// <summary>
        /// Reads a parameterised property — a COM property that takes an
        /// argument, such as <c>IModelDocExtension.CustomPropertyManager[config]</c>.
        /// </summary>
        /// <remarks>
        /// Reflection handles these through GetProperty with arguments, which is
        /// also the only way to reach them without binding to one interop
        /// assembly's idea of what the accessor is called.
        /// </remarks>
        public static bool TryGetIndexed(object target, string propertyName, object[] args,
                                         out object value, ILog log = null)
        {
            value = null;
            if (target == null) return false;

            try
            {
                value = target.GetType().InvokeMember(propertyName, GetFlags, null, target, args,
                                                      CultureInfo.InvariantCulture);
                return value != null;
            }
            catch (MissingMemberException)
            {
                log.Debug("Indexed property " + propertyName + " is not available on " +
                          target.GetType().Name + ".");
                return false;
            }
            catch (TargetInvocationException ex)
            {
                log.Warn("Reading " + propertyName + " failed: " + LogExtensions.Describe(ex.InnerException ?? ex));
                return false;
            }
            catch (COMException ex)
            {
                log.Warn("Reading " + propertyName + " failed: " + LogExtensions.Describe(ex));
                return false;
            }
        }

        /// <summary>Writes a property, returning false when it does not exist.</summary>
        public static bool TrySet(object target, string propertyName, object value, ILog log = null)
        {
            if (target == null) return false;

            try
            {
                target.GetType().InvokeMember(propertyName, SetFlags, null, target, new[] { value },
                                              CultureInfo.InvariantCulture);
                return true;
            }
            catch (MissingMemberException)
            {
                log.Debug("Property " + propertyName + " is not settable on " + target.GetType().Name + ".");
                return false;
            }
            catch (TargetInvocationException ex)
            {
                log.Warn("Setting " + propertyName + " failed: " + LogExtensions.Describe(ex.InnerException ?? ex));
                return false;
            }
            catch (COMException ex)
            {
                log.Warn("Setting " + propertyName + " failed: " + LogExtensions.Describe(ex));
                return false;
            }
        }

        /// <summary>Reads a property as a bool, with a default when it is unavailable.</summary>
        public static bool GetBool(object target, string propertyName, bool fallback, ILog log = null)
        {
            object value;
            if (!TryGet(target, propertyName, out value, log) || value == null) return fallback;
            try { return Convert.ToBoolean(value, CultureInfo.InvariantCulture); }
            catch (InvalidCastException) { return fallback; }
            catch (FormatException) { return fallback; }
        }

        /// <summary>Calls a method that returns a bool, with a default when unavailable.</summary>
        public static bool InvokeBool(object target, string[] methodNames, object[] args, bool fallback,
                                      ILog log = null)
        {
            object result;
            if (!TryInvoke(target, methodNames, args, out result, log) || result == null) return fallback;
            try { return Convert.ToBoolean(result, CultureInfo.InvariantCulture); }
            catch (InvalidCastException) { return fallback; }
            catch (FormatException) { return fallback; }
        }

        /// <summary>
        /// Turns a COM array return value into a double[], which is what most
        /// of the geometry-returning calls hand back.
        /// </summary>
        public static double[] AsDoubles(object value)
        {
            double[] typed = value as double[];
            if (typed != null) return typed;

            object[] boxed = value as object[];
            if (boxed != null)
            {
                double[] result = new double[boxed.Length];
                for (int i = 0; i < boxed.Length; i++)
                {
                    try { result[i] = Convert.ToDouble(boxed[i], CultureInfo.InvariantCulture); }
                    catch (InvalidCastException) { return null; }
                    catch (FormatException) { return null; }
                }
                return result;
            }

            Array array = value as Array;
            if (array != null)
            {
                double[] result = new double[array.Length];
                int index = 0;
                foreach (object item in array)
                {
                    try { result[index++] = Convert.ToDouble(item, CultureInfo.InvariantCulture); }
                    catch (InvalidCastException) { return null; }
                    catch (FormatException) { return null; }
                }
                return result;
            }

            return null;
        }

        /// <summary>Turns a COM array return value into an object[], never null.</summary>
        public static object[] AsObjects(object value)
        {
            object[] typed = value as object[];
            if (typed != null) return typed;

            Array array = value as Array;
            if (array != null)
            {
                object[] result = new object[array.Length];
                int index = 0;
                foreach (object item in array) result[index++] = item;
                return result;
            }

            return value == null ? new object[0] : new[] { value };
        }

        /// <summary>
        /// Releases a COM object without letting a failure there break the run.
        /// Long batches leak SOLIDWORKS handles otherwise.
        /// </summary>
        public static void Release(object comObject)
        {
            if (comObject == null) return;
            if (!Marshal.IsComObject(comObject)) return;
            try { Marshal.ReleaseComObject(comObject); }
            catch (ArgumentException) { }
            catch (InvalidComObjectException) { }
        }

        /// <summary>Names and shapes actually tried, for the diagnostics dialog.</summary>
        public static string DescribeAttempt(string[] names, object[] args)
        {
            StringBuilder sb = new StringBuilder();
            sb.Append(string.Join("/", names ?? new string[0]));
            sb.Append('(').Append(args == null ? 0 : args.Length).Append(" args)");
            return sb.ToString();
        }
    }
}
