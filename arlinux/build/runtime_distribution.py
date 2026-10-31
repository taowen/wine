"""Independent Bionic runtime manifests and immutable component publication."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile

from component_recipes import load_plan


def release_base(stage, repository=None):
    spec = load_plan()['stages'][stage]
    return f'https://github.com/{repository or spec["repository"]}/releases/download/{spec["release"]}/'
LEAF_INDEX = 'arlinux-wine-runtime.json'
RUNTIME_STAGES = ('wine-bionic', 'steamworks', 'windows-accessibility', 'windows-tray',
                  'wine-pe-aarch64', 'wine-pe-x86_64', 'wine-pe-i386',
                  'fex', 'dxvk', 'windows-fonts', 'wine-addons')


def publish_runtime_component(data, framework, record, built, complete=True):
    """Publish a producer's own installable files and source manifest."""
    repository = os.environ['GITHUB_REPOSITORY']
    base = release_base(data['stage'], repository)
    (framework/'build/artifacts').mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='runtime-publication-', dir=framework/'build/artifacts') as temporary:
        work = Path(temporary)
        output = work/'publication'
        output.mkdir()
        stage = data['stage']
        if stage == 'wine-bionic':
            runtime = built/'runtime'
            for source in built.glob('*.tar.gz'):
                shutil.copyfile(source, output/source.name)
        else:
            runtime = work/'runtime'
            runtime.mkdir()
            metadata = dict(protocol=1, libc='bionic', originalSources={})
            if stage.startswith('wine-pe-'):
                files = built/'files'
                for source in sorted(files.rglob('*')):
                    relative = source.relative_to(files)
                    name = relative.as_posix()
                    if (source.is_dir() or source.suffix == '.a'
                            or source.name in ('winebus.sys', 'explorer.exe')
                            or not (name.startswith('lib/wine/') and '-windows/' in name
                                    or stage == 'wine-pe-aarch64' and name.startswith('share/wine/'))):
                        continue
                    target = runtime/'files'/relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target, follow_symlinks=False)
                if stage == 'wine-pe-aarch64':
                    inf = runtime/'files/share/wine/wine.inf'
                    text = inf.read_text()
                    for arch, old, new in (('amd64', 'xtajit64.dll', 'libarm64ecfex.dll'),
                                           ('x86', 'xtajit.dll', 'libwow64fex.dll')):
                        needle = f'HKLM,Software\\Microsoft\\Wow64\\{arch},,2,"{old}"'
                        if text.count(needle) != 1:
                            raise ValueError('Wine CPU-module registry default changed: '+old)
                        text = text.replace(needle, needle.replace(old, new))
                    inf.write_text(text)
                    source = output/'wine-pe-source.tar.gz'
                    shutil.copyfile(built/'wine-source.tar.gz', source)
                    metadata['originalSources']['wine-pe'] = dict(asset=source.name, sha256=sha(source))
            elif stage == 'fex':
                target = runtime/'files/lib/wine/aarch64-windows'
                target.mkdir(parents=True)
                for binary in built.glob('lib*fex.dll'):
                    shutil.copy2(binary, target/binary.name)
                config = runtime/'files/share/fex-emu/Config.json'
                config.parent.mkdir(parents=True)
                config.write_text(json.dumps({'Config': {'TSOEnabled': '1', 'X87ReducedPrecision': '1',
                    'VectorTSOEnabled': '0', 'MemcpySetTSOEnabled': '0', 'HalfBarrierTSOEnabled': '1',
                    'MaxInst': '500', 'Multiblock': '1'}}, indent=2)+'\n')
                source = output/'fex-source.tar.gz'
                shutil.copyfile(built/source.name, source)
                metadata['fexCommit'] = (built/'source-commit').read_text().strip()
                metadata['originalSources']['fex'] = dict(asset=source.name, sha256=sha(source))
            elif stage == 'dxvk':
                target = runtime/'files/lib/wine/dxvk'
                target.mkdir(parents=True)
                shutil.copyfile(built/'profiles.json', target/'profiles.json')
                helper = runtime/'files/lib/arlinux/dxvk-probe.exe'
                helper.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(built/'dxvk-probe.exe', helper)
                for profile in ('modern', 'legacy'):
                    destination = target if profile == 'modern' else target/profile
                    for folder in (built/profile).glob('*-windows'):
                        shutil.copytree(folder, destination/folder.name)
                    name = 'dxvk' if profile == 'modern' else 'dxvk-legacy'
                    source = output/(name+'-source.tar.gz')
                    shutil.copyfile(built/profile/'source.tar.gz', source)
                    metadata['dxvkCommit' if profile == 'modern' else 'dxvkLegacyCommit'] = (built/profile/'source-commit').read_text().strip()
                    metadata['originalSources'][name] = dict(asset=source.name, sha256=sha(source))
            elif stage == 'windows-fonts':
                import sys
                sys.path.insert(0, str(framework/'arlinux'))
                from wine_fonts import install
                install(runtime/'files/share/arlinux/fonts', source=built)
            elif stage == 'steamworks':
                target = runtime/'files/lib/arlinux'
                target.mkdir(parents=True)
                target = target/'steamworks'
                shutil.copytree(built, target, ignore=shutil.ignore_patterns('*-source.tar.gz'))
                source = output/'steamworks-source.tar.gz'
                shutil.copyfile(built/source.name, source)
                metadata['support'] = dict(protocol=1, directory='files/lib/arlinux')
                metadata['originalSources'][stage] = dict(asset=source.name, sha256=sha(source))
            else:
                target = runtime/'files/lib/arlinux'
                target.mkdir(parents=True)
                for binary in built.iterdir():
                    if binary.is_file():
                        shutil.copyfile(binary, target/binary.name)
                source = output/(stage+'-source.tar.gz')
                module = framework/'arlinux/components/windows-integration'
                with source.open('wb') as raw, gzip.GzipFile(filename='', fileobj=raw, mode='wb', mtime=0) as compressed:
                    process = subprocess.Popen(['git', '-C', str(module), 'archive', '--format=tar', 'HEAD'], stdout=subprocess.PIPE)
                    try:
                        shutil.copyfileobj(process.stdout, compressed)
                    finally:
                        process.stdout.close()
                    if process.wait():
                        raise RuntimeError('Cannot archive the pinned Windows integration sources')
                metadata['support'] = dict(protocol=1, directory='files/lib/arlinux')
                metadata['originalSources'][stage] = dict(asset=source.name, sha256=sha(source))
            if stage.startswith('wine-pe-') or stage in ('fex', 'dxvk'):
                from importlib.util import spec_from_file_location, module_from_spec
                spec = spec_from_file_location('wine_pe_debug', framework/'arlinux/wine_pe_debug.py')
                optimizer = module_from_spec(spec)
                spec.loader.exec_module(optimizer)
                optimizer.optimize(runtime, framework.parent/'toolchains/mingw/bin/llvm-strip', remove_x64=False)
            metadata['files'] = {path.relative_to(runtime).as_posix(): sha(path)
                                  for path in sorted(runtime.rglob('*')) if path.is_file()}
            (runtime/'bionic-runtime.json').write_text(json.dumps(metadata, sort_keys=True)+'\n')
        name = 'arlinux-'+stage+'.json'
        assemble(runtime, output, component_base=base,
                 group_prefix='' if stage == 'wine-bionic' else stage+'-', index_name=name)
        publish_distribution(data, record, output, repository=repository, index_name=name, complete=complete)


