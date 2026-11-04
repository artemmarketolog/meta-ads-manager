# Сценарии запуска: схемы и спеки (API v26)

Источник правды по схемам: `scripts/meta_launch_builders.py` (`CANONICAL_SCENARIO_MATRIX`, билдеры, валидаторы).
Здесь то, что нужно держать в голове, и готовые куски спеки для `meta_launch.py`. Полные примеры — `examples/specs/`.

## Общая спека

```json
{
  "client": "coffee-house", "label": "wa-cappuccino", "task": "кратко, что просил пользователь",
  "account_id": "act_<ID кабинета>", "scenario": "ctwa_static",
  "validate_campaign_id": "<любая ABO-кампания этого кабинета, необязательно>",
  "bindings": {"page_id": "<ID страницы>", "instagram_user_id": "<ID Instagram>",
               "whatsapp_phone_number": "77001234567"},
  "media": {"poster1": {"file": "/abs/path/poster1.png"}, "vid1": {"file": "/abs/path/vid1.mp4", "thumbnail": "/abs/path/cover.jpg"}},
  "defaults": {
    "adset": {"daily_budget": 1000, "start_time": "2026-11-05T00:10:00+05:00",
              "targeting": {"age_min": 25, "age_max": 55, "genders": [1, 2],
                            "geo_locations": {"cities": [{"key": "1289662"}],
                                              "location_types": ["home", "recent"]}}},
    "creative": {"message": "Текст объявления", "headline": "Заголовок",
                 "page_welcome_message": {"…": "см. whatsapp-instagram.md"}}
  },
  "campaigns": [{"name": "[almaty] | whatsapp | кофе с собой | 05.11", "adsets": [
    {"name": "Капучино баннер | 25+ все | 05.11", "ads": [{"name": "Капучино баннер", "media": "poster1"}]},
    {"name": "Бариста ролик | 25+ все | 05.11", "ads": [{"name": "Бариста ролик", "media": "vid1"}]}
  ]}]
}
```

- Бюджет — в центах валюты кабинета (`1000` = 10.00). Время — ISO с часовым поясом кабинета.
- Площадки в спеке можно не писать: подставятся `META_DEFAULT_PLATFORMS` (метод — Instagram везде + Threads).
  Явно: `"publisher_platforms": ["instagram"], "instagram_positions": ["stream", "story", "reels", "profile_feed", "ig_search", "profile_reels"]`.
- Город: ключ из `GET /search?type=adgeolocation&q=<город>&location_types=["city"]` (Алматы `1289662`, Астана `1301648`).
  Страна: `"countries": ["KZ"]`.
- `params` на любом уровне: сырые поля Meta поверх собранных (например `"params": {"bid_amount": 100}`).
- `creative_params` у объявления заменяет билдер целиком; внутри `$media:poster1.hash`, `$media:vid1.id`,
  `$media:vid1.thumbnail`.
- Кампания с `"id"` вместо `"name"`: группы добавляются в существующую (создаются `PAUSED`, gate на группе).
- `"cbo": true` у кампании: бюджет групп не передаётся (бюджет кампании задать в `params`).
- `advantage_creative: true` запрещён методом. На каждом финальном креативе — явный `degrees_of_freedom_spec` с `OPT_OUT`
  (ставится автоматически, в том числе для `creative_params` и сырых `params`).
- Медиа, загруженное заранее: `{"hash": "…", "width": 1080, "height": 1920}` или `{"video_id": "…", "thumbnail_url": "…"}`.
- Плейсменты: Instagram `explore`/`explore_home` сняты в v26 (скрипты вычищают сами), `video_feeds` устарел,
  `messenger_home` удалён. Facebook `story` только вместе с feed или IG story; `notification` только с feed.
  Пустые `publisher_platforms` и Facebook без `facebook_positions` блокируются (это автоплейсменты с Audience Network
  и Marketplace).
- Своя обложка видео: `"thumbnail": "/path/cover.jpg"` — без неё Meta берёт кадр начала ролика, часто размытый.
- Показать до запуска: `--validate-only` и `--preview-only` (лист `…-previews.jpg` рядом со state — смотрит агент).

## lead_site_static / lead_site_video — сайт с пикселем

Campaign `OUTCOME_LEADS`. Adset: `OFFSITE_CONVERSIONS`, `IMPRESSIONS`, `LOWEST_COST_WITHOUT_CAP`,
`promoted_object={"pixel_id","custom_event_type":"LEAD"}`, клик 7 дней. Creative: `link_data` (`link`, `message`, `name`,
`image_hash`, CTA `LEARN_MORE`/`SEE_DETAILS`) или `video_data` (`video_id`, обложка, `title`, CTA с `value.link`).
`url_tags` по умолчанию:
`utm_source={{site_source_name}}&utm_medium=<META_UTM_MEDIUM>&utm_campaign={{campaign.name}}&utm_content={{ad.name}}&utm_term={{adset.name}}`
— метки по названиям, без ID: заявки сравниваются по имени объявления и группы. Один креатив в нескольких группах —
одно название объявления, так заявки по креативу складываются.
Спека: `"bindings": {"page_id", "instagram_user_id", "pixel_id"}`, `"creative": {"link": "https://…", "message": "…", "cta_type": "SEE_DETAILS"}`.
Карусель: `creative_params` с `link_data.child_attachments` (минимум 2 карточки со своим hash/link/CTA).

