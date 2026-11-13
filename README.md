# diskimg-py — Python-версия diskimg

Python-версия комплекса программ `diskimg` для работы с образами дисков
(дискеты, CD/DVD, жёсткие диски / VHD) с файловыми системами **FAT**,
**NTFS** и **ISO9660+Joliet**. Реализация **полностью совместима с
C-версией**: Python читает образы, созданные C, и наоборот; C читает
образы, изменённые Python.

| Операция | Команда |
|---|---|
| создание нового образа | `diskimg create` |
| извлечение файлов в локальный каталог | `diskimg extract`, `diskimg cat` |
| добавление и замена файлов | `diskimg add` |
| удаление файлов и каталогов | `diskimg rm` |
| дополнительно: список файлов, создание каталогов, сведения | `ls`, `mkdir`, `info` |

## Поддерживаемые форматы

| Носитель | Контейнер | Разметка | Файловые системы |
|---|---|---|---|
| Дискета (160K…2.88M) | raw `.img/.ima` | без разметки | FAT12 |
| HDD | raw `.img`, VHD fixed, VHD dynamic `.vhd` | MBR или без разметки | FAT12, FAT16, FAT32, NTFS 3.1 |
| CD/DVD | `.iso` | — | ISO9660 + Joliet (длинные имена в Юникоде) |

Тип контейнера и файловой системы определяется автоматически: VHD — по
сигнатуре `conectix`, ISO — по `CD001`, NTFS — по `NTFS    ` в
загрузочном секторе, FAT — по BPB, иначе образ разбирается как таблица
разделов MBR.

## Требования

- **Python 3.8+** (используется только стандартная библиотека:
  `os`, `sys`, `struct`, `time`, `tempfile`, `argparse`, `functools`,
  `dataclasses`).
- **NASM** и **Python 3** — только если нужно пересобрать
  `diskimg/bootcode.py` из C-версии (цель `make bootcode`), для обычного
  запуска не требуются.
- **Git Bash** или **WSL** — только для запуска `tests/run_tests.sh`.
  Если их нет, автотесты можно прогнать вручную в PowerShell (см.
  раздел «Запуск тестов»).

## Сборка

Отдельная сборка не нужна — это чистый Python. Достаточно положить
папку `diskimg/` в `PYTHONPATH` (или запускать из корня проекта).

Если нужно пересобрать `bootcode.py` из C-версии:

