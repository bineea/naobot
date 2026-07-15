param(
    [string]$Workspace = (Join-Path $PSScriptRoot "_work"),
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
$MicroPythonTag = "v1.28.0"
$MicroPythonTagObject = "2b0015629f67fd186f980079b2e696ad0bc7343c"
$MicroPythonCommit = "e0e9fbb17ed6fd06bb76e266ae554784c9c80804"
$Esp32CameraTag = "v2.1.6"
$Esp32CameraCommit = "2ac69a6f1749694804f5196e63fa1f79800b74bf"
$MicroPythonDir = Join-Path $Workspace "micropython"
$CameraDir = Join-Path $MicroPythonDir "ports/esp32/components/esp32-camera"

if ($Clean -and (Test-Path -LiteralPath $Workspace)) {
    $resolvedRoot = [IO.Path]::GetFullPath($PSScriptRoot)
    $resolvedWorkspace = [IO.Path]::GetFullPath($Workspace)
    if (-not $resolvedWorkspace.StartsWith($resolvedRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Clean 仅允许删除 build 目录内的工作区。"
    }
    Remove-Item -LiteralPath $resolvedWorkspace -Recurse -Force
}

New-Item -ItemType Directory -Force -Path $Workspace | Out-Null
if (-not (Test-Path -LiteralPath $MicroPythonDir)) {
    git clone --filter=blob:none --no-checkout https://github.com/micropython/micropython.git $MicroPythonDir
}
git -C $MicroPythonDir fetch --depth 1 origin "refs/tags/$MicroPythonTag"
git -C $MicroPythonDir checkout --detach $MicroPythonCommit

if (-not (Test-Path -LiteralPath $CameraDir)) {
    git clone --filter=blob:none --no-checkout https://github.com/espressif/esp32-camera.git $CameraDir
}
git -C $CameraDir fetch --depth 1 origin "refs/tags/$Esp32CameraTag"
git -C $CameraDir checkout --detach $Esp32CameraCommit

$actualMicroPython = git -C $MicroPythonDir rev-parse HEAD
$actualMicroPythonTag = git -C $MicroPythonDir rev-parse "$MicroPythonTag^{tag}"
$actualCamera = git -C $CameraDir rev-parse HEAD
if (
    $actualMicroPython -ne $MicroPythonCommit `
    -or $actualMicroPythonTag -ne $MicroPythonTagObject `
    -or $actualCamera -ne $Esp32CameraCommit
) {
    throw "上游源码提交校验失败。"
}

$Manifest = (Resolve-Path (Join-Path $PSScriptRoot "manifest.py")).Path
$UserModule = (Resolve-Path (Join-Path $PSScriptRoot "camera_module/micropython.cmake")).Path
$BoardDir = (Resolve-Path (Join-Path $PSScriptRoot "XIAO_ESP32S3_SENSE")).Path
$Partitions = Join-Path $BoardDir "partitions.csv"
$PartitionTarget = Join-Path $MicroPythonDir "ports/esp32/partitions.csv"

Copy-Item -LiteralPath $Partitions -Destination $PartitionTarget -Force

make -C (Join-Path $MicroPythonDir "ports/esp32") submodules
make -C (Join-Path $MicroPythonDir "mpy-cross")
make -C (Join-Path $MicroPythonDir "ports/esp32") `
    BOARD_DIR=$BoardDir `
    BOARD_VARIANT=SPIRAM_OCT `
    FROZEN_MANIFEST=$Manifest `
    USER_C_MODULES=$UserModule `
    all

$BuildDir = Join-Path $MicroPythonDir "ports/esp32/build-XIAO_ESP32S3_SENSE-SPIRAM_OCT"
$FirmwareBin = Join-Path $BuildDir "firmware.bin"
$MaxFirmwareSize = 0x280000
if (-not (Test-Path -LiteralPath $FirmwareBin)) {
    throw "构建失败：未生成 firmware.bin。"
}
$FirmwareSize = (Get-Item -LiteralPath $FirmwareBin).Length
if ($FirmwareSize -gt $MaxFirmwareSize) {
    throw "构建失败：firmware.bin 大小 $FirmwareSize 字节，超过 0x280000 字节上限。"
}

Write-Host "构建流程完成：firmware.bin 大小 $FirmwareSize 字节。"