## lead_form_static / lead_form_video — лид-форма

Campaign `OUTCOME_LEADS`. Adset: `LEAD_GENERATION` (или `QUALITY_LEAD`), `destination_type=ON_AD`,
`promoted_object={"page_id"}`; форма в группу не кладётся. Creative: ID формы только в CTA. Static по умолчанию
`asset_feed` с `optimization_type=PLACEMENT`; с `focus` лаунчер делает два ассета (оригинал для Stories/Reels,
кадрированный для ленты) и два правила. Без правил при `PLACEMENT` объявление показывается 0 раз.
`creative_mode: "simple_story"` — обычный `link_data`. Video по умолчанию `simple_story`.
Форма должна быть `ACTIVE`: `GET /<form_id>?fields=status`. Спека: `"bindings": {"page_id", "lead_gen_form_id"}`.
Нет формы — промпт `prompts/desktop/07-lead-form.md`.

## ctwa_static / ctwa_video — WhatsApp

Campaign `OUTCOME_ENGAGEMENT`. Adset: `CONVERSATIONS`, `destination_type=WHATSAPP`,
`promoted_object={"page_id","whatsapp_phone_number"}` (номер без «+»; можно опустить, если у страницы один номер),
клик 1 день (7 дней Meta отвергает, 1885423). Creative: `link_data.link=https://api.whatsapp.com/send`,
CTA `WHATSAPP_MESSAGE` с `app_destination=WHATSAPP`, `page_welcome_message` внутри `link_data`/`video_data`
(форматы — `whatsapp-instagram.md`). `url_tags` не нужен.
Перед первым запуском на новую страницу/номер: `meta_api.py validate-ctwa` или `--validate-only`.

## ig_direct_static / ig_direct_video — Instagram Direct

Campaign `OUTCOME_ENGAGEMENT`. Adset: `CONVERSATIONS`, `destination_type=INSTAGRAM_DIRECT`, `promoted_object={"page_id"}`.
Creative: `object_story_spec.instagram_user_id` обязателен, CTA `INSTAGRAM_MESSAGE`, `link=https://www.instagram.com/`.
Схема из билдеров; при первом запуске проверить readback и превью особенно внимательно.

## ig_profile_static / ig_profile_video — трафик на профиль Instagram

Campaign `OUTCOME_TRAFFIC`. Adset: `PROFILE_VISIT`, `destination_type=INSTAGRAM_PROFILE`, `promoted_object={"page_id"}`,
только Instagram (`stream`, `story`, `reels`, `profile_feed`), клик 1 день. Creative: `instagram_user_id` + медиа с CTA
`VIEW_INSTAGRAM_PROFILE` (`link=http://instagram.com/<user>`, `app_link=instagram://user?username=<user>`).
Спека: `"bindings": {"page_id", "instagram_user_id", "instagram_username"}`.

## ig_profile_post — продвижение готового поста или рилса на профиль

То же, что кнопка «Продвигать» в Instagram/Business Suite, но по правилам метода: `OUTCOME_TRAFFIC`, бюджет на группе,
без даты окончания, автоулучшения и мультиобъявления выключены. Adset как у `ig_profile_*`. Creative без
`object_story_spec`: `object_id` = страница, `instagram_user_id`, `source_instagram_media_id`, CTA `VIEW_INSTAGRAM_PROFILE`;
текст, медиа, лайки и комментарии — от исходного поста. ID постов: `meta_api.py ig-media <instagram_user_id>`.
Спека: `"media": {"post1": {"ig_media_id": "<ID поста>"}}`, у объявления `"media": "post1"`, `creative.message` не нужен.
Если пост архивировать или удалить, объявление встанет («Органические медиафайлы помещены в архив»).

Чем «Продвигать» из приложения хуже: устаревшая цель `LINK_CLICKS`, бюджет на кампании, дата окончания (после неё стоп),
включённые мультиобъявления и автоулучшения.

## Дубли

Сначала `copy-campaign-native` / `copy-adset-native` (native `/copies`, deep_copy до 3 объявлений синхронно).
Падает на старых креативах — `duplicate-campaign-chunked` / `duplicate-adset`. При частичном сбое чинить только
упавшую группу в уже созданной кампании. Таргетинг существующей группы через `update` не меняется: гео правится
пересозданием группы и `create-ad` на том же creative_id.
