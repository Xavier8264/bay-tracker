<#
    make_icons.ps1 -- (re)generate the desktop-shortcut icons.

    Produces two crisp, intuitive .ico files used by make_shortcut.ps1:

        assets\start.ico   green circle + white "play" triangle   -> Start the server
        assets\update.ico  blue  circle + white download arrow    -> Pull the latest update

    The icons are committed to the repo so a fresh clone already has them; this
    script only needs to be re-run if you want to change the artwork. Each .ico
    bundles several sizes (16/32/48/64/128/256) as PNG frames -- the format the
    Windows shell uses for shortcut icons -- so the icon stays sharp whether it's
    shown small on the taskbar or large on the Desktop.

    Run:  powershell -ExecutionPolicy Bypass -File .\assets\make_icons.ps1
#>
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Drawing

# PrivateExtractIcons is exactly how the Explorer shell loads an icon file, so
# it is the right thing to validate against (System.Drawing.Icon is a different,
# pickier code path that can't rasterize PNG-compressed frames at all).
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class IconProbe {
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern int PrivateExtractIcons(string file, int index, int cx, int cy,
        IntPtr[] phicon, uint[] piconid, uint nIcons, uint flags);
    [DllImport("user32.dll")]
    public static extern bool DestroyIcon(IntPtr h);
    public static bool CanLoad(string path, int size) {
        IntPtr[] h = new IntPtr[1]; uint[] id = new uint[1];
        int n = PrivateExtractIcons(path, 0, size, size, h, id, 1, 0);
        if (n > 0 && h[0] != IntPtr.Zero) { DestroyIcon(h[0]); return true; }
        return false;
    }
}
'@

$AssetsDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$sizes = @(16, 32, 48, 64, 128, 256)

function New-Glyph {
    # Draws one square frame of the chosen glyph at the given pixel size and
    # returns it as a System.Drawing.Bitmap (32-bit, transparent background).
    param([int]$s, [string]$Kind, [System.Drawing.Color]$Circle)

    $bmp = New-Object System.Drawing.Bitmap($s, $s, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode      = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.PixelOffsetMode    = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
    $g.InterpolationMode  = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $g.CompositingQuality = [System.Drawing.Drawing2D.CompositingQuality]::HighQuality
    $g.Clear([System.Drawing.Color]::Transparent)

    # Coloured disc, inset a little so the antialiased edge isn't clipped.
    $m = [single]($s * 0.045)
    $d = [single]($s - 2 * $m)
    $circleBrush = New-Object System.Drawing.SolidBrush($Circle)
    $g.FillEllipse($circleBrush, $m, $m, $d, $d)

    $white = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::White)

    if ($Kind -eq "play") {
        # Right-pointing triangle, nudged slightly right like a real play button.
        $pts = @(
            (New-Object System.Drawing.PointF([single]($s*0.40), [single]($s*0.31))),
            (New-Object System.Drawing.PointF([single]($s*0.40), [single]($s*0.69))),
            (New-Object System.Drawing.PointF([single]($s*0.71), [single]($s*0.50)))
        )
        $g.FillPolygon($white, $pts)
    }
    elseif ($Kind -eq "download") {
        # Down arrow landing on a short baseline = "download / get the latest".
        $stem = New-Object System.Drawing.RectangleF([single]($s*0.435), [single]($s*0.22), [single]($s*0.13), [single]($s*0.30))
        $g.FillRectangle($white, $stem)
        $head = @(
            (New-Object System.Drawing.PointF([single]($s*0.345), [single]($s*0.47))),
            (New-Object System.Drawing.PointF([single]($s*0.655), [single]($s*0.47))),
            (New-Object System.Drawing.PointF([single]($s*0.50),  [single]($s*0.70)))
        )
        $g.FillPolygon($white, $head)
        $base = New-Object System.Drawing.RectangleF([single]($s*0.33), [single]($s*0.755), [single]($s*0.34), [single]($s*0.065))
        $g.FillRectangle($white, $base)
    }
    else { throw "Unknown glyph kind: $Kind" }

    $circleBrush.Dispose(); $white.Dispose(); $g.Dispose()
    return $bmp
}

function Save-Ico {
    # Bundles the per-size PNG frames into a single valid .ico file, then
    # confirms the Windows shell can actually load it at small and large sizes.
    param([string]$Path, [string]$Kind, [System.Drawing.Color]$Circle)

    $pngs = @()
    foreach ($s in $sizes) {
        $bmp = New-Glyph -s $s -Kind $Kind -Circle $Circle
        $ms = New-Object System.IO.MemoryStream
        $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
        $pngs += ,([byte[]]$ms.ToArray())
        $ms.Dispose(); $bmp.Dispose()
    }

    $fs = [System.IO.File]::Create($Path)
    $bw = New-Object System.IO.BinaryWriter($fs)
    # ICONDIR header
    $bw.Write([uint16]0)              # reserved
    $bw.Write([uint16]1)              # type = icon
    $bw.Write([uint16]$sizes.Count)   # image count
    # Directory entries (16 bytes each); image data follows all entries.
    $offset = 6 + (16 * $sizes.Count)
    for ($i = 0; $i -lt $sizes.Count; $i++) {
        $s = $sizes[$i]
        $dim = [byte]($(if ($s -ge 256) { 0 } else { $s }))   # 0 means 256
        $bw.Write([byte]$dim)         # width
        $bw.Write([byte]$dim)         # height
        $bw.Write([byte]0)            # palette count
        $bw.Write([byte]0)            # reserved
        $bw.Write([uint16]1)          # colour planes
        $bw.Write([uint16]32)         # bits per pixel
        $bw.Write([uint32]$pngs[$i].Length)
        $bw.Write([uint32]$offset)
        $offset += $pngs[$i].Length
    }
    foreach ($png in $pngs) { $bw.Write($png) }
    $bw.Flush(); $bw.Close(); $fs.Close()

    if (-not [IconProbe]::CanLoad($Path, 256)) { throw "Shell could not load $Path at 256px." }
    if (-not [IconProbe]::CanLoad($Path, 32))  { throw "Shell could not load $Path at 32px." }
    Write-Host ("Created: {0}  ({1:N0} bytes)" -f $Path, (Get-Item $Path).Length) -ForegroundColor Green
}

$green = [System.Drawing.Color]::FromArgb(255, 46, 158, 79)    # start  (#2E9E4F)
$blue  = [System.Drawing.Color]::FromArgb(255, 30, 120, 215)   # update (#1E78D7)

Save-Ico -Path (Join-Path $AssetsDir "start.ico")  -Kind "play"     -Circle $green
Save-Ico -Path (Join-Path $AssetsDir "update.ico") -Kind "download" -Circle $blue
