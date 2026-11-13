import argparse, sys
from .common import *
from .bdev import bdev_open_file, mbr_read, part_type_name, Window

def image_open(path):
    base = bdev_open_file(path, False)
    sec0 = base.read(0, 512)
    # VHD/ISO/NTFS/FAT на всём томе
    if sec0[3:11] == b"NTFS    ": return base, None, "NTFS"
    if sec0[0] in (0xEB, 0xE9): return base, None, "FAT"
    parts = mbr_read(base)
    if not parts: raise RuntimeError("не найдено ни ФС, ни MBR")
    for p in parts:
        if not p["type"]: continue
        w = Window(base, p["lba"]*512, p["count"]*512)
        vb = w.read(0, 512)
        if vb[3:11] == b"NTFS    " or vb[0] in (0xEB, 0xE9):
            return base, p, ("NTFS" if vb[3:11] == b"NTFS    " else "FAT")
    raise RuntimeError("нет раздела с поддерживаемой ФС")

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
    # BPB — минимальный разбор, чтобы уже сейчас сверить с C
    if fstype in ("FAT", "NTFS"):
        if part: v = Window(base, part["lba"]*512, part["count"]*512)
        else: v = base
        b = v.read(0, 512)
        if fstype == "FAT":
            from .fat import Fat
            v = Window(base, part["lba"]*512, part["count"]*512) if part else base
            fs = Fat(v)
            fs.info(sys.stdout)
        elif fstype == "NTFS":
            print("NTFS: info — следующая итерация")

def main(argv=None):
    ap = argparse.ArgumentParser(prog="diskimg")
    ap.add_argument("-v", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_info = sub.add_parser("info")
    p_info.add_argument("image")

    p_ls = sub.add_parser("ls")
    p_ls.add_argument("image")
    p_ls.add_argument("path", nargs="?", default="/")
    a = ap.parse_args(argv)
    globals()["g_verbose"] = 1 if a.v else 0
    if a.cmd == "info": return cmd_info(a.image)
    if a.cmd == "ls":
        base, part, fstype = image_open(a.image)
        if fstype != "FAT": print("ls: сейчас только FAT"); return 0
        from .fat import Fat
        v = Window(base, part["lba"]*512, part["count"]*512) if part else base
        fs = Fat(v)
        def cb(e):
            if e.is_dir: print(f"{'<DIR>':>12}  {e.name}/")
            else:        print(f"{e.size:>12}  {e.name}")
        fs.list(a.path, cb)

if __name__ == "__main__": sys.exit(main())