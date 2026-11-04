---
name: meta-ads-manager
description: |
  Реклама Meta (Instagram, Threads, Facebook) через Marketing API v26 по готовому методу: подключение с нуля
  (бизнес-портфолио, приложение, системный пользователь, токен — промпты для Claude Desktop), запуск кампаний из
  JSON-спеки, WhatsApp, Instagram Direct, трафик на профиль, продвижение готового поста, лид-формы, сайт с пикселем,
  бюджеты, пауза, дубли, превью картинками до запуска, статистика. Все write только через scripts/meta_api.py и meta_launch.py.
  Триггеры: «подключи Meta Ads», «настрой рекламный кабинет», «какие разрешения нужны», «запусти рекламу»,
  «создай кампанию», «измени бюджет», «поставь на паузу», «включи кампанию», «продублируй группу», «реклама в вацап»,
  «трафик на профиль», «продвинь пост/рилс», «лид-форма», «покажи превью», «как идёт реклама», «новый клиент».
---

# meta-ads-manager

Скилл управляет рекламой Meta по API по одному методу для всех кабинетов: никаких автоизменений креатива,
никаких Audience Network и Marketplace, понятные имена, запуск в выключенном виде с проверкой превью.

Все команды — из папки скилла (там, где этот файл):

```bash
PY=.venv/bin/python          # окружение создаёт python3 setup.py
$PY scripts/meta_api.py <команда> …      # точечные действия
$PY scripts/meta_launch.py --spec …      # дерево кампания → группы → креативы → объявления из JSON
$PY scripts/doctor.py                    # проверка подключения (только чтение)
```

Write-запросы к Meta — **только** через `meta_api.py` и `meta_launch.py`. Прямой `curl -X POST` / `requests.post`
к write-эндпоинтам запрещён. Чтение (GET) свободно.

## 0. Первым делом — подключено ли

```bash
python3 setup.py --check
```

- Нет `.venv` → `python3 setup.py`.
- `META_TOKEN пустой` или пользователь просит «подключи», «настрой», «какие разрешения» → **режим подключения**:
  `references/onboarding.md`. Там интервью, порядок промптов для Claude Desktop (`prompts/desktop/`), приём токена,
  `doctor.py`, клиенты.
- Токен есть, но что-то падает с 190/200/10 → `$PY scripts/doctor.py` и таблица в `references/errors.md`.

**Токен в чат не принимать никогда.** Только `python3 setup.py token --from-clipboard` (буфер обмена, на этой машине)
или `python3 setup.py token` (скрытый ввод, пользователь запускает сам). Пользователь вставил токен в чат —
не использовать, попросить отозвать (промпт 09) и выпустить новый (04).

## 1. Клиенты и биндинги

`~/.config/meta-ads-manager/clients.json` — алиасы клиента, `account_id`, `page_id`, `instagram_user_id`,
`instagram_username`, `whatsapp_phone_number`, `pixel_id`, `lead_gen_form_id`, `geo`, `notes` (запреты и пожелания
клиента — соблюдать). Пример — `examples/clients.example.json`.

- Клиента нет в файле → `$PY scripts/doctor.py discover --account=act_… --write`, потом дописать пустые поля вместе
  с пользователем. Кабинета нет у токена → промпт `prompts/desktop/05-client-access.md`.
- Поле пустое — не гадать: прочитать последнюю рабочую кампанию кабинета (`status adset/ad <id>`), `whatsapp-numbers`,
  `validate-ctwa`, `GET /<page>?fields=instagram_business_account`. Найденное — записать в `clients.json`.

## 2. Когда запускать

- В задаче прямо сказано «запусти / включи / залей рекламу» и понятно что (кабинет, креативы, бюджет, дата) —
  запускать одной командой, второй раз не спрашивать.
- «Подготовь», «сделай связку», «посмотрим» — только спека, `--validate-only`, `--preview-only`; без `--activate`.
- Состав запуска оставлен на усмотрение агента («возьми лучшие ролики», «распредели бюджет») — сначала короткий план
  (креативы, гео, бюджеты, статистика, на которую опираешься) и согласие пользователя, потом создание.
- Новые креативы, которых пользователь ещё не видел, — сначала показать, запуск после «ок».
- Что-то неясно или необычно (сайт ещё правится, нет номера WhatsApp, странный бюджет) — один короткий вопрос.

## 3. Правила метода (в коде, для всех кабинетов)

- **Креатив не меняется Meta.** `create_creative` в любом пути ставит OPT_OUT всем автоулучшениям
  (Advantage+ creative, ретушь, генерация фона и текста, музыка, автоанимация, смена CTA) и блокирует OPT_IN;
  мультиобъявления выключены; `is_dynamic_creative=true` заблокирован. Ошибку Meta чинить, сохраняя запрет.
