#!/usr/bin/env bash
# Build version-matched Wine Windows modules; native binaries are built for Bionic.
set -euo pipefail
if [[ $# != 3 ]]; then
    echo 'Usage: wine-pe.sh WINE_SOURCE LLVM_MINGW_BIN OUTPUT_DIRECTORY' >&2
    exit 2
fi
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source_dir="$(realpath "$1")"
compiler="$(realpath "$2")"
export PATH="$compiler:$PATH"
architecture="${ARLINUX_WINE_PE_ARCH:-all}"
case "$architecture" in
    all) pe_host=aarch64; pe_archs=arm64ec,aarch64,i386,x86_64; pe_win64=--enable-win64 ;;
    aarch64) pe_host=aarch64; pe_archs=arm64ec,aarch64; pe_win64=--enable-win64 ;;
    x86_64) pe_host=x86_64; pe_archs=x86_64; pe_win64=--enable-win64 ;;
    i386) pe_host=i686; pe_archs=i386; pe_win64= ;;
    *) echo 'Unsupported Wine PE architecture.' >&2; exit 2 ;;
esac
export ARLINUX_WINE_PE_ARCH="$architecture"
destination="$(realpath -m "$3")"
[[ ! -e "$destination" ]] || { echo 'Output directory already exists.' >&2; exit 2; }
mkdir -p "$repo/build/wine-pe"
# Content-addressed build directories preserve Make's dependency graph on retry.
# JOBS and destination are deliberately not inputs: they do not change binaries.
identity="$(python3 - "$repo" "$source_dir" "$compiler" <<'KEY'
from pathlib import Path
import hashlib,subprocess,sys,os
repo,source,compiler=map(Path,sys.argv[1:])
h=hashlib.sha256()
h.update(os.environ["ARLINUX_WINE_PE_ARCH"].encode())
sys.path.insert(0,str(repo/'arlinux/build'))
from component_inputs import core_path,tree_entries
import json
h.update(json.dumps({n:v for n,v in tree_entries(source,'HEAD').items() if core_path(n)},sort_keys=True).encode())
inputs=[repo/'arlinux/build/wine-pe.sh',source/'dlls/combase/parameterized.c']
for path in inputs:
    h.update(path.name.encode());h.update(path.read_bytes())
for name in ('clang','ld.lld'):
    path=(compiler/name).resolve()
    h.update(str(path).encode());h.update(path.read_bytes())
for command in (['gcc','--version'],['make','--version'],['pkg-config','--modversion','freetype2']):
    h.update(subprocess.check_output(command))
print(h.hexdigest())
KEY
)"
stage="$repo/build/wine-pe/$identity"
mkdir -p "$stage"
export CCACHE_BASEDIR="$stage"
export CFLAGS="-g -O2 -ffile-prefix-map=$stage=/usr/src/arlinux/wine-pe/$architecture -fdebug-prefix-map=$stage=/usr/src/arlinux/wine-pe/$architecture"
export CROSSCFLAGS="$CFLAGS"
exec 9>"$stage/build.lock"
flock -n 9 || { echo "Build already running: $stage" >&2; exit 2; }
echo "Build directory: $stage"
echo "Build identity: $identity"
if [[ -f "$stage/source/.arlinux-source-ready" ]]; then
    touch "$stage/source-ready"
fi
if [[ ! -f "$stage/source-ready" ]]; then
    # An interrupted source preparation is retained for inspection, never reused.
    prepare="$(mktemp -d "$stage/prepare.XXXXXXXX")"
    mkdir "$prepare/source"
    git -C "$source_dir" archive HEAD | tar -xf - -C "$prepare/source"
    [[ -f "$prepare/source/dlls/combase/parameterized.c" ]] || {
        echo 'Pinned Wine commit is incomplete: missing combase/parameterized.c.' >&2; exit 2;
    }
    (cd "$prepare/source" && perl tools/make_requests)
    if [[ -d "$stage/source" ]]; then
        # A compiler capsule can restore individual source files referenced by
        # its SDK tools. It is not a complete prepared tree. Preserve those
        # files for diagnosis and install the freshly prepared source atomically.
        incomplete="$(mktemp -d "$stage/incomplete.XXXXXXXX")"
        mv -T "$stage/source" "$incomplete/source"
        echo "Retained partial SDK source at $incomplete/source" >&2
    fi
    touch "$prepare/source/.arlinux-source-ready"
    mv -T "$prepare/source" "$stage/source"
    touch "$stage/source-ready"
