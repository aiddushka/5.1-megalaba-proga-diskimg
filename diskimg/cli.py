"""CLI: info/ls/cat/extract/create/add/rm/mkdir для FAT (чтение+запись)."""
import argparse, sys, os
from .common import *
from .bdev import (bdev_open_file, mbr_read, part_type_name, Window,
                   raw_create, vhd_create, mbr_write_single)
from .fs import FileSystem

def _fatal(msg, code=1):
    print(f"diskimg: {msg}", file=sys.stderr); return code

def image_open(path, writable=False):
    base = bdev_open_file(path, writable)
    from .iso import iso_probe
    if iso_probe(base): return base, None, "ISO"
    sec0 = base.read(0, 512)
    if sec0[3:11] == b"NTFS    ": return base, None, "NTFS"
    if sec0[0] in (0xEB, 0xE9):  return base, None, "FAT"
    parts = mbr_read(base)
    if not parts: raise RuntimeError("не найдено ни ФС, ни MBR")
    for p in parts:
        if not p["type"]: continue
        w = Window(base, p["lba"]*512, p["count"]*512)
        vb = w.read(0, 512)
        if vb[3:11] == b"NTFS    ": return base, p, "NTFS"
        if vb[0] in (0xEB, 0xE9):   return base, p, "FAT"
    raise RuntimeError("нет раздела с поддерживаемой ФС")

def mount_fs(base, part, fstype):
    if fstype == "ISO":
        from .iso import Iso
        return Iso(base)

    if fstype != "FAT":
        raise RuntimeError(f"{fstype}: сейчас реализован только FAT")
    from .fat import Fat
    v = Window(base, part["lba"]*512, part["count"]*512) if part else base
    return Fat(v)

def _fmt(e):
    if e.is_dir: return f"  <DIR>         {e.name}/"
    return f"  {e.size:>10}    {e.name}"

def cmd_info(img):
    base, part, fstype = image_open(img)
    print(f"Образ            : {img}")
    print(f"Контейнер        : {base.kind}, {base.size} байт")
    if part:
        print("Таблица разделов : MBR")
        print(f"  *{part['index']}: тип {part['type']:02X}h ({part_type_name(part['type'])}), "
              f"LBA {part['lba']}, {part['count']} секторов ({part['count']/2048:.1f} МБ)")
    else:
        print("Таблица разделов : нет (том на весь образ)")
    fs = mount_fs(base, part, fstype)
    fs.info(sys.stdout)
    return 0

def cmd_ls(img, path, recursive):
    base, part, fstype = image_open(img)
    fs = mount_fs(base, part, fstype)
    def walk(p, prefix):
        
        ents = []
        rc = fs.list(p, ents.append)
        if rc: return rc
        for e in ents:
            print(prefix + _fmt(e))
            if recursive and e.is_dir:
                sub = (p.rstrip("/") + "/" + e.name) if p != "/" else "/" + e.name
                walk(sub, prefix + e.name + "/")
        return 0
    return walk(path, "")

def cmd_cat(img, path):
    base, part, fstype = image_open(img)
    fs = mount_fs(base, part, fstype)
    if sys.platform == "win32":
        import msvcrt
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    return fs.read(path, sys.stdout.buffer)

def cmd_extract(img, src, dst):
    base, part, fstype = image_open(img)
    fs = mount_fs(base, part, fstype)
    st = fs.stat(src)
    if st is None: return _fatal(f"не найдено: {src}")
    os.makedirs(dst, exist_ok=True)
    count = [0]
    def one(p, local):
        s = fs.stat(p)
        if s is None: return
        if s.is_dir:
            os.makedirs(local, exist_ok=True)
            ents = []; fs.list(p, ents.append)
            for e in ents:
                sub = (p.rstrip("/") + "/" + e.name) if p != "/" else "/" + e.name
                one(sub, os.path.join(local, e.name))
        else:
            with open(local, "wb") as f:
                if fs.read(p, f):
                    print(f"diskimg: ошибка чтения {p}", file=sys.stderr); return
                count[0] += 1
                if s.mtime: os.utime(local, (s.mtime, s.mtime))
    if st.is_dir:
        if src.strip("/") == "":
            ents = []; fs.list("/", ents.append)
            for e in ents:
                one("/" + e.name, os.path.join(dst, e.name))
        else:
            one(src, os.path.join(dst, os.path.basename(src.rstrip("/"))))
    else:
        one(src, os.path.join(dst, st.name))
    print(f"Извлечено файлов: {count[0]} -> {dst}")
    return 0

