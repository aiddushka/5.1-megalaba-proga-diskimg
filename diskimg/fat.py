"""FAT12/16/32: чтение (list/stat/read). Запись — следующая итерация."""
from .common import *
from .fs import FileSystem, fsent_t

ATTR_RO, ATTR_HIDDEN, ATTR_SYSTEM, ATTR_VOLUME = 0x01, 0x02, 0x04, 0x08
ATTR_DIR, ATTR_ARCH, ATTR_LFN = 0x10, 0x20, 0x0F

class Fat(FileSystem):
    type = "FAT"

    def __init__(self, dev):
        self.d = dev
        b = dev.read(0, 512)
        if not fat_probe(b):
            raise RuntimeError("это не том FAT")
        self.bps   = rd16(b, 11)
        self.spc   = b[13]
        self.csize = self.bps * self.spc
        self.resv  = rd16(b, 14)
        self.nfats = b[16]
        self.root_entries = rd16(b, 17)
        self.total_secs = rd16(b, 19) or rd32(b, 32)
        self.fatsz = rd16(b, 22) or rd32(b, 36)
        self.root_secs = DIV_UP(self.root_entries * 32, self.bps)
        data_secs = self.total_secs - self.resv - self.nfats * self.fatsz - self.root_secs
        self.nclusters = data_secs // self.spc
        self.bits = 12 if self.nclusters < 4085 else 16 if self.nclusters < 65525 else 32
        self.fat_off  = self.resv * self.bps
        self.root_off = self.fat_off + self.nfats * self.fatsz * self.bps
        self.data_off = self.root_off + self.root_secs * self.bps
        eb = b[64 if self.bits == 32 else 36:]
        self.serial = rd32(eb, 3)
        self.label  = eb[7:18].decode("latin-1").rstrip(" \0") if eb[2] == 0x29 else "NO NAME"
        self.root_clus = rd32(b, 44) if self.bits == 32 else 0
        self.fsinfo = rd16(b, 48) if self.bits == 32 else 0
        self._load_fat()

    # ---- FAT ----
    def _load_fat(self):
        raw = self.d.read(self.fat_off, self.fatsz * self.bps)
        n = self.nclusters + 2
        self.fat = [0] * n
        if self.bits == 12:
            for i in range(n):
                o = i + i // 2
                if o + 1 >= len(raw): break
                v = rd16(raw, o)
                self.fat[i] = (v >> 4) if (i & 1) else (v & 0xFFF)
        elif self.bits == 16:
            for i in range(n):
                if 2*i + 1 >= len(raw): break
                self.fat[i] = rd16(raw, 2*i)
        else:
            for i in range(n):
                if 4*i + 3 >= len(raw): break
                self.fat[i] = rd32(raw, 4*i) & 0x0FFFFFFF

    def _eoc(self): return 0xFFF if self.bits == 12 else 0xFFFF if self.bits == 16 else 0x0FFFFFFF
    def _is_eoc(self, v):
        return v >= (0xFF8 if self.bits == 12 else 0xFFF8 if self.bits == 16 else 0x0FFFFFF8)
    def _valid(self, c): return 2 <= c < self.nclusters + 2
    def _clus_off(self, c): return self.data_off + (c - 2) * self.csize

    def chain_list(self, c):
        out = []
        seen = set()
        while self._valid(c):
            if c in seen or len(out) > self.nclusters: break
            seen.add(c); out.append(c)
            nx = self.fat[c]
            if self._is_eoc(nx): break
            c = nx
        return out

    def _io_clusters(self, chain, buf_len, wr=False, buf=None):
        """Читает/пишет непрерывными кусками (как io_clusters в fat.c)."""
        i = 0; n = len(chain)
        if wr:
            pos = 0
            while i < n:
                j = i + 1
                while j < n and chain[j] == chain[j-1] + 1 and j - i < 256: j += 1
                self.d.write(self._clus_off(chain[i]), bytes(buf[pos:pos + (j-i)*self.csize]))
                pos += (j-i)*self.csize; i = j
            return b""
        out = bytearray()
        while i < n:
            j = i + 1
            while j < n and chain[j] == chain[j-1] + 1 and j - i < 256: j += 1
            out += self.d.read(self._clus_off(chain[i]), (j-i)*self.csize)
            i = j
        return bytes(out[:buf_len])

    # ---- каталоги ----
    def _dir_load(self, clus):
        if clus == 0:
            return 0, bytearray(self.d.read(self.root_off, self.root_entries * 32))
        chain = self.chain_list(clus)
        return chain, bytearray(self._io_clusters(chain, len(chain) * self.csize))


    def _parse(self, buf):
        ents = []
        lfn = [0] * 260; lfn_ok = False; lfn_next = 0; lfn_first = -1; lfn_sum = 0
        offs = [1,3,5,7,9,14,16,18,20,22,24,28,30]
        for i in range(0, len(buf), 32):
            e = buf[i:i+32]
            if len(e) < 32: break
            if e[0] == 0x00: break
            if e[0] == 0xE5: lfn_ok = False; continue
            if (e[11] & 0x3F) == ATTR_LFN:
                ord_ = e[0] & 0x1F
                if e[0] & 0x40:
                    lfn_ok = 1 <= ord_ <= 20
                    lfn_next = ord_; lfn_sum = e[13]; lfn_first = i
                    lfn = [0xFFFF] * 260
                if not lfn_ok or ord_ != lfn_next or e[13] != lfn_sum:
                    lfn_ok = False; continue
                base = (ord_ - 1) * 13
                for k in range(13): lfn[base + k] = rd16(e, offs[k])
                lfn_next -= 1
                continue
            if e[11] & ATTR_VOLUME: lfn_ok = False; continue
            if e[0] == ord('.') and e[1] in (ord(' '), ord('.')): lfn_ok = False; continue
            name = ""
            if lfn_ok and lfn_next == 0 and _lfn_checksum(e) == lfn_sum:
                ln = 0
                while ln < 260 and lfn[ln] not in (0, 0xFFFF): ln += 1
                name = utf16_to_utf8(lfn[:ln])
            else:
                name = _sfn_name(e, self.bits)
            ents.append({
                "name": name, "attr": e[11],
                "clus": rd16(e, 26) | ((rd16(e, 20) << 16) if self.bits == 32 else 0),
                "size": rd32(e, 28),
                "wtime": rd16(e, 22), "wdate": rd16(e, 24),
                "sfn_slot": i,
            })
            lfn_ok = False
        return ents

    def _find(self, buf, name):
        for e in self._parse(buf):
            if name_ieq(e["name"], name) or name_ieq(_sfn_name(buf[e["sfn_slot"]:e["sfn_slot"]+32], self.bits), name):
                return e
        return None

    def _root(self): return self.root_clus if self.bits == 32 else 0

    def _resolve(self, path):
        comps = path_split(path)
        cur = self._root()
        if not comps:
            return {"name": "/", "attr": ATTR_DIR, "clus": cur, "size": 0, "wdate": 0, "wtime": 0}, None
        last = None
        for i, comp in enumerate(comps):
            chain, buf = self._dir_load(cur)
            e = self._find(buf, comp)
            if i == len(comps) - 1:
                return e, cur
            if not e or not (e["attr"] & ATTR_DIR): return None, cur
            cur = e["clus"] or self._root()
            last = e
        return last, cur

    # ---- fs API ----
    def list(self, path, cb):
        e, _ = self._resolve(path)
        if not e: return err("не найдено: %s", path)
        if not (e["attr"] & ATTR_DIR):
            cb(fsent_t(e["name"], False, e["size"], fat_to_unix(e["wdate"], e["wtime"])))
            return 0
        chain, buf = self._dir_load(e["clus"] if e["attr"] & ATTR_DIR else self._root())
        for x in self._parse(buf):
            cb(fsent_t(x["name"], bool(x["attr"] & ATTR_DIR),
                      0 if x["attr"] & ATTR_DIR else x["size"],
                      fat_to_unix(x["wdate"], x["wtime"])))
        return 0

    def stat(self, path):
        e, _ = self._resolve(path)
        if not e: return None
        return fsent_t(e["name"], bool(e["attr"] & ATTR_DIR), e["size"],
                       fat_to_unix(e["wdate"], e["wtime"]))

    def read(self, path, out):
        e, _ = self._resolve(path)
        if not e: return err("не найдено: %s", path)
        if e["attr"] & ATTR_DIR: return err("%s — каталог", path)
        chain = self.chain_list(e["clus"])
        if len(chain) * self.csize < e["size"]:
            return err("цепочка кластеров короче размера файла")
        data = self._io_clusters(chain, e["size"])
        out.write(data)
        return 0

    def write(self, path, src): raise NotImplementedError("write — следующая итерация")
    def mkdir(self, path):      raise NotImplementedError("mkdir — следующая итерация")
    def remove(self, path):     raise NotImplementedError("remove — следующая итерация")
    def flush(self): return 0

    def info(self, out):
        free = sum(1 for c in range(2, self.nclusters + 2) if not self.fat[c])
        out.write(f"Файловая система : FAT{self.bits}\n")
        out.write(f"Метка тома       : {self.label}\n")
        out.write(f"Серийный номер   : {self.serial>>16:04X}-{self.serial&0xFFFF:04X}\n")
        out.write(f"Байт в секторе   : {self.bps}\n")
        out.write(f"Размер кластера  : {self.csize} байт\n")
        out.write(f"Секторов всего   : {self.total_secs}\n")
        out.write(f"Копий FAT        : {self.nfats} по {self.fatsz} секторов\n")
        if self.bits == 32: out.write(f"Кластер корня    : {self.root_clus}\n")
        else:               out.write(f"Записей в корне  : {self.root_entries}\n")
        out.write(f"Кластеров        : {self.nclusters} (свободно {free}, {free*self.csize/1024:.1f} КБ)\n")


