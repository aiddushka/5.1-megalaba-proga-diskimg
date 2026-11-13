"""Блочные устройства: raw, window, VHD fixed/dynamic, MBR — аналог bdev.c."""
import os, struct
from .common import *
from . import bootcode

class Bdev:
    kind = "raw"
    size = 0
    def read(self, off, n): raise NotImplementedError
    def write(self, off, b): raise NotImplementedError
    def flush(self): return 0
    def close(self): pass

class Raw(Bdev):
    def __init__(self, path, writable=False):
        self.f = open(path, "r+b" if writable else "rb")
        self.f.seek(0, 2); self.size = self.f.tell()
        self.kind = "raw"
    def read(self, off, n):
        if off + n > self.size: return err("чтение за концом устройства (%d)", off)
        self.f.seek(off); b = self.f.read(n)
        if len(b) < n: b += b"\0" * (n - len(b))
        return b
    def write(self, off, b):
        if off + len(b) > self.size: return err("запись за концом устройства (%d)", off)
        self.f.seek(off); self.f.write(b); return 0
    def flush(self): self.f.flush(); return 0
    def close(self): self.f.close()

class Window(Bdev):
    kind = "partition"
    def __init__(self, parent, off, size):
        self.p, self.off, self.size = parent, off, size
    def read(self, off, n):
        if off + n > self.size: return err("чтение за концом раздела")
        return self.p.read(self.off + off, n)
    def write(self, off, b):
        if off + len(b) > self.size: return err("запись за концом раздела")
        return self.p.write(self.off + off, b)
    def flush(self): return self.p.flush()
    def close(self): pass

VHD_FIXED, VHD_DYNAMIC, VHD_BLOCK = 2, 3, 2*1024*1024

def _vhd_checksum(footer, csum_off):
    s = sum(b for i, b in enumerate(footer) if not (csum_off <= i < csum_off+4))
    return (~s) & 0xFFFFFFFF

def _vhd_geometry(size):
    ts = size // 512
    if ts > 65535*16*255: ts = 65535*16*255
    if ts >= 65535*16*63: s, h, cth = 255, 16, ts // 255
    else:
        s = 17; cth = ts // s
        h = (cth + 1023) // 1024
        if h < 4: h = 4
        if cth >= h*1024 or h > 16: s, h, cth = 31, 16, ts // 31
        if cth >= h*1024: s, h, cth = 63, 16, ts // 63
    return cth // h, h, s

def _vhd_footer(size, typ):
    f = bytearray(512)
    f[0:8] = b"conectix"
    wrb32(f, 8, 2); wrb32(f, 12, 0x00010000)
    wrb64(f, 16, 512 if typ == VHD_DYNAMIC else 0xFFFFFFFFFFFFFFFF)
    wrb32(f, 24, (int(__import__("time").time()) - 946684800) & 0xFFFFFFFF)
    f[28:32] = b"dimg"; wrb32(f, 32, 0x00010000); f[36:40] = b"Wi2k"
    wrb64(f, 40, size); wrb64(f, 48, size)
    c, h, s = _vhd_geometry(size)
    wrb16(f, 56, c); f[58] = h; f[59] = s
    wrb32(f, 60, typ)
    for i in range(16): f[68+i] = rand32() & 0xFF
    f[74] = (f[74] & 0x0F) | 0x40
    f[76] = (f[76] & 0x3F) | 0x80
    wrb32(f, 64, _vhd_checksum(f, 64))
    return bytes(f)

