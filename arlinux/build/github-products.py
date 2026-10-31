#!/usr/bin/env python3
"""Dispatch independent repository workflows from Windows or Linux, without WSL."""
from component_recipes import load_plan
import argparse
from contextlib import contextmanager
import errno
import os
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import hashlib
import json
import re
from pathlib import Path
import subprocess
import sys
import tarfile
import time
from threading import Event
from artifact_download import download,sha256_file,reuse_product,download_capsule
from component_inputs import request_identity, source_inputs


def gh(*args, optional=False):
    attempts=4 if args[0] in ('api','run') else 1
    for attempt in range(attempts):
        result = subprocess.run(['gh', *map(str,args)], capture_output=True, text=True)
        if not result.returncode or not any(message in result.stderr.lower() for message in
                ('timeout','timed out','connection','eof','502','503','504','tls handshake')):
            break
        if attempt+1<attempts:
            time.sleep(2**attempt)
    if result.returncode:
        if optional and 'HTTP 404' in result.stderr:
            return None
        raise RuntimeError(result.stderr.strip())
    return result.stdout


def api(path, optional=False):
    value = gh('api', path, optional=optional)
    return json.loads(value) if value else None


def dispatch_release(repository, request, workflow, poll=15):
    """Run only the owning repository's publication job, not its compiler graph."""
    title = 'arlinux/'+request['stage']+'/'+request['identity']
    def runs():
        return json.loads(gh('run', 'list', '--repo', repository, '--workflow', workflow,
                             '--limit', '100', '--json', 'databaseId,displayTitle,status,conclusion,url'))
    previous = runs()
    active = next((r for r in previous if r['displayTitle'] == title and r['status'] != 'completed'), None)
    ignored = {r['databaseId'] for r in previous if r['status'] == 'completed'}
    if not active:
        gh('workflow', 'run', workflow, '--repo', repository,
           '-f', 'request='+json.dumps(request, sort_keys=True))
        print('START publication '+repository+'/'+request['stage'], flush=True)
    deadline, last = time.monotonic()+50*60, None
    while time.monotonic() < deadline:
        matches = [r for r in runs() if r['displayTitle'] == title and r['databaseId'] not in ignored]
        run = matches[0] if matches else None
        if run:
            state = run['status']+'/'+str(run['conclusion'])
            if state != last:
                print(request['action']+' '+request['stage']+': '+state+' '+run['url'], flush=True)
                last = state
            if run['status'] == 'completed':
                if run['conclusion'] != 'success':
                    raise RuntimeError('Publication failed: '+run['url'])
                return run['url']
        time.sleep(poll)
    raise TimeoutError('Publication workflow timed out: '+title)


@contextmanager
def request_lock(path, stopped):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as lock:
        if not path.stat().st_size:
            lock.write(b'0')
            lock.flush()
        deadline = time.monotonic() + 8*60*60
        while True:
            try:
                lock.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if stopped.wait(0.25):
                    raise RuntimeError('Local coordinator stopped while waiting for a component')
                if time.monotonic() > deadline:
                    raise TimeoutError('Timed out waiting for component request lock')
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == 'nt':
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)