def publish_distribution(data, record, final, repository=None, index_name=LEAF_INDEX, complete=True):
    """The completion marker follows every immutable object upload."""
    index = json.loads((final/index_name).read_text())
    repository = repository or os.environ['GITHUB_REPOSITORY']
    tag = load_plan()['stages'][data['stage']]['release']
    def run(*args):
        subprocess.run(list(map(str, args)), check=True)
    def release_assets(name):
        result = subprocess.run(['gh', 'api', f'repos/{repository}/releases/tags/{name}'],
                                capture_output=True, text=True)
        if result.returncode:
            run('gh', 'release', 'create', name, '--repo', repository, '--prerelease',
                '--title', name, '--notes', 'Independent ARLinux component. Compiler publication does not promote runtime.tar.gz.')
            return {}
        return {asset['name']: asset for asset in json.loads(result.stdout)['assets']}
    assets = release_assets(tag)
    def upload(path, name, release, existing, replace=False):
        checksum, size = sha(path), path.stat().st_size
        if name in existing:
            prior = existing[name]
            if prior.get('digest') == 'sha256:'+checksum and prior['size'] == size:
                return checksum, size
            if not replace:
                raise ValueError('Immutable product asset conflicts: '+name)
        if name not in existing or replace:
            with tempfile.TemporaryDirectory(prefix='product-asset-') as directory:
                staged = Path(directory)/name
                shutil.copyfile(path, staged)
                try:
                    run('gh', 'release', 'upload', release, '--repo', repository, staged,
                        *(['--clobber'] if replace else []))
                except subprocess.CalledProcessError:
                    # Another product may publish the same content-addressed
                    # object after our release inventory was fetched. Never
                    # clobber it; accept only its exact verified bytes.
                    concurrent = release_assets(release).get(name)
                    if (not concurrent or concurrent.get('digest') != 'sha256:'+checksum
                            or concurrent['size'] != size):
                        raise
                    print('Verified concurrent immutable upload: '+name, flush=True)
            existing[name] = dict(digest='sha256:'+checksum, size=size)
        return checksum, size
    component_files = {item['file'] for item in index.get('components', [])}
    record['artifacts'], record['parts'] = [], []
    for path in sorted(final.iterdir()):
        if not path.is_file():
            continue
        role = 'runtime' if path.name in component_files else 'manifest' if path.name == index_name else 'source'
        if role == 'source':
            name = 'source-'+sha(path)+'.tar.gz'
        elif role == 'runtime':
            name = path.name
        else:
            name = path.name
        checksum, size = upload(path, name, tag, assets, replace=role == 'manifest')
        record['artifacts'].append(dict(name=path.name, sha256=checksum, bytes=size, role=role,
            url=f'https://github.com/{repository}/releases/download/{tag}/{name}'))
    if data['stage'] in RUNTIME_STAGES:
        item = next(item for item in record['artifacts'] if item['role'] == 'manifest')
        record['runtimeManifest'] = dict(name=data['stage'], file=item['name'],
            sha256=item['sha256'], bytes=item['bytes'], url=item['url'])
    if complete:
        manifest = final.parent/'wine-component.json'
        manifest.write_text(json.dumps(record, sort_keys=True, indent=2)+'\n')
        upload(manifest, 'component.json', tag, assets, replace=True)


