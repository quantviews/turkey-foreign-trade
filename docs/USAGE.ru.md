# Документация: пайплайн внешней торговли Турции (TUIK BI → ТНВЭД-совместимый parquet)

Подробное руководство на русском. Описывает все CLI-команды с примерами по
каждому аргументу, Python API, структуру хранения данных и интеграцию со
старой схемой `tr_full.parquet`.

---

## Содержание

1. [Быстрый старт](#быстрый-старт)
2. [Установка](#установка)
3. [Архитектура](#архитектура)
4. [CLI: обзор](#cli-обзор)
5. [CLI: команда `tuik`](#cli-команда-tuik) — основной сбор данных
6. [CLI: команда `update`](#cli-команда-update) — регулярное обновление новых месяцев
7. [CLI: команда `coverage`](#cli-команда-coverage) — что есть в TUIK vs локально
8. [CLI: команда `compat-export`](#cli-команда-compat-export) — drop-in замена `tr_full.parquet`
9. [CLI: команда `comtrade`](#cli-команда-comtrade) — резервный источник UN Comtrade
10. [CLI: команда `smoke`](#cli-команда-smoke) — быстрая проверка пайплайна
11. [CLI: команда `countries`](#cli-команда-countries) — справочник стран TUIK
12. [CLI: команда `monthly` / `top`](#cli-команды-monthly--top) — встроенная аналитика
13. [CLI: команда `duckdb-view`](#cli-команда-duckdb-view) — DuckDB view
14. [CLI: команда `hs-sync`](#cli-команда-hs-sync) — справочник HS-6 EN
15. [CLI: команда `backfill-trade-system`](#cli-команда-backfill-trade-system) — миграция партиций
16. [Глобальный workflow обновления](#глобальный-workflow-обновления) — регулярная актуализация всего пайплайна
17. [Python API](#python-api) — программный доступ
18. [Структура хранилища данных](#структура-хранилища-данных)
19. [Схема выхода compat-export](#схема-выхода-compat-export)
20. [Интеграция с проектом mgimo-foreign_trade](#интеграция-с-проектом-mgimo-foreign_trade)
21. [Аналитика через DuckDB](#аналитика-через-duckdb)
22. [Troubleshooting](#troubleshooting)
23. [Внутренности: как работает Qlik-клиент](#внутренности-как-работает-qlik-клиент)

---

## Быстрый старт

```powershell
# 1. Установить
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
Copy-Item .env.example .env

# 2. Посмотреть, что TUIK сейчас отдаёт (история начинается с 2013 г.)
python -m pipeline.cli coverage

# 3. Скачать ВСЮ доступную историю торговли Турции с Россией (~50 мин на 14 лет)
python -m pipeline.cli update --refresh-latest 0

# 4. Получить drop-in замену старого tr_full.parquet
python -m pipeline.cli hs-sync                  # один раз: справочник HS-6 EN
python -m pipeline.cli compat-export            # -> data/exports/tr_full_compat.parquet

# 5. Когда выходит новый месяц (обычно через 1.5-2 месяца после окончания)
python -m pipeline.cli update                   # догнать пропуски + перезалить последний месяц
python -m pipeline.cli compat-export            # пересобрать compat-файл
```

---

## Установка

### Требования

- Python 3.10+ (рекомендую 3.12)
- Chromium (ставится через playwright)
- ~500 МБ свободного места под данные за 10 лет

### Шаги

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1                    # Linux/Mac: source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
Copy-Item .env.example .env                     # отредактировать при необходимости
```

### Переменные окружения (`.env`)

| Переменная | Значение по умолчанию | Описание |
| --- | --- | --- |
| `UN_COMTRADE_KEY` | пусто | API-ключ UN Comtrade Plus. Регистрация на https://comtradedeveloper.un.org/. Без ключа доступен только public preview (с лимитами). |
| `HEADLESS` | `true` | Запускать ли браузер в headless-режиме. `false` для отладки. |
| `TUIK_APP_LANG` | `en` | Язык приложения Qlik: `en` или `tr`. Влияет только на названия категорий. |
| `DATA_DIR` | `./data` | Корневая папка для всех данных. |
| `LOG_LEVEL` | `INFO` | Уровень логирования: `DEBUG`/`INFO`/`WARNING`/`ERROR`. |
| `MGIMO_FOREIGN_TRADE_SRC` | автодетект | Путь к `mgimo-foreign_trade/src` для подгрузки ОКЕИ/ТНВЭД справочников. Автоматически ищется в `../mgimo-foreign_trade/src`. |

---

## Архитектура

```
┌────────────────────────────────────────────────────────────────┐
│ TUIK BI (Qlik Sense Enterprise за Netscaler WAF)               │
│   - Mashup React app, app GUIDs hardcoded                      │
└─────────────┬──────────────────────────────────────────────────┘
              │ Playwright + Chromium
              │ → window.qlik.openApp(GUID)
              │ → app.model.engineApp.createSessionObject(...)
              │ → set-analysis selections + pagination
              ▼
┌────────────────────────────────────────────────────────────────┐
│ pipeline.tuik_bi.TuikBI (async context manager)                │
└─────────────┬──────────────────────────────────────────────────┘
              │ Qlik hypercube JSON
              ▼
┌────────────────────────────────────────────────────────────────┐
│ pipeline.normalize.cube_to_dataframe                           │
│ pipeline.storage.write_partition                               │
│ → data/raw/tuik_bi/partner_kodu=K/year=Y/month=M.parquet       │
└─────────────┬──────────────────────────────────────────────────┘
              │
              ├──────────────────────────► DuckDB view → queries.py
              │
              └─► compat.compat_export ──► data/exports/tr_full_compat.parquet
                  (через core.edizm + core.country_processor_contract из mgimo)
```

**Trade System: только General (GTS)**. Special Trade System apps есть в
TUIK BI, но в этом проекте не подключены — UN Comtrade и данные
Eurostat-mirror идут по GTS, смешивание систем дало бы скрытое расхождение
~2-5% на двусторонних итогах.

---

## CLI: обзор

```powershell
python -m pipeline.cli --help
```

| Команда | Назначение |
| --- | --- |
| `tuik` | Основной сбор: GTIP-12 месячные данные из TUIK BI по диапазону лет |
| `update` | **Регулярное обновление**: автодетект новых месяцев в TUIK + перезалив последнего |
| `coverage` | Diff между тем что есть в TUIK и тем что у тебя локально |
| `compat-export` | Конвертация партиций в схему `tr_full.parquet` |
| `comtrade` | Резервный источник: UN Comtrade HS-6 |
| `smoke` | Быстрый тест: один месяц + статистика |
| `countries` | Список доступных в TUIK стран и их кодов `ULKE_KODU` |
| `monthly` | DuckDB-сводка: помесячные итоги USD/EUR/TRY |
| `top` | DuckDB-аналитика: топ-N товаров по обороту |
| `duckdb-view` | (Пере)создать DuckDB view на всех partition-файлах |
| `hs-sync` | Скачать/обновить кэш HS-6 English-описаний |
| `backfill-trade-system` | Миграция: добавить колонку `trade_system` в старые партиции |

**Глобальная опция** для всех команд:

| Опция | Значение | Пример |
| --- | --- | --- |
| `-v`, `--log-level` | `DEBUG`/`INFO`/`WARNING`/`ERROR` | `python -m pipeline.cli -v DEBUG tuik --from 2024 --to 2024` |

---

## CLI: команда `tuik`

Основной сбор данных из TUIK BI. Загружает GTIP-12 месячные строки для
указанного партнёра в указанном диапазоне лет.

### Полная сигнатура

```powershell
python -m pipeline.cli tuik [OPTIONS]
```

### Аргументы

| Аргумент | Тип | По умолчанию | Описание |
| --- | --- | --- | --- |
| `--from` | int | текущий год − 9 | Первый год сбора, включительно. |
| `--to` | int | текущий год | Последний год сбора, включительно. Если совпадает с текущим — собираются только закрытые месяцы. |
| `--partner` | str | `75` (Россия) | TUIK код страны-партнёра (`ULKE_KODU`). NB: это **не** ISO-числовой код. |
| `--lang` | str | `en` | Язык Qlik-приложения GTS: `en` или `tr`. |
| `--skip-existing` / `--rerun` | flag | `--skip-existing` | Пропускать партиции, которые уже есть на диске, или пересобрать заново. |
| `--headed` | flag | `false` (т.е. headless) | Показать окно браузера (для отладки/наблюдения). |

### Примеры

**Базовый сбор за 10 лет (Россия)** — типичный сценарий первой загрузки. Идемпотентно: повторный запуск пропустит уже скачанные месяцы.

```powershell
python -m pipeline.cli tuik --from 2016 --to 2025
```

**Только один год**:

```powershell
python -m pipeline.cli tuik --from 2024 --to 2024
```

**Принудительная перезагрузка** (например, после релиза TUIK с пересчитанной историей):

```powershell
python -m pipeline.cli tuik --from 2024 --to 2024 --rerun
```

**Другая страна-партнёр** — сначала найди код, потом скачивай:

```powershell
python -m pipeline.cli countries --contains german
#     4  Almanya
python -m pipeline.cli tuik --partner 4 --from 2020 --to 2024
```

**Сбор по нескольким партнёрам последовательно** (powershell-цикл):

```powershell
"75","4","380","804" | ForEach-Object {                       # RU, DE, IT, UA
    python -m pipeline.cli tuik --partner $_ --from 2020 --to 2024
}
```

**Наблюдать за выполнением в браузере** (полезно если TUIK поменял UI):

```powershell
python -m pipeline.cli tuik --from 2024 --to 2024 --headed
```

**Сбор с турецкими лейблами** (`İhracat`/`İthalat` вместо `Export`/`Import`):

```powershell
python -m pipeline.cli tuik --from 2024 --to 2024 --lang tr
```

**С полным логом** (видны JS-вызовы Qlik):

```powershell
python -m pipeline.cli -v DEBUG tuik --from 2024 --to 2024
```

### Что записывается

Один parquet-файл на каждый (partner × year × month):

```
data/raw/tuik_bi/partner_kodu=75/year=2024/month=01.parquet
data/raw/tuik_bi/partner_kodu=75/year=2024/month=02.parquet
...
```

Колонки: `YIL, AY, IHRITH, ISTPOZ, ISTPOZ_ADI, OLCU_ADI, usd, eur, try, q1, q2, flow, partner_kodu, trade_system`.

---

## CLI: команда `update`

Регулярное обновление: спрашивает у TUIK BI **какие (год, месяц) сейчас
опубликованы**, сравнивает с тем что есть на диске, и догоняет всё что не
хватает. Дополнительно перезаливает последний опубликованный месяц
(TUIK ревизирует поздние подачи и конфиденциальную агрегацию ещё несколько
недель после публикации).

**Это основной способ держать данные актуальными.** Использовать вместо
`tuik`, когда не нужен явный диапазон лет.

### Полная сигнатура

```powershell
python -m pipeline.cli update [OPTIONS]
```

### Аргументы

| Аргумент | Тип | По умолчанию | Описание |
| --- | --- | --- | --- |
| `--partner` | str | `75` | TUIK код страны-партнёра. |
| `--lang` | str | `en` | Язык Qlik-приложения (`en`/`tr`). |
| `--refresh-latest` | int | `1` | Сколько **последних опубликованных** месяцев перезалить (закрывает окно ревизий TUIK). `0` = не перезаливать, только догнать пропуски. |
| `--headed` | flag | `false` | Показать окно браузера. |

### Когда TUIK публикует новые данные

Эмпирически: **данные за месяц X появляются в TUIK BI ~15-25 числа месяца X+2**.
Например, данные за март появляются в середине-конце мая. Это типичный лаг
для бюллетеней внешней торговли в большинстве стран (Турция ничем
не выделяется).

В первые **~2-4 недели** после публикации TUIK мелко корректирует данные:
- Поздние подачи декларантов
- Уточнения по конфиденциальным линиям (некоторые позиции газа/нефти
  переходят между раскрытием и агрегацией под `Gizli veri`)
- Курсовые пересчёты USD/EUR/TRY

Поэтому `--refresh-latest 1` (дефолт) разумен: всегда перекачивает самый
свежий месяц. Если хочется параноидально — `--refresh-latest 3` перезальёт
3 последних. Один месяц = ~5 тыс. строк, ~30 секунд = недорого.

### Примеры

**Первая массовая закачка** (нечего ещё перезаливать):

```powershell
python -m pipeline.cli update --refresh-latest 0
```

**Регулярное обновление** — догнать новые + обновить последний:

```powershell
python -m pipeline.cli update
# updated 2 month(s)
#   2026-03:   4,832 rows       # последний месяц перезалит
#   2026-04:   5,123 rows       # новый месяц подкачан
```

**Когда обновлять не надо** (всё уже актуально):

```powershell
python -m pipeline.cli update
# nothing to update
```

**Параноидально** — перезалить 3 последних месяца:

```powershell
python -m pipeline.cli update --refresh-latest 3
```

**Обновление по нескольким партнёрам подряд**:

```powershell
"75","4","380","804" | ForEach-Object {
    python -m pipeline.cli update --partner $_
}
```

### Что важно помнить

- После `update` нужно **пересобрать `compat-export`** если ты им пользуешься:
  ```powershell
  python -m pipeline.cli update
  python -m pipeline.cli compat-export
  ```
- DuckDB view (`data/trade.duckdb`) обновляется автоматически при следующем
  запросе через `queries.py` / `compat-export` / `monthly` — оно делает
  `CREATE OR REPLACE VIEW` поверх всех parquet-файлов. Принудительно: `python -m pipeline.cli duckdb-view`.
- Команда **идемпотентна**: повторный запуск ничего не сломает, просто
  проверит `coverage` и выйдет без работы если всё ок.

---

## CLI: команда `coverage`

Diagnostic: показывает что сейчас в TUIK BI и что есть локально на диске.
Полезно перед/после `update`, чтобы понять состояние.

### Аргументы

| Аргумент | По умолчанию | Описание |
| --- | --- | --- |
| `--partner` | `75` | TUIK код партнёра. |
| `--lang` | `en` | Язык Qlik-приложения. |

### Пример вывода

```powershell
python -m pipeline.cli coverage
# TUIK BI for partner=75:
#   years: 2013..2026 (14 years)
#   latest published: 2026-03
#   latest year months: [1, 2, 3]
# Local: 159/159 months on disk
```

Если всё на диске — `159/159`. Если чего-то не хватает — увидишь список
конкретных `(year, month)` под ключом `Missing:`.

---

## CLI: команда `compat-export`

Превращает партиции TUIK BI в drop-in замену старого `tr_full.parquet` —
схему `NAPR/PERIOD/STRANA/TNVED/EDIZM/EDIZM_ISO/STOIM/NETTO/KOL/TNVED4/TNVED6/TNVED2`
+ дополнительные колонки `ISTPOZ`, `ISTPOZ_ADI`, `TNVED_EN_NAME`,
`TNVED_RU_NAME`.

### Полная сигнатура

```powershell
python -m pipeline.cli compat-export [OPTIONS]
```

### Аргументы

| Аргумент | Тип | По умолчанию | Описание |
| --- | --- | --- | --- |
| `--partner` | str | `75` | TUIK код страны-партнёра. |
| `--from` | int | None (=все годы) | Первый год включительно. |
| `--to` | int | None (=все годы) | Последний год включительно. |
| `--hs` | int | `8` | Длина кода в колонке `TNVED`: `6`/`8`/`10`/`12`. **`8` = точная замена `tr_full.parquet`**. `12` = полная детализация GTIP-12. |
| `--country` | str | `TR` | Значение колонки `STRANA` (ISO-2 партнёра в russia-centric нотации). |
| `--out` | Path | `data/exports/tr_full_compat.parquet` | Путь выходного parquet. |
| `--no-aggregate` | flag | `false` (т.е. агрегировать) | Не суммировать строки по ключу. Имеет смысл только если хочешь 1:1 дамп без свёртки. |

### Примеры

**Drop-in замена для `tr_full.parquet`** (Россия, все годы):

```powershell
python -m pipeline.cli hs-sync                                # один раз
python -m pipeline.cli compat-export
# -> data/exports/tr_full_compat.parquet
# 100% совместима по схеме со старым файлом, плюс 4 extra-колонки.
```

**Только 2024 год, полная GTIP-12 детализация**:

```powershell
python -m pipeline.cli compat-export --from 2024 --to 2024 --hs 12 `
    --out data/exports/tr_2024_gtip12.parquet
```

**HS-6 уровень** (для сравнения с UN Comtrade напрямую):

```powershell
python -m pipeline.cli compat-export --hs 6 --out data/exports/tr_hs6.parquet
```

**Другая страна-партнёр** (Германия, со STRANA=DE):

```powershell
python -m pipeline.cli compat-export --partner 4 --country DE `
    --out data/exports/de_full_compat.parquet
```

**Без агрегации** (1 строка на каждый исходный GTIP-12; редко нужно):

```powershell
python -m pipeline.cli compat-export --no-aggregate `
    --out data/exports/tr_raw_unaggregated.parquet
```

### Маппинг старая → новая схема

| Старая колонка | Источник в новой | Замечания |
| --- | --- | --- |
| `NAPR` | `IHRITH` ↔ `flow` | **Russia-centric**: турецкий экспорт (`X`) → `ИМ` (импорт РФ), турецкий импорт (`M`) → `ЭК` (экспорт РФ). |
| `PERIOD` | `YIL` + `AY` | Первое число месяца, `datetime64[ns]`. |
| `STRANA` | argument `--country` | ISO-2 партнёра. По умолчанию `TR`. |
| `TNVED` | `LEFT(ISTPOZ, --hs)` | Сначала zero-padding до 12 символов (фикс багa Qlik со срезкой ведущих нулей). |
| `EDIZM` / `EDIZM_ISO` | `OLCU_ADI` через `core.edizm.resolve_edizm_records` | Покрытие ~99.7%. Без mgimo проекта работает на встроенном fallback (~30 турецких единиц). |
| `STOIM` | `Sum(DOLAR)` (= `usd`) | `float64`, USD. |
| `NETTO` | `Sum(MIKTAR_1)` (= `q1`) | `float64`, килограммы. |
| `KOL` | `Sum(MIKTAR_2)` (= `q2`) | `float64`, дополнительная единица (зависит от `OLCU_ADI`). |
| `TNVED2`/`TNVED4`/`TNVED6` | `TNVED[:2]` etc. | Через `finalize_country_output` из mgimo. |

**Extra-колонки** (зеро-стоимость для downstream — pandas/duckdb их просто игнорируют):

- `ISTPOZ` — полный 12-значный GTIP (сохраняется и при `--hs 8`)
- `ISTPOZ_ADI` — оригинальное турецкое название товара
- `TNVED_EN_NAME` — английское HS-6 описание (через WCO datasets/harmonized-system)
- `TNVED_RU_NAME` — русское ТНВЭД название из `metadata/tnved.csv` mgimo-проекта

### Sanity-check после экспорта

```powershell
python -c @"
import pandas as pd
df = pd.read_parquet('data/exports/tr_full_compat.parquet')
print('Строк:', len(df))
print('Период:', df.PERIOD.min(), '..', df.PERIOD.max())
print('Россия → Турция (NAPR=ЭК), $:', df.loc[df.NAPR=='ЭК','STOIM'].sum()/1e9, 'млрд')
print('Турция → Россия (NAPR=ИМ), $:', df.loc[df.NAPR=='ИМ','STOIM'].sum()/1e9, 'млрд')
print('EDIZM покрытие:', (df.EDIZM.notna().mean()*100).round(1), '%')
"@
```

---

## CLI: команда `comtrade`

Резервный источник: UN Comtrade Plus HS-6 ежемесячные данные. Полезен для
кросс-валидации (расхождения с TUIK обычно ~0.5% за счёт rerating валюты,
конфиденциальных линий, золота).

### Аргументы

| Аргумент | Тип | По умолчанию | Описание |
| --- | --- | --- | --- |
| `--from` | int | текущий год − 9 | Первый год. |
| `--to` | int | текущий год | Последний год. |

### Примеры

```powershell
# Получить ключ: https://comtradedeveloper.un.org/
# Положить в .env: UN_COMTRADE_KEY=xxx

python -m pipeline.cli comtrade --from 2020 --to 2024
# -> data/comtrade/turkey_russia_<year>.parquet (по файлу на год)
```

Без ключа работает через public preview endpoint (с лимитами строк) — для
быстрой проверки.

---

## CLI: команда `smoke`

Быстрый sanity-check: тянет один месяц из TUIK BI, печатает первые 5 строк
и помесячные итоги USD/EUR/TRY. **Не пишет на диск** — только в stdout.

### Аргументы

| Аргумент | Тип | По умолчанию | Описание |
| --- | --- | --- | --- |
| `--year` | int | текущий год − 1 | Год. |
| `--month` | int | `1` | Месяц (1-12). |

### Примеры

```powershell
# Дефолт: январь предыдущего года
python -m pipeline.cli smoke

# Конкретный месяц
python -m pipeline.cli smoke --year 2024 --month 6

# С браузером (для отладки если что-то сломалось у TUIK)
$env:HEADLESS="false"; python -m pipeline.cli smoke --year 2024 --month 1
```

Типичный вывод (~30с включая boot Chromium):

```
got 4,943 rows for Russia 2024-01
  YIL  AY   IHRITH        ISTPOZ  ... usd     eur       try      q1    q2 flow partner_kodu
0 2024  1  İhracat  401180000000  ... 733981  672069   22030233  219787 2854    X           75
...
              usd          eur         try
flow
M     4326845091  3984573103  131093148716
X      627951024   579002472   19046126437
```

---

## CLI: команда `countries`

Возвращает таблицу всех доступных в TUIK BI стран с их кодами `ULKE_KODU`.
Нужно перед сбором по новому партнёру.

### Аргументы

| Аргумент | Тип | По умолчанию | Описание |
| --- | --- | --- | --- |
| `--contains` | str | пусто | Подстрока (case-insensitive) для фильтра. |

### Примеры

```powershell
python -m pipeline.cli countries
#    1  Almanya Federal Cumhuriyeti
#    2  ABD
#    ...

python -m pipeline.cli countries --contains russia
#   75  Rusya Federasyonu

python -m pipeline.cli countries --contains china
#  120  Çin Halk Cumhuriyeti

python -m pipeline.cli countries --contains kazakh
#  316  Kazakistan
```

NB: TUIK использует собственную кодировку, не ISO-3166. Россия = `75`, не `643`.

---

## CLI: команды `monthly` / `top`

Встроенная DuckDB-аналитика поверх собранных партиций. Удобно для быстрой проверки.

### `monthly` — помесячные итоги

| Аргумент | Тип | По умолчанию | Описание |
| --- | --- | --- | --- |
| `--partner` | str | `75` | TUIK код партнёра. |
| `--csv` | flag | `false` | Дополнительно сохранить в `data/exports/monthly_partner=<K>.csv`. |

```powershell
python -m pipeline.cli monthly
# year month flow         usd         eur          try  n_gtip       qty1        qty2
# 2024     1    M  4326845091  3984573103  131093148716    1025  316721354   ...
# 2024     1    X   627951024   579002472   19046126437    3918   84238391   ...
# ...

python -m pipeline.cli monthly --partner 4 --csv     # Германия + дамп в CSV
```

### `top` — топ N товаров по обороту

| Аргумент | Тип | По умолчанию | Описание |
| --- | --- | --- | --- |
| `--partner` | str | `75` | TUIK код партнёра. |
| `--year` | int | None | Фильтр по году. |
| `--flow` | str | None | `X` = экспорт Турции, `M` = импорт Турции. |
| `--n` | int | `20` | Сколько строк показать. |
| `--hs-level` | int | `12` | Уровень группировки: `2`/`4`/`6`/`8`/`12`. |
| `--csv` | flag | `false` | Сохранить в CSV. |

```powershell
# Топ-10 категорий HS-6 турецкого импорта из РФ в 2024
python -m pipeline.cli top --year 2024 --flow M --hs-level 6 --n 10

# Топ-20 турецкого экспорта в РФ за все годы, HS-4
python -m pipeline.cli top --flow X --hs-level 4

# То же + CSV
python -m pipeline.cli top --year 2024 --flow X --hs-level 6 --n 50 --csv
# -> data/exports/top50_hs6_y2024_fX.csv
```

---

## CLI: команда `duckdb-view`

(Пере)регистрирует DuckDB view `tuik_trade` поверх всех parquet-партиций.
Используется автоматически другими командами, но можно вызвать вручную после
ручных правок в `data/raw/`.

```powershell
python -m pipeline.cli duckdb-view
# tuik_trade view ready: 59,942 rows | db=G:\YandexDisk\HSE\turkey-foreign-trade\data\trade.duckdb
```

Потом можно ходить в `data/trade.duckdb` напрямую — см.
[Аналитика через DuckDB](#аналитика-через-duckdb).

---

## CLI: команда `hs-sync`

Скачивает (или обновляет) кэш HS-6 английских описаний из репозитория
`datasets/harmonized-system` (MIT/PDDL, ~6.9k кодов, ~600 КБ).

| Аргумент | Тип | По умолчанию | Описание |
| --- | --- | --- | --- |
| `--refresh` / `--no-refresh` | flag | `--refresh` | Перекачать даже если кэш уже есть. |

```powershell
python -m pipeline.cli hs-sync                   # обновить кэш
python -m pipeline.cli hs-sync --no-refresh      # ничего не делать, если кэш есть
# HS-6 EN reference: 6,940 codes (cached in G:\...\data\refs)
```

Кэш живёт вечно в `data/refs/hs6_en.csv`. `compat-export` использует его для
заполнения `TNVED_EN_NAME`.

---

## CLI: команда `backfill-trade-system`

Одноразовая миграция: добавляет колонку `trade_system="general"` в parquet-партиции,
собранные до её появления. Идемпотентна — повторный запуск ничего не делает.

```powershell
python -m pipeline.cli backfill-trade-system
# backfilled G:\...\data\raw\tuik_bi\partner_kodu=75\year=2024\month=01.parquet
# ...
# done: 12 files updated, 0 already had column
```

Запускать имеет смысл один раз, если у тебя есть данные собранные ранее. Новые
загрузки через `tuik` уже пишут эту колонку сами.

---

## Глобальный workflow обновления

Полный цикл «у нас N месяцев на диске → у нас N+k месяцев + актуальный
compat-файл». Можно повесить на cron / Windows Task Scheduler.

### Минимальный pipeline

```powershell
# 1. Догнать новые месяцы и перезалить последний
python -m pipeline.cli update

# 2. Пересобрать tr_full_compat.parquet (если используется downstream)
python -m pipeline.cli compat-export
```

Всё. Идемпотентно — можно запускать сколь угодно часто.

### Полная версия с проверками

```powershell
# Перед обновлением: что сейчас в TUIK vs локально
python -m pipeline.cli coverage

# Обновление
python -m pipeline.cli update

# (Опц.) Обновить HS-6 EN справочник, если он давно не обновлялся
python -m pipeline.cli hs-sync

# Пересобрать compat-export (по умолчанию HS-8 drop-in)
python -m pipeline.cli compat-export

# (Опц.) Также HS-12 версию для полной детализации
python -m pipeline.cli compat-export --hs 12 --out data/exports/tr_full_compat_hs12.parquet

# (Опц.) Кросс-проверка с UN Comtrade
python -m pipeline.cli comtrade --from 2024 --to 2026
```

### Windows Task Scheduler

Создать задачу, которая раз в неделю по понедельникам в 08:00 запускает
обновление:

```powershell
$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument @'
-NoProfile -Command "cd G:\YandexDisk\HSE\turkey-foreign-trade; .\.venv\Scripts\Activate.ps1; python -m pipeline.cli update; python -m pipeline.cli compat-export"
'@ `
    -WorkingDirectory 'G:\YandexDisk\HSE\turkey-foreign-trade'

$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At 08:00

Register-ScheduledTask `
    -Action $action `
    -Trigger $trigger `
    -TaskName 'TUIK weekly update' `
    -Description 'Auto-update Turkey-Russia trade data from TUIK BI'
```

Управление задачей:

```powershell
Get-ScheduledTask -TaskName 'TUIK weekly update'           # статус
Start-ScheduledTask -TaskName 'TUIK weekly update'         # запустить вручную
Unregister-ScheduledTask -TaskName 'TUIK weekly update'    # удалить
```

### Cron (Linux/Mac)

```cron
# /etc/cron.d/tuik-update — каждый понедельник в 08:00
0 8 * * 1 user cd /path/to/turkey-foreign-trade && .venv/bin/python -m pipeline.cli update >> data/cron.log 2>&1
5 8 * * 1 user cd /path/to/turkey-foreign-trade && .venv/bin/python -m pipeline.cli compat-export >> data/cron.log 2>&1
```

### Что делать если `update` сломался посередине

`update` — идемпотентен. Если упал на 7-м из 50 месяцев — просто запусти
ещё раз, он догонит остальные 43:

```powershell
python -m pipeline.cli update
```

Все ранее успешно скачанные месяцы останутся на диске. **Не нужно
вмешиваться вручную** — пайплайн сам разберётся что докачать.

### История и максимальный охват

TUIK BI отдаёт **с января 2013** по последний опубликованный месяц
(сейчас — март 2026, лаг ~2 месяца). Это **14 лет помесячных данных**:

- 14 лет × 12 месяцев ≈ 159 партиций на партнёра
- ~5 000 строк GTIP-12 на партицию по России
- ~800 тыс. строк всего по торговле Россия-Турция
- ~30-40 МБ в сжатом zstd parquet

История **до 2013 года через TUIK BI недоступна** — ни через mashup, ни
через прямой Qlik API, ни через TUIK SDDS. Если нужны 2002-2012:

| Источник | Уровень | Замечания |
| --- | --- | --- |
| **UN Comtrade** | HS-6 | `python -m pipeline.cli comtrade --from 2002` — есть, но HS-6 максимум |
| **Eurostat Comext** | CN8 | Зеркальная торговля ЕС-Турция, не Россия-Турция |
| **TUIK SDDS legacy** | HS-2/HS-4 | Старые CSV-дампы; нужна отдельная парсилка |
| **Russian customs (ФТС)** | ТНВЭД-10 | С точки зрения РФ, через `russian_collector` в mgimo-проекте |

Подключение этих источников выходит за рамки данного пайплайна. Если
понадобится — обращайся, добавлю модули.

---

## Python API

Если CLI не хватает гибкости — пайплайн полностью доступен как библиотека.

### Прямой запрос к Qlik

```python
import asyncio
from pipeline.tuik_bi import TuikBI
from pipeline.config import DEFAULT_DIMS_GTIP12, DEFAULT_MEASURES
from pipeline.normalize import cube_to_dataframe


async def fetch_one_month(year: int, month: int, partner: str = "75"):
    async with TuikBI(app_key="general_en", headless=True) as client:
        cube = await client.query(
            selections={
                "ULKE_KODU": [partner],
                "YIL": [year],
                "AY": [month],
            },
            dims=DEFAULT_DIMS_GTIP12,
            measures=DEFAULT_MEASURES,
        )
        return cube_to_dataframe(cube, DEFAULT_DIMS_GTIP12, DEFAULT_MEASURES)


df = asyncio.run(fetch_one_month(2024, 6))
print(df.shape, df.columns.tolist())
```

### Кастомные дименсии

```python
# Хочу разрез по способу перевозки и таможне
custom_dims = DEFAULT_DIMS_GTIP12 + ["TASIMA_SEKLI_ADI", "GUMRUK_ADI"]

async def fetch_with_transport():
    async with TuikBI() as c:
        cube = await c.query(
            selections={"ULKE_KODU": ["75"], "YIL": [2024]},
            dims=custom_dims,
            measures=DEFAULT_MEASURES,
        )
        return cube_to_dataframe(cube, custom_dims, DEFAULT_MEASURES)

df = asyncio.run(fetch_with_transport())
print(df.groupby("TASIMA_SEKLI_ADI")["usd"].sum().sort_values(ascending=False))
```

### Бакфилл массивом (без CLI)

```python
from pipeline.runner import main_sync

main_sync(
    year_from=2020,
    year_to=2024,
    partner_kodu="75",
    app_key="general_en",
    skip_existing=True,
    headless=True,
)
```

### Программное обновление

```python
from pipeline.runner import update_sync

# Догнать пропуски + перезалить последний месяц
counts = update_sync(partner_kodu="75", refetch_latest_n=1)
print(f"Updated {len(counts)} months")
```

### Discovery: что TUIK сейчас отдаёт

```python
import asyncio
from pipeline.tuik_bi import TuikBI, discover_coverage

async def what_is_available():
    async with TuikBI() as c:
        return await discover_coverage(c, partner_kodu="75")

cov = asyncio.run(what_is_available())
print(f"TUIK history: {cov['years'][0]}..{cov['years'][-1]}")
print(f"Latest published month: {cov['latest_year']}-{cov['latest_month']:02d}")
```

### Программный compat-export

```python
from pipeline.compat import compat_export

path = compat_export(
    partner_kodu="75",
    year_from=2020,
    year_to=2024,
    hs_level=8,
    country_code="TR",
    out=None,                  # дефолт data/exports/tr_full_compat.parquet
    aggregate=True,
)
print("Wrote", path)
```

### compat-export без записи на диск

Если нужен только in-memory DataFrame:

```python
from pipeline.compat import _load_raw, to_old_schema

raw = _load_raw(partner_kodu="75", year_from=2024, year_to=2024)
df = to_old_schema(raw, hs_level=8, country_code="TR", aggregate=True)
# df теперь в памяти, можно работать дальше без записи parquet
```

### HS-6 EN lookup отдельно

```python
from pipeline.hs_names import resolve_codes, build_hs_lookup

# Для конкретного списка кодов
codes = ["010641", "271019", "844339"]
print(resolve_codes(codes))
# {'010641': 'Insects; live, bees', '271019': '...', '844339': '...'}

# Или полный словарь {code: en_name}
all_codes = build_hs_lookup()
print(len(all_codes), "codes available")
```

### Список стран программно

```python
import asyncio
from pipeline.tuik_bi import TuikBI, list_countries

async def all_countries():
    async with TuikBI() as c:
        return await list_countries(c)

rows = asyncio.run(all_countries())
print(rows[:5])
# [{'ulke_kodu': '1', 'ulke_adi': 'Almanya...'}, ...]
```

---

## Структура хранилища данных

```
data/
├── raw/
│   └── tuik_bi/                               # Source-prefixed
│       └── partner_kodu=75/                   # Hive partition: код партнёра TUIK
│           └── year=2024/                     # Hive partition: год
│               ├── month=01.parquet
│               ├── month=02.parquet
│               └── ...
├── refs/
│   └── hs6_en.csv                             # WCO HS-6 EN кэш
├── comtrade/
│   └── turkey_russia_2024.parquet             # UN Comtrade выгрузки
├── exports/
│   ├── tr_full_compat.parquet                 # compat-export результат
│   └── monthly_partner=75.csv                 # CSV дампы из CLI
├── trade.duckdb                               # DuckDB БД (view над parquets)
└── pipeline.log                               # ротируемый лог
```

### Схема `data/raw/tuik_bi/.../month=MM.parquet`

| Колонка | Тип | Описание |
| --- | --- | --- |
| `YIL` | `Int32` | Год. |
| `AY` | `Int8` | Месяц (1-12). |
| `IHRITH` | `object` | Турецкое название потока: `İhracat`/`İthalat`. |
| `ISTPOZ` | `object` | GTIP-12 код, всегда 12 символов (zero-padded). |
| `ISTPOZ_ADI` | `object` | Турецкое название позиции. |
| `OLCU_ADI` | `object` | Турецкое название дополнительной единицы (KG, KG/ADET, ÇİFT, ...). |
| `usd` | `int64` / `float` | Сумма `Sum(DOLAR)`. |
| `eur` | `int64` / `float` | Сумма `Sum(EURO)`. |
| `try` | `int64` / `float` | Сумма `Sum(TL)`. |
| `q1` | `int64` / `float` | Сумма `Sum(MIKTAR_1)` (кг). |
| `q2` | `int64` / `float` | Сумма `Sum(MIKTAR_2)` (доп. единица). |
| `flow` | `object` | Канонический поток: `X`=экспорт, `M`=импорт (с точки зрения Турции). |
| `partner_kodu` | `object` | Код партнёра (Россия = `75`). |
| `trade_system` | `object` | Всегда `general`. |

---

## Схема выхода compat-export

При `--hs 8` (по умолчанию):

| Колонка | Тип | Происхождение |
| --- | --- | --- |
| `NAPR` | `object` ∈ {`ИМ`, `ЭК`} | `flow` через russia-centric маппинг |
| `PERIOD` | `datetime64[ns]` | `YIL` + `AY` → первое число месяца |
| `STRANA` | `object` | Аргумент `--country` (`TR`) |
| `TNVED` | `object` (8 символов) | `ISTPOZ[:8]` |
| `EDIZM` | `object` | Через `resolve_edizm_records(OLCU_ADI)` |
| `EDIZM_ISO` | `object` | Код ОКЕИ (e.g. `166` для килограмма) |
| `STOIM` | `float64` | USD сумма |
| `NETTO` | `float64` | Килограммы |
| `KOL` | `float64` | Дополнительное количество |
| `TNVED4` | `object` (4) | `TNVED[:4]` |
| `TNVED6` | `object` (6) | `TNVED[:6]` |
| `TNVED2` | `object` (2) | `TNVED[:2]` |
| **`ISTPOZ`** | `object` (12) | Оригинальный GTIP-12 (extra) |
| **`ISTPOZ_ADI`** | `object` | Турецкое название (extra) |
| **`TNVED_EN_NAME`** | `object` | EN из WCO (extra) |
| **`TNVED_RU_NAME`** | `object` | RU из mgimo `tnved.csv` (extra) |

Контрактные 12 колонок **точно совпадают** со старым `tr_full.parquet` (включая dtypes).

---

## Интеграция с проектом mgimo-foreign_trade

Этот пайплайн умеет напрямую использовать ОКЕИ / ТНВЭД справочники и
финалайзеры из соседнего проекта `mgimo-foreign_trade`.

### Автодетект

При импорте `pipeline._mgimo` проверяет:
1. `MGIMO_FOREIGN_TRADE_SRC` env variable
2. `../mgimo-foreign_trade/src` (рядом с этим проектом)
3. `../../mgimo-foreign_trade/src` (если оба проекта в подпапке)

Если найден — добавляет в `sys.path` и импортирует:

- `core.normalization_rules.resolve_edizm_records` — ОКЕИ маппинг
- `core.country_processor_contract.finalize_country_output` — финалайзер схемы
- `pipelines.merge_pipeline.load_common_edizm_mapping` — каноническая `edizm.csv`
- `pipelines.merge_pipeline.load_tnved_mapping` — `tnved.csv` с русскими названиями

### Проверка статуса

```python
from pipeline._mgimo import mgimo_available, mgimo_root

print("mgimo connected:", mgimo_available())
print("mgimo root:", mgimo_root())
```

### Если mgimo не найден

`compat-export` всё равно работает, но:

- `EDIZM`/`EDIZM_ISO` резолвится только через встроенный fallback (~30 турецких алиасов, покрытие ~95% вместо 99.7%)
- `TNVED_RU_NAME` будет `None` во всех строках
- `finalize_country_output` использует локальную копию контракта (та же логика, но не обновляется при изменениях в mgimo)

### Принудительно указать путь

В `.env`:

```ini
MGIMO_FOREIGN_TRADE_SRC=D:/path/to/mgimo-foreign_trade/src
```

---

## Аналитика через DuckDB

После любого `tuik` или `duckdb-view` есть `data/trade.duckdb` с view `tuik_trade`:

```python
import duckdb
con = duckdb.connect("data/trade.duckdb")
con.execute("SELECT COUNT(*) FROM tuik_trade").fetchone()
# (59942,)
```

### Примеры запросов

**Топ HS-2 разделов по экспорту РФ в Турцию**:

```sql
SELECT
  LEFT(ISTPOZ, 2) AS chapter,
  SUM(usd) / 1e9 AS usd_bln
FROM tuik_trade
WHERE partner_kodu = '75' AND flow = 'M'        -- импорт Турции из РФ = экспорт РФ
GROUP BY chapter
ORDER BY usd_bln DESC
LIMIT 10;
```

**Динамика торговли по месяцам**:

```sql
SELECT
  YIL, AY, flow,
  SUM(usd) AS usd
FROM tuik_trade
WHERE partner_kodu = '75'
GROUP BY YIL, AY, flow
ORDER BY YIL, AY, flow;
```

**Какие GTIP-12 коды появились впервые в 2024**:

```sql
WITH first_seen AS (
  SELECT ISTPOZ, MIN(YIL) AS first_year
  FROM tuik_trade
  WHERE partner_kodu = '75'
  GROUP BY ISTPOZ
)
SELECT t.ISTPOZ, ANY_VALUE(t.ISTPOZ_ADI) AS name, SUM(t.usd) AS usd_2024
FROM tuik_trade t
JOIN first_seen fs ON t.ISTPOZ = fs.ISTPOZ
WHERE fs.first_year = 2024 AND t.YIL = 2024
GROUP BY t.ISTPOZ
ORDER BY usd_2024 DESC
LIMIT 20;
```

**Доля конфиденциальных строк** (TUIK маскирует чувствительные позиции под `Gizli veri` или код `279999`):

```sql
SELECT
  YIL,
  SUM(CASE WHEN ISTPOZ_ADI LIKE 'Gizli%' OR LEFT(ISTPOZ,6) = '279999' THEN usd END) AS confidential_usd,
  SUM(usd) AS total_usd,
  ROUND(100.0 * SUM(CASE WHEN ISTPOZ_ADI LIKE 'Gizli%' OR LEFT(ISTPOZ,6) = '279999' THEN usd END) / SUM(usd), 2) AS pct
FROM tuik_trade
WHERE partner_kodu = '75'
GROUP BY YIL ORDER BY YIL;
```

---

## Troubleshooting

### "Qlik app not found" при загрузке

TUIK BI время от времени пересобирается с новыми GUID. Обновить их можно так:

```powershell
Invoke-WebRequest https://bi.tuik.gov.tr/extensions/tuik-mashup/index.html `
  -OutFile mashup.html
Select-String mashup.html -Pattern 'index-[a-z0-9]+\.js'
# из найденного URL скачать JS-бандл:
Invoke-WebRequest https://bi.tuik.gov.tr/extensions/tuik-mashup/assets/index-XXX.js `
  -OutFile bundle.js
Select-String bundle.js -Pattern 'VITE_APP_GENERAL_EN[A-Z_]*\s*[:=]\s*"[a-f0-9-]+"'
```

Свежие GUID положить в `pipeline/config.py` → `TUIK_APPS`.

### `Error: Result too large` или таймаут на больших запросах

Это лимит Qlik (~10k ячеек за один fetch). Должно решаться автоматически —
клиент сам паджинирует через `getHyperCubeData`. Если всё-таки сломалось:

```powershell
python -m pipeline.cli -v DEBUG tuik --from 2024 --to 2024 --headed
```

В DEBUG-логе видно `page=` и `rows=`/`totalRows=` для каждого запроса.

### `Unable to merge: Field partner_kodu has incompatible types`

DuckDB / pyarrow видит партиции с разными dtypes для одной колонки (бывает
если в часть файлов колонка попала как `dictionary`, в часть — как `string`).
Решение: пересобрать проблемные месяцы:

```powershell
python -m pipeline.cli tuik --from 2024 --to 2024 --rerun
```

### `ModuleNotFoundError: core.normalization_rules`

Это значит, mgimo проект не найден. Это не критично — `compat-export` всё
равно отработает на встроенном fallback. Чтобы подключить:

```ini
# в .env
MGIMO_FOREIGN_TRADE_SRC=G:/YandexDisk/HSE/mgimo-foreign_trade/src
```

Проверить:

```python
from pipeline._mgimo import mgimo_available
print(mgimo_available())   # должно быть True
```

### 11-значные ISTPOZ коды вместо 12-значных

Qlik страйпит ведущие нули у numeric-looking строк (chapter 01-09). Фикс
включён по умолчанию в `pipeline/normalize.py` (`.zfill(12)`). Для старых
партиций, собранных до фикса:

```powershell
python -m pipeline.cli tuik --from 2020 --to 2024 --rerun
```

(Это пересоберёт партиции — fix применится автоматически.) Или, проще,
пересборка происходит автоматически при `compat-export` — он делает zfill в
`_load_raw`.

### Браузер открывается, но ничего не происходит

Запусти с `--headed` и смотри в консоль DevTools. Часто причина — TUIK
вернул капчу WAF (это редко, но случается при слишком частых обращениях):

```powershell
python -m pipeline.cli tuik --from 2024 --to 2024 --headed
```

Подожди минуту, перезапусти.

### `UnicodeEncodeError: 'charmap' codec`

PowerShell не любит UTF-8 в stdout. Лечится переменной окружения:

```powershell
$env:PYTHONIOENCODING="utf-8"
python -m pipeline.cli smoke --year 2024 --month 1
```

Можно добавить в профиль `$PROFILE`.

---

## Внутренности: как работает Qlik-клиент

Чтобы был контекст для дебага.

### Boot sequence

1. **Открыть mashup URL** — `https://bi.tuik.gov.tr/extensions/tuik-mashup/index.html?lang=en`. Это React-обёртка вокруг Qlik Sense Enterprise.
2. **Дождаться `window.qlikMashupLoader.promise`** — внутренний промис React-приложения, резолвится когда Qlik Capability API готов.
3. **Подгрузить модуль `qlik` через `window.require`** — RequireJS-обёртка над Qlik API.
4. **`qlik.openApp(GUID, {host, prefix, port, isSecure})`** — открывает сессию к выбранному Qlik app (GTS English по умолчанию).
5. **`await app.getList('FieldList')`** — sanity check что приложение реально загрузилось.

### Query lifecycle

1. **`app.clearAll(false)`** — очистить все global selections (мы их не используем).
2. **Построить set-analysis prefix** из `selections` dict:
   ```
   {"YIL": [2024], "AY": [1], "ULKE_KODU": ["75"]}
   →  "{<YIL={'2024'}, AY={'1'}, ULKE_KODU={'75'}>}"
   ```
3. **Завернуть каждый measure expression** в set-analysis:
   ```
   "Sum(DOLAR)"  →  "Sum({<YIL={'2024'}, ...>} DOLAR)"
   ```
4. **`engine.createSessionObject({qHyperCubeDef: {qDimensions, qMeasures, qInitialDataFetch}})`** — создать session object с hypercube'ом.
5. **`obj.getLayout()`** — получить первую страницу + общий размер (`qSize.qcy`).
6. **Цикл `obj.getHyperCubeData('/qHyperCubeDef', [{qLeft, qTop, qWidth, qHeight}])`** — добирать остальные страницы до `totalRows`.

### Лимиты Qlik

- **~10к ячеек за один `getHyperCubeData`** — обходится pagination'ом (`pageHeight = floor(8000 / width)`).
- **Глобальные selections неидемпотентны** — поэтому мы используем set-analysis в выражениях, а не `app.field(...).selectValues()`.
- **`qNullSuppression: true`** на dimensions — без этого Qlik вернёт строки с NULL во всех мерах.

### Альтернатива: прямой REST к engine?

Не работает — TUIK сидит за Netscaler WAF, который блокирует не-браузерные клиенты (возвращает 1214-байтовый HTML с 404 на все HTTP-запросы без real browser session). Поэтому только Playwright + JS injection.

---

## Лицензия и атрибуция данных

Данные: © Турецкий статистический институт (TÜİK). Цитировать как
*"TÜİK, Foreign Trade Statistics"* при публикации.

UN Comtrade: © United Nations.

HS-6 справочник: ODC-PDDL (datasets/harmonized-system).

ТНВЭД-справочник (русские названия): берётся из соседнего проекта
mgimo-foreign_trade — его лицензию см. там.