class Coordinator:
    def __init__(self, plan, framework, android, state, poll=30):
        self.plan,self.framework,self.android,self.state,self.poll = plan,framework,android,state,poll
        state.mkdir(parents=True,exist_ok=True)
        self.stopped = Event()
        self.timings = {}
        self.local_manifests = set()

    def runs(self, repository):
        return json.loads(gh('run','list','--repo',repository,'--workflow',self.plan['workflow'],
            '--limit','100','--json','databaseId,displayTitle,status,conclusion,url'))

    def existing(self, request):
        repository = self.plan['stages'][request['stage']]['repository']
        tag = self.plan['stages'][request['stage']]['release']
        directory = self.state/request['identity']
        manifest = directory/'component.json'
        reference_file = directory/'reference.json'
        url = f'https://github.com/{repository}/releases/download/{tag}/component.json'
        def validate(record):
            for name in ('stage','identity','android','dependencies', 'sourceInputs'):
                if record.get(name) != request.get(name):
                    return False
            return True
        # Always resolve the fixed release; local metadata alone cannot prove
        # that its mutable endpoint still selects the same component.
        release = api(f'repos/{repository}/releases/tags/{tag}',optional=True)
        if not release:
            return None
        asset = next((a for a in release['assets'] if a['name']=='component.json'),None)
        if not asset:
            return None
        digest = asset.get('digest','')
        if not digest.startswith('sha256:'):
            raise ValueError('GitHub did not supply the component manifest SHA-256')
        download(asset['browser_download_url'],manifest,digest[7:])
        record = json.loads(manifest.read_text())
        if not validate(record):
            return None
        names={a['name'] for a in release['assets']}
        if any(part['name'] not in names for part in record['parts']):
            return None
        reference = dict(repository=repository,identity=request['identity'],
            manifestUrl=asset['browser_download_url'],manifestSha256=digest[7:])
        if reference['manifestUrl'] != url:
            raise ValueError('Unexpected immutable component manifest URL')
        pending = reference_file.with_suffix('.tmp')
        pending.write_text(json.dumps(reference, sort_keys=True)+'\n')
        pending.replace(reference_file)
        return reference

    def component(self, stage, dependencies):
        started = time.monotonic()
        framework = Path(__file__).resolve().parents[2]
        request = dict(format=2,stage=stage,framework=self.framework,android=self.android,
                       dependencies=dependencies, sourceInputs=source_inputs(framework, self.framework, stage, self.plan))
        if stage == 'wine-addons':
            from local_addons import toolchain_identity
            request['hostTools'] = toolchain_identity()
        request['identity']=request_identity(request)
        with request_lock(self.state/request['identity']/'dispatch.lock', self.stopped):
            result = self._component(request)
        self.timings[stage] = dict(seconds=round(time.monotonic()-started, 3),
                                  repository=result['repository'], identity=result['identity'],
                                  manifestSource='local' if stage in self.local_manifests else 'github')
        return result

    def _component(self, request):
        stage = request['stage']
        repository=self.plan['stages'][stage]['repository']
        result=self.existing(request)
        if result:
            print('OK published '+repository+'/'+stage,flush=True)
            return result
        if self.plan['stages'][stage].get('execution') == 'local':
            from local_addons import build
            print('START local '+stage, flush=True)
            build(request, self.state/request['identity'], repository, self.state/'downloads/objects')
            result = self.existing(request)
            if result is None:
                raise RuntimeError('Local assembly did not publish its completion record')
            return result
        payload=json.dumps(request,sort_keys=True)
        # A standalone workflow_dispatch creates a run in the component repo.
        # Do not replace this with workflow_call: caches must belong to that repo.
        title='arlinux/'+stage+'/'+request['identity']
        previous_runs=self.runs(repository)
        run=next((r for r in previous_runs if r['displayTitle']==title and r['status']!='completed'),None)
        ignored={r['databaseId'] for r in previous_runs if r['status']=='completed'}
        if not run:
            # Capsules contain verified build receipts, including references.
            # They can seed a new run after an unrelated framework commit; the
            # producer's input hashes, not this coordinator, decide cache hits.
            # Pure assemblers have no compiler work to seed. Importing an old
            # runtime plus its dependency closure just to rebuild a new runtime
            # duplicates large downloads; current declared inputs are sufficient.
            if stage not in ('yibu','debian','arch','omarchy','plasma','lxqt','windows-fonts',
                             'wine-pe','wine-bionic'):
                candidate=api(f"repos/{repository}/releases/tags/{self.plan['stages'][stage]['release']}", optional=True)
                if candidate:
                    asset=next((a for a in candidate['assets'] if a['name']=='component.json'), None)
                    digest=asset.get('digest','') if asset else ''
                    if digest.startswith('sha256:'):
                        # Read the actual identity from the verified fixed-release metadata.
                        file=self.state/request['identity']/'seed.json'
                        download(asset['browser_download_url'],file,digest[7:])
                        seed=json.loads(file.read_text())
                        if seed['stage']!=stage:
                            raise ValueError('Cache seed belongs to a different component')
                        # Runtime-only producers publish delivery archives, not
                        # compiler capsules. Their Actions caches remain usable,
                        # but a runtime archive cannot seed compiler work trees.
                        if 'archiveSha256' in seed:
                            request['cacheSeed']=dict(repository=repository, identity=seed['identity'],
                                manifestUrl=asset['browser_download_url'], manifestSha256=digest[7:],
                                # Pin bytes: the fixed release can change while queued.
                                manifestText=file.read_bytes().decode('utf-8'))
            payload=json.dumps(request,sort_keys=True)
            gh('workflow','run',self.plan['workflow'],'--repo',repository,'-f','request='+payload)
            print('START '+repository+'/'+stage,flush=True)
        else:
            print('RESUME '+repository+'/'+stage+' '+run['url'],flush=True)
        deadline=time.monotonic()+7*60*60
        previous=None
        while time.monotonic()<deadline:
            if self.stopped.is_set():
                raise RuntimeError('Local coordinator stopped; remote workflows remain available to resume')
            runs=self.runs(repository)
            matches=[r for r in runs if r['displayTitle']==title and r['databaseId'] not in ignored]
            run=next((r for r in matches if r['conclusion']=='success'),
                     next((r for r in matches if r['status']!='completed'), matches[0] if matches else None))
            if run:
                status=run['status']+'/'+str(run['conclusion'])
                if status!=previous:
                    print(stage+': '+status+' '+run['url'],flush=True)
                    previous=status
                if run['status'] != 'queued':
                    result = self.existing(request)
                    if result:
                        print('OK verified publication '+repository+'/'+stage+
                              '; workflow cache cleanup may still be running', flush=True)
                        return result
                if run['status']=='completed':
                    if run['conclusion']!='success':
                        raise RuntimeError(stage+' failed: '+run['url'])
                    result=self.existing(request)
                    if not result:
                        raise ValueError('Successful workflow did not publish its verified capsule')
                    return result
            self.stopped.wait(self.poll)
        raise TimeoutError('Component workflow timed out: '+stage)

    def run(self, products, parallel, output=None):
        required=set()
        def visit(stage):
            if stage in required:
                return
            for dependency in self.plan['stages'][stage]['dependencies']:
                visit(dependency)
            required.add(stage)
        targets = {}
        for product in products:
            stages = self.plan['products'].get(product, [product])
            alias = product if isinstance(stages, str) else None
            if isinstance(stages, str):
                stages = [stages]
            for stage in stages:
                if stage not in self.plan['stages']:
                    raise ValueError('Unknown component: '+stage)
                targets[stage] = alias or stage
                visit(stage)
        completed={}
        pending=set(required)
        active={}
        consumers = {name: [] for name in required}
        for name in required:
            for dependency in self.plan['stages'][name]['dependencies']:
                consumers[dependency].append(name)
        ranks = {}
        def rank(name):
            if name not in ranks:
                ranks[name] = 1 + max((rank(child) for child in consumers[name]), default=0)
            return ranks[name]
        for name in required:
            rank(name)
        # Local manifest/vendor preparation must not wait for all remote poll
        # workers. It consumes no hosted runner slot and can overlap downloads.
        with ThreadPoolExecutor(max_workers=parallel+2) as executor:
            while pending or active:
                for stage in sorted(pending, key=lambda name: (-ranks[name], name)):
                    dependencies=self.plan['stages'][stage]['dependencies']
                    local = self.plan['stages'][stage].get('execution') == 'local'
                    running = sum((self.plan['stages'][name].get('execution') == 'local') == local
                                  for name in active.values())
                    if all(name in completed for name in dependencies) and running < (2 if local else parallel):
                        future=executor.submit(self.component,stage,[completed[n] for n in dependencies])
                        active[future]=stage
                        pending.remove(stage)
                if not active:
                    raise ValueError('Component dependency graph contains a cycle')
                done,_=wait(active,return_when=FIRST_COMPLETED)
                for future in done:
                    stage=active.pop(future)
                    try:
                        completed[stage]=future.result()
                        if output is not None and stage in targets:
                            fetch_product(completed[stage], output/targets[stage])
                            print('READY '+stage+' '+str(output/targets[stage]), flush=True)
                    except BaseException:
                        self.stopped.set()
                        raise
                    (self.state/'progress.json').write_text(json.dumps(completed,indent=2)+'\n')
        return {alias:completed[stage] for stage,alias in targets.items()}


