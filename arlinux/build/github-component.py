#!/usr/bin/env python3
"""Internal hosted-runner adapter; recipes remain behind build.sh."""
from component_recipes import load_plan
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from artifact_download import download, download_capsule
from component_inputs import requirements, request_identity, source_inputs



def run(*args, **kwargs):
    subprocess.run(list(map(str, args)), check=True, **kwargs)


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def request():
    data = json.loads(os.environ['ARLINUX_COMPONENT_REQUEST'])
    framework = Path(__file__).resolve().parents[2]
    plan = load_plan(framework/'arlinux/build')
    stage = plan['stages'].get(data.get('stage'))
    if not stage or os.environ.get('GITHUB_REPOSITORY') != stage['repository']:
        raise ValueError('Component must run in its owning repository')
    for name in ('framework',):
        if not re.fullmatch('[0-9a-f]{40}', data.get(name, '')):
            raise ValueError('Expected a pinned ' + name + ' commit')
    identity = data['identity']
    actual = request_identity(data)
    if identity != actual:
        raise ValueError('Component request identity differs from its inputs')
    if data.get('format') == 2 and data['sourceInputs'] != source_inputs(framework, data['framework'], data['stage'], plan):
        raise ValueError('Pinned checkout differs from the declared component sources')
    expected_arch = 'arm64' if stage['runner'].endswith('-arm') else 'x64'
    if os.environ.get('RUNNER_ARCH', '').lower() != expected_arch:
        raise ValueError('Unexpected hosted runner architecture')
    return data, framework


