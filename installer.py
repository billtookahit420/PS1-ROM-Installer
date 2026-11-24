#!/usr/bin/env python3
"""Recomp ROM Installer - core logic (UI-free).

Scans a folder of extracted recomp game packages, finds the ones that
are waiting on a player-supplied ROM, works out the exact ROM each one
expects from the identity data recorded in the package itself, and
fills in ONLY exact hash matches from user-chosen ROM source folders
(loose files and .zip archives alike).

Hard rules (settled with Duane, 2026-11-03):
  * Never fill by filename alone. A package with no usable hash
    identity in it is reported as "no identity data" and left alone.
  * A different file already sitting where the ROM would go is a
    conflict - it is never overwritten unless the user explicitly
    marks that package for replacement.
  * Installs are atomic: stream to a temp file in the destination
    folder, verify its hash, then os.replace() into place. An
    interrupted install never leaves a partial file at the destination.
  * Zip sources are matched in place: the central-directory CRC32 is
    a free pre-filter and only candidate entries are stream-hashed;
    whole archives are never extracted.
  * Scanning is two-phase (v1.0.1 fix): scan_packages only lists
    packages - it never reads a game executable, so a ~700-game
    library scans in seconds. The exe-compiled identities of the
    NES/GB/SNES packages are extracted later, per package, inside
    run_install (streamed in chunks, cached in the app's cache/
    folder), and in-folder ROM files of those packages are only
    hash-verified at that point.

Package ground truth (from the converter apps' own packaging code and
shipped READMEs, inspected 2026-11-04):
  NES    README "(NESRecomp, cycle backend)"; no hash printed in the
         README - the launcher's known SHA-256 (of the ROM body after
         its iNES header, the way recomp-ui hashes) is compiled into
         the executable as an ASCII string.
  GB     README "(GBRecomp)"; same story - the executable carries the
         known SHA-256 (whole file) as an ASCII string; the link-test
         build's README also names the exact dump file.
  SNES   README "(SNESRecomp)"; executable carries the known SHA-256
         (body after a 512-byte copier header when one is present).
  GBA    game.toml [identity]/[rom] sha1 + md5 and [program] size;
         packages ship per-OS payload subfolders (linux/ windows/),
         each payload folder is its own install target. (The game
         also needs a player BIOS - that is the converter's own
         first-run prompt, not this tool's job.)
  N64    Zelda64Recompiled package; the converter supports exactly one
         title (Majora's Mask (USA), byte-order-normalized SHA-1
         d6133ace5afaa0882cf214cf88daba39e266c078, 32 MiB). The package
         README names that exact title; the hash comes from the
         converter's supported-title table.
  VB     README/play.sh print CRC32 + SHA-256 + exact size.
  SMS/GG README/play.sh print CRC32 + exact size (the pair the spec
         allows: CRC32 + exact size).
  PSX    game.toml [game] id + disc_serials name the expected disc
         serial(s); packages ship WITHOUT their disc images, so the
         installer fills them from complete .cue/.bin sets whose
         on-disc boot-EXE serial matches the package's - a set is
         verified by the serial read off the disc image itself,
         never by filename alone.
  Genesis Its launcher keeps the ROM external (no in-package install
         destination in the settled scope) - never flagged.

Cross-platform: pathlib throughout, os.replace for the atomic rename,
read-only detection via stat mode bits (covers the Windows read-only
attribute as well as POSIX permission bits), no POSIX-only calls.
"""

import hashlib
import json
import os
import re
import stat as stat_mod
import sys
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path

APP_NAME = "Recomp ROM Installer"
TEMP_PREFIX = ".recomp-rom-installer-"
TEMP_SUFFIX = ".tmp"
CHUNK = 1 << 20

SYSTEMS = {
    "nes": ("NES", {".nes"}),
    "gb": ("Game Boy / Color", {".gb", ".gbc", ".sgb"}),
    "snes": ("SNES", {".sfc", ".smc", ".fig", ".swc"}),
    "gba": ("Game Boy Advance", {".gba", ".agb"}),
    "n64": ("Nintendo 64", {".z64", ".n64", ".v64"}),
    "vb": ("Virtual Boy", {".vb"}),
    "sms": ("Master System", {".sms"}),
    "gg": ("Game Gear", {".gg"}),
    "psx": ("PlayStation", {".cue"}),
}
ALL_ROM_EXTS = set().union(*(v[1] for v in SYSTEMS.values()))
# Systems whose launcher persists the picked ROM path in rom.cfg
# beside the executable; after a fill we point it at the installed ROM.
ROM_CFG_SYSTEMS = {"nes", "gb", "snes", "gba"}
# Systems whose only identity is the known-ROM SHA-256 compiled into
# the game executable. Reading executables is the expensive part of
# this tool, so for these systems the scan defers identity entirely:
# extraction happens per package at install time (see run_install),
# never during scan_packages.
EXE_IDENTITY_SYSTEMS = {"nes", "gb", "snes"}

# The one title the N64 converter supports (its SUPPORTED table).
N64_PROFILES = {
    "majora's mask": {
        "sha1_norm": "d6133ace5afaa0882cf214cf88daba39e266c078",
        "size": 33554432,
    },
}

_HEX64 = re.compile(rb"(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])")
_HEX40 = re.compile(rb"(?<![0-9a-fA-F])[0-9a-fA-F]{40}(?![0-9a-fA-F])")


class Cancelled(Exception):
    pass


def _check_cancel(cancel):
    if cancel is not None and cancel():
        raise Cancelled("cancelled")


# ----------------------------------------------------------------------
# Hashing
# ----------------------------------------------------------------------
@dataclass
class Hashes:
    size: int = 0
    crc32: int = 0
    md5: str = ""
    sha1: str = ""
    sha256: str = ""
    sha1_payload: str = ""
    sha256_payload: str = ""
    sha1_norm: str = ""          # N64 byte-order-normalized SHA-1


_N64_MAGICS = {
    b"\x80\x37\x12\x40": "z64",
    b"\x37\x80\x40\x12": "v64",
    b"\x40\x12\x37\x80": "n64",
}


def n64_format(head4):
    return _N64_MAGICS.get(bytes(head4))


def n64_normalize(data, fmt):
    if fmt == "z64":
        return data
    if fmt == "v64":
        out = bytearray(len(data))
        out[0::2] = data[1::2]
        out[1::2] = data[0::2]
        return bytes(out)
    out = bytearray(data)  # n64: reverse each 32-bit word
    for i in range(0, len(data) - 3, 4):
        out[i:i + 4] = data[i:i + 4][::-1]
    return bytes(out)


def _n64_transform(data, fmt):
    """Byte-order transform for whole 4-byte-aligned blocks."""
    if fmt == "v64":
        out = bytearray(len(data))
        out[0::2] = data[1::2]
        out[1::2] = data[0::2]
        return bytes(out)
    if fmt == "n64":
        out = bytearray(data)
        for i in range(0, len(data) - 3, 4):
            out[i:i + 4] = data[i:i + 4][::-1]
        return bytes(out)
    return data


def hash_stream(chunks, size_hint=0, want_n64_norm=False, cancel=None):
    """Hash a byte stream (an iterable of bytes blocks) once, computing
    every digest the matcher can need.

    Payload digests follow recomp-ui's launcher convention: skip a
    512-byte copier header when size % 1024 == 512, else skip a 16-byte
    iNES header when one is present, else hash the whole file. The
    header decision is made from the first 512 bytes (or the whole
    stream when it is shorter than that).
    """
    crc = 0
    md5 = hashlib.md5()
    sha1 = hashlib.sha1()
    sha256 = hashlib.sha256()
    p_sha1 = hashlib.sha1()
    p_sha256 = hashlib.sha256()
    norm = hashlib.sha1() if want_n64_norm else None
    total = 0
    fmt = None
    prefix = bytearray()
    decided = False
    norm_carry = b""

    def feed_payload(data):
        if data:
            p_sha1.update(data)
            p_sha256.update(data)

    def feed_norm(data):
        nonlocal norm_carry
        if norm is None or not data:
            return
        buf = norm_carry + data
        whole = len(buf) - (len(buf) % 4)
        if whole:
            norm.update(_n64_transform(buf[:whole], fmt))
        norm_carry = buf[whole:]

    for chunk in chunks:
        _check_cancel(cancel)
        if not chunk:
            continue
        total += len(chunk)
        crc = zlib.crc32(chunk, crc)
        md5.update(chunk)
        sha1.update(chunk)
        sha256.update(chunk)
        if not decided:
            prefix += chunk
            if len(prefix) >= 512:
                fmt = n64_format(prefix[:4]) if want_n64_norm else None
                if size_hint and size_hint % 1024 == 512:
                    skip = 512
                elif bytes(prefix[:4]) == b"NES\x1a":
                    skip = 16
                else:
                    skip = 0
                decided = True
                feed_payload(bytes(prefix[skip:]))
                feed_norm(bytes(prefix))
                prefix = bytearray()
            continue
        feed_payload(chunk)
        feed_norm(chunk)
    if not decided:
        data = bytes(prefix)
        fmt = n64_format(data[:4]) if want_n64_norm else None
        if size_hint and size_hint % 1024 == 512:
            skip = 512
        elif data[:4] == b"NES\x1a":
            skip = 16
        else:
            skip = 0
        feed_payload(data[skip:])
        feed_norm(data)
    if norm is not None and norm_carry:
        norm.update(_n64_transform(norm_carry, fmt))
    return Hashes(
        size=total, crc32=crc & 0xFFFFFFFF,
        md5=md5.hexdigest(), sha1=sha1.hexdigest(),
        sha256=sha256.hexdigest(),
        sha1_payload=p_sha1.hexdigest(), sha256_payload=p_sha256.hexdigest(),
        sha1_norm=norm.hexdigest() if norm is not None else "")