####################### отсюда начинается логика add, rm, mkdir, create
    def _fat_store(self):
        if not getattr(self, "fat_dirty", False): return 0
        self.modified = True
        bytes_ = self.fatsz * self.bps
        raw = bytearray(self.d.read(self.fat_off, bytes_))
        n = self.nclusters + 2
        for i in range(n):
            if self.bits == 12:
                o = i + i // 2
                if o + 1 >= bytes_: break
                v = rd16(raw, o)
                if i & 1: v = (v & 0x000F) | ((self.fat[i] & 0xFFF) << 4)
                else:     v = (v & 0xF000) | (self.fat[i] & 0xFFF)
                wr16(raw, o, v)
            elif self.bits == 16:
                if 2*i + 1 >= bytes_: break
                wr16(raw, 2*i, self.fat[i] & 0xFFFF)
            else:
                if 4*i + 3 >= bytes_: break
                wr32(raw, 4*i, (rd32(raw, 4*i) & 0xF0000000) | (self.fat[i] & 0x0FFFFFFF))
        for k in range(self.nfats):
            self.d.write(self.fat_off + k * bytes_, bytes(raw))
        self.fat_dirty = False
        return 0

    def _count_free(self):
        return sum(1 for c in range(2, self.nclusters + 2) if not self.fat[c])

    def _free_chain(self, c):
        guard = 0
        while self._valid(c) and guard < self.nclusters + 2:
            guard += 1
            nx = self.fat[c]; self.fat[c] = 0
            if c < getattr(self, "next_free", 2): self.next_free = c
            if self._is_eoc(nx) or nx < 2: break
            c = nx
        self.fat_dirty = True

    def _alloc_clusters(self, n):
        if not n: return []
        if self._count_free() < n:
            raise RuntimeError(f"на томе недостаточно места (нужно {n} кластеров)")
        got = []
        # сначала ищем непрерывный участок
        run = 0; start = 0
        for c in range(2, self.nclusters + 2):
            if not self.fat[c]:
                if not run: start = c
                run += 1
                if run >= n:
                    got = list(range(start, start + n)); break
            else:
                run = 0
        if not got:
            c = getattr(self, "next_free", 2) or 2
            scanned = 0
            while len(got) < n and scanned < self.nclusters:
                if c >= self.nclusters + 2: c = 2
                if not self.fat[c]: got.append(c)
                c += 1; scanned += 1
        for i, c in enumerate(got):
            self.fat[c] = got[i+1] if i + 1 < n else self._eoc()
        self.next_free = got[-1] + 1
        self.fat_dirty = True
        return got

    # ---------------- каталоги: запись ----------------
    def _dir_store(self, clus, chain, buf):
        if clus == 0:
            self.d.write(self.root_off, bytes(buf[:self.root_entries*32]))
        else:
            self._io_clusters(chain, len(buf), wr=True, buf=buf)

    def _dir_grow(self, clus, chain, buf):
        if clus == 0:
            raise RuntimeError(f"корневой каталог FAT12/16 заполнен ({self.root_entries} записей)")
        new = self._alloc_clusters(1)
        self.fat[chain[-1]] = new[0]; self.fat_dirty = True
        chain = chain + new
        buf = buf + bytearray(self.csize)   # было b"\0"*... — теперь bytearray
        return chain, buf


    def _dir_find_slots(self, clus, chain, buf, need):
        while True:
            slots = len(buf) // 32
            run = 0
            for i in range(slots):
                c = buf[i*32]
                if c == 0x00:
                    if slots - i + run >= need: return i - run, chain, buf
                    break
                if c == 0xE5:
                    run += 1
                    if run == need: return i - need + 1, chain, buf
                else:
                    run = 0
            chain, buf = self._dir_grow(clus, chain, buf)

    def _sfn_exists(self, buf, sfn):
        for i in range(0, len(buf) - 31, 32):
            e = buf[i:i+32]
            if e[0] == 0: break
            if e[0] == 0xE5 or (e[11] & 0x3F) == ATTR_LFN: continue
            if bytes(e[:11]) == sfn: return True
        return False

    def _dir_add(self, clus, chain, buf, name, attr, cclus, size, mt):
        sfn, nt = bytearray(11), 0
        need_lfn = True
        ok83, nt83 = _fits_83(name, sfn)
        if ok83 and not self._sfn_exists(buf, bytes(sfn)):
            need_lfn = False
            nt = nt83
        if need_lfn:
            lfn = utf8_to_utf16(name, 255)
            nl = DIV_UP(len(lfn), 13) if lfn else 0
            self._make_sfn(buf, name, sfn); nt = 0
        else:
            nl = 0
        slot, chain, buf = self._dir_find_slots(clus, chain, buf, nl + 1)
        sum_ = _lfn_checksum(bytes(sfn))
        offs = [1,3,5,7,9,14,16,18,20,22,24,28,30]
        for k in range(nl):
            ord_ = nl - k
            e = bytearray(32); e[0] = ord_ | (0x40 if k == 0 else 0); e[11] = ATTR_LFN; e[13] = sum_
            for j in range(13):
                idx = (ord_ - 1) * 13 + j
                ch = lfn[idx] if idx < len(lfn) else (0 if idx == len(lfn) else 0xFFFF)
                wr16(e, offs[j], ch)
            buf[(slot+k)*32:(slot+k+1)*32] = e
        e = bytearray(32)
        e[:11] = sfn
        e[11] = attr; e[12] = nt
        d, t = unix_to_fat(mt)
        wr16(e, 14, t); wr16(e, 16, d); wr16(e, 18, d)
        wr16(e, 22, t); wr16(e, 24, d)
        wr16(e, 20, (cclus >> 16) if self.bits == 32 else 0)
        wr16(e, 26, cclus & 0xFFFF)
        wr32(e, 28, size)
        buf[(slot+nl)*32:(slot+nl+1)*32] = e
        return slot + nl, chain, buf

    def _make_sfn(self, buf, name, sfn):
        base, ext = bytearray(), bytearray()
        dot = name.rfind(".")
        if dot <= 0: dot = -1
        for ch in name[:dot if dot > 0 else len(name)]:
            if ch in " .": continue
            c = ord(ch)
            if c >= 0x80 or not (32 < c < 127) or ch in '"*+,./:;<=>?[\\]|': c = ord("_")
            if ord("a") <= c <= ord("z"): c -= 32
            if len(base) < 8: base.append(c)
        if dot > 0:
            for ch in name[dot+1:]:
                if ch in " .": continue
                c = ord(ch)
                if c >= 0x80 or not (32 < c < 127) or ch in '"*+,./:;<=>?[\\]|': c = ord("_")
                if ord("a") <= c <= ord("z"): c -= 32
                if len(ext) < 3: ext.append(c)
        if not base: base.append(ord("_"))
        for n in range(1, 1_000_000):
            tail = f"~{n}".encode()
            keep = min(len(base), 8 - len(tail))
            sfn[:] = b" " * 11
            sfn[0:keep] = base[:keep]; sfn[keep:keep+len(tail)] = tail
            sfn[8:8+len(ext)] = ext
            if not self._sfn_exists(buf, bytes(sfn)): return

    # ---------------- fs API: запись ----------------
    def write(self, path, src):
        comps = path_split(path)
        if not comps: return err("не указано имя файла")
        parent_path = "/" + "/".join(comps[:-1]) if len(comps) > 1 else "/"
        name = comps[-1]
        pe, _ = self._resolve(parent_path)
        if pe is None or not (pe["attr"] & ATTR_DIR): return err("нет каталога %s", parent_path)
        pclus = pe["clus"]
        chain, buf = self._dir_load(pclus)
        old = self._find(buf, name)
        if old is not None:
            if old["attr"] & ATTR_DIR: return err("%s — каталог", path)
            self._free_chain(old["clus"])
            for i in range(old["sfn_slot"]*32 - 32*20, old["sfn_slot"]*32 + 32, 32):
                if 0 <= i < len(buf) and buf[i] == 0xE5 or (buf[i+11] & 0x3F) == ATTR_LFN:
                    buf[i:i+32] = b"\xE5" * 1 + buf[i+1:i+32]
            # надёжнее: удалить все LFN + SFN этого файла
            _del_entry(buf, old)
            name = old["name"]  # сохранить исходное написание
        src.seek(0); data = src.read()
        nclus = DIV_UP(len(data), self.csize)
        cl = self._alloc_clusters(nclus) if nclus else []
        if cl:
            full = data + b"\0" * (nclus*self.csize - len(data))
            self._io_clusters(cl, len(full), wr=True, buf=full)
        _, chain, buf = self._dir_add(pclus, chain, buf, name, ATTR_ARCH,
                                      cl[0] if cl else 0, len(data), src.mtime if hasattr(src, "mtime") else 0)
        self._dir_store(pclus, chain, buf)
        return self._fat_store()

    def mkdir(self, path):
        comps = path_split(path)
        if not comps: return 0
        # создать всех родителей (как mkdir -p)
        cur = "/"
        for comp in comps:
            nxt = (cur.rstrip("/") + "/" + comp) if cur != "/" else "/" + comp
            st = self.stat(nxt)
            if st is None:
                if self._mkdir_one(cur, comp): return -1
            cur = nxt
        return 0

    def _mkdir_one(self, parent_path, name):
        pe, _ = self._resolve(parent_path)
        if pe is None or not (pe["attr"] & ATTR_DIR): return err("нет каталога %s", parent_path)
        pclus = pe["clus"]
        cl = self._alloc_clusters(1); c = cl[0]
        t = int(__import__("time").time()); d, tim = unix_to_fat(t)
        pb = bytearray(self.csize)
        p_clus = 0 if pclus == self._root() else pclus
        for k in range(2):
            e = bytearray(32); e[:11] = b" " * 11
            e[0] = ord(".")
            if k: e[1] = ord(".")
            e[11] = ATTR_DIR
            cc = p_clus if k else c
            wr16(e, 14, tim); wr16(e, 16, d); wr16(e, 18, d)
            wr16(e, 22, tim); wr16(e, 24, d)
            wr16(e, 20, (cc >> 16) if self.bits == 32 else 0)
            wr16(e, 26, cc & 0xFFFF)
            pb[k*32:(k+1)*32] = e
        self.d.write(self._clus_off(c), bytes(pb))
        chain, buf = self._dir_load(pclus)
        _, chain, buf = self._dir_add(pclus, chain, buf, name, ATTR_DIR, c, 0, t)
        self._dir_store(pclus, chain, buf)
        return self._fat_store()

    def remove(self, path):
        comps = path_split(path)
        if not comps: return err("нельзя удалить корень")
        parent_path = "/" + "/".join(comps[:-1]) if len(comps) > 1 else "/"
        name = comps[-1]
        pe, _ = self._resolve(parent_path)
        if pe is None: return err("не найдено: %s", path)
        pclus = pe["clus"]
        chain, buf = self._dir_load(pclus)
        e = self._find(buf, name)
        if e is None: return err("не найдено: %s", path)
        if e["attr"] & ATTR_DIR:
            _, sub = self._dir_load(e["clus"])
            if any((sub[i] != 0 and sub[i] != 0xE5) for i in range(0, len(sub), 32)):
                # каталог не пуст
                files = [x for x in self._parse(sub)]
                if files: return err("каталог %s не пуст", path)
        if e["clus"]: self._free_chain(e["clus"])
        _del_entry(buf, e)
        self._dir_store(pclus, chain, buf)
        return self._fat_store()

    def flush(self):
        return self._fat_store()

    def close(self):
        self.flush()
