"""Wine-owned component selection and immutable launch views.

Protocol 1 receives a verified component store, an instance state directory and
run_native_tool(runtime, tool, arguments) -> stdout. No launcher internals are
imported. Component release endpoints and initialization policy belong here.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import shared


def ensure_runtime(components, state, run_native_tool):
    rows = [line.split('\t') for line in (Path(__file__).parent/'components.tsv').read_text().splitlines()
            if line.strip() and not line.startswith('#')]
    if not rows or any(len(row) != 4 for row in rows) or len({r[0] for r in rows}) != len(rows):
        raise ValueError('Invalid runtime release endpoints')
    endpoints = {row[0]: row for row in rows}
    native = components.get(endpoints.pop('wine-bionic'))
    probe = Path(__file__).parents[3]/'bin/wine-vulkan-info'
    supported = json.loads(run_native_tool(native, probe,
        [native/'files/lib/wine/aarch64-unix/libvulkan.so.1']))
    profile = 'dxvk' if supported['modern'] else 'dxvk-legacy' if supported['legacy'] else None
    if profile is None:
        raise RuntimeError('No DXVK profile supports this device hardware')
    print('Selected hardware graphics component: '+profile, flush=True)
    endpoints.pop('dxvk-legacy' if profile == 'dxvk' else 'dxvk')
    with ThreadPoolExecutor(max_workers=4) as pool:
        roots = [native, *pool.map(components.get, endpoints.values())]
    installer = Path(__file__).parents[4]
    setup = json.loads((installer/'bionic-runtime.json').read_text())
    initialization = setup['initialization']
    setup_files = {path: checksum for path, checksum in setup['files'].items()
                   if path == initialization['executable'] or path.startswith(initialization['template']+'/')}
    setup['files'] = setup_files
    roots.append(installer)
    metadata = {}
    for root in roots:
        component = setup if root == installer else json.loads((root/'bionic-runtime.json').read_text())
        if component.get('protocol') != 1 or component.get('libc') != 'bionic':
            raise ValueError('Only Bionic Wine components are supported')
        for key, value in component.items():
            if key == 'files':
                target = metadata.setdefault(key, {})
                for field, content in value.items():
                    if field in target:
                        raise ValueError('Conflicting runtime ownership: '+field)
                    target[field] = content
            elif key in ('sources', 'compatibility', 'originalSources', 'wineSource', 'wineCommit', 'fexCommit', 'dxvkCommit', 'installerProtocol'):
                # Source provenance stays with each producer. Independent PE
                # architectures can legitimately originate from different commits.
                continue
            elif key in metadata and metadata[key] != value:
                raise ValueError('Conflicting runtime metadata: '+key)
            else:
                metadata[key] = value
    encoded = (json.dumps(metadata, sort_keys=True)+'\n').encode()
    identity = hashlib.sha256(encoded).hexdigest()
    home = components.root.parent
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
                        if root == installer and relative.as_posix() not in setup_files:
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
    pointer = state/'runtime-id'
    pointer.parent.mkdir(parents=True, exist_ok=True)
    with shared.locked(pointer.with_suffix('.lock')):
        temporary = pointer.with_suffix('.tmp')
        temporary.write_text(identity+'\n')
        temporary.replace(pointer)
    return destination