- **Площадки.** По умолчанию `META_DEFAULT_PLATFORMS` (метод: Instagram во всех позициях + Threads — ролик виден
  целиком). Facebook — только если пользователь прямо попросил. Audience Network и Marketplace — никогда:
  `create_adset`/`validate_adset` блокируют их и автоплейсменты. Explore вычищается сам (снят Meta в v26).
- **Аудитория не расширяется.** `advantage_audience=0` и `individual_setting=0` для ручных возраста, пола, гео.
- **Одна группа = одно объявление**, бюджет на группе (ABO), `LOWEST_COST_WITHOUT_CAP`, без `special_ad_categories`.
- **Метки сайта по названиям**: `utm_medium=<META_UTM_MEDIUM>`, `utm_campaign/term/content` = имена кампании,
  группы, объявления. На работающих объявлениях метки ради меток не менять: новый креатив = новая модерация и обучение.
- **Плановое выключение** — отложенной командой `pause` (at/cron/launchd), не через `end_time`.

## 4. Главный pipeline

`задача → scope → спека → запуск в фоне → verify и лист превью → отчёт`

1. **Scope**: клиент из `clients.json`, сценарий, бюджеты (в центах валюты кабинета), имена, креативы, URL.
2. **Спека**: формат — docstring `meta_launch.py`, примеры — `examples/specs/<сценарий>.json`, схемы —
   `references/scenarios.md`. Имена — раздел 7; предупреждения `⚠️ имя:` исправлять.
   `validate_campaign_id` — любая ABO-кампания этого кабинета (необязательно).
3. **Если сомневаешься** (просили «подготовь», непривычный формат): `--validate-only` (Meta проверяет всё, ничего не
   создаёт) и `--preview-only` (лист превью из спеки, медиа грузятся в библиотеку кабинета). Лист смотришь сам.
4. **Запуск** одной строкой в фоне (долгие команды агента обрываются):
   `nohup .venv/bin/python scripts/meta_launch.py --spec spec.json --check-previews --activate > /tmp/<label>.log 2>&1 &`
   Внутри: локальная проверка → медиа → validate_only → создание дерева → проверка автоулучшений → превью → включение.
   Meta отвергла схему — ничего не создано, чинить спеку. State пишется после каждого create; повтор той же спеки
   досоздаёт недостающее. `--no-validate` — только для групп в уже работающую кампанию с теми же биндингами.
   На Limited access большой запуск делить на части по 2–3 группы (`references/app-setup.md`).
5. **Verify**: итог лаунчера + `status campaign/adset/ad <id>` + один взгляд на лист превью (`*-previews.jpg` рядом
   со state). `ACTIVE` в readback не доказывает показ.
6. **Отчёт**: ID, статусы, бюджет, старт, отклонения, путь к state. Лист превью прикладывать, только если нашёл проблему
   или пользователь просил.

Проверка листа превью (~30 секунд): в ленте не обрезаны заголовок, оффер и цена; обложка видео не размыта и не тёмная;
кнопка и текст — из спеки; в Stories и Reels кадр целиком, без полос; в шапке правильные страница и Instagram.
Проблема — не включать, починить (обложка `thumbnail`, файл под ленту, текст), потом писать пользователю с листом.

## 5. Инструменты

`meta_api.py <команда>` (все с `--dry-run`):

| Команда | Что делает |
|---|---|
| `status <campaign\|adset\|ad> <id>` | readback ключевых полей |
| `insights <act\|campaign\|adset id> [--preset=last_7d] [--level=campaign]` | расход, показы, лиды/переписки/визиты, цена результата |
| `budget <adset_id> <cents>` / `budget campaign <id> <cents>` | не больше 3 смен в час на сущность (Meta: 4) |
| `pause` / `activate` / `delete <type> <id>` | activate поднимает выключенных родителей и показывает scope |
| `batch-pause` / `batch-activate <type> <id1,id2,…>` | до 25 ID за раз |
| `create-campaign` / `create-adset` / `create-creative` / `create-ad` | точечное создание, staging-gate и запреты внутри |
| `validate-adset <campaign_id> <json>` / `validate-creative <act> <json>` | validate_only, ничего не создаёт |
| `validate-ctwa <campaign_id> <page_id> <phone>` | связка страница ↔ WhatsApp |
| `whatsapp-numbers [--business=<id>]` | номера WhatsApp по портфолио |
| `ensure-page-backed-instagram <page_id>` | Instagram от имени страницы, когда своего нет |
| `ig-media <instagram_user_id> [n]` | последние посты/рилсы с ID — для `ig_profile_post` |
| `preview-sheet <ad_id,…> [--out=…]` | лист картинок превью созданных объявлений |
| `check-previews <ad_id>` | превью открываются без ошибок |
| `upload-image` / `upload-video <act> <file>` | видео ждёт готовности и отдаёт обложку |
| `replace-creative <ad_id> <act> <image> [--no-crop]` | замена картинки в объявлении |
| `copy-adset-native` / `copy-campaign-native` / `duplicate-adset` / `duplicate-campaign-chunked` | дубли; native `/copies` первым |
| `update <id> <json>` | только whitelist: name, start/end_time, creative и т.п. |
| `log [n]` | лог write-операций |

