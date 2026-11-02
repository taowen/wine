#!/usr/bin/env bash
# Compile the native Wine backend and export its packaging/Steamworks ABI inputs.
set -euo pipefail
if [[ $# != 7 ]]; then
    echo 'Usage: wine-bionic.sh WINE_SOURCE WINE_TOOLS LLVM_MINGW_BIN NDK_ROOT NATIVE_PREFIX FEX_SOURCE OUTPUT' >&2
    exit 2
fi
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source_dir="$(realpath "$1")"
tools_dir="$(realpath "$2")"
mingw="$(realpath "$3")"
ndk="$(realpath "$4")/toolchains/llvm/prebuilt/linux-x86_64"
prefix="$(realpath "$5")"
tls="$(realpath "${BIONIC_TLS_PREFIX:-$prefix}")"
pulse="$(realpath "${BIONIC_PULSE_PREFIX:-$prefix}")"
graphics="$(realpath "${BIONIC_GRAPHICS_PREFIX:-$prefix}")"
case "${BIONIC_OPENGL:-zink}" in
    zink) opengl_arg=--with-opengl ;;
    none) opengl_arg=--without-opengl ;;
    *) echo 'Unknown BIONIC_OPENGL.' >&2; exit 2 ;;
esac
fex_source="$(realpath "$6")"
output="$(realpath -m "$7")"
[[ ! -e "$output" ]] || { echo 'Output already exists.' >&2; exit 2; }
mkdir -p "$repo/build/wine-bionic"
stage="$(mktemp -d "$repo/build/wine-bionic/build.XXXXXXXX")"
echo "Build directory: $stage"
mkdir "$stage/core" "$stage/fex"
mkdir "$stage/source"
# The integration now lives in this Wine checkout. Copy only tracked source:
# walking the checkout would recursively copy build/ into its own destination.
git -C "$source_dir" archive HEAD | tar -xf - -C "$stage/source"
source_dir="$stage/source"
(cd "$source_dir" && perl tools/make_requests)
# Maintained Wine fixes are part of the archived fork, shared with PE builds.
(cd "$source_dir/dlls/winevulkan" && python3 make_vulkan)
cp "$fex_source/Source/Windows/UnixLib/"{FEXUnixLib.cpp,FEXUnixLib.h} "$stage/fex/"
export PATH="$mingw:$PATH"
export CC="ccache $ndk/bin/aarch64-linux-android29-clang" CXX="ccache $ndk/bin/aarch64-linux-android29-clang++"
export AR="$ndk/bin/llvm-ar" RANLIB="$ndk/bin/llvm-ranlib"
export CCACHE_BASEDIR="$stage"
# Keep debug/source paths stable across content-addressed build generations.
# Otherwise a new temporary directory defeats caching unchanged source files.
export CFLAGS="-O2 -g -DANDROID_SUPPORT_FLEXIBLE_PAGE_SIZES -ffile-prefix-map=$stage=/usr/src/arlinux/wine-bionic -fdebug-prefix-map=$stage=/usr/src/arlinux/wine-bionic"
export CPPFLAGS="-I$prefix/include -I$graphics/include" LDFLAGS="-L$prefix/lib -L$graphics/lib -Wl,-z,max-page-size=16384"
export WAYLAND_CLIENT_CFLAGS="-I$prefix/include" WAYLAND_CLIENT_LIBS="-L$prefix/lib -lwayland-client"
export WAYLAND_EGL_CFLAGS="-I$prefix/include" WAYLAND_EGL_LIBS="-L$prefix/lib -lwayland-egl"
export XKBCOMMON_CFLAGS="-I$prefix/include" XKBCOMMON_LIBS="-L$prefix/lib -lxkbcommon"
export XKBREGISTRY_CFLAGS="-I$prefix/include" XKBREGISTRY_LIBS="-L$prefix/lib -lxkbregistry"
export DBUS_CFLAGS="-I$prefix/include/dbus-1.0 -I$prefix/lib/dbus-1.0/include" DBUS_LIBS="-L$prefix/lib -ldbus-1"
export EGL_CFLAGS="-I$graphics/include" EGL_LIBS="-L$graphics/lib -lEGL"
export FREETYPE_CFLAGS="-I$prefix/include/freetype2" FREETYPE_LIBS="-L$prefix/lib -lfreetype"
export FONTCONFIG_CFLAGS="-I$prefix/include" FONTCONFIG_LIBS="-L$prefix/lib -lfontconfig"
export GNUTLS_CFLAGS="-I$tls/include" GNUTLS_LIBS="-L$tls/lib -lgnutls"
export PULSE_CFLAGS="-I$pulse/include" PULSE_LIBS="-L$pulse/lib -Wl,-rpath-link,$pulse/lib/pulseaudio -lpulse"
# configure requires a Gradle executable even with wineandroid.drv disabled.
export PKG_CONFIG_LIBDIR=/nonexistent GRADLE=/bin/true
(
    cd "$stage/core"
    "$source_dir/configure" --host=aarch64-linux-android --enable-wineandroid_drv=no \
        --enable-win64 --enable-archs=arm64ec,aarch64,i386,x86_64 --with-wine-tools="$tools_dir" \
        --disable-tests --without-x --without-xinput --without-xinput2 --with-freetype --with-fontconfig --without-xshm \
        --with-wayland --disable-winex11.drv --without-unwind --with-gnutls --without-gstreamer \
        --without-alsa --with-pulse --with-dbus "$opengl_arg" --with-vulkan \
        --prefix=/arlinux-wine --exec-prefix=/arlinux-wine
) > "$stage/configure.log" 2>&1
if [[ "$opengl_arg" == --with-opengl ]]; then
    grep -q '^#define SONAME_LIBEGL ' "$stage/core/include/config.h" || {
        echo 'Wine could not enable Wayland EGL; check the Bionic graphics prefix.' >&2; exit 2;
    }
