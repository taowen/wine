#!/usr/bin/env bash
# Build small diagnostics, not a new Wine runtime.
set -euo pipefail
if [[ $# != 2 ]]; then
    echo 'Usage: windows-debug.sh LLVM_MINGW_BIN OUTPUT_DIR' >&2
    exit 2
fi
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
compiler="$(realpath "$1")/aarch64-w64-mingw32-clang"
output="$(realpath -m "$2")"
mkdir -p "$output"
"$compiler" -Wall -Wextra -Werror -municode \
    "$repo/arlinux/components/windows-integration/diagnostics/exception-observer.c" -ldbghelp -Wl,--no-insert-timestamp \
    -o "$output/exception-observer.exe"
"$compiler" -Wall -Wextra -Werror \
    "$repo/arlinux/components/windows-integration/diagnostics/exception-observer-probe.c" -Wl,--no-insert-timestamp \
    -o "$output/exception-observer-probe.exe"
for arch in i686 x86_64 aarch64; do
    "$(realpath "$1")/$arch-w64-mingw32-clang" -Wall -Wextra -Werror \
        "$repo/arlinux/components/windows-integration/diagnostics/message-wait.c" \
        -ldsound -luser32 -Wl,--no-insert-timestamp -o "$output/message-wait-$arch.exe"
done
"$compiler" -std=c11 -static -Wall -Wextra -Werror -municode -mwindows \
    "$repo/arlinux/components/windows-integration/diagnostics/tray-sample.c" \
    -luser32 -lshell32 -lgdi32 -Wl,--no-insert-timestamp -o "$output/tray-sample.exe"
sha256sum "$output/"*.exe