def _file_chunks(f, cancel=None):
    while True:
        _check_cancel(cancel)
        chunk = f.read(CHUNK)
        if not chunk:
            return
        yield chunk


def hash_file(path, want_n64_norm=False, cancel=None):
    with open(path, "rb") as f:
        return hash_stream(_file_chunks(f, cancel),
                           size_hint=_safe_size(path),
                           want_n64_norm=want_n64_norm, cancel=cancel)


def _safe_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


# ----------------------------------------------------------------------
# Identity
# ----------------------------------------------------------------------
@dataclass
class Identity:
    size: int = None
    crc32: int = None
    md5: str = ""
    sha1: str = ""
    sha256: str = ""
    sha1_norm: str = ""
    sha256_set: set = field(default_factory=set)   # from executable strings
    sha1_set: set = field(default_factory=set)     # from executable strings
    sources: list = field(default_factory=list)    # where each fact came from

    def strong(self):
        return bool(self.md5 or self.sha1 or self.sha256 or self.sha1_norm
                    or self.sha256_set or self.sha1_set
                    or (self.crc32 is not None and self.size))

    def describe(self):
        parts = []
        if self.crc32 is not None:
            parts.append(f"CRC32 {self.crc32:08X}")
        if self.size:
            parts.append(f"{self.size} bytes")
        if self.md5:
            parts.append(f"MD5 {self.md5}")
        if self.sha1:
            parts.append(f"SHA-1 {self.sha1}")
        if self.sha256:
            parts.append(f"SHA-256 {self.sha256}")
        if self.sha1_norm:
            parts.append(f"normalized SHA-1 {self.sha1_norm}")
        if self.sha256_set:
            parts.append(f"SHA-256 (executable) {sorted(self.sha256_set)[0]}")
        if self.sha1_set:
            parts.append(f"SHA-1 (executable) {sorted(self.sha1_set)[0]}")
        return " + ".join(parts) if parts else "(none)"


def identity_matches(ident, hashes):
    """True only when EVERY identity fact the package recorded agrees
    with the candidate. Any single mismatch rejects the candidate."""
    if ident.size is not None and hashes.size != ident.size:
        return False
    if ident.crc32 is not None and hashes.crc32 != ident.crc32:
        return False
    if ident.md5 and hashes.md5 != ident.md5:
        return False
    if ident.sha1 and hashes.sha1 != ident.sha1 \
            and hashes.sha1_payload != ident.sha1:
        return False
    if ident.sha256 and hashes.sha256 != ident.sha256 \
            and hashes.sha256_payload != ident.sha256:
        return False
    if ident.sha1_norm and hashes.sha1_norm != ident.sha1_norm:
        return False
    if ident.sha256_set and hashes.sha256 not in ident.sha256_set \
            and hashes.sha256_payload not in ident.sha256_set:
        return False
    if ident.sha1_set and hashes.sha1 not in ident.sha1_set \
            and hashes.sha1_payload not in ident.sha1_set:
        return False
    return True


# ----------------------------------------------------------------------
# Package scanning
# ----------------------------------------------------------------------
@dataclass
class PackageUnit:
    folder: Path
    title: str
    system: str
    identity: Identity
    status: str = "ready"        # ready/no_identity/already_present/
    #                              conflict/filled/not_found/error/cancelled
    #                              waiting/present_unverified (deferred
    #                              exe-identity systems, pre-install)
    detail: str = ""
    exe_name: str = ""
    exe_names: list = field(default_factory=list)
    identity_pending: bool = False   # exe-hash identity not yet extracted
    rom_cfg: str = ""            # path text recorded in rom.cfg, if any
    dest_name: str = ""          # file name chosen at install time
    source_desc: str = ""
    psx_serials: list = field(default_factory=list)  # expected disc serials
    psx_cue_names: list = field(default_factory=list)  # [game] discs hints
    disc_cfg: str = ""           # path text recorded in disc.cfg, if any

    @property
    def key(self):
        return str(self.folder)

    def expects_text(self):
        if self.system == "psx" and self.psx_serials:
            return "disc serial(s): " + ", ".join(self.psx_serials)
        if self.identity.strong():
            return self.identity.describe()
        if self.identity_pending:
            return "identity in executable (checked at install)"
        return "no identity data in package"


def _read_text(path, limit=40000):
    try:
        return Path(path).read_text(errors="replace")[:limit]
    except OSError:
        return ""


def _first_line(text):
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _is_elf(path):
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"\x7fELF"
    except OSError:
        return False


def _is_pe(path):
    try:
        with open(path, "rb") as f:
            return f.read(2) == b"MZ"
    except OSError:
        return False


def _toml_scalar(text, section, key):
    in_section = section is None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            in_section = (s.strip("[]").strip() == section)
            continue
        if in_section:
            m = re.match(re.escape(key) + r'\s*=\s*"([^"]*)"', s)
            if m:
                return m.group(1)
            m = re.match(re.escape(key) + r"\s*=\s*(\S+)", s)
            if m:
                return m.group(1)
    return None


def _toml_string_list(text, section, key):
    """All quoted strings in a (possibly multi-line) TOML array
    assigned to key in section, e.g. disc_serials = [ "A", "B" ]."""
    in_section = section is None
    lines = text.splitlines()
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            in_section = (s.strip("[]").strip() == section)
            continue
        if not in_section:
            continue
        m = re.match(re.escape(key) + r"\s*=\s*\[(.*)", s)
        if m:
            block = m.group(1)
            j = i
            while "]" not in block and j + 1 < len(lines):
                j += 1
                block += "\n" + lines[j]
            return re.findall(r'"([^"]*)"', block.split("]", 1)[0])
    return []


def _parse_int(text):
    if text is None:
        return None
    try:
        return int(str(text).strip().strip('"'), 0)
    except ValueError:
        return None


def _clean_hash(value, length):
    if not value:
        return ""
    v = str(value).strip().lower()
    if len(v) != length or not re.fullmatch(r"[0-9a-f]+", v):
        return ""
    if set(v) == {"0"}:
        return ""            # placeholder zeros are not identity data
    return v


_SIZE_BYTES_RE = re.compile(r"(\d{4,10})\s*bytes")
_SIZE_KIB_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(KiB|MiB)\b")


def _text_identity(text, source_name):
    """Pull labelled hash/size facts out of README / play-script text."""
    ident = Identity()
    m = re.search(r"CRC32[:\s]*\(?\s*([0-9A-Fa-f]{8})\b", text)
    if not m:
        m = re.search(r"expected\s+([0-9A-Fa-f]{8})\b", text)
    if m:
        ident.crc32 = int(m.group(1), 16)
        ident.sources.append(f"CRC32 from {source_name}")
    m = re.search(r"SHA-256[:\s]*([0-9a-fA-F]{64})\b", text)
    if m and _clean_hash(m.group(1), 64):
        ident.sha256 = m.group(1).lower()
        ident.sources.append(f"SHA-256 from {source_name}")
    m = re.search(r"SHA-1[:\s]*([0-9a-fA-F]{40})\b", text)
    if m and _clean_hash(m.group(1), 40):
        ident.sha1 = m.group(1).lower()
        ident.sources.append(f"SHA-1 from {source_name}")
    m = re.search(r"MD5[:\s]*([0-9a-fA-F]{32})\b", text)
    if m and _clean_hash(m.group(1), 32):
        ident.md5 = m.group(1).lower()
        ident.sources.append(f"MD5 from {source_name}")
    m = _SIZE_BYTES_RE.search(text)
    if m:
        ident.size = int(m.group(1))
        ident.sources.append(f"size from {source_name}")
    else:
        m = _SIZE_KIB_RE.search(text)
        if m:
            mult = 1024 if m.group(2) == "KiB" else 1024 * 1024
            ident.size = int(float(m.group(1)) * mult)
            ident.sources.append(f"size from {source_name}")
    return ident


