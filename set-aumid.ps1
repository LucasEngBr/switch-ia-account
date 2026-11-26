# Gives a .lnk the same AppUserModelID as the Claude app, so Windows groups the Claude window
# under this shortcut and a taskbar pin keeps launching it (instead of the plain Claude app).
param([string[]] $Links, [string] $AppId)
if (-not $Links) {
    $Links = @((Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Claude (ultima conta).lnk'),
               (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Claude.lnk'))
}

if (-not $AppId) {
    $pkg = Get-AppxPackage -Name Claude -ErrorAction SilentlyContinue | Select-Object -First 1
    $app = if ($pkg) { (Get-AppxPackageManifest $pkg).Package.Applications.Application | Select-Object -First 1 }
    if (-not $app) { throw 'App do Claude nao encontrado.' }
    $AppId = "$($pkg.PackageFamilyName)!$($app.Id)"
}

if (-not ('LnkAumid3' -as [type])) {
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
public static class LnkAumid3 {
    [StructLayout(LayoutKind.Sequential, Pack = 4)]
    public struct PROPERTYKEY { public Guid fmtid; public uint pid; }
    [StructLayout(LayoutKind.Sequential)]
    public struct PROPVARIANT { public ushort vt; public ushort r1, r2, r3; public IntPtr p; public IntPtr p2; }
    [ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IPropertyStore {
        [PreserveSig] int GetCount(out uint c);
        [PreserveSig] int GetAt(uint i, out PROPERTYKEY k);
        [PreserveSig] int GetValue(ref PROPERTYKEY k, out PROPVARIANT v);
        [PreserveSig] int SetValue(ref PROPERTYKEY k, ref PROPVARIANT v);
        [PreserveSig] int Commit();
    }
    [ComImport, Guid("00021401-0000-0000-C000-000000000046")] class CShellLink { }
    static PROPERTYKEY Key() { return new PROPERTYKEY { fmtid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), pid = 5 }; }
    public static string Run(string lnk, string id) {
        object link = new CShellLink();
        var pf = (IPersistFile)link;
        pf.Load(lnk, 2 /* STGM_READWRITE */);
        var ps = (IPropertyStore)link;
        var key = Key();
        if (id != null) {
            var v = new PROPVARIANT { vt = 31, p = Marshal.StringToCoTaskMemUni(id) };
            int hr = ps.SetValue(ref key, ref v);
            if (hr < 0) return "SetValue hr=0x" + hr.ToString("X");
            hr = ps.Commit();
            if (hr < 0) return "Commit hr=0x" + hr.ToString("X");
            pf.Save(lnk, true);
            Marshal.FreeCoTaskMem(v.p);
        }
        PROPVARIANT o;
        ps.GetValue(ref key, out o);
        string got = o.vt == 31 ? Marshal.PtrToStringUni(o.p) : "(vazio)";
        Marshal.ReleaseComObject(link);
        return got;
    }
}
'@
}

foreach ($l in $Links) {
    if (-not (Test-Path $l)) { "nao existe: $l"; continue }
    $p = (Resolve-Path $l).Path
    "{0}`n    AppUserModelID = {1}" -f $p, [LnkAumid3]::Run($p, $AppId)
}