fi
grep -q '^#define SONAME_LIBVULKAN ' "$stage/core/include/config.h" || {
    echo 'Wine could not enable Vulkan; check the Bionic graphics prefix.' >&2; exit 2;
}
(
    cd "$stage/core"
    mapfile -t modules < <(sed -n -E 's/^(dlls\/[^ :]*\.so):.*/\1/p' Makefile)
    make -j"${JOBS:-8}" loader/wine server/wineserver "${modules[@]}"
    # winebus PE and Unix halves exchange private structures. Build the driver
    # from this same prepared source instead of reusing a potentially older PE.
    make -j"${JOBS:-8}" dlls/winebus.sys/{aarch64,i386,x86_64}-windows/winebus.sys
    make -j"${JOBS:-8}" programs/explorer/{aarch64,i386,x86_64}-windows/explorer.exe
) > "$stage/build.log" 2>&1
ccache "$ndk/bin/aarch64-linux-android29-clang++" -std=c++17 -fPIC -O2 \
    "-ffile-prefix-map=$stage=/usr/src/arlinux/wine-bionic" \
    -c "$stage/fex/FEXUnixLib.cpp" -o "$stage/fex/FEXUnixLib.o"
"$ndk/bin/aarch64-linux-android29-clang++" -shared -static-libstdc++ -Wl,-z,defs \
    -Wl,-z,max-page-size=16384 "$stage/fex/FEXUnixLib.o" -o "$stage/fex/libarm64ecfex.so"
cp "$stage/fex/libarm64ecfex.so" "$stage/fex/libwow64fex.so"
# Export only compiler outputs consumed by packaging and Steamworks. Keep the
# private object tree in this runner; it is never a downstream artifact.
python3 - "$stage" "$output" "$repo" "$fex_source" <<'COMPILE_OUTPUT'
import json, shutil, subprocess, sys
from pathlib import Path
stage, output, repo, fex = map(Path, sys.argv[1:])
output.mkdir()
shutil.copytree(stage/'source/include', output/'source/include')
shutil.copytree(stage/'core/include', output/'core/include')
files = [stage/'core/loader/wine', stage/'core/server/wineserver']
files.extend((stage/'core/dlls').glob('*/*.so'))
for arch in ('aarch64', 'i386', 'x86_64'):
    files.extend((stage/'core/dlls/winebus.sys'/f'{arch}-windows/winebus.sys',
                  stage/'core/programs/explorer'/f'{arch}-windows/explorer.exe'))
for source in files:
    target = output/'core'/source.relative_to(stage/'core')
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
shutil.copytree(stage/'fex', output/'fex', ignore=shutil.ignore_patterns('*.cpp', '*.h', '*.o'))
metadata = dict(wine_commit=subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip(),
                wine_version=(stage/'source/VERSION').read_text().strip(),
                fex_commit=subprocess.check_output(['git','-C',str(fex),'rev-parse','HEAD'],text=True).strip())
(output/'wine-build.json').write_text(json.dumps(metadata,sort_keys=True)+'\n')
COMPILE_OUTPUT
tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner --format=gnu \
    -czf "$output/wine-source.tar.gz" -C "$stage" source
echo "Compiled Bionic Wine: $output"
