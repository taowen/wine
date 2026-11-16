"""Bounded opt-in capture or validation of one Bionic Wine application.

Run with arlinux-py.py. Tools must come from build.ps1 wine-capture, extracted
under app-private storage. Captures may contain private application contents.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import re
import tarfile
import tempfile
import time


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--tools', type=Path)
    source.add_argument('--archive', type=Path)
    parser.add_argument('--archive-sha256')
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--seconds', type=int, default=30)
    parser.add_argument('--max-mib', type=int, default=512)
    parser.add_argument('--memory-tracking', choices=('unassisted', 'page_guard', 'assisted'), default='unassisted')
    parser.add_argument('--frames', default='')
    parser.add_argument('--convert', action='store_true')
    parser.add_argument('--validation', action='store_true', help='Use the Wayland-enabled Bionic validation product')
    parser.add_argument('executable', type=Path)
    parser.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 120 or not 1 <= args.max_mib <= 512:
        parser.error('Use 1..120 seconds and 1..512 MiB')
    if args.validation and (args.frames or args.convert):
        parser.error('Frame ranges and conversion apply only to capture')
    sys.path.insert(0, '/usr/lib/arlinux')
    import windows
    if args.archive:
        archive = args.archive.expanduser().resolve(strict=True)
        expected = args.archive_sha256 or ''
        if not re.fullmatch('[0-9a-f]{64}', expected) or digest(archive) != expected:
            raise ValueError('Use the verified capture archive SHA-256 printed by the build entry')
        cache = Path.home()/('.cache/arlinux/bionic-validation' if args.validation else '.cache/arlinux/bionic-capture')
        cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = cache/expected
        if not destination.exists():
            with tempfile.TemporaryDirectory(dir=cache) as stage:
                stage = Path(stage)
                with tarfile.open(archive) as bundle:
                    def members():
                        for member in bundle:
                            if (member.name.startswith('/') or '\\' in member.name
                                    or ':' in member.name or '..' in Path(member.name).parts):
                                raise ValueError('Unsafe capture archive path')
                            yield member
                    bundle.extractall(stage, members=members(), filter='data')
                (stage/('validation-tools' if args.validation else 'capture-tools')).rename(destination)
        tools = destination.resolve(strict=True)
    else:
        tools = args.tools.expanduser().resolve(strict=True)
    provenance = json.loads((tools/'provenance.json').read_text())
    if provenance['abi'] != 'aarch64-bionic':
        raise ValueError('Use the Bionic capture product, not the glibc tools')
    for name, expected in provenance['files'].items():
        path = (tools/name).resolve(strict=True)
        if not path.is_relative_to(tools) or digest(path) != expected:
            raise ValueError('Capture tool hash/path mismatch: '+name)
    executable = args.executable.expanduser().resolve(strict=True)
    report = args.output.expanduser()
    report.mkdir(parents=True, mode=0o700, exist_ok=False)
    report = report.resolve()
    runtime, prefix = windows.ensure_runtime(), windows.instance_prefix()
    windows.initialize(runtime, prefix)
    # Capture a generic executable without installing or changing unrelated
    # WebView2 dependencies. The application's installer owns those policies.
    settings = json.dumps(dict(
        tools=str(windows.host_path(tools)), output=str(windows.host_path(report)),
        memoryTracking=args.memory_tracking, frames=args.frames))
    metadata = dict(started=time.time(), runtime_sha256=runtime.name,
        executable=str(executable), executable_sha256=digest(executable),
        arguments=args.arguments, seconds=args.seconds, max_mib=args.max_mib,
        memory_tracking=args.memory_tracking, frames=args.frames, validation=args.validation, provenance=provenance,
        layer_processes={}, stop_reason=None)
    (report/'runtime.json').write_text((runtime/'bionic-runtime.json').read_text())
    setting = 'ARLINUX_VULKAN_VALIDATION' if args.validation else 'ARLINUX_VULKAN_CAPTURE'
    response = windows.request(runtime, prefix, executable, args.arguments,
                               diagnostics={setting: settings}, task=True)
    metadata['job'] = response['job']
    deadline = time.monotonic()+args.seconds
    library = 'libVkLayer_khronos_validation.so' if args.validation else 'libVkLayer_gfxreconstruct.so'
    print('Diagnosing one owned Windows job; output can contain private data.', flush=True)
    try:
        while True:
            for process in Path('/proc').iterdir():
                if not process.name.isdigit():
                    continue
                try:
                    # Match only this task; never retain process environments.
                    if process.stat().st_uid != os.getuid():
                        continue
                    entries = (process/'environ').read_bytes().split(b'\0')
                    tag = ('ARLINUX_RUN_ID='+response['job']).encode()
                    if tag not in entries:
                        continue
                    maps = (process/'maps').read_text()
                    if library in maps:
                        metadata['layer_processes'][process.name] = 'process-'+process.name+'-maps.txt'
                        (report/metadata['layer_processes'][process.name]).write_text(maps)
                except (OSError, PermissionError):
                    continue
            size = sum(p.stat().st_size for p in report.glob('*.gfx*') if p.is_file())
            if args.validation:
                source_log = windows.guest_path(response['log'])
                size = source_log.stat().st_size if source_log.is_file() else 0
            if windows.guest_path(response['exit']).is_file():
                metadata['stop_reason'] = 'application_exit'; break
            if size >= args.max_mib*1024*1024:
                metadata['stop_reason'] = 'size_limit'; break
            if time.monotonic() >= deadline:
                metadata['stop_reason'] = 'time_limit'; break
            time.sleep(.25)
    finally:
        helper = runtime/'files/lib/arlinux/windows-tray.exe'
        stopped = windows.run(runtime, prefix, helper, ['--stop', response['job']], capture=True, timeout=30)
        metadata['stop_exit_code'] = stopped.returncode
        source_log = windows.guest_path(response['log'])
        if source_log.is_file():
            shutil.copyfile(source_log, report/'wine.log')
        metadata['finished'] = time.time()
        metadata['files'] = {p.name:dict(sha256=digest(p),bytes=p.stat().st_size)
                             for p in report.iterdir() if p.is_file()}
        (report/'metadata.json').write_text(json.dumps(metadata,indent=2)+'\n')
    captures = sorted(report.glob('*.gfxr'))
    if (not args.validation and not captures) or not metadata['layer_processes'] or stopped.returncode:
        raise RuntimeError('Capture was not verified; inspect the retained report')
    if args.validation:
        log = (report/'wine.log').read_text(errors='replace')
        metadata['validation_errors'] = log.count('Validation Error:')
        (report/'metadata.json').write_text(json.dumps(metadata,indent=2)+'\n')
        print(json.dumps(dict(report=str(report), status='VALIDATION_FINISHED', errors=metadata['validation_errors'])))
        return
    if args.convert:
        converter = Path('/usr/lib/arlinux/gfxreconstruct/gfxrecon-convert')
        metadata['conversions'] = {}
        try:
            metadata['converter_sha256'] = digest(converter)
            conversion_env = os.environ.copy()
            conversion_env['LD_LIBRARY_PATH'] = ':'.join(filter(None, (
                str(converter.parent/'runtime'), conversion_env.get('LD_LIBRARY_PATH'))))
            for capture in captures:
                target = capture.with_suffix('.jsonl')
                with target.open('w') as output, target.with_suffix('.convert.log').open('w') as errors:
                    result = subprocess.run([str(converter), '--format', 'jsonl', '--output', 'stdout', str(capture)], stdout=output,
                                            stderr=errors, env=conversion_env, timeout=120)
                metadata['conversions'][capture.name] = result.returncode
                if result.returncode == 0 and target.stat().st_size == 0:
                    raise RuntimeError('Converter returned no JSON records: '+capture.name)
        except (OSError, subprocess.TimeoutExpired) as error:
            metadata['conversion_error'] = str(error)
            raise
        finally:
            metadata['files'] = {p.name:dict(sha256=digest(p),bytes=p.stat().st_size)
                                 for p in report.iterdir() if p.is_file() and p.name != 'metadata.json'}
            (report/'metadata.json').write_text(json.dumps(metadata,indent=2)+'\n')
        if any(metadata['conversions'].values()):
            raise RuntimeError('Capture conversion failed; inspect conversion logs')
    print(json.dumps(dict(report=str(report), status='CAPTURED_AND_CONVERTED' if args.convert else 'CAPTURED')))


if __name__ == '__main__':
    main()
