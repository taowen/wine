"""Wine-owned component resolution, hardware selection and immutable installation."""
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import shared
from windows import storage, runtime_storage, instance_prefix, host_path, _host_request

def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def validate(root):
    root = root.resolve()
    metadata = json.loads((root/'bionic-runtime.json').read_text())
    if metadata.get('protocol') != 1 or metadata.get('libc') != 'bionic':
        raise ValueError('Only Bionic Wine bundles are supported')
    files = metadata['files']
    for name, expected in files.items():
        if not isinstance(name, str) or not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected):
            raise ValueError('Invalid Bionic file checksum')
        path = root/name
        if not path.resolve().is_relative_to(root):
            raise ValueError('Runtime file escapes the bundle')
        if digest(path) != expected:
            raise ValueError(f'Bionic bundle checksum mismatch: {name}')
    return metadata


def runtime_url(url, filename):
    return bool(re.fullmatch(r'https://github\.com/taowen/(?:wine|FEX|dxvk|arlinux-steamworks|arlinux-windows-integration)'
                            r'/releases/download/[^/]+/'+re.escape(filename), url))


def install_component(name, source, expected):
    """Install only one checksum-addressed producer into the immutable store."""
    if not re.fullmatch('[0-9a-f]{64}', expected) or digest(source) != expected:
        raise ValueError('Runtime producer SHA-256 mismatch')
    descriptor = json.loads(source.read_text())
    identity = descriptor['sha256'] if descriptor.get('name') == name else expected
    if not re.fullmatch('[0-9a-f]{64}', identity):
        raise ValueError('Invalid runtime archive identity')
    home = runtime_storage()/'components'/name
    home.mkdir(parents=True, exist_ok=True)
    destination = home/identity
    with shared.locked(home/'install.lock'):
        if destination.is_dir():
            return destination
        if descriptor.get('kind') == 'arlinux-bionic-components' and descriptor.get('format') == 1:
            archives = descriptor['components']
            metadata = descriptor['runtime']
        elif descriptor.get('name') == name:
            archives = [descriptor]
            metadata = None
        else:
            raise ValueError('Expected one runtime producer: '+name)
        with tempfile.TemporaryDirectory(prefix='install-', dir=home) as temporary:
            stage = Path(temporary)/'runtime'
            stage.mkdir()
            seen = set()
            for item in archives:
                checksum = item['sha256']
                asset = item.get('asset', item['file'])
                if (not re.fullmatch('[0-9a-f]{64}', checksum)
                        or not runtime_url(item['url'], asset)):
                    raise ValueError('Invalid runtime component archive')
                print('Preparing runtime component: '+name, flush=True)
                archive = shared.download(item['url'], checksum, '.tar.gz', progress=False)
                if item.get('bytes') is not None and archive.stat().st_size != item['bytes']:
                    raise ValueError('Runtime component size mismatch: '+name)
                with tarfile.open(archive, 'r:*') as bundle:
                    for member in bundle:
                        path = member.name
                        if path == 'bionic-runtime.json':
                            if metadata is not None or not member.isfile() or member.size > 4*1024*1024:
                                raise ValueError('Invalid embedded runtime metadata')
                            metadata = json.load(bundle.extractfile(member))
                            continue
                        if (path in seen or '\\' in path or ':' in path
                                or any(part in ('', '.', '..') for part in path.split('/'))
                                or member.isdir()):
                            raise ValueError('Unsafe or duplicate runtime path: '+path)
                        seen.add(path)
                        bundle.extract(member, stage, filter='data')
            if not metadata or metadata.get('protocol') != 1 or metadata.get('libc') != 'bionic':
                raise ValueError('Expected Bionic runtime metadata')
            # Legacy delivery archives may carry a product-wide pairing token.
            # Component releases have independent lifetimes; it is not a dependency.
            metadata.pop('compatibility', None)
            (stage/'bionic-runtime.json').write_text(json.dumps(metadata)+'\n')
            validate(stage)
            stage.rename(destination)
    print('Installed runtime component: '+name+' '+expected[:12], flush=True)
    return destination


