"""Файлы хоста (Linux/Windows), имена в UTF-8."""
import os, stat, time

def host_fopen(path, mode): return open(path, mode)
def host_mkdir(path):
    try: os.mkdir(path); return 0
    except FileExistsError: return 0
    except OSError: return -1
def host_stat(path):
    try:
        st = os.stat(path)
        return {"is_dir": stat.S_ISDIR(st.st_mode), "size": st.st_size, "mtime": int(st.st_mtime)}
    except OSError: return None
def host_set_mtime(path, t): os.utime(path, (t, t))
def host_listdir(path):
    try:
        names = [n for n in os.listdir(path) if n not in (".", "..")]
        names.sort()
        return names
    except OSError: return None
def host_rename(a, b): os.replace(a, b)
def host_unlink(p): os.unlink(p)