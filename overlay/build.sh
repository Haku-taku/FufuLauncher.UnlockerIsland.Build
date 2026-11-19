#!/usr/bin/env bash
#
# build.sh -- cross-compile FufuLauncher.UnlockerIsland.dll on Linux.
#
# Reproduces the Visual Studio "Release|x64" configuration with the
# MSVC-compatible LLVM toolchain, so the result is ABI-compatible with a
# native MSVC build:
#
#   xwin      splats the MSVC CRT + Windows SDK headers/import libraries
#   clang-cl  compiles; unlike GCC it understands the __try/__except SEH
#             the sources rely on, and it consumes the real MSVC STL
#   llvm-lib  repacks MinHook from source (the shipped libMinHook.x64.lib
#             is /GL LTCG, which lld-link cannot read)
#   llvm-ml   assembles the project's x64 MASM sources
#   llvm-rc   compiles Resource.rc (version info)
#   lld-link  links against the MSVC import libraries, /MT static CRT
#
# Usage:
#   ./build.sh                  # release build, output in ./x64/
#   JOBS=4 ./build.sh           # cap compile parallelism
#   DEBUG=1 ./build.sh          # /Z7 + /DEBUG, also emits a .pdb
#   CLEAN=1 ./build.sh          # wipe .build/ before building
#
# Environment overrides: JOBS, DEBUG, CLEAN, XWIN_DIR, MINHOOK_SRC,
#                        CLANG_CL, LLD_LINK, LLVM_LIB, LLVM_RC, LLVM_ML,
#                        (existing invalid MINHOOK_SRC directories are never deleted).
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJ="$ROOT/FufuLauncher.UnlockerIsland"
VCXPROJ="$PROJ/FufuLauncher.UnlockerIsland.vcxproj"

BUILD="$ROOT/.build"          # scratch: objects, MinHook, xwin staging
OBJDIR="$BUILD/obj"           # re-pointed at obj/$CONFIG once DEBUG is known
STAGE="$BUILD/stage"          # holds the replacement MinHook/libMinHook.x64.lib
OUTDIR="$ROOT/x64"            # final artifact

XWIN_DIR="${XWIN_DIR:-$HOME/.xwin}"
XWIN_CACHE="${XWIN_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/xwin}"
MINHOOK_REPO="https://github.com/TsudaKageyu/minhook"
MINHOOK_TAG="v1.3.3"
MINHOOK_SRC="${MINHOOK_SRC:-$BUILD/minhook}"

JOBS="${JOBS:-$(nproc 2>/dev/null || echo 4)}"
(( JOBS >= 1 )) || JOBS=1      # the pool indexes POOL[0], so 0 is not allowed
DEBUG="${DEBUG:-0}"
CLEAN="${CLEAN:-0}"
DLL_NAME="FufuLauncher.UnlockerIsland.dll"

