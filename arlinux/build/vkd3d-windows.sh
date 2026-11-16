#!/usr/bin/env bash
# Standalone D3D12 DLLs for Bionic Wine; no Steam Proton runtime is built.
set -euo pipefail
if [[ $# != 2 ]]; then
    echo 'Usage: vkd3d-windows.sh LLVM_MINGW_BIN OUTPUT_DIRECTORY' >&2
    exit 2
fi
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
compiler="$(realpath "$1")"
output="$(realpath -m "$2")"
source="$repo/arlinux/components/vkd3d-proton"
revision="$(git -C "$source" rev-parse HEAD)"
cache="$repo/build/vkd3d-windows"
mkdir -p "$cache" "$output"
git -C "$source" submodule update --init --recursive --depth 1 --jobs 4
if [[ ! -f "$cache/source-commit" || "$(cat "$cache/source-commit")" != "$revision" ]]; then
    rm -rf -- "$cache/source"
    mkdir -p "$cache/source"
    git -C "$source" archive HEAD | tar -xf - -C "$cache/source"
    while IFS= read -r dependency; do
        mkdir -p "$cache/source/$dependency"
        git -C "$source/$dependency" archive HEAD | tar -xf - -C "$cache/source/$dependency"
    done < <(git -C "$source" submodule foreach --recursive --quiet 'echo "$displaypath"')
    printf '%s\n' "$revision" > "$cache/source-commit"
fi
for arch in aarch64 x86_64 i386; do
    target="$arch"
    family="$arch"
    extra='[]'
    widl=x86_64-w64-mingw32-widl
    if [[ "$arch" == aarch64 ]]; then
        extra="['-marm64x']"
    elif [[ "$arch" == i386 ]]; then
        target=i686
        widl=i686-w64-mingw32-widl
        family=x86
    fi
    cat > "$cache/$arch.ini" <<EOF
[binaries]
c = '$compiler/$target-w64-mingw32-clang'
cpp = '$compiler/$target-w64-mingw32-clang++'
ar = '$compiler/llvm-ar'
strip = '$compiler/llvm-strip'
windres = '$compiler/$target-w64-mingw32-windres'
widl = '/usr/bin/$widl'
[properties]
needs_exe_wrapper = true
[built-in options]
c_args = $extra
cpp_args = $extra
c_link_args = $extra
cpp_link_args = $extra
[host_machine]
system = 'windows'
cpu_family = '$family'
cpu = '$target'
endian = 'little'
EOF
    options=()
    [[ ! -f "$cache/$arch/build.ninja" ]] || options+=(--reconfigure)
    # Build from the pinned component checkout so upstream's vcs_tag reads its
    # own commit, not the surrounding Wine repository of an exported copy.
    meson setup "${options[@]}" "$cache/$arch" "$source" \
        --cross-file "$cache/$arch.ini" --buildtype release \
        -Denable_tests=false -Denable_extras=false -Denable_trace=false
    ninja -C "$cache/$arch" -j"${JOBS:-8}"
    mkdir -p "$output/$arch-windows"
    install -m755 "$cache/$arch/libs/d3d12/d3d12.dll" "$output/$arch-windows/d3d12.dll"
    install -m755 "$cache/$arch/libs/d3d12core/d3d12core.dll" "$output/$arch-windows/d3d12core.dll"
done
printf '%s\n' "$revision" > "$output/source-commit"
tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner --format=gnu \
    -czf "$output/vkd3d-source.tar.gz" --exclude=.git -C "$cache" source
