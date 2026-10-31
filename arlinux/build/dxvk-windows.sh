#!/usr/bin/env bash
# Build both hardware DXVK profiles through the same compiler/cache recipe.
set -euo pipefail
if [[ $# != 2 ]]; then
    echo 'Usage: dxvk-windows.sh LLVM_MINGW_BIN OUTPUT_DIRECTORY' >&2
    exit 2
fi
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
compiler="$(realpath "$1")"
output="$(realpath -m "$2")"
"$compiler/aarch64-w64-mingw32-clang" -marm64x -shared -x c /dev/null -o /dev/null -### 2>/dev/null \
    || { echo 'LLVM-MinGW with ARM64X support (LLVM 23+) is required.' >&2; exit 2; }
build_variant() {
local variant="$1"
local source="$repo/arlinux/components/$2"
local dlls="$3"
revision="$(git -C "$source" rev-parse HEAD)"
link_extra="['-Wl,--no-insert-timestamp']"
key="$(printf '%s\n' "$revision" "$compiler" "$link_extra" \
    "$("$compiler/aarch64-w64-mingw32-clang" --version)" | sha256sum | cut -c1-24)"
cache="$repo/build/dxvk-windows/$key"
mkdir -p "$cache" "$output"
# Export the pinned component and its declared header dependencies, never a
# second clone or a patched working tree. Git archives retain stable mtimes.
if [[ "$variant" == modern ]]; then
    git -C "$source" submodule update --init --depth 1 \
        include/vulkan include/spirv subprojects/libdisplay-info
fi
mkdir -p "$cache/source"
git -C "$source" archive HEAD | tar -xf - -C "$cache/source"
for dependency in $(git -C "$source" submodule foreach --quiet 'echo "$sm_path"'); do
    mkdir -p "$cache/source/$dependency"
    git -C "$source/$dependency" archive HEAD | tar -xf - -C "$cache/source/$dependency"
done
for arch in aarch64 x86_64 i386; do
    target="$arch"
    family="$arch"
    extra='[]'
    if [[ "$arch" == aarch64 ]]; then
        # One ARM64X DLL serves native ARM64 and emulated x64 clients.
        extra="['-marm64x']"
    elif [[ "$arch" == i386 ]]; then
        target=i686
        family=x86
    fi
    if [[ "$extra" == '[]' ]]; then
        link_args="$link_extra"
    else
        link_args="${extra%]}, '-Wl,--no-insert-timestamp']"
    fi
    cross="$cache/$arch.ini"
    if [[ ! -f "$cross" ]]; then
    cat > "$cross" <<EOF
[binaries]
c = '$compiler/$target-w64-mingw32-clang'
cpp = '$compiler/$target-w64-mingw32-clang++'
ar = '$compiler/llvm-ar'
strip = '$compiler/llvm-strip'
windres = '$compiler/$target-w64-mingw32-windres'
[built-in options]
c_args = $extra
cpp_args = $extra
c_link_args = $link_args
cpp_link_args = $link_args
[host_machine]
system = 'windows'
cpu_family = '$family'
cpu = '$target'
endian = 'little'
EOF
    fi
    if [[ ! -f "$cache/$arch/build.ninja" ]]; then
        options=()
        [[ "$variant" != modern ]] || options+=(--force-fallback-for=libdisplay-info)
        meson setup "$cache/$arch" "$cache/source" --cross-file "$cross" \
            --buildtype release "${options[@]}"
    fi
    ninja -C "$cache/$arch" -j"${JOBS:-8}"
    mkdir -p "$output/$variant/$arch-windows"
    for dll in $dlls; do
        directory="$dll"
        [[ "$dll" != d3d10core && "$dll" != d3d10_1 ]] || directory=d3d10
        install -m755 "$cache/$arch/src/$directory/$dll.dll" "$output/$variant/$arch-windows/$dll.dll"
    done
done
printf '%s\n' "$revision" > "$output/$variant/source-commit"
tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner --format=gnu \
    -czf "$output/$variant/source.tar.gz" --exclude=.git -C "$cache" source
}
build_variant modern dxvk 'dxgi d3d8 d3d9 d3d10core d3d11'
build_variant legacy dxvk-legacy 'dxgi d3d9 d3d10core d3d10 d3d10_1 d3d11'
"$compiler/aarch64-w64-mingw32-clang" -std=c11 -O2 -municode \
    -Wl,--no-insert-timestamp "$repo/arlinux/dxvk-probe.c" -o "$output/dxvk-probe.exe"
python3 - "$output" <<'PY'
from pathlib import Path
import json,sys
root=Path(sys.argv[1])
profiles=[]
for name,version in (('modern','2.7.1'), ('legacy','1.10.3')):
    dlls=sorted(path.stem for path in (root/name/'aarch64-windows').glob('*.dll'))
    profiles.append(dict(name=name,version=version,directory=('.' if name=='modern' else name),dlls=dlls))
(root/'profiles.json').write_text(json.dumps(dict(format=1,profiles=profiles),sort_keys=True)+'\n')
PY