CLANG_CL="${CLANG_CL:-clang-cl}"
LLD_LINK="${LLD_LINK:-lld-link}"
LLVM_LIB="${LLVM_LIB:-llvm-lib}"
LLVM_RC="${LLVM_RC:-llvm-rc}"
LLVM_ML="${LLVM_ML:-llvm-ml}"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- job pool --
# Minimal parallel runner: `run <cmd...>` keeps at most $JOBS children alive,
# `pool_wait` drains them and fails if any child failed.
declare -a POOL=()
pool_failed=0
run() {
    "$@" &
    POOL+=("$!")
    if (( ${#POOL[@]} >= JOBS )); then
        wait "${POOL[0]}" || pool_failed=1
        POOL=("${POOL[@]:1}")
    fi
}
pool_wait() {
    local pid
    for pid in "${POOL[@]}"; do wait "$pid" || pool_failed=1; done
    POOL=()
    local rc=$pool_failed
    pool_failed=0
    return $rc
}

# ------------------------------------------------------------ prerequisites --
for tool in "$CLANG_CL" "$LLD_LINK" "$LLVM_LIB" "$LLVM_RC" "$LLVM_ML"; do
    command -v "$tool" >/dev/null 2>&1 || die "$tool not found.
    Install the LLVM/clang toolchain, e.g.:
      Arch:   sudo pacman -S clang lld llvm
      Debian: sudo apt install clang lld llvm"
done

log "Toolchain: $("$CLANG_CL" --version | head -1)"

[[ "$DEBUG" == "1" ]] && CONFIG="Debug" || CONFIG="Release"
OBJDIR="$BUILD/obj/$CONFIG"
log "Configuration: $CONFIG|x64"

if [[ "$CLEAN" == "1" ]]; then
    log "Cleaning $BUILD"
    rm -rf "$BUILD"
fi
mkdir -p "$OBJDIR" "$STAGE" "$OUTDIR"

# ------------------------------------------------------------------- xwin --
# Splat the MSVC CRT + Windows SDK. xwin also creates the case-insensitive
# filename aliases the sources need (they say <Windows.h>, the real file on
# this case-sensitive filesystem is Windows.h).
if [[ -d "$XWIN_DIR/crt/lib/x86_64" && -d "$XWIN_DIR/sdk/lib/um/x86_64" ]]; then
    log "Using existing MSVC headers/libs at $XWIN_DIR"
else
    command -v xwin >/dev/null 2>&1 || die "xwin not found (cargo install xwin)"
    log "Splatting MSVC CRT + Windows SDK into $XWIN_DIR (one-time, ~1 GB)"
    mkdir -p "$XWIN_DIR"
    xwin --accept-license --cache-dir "$XWIN_CACHE" splat --output "$XWIN_DIR"
fi

CRT_INC="$XWIN_DIR/crt/include"
SDK_INC_UCRT="$XWIN_DIR/sdk/include/ucrt"
SDK_INC_UM="$XWIN_DIR/sdk/include/um"
SDK_INC_SHARED="$XWIN_DIR/sdk/include/shared"
CRT_LIB="$XWIN_DIR/crt/lib/x86_64"
SDK_LIB_UM="$XWIN_DIR/sdk/lib/um/x86_64"
SDK_LIB_UCRT="$XWIN_DIR/sdk/lib/ucrt/x86_64"

for d in "$CRT_INC" "$SDK_INC_UM" "$SDK_INC_UCRT" "$CRT_LIB" "$SDK_LIB_UM" "$SDK_LIB_UCRT"; do
    [[ -d "$d" ]] || die "missing $d -- re-run with CLEAN=1 to re-splat xwin"
done

INCLUDES=(
    "-I$CRT_INC"
    "-I$SDK_INC_UCRT"
    "-I$SDK_INC_UM"
    "-I$SDK_INC_SHARED"
)

# These are clang-vs-MSVC-header interop notices (case-sensitive filesystem,
# MSVC-only pragmas/intrinsics/attributes, SAL), not project defects. Suppress
# them so the ~16 warnings that come from the project's own code stay visible.
NOISE=(
    -Wno-nonportable-include-path
    -Wno-ignored-pragma-intrinsic
    -Wno-unknown-pragmas
    -Wno-pragma-pack
    -Wno-ignored-attributes
    -Wno-microsoft-anon-tag
    -Wno-deprecated-declarations
)

# Mirrors the Release|x64 ItemDefinitionGroup of the .vcxproj.
CXXFLAGS=(
    /nologo /c
    /std:c++17
    /O2
    /Oi                 # IntrinsicFunctions
    /Gy                 # FunctionLevelLinking
    /EHa                # ExceptionHandling=Async (the SEH __try/__except paths)
    /MT                 # static CRT: self-contained DLL, no VC++ redist
    /DNDEBUG /D_WINDOWS /D_USRDLL /DUNICODE /D_UNICODE
    /DFUFULAUNCHERUNLOCKERISLAND_EXPORTS
    "${NOISE[@]}"
    "${INCLUDES[@]}"
)
LDFLAGS=(
    /nologo /DLL
    /SUBSYSTEM:WINDOWS
    /MACHINE:X64
    /LIBPATH:"$CRT_LIB"
    /LIBPATH:"$SDK_LIB_UM"
    /LIBPATH:"$SDK_LIB_UCRT"
    /LIBPATH:"$STAGE"
)
if [[ "$DEBUG" == "1" ]]; then
    CXXFLAGS+=(/Z7 /Od)
    CXXFLAGS=("${CXXFLAGS[@]/\/O2/}")
    # LNK4099 is expected here: the MSVC static libraries ship without their
    # PDBs on Linux, so lld-link cannot merge their debug info.
    LDFLAGS+=(/DEBUG)
fi

# ----------------------------------------------------------------- MinHook --
# The vendored MinHook/libMinHook.x64.lib is built with /GL, so lld-link
# rejects it ("not a native COFF file. Recompile without /GL?"). Rebuild it
# from source into a plain COFF archive. Hooks.cpp references it by relative
# path via #pragma comment(lib, "MinHook/libMinHook.x64.lib"), hence $STAGE.
MINHOOK_LIB="$STAGE/MinHook/libMinHook.x64.lib"
if [[ -f "$MINHOOK_LIB" ]]; then
    log "MinHook: reusing $MINHOOK_LIB"
else
    if [[ ! -f "$MINHOOK_SRC/src/hook.c" ]]; then
        command -v git >/dev/null 2>&1 || die "git not found (needed to fetch MinHook)
    Or point MINHOOK_SRC at an existing checkout."

        # Never delete an override path or block unattended builds on stdin.
        [[ ! -e "$MINHOOK_SRC" ]] \
            || die "MINHOOK_SRC exists but is not a MinHook checkout: $MINHOOK_SRC"

        git clone --depth 1 --branch "$MINHOOK_TAG" "$MINHOOK_REPO" "$MINHOOK_SRC" >/dev/null 2>&1 \
            || die "failed to clone MinHook -- set MINHOOK_SRC to a local checkout"
    fi
    log "MinHook: building from $MINHOOK_SRC"
    mkdir -p "$BUILD/minhook_obj" "$STAGE/MinHook"
    # hde32.c is intentionally omitted: x64 trampolines use the 64-bit decoder.
    for unit in buffer hook trampoline; do
        run "$CLANG_CL" /nologo /c /O2 /MT /DNDEBUG "${NOISE[@]}" "${INCLUDES[@]}" \
            "-Fo$BUILD/minhook_obj/$unit.obj" "$MINHOOK_SRC/src/$unit.c"
    done
    run "$CLANG_CL" /nologo /c /O2 /MT /DNDEBUG "${NOISE[@]}" "${INCLUDES[@]}" \
        "-Fo$BUILD/minhook_obj/hde64.obj" "$MINHOOK_SRC/src/hde/hde64.c"
    pool_wait || die "MinHook compilation failed"

    "$LLVM_LIB" /nologo /OUT:"$MINHOOK_LIB" \
        "$BUILD/minhook_obj/buffer.obj" "$BUILD/minhook_obj/hook.obj" \
        "$BUILD/minhook_obj/trampoline.obj" "$BUILD/minhook_obj/hde64.obj"
fi

# -------------------------------------------------------------- __movsb shim --
# MinHook's trampoline.c calls __movsb directly under #ifdef _MSC_VER (clang-cl
# defines it). It is an MSVC intrinsic with no counterpart in the UCRT import
# libraries or libcmt, so supply a definition. -fno-builtin keeps clang from
# folding the copy loop back into a memcpy call.
SHIM_SRC="$BUILD/movsb_shim.c"
SHIM_OBJ="$OBJDIR/movsb_shim.obj"
if [[ ! -f "$SHIM_OBJ" || "$SHIM_SRC" -nt "$SHIM_OBJ" ]]; then
    log "Building __movsb shim"
    cat > "$SHIM_SRC" <<'EOF'
/* __movsb -- MSVC intrinsic, RCX=dst / RDX=src / R8=count on x64. */
void __movsb(unsigned char *dst, const unsigned char *src, unsigned long long count) {
    for (unsigned long long i = 0; i < count; ++i) dst[i] = src[i];
}
EOF
    run "$CLANG_CL" /nologo /c /O2 /MT /DNDEBUG -fno-builtin \
        "${NOISE[@]}" "${INCLUDES[@]}" "-Fo$SHIM_OBJ" "$SHIM_SRC"
    pool_wait || die "failed to build the __movsb shim"
fi

# ----------------------------------------------------------------- sources --
# Taken from the .vcxproj so the script cannot drift from the VS project.
mapfile -t SRCS < <(
    grep -o 'ClCompile Include="[^"]*"' "$VCXPROJ" \
        | sed 's/.*Include="//; s/"$//' \
        | tr '\\' '/'
)
((${#SRCS[@]})) || die "no <ClCompile> entries found in $VCXPROJ"

log "Compiling ${#SRCS[@]} translation units (JOBS=$JOBS)"
declare -a OBJS=()
for src in "${SRCS[@]}"; do
    [[ -f "$PROJ/$src" ]] || die "missing source: $PROJ/$src"
    obj="$OBJDIR/$(tr '/' '_' <<<"${src%.cpp}").obj"
    OBJS+=("$obj")
    run "$CLANG_CL" "${CXXFLAGS[@]}" "-Fo$obj" "$PROJ/$src"
done
pool_wait || die "compilation failed"

# --------------------------------------------------------------------- MASM --
# The x64 project also contains assembly that supplies symbols used by C++.
mapfile -t ASM_SRCS < <(
    grep -o 'MASM Include="[^"]*"' "$VCXPROJ" \
        | sed 's/.*Include="//; s/"$//' \
        | tr '\\' '/'
)
if ((${#ASM_SRCS[@]})); then
    log "Assembling ${#ASM_SRCS[@]} MASM sources"
    for src in "${ASM_SRCS[@]}"; do
        [[ -f "$PROJ/$src" ]] || die "missing assembly source: $PROJ/$src"
        obj="$OBJDIR/$(tr '/' '_' <<<"${src%.asm}").obj"
        OBJS+=("$obj")
        run "$LLVM_ML" --m64 /nologo /c "/Fo$obj" "$PROJ/$src"
    done
    pool_wait || die "assembly failed"
fi

# ----------------------------------------------------------------- resource --
# The .vcxproj feeds these two defines from the build timestamp.
BUILD_NUMBER="$(date +%y%m)"
REVISION_NUMBER="$(date +%d%H)"
RES_OBJ="$OBJDIR/resource.res"
log "Compiling Resource.rc (build $BUILD_NUMBER$REVISION_NUMBER)"
# /C 65001: the file is UTF-8, so the © in LegalCopyright is not 8-bit.
# /L 2052 (0x804): the zh-CN language the resource's Translation block declares.
"$LLVM_RC" /C 65001 /L 2052 \
    "/I$SDK_INC_UM" "/I$SDK_INC_SHARED" "/I$CRT_INC" \
    "/D" "BUILD_NUMBER=$BUILD_NUMBER" "/D" "REVISION_NUMBER=$REVISION_NUMBER" \
    "/FO" "$RES_OBJ" "$PROJ/Resource.rc" \
    || die "resource compilation failed"

# --------------------------------------------------------------------- link --
log "Linking $DLL_NAME"
OUT_DLL="$OUTDIR/$DLL_NAME"
"$LLD_LINK" "${LDFLAGS[@]}" "/OUT:$OUT_DLL" \
    "${OBJS[@]}" "$SHIM_OBJ" "$RES_OBJ" \
    "$MINHOOK_LIB" \
    user32.lib gdi32.lib d3d11.lib dxgi.lib wininet.lib \
    crypt32.lib advapi32.lib ole32.lib ws2_32.lib psapi.lib \
    || die "link failed"

# ------------------------------------------------------------------- verify --
summary="$(llvm-readobj --file-headers "$OUT_DLL" 2>/dev/null \
    | grep -E 'Machine:|Subsystem:' | tr -s ' ' | paste -sd' ' -)"
size="$(stat -c %s "$OUT_DLL")"
log "Built $OUT_DLL"
if command -v numfmt >/dev/null 2>&1; then
    info "$(numfmt --to=iec "$size") ($size bytes)"
else
    info "$size bytes"
fi
info "${summary:-<llvm-readobj unavailable>}"
info "imports: $(llvm-readobj --coff-imports "$OUT_DLL" 2>/dev/null \
    | sed -n 's/^ *Name: //p' | sort -u | paste -sd' ' -)"
