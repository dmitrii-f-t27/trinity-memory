# Trinity Memory

[![Executable t27 stack](https://github.com/dmitrii-f-t27/trinity-memory/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/dmitrii-f-t27/trinity-memory/actions/workflows/ci.yml)
[![Ternary Check weekly](https://github.com/dmitrii-f-t27/trinity-memory/actions/workflows/ternary-check-weekly.yml/badge.svg?branch=master)](https://github.com/dmitrii-f-t27/trinity-memory/actions/workflows/ternary-check-weekly.yml)

**Версия 0.4 добавляет [t27 Ternary Check](#t27-ternary-check-версия-04)**, точную
проверку совместимости публичных форматов тернарных весов ([список изменений](CHANGELOG.md)).
**С версии 0.3 стек исполняется на t27.** Кодеки, контейнеры, JSON/HTTP Bridge,
вычисления, эксперименты, отчёты и CLI находятся в [`t27/`](t27/). Компилятор
генерирует нативный код, Verilog и WebAssembly для интерактивного отчёта.
Python API вызывает нативную библиотеку; прежняя реализация сохранена как
независимый тестовый эталон. [Сборка и границы совместимости](docs/T27-MIGRATION.md),
[issue #1](https://github.com/dmitrii-f-t27/trinity-memory/issues/1).

[Новый Edge-отчёт](reports/t27/edge.html) · [Нативный benchmark](reports/t27/index.html) · [Проверки переноса](reports/t27/validation.md)

[English overview](README.md) · [Направление и план](docs/DIRECTION.md) · [Полный запуск](docs/STACK.md)

## t27 Ternary Check (версия 0.4)

Ternary Check отвечает для публичных форматов упаковки тернарных весов на один
вопрос: **хранят ли два файла, или файл и декодер, одни и те же триты и одни и
те же значения масштабов, точно?** Между файлами масштабы сравниваются как значения
(слово f16 и слово bf16 одного и того же числа равны); с декодером Action сравнивает
слова масштабов в том виде, как они хранятся, бит в бит. Каждый вердикт матрицы и
Action выносит исполняемый t27
([`t27/formats.t27`](t27/formats.t27), [`t27/matrix.t27`](t27/matrix.t27),
[`t27/ternary_contract.t27`](t27/ternary_contract.t27)); Python только перекладывает байты.
Проверки масштабов и хвостов во всех 210 тернарных тензорах BitNet (выводы 1 и 3)
делает [`tools/bitnet_audit.py`](tools/bitnet_audit.py) — перекрёстная проверка на
стандартной библиотеке Python; её закоммиченный отчёт воспроизводит задание CI
`fixtures`.
Проверка охватывает TQ1_0, TQ2_0, Q2_0 и Q1_0 из llama.cpp, PQ2_0 и PTQ1_0 из форка
PrismML, I2_S из bitnet.cpp, упакованный `uint8` BitNet из transformers, 2-битный MLX
и `MatMulNBits` с `bits=2` из ONNX Runtime — по побайтовым контрактам в
[`specs/formats/`](specs/formats/), закреплённым на коммитах апстрима.

**Реальные чекпойнты.** Слой 0 BitNet b1.58 2B4T (`q_proj`, `down_proj`) и
Ternary Bonsai 2 27B (`ffn_down`), прочитанный HTTP-запросами диапазонов из
закреплённых ревизий Hugging Face, в каждом формате
([`reports/ternary-check.json`](reports/ternary-check.json),
[`reports/ternary-check.html`](reports/ternary-check.html): HTML нужно скачать, чтобы
открыть). Каждый тензор сравнивается с эталоном — одной из его опубликованных форм:

<!-- ternary-check-table: generated from reports/ternary-check.json, checked by tests/test_release.py -->
| Формат | BitNet `q_proj` | BitNet `down_proj` | Bonsai `ffn_down` |
|---|---|---|---|
| HF packed uint8 + bf16 weight_scale | эталон | эталон | не представим |
| I2_S (bitnet.cpp) | расходится (опубликованный файл) | расходится (опубликованный файл) | не представим |
| PTQ1_0 (PrismML) | совпадает (запись и чтение t27) | совпадает (запись и чтение t27) | эталон |
| PQ2_0 (PrismML) | совпадает (запись и чтение t27) | совпадает (запись и чтение t27) | совпадает (опубликованный файл) |
| Q2_0, group 64 (llama.cpp) | совпадает (запись и чтение t27) | совпадает (запись и чтение t27) | совпадает (опубликованный файл) |
| MLX 2-bit affine, group 128 | совпадает (запись и чтение t27) | совпадает (запись и чтение t27) | совпадает (опубликованный файл) |
| TQ1_0 (llama.cpp) | совпадает (запись и чтение t27) | совпадает (запись и чтение t27) | не представим |
| TQ2_0 (llama.cpp) | совпадает (запись и чтение t27) | совпадает (запись и чтение t27) | не представим |
| Q1_0, binary (llama.cpp) | не представим | не представим | не представим |
| ONNX MatMulNBits bits=2, block 128 | совпадает (запись и чтение t27) | совпадает (запись и чтение t27) | совпадает (запись и чтение t27) |
| TQ1_0 by llama.cpp quantize_row_tq1_0_ref | расходится (запись llama.cpp) | совпадает (запись llama.cpp) | не представим |
| TQ2_0 by llama.cpp quantize_row_tq2_0_ref | расходится (запись llama.cpp) | совпадает (запись llama.cpp) | не представим |
<!-- /ternary-check-table -->

36 ячеек: 23 совпадают, 4 расходятся, 9 не представимы. 12 ячеек с пометками
«эталон», «опубликованный файл» и «запись llama.cpp» — сторонние свидетельства
(байты закреплённых чекпойнтов или байты, записанные закреплёнными эталонными
квантизаторами llama.cpp); 15 записей и чтений t27 показывают лишь, что формат
способен хранить тензор. «Не представим» всегда с причиной: например, один масштаб
на тензор или на 256 весов при 128-весовых группах Bonsai, или отсутствие кода для 0
в двоичном Q1_0. Расхождение I2_S — это точность масштаба из вывода 1; писатели
llama.cpp сохраняют масштаб 0 для 50 полностью нулевых блоков `q_proj`, при этом
каждый вес по-прежнему деквантуется в то же значение. Для каждого расхождения есть
воспроизведение в [`reports/ternary-check/repro/`](reports/ternary-check/repro/).

**Четыре вывода** ([docs/ternary-check.md](docs/ternary-check.md)):

1. BitNet хранит один масштаб с двумя точностями: bf16 в упакованном чекпойнте,
   f32 в файле GGUF I2_S, причём bf16 — это округлённое значение f32, во всех 210
   тернарных тензорах; триты сравненных тензоров совпадают
   ([подробнее](docs/ternary-check.md#results)).
2. Упакованные триты BitNet нельзя пересчитать из опубликованных весов bf16:
   `WeightQuant` из `transformers` расходится с упакованными тритами в 79 719 весах
   `q_proj` слоя 0 (1,22%) и в 101 673 весах `down_proj` (0,57%), все при значении bf16
   `0.5 × weight_scale`, где упакованный файл хранит и ±1, и 0
   ([подробнее](docs/ternary-check.md#results)).
3. В хвостах I2_S остаются лишние байты: после масштаба f32 — 28 байт, не нулевых
   целиком, во всех 210 тензорах I2_S, равных байтам более раннего тензора на том же смещении
   (повторно использованный буфер); деквантизатор их не читает, поэтому на
   inference это не влияет ([подробнее](docs/ternary-check.md#results)).
4. Ternary Bonsai 2 согласован во всех четырёх дистрибутивах: PTQ1_0, PQ2_0,
   Q2_0 (группа 64) и 2-битный MLX хранят одни и те же триты и масштабы fp16 для
   `ffn_down` слоя 0; файлы GGUF объявляют метаданные `prism.*` — вращение Адамара,
   которое должен применить рантайм ([подробнее](docs/ternary-check.md#results)).

Только хранение и целочисленная арифметика: три тензора сравнены трит за тритом
(проверки масштабов и хвостов BitNet охватывают все 210 тернарных тензоров), ни одна
модель не запускалась.

**Одна команда** из чистого клона воспроизводит отчёты байт в байт (она загружает
закреплённые диапазоны фикстур, 205,8 МиБ, и собирает закреплённый компилятор t27;
`OFFLINE=1` берёт только кэши):

```sh
make ternary-check          # пишет reports/ternary-check.json, .html и repro/
make ternary-check-verify   # пересчитать и сравнить с закоммиченными отчётами
```

**Проверьте свой декодер** в GitHub Actions. Он должен реализовать небольшой
[контракт CLI](ternary-check/CONTRACT.md) (см. также
[документацию Action](ternary-check/README.md)). Action прогоняет 126 векторов с
данными из `conformance/formats_*.json`, 178 вызовов декодера, и сравнивает значения,
слова масштабов, флаги и отказы с их классами ошибок; по умолчанию любое
расхождение, неверный отказ или молчаливое принятие проваливает шаг:

```yaml
- uses: dmitrii-f-t27/trinity-memory/ternary-check@v0.4.0
  with:
    decoder: ./build/my-decoder   # аргументы контракта добавляются в конец
    formats: TQ1_0,TQ2_0          # необязательно: только реализованные форматы
```

По умолчанию (`runtime: release`) Action работает из колеса релиза, поэтому раннер
должен быть Linux x86_64 или macOS arm64 с macOS 14 и новее; в остальных случаях
соберите из исходников и укажите в `runtime` каталог клона
([рантайм](ternary-check/README.md#runtime)).

Еженедельный workflow (значок выше) проверяет, что закреплённые файлы апстрима и
файлы моделей не изменились, и заново прогоняет все векторы.

## Пять направлений на t27 (с версии 0.3)

Реализована программная цепочка с проверкой RTL: **TensorPack → Bridge по HTTP →
эмулятор памяти → вычисление → Edge Demo**. Conformance Lab проверяет границы
между компонентами. Исторический [отчёт v0.2](reports/stack.html) нужно скачать и открыть локально;
[JSON](reports/stack.json) и [протокол проверок](reports/stack-validation.md) доступны на GitHub.

| Направление | Реализация |
|---|---|
| Bridge | Настоящий локальный HTTP JSON-RPC, загрузка/чтение/удаление, точное скалярное произведение, адаптер существующего SDK |
| TensorPack | Имена тензоров, размеры, оси, масштабы, TMEM payload и строгая проверка контейнера |
| Stream Compute | RTL для dense5 и 2-битного baseline, int8-входы, ready/valid, сбросы и ошибки |
| Conformance Lab | Эталонные байты и суммы, повреждённые файлы, сетевые и RTL-проверки |
| Edge Demo | Классификатор сигналов по заданным вручную шаблонам, сравнение с точной арифметикой |

```sh
git clone https://github.com/gHashTag/t27.git build/compiler
git -C build/compiler checkout "$(cat native/compiler.lock)"
export T27_ROOT="$PWD/build/compiler"
make t27
make t27-test
make check
make stack
make demo
```

Нужны Rust 1.94.0, C/C++ компиляторы и инструменты из руководства сборки.
Для RTL-проверок нужен Icarus Verilog. Без него доступен программный запуск:
`python3 -m trinity_memory edge-demo`. Wheel включает исходники RTL; сам
симулятор устанавливается отдельно.

Это программный/RTL-демонстратор. HTTP-сервер выполняет скомпилированный код t27, а RTL отдельно
повторяет вычисление на прочитанных обратно весах; плата AX7203 выполняет плеер
трасс и стенд блочной памяти (ниже). DDR, энергопотребление и качество на реальных
данных не измерены. В примере шесть
синтетических сигналов; он не является обученной моделью. SDK проверен через
наш внедряемый backend, исходный Rust node и его upstream-репозиторий не изменены.

Ниже сохранены методика и результаты исходного этапа 0.1.

Отдельное экспериментальное направление Trinity для форматов хранения,
RTL памяти и будущих интерфейсов устройств. Репозиторий ведётся самостоятельно
в `dmitrii-f-t27/trinity-memory` и связан с общим стеком Trinity; интеграция
с ним потребует согласования интерфейсов. Исходный этап — Ternary Memory Lab.

Воспроизводимый прототип хранения троичных весов: **6 программных кодеков,
бинарный формат с проверкой целостности, RTL-декодеры и синтетический бенчмарк**.

Реализован первый этап исследования памяти Trinity. Код сохраняет уже полученные
веса `−1 / 0 / +1` без потерь. Это исследовательский стенд; обучение модели,
полноценный inference engine остаётся следующим этапом. 2026-09-11 плата ALINX AX7203 (XC7A200T)
выполнила плеер, написанный на t27: все 34 вектора трасс stream compute, включая объединённый
путь read -> decode -> dot, совпали с эталоном, потоковая нагрузка дала 0,94 beat за тик
(23,5 М beat/с при 25 М тиков/с) с пересчётом каждого результата на хосте, а классификатор
Edge Demo разметил все шесть фикстур на устройстве за 7 тиков каждую
([reports/fpga](reports/fpga/README.md)). 2026-09-22 стенд упаковки тритов в блочную память
(тоже на t27) уложил на плате один тензор из 1 013 760 тритов в 55, 50 и 45 блоков RAMB36E1
(2 бита на трит, байты dense5, dense5 плюс полубайт dense2 в битах чётности: 2,000, 1,800 и
1,636 бита на трит) и прочитал каждое слово без ошибок, по 18, 20 и 22 трита за чтение
([docs/hardware.md](docs/hardware.md)). DDR и энергопотребление не измерены.

## Спецификации (слой контрактов)

В [`specs/memory/`](specs/memory/) лежат запечатанные спецификации `.t27`, которые
фиксируют контракты исполняемых модулей. [`types.t27`](specs/memory/types.t27)
задаёт коды трит-линий, идентификаторы кодеков и геометрию групп, границы допустимых
кодов, формат контейнера TMEM v1, параметры CRC32, коды статусов и метки источника
измерений (`emulator`, `software`, `rtl-simulation`, `fpga`);
[`bridge.t27`](specs/memory/bridge.t27) — протокол Bridge: envelope, коды ошибок и
HTTP-статусы, лимиты, handle, правила чтения и dot, идентичность (см. [docs/bridge.md](docs/bridge.md)); [`tensorpack.t27`](specs/memory/tensorpack.t27) —
контейнер TTPK v1: заголовок, лимиты, схема метаданных, правила дескрипторов и цепочки,
представление float (см. [docs/tensorpack.md](docs/tensorpack.md));
[`stream_compute.t27`](specs/memory/stream_compute.t27) — конвейер dot, sequencer хранилища
с обратным давлением (`out_ready`), view и объединённый путь read -> decode -> dot
(`rtl/t27/stream_dot.v`) с потактовыми трассами, воспроизводимыми в Icarus
(см. [docs/stream-compute.md](docs/stream-compute.md));
[`conformance.t27`](specs/memory/conformance.t27) — схема conformance-фикстуры, план нативного
эксперимента и лабораторный отчёт, который [`tools/conformance-lab.py`](tools/conformance-lab.py)
воспроизводит по всем потребителям (см. [docs/STACK.md](docs/STACK.md)); [`edge_demo.t27`](specs/memory/edge_demo.t27) —
классификатор-шаблон, его фикстуры, правила скоринга и поля отчёта. Инварианты —
константные выражения, они компилируются в `_Static_assert`; блоки `test`
исполняются сгенерированным C-раннером. У каждой спецификации есть печать в
[`.trinity/seals/`](.trinity/seals/) и независимые от языка векторы в
[`conformance/`](conformance/), которые [`tools/generate-spec-vectors.py`](tools/generate-spec-vectors.py)
вычисляет из спецификации без вызова нативного кода.

В [`specs/formats/`](specs/formats/) лежат побайтовые контракты внешних форматов
упаковки тернарных весов, которые читает t27 Ternary Check
([эпик #27](https://github.com/dmitrii-f-t27/trinity-memory/issues/27)):
[`llama_cpp.t27`](specs/formats/llama_cpp.t27) (TQ1_0, TQ2_0, Q2_0, Q1_0 и правила GGUF
для идентификаторов типов, выравнивания и границ тензоров), [`prismml.t27`](specs/formats/prismml.t27)
(PQ2_0, PTQ1_0), [`bitnet_cpp.t27`](specs/formats/bitnet_cpp.t27) (I2_S),
[`hf_bitnet.t27`](specs/formats/hf_bitnet.t27) (упакованные веса transformers и
`weight_scale`), [`mlx.t27`](specs/formats/mlx.t27) (2-битная аффинная квантизация) и
[`onnx.t27`](specs/formats/onnx.t27) (MatMulNBits `bits=2`). Каждая повторяет
закреплённые коммиты апстрима ([`upstream.lock.json`](specs/formats/upstream.lock.json)):
геометрию блоков, таблицы кодов, биты на вес, правила отказа и флаги; классы статусов —
в [`specs/formats/OWNERS.md`](specs/formats/OWNERS.md). Их векторы `conformance/formats_*.json`
содержат отвергаемые, помечаемые и незаметные случаи и байты, вырезанные из BitNet b1.58 2B4T
и Ternary Bonsai 2; [`tests/native_spec_formats.c`](tests/native_spec_formats.c) прогоняет
их через спецификации и `t27/formats.t27`, а
[`tests/spec_formats_wasm_replay.mjs`](tests/spec_formats_wasm_replay.mjs) — через
`build/t27/formats.wasm`.

Гейт [`tools/check-specs.sh`](tools/check-specs.sh) (его же запускают `make t27-test`
и job `spec` в CI): полнота лексера и парсера, typecheck, генерация C и Verilog,
исполнение тестов, проверка печати, валидация conformance и дифференциальный
harness [`tests/native_spec_types.c`](tests/native_spec_types.c), связывающий
константы спецификации с `t27/codecs.t27` и `t27/container.t27`. Для `specs/formats/`
гейт также падает, если у спецификации нет файла векторов или harness, который их прогоняет.
[`tests/test_spec_types.py`](tests/test_spec_types.py) прогоняет векторы через
Python-адаптеры. Соглашения и ловушки пина компилятора — в
[`specs/memory/OWNERS.md`](specs/memory/OWNERS.md); план остальных направлений —
[эпик #3](https://github.com/dmitrii-f-t27/trinity-memory/issues/3).

## Запуск

После сборки нативного ядра доступен Python API (Python 3.10+).
Из корня репозитория:

```sh
python3 -m unittest discover -s tests -v
python3 -m trinity_memory benchmark --count 65536 --repeats 3
python3 scripts/render_report.py
```

Откройте исторический отчёт v0.1 [`reports/index.html`](reports/index.html)
(его формулировки о FPGA устарели, актуальное — в [docs/hardware.md](docs/hardware.md)): автономный отчёт с реальными
результатами запуска, переключением наборов данных и интерактивным кодированием
пяти тритов. [`reports/benchmark.json`](reports/benchmark.json) содержит исходные
числа, параметры, дату и среду. HTML не требует сервера или внешних библиотек.

Для RTL нужен [Icarus Verilog](https://github.com/steveicarus/iverilog):

```sh
# macOS
brew install icarus-verilog
# Ubuntu/Debian: sudo apt-get install iverilog
python3 scripts/test_rtl.py
```

Полный локальный цикл: `make check` и `make report`.
Опциональная установка CLI: `python3 -m pip install -e .`.
Проверенный локальный результат: [22 Python-теста и 1 296 входов RTL-декодеров](reports/validation.md),
плюс тесты потокового протокола и неизвестных сигналов.

## Результат первого запуска

Из [`reports/benchmark.json`](reports/benchmark.json), 65 536 весов в каждом
синтетическом наборе, seed=27. Размер включает хвостовое выравнивание; контейнер
добавляет ровно 24 байта. Эта таблица относится к TMEM v1; накладные расходы
нового TensorPack с масштабами и размерами в неё не входят.

| Формат | Байты payload | Бит/вес payload | Условие |
|---|---:|---:|---|
| baseline2 | 16 384 | 2,000000 | любые триты |
| dense5 · 5/8 | 13 108 | 1,600098 | любые триты |
| dense17 · 17/27 | 13 014 | 1,588623 | любые триты |
| dense22 · 22/35 | 13 034 | 1,591064 | любые триты |
| sparse41 · (4,≤1) | 8 192 | 1,000000 | ≤1 ненулевого в каждой четвёрке |
| sparse82 · (8,≤2) | 8 192 | 1,000000 | ≤2 ненулевых в каждой восьмёрке |

Все 15 сочетаний набора/кодека в отчёте прошли точное восстановление весов и
контейнера. Время encode/decode — медиана трёх запусков Python после прогрева;
оно не характеризует оптимизированный CPU kernel или FPGA.

`dense5` в пределе уменьшает payload на `1 − 1,6/2 = 20%`. При ограничении
**только** полосой чтения идеальное отношение скоростей равно `2/1,6 = 1,25`.
В отчёте конечный расчёт использует фактические байты с учётом padding.
Для трёх раскладок 36-битного слова число блоков BRAM, LUT и триггеров и оценку
частоты даёт открытый маршрут (yosys, nextpnr-xilinx) для кристалла AX7203, а все три
раскладки прошли на плате без ошибок ([docs/hardware.md](docs/hardware.md),
раздел «Block-RAM trit packing»). Измеренных tokens/s, DDR throughput,
энергопотребления или качества обученной модели здесь пока нет.

## Что реализовано

- Кодеки dense5, dense17, dense22 и 2-битный baseline; проверка зарезервированных
  кодов, хвостов, пустых и некорректных входов.
- Структурные sparse41/sparse82. Несовместимые веса отклоняются; кодек сам их не обнуляет.
- TMEM v1: версия, число весов, длина payload и CRC32 метаданных/данных.
- CLI `pack`, `unpack`, `inspect`, `benchmark`, `export-rtl`.
- Синтезируемые Verilog-декодеры dense5 и sparse41; потоковое синхронное хранилище
  dense5 и 2-битный baseline с одинаковыми пятью выходными lanes.
- Исчерпывающая проверка маленьких кодовых пространств и симуляция протокола памяти.
- GitHub Actions для Python и RTL. Удалённый запуск CI подтверждается только после публикации.

```sh
mkdir -p build
python3 -m trinity_memory pack examples/trits.json build/example.tmem --codec dense5
python3 -m trinity_memory inspect build/example.tmem
python3 -m trinity_memory unpack build/example.tmem build/restored.json
python3 -m trinity_memory export-rtl examples/trits.json build/dense.mem --codec dense5
```

## Исследование и дальнейшая работа

В исходной записке потребовались поправки: Sparse-BitNet 6:8 сохраняет шесть
весов, LUT-декодирование расходует ресурсы, а широкое утверждение об отсутствии
работ по тернарному KV-cache не подтверждается. Проверка первоисточников,
ссылки и границы выводов — в [`docs/research.md`](docs/research.md).

1. **AX7203:** плата выбрана (XC7A200T, 25 МГц), синтез, размещение и прогон на плате
   выполнены открытым маршрутом, число блоков BRAM измерено
   ([`docs/hardware.md`](docs/hardware.md)). Дальше: 72-битный режим BRAM
   (45 тритов = 1,600 бит/трит), контроллер DDR3 и измерение энергопотребления.
2. **QAT:** подключить настоящий checkpoint и обучающий pipeline; сравнить качество,
   фактическую разреженность по блокам и стоимость скейлов. Текущие наборы синтетические.
3. **Weights-as-logic и KV:** отдельные эксперименты с заранее заданными baselines
   и метриками. В этом прототипе они не реализованы.

Спецификация: [`docs/format.md`](docs/format.md).
Материал для будущей публикации: [`docs/showcase.md`](docs/showcase.md).
Этот кодек не совместим побайтово с GGUF/TQ1_0 и не заявляется новым изобретением
base-3 упаковки; ценность текущей работы — воспроизводимая связка формата, кода и RTL.

## Лицензия

Apache License 2.0: см. [LICENSE](LICENSE) и [NOTICE](NOTICE).