`meta_launch.py --spec <json> [--preview-only] [--validate-only] [--dry-run] [--check-previews] [--activate] [--state <path>] [--json]`.
Сценарии: `lead_site_static/video`, `lead_form_static/video`, `ctwa_static/video`, `ig_direct_static/video`,
`ig_profile_static/video`, `ig_profile_post`. Остальное (карусели, нестандартные схемы) — `creative_params` в спеке
или тонкий скрипт поверх `MetaAdsAPI` и `meta_launch_builders`, не сырые запросы.

State и листы превью: `~/.local/share/meta-ads-manager/state/<client>/<date>-<scenario>-<label>.json`.

## 6. Сценарии (кратко, подробно — references/scenarios.md)

| Scenario | Campaign | Adset | Creative |
|---|---|---|---|
| `lead_site_static/video` | `OUTCOME_LEADS` | `OFFSITE_CONVERSIONS`, pixel `LEAD`, клик 7 дней | `link_data`/`video_data`, `url_tags` с UTM |
| `lead_form_static/video` | `OUTCOME_LEADS` | `LEAD_GENERATION`, `ON_AD` | ID формы только в CTA |
| `ctwa_static/video` | `OUTCOME_ENGAGEMENT` | `CONVERSATIONS`, `WHATSAPP`, клик 1 день | CTA `WHATSAPP_MESSAGE`, приветствие в `link_data`/`video_data` |
| `ig_direct_static/video` | `OUTCOME_ENGAGEMENT` | `CONVERSATIONS`, `INSTAGRAM_DIRECT` | `instagram_user_id`, CTA `INSTAGRAM_MESSAGE` |
| `ig_profile_static/video` | `OUTCOME_TRAFFIC` | `PROFILE_VISIT`, только Instagram | CTA `VIEW_INSTAGRAM_PROFILE` |
| `ig_profile_post` | `OUTCOME_TRAFFIC` | как `ig_profile_*` | готовый пост/рилс: `source_instagram_media_id` |

Картинки и обложки — `references/crops.md` (главное: `link_data.image_crops` не использовать, ключевое в 9:16 —
в средних 56% высоты, своя обложка видео через `thumbnail`). WhatsApp и Instagram — `references/whatsapp-instagram.md`.

## 7. Именование

| Уровень | Формат | Пример |
|---|---|---|
| Campaign | `[гео] \| цель \| обозначение \| DD.MM` | `[almaty] \| whatsapp \| кофе с собой \| 05.11` |
| Adset | `креатив \| аудитория \| DD.MM` | `Капучино баннер \| 25+ все \| 05.11` |
| Ad | ровно первый сегмент имени adset | `Капучино баннер` |

- Гео: город латиницей (`almaty`, `minsk`) или страна (`kz`); несколько — через запятую; можно `geo` клиента.
- Цель из словаря: `lead site`, `lead form`, `whatsapp`, `direct`, `profile` / `трафик`.
- Дата запуска одна у кампании и всех её групп. Сегмент креатива — до четырёх слов о ролике/оффере + слово визуала.
- Существующие кампании не переименовывать: метки на сайте завязаны на имена.

## 8. Staging-gate и активация

- Новая кампания: campaign `PAUSED`, дети `ACTIVE`; включается только кампания.
- Группа в активной кампании: adset `PAUSED`, ads `ACTIVE`; включается только группа.
- Объявление в активной группе: ad `PAUSED`; включается только объявление.
- `activate adset` включает и выключенную кампанию-родителя — в выключенной кампании группу так не «восстанавливать».

## 9. Ошибки, лимиты, dry-run

- Таблица ошибок Meta — `references/errors.md`. Лимиты и что считается write — `references/safety-rules.md`.
- На ошибке лимита скрипт останавливается без повторов — продолжить через 5 минут той же командой (code 4 — через 15).
- `--dry-run` у любой команды: локальная логика без запросов к Meta. Не заменяет validate-only, лист превью и readback.
