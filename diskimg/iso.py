"""ISO9660 + Joliet: чтение (list/stat/read). Запись — итерация 2."""
from .common import *
from .fs import FileSystem, fsent_t
import os

SEC = 2048

class Iso(FileSystem):
    type = "ISO9660"

    def __init__(self, dev):
        self.d = dev
        if dev.size < 17 * SEC: raise RuntimeError("слишком мал для ISO")
        if dev.read(16*SEC, 8)[1:6] != b"CD001": raise RuntimeError("это не ISO9660")
        pvd = jvd = None
        s = 16
        while True:
            v = dev.read(s*SEC, SEC)
            if v[1:6] != b"CD001": break
            if v[0] == 255: break
            if v[0] == 1 and pvd is None: pvd = v
            if v[0] == 2 and v[88:91] == b"%/E": jvd = v
            s += 1
        if pvd is None: raise RuntimeError("нет Primary Volume Descriptor")
        self.joliet = jvd is not None
        use = jvd if jvd else pvd
        self.vol_blocks = rd32(pvd, 80)
        if jvd:
            self.label = utf16be_to_utf8(jvd[40:40+32], 16).rstrip(" \x00")
        else:
            self.label = pvd[40:72].decode("latin-1").rstrip(" ")
        self.root = {"name": "/", "is_dir": True, "size": 0, "lba": 0,
                     "children": [], "mtime": _date7(use, 156+18)}
        self.spool = None
        self.spool_end = 0
        self.dirty = False
        self._read_dir(self.root, rd32(use, 156+2), rd32(use, 156+10), 0)

    def _read_dir(self, node, lba, size, depth):
        if depth > 64: raise RuntimeError("ISO: слишком глубокая вложенность")
        buf = self.d.read(lba*SEC, ALIGN_UP(size, SEC))
        pos = 0
        while pos < size:
            if buf[pos] == 0:
                pos = (pos // SEC + 1) * SEC; continue
            ln = buf[pos]
            if pos + ln > size or ln < 34: break
            r = buf[pos:pos+ln]
            nl = r[32]; flags = r[25]
            if not (nl == 1 and r[33] in (0, 1)):
                if self.joliet: name = utf16be_to_utf8(r[33:33+nl], nl//2)
                else: name = r[33:33+nl].decode("latin-1")
                if ";" in name and not (flags & 2): name = name.split(";")[0]
                if name.endswith(".") and not (flags & 2): name = name[:-1]
                # многоэкстентные файлы (>4 ГБ) — пропускаем для простоты
                
                child = _node_new(name, bool(flags & 2), _date7(r, 18))
                child["size"] = rd32(r, 10)
                child["lba"] = rd32(r, 2)
                child["src"] = SRC_IMAGE
                child["src_off"] = rd32(r, 2) * SEC
                node["children"].append(child)
                child["parent"] = node
                
                
                if flags & 2:
                    self._read_dir(child, child["lba"], child["size"], depth + 1)
            pos += ln

    def _lookup(self, path):
        cur = self.root
        for c in path_split(path):
            cur = next((x for x in cur["children"] if name_ieq(x["name"], c)), None)
            if cur is None: return None
        return cur

    def list(self, path, cb):
        n = self._lookup(path)
        if n is None: return err("не найдено: %s", path)
        if not n["is_dir"]:
            cb(fsent_t(n["name"], False, n["size"], n["mtime"])); return 0
        for c in n["children"]:
            cb(fsent_t(c["name"], c["is_dir"], c["size"], c["mtime"]))
        return 0

    def stat(self, path):
        n = self._lookup(path)
        if n is None: return None
        return fsent_t(n["name"], n["is_dir"], n["size"], n["mtime"])

    def read(self, path, out):
        n = self._lookup(path)
        if n is None: return err("не найдено: %s", path)
        if n["is_dir"]: return err("%s — каталог", path)
        out.write(self.d.read(n["lba"]*SEC, n["size"]))
        return 0

    def info(self, out):
        out.write(f"Файловая система : ISO9660{' + Joliet' if self.joliet else ''}\n")
        out.write(f"Метка тома       : {self.label}\n")
        out.write(f"Размер блока     : {SEC} байт\n")
        out.write(f"Блоков в томе    : {self.vol_blocks} ({self.vol_blocks*SEC/1048576:.1f} МБ)\n")

def _date7(buf, off):
    """7-байтовая дата ISO → unix time (UTC)."""
    y = 1900 + buf[off]
    if not buf[off+1]: return 0
    return days_from_civil(y, buf[off+1], buf[off+2]) * 86400 + buf[off+3]*3600 + buf[off+4]*60 + buf[off+5]

def iso_probe(dev):
    return dev.size >= 17*SEC and dev.read(16*SEC, 6)[1:6] == b"CD001"


# ========================================================================
#                        Запись: создание и пересборка
# ========================================================================
import tempfile

SRC_NONE, SRC_IMAGE, SRC_SPOOL, SRC_HOST = 0, 1, 2, 3

def _node_new(name, is_dir, mtime=None):
    return {"name": name, "is_dir": is_dir, "size": 0, "mtime": mtime or int(__import__("time").time()),
            "children": [] if is_dir else None, "lba": 0,
            "src": SRC_NONE, "src_off": 0, "host_path": None,
            "iso_name": "", "jname": None, "jlen": 0,
            "dsize": 0, "jdsize": 0, "dirnum": 0}

def _node_add(parent, child):
    child["parent"] = parent
    parent["children"].append(child)

def _node_unlink(child):
    p = child.get("parent")
    if p and child in p["children"]:
        p["children"].remove(child)

def _scan_host(dir_node, path):
    """Рекурсивно обойти каталог хоста и заполнить дерево."""
    for name in sorted(os.listdir(path)):
        full = os.path.join(path, name)
        st = os.stat(full)
        is_dir = os.path.isdir(full)
        n = _node_new(name, is_dir, int(st.st_mtime))
        if is_dir:
            _node_add(dir_node, n)
            _scan_host(n, full)
        else:
            if st.st_size > 0xFFFFFFFF:
                raise RuntimeError(f"{full}: больше 4 ГБ — ISO не поддерживает")
            n["size"] = st.st_size
            n["src"] = SRC_HOST
            n["host_path"] = full
            _node_add(dir_node, n)

def iso_create(image_path, label, src_dir):
    iso = {"root": _node_new("/", True), "label": label or "CDROM", "joliet": True,
           "spool": None, "spool_end": 0, "dirty": False, "dev": None}
    if src_dir:
        if not os.path.isdir(src_dir): raise RuntimeError(f"{src_dir} не каталог")
        _scan_host(iso["root"], src_dir)
    tmp = image_path + ".tmp"
    total = _iso_build(iso, tmp)
    os.replace(tmp, image_path)
    return 0

# ---- имена ISO 8.3 и Joliet ----
def _dchar(c):
    if ord("a") <= c <= ord("z"): return c - 32
    if (ord("A") <= c <= ord("Z")) or (ord("0") <= c <= ord("9")) or c == ord("_"): return c
    return ord("_")

def _make_iso_names(dir_node):
    used = set()
    for c in dir_node["children"]:
        base, ext = [], []
        dot = c["name"].rfind(".") if not c["is_dir"] else -1
        if dot <= 0: dot = -1
        for ch in c["name"][:dot if dot > 0 else len(c["name"])]:
            if ch in " .": continue
            b = ord(ch)
            if b >= 0x80:
                pass   # UTF-8 байт — упрощаем до _, как в C
            base.append(_dchar(b if b < 0x80 else ord("_")))
            if len(base) >= 8: break
        if dot > 0:
            for ch in c["name"][dot+1:]:
                if ch in " .": continue
                b = ord(ch)
                ext.append(_dchar(b if b < 0x80 else ord("_")))
                if len(ext) >= 3: break
        if not base: base = [ord("_")]
        base_s = bytes(base).decode("latin-1"); ext_s = bytes(ext).decode("latin-1")
        for k in range(100000):
            if k == 0:
                b2 = base_s
            else:
                t = f"_{k}"
                b2 = base_s[:max(1, min(len(base_s), 8 - len(t)))] + t
            if c["is_dir"]:
                nm = b2
            else:
                nm = f"{b2}.{ext_s};1" if ext_s else f"{b2}.;1"
            if nm not in used:
                used.add(nm); c["iso_name"] = nm; break
        c["jname"] = utf8_to_utf16(c["name"], 64)
        c["jlen"] = len(c["jname"])
        if c["is_dir"]: _make_iso_names(c)

def _sort_children(dir_node, joliet):
    def key(c):
        if joliet:
            return [u for u in c["jname"]]
        return c["iso_name"].encode("latin-1")
    dir_node["children"].sort(key=key)
    for c in dir_node["children"]:
        if c["is_dir"]: _sort_children(c, joliet)

def _rec_len(nl): return 33 + nl + (1 if (33 + nl) & 1 else 0)
def _jname_len(c): return c["jlen"] * 2 + (4 if not c["is_dir"] else 0)

def _dir_bytes(dir_node, joliet):
    pos = 68
    for c in dir_node["children"]:
        nl = _jname_len(c) if joliet else len(c["iso_name"])
        l = _rec_len(nl)
        if pos // SEC != (pos + l - 1) // SEC:
            pos = ALIGN_UP(pos, SEC)
        pos += l
    return ALIGN_UP(pos, SEC)

def _put_both16(b, o, v): wr16(b, o, v); wrb16(b, o+2, v)
def _put_both32(b, o, v): wr32(b, o, v); wrb32(b, o+4, v)

def _put_date7(b, o, t):
    tm = __import__("time").gmtime(t)
    if not tm: b[o:o+7] = b"\0"*7; return
    b[o]   = (tm.tm_year - 1900) & 0xFF
    b[o+1] = tm.tm_mon; b[o+2] = tm.tm_mday
    b[o+3] = tm.tm_hour; b[o+4] = tm.tm_min; b[o+5] = tm.tm_sec; b[o+6] = 0

def _put_date17(b, o, t):
    tm = __import__("time").gmtime(t)
    s = f"{tm.tm_year:04d}{tm.tm_mon:02d}{tm.tm_mday:02d}{tm.tm_hour:02d}{tm.tm_min:02d}{tm.tm_sec:02d}00".encode()
    b[o:o+16] = s; b[o+16] = 0

def _put_rec(b, o, lba, size, t, is_dir, name, nl):
    l = _rec_len(nl)
    b[o:o+l] = b"\0"*l
    b[o] = l
    _put_both32(b, o+2, lba); _put_both32(b, o+10, size)
    _put_date7(b, o+18, t)
    b[o+25] = 2 if is_dir else 0
    _put_both16(b, o+28, 1)
    b[o+32] = nl
    b[o+33:o+33+nl] = name
    return l

def _collect_dirs(root, joliet):
    """BFS: каталоги в порядке уровней, родители раньше детей."""
    _sort_children(root, joliet)
    dl = [root]
    for d in dl:
        for c in d["children"]:
            if c["is_dir"]: dl.append(c)
    for i, d in enumerate(dl): d["dirnum"] = i + 1
    return dl

def _collect_files(root):
    fl = []
    def walk(d):
        for c in d["children"]:
            if c["is_dir"]: walk(c)
            else: fl.append(c)
    walk(root); return fl

def _path_table(dl, joliet, big, out=None):
    pos = 0
    for i, d in enumerate(dl):
        if i == 0:
            nm = b"\0"; nl = 1
        elif joliet:
            nm = b"".join(struct_pack_be16(u) for u in d["jname"]); nl = len(nm)
        else:
            nm = d["iso_name"].encode("latin-1"); nl = len(nm)
        if out is not None:                         # ← LBA нужен только при записи
            lba = d["jlba"] if joliet else d["lba"]
            parent = 1 if i == 0 else d["parent"]["dirnum"]
            out[pos] = nl; out[pos+1] = 0
            if big:
                wrb32(out, pos+2, lba); wrb16(out, pos+6, parent)
            else:
                wr32(out, pos+2, lba); wr16(out, pos+6, parent)
            out[pos+8:pos+8+nl] = nm
            if nl & 1: out[pos+8+nl] = 0
        pos += 8 + nl + (nl & 1)
    return pos

def struct_pack_be16(u):
    return bytes([(u >> 8) & 0xFF, u & 0xFF])

def _fill_a(b, o, n, s, d):
    b[o:o+n] = b" " * n
    for i, ch in enumerate(s or ""):
        if i >= n: break
        c = ord(ch); b[o+i] = _dchar(c) if d else (c if c < 0x80 else ord("_"))

def _fill_j(b, o, n, s):
    w = utf8_to_utf16(s or "", n // 2)
    for i in range(n // 2):
        u = w[i] if i < len(w) else 0x20
        wrb16(b, o + 2*i, u)

def _write_vd(out, joliet, label, total, pt_size, ptl, ptm, root):
    v = bytearray(SEC)
    v[0] = 2 if joliet else 1
    v[1:6] = b"CD001"; v[6] = 1
    if joliet:
        _fill_j(v, 8, 32, ""); _fill_j(v, 40, 32, label)
        v[88:91] = b"%/E"
    else:
        _fill_a(v, 8, 32, "", 0); _fill_a(v, 40, 32, label, 1)
    _put_both32(v, 80, total)
    _put_both16(v, 120, 1); _put_both16(v, 124, 1); _put_both16(v, 128, SEC)
    _put_both32(v, 132, pt_size)
    wr32(v, 140, ptl); wrb32(v, 148, ptm)
    _put_rec(v, 156, root["jlba"] if joliet else root["lba"],
             root["jdsize"] if joliet else root["dsize"], root["mtime"], True, b"\0", 1)
    if joliet:
        _fill_j(v, 190, 128, ""); _fill_j(v, 318, 128, "")
        _fill_j(v, 446, 128, ""); _fill_j(v, 574, 128, "DISKIMG")
        _fill_j(v, 702, 36, ""); _fill_j(v, 739, 36, ""); _fill_j(v, 776, 36, "")
    else:
        _fill_a(v, 190, 128, "", 0); _fill_a(v, 318, 128, "", 0)
        _fill_a(v, 446, 128, "", 0); _fill_a(v, 574, 128, "DISKIMG", 0)
        _fill_a(v, 702, 37, "", 0); _fill_a(v, 739, 37, "", 0); _fill_a(v, 776, 37, "", 0)
    now = int(__import__("time").time())
    _put_date17(v, 813, now); _put_date17(v, 830, now)
    v[847:863] = b"0"*16; v[863] = 0
    v[864:880] = b"0"*16; v[880] = 0
    v[881] = 1
    out.write(v)

def _write_dir(out, d, joliet, dev):
    size = d["jdsize"] if joliet else d["dsize"]
    me = d["jlba"] if joliet else d["lba"]
    buf = bytearray(size)
    par = d.get("parent") or d
    pos = 0
    pos += _put_rec(buf, pos, me, size, d["mtime"], True, b"\0", 1)
    pos += _put_rec(buf, pos,
                    par["jlba"] if joliet else par["lba"],
                    par["jdsize"] if joliet else par["dsize"],
                    par["mtime"], True, b"\1", 1)
    for c in d["children"]:
        if joliet:
            nm = b"".join(struct_pack_be16(u) for u in c["jname"])
            if not c["is_dir"]:
                nm += struct_pack_be16(ord(";")) + struct_pack_be16(ord("1"))
        else:
            nm = c["iso_name"].encode("latin-1")
        l = _rec_len(len(nm))
        if pos // SEC != (pos + l - 1) // SEC: pos = ALIGN_UP(pos, SEC)
        lba = (c["jlba"] if joliet else c["lba"]) if c["is_dir"] else c["lba"]
        sz = (c["jdsize"] if joliet else c["dsize"]) if c["is_dir"] else c["size"]
        if not c["is_dir"] and c["size"] == 0: lba = 0
        pos += _put_rec(buf, pos, lba, sz, c["mtime"], c["is_dir"], nm, len(nm))
    dev.seek(me * SEC)
    dev.write(bytes(buf))

def _node_copy(iso, node, out):
    left = node["size"]; off = node["src_off"]
    hf = None
    if node["src"] == SRC_HOST: hf = open(node["host_path"], "rb")
    while left:
        k = min(left, 1 << 16)
        if node["src"] == SRC_IMAGE:
            data = iso["dev"].read(off, k)
        elif node["src"] == SRC_SPOOL:
            iso["spool"].seek(off); data = iso["spool"].read(k)
        elif node["src"] == SRC_HOST:
            data = hf.read(k)
            if len(data) < k: data += b"\0" * (k - len(data))
        else:
            data = b"\0" * k
        out.write(data)
        left -= k; off += k
    if hf: hf.close()

def _iso_build(iso, out_path):
    dev = open(out_path, "w+b")
    try:
        root = iso["root"]
        # очистить имена
        def clear(d):
            for c in d["children"]:
                c["iso_name"] = ""
                if c["is_dir"]: clear(c)
        clear(root)
        _make_iso_names(root)

        # ISO-дерево: сортировка по ISO-имени, BFS
        pd = _collect_dirs(root, False)
        pt_size = _path_table(pd, False, False)
        pt_secs = DIV_UP(pt_size, SEC)
        lba = 19
        ptl = lba; lba += pt_secs
        ptm = lba; lba += pt_secs
        for d in pd: d["dsize"] = _dir_bytes(d, False)

        # Joliet-дерево: размеры каталогов и таблиц путей (без LBA!)
        jd = _collect_dirs(root, True)
        jpt_size = _path_table(jd, True, False)    # ← размер не зависит от jlba, но _path_table всё равно читает d["jlba"]...
        jpt_secs = DIV_UP(jpt_size, SEC)
        jptl = lba; lba += jpt_secs
        jptm = lba; lba += jpt_secs
        for d in jd: d["jdsize"] = _dir_bytes(d, True)

        # распределить каталоги (теперь задаём lba и jlba)
        for d in pd: d["lba"] = lba; lba += d["dsize"] // SEC
        for d in jd: d["jlba"] = lba; lba += d["jdsize"] // SEC
        # файлы
        fl = _collect_files(root)
        for f in fl:
            f["lba"] = lba if f["size"] else 0
            lba += DIV_UP(f["size"], SEC)
        total = lba if lba >= 32 else 32

        # запись
        zero = b"\0" * SEC
        dev.seek(0)
        for _ in range(16): dev.write(zero)
        _write_vd(dev, False, iso["label"], total, pt_size, ptl, ptm, root)
        _write_vd(dev, True,  iso["label"], total, jpt_size, jptl, jptm, root)
        term = bytearray(SEC); term[0] = 255; term[1:6] = b"CD001"; term[6] = 1
        dev.write(bytes(term))

        # таблицы путей ISO
        buf = bytearray(pt_secs * SEC)
        _path_table(pd, False, False, buf); dev.seek(ptl * SEC); dev.write(buf)
        buf = bytearray(pt_secs * SEC)
        _path_table(pd, False, True, buf); dev.seek(ptm * SEC); dev.write(buf)
        for d in pd: _write_dir(dev, d, False, dev)
        # таблицы путей Joliet
        buf = bytearray(jpt_secs * SEC)
        _path_table(jd, True, False, buf); dev.seek(jptl * SEC); dev.write(buf)
        buf = bytearray(jpt_secs * SEC)
        _path_table(jd, True, True, buf); dev.seek(jptm * SEC); dev.write(buf)
        for d in jd: _write_dir(dev, d, True, dev)
        # данные файлов
        for f in fl:
            if not f["size"]: continue
            dev.seek(f["lba"] * SEC)
            _node_copy(iso, f, dev)
            pad = ALIGN_UP(f["size"], SEC) - f["size"]
            if pad: dev.write(b"\0" * pad)
        dev.flush()
    finally:
        dev.close()
    return total

# ---- Iso: запись/удаление/каталоги/пересборка ----
def _iso_write(self, path, src):
    comps = path_split(path)
    if not comps: return err("не указано имя файла")
    cur = self.root
    for c in comps[:-1]:
        cur = next((x for x in cur["children"] if name_ieq(x["name"], c)), None)
        if cur is None or not cur["is_dir"]: return err("нет каталога %s", "/".join(comps[:-1]))
    name = comps[-1]
    old = next((x for x in cur["children"] if name_ieq(x["name"], name)), None)
    if old and old["is_dir"]: return err("%s — каталог", path)
    if not self.spool: self.spool = tempfile.TemporaryFile()
    self.spool.seek(self.spool_end)
    left = src.size
    while left:
        k = min(left, 1 << 16)
        chunk = src.read(k)
        if len(chunk) < k: chunk += b"\0" * (k - len(chunk))
        self.spool.write(chunk); left -= k
    if old:
        old["size"] = src.size; old["mtime"] = src.mtime
        old["src"] = SRC_SPOOL; old["src_off"] = self.spool_end
        old["name"] = name
    else:
        n = _node_new(name, False, src.mtime)
        n["size"] = src.size; n["src"] = SRC_SPOOL; n["src_off"] = self.spool_end
        _node_add(cur, n)
    self.spool_end += src.size
    self.dirty = True
    return 0

def _iso_mkdir(self, path):
    comps = path_split(path)
    if not comps: return 0
    cur = self.root
    for c in comps:
        nxt = next((x for x in cur["children"] if name_ieq(x["name"], c)), None)
        if nxt is None:
            if not cur["is_dir"]: return err("%s — не каталог", cur["name"])
            nxt = _node_new(c, True)
            _node_add(cur, nxt)
            self.dirty = True
        elif not nxt["is_dir"]: return err("%s уже файл", c)
        cur = nxt
    return 0

def _iso_remove(self, path):
    comps = path_split(path)
    if not comps: return err("нельзя удалить корень")
    cur = self.root
    for c in comps[:-1]:
        cur = next((x for x in cur["children"] if name_ieq(x["name"], c)), None)
        if cur is None: return err("не найдено: %s", path)
    name = comps[-1]
    n = next((x for x in cur["children"] if name_ieq(x["name"], name)), None)
    if n is None: return err("не найдено: %s", path)
    if n["is_dir"] and n["children"]: return err("каталог %s не пуст", path)
    _node_unlink(n)
    self.dirty = True
    return 0

def _iso_flush(self):
    if not self.dirty: return 0
    tmp = tempfile.NamedTemporaryFile(delete=False)
    name = tmp.name; tmp.close()
    try:
        total = _iso_build({"root": self.root, "label": self.label, "joliet": True,
                            "spool": self.spool, "spool_end": self.spool_end, "dev": self.d}, name)
        # переписать содержимое в d через bdev_replace
        from .bdev import bdev_replace
        rc = bdev_replace(self.d, name, total * SEC)
        # перечитать дерево
        if rc == 0:
            self.__init__(self.d)   # переинициализация
    finally:
        os.unlink(name)
    return 0

Iso.write  = _iso_write
Iso.mkdir  = _iso_mkdir
Iso.remove = _iso_remove
Iso.flush  = _iso_flush