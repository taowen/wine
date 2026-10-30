# Bionic Wine build and release

This Wine fork owns Bionic Wine integration, the component dependency graph,
source archives, hosted dispatch and public runtime selection. Rootfs products
only download the runtime on demand. They neither build nor dispatch Wine.

The `arlinux/components/libhybris` submodule follows libhybris's single `arlinux`
integration branch, shared with the rootfs consumer. Keep both consumers pinned
to the same commit. Linux glibc and Android Bionic are two build modes of those
sources, with independent runners and artifacts; see the
[component ownership guide](https://github.com/taowen/libhybris/blob/arlinux/arlinux/BUILD.md).

Our implementation upstream is [giang17/wine](https://github.com/giang17/wine),
branch `d2d1-dcomp-11.20`, integrated at
`fcad3e89b99c5882a4a0b4e889124b5f4a1c6631` on the Wine 11.20 base.
Merge that upstream into the ARLinux branch; do not replace individual DComp
files from another revision or replay our old DComp readback/lifetime patch.
Direct2D, DirectComposition and related fixes follow the upstream source tree.
The 11.20 merge supersedes the earlier DComp backports and experiments with
the upstream implementation. Keep its window-tree, cross-thread composition,
readback and DPI behavior rather than restoring private DComp workarounds.
An upstream merge does not establish application compatibility; validate cold
startup and interaction in a fresh device instance before promotion.
Android/Bionic integration remains in this fork. The Wine Staging VKD3D update is
committed directly under `libs/vkd3d`, not applied during source preparation.
The resulting ARLinux commit identifies the complete compiled Wine sources.

The workstation entry is the Android repository's `build.ps1`; follow its
[canonical entry guide](https://github.com/taowen/arlinux/blob/main/docs/BUILD.md).
`arlinux/build.sh` and `arlinux/build/` are the implementation used by that entry
and the independent component runners. Do not create another workstation builder,
use WSL, assemble an old runtime archive, or bypass the pinned graph stages.

## Independent runtime producers

Each producer owns its compiler workflow, runtime manifest, archives and releases.
Daily development overwrites its fixed `arlinux-COMPONENT-debug` release;
only explicit promotion updates the public `arlinux-COMPONENT-latest`.
Neither path creates a release tag containing an input SHA.
There is no `wine-product` stage, total runtime manifest, version combination
selection or product-wide compatibility hash. Each `build/components/COMPONENT.json` defines that component's repository,
compiler dependencies and fixed release, such as `arlinux-wine-capture-debug`.
The coordinator reads these independent recipes; there is no central component
manifest. It queries that exact release directly, never scans SHA-tagged releases.
Input and output hashes remain cache keys and integrity/provenance records.
Payloads upload before the component completion marker. Compiler publication
does not replace the tested public `runtime.tar.gz`; explicit promotion does.

The DXVK fork carries upstream incremental `Present1` support, including dirty
rectangles, scrolling and complete-frame preservation across sequential buffers.
Its offscreen composition path applies the same operations before rotating
buffers, so Wine's DirectComposition readback receives a complete presented
frame. The shader integration uses the existing Vulkan 1.3-compatible API;
this does not require a newer Vulkan baseline or application-specific flags.

On Android, the host passes its actual language in `LANG` / `LC_MESSAGES` and
retains `LC_CTYPE=C.UTF-8` for Unix filenames and text input. Bionic implements
only C locales, so native ntdll resolves the requested language with Wine's own
NLS tables rather than treating Bionic's C locale as the Windows system language.

Window creation posts the initial `WM_DWMNCRENDERINGCHANGED` notification for
captioned top-level windows in composition-enabled Windows versions, including
windows created hidden. It queues the notification before entering `WM_CREATE`,
so initialization waits inside that callback can observe it;
`MsgWaitForMultipleObjects` retains its ordinary WaitAll contract. The optional
Windows integration `message-wait` diagnostic compares the same executable on
native Windows and Wine without modifying or injecting into the application.

The producers are `wine-bionic`, `wine-pe-aarch64`, `wine-pe-x86_64`,
`wine-pe-i386`, `fex`, `dxvk`, `windows-fonts`, `wine-addons`, `steamworks`,
`windows-accessibility` and `windows-tray`. Their independently hashed compiler
receipts and source archives remain reproducible. Compiler dependencies are
built only when required by the requested producer; building `wine-bionic`
does not wait for Steamworks or the x86 PE producers.

`wine-vulkan` depends on the base Bionic SDK and a hash-locked Vulkan-Headers
archive, not on Mesa compilation. Its standalone `hybris-diagnostics` APK can
therefore build without Mesa; a full Wine build schedules the Vulkan adapter
and Mesa independently after their shared SDK is ready.

The phone caches each producer's extracted files independently. Updating one
producer downloads and extracts only its changed archives. Immutable launch
views retain real files from the native producer for Wine's realpath-based data
lookup and use relative symbolic links for other producers. Running processes
keep their old view.
The view's `bionic-runtime.json` contains merged runtime configuration and file
ownership, not a producer-version vector or a requirement to rebuild the other
producers. Steam and Windows launcher apps share these files and the instance's
single Wine prefix. Conflicting file ownership and unsafe archives still fail.

## Cache and provenance

Maintained Wine fixes live directly in this fork's source tree, including the
US keyboard layout, DirectWrite rendering fixes, Android audio/locale/storage
adaptations, ARM64 APC handling and Vulkan/window/memory fixes. Native and PE
builds archive the same pinned Git tree instead of replaying those patches.
`@wine` includes these source files in compiler identities, so changing them
invalidates affected outputs without an additional patch manifest. The former
standalone patches remain recoverable in Git history. DirectComposition comes
from the integrated upstream. VKD3D is integrated through revision
`fed0c3418f815b0bc07533f6fc6f4aa06e8712c3`, from Wine Staging
`11dd86bdfada3c85877b90c765ff9e3af200ddd1`'s eight-update series. The Wine Git
tree identifies the actual compiled sources; there is no patch replay or stamp.
DXVK is pinned by `arlinux/components/dxvk`, including its optional pipeline-library
support. The FEX UnixLib keeps its Android stats-file adaptation under
`__ANDROID__` in the pinned FEX source, preserving POSIX shared memory elsewhere.

FEX is pinned to the taowen/FEX fork commit
`74c391f3f0da1ced939b3842c2b633c8cf00b7ac` on FEX-2610, including the ARM64EC
and WoW64 process-termination fixes and Android UnixLib stats-file adaptation.
The Windows DLLs (`fex`) and Bionic UnixLib (`wine-bionic`) must be built and
validated together when this source pin changes.
Before terminating peer threads, it drains code invalidation and retains the
locks across the termination notification. The WoW64 change follows the
observed .NET service's detach/unmap wait with a stranded invalidation reader;
fresh-instance Power BI validation remains required. Keep such component fixes
in the pinned fork rather than applying patches during builds.

Requests hash declared source trees, recipes, locked toolchains, publication
recipes and dependency identities. A source commit is retained as provenance,
not a blanket invalidation key. Compiler outputs, references and receipts are
verified before reuse. Compiler caches are bounded in the graph; exported SDK
capsules do not duplicate complete private object trees or unrelated toolchains.

`component_inputs.py` separates runtime publication from compiler recipes.
Changing a producer manifest or PE stripping recipe republishes from verified
compiler outputs. Command-line entry guards do not enter function recipe hashes.
Changes to actual shared compiler logic still invalidate affected compile inputs.
The graph/source-identity migration requires its own first build; do not describe
that build as a steady-state warm compiler measurement.

Concurrent coordinators lock component identities and share hosted runs. Fixed
source views and product directories are isolated by commit. Immutable object
downloads resume and retain checksum verification. Cache receipts bind every
downloaded manifest and archive to the selected component record. Repeated runs
must verify outputs rather than accept a filename or an old installed instance.

The entry records `build-timing-wine.json` and `products-wine.json`. Compare exact
pins, workflow IDs, preparation, transfer, compilation and publication costs.
Repeat the complete entry to measure verified reuse; a cache hit is not evidence
of a faster fresh compilation.

On 2026-10-20, candidate `30b74024f6aedb153503486fc95fc4552300fd77`
completed the independent-manifest pipeline. Its 3,716-byte selection has SHA-256
`0a3e0a629aa373950f65d5b5d53a3d7ea6736e08137315ba59ad7ef94c32a59e`.
The native runtime components total 17.74 MiB; PE, vendor and support downloads
are separate. The first publication/recovery run took 1,204.86 seconds, including
1,096.875 seconds graph work and the initial runtime transfers. Repeating the
complete coordinator for the identical pin took 3.328 seconds, including 2.609
seconds graph work, with no new hosted job or runtime download. This excludes
device seeding and is verified reuse, not a fresh compiler benchmark.

The corrected consumer APK used rootfs
`7e7b406d8e860fe7c8b089ccffdd86e35232ecc2`; installed APK SHA-256 was
`cdc7d1238390f8df3355a96e386b439de44b19d2fbf9a54dddbe16a9c4ba4a49`.
Xiaomi 15 (`3fad759`), fresh instance `yibu-62fb6886`, passed built-in FontForge
first-use runtime installation, vendor installation, typed Arial opening, visible
glyph rendering, SFD save, normal quit, icon relaunch with the saved SFD and final
quit. Live native mappings identified the exact selection above. This validates
the split installation journey; it does not establish Power BI/WebView2 or game
compatibility. Those regressions and public latest promotion remain pending.

## Development validation

Use the Android checkout's sole entry. A Wine invocation defaults to the native
`wine-bionic` producer. Select any other producer explicitly:

```powershell
.\build.ps1 wine -WineRef FULL_PUSHED_COMMIT -WineComponent wine-bionic
.\build.ps1 wine -WineRef FULL_PUSHED_COMMIT -WineComponent wine-pe-aarch64
```

The command prints the exact producer directory. Each directory contains its
own `arlinux-PRODUCER.json`, `github-component.json` and verified download receipt.
It never generates an aggregate runtime selection.

Before acceptance, replace the tested instance through the Android guide's
canonical rootfs deploy/import flow. Debug APKs do not embed rootfs. Confirm the
device serial, package and explicit instance ID; preserve only Steam test
credentials using the documented exception. Never restore old homes, installed
applications or Wine prefixes. Keep the instance-external verified download cache.

With that fresh instance and desktop stopped, deploy the one changed producer:

```powershell
.\build.ps1 wine -WineRef FULL_PUSHED_COMMIT -WineComponent wine-bionic -Serial SERIAL -Instance yibu-FRESH_INSTANCE
```

Only the small verified producer manifest crosses ADB. Its override is stored at
`instances/INSTANCE/home/.config/arlinux/wine-components/PRODUCER.json`.
The phone downloads archives directly through the usual routing/cache/checksum
path. Other producers keep their own independent public releases. Missing
candidate metadata fails rather than silently falling back. Subsequent Wine-only
changes require no APK rebuild or latest publication. A protocol change requires
a new APK and fresh distribution through the same canonical entry.

Complete install, interaction, close, reopen and task stop on the device. Retain
APK/source hashes, per-producer receipts, process maps, screenshots and logs in
private validation evidence. Debug receiver maintenance and authentication
commands are maintained only in the [Android diagnostic guide](https://github.com/taowen/arlinux/blob/main/tools/diagnostics/README.md#debug-yibu-instance-maintenance).
Release builds expose no candidate receiver. Do not treat compilation as device
acceptance. Preserve the tray as an independent window selected in Yibu's bar.

## Publishing fixed component releases

Every producer publishes `runtime.tar.gz` in its own fixed
`arlinux-PRODUCER-latest` release. GitHub supplies its SHA-256, size and URL.
There is no public or internal total-version JSON and no pairing token.
The rootfs owns only stable endpoint names in `windows-downloads.tsv`, never a
Wine build dependency or a version combination.

New builds upload payloads and `component.json` to the component's fixed debug
release. Candidate preparation replaces `runtime.tar.gz` and then `candidate.json`
in that same release; it creates no additional candidate release or SHA tag.
An unchanged request reuses the verified completion record. SHA-256 remains in
receipts and content-addressed payload filenames, not in release selection.
Previously published runtimes can still reference source/assets under the old
`arlinux-wine-components` release. Retain those referenced assets; new builds
do not read or write this legacy warehouse.

Prepare a delivery candidate from one exact verified producer manifest:

```powershell
$wineRef = 'FULL_PUBLISHER_COMMIT'
$manifest = 'EXACT_PRODUCER_DIRECTORY/arlinux-wine-bionic.json'
.\build.ps1 publish-wine -WineRef $wineRef -Archive $manifest -Check -Serial SERIAL -Instance yibu-FRESH_INSTANCE
```

This dispatches only that producer's release job. The runner verifies declared
runtime/source hashes and replaces `runtime.tar.gz` and its completion marker in the same debug release;
it does not compile Wine or restore compiler caches. Windows handles only the
small `public-release/candidate.json` descriptor and receipts. Its digest pins the
tested bytes; if a newer debug upload replaces them before promotion, the
promotion fails checksum verification and must be prepared and tested again. The phone tests
the candidate archive directly. Finish the fresh-instance journey before promotion.

```powershell
.\build.ps1 publish-wine -WineRef $wineRef -Archive $manifest
```

Promotion copies the exact tested archive to that producer's fixed release
without rebuilding or repackaging. Other producers are untouched. Compare the
published SHA-256 with the tested archive, then use a fresh instance without
candidate overrides to verify the ordinary public download path. Publishing one
component never waits for unrelated component publications.

## Optional diagnostics

`build.ps1 diagnostics` produces matching Windows exception helpers;
`build.ps1 wine-capture` produces optional Bionic GFXReconstruct tools. These
products are independent of ordinary runtime assembly and are disabled by
default. The rootfs/glibc capture product is separate. Retain capture limits,
exact module identity and an uncaptured control run; initialization records do
not establish rendered frames, successful replay or application compatibility.

The ordinary component build publishes to its fixed release in the owning
repository, for example `taowen/gfxreconstruct` / `arlinux-wine-capture-debug`.
Updating the component replaces its completion metadata at this same endpoint;
archives retain SHA-256 verification. No SHA-tagged release is created. The product's `download-receipt.json` records
the exact release URL and hashes; retain it with the tested capture archive.
No combined Wine archive or runtime-producer promotion is needed for these
optional tools. Updating their source pin does not change an installed runtime
or enable capture in normal application launches.
