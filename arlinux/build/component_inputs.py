"""Content identities and bootstrap requirements for the Wine component graph."""
import ast
import hashlib
import json
from pathlib import Path
import subprocess

PE = ('arlinux/build/wine-pe.sh',
      'arlinux/components/windows-integration')
SOURCES = {
    'bionic-deps': ('arlinux/components/bionic-deps', 'tools/build', 'toolchain.json'),
    'bionic-x11': ('arlinux/build/bionic-x11.sh', 'arlinux/components/libX11'),
    'wine-pulse': ('arlinux/components/pulseaudio-android', 'tools/build/ndk-env.sh', 'toolchain.json'),
    **{'wine-pe-'+arch: ('@wine', *PE, 'arlinux/build/runtime_distribution.py', 'arlinux/wine_pe_debug.py') for arch in ('aarch64', 'x86_64', 'i386')},
    'wine-pe': (),
    'fex': ('arlinux/build/fex-windows.sh', 'arlinux/components/FEX', 'arlinux/build/runtime_distribution.py', 'arlinux/wine_pe_debug.py'),
    'dxvk': ('arlinux/build/dxvk-windows.sh', 'arlinux/components/dxvk', 'arlinux/components/dxvk-legacy', 'arlinux/windows-graphics.cpp', 'arlinux/build/runtime_distribution.py', 'arlinux/wine_pe_debug.py'),
    'wine-addons': ('arlinux/build/local_addons.py', 'arlinux/build/runtime_distribution.py'),
    'wine-tls': ('arlinux/components/bionic-deps',),
    'wine-graphics': ('arlinux/build/wine-bionic-graphics.sh', 'arlinux/components/mesa',
                      'arlinux/components/libxshmfence', 'arlinux/components/libhybris'),
    'wine-vulkan': ('arlinux/components/libhybris', 'arlinux/components/android-headers'),
    'wine-bionic-compile': ('@wine', *PE, 'arlinux/build/wine-bionic.sh', 'arlinux/components/FEX'),
    'wine-bionic': ('arlinux/package-wine-bionic.py', 'arlinux/wine_pe_debug.py',
                    'dlls/winebus.sys/bus_arlinux.h', 'arlinux/winebus', 'COPYING.LIB',
                    'arlinux/build/runtime_assembly.py', 'arlinux/build/runtime_distribution.py'),
    'steamworks': ('arlinux/components/steamworks', 'arlinux/build/runtime_distribution.py'),
    'windows-accessibility': ('arlinux/components/windows-integration', 'arlinux/build/runtime_distribution.py'),
    'windows-tray': ('arlinux/components/windows-integration', 'arlinux/build/runtime_distribution.py'),
    'wine-diagnostics': ('arlinux/build/windows-debug.sh', 'arlinux/windows-debug.py',
                         'arlinux/components/windows-integration'),
    'wine-capture': ('arlinux/components/gfxreconstruct', 'arlinux/vulkan-capture.py'),
    'windows-fonts': ('arlinux/wine_fonts.py', 'arlinux/build/runtime_distribution.py'),
}
RESOURCES = {name: ('mingw',) for name in SOURCES}
for name in ('bionic-deps', 'bionic-x11', 'wine-pulse', 'wine-tls', 'wine-graphics', 'wine-vulkan'):
    RESOURCES[name] = ('ndk',)
RESOURCES.update({'wine-addons': ('addons',), 'wine-bionic-compile': ('mingw', 'ndk'), 'wine-bionic': ('ndk',),
                  'wine-capture': ('ndk',), 'windows-fonts': (), 'wine-vulkan': ('ndk', 'vulkanHeaders')})
SHARED = ('arlinux/build/github-component.py',
          'arlinux/build/wine-pipeline.py', 'arlinux/build/pipeline_cache.py',
          'arlinux/build/wine_graph.py', 'arlinux/build.sh')
ASSEMBLY_CODE = {
    'wine-bionic': ('package_bionic', 'build_native'),
}