fi
mkdir -p "$stage/tools" "$stage/core"
native_triplet="$(gcc -dumpmachine)"
export PKG_CONFIG_LIBDIR="/usr/lib/$native_triplet/pkgconfig:/usr/share/pkgconfig"
pkg-config --exists freetype2 || {
    echo 'Install native FreeType development headers for Wine font generation.' >&2; exit 2;
}
(cd "$stage/tools" && if [[ ! -f Makefile ]]; then CC='ccache gcc' CXX='ccache g++' "$stage/source/configure" --enable-win64 --disable-tests \
    --without-mingw --without-x --without-wayland \
    ; fi
    make -j"${JOBS:-8}" tools/all tools/sfnt2fon/all \
        tools/widl/all tools/winebuild/all tools/winegcc/all tools/wmc/all tools/wrc/all nls/all) \
    > "$stage/tools.log" 2>&1
export PKG_CONFIG_LIBDIR=/usr/lib/aarch64-linux-gnu/pkgconfig:/usr/share/pkgconfig
(cd "$stage/core" && if [[ ! -f Makefile ]]; then "$stage/source/configure" --host="$pe_host-w64-mingw32" $pe_win64 \
    --enable-archs="$pe_archs" --with-wine-tools="$stage/tools" \
    --disable-tests --disable-winex11.drv --enable-winewayland.drv --without-wayland --without-unwind --without-x --without-freetype --without-fontconfig --without-gnutls --without-opengl; fi) > "$stage/configure.log" 2>&1
make -C "$stage/core" -j"${JOBS:-8}" > "$stage/build.log" 2>&1
make -C "$stage/core" install DESTDIR="$stage/install" > "$stage/install.log" 2>&1
# PE-only configure omits native loader data and font installation rules.
# Generate these with matching native tools, preserving the same source version.
if [[ "$architecture" == all || "$architecture" == aarch64 ]]; then
make -C "$stage/tools" -j"${JOBS:-8}" loader/wine.inf fonts/all > "$stage/data.log" 2>&1
mkdir -p "$stage/install/usr/local/share/wine/fonts"
install -m644 "$stage/tools/loader/wine.inf" "$stage/install/usr/local/share/wine/wine.inf"
find "$stage/tools/fonts" -maxdepth 1 -type f \( -name '*.fon' -o -name '*.ttf' \) \
    -exec install -m644 -t "$stage/install/usr/local/share/wine/fonts" {} +
find "$stage/source/fonts" -maxdepth 1 -type f -name '*.ttf' \
    -exec install -m644 -t "$stage/install/usr/local/share/wine/fonts" {} +
fi
if [[ -d "$stage/install/usr/local/lib/wine/aarch64-unix" ]]; then
    rm -r -- "$stage/install/usr/local/lib/wine/aarch64-unix"
fi
mkdir -p "$stage/output/files"
# Copy rather than move: an interrupted packaging step can be retried safely.
if [[ "$architecture" == all || "$architecture" == aarch64 ]]; then
cp -a "$stage/install/usr/local/." "$stage/output/files/"
git -C "$source_dir" rev-parse HEAD > "$stage/output/wine-commit"
cp "$stage/source/VERSION" "$stage/output/wine-version"
tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner --format=gnu \
    -czf "$stage/output/wine-source.tar.gz" -C "$stage" source
python3 - "$stage" <<'BUILD_STATE'
import json, shutil, sys
from pathlib import Path
stage = Path(sys.argv[1])
# Configure logs contain runner hostnames and timestamps, not tool inputs.
# Keep diagnostics in the private builder and export the usable tools separately.
exported = stage/'host-tools'
# This is a regenerated export, not a merge target. copytree with
# dirs_exist_ok replaces regular files but fails on existing symlinks after
# restoring a compiler cache. Keep compiled tools intact and replace only
# this owned export directory.
if exported.is_symlink():
    exported.unlink()
elif exported.exists():
    shutil.rmtree(exported)
shutil.copytree(stage/'tools', exported, symlinks=True,
                ignore=shutil.ignore_patterns('*.log'))
(stage/'output/build-state.json').write_text(json.dumps({'tools': str(exported)}) + '\n')
BUILD_STATE
else
    # The merge consumes only these modules. Common data, source archives and
    # host-tool references come from the ARM64 part, never these two jobs.
    mkdir -p "$stage/output/files/lib/wine"
    cp -a "$stage/install/usr/local/lib/wine/$architecture-windows" "$stage/output/files/lib/wine/"
fi
mkdir -p "$(dirname "$destination")"
mv -T --no-clobber "$stage/output" "$destination"
[[ ! -d "$stage/output" ]] || { echo 'Destination appeared during build; preserving outputs.' >&2; exit 2; }
echo "Built Wine 11.19 Windows modules in $destination"