class VhdDynamic(Bdev):
    kind = "vhd-dynamic"
    def __init__(self, f):
        self.f = f
        fsz = os.fstat(f.fileno()).st_size
        f.seek(fsz - 512); self.footer = f.read(512)
        if self.footer[0:8] != b"conectix":
            f.seek(0); self.footer = f.read(512)
        hoff = rdb64(self.footer, 16)
        f.seek(hoff); hdr = f.read(1024)
        if hdr[0:8] != b"cxsparse": raise RuntimeError("повреждён заголовок dynamic VHD")
        self.bat_off = rdb64(hdr, 16)
        self.nblocks = rdb32(hdr, 28)
        self.block_size = rdb32(hdr, 32)
        self.bitmap_sectors = DIV_UP(DIV_UP(self.block_size // 512, 8), 512)
        f.seek(self.bat_off); raw = f.read(self.nblocks * 4)
        if len(raw) < self.nblocks * 4: raw += b"\xFF" * (self.nblocks*4 - len(raw))
        self.bat = [rdb32(raw, 4*i) for i in range(self.nblocks)]
        self.size = rdb64(self.footer, 48)
        self.next_free = fsz - 512
        for b in self.bat:
            if b != 0xFFFFFFFF:
                e = b*512 + self.bitmap_sectors*512 + self.block_size
                if e > self.next_free: self.next_free = e
        self.dirty_bat = False
    def read(self, off, n):
        if off + n > self.size: return err("чтение за концом VHD")
        out = bytearray()
        while n:
            blk = off // self.block_size; inn = off % self.block_size
            take = min(n, self.block_size - inn)
            if self.bat[blk] == 0xFFFFFFFF: out += b"\0" * take
            else:
                pos = self.bat[blk]*512 + self.bitmap_sectors*512 + inn
                self.f.seek(pos); out += self.f.read(take)
            off += take; n -= take
        return bytes(out)
    def write(self, off, b):
        if off + len(b) > self.size: return err("запись за концом VHD")
        while b:
            blk = off // self.block_size; inn = off % self.block_size
            take = min(len(b), self.block_size - inn)
            if self.bat[blk] == 0xFFFFFFFF:
                if all(x == 0 for x in b[:take]): off += take; b = b[take:]; continue
                pos = self.next_free; total = self.bitmap_sectors*512 + self.block_size
                self.f.seek(pos)
                self.f.write(b"\xFF" * (self.bitmap_sectors*512) + b"\0"*self.block_size)
                self.bat[blk] = pos // 512
                self.next_free = pos + total
                self.f.seek(self.next_free); self.f.write(self.footer)
                self.dirty_bat = True
            pos = self.bat[blk]*512 + self.bitmap_sectors*512 + inn
            self.f.seek(pos); self.f.write(b[:take])
            off += take; b = b[take:]
        return 0
    def flush(self):
        if self.dirty_bat:
            self.f.seek(self.bat_off)
            self.f.write(b"".join(wrb32_dummy(x) for x in self.bat))
            self.dirty_bat = False
        self.f.flush(); return 0
    def close(self): self.flush(); self.f.close()

def wrb32_dummy(v):
    b = bytearray(4); wrb32(b, 0, v); return bytes(b)

def vhd_detect(f):
    fsz = os.fstat(f.fileno()).st_size
    if fsz < 512: return 0
    f.seek(fsz - 512); b = f.read(512)
    if b[0:8] != b"conectix":
        f.seek(0); b = f.read(512)
        if b[0:8] != b"conectix": return 0
    t = rdb32(b, 60)
    return 1 if t == VHD_FIXED else (2 if t == VHD_DYNAMIC else -1)

def bdev_open_file(path, writable=False):
    f = open(path, "r+b" if writable else "rb")
    v = vhd_detect(f)
    if v == 1:
        d = Raw.__new__(Raw); d.f = f
        f.seek(0, 2); d.size = f.tell() - 512
        d.kind = "vhd-fixed"; return d
    if v == 2: return VhdDynamic(f)
    if v < 0: f.close(); raise RuntimeError("неподдерживаемый тип VHD")
    d = Raw.__new__(Raw); d.f = f
    f.seek(0, 2); d.size = f.tell(); d.kind = "raw"; return d

def raw_create(path, size):
    with open(path, "wb") as f: f.truncate(size)

def vhd_create(path, size, dynamic):
    size = ALIGN_UP(size, 512)
    with open(path, "wb") as f:
        if not dynamic:
            f.truncate(size); f.seek(size); f.write(_vhd_footer(size, VHD_FIXED))
        else:
            footer = _vhd_footer(size, VHD_DYNAMIC)
            nblocks = DIV_UP(size, VHD_BLOCK)
            bat_off = 512 + 1024
            bat_bytes = ALIGN_UP(nblocks*4, 512)
            hdr = bytearray(1024); hdr[0:8] = b"cxsparse"
            wrb64(hdr, 8, 0xFFFFFFFFFFFFFFFF); wrb64(hdr, 16, bat_off)
            wrb32(hdr, 24, 0x00010000); wrb32(hdr, 28, nblocks); wrb32(hdr, 32, VHD_BLOCK)
            wrb32(hdr, 36, _vhd_checksum(hdr, 36))
            f.write(footer); f.write(hdr); f.write(b"\xFF"*bat_bytes); f.write(footer)

# --- MBR ---
def mbr_read(d):
    if d.size < 512: return None
    s = d.read(0, 512)
    if s[510:512] != b"\x55\xAA": return None
    parts = []
    for i in range(4):
        e = s[446+16*i: 446+16*i+16]
        if e[0] not in (0, 0x80): return None
        t = e[4]; lba = rd32(e, 8); cnt = rd32(e, 12)
        if t and cnt and (lba + cnt) * 512 > d.size: return None
        parts.append({"index": i+1, "active": e[0] == 0x80, "type": t, "lba": lba, "count": cnt})
    return parts

def _lba_to_chs(lba):
    H, S = 255, 63
    if lba >= 1024*H*S: return bytes([0xFE, 0xFF, 0xFF])
    c = lba // (H*S); h = (lba // S) % H; s = lba % S + 1
    return bytes([h, s | ((c >> 2) & 0xC0), c & 0xFF])

def mbr_write_single(d, lba, count, ptype):
    s = bytearray(512)
    s[0:len(bootcode.mbr_boot_code)] = bootcode.mbr_boot_code[:440]
    wr32(s, 440, rand32())
    e = 446
    s[e] = 0x80
    s[e+1:e+4] = _lba_to_chs(lba)
    s[e+4] = ptype
    s[e+5:e+8] = _lba_to_chs(lba + count - 1)
    wr32(s, e+8, lba); wr32(s, e+12, count)
    s[510:512] = b"\x55\xAA"
    return d.write(0, bytes(s))

def part_type_name(t):
    return {0x01:"FAT12",0x04:"FAT16 <32M",0x06:"FAT16",0x0E:"FAT16 LBA",
            0x0B:"FAT32 CHS",0x0C:"FAT32 LBA",0x07:"NTFS/exFAT",0x05:"Extended",
            0x0F:"Extended",0xEE:"GPT protective",0x83:"Linux"}.get(t, "?")

def bdev_replace(d, src_path, size):
    if not isinstance(d, Raw): return err("пересборка только для raw")
    with open(src_path, "rb") as s, open(d.f.name, "r+b") as t:
        t.seek(0); left = size
        while left:
            chunk = s.read(min(left, 1 << 20))
            if not chunk: break
            t.write(chunk); left -= len(chunk)
        t.truncate(size)
    d.size = size
    return 0