def implementation(source, names=None):
    """Hash declared Python recipes and imports, independent of other recipes.

    The input engine's declaration tables are represented by their resolved
    per-stage values below, not by unrelated stages' table entries.
    """
    tree = ast.parse(source)
    definitions = {node.name: node for node in tree.body
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    selected = sorted(definitions) if names is None else names
    def entrypoint(node):
        return (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
                and ast.dump(node.test, include_attributes=False) == ast.dump(
                    ast.parse('__name__ == "__main__"', mode='eval').body, include_attributes=False))
    nodes = [node for node in tree.body if not entrypoint(node) and
             (not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
              if names is not None else isinstance(node, (ast.Import, ast.ImportFrom)))]
    nodes += [definitions[name] for name in selected]
    return hashlib.sha256(ast.dump(ast.Module(body=nodes, type_ignores=[]),
                                  include_attributes=False).encode()).hexdigest()


def core_path(name):
    return not (name.startswith(('arlinux/', '.github/', 'tools/build/')) or
                name in ('.gitmodules', 'toolchain.json'))


def tree_entries(repo, revision, paths=()):
    args = ['git', '-C', str(repo), 'ls-tree', '-r', '-z', revision]
    if paths:
        args += ['--', *paths]
    entries = {}
    for record in subprocess.check_output(args).split(b'\0'):
        if record:
            metadata, name = record.split(b'\t', 1)
            entries[name.decode()] = metadata.decode()
    return entries


def source_inputs(repo, revision, stage, plan):
    paths = SOURCES[stage]
    selected = tree_entries(repo, revision, (*SHARED, *(p for p in paths if p != '@wine')))
    def source(path):
        return subprocess.check_output(['git', '-C', str(repo), 'show', revision+':'+path]).decode()
    selected['arlinux/build/component_inputs.py'] = implementation(source('arlinux/build/component_inputs.py'))
    if stage == 'bionic-deps':
        # Vulkan/Mesa/PE recipes do not participate in the base SDK compiler.
        path = 'arlinux/build/wine-pipeline.py'
        selected[path] = implementation(source(path), ('build_bionic_deps', 'main'))
    publication = {}
    runtime_publisher = 'arlinux/build/runtime_distribution.py'
    if runtime_publisher in paths and plan['stages'][stage].get('execution') != 'local':
        # Runtime manifests and install archives are post-processing. A change
        # here republishes the component using verified compiler outputs.
        publication[runtime_publisher] = selected.pop(runtime_publisher)
        if stage != 'wine-bionic' and 'arlinux/wine_pe_debug.py' in selected:
            publication['arlinux/wine_pe_debug.py'] = selected.pop('arlinux/wine_pe_debug.py')
    selected['declaredPaths'] = tuple(path for path in paths if path not in publication)
    adapter = 'arlinux/build/github-component.py'
    handlers = ('build_'+stage.replace('-', '_'),) if stage in (
        'windows-fonts', 'wine-capture', 'wine-diagnostics') else ()
    selected[adapter] = implementation(source(adapter),
        ('run', 'sha', 'request', 'prepare', 'toolchains', 'import_dependency', 'restore_seed', 'build', *handlers))
    selected['arlinux/build/artifact_download.py'] = implementation(
        source('arlinux/build/artifact_download.py'), ('download', 'sha256_file', 'download_capsule'))
    if stage in ASSEMBLY_CODE:
        path = 'arlinux/build/runtime_assembly.py'
        selected[path] = implementation(source(path), ASSEMBLY_CODE[stage])
    if '@wine' in paths:
        selected.update({n: v for n, v in tree_entries(repo, revision).items() if core_path(n)})
    lock = subprocess.check_output(['git', '-C', str(repo), 'show', revision+':arlinux/build/github-inputs.json'])
    versions = json.loads(lock)
    recipe = dict(selected)
    publisher = implementation(source(adapter), ('publish', 'publish_distribution',
        *(('diagnostic_artifacts',) if stage == 'wine-vulkan' else ())))
    if publication:
        publisher = hashlib.sha256(json.dumps(dict(adapter=publisher, sources=publication), sort_keys=True).encode()).hexdigest()
    return dict(sourceSha256=hashlib.sha256(json.dumps(selected, sort_keys=True).encode()).hexdigest(),
                recipeSha256=hashlib.sha256(json.dumps(recipe, sort_keys=True).encode()).hexdigest(),
                publicationSha256=publisher,
                tools={n: versions[n] for n in RESOURCES[stage]},
                runner=plan['stages'][stage]['runner'],
                artifactInputsOnly=plan['stages'][stage].get('artifactInputsOnly', False))


def request_identity(data):
    fields = {k: v for k, v in data.items() if k not in ('identity', 'cacheSeed')}
    if data.get('format') == 2:
        fields.pop('framework')  # Provenance is retained, but unrelated commits are not build inputs.
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


def requirements(stage, plan):
    stages = set()
    def visit(name):
        if name not in stages:
            stages.add(name)
            if not plan['stages'][name].get('artifactInputsOnly'):
                for dependency in plan['stages'][name]['dependencies']:
                    visit(dependency)
    visit(stage)
    paths = {p for name in stages for p in SOURCES[name] if p.startswith('arlinux/components/')}
    modules = sorted({'/'.join(p.split('/')[:3]) for p in paths})
    resources = {r for name in stages for r in RESOURCES[name]}
    return modules, resources