```sh
python tools/gen_bootcode.py ../src/bootcode.h
Структура Python-проекта
text
diskimg-py/
├── diskimg/
│   ├── __init__.py
│   ├── __main__.py          # python -m diskimg
│   ├── common.py            # rd16/…, UTF, время, пути
│   ├── host.py              # файлы хоста
│   ├── bdev.py              # raw, window, VHD fixed/dynamic, MBR
│   ├── fs.py                # интерфейс ФС (ABC)
│   ├── fat.py               # FAT12/16/32 (чтение и запись)
│   ├── iso.py               # ISO9660 + Joliet (чтение и пересборка)
│   ├── ntfs.py              # NTFS 3.1 (чтение и запись)
│   ├── bootcode.py          # СГЕНЕРИРОВАН из src/bootcode.h
│   └── cli.py               # argparse + команды
├── tools/
│   └── gen_bootcode.py      # src/bootcode.h → diskimg/bootcode.py
├── tests/
│   └── run_tests.sh         # тот же, DISKIMG="python -m diskimg"
├── examples/                # образы для сверки
└── README-py.md
Слои не зависят друг от друга: файловая система читает и пишет байты
через bdev_t и не знает, лежит ли она в обычном файле, в разделе MBR
или в блоках dynamic VHD.

Использование
text
python -m diskimg create  <образ> [-f fat12|fat16|fat32|ntfs|iso] [-s размер]
                          [-t floppy|hdd] [--raw|--vhd|--vhd-dynamic]
                          [--no-mbr] [-L метка] [-c кластер]
                          [--align сектор] [--from каталог]
python -m diskimg info    <образ>
python -m diskimg ls      <образ> [путь] [-r]
python -m diskimg extract <образ> <путь> [локальный_каталог]
python -m diskimg cat     <образ> <путь>
python -m diskimg add     <образ> <локальный_путь>... [--to путь_в_образе]
python -m diskimg rm      <образ> <путь>... [-r]
python -m diskimg mkdir   <образ> <путь>
Общие параметры: -P N (номер раздела MBR), -v (подробно).

Пути внутри образа пишутся через /, регистр букв не важен
(/DOCS/readme.TXT = /docs/README.txt).

Примеры
sh
# дискета 1.44 МБ, FAT12
python -m diskimg create boot.img -t floppy -L MYFLOPPY
python -m diskimg add boot.img prog.com /
python -m diskimg ls boot.img

# жёсткий диск VHD 256 МБ: MBR + раздел NTFS
python -m diskimg create disk.vhd -s 256M -f ntfs -L DATA
python -m diskimg mkdir disk.vhd "/Мои документы"
python -m diskimg add disk.vhd ./report.docx --to "/Мои документы/"
python -m diskimg add disk.vhd ./project --to /src     # каталог рекурсивно
python -m diskimg rm disk.vhd /src/build -r
python -m diskimg extract disk.vhd / ./restored

# расширяемый (dynamic) VHD на 2 ГБ, FAT32
python -m diskimg create big.vhd -s 2G -f fat32 --vhd-dynamic --from ./files

# образ CD из каталога, затем изменение (образ пересобирается)
python -m diskimg create cd.iso --from ./cdroot -L INSTALL
python -m diskimg add cd.iso setup.exe /
python -m diskimg rm cd.iso /old -r
Если add получает существующий файл, он заменяется. Если назначение —
существующий каталог (или путь заканчивается на /), файл кладётся
внутрь под своим именем.

Устройство программы
text
diskimg/common.py  типы, little/big-endian, UTF-8 ⇄ UTF-16, даты FAT/NTFS
diskimg/host.py    файлы хоста (Linux и Windows, имена в UTF-8)
diskimg/bdev.py    блочные устройства: raw, окно-раздел, VHD fixed/dynamic, MBR
diskimg/fs.py      общий интерфейс ФС: list/stat/read/write/mkdir/remove/flush
diskimg/fat.py     FAT12/16/32 + длинные имена VFAT, форматирование
diskimg/ntfs.py    NTFS 3.1, форматирование
diskimg/iso.py     ISO9660 + Joliet, сборка образа
diskimg/cli.py     командная строка, рекурсивные add/extract/rm
VHD
fixed: данные диска, за ними 512-байтный footer (big-endian поля,
контрольная сумма — инвертированная сумма байтов, геометрия CHS по
алгоритму из спецификации).

dynamic: копия footer, dynamic header (cxsparse), таблица BAT и
блоки по 2 МБ (битовая карта секторов + данные). Блок выделяется в
конце файла только при первой ненулевой записи, footer переносится в
новый конец. Невыделенные блоки читаются нулями.

FAT12/16/32
Тип FAT определяется по числу кластеров, как в спецификации
Microsoft (< 4085 — FAT12, < 65525 — FAT16).

Таблица FAT целиком загружается в память и записывается во все копии.
12-битные записи упаковываются по полтора байта.

Длинные имена (LFN) хранятся записями с атрибутом 0x0F, символы
UCS-2 идут в обратном порядке, проверяется контрольная сумма
8.3-имени. Короткое имя генерируется в виде ИМЯ~N.РАС. Для имён
вида readme.txt используются флаги регистра Windows NT (байт 12),
без LFN.

Форматирование: стандартные параметры дискет (media byte, сектора на
дорожку, размер корня), для HDD размер кластера выбирается по
таблицам Microsoft. У FAT32 есть FSInfo и резервная копия
загрузочного сектора в секторе 6.

NTFS
Форматирование создаёт полный набор системных файлов: $MFT,
$MFTMirr, $LogFile (пустой, заполнен 0xFF), $Volume
(версия 3.1, метка), $AttrDef, корень, $Bitmap, $Boot,
$BadClus (с разреженным $Bad), $Secure ($SDS + индексы
$SDH/$SII), $UpCase (таблица Windows), $Extend с $Quota,
$ObjId, $Reparse. Резервная копия загрузочного сектора лежит в
последнем секторе тома.

Записи MFT и блоки индекса защищены update sequence (fixup). При
изменении записей 0…3 обновляется и $MFTMirr.

Файлы до ~700 байт хранятся резидентно (внутри записи MFT),
остальные — нерезидентно: программа выделяет кластеры
(предпочтительно непрерывным участком) и кодирует runlist (mapping
pairs).

Каталоги — B-дерево индекса $I30. Порядок задаётся сравнением
имён через $UpCase. При каждом изменении индекс каталога строится
заново, снизу вверх: листья, разделители, корень $INDEX_ROOT, блоки
INDX в $INDEX_ALLOCATION, карта $BITMAP. Так дерево всегда
сбалансировано.

$MFT растёт автоматически, когда кончаются свободные записи; при
этом растёт и $MFT:$BITMAP.

Удаление освобождает кластеры, помечает запись MFT свободной (с
увеличением sequence number) и убирает имя из индекса родителя.

При чтении поддерживается $ATTRIBUTE_LIST: атрибуты могут лежать в
записях-расширениях.

ISO9660 + Joliet
Создаётся первичное дерево ISO9660 (имена 8.3 из d-символов) и
параллельное дерево Joliet с длинными именами в UCS-2 BE. Обоим
деревьям соответствуют свои таблицы путей (L и M). Данные файлов у
деревьев общие.

ISO9660 — формат «только для чтения»: файлы лежат непрерывными
экстентами. Поэтому добавление, замена и удаление сделаны так же, как
в программах записи дисков: дерево читается в память, изменяется, и
образ пересобирается целиком. Данные неизменённых файлов копируются
из старого образа.

Запуск тестов
tests/run_tests.sh прогоняет для каждого формата (дискета FAT12,
FAT16, FAT32 в fixed и dynamic VHD, NTFS raw/fixed/dynamic VHD, ISO)
один и тот же сценарий: создать из каталога → извлечь и сравнить
побайтно → заменить файл → создать каталог и добавить 1 МБ → удалить
каталог рекурсивно → удалить файл.

Через Git Bash / WSL
sh
cd diskimg-py
DISKIMG="python -m diskimg" sh tests/run_tests.sh
С внешними проверяльщиками:

sh
FSCK_FAT=fsck.fat NTFSCK=ntfsck QEMU_IMG=qemu-img \
  DISKIMG="python -m diskimg" sh tests/run_tests.sh
Ожидаемый результат: 48 из 48 (без внешних проверок) или
55 из 55 (с ними) — ровно те же проверки, что проходит C-версия.

Без Git Bash / WSL — вручную в PowerShell
Скопировать текст ниже в окно PowerShell (из корня проекта).

powershell
# --- подготовка тестовых данных ---
$T = "$PWD\test_tmp"; if (Test-Path $T) { Remove-Item $T -Recurse -Force }
New-Item -ItemType Directory $T | Out-Null
New-Item -ItemType Directory "$T\src\sub\deep" | Out-Null
Set-Content "$T\src\a.txt" "hello"
$b = New-Object byte[] 300000; (New-Object Random 1).NextBytes($b); [IO.File]::WriteAllBytes("$T\src\big.bin", $b)
[IO.File]::WriteAllText("$T\src\Длинное имя файла.txt", "привет`n", (New-Object Text.UTF8Encoding $false))
Set-Content "$T\src\sub\MixedCase.Txt" "x"
Set-Content "$T\src\sub\deep\z.c" "y"
New-Item -ItemType File "$T\src\empty.dat" | Out-Null
$b = New-Object byte[] 1000000; (New-Object Random 2).NextBytes($b); [IO.File]::WriteAllBytes("$T\huge.bin", $b)
Set-Content "$T\r.txt" "replaced"