def _merge_identity(dst, src):
    for f in ("size", "crc32"):
        if getattr(dst, f) is None and getattr(src, f) is not None:
            setattr(dst, f, getattr(src, f))
    for f in ("md5", "sha1", "sha256", "sha1_norm"):
        if not getattr(dst, f) and getattr(src, f):
            setattr(dst, f, getattr(src, f))
    dst.sha256_set |= src.sha256_set
    dst.sha1_set |= src.sha1_set
    for s in src.sources:
        if s not in dst.sources:
            dst.sources.append(s)


_HEX64_SENTINEL = "ab" + "0" * 60 + "01"
_EXE_SCAN_CHUNK = 8 << 20          # stream the exe in 8 MB pieces
# Byte-translation table: 1 where a byte is a hex digit, else 0. Run
# detection over the translated buffer is a pair of C-speed memchr
# scans - ~16x faster than the v1.0.0 lookaround regex over the raw
# bytes, which spent ~1.1 s on every 48 MB executable.
_HEXMAP = bytes(1 if chr(c) in "0123456789abcdefABCDEF" else 0
                for c in range(256))


def _accept_hex64(raw, found):
    """raw is exactly 64 hex characters (bytes)."""
    v = raw.decode().lower()
    if set(v) != {"0"} and v != _HEX64_SENTINEL:
        found.add(v)


def _hex_runs(buf):
    """Maximal runs of hex-digit bytes in buf, as (start, end)."""
    t = buf.translate(_HEXMAP)
    i = 0
    n = len(t)
    while i < n:
        s = t.find(b"\x01", i)
        if s < 0:
            return
        e = t.find(b"\x00", s)
        if e < 0:
            e = n
        yield s, e
        i = e


def _extract_exe_hashes(path, cancel=None):
    """Stream one executable and harvest the 64-hex SHA-256 strings
    embedded in it, without ever holding the whole file in memory.

    Semantics are exactly v1.0.0's lookaround regex
    (?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F]): its matches are
    precisely the maximal hex runs of length exactly 64 (a match can
    only start at a run's first byte, and only survives its lookahead
    when the run is exactly 64 long). So the scan tracks maximal hex
    runs - including runs straddling chunk boundaries, whose length
    and head bytes are carried in two variables - and keeps the runs
    of length exactly 64. Differential-tested against the old regex
    implementation (see tests/extract_diff_test.py).
    """
    found = set()
    try:
        if path.stat().st_size > 300_000_000:
            return found
    except OSError:
        return found

    pending_len = 0            # hex run still open at the last
    pending_head = b""         # buffer edge: length + first 64 bytes

    def judge(length, head):
        if length == 64:
            _accept_hex64(head, found)

    try:
        with open(path, "rb") as f:
            while True:
                _check_cancel(cancel)
                buf = f.read(_EXE_SCAN_CHUNK)
                if not buf:
                    break
                runs = list(_hex_runs(buf))
                if not runs:
                    if pending_len:
                        judge(pending_len, pending_head)
                        pending_len = 0
                        pending_head = b""
                    continue
                for s, e in runs:
                    at_start = s == 0
                    at_end = e == len(buf)
                    if at_start and pending_len:
                        # The open run continues into this buffer.
                        pending_len += e - s
                        if len(pending_head) < 64:
                            pending_head += buf[s:e][:64
                                                       - len(pending_head)]
                        if not at_end:
                            judge(pending_len, pending_head)
                            pending_len = 0
                            pending_head = b""
                        continue
                    if pending_len:
                        # Closed by this buffer's first non-hex byte.
                        judge(pending_len, pending_head)
                        pending_len = 0
                        pending_head = b""
                    if at_end:
                        pending_len = e - s
                        pending_head = buf[s:e][:64]
                    else:
                        judge(e - s, buf[s:e])
    except OSError:
        return found
    if pending_len:
        judge(pending_len, pending_head)     # EOF closes the run
    return found


# Extracted executable identities are cached in a JSON sidecar under
# the app's own cache/ folder (beside this file, created at runtime,
# never shipped), keyed by absolute exe path + size + mtime - so a
# second run over the same library never re-reads the executables.
_CACHE_DIR = Path(__file__).resolve().parent / "cache"
_CACHE_PATH = _CACHE_DIR / "exe-identity.json"
_identity_cache = None             # lazily loaded dict, process-wide


def _identity_cache_load():
    global _identity_cache
    if _identity_cache is None:
        _identity_cache = {}
        try:
            data = json.loads(_CACHE_PATH.read_text())
            if isinstance(data, dict):
                _identity_cache = data
        except (OSError, ValueError):
            pass            # a broken cache is a cache miss, not a failure
    return _identity_cache


def _identity_cache_save():
    if _identity_cache is None:
        return
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _CACHE_PATH.with_name(_CACHE_PATH.name + ".tmp")
        tmp.write_text(json.dumps(_identity_cache))
        os.replace(tmp, _CACHE_PATH)
    except OSError:
        pass                # cache write failures are non-fatal


def _binary_identity(folder, exe_names, cancel=None):
    """Install-time identity: the known-ROM hashes the launcher was
    compiled with, embedded in the executable as ASCII hex strings.

    Called only from run_install, never from the scan: results are
    cached (see above) and the executables are streamed, not loaded.
    """
    ident = Identity()
    cache = _identity_cache_load()
    dirty = False
    for name in exe_names:
        p = folder / name
        try:
            st = p.stat()
        except OSError:
            continue
        found = None
        ent = cache.get(os.path.realpath(p))
        if isinstance(ent, dict) and ent.get("size") == st.st_size \
                and ent.get("mtime") == st.st_mtime:
            found = {v for v in ent.get("sha256", [])
                     if isinstance(v, str)}
        if found is None:
            found = _extract_exe_hashes(p, cancel=cancel)
            cache[os.path.realpath(p)] = {
                "size": st.st_size, "mtime": st.st_mtime,
                "sha256": sorted(found)}
            dirty = True
        # SHA-1 strings are deliberately NOT harvested from binaries:
        # long executables contain many incidental 40-hex strings, and a
        # stray one would make the true ROM fail the combined check.
        # The cartridge launchers' authoritative compiled-in gate is
        # the SHA-256 above; GBA's SHA-1 comes from its game.toml.
        if found:
            ident.sha256_set |= found
            ident.sources.append(f"hash strings in executable {name}")
            break
    if dirty:
        _identity_cache_save()
    return ident


_SKIP_DESCEND = {"assets", "artwork", "mods", "licenses", "__pycache__",
                 ".git", ".venv"}


def _exe_candidates(folder, names):
    out = []
    for n in sorted(names):
        p = folder / n
        if n.lower().endswith(".exe") or _is_elf(p) or _is_pe(p):
            out.append(n)
    return out