def prepare(data, framework):
    if os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted':
        raise RuntimeError('Hosted-runner bootstrap cannot run on a workstation')
    if data['stage'] == 'windows-fonts':
        root = framework.parent
        run(sys.executable, '-m', 'pip', 'install', 'zstandard==0.25.0')
    else:
        root = Path('/opt/arlinux')
        run('sudo', 'mkdir', '-p', root)
        run('sudo', 'chown', '-R', f'{os.getuid()}:{os.getgid()}', root)
        for name in ('framework',):
            shutil.move(str(framework.parent / name), root / name)
        framework = root / 'framework'
        plan = load_plan(framework/'arlinux/build')
        if plan['stages'][data['stage']].get('artifactInputsOnly'):
            # Standard hosted runners already provide Python, Git and tar.
            # Assembly needs the capsule decoder, not any compiler toolchain.
            python_lib = root/'pythonlib'
            run(sys.executable, '-m', 'pip', 'install', '--target', python_lib, 'zstandard==0.25.0')
            modules, _ = requirements(data['stage'], plan)
            if modules:
                run('git', '-C', framework, 'submodule', 'update', '--init', '--depth', '1', '--', *modules)
            with Path(os.environ['GITHUB_ENV']).open('a') as out:
                out.write('ARLINUX_FRAMEWORK='+framework.as_posix()+'\n')
                out.write('ARLINUX_WORKSPACE='+root.as_posix()+'\n')
                out.write('PYTHONPATH='+str(python_lib)+'\nARLINUX_ASSEMBLY_ONLY=1\n')
            with Path(os.environ['GITHUB_OUTPUT']).open('a') as out:
                out.write('cache-paths='+str(framework/'build/pipeline'/data['stage'])+'\n')
                lock = json.loads((framework/'arlinux/build/github-inputs.json').read_text())
                _, resources = requirements(data['stage'], plan)
                for name in sorted(resources):
                    identity = hashlib.sha256(json.dumps(lock[name], sort_keys=True).encode()).hexdigest()
                    out.write('vendor-'+name+'-key='+identity+'\n')
            return
        # Only remove optional software on the ephemeral GitHub runner.
        # Never run this cleanup in a workstation or self-hosted environment.
        if data['stage'] in ('wine-bionic-compile', 'wine-graphics', 'fex'):
            run('sudo', 'rm', '-rf', '/usr/share/dotnet', '/usr/local/lib/android',
                '/opt/ghc', '/usr/local/share/boost', '/opt/hostedtoolcache/CodeQL')
        packages = ['autoconf', 'automake', 'libtool', 'build-essential', 'bison', 'flex',
            'curl', 'git', 'cmake', 'ninja-build', 'patchelf', 'pkg-config', 'python3',
            'python3-venv', 'python3-pip', 'python3-mako', 'python3-zstandard',
            'debootstrap', 'util-linux', 'zstd',
            'libarchive-tools', 'jq', 'gcc-aarch64-linux-gnu', 'g++-aarch64-linux-gnu',
            'glslang-tools', 'wayland-protocols', 'libfreetype-dev', 'libexpat1-dev',
            'libwayland-dev', 'libwayland-egl-backend-dev', 'libx11-dev', 'libx11-xcb-dev', 'libxcb1-dev',
            'libegl-dev', 'libgl-dev', 'libgles-dev', 'libglvnd-dev', 'libvulkan-dev',
            'libdrm-dev', 'libxrandr-dev', 'libxfixes-dev', 'libxshmfence-dev',
            'libxxf86vm-dev', 'libxcb-dri3-dev', 'libxcb-present-dev', 'libxcb-sync-dev',
            'libxcb-glx0-dev', 'libxcb-dri2-0-dev', 'libxcb-shm0-dev',
            'libxcb-xfixes0-dev', 'libxcb-randr0-dev', 'libxdamage-dev', 'libxext-dev',
            'libelf-dev', 'libudev-dev', 'libgbm-dev', 'gperf', 'libglib2.0-dev', 'libglib2.0-dev-bin',
            'ccache', 'gettext', 'libgettextpo-dev', 'libssl-dev', 'unzip', 'autopoint', 'intltool', 'python-is-python3']
        if data['stage'] in ('wine-capture', 'wine-diagnostics', 'windows-accessibility', 'windows-tray',
                             'wine-pe', 'wine-pe-aarch64', 'wine-pe-x86_64', 'wine-pe-i386'):
            packages = ['build-essential', 'bison', 'flex', 'ccache', 'gettext', 'libfreetype-dev',
                        'cmake', 'ninja-build', 'pkg-config', 'git', 'curl', 'unzip',
                        'python3', 'python3-venv', 'python3-pip', 'zstd']
        if data['stage'] == 'wine-vulkan':
            packages = ['build-essential', 'ccache', 'cmake', 'ninja-build', 'pkg-config',
                        'git', 'curl', 'unzip', 'patch', 'python3', 'python3-venv',
                        'python3-pip', 'zstd', 'libfreetype-dev', 'libwayland-bin']
        run('sudo', 'apt-get', 'update')
        run('sudo', 'apt-get', 'install', '-y', '--no-install-recommends', *packages,
            env=dict(os.environ, DEBIAN_FRONTEND='noninteractive'))
        run('python3', '-m', 'venv', root / 'python')
        run(root / 'python/bin/pip', 'install', 'meson==1.12.0', 'mako==1.3.10', 'packaging==25.0', 'PyYAML==6.0.2', 'zstandard==0.25.0')
        bin_dir = root / 'bin'
        bin_dir.mkdir(exist_ok=True)
        cross = bin_dir / 'aarch64-linux-gnu-pkg-config'
        cross.write_text('#!/bin/sh\nexport PKG_CONFIG_LIBDIR=/usr/lib/aarch64-linux-gnu/pkgconfig:/usr/share/pkgconfig\nexec pkg-config "$@"\n')
        cross.chmod(0o755)
        plan = load_plan(framework/'arlinux/build')
        modules, _ = requirements(data['stage'], plan)
        if modules:
            run('git', '-C', framework, 'submodule', 'update', '--init', '--depth', '1', '--', *modules)
        if 'arlinux/components/gfxreconstruct' in modules:
            run('git', '-C', framework/'arlinux/components/gfxreconstruct', 'submodule', 'update', '--init', '--depth', '1',
                '--', 'external/Vulkan-Headers', 'external/SPIRV-Headers', 'external/SPIRV-Reflect')
        if 'arlinux/components/FEX' in modules:
            run('git', '-C', framework/'arlinux/components/FEX', 'submodule', 'update', '--init', '--recursive', '--depth', '1')
        for marker in [framework/'.git', *framework.glob('arlinux/components/*/.git')]:
            if marker.exists():
                run('git', 'config', '--global', '--add', 'safe.directory', marker.parent)
        with Path(os.environ['GITHUB_PATH']).open('a') as out:
            out.write(str(root / 'python/bin')+'\n'+str(bin_dir)+'\n')
    with Path(os.environ['GITHUB_ENV']).open('a') as out:
        out.write('ARLINUX_FRAMEWORK='+framework.as_posix()+'\n')
        out.write('ARLINUX_WORKSPACE='+root.as_posix()+'\n')
        if platform.system() == 'Linux':
            # GNU tar otherwise applies the runner umask to cached file modes.
            # Output receipts include those modes, so preserve them on restore.
            out.write('TAR_OPTIONS=--same-permissions\n')
    if data['stage'] != 'windows-fonts':
        paths = [str(root/'cache/ccache')]
        # Complete compiler trees travel in verified capsules; Actions caches
        # hold bounded compiler results and small component outputs.
        if not data['stage'].startswith('wine-pe') and data['stage'] not in ('wine-bionic-compile','wine-bionic'):
            paths.append(str(framework/'build/pipeline'/data['stage']))
        if data['stage'] == 'wine-capture':
            paths.append(str(framework/'build/gfxreconstruct-bionic'))
        # PE output, source and tool trees already travel in verified component
        # capsules. Retain only ccache here, not a second multi-GB output copy.
        with Path(os.environ['GITHUB_OUTPUT']).open('a') as out:
            out.write('cache-paths<<ARLINUX_CACHE_PATHS\n'+'\n'.join(paths)+'\nARLINUX_CACHE_PATHS\n')
            lock = json.loads((framework/'arlinux/build/github-inputs.json').read_text())
            _, resources = requirements(data['stage'], plan)
            for name in sorted(resources):
                identity = hashlib.sha256(json.dumps(lock[name], sort_keys=True).encode()).hexdigest()
                out.write('vendor-'+name+'-key='+identity+'\n')