def fetch_product(reference, output):
    identity = reference['identity']
    if not re.fullmatch('[0-9a-f]{64}', identity):
        raise ValueError('Invalid product component identity')
    directory = output.parent/'github-state/downloads'/identity
    with request_lock(directory/'transfer.lock', Event()):
        cache_receipt=output/'download-receipt.json'
        import zstandard
        manifest=directory/'component.json'
        download(reference['manifestUrl'],manifest,reference['manifestSha256'])
        record=json.loads(manifest.read_text())
        if record['identity']!=reference['identity']:
            raise ValueError('Product identity mismatch')
        if reuse_product(output, reference):
            return
        if record.get('artifacts') or record.get('diagnostics'):
            # Device testing needs only the runtime. Source archives are fetched
            # by the publisher on demand, using the same verified component record.
            output.mkdir(parents=True, exist_ok=True)
            files = {}
            object_cache = directory.parent/'objects'
            def fetch_artifact(item):
                name = item['name']
                if Path(name).name != name or '\\' in name or ':' in name:
                    raise ValueError('Unsafe product artifact name')
                with request_lock(object_cache/(item['sha256']+'.lock'), Event()):
                    cached = download(item['url'], object_cache/item['sha256'], item['sha256'], label=name)
                if cached.stat().st_size != item['bytes']:
                    raise ValueError('Product artifact size mismatch')
                import shutil
                shutil.copyfile(cached, output/name)
                return name, item['sha256']
            # Each producer is ready independently; no product selection is
            # generated and no other producer blocks device deployment.
            if record.get('diagnostics'):
                for item in record['diagnostics']:
                    name, checksum = fetch_artifact(item)
                    files[name] = checksum
            else:
                manifest = record['runtimeManifest']
                name, checksum = fetch_artifact(dict(manifest, name=manifest['file']))
                files[name] = checksum
            (output/'github-component.json').write_text(json.dumps(record, indent=2)+'\n')
            files['github-component.json'] = sha256_file(output/'github-component.json')
            cache_receipt.write_text(json.dumps(dict(format=1, reference=reference, files=files), indent=2)+'\n')
            return
        archive=download_capsule(record, directory)
        audio=record['stage']=='wine-pulse'
        product=('audio' if audio else 'diagnostics' if record['stage']=='wine-diagnostics'
                 else 'wine-capture' if record['stage']=='wine-capture' else None)
        if product is None:
            raise ValueError('Compiler capsules are not deployable runtime producers')
        prefix='framework/build/artifacts/'+product+'/'
        output.mkdir(parents=True,exist_ok=True)
        with archive.open('rb') as source,zstandard.ZstdDecompressor().stream_reader(source) as decoded:
            with tarfile.open(fileobj=decoded,mode='r|') as capsule:
                for member in capsule:
                    if not member.isfile():
                        continue
                    if audio:
                        if not member.name.startswith('framework/build/pipeline/wine-pulse/') or '/output/android/' not in member.name:
                            continue
                        name=member.name.split('/output/android/',1)[1]
                        if not name.startswith(('jni/arm64-v8a/','assets/pulseaudio/')):
                            raise ValueError('Unexpected audio output')
                    else:
                        if not member.name.startswith(prefix):
                            continue
                        name=member.name[len(prefix):]
                    if '\\' in name or ':' in name or any(p in ('','.','..') for p in name.split('/')) or (not audio and '/' in name):
                        raise ValueError('Unsafe product archive member')
                    (output/name).parent.mkdir(parents=True,exist_ok=True)
                    with (output/name).open('wb') as dest:
                        import shutil
                        shutil.copyfileobj(capsule.extractfile(member),dest)
        (output/'github-component.json').write_text(json.dumps(record,indent=2)+'\n')
        if audio:
            files={str(p.relative_to(output)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest()
                   for folder in ('jni','assets') for p in (output/folder).rglob('*') if p.is_file()}
            (output/'build-receipt.json').write_text(json.dumps(dict(format=1,component=reference,files=files),indent=2)+'\n')
        files={p.relative_to(output).as_posix():sha256_file(p)
               for p in output.rglob('*') if p.is_file() and p!=cache_receipt}
        cache_receipt.write_text(json.dumps(dict(format=1,reference=reference,files=files),indent=2)+'\n')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--framework-ref',default='arlinux')
    parser.add_argument('--product',choices=('wine','audio','diagnostics','hybris-diagnostics','wine-capture','all'),default='all')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--parallel',type=int,default=8)
    parser.add_argument('--component', choices=None, help='Build only this runtime producer and its compiler dependencies')
    parser.add_argument('--plan',action='store_true')
    args=parser.parse_args()
    framework=Path(__file__).resolve().parents[2]
    plan=load_plan()
    if args.plan:
        print(json.dumps(plan,indent=2))
        return
    if args.component and args.component not in plan['products']['wine']:
        parser.error('--component must name an installable Wine producer')
    framework_sha=(args.framework_ref if re.fullmatch('[0-9a-f]{40}',args.framework_ref)
                   else api('repos/taowen/wine/commits/'+args.framework_ref)['sha'])
    products=([args.component] if args.component else
              ['wine','audio'] if args.product=='all' else [args.product])
    state=args.output/'github-state'
    coordinator=Coordinator(plan,framework_sha,None,state)
    started=time.monotonic()
    result=coordinator.run(products,args.parallel,args.output)
    graph_seconds=time.monotonic()-started
    transfers={}
    for name,reference in result.items():
        transfer_started=time.monotonic()
        fetch_product(reference,args.output/name)
        transfers[name]=dict(seconds=round(time.monotonic()-transfer_started,3))
    (args.output/('products-'+args.product+'.json')).write_text(json.dumps(dict(format=1,
        framework=framework_sha,products=result),indent=2)+'\n')
    (args.output/('build-timing-'+args.product+'.json')).write_text(json.dumps(dict(format=1,
        framework=framework_sha, totalSeconds=round(time.monotonic()-started,3),
        graphSeconds=round(graph_seconds,3), components=coordinator.timings,
        productTransfers=transfers),indent=2)+'\n')


if __name__=='__main__':
    main()
