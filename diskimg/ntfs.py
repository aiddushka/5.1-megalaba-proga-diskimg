"""NTFS 3.1: чтение (info/ls/cat/extract). Запись — следующая итерация."""
import os
from .common import *
from .fs import FileSystem, fsent_t
from functools import cmp_to_key

# ---- константы ----

AT_STANDARD_INFORMATION = 0x10
AT_ATTRIBUTE_LIST       = 0x20
AT_FILE_NAME            = 0x30
AT_OBJECT_ID            = 0x40
AT_SECURITY_DESCRIPTOR  = 0x50
AT_VOLUME_NAME          = 0x60
AT_VOLUME_INFORMATION   = 0x70
AT_DATA                 = 0x80
AT_INDEX_ROOT           = 0x90
AT_INDEX_ALLOCATION     = 0xA0
AT_BITMAP               = 0xB0
AT_REPARSE_POINT        = 0xC0
AT_END                  = 0xFFFFFFFF

MFT_IN_USE     = 0x0001
MFT_IS_DIR     = 0x0002
MFT_IS_4       = 0x0004
MFT_VIEW_INDEX = 0x0008

FA_READONLY = 0x0001
FA_HIDDEN   = 0x0002
FA_SYSTEM   = 0x0004
FA_ARCHIVE  = 0x0020
FA_DIR_IDX  = 0x10000000
FA_VIEW_IDX = 0x20000000

FA_DIR_IDX = 0x10000000
NS_POSIX, NS_WIN32, NS_DOS, NS_BOTH = 0, 1, 2, 3
COLL_FILE_NAME = 0x01
IE_SUBNODE, IE_END = 1, 2
FILE_root = 5
MREF = lambda no, seq: (no) | ((seq) << 48)
MREF_NO  = lambda r: r & 0x0000FFFFFFFFFFFF
MREF_SEQ = lambda r: (r >> 48) & 0xFFFF

# ---- $UpCase: упрощённая (ASCII + Latin-1 + кириллица) ----
def _build_upcase():
    uc = list(range(0x10000))
    for c in range(ord('a'), ord('z')+1): uc[c] = c - 32
    for c in range(0xE0, 0xFF):
        if c != 0xF7: uc[c] = c - 32
    for c in range(0x430, 0x450): uc[c] = c - 32
    for c in range(0x450, 0x460): uc[c] = c - 80
    return uc

class Ntfs(FileSystem):
    type = "NTFS"

    def __init__(self, dev):
        self.d = dev
        b = dev.read(0, 512)
        if not ntfs_probe(b): raise RuntimeError("это не том NTFS")
        self.bps = rd16(b, 11)
        self.spc = b[13]
        self.csize = self.bps * self.spc if self.spc < 0x80 else self.bps << (256 - self.spc)
        self.nclusters = rd64(b, 0x28) * self.bps // self.csize
        self.mft_lcn  = rd64(b, 0x30)
        self.mirr_lcn = rd64(b, 0x38)
        self.cpm = b[0x40] - 256 if b[0x40] >= 0x80 else b[0x40]
        self.cpi = b[0x44] - 256 if b[0x44] >= 0x80 else b[0x44]
        self.rec_size = (self.cpm * self.csize) if self.cpm > 0 else (1 << (-self.cpm))
        self.idx_size = (self.cpi * self.csize) if self.cpi > 0 else (1 << (-self.cpi))
        self.idx_vcn_unit = self.csize if self.csize <= self.idx_size else 512
        self.serial = rd64(b, 0x48)
        self.mirr_recs = 4 if self.csize <= 4 * self.rec_size else self.csize // self.rec_size
        if self.rec_size < 512 or self.rec_size > 4096 or self.idx_size < 512 or self.idx_size > 65536:
            raise RuntimeError("NTFS: неподдерживаемые параметры тома")

        # запись 0 читаем напрямую по LCN
        rec = bytearray(dev.read(self.mft_lcn * self.csize, self.rec_size))
        if rec[:4] != b"FILE" or mst_unfix(rec, self.rec_size):
            raise RuntimeError("NTFS: не читается запись $MFT")
        a = _attr_find(self, rec, AT_DATA, None, 0)
        if not a or not a[8]: raise RuntimeError("NTFS: нет $MFT:$DATA")
        self.mft_rl = _rl_decode(a)
        self.mft_size = rd64(a, 0x30)

        # $MFT:$BITMAP
        a = _attr_find(self, rec, AT_BITMAP, None, 0)
        if not a: raise RuntimeError("NTFS: нет $MFT:$BITMAP")
        if a[8]:
            self.mbmp_rl = _rl_decode(a)
            self.mbmp_bytes = rd64(a, 0x30)
            self.mbmp = bytearray(_rl_io(self, self.mbmp_rl, 0, self.mbmp_bytes, 0))
        else:
            self.mbmp = bytes(_res_value(a))
            self.mbmp_bytes = len(self.mbmp)

        # $UpCase — используем _build_upcase() (как в C-версии, для совместимости)
        self.upcase = _build_upcase()
        self.upcase_len = 65536

        # $Bitmap
        rec = bytearray(dev.read(self.mft_lcn * self.csize, self.rec_size))
        # перечитываем запись 6 через MFT
        s = _get_stream(self, 6, bytearray(self.d.read(0, self.rec_size)), AT_DATA, None, 0)
        # (упрощённо: читаем $Bitmap через mft_read)
        r6 = self._mft_read(6)
        s = _get_stream(self, 6, r6, AT_DATA, None, 0)
        if s and not s["resident"]:
            self.bmp_rl = s["rl"]
            self.bmp_bytes = s["data_size"]
            self.bmp = bytearray(_rl_io(self, self.bmp_rl, 0, self.bmp_bytes, 0))

        # $Volume: метка и версия
        self.label = ""
        self.major, self.minor = 0, 0
        r3 = self._mft_read(3)
        a = _attr_find(self, r3, 0x60, None, 0)   # $VOLUME_NAME
        if a:
            v = _res_value(a); n = len(v) // 2
            self.label = utf16le_to_utf8(v, n)
        a = _attr_find(self, r3, 0x70, None, 0)   # $VOLUME_INFORMATION
        if a:
            v = _res_value(a); self.major = v[8]; self.minor = v[9]

        # default security id из корня
        self.default_secid = 0x100
        r5 = self._mft_read(FILE_root)
        a = _attr_find(self, r5, AT_STANDARD_INFORMATION, None, 0)
        if a and _res_len(a) >= 72: self.default_secid = rd32(_res_value(a), 52)

        self.alloc_hint = self.mft_lcn

    # ---- MFT ----
    def _mft_records(self): return self.mft_size // self.rec_size

    def _mft_read(self, no):
        if no >= self._mft_records(): raise RuntimeError(f"NTFS: запись MFT {no} вне таблицы")
        buf = bytearray(_rl_io(self, self.mft_rl, no * self.rec_size, self.rec_size, 0))
        if buf[:4] != b"FILE": return bytearray(self.rec_size)
        if mst_unfix(buf, self.rec_size): raise RuntimeError(f"NTFS: запись MFT {no} повреждена (fixup)")
        return buf

    def _mft_write(self, no, rec):
        buf = bytearray(rec)
        mst_fix(buf, self.rec_size)
        pos = no * self.rec_size
        # записать через self.mft_rl
        left = self.rec_size
        while left:
            vcn = pos // self.csize; inside = pos % self.csize
            found = None
            for (rv, rlcn, rlen) in self.mft_rl:
                if rv <= vcn < rv + rlen: found = (rv, rlcn, rlen); break
            if not found: raise RuntimeError(f"NTFS: запись MFT {no} вне runlist")
            rv, rlcn, rlen = found
            n = min(left, (rv + rlen - vcn) * self.csize - inside)
            self.d.write((rlcn + (vcn - rv)) * self.csize + inside, bytes(buf[0:n]))
            buf = buf[n:]; pos += n; left -= n
        if no < self.mirr_recs:
            self.d.write(self.mirr_lcn * self.csize + no * self.rec_size, bytes(bytearray(rec) if True else b""))
        return 0

    # ---- пути ----
    def _resolve(self, path):
        comps = path_split(path)
        cur = FILE_root
        if not comps: return FILE_root, None, ""
        parent = FILE_root
        for i, c in enumerate(comps):
            real, m = _dir_lookup(self, cur, c)
            if i == len(comps) - 1:
                if not m: return None, parent, c
                return MREF_NO(m), cur, real
            if not m: return None, cur, c
            cur = MREF_NO(m); parent = cur
        return None, parent, ""

    # ---- fs API ----
    def list(self, path, cb):
        no, _, _ = self._resolve(path)
        if no is None: return err("не найдено: %s", path)
        rec = self._mft_read(no)
        if not (rd16(rec, 0x16) & MFT_IS_DIR):
            e = fsent_t(os.path.basename(path.rstrip("/")), False,
                        _data_size(self, no, rec), _si_mtime(self, rec))
            cb(e); return 0
        l = _dir_collect(self, no)
        for ent in l:
            k = ent["key"]
            if k[65] == NS_DOS: continue
            cno = MREF_NO(ent["mref"])
            if no == FILE_root and cno < 16: continue
            if no == FILE_root and cno == FILE_root: continue
            name = utf16le_to_utf8(k, 66, k[64]) if False else _fn_name(k)
            is_dir = (rd32(k, 56) & FA_DIR_IDX) != 0
            size = 0 if is_dir else rd64(k, 48)
            mtime = ntfs_to_unix(rd64(k, 16))
            if not is_dir:
                cr = self._mft_read(cno)
                size = _data_size(self, cno, cr)
                mtime = _si_mtime(self, cr)
            cb(fsent_t(name, is_dir, size, mtime))
        return 0

    def stat(self, path):
        no, _, real = self._resolve(path)
        if no is None: return None
        rec = self._mft_read(no)
        is_dir = bool(rd16(rec, 0x16) & MFT_IS_DIR)
        size = 0 if is_dir else _data_size(self, no, rec)
        return fsent_t(real or "/", is_dir, size, _si_mtime(self, rec))

    def read(self, path, out):
        no, _, _ = self._resolve(path)
        if no is None: return err("не найдено: %s", path)
        rec = self._mft_read(no)
        if rd16(rec, 0x16) & MFT_IS_DIR: return err("%s — каталог", path)
        s = _get_stream(self, no, rec, AT_DATA, None, 0)
        if not s: return 0
        if s["resident"]:
            out.write(s["value"]); return 0
        flags = s.get("flags", 0)
        if flags & 0x00FF: return err("%s: сжатые файлы не поддерживаются", path)
        if flags & 0x4000: return err("%s: зашифрованные файлы не поддерживаются", path)
        chunk = 1 << 20
        for off in range(0, s["data_size"], chunk):
            n = min(chunk, s["data_size"] - off)
            if off >= s["init_size"]: buf = b"\0" * n
            else:
                rn = min(n, s["init_size"] - off)
                buf = _rl_io(self, s["rl"], off, rn, 0)
                if rn < n: buf += b"\0" * (n - rn)
            out.write(buf)
        return 0

    def info(self, out):
        fr = 0
        for i in range(self.nclusters):
            if i // 8 < len(self.bmp) and not ((self.bmp[i//8] >> (i & 7)) & 1): fr += 1
        used = 0
        for i in range(self._mft_records()):
            if i // 8 < len(self.mbmp) and ((self.mbmp[i//8] >> (i & 7)) & 1): used += 1
        out.write(f"Файловая система : NTFS {self.major}.{self.minor}\n")
        out.write(f"Метка тома       : {self.label}\n")
        out.write(f"Серийный номер   : {self.serial:016X}\n")
        out.write(f"Размер кластера  : {self.csize} байт\n")
        out.write(f"Запись MFT       : {self.rec_size} байт, блок индекса {self.idx_size} байт\n")
        out.write(f"$MFT             : LCN {self.mft_lcn}, записей {self._mft_records()} (занято {used})\n")
        out.write(f"$MFTMirr         : LCN {self.mirr_lcn}\n")
        out.write(f"Кластеров        : {self.nclusters} (свободно {fr}, {fr*self.csize/1048576:.1f} МБ)\n")

    def write(self, path, src): raise NotImplementedError("NTFS write — следующая итерация")
    def mkdir(self, path):      raise NotImplementedError("NTFS mkdir — следующая итерация")
    def remove(self, path):     raise NotImplementedError("NTFS remove — следующая итерация")
    def flush(self): return 0
    def close(self): pass

