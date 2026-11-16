"""Native backend packaging; PE and support producers publish independently."""
import json
import os
from pathlib import Path


def build_native(data, framework, artifacts, tools):
    from pipeline_cache import Cache, inventory, build_references, run
    root = framework.parent
    outputs, dependencies = {}, {}
    for name, relative in artifacts.items():
        output = (root/relative).resolve(strict=True)
        if not output.is_relative_to(framework/'build/pipeline'/name):
            raise ValueError('Native packaging input escapes its declared stage')
        receipt = json.loads((output.parent.parent/'receipt.json').read_text())
        if (output != framework/'build/pipeline'/name/receipt['inputs']/receipt['generation']/'output'
                or inventory(output) != receipt['outputs'] or build_references(output) != receipt['references']):
            raise ValueError('Native packaging input differs from its build receipt: '+name)
        outputs[name], dependencies[name] = output, receipt
    cache = Cache(framework/'build/pipeline')
    inputs = dict(recipe=data['sourceInputs']['recipeSha256'], tools=data['sourceInputs']['tools'],
                  dependencies=dependencies)
    paths = dict(ndk=Path(tools['ndk']), bionicPrefix=outputs['bionic-deps']/'prefix')
    invoke = lambda *args: run(*args, cwd=framework, env=os.environ.copy())
    output = cache.stage('wine-bionic', inputs, lambda out: package_bionic(out, framework,
        outputs['wine-bionic-compile'], outputs['wine-tls'], outputs['wine-media'], outputs['wine-pulse'],
        outputs['wine-graphics'], outputs['wine-vulkan'], paths, invoke))
    descriptor = framework/'build/artifacts/components/wine-bionic.json'
    descriptor.parent.mkdir(parents=True, exist_ok=True)
    descriptor.write_text(json.dumps(dict(output=str(output), records=cache.records), sort_keys=True)+'\n')


def package_bionic(out, framework, compiled, tls, media, pulse, graphics, adapter, paths, invoke):
    out.mkdir()
    invoke('python3', 'arlinux/package-wine-bionic.py', '--core', compiled/'core',
           '--fex', compiled/'fex', '--native-prefix', paths['bionicPrefix'],
           '--dependency-prefix', tls/'prefix', '--dependency-prefix', pulse/'work/prefix',
           '--dependency-prefix', media/'prefix',
           '--graphics-prefix', graphics/'prefix',
           '--ndk', paths['ndk']/'toolchains/llvm/prebuilt/linux-x86_64',
           '--output', out/'runtime',
           '--wine-source-archive', compiled/'wine-source.tar.gz',
           '--vulkan-driver', 'system',
           '--system-vulkan-adapter', adapter/'libvulkan_android_desktop.so')
