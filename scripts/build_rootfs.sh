#!/bin/bash
# Build a minimal arm64 Ubuntu rootfs for the Android/proot package.
#
# Downloading ubuntu-base is not an option on this device (mirrors deliver
# ~16 KB/s → 107 MB would take hours), so the tree is assembled from the packages
# *already installed* in the Ubuntu noble arm64 guest:
#
#   1. resolve a dependency closure that includes already-installed packages
#      (apt's install simulation does NOT, which is what produced an image with
#      no libc6 the first time) and maps virtual names like `awk` to a provider;
#   2. copy exactly the files each package owns — note that multi-arch packages
#      keep their list as `<pkg>:<arch>.list`;
#   3. carry a dpkg status restricted to that closure, so apt inside the image
#      does not believe packages whose files are absent are installed;
#   4. verify the tree by executing its python through *its own* ELF loader.
#
# Output: $OUT/rootfs.tar.gz + $OUT/MANIFEST (package list, reproducibility).
# Usage: scripts/build_rootfs.sh [OUT_DIR]
set -euo pipefail

OUT="${1:-/tmp/kimi-rootfs}"
SRC="${SRC:-/}"                      # the running Ubuntu noble arm64 tree
ARCH="${ARCH:-arm64}"
WORK="${WORK:-/tmp/rootfs-work}"

PKGS="${PKGS:-
  python3 python3-venv python3-pip
  libstdc++6 ca-certificates openssl curl wget
  apt ubuntu-keyring dpkg
  base-files base-passwd passwd login
  bash dash coreutils grep sed gawk findutils diffutils patch util-linux
  tar gzip bzip2 xz-utils zstd unzip zip file
  procps iproute2 hostname netbase netcat-openbsd iputils-ping
  git ripgrep jq nano less
  libnss3 libpam-modules libpam-runtime
  ncurses-term readline-common
}"

# Paths that exist only because of how *this* guest is bind-mounted; they must
# never land in the image (/tmp here holds gigabytes of unrelated work).
EXCLUDES=(
  '^/tmp' '^/home' '^/proc' '^/sys' '^/dev' '^/run' '^/workspace'
  '^/opt/coomi-dev' '^/var/log' '^/var/cache/apt/archives/.*\.deb$'
  '^/usr/share/doc' '^/usr/share/man' '^/usr/share/locale' '^/usr/share/i18n'
  '^/var/lib/dpkg' '^/var/cache'
)

rm -rf "$WORK" "$OUT"
mkdir -p "$WORK/rootfs" "$OUT"
printf '%s\n' "${EXCLUDES[@]}" > "$WORK/excludes.txt"

echo "==> resolving dependency closure (includes already-installed packages)"
apt-cache depends --recurse --no-recommends --no-suggests --no-conflicts \
    --no-breaks --no-replaces --no-enhances $PKGS 2>/dev/null \
  | grep -E '^[0-9a-zA-Z][0-9a-zA-Z+.-]*$' | sort -u > "$WORK/raw.txt"
: > "$WORK/closure.txt"
while read -r n; do
  if [ -f "$SRC/var/lib/dpkg/info/$n.list" ] || ls "$SRC/var/lib/dpkg/info/$n:$ARCH.list" >/dev/null 2>&1; then
    echo "$n" >> "$WORK/closure.txt"
  else
    r="$(apt-cache showpkg "$n" 2>/dev/null | awk '/^Reverse Provides:/{f=1;next} f&&NF{print $1;exit}')"
    [ -n "$r" ] && echo "$r" >> "$WORK/closure.txt" && echo "    virtual: $n -> $r"
  fi
done < "$WORK/raw.txt"
# requested packages come along even if the recurse output dropped them
for n in $PKGS; do echo "$n"; done >> "$WORK/closure.txt"
sort -u "$WORK/closure.txt" | grep -v '^$' > "$WORK/closure.sorted" && mv "$WORK/closure.sorted" "$WORK/closure.txt"
echo "    packages: $(wc -l < "$WORK/closure.txt")"

list_of() {
  local pkg="$1"
  [ -f "$SRC/var/lib/dpkg/info/$pkg.list" ] && { echo "$SRC/var/lib/dpkg/info/$pkg.list"; return; }
  ls "$SRC/var/lib/dpkg/info/$pkg:$ARCH.list" 2>/dev/null | head -1
}

echo "==> copying package files"
MISSING=0
while read -r pkg; do
  f="$(list_of "$pkg" || true)"
  if [ -z "$f" ]; then MISSING=$((MISSING+1)); continue; fi
  grep -v -f "$WORK/excludes.txt" -e '^$' "$f" > "$WORK/files.txt" || true
  [ -s "$WORK/files.txt" ] || continue
  # --no-recursion is essential: the list is exact, and without it tar descends
  # into each directory and swallows the whole mounted filesystem underneath.
  ( cd "$SRC" && tar --no-recursion -cf - -T "$WORK/files.txt" 2>/dev/null ) \
    | tar -C "$WORK/rootfs" -xf - 2>/dev/null
done < "$WORK/closure.txt"
echo "    no file list for $MISSING package(s) (virtual/skip)"
[ -s "$SRC/var/lib/dpkg/info/libc6:$ARCH.list" ] || { echo "FATAL: libc6 not copied"; exit 1; }

R="$WORK/rootfs"
echo "==> dpkg database, restricted to the closure"
mkdir -p "$R/var/lib/dpkg/info" "$R/var/lib/dpkg/updates" "$R/var/lib/dpkg/triggers" \
         "$R/var/cache/apt/archives/partial" "$R/var/lib/apt/lists/partial" "$R/run/lock"