def _detect_unit(folder):
    """Return (PackageUnit, ignored_system_or_None). (None, None) when
    this directory is not a recomp package payload at all."""
    folder = Path(folder)
    try:
        entries = sorted(folder.iterdir())
    except OSError:
        return None, None
    names = {p.name for p in entries if p.is_file()}
    if folder.name.endswith("-src"):
        return None, None            # converter project dir, not a package

    readme = ""
    for cand in ("README.txt", "README.md"):
        if cand in names:
            readme = _read_text(folder / cand)
            if readme:
                break
    first = _first_line(readme)
    toml_text = _read_text(folder / "game.toml") if "game.toml" in names \
        else ""
    play_text = ""
    for cand in ("play.sh", "play.bat"):
        if cand in names:
            play_text += _read_text(folder / cand) + "\n"

    system = None
    title = None
    if toml_text and ("[identity]" in toml_text or "[rom]" in toml_text
                      or "GBARecomp" in toml_text):
        system = "gba"
        title = _toml_scalar(toml_text, "game", "name")
    elif "(NESRecomp" in first:
        system = "nes"
    elif "(SNESRecomp)" in first:
        system = "snes"
    elif "(GBRecomp)" in first:
        system = "gb"
    elif "(VBRecomp Tools)" in first:
        system = "vb"
    elif "(SMSGGRecomp Tools)" in first:
        system = "gg" if ("Game Gear" in first
                          or folder.name.endswith("(Game Gear)")) else "sms"
    elif "(GenesisRecomp Tools)" in first:
        return None, "genesis"
    elif "(N64Recomp" in first or "Zelda64Recompiled" in names:
        system = "n64"
    elif toml_text and _toml_scalar(toml_text, "game", "id"):
        system = "psx"
        title = _toml_scalar(toml_text, "game", "name")
    elif "Game Boy" in first and "native PC recomp" in first \
            and "SUPPLY THE ROM" in readme:
        system = "gb"                # legacy GB link-test build layout

    if system is None:
        return None, None

    exes = _exe_candidates(folder, names)
    has_play = "play.sh" in names or "play.bat" in names
    if not exes and not has_play:
        return None, None            # marker but no payload: not a package

    if not title:
        m = re.match(r"^(.+?)\s+[—-]\s+native PC recomp", first)
        if m:
            title = m.group(1).strip()
            title = re.sub(r"\s+\((Game Boy|Game Boy Color|Master System|"
                           r"Game Gear|Genesis|Virtual Boy|GBA)\)\s*$",
                           "", title)
            if title.isupper():
                title = title.title()
        else:
            title = folder.name

    ident = Identity()
    if system == "gba" and toml_text:
        sha1 = _clean_hash(_toml_scalar(toml_text, "identity", "sha1")
                           or _toml_scalar(toml_text, "rom", "sha1"), 40)
        md5 = _clean_hash(_toml_scalar(toml_text, "identity", "md5"), 32)
        size = _parse_int(_toml_scalar(toml_text, "program", "size"))
        if sha1:
            ident.sha1 = sha1
            ident.sources.append("SHA-1 from game.toml")
        if md5:
            ident.md5 = md5
            ident.sources.append("MD5 from game.toml")
        if size:
            ident.size = size
            ident.sources.append("size from game.toml")
    if readme:
        _merge_identity(ident, _text_identity(readme, "README"))
    if play_text:
        _merge_identity(ident, _text_identity(play_text, "play script"))
    if system == "n64":
        low = (title or "").lower()
        for key_name, prof in N64_PROFILES.items():
            if key_name in low:
                ident.sha1_norm = prof["sha1_norm"]
                ident.size = prof["size"]
                ident.sources.append(
                    "normalized SHA-1 from the converter's supported-title "
                    "table for the exact title this package names")
                break
    # NOTE: no _binary_identity() here. For the exe-hash systems the
    # identity is resolved at install time (run_install); a scan must
    # stay a cheap listing even over a whole library.

    rom_cfg = ""
    cfg_path = folder / "rom.cfg"
    if cfg_path.is_file():
        rom_cfg = _first_line(_read_text(cfg_path, 2000))

    psx_serials = []
    psx_cue_names = []
    disc_cfg = ""
    if system == "psx":
        gid = _toml_scalar(toml_text, "game", "id") or ""
        psx_serials = _toml_string_list(toml_text, "game", "disc_serials")
        if not psx_serials and gid:
            psx_serials = [gid]
        psx_cue_names = _toml_string_list(toml_text, "game", "discs")
        cfg_path = folder / "disc.cfg"
        if cfg_path.is_file():
            disc_cfg = _first_line(_read_text(cfg_path, 2000))

    unit = PackageUnit(folder=folder, title=title or folder.name,
                       system=system, identity=ident,
                       exe_name=exes[0] if exes else "",
                       exe_names=list(exes),
                       identity_pending=bool(
                           not ident.strong() and exes
                           and system in EXE_IDENTITY_SYSTEMS),
                       rom_cfg=rom_cfg,
                       psx_serials=psx_serials,
                       psx_cue_names=psx_cue_names,
                       disc_cfg=disc_cfg)
    return unit, None


def _is_readonly(path):
    """True when the path's mode bits deny writing to everyone - the
    cross-platform signal for POSIX read-only bits AND the Windows
    read-only file attribute (which Python maps onto the mode bits)."""
    try:
        return (os.stat(path).st_mode & 0o222) == 0
    except OSError:
        return False


def _folder_writable(folder):
    if _is_readonly(folder):
        return False
    return os.access(folder, os.W_OK)


def _list_existing(unit):
    """ROM-extension files sitting in the package folder, plus the
    rom.cfg target when it names one inside the folder. Also sweeps
    stale temp files from interrupted runs. Returns
    (existing, cfg_target), or None when the folder cannot be listed
    (unit.status/detail are set to the error)."""
    folder = unit.folder
    exts = SYSTEMS[unit.system][1]
    existing = []
    try:
        entries = sorted(folder.iterdir())
    except OSError as e:
        unit.status = "error"
        unit.detail = f"cannot list package folder: {e}"
        return None
    for p in entries:
        if p.is_file() and p.name.startswith(TEMP_PREFIX) \
                and p.name.endswith(TEMP_SUFFIX):
            try:
                p.unlink()           # stale temp from an interrupted run
            except OSError:
                pass
        elif p.is_file() and p.suffix.lower() in exts:
            existing.append(p)
    cfg_target = None
    if unit.rom_cfg:
        raw = unit.rom_cfg.strip().strip('"')
        cand = Path(raw)
        if not cand.is_absolute():
            cand = folder / cand
        if cand.is_file():
            cfg_target = cand
            if cand not in existing and cand.suffix.lower() in exts \
                    and cand.parent == folder:
                existing.append(cand)
    return existing, cfg_target


def _assess_existing(unit, cancel=None):
    """Set the unit's status from ROM files already in the package
    folder (and from a rom.cfg pointing at one).

    When the unit's identity is fully known (paper/profile systems),
    in-folder ROMs are hash-verified right here, exactly as v1.0.0
    did. When the identity lives in the executable (NES/GB/SNES), no
    verdict is faked: the unit is reported as waiting, or as holding
    an unverified ROM file, and the real check happens at install
    time once the identity has been extracted."""
    if unit.system == "psx":
        _assess_psx(unit, cancel=cancel)
        return
    listed = _list_existing(unit)
    if listed is None:
        return
    existing, cfg_target = listed

    if not unit.identity.strong():
        if unit.identity_pending:
            if existing or cfg_target is not None:
                names = ", ".join(p.name for p in existing)
                if cfg_target is not None and cfg_target not in existing:
                    names = (names + ", " if names else "") \
                        + cfg_target.name
                unit.status = "present_unverified"
                unit.detail = "ROM file present (verified at install): " \
                              + names
            else:
                unit.status = "waiting"
                unit.detail = "waiting for its ROM " \
                              "(identity checked at install)"
            return
        if existing:
            unit.status = "conflict"
            unit.detail = ("a different ROM file is already in this "
                           "package: "
                           + ", ".join(p.name for p in existing))
        elif cfg_target is not None:
            unit.status = "conflict"
            unit.detail = "rom.cfg points at a file that does not match " \
                          "this package's ROM identity"
        else:
            unit.status = "no_identity"
            unit.detail = "package carries no usable hash identity - " \
                          "cannot safely fill"
        return

    want_norm = unit.system == "n64"
    for p in existing:
        try:
            h = hash_file(p, want_n64_norm=want_norm, cancel=cancel)
        except (OSError, Cancelled):
            continue
        if identity_matches(unit.identity, h):
            unit.status = "already_present"
            unit.detail = f"correct ROM already in place: {p.name}" \
                + (" (via rom.cfg)" if p == cfg_target else "")
            return
    if existing:
        unit.status = "conflict"
        unit.detail = ("a different ROM file is already in this package: "
                       + ", ".join(p.name for p in existing))
    elif cfg_target is not None:
        unit.status = "conflict"
        unit.detail = "rom.cfg points at a file that does not match " \
                      "this package's ROM identity"
    else:
        unit.status = "ready"
        unit.detail = "waiting for its ROM"