# --- список форматов ---
$formats = @(
    @("floppy.img", @("-t", "floppy", "-L", "FLOPPY")),
    @("fat16.img", @("-f", "fat16", "-s", "32M")),
    @("fat32.vhd", @("-f", "fat32", "-s", "100M", "--vhd")),
    @("fat32dyn.vhd", @("-f", "fat32", "-s", "300M", "--vhd-dynamic")),
    @("ntfs.img", @("-f", "ntfs", "-s", "64M", "--no-mbr", "-L", "DATA")),
    @("ntfs.vhd", @("-f", "ntfs", "-s", "128M", "--vhd")),
    @("ntfsdyn.vhd", @("-f", "ntfs", "-s", "1G", "--vhd-dynamic")),
    @("cd.iso", @("-L", "TESTCD"))
)

# --- прогон ---
$pass = 0; $fail = 0
foreach ($f in $formats) {
    $name = $f[0]; $opts = $f[1]
    $img = "$T\$name"
    Write-Host "== $name ($($opts -join ' '))"

    py -m diskimg create $img @opts --from "$T\src" 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-Host "  ok   create"; $pass++ } else { Write-Host "  FAIL create"; $fail++ }

    Remove-Item "$T\out_$name" -Recurse -Force -ErrorAction SilentlyContinue
    py -m diskimg extract $img / "$T\out_$name" 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-Host "  ok   extract"; $pass++ } else { Write-Host "  FAIL extract"; $fail++ }

    py -m diskimg add $img "$T\r.txt" /a.txt 2>&1 | Out-Null
    $cat = py -m diskimg cat $img /A.TXT 2>&1
    if ($cat -match "replaced") { Write-Host "  ok   replace"; $pass++ } else { Write-Host "  FAIL replace"; $fail++ }

    py -m diskimg mkdir $img /new/dir 2>&1 | Out-Null
    py -m diskimg add $img "$T\huge.bin" /new/dir/ 2>&1 | Out-Null
    py -m diskimg cat $img /new/dir/huge.bin 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-Host "  ok   add big"; $pass++ } else { Write-Host "  FAIL add big"; $fail++ }

    py -m diskimg rm $img /sub -r 2>&1 | Out-Null
    py -m diskimg ls $img /sub 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Host "  ok   rm -r"; $pass++ } else { Write-Host "  FAIL rm -r"; $fail++ }

    py -m diskimg rm $img /empty.dat 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-Host "  ok   rm file"; $pass++ } else { Write-Host "  FAIL rm file"; $fail++ }
}
Write-Host ""
Write-Host "Итого: пройдено $pass, ошибок $fail"
Ожидаемый результат в PowerShell:

