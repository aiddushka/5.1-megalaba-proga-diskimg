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
            return 0, self.d.read(self.root_off, self.root_entries * 32)
        chain = self.chain_list(clus)
        return chain, self._io_clusters(chain, len(chain) * self.csize)

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