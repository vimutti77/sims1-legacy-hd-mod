<#
  The Sims Legacy Collection - resolution launcher
  - Temporarily switches the display to a lower resolution (default 1920x1080)
  - Launches the game and waits until it closes
  - Restores the original resolution automatically (even if the game crashes)

  Put this file and Sims4K.bat in the game folder, e.g.
  C:\Program Files (x86)\Steam\steamapps\common\The Sims Legacy Collection\

  Custom resolution:  Sims4K.bat -Width 1280 -Height 720
#>
param(
    [int]$Width  = 1920,
    [int]$Height = 1080,
    [string]$GameExe = (Join-Path $PSScriptRoot 'Sims.exe'),
    [string]$ProcessName = 'Sims',
    [int]$StartTimeoutSec = 120,
    [int]$ExitGraceSec = 8
)

$ErrorActionPreference = 'Stop'

Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

public static class SimsDisplay {
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct DEVMODE {
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string dmDeviceName;
        public short dmSpecVersion;
        public short dmDriverVersion;
        public short dmSize;
        public short dmDriverExtra;
        public int   dmFields;
        public int   dmPositionX;
        public int   dmPositionY;
        public int   dmDisplayOrientation;
        public int   dmDisplayFixedOutput;
        public short dmColor;
        public short dmDuplex;
        public short dmYResolution;
        public short dmTTOption;
        public short dmCollate;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string dmFormName;
        public short dmLogPixels;
        public int   dmBitsPerPel;
        public int   dmPelsWidth;
        public int   dmPelsHeight;
        public int   dmDisplayFlags;
        public int   dmDisplayFrequency;
        public int   dmICMMethod;
        public int   dmICMIntent;
        public int   dmMediaType;
        public int   dmDitherType;
        public int   dmReserved1;
        public int   dmReserved2;
        public int   dmPanningWidth;
        public int   dmPanningHeight;
    }

    [DllImport("user32.dll", CharSet = CharSet.Unicode, EntryPoint = "EnumDisplaySettingsW")]
    static extern bool EnumDisplaySettings(string deviceName, int modeNum, ref DEVMODE devMode);

    [DllImport("user32.dll", CharSet = CharSet.Unicode, EntryPoint = "ChangeDisplaySettingsW")]
    static extern int ChangeDisplaySettings(ref DEVMODE devMode, int flags);

    [DllImport("user32.dll", CharSet = CharSet.Unicode, EntryPoint = "ChangeDisplaySettingsW")]
    static extern int ChangeDisplaySettingsNull(IntPtr devMode, int flags);

    const int ENUM_CURRENT_SETTINGS = -1;
    const int DM_PELSWIDTH  = 0x80000;
    const int DM_PELSHEIGHT = 0x100000;
    const int CDS_TEST       = 0x2;
    const int CDS_FULLSCREEN = 0x4;   // temporary: Windows reverts it when this process exits

    public static string Current() {
        DEVMODE dm = new DEVMODE();
        dm.dmSize = (short)Marshal.SizeOf(typeof(DEVMODE));
        EnumDisplaySettings(null, ENUM_CURRENT_SETTINGS, ref dm);
        return dm.dmPelsWidth + "x" + dm.dmPelsHeight + " @ " + dm.dmDisplayFrequency + "Hz";
    }

    // Returns 0 on success, otherwise a DISP_CHANGE_* error code
    public static int Set(int width, int height) {
        DEVMODE dm = new DEVMODE();
        dm.dmSize = (short)Marshal.SizeOf(typeof(DEVMODE));
        if (!EnumDisplaySettings(null, ENUM_CURRENT_SETTINGS, ref dm)) return -100;
        dm.dmPelsWidth  = width;
        dm.dmPelsHeight = height;
        dm.dmFields = DM_PELSWIDTH | DM_PELSHEIGHT;
        int test = ChangeDisplaySettings(ref dm, CDS_TEST);
        if (test != 0) return test;
        return ChangeDisplaySettings(ref dm, CDS_FULLSCREEN);
    }

    // Restore the resolution saved in Windows settings (your normal 4K)
    public static int Reset() {
        return ChangeDisplaySettingsNull(IntPtr.Zero, 0);
    }
}
"@

function Get-GameProcess {
    Get-Process -Name $ProcessName -ErrorAction SilentlyContinue
}

if (-not (Test-Path $GameExe)) {
    Write-Host "Game not found: $GameExe" -ForegroundColor Red
    Write-Host "Put this script in the The Sims Legacy Collection folder, or pass -GameExe."
    Read-Host "Press Enter to close"
    exit 1
}

$original = [SimsDisplay]::Current()
Write-Host "Current display : $original"

try {
    $result = [SimsDisplay]::Set($Width, $Height)
    if ($result -ne 0) {
        Write-Host "Could not switch to ${Width}x${Height} (code $result). Launching at current resolution." -ForegroundColor Yellow
    } else {
        Write-Host "Switched to     : $([SimsDisplay]::Current())"
        Start-Sleep -Seconds 2   # give the monitor time to settle
    }

    Write-Host "Launching       : $GameExe"
    Start-Process -FilePath $GameExe -WorkingDirectory (Split-Path $GameExe)

    # Wait for the game process to appear (Steam / EA app may relaunch it)
    $deadline = (Get-Date).AddSeconds($StartTimeoutSec)
    while (-not (Get-GameProcess)) {
        if ((Get-Date) -gt $deadline) {
            Write-Host "Game did not start within $StartTimeoutSec s." -ForegroundColor Yellow
            break
        }
        Start-Sleep -Seconds 1
    }

    # Wait until the game has been closed for a few seconds in a row
    $goneFor = 0
    while ($goneFor -lt $ExitGraceSec) {
        if (Get-GameProcess) { $goneFor = 0 } else { $goneFor++ }
        Start-Sleep -Seconds 1
    }
    Write-Host "Game closed."
}
finally {
    [void][SimsDisplay]::Reset()
    Write-Host "Restored display: $([SimsDisplay]::Current())"
}