def toolchains(root, framework):
    lock = json.loads((framework / 'arlinux/build/github-inputs.json').read_text())
    stage = json.loads(os.environ['ARLINUX_COMPONENT_REQUEST'])['stage']
    plan = load_plan(framework/'arlinux/build')
    _, resources = requirements(stage, plan)
    cache = root / 'cache/downloads'
    tools = root / 'toolchains'
    tools.mkdir(exist_ok=True)
    for name in sorted(resources - {'addons'}):
        item = lock[name]
        archive = download(item['url'], cache / name / item['file'], item['sha256'])
        destination = tools / name
        if not destination.exists():
            temporary = tools / (name+'-extract')
            temporary.mkdir(exist_ok=True)
            if archive.suffix == '.zip':
                run('unzip', '-q', archive, '-d', temporary)
            else:
                with tarfile.open(archive) as source:
                    source.extractall(temporary, filter='data')
            entries = list(temporary.iterdir())
            if len(entries) != 1 or not entries[0].is_dir():
                raise ValueError('Unexpected toolchain archive layout')
            entries[0].rename(destination)
            temporary.rmdir()
    addons = root / 'vendor/addons'
    addons.mkdir(parents=True, exist_ok=True)
    for item in (lock['addons'] if 'addons' in resources else []):
        archive = download(item['url'], cache / 'addons' / item['file'], item['sha256'])
        destination = addons / item['directory']
        destination.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive) as source:
            source.extractall(destination, filter='data')
    mono = addons / 'mono'
    alias = mono / 'wine-mono'
    if 'addons' in resources and not alias.exists():
        alias.symlink_to('wine-mono-11.3.0')
    launcher = tools / 'mingw-ccache/bin'
    launcher.mkdir(parents=True, exist_ok=True)
    # clang-scan-deps constructs a driver from the compiler path without
    # executing its wrapper, so the complete MinGW sysroot must remain visible.
    for entry in ((tools/'mingw').iterdir() if 'mingw' in resources else []):
        target=launcher.parent/entry.name
        if entry.name!='bin' and not target.exists():
            target.symlink_to(entry,target_is_directory=entry.is_dir())
    for compiler in ((tools/'mingw/bin').iterdir() if 'mingw' in resources else []):
        target = launcher/compiler.name
        if target.exists() or target.is_symlink():
            continue
        if (compiler.name in ('clang','clang++','cc','c++','gcc','g++') or
                compiler.name.endswith(('-clang','-clang++','-gcc','-g++','-cc','-c++'))) and compiler.is_file():
            target.write_text('#!/bin/sh\nexec ccache "'+str(compiler)+'" "$@"\n')
            target.chmod(0o755)
        else:
            target.symlink_to(compiler)
    plan=load_plan(framework/'arlinux/build')
    if not plan['stages'][stage].get('artifactInputsOnly'):
        run('ccache', '--max-size', str(plan['stages'][stage]['compilerCacheMiB'])+'M',
            env=dict(os.environ,CCACHE_DIR=str(root/'cache/ccache')))
    return dict(format=1, framework=str(framework), wineSource=str(framework),
        ndk=str(tools/'ndk'), mingw=str(launcher), addons=str(addons),
        vulkanHeaders=str(tools/'vulkanHeaders'))