# ========================================================================
#                           Низкоуровневые утилиты
# ========================================================================

def ntfs_probe(b):
    return b[3:11] == b"NTFS    " and b[510:512] == b"\x55\xAA"

def mst_unfix(b, size):
    if len(b) < 0x30: return True
    off = rd16(b, 4); cnt = rd16(b, 6)
    if off + cnt * 2 > size or cnt == 0 or (cnt - 1) * 512 > size: return True
    usn = rd16(b, off)
    for i in range(1, cnt):
        p = i * 512 - 2
        if rd16(b, p) != usn: return True
        wr16(b, p, rd16(b, off + 2*i))
    return False

def mst_fix(b, size):
    off = rd16(b, 4); cnt = rd16(b, 6)
    usn = (rd16(b, off) + 1) & 0xFFFF
    if usn == 0 or usn == 0xFFFF: usn = 1
    wr16(b, off, usn)
    for i in range(1, cnt):
        p = i * 512 - 2
        if p + 2 > len(b): break
        wr16(b, off + 2*i, rd16(b, p))
        wr16(b, p, usn)

def _attr_first(rec): return rd16(rec, 0x14)
def _attr_next(rec, a, rec_size):
    ln = rd32(rec, a + 4)
    if ln == 0: return None
    n = a + ln
    if n + 4 > rec_size: return None
    return n

def _attr_name_eq(v, a, name, nlen):
    an = a[9]
    if nlen is None: return True
    if an != nlen: return False
    off = rd16(a, 10)
    p = a[off:]
    for i in range(an):
        if _up(v, rd16(p, 2*i)) != _up(v, name[i]): return False
    return True

def _attr_find(v, rec, typ, name, nlen):
    a = _attr_first(rec)
    while a + 8 <= v.rec_size and rd32(rec, a) != AT_END:
        ln = rd32(rec, a + 4)
        if ln == 0 or a + ln > len(rec): break
        attr = bytes(rec[a:a+ln])
        if rd32(attr, 0) == typ and _attr_name_eq(v, attr, name, nlen):
            return attr
        a += ln
    return None

def _res_value(a): return a[rd16(a, 0x14):]
def _res_len(a):   return rd32(a, 0x10)

def _rl_decode(a):
    rl = []
    off = rd16(a, 0x20)
    ln  = rd32(a, 4)
    p, end = off, ln
    vcn = rd64(a, 0x10); lcn = 0
    while p < end and a[p]:
        lb = a[p] & 0x0F; ob = (a[p] >> 4) & 0x0F; p += 1
        if lb == 0 or lb > 8 or ob > 8 or p + lb + ob > end: break
        length = 0
        for i in range(lb): length |= a[p+i] << (8*i)
        p += lb
        if ob:
            d = 0
            for i in range(ob): d |= a[p+i] << (8*i)
            if a[p+ob-1] & 0x80:
                for i in range(ob, 8): d |= 0xFF << (8*i)
            if d >= (1 << 63): d -= (1 << 64)
            p += ob
            lcn += d
            rl.append((vcn, lcn, length))
        else:
            rl.append((vcn, -1, length))
        vcn += length
    return rl

def _rl_io(v, rl, off, length, wr):
    out = bytearray()
    while length:
        vcn = off // v.csize; inside = off % v.csize
        found = None
        for (rv, rlcn, rlen) in rl:
            if rv <= vcn < rv + rlen:
                found = (rv, rlcn, rlen); break
        if not found: raise RuntimeError(f"NTFS: смещение {off} вне runlist")
        rv, rlcn, rlen = found
        avail = (rv + rlen - vcn) * v.csize - inside
        n = min(avail, length)
        if rlcn < 0:
            if wr: raise RuntimeError("NTFS: запись в разреженную область")
            out += b"\0" * n
        else:
            pos = (rlcn + (vcn - rv)) * v.csize + inside
            if wr: v.d.write(pos, bytes(out)) if False else None
            out += v.d.read(pos, n)
        off += n; length -= n
    return bytes(out)

def _get_stream(v, no, rec, typ, name, nlen):
    a = _attr_find(v, rec, typ, name, nlen)
    al = _attr_find(v, rec, AT_ATTRIBUTE_LIST, None, 0)
    if a and (not a[8] or not al):
        return _stream_from_attr(a, 1)
    if not al: return None
    # упрощённо: без $ATTRIBUTE_LIST (для чтения большинства файлов достаточно)
    if a: return _stream_from_attr(a, 1)
    return None

