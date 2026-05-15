# Пайплайн внешней торговли Турции (Турция ↔ Россия, GTIP-12, помесячно)

Загрузчик данных по **внешней торговле Турции с Россией** с
максимальной детализацией (GTIP-12 / HS-12, помесячно) из официального
TUIK BI mashup на `https://bi.tuik.gov.tr/extensions/tuik-mashup/`, с
резервным источником UN Comtrade для кросс-проверки.

**Система учёта: только General Trade System (GTS).** STS-приложения есть
в источнике TUIK BI, но в этом проекте намеренно не подключены — UN Comtrade
и зеркальные данные ЕС идут по GTS, и смешивание систем дало бы скрытое
смещение ~2–5% на двусторонних итогах.

> 📖 **Полная документация на русском с примерами по каждому аргументу CLI:**
> [`docs/USAGE.ru.md`](docs/USAGE.ru.md)

## Как выглядят данные

```
year  month flow  n_gtip  usd_bln
2024      1   M     1025    4.327     # Импорт Турции из России (= экспорт РФ в Турцию), USD
2024      1   X     3918    0.628     # Экспорт Турции в Россию (= импорт РФ из Турции), USD
...
```

Колоночная схема (Parquet): `YIL, AY, IHRITH ("İhracat"/"İthalat"), ISTPOZ
(12-значный GTIP), ISTPOZ_ADI, OLCU_ADI (ед. изм.), usd, eur, try, q1, q2,
flow ("X"|"M"), partner_kodu, trade_system`.

Hive-партиционирование:
`data/raw/tuik_bi/partner_kodu={K}/year={YYYY}/month={MM}.parquet` со
сжатием `zstd`. В среднем ~5 000 строк на (партнёр × месяц). 10 лет данных
по России ≈ 600 тыс. строк, ~30 МБ в сжатом виде.

## Архитектура

TUIK BI — это React-mashup, оборачивающий дашборд Qlik Sense Enterprise.
Mashup подгружает четыре Qlik-приложения (general/special × en/tr); движок
закрыт фаерволом от не-браузерных клиентов (Netscaler перед ним возвращает
1214-байтовый 404 на все HTTP-запросы без реальной браузерной сессии).

Поэтому мы управляем mashup'ом через **Playwright + Chromium**, ждём пока
загрузится Qlik Capability API (`window.qlik`), и инжектим JS, который
напрямую общается с **engine API**
(`app.model.engineApp.createSessionObject`) через фильтры
**set-analysis** — никаких хрупких UI-кликов, никакого нестабильного
глобального состояния selections, каждый запрос атомарен и идемпотентен.

Файлы:

```
pipeline/
  config.py     константы: GUID приложений (только GTS), Россия (ULKE_KODU=75_, размерности, меры
  tuik_bi.py    Playwright+Qlik клиент (класс TuikBI, set-analysis запросы)
  normalize.py  cube payload -> tidy DataFrame (вкл. zero-pad ISTPOZ до 12)
  storage.py    Parquet-писатель с hive-партиционированием + DuckDB view
  runner.py     цикл массовой загрузки (год × месяц)
  queries.py    DuckDB аналитические хелперы
  comtrade.py   UN Comtrade Plus резервный источник (HS-6 месячный)
  hs_names.py   HS-6 EN описания (WCO datasets/harmonized-system)
  compat.py     drop-in замена tr_full.parquet (NAPR/PERIOD/STRANA/TNVED/...)
  _mgimo.py     опциональный мост к соседнему mgimo-foreign_trade для справочников ОКЕИ + ТНВЭД
  cli.py        Typer CLI entrypoint
```

## Установка

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1                     # Windows PowerShell
pip install -r requirements.txt
python -m playwright install chromium
Copy-Item .env.example .env                      # отредактировать при необходимости
```

## Использование

```powershell
# Smoke-тест на одном месяце (быстро: ~30с включая boot браузера)
python -m pipeline.cli smoke --year 2024 --month 1

# Что сейчас опубликовано в TUIK и сколько у нас уже локально
python -m pipeline.cli coverage

# Полная заливка ВСЕЙ доступной истории (TUIK BI даёт 2013-..., помесячно).
# Идемпотентно: повторный запуск пропустит уже скачанные партиции. ~50 минут на 14 лет.
python -m pipeline.cli update --refresh-latest 0          # первоначальная закачка
python -m pipeline.cli update                              # регулярное обновление (см. ниже)