def scan_packages(root, cancel=None, progress=None):
    """Walk a packages root; return (units, ignored_counts, notes).

    Listing only: this never reads a game executable, so scanning a
    whole library stays fast. Exe-hash identities (NES/GB/SNES) are
    deferred - those units come back 'waiting' or
    'present_unverified' and are resolved by run_install."""
    root = Path(root).expanduser()
    units = []
    ignored = {}
    notes = []
    if not root.is_dir():
        return units, ignored, [f"packages folder not found: {root}"]
    stack = [(root, 0)]
    visited = set()
    max_depth = 8
    while stack:
        _check_cancel(cancel)
        folder, depth = stack.pop()
        rp = os.path.realpath(folder)
        if rp in visited:
            continue
        visited.add(rp)
        unit, ignored_sys = _detect_unit(folder)
        if ignored_sys:
            ignored[ignored_sys] = ignored.get(ignored_sys, 0) + 1
            continue
        if unit is not None:
            _assess_existing(unit, cancel=cancel)
            units.append(unit)
            if progress:
                progress(f"package: {unit.title} [{unit.system}] "
                         f"- {unit.status}")
            continue
        if depth >= max_depth:
            continue
        try:
            children = sorted(folder.iterdir(), reverse=True)
        except OSError as e:
            notes.append(f"cannot list {folder}: {e}")
            continue
        for child in children:
            if child.is_dir() and child.name not in _SKIP_DESCEND:
                stack.append((child, depth + 1))
    units.sort(key=lambda u: (u.system, u.title.lower()))
    return units, ignored, notes


# ----------------------------------------------------------------------
# PlayStation disc sets (bin/cue)
# ----------------------------------------------------------------------
_SERIAL_FILE_RE = re.compile(
    r"^([A-Za-z]{4})[_-]?(\d{3})\.?(\d{2})(?:;\d+)?$")


def _serial_from_exe_name(name):
    """'SLUS_004.02;1' -> 'SLUS-00402' (the game.toml hyphen form).
    A cdrom path ('TEKKEN3\\SLUS_004.02;1') is reduced to its file
    name first - backslashes are separators on the disc, whatever
    the host OS thinks."""
    base = str(name).replace("\\", "/").rsplit("/", 1)[-1]
    m = _SERIAL_FILE_RE.match(base.strip())
    if not m:
        return ""
    return f"{m.group(1).upper()}-{m.group(2)}{m.group(3)}"


def _parse_cue(text):
    """[(file_name, track_mode)] in cue order; track_mode is '' when
    the cue declares no TRACK line before the next FILE."""
    files = []
    cur = None
    for line in text.splitlines():
        m = re.match(r'\s*FILE\s+"([^"]+)"', line)
        if m:
            cur = [m.group(1), ""]
            files.append(cur)
            continue
        m = re.match(r"\s*TRACK\s+\d+\s+(\S+)", line)
        if m and cur is not None and not cur[1]:
            cur[1] = m.group(1).upper()
    return [(n, mode) for n, mode in files]


def _cue_data_file(files):
    """The FILE holding the data track (the ISO the serial lives on)."""
    for name, mode in files:
        if mode != "AUDIO":
            return name
    return files[0][0] if files else ""


class _FileReader:
    """Random-access read_at over a loose file."""

    def __init__(self, path):
        self._f = open(path, "rb")

    def read_at(self, offset, size):
        self._f.seek(offset)
        return self._f.read(size)

    def close(self):
        self._f.close()


class _ZipEntryReader:
    """Forward-streaming read_at over one zip entry, served from a
    growing in-memory prefix: the ISO data the serial check needs
    (volume descriptor, root directory, SYSTEM.CNF) lives at the
    start of the image, so the whole entry is never pulled."""

    def __init__(self, zf, entry_name, cap=48 << 20):
        self._stream = zf.open(entry_name, "r")
        self._buf = bytearray()
        self._cap = cap
        self._eof = False

    def read_at(self, offset, size):
        want = offset + size
        while len(self._buf) < want and not self._eof \
                and len(self._buf) < self._cap:
            chunk = self._stream.read(CHUNK)
            if not chunk:
                self._eof = True
            else:
                self._buf += chunk
        return bytes(self._buf[offset:want])

    def close(self):
        try:
            self._stream.close()
        except Exception:
            pass


def _iso_user_stream(read_at, lba, nbytes, sector_size, data_off):
    """nbytes of user data starting at a logical block: de-interleave
    the 2048-byte user area out of each raw sector."""
    out = bytearray()
    sectors = (nbytes + 2047) // 2048
    raw = read_at(lba * sector_size, sectors * sector_size)
    for i in range(sectors):
        base = i * sector_size + data_off
        out += raw[base:base + 2048]
    return bytes(out[:nbytes])


