#!/usr/bin/env bash
# Shared native code generators for the independent Wine PE/Bionic compilers.
set -euo pipefail
[[ $# == 2 ]] || { echo 'Usage: wine-tools.sh WINE_SOURCE OUTPUT' >&2; exit 2; }
source_dir="$(realpath "$1")"
output="$(realpath -m "$2")"
[[ ! -e "$output" ]] || { echo 'Output already exists.' >&2; exit 2; }
mkdir -p "$output/source" "$output/tools"
# Generated tools refer to this matching source tree. Keep it in the verified
# SDK output rather than exporting dangling paths into a private builder.
git -C "$source_dir" archive HEAD | tar -xf - -C "$output/source"
(cd "$output/source" && perl tools/make_requests)
export CCACHE_BASEDIR="$output"
export CFLAGS="-g -O2 -ffile-prefix-map=$output=/usr/src/arlinux/wine-tools -fdebug-prefix-map=$output=/usr/src/arlinux/wine-tools"
native_triplet="$(gcc -dumpmachine)"
export PKG_CONFIG_LIBDIR="/usr/lib/$native_triplet/pkgconfig:/usr/share/pkgconfig"
pkg-config --exists freetype2
cd "$output/tools"
CC='ccache gcc' CXX='ccache g++' "$output/source/configure" \
    --enable-win64 --disable-tests --without-mingw --without-x --without-wayland
make -j"${JOBS:-4}" tools/all tools/sfnt2fon/all tools/widl/all \
    tools/winebuild/all tools/winegcc/all tools/wmc/all tools/wrc/all nls/all \
    loader/wine.inf fonts/all
# Object files and configure diagnostics belong to compilation, not the SDK.
# Source, generated headers/data and executables remain usable by consumers.
find "$output/tools" -type f \( -name '*.o' -o -name '*.log' \) -delete
