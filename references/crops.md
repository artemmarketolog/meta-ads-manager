# Обрезка, обложки и превью

## Главное правило

`link_data.image_crops` (simple story) **не использовать**. Meta применяет эти crops не только к ленте, но и к Reels
и Stories: объявления с `image_crops` `100x100` + `400x500` показываются в Reels обрезкой 4:5 с серыми полосами вместо
полного 9:16 (проверено 10.09.2026; ключ `90x160` с полным кадром не помогает). Без `image_crops` Reels и Stories
показывают оригинал 9:16, а ленту Meta кропает сама по центру. `meta_api.py` блокирует `link_data.image_crops`
(обход `allow_link_data_crops=true` только для объявлений без вертикальных плейсментов), лаунчер игнорирует `focus`
во всех сценариях, кроме `lead_form_static` в режиме `asset_feed`.

## Статика 9:16

1. Ключевое (заголовок, оффер, цена, кнопка) — в средних 56% высоты кадра (от 22% до 78%): ровно это попадает
   в квадрат 1:1 при центральном кропе ленты; 4:5 захватывает средние 70%. Сверху и снизу всё обрежется.
2. Для Stories: весь текст между ~12% и ~79% высоты (сверху шапка профиля, снизу кнопка).
3. Акцент вне этой зоны — просить перерисовать креатив или делать отдельный файл под ленту.
4. Агент сам смотрит превью по листу картинок: до запуска `meta_launch.py --spec … --preview-only`
   (Meta рисует превью из спеки через `generatepreviews`, ничего не создаётся), после — `--check-previews`
   или `meta_api.py preview-sheet <ad_id,…>`. Лист: строка = объявление, колонки = плейсменты его группы
   (IG лента/stories/reels, FB лента/stories/reels, Threads), кадры снимает headless Chrome (~5 с на кадр).

## Когда кадрирование по плейсментам всё же нужно

Только `asset_feed_spec` с правилами: два ассета с одним hash (`img_story` — оригинал, `img_feed` — с `image_crops`)
и `asset_customization_rules` (лента → `img_feed`, остальное → `img_story`), `optimization_type=PLACEMENT`.
В лаунчере: сценарий `lead_form_static`, `creative_mode: asset_feed`, `"creative": {"focus": "0.4"}`.
**В asset_feed разрешён только один ключ crops** (`400x500`): два ключа дают #100 «Only one crop key».
Кадр 4:5 брать от верха оффера. Для WhatsApp-статики asset_feed не применять: только оригинал без crops
(редактор Ads Manager нестабилен на таких объявлениях).

Сайт с отдельным кадром для ленты — тот же asset_feed через `creative_params`: `link_urls`, `titles`, `bodies`,
`call_to_action_types`, два image-ассета с одним hash, правила IG `stream/profile_feed/ig_search` → `img_feed`,
fallback `{}` → `img_story`, `optimization_type=PLACEMENT`.

`meta_launch_builders.image_crops_for(w, h, focus, keys)` и CLI `meta_api.py image-crops` — только для asset_feed.
Допустимые ключи Meta: `100x100`, `400x500`, `100x72`, `400x150`, `600x360`, `90x160` (`191x100` мёртв с 2019).
`replace-creative` кропает только asset_feed-креативы; для simple story картинка ставится без crops.

## Видео

Crops для видео нет: Reels/Stories показывают 9:16 целиком, лента 4:5 по центру. Субтитры, лица, ключевые кадры —
в средних 60% высоты. Обложку Meta выбирает сама и берёт кадр начала: у роликов с размытым или тёмным стартом
обложка выходит размытой. Для таких роликов задавать свою: `"thumbnail": "/path/cover.jpg"` в media спеки
(чёткий ключевой кадр, например `ffmpeg -ss 2 -i video.mp4 -frames:v 1 cover.jpg`).
