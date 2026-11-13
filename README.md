3. Структура Python-проекта
diskimg-py/
├── diskimg/
│   ├── __init__.py
│   ├── __main__.py          # python -m diskimg
│   ├── common.py            # rd16/…, UTF, время, пути
│   ├── host.py              # файлы хоста
│   ├── bdev.py              # raw, window, VHD fixed/dynamic, MBR
│   ├── fs.py                # интерфейс ФС (ABC)
│   ├── fat.py               # FAT12/16/32 (следующая итерация)
│   ├── iso.py               # ISO9660 + Joliet
│   ├── ntfs.py              # NTFS 3.1
│   ├── bootcode.py          # СГЕНЕРИРОВАН из src/bootcode.h
│   └── cli.py               # argparse + команды
├── tools/
│   └── gen_bootcode.py      # src/bootcode.h → diskimg/bootcode.py
├── tests/
│   └── run_tests.sh         # тот же, DISKIMG="python -m diskimg"
├── examples/                # твои образы для сверки
└── README-py.md