# ---- helpers ----
def _lfn_checksum(e):
    s = 0
    for i in range(11): s = (((s & 1) << 7) + (s >> 1) + e[i]) & 0xFF
    return s

def _sfn_name(e, bits):
    out = []
    nt = e[12]
    for i in range(8):
        if e[i] == ord(' '): break
        c = 0xE5 if (i == 0 and e[0] == 0x05) else e[i]
        if (nt & 0x08) and 0x41 <= c <= 0x5A: c += 32
        out.append(chr(c) if c < 0x80 else '_')
    if e[8] != ord(' '):
        out.append('.')
        for i in range(8, 11):
            if e[i] == ord(' '): break
            c = e[i]
            if (nt & 0x10) and 0x41 <= c <= 0x5A: c += 32
            out.append(chr(c) if c < 0x80 else '_')
    return "".join(out)

def fat_probe(b):
    if b[0] not in (0xEB, 0xE9): return False
    bps = rd16(b, 11); spc = b[13]
    if bps not in (512, 1024, 2048, 4096): return False
    if not spc or (spc & (spc - 1)): return False
    if not rd16(b, 14) or not b[16] or b[16] > 4: return False
    if b[3:11] == b"NTFS    ": return False
    total = rd16(b, 19) or rd32(b, 32)
    return total != 0