def parse_size(s):
    if not s: return None
    su = s.upper().strip()
    mult = {"K":1024, "M":1024**2, "G":1024**3, "T":1024**4}.get(su[-1], 1)
    num = su[:-1] if su[-1] in "KMGT" else su
    return int(float(num) * mult)

def cmd_create(a):
    ext = a.image.lower().rsplit(".", 1)[-1] if "." in a.image else ""
    fstype = a.fstype
    if not fstype and ext == "iso": fstype = "iso"          # ← автоопределение по .iso
    if fstype == "iso":
        from .iso import iso_create
        label = a.label or "CDROM"
        rc = iso_create(a.image, label, a.from_dir)
        if rc == 0:
            print(f"Создан образ {a.image}: ISO9660+Joliet, метка {label}")
        return rc    

    floppy = (a.typ == "floppy") or ext in ("ima", "flp")
    cont = "vhd-dynamic" if a.vhd_dynamic else "vhd" if (a.vhd or ext == "vhd") else "raw"
    bits = {"fat12":12, "fat16":16, "fat32":32}.get(a.fstype or "", 0)
    if not bits:
        bits = 12 if (floppy or size <= 4*1024*1024) else 16 if size <= 512*1024*1024 else 32
    # создать контейнер
    if cont == "raw": raw_create(a.image, size)
    elif cont == "vhd": vhd_create(a.image, size, False)
    else: vhd_create(a.image, size, True)
    base = bdev_open_file(a.image, True)
    align = a.align
    if not a.no_mbr and not floppy:
        total = base.size // 512
        if align < 1 or align >= total // 2: align = 2048
        count = total - align
        vol = Window(base, align*512, count*512)
    else:
        vol = base
        align = 0
    from .fat import fat_format
    fat_format(vol, {"label": a.label or "NO NAME", "fat_bits": bits,
                     "cluster_size": parse_size(a.cluster),
                     "hidden_sectors": align, "floppy": floppy})
    if not a.no_mbr and not floppy:
        total = base.size // 512
        count = total - align
        ptype = 0x01 if bits == 12 else (0x06 if count >= 65536 else 0x04) if bits == 16 else 0x0C
        mbr_write_single(base, align, count, ptype)
    base.flush(); base.close()
    print(f"Создан образ {a.image}: {cont}, {size} байт, FAT{bits}"
          + (", MBR + 1 раздел" if (not a.no_mbr and not floppy) else ", без MBR"))
    if a.from_dir:
        return cmd_add(a.image, [a.from_dir], "/")
    return 0

def cmd_add(image, locals_, dest="/"):
    base, part, fstype = image_open(image, writable=True)
    fs = mount_fs(base, part, fstype)
    # если последний "local" не существует на хосте и начинается с "/" — это dest
    if len(locals_) > 1 and locals_[-1].startswith("/") and not os.path.isfile(locals_[-1]):
        dest = locals_[-1]
        locals_ = locals_[:-1]
    count = 0

    def walk(lp, ip):
        nonlocal count
        try:
            st = os.stat(lp)
        except (FileNotFoundError, PermissionError):
            print(f"diskimg: пропущен {lp}", file=sys.stderr); return
        if os.path.isdir(lp):
            try:
                fs.mkdir(ip)
            except Exception:
                pass
            try:
                names = sorted(os.listdir(lp))
            except PermissionError:
                print(f"diskimg: нет доступа к {lp}", file=sys.stderr); return
            for n in names:
                walk(os.path.join(lp, n), ip.rstrip("/") + "/" + n)
        else:
            class _S: pass
            s = _S()
            with open(lp, "rb") as f:
                s.seek = f.seek; s.read = f.read
                s.size = st.st_size; s.mtime = int(st.st_mtime)
                fs.write(ip, s)
            count += 1
    for local in locals_:
        st = os.stat(local)
        target = dest
        dest_stat = fs.stat(dest) if dest else None
        if dest.endswith("/") or (dest_stat and dest_stat.is_dir):
            target = (dest.rstrip("/") + "/" + os.path.basename(local.rstrip("/\\")))
        walk(local, target)
    fs.flush(); base.flush()
    print(f"Добавлено/заменено файлов: {count}")
    return 0

