# Builds artifacts\installer\BlenderMcpSetup-<version>.exe
#   1. publishes the server as a self-contained single-file exe (no .NET needed on target PCs)
#   2. packages the Blender add-on as an extension zip
#   3. compiles installer\BlenderMcp.iss with Inno Setup 6
param(
    [string]$Configuration = "Release",
    [string]$Iscc = ""
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$artifacts = Join-Path $root "artifacts"
$csproj = Join-Path $root "src\BlenderMcp.Server\BlenderMcp.Server.csproj"

$version = ([xml](Get-Content $csproj)).Project.PropertyGroup.Version | Where-Object { $_ } | Select-Object -First 1
if (-not $version) { throw "No <Version> in $csproj" }
Write-Host "Building Blender MCP $version" -ForegroundColor Cyan

# 1. server
$serverOut = Join-Path $artifacts "server"
if (Test-Path $serverOut) { Remove-Item $serverOut -Recurse -Force }
dotnet publish $csproj -c $Configuration -r win-x64 --self-contained true -o $serverOut -nologo `
    -p:PublishSingleFile=true -p:EnableCompressionInSingleFile=true `
    -p:IncludeNativeLibrariesForSelfExtract=true -p:DebugType=None -p:GenerateDocumentationFile=false
if ($LASTEXITCODE -ne 0) { throw "dotnet publish failed" }

# 2. add-on zip (the zip must contain the folder)
$addonSrc = Join-Path $root "blender_addon\blender_mcp_bridge"
Get-ChildItem $addonSrc -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
$zip = Join-Path $artifacts "blender_mcp_bridge.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path $addonSrc -DestinationPath $zip

# 3. installer
if (-not $Iscc) {
    $Iscc = @(
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $Iscc) { throw "Inno Setup 6 not found (winget install JRSoftware.InnoSetup)" }
& $Iscc "/DAppVersion=$version" (Join-Path $PSScriptRoot "BlenderMcp.iss")
if ($LASTEXITCODE -ne 0) { throw "ISCC failed" }

Get-ChildItem (Join-Path $artifacts "installer") -Filter "*.exe" | Format-Table Name, @{n = "MB"; e = { [math]::Round($_.Length / 1MB, 1) } }
