using System;
using Microsoft.Win32;

namespace DrawingForge.AddIn.Registration
{
    /// <summary>
    /// Writes and removes the registry entries SOLIDWORKS reads to find an
    /// add-in.
    /// </summary>
    /// <remarks>
    /// Two keys matter:
    ///
    /// * <c>HKLM\SOFTWARE\SOLIDWORKS\Addins\{guid}</c> describes the add-in and
    ///   its default value decides whether it is on by default for new users.
    /// * <c>HKCU\SOFTWARE\SOLIDWORKS\AddInsStartup\{guid}</c> is the current
    ///   user's own choice about loading it at startup.
    ///
    /// The HKLM write needs an elevated regasm; the HKCU one does not. If the
    /// machine-wide write fails the per-user one is still attempted and the
    /// failure is reported, because a half-registered add-in that says so is
    /// easier to fix than one that silently does not appear.
    /// </remarks>
    internal static class ComRegistration
    {
        private const string MachineKey = @"SOFTWARE\SOLIDWORKS\Addins\{0}";
        private const string UserKey = @"SOFTWARE\SOLIDWORKS\AddInsStartup\{0}";

        public static void Register(Type type, string title, string description)
        {
            if (type == null) throw new ArgumentNullException("type");
            string guid = "{" + type.GUID.ToString().ToUpperInvariant() + "}";

            try
            {
                using (RegistryKey key = Registry.LocalMachine.CreateSubKey(string.Format(MachineKey, guid)))
                {
                    if (key == null) throw new InvalidOperationException("Could not create the HKLM add-in key.");

                    // 1 means "loaded by default"; SOLIDWORKS reads this once,
                    // when a user first sees the add-in.
                    key.SetValue(null, 1);
                    key.SetValue("Description", description ?? string.Empty);
                    key.SetValue("Title", title ?? string.Empty);
                }
            }
            catch (UnauthorizedAccessException ex)
            {
                throw new InvalidOperationException(
                    "Registering under HKEY_LOCAL_MACHINE needs an elevated command prompt. " +
                    "Run regasm from an administrator shell.", ex);
            }

            try
            {
                using (RegistryKey key = Registry.CurrentUser.CreateSubKey(string.Format(UserKey, guid)))
                {
                    if (key != null) key.SetValue(null, 1);
                }
            }
            catch (UnauthorizedAccessException)
            {
                // The machine key is written; the user can still switch the
                // add-in on from the Add-Ins dialog.
            }
        }

        public static void Unregister(Type type)
        {
            if (type == null) return;
            string guid = "{" + type.GUID.ToString().ToUpperInvariant() + "}";

            TryDelete(Registry.LocalMachine, string.Format(MachineKey, guid));
            TryDelete(Registry.CurrentUser, string.Format(UserKey, guid));
        }

        private static void TryDelete(RegistryKey root, string path)
        {
            try { root.DeleteSubKeyTree(path, false); }
            catch (UnauthorizedAccessException) { }
            catch (ArgumentException) { }
        }
    }
}