def _fits_83(name, sfn_out):
    dot = name.rfind(".")
    if dot <= 0: bl, el = len(name), 0
    else: bl, el = dot, len(name) - dot - 1
    if bl == 0 or bl > 8 or el > 3 or (dot > 0 and el == 0): return False, 0
    if dot > 0 and name.find(".") != dot: return False, 0
    sfn = bytearray(b" " * 11)
    bup = blo = eup = elo = False
    for i in range(bl):
        c = ord(name[i])
        if ord("a") <= c <= ord("z"): blo = True; c -= 32
        elif ord("A") <= c <= ord("Z"): bup = True
        if not (32 < c < 127) or chr(c) in '"*+,./:;<=>?[\\]| ': return False, 0
        sfn[i] = c
    for i in range(el):
        c = ord(name[dot+1+i])
        if ord("a") <= c <= ord("z"): elo = True; c -= 32
        elif ord("A") <= c <= ord("Z"): eup = True
        if not (32 < c < 127) or chr(c) in '"*+,./:;<=>?[\\]| ': return False, 0
        sfn[8+i] = c
    if (bup and blo) or (eup and elo): return False, 0
    nt = (0x08 if blo else 0) | (0x10 if elo else 0)
    if sfn[0] == 0xE5: sfn[0] = 0x05
    sfn_out[:] = sfn
    return True, nt