# Узкая выгрузка конкретного диапазона лет (если update не подходит)
python -m pipeline.cli tuik --from 2016 --to 2025

# Другой партнёр: передать его TUIK ULKE_KODU
python -m pipeline.cli countries --contains german      # найти код
python -m pipeline.cli tuik --partner 4 --from 2020 --to 2024   # Германия

# Наблюдать в браузере (non-headless)
python -m pipeline.cli tuik --from 2024 --to 2024 --headed

# DuckDB view + ad-hoc аналитика
python -m pipeline.cli duckdb-view
python -m pipeline.cli monthly --csv
python -m pipeline.cli top --year 2024 --flow M --hs-level 6 --n 20 --csv

# Кросс-проверка через UN Comtrade (HS-6). Бесплатный ключ:
# https://comtradedeveloper.un.org/ → положить в UN_COMTRADE_KEY в .env.
python -m pipeline.cli comtrade --from 2020 --to 2024

# Drop-in замена легаси tr_full.parquet (NAPR/PERIOD/STRANA/TNVED/...).
# При --hs 8 точно совпадает с country-output контрактом mgimo-проекта,
# плюс extra колонки: ISTPOZ (полный GTIP-12), ISTPOZ_ADI (турецкое название),
# TNVED_EN_NAME (HS-6 EN через WCO), TNVED_RU_NAME (ТНВЭД из соседнего проекта).
python -m pipeline.cli hs-sync                              # один раз: справочник HS-6 EN
python -m pipeline.cli compat-export --hs 8
python -m pipeline.cli compat-export --hs 12 --out data/exports/tr_full_compat_hs12.parquet
```

## Регулярное обновление (когда появляются данные за новый месяц)

TUIK публикует месячные данные **примерно через 1.5–2 месяца** после
окончания отчётного периода (например, данные за март выходят в середине-конце
мая). В первые недели после публикации TUIK ещё корректирует поздние подачи и
агрегацию конфиденциальных линий, поэтому **последний опубликованный месяц
имеет смысл периодически перезаливать**.

Команда `update` делает всё это автоматически:

```powershell
python -m pipeline.cli update
```

Что она делает под капотом:

1. Заходит в TUIK BI, спрашивает у Qlik какие `(год, месяц)` сейчас опубликованы.
2. Сравнивает со списком файлов под `data/raw/tuik_bi/partner_kodu=K/`.
3. Скачивает каждый отсутствующий локально месяц.
4. Дополнительно **перекачивает 1 последний опубликованный месяц** (флаг
   `--refresh-latest 1` по умолчанию) — закрывает окно правок TUIK.
5. Если на диске уже всё — печатает `nothing to update` и выходит.

После закачки сразу пересобери compat-export, если он используется:

```powershell
python -m pipeline.cli update
python -m pipeline.cli compat-export                       # пересобрать tr_full_compat.parquet
```

### Опции `update`

| Опция | По умолчанию | Назначение |
| --- | --- | --- |
| `--partner` | `75` | TUIK код страны-партнёра. |
| `--lang` | `en` | Язык Qlik-приложения (`en`/`tr`). |
| `--refresh-latest` | `1` | Сколько последних опубликованных месяцев перезалить (закрывает окно ревизий TUIK). `0` — не перезаливать ничего, только догнать пропуски (полезно при первой массовой закачке). |
| `--headed` | `false` | Показать окно браузера (для отладки). |

### Примеры

```powershell
# Первая массовая закачка — без перезалива (нечего ещё перезаливать)
python -m pipeline.cli update --refresh-latest 0

# Регулярное обновление: догнать новые месяцы + обновить последний
python -m pipeline.cli update

# Перестраховка: перезалить последние 3 месяца + догнать пропуски
python -m pipeline.cli update --refresh-latest 3

# Обновление по нескольким странам подряд
"75","4","380" | ForEach-Object { python -m pipeline.cli update --partner $_ }
```

### Автоматизация (Windows Task Scheduler)

Создать задачу, которая раз в неделю запускает обновление:

```powershell
$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument '-NoProfile -Command "cd G:\YandexDisk\HSE\turkey-foreign-trade; .\.venv\Scripts\Activate.ps1; python -m pipeline.cli update; python -m pipeline.cli compat-export"' `
    -WorkingDirectory 'G:\YandexDisk\HSE\turkey-foreign-trade'
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At 08:00
Register-ScheduledTask -Action $action -Trigger $trigger -TaskName 'TUIK weekly update'
```

