#!/usr/bin/env bash
# One independent Wine-owned downloader/installer, with native capability and prefix tools.
set -euo pipefail
if [[ $# != 4 ]]; then
    echo 'Usage: wine-installer.sh NDK MINGW_BIN VULKAN_HEADERS OUTPUT' >&2
    exit 2
fi
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ndk="$(realpath "$1")"
mingw="$(realpath "$2")"
headers="$(realpath "$3")"
output="$(realpath -m "$4")"
files="$output/files"
mkdir -p "$files/bin" "$files/lib/arlinux" "$files/share/arlinux/wine-installer"
"$ndk/toolchains/llvm/prebuilt/linux-x86_64/bin/aarch64-linux-android28-clang++" \
    -std=c++17 -O2 -static-libstdc++ -I"$headers/include" \
    -I"$repo/arlinux/components/dxvk/src/dxvk" "$repo/arlinux/installer/vulkan-probe.cpp" \
    -ldl -o "$files/bin/wine-vulkan-info"
"$mingw/aarch64-w64-mingw32-clang++" -std=c++17 -O2 -static -municode \
    -Wl,--no-insert-timestamp "$repo/arlinux/installer/prefix-setup.cpp" \
    -ladvapi32 -o "$files/lib/arlinux/wine-setup.exe"
cp "$repo/arlinux/installer/runtime.py" "$repo/arlinux/installer/components.tsv" "$files/share/arlinux/wine-installer/"
cp -R "$repo/arlinux/installer/prefix" "$files/share/arlinux/wine-installer/"
mkdir -p "$files/share/licenses/wine-installer" "$output/source/dxvk"
cp "$repo/arlinux/components/dxvk/LICENSE" "$files/share/licenses/wine-installer/DXVK-LICENSE"
git -C "$repo" archive HEAD arlinux/installer arlinux/build/wine-installer.sh | tar -xf - -C "$output/source"
git -C "$repo/arlinux/components/dxvk" archive HEAD LICENSE src/dxvk/dxvk_device_requirements.h src/dxvk/dxvk_device_features.inc | tar -xf - -C "$output/source/dxvk"
tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner --format=gnu \
    -czf "$output/wine-installer-source.tar.gz" -C "$output" source