def _del_entry(buf, e):
    start = e["sfn_slot"]
    while start >= 32 and (buf[start-32+11] & 0x3F) == ATTR_LFN:
        start -= 32
    for i in range(start, e["sfn_slot"] + 32, 32):
        buf[i] = 0xE5


def fat_format(dev, opts):
    """opts: label, fat_bits, cluster_size, hidden_sectors, floppy"""
    total = dev.size // 512
    media, spt, heads = 0xF8, 63, 255
    spc, root_entries, resv, nfats = 0, 512, 1, 2
    bits = opts.get("fat_bits") or 0
    if opts.get("floppy"):
        # таблица дискет (1.44M и т.п.)
        floppies = {160:(0xFE,1,8,1,64),180:(0xFC,1,9,1,64),320:(0xFF,2,8,2,112),
                    360:(0xFD,2,9,2,112),720:(0xF9,2,9,2,112),1200:(0xF9,1,15,2,224),
                    1440:(0xF0,1,18,2,224),2880:(0xF0,2,36,2,240)}
        kb = total // 2
        if kb in floppies:
            media, spc, spt, heads, root_entries = floppies[kb]
        else:
            media, spt, heads, root_entries = 0xF0, 18, 2, 224
        if not bits: bits = 12
    if not bits:
        bits = 12 if total <= 8400 else 16 if total < 1048576 else 32
    if bits == 32: resv, root_entries = 32, 0
    root_secs = DIV_UP(root_entries * 32, 512)
    if opts.get("cluster_size"):
        cs = opts["cluster_size"]
        if cs % 512 or (cs & (cs-1)) or cs > 65536:
            raise RuntimeError(f"недопустимый размер кластера {cs}")
        spc = cs // 512
    if not spc:
        if bits == 16:
            spc = 2 if total <= 32680 else 4 if total <= 262144 else 8 if total <= 524288 else \
                  16 if total <= 1048576 else 32 if total <= 2097152 else 64 if total <= 4194304 else 128
        elif bits == 32:
            spc = 1 if total <= 532480 else 8 if total <= 16777216 else 16 if total <= 33554432 else \
                  32 if total <= 67108864 else 64
        else:
            spc = 1
    fatsz = ncl = 0
    for _ in range(16):
        # подобрать размер FAT
        for fs in range(1, 0x800000):
            data = total - resv - nfats*fs - root_secs
            if data <= 0: raise RuntimeError("том слишком мал")
            cl = data // spc
            cap = fs * 512 * 8 // bits
            if cap >= cl + 2: fatsz, ncl = fs, cl; break
        ok = (bits == 12 and 1 <= ncl < 4085) or (bits == 16 and 4085 <= ncl < 65525) or (bits == 32 and 65525 <= ncl < 0x0FFFFFF5)
        if ok: break
        if opts.get("cluster_size"): raise RuntimeError(f"при кластере {opts['cluster_size']} — {ncl} кластеров, не подходит для FAT{bits}")
        if bits in (12,16) and ncl >= (4085 if bits == 12 else 65525):
            if spc >= 128: break
            spc *= 2
        else:
            if spc == 1: break
            spc //= 2
    if not ok: raise RuntimeError(f"размер тома не подходит для FAT{bits} ({ncl} кластеров)")

    # --- загрузочный сектор ---
    b = bytearray(512)
    b[0], b[1], b[2] = 0xEB, 0x58 if bits == 32 else 0x3C, 0x90
    b[3:11] = b"DISKIMG "
    wr16(b, 11, 512); b[13] = spc; wr16(b, 14, resv); b[16] = nfats
    wr16(b, 17, root_entries)
    if total < 65536 and bits != 32: wr16(b, 19, total)
    else: wr32(b, 32, total)
    b[21] = media
    if bits != 32: wr16(b, 22, fatsz)
    wr16(b, 24, spt); wr16(b, 26, heads); wr32(b, 28, opts.get("hidden_sectors", 0))
    if bits == 32:
        wr32(b, 36, fatsz); wr32(b, 44, 2); wr16(b, 48, 1); wr16(b, 50, 6)
        eb_off = 64
    else:
        eb_off = 36
    b[eb_off] = 0x00 if opts.get("floppy") else 0x80
    b[eb_off + 2] = 0x29
    wr32(b, eb_off + 3, rand32())
    lab = bytearray(b" " * 11)
    for i, ch in enumerate((opts.get("label") or "NO NAME")[:11]):
        c = ord(ch)
        if ord("a") <= c <= ord("z"): c -= 32
        lab[i] = c if c < 0x80 else ord("_")
    b[eb_off + 7:eb_off + 18] = lab
    b[eb_off + 18:eb_off + 26] = b"FAT12   " if bits == 12 else b"FAT16   " if bits == 16 else b"FAT32   "

    from . import bootcode
    if bits == 32: b[0x5A:0x5A+len(bootcode.vbr_stub_fat32)] = bootcode.vbr_stub_fat32
    else:          b[0x3E:0x3E+len(bootcode.vbr_stub_fat16)] = bootcode.vbr_stub_fat16
    b[510:512] = b"\x55\xAA"

    # обнулить служебную область
    sys_end = (resv + nfats*fatsz + root_secs + (spc if bits == 32 else 0)) * 512
    dev.write(0, b"\0" * sys_end)
    dev.write(0, bytes(b))
    
    if bits == 32:
        fi = bytearray(512)
        wr32(fi, 0, 0x41615252)          # ← добавлен offset 0
        wr32(fi, 484, 0x61417272)
        wr32(fi, 488, ncl - 1)
        wr32(fi, 492, 3)
        wr32(fi, 508, 0xAA550000)
        dev.write(512, bytes(fi))
        dev.write(6*512, bytes(b)); dev.write(7*512, bytes(fi))

    # первые записи FAT
    fh = bytearray(12)
    if bits == 12: fh[0] = media; fh[1] = 0xFF; fh[2] = 0xFF
    elif bits == 16: wr16(fh, 0, 0xFF00 | media); wr16(fh, 2, 0xFFFF)
    else: wr32(fh, 0, 0x0FFFFF00 | media); wr32(fh, 4, 0x0FFFFFFF); wr32(fh, 8, 0x0FFFFFFF)
    for k in range(nfats):
        dev.write((resv + k*fatsz) * 512, bytes(fh[:12 if bits == 32 else 4 if bits == 16 else 3]))
    # метка в корне
    if opts.get("label"):
        e = bytearray(32); e[:11] = lab; e[11] = ATTR_VOLUME
        d, t = unix_to_fat(int(__import__("time").time()))
        wr16(e, 22, t); wr16(e, 24, d)
        dev.write((resv + nfats*fatsz) * 512, bytes(e))
    return dev.flush()