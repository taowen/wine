#!/usr/bin/env python3
"""Build and publish the independent Android Bionic Wine runtime artifacts."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from wine_graph import ComponentCache, ComponentComplete, required_stages, PLAN
from pipeline_cache import Cache, digest, inventory, key, run, source_state
from component_inputs import core_path, RESOURCES, source_inputs
import runtime_assembly as assembly

def build_wine(cache, framework, android, paths, common, invoke, env):
    """Build the complete standalone Wine chain, including the Steamworks ABI inputs."""
    recipes = {
        'wine-pulse': (),
        'wine-pe': ('arlinux/build/wine-pe.sh',),
        'fex': ('arlinux/build/fex-windows.sh', 'arlinux/components/FEX'),
        'dxvk': ('arlinux/build/dxvk-windows.sh', 'arlinux/components/dxvk', 'arlinux/components/dxvk-legacy', 'arlinux/dxvk-probe.c'),
        'wine-tls': ('arlinux/components/bionic-deps/build-tls.sh',),
        'wine-graphics': ('arlinux/build/wine-bionic-graphics.sh', 'arlinux/components/mesa', 'arlinux/components/libxshmfence', 'arlinux/components/libhybris/arlinux/graphics-protocols'),
        'wine-vulkan': ('arlinux/components/libhybris/arlinux/build-bionic-vulkan.py', 'arlinux/components/libhybris', 'arlinux/components/android-headers'),
        'wine-bionic-compile': ('arlinux/build/wine-bionic.sh', 'arlinux/components/FEX'),
        'wine-bionic': ('arlinux/package-wine-bionic.py', 'arlinux/wine_pe_debug.py',
                        'dlls/winebus.sys/bus_arlinux.h', 'arlinux/winebus', 'COPYING.LIB'),
    }
    def stage(name, command, dependencies=()):
        if not cache.selected(name):
            return None
        inputs = {k: v for k, v in common.items() if k not in ('sources', 'wineSources', 'pulseRecipe', 'engines')}
        inputs['engine'] = common['engines'][name]
        inputs['toolchain'] = {n: common['toolchain'][n] for n in RESOURCES[name]}
        inputs['sources'] = {p: source_state(framework/p) if (framework/p).is_dir() else inventory(framework/p)
                             for p in recipes.get(name, recipes['wine-pe'])}
        if name in ('wine-pe-aarch64', 'wine-pe-x86_64', 'wine-pe-i386', 'wine-bionic-compile'):
            inputs['wineSources'] = common['wineSources']
        if name == 'wine-pulse':
            inputs['pulseRecipe'] = common['pulseRecipe']
        inputs['dependencies'] = {n: cache.records[n] for n in dependencies}
        return cache.stage(name, inputs, command)

    def pulse(out):
        out.mkdir()
        env.update(ARLINUX_PULSE_WORK=str(out / 'work'), ARLINUX_PULSE_OUTPUT=str(out / 'android'))
        try:
            invoke('bash', android / 'arlinux/components/pulseaudio-android/arlinux/build.sh', android,
                   common['engines']['wine-pulse'])
        finally:
            env.pop('ARLINUX_PULSE_WORK', None)
            env.pop('ARLINUX_PULSE_OUTPUT', None)
    pulse = stage('wine-pulse', pulse)

    pe_parts = {}
    for arch in ('aarch64', 'x86_64', 'i386'):
        def compile_pe(out, arch=arch):
            env['ARLINUX_WINE_PE_ARCH'] = arch
            try:
                invoke('bash', 'arlinux/build/wine-pe.sh', paths['wineSource'], paths['mingw'], out)
            finally:
                env.pop('ARLINUX_WINE_PE_ARCH', None)
        pe_parts[arch] = stage('wine-pe-' + arch, compile_pe)
    def merge_pe(out):
        # Common headers, data and host tools come from the ARM64 build.
        # Other jobs contribute only their architecture's Windows modules.
        shutil.copytree(pe_parts['aarch64'], out, symlinks=True)
        for arch in ('x86_64', 'i386'):
            relative = Path('files/lib/wine') / (arch + '-windows')
            shutil.copytree(pe_parts[arch] / relative, out / relative, symlinks=True)
    wine = stage('wine-pe', merge_pe, tuple('wine-pe-'+a for a in pe_parts))
    arm = pe_parts['aarch64']
    tools = Path(json.loads((arm / 'build-state.json').read_text())['tools']) if arm else None
    fex = stage('fex', lambda out: invoke('bash', 'arlinux/build/fex-windows.sh', paths['mingw'], out))
    dxvk = stage('dxvk', lambda out: invoke('bash', 'arlinux/build/dxvk-windows.sh', paths['mingw'], out))

    tls = stage('wine-tls', lambda out: invoke('bash', 'arlinux/components/bionic-deps/build-tls.sh', paths['ndk'], out))
    graphics = stage('wine-graphics', lambda out: invoke('bash', 'arlinux/build/wine-bionic-graphics.sh',
                     framework / 'arlinux/components/mesa', paths['ndk'], paths['bionicPrefix'],
                     framework / 'arlinux/components/libhybris/arlinux/graphics-protocols', framework / 'arlinux/components/libxshmfence', out), ('bionic-deps',))
    adapter = stage('wine-vulkan', lambda out: invoke('python3', 'arlinux/components/libhybris/arlinux/build-bionic-vulkan.py',
                    '--framework', framework, '--source', framework / 'arlinux/components/libhybris', '--headers', framework / 'arlinux/components/android-headers',
                    '--ndk', paths['ndk'], '--native-prefix', paths['bionicPrefix'],
                    '--vulkan-headers', paths['vulkanHeaders'] / 'include', '--output', out), ('bionic-deps',))
    if cache.selected('wine-bionic-compile'):
        env.update(BIONIC_TLS_PREFIX=str(tls / 'prefix'), BIONIC_PULSE_PREFIX=str(pulse / 'work/prefix'),
                   BIONIC_GRAPHICS_PREFIX=str(graphics / 'prefix'), BIONIC_OPENGL='zink')
    compiled = stage('wine-bionic-compile', lambda out: invoke(
        'bash', 'arlinux/build/wine-bionic.sh', paths['wineSource'], tools,
        paths['mingw'], paths['ndk'], paths['bionicPrefix'], framework / 'arlinux/components/FEX', out),
        ('wine-pe-aarch64', 'bionic-deps', 'wine-tls', 'wine-pulse', 'wine-graphics'))

    native = stage('wine-bionic', lambda out: assembly.package_bionic(out, framework,
        compiled, tls, pulse, graphics, adapter, paths, invoke),
        ('wine-bionic-compile', 'bionic-deps', 'wine-tls', 'wine-pulse', 'wine-graphics', 'wine-vulkan'))
    if compiled is not None and wine is not None:
        paths.update(wineSource=compiled / 'source', wineBuild=compiled / 'core',
                     wineTools=tools, wineImportLibs=wine / 'files/lib/wine')
        env['WINE_IMPORT_LIBS'] = str(paths['wineImportLibs'])
    return native



def build_bionic_deps(cache, framework, android, ndk):
    files = ['toolchain.json'] + ['tools/build/' + name for name in (
        'ndk-env.sh', 'ndk-wayland.sh', 'ndk-deps.sh', 'ndk-xcb.sh',
        'android-cross.ini.tpl', 'native.ini.tpl', 'stubs')]
    inputs = dict(recipe=inventory(framework / 'arlinux/components/bionic-deps/build-deps.sh'),
                  sources={name: inventory(android / name) for name in files},
                  ndk=inventory(ndk / 'source.properties'),
                  compiler=inventory(ndk / 'toolchains/llvm/prebuilt/linux-x86_64/bin/clang'))
    out = cache.stage('bionic-deps', inputs, lambda out: run(
        'bash', framework / 'arlinux/components/bionic-deps/build-deps.sh', android, ndk, out,
        cwd=framework, env=os.environ.copy()))
    return out / 'prefix'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--target', required=True,
                        choices=tuple(name for name in required_stages(None)
                                      if PLAN['stages'][name].get('execution') != 'local'))
    parser.add_argument('--require-dependencies', action='store_true')
    parser.add_argument('--rebuild', action='store_true')
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    framework = Path(config['framework']).resolve(strict=True)
    paths = {name: Path(config[name]).resolve() for name in ('wineSource', 'mingw', 'ndk', 'addons', 'vulkanHeaders')}
    android = framework
    dirty = subprocess.check_output(['git', '-C', str(paths['wineSource']), 'status', '--porcelain', '--untracked-files=normal'])
    if dirty:
        raise ValueError('wineSource must be the pristine pinned checkout')
    root = framework / 'build/pipeline'
    root.mkdir(parents=True, exist_ok=True)
    with (root / '.wine.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        cache = ComponentCache(root, args.rebuild, args.target, args.require_dependencies)
        if cache.selected('bionic-deps'):
            base = build_bionic_deps(cache, framework, android, paths['ndk'])
            paths['bionicPrefix'] = base
        env = {name: value for name, value in os.environ.items() if name in (
            'PATH', 'HOME', 'LANG', 'LC_ALL', 'TMPDIR', 'SSL_CERT_FILE', 'SSL_CERT_DIR',
            'http_proxy', 'https_proxy', 'no_proxy', 'ARLINUX_WINDOWS_FONTS',
            'JOBS', 'CCACHE_DIR', 'CCACHE_BASEDIR', 'ANDROID_HOME', 'JAVA_HOME')}
        env.update(ANDROID_NDK_HOME=str(paths['ndk']), BIONIC_VULKAN_DRIVER='system', BIONIC_OPENGL='zink')
        if 'bionicPrefix' in paths:
            env['BIONIC_INCLUDE'] = str(paths['bionicPrefix'] / 'include')
        resources = {r for name in cache.required for r in RESOURCES[name]}
        toolchain = {name: inventory(paths[name]) for name in resources}
        sources = source_state(framework)
        revision = subprocess.check_output(['git', '-C', str(framework), 'rev-parse', 'HEAD'], text=True).strip()
        engines = {name: source_inputs(framework, revision, name, PLAN)['recipeSha256'] for name in cache.required}
        common = dict(engines=engines, config=config, sources=sources,
                      wineSources={n: v for n, v in sources.items() if core_path(n)}, toolchain=toolchain)
        common['hostTools'] = {name: subprocess.check_output([name, '--version'], text=True)
                               for name in ('gcc', 'g++', 'make', 'cmake', 'ninja', 'meson', 'pkg-config')}
        common['hostFreeType'] = subprocess.check_output(['pkg-config', '--modversion', 'freetype2'], text=True).strip()
        common['pulseRecipe'] = {str(p.relative_to(android)): inventory(p) for p in (
            android / 'arlinux/components/pulseaudio-android/arlinux/build.sh', android / 'tools/build/ndk-env.sh',
            android / 'toolchain.json', android / 'arlinux/components/pulseaudio-android')} if cache.selected('wine-pulse') else {}
        invoke = lambda *command: run(*command, cwd=framework, env=env)
        base = build_wine(cache, framework, android, paths, common, invoke, env)
        def helper_inputs(stage, *sources, **dependencies):
            inputs = {name: value for name, value in common.items()
                      if name not in ('sources', 'wineSources', 'pulseRecipe', 'engines')}
            inputs['engine'] = engines[stage]
            inputs['sources'] = {name: inventory(framework / name) for name in sources}
            inputs['dependencies'] = dependencies
            inputs['toolchain'] = {'mingw': common['toolchain']['mingw']}
            return inputs
        bridge = observer = tray = None
        if cache.selected('steamworks'):
            bridge = cache.stage('steamworks', helper_inputs('steamworks', 'arlinux/components/steamworks', 'arlinux/components/steamworks/build.sh',
                wine=cache.records['wine-bionic-compile'], imports=cache.records['wine-pe']), lambda out: invoke('bash', 'arlinux/components/steamworks/build.sh',
                    paths['wineSource'], paths['wineBuild'], paths['wineTools'], paths['mingw'], out))
        if cache.selected('windows-accessibility'):
            observer = cache.stage('windows-accessibility', helper_inputs('windows-accessibility', 'arlinux/components/windows-integration/accessibility',
                'arlinux/components/windows-integration/build-accessibility.sh'), lambda out: invoke(
                'bash', 'arlinux/components/windows-integration/build-accessibility.sh', paths['mingw'], out))
        if cache.selected('windows-tray'):
            tray = cache.stage('windows-tray', helper_inputs('windows-tray', 'arlinux/components/windows-integration/tray',
                'arlinux/components/windows-integration/build-tray.sh'), lambda out: invoke(
                'bash', 'arlinux/components/windows-integration/build-tray.sh', paths['mingw'], out))


if __name__ == '__main__':
    try:
        main()
    except ComponentComplete as complete:
        print('Completed component: ' + complete.name, flush=True)