def cmd_rm(image, paths, recursive):
    base, part, fstype = image_open(image, writable=True)
    fs = mount_fs(base, part, fstype)
    n = 0
    def rm_rec(p):
        nonlocal n
        st = fs.stat(p)
        if st is None: return _fatal(f"не найдено: {p}")
        if st.is_dir:
            if not recursive: return _fatal(f"каталог {p} (нужен -r)")
            ents = []; fs.list(p, ents.append)
            for e in ents:
                rm_rec((p.rstrip("/") + "/" + e.name) if p != "/" else "/" + e.name)
        fs.remove(p); n += 1
    for p in paths:
        rc = rm_rec(p)
        if rc: return rc
    fs.flush(); base.flush()
    print(f"Удалено объектов: {n}")
    return 0

def cmd_mkdir(image, path):
    base, part, fstype = image_open(image, writable=True)
    fs = mount_fs(base, part, fstype)
    rc = fs.mkdir(path)
    fs.flush(); base.flush()
    if rc: return 1
    return 0

def main(argv=None):
    ap = argparse.ArgumentParser(prog="diskimg")
    ap.add_argument("-v", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info").add_argument("image")
    p_ls = sub.add_parser("ls"); p_ls.add_argument("image"); p_ls.add_argument("path", nargs="?", default="/"); p_ls.add_argument("-r", action="store_true")
    p_cat = sub.add_parser("cat"); p_cat.add_argument("image"); p_cat.add_argument("path")
    p_ex = sub.add_parser("extract"); p_ex.add_argument("image"); p_ex.add_argument("path"); p_ex.add_argument("dst", nargs="?", default=".")
    p_cr = sub.add_parser("create")
    p_cr.add_argument("image")
    p_cr.add_argument("-f", dest="fstype", choices=["fat12","fat16","fat32","ntfs","iso"])
    p_cr.add_argument("-s", dest="size")
    p_cr.add_argument("-t", dest="typ", choices=["floppy","hdd"])
    p_cr.add_argument("--vhd", action="store_true")
    p_cr.add_argument("--vhd-dynamic", action="store_true")
    p_cr.add_argument("--raw", action="store_true")
    p_cr.add_argument("--no-mbr", action="store_true")
    p_cr.add_argument("-L", dest="label")
    p_cr.add_argument("-c", dest="cluster")
    p_cr.add_argument("--align", type=int, default=2048)
    p_cr.add_argument("--from", dest="from_dir")
    p_add = sub.add_parser("add"); p_add.add_argument("image"); p_add.add_argument("local", nargs="+"); p_add.add_argument("--to", dest="dest", default="/")
    p_rm = sub.add_parser("rm"); p_rm.add_argument("image"); p_rm.add_argument("paths", nargs="+"); p_rm.add_argument("-r", action="store_true")
    p_mk = sub.add_parser("mkdir"); p_mk.add_argument("image"); p_mk.add_argument("path")
    a = ap.parse_args(argv)
    globals()["g_verbose"] = 1 if a.v else 0
    try:
        if a.cmd == "info":    return cmd_info(a.image)
        if a.cmd == "ls":      return cmd_ls(a.image, a.path, a.r)
        if a.cmd == "cat":     return cmd_cat(a.image, a.path)
        if a.cmd == "extract": return cmd_extract(a.image, a.path, a.dst)
        if a.cmd == "create":  return cmd_create(a)
        if a.cmd == "add":     return cmd_add(a.image, a.local, a.dest)
        if a.cmd == "rm":      return cmd_rm(a.image, a.paths, a.r)
        if a.cmd == "mkdir":   return cmd_mkdir(a.image, a.path)
    except RuntimeError as e:
        return _fatal(str(e))
    except FileNotFoundError as e:
        return _fatal(f"файл не найден: {e.filename}")
    return 0

if __name__ == "__main__":
    sys.exit(main())