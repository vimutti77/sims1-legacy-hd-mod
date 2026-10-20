# Build vkproxy\build\vulkan-1.dll (32-bit) with llvm-mingw and the Vulkan SDK headers.
# Usage: powershell -File vkproxy\build.ps1
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$out = Join-Path $root 'build'
New-Item -ItemType Directory -Force $out | Out-Null

$sdk = $env:VULKAN_SDK
if (-not $sdk) { $sdk = (Get-ChildItem 'C:\VulkanSDK' -Directory | Sort-Object Name | Select-Object -Last 1).FullName }
if (-not (Test-Path "$sdk\Include\vulkan\vulkan.h")) { throw "Vulkan SDK headers not found (set VULKAN_SDK)" }

$cxx = (Get-Command i686-w64-mingw32-clang++ -ErrorAction SilentlyContinue).Source
if (-not $cxx) {
    $cxx = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Recurse -Filter 'i686-w64-mingw32-clang++.exe' -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty FullName
}
if (-not $cxx) { throw "i686-w64-mingw32-clang++ not found (install MartinStorsjo.LLVM-MinGW.UCRT)" }

foreach ($shader in 'xbr', 'world_floor', 'world_wall', 'world_object', 'world_object_yiq', 'world_wall_top') {
    & "$sdk\Bin\glslc.exe" -O -mfmt=c -o "$out\${shader}_frag.inc" "$root\src\$shader.frag"
    if ($LASTEXITCODE -ne 0) { throw "shader compile failed: $shader.frag" }
}

& $cxx -std=c++17 -O2 -Wall -shared -static -o "$out\vulkan-1.dll" "$root\src\vkproxy.cpp" `
    "-I$sdk\Include" "-I$out" '-Wl,--kill-at'
if ($LASTEXITCODE -ne 0) { throw "build failed" }
Write-Output "built $out\vulkan-1.dll"
