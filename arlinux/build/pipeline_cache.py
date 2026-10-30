#!/usr/bin/env python3
"""Content-addressed cache primitives shared by independent build products."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def inventory(path, _digests=None):
    # Toolchains expose many compiler aliases to the same large binary. Hash
    # each inode once per scan; never retain memoized hashes between scans.
    if _digests is None:
        _digests = {}
    path = Path(path)
    if path.is_symlink():
        target = path.resolve(strict=False)
        return {'link': os.readlink(path), 'target': inventory(target, _digests) if target.exists() else None}
    if path.is_file():
        stat = path.stat()
        identity = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        if identity not in _digests:
            _digests[identity] = digest(path)
        return {'sha256': _digests[identity], 'mode': stat.st_mode & 0o777}
    if not path.is_dir():
        raise FileNotFoundError(path)
    return {p.relative_to(path).as_posix(): inventory(p, _digests) for p in sorted(path.rglob('*'))
            if p.is_file() or p.is_symlink()}


def source_state(repo):
    """Include staged/unstaged/new sources and recursively inspect submodules."""
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), *args])
    # Prepared Wine sources may live beneath a checkout's ignored build/ tree;
    # git ls-files there would silently return an empty dependency set.
    top = subprocess.run(['git', '-C', str(repo), 'rev-parse', '--show-toplevel'],
                         capture_output=True, text=True)
    if top.returncode or Path(top.stdout.strip()).resolve() != repo.resolve():
        return inventory(repo)
    tracked = git('ls-files', '--stage', '-z').split(b'\0')
    state = {}
    for record in tracked:
        if not record:
            continue
        header, raw = record.split(b'\t', 1)
        name = os.fsdecode(raw)
        path = repo / name
        if header.startswith(b'160000 '):
            state[name] = source_state(path)
        else:
            # A deletion has the same content identity before and after commit.
            if path.exists() or path.is_symlink():
                state[name] = inventory(path)
    for raw in git('ls-files', '--others', '--exclude-standard', '-z').split(b'\0'):
        if raw:
            name = os.fsdecode(raw)
            state[name] = inventory(repo / name)
    return state


def key(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def build_references(output):
    # The Wine compiler tools and native ABI headers stay in the builder's work
    # tree. They are dependencies too, even though they are never distributed.
    references = {}
    for state in Path(output).rglob('build-state.json'):
        for name, path in json.loads(state.read_text()).items():
            references[str(path)] = inventory(Path(path))
    return references


def first_difference(expected, actual, path='output'):
    if isinstance(expected, dict) and isinstance(actual, dict):
        for name in sorted(expected.keys() | actual.keys()):
            location = path + '/' + name
            if name not in expected:
                return location + ': unexpected entry'
            if name not in actual:
                return location + ': missing entry'
            if expected[name] != actual[name]:
                return first_difference(expected[name], actual[name], location)
    return path + ': recorded=' + repr(expected) + ', current=' + repr(actual)


def verified_output(destination, record):
    generation = record['generation']
    if (not isinstance(generation, str) or not generation.startswith('generation-')
            or Path(generation).name != generation):
        raise ValueError('Unsafe cache generation')
    output = Path(destination) / generation / 'output'
    actual = inventory(output)
    if record['outputs'] != actual:
        raise ValueError(first_difference(record['outputs'], actual))
    actual = build_references(output)
    if record['references'] != actual:
        raise ValueError(first_difference(record['references'], actual, 'references'))
    return output


class Cache:
    def __init__(self, root, rebuild=False):
        self.root, self.rebuild = Path(root), rebuild
        self.records = {}

    def stage(self, name, inputs, build):
        identity = key(inputs)
        destination = self.root / name / identity
        receipt = destination / 'receipt.json'
        if not self.rebuild and receipt.is_file():
            try:
                record = json.loads(receipt.read_text())
                if record['inputs'] == identity:
                    output = verified_output(destination, record)
                    print('OK cached ' + name, flush=True)
                    self.records[name] = record
                    return output
            except (OSError, ValueError, KeyError) as failure:
                print('Invalid cached ' + name + ': ' + str(failure), flush=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Keep successful generations at their original absolute paths. Configure,
        # pkg-config and Wine tools embed build paths, so moving a built tree breaks it.
        generation = 'generation-' + uuid.uuid4().hex
        pending = destination / generation
        pending.mkdir(parents=True)
        try:
            build(pending / 'output')
            record = {'inputs': identity, 'generation': generation,
                      'outputs': inventory(pending / 'output'),
                      'references': build_references(pending / 'output')}
            draft = destination / 'receipt.json.part'
            draft.write_text(json.dumps(record, sort_keys=True) + '\n')
            draft.replace(receipt)
        except BaseException:
            shutil.rmtree(pending)
            raise
        self.records[name] = record
        return pending / 'output'


def run(*args, cwd, env):
    print('> ' + ' '.join(map(str, args)), flush=True)
    subprocess.run(list(map(str, args)), cwd=cwd, env=env, check=True)
