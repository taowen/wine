#!/usr/bin/env python3
"""Package only the native Bionic backend and its paired shell/HID frontends."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
from wine_pe_debug import optimize

SYSTEM_LIBS = {'libc.so', 'libm.so', 'libdl.so', 'libz.so', 'liblog.so', 'libandroid.so', 'libnativewindow.so'}



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['core', 'fex', 'native-prefix', 'ndk', 'output']:
        parser.add_argument('--' + name, type=Path)
    parser.add_argument('--dependency-prefix', type=Path, action='append', default=[])
    parser.add_argument('--graphics-prefix', type=Path)
    parser.add_argument('--vulkan-driver', choices=['system', 'turnip', 'none'], default='system')
    parser.add_argument('--system-vulkan-adapter', type=Path,
                        help='Bionic libvulkan_android_desktop.so from arlinux/components/libhybris/arlinux/build-bionic-vulkan.py')
    parser.add_argument('--wine-source-archive', type=Path,
                        help='Source archive exported by the matching native Wine compile')
    args = parser.parse_args()
    for name in ('core', 'fex', 'native_prefix', 'ndk', 'output'):
        if getattr(args, name) is None:
            parser.error('Full packaging requires --' + name.replace('_', '-'))
    if args.vulkan_driver == 'system' and (not args.system_vulkan_adapter or not args.system_vulkan_adapter.is_file()):
        parser.error('System Vulkan requires --system-vulkan-adapter; build the native desktop WSI first')
    compat_layer = args.system_vulkan_adapter.with_name('libVkLayer_hybris_compat.so') if args.vulkan_driver == 'system' else None
    if compat_layer and (not compat_layer.is_file() or not compat_layer.with_name('VkLayer_hybris_compat.json').is_file()):
        parser.error('Build the system adapter and its matching Bionic hybris compatibility layer together')
    if args.vulkan_driver != 'none' and not args.graphics_prefix:
        parser.error('A graphics prefix containing the desktop Vulkan loader is required')
    config_header = (args.core / 'include/config.h').read_text()
    if args.vulkan_driver != 'none' and not re.search(r'^#define SONAME_LIBVULKAN ', config_header, re.MULTILINE):
        parser.error('Selected Vulkan backend requires a Wine build with SONAME_LIBVULKAN enabled')
    opengl = bool(re.search(r'^#define SONAME_LIBEGL ', config_header, re.MULTILINE))
    graphics_record = None
    if opengl:
        if not args.graphics_prefix:
            parser.error('OpenGL requires a graphics prefix with Zink')
        graphics_record = json.loads((args.graphics_prefix / 'arlinux-bionic-graphics.json').read_text())
    original = json.loads((args.core.parent / 'wine-build.json').read_text())
    wine_commit = original['wine_commit']
    fex_commit = original['fex_commit']
    source_archive = args.wine_source_archive or (args.core.parent / 'wine-source.tar.gz')
    with source_archive.open('rb') as stream:
        source_archive_sha256 = hashlib.file_digest(stream, 'sha256').hexdigest()
    source_asset = 'wine-source-' + source_archive_sha256 + '.tar.gz'
    source_url = ('https://github.com/taowen/wine/releases/download/arlinux-wine-source-' +
                  source_archive_sha256 + '/' + source_asset)
    source_target = args.output.parent / source_asset
    if source_target.exists():
        if hashlib.sha256(source_target.read_bytes()).hexdigest() != source_archive_sha256:
            raise RuntimeError('Existing source asset has different contents')
    elif source_archive.resolve() != source_target.resolve():
        shutil.copyfile(source_archive, source_target)
    args.output.mkdir(parents=True, exist_ok=False)
    destination = args.output / 'files'
    destination.mkdir()
    # Keep the HID driver's private Unix ABI paired with the native backend.
    for arch in ('aarch64', 'i386', 'x86_64'):
        driver = args.core / 'dlls/winebus.sys' / (arch + '-windows/winebus.sys')
        target = destination / 'lib/wine' / (arch + '-windows/winebus.sys')
        target.parent.mkdir(parents=True)
        if target.is_symlink():
            target.unlink()
        shutil.copyfile(driver, target)
        # Explorer contains the native tray and must match our shell patch.
        explorer = args.core / 'programs/explorer' / (arch + '-windows/explorer.exe')
        target = destination / 'lib/wine' / (arch + '-windows/explorer.exe')
        if target.is_symlink():
            target.unlink()
        shutil.copyfile(explorer, target)
    configuration = destination / 'etc/dxvk.conf'
    configuration.parent.mkdir(parents=True, exist_ok=True)
    configuration.write_text('[msedgewebview2.exe]\n'
                             'dxgi.enableDummyCompositionSwapchain = True\n')
    unix = destination / 'lib/wine/aarch64-unix'
    unix.mkdir(parents=True)
    readelf = args.ndk / 'bin/llvm-readelf'
    strip = args.ndk / 'bin/llvm-strip'
    # These are opened by Wine at runtime rather than linked as DT_NEEDED.
    pending = re.findall(r'^#define SONAME_LIB(?:FONTCONFIG|FREETYPE|GNUTLS|VULKAN|GL|EGL|GLESV2|DBUS_1|X[A-Z0-9_]+) "([^"]+)"',
                         config_header, re.MULTILINE)
    # Ship Turnip alongside the system driver so Adreno can use modern Vulkan.
    turnip = args.vulkan_driver != 'none'
    if turnip:
        pending.append('libvulkan_freedreno.so')
    prefixes = [args.native_prefix, *args.dependency_prefix]
    if args.graphics_prefix:
        prefixes.append(args.graphics_prefix)
    hashes = {}

    def install(src, target):
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise RuntimeError('Duplicate native binary: ' + str(target))
        shutil.copyfile(src, target)
        target.chmod(0o755)
        subprocess.run([str(strip), '--strip-debug', str(target)], check=True)
        header = subprocess.check_output([str(readelf), '-h', str(target)], text=True)
        if 'AArch64' not in header:
            raise RuntimeError('Expected an ARM64 native binary: ' + str(target))
        dynamic = subprocess.check_output([str(readelf), '-d', str(target)], text=True)
        pending.extend(re.findall(r'\(NEEDED\).*\[([^]]+)\]', dynamic))

    install(args.core / 'loader/wine', unix / 'wine')
    install(args.core / 'server/wineserver', destination / 'bin/wineserver')
    for src in sorted((args.core / 'dlls').glob('*/*.so')):
        install(src, unix / src.name)
    for name in ['libarm64ecfex.so', 'libwow64fex.so']:
        install(args.fex / name, unix / name)
    if args.vulkan_driver == 'system':
        install(args.system_vulkan_adapter, unix / 'libvulkan_android_desktop.so')
        install(compat_layer, unix / compat_layer.name)
    while pending:
        name = pending.pop()
        if name in SYSTEM_LIBS or (unix / name).exists():
            continue
        libraries = [prefix / subdir / name for prefix in prefixes
                     for subdir in ['lib', 'lib/pulseaudio', 'lib/aarch64-linux-gnu']]
        library = next((path for path in libraries if path.is_file()), None)
        if library is None:
            raise RuntimeError('Missing Bionic dependency: ' + name)
        install(library.resolve(), unix / name)
    loader = subprocess.check_output([str(readelf), '-l', str(unix / 'wine')], text=True)
    if '/system/bin/linker64' not in loader:
        raise RuntimeError('Wine loader does not use the Android linker')
    (destination / 'bin/wine').symlink_to('../lib/wine/aarch64-unix/wine')
    config = destination / 'etc/fonts/fonts.conf'
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text('<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "fonts.dtd">\n'
                      '<fontconfig><dir>/system/fonts</dir><cachedir prefix="xdg">fontconfig</cachedir></fontconfig>\n')
    drivers = [] if args.vulkan_driver == 'none' else [args.vulkan_driver]
    if turnip and 'turnip' not in drivers:
        drivers.append('turnip')
    for backend in drivers:
        icd = destination / ('share/vulkan/icd.d/' + backend + '_icd.aarch64.json')
        icd.parent.mkdir(parents=True, exist_ok=True)
        if backend == 'system':
            driver = {'file_format_version': '1.0.0', 'ICD': {'api_version': '1.3.0',
                      'library_path': '../../../lib/wine/aarch64-unix/libvulkan_android_desktop.so'}}
        else:
            original_icd = next((args.graphics_prefix / 'share/vulkan/icd.d').glob('*freedreno*.json'))
            driver = json.loads(original_icd.read_text())
            driver['ICD']['library_path'] = '../../../lib/wine/aarch64-unix/libvulkan_freedreno.so'
        icd.write_text(json.dumps(driver, indent=2) + '\n')
    if compat_layer:
        manifest = json.loads(compat_layer.with_name('VkLayer_hybris_compat.json').read_text())
        manifest['layer']['library_path'] = '../../../lib/wine/aarch64-unix/' + compat_layer.name
        target = destination / 'share/vulkan/explicit_layer.d/VkLayer_hybris_compat.json'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(manifest, indent=2) + '\n')
    # Ship the LGPL-derived backend and its source preparation alongside Wine.
    shutil.copytree(args.native_prefix / 'share/licenses/dbus-1', destination / 'share/licenses/dbus-1')
    gamepad_sources = Path(__file__).resolve().parents[1]
    if compat_layer:
        shutil.copytree(compat_layer.parent / 'licenses/libhybris-vulkan',
                        destination / 'share/licenses/libhybris-vulkan')
    gamepad_destination = destination / 'share/arlinux/winebus'
    gamepad_destination.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(gamepad_sources / 'dlls/winebus.sys/bus_arlinux.h', gamepad_destination / 'bus_arlinux.h')
    shutil.copyfile(gamepad_sources / 'arlinux/winebus/README.md', gamepad_destination / 'README.md')
    shutil.copyfile(gamepad_sources / 'COPYING.LIB', gamepad_destination / 'COPYING.LIB')
    packaging = optimize(args.output, strip, remove_x64=False)
    for path in sorted(destination.rglob('*')):
        if path.is_file() and not path.is_symlink():
            with path.open('rb') as stream:
                hashes[path.relative_to(args.output).as_posix()] = hashlib.file_digest(stream, 'sha256').hexdigest()
    metadata = dict(protocol=1, taskProtocol=1, libc='bionic', experimental=True,
                    wine=original['wine_version'].removeprefix('Wine version ').strip(),
                    wineCommit=wine_commit,
                    wineSource=dict(repository='https://github.com/taowen/wine',
                                    commit=wine_commit, archiveURL=source_url,
                                    archiveName=source_asset,
                                    archiveSHA256=source_archive_sha256),
                    fexCommit=fex_commit,
                    originalSources={'wine-native': dict(asset=source_asset, sha256=source_archive_sha256)},
                    packaging=packaging, vulkanDriver=args.vulkan_driver,
                    vulkanCompat='hybris' if compat_layer else 'none',
                    openglDriver='zink' if opengl else 'none',
                    graphicsBuild=graphics_record,
                    backends=dict(gamepad=True, vulkan=(unix / 'winevulkan.so').exists(),
                                  opengl=opengl,
                                  audio=(unix / 'winepulse.so').exists(),
                                  tls=bool(list(unix.glob('libgnutls.so*')))),
                    limitations=([] if opengl else ['No OpenGL backend']) + ([] if args.vulkan_driver != 'none' else
                                 ['Vulkan driver not packaged']), files=hashes)
    (args.output / 'bionic-runtime.json').write_text(json.dumps(metadata, indent=2) + '\n')
    print('Packaged Bionic Wine:', len(hashes), 'files')



if __name__ == '__main__':
    main()
