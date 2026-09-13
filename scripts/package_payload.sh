#!/usr/bin/env bash
# Assemble the payload the Android package ships, into android/app/assets/.
#
#   rootfs.tar.gz  minimal arm64 Ubuntu built by scripts/build_rootfs.sh. Must be
#                  built on arm64 Ubuntu — i.e. this phone — because Debian
#                  mirrors here deliver ~16 KB/s while the local package DB has
#                  everything we need already.
#   deps.tar.gz    the agent's 5 runtime deps, installed with `--target` so the
#                  tree carries no absolute venv paths; it goes on PYTHONPATH.
#   app.tar.gz     agent source: kimi_agent/ web/ config/ run.sh.
#   jniLibs/.../libproot.so   the Android(bionic) proot build. Executable files
#                  ship as jniLibs because Android refuses execve() inside the
#                  app data dir for targets >= 29.
#
# Kimi Code itself is NOT packaged here: at 170 MB it exceeds GitHub's 100 MB
# per-file limit, so the CI job downloads it into the build (checksum verified)
# and only then assembles the APK.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJ="$(cd "$HERE/.." && pwd)"
AND="$PROJ/android/app"
ASSETS="$AND/assets"
STAGE="${STAGE:-/tmp/kimi-payload}"
ROOTFS_SRC="${ROOTFS_SRC:-/tmp/kimi-rootfs/rootfs.tar.gz}"
PROOT_SRC="${PROOT_SRC:-/workspace/.coomi/runtime-v2/downloads/proot-host-arm64.tar.gz}"

# The asset names end in .bin for a non-obvious reason: Android's AssetManager
# treats a ".gz" suffix specially — it strips it from the visible name and
# gunzips transparently on open. A 67 MB rootfs.tar.gz would therefore appear as
# "rootfs.tar" and arrive decompressed (or worse, half-decompressed), which is
# what this build actually shipped before being fixed. ".bin" is inert.
mkdir -p "$ASSETS" "$STAGE"

echo "==> rootfs"
[ -s "$ROOTFS_SRC" ] || "$HERE/build_rootfs.sh" "$(dirname "$ROOTFS_SRC")"
install -m 644 "$ROOTFS_SRC" "$ASSETS/rootfs.tar.gz.bin"

echo "==> python deps (--target keeps the tree relocatable)"
rm -rf "$STAGE/deps"; mkdir -p "$STAGE/deps"
uv pip install --python /usr/bin/python3.12 --target "$STAGE/deps" --no-cache \
    -r "$PROJ/requirements-runtime.txt" >/dev/null
rm -rf "$STAGE/deps/pip" "$STAGE/deps/setuptools" "$STAGE/deps/wheel" \
       "$STAGE/deps/pkg_resources" "$STAGE/deps/_distutils_hack" 2>/dev/null || true
find "$STAGE/deps" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
tar -C "$STAGE" --format=gnu -czf "$ASSETS/deps.tar.gz.bin" deps

echo "==> agent source"
find "$PROJ" -maxdepth 3 -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
rm -rf "$PROJ/.pytest_cache"
tar -C "$PROJ" --format=gnu -czf "$ASSETS/app.tar.gz.bin" \
  --exclude='__pycache__' --exclude='*.pyc' \
  kimi_agent web config run.sh requirements-runtime.txt

echo "==> proot (bionic build, execable from jniLibs)"
JNI="$AND/jniLibs/arm64-v8a"
mkdir -p "$JNI"
if [ ! -s "$JNI/libproot.so" ]; then
  [ -f "$PROOT_SRC" ] || { echo "FATAL: $PROOT_SRC missing"; exit 1; }
  rm -rf "$STAGE/proot"; mkdir -p "$STAGE/proot"
  tar -C "$STAGE/proot" -xf "$PROOT_SRC"
  install -m 755 "$STAGE/proot/bin/proot" "$JNI/libproot.so"
fi
file "$JNI/libproot.so" | cut -c1-100

echo "==> result (kimi/kimi is fetched by CI, not stored here)"
( cd "$AND" && du -sh assets assets/* jniLibs/arm64-v8a/libproot.so ) | sed 's/^/    /'