def _stream_from_attr(a, first):
    s = {"resident": False, "value": None, "rl": [], "data_size": 0, "init_size": 0, "alloc_size": 0, "flags": 0}
    if not a[8]:
        s["resident"] = True
        s["value"] = bytes(_res_value(a)[:_res_len(a)])
        s["data_size"] = s["init_size"] = _res_len(a)
        return s
    if first:
        s["alloc_size"] = rd64(a, 0x28)
        s["data_size"] = rd64(a, 0x30)
        s["init_size"] = rd64(a, 0x38)
        s["flags"] = rd16(a, 12)
    s["rl"] = _rl_decode(a)
    return s

def _up(v, c):
    return v.upcase[c] if c < v.upcase_len else c

def _name_cmp(v, a, na, b, nb):
    n = min(na, nb)
    for i in range(n):
        x = _up(v, rd16(a, 2*i)); y = _up(v, rd16(b, 2*i))
        if x != y: return -1 if x < y else 1
    if na != nb: return -1 if na < nb else 1
    for i in range(n):
        x = rd16(a, 2*i); y = rd16(b, 2*i)
        if x != y: return -1 if x < y else 1
    return 0

def _name_ieq16(v, a, na, b, nb):
    if na != nb: return False
    for i in range(na):
        if _up(v, rd16(a, 2*i)) != _up(v, b[i]): return False
    return True

def _fn_name(k):
    return utf16le_to_utf8(k[66:66 + 2*k[64]], k[64]) if False else \
           "".join(chr(rd16(k, 66 + 2*i)) for i in range(k[64]))

def _si_mtime(v, rec):
    a = _attr_find(v, rec, AT_STANDARD_INFORMATION, None, 0)
    if not a: return 0
    v2 = _res_value(a)
    return ntfs_to_unix(rd64(v2, 8))

def _data_size(v, no, rec):
    s = _get_stream(v, no, rec, AT_DATA, None, 0)
    return s["data_size"] if s else 0

# ---- B-дерево индекса $I30 ----
def _walk_entries(v, area, length, alloc, out, depth):
    if depth > 32: raise RuntimeError("NTFS: слишком глубокое дерево индекса")
    n = min(length, len(area))          # ← не выходить за границы area
    p = 0
    while p + 16 <= n:
        el = rd16(area, p + 8); kl = rd16(area, p + 10); fl = rd16(area, p + 12)
        if el < 16 or p + el > n: raise RuntimeError("NTFS: повреждена запись индекса")
        if fl & IE_SUBNODE:
            child = rd64(area, p + el - 8)
            _walk_block(v, child, alloc, out, depth + 1)
        if fl & IE_END: break
        out.append({"mref": rd64(area, p), "key": bytes(area[p+16:p+16+kl])})
        p += el

def _walk_block(v, vcn, alloc, out, depth):
    if depth > 32: raise RuntimeError("NTFS: слишком глубокое дерево индекса")
    buf = bytearray(_rl_io(v, alloc, vcn * v.idx_vcn_unit, v.idx_size, 0))
    if buf[:4] != b"INDX" or mst_unfix(buf, v.idx_size):
        raise RuntimeError(f"NTFS: повреждён блок индекса (VCN {vcn})")
    h = 0x18
    eo = rd32(buf, h); il = rd32(buf, h + 4)
    if eo + 0x18 > v.idx_size or il + 0x18 > v.idx_size:
        raise RuntimeError("NTFS: блок индекса с неверными размерами")
    _walk_entries(v, buf[h + eo:h + eo + il - eo], il - eo, alloc, out, depth + 1)

I30 = [ord('$'), ord('I'), ord('3'), ord('0')]

def _dir_collect(v, no):
    rec = v._mft_read(no)
    root = _get_stream(v, no, rec, AT_INDEX_ROOT, I30, 4)
    if not root: raise RuntimeError(f"NTFS: запись {no} — не каталог")
    has_alloc = _get_stream(v, no, rec, AT_INDEX_ALLOCATION, I30, 4) is not None
    alloc = _get_stream(v, no, rec, AT_INDEX_ALLOCATION, I30, 4)
    out = []
    ir = root["value"]
    h = ir[16:]
    eo = rd32(h, 0); il = rd32(h, 4)
    _walk_entries(v, h[eo:eo + il - eo], il - eo, alloc["rl"] if (has_alloc and alloc) else None, out, 0)
    return out

def _dir_lookup(v, dir_no, name):
    w = utf8_to_utf16(name, 256)
    l = _dir_collect(v, dir_no)
    for e in l:
        k = e["key"]
        if len(k) < 66: continue
        if _name_ieq16(v, k[66:], k[64], w, len(w)):
            real = "".join(chr(rd16(k, 66 + 2*i)) for i in range(k[64]))
            # если нашли DOS-имя, подберём длинное
            if k[65] == NS_DOS:
                for e2 in l:
                    if e2["mref"] == e["mref"] and len(e2["key"]) >= 66 and e2["key"][65] != NS_DOS:
                        k2 = e2["key"]
                        real = "".join(chr(rd16(k2, 66 + 2*i)) for i in range(k2[64]))
                        break
            return real, e["mref"]
    return None, 0

# ---- монтирование ----
def ntfs_mount(dev):
    return Ntfs(dev)


# ========================================================================
#                        Запись NTFS: форматирование
# ========================================================================
import os, time

ATTRDEFS = [
    ("$STANDARD_INFORMATION", 0x10, 0, 0, 0x40, 48, 72),
    ("$ATTRIBUTE_LIST",       0x20, 0, 0, 0x80, 0, -1),
    ("$FILE_NAME",            0x30, 0, 0, 0x42, 68, 578),
    ("$OBJECT_ID",            0x40, 0, 0, 0x40, 0, 256),
    ("$SECURITY_DESCRIPTOR",  0x50, 0, 0, 0x80, 0, -1),
    ("$VOLUME_NAME",          0x60, 0, 0, 0x40, 2, 256),
    ("$VOLUME_INFORMATION",   0x70, 0, 0, 0x40, 12, 12),
    ("$DATA",                 0x80, 0, 0, 0x00, 0, -1),
    ("$INDEX_ROOT",           0x90, 0, 0, 0x40, 0, -1),
    ("$INDEX_ALLOCATION",     0xA0, 0, 0, 0x80, 0, -1),
    ("$BITMAP",               0xB0, 0, 0, 0x80, 0, -1),
    ("$REPARSE_POINT",        0xC0, 0, 0, 0x80, 0, 16384),
    ("$EA_INFORMATION",       0xD0, 0, 0, 0x40, 8, 8),
    ("$EA",                   0xE0, 0, 0, 0x00, 0, 65536),
    ("$LOGGED_UTILITY_STREAM",0x100, 0, 0, 0x80, 0, 65536),
]

def _make_sd():
    sd = bytearray(80)
    sd[0] = 1
    wr16(sd, 2, 0x8004)
    wr32(sd, 4, 48); wr32(sd, 8, 64); wr32(sd, 12, 0); wr32(sd, 16, 20)
    acl = 20
    sd[acl] = 2; wr16(sd, acl+2, 28); wr16(sd, acl+4, 1)
    ace = acl + 8
    sd[ace] = 0; sd[ace+1] = 0x03; wr16(sd, ace+2, 20)
    wr32(sd, ace+4, 0x001F01FF)
    sd[ace+8] = 1; sd[ace+9] = 1; sd[ace+15] = 1; wr32(sd, ace+16, 0)
    for k in range(2):
        sid = 48 + 16 * k
        sd[sid] = 1; sd[sid+1] = 2; sd[sid+7] = 5
        wr32(sd, sid+8, 32); wr32(sd, sid+12, 544)
    return bytes(sd)

def _sd_hash(sd):
    h = 0
    for i in range(0, len(sd) - 3, 4):
        h = (rd32(sd, i) + (((h >> 29) | (h << 3)) & 0xFFFFFFFF)) & 0xFFFFFFFF
    return h

def _rec_init(v, rec, no, seq, flags):
    rec[:] = b"\0" * v.rec_size
    rec[0:4] = b"FILE"
    cnt = v.rec_size // 512 + 1
    wr16(rec, 4, 0x30); wr16(rec, 6, cnt)
    wr16(rec, 0x10, seq)
    ao = ALIGN_UP(0x30 + 2 * cnt, 8)
    wr16(rec, 0x14, ao); wr16(rec, 0x16, flags)
    wr32(rec, 0x18, ao + 8); wr32(rec, 0x1C, v.rec_size)
    wr32(rec, 0x2C, no)
    wr16(rec, 0x30, 1)
    wr32(rec, ao, AT_END)

