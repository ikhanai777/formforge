using System;

// --------------------------------------------------------------------------
// Stand-ins for the SOLIDWORKS interop assemblies.
//
// These exist for one reason: to let the add-in's source be compiled on a
// machine that has no SOLIDWORKS installed, so that syntax, typing and
// dependency errors are caught rather than discovered by whoever first opens
// the solution on a CAD workstation.
//
// WHAT THIS PROVES AND WHAT IT DOES NOT
//
// It proves the add-in is internally consistent: every name resolves, every
// call type-checks, nothing is missing. It does NOT prove the signatures below
// match a real SOLIDWORKS release — they are the add-in's own claims about the
// API, not a copy of it. Only building against the real
// SolidWorks.Interop.*.dll on a workstation proves that.
//
// This is also why the add-in binds so little at compile time. Everything
// below is API that has been stable across every supported release; the
// version-sensitive calls go through SwDispatch at runtime instead, where a
// wrong guess is a logged warning rather than a build failure. The list below
// is therefore the complete compile-time surface, and it is deliberately
// short.
//
// The real assemblies are referenced by DrawingForge.AddIn.csproj. This project
// never ships.
// --------------------------------------------------------------------------

namespace SolidWorks.Interop.sldworks
{
    public interface ISldWorks
    {
        object ActiveDoc { get; }

        object OpenDoc6(string fileName, int type, int options, string configuration,
                        ref int errors, ref int warnings);

        object ActivateDoc3(string name, bool useUserPreferences, int option, ref int errors);

        bool CloseDoc(string name);

        object NewDocument(string templateName, int paperSize, double width, double height);

        string GetUserPreferenceStringValue(int preference);

        bool SetUserPreferenceToggle(int preference, bool value);

        ICommandManager GetCommandManager(int cookie);

        bool SetAddinCallbackInfo(int extension, object addin, int cookie);
    }

    public interface ICommandManager
    {
        ICommandGroup CreateCommandGroup2(int userId, string title, string toolTip, string hint,
                                          int position, bool ignorePreviousVersion, ref int errors);

        bool RemoveCommandGroup(int userId);
    }

    public interface ICommandGroup
    {
        bool HasToolbar { get; set; }
        bool HasMenu { get; set; }

        int AddCommandItem2(string name, int position, string hintString, string toolTip,
                            int imageListIndex, string callbackFunction, string enableMethod,
                            int userId, int menuToolbarOption);

        bool Activate();
    }

    public interface ModelDoc2
    {
        ModelDocExtension Extension { get; }

        string GetPathName();
        string GetTitle();
        bool ClearSelection2(bool all);
    }

    public interface ModelDocExtension
    {
        // CustomPropertyManager is a parameterised COM property and is reached
        // through SwDispatch.TryGetIndexed, not bound here.

        bool SelectByID2(string name, string type, double x, double y, double z,
                         bool append, int mark, object callout, int selectOption);

        bool SaveAs(string name, int version, int options, object exportData,
                    ref int errors, ref int warnings);
    }

    public interface CustomPropertyManager
    {
    }

    public interface DrawingDoc
    {
    }

    public interface View
    {
    }

    public interface AssemblyDoc
    {
    }

    public interface PartDoc
    {
    }

    public interface Component2
    {
        string Name2 { get; }
        string ReferencedConfiguration { get; set; }

        string GetPathName();
    }
}

namespace SolidWorks.Interop.swpublished
{
    public interface ISwAddin
    {
        bool ConnectToSW(object thisSw, int cookie);
        bool DisconnectFromSW();
    }
}