### Cron (Linux/Mac, для полноты)

```cron
# Каждый понедельник в 08:00
0 8 * * 1 cd /path/to/turkey-foreign-trade && .venv/bin/python -m pipeline.cli update && .venv/bin/python -m pipeline.cli compat-export
```

### Сколько данных доступно

TUIK BI на момент написания отдаёт **с января 2013** по текущий
опубликованный месяц. История за более ранние годы (до 2013) **через BI не
доступна** — ни через mashup, ни через прямой API. Если нужны данные за
2002–2012:

- UN Comtrade (`python -m pipeline.cli comtrade --from 2002`) — есть, но
  только до HS-6
- TUIK SDDS (legacy CSV-дампы) — на уровне HS-2/HS-4
- Eurostat Comext (зеркальные данные ЕС о торговле с Турцией) — на CN8

Связаться с этим пайплайном через CLI напрямую нельзя — нужна отдельная
обработка. 

## Схема compat-export

`compat-export` (при `--hs 8`, по умолчанию) производит parquet с **точно
12 колонками** легаси `tr_full.parquet`:

| Колонка | Источник | Пример |
| --- | --- | --- |
| `NAPR` | `IHRITH` маппится russia-centric: `X` (экспорт TR) → `ИМ`, `M` (импорт TR) → `ЭК` | `ИМ` |
| `PERIOD` | `YIL`+`AY` → первое число месяца, `datetime64[ns]` | `2024-01-01` |
| `STRANA` | ISO-2 партнёра, hardcoded (по умолчанию `TR`) | `TR` |
| `TNVED` | `LEFT(ISTPOZ, --hs)`, предварительно zero-padded до 12 | `01064100` |
| `EDIZM` / `EDIZM_ISO` | `OLCU_ADI` через ОКЕИ-карту mgimo (`COUNTRY_UNIT_ALIAS_RECORDS` покрывает все турецкие единицы) | `КИЛОГРАММ` / `166` |
| `STOIM` | `Sum(DOLAR)`, float64 USD | `208208.0` |
| `NETTO` | `Sum(MIKTAR_1)`, float64 кг | `21224.0` |
| `KOL` | `Sum(MIKTAR_2)`, float64 доп. кол-во | `0.0` |
| `TNVED4/6/2` | `TNVED[:N]` через mgimo `finalize_country_output` | `0106` / `010641` / `01` |

Extra-колонки поверх легаси-схемы (нулевая цена — pandas/duckdb просто
игнорируют незнакомые колонки):

* `ISTPOZ` — полный GTIP-12 код (сохраняется и когда `TNVED` обрезан).
* `ISTPOZ_ADI` — турецкое название товара из TUIK.
* `TNVED_EN_NAME` — лучшее английское описание (HS-6 из `datasets/harmonized-system`).
* `TNVED_RU_NAME` — русское ТНВЭД название из mgimo-foreign-trade
  `metadata/tnved.csv` (автодетект; работает когда этот проект лежит рядом
  с mgimo-foreign_trade).

## Программное использование

```python
import asyncio, duckdb
from pipeline.tuik_bi import TuikBI
from pipeline.config import DEFAULT_DIMS_GTIP12, DEFAULT_MEASURES
from pipeline.normalize import cube_to_dataframe

async def fetch():
    async with TuikBI(app_key="general_en") as c:
        cube = await c.query(
            selections={"ULKE_KODU": ["75"], "YIL": [2024], "AY": [3]},
            dims=DEFAULT_DIMS_GTIP12 + ["TASIMA_SEKLI", "GUMRUK_ADI"],
            measures=DEFAULT_MEASURES,
        )
        return cube_to_dataframe(cube, DEFAULT_DIMS_GTIP12 + ["TASIMA_SEKLI", "GUMRUK_ADI"])

df = asyncio.run(fetch())
print(df.head())

# DuckDB по всему хранилищу
con = duckdb.connect("data/trade.duckdb")
con.execute("""
  CREATE OR REPLACE VIEW v AS
  SELECT * FROM read_parquet('data/raw/tuik_bi/**/*.parquet', hive_partitioning=TRUE)
""")
con.execute("SELECT YIL, flow, SUM(usd) FROM v GROUP BY YIL, flow").fetchdf()
```

