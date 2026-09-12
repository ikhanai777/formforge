using System.Reflection;
using System.Runtime.InteropServices;

[assembly: AssemblyTitle("DrawingForge")]
[assembly: AssemblyDescription("Generates fabrication-ready drawings for every part in a SOLIDWORKS assembly.")]
[assembly: AssemblyProduct("DrawingForge")]
[assembly: AssemblyVersion("0.1.0.0")]
[assembly: AssemblyFileVersion("0.1.0.0")]

// The assembly is not COM-visible as a whole; only the add-in class is, and it
// carries its own [ComVisible(true)]. Exposing everything would put every
// internal helper in the type library.
[assembly: ComVisible(false)]

// Fixed so regasm produces the same type library identity on every machine.
[assembly: Guid("B4D1F0A6-2E77-4C38-9E5A-1F6C0B93D742")]