def ensure_component(row):
    name = row[0]
    if not re.fullmatch('[a-z0-9_-]+', name):
        raise ValueError('Invalid runtime producer name')
    overrides = os.environ.get('ARLINUX_WINE_COMPONENTS')
    override = Path(overrides)/(name+'.json') if overrides else None
    if override is not None and override.is_file():
        expected = json.loads(override.read_text())['sha256']
        return install_component(name, shared.directory('downloads')/(expected+'.json'), expected)
    state = storage()/'components'/(name+'.json')
    try:
        selected = json.loads(state.read_text())
        age = time.time()-selected['checkedAt']
        root = runtime_storage()/'components'/name/selected['cache']
        if 0 <= age < 3600 and root.is_dir() and selected['endpoint'] == row:
            return root
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        pass
    item = release_asset(row)
    encoded = (json.dumps(item, sort_keys=True)+'\n').encode()
    expected = hashlib.sha256(encoded).hexdigest()
    source = shared.directory('downloads')/(expected+'.json')
    with shared.locked(source.with_suffix('.lock')):
        source.write_bytes(encoded)
    root = install_component(name, source, expected)
    state.parent.mkdir(parents=True, exist_ok=True)
    with shared.locked(state.with_suffix('.lock')):
        temporary = state.with_suffix('.tmp')
        temporary.write_text(json.dumps(dict(sha256=expected, cache=root.name, checkedAt=time.time(), endpoint=row))+'\n')
        temporary.replace(state)
    return root


def runtime(identity=None):
    if identity is None:
        identity = (storage()/'runtime-id').read_text().strip()
    if not isinstance(identity, str) or not re.fullmatch('[0-9a-f]{64}', identity):
        raise ValueError('Invalid runtime identity')
    root = runtime_storage()/identity
    metadata = json.loads((root/'bionic-runtime.json').read_text())
    if metadata.get('protocol') != 1 or metadata.get('libc') != 'bionic':
        raise ValueError('Only Bionic Wine is supported')
    return root


def ensure_runtime():
    """Resolve producers independently and reference their immutable files."""
    from concurrent.futures import ThreadPoolExecutor
    rows = [line.split('\t') for line in (Path(__file__).parent/'components.tsv').read_text().splitlines()
            if line.strip() and not line.startswith('#')]
    if not rows or any(len(row) != 4 for row in rows) or len({r[0] for r in rows}) != len(rows):
        raise ValueError('Invalid runtime release endpoints')
    endpoints = {row[0]: row for row in rows}
    native = ensure_component(endpoints.pop('wine-bionic'))
    probe = Path(__file__).parents[3]/'bin/wine-vulkan-info'
    result = _host_request(dict(action='runtime-tool', runtime=str(host_path(native)),
        tool=str(host_path(probe)), args=[str(host_path(native/'files/lib/wine/aarch64-unix/libvulkan.so.1'))]), timeout=40)
    supported = json.loads(result['stdout'])
    profile = 'dxvk' if supported['modern'] else 'dxvk-legacy' if supported['legacy'] else None
    if profile is None:
        raise RuntimeError('No DXVK profile supports this device hardware')
    print('Selected hardware graphics component: '+profile, flush=True)
    endpoints.pop('dxvk-legacy' if profile == 'dxvk' else 'dxvk')
    with ThreadPoolExecutor(max_workers=4) as pool:
        roots = [native, *pool.map(ensure_component, endpoints.values())]
    roots.append(Path(__file__).parents[4])
    metadata = {}
    for root in roots:
        for key, value in json.loads((root/'bionic-runtime.json').read_text()).items():
            if key == 'files':
                target = metadata.setdefault(key, {})
                for field, content in value.items():
                    if field in target:
                        raise ValueError('Conflicting runtime ownership: '+field)
                    target[field] = content
            elif key in ('sources', 'compatibility', 'originalSources'):
                # Source provenance stays with each producer. Independent PE
                # architectures can legitimately originate from different commits.
                continue
            elif key in metadata and metadata[key] != value:
                raise ValueError('Conflicting runtime metadata: '+key)
            else:
                metadata[key] = value
    encoded = (json.dumps(metadata, sort_keys=True)+'\n').encode()
    identity = hashlib.sha256(Path(__file__).read_bytes()+b'\0'+encoded).hexdigest()
    home = runtime_storage()
    destination = home/identity
    with shared.locked(home/'install.lock'):
        if not destination.is_dir():
            with tempfile.TemporaryDirectory(prefix='view-', dir=home) as temporary:
                stage = Path(temporary)/'runtime'
                stage.mkdir()
                for root in roots:
                    for source in root.rglob('*'):
                        relative = source.relative_to(root)
                        if relative.as_posix() == 'bionic-runtime.json' or source.is_dir() and not source.is_symlink():
                            continue
                        target = stage/relative
                        target.parent.mkdir(parents=True, exist_ok=True)
                        if source.is_symlink():
                            target.symlink_to(os.readlink(source))
                        elif root.parent.name == 'wine-bionic':
                            # Wine derives its shared-data directory from the
                            # real paths of wineserver and ntdll.so. Keep this
                            # producer's files in the launch view; other, much
                            # larger producers remain linked to their cache.
                            shutil.copy2(source, target)
                        else:
                            # Android denies app_data_file hardlinks. Keep the
                            # native host's paths usable without tawcroot's
                            # private hardlink emulation or copying runtime bytes.
                            target.symlink_to(os.path.relpath(source, destination/relative.parent))
                (stage/'bionic-runtime.json').write_bytes(encoded)
                stage.rename(destination)
    pointer = storage()/'runtime-id'
    pointer.parent.mkdir(parents=True, exist_ok=True)
    with shared.locked(pointer.with_suffix('.lock')):
        temporary = pointer.with_suffix('.tmp')
        temporary.write_text(identity+'\n')
        temporary.replace(pointer)
    return runtime(identity)