## Доступные дименсии и меры (полный Qlik-куб)

Захвачено вживую из куба: 97 полей. Самые полезные:

| Группа | Поля |
| --- | --- |
| **Время** | `YIL`, `AY`, `YAYIN_TARIHI` (дата публикации) |
| **Поток** | `IHRITH` ("İhracat"=экспорт / "İthalat"=импорт), `IHRITH_FLAG` |
| **HS** | `FASIL` (HS-2), `TARIFE4` (HS-4), `TARIFE6` (HS-6), `TARIFE8` (CN/HS-8), `ISTPOZ` (**GTIP-12**) — у каждого есть `_ADI` с описанием |
| **Страна** | `ULKE_KODU`, `ULKE_ADI`, `ULKE_COGRAFI_GRUBU` (географическая группа) |
| **Регион** | `IL_KODU`, `IL_ADI` (провинция фирмы в Турции) |
| **Таможня** | `GUMRUK`, `GUMRUK_ADI` (таможенный пост) |
| **Контракт** | `SOZLESME_KODU`, `SOZLESME_ADI` (тип контракта) |
| **Оплата** | `ODEME_SEKLI`, `ODEME_SEKLI_ADI` |
| **Транспорт** | `TASIMA_SEKLI`, `TASIMA_SEKLI_ADI` |
| **Валюта** | `DOVIZ_KODU`, `DOVIZ_ADI` (валюта инвойса) |
| **Классификации** | `BEC`, `BEC_1` (Broad Economic Categories), `SITC4_1` |
| **Меры** | `Sum(DOLAR)` USD, `Sum(EURO)` EUR, `Sum(TL)` TRY, `Sum(MIKTAR_1)` кг, `Sum(MIKTAR_2)` доп., `OLCU_ADI` название единицы |

Россия = `ULKE_KODU = "75"` (TUIK использует собственную кодировку, не
ISO-числовую — Россия здесь НЕ 643).

## Qlik-приложения (вытащены из бандла mashup)

```
general_en  6310efbf-deef-43d9-b397-dfcf355ce1fd   General Trade System / English
general_tr  bd4b4757-a3c9-45ba-b4fb-5c8d7e2d2c42   General Trade System / Turkish
```

Используйте `--lang tr` чтобы переключиться на турецкое приложение (те же
данные, отличаются только лейблы). STS-приложения есть в TUIK-бандле, но
намеренно не подключены — обоснование выбора GTS см. в шапке
`pipeline/config.py`.

## Заметки и подводные камни

- TUIK BI пересобирается раз в несколько месяцев. GUIDы приложений
  захардкожены — если запрос падает с "Qlik app not found", достань их
  заново из бандла mashup:
  `Invoke-WebRequest https://bi.tuik.gov.tr/extensions/tuik-mashup/index.html`,
  выдерни URL `index-*.js`, затем `Select-String -Pattern 'VITE_APP_'` по нему.
- У Qlik engine жёсткий лимит ~10к ячеек за один fetch — клиент
  паджинирует автоматически (циклы `getHyperCubeData`).
- Значения возвращаются в исходных единицах: `DOLAR`/`EURO`/`TL` — это
  полные единицы валют (USD, EUR, TRY), а не центы/курушы.
- Многие HS-12 коды появляются как `"Gizli veri"` (= конфиденциальные
  данные) — TUIK скрывает некоторые категории (обычно подстроки газа/нефти)
  и агрегирует их под кодом `279999`. Это нормально; итоги по-прежнему
  балансируются.
- "Конфиденциальные" коды означают, что для самой гранулярной разбивки
  газа/нефти может понадобиться кросс-источник (OPEC / российская таможня).
- Не запускай несколько параллельных браузерных сессий против mashup —
  TUIK агрессивно ограничивает по rate-limit. Последовательно безопаснее.

## Кросс-проверка с UN Comtrade

UN Comtrade публикует данные, отчитанные Турцией, на уровне HS-6 с задержкой
~2-3 месяца. Быстрая проверка:

```powershell
python -m pipeline.cli comtrade --from 2024 --to 2024
duckdb data/trade.duckdb -c "SELECT * FROM read_parquet('data/comtrade/turkey_russia_2024.parquet') LIMIT 5;"
```

