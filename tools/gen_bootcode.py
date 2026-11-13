#!/usr/bin/env python3
"""src/bootcode.h → diskimg/bootcode.py (без NASM)."""
import re, sys, pathlib

src = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "src/bootcode.h").read_text(encoding="utf-8")
out = ["# Сгенерировано tools/gen_bootcode.py из src/bootcode.h — не редактировать", ""]
for name in ("mbr_boot_code", "vbr_stub_fat16", "vbr_stub_fat32", "vbr_stub_ntfs"):
    m = re.search(rf"{name}\[\d+\]\s*=\s*\{{(.*?)\}};", src, re.S)
    if not m:
        sys.exit(f"не найден {name}")
    data = bytes(int(x, 16) for x in re.findall(r"0x([0-9A-Fa-f]{2})", m.group(1)))
    out.append(f"{name} = bytes([")
    for i in range(0, len(data), 16):
        out.append("    " + ", ".join(f"0x{b:02X}" for b in data[i:i+16]) + ",")
    out.append("])\n")
pathlib.Path("diskimg/bootcode.py").write_text("\n".join(out), encoding="utf-8")
print("diskimg/bootcode.py сгенерирован")