def release_asset(row):
    name, repository, tag, filename = row
    if (not re.fullmatch('[a-z0-9_-]+', name)
            or not re.fullmatch('taowen/(wine|FEX|dxvk|arlinux-steamworks|arlinux-windows-integration)', repository)
            or tag != 'arlinux-'+name+'-latest' or filename != 'runtime.tar.gz'):
        raise ValueError('Invalid runtime release endpoint')
    endpoint = f'https://api.github.com/repos/{repository}/releases/tags/{tag}'
    try:
        release = json.loads(shared.github_metadata(endpoint))
    except (OSError, subprocess.SubprocessError, ValueError):
        # Preserve GitHub's public digest fallback when the unauthenticated API
        # is rate limited. Download size is then recorded after hash validation.
        class Assets(HTMLParser):
            def __init__(self):
                super().__init__()
                self.checksums = []
                self.link = False

            def handle_starttag(self, tag_name, attrs):
                attrs = dict(attrs)
                if tag_name == 'a' and attrs.get('href') == f'/{repository}/releases/download/{tag}/{filename}':
                    self.link = True
                if tag_name == 'clipboard-copy' and attrs.get('aria-label') == 'Copy to clipboard digest for '+filename:
                    self.checksums.append(attrs.get('value', ''))

        page = shared.github_metadata(f'https://github.com/{repository}/releases/expanded_assets/{tag}')
        assets = Assets()
        assets.feed(page.decode('utf-8'))
        if not assets.link or len(assets.checksums) != 1 or not re.fullmatch('sha256:[0-9a-f]{64}', assets.checksums[0]):
            raise ValueError('Runtime release has no verified digest: '+name)
        checksum = assets.checksums[0][7:]
        return dict(name=name, file=name+'-'+checksum+'.tar.gz', asset=filename,
                    sha256=checksum, bytes=None,
                    url=f'https://github.com/{repository}/releases/download/{tag}/{filename}')
    if release.get('draft') or release.get('tag_name') != tag:
        raise ValueError('Invalid runtime release')
    asset = next((a for a in release.get('assets', []) if a.get('name') == filename), None)
    if (not asset or asset.get('state') != 'uploaded'
            or not isinstance(asset.get('digest'), str)
            or not re.fullmatch('sha256:[0-9a-f]{64}', asset['digest'])
            or type(asset.get('size')) is not int or asset['size'] <= 0
            or type(asset.get('id')) is not int or asset['id'] <= 0):
        raise ValueError('Runtime release has no verified asset: '+name)
    checksum = asset['digest'][7:]
    return dict(name=name, file=name+'-'+checksum+'.tar.gz', asset=filename,
                sha256=checksum, bytes=asset['size'],
                url=f'https://github.com/{repository}/releases/download/{tag}/{filename}')