def import_dependency(root, dependency, visited=None, *, recursive=True):
    if visited is None:
        visited = set()
    if dependency['identity'] in visited:
        return
    visited.add(dependency['identity'])
    directory = root / 'incoming' / dependency['identity']
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory/'component.json'
    if 'manifestText' in dependency:
        contents = dependency['manifestText'].encode('utf-8')
        if hashlib.sha256(contents).hexdigest() != dependency['manifestSha256']:
            raise ValueError('Pinned dependency manifest SHA-256 mismatch')
        manifest.write_bytes(contents)
    else:
        manifest = download(dependency['manifestUrl'], manifest, dependency['manifestSha256'])
    record = json.loads(manifest.read_text())
    if record['identity'] != dependency['identity']:
        raise ValueError('Dependency identity mismatch')
    if recursive:
        for parent in record.get('dependencies', []):
            import_dependency(root, parent, visited)
    archive = download_capsule(record, directory)
    def capsule_filter(member, destination):
        original_link = None
        if member.issym() and member.linkname.startswith('/'):
            target = Path(member.linkname)
            if not target.is_relative_to(root):
                raise ValueError('Component link escapes build workspace')
            original_link = member.linkname
            member = member.replace(linkname=os.path.relpath(target, root/Path(member.name).parent))
        filtered = tarfile.data_filter(member, destination)
        if filtered.isfile() or filtered.islnk():
            # Git pack objects are read-only. Restoring over a prior Actions
            # cache must replace the file before writing, then restore its
            # exact original mode for the build-cache receipt to match.
            existing=Path(destination)/filtered.name
            if existing.is_file() or existing.is_symlink():
                existing.unlink()
            filtered=filtered.replace(mode=member.mode & 0o777)
        return filtered.replace(linkname=original_link) if original_link else filtered
    def checked_members(source):
        for member in source:
            if member.name.startswith('/') or '..' in Path(member.name).parts:
                raise ValueError('Unsafe component capsule path')
            yield member
    import zstandard
    with archive.open('rb') as compressed, zstandard.ZstdDecompressor().stream_reader(compressed) as decoded:
        with tarfile.open(fileobj=decoded, mode='r|') as source:
            source.extractall(root, members=checked_members(source), filter=capsule_filter)
    archive.unlink()
    return record


def restore_seed(root, framework, data, visited):
    if not data.get('cacheSeed'):
        return
    stage_cache = framework/'build/pipeline'/data['stage']
    from pipeline_cache import verified_output
    for receipt in stage_cache.glob('*/receipt.json'):
        try:
            verified_output(receipt.parent, json.loads(receipt.read_text()))
            print('Keep verified Actions cache before release seed: '+data['stage'], flush=True)
            return
        except (OSError, ValueError, KeyError) as failure:
            print('Invalid restored '+data['stage']+': '+str(failure), flush=True)
    # A debug release may have newer dependencies than this prior receipt.
    # Seed only this component; declared current dependencies are imported next.
    import_dependency(root, data['cacheSeed'], visited, recursive=False)


def build_windows_fonts(data, framework):
    root = Path(os.environ['ARLINUX_WORKSPACE'])
    output = root / 'vendor/fonts'
    output.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(framework/'arlinux'))
    from wine_fonts import FILES
    for name in FILES:
        shutil.copyfile(Path(os.environ['WINDIR'])/'Fonts'/name, output/name)