def sha(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def component_name(name):
    """Partition actual output paths, not a list of required DLLs/features."""
    path = Path(name)
    if 'fex' in path.name.lower():
        return 'fex'
    for prefix, group in (
        ('files/etc/', 'configuration'),
        ('files/share/fex-emu/', 'configuration'),
        ('files/lib/arlinux/', 'integration'),
        ('files/share/arlinux/fonts/', 'fonts'),
        ('files/share/wine/fonts/', 'fonts'),
        ('files/lib/wine/dxvk/', 'dxvk'),
        ('files/lib/wine/aarch64-windows/', 'wine-pe-arm64'),
        ('files/lib/wine/x86_64-windows/', 'wine-pe-x64'),
        ('files/lib/wine/i386-windows/', 'wine-pe-x86'),
        ('files/lib/wine/aarch64-unix/', 'wine-native'),
    ):
        if name.startswith(prefix):
            return group
    return 'wine-data'


def assemble(root, output, component_base=None, group_prefix='', index_name=LEAF_INDEX):
    if component_base is None:
        component_base = release_base('wine-bionic')
    metadata = json.loads((root/'bionic-runtime.json').read_text())
    groups = {}
    for path in sorted(root.rglob('*')):
        if path.is_dir() and not path.is_symlink():
            continue
        name = path.relative_to(root).as_posix()
        if name == 'bionic-runtime.json':
            continue
        groups.setdefault(group_prefix+component_name(name), []).append(path)
    def archive_group(item):
        group, paths = item
        pending = output/(group+'.tar.gz.part')
        with pending.open('wb') as target, gzip.GzipFile(filename='', mode='wb', fileobj=target,
                                                        compresslevel=1, mtime=0) as zipped:
            with tarfile.open(fileobj=zipped, mode='w|') as archive:
                for path in paths:
                    entry = archive.gettarinfo(str(path), path.relative_to(root).as_posix())
                    entry.uid = entry.gid = entry.mtime = 0
                    entry.uname = entry.gname = ''
                    if entry.isfile():
                        with path.open('rb') as source:
                            archive.addfile(entry, source)
                    else:
                        archive.addfile(entry)
        checksum = sha(pending)
        name = group+'-'+checksum+'.tar.gz'
        archive = output/name
        pending.rename(archive)
        return dict(name=group, file=name, sha256=checksum,
                    bytes=archive.stat().st_size, url=component_base+name)
    # gzip releases the GIL. Independent archives can use the runner's cores
    # without changing group ordering, tar metadata or compressed output bytes.
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(4, os.cpu_count() or 1)) as pool:
        components = list(pool.map(archive_group, sorted(groups.items())))
    sources = []
    for path in sorted(output.glob('*.tar.gz')):
        if path.name in {item['file'] for item in components}:
            continue
        checksum = sha(path)
        sources.append(dict(file=path.name, sha256=checksum, bytes=path.stat().st_size,
                            url=component_base+'source-'+checksum+'.tar.gz'))
    index = dict(format=1, kind='arlinux-bionic-components', runtime=metadata,
                 componentBase=component_base, components=components, sources=sources)
    (output/index_name).write_text(json.dumps(index, sort_keys=True, indent=2)+'\n')


def runtime_url(url, filename):
    return bool(re.fullmatch(r'https://github\.com/taowen/(?:wine|FEX|dxvk|arlinux-steamworks|arlinux-windows-integration)'
                            r'/releases/download/[^/]+/'+re.escape(filename), url))
