# Trinity Memory

**Версия 0.3: исполняемый стек на t27.** Кодеки, контейнеры, JSON/HTTP Bridge,
вычисления, эксперименты, отчёты и CLI находятся в [`t27/`](t27/). Компилятор
генерирует нативный код, Verilog и WebAssembly для интерактивного отчёта.
Python API вызывает нативную библиотеку; прежняя реализация сохранена как
независимый тестовый эталон. [Сборка и границы совместимости](docs/T27-MIGRATION.md),
[issue #1](https://github.com/dmitrii-f-t27/trinity-memory/issues/1).

[Новый Edge-отчёт](reports/t27/edge.html) · [Нативный benchmark](reports/t27/index.html) · [Проверки переноса](reports/t27/validation.md)

[English overview](README.md) · [Направление и план](docs/DIRECTION.md) · [Полный запуск](docs/STACK.md)

## Версия 0.3: пять направлений на t27

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
повторяет вычисление на прочитанных обратно весах. Физическая FPGA, DDR,
энергопотребление и качество на реальных данных не измерены. В примере шесть
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
полноценный inference engine и измерения на физической FPGA остаются следующими этапами.

## Спецификации (слой контрактов)

В [`specs/memory/`](specs/memory/) лежат запечатанные спецификации `.t27`, которые
фиксируют контракты исполняемых модулей. [`types.t27`](specs/memory/types.t27)
задаёт коды трит-линий, идентификаторы кодеков и геометрию групп, границы допустимых
кодов, формат контейнера TMEM v1, параметры CRC32, коды статусов и метки источника
измерений (`emulator`, `software`, `rtl-simulation`, `fpga`);
[`bridge.t27`](specs/memory/bridge.t27) — протокол Bridge: envelope, коды ошибок и
HTTP-статусы, лимиты, handle, правила чтения и dot, идентичность (см. [docs/bridge.md](docs/bridge.md)). Инварианты —
константные выражения, они компилируются в `_Static_assert`; блоки `test`
исполняются сгенерированным C-раннером. У каждой спецификации есть печать в
[`.trinity/seals/`](.trinity/seals/) и независимые от языка векторы в
[`conformance/`](conformance/), которые [`tools/generate-spec-vectors.py`](tools/generate-spec-vectors.py)
вычисляет из спецификации без вызова нативного кода.

Гейт [`tools/check-specs.sh`](tools/check-specs.sh) (его же запускают `make t27-test`
и job `spec` в CI): полнота лексера и парсера, typecheck, генерация C и Verilog,
исполнение тестов, проверка печати, валидация conformance и дифференциальный
harness [`tests/native_spec_types.c`](tests/native_spec_types.c), связывающий
константы спецификации с `t27/codecs.t27` и `t27/container.t27`.
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

Откройте [`reports/index.html`](reports/index.html): автономный отчёт с реальными
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
Измеренных tokens/s, частоты FPGA, DDR throughput, LUT/BRAM utilization,
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

1. **AX7203:** выбрать подтверждённый part/clock, выполнить Vivado synthesis и
   implementation для обеих схем, измерить физическую FPGA по
   [`docs/hardware.md`](docs/hardware.md). Битовая ширина массива в RTL не равна
   числу выделенных физических блоков BRAM.
2. **QAT:** подключить настоящий checkpoint и обучающий pipeline; сравнить качество,
   фактическую разреженность по блокам и стоимость скейлов. Текущие наборы синтетические.
3. **Weights-as-logic и KV:** отдельные эксперименты с заранее заданными baselines
   и метриками. В этом прототипе они не реализованы.

Спецификация: [`docs/format.md`](docs/format.md).
Материал для будущей публикации: [`docs/showcase.md`](docs/showcase.md).
Этот кодек не совместим побайтово с GGUF/TQ1_0 и не заявляется новым изобретением
base-3 упаковки; ценность текущей работы — воспроизводимая связка формата, кода и RTL.