def build_wine_capture(data, framework):
    root = Path(os.environ['ARLINUX_WORKSPACE'])
    stage = data['stage']
    tools = toolchains(root, framework)
    from pipeline_cache import Cache, inventory
    cache = Cache(framework/'build/pipeline')
    dependency = import_dependency(root, data['dependencies'][0], set(), recursive=False)
    prefix = root/dependency['pipelineOutput']/'prefix'
    source = framework/'arlinux/components/gfxreconstruct'
    source_commit = subprocess.check_output(['git','-C',str(source),'rev-parse','HEAD'],text=True).strip()
    inputs = dict(source=data['sourceInputs']['recipeSha256'], dependency=dependency['outputIdentity'],
                  toolchain=sha(framework/'arlinux/build/github-inputs.json'))
    def compile_capture(output):
        build_dir = framework/'build/gfxreconstruct-bionic'/source_commit
        staged = output/'tools'
        run('bash', source/'arlinux/build-bionic.sh', tools['ndk'], prefix, build_dir, staged,
            env=dict(os.environ, JOBS='4', CCACHE_DIR=str(root/'cache/ccache')))
        archive = output/'arlinux-wine-capture.tar.gz'
        with tarfile.open(archive,'w:gz') as target:
            target.add(staged,arcname='capture-tools')
        (output/(archive.name+'.sha256')).write_text(sha(archive)+'  '+archive.name+'\n')
        shutil.rmtree(staged)
        shutil.copyfile(framework/'arlinux/vulkan-capture.py', output/'vulkan-capture.py')
    output = cache.stage(stage, inputs, compile_capture)
    descriptor = framework/'build/artifacts/components'/f'{stage}.json'
    descriptor.parent.mkdir(parents=True,exist_ok=True)
    descriptor.write_text(json.dumps(dict(output=str(output),records=cache.records))+'\n')


def build_wine_diagnostics(data, framework):
    root = Path(os.environ['ARLINUX_WORKSPACE'])
    stage = data['stage']
    tools = toolchains(root, framework)
    from pipeline_cache import Cache, inventory
    cache = Cache(framework/'build/pipeline')
    inputs = {str(path.relative_to(framework)): inventory(path) for path in (
        framework/'arlinux/build/windows-debug.sh', framework/'arlinux/windows-debug.py',
        framework/'arlinux/components/windows-integration/diagnostics/exception-observer.c',
        framework/'arlinux/components/windows-integration/diagnostics/exception-observer-probe.c',
        framework/'arlinux/components/windows-integration/diagnostics/message-wait.c')}
    inputs['toolchainLock'] = sha(framework/'arlinux/build/github-inputs.json')
    inputs['engine'] = data['sourceInputs']['recipeSha256']
    inputs['compiler'] = inventory(Path(tools['mingw'])/'aarch64-w64-mingw32-clang')
    def compile(output):
        run('bash', framework/'arlinux/build/windows-debug.sh', tools['mingw'], output)
        shutil.copyfile(framework/'arlinux/windows-debug.py', output/'windows-debug.py')
    output = cache.stage(stage, inputs, compile)
    descriptor = framework/'build/artifacts/components'/f'{stage}.json'
    descriptor.parent.mkdir(parents=True, exist_ok=True)
    descriptor.write_text(json.dumps(dict(output=str(output), records=cache.records))+'\n')


def build(data, framework):
    # The tar option belongs to Actions cache restore/save, not compiler recipes.
    os.environ.pop('TAR_OPTIONS', None)
    root = Path(os.environ['ARLINUX_WORKSPACE'])
    stage = data['stage']
    if stage in ('windows-fonts', 'wine-capture', 'wine-diagnostics'):
        globals()['build_'+stage.replace('-', '_')](data, framework)
        return
    visited = set()
    plan = load_plan(framework/'arlinux/build')
    assembly = plan['stages'][stage].get('artifactInputsOnly', False)
    restore_seed(root, framework, data, visited)
    artifacts = {}
    for dependency in data['dependencies']:
        record = import_dependency(root, dependency, visited, recursive=not assembly)
        if assembly:
            artifacts[record['stage']] = record['pipelineOutput']
    if stage == 'wine-bionic':
        from runtime_assembly import build_native
        build_native(data, framework, artifacts, toolchains(root, framework))
        return
    config = root / 'product.json'
    config.write_text(json.dumps(dict(framework=str(framework), artifacts=artifacts)
                                 if assembly else toolchains(root, framework)))
    env = dict(os.environ, ARLINUX_WINDOWS_FONTS=str(root/'vendor/fonts'), JOBS='4',
        CCACHE_DIR=str(root/'cache/ccache'), CCACHE_BASEDIR=str(framework))
    run('bash', framework/'arlinux/build.sh', 'wine', '--config', config,
        '--target', stage, '--require-dependencies', env=env)


