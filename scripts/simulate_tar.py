#!/usr/bin/env python3
"""Faithful port of TarReader/TarExtractor (the Kotlin sources) used to reason
about the archive stream without a JVM on the device.

Run:  python3 scripts/simulate_tar.py [path/to/fixture.tar.gz]

Exits non-zero if the simulated extraction disagrees with what the Kotlin code
should produce, so it doubles as a cheap regression probe for the tar reader.
"""
import gzip
import os
import sys
import tempfile

BLOCK = 512
FILE, FILE_ALT, DIR, SYMLINK, HARDLINK, LONG, PAX, PAXG = '0', '\x00', '5', '2', '1', 'L', 'x', 'g'


class Reader:
    def __init__(self, raw: bytes):
        self.raw = raw
        self.pos = 0
        self.pending_name = None
        self.pending_size = None

    def read_fully(self, n: int) -> bytes:
        b = self.raw[self.pos:self.pos + n]
        self.pos += len(b)
        return b

    def skip(self, n: int):
        left = n
        while left > 0:
            # InputStream.skip() semantics: always advance, like the Kotlin loop
            self.pos += 1
            left -= 1

    def read_header(self):
        b = self.read_fully(BLOCK)
        if len(b) < BLOCK:
            return None
        if b == b'\x00' * BLOCK:
            return None
        name = b[0:100].split(b'\x00')[0].decode('utf-8', 'replace').strip()
        size_raw = b[124:136].split(b'\x00')[0].decode('latin-1').strip()
        try:
            size = int(size_raw, 8)
        except ValueError:
            size = -1
        return {
            'name': name,
            'mode': int(b[100:108].split(b'\x00')[0].decode('latin-1').strip() or '0', 8),
            'size': max(size, 0),
            'type': chr(b[156]),
            'link': b[157:257].split(b'\x00')[0].decode('utf-8', 'replace').strip(),
        }

    def read_payload_string(self, size: int) -> str:
        cap = min(size, 1 << 20)
        out = self.read_fully(cap)
        self.skip(((size + 511) // 512) * 512 - cap)     # padding only
        return out.decode('utf-8', 'replace').strip('\x00').strip(' ').strip('\n')

    def skip_blocks(self, size: int):
        self.skip(((size + 511) // 512) * 512)

    def copy_payload(self, size: int) -> bytes:
        data = self.read_fully(size)
        self.skip(((size + 511) // 512) * 512 - size)
        return data


def apply_pax(payload: str):
    out, i = {}, 0
    while i < len(payload):
        sp = payload.find(' ', i)
        if sp < 0:
            break
        try:
            length = int(payload[i:sp].strip())
        except ValueError:
            break
        if length <= 0:
            break
        record = payload[sp + 1:min(len(payload), i + length)]
        sep = record.find('=')
        if sep > 0:
            out[record[:sep]] = record[sep + 1:].rstrip('\n')
        i += length
    return out


def resolve(dest, entry):
    clean = entry
    if clean.startswith('./'):
        clean = clean[2:]
    elif clean.startswith('/'):
        clean = clean.lstrip('/')
    if clean == '' or clean == '.':
        return dest
    base = os.path.realpath(dest)
    cand = os.path.normpath(os.path.join(dest, clean))
    return cand if cand == base or cand.startswith(base + os.sep) else None


def extract(raw: bytes, dest: str):
    tar = Reader(raw)
    files = dirs = links = 0
    skipped = []
    written = {}
    while True:
        hdr = tar.read_header()
        if hdr is None:
            break
        if tar.pending_name is not None:
            hdr['name'] = tar.pending_name
        if tar.pending_size is not None:
            hdr['size'] = tar.pending_size
        tar.pending_name = tar.pending_size = None

        if hdr['type'] in (LONG, PAX):
            payload = tar.read_payload_string(hdr['size'])
            if hdr['type'] == LONG:
                tar.pending_name = payload
            else:
                pax = apply_pax(payload)
                if 'path' in pax:
                    tar.pending_name = pax['path']
                if 'size' in pax:
                    tar.pending_size = int(pax['size'])
            continue

        name = hdr['name']
        target = resolve(dest, name)
        if target is None:
            tar.skip_blocks(hdr['size'])
            skipped.append(f"escapes destination: {name}")
            continue

        t = hdr['type']
        if t == DIR:
            tar.skip_blocks(hdr['size'])
            os.makedirs(target, exist_ok=True)
            dirs += 1
        elif t in (FILE, FILE_ALT):
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if os.path.isdir(target):
                tar.skip_blocks(hdr['size'])
                skipped.append(f"regular file over a directory: {name}")
                continue
            data = tar.copy_payload(hdr['size'])
            with open(target, 'wb') as fh:
                fh.write(data)
            written[os.path.relpath(target, dest)] = data
            files += 1
        elif t == SYMLINK:
            tar.skip_blocks(hdr['size'])
            try:
                if os.path.lexists(target):
                    os.remove(target)
                os.symlink(hdr['link'], target)
                links += 1
            except OSError:
                skipped.append(f"symlink {name} -> {hdr['link']} (filesystem refused)")
        elif t == HARDLINK:
            tar.skip_blocks(hdr['size'])
            src = resolve(dest, hdr['link'])
            if src and os.path.isfile(src):
                with open(src, 'rb') as fh:
                    blob = fh.read()
                with open(target, 'wb') as fh:
                    fh.write(blob)
                files += 1
            else:
                skipped.append(f"hardlink {name} -> {hdr['link']}")
        else:
            tar.skip_blocks(hdr['size'])
            if name:
                skipped.append(f"type {t!r} {name}")
    return files, dirs, links, skipped, written


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else \
        os.path.join(os.path.dirname(__file__), '..', 'android', 'app', 'src', 'test', 'resources', 'fixture.tar.gz')
    blob = gzip.decompress(open(path, 'rb').read())
    LONG_NAME = ("usr/lib/deep/nested/dir/this_filename_is_definitely_longer_than_one_hundred_"
                 "bytes_and_forces_a_gnu_long_name_header_record_in_the_tar_stream.txt")
    with tempfile.TemporaryDirectory() as dest:
        files, dirs, links, skipped, written = extract(blob, dest)
        print(f"files={files} dirs={dirs} links={links} skipped={skipped}")
        ok = True
        checks = {
            'usr/bin/echo.sh': b'hello-file\n',
            LONG_NAME: b'long-name-content',
            'etc/plain.txt': b'x',
        }
        for rel, want in checks.items():
            got = written.get(rel)
            if got != want:
                print(f"  FAIL {rel}: got {got!r} want {want!r}")
                ok = False
        if len(written.get('payload.bin', b'')) != 333:
            print(f"  FAIL payload.bin size {len(written.get('payload.bin', b''))}")
            ok = False
        bad_skips = [s for s in skipped if not s.startswith('symlink')]
        if bad_skips:
            print(f"  FAIL unexpected skips: {bad_skips}")
            ok = False
        print("SIMULATION PASS" if ok else "SIMULATION FAIL")
        return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