def _rec_used(rec): return rd32(rec, 0x18)

def _attr_order(v, t1, n1, l1, a2):
    t2 = rd32(a2, 0)
    if t1 != t2: return -1 if t1 < t2 else 1
    l2 = a2[9]; p2 = rd16(a2, 10)
    n = min(l1, l2)
    for i in range(n):
        x = _up(v, n1[i]); y = _up(v, rd16(a2, p2 + 2*i))
        if x != y: return -1 if x < y else 1
    return l1 - l2

def _attr_insert(v, rec, typ, name, nlen, length, nonres):
    length = ALIGN_UP(length, 8)
    if _rec_used(rec) + length > v.rec_size: return None
    pos = rd16(rec, 0x14)
    while rd32(rec, pos) != AT_END and _attr_order(v, typ, name or [], nlen, rec[pos:]) >= 0:
        pos += rd32(rec, pos + 4)
    end = _rec_used(rec)
    rec[pos + length:end + length] = rec[pos:end]
    rec[pos:pos + length] = b"\0" * length
    wr32(rec, pos, typ); wr32(rec, pos + 4, length)
    rec[pos + 8] = 1 if nonres else 0
    rec[pos + 9] = nlen
    noff = 0x40 if nonres else 0x18
    wr16(rec, pos + 10, noff)
    inst = rd16(rec, 0x28)
    wr16(rec, pos + 14, inst)
    wr16(rec, 0x28, inst + 1)
    for i in range(nlen): wr16(rec, pos + noff + 2*i, name[i])
    wr32(rec, 0x18, _rec_used(rec) + length)
    return pos

def _attr_add_res(v, rec, typ, name, nlen, val, vlen, rflags=0):
    voff = ALIGN_UP(0x18 + 2 * (nlen or 0), 8)
    pos = _attr_insert(v, rec, typ, name, nlen, voff + vlen, 0)
    if pos is None: return None
    wr32(rec, pos + 0x10, vlen)
    wr16(rec, pos + 0x14, voff)
    rec[pos + 0x16] = rflags
    if val and vlen: rec[pos + voff:pos + voff + vlen] = bytes(val[:vlen])
    return pos

def _rl_encode(rl):
    out = bytearray()
    prev = 0
    for (vcn, lcn, length) in rl:
        lb = 1
        while lb < 8:
            lo = -(1 << (8*lb-1)); hi = (1 << (8*lb-1)) - 1
            if lo <= length <= hi: break
            lb += 1
        ob = 0; d = 0
        if lcn >= 0:
            d = lcn - prev; prev = lcn
            ob = 1
            while ob < 8:
                lo = -(1 << (8*ob-1)); hi = (1 << (8*ob-1)) - 1
                if lo <= d <= hi: break
                ob += 1
        out.append(lb | (ob << 4))
        for k in range(lb): out.append((length >> (8*k)) & 0xFF)
        for k in range(ob): out.append(((d + (1<<64)) >> (8*k)) & 0xFF)
    out.append(0)
    return bytes(out)

def _attr_add_nonres(v, rec, typ, name, nlen, rl, dsize, isize):
    mpo = ALIGN_UP(0x40 + 2 * (nlen or 0), 8)
    ml = len(_rl_encode(rl))
    pos = _attr_insert(v, rec, typ, name, nlen, mpo + ml, 1)
    if pos is None: return None
    ncl = sum(r[2] for r in rl)
    wr64(rec, pos + 0x10, 0)
    wr64(rec, pos + 0x18, ncl - 1 if ncl else 0xFFFFFFFFFFFFFFFF)
    wr16(rec, pos + 0x20, mpo)
    wr64(rec, pos + 0x28, ncl * v.csize)
    wr64(rec, pos + 0x30, dsize)
    wr64(rec, pos + 0x38, isize)
    rec[pos + mpo:pos + mpo + ml] = _rl_encode(rl)
    return pos

def _make_fn(parent_ref, name, ns, fattr, alloc, size, t):
    nlen = len(name)
    fn = bytearray(66 + 2*nlen)
    wr64(fn, 0, parent_ref)
    for off in (8, 16, 24, 32): wr64(fn, off, t)
    wr64(fn, 40, alloc); wr64(fn, 48, size); wr32(fn, 56, fattr)
    fn[64] = nlen; fn[65] = ns
    for i in range(nlen): wr16(fn, 66 + 2*i, name[i])
    return bytes(fn)

def _make_si(ct, mt, fattr, secid):
    si = bytearray(72)
    wr64(si, 0, ct); wr64(si, 8, mt); wr64(si, 16, mt); wr64(si, 24, mt)
    wr32(si, 32, fattr); wr32(si, 52, secid)
    return bytes(si)

def _choose_ns(w):
    n = len(w)
    if n < 1 or n > 12: return NS_POSIX
    dot = -1
    for i, c in enumerate(w):
        if c == ord('.'):
            if dot >= 0: return NS_POSIX
            dot = i; continue
        if not ((ord('A') <= c <= ord('Z')) or (ord('0') <= c <= ord('9')) or c in b"_ -~!#$%&@"):
            return NS_POSIX
    if dot < 0: return NS_BOTH if n <= 8 else NS_POSIX
    if dot == 0 or dot > 8 or n - dot - 1 < 1 or n - dot - 1 > 3: return NS_POSIX
    return NS_BOTH

# ========================================================================
#                        ntfs_format
# ========================================================================