text
== floppy.img (-t floppy -L FLOPPY)
  ok   create
  ok   extract
  ok   replace
  ok   add big
  ok   rm -r
  ok   rm file
...
Итого: пройдено 48, ошибок 0
Проверка совместимости с C-версией
Главное доказательство корректности — C читает Python-образы и
наоборот.

sh
# 1. Python создаёт NTFS с файлами
python -m diskimg create py_ntfs.vhd -f ntfs -s 64M --vhd -L PYNTFS --from ./src

# 2. C читает
./diskimg info py_ntfs.vhd
./diskimg ls -r py_ntfs.vhd
./diskimg cat py_ntfs.vhd /a.txt

# 3. Python изменяет
python -m diskimg add py_ntfs.vhd ./r.txt /a.txt
python -m diskimg rm py_ntfs.vhd /empty.dat

# 4. C читает изменения
./diskimg cat py_ntfs.vhd /a.txt      # replaced
./diskimg ls py_ntfs.vhd               # empty.dat нет
Аналогично проверяется FAT и ISO.

Результаты проверки (при разработке)
Python-версия: 48/48 автотестов (без внешних проверяльщиков),
ровно те же 48, что проходит C-версия.

C-версия: 55/55 с внешними проверяльщиками (fsck.fat,
ntfsck, qemu-img).

Python читает C-образы (FAT, ISO, NTFS) и C читает
Python-образы; Python изменяет C-образы — C читает изменения.

fsck.fat (dosfstools) не нашёл ошибок во всех образах FAT,
созданных Python.

ntfsck (ntfsprogs-plus) выдал «Clean, No errors found» на томах
NTFS, созданных Python.

qemu-img открывает VHD fixed и dynamic, созданные Python.

Ограничения
NTFS: не поддерживаются сжатые и зашифрованные файлы (при
извлечении будет понятная ошибка) и альтернативные потоки данных — они
не показываются. Удаление файла с несколькими жёсткими ссылками
запрещено. Журнал $LogFile не воспроизводится, поэтому образ из
Windows лучше снимать после корректного завершения работы. Новые
файлы получают тот же дескриптор безопасности, что и корень тома.

FAT: файлы до 4 ГБ, корень FAT12/16 фиксированного размера
(224/512 записей), exFAT не поддерживается.

ISO: ISO9660 level 1 + Joliet. Rock Ridge и UDF не поддерживаются,
загрузочная запись El Torito при пересборке не сохраняется, файлы до
4 ГБ.

Разметка: только MBR (первичные разделы), без GPT. VHD — fixed и
dynamic, без differencing и VHDX.

Сравнение с C-версией
C-версия	Python-версия
Строк кода	≈ 5500 C + 130 NASM	≈ 4500 Python + 130 NASM
Форматы	FAT12/16/32, NTFS 3.1, ISO9660+Joliet, VHD fixed/dynamic, MBR	те же
Операции	create/extract/cat/add/rm/mkdir/ls/info	те же
Автотесты	55/55 (с внешними)	48/48 (без внешних)
Совместимость	—	C читает Python-образы, Python читает C-образы
Зависимости	gcc, make; NASM + Python для bootcode	Python 3.8+; NASM + Python для bootcode
Итог: Python-версия diskimg полностью совместима с C-версией по
образам и проходит те же автотесты. C-версия остаётся эталонной
(55/55 с внешними проверяльщиками); Python-версия — 48/48 без внешних
и полная двусторонняя совместимость образов.