"""Типы, endian, UTF-8⇄UTF-16, время FAT/NTFS, пути — аналог common.c."""
import sys, time, struct

g_verbose = 0

def die(fmt, *a):
    print("diskimg: ошибка: " + (fmt % a), file=sys.stderr); sys.exit(1)

def err(fmt, *a):
    print("diskimg: " + (fmt % a), file=sys.stderr); return -1

def vlog(fmt, *a):
    if g_verbose: print(fmt % a, file=sys.stderr, end="")

# --- little-endian ---
def rd16(b, o=0): return b[o] | (b[o+1] << 8)
def rd32(b, o=0): return b[o] | (b[o+1] << 8) | (b[o+2] << 16) | (b[o+3] << 24)
def rd64(b, o=0): return rd32(b, o) | (rd32(b, o+4) << 32)
def wr16(b, o, v): b[o] = v & 0xFF; b[o+1] = (v >> 8) & 0xFF
def wr32(b, o, v):
    b[o]=v&0xFF; b[o+1]=(v>>8)&0xFF; b[o+2]=(v>>16)&0xFF; b[o+3]=(v>>24)&0xFF
def wr64(b, o, v): wr32(b, o, v & 0xFFFFFFFF); wr32(b, o+4, (v >> 32) & 0xFFFFFFFF)

# --- big-endian ---
def rdb16(b, o=0): return (b[o] << 8) | b[o+1]
def rdb32(b, o=0): return (b[o]<<24)|(b[o+1]<<16)|(b[o+2]<<8)|b[o+3]
def rdb64(b, o=0): return (rdb32(b, o) << 32) | rdb32(b, o+4)
def wrb16(b, o, v): b[o] = (v >> 8) & 0xFF; b[o+1] = v & 0xFF
def wrb32(b, o, v):
    b[o]=(v>>24)&0xFF; b[o+1]=(v>>16)&0xFF; b[o+2]=(v>>8)&0xFF; b[o+3]=v&0xFF
def wrb64(b, o, v): wrb32(b, o, (v>>32)&0xFFFFFFFF); wrb32(b, o+4, v&0xFFFFFFFF)

def MIN(a,b): return a if a < b else b
def MAX(a,b): return a if a > b else b
def DIV_UP(a,b): return (a + b - 1) // b
def ALIGN_UP(a,b): return DIV_UP(a,b) * b

# --- Юникод ---
def utf8_to_utf16(s, max_n):
    out = []
    for ch in s:
        c = ord(ch)
        if c >= 0x10000:
            if len(out) + 2 > max_n: break
            c -= 0x10000
            out.append(0xD800 | (c >> 10)); out.append(0xDC00 | (c & 0x3FF))
        else:
            if len(out) >= max_n: break
            out.append(c)
    return out

def utf16_to_utf8(u16_list):
    out = []
    i, n = 0, len(u16_list)
    while i < n:
        c = u16_list[i]
        if 0xD800 <= c < 0xDC00 and i+1 < n and 0xDC00 <= u16_list[i+1] < 0xE000:
            c = 0x10000 + ((c - 0xD800) << 10) + (u16_list[i+1] - 0xDC00); i += 2
        else:
            i += 1
        out.append(chr(c))
    return "".join(out)

def utf16le_to_utf8(buf, n):
    return utf16_to_utf8([rd16(buf, 2*i) for i in range(n)])
def utf16be_to_utf8(buf, n):
    return utf16_to_utf8([rdb16(buf, 2*i) for i in range(n)])

def uc_simple(c):
    if 'a' <= c <= 'z': return ord(c) - 32
    o = ord(c)
    if 0xE0 <= o <= 0xFE and o != 0xF7: return o - 32
    if 0x430 <= o <= 0x44F: return o - 32
    if 0x450 <= o <= 0x45F: return o - 80
    return o

def name_ieq(a, b):
    ua = utf8_to_utf16(a, 512); ub = utf8_to_utf16(b, 512)
    if len(ua) != len(ub): return False
    return all(uc_simple(chr(x)) == uc_simple(chr(y)) for x, y in zip(ua, ub))

# --- время ---
NTFS_EPOCH_DIFF = 11644473600
def unix_to_ntfs(t): return (int(t) + NTFS_EPOCH_DIFF) * 10000000
def ntfs_to_unix(t): return t // 10000000 - NTFS_EPOCH_DIFF

def days_from_civil(y, m, d):
    y -= 1 if m <= 2 else 0
    era = (y if y >= 0 else y - 399) // 400
    yoe = y - era * 400
    doy = (153 * (m + (-3 if m > 2 else 9)) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468

def civil_from_days(z):
    z += 719468
    era = (z if z >= 0 else z - 146096) // 146097
    doe = z - era * 146097
    yoe = (doe - doe//1460 + doe//36524 - doe//146096) // 365
    y = yoe + era * 400
    doy = doe - (365*yoe + yoe//4 - yoe//100)
    mp = (5*doy + 2) // 153
    d = doy - (153*mp + 2)//5 + 1
    m = mp + 3 if mp < 10 else mp - 9
    return y + (1 if m <= 2 else 0), m, d

def unix_to_fat(t):
    if t < 315532800: t = 315532800
    days, rem = t // 86400, t % 86400
    y, m, d = civil_from_days(days)
    if y > 2107: y = 2107
    return ((y-1980)<<9)|(m<<5)|d, ((rem//3600)<<11)|(((rem//60)%60)<<5)|((rem%60)//2)

def fat_to_unix(date, tim):
    y = 1980 + (date >> 9); m = (date >> 5) & 15; d = date & 31
    if m < 1: m = 1
    if d < 1: d = 1
    return days_from_civil(y, m, d)*86400 + (tim>>11)*3600 + ((tim>>5)&63)*60 + (tim&31)*2

# --- пути ---
def path_split(path, max_n=64):
    comps = []
    for p in path.replace("\\", "/").split("/"):
        if p in ("", "."): continue
        if p == "..":
            if comps: comps.pop()
            continue
        if len(p) > 255: p = p[:255]
        comps.append(p)
        if len(comps) >= max_n: break
    return comps

def path_basename(p):
    return p.replace("\\", "/").rsplit("/", 1)[-1]

_rng = [0]
def rand32():
    if not _rng[0]: _rng[0] = int(time.time() * 1000003) | 1
    x = _rng[0]
    x ^= (x << 13) & 0xFFFFFFFFFFFFFFFF
    x ^= x >> 7
    x ^= (x << 17) & 0xFFFFFFFFFFFFFFFF
    _rng[0] = x
    return (x >> 16) & 0xFFFFFFFF