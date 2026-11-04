# WhatsApp (реклама «Написать в WhatsApp») и Instagram через API

## WhatsApp

### Где номер
- `meta_api.py whatsapp-numbers [--business=<id>]`: портфолио → WABA → номера. Печатает номер, готовое
  `whatsapp_phone_number` (только цифры), имя, WABA и портфолио. Номер из приложения WhatsApp Business виден как
  `status=DISCONNECTED`, `platform_type=ON_PREMISE`: для рекламы это норма.
- `Page.whatsapp_number` пустое даже у рабочих страниц — не опираться на него.
- Если бизнес клиента не виден токену (`(#200)` на WABA), номер узнать у клиента и проверить `validate-ctwa`.
- Привязка номера к странице — человеком (промпт `prompts/desktop/06-whatsapp-link.md`): Meta присылает код на номер.

### Проверка связки
`meta_api.py validate-ctwa <campaign_id> <page_id> <phone>`: validate_only группы с `promoted_object`
`{"page_id","whatsapp_phone_number"}`. Успех — `success:true`; непривязанный номер — subcode 1487246
«This WhatsApp phone number is not linked to your account». Нужна любая реальная кампания этого кабинета.
Если в `promoted_object` номер не указан, Meta берёт привязанный к странице.

### Схема
Adset: `OUTCOME_ENGAGEMENT` → `CONVERSATIONS`, `destination_type=WHATSAPP`, `promoted_object.page_id`
(+ `whatsapp_phone_number`, если у страницы несколько номеров), `attribution_spec=[{"event_type":"CLICK_THROUGH","window_days":1}]`.
Creative: CTA `WHATSAPP_MESSAGE`, `value.app_destination=WHATSAPP`, `link=https://api.whatsapp.com/send`,
`page_welcome_message` строкой JSON внутри `link_data`/`video_data` (не в `call_to_action.value`).

Приветствие с готовым первым сообщением клиента:
```json
{"type":"VISUAL_EDITOR","version":2,"landing_screen_type":"welcome_message","media_type":"text",
 "text_format":{"customer_action_type":"autofill_message",
   "message":{"text":"Здравствуйте! Нажмите кнопку, чтобы отправить сообщение",
              "autofill_message":{"content":"Здравствуйте, хочу узнать подробнее"}}}}
```
Приветствие с вопросами-кнопками (ice breakers):
```json
{"type":"VISUAL_EDITOR","version":2,"landing_screen_type":"welcome_message","media_type":"text",
 "text_format":{"customer_action_type":"ice_breakers",
   "message":{"text":"Здравствуйте! Выберите вопрос",
              "ice_breakers":[{"title":"Сколько стоит?"},{"title":"Какие сроки?"},{"title":"Где вы находитесь?"}]}}}
```
Meta не всегда возвращает `whatsapp_phone_number` в readback группы, даже если он был передан.

### Две картинки (лента 4:5 + сторис 9:16) в одном WhatsApp-объявлении
Простой `asset_feed_spec` API принимает, но редактор Ads Manager потом показывает ошибки #1487212, #2016052, #1373054.
Делать сразу в формате Ads Manager: свои ярлыки `pa_<tag>_<story|feed>_<img|body|title|url>` на каждое правило,
`ad_formats=["AUTOMATIC_FORMAT"]`, `descriptions=[{"text":""}]`, CTA `{"type":"WHATSAPP_MESSAGE","value":{"app_destination":"WHATSAPP"}}`
без link, второе правило `{"age_min":13,"age_max":65}`, приветствие в `asset_feed_spec.additional_data.page_welcome_message`,
`is_click_to_message=false`. Через `creative_params` в спеке.

## Instagram

- В креативе поле `object_story_spec.instagram_user_id`; `instagram_actor_id` устарел с v22.
- `GET /<ig_id>?fields=id,username` читает Instagram-аккаунты, назначенные токену. Чужой (subcode 33) — взять
  `instagram_user_id` из рабочего креатива этого клиента, не блокировать запуск.
- Связка страницы с Instagram: `GET /<page>?fields=instagram_business_account{id,username}` (так же делает `doctor.py discover`).
- Своего Instagram у клиента нет — `meta_api.py ensure-page-backed-instagram <page_id>` (Instagram от имени страницы).
  Если реальный Instagram есть, его не подменять.
- Трафик на профиль: сценарий `ig_profile_*`, только Instagram-плейсменты, CTA `VIEW_INSTAGRAM_PROFILE`.
- Продвижение опубликованного поста: `ig_profile_post` (`source_instagram_media_id`).
