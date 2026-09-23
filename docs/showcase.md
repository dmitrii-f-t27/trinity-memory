# Trinity Memory: материал для демонстрации

Отдельное направление: `dmitrii-f-t27/trinity-memory`. Его предмет —
форматы хранения, RTL памяти и будущие аппаратные интерфейсы. Связь с основным
Trinity описана в [README](../README.md), этапы развития — в [плане](DIRECTION.md).

## Проверяемый тезис

«Мы сделали Trinity Ternary Memory Lab: программные кодеки троичных весов,
формат файла и согласованные с ним RTL-декодеры. На синтетическом наборе из
65 536 весов dense5 занимает 13 108 байт против 16 384 у 2-битного baseline;
при структурном ограничении разреженности — 8 192 байта. Веса восстанавливаются
точно. На плате AX7203 тот же подход проверен в блочной памяти: 45 блоков RAMB36
вместо 55 на один тензор. Оценка качества модели, DDR и энергопотребление —
следующий этап».

Источник чисел: [`../reports/benchmark.json`](../reports/benchmark.json).
Спецификация и воспроизведение: [`../README.md`](../README.md),
[`format.md`](format.md), [`hardware.md`](hardware.md).

## Демонстрация за несколько минут

1. Открыть `reports/index.html`; показать исходные параметры измерений.
2. Переключить uniform / sparse наборы. Объяснить, что sparse-кодек требует
   ограничений по блокам; просто наличие 75% нулей во всём тензоре недостаточно.
3. Изменить пять тритов в интерактивном кодировщике: один байт на входе,
   пять двухбитных lanes на выходе декодера.
4. Запустить `make check`: Python проверяет кодеки/контейнер, Icarus симулирует RTL.
5. Показать измерения на FPGA: число блоков BRAM для трёх раскладок одного тензора
   (55 / 50 / 45 RAMB36E1, [hardware.md](hardware.md)). Энергопотребление, DDR и
   tokens/s требуют отдельного измерения.

## English draft for a repository description

Reproducible ternary weight storage experiments: lossless dense and structured
sparse codecs, a checksummed binary format, matching Verilog decoders, exhaustive
simulation tests, an offline benchmark report, and block-RAM packing measured on an
AX7203 FPGA (45 RAMB36 instead of 55 for the same tensor). Model quality, DDR and
power remain separate next steps.

## Дальнейшая интеграция

- Проверять фактический результат CI для каждой публикуемой версии.
- Согласовать с командой место нового направления в общем Trinity и условия дальнейшего использования кода.
- При интеграции с основным Trinity согласовать интерфейс весов и скейлов с командой.
- Использовать результаты воспроизводимых проверок, не заявлять новизну base-3
  упаковки, «бесплатный декодер», неэкстрагируемость модели или измеренные tokens/s.

Это подготовленный текст. Публикации в GitHub, X или LinkedIn этот файл не выполняет.