awk -v C="$WORK/closure.txt" '
  BEGIN { while ((getline p < C) > 0) keep[p]=1 }
  /^Package: / { hold = ($2 in keep) ? 1 : 0 }
  { if (hold) print }
  /^$/ { if (hold) print; hold = 0 }
' "$SRC/var/lib/dpkg/status" > "$R/var/lib/dpkg/status"
echo "    status entries: $(grep -c '^Package: ' "$R/var/lib/dpkg/status")"
while read -r pkg; do
  for ext in list md5sums conffiles shlibs; do
    for cand in "$SRC/var/lib/dpkg/info/$pkg.$ext" "$SRC/var/lib/dpkg/info/$pkg:$ARCH.$ext"; do
      [ -f "$cand" ] && cp "$cand" "$R/var/lib/dpkg/info/" 2>/dev/null
    done
  done
done < "$WORK/closure.txt"
cp "$SRC/var/lib/dpkg/diversions" "$R/var/lib/dpkg/diversions" 2>/dev/null || true
[ -f "$SRC/etc/ld.so.cache" ] && cp "$SRC/etc/ld.so.cache" "$R/etc/ld.so.cache"

echo "==> finalising the tree"
mkdir -p "$R/dev" "$R/proc" "$R/sys" "$R/tmp" "$R/root" "$R/var/tmp" "$R/run" "$R/etc"
chmod 1777 "$R/tmp" "$R/var/tmp"; chmod 700 "$R/root"
for d in bin sbin lib libexec; do [ -e "$R/$d" ] || ln -s "usr/$d" "$R/$d"; done
[ -e "$R/lib64" ] || ln -sf usr/lib64 "$R/lib64" 2>/dev/null || true
printf 'nameserver 1.1.1.1\nnameserver 8.8.8.8\n' > "$R/etc/resolv.conf"
[ -f "$R/etc/hosts" ] || printf '127.0.0.1 localhost\n' > "$R/etc/hosts"

# Files generated by maintainer scripts belong to no package, so the .list-based
# copy above never brings them in -- and their absence is fatal, not cosmetic:
# without /etc/ssl/certs/ca-certificates.crt the SSL_CERT_FILE we set in the
# guest points at nothing, Python builds a trust store with 0 certificates and
# Go (Kimi Code) reads the same variable, so every HTTPS request fails with
# CERTIFICATE_VERIFY_FAILED. passwd/group/nsswitch.conf back getpwuid()/DNS.
for f in etc/ssl/certs/ca-certificates.crt etc/passwd etc/group etc/nsswitch.conf; do
  if [ ! -e "$R/$f" ] && [ -e "$SRC/$f" ]; then
    mkdir -p "$(dirname "$R/$f")"
    cp -a "$SRC/$f" "$R/$f"
    echo "    + generated $f"
  fi
done
# /etc/localtime is a symlink into zoneinfo, which the tzdata list does own.
if [ ! -e "$R/etc/localtime" ] && [ ! -L "$R/etc/localtime" ] && [ -e "$SRC/usr/share/zoneinfo/Etc/UTC" ]; then
  mkdir -p "$R/usr/share/zoneinfo/Etc"
  cp -a "$SRC/usr/share/zoneinfo/Etc/UTC" "$R/usr/share/zoneinfo/Etc/UTC" 2>/dev/null || true
  ln -sf /usr/share/zoneinfo/Etc/UTC "$R/etc/localtime"
  echo "    + generated etc/localtime"
fi
[ -s "$R/etc/ssl/certs/ca-certificates.crt" ] || { echo "FATAL: no CA bundle in image"; exit 1; }
: > "$R/etc/ld.so.preload" 2>/dev/null || true
rm -f "$R/etc/ssh/ssh_host_"* 2>/dev/null || true

echo "==> smoke test: run the image's python through the image's ELF loader"
LD="$(ls "$R"/lib/aarch64-linux-gnu/ld-linux-aarch64.so.1 2>/dev/null || echo '')"
[ -n "$LD" ] || { echo "FATAL: no dynamic linker in image"; exit 1; }
LIBS="$R/usr/lib/aarch64-linux-gnu:$R/lib/aarch64-linux-gnu:$R/usr/lib:$R/lib"
"$LD" --inhibit-cache --library-path "$LIBS" "$R/usr/bin/python3.12" - <<'PY'
import sys, ssl, sqlite3, json, ctypes, ensurepip, zlib, hashlib, subprocess
print("python:", sys.version.split()[0], "stdlib ok")
PY
"$LD" --inhibit-cache --library-path "$LIBS" "$R/usr/bin/curl" --version | head -1
"$LD" --inhibit-cache --library-path "$LIBS" "$R/usr/bin/git" --version 2>/dev/null | head -1
"$LD" --inhibit-cache --library-path "$LIBS" "$R/usr/bin/dash" -c 'echo "dash ok"'
# any unresolved soname at all would make the real (proot) run fail later
for b in python3.12 curl git dash apt-get dpkg tar gzip; do
  p="$R/usr/bin/$b"; [ -x "$p" ] || p="$(ls "$R"/usr/bin/"$b"* 2>/dev/null | head -1)"
  [ -n "$p" ] && [ -f "$p" ] || continue
  miss="$("$LD" --inhibit-cache --library-path "$LIBS" --list "$p" 2>&1 | grep -c 'not found' || true)"
  echo "    $b: unresolved=$miss"
done

echo "==> tar"
tar -C "$R" --format=gnu -czf "$OUT/rootfs.tar.gz" .
cp "$WORK/closure.txt" "$OUT/MANIFEST"
du -sh "$R" | sed 's/^/extracted: /'
ls -la "$OUT/rootfs.tar.gz"
echo "OK $OUT/rootfs.tar.gz"