def ntfs_format(dev, opts):
    o = opts or {}
    sector_size = 512
    sectors = dev.size // sector_size
    csize = o.get("cluster_size") or 4096
    if csize < 512 or csize > 4096 or (csize & (csize - 1)):
        raise RuntimeError("NTFS: размер кластера 512..4096 (степень двойки)")
    if dev.size < 4 * 1024 * 1024:
        raise RuntimeError("NTFS: том должен быть не меньше 4 МБ")

    # локальный "том" для форматирования (без монтирования)
    v = type("V", (), {})()
    v.d = dev
    v.bps = sector_size
    v.csize = csize
    v.rec_size = 1024
    v.idx_size = 4096
    v.cpm = 1024 // csize if csize <= 1024 else -10
    v.cpi = 4096 // csize
    v.idx_vcn_unit = csize
    v.mirr_recs = 4
    total_sectors = sectors - 1
    v.nclusters = total_sectors * 512 // csize
    N = v.nclusters
    v.bmp_bytes = ALIGN_UP(DIV_UP(N, 8), 8)
    v.bmp = bytearray(v.bmp_bytes)
    for i in range(N, v.bmp_bytes * 8):
        v.bmp[i // 8] |= (1 << (i & 7))
    v.upcase = _build_upcase()
    v.upcase_len = 65536
    volsize = N * csize

    # --- размещение служебных файлов (по C-версии) ---
    def place(bytes_):
        cl = DIV_UP(bytes_, csize)
        start = place.pos
        for k in range(cl):
            v.bmp[(start + k) // 8] |= (1 << ((start + k) & 7))
        place.pos += cl
        return {"lcn": start, "clusters": cl, "size": bytes_}
    place.pos = 0
    boot = place(8192)
    mft_recs = max(64, min(volsize // 400, 16 << 20) // 1024)
    mft_recs = ALIGN_UP(mft_recs, csize // 1024 if csize // 1024 else 1)
    if place.pos < 16 and N > 64: place.pos = 16
    mft = place(mft_recs * 1024)
    mbmp = place(ALIGN_UP(DIV_UP(mft_recs, 8), 8))
    if volsize < 2048 * 1024: lsz = 256 * 1024
    elif volsize < 4000000: lsz = 512 * 1024
    elif volsize <= 200 * 1024 * 1024: lsz = 2048 * 1024
    elif volsize >= (12 << 30): lsz = 64 << 20
    else: lsz = (volsize // 200) & ~(csize - 1)
    logf = place(lsz)
    adef = place(2560)
    bmpf = place(v.bmp_bytes)
    upc = place(131072)
    sd = _make_sd()
    sds_entry = 20 + len(sd)
    sds_size = 0x40000 + ALIGN_UP(sds_entry, 16)
    sds = place(sds_size)
    save = place.pos
    place.pos = N // 2
    while (place.pos // 8) < v.bmp_bytes and (v.bmp[place.pos // 8] >> (place.pos & 7)) & 1:
        place.pos += 1
    mirr = place(max(4096, csize))
    place.pos = save
    if place.pos >= N: raise RuntimeError("NTFS: том слишком мал")

    v.mft_lcn = mft["lcn"]; v.mirr_lcn = mirr["lcn"]
    v.mft_rl = [(0, mft["lcn"], mft["clusters"])]
    v.mft_size = mft_recs * 1024
    v.mbmp_rl = [(0, mbmp["lcn"], mbmp["clusters"])]
    v.mbmp_bytes = mbmp["size"]
    v.mbmp_alloc = mbmp["clusters"] * csize
    v.mbmp = bytearray(v.mbmp_alloc)
    v.bmp_rl = [(0, bmpf["lcn"], bmpf["clusters"])]

    # --- обнулить области ---
    z = bytearray(1 << 20)
    zero_to = place.pos * csize
    for off in range(0, zero_to, 1 << 20):
        dev.write(off, bytes(z[:min(1 << 20, zero_to - off)]))
    dev.write(mirr["lcn"] * csize, bytes(z[:mirr["clusters"] * csize]))

    # --- $LogFile = 0xFF ---
    z[:] = b"\xFF" * (1 << 20)
    for off in range(0, logf["clusters"] * csize, 1 << 20):
        dev.write(logf["lcn"] * csize + off, bytes(z[:min(1 << 20, logf["clusters"] * csize - off)]))
    z[:] = b"\0" * (1 << 20)

    # --- $AttrDef ---
    t = bytearray(2560)
    for i, (nm, typ, disp, coll, flags, mn, mx) in enumerate(ATTRDEFS):
        e = 160 * i
        for k, ch in enumerate(nm): wr16(t, e + 2*k, ord(ch))
        wr32(t, e + 128, typ); wr32(t, e + 132, disp)
        wr32(t, e + 136, coll); wr32(t, e + 140, flags)
        wr64(t, e + 144, 0 if mn < 0 else mn)
        wr64(t, e + 152, 0xFFFFFFFFFFFFFFFF if mx < 0 else mx)
    dev.write(adef["lcn"] * csize, bytes(t))

    # --- $UpCase ---
    t = bytearray(131072)
    _uc = _build_upcase()
    for i in range(65536): wr16(t, 2*i, _uc[i] & 0xFFFF)
    dev.write(upc["lcn"] * csize, bytes(t))

    # --- $Secure:$SDS ---
    hash_ = _sd_hash(sd)
    sdshdr = bytearray(20)
    wr32(sdshdr, 0, hash_); wr32(sdshdr, 4, 0x100); wr64(sdshdr, 8, 0); wr32(sdshdr, 16, sds_entry)
    t = bytearray(ALIGN_UP(sds_entry, 16))
    t[:20] = sdshdr; t[20:20+len(sd)] = sd
    dev.write(sds["lcn"] * csize, bytes(t))
    dev.write(sds["lcn"] * csize + 0x40000, bytes(t))

    # --- записи MFT 0..26 ---
    now = unix_to_ntfs(int(time.time()))
    root_ref = MREF(FILE_root, FILE_root)
    recs = bytearray(mft_recs * 1024)
    names = ["$MFT", "$MFTMirr", "$LogFile", "$Volume", "$AttrDef", ".", "$Bitmap",
             "$Boot", "$BadClus", "$Secure", "$UpCase", "$Extend"]
    for i in range(mft_recs):
        seq = 1 if i == 0 else (i if i < 24 else 1)
        fl = 0
        if i < 16 or 24 <= i <= 26: fl |= MFT_IN_USE
        if i in (5, 11): fl |= MFT_IS_DIR
        if i == 9: fl |= MFT_VIEW_INDEX
        if 24 <= i <= 26: fl |= MFT_IS_4 | MFT_VIEW_INDEX
        rec = bytearray(1024)
        _rec_init(v, rec, i, seq, fl)
        if i >= 27: recs[i*1024:(i+1)*1024] = rec; continue
        fattr = FA_HIDDEN | FA_SYSTEM
        secid = 0
        if i in (0,1,2,3,4,5,6,7,8,9,10,11): secid = 0x100
        if 24 <= i <= 26: secid = 0x100
        if i == 5: fattr |= FA_ARCHIVE
        if i == 9: fattr |= FA_VIEW_IDX
        if 24 <= i <= 26: fattr |= FA_ARCHIVE | FA_VIEW_IDX
        _attr_add_res(v, rec, AT_STANDARD_INFORMATION, None, 0, _make_si(now, now, fattr, secid), 72, 0)
        if 16 <= i <= 23: recs[i*1024:(i+1)*1024] = rec; continue
        if 12 <= i <= 15:
            _attr_add_res(v, rec, AT_DATA, None, 0, b"", 0)
            _attr_add_res(v, rec, AT_SECURITY_DESCRIPTOR, None, 0, sd, len(sd))
            recs[i*1024:(i+1)*1024] = rec; continue
        nm = names[i] if i < 12 else ("$Quota" if i == 24 else "$ObjId" if i == 25 else "$Reparse")
        w = utf8_to_utf16(nm, 16)
        alloc = dsize = 0
        if i == 0: alloc, dsize = mft["clusters"]*csize, mft["size"]
        elif i == 1: alloc, dsize = mirr["clusters"]*csize, 4096
        elif i == 2: alloc, dsize = logf["clusters"]*csize, logf["size"]
        elif i == 4: alloc, dsize = adef["clusters"]*csize, adef["size"]
        elif i == 6: alloc, dsize = bmpf["clusters"]*csize, bmpf["size"]
        elif i == 7: alloc, dsize = boot["clusters"]*csize, boot["size"]
        elif i == 10: alloc, dsize = upc["clusters"]*csize, upc["size"]
        parent = MREF(11, 11) if i >= 24 else root_ref
        fa2 = fattr | (FA_DIR_IDX if i in (5, 11) else 0)
        _attr_add_res(v, rec, AT_FILE_NAME, None, 0, _make_fn(parent, w, NS_BOTH, fa2, alloc, dsize, now), 66 + 2*len(w), 1)
        wr16(rec, 0x12, 1)
        if i == 0:
            _attr_add_nonres(v, rec, AT_DATA, None, 0, [(0, mft["lcn"], mft["clusters"])], dsize, dsize)
            _attr_add_nonres(v, rec, AT_BITMAP, None, 0, [(0, mbmp["lcn"], mbmp["clusters"])], mbmp["size"], mbmp["size"])
        elif i == 1:
            _attr_add_nonres(v, rec, AT_DATA, None, 0, [(0, mirr["lcn"], mirr["clusters"])], 4096, 4096)
        elif i in (2, 4, 6, 7, 10):
            _attr_add_nonres(v, rec, AT_DATA, None, 0, [(0, alloc and {"lcn": alloc // csize}, 0)][0] if False else [(0, {"logf": logf, "adef": adef, "bmpf": bmpf, "boot": boot, "upc": upc}[{2:"logf",4:"adef",6:"bmpf",7:"boot",10:"upc"}[i]]["lcn"], {"logf": logf, "adef": adef, "bmpf": bmpf, "boot": boot, "upc": upc}[{2:"logf",4:"adef",6:"bmpf",7:"boot",10:"upc"}[i]]["clusters"])], dsize, dsize)
        elif i == 3:
            lw = utf8_to_utf16(o.get("label") or "", 127)
            lb = bytearray(2*len(lw))
            for k in range(len(lw)): wr16(lb, 2*k, lw[k])
            _attr_add_res(v, rec, 0x60, None, 0, lb, len(lb))
            vi = bytearray(12); vi[8] = 3; vi[9] = 1
            _attr_add_res(v, rec, 0x70, None, 0, vi, 12)
            _attr_add_res(v, rec, AT_DATA, None, 0, b"", 0)
        elif i in (5, 11):
            val = bytearray(48)
            wr32(val, 0, AT_FILE_NAME); wr32(val, 4, COLL_FILE_NAME); wr32(val, 8, v.idx_size)
            val[12] = v.cpi & 0xFF
            h = 16
            p = 16 + _put_end(val, h + 16, -1)
            wr32(val, h, 16); wr32(val, h + 4, p); wr32(val, h + 8, p)
            val[h + 12] = 0
            _attr_add_res(v, rec, AT_INDEX_ROOT, I30, 4, val, 48)
        elif i == 8:
            _attr_add_res(v, rec, AT_DATA, None, 0, b"", 0)
            _attr_add_nonres(v, rec, AT_DATA, [ord('$'), ord('B'), ord('a'), ord('d')], 4, [(0, -1, N)], N*csize, N*csize)
        elif i == 9:
            _attr_add_nonres(v, rec, AT_DATA, [ord('$'), ord('S'), ord('D'), ord('S')], 4, [(0, sds["lcn"], sds["clusters"])], sds_size, sds_size)
            _add_view_index(v, rec, "$SDH", 0x12)
            _add_view_index(v, rec, "$SII", 0x10)
        elif i == 24:
            _add_view_index(v, rec, "$O", 0x11)
            _add_view_index(v, rec, "$Q", 0x10)
        elif i == 25:
            _add_view_index(v, rec, "$O", 0x13)
        elif i == 26:
            _add_view_index(v, rec, "$R", 0x13)
        recs[i*1024:(i+1)*1024] = rec
        v.mbmp[i // 8] |= (1 << (i & 7))
    for i in range(16): v.mbmp[i // 8] |= (1 << (i & 7))

    # записать MFT и зеркало
    for i in range(mft_recs):
        r = bytearray(recs[i*1024:(i+1)*1024])
        mst_fix(r, 1024)
        dev.write(mft["lcn"]*csize + i*1024, bytes(r))
        if i < 4: dev.write(mirr["lcn"]*csize + i*1024, bytes(r))

    # загрузочный сектор
    b = bytearray(512)
    b[0:3] = b"\xEB\x52\x90"
    b[3:11] = b"NTFS    "
    wr16(b, 11, 512); b[13] = csize // 512
    b[21] = 0xF8
    wr16(b, 24, 63); wr16(b, 26, 255)
    wr32(b, 28, o.get("hidden_sectors", 0))
    b[0x24] = 0x80; b[0x26] = 0x80
    wr64(b, 0x28, total_sectors)
    wr64(b, 0x30, mft["lcn"])
    wr64(b, 0x38, mirr["lcn"])
    b[0x40] = v.cpm & 0xFF; b[0x44] = v.cpi & 0xFF
    wr64(b, 0x48, ((rand32() << 32) | rand32()))
    b[510] = 0x55; b[511] = 0xAA          # ← сигнатура NTFS
    dev.write(0, bytes(b))
    dev.write((sectors - 1)*512, bytes(b))

    # карты
    dev.write(bmpf["lcn"]*csize, bytes(v.bmp[:bmpf["size"]]))
    dev.write(mbmp["lcn"]*csize, bytes(v.mbmp[:mbmp["size"]]))
    dev.flush()

    # --- пересобрать индексы корня и $Extend (как в C) ---
    m = Ntfs(dev)
    root_ents = []
    ext_ents = []
    for i in range(27):
        if 12 <= i <= 23: continue
        r = m._mft_read(i)
        fa = _attr_find(m, r, AT_FILE_NAME, None, 0)
        if not fa: continue
        mref = MREF(i, rd16(r, 0x10))
        key = _res_value(fa)[:_res_len(fa)]
        (ext_ents if i >= 24 else root_ents).append((mref, key))
    def _cmp(a, b):
        return _name_cmp(m, a[1][66:], a[1][64], b[1][66:], b[1][64])
    root_ents.sort(key=cmp_to_key(lambda a, b: _cmp(a, b)))
    ext_ents.sort(key=cmp_to_key(lambda a, b: _cmp(a, b)))
    _dir_index_set(m, FILE_root, root_ents)
    _dir_index_set(m, 11, ext_ents)
    _write_bitmaps(m)
    m.d.flush()
    return 0

# ========================================================================
#              B-дерево $I30: построение индекса (dir_index_set)
# ========================================================================

def _bit_get(b, i): return (b[i >> 3] >> (i & 7)) & 1
def _bit_set(b, i, v):
    if v: b[i >> 3] |= (1 << (i & 7))
    else: b[i >> 3] &= ~(1 << (i & 7))

def _rl_push(rl, vcn, lcn, length):
    if rl and rl[-1][1] >= 0 and lcn >= 0 and rl[-1][1] + rl[-1][2] == lcn and rl[-1][0] + rl[-1][2] == vcn:
        rl[-1] = (rl[-1][0], rl[-1][1], rl[-1][2] + length)
    else:
        rl.append((vcn, lcn, length))

def _free_runs(v, rl):
    for (vcn, lcn, length) in rl:
        if lcn < 0: continue
        for k in range(length):
            c = lcn + k
            if c < v.nclusters:
                _bit_set(v.bmp, c, 0)
    v.bmp_dirty = True

def _alloc_clusters(v, count):
    if count == 0: return []
    rl = []
    best = None; bestlen = 0; run = 0; start = 0
    for _pass in range(2):
        if bestlen >= count: break
        from_ = getattr(v, "alloc_hint", 0) if _pass == 0 else 0
        to_   = v.nclusters if _pass == 0 else getattr(v, "alloc_hint", 0)
        run = 0
        for c in range(from_, to_):
            if not _bit_get(v.bmp, c):
                if run == 0: start = c
                run += 1
                if run >= count: best = start; bestlen = run; break
            else:
                run = 0
    if bestlen >= count:
        _rl_push(rl, 0, best, count)
        for k in range(count): _bit_set(v.bmp, best + k, 1)
        v.alloc_hint = best + count
    else:
        left = count; vcn = 0
        for c in range(v.nclusters):
            if left == 0: break
            if _bit_get(v.bmp, c): continue
            _bit_set(v.bmp, c, 1)
            _rl_push(rl, vcn, c, 1); vcn += 1; left -= 1
    v.bmp_dirty = True
    return rl

def _attr_remove(v, rec, pos):
    ln = rd32(rec, pos + 4)
    end = _rec_used(rec)
    rec[pos:end - ln] = rec[pos + ln:end]
    rec[end - ln:end] = b"\0" * ln
    wr32(rec, 0x18, _rec_used(rec) - ln)


def _ie_size(klen, sub):
    return ALIGN_UP(16 + klen, 8) + (8 if sub else 0)

def _put_ie(buf, off, mref, key, child):
    sz = _ie_size(len(key), child >= 0)
    wr64(buf, off, mref)
    wr16(buf, off + 8, sz)
    wr16(buf, off + 10, len(key))
    wr16(buf, off + 12, IE_SUBNODE if child >= 0 else 0)
    buf[off + 16:off + 16 + len(key)] = key
    if child >= 0: wr64(buf, off + sz - 8, child)
    return sz

def _put_end(buf, off, child):
    sz = 24 if child >= 0 else 16
    buf[off:off + sz] = b"\0" * sz
    wr16(buf, off + 8, sz)
    wr16(buf, off + 12, IE_END | (IE_SUBNODE if child >= 0 else 0))
    if child >= 0: wr64(buf, off + 16, child)
    return sz

def _dir_index_set(v, dir_no, ents):
    """
    Перестроить индекс $I30 каталога dir_no из набора ents (список (mref, key)).
    ents не сортируются — должны быть УЖЕ отсортированы по name_cmp.
    Если дерево помещается в $INDEX_ROOT — пишем туда; иначе строим B-дерево
    снизу вверх с $INDEX_ALLOCATION + $BITMAP.
    """
    rec = v._mft_read(dir_no)
    if not rec or rec[:4] != b"FILE":
        raise RuntimeError(f"NTFS: нет записи MFT {dir_no}")
    # удалить старые $INDEX_ROOT/$INDEX_ALLOCATION/$BITMAP
    
    for typ in (AT_INDEX_ALLOCATION, AT_BITMAP, AT_INDEX_ROOT):
        pos = _attr_first(rec)
        while pos + 8 <= v.rec_size and rd32(rec, pos) != AT_END:
            t = rd32(rec, pos)
            if t == typ:
                # удалить ЛЮБОЙ $INDEX_ROOT/$INDEX_ALLOCATION/$BITMAP
                _attr_remove(v, rec, pos)
                continue
            pos += rd32(rec, pos + 4)

    # small value (если помещается)
    entries_bytes = sum(_ie_size(len(k), False) for (_, k) in ents)
    small_val = 32 + entries_bytes + 16
    small_attr = ALIGN_UP(0x18 + 8, 8) + small_val
    coll = COLL_FILE_NAME
    if False:   # всегда строим большое дерево ($INDEX_ALLOCATION), small value не помещается
        val = bytearray(small_val)
        wr32(val, 0, AT_FILE_NAME); wr32(val, 4, coll); wr32(val, 8, v.idx_size)
        val[12] = v.cpi & 0xFF
        h = 16
        p = 16
        for (mref, key) in ents:
            p += _put_ie(val, h + p, mref, key, -1)
        p += _put_end(val, h + p, -1)
        wr32(val, h, 16); wr32(val, h + 4, p); wr32(val, h + 8, p)
        val[h + 12] = 0

        if _attr_add_res(v, rec, AT_INDEX_ROOT, I30, 4, val, small_val) is None:
            raise RuntimeError(f"NTFS: не удалось записать $INDEX_ROOT (small) для {dir_no}")
        wr16(rec, 0x16, rd16(rec, 0x16) | MFT_IS_DIR)
        v._mft_write(dir_no, rec)
        return
    # большое дерево: строим уровни снизу вверх
    area = v.idx_size - 0x18 - (ALIGN_UP(0x28 + 2 * (v.idx_size // 512 + 1), 8) - 0x18)
    blocks = []  # list of dict: items=[(ent_idx, child)], end_child
    lvl = [(i, -1) for i in range(len(ents))]
    tail = -1
    while True:
        groups = []
        used = 0; start = 0
        endsz = 24 if tail >= 0 else 16
        for i, (ei, ch) in enumerate(lvl):
            es = _ie_size(len(ents[ei][1]), ch >= 0)
            if used + es + endsz > area and used > 0:
                groups.append((start, i)); start = i; used = 0
            used += es
        groups.append((start, len(lvl)))
        if len(groups) > 1 and groups[-1][1] - groups[-1][0] < 2:
            s, e = groups[-1]
            groups[-1] = (s - 1, e)
        first_block = len(blocks)
        parent = []
        for gi, (s, e) in enumerate(groups):
            if gi > 0: s += 1
            items = lvl[s:e]
            end_child = lvl[groups[gi + 1][0]][0] and tail if gi + 1 < len(groups) else tail
            # end_child должен быть tail или номер блока справа; для простоты:
            end_child = tail if gi + 1 == len(groups) else len(blocks)
            blocks.append({"items": items, "end_child": end_child})
            if gi > 0:
                parent.append((lvl[groups[gi][0]][0], len(blocks) - 1))
        if len(groups) == 1:
            top = first_block
            break
        lvl = parent
        tail = len(blocks) - 1
    nb = len(blocks)
    total_bytes = nb * v.idx_size
    arl = _alloc_clusters(v, DIV_UP(total_bytes, v.csize))
    vcn_per_block = v.idx_size // v.idx_vcn_unit
    for i, blk in enumerate(blocks):
        buf = bytearray(v.idx_size)
        buf[0:4] = b"INDX"
        cnt = v.idx_size // 512 + 1
        wr16(buf, 4, 0x28); wr16(buf, 6, cnt)
        wr16(buf, 0x28, 1)
        wr64(buf, 0x10, i * vcn_per_block)
        h = 0x18
        eo = ALIGN_UP(0x28 + 2 * cnt, 8) - 0x18
        p = eo
        for (ei, ch) in blk["items"]:
            mref, key = ents[ei]
            p += _put_ie(buf, h + p, mref, key, ch * vcn_per_block if ch >= 0 else -1)
        p += _put_end(buf, h + p, blk["end_child"] * vcn_per_block if blk["end_child"] >= 0 else -1)
        wr32(buf, h, eo); wr32(buf, h + 4, p); wr32(buf, h + 8, v.idx_size - 0x18)
        buf[h + 12] = 1 if any(c >= 0 for (_, c) in blk["items"]) or blk["end_child"] >= 0 else 0
        mst_fix(buf, v.idx_size)
        # записать блок по runlist arl
        _rl_write(v, arl, i * v.idx_size, buf)
    val = bytearray(56)   # 16 заголовок + 16 $INDEX_HEADER + 24 put_end
    wr32(val, 0, AT_FILE_NAME); wr32(val, 4, coll); wr32(val, 8, v.idx_size)
    val[12] = v.cpi & 0xFF
    h = 16
    p = 16 + _put_end(val, h + 16, top * vcn_per_block)
    wr32(val, h, 16); wr32(val, h + 4, p); wr32(val, h + 8, p)
    val[h + 12] = 1
    
    if _attr_add_res(v, rec, AT_INDEX_ROOT, I30, 4, val, 56) is None:
        raise RuntimeError(f"NTFS: не удалось записать $INDEX_ROOT (big) для {dir_no}")
    if _attr_add_nonres(v, rec, AT_INDEX_ALLOCATION, I30, 4, arl, total_bytes, total_bytes) is None:
        raise RuntimeError(f"NTFS: не удалось записать $INDEX_ALLOCATION для {dir_no}")
    bml = ALIGN_UP(DIV_UP(nb, 8), 8)
    bm = bytearray(bml)
    for i in range(nb): _bit_set(bm, i, 1)
    if _attr_add_res(v, rec, AT_BITMAP, I30, 4, bm, bml) is None:
        raise RuntimeError(f"NTFS: не удалось записать $BITMAP для {dir_no}")
    wr16(rec, 0x16, rd16(rec, 0x16) | MFT_IS_DIR)
    v._mft_write(dir_no, rec)

def _rl_write(v, rl, off, buf):
    """Записать buf в v по runlist rl со смещения off."""
    pos = 0
    left = len(buf)
    while left:
        vcn = off // v.csize; inside = off % v.csize
        found = None
        for (rv, rlcn, rlen) in rl:
            if rv <= vcn < rv + rlen: found = (rv, rlcn, rlen); break
        if not found: raise RuntimeError(f"NTFS: смещение {off} вне runlist")
        rv, rlcn, rlen = found
        avail = (rv + rlen - vcn) * v.csize - inside
        n = min(avail, left)
        if rlcn < 0:
            raise RuntimeError("NTFS: запись в разреженную область")
        v.d.write((rlcn + (vcn - rv)) * v.csize + inside, bytes(buf[pos:pos + n]))
        pos += n; off += n; left -= n

def _add_view_index(v, rec, nm, coll):
    w = utf8_to_utf16(nm, 8)
    val = bytearray(32 + 16)
    wr32(val, 0, 0); wr32(val, 4, coll); wr32(val, 8, v.idx_size)
    val[12] = v.cpi
    wr16(val, 16, 16); wr16(val, 20, 32); wr16(val, 24, 32); wr16(val, 32, 16); wr16(val, 36, 32); wr16(val, 40, 32)
    _attr_add_res(v, rec, AT_INDEX_ROOT, w, len(w), val, len(val))

# ========================================================================
#                        NTFS write: add / mkdir / rm
# ========================================================================

def _mft_alloc(v):
    """Найти свободную запись MFT с 24; при нехватке — расширить MFT."""
    while True:
        total = v._mft_records()
        for i in range(24, total):
            if i // 8 >= v.mbmp_bytes: break
            if _bit_get(v.mbmp, i): continue
            rec = v._mft_read(i)
            s = 1
            if rec and rec[:4] == b"FILE":
                if rd16(rec, 0x16) & MFT_IN_USE: continue
                s = rd16(rec, 0x10)
                if not s: s = 1
            _bit_set(v.mbmp, i, 1)
            v.mbmp_dirty = True
            return i, s
        _mft_extend(v, 64)

def _mft_extend(v, nrec):
    """Расширить $MFT на nrec записей: новые кластеры, обновить $MFT:$DATA."""
    add = ALIGN_UP(nrec * v.rec_size, v.csize)
    nrl = _alloc_clusters(v, add // v.csize)
    rec = v._mft_read(0)
    old_recs = v._mft_records()
    old_vcn = sum(r[2] for r in v.mft_rl)
    for (vcn, lcn, length) in nrl:
        _rl_push(v.mft_rl, old_vcn + vcn, lcn, length)
    v.mft_size += add
    # записать новые пустые записи
    for r in range(old_recs, v._mft_records()):
        nr = bytearray(v.rec_size)
        _rec_init(v, nr, r, 1, 0)
        mst_fix(nr, v.rec_size)
        _rl_write(v, v.mft_rl, r * v.rec_size, nr)
    # перезаписать $MFT:$DATA в записи 0
    pos = _attr_first(rec)
    while pos + 8 <= v.rec_size and rd32(rec, pos) != AT_END:
        if rd32(rec, pos) == AT_DATA and rec[pos+9] == 0:
            _attr_remove(v, rec, pos); break
        pos += rd32(rec, pos + 4)
    _attr_add_nonres(v, rec, AT_DATA, None, 0, v.mft_rl, v.mft_size, v.mft_size)
    # расширить $MFT:$BITMAP при необходимости
    need = ALIGN_UP(DIV_UP(v._mft_records(), 8), 8)
    if need > v.mbmp_bytes:
        v.mbmp = bytearray(v.mbmp) + bytearray(need - v.mbmp_bytes)
        v.mbmp_bytes = need
        v.mbmp_dirty = True
        # обновить $BITMAP в записи 0
        pos = _attr_first(rec)
        while pos + 8 <= v.rec_size and rd32(rec, pos) != AT_END:
            if rd32(rec, pos) == AT_BITMAP:
                _attr_remove(v, rec, pos); break
            pos += rd32(rec, pos + 4)
        _attr_add_nonres(v, rec, AT_BITMAP, None, 0, v.mbmp_rl, v.mbmp_bytes, v.mbmp_bytes)
    v._mft_write(0, rec)

def _write_bitmaps(v):
    if getattr(v, "bmp_dirty", False):
        _rl_write(v, v.bmp_rl, 0, v.bmp[:v.bmp_bytes])
        v.bmp_dirty = False
    if getattr(v, "mbmp_dirty", False):
        _rl_write(v, v.mbmp_rl, 0, v.mbmp[:v.mbmp_bytes])
        v.mbmp_dirty = False

def _rec_ref(v, no):
    rec = v._mft_read(no)
    seq = rd16(rec, 0x10) if rec and rec[:4] == b"FILE" else 0
    return MREF(no, seq)

def _dir_add_entry(v, parent, mref, fn):
    ents = _dir_collect(v, parent)
    ents.append({"mref": mref, "key": bytes(fn)})
    ents.sort(key=cmp_to_key(lambda a, b: _name_cmp(v, a["key"][66:], a["key"][64], b["key"][66:], b["key"][64])))
    _dir_index_set(v, parent, [(e["mref"], e["key"]) for e in ents])

def _dir_del_entries(v, parent, no):
    ents = [e for e in _dir_collect(v, parent) if MREF_NO(e["mref"]) != no]
    _dir_index_set(v, parent, [(e["mref"], e["key"]) for e in ents])

def _create_node(v, parent, name, is_dir, src):
    """Создать запись MFT (файл или каталог). Возвращает (no, seq, fn)."""
    w = utf8_to_utf16(name, 256)
    if not w: raise RuntimeError("пустое имя")
    no, seq = _mft_alloc(v)
    rec = bytearray(v.rec_size)
    _rec_init(v, rec, no, seq, MFT_IN_USE | (MFT_IS_DIR if is_dir else 0))
    wr16(rec, 0x12, 1)
    now = unix_to_ntfs(int(time.time()))
    mt = unix_to_ntfs(src.mtime) if src else now
    fattr = 0 if is_dir else FA_ARCHIVE
    si = _make_si(now, mt, fattr, v.default_secid)
    _attr_add_res(v, rec, AT_STANDARD_INFORMATION, None, 0, si, 72)
    size = src.size if src else 0
    ns = _choose_ns(w)
    pref = _rec_ref(v, parent)
    rl = []
    resident = True
    rbuf = None
    if not is_dir:
        room = v.rec_size - _rec_used(rec) - ALIGN_UP(66 + 2*len(w) + 0x18, 8) - 0x18 - 16
        if size > room or size > 700: resident = False
        if resident:
            rbuf = src.read(size) if size else b""
        else:
            ncl = DIV_UP(size, v.csize)
            rl = _alloc_clusters(v, ncl)
            data = src.read(size) if size else b""
            data = data + b"\0" * (ncl * v.csize - len(data))
            _rl_write(v, rl, 0, data)
    alloc = 0 if is_dir else (ALIGN_UP(size, 8) if resident else sum(r[2] for r in rl) * v.csize)
    fn = _make_fn(pref, w, ns, FA_DIR_IDX if is_dir else fattr, alloc, 0 if is_dir else size, mt)
    _attr_add_res(v, rec, AT_FILE_NAME, None, 0, fn, 66 + 2*len(w), 1)

    if is_dir:
        val = bytearray(48)
        wr32(val, 0, AT_FILE_NAME); wr32(val, 4, COLL_FILE_NAME); wr32(val, 8, v.idx_size)
        val[12] = v.cpi & 0xFF
        h = 16
        p = 16 + _put_end(val, h + 16, -1)   # $INDEX_HEADER eo=16, il=32, put_end (16 байт)
        wr32(val, h, 16); wr32(val, h + 4, p); wr32(val, h + 8, p)
        val[h + 12] = 0
        _attr_add_res(v, rec, AT_INDEX_ROOT, I30, 4, val, 48)
    elif resident:
        _attr_add_res(v, rec, AT_DATA, None, 0, rbuf or b"", size)
    else:
        _attr_add_nonres(v, rec, AT_DATA, None, 0, rl, size, size)
    v._mft_write(no, rec)
    _dir_add_entry(v, parent, MREF(no, seq), fn)
    return no, seq, fn

def _delete_node(v, no):
    if no < 16: raise RuntimeError("нельзя удалять служебные файлы NTFS")
    rec = v._mft_read(no)
    if not rec or rec[:4] != b"FILE": return
    if rd16(rec, 0x16) & MFT_IS_DIR:
        if _dir_collect(v, no): raise RuntimeError("каталог не пуст")
    parents = []
    pos = _attr_first(rec)
    while pos + 8 <= v.rec_size and rd32(rec, pos) != AT_END:
        if rd32(rec, pos) == AT_FILE_NAME and not rec[pos+8]:
            fn = bytes(rec[pos:pos + rd32(rec, pos+4)])
            p = MREF_NO(rd64(_res_value(fn), 0))
            if p not in parents: parents.append(p)
        pos += rd32(rec, pos + 4)
    # освободить кластеры нерезидентных атрибутов
    pos = _attr_first(rec)
    while pos + 8 <= v.rec_size and rd32(rec, pos) != AT_END:
        if rec[pos+8]:
            rl = _rl_decode(bytes(rec[pos:pos + rd32(rec, pos+4)]))
            _free_runs(v, rl)
        pos += rd32(rec, pos + 4)
    seq = (rd16(rec, 0x10) + 1) & 0xFFFF
    if not seq: seq = 1
    wr16(rec, 0x10, seq)
    wr16(rec, 0x16, 0)
    wr16(rec, 0x12, 0)
    v._mft_write(no, rec)
    _bit_set(v.mbmp, no, 0)
    v.mbmp_dirty = True
    for p in parents:
        _dir_del_entries(v, p, no)

class _Src:
    def __init__(self, f, size, mtime):
        self._f = f; self.size = size; self.mtime = mtime
    def read(self, n):
        return self._f.read(n)

def _ntfs_write(self, path, src):
    no, parent, last = self._resolve(path)
    if parent is None or not last: raise RuntimeError(f"не указано имя: {path}")
    if no is not None:
        # заменить: удалить старый
        prec = self._mft_read(no)
        if rd16(prec, 0x16) & MFT_IS_DIR:
            raise RuntimeError(f"{path} — каталог")
        _delete_node(self, no)
    s = _Src(src._f if hasattr(src, "_f") else src, src.size, src.mtime)
    _create_node(self, parent, last, False, s)
    _write_bitmaps(self)
    return 0

def _ntfs_mkdir(self, path):
    comps = path_split(path)
    cur = "/"
    for c in comps:
        nxt = (cur.rstrip("/") + "/" + c) if cur != "/" else "/" + c
        no, parent, last = self._resolve(nxt)
        if no is not None:
            rec = self._mft_read(no)
            if not (rd16(rec, 0x16) & MFT_IS_DIR):
                return err("%s уже файл", nxt)
        else:
            if parent is None: raise RuntimeError(f"нет каталога для {nxt}")
            _create_node(self, parent, last, True, None)
        cur = nxt
    _write_bitmaps(self)
    return 0

def _ntfs_remove(self, path):
    no, _, _ = self._resolve(path)
    if no is None: raise RuntimeError(f"не найдено: {path}")
    if no == FILE_root: raise RuntimeError("нельзя удалить корень")
    _delete_node(self, no)
    _write_bitmaps(self)
    return 0

Ntfs.write  = _ntfs_write
Ntfs.mkdir  = _ntfs_mkdir
Ntfs.remove = _ntfs_remove