def _psx_serial_from_disc(read_at):
    """The disc's boot serial ('SLUS-00402'), read off the image
    itself: the boot EXE's filename in the ISO9660 root directory,
    with SYSTEM.CNF's BOOT line as fallback. '' when unreadable."""
    for sector_size, data_off in ((2352, 24), (2048, 0), (2336, 8)):
        try:
            pvd = read_at(16 * sector_size + data_off, 2048)
        except OSError:
            return ""
        if len(pvd) < 190 or pvd[0] != 1 or pvd[1:6] != b"CD001":
            continue
        rec = pvd[156:190]
        root_lba = int.from_bytes(rec[2:6], "little")
        root_size = int.from_bytes(rec[10:14], "little")
        try:
            dirdata = _iso_user_stream(read_at, root_lba,
                                       min(root_size, 1 << 18),
                                       sector_size, data_off)
        except OSError:
            return ""
        cnf = None
        subdirs = []
        pos = 0
        while pos + 34 <= len(dirdata):
            reclen = dirdata[pos]
            if reclen == 0:
                pos = (pos // 2048 + 1) * 2048
                continue
            if reclen < 34:
                break
            name_len = dirdata[pos + 32]
            name = dirdata[pos + 33:pos + 33 + name_len].decode(
                "ascii", "replace")
            serial = _serial_from_exe_name(name)
            if serial:
                return serial
            if name.upper().startswith("SYSTEM.CNF"):
                cnf = (int.from_bytes(dirdata[pos + 2:pos + 6], "little"),
                       int.from_bytes(dirdata[pos + 10:pos + 14],
                                      "little"))
            elif dirdata[pos + 25] & 2 and name not in ("\x00", "\x01"):
                subdirs.append(
                    (int.from_bytes(dirdata[pos + 2:pos + 6], "little"),
                     int.from_bytes(dirdata[pos + 10:pos + 14],
                                    "little")))
            pos += reclen
        # Some discs keep the boot EXE in a subfolder (Tekken 3:
        # TEKKEN3\SLUS_004.02) - scan one level down before falling
        # back to SYSTEM.CNF.
        for lba, size in subdirs:
            try:
                sub = _iso_user_stream(read_at, lba,
                                       min(size, 1 << 18),
                                       sector_size, data_off)
            except OSError:
                continue
            pos = 0
            while pos + 34 <= len(sub):
                reclen = sub[pos]
                if reclen == 0:
                    pos = (pos // 2048 + 1) * 2048
                    continue
                if reclen < 34:
                    break
                name_len = sub[pos + 32]
                name = sub[pos + 33:pos + 33 + name_len].decode(
                    "ascii", "replace")
                serial = _serial_from_exe_name(name)
                if serial:
                    return serial
                pos += reclen
        if cnf is not None:
            try:
                body = _iso_user_stream(read_at, cnf[0],
                                        min(cnf[1], 4096),
                                        sector_size, data_off)
            except OSError:
                return ""
            m = re.search(rb"BOOT\s*=\s*cdrom:\\?([^;\s]+)", body,
                          re.I)
            if m:
                return _serial_from_exe_name(
                    m.group(1).decode("ascii", "replace"))
        return ""
    return ""


def _psx_folder_discs(folder, cancel=None):
    """Cue/bin sets sitting in a package folder. Returns
    (found, stray): found maps serial -> cue file name for complete,
    readable sets; stray lists cue names that are incomplete or
    whose disc serial cannot be read."""
    found = {}
    stray = []
    try:
        entries = sorted(folder.iterdir())
    except OSError:
        return found, stray
    for p in entries:
        if not (p.is_file() and p.suffix.lower() == ".cue"):
            continue
        _check_cancel(cancel)
        try:
            text = p.read_text(errors="replace")
        except OSError:
            stray.append(p.name)
            continue
        files = _parse_cue(text)
        if not files or not all((folder / Path(n).name).is_file()
                                for n, _ in files):
            stray.append(p.name)
            continue
        serial = ""
        data_name = _cue_data_file(files)
        if data_name:
            try:
                r = _FileReader(folder / Path(data_name).name)
                try:
                    serial = _psx_serial_from_disc(r.read_at)
                finally:
                    r.close()
            except OSError:
                serial = ""
        if serial:
            found[serial] = p.name
        else:
            stray.append(p.name)
    return found, stray


def _assess_psx(unit, cancel=None):
    """PSX presence verdict: satisfied only when every expected
    disc serial is present as a complete, serial-verified set in
    the package folder. A guard-staged disc/<EXE> alone never
    counts - only real .cue sets do."""
    if _list_existing(unit) is None:
        return                       # folder error already reported
    found, stray = _psx_folder_discs(unit.folder, cancel=cancel)
    missing = [s for s in unit.psx_serials if s not in found]
    if not missing:
        cues = ", ".join(found[s] for s in unit.psx_serials)
        unit.status = "already_present"
        unit.detail = f"correct disc(s) already in place: {cues}"
        return
    have = [s for s in unit.psx_serials if s in found]
    if have:
        unit.status = "ready"
        unit.detail = ("disc(s) in place: " + ", ".join(have)
                       + "; still waiting for: " + ", ".join(missing))
        return
    foreign = [c for s, c in found.items() if s not in unit.psx_serials]
    if stray or foreign:
        unit.status = "conflict"
        unit.detail = ("unrelated or unreadable disc set(s) in this "
                       "package: " + ", ".join(stray + foreign)
                       + " (tick Replace to install over them)")
        return
    unit.status = "ready"
    unit.detail = "waiting for its disc image (bin/cue): " \
        + ", ".join(unit.psx_serials)


# ----------------------------------------------------------------------
# Source index
# ----------------------------------------------------------------------
@dataclass
class SourceEntry:
    kind: str                      # "file" or "zip"
    path: Path                     # loose file, or the .zip archive
    entry_name: str = ""           # inside the zip, when kind == "zip"
    size: int = 0
    crc32: int = None              # zip central-directory CRC, when known

    @property
    def name(self):
        return Path(self.entry_name).name if self.kind == "zip" \
            else self.path.name

    @property
    def display(self):
        if self.kind == "zip":
            return f"{self.path} :: {self.entry_name}"
        return str(self.path)


@dataclass
class PsxSet:
    """One complete (or attempted) cue/bin disc set in the sources."""
    cue: SourceEntry                 # the .cue itself
    members: list = field(default_factory=list)  # cue + every FILE
    data: object = None              # SourceEntry of the data track
    serial: str = None               # None = unread; "" = unreadable
    complete: bool = False
    problem: str = ""


class SourceIndex:
    def __init__(self):
        self.entries = []
        self.by_size = {}
        self.notes = []
        self._hash_cache = {}
        self._psx_sets = None
        self.corrupt_zips = 0
        self.unreadable = 0

    # ---------------------------------------------------------- build
    def build(self, sources, cancel=None, progress=None):
        for src in sources:
            _check_cancel(cancel)
            src = Path(src).expanduser()
            if src.is_file() and src.suffix.lower() == ".zip":
                self._add_zip(src)
            elif src.is_file():
                if src.suffix.lower() in ALL_ROM_EXTS:
                    self._add_loose(src)
            elif src.is_dir():
                self._walk_source_dir(src, cancel, progress)
            else:
                self.notes.append(f"source not found: {src}")
        return self

    def _add_loose(self, path):
        try:
            size = path.stat().st_size
        except OSError:
            self.unreadable += 1
            self.notes.append(f"unreadable source file: {path}")
            return
        e = SourceEntry("file", path, size=size)
        self.entries.append(e)
        self.by_size.setdefault(size, []).append(e)

    def _add_zip(self, path):
        try:
            z = zipfile.ZipFile(path)
            infos = z.infolist()
        except (zipfile.BadZipFile, OSError) as e:
            self.corrupt_zips += 1
            self.notes.append(f"corrupt/unreadable zip skipped: {path} ({e})")
            return
        for info in infos:
            if info.is_dir():
                continue
            if Path(info.filename).suffix.lower() not in ALL_ROM_EXTS:
                continue
            e = SourceEntry("zip", path, entry_name=info.filename,
                            size=info.file_size, crc32=info.CRC)
            self.entries.append(e)
            self.by_size.setdefault(info.file_size, []).append(e)

    def _walk_source_dir(self, root, cancel, progress):
        stack = [(root, 0)]
        visited = set()
        max_depth = 32
        while stack:
            _check_cancel(cancel)
            folder, depth = stack.pop()
            rp = os.path.realpath(folder)
            if rp in visited:
                continue                 # symlink loop guard
            visited.add(rp)
            try:
                children = sorted(folder.iterdir(), reverse=True)
            except OSError as e:
                self.unreadable += 1
                self.notes.append(f"cannot list source folder {folder}: {e}")
                continue
            for child in children:
                if child.is_dir():
                    if depth < max_depth:
                        stack.append((child, depth + 1))
                elif child.is_file():
                    suf = child.suffix.lower()
                    if suf == ".zip":
                        self._add_zip(child)
                    elif suf in ALL_ROM_EXTS:
                        self._add_loose(child)
            if progress:
                progress(f"indexed {len(self.entries)} source ROMs so far")

    # --------------------------------------------------------- hashes
    def hashes_for(self, entry, want_n64_norm=False, cancel=None):
        key = (entry.kind, str(entry.path), entry.entry_name,
               entry.size, entry.crc32, want_n64_norm)
        if key in self._hash_cache:
            return self._hash_cache[key]
        if entry.kind == "file":
            h = hash_file(entry.path, want_n64_norm=want_n64_norm,
                          cancel=cancel)
        else:
            z = zipfile.ZipFile(entry.path)
            try:
                stream = z.open(entry.entry_name, "r")
            except (KeyError, RuntimeError, zipfile.BadZipFile):
                z.close()
                raise
            def gen():
                with stream:
                    while True:
                        _check_cancel(cancel)
                        chunk = stream.read(CHUNK)
                        if not chunk:
                            return
                        yield chunk
            try:
                h = hash_stream(gen(), size_hint=entry.size,
                                want_n64_norm=want_n64_norm, cancel=cancel)
            finally:
                z.close()
        self._hash_cache[key] = h
        return h

    def open_entry_stream(self, entry):
        """Return (iterator_of_chunks, closer) for install copying."""
        if entry.kind == "file":
            f = open(entry.path, "rb")
            return _file_chunks(f), f.close
        z = zipfile.ZipFile(entry.path)
        stream = z.open(entry.entry_name, "r")

        def gen():
            with stream:
                while True:
                    chunk = stream.read(CHUNK)
                    if not chunk:
                        return
                    yield chunk

        return gen(), z.close

    # ----------------------------------------------------------- find
    def find(self, ident, system, cancel=None, progress=None):
        """First source entry whose hashes satisfy the identity, or
        None. Zip entries are pre-filtered by the central-directory
        CRC32 whenever the identity carries one."""
        exts = SYSTEMS[system][1]
        if ident.size is not None:
            candidates = list(self.by_size.get(ident.size, []))
        else:
            candidates = [e for e in self.entries
                          if Path(e.name).suffix.lower() in exts]
        candidates.sort(key=lambda e: e.display)
        want_norm = system == "n64"
        for e in candidates:
            _check_cancel(cancel)
            if Path(e.name).suffix.lower() not in exts:
                continue
            if ident.crc32 is not None and e.crc32 is not None \
                    and e.crc32 != ident.crc32:
                continue             # central-directory pre-filter
            try:
                h = self.hashes_for(e, want_n64_norm=want_norm,
                                    cancel=cancel)
            except (OSError, zipfile.BadZipFile, RuntimeError) as ex:
                self.notes.append(f"cannot hash {e.display}: {ex}")
                continue
            if identity_matches(ident, h):
                return e
            if progress:
                progress(f"checked {e.display} - no match")
        return None

    # ------------------------------------------------- PSX disc sets
    def _psx_set_for(self, cue_entry, cancel=None):
        """Resolve one indexed .cue into its full file set: every
        FILE the cue names must sit beside it (loose) or inside the
        same archive (zip), or the set is incomplete."""
        pset = PsxSet(cue=cue_entry, members=[cue_entry])
        if cue_entry.kind == "file":
            try:
                text = cue_entry.path.read_text(errors="replace")
            except OSError as e:
                pset.problem = f"cannot read cue: {e}"
                return pset
            files = _parse_cue(text)
            missing = []
            for name, _mode in files:
                p = cue_entry.path.parent / Path(name).name
                if p.is_file():
                    m = SourceEntry("file", p, size=_safe_size(p))
                    pset.members.append(m)
                    if name == _cue_data_file(files):
                        pset.data = m
                else:
                    missing.append(name)
            if missing:
                pset.problem = "missing file(s): " + ", ".join(missing)
                return pset
        else:
            try:
                z = zipfile.ZipFile(cue_entry.path)
                try:
                    raw = z.read(cue_entry.entry_name)
                    infos = {i.filename: i for i in z.infolist()}
                finally:
                    z.close()
            except (OSError, KeyError, zipfile.BadZipFile) as e:
                pset.problem = f"cannot read cue in archive: {e}"
                return pset
            text = raw.decode("utf-8", "replace")
            files = _parse_cue(text)
            cue_dir = str(Path(cue_entry.entry_name).parent)
            missing = []
            for name, _mode in files:
                base = Path(name).name
                cands = [f"{cue_dir}/{base}"] if cue_dir != "." \
                    else [base]
                cands.append(base)
                hit = next((c for c in cands if c in infos), None)
                if hit is None:
                    same = [f for f in infos
                            if Path(f).name == base]
                    hit = same[0] if len(same) == 1 else None
                if hit is None:
                    missing.append(name)
                    continue
                info = infos[hit]
                m = SourceEntry("zip", cue_entry.path, entry_name=hit,
                                size=info.file_size, crc32=info.CRC)
                pset.members.append(m)
                if name == _cue_data_file(files):
                    pset.data = m
            if missing:
                pset.problem = "missing file(s): " + ", ".join(missing)
                return pset
        if not files:
            pset.problem = "cue names no files"
            return pset
        pset.complete = True
        return pset

    def _psx_set_serial(self, pset, cancel=None):
        """The set's disc serial, read off the data track itself
        (cached on the set). '' when it cannot be read."""
        if pset.serial is not None:
            return pset.serial
        pset.serial = ""
        if pset.data is None:
            return ""
        _check_cancel(cancel)
        try:
            if pset.data.kind == "file":
                r = _FileReader(pset.data.path)
                try:
                    pset.serial = _psx_serial_from_disc(r.read_at)
                finally:
                    r.close()
            else:
                z = zipfile.ZipFile(pset.data.path)
                try:
                    r = _ZipEntryReader(z, pset.data.entry_name)
                    try:
                        pset.serial = _psx_serial_from_disc(r.read_at)
                    finally:
                        r.close()
                finally:
                    z.close()
        except (OSError, zipfile.BadZipFile, RuntimeError):
            pset.serial = ""
        return pset.serial

    def find_psx_set(self, serial, cue_hints=(), cancel=None,
                     progress=None):
        """The complete source set whose on-disc serial is `serial`,
        or None. The package's expected cue names only order the
        search - the serial read off each disc decides the match."""
        if self._psx_sets is None:
            self._psx_sets = [self._psx_set_for(e, cancel=cancel)
                              for e in self.entries
                              if Path(e.name).suffix.lower() == ".cue"]
            for pset in self._psx_sets:
                if not pset.complete:
                    self.notes.append(
                        f"incomplete disc set skipped: "
                        f"{pset.cue.display} ({pset.problem})")
        hints = {h.lower() for h in cue_hints}
        ordered = sorted(
            self._psx_sets,
            key=lambda s: (Path(s.cue.name).name.lower() not in hints,
                           s.cue.display))
        for pset in ordered:
            _check_cancel(cancel)
            if not pset.complete:
                continue
            if self._psx_set_serial(pset, cancel=cancel) == serial:
                return pset
            if progress:
                progress(f"checked disc set {pset.cue.display} "
                         f"- no match")
        return None


# ----------------------------------------------------------------------
# Installing
# ----------------------------------------------------------------------
def _make_writable(path):
    try:
        mode = os.stat(path).st_mode
        os.chmod(path, mode | stat_mod.S_IWRITE | stat_mod.S_IREAD)
    except OSError:
        pass


def install(unit, entry, index, replace=False, cancel=None, progress=None):
    """Fill one package from one source entry, atomically. Returns the
    unit with its status/detail updated."""
    folder = unit.folder
    dest_name = Path(entry.name).name
    transform = None
    if unit.system == "n64":
        # Byte-swapped dumps are installed normalized to .z64 order
        # (the form the runtime verifies) when they are not already.
        try:
            if entry.kind == "file":
                with open(entry.path, "rb") as f:
                    fmt = n64_format(f.read(4))
            else:
                z = zipfile.ZipFile(entry.path)
                with z.open(entry.entry_name, "r") as s:
                    fmt = n64_format(s.read(4))
                z.close()
        except (OSError, zipfile.BadZipFile):
            fmt = "z64"
        if fmt in ("v64", "n64"):
            transform = fmt
            dest_name = Path(dest_name).stem + ".z64"
    dest = folder / dest_name
    unit.dest_name = dest_name

    if dest.exists():
        try:
            h = hash_file(dest, want_n64_norm=(unit.system == "n64"),
                          cancel=cancel)
            if identity_matches(unit.identity, h):
                unit.status = "already_present"
                unit.detail = f"correct ROM already in place: {dest_name}"
                return unit
        except (OSError, Cancelled):
            pass
        if not replace:
            unit.status = "conflict"
            unit.detail = f"destination already holds a different file: " \
                          f"{dest_name} (tick Replace to overwrite it)"
            return unit

    if not _folder_writable(folder):
        unit.status = "error"
        unit.detail = f"destination folder is read-only: {folder}"
        return unit

    temp = folder / f"{TEMP_PREFIX}{os.getpid()}-{abs(hash(unit.key))}" \
                    f"{TEMP_SUFFIX}"
    try:
        chunks, closer = index.open_entry_stream(entry)
        carry = b""
        with open(temp, "wb") as out:
            for chunk in chunks:
                _check_cancel(cancel)
                if transform:
                    data = carry + chunk
                    whole = len(data) - (len(data) % 4)
                    if whole:
                        out.write(_n64_transform(data[:whole], transform))
                    carry = data[whole:]
                else:
                    out.write(chunk)
                if progress:
                    progress(f"copying {dest_name} into {unit.title}")
            if transform and carry:
                out.write(_n64_transform(carry, transform))
            out.flush()
            os.fsync(out.fileno())
        closer()
    except Cancelled:
        _silent_unlink(temp)
        unit.status = "cancelled"
        unit.detail = "install cancelled - no partial file left behind"
        return unit
    except (OSError, zipfile.BadZipFile, RuntimeError) as e:
        _silent_unlink(temp)
        unit.status = "error"
        unit.detail = f"copy failed: {e}"
        return unit

    # Verify what landed on disk before it gets its real name.
    try:
        h = hash_file(temp, want_n64_norm=(unit.system == "n64"),
                      cancel=cancel)
    except (OSError, Cancelled) as e:
        _silent_unlink(temp)
        unit.status = "error"
        unit.detail = f"verification read failed: {e}"
        return unit
    if not identity_matches(unit.identity, h):
        _silent_unlink(temp)
        unit.status = "error"
        unit.detail = "copied file failed hash verification - not installed"
        return unit

    try:
        if dest.exists() and _is_readonly(dest):
            _make_writable(dest)     # Windows read-only attribute / mode
        os.replace(temp, dest)
    except OSError as e:
        _silent_unlink(temp)
        unit.status = "error"
        unit.detail = f"rename into place failed: {e}"
        return unit

    removed = []
    if replace and unit.status == "conflict":
        # The user explicitly marked this conflict for replacement and
        # the clash was a differently-named ROM file in the package:
        # remove the non-matching ROM files so the package's own
        # launcher/play script cannot pick the wrong one first.
        exts = SYSTEMS[unit.system][1]
        for p in sorted(folder.iterdir()):
            if p.is_file() and p != dest \
                    and p.suffix.lower() in exts:
                try:
                    h = hash_file(p, want_n64_norm=(unit.system == "n64"))
                    if not identity_matches(unit.identity, h):
                        _silent_unlink(p)
                        removed.append(p.name)
                except OSError:
                    pass

    unit.status = "filled"
    unit.detail = f"installed {dest_name} from {entry.display}"
    if removed:
        unit.detail += "; removed conflicting file(s): " \
                       + ", ".join(removed)
    unit.source_desc = entry.display
    if unit.system in ROM_CFG_SYSTEMS:
        _write_rom_cfg(unit, dest)
    return unit


def _silent_unlink(path):
    try:
        Path(path).unlink()
    except OSError:
        try:
            _make_writable(path)
            Path(path).unlink()
        except OSError:
            pass


def _write_rom_cfg(unit, dest):
    """Point the package's rom.cfg at the installed ROM, the way the
    launcher itself records a picked ROM. Only writes when rom.cfg is
    absent or points at a file that no longer exists, and never when
    one already points at another existing file."""
    cfg = unit.folder / "rom.cfg"
    try:
        if cfg.is_file():
            raw = _first_line(_read_text(cfg, 2000)).strip().strip('"')
            if raw:
                target = Path(raw)
                if not target.is_absolute():
                    target = unit.folder / target
                if target.is_file():
                    return
        cfg.write_text(str(dest) + "\n")
        unit.detail += "; rom.cfg now points at it"
    except OSError:
        unit.detail += "; (rom.cfg could not be written)"


def _write_disc_cfg(unit, cue_path):
    """Point the package's disc.cfg at the installed disc-1 cue,
    the way the launcher itself records a picked disc. Only writes
    when disc.cfg is absent or points at a file that no longer
    exists, and never when one already points at another file."""
    cfg = unit.folder / "disc.cfg"
    try:
        if cfg.is_file():
            raw = _first_line(_read_text(cfg, 2000)).strip().strip('"')
            if raw:
                target = Path(raw)
                if not target.is_absolute():
                    target = unit.folder / target
                if target.is_file():
                    return
        cfg.write_text(str(cue_path) + "\n")
        unit.detail += "; disc.cfg now points at disc 1"
    except OSError:
        unit.detail += "; (disc.cfg could not be written)"


def _install_psx(unit, index, replace=False, cancel=None, progress=None):
    """Fill one PSX package with complete, serial-verified bin/cue
    sets: every missing disc is located first, every file is staged
    to a temp name, and only then is anything moved into place -
    a set installs whole or not at all."""
    folder = unit.folder
    found, _stray = _psx_folder_discs(folder, cancel=cancel)
    missing = [s for s in unit.psx_serials if s not in found]
    if not missing:
        unit.status = "already_present"
        unit.detail = "correct disc(s) already in place: " + ", ".join(
            found[s] for s in unit.psx_serials)
        return unit
    chosen = {}
    for s in missing:
        pset = index.find_psx_set(s, unit.psx_cue_names,
                                  cancel=cancel, progress=progress)
        if pset is None:
            unit.status = "not_found"
            unit.detail = f"no complete bin/cue set with serial {s} " \
                          f"in the source folders"
            return unit
        chosen[s] = pset

    plan = []                        # (SourceEntry, dest Path)
    seen = {}
    for s in missing:
        for m in chosen[s].members:
            name = Path(m.entry_name).name if m.kind == "zip" \
                else m.path.name
            key = (m.kind, str(m.path), m.entry_name)
            if name in seen and seen[name] != key:
                unit.status = "error"
                unit.detail = f"two source sets both need {name} " \
                              f"with different contents - cannot " \
                              f"install both"
                return unit
            seen[name] = key
            plan.append((m, folder / name))
    for _m, dest in plan:
        if dest.exists() and not replace:
            unit.status = "conflict"
            unit.detail = f"destination already holds a file: " \
                          f"{dest.name} (tick Replace to overwrite it)"
            return unit
    if not _folder_writable(folder):
        unit.status = "error"
        unit.detail = f"destination folder is read-only: {folder}"
        return unit

    temps = []
    try:
        for m, dest in plan:
            temp = folder / f"{TEMP_PREFIX}{os.getpid()}-" \
                            f"{abs(hash(str(dest)))}-{dest.name}" \
                            f"{TEMP_SUFFIX}"
            chunks, closer = index.open_entry_stream(m)
            try:
                with open(temp, "wb") as out:
                    for chunk in chunks:
                        _check_cancel(cancel)
                        out.write(chunk)
                        if progress:
                            progress(f"copying {dest.name} into "
                                     f"{unit.title}")
                    out.flush()
                    os.fsync(out.fileno())
            finally:
                closer()
            temps.append((temp, dest))
    except Cancelled:
        for t, _d in temps:
            _silent_unlink(t)
        unit.status = "cancelled"
        unit.detail = "install cancelled - no partial disc set " \
                      "left behind"
        return unit
    except (OSError, zipfile.BadZipFile, RuntimeError) as e:
        for t, _d in temps:
            _silent_unlink(t)
        unit.status = "error"
        unit.detail = f"copy failed: {e}"
        return unit

    try:
        for temp, dest in temps:
            if dest.exists() and _is_readonly(dest):
                _make_writable(dest)
            os.replace(temp, dest)
    except OSError as e:
        for t, _d in temps:
            _silent_unlink(t)
        unit.status = "error"
        unit.detail = f"rename into place failed: {e}"
        return unit

    now, _ = _psx_folder_discs(folder, cancel=cancel)
    if any(s not in now for s in unit.psx_serials):
        unit.status = "error"
        unit.detail = "installed disc set failed serial verification" \
                      " - check the package folder"
        return unit
    unit.status = "filled"
    unit.detail = "installed " + ", ".join(now[s] for s in missing) \
        + " from " + "; ".join(chosen[s].cue.display for s in missing)
    unit.source_desc = "; ".join(chosen[s].cue.display for s in missing)
    _write_disc_cfg(unit, folder / now[unit.psx_serials[0]])
    return unit


# ----------------------------------------------------------------------
# Run + report
# ----------------------------------------------------------------------
def _resolve_exe_identity(unit, cancel=None, progress=None):
    """Extract a deferred exe-hash identity for one unit (cached;
    see _binary_identity), then judge any in-folder ROM files
    against the now-known identity. Updates unit.status in place."""
    if progress:
        progress(f"checking {unit.title}'s executable…")
    _merge_identity(unit.identity,
                    _binary_identity(unit.folder, unit.exe_names,
                                     cancel=cancel))
    unit.identity_pending = False
    if not unit.identity.strong():
        unit.status = "no_identity"
        unit.detail = "package carries no usable hash identity - " \
                      "cannot safely fill"
        return
    _assess_existing(unit, cancel=cancel)


def run_install(units, sources, replace_keys=(), cancel=None, progress=None):
    index = SourceIndex().build(sources, cancel=cancel, progress=progress)
    for unit in units:
        _check_cancel(cancel)
        if unit.status in ("already_present", "no_identity"):
            continue
        if unit.system == "psx":
            if unit.status == "conflict" and unit.key not in replace_keys:
                continue
            _install_psx(unit, index,
                         replace=(unit.key in replace_keys),
                         cancel=cancel, progress=progress)
            continue
        if not unit.identity.strong():
            # Deferred identity (NES/GB/SNES): extract it now, once,
            # before matching this unit.
            _resolve_exe_identity(unit, cancel=cancel, progress=progress)
            if unit.status in ("already_present", "no_identity"):
                continue
        if unit.status == "conflict" and unit.key not in replace_keys:
            continue
        if progress:
            progress(f"finding ROM for {unit.title} [{unit.system}]")
        entry = index.find(unit.identity, unit.system, cancel=cancel,
                           progress=progress)
        if entry is None:
            if unit.status != "conflict":
                unit.status = "not_found"
                unit.detail = "no exact hash match in the source folders"
            continue
        install(unit, entry, index,
                replace=(unit.key in replace_keys),
                cancel=cancel, progress=progress)
    return index


def make_report(units, ignored=None, index_notes=None, source_count=None):
    lines = [f"# {APP_NAME} - report", ""]
    counts = {}
    for u in units:
        counts[u.status] = counts.get(u.status, 0) + 1
    lines.append("Summary: " + (", ".join(
        f"{k} {v}" for k, v in sorted(counts.items())) or "no packages"))
    if source_count is not None:
        lines.append(f"Source ROM entries indexed: {source_count}")
    if ignored:
        lines.append("Not ROM-waiting (left alone): " + ", ".join(
            f"{k} x{v}" for k, v in sorted(ignored.items())))
    lines.append("")
    for u in units:
        lines.append(f"- **{u.title}** [{SYSTEMS[u.system][0]}] - "
                     f"{u.status}: {u.detail}")
        lines.append(f"  - expects: {u.expects_text()}")
        lines.append(f"  - package: {u.folder}")
        if u.identity.sources:
            lines.append("  - identity from: "
                         + "; ".join(u.identity.sources))
    if index_notes:
        lines.append("")
        lines.append("Source notes:")
        for n in index_notes:
            lines.append(f"- {n}")
    return "\n".join(lines) + "\n"


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 2 or argv[0] not in ("scan", "fill"):
        print("usage: installer.py scan <packages-folder>\n"
              "       installer.py fill <packages-folder> "
              "<source> [<source> ...] [--replace]")
        return 2
    replace = "--replace" in argv
    argv = [a for a in argv if a != "--replace"]
    try:
        units, ignored, notes = scan_packages(argv[1])
        for n in notes:
            print(f"note: {n}")
        if argv[0] == "fill":
            index = run_install(
                units, argv[2:],
                replace_keys={u.key for u in units} if replace else ())
            print(make_report(units, ignored, index.notes,
                              len(index.entries)))
        else:
            print(make_report(units, ignored, notes))
    except Cancelled:
        print("cancelled")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
