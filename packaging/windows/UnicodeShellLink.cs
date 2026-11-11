using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text;

public static class UnicodeShellLink
{
    [ComImport, Guid("00021401-0000-0000-C000-000000000046")]
    private class ShellLink { }

    [ComImport, Guid("000214F9-0000-0000-C000-000000000046"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IShellLinkW
    {
        // Keep native vtable order, including methods not called here.
        void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int size, IntPtr findData, uint flags);
        void GetIDList(out IntPtr pidl);
        void SetIDList(IntPtr pidl);
        void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder text, int size);
        void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string text);
        void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int size);
        void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string path);
        void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder args, int size);
        void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string args);
        void GetHotkey(out ushort hotkey);
        void SetHotkey(ushort hotkey);
        void GetShowCmd(out int show);
        void SetShowCmd(int show);
        void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int size, out int index);
        void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string path, int index);
        void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string path, uint reserved);
        void Resolve(IntPtr window, uint flags);
        void SetPath([MarshalAs(UnmanagedType.LPWStr)] string path);
    }

    public sealed class Entry
    {
        public string TargetPath { get; internal set; }
        public string WorkingDirectory { get; internal set; }
        public string Arguments { get; internal set; }
    }

    public static void Save(string file, string target, string directory, string arguments)
    {
        var link = (IShellLinkW)new ShellLink();
        try
        {
            link.SetPath(target);
            link.SetWorkingDirectory(directory);
            link.SetArguments(arguments);
            link.SetIconLocation(target, 0);
            ((IPersistFile)link).Save(file, true);
        }
        finally { Marshal.FinalReleaseComObject(link); }
    }

    public static Entry Read(string file)
    {
        var link = (IShellLinkW)new ShellLink();
        try
        {
            ((IPersistFile)link).Load(file, 0);
            var path = new StringBuilder(32768);
            var directory = new StringBuilder(32768);
            var arguments = new StringBuilder(32768);
            link.GetPath(path, path.Capacity, IntPtr.Zero, 0);
            link.GetWorkingDirectory(directory, directory.Capacity);
            link.GetArguments(arguments, arguments.Capacity);
            return new Entry { TargetPath = path.ToString(), WorkingDirectory = directory.ToString(), Arguments = arguments.ToString() };
        }
        finally { Marshal.FinalReleaseComObject(link); }
    }
}