def publish_distribution(data, record, final):
    from runtime_distribution import publish_distribution as publish_product
    publish_product(data, record, final)


def diagnostic_artifacts(framework, record, path):
    files = [path/'diagnostics/libhybris-diagnostics.apk',
             path/'diagnostics/libhybris-diagnostics.json']
    tag = load_plan(framework/'arlinux/build')['stages']['wine-vulkan']['release']
    repository = os.environ['GITHUB_REPOSITORY']
    record['diagnostics'] = [dict(name=file.name, sha256=sha(file), bytes=file.stat().st_size,
        url=f'https://github.com/{repository}/releases/download/{tag}/{file.name}') for file in files]
    return files


def publish(data, framework):
    root = Path(os.environ['ARLINUX_WORKSPACE'])
    stage = data['stage']
    output = root / 'export'
    output.mkdir(exist_ok=True)
    record = dict(data)
    if record.get('cacheSeed'):
        # Cache provenance is useful, but embedding its full prior manifest in
        # each completion marker would recursively grow dispatch requests.
        record['cacheSeed'] = {key: value for key, value in record['cacheSeed'].items()
                               if key != 'manifestText'}
    paths = []
    diagnostic_assets = []
    if stage == 'windows-fonts':
        paths = [root/'vendor/fonts']
    else:
        sys.path.insert(0, str(framework/'arlinux/build'))
        from pipeline_cache import inventory, build_references
        descriptor = json.loads((framework/'build/artifacts/components'/f'{stage}.json').read_text())
        state = descriptor['records'][stage]
        path = Path(descriptor['output'])
        if inventory(path) != state['outputs'] or build_references(path) != state['references']:
            raise ValueError('Component output differs from its build-cache receipt')
        paths = [path.parent, path.parent.parent/'receipt.json',
                 *(Path(name) for name in state['references'])]
        record['pipelineOutput'] = path.relative_to(root).as_posix()
        if stage == 'wine-vulkan':
            diagnostic_assets = diagnostic_artifacts(framework, record, path)
        if stage in ('wine-diagnostics', 'wine-capture'):
            # Consumers need distributable archives, not the native compiler work tree.
            final = framework/('build/artifacts/'+('diagnostics' if stage == 'wine-diagnostics' else 'wine-capture' if stage == 'wine-capture' else 'wine'))
            final.mkdir(parents=True, exist_ok=True)
            for source in path.iterdir():
                if source.is_file():
                    shutil.copyfile(source, final/source.name)
            paths = [final]
    # A forced hosted rerun must verify the rebuilt/cached bytes, but need not
    # recompress or upload the same immutable capsule a second time.
    contents = ({'outputs': state['outputs'], 'references': state['references']}
                if stage != 'windows-fonts' else {str(p.relative_to(root)): {
                    file.relative_to(p).as_posix(): sha(file) for file in sorted(p.rglob('*')) if file.is_file()}
                    for p in paths})
    record['outputIdentity'] = hashlib.sha256(json.dumps(contents, sort_keys=True).encode()).hexdigest()
    tag = load_plan(framework/'arlinux/build')['stages'][stage]['release']
    repository = os.environ['GITHUB_REPOSITORY']
    previous = subprocess.run(['gh','api',f'repos/{repository}/releases/tags/{tag}'], capture_output=True, text=True)
    if not previous.returncode:
        assets = json.loads(previous.stdout)['assets']
        completed = next((a for a in assets if a['name'] == 'component.json'), None)
        if completed:
            expected = completed['digest'].removeprefix('sha256:')
            saved = download(completed['browser_download_url'], output/'previous.json', expected)
            prior = json.loads(saved.read_text())
            if prior['identity'] == data['identity'] and prior.get('outputIdentity') != record['outputIdentity']:
                if stage != 'windows-fonts' and prior['identity'] == data['identity']:
                    from artifact_download import explain_component_difference
                    explain_component_difference(prior, contents, output/'comparison')
                raise ValueError('Published component differs from the verified rerun outputs')
            if prior['identity'] == data['identity']:
                print('OK identical published component '+stage, flush=True)
                return
    from runtime_distribution import RUNTIME_STAGES, publish_runtime_component
    if stage in RUNTIME_STAGES:
        sdk = stage in ('wine-pe-aarch64', 'wine-pe-x86_64', 'wine-pe-i386')
        publish_runtime_component(data, framework, record, paths[0] if stage == 'windows-fonts' else path,
                                  complete=not sdk)
        if not sdk:
            return
    archive = output / 'capsule.tar'
    # Compiler tools can point at generated source/header trees outside the
    # output directory. Preserve those references at the same absolute path.
    # Toolchains, pinned checkouts and declared pipeline dependencies are
    # provisioned separately and must not be duplicated into every capsule.
    position = 0
    while position < len(paths):
        path = paths[position]
        position += 1
        for link in ([path] if path.is_symlink() else path.rglob('*') if path.is_dir() else []):
            if not link.is_symlink():
                continue
            # Source checkouts may intentionally contain dangling links (for
            # example Mesa's .github/AGENTS.md). Preserve their link text;
            # they do not introduce an additional build reference.
            target = link.resolve(strict=False)
            if not target.is_relative_to(root):
                raise ValueError('Build reference is outside the component workspace')
            if not target.exists():
                continue
            if any(target == parent or target.is_relative_to(parent) for parent in paths):
                continue
            if any(target.is_relative_to(parent) for parent in (
                    root/'toolchains', framework/'arlinux/components', framework/'build/pipeline')):
                continue
            paths.append(target)
    with tarfile.open(archive, 'w') as target:
        seen = set()
        for path in paths:
            relative = path.resolve().relative_to(root.resolve())
            if relative in seen:
                continue
            seen.add(relative)
            target.add(path, arcname=relative.as_posix())
    compressed = archive.with_suffix('.tar.zst')
    import zstandard
    with archive.open('rb') as source, compressed.open('wb') as dest:
        with zstandard.ZstdCompressor(level=3, threads=4).stream_writer(dest) as writer:
            shutil.copyfileobj(source, writer)
    archive.unlink()
    record['archiveSha256'] = sha(compressed)
    tag = load_plan(framework/'arlinux/build')['stages'][stage]['release']
    repository = os.environ['GITHUB_REPOSITORY']
    record['parts'] = []
    with compressed.open('rb') as source:
        while block := source.read(1024*1024*1024):
            name = f'capsule-{record["archiveSha256"]}-{len(record["parts"]):03d}.zst.part'
            part = output/name
            part.write_bytes(block)
            record['parts'].append(dict(name=name, sha256=sha(part),
                url=f'https://github.com/{repository}/releases/download/{tag}/{name}'))
    manifest = output/'component.json'
    manifest.write_text(json.dumps(record,sort_keys=True,indent=2)+'\n')
    # Each component owns one fixed release. Upload verified payloads first;
    # component.json is the final completion marker for this generation.
    existing = subprocess.run(['gh','api',f'repos/{repository}/releases/tags/{tag}'],
                              capture_output=True,text=True)
    if existing.returncode:
        run('gh','release','create',tag,'--repo',repository,'--prerelease','--title',tag,
            '--notes','Verified ARLinux build component; not an application release.')
        assets = {}
    else:
        assets = {asset['name']:asset for asset in json.loads(existing.stdout)['assets']}
    # Payload names contain their archive digest so an in-flight reader cannot
    # mix generations. Replace the completion marker only after all uploads.
    for file in [*(output/part['name'] for part in record['parts']), *diagnostic_assets, manifest]:
        if file.name in assets:
            if assets[file.name].get('digest') != 'sha256:'+sha(file):
                run('gh','release','upload',tag,'--repo',repository,file,'--clobber')
        else:
            run('gh','release','upload',tag,'--repo',repository,file)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare','build','publish'))
    args = parser.parse_args()
    data, framework = request()
    try:
        globals()[args.action](data, framework)
    except subprocess.CalledProcessError:
        # Existing recipes retain detailed compiler logs in their work trees.
        # Surface them when a hosted build fails so diagnosis needs no SSH.
        candidates=[p for p in (framework/'build').rglob('*.log') if p.is_file()]
        for path in sorted(candidates,key=lambda p:p.stat().st_mtime,reverse=True)[:8]:
            print('::group::'+str(path),flush=True)
            print('\n'.join(path.read_text(errors='replace').splitlines()[-60:]),flush=True)
            print('::endgroup::',flush=True)
        raise
