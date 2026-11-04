#!/usr/bin/env python3
"""Pure builders and validators for Meta launch workflow v2.

This module does not call Meta. It only builds request dictionaries and validates
scenario-specific schema so one-off launch scripts do not re-invent creative logic.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

from config import DEFAULT_PLATFORMS, UTM_MEDIUM


WHATSAPP_LINK = "https://api.whatsapp.com/send"
# Метки по названиям, без ID: заявки сравниваются по имени объявления и группы.
# utm_medium — META_UTM_MEDIUM из meta.env (название вашего агентства/канала).
DEFAULT_URL_TAGS = (
    "utm_source={{site_source_name}}&utm_medium=" + UTM_MEDIUM + "&"
    "utm_campaign={{campaign.name}}&utm_content={{ad.name}}&utm_term={{adset.name}}"
)

# Окна атрибуции, которые реально стоят в рабочих адсетах (скан кабинетов 10.09.2026):
# сайт с пикселем — 7 дней клик, WhatsApp/профиль — 1 день клик. Для новых
# WhatsApp-адсетов Meta с 09.2026 требует явное окно по клику (subcode 1885423).
CLICK_ATTRIBUTION_1D = [{"event_type": "CLICK_THROUGH", "window_days": 1}]
CLICK_ATTRIBUTION_7D = [{"event_type": "CLICK_THROUGH", "window_days": 7}]

# Advantage+ creative выключен полностью (правило метода): никаких автоправок
# картинок, текста и CTA. Обобщённый standard_enhancements Meta больше не принимает
# (subcode 3858504), поэтому перечисляем отдельные фичи.
ADVANTAGE_CREATIVE_FEATURES = (
    "adapt_to_placement", "add_text_overlay", "advantage_plus_creative", "audio",
    "enhance_cta", "image_auto_crop", "image_enhancement", "image_touchups",
    "inline_comment", "media_type_automation", "text_generation", "text_optimizations",
    "video_auto_crop",
)


def advantage_creative_opt_out() -> dict[str, Any]:
    return {
        "creative_features_spec": {
            feature: {"enroll_status": "OPT_OUT"} for feature in ADVANTAGE_CREATIVE_FEATURES
        }
    }


# ── Плейсменты ──────────────────────────────────────────────────────────
# Instagram Explore снят в v26.0 (29.07.2026): показ туда остановлен для всех версий,
# запрос с explore на v26 падает (2490589), на остальных версиях с 27.10.2026.
REMOVED_INSTAGRAM_POSITIONS = ("explore", "explore_home")


def strip_removed_placements(targeting: dict[str, Any]) -> list[str]:
    """Убрать из targeting снятые Meta плейсменты (на месте). Возвращает убранные."""
    positions = targeting.get("instagram_positions")
    if not isinstance(positions, list):
        return []
    removed = [p for p in positions if p in REMOVED_INSTAGRAM_POSITIONS]
    if removed:
        targeting["instagram_positions"] = [p for p in positions if p not in REMOVED_INSTAGRAM_POSITIONS]
    return removed


# Площадки по умолчанию (метод): Instagram везде + Threads, без Facebook — ролик показывается
# целиком. Меняется META_DEFAULT_PLATFORMS; Audience Network и Marketplace никогда.
DEFAULT_INSTAGRAM_POSITIONS = ["stream", "story", "reels", "profile_feed", "ig_search", "profile_reels"]
DEFAULT_FACEBOOK_POSITIONS = ["feed", "story", "facebook_reels", "profile_feed", "search"]


def apply_default_placements(targeting: dict[str, Any], threads: bool = True) -> bool:
    """Нет publisher_platforms (автоплейсменты) → META_DEFAULT_PLATFORMS (на месте).
    threads=False для трафика на профиль Instagram: туда ведёт только Instagram."""
    if targeting.get("publisher_platforms"):
        return False
    platforms = [p for p in DEFAULT_PLATFORMS if p in ("instagram", "threads", "facebook")] or ["instagram"]
    if not threads:
        platforms = ["instagram"]
    targeting["publisher_platforms"] = platforms
    targeting["instagram_positions"] = list(DEFAULT_INSTAGRAM_POSITIONS)
    if "threads" in platforms:
        targeting["threads_positions"] = ["threads_stream"]
    if "facebook" in platforms:
        targeting["facebook_positions"] = list(DEFAULT_FACEBOOK_POSITIONS)
    return True


def preview_formats_for(targeting: dict[str, Any] | str | None) -> list[str]:
    """ad_format превью (generatepreviews / ad previews) под плейсменты адсета."""
    targeting = _decode_maybe_json(targeting)
    targeting = targeting if isinstance(targeting, dict) else {}
    platforms = targeting.get("publisher_platforms") or ["facebook", "instagram"]
    ig = targeting.get("instagram_positions") or ["stream", "story", "reels"]
    fb = targeting.get("facebook_positions") or ["feed"]
    formats: list[str] = []
    if "instagram" in platforms:
        formats += [fmt for pos, fmt in (("stream", "INSTAGRAM_STANDARD"), ("story", "INSTAGRAM_STORY"),
                                         ("reels", "INSTAGRAM_REELS")) if pos in ig]
    if "facebook" in platforms:
        formats += [fmt for pos, fmt in (("feed", "MOBILE_FEED_STANDARD"), ("story", "FACEBOOK_STORY_MOBILE"),
                                         ("facebook_reels", "FACEBOOK_REELS_MOBILE")) if pos in fb]
    if "threads" in platforms:
        formats.append("THREADS_STREAM")
    return formats or ["INSTAGRAM_STANDARD"]


def placement_violations(targeting: dict[str, Any] | str | None) -> list[str]:
    """Запрещённые площадки (правило метода): Audience Network и Marketplace никогда.
    Пустые publisher_platforms или Facebook без facebook_positions = автоплейсменты, они их возвращают."""
    targeting = _decode_maybe_json(targeting)
    if not isinstance(targeting, dict):
        return []
    issues: list[str] = []
    platforms = targeting.get("publisher_platforms")
    if not platforms:
        issues.append("нет publisher_platforms: это автоплейсменты, Meta добавит Audience Network и Marketplace")
    else:
        if "audience_network" in platforms:
            issues.append("audience_network в publisher_platforms запрещён")
        if "facebook" in platforms:
            fb_positions = targeting.get("facebook_positions")
            if not fb_positions:
                issues.append("facebook без facebook_positions: Meta добавит Marketplace")
            elif "marketplace" in fb_positions:
                issues.append("marketplace в facebook_positions запрещён")
    if targeting.get("audience_network_positions"):
        issues.append("audience_network_positions задан, Audience Network запрещён")
    return issues


# ── Обрезка картинок под плейсменты ─────────────────────────────────────
# Ключ image_crops — целевое соотношение сторон width:height. Координаты в пикселях
# исходника: [[x1, y1], [x2, y2]]. Один hash + image_crops в link_data даёт разные
# кадрирования для Feed (1:1 и 4:5), Stories/Reels показывают 9:16 целиком
# (Meta: «Stories do not support image crops»). Допустимые ключи Meta:
# 100x100, 400x500, 100x72, 400x150, 600x360, 90x160; 191x100 снят ещё в 2019.
CROP_KEYS_FEED = ("100x100", "400x500")
CROP_KEY_STORY = "90x160"
CROP_FOCUS_PRESETS = {"top": 0.0, "center": 0.5, "bottom": 1.0}


def crop_box(width: int, height: int, key: str, focus: str | float = "center") -> list[list[int]]:
    """Максимальный прямоугольник с соотношением key внутри картинки width×height.

    focus задаёт, какую часть исходника оставить, когда что-то приходится резать:
    "top" / "center" / "bottom" или число 0..1 — где по вертикали (для вертикальных
    исходников) или по горизонтали (для широких) находится центр акцентной зоны.
    """
    ratio_w, ratio_h = (int(part) for part in key.lower().split("x"))
    if width <= 0 or height <= 0:
        raise ValueError("image size must be positive")
    if isinstance(focus, str):
        if focus not in CROP_FOCUS_PRESETS:
            raise ValueError(f"unknown crop focus: {focus}")
        anchor = CROP_FOCUS_PRESETS[focus]
        preset = True
    else:
        anchor = min(max(float(focus), 0.0), 1.0)
        preset = False

    if width * ratio_h > height * ratio_w:
        # Исходник шире цели: режем по ширине, высота целиком.
        crop_w = height * ratio_w // ratio_h
        crop_h = height
        slack = width - crop_w
        x1 = int(round(anchor * slack)) if preset else int(round(anchor * width - crop_w / 2))
        x1 = min(max(x1, 0), slack)
        y1 = 0
    else:
        # Исходник выше цели (типично 9:16 → 1:1): режем по высоте, ширина целиком.
        crop_w = width
        crop_h = width * ratio_h // ratio_w
        slack = height - crop_h
        y1 = int(round(anchor * slack)) if preset else int(round(anchor * height - crop_h / 2))
        y1 = min(max(y1, 0), slack)
        x1 = 0
    return [[x1, y1], [x1 + crop_w, y1 + crop_h]]


def image_crops_for(
    width: int,
    height: int,
    focus: str | float = "center",
    keys: tuple[str, ...] | list[str] = CROP_KEYS_FEED,
) -> dict[str, list[list[int]]]:
    """image_crops для link_data / asset_feed_spec.images[]: {key: [[x1,y1],[x2,y2]]}."""
    return {key: crop_box(width, height, key, focus) for key in keys}


def image_size(path: str) -> tuple[int, int]:
    from PIL import Image  # локальный импорт: билдерам PIL нужен только здесь

    with Image.open(path) as img:
        return img.size


def placement_image_assets(
    image_hash: str,
    crops: dict[str, list[list[int]]],
    *,
    story_label: str = "img_story",
    feed_label: str = "img_feed",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Пара (images, asset_customization_rules) для asset_feed_spec: оригинал в
    Stories/Reels, кадрированный вариант того же hash в Feed-плейсментах."""
    images = [
        {"adlabels": [{"name": story_label}], "hash": image_hash},
        {"adlabels": [{"name": feed_label}], "hash": image_hash, "image_crops": crops},
    ]
    feed_spec = {
        "publisher_platforms": ["facebook", "instagram"],
        "facebook_positions": ["feed", "profile_feed", "instream_video",
                               "search", "right_hand_column", "notification",
                               "facebook_reels_overlay"],
        "instagram_positions": ["stream", "profile_feed", "ig_search"],
    }
    rules = [
        {"customization_spec": feed_spec, "image_label": {"name": feed_label}, "priority": 1},
        {"customization_spec": {}, "image_label": {"name": story_label}, "priority": 2},
    ]
    return images, rules


CANONICAL_SCENARIO_MATRIX = {
    "lead_site_static": {
        "objective": "OUTCOME_LEADS",
        "optimization_goal": "OFFSITE_CONVERSIONS",
        "destination_type": None,
        "creative_schema": "object_story_spec.link_data",
        "media_type": "static",
        "destination": "lead_site",
        "default_creative_mode": "simple_story",
        "required_bindings": ["page_id", "pixel_id"],
        "forbidden_creative_fields": ["asset_feed_spec"],
    },
    "lead_site_video": {
        "objective": "OUTCOME_LEADS",
        "optimization_goal": "OFFSITE_CONVERSIONS",
        "destination_type": None,
        "creative_schema": "object_story_spec.video_data",
        "media_type": "video",
        "destination": "lead_site",
        "default_creative_mode": "simple_story",
        "required_bindings": ["page_id", "pixel_id"],
        "forbidden_creative_fields": ["asset_feed_spec"],
    },
    "ig_direct_video": {
        "objective": "OUTCOME_ENGAGEMENT",
        "optimization_goal": "CONVERSATIONS",
        "destination_type": "INSTAGRAM_DIRECT",
        "creative_schema": "object_story_spec.video_data",
        "media_type": "video",
        "destination": "ig_direct",
        "default_creative_mode": "simple_story",
        "required_bindings": ["page_id", "instagram_user_id"],
        "forbidden_creative_fields": ["asset_feed_spec"],
    },
    "ig_direct_static": {
        "objective": "OUTCOME_ENGAGEMENT",
        "optimization_goal": "CONVERSATIONS",
        "destination_type": "INSTAGRAM_DIRECT",
        "creative_schema": "object_story_spec.link_data",
        "media_type": "static",
        "destination": "ig_direct",
        "default_creative_mode": "simple_story",
        "required_bindings": ["page_id", "instagram_user_id"],
        "forbidden_creative_fields": ["asset_feed_spec"],
    },
    "ctwa_static": {
        "objective": "OUTCOME_ENGAGEMENT",
        "optimization_goal": "CONVERSATIONS",
        "destination_type": "WHATSAPP",
        "creative_schema": "object_story_spec.link_data",
        "media_type": "static",
        "destination": "ctwa",
        "default_creative_mode": "simple_story",
        "required_bindings": ["page_id"],
        "forbidden_creative_fields": ["asset_feed_spec"],
    },
    "ctwa_video": {
        "objective": "OUTCOME_ENGAGEMENT",
        "optimization_goal": "CONVERSATIONS",
        "destination_type": "WHATSAPP",
        "creative_schema": "object_story_spec.video_data_or_asset_feed",
        "media_type": "video",
        "destination": "ctwa",
        "default_creative_mode": "simple_story",
        "required_bindings": ["page_id"],
        "forbidden_creative_fields": [],
    },
    # Трафик на профиль Instagram (схема рабочей кампании 30.08.2026): цель TRAFFIC, оптимизация
    # на визиты профиля, CTA VIEW_INSTAGRAM_PROFILE с app_link/link на профиль.
    "ig_profile_video": {
        "objective": "OUTCOME_TRAFFIC",
        "optimization_goal": "PROFILE_VISIT",
        "destination_type": "INSTAGRAM_PROFILE",
        "creative_schema": "object_story_spec.video_data",
        "media_type": "video",
        "destination": "ig_profile",
        "default_creative_mode": "simple_story",
        "required_bindings": ["page_id", "instagram_user_id", "instagram_username"],
        "forbidden_creative_fields": ["asset_feed_spec"],
    },
    "ig_profile_static": {
        "objective": "OUTCOME_TRAFFIC",
        "optimization_goal": "PROFILE_VISIT",
        "destination_type": "INSTAGRAM_PROFILE",
        "creative_schema": "object_story_spec.link_data",
        "media_type": "static",
        "destination": "ig_profile",
        "default_creative_mode": "simple_story",
        "required_bindings": ["page_id", "instagram_user_id", "instagram_username"],
        "forbidden_creative_fields": ["asset_feed_spec"],
    },
    # Продвижение уже опубликованного поста/рилса на визиты профиля — то, что делает
    # Business Suite («Продвигать»), но с нашими правилами: OUTCOME_TRAFFIC вместо
    # устаревшего LINK_CLICKS, без автоулучшений, без даты окончания. Лайки и
    # комментарии копятся на исходном посте. validate_only прошёл 04.10.2026.
    "ig_profile_post": {
        "objective": "OUTCOME_TRAFFIC",
        "optimization_goal": "PROFILE_VISIT",
        "destination_type": "INSTAGRAM_PROFILE",
        "creative_schema": "source_instagram_media_id",
        "media_type": "ig_post",
        "destination": "ig_profile",
        "default_creative_mode": "existing_post",
        "required_bindings": ["page_id", "instagram_user_id", "instagram_username"],
        "forbidden_creative_fields": ["asset_feed_spec", "object_story_spec"],
    },
    "lead_form_static": {
        "objective": "OUTCOME_LEADS",
        "optimization_goal": "LEAD_GENERATION",
        "destination_type": "ON_AD",
        "creative_schema": "lead_form_static",
        "media_type": "static",
        "destination": "lead_form",
        "default_creative_mode": "asset_feed",
        "required_bindings": ["page_id", "lead_gen_form_id"],
        "forbidden_creative_fields": [],
    },
    "lead_form_video": {
        "objective": "OUTCOME_LEADS",
        "optimization_goal": "LEAD_GENERATION",
        "destination_type": "ON_AD",
        "creative_schema": "lead_form_video",
        "media_type": "video",
        "destination": "lead_form",
        "default_creative_mode": "asset_feed",
        "required_bindings": ["page_id", "lead_gen_form_id"],
        "forbidden_creative_fields": [],
    },
    "ctwa_native_copy": {
        "objective": "OUTCOME_ENGAGEMENT",
        "optimization_goal": "CONVERSATIONS",
        "destination_type": "WHATSAPP",
        "creative_schema": "native_copy_or_template_asset_feed_spec",
        "media_type": None,
        "destination": "ctwa",
        "default_creative_mode": "native_copy",
        "required_bindings": ["source_adset_id", "source_ad_id"],
        "forbidden_creative_fields": [],
    },
    "post_launch_patch": {
        "objective": None,
        "optimization_goal": None,
        "destination_type": None,
        "creative_schema": "replace_or_update_existing",
        "media_type": None,
        "destination": None,
        "default_creative_mode": None,
        "required_bindings": ["target_ids"],
        "forbidden_creative_fields": [],
    },
    "legacy_state_verify": {
        "objective": None,
        "optimization_goal": None,
        "destination_type": None,
        "creative_schema": "legacy_read_only_verify",
        "media_type": None,
        "destination": None,
        "default_creative_mode": None,
        "required_bindings": [],
        "forbidden_creative_fields": [],
    },
}

SCENARIO_ALIASES = {
    "ctwa_static_link_data": "ctwa_static",
    "whatsapp_static": "ctwa_static",
    "whatsapp_video": "ctwa_video",
    "direct_static": "ig_direct_static",
    "direct_video": "ig_direct_video",
}

SCENARIO_MATRIX = copy.deepcopy(CANONICAL_SCENARIO_MATRIX)
for alias, canonical in SCENARIO_ALIASES.items():
    SCENARIO_MATRIX[alias] = {**copy.deepcopy(CANONICAL_SCENARIO_MATRIX[canonical]), "alias_of": canonical}


def canonical_scenario(scenario: str | None) -> str:
    return SCENARIO_ALIASES.get(str(scenario or ""), str(scenario or ""))


def get_scenario_config(scenario: str | None) -> dict[str, Any] | None:
    return CANONICAL_SCENARIO_MATRIX.get(canonical_scenario(scenario))


def is_known_scenario(scenario: str | None) -> bool:
    return canonical_scenario(scenario) in CANONICAL_SCENARIO_MATRIX


def _decode_maybe_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def deep_copy_jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value or {}, ensure_ascii=False))


def normalize_targeting_for_create(targeting: dict[str, Any] | str | None) -> dict[str, Any]:
    """Normalize Meta targeting before create.

    The two important launch defaults are:
    - Instagram explore/explore_home are removed (Meta retired Explore in v26);
    - if age/gender/geo are manually selected, disable Advantage expansion for them.
    """
    targeting = _decode_maybe_json(targeting)
    if not isinstance(targeting, dict):
        return {}
    normalized = deep_copy_jsonable(targeting)

    strip_removed_placements(normalized)

    manual = set()
    if "age_min" in normalized or "age_max" in normalized:
        manual.add("age")
    if normalized.get("genders") is not None:
        manual.add("gender")
    geo_locations = normalized.get("geo_locations") or {}
    if isinstance(geo_locations, dict) and any(
        geo_locations.get(k) for k in ("cities", "custom_locations", "regions", "countries", "zips")
    ):
        manual.add("geo")

    # С v23 Meta требует явный advantage_audience (0/1) при создании адсета, иначе
    # либо ошибка, либо молчаливое расширение аудитории. Метод расширение не допускает.
    automation = normalized.setdefault("targeting_automation", {})
    automation.setdefault("advantage_audience", 0)
    if manual:
        automation["advantage_audience"] = 0
        individual = automation.setdefault("individual_setting", {})
        for dimension in sorted(manual):
            individual[dimension] = 0

    return normalized


def validate_targeting(targeting: dict[str, Any] | str | None) -> list[str]:
    targeting = _decode_maybe_json(targeting)
    if not isinstance(targeting, dict):
        return ["targeting must be a JSON object"]

    issues: list[str] = []
    ig_positions = targeting.get("instagram_positions")
    if isinstance(ig_positions, list) and any(p in REMOVED_INSTAGRAM_POSITIONS for p in ig_positions):
        issues.append("instagram_positions содержит explore/explore_home: Meta сняла Explore в v26 (29.07.2026)")
    issues.extend(placement_violations(targeting))

    manual = set()
    if "age_min" in targeting or "age_max" in targeting:
        manual.add("age")
    if targeting.get("genders") is not None:
        manual.add("gender")
    geo_locations = targeting.get("geo_locations") or {}
    if isinstance(geo_locations, dict) and any(
        geo_locations.get(k) for k in ("cities", "custom_locations", "regions", "countries", "zips")
    ):
        manual.add("geo")

    automation = targeting.get("targeting_automation") or {}
    individual = automation.get("individual_setting") or {}
    for dimension in sorted(manual):
        if individual.get(dimension) != 0:
            issues.append(f"manual {dimension} targeting must set targeting_automation.individual_setting.{dimension}=0")
    if manual and automation.get("advantage_audience") != 0:
        issues.append("manual targeting must set targeting_automation.advantage_audience=0")

    return issues


def campaign_params(name: str, objective: str, buying_type: str = "AUCTION") -> dict[str, Any]:
    return {"name": name, "objective": objective, "buying_type": buying_type}


def lead_site_adset_params(
    *,
    name: str,
    daily_budget: int | str,
    pixel_id: str,
    targeting: dict[str, Any],
    account_id: str | None = None,
    attribution_spec: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    params = {
        "name": name,
        "daily_budget": str(daily_budget),
        "optimization_goal": "OFFSITE_CONVERSIONS",
        "billing_event": "IMPRESSIONS",
        "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
        "promoted_object": {"pixel_id": str(pixel_id), "custom_event_type": "LEAD", "smart_pse_enabled": False},
        "targeting": normalize_targeting_for_create(targeting),
    }
    if account_id:
        params["account_id"] = account_id
    if attribution_spec:
        params["attribution_spec"] = attribution_spec
    return params


def ctwa_adset_params(
    *,
    name: str,
    daily_budget: int | str,
    page_id: str,
    targeting: dict[str, Any],
    whatsapp_phone_number: str | None = None,
    account_id: str | None = None,
    attribution_spec: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """CTWA adset. whatsapp_phone_number — номер без «+» (77001234567); если у
    страницы один привязанный номер, поле можно не передавать, Meta возьмёт его."""
    promoted_object = {"page_id": str(page_id), "smart_pse_enabled": False}
    if whatsapp_phone_number:
        promoted_object["whatsapp_phone_number"] = str(whatsapp_phone_number).lstrip("+")
    params = {
        "name": name,
        "daily_budget": str(daily_budget),
        "optimization_goal": "CONVERSATIONS",
        "billing_event": "IMPRESSIONS",
        "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
        "destination_type": "WHATSAPP",
        "promoted_object": promoted_object,
        "targeting": normalize_targeting_for_create(targeting),
        "attribution_spec": attribution_spec or CLICK_ATTRIBUTION_1D,
    }
    if account_id:
        params["account_id"] = account_id
    return params


def ig_profile_adset_params(
    *,
    name: str,
    daily_budget: int | str,
    page_id: str,
    targeting: dict[str, Any],
    account_id: str | None = None,
    attribution_spec: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Трафик на профиль Instagram: OUTCOME_TRAFFIC → PROFILE_VISIT → INSTAGRAM_PROFILE.
    Плейсменты только Instagram (stream, story, reels, profile_feed)."""
    params = {
        "name": name,
        "daily_budget": str(daily_budget),
        "optimization_goal": "PROFILE_VISIT",
        "billing_event": "IMPRESSIONS",
        "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
        "destination_type": "INSTAGRAM_PROFILE",
        "promoted_object": {"page_id": str(page_id), "smart_pse_enabled": False},
        "targeting": normalize_targeting_for_create(targeting),
        "attribution_spec": attribution_spec or CLICK_ATTRIBUTION_1D,
    }
    if account_id:
        params["account_id"] = account_id
    return params


def lead_form_adset_params(
    *,
    name: str,
    daily_budget: int | str,
    page_id: str,
    targeting: dict[str, Any],
    account_id: str | None = None,
    bid_strategy: str | None = None,
) -> dict[str, Any]:
    params = {
        "name": name,
        "daily_budget": str(daily_budget),
        "optimization_goal": "LEAD_GENERATION",
        "billing_event": "IMPRESSIONS",
        "destination_type": "ON_AD",
        "promoted_object": {"page_id": str(page_id), "smart_pse_enabled": False},
        "targeting": normalize_targeting_for_create(targeting),
    }
    if account_id:
        params["account_id"] = account_id
    if bid_strategy:
        params["bid_strategy"] = bid_strategy
    return params


def ig_direct_adset_params(
    *,
    name: str,
    daily_budget: int | str,
    page_id: str,
    targeting: dict[str, Any],
    account_id: str | None = None,
) -> dict[str, Any]:
    params = {
        "name": name,
        "daily_budget": str(daily_budget),
        "optimization_goal": "CONVERSATIONS",
        "billing_event": "IMPRESSIONS",
        "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
        "destination_type": "INSTAGRAM_DIRECT",
        "promoted_object": {"page_id": str(page_id), "smart_pse_enabled": False},
        "targeting": normalize_targeting_for_create(targeting),
    }
    if account_id:
        params["account_id"] = account_id
    return params


def lead_site_static_creative(
    *,
    name: str,
    page_id: str,
    link: str,
    message: str,
    image_hash: str,
    instagram_user_id: str | None = None,
    cta_type: str = "LEARN_MORE",
    url_tags: str = DEFAULT_URL_TAGS,
) -> dict[str, Any]:
    oss: dict[str, Any] = {
        "page_id": str(page_id),
        "link_data": {
            "link": link,
            "message": message,
            "image_hash": image_hash,
            "call_to_action": {"type": cta_type},
        },
    }
    if instagram_user_id:
        oss["instagram_user_id"] = str(instagram_user_id)
    return {"name": name, "url_tags": url_tags, "object_story_spec": oss}


def lead_site_carousel_creative(
    *,
    name: str,
    page_id: str,
    link: str,
    message: str,
    cards: list[dict[str, Any]],
    instagram_user_id: str | None = None,
    cta_type: str = "LEARN_MORE",
    multi_share_end_card: bool = False,
    multi_share_optimized: bool | None = None,
    url_tags: str = DEFAULT_URL_TAGS,
) -> dict[str, Any]:
    """Build a non-dynamic site carousel from ordered source cards.

    A working carousel can legitimately omit the parent ``image_hash``: its media
    lives in ``link_data.child_attachments``.  Card text and order are preserved,
    while destination and CTA are made explicit for every attachment.
    """
    if len(cards) < 2:
        raise ValueError("lead_site_carousel_creative requires at least two cards")

    child_attachments: list[dict[str, Any]] = []
    for index, source_card in enumerate(cards, start=1):
        image_hash = str(source_card.get("image_hash") or "").strip()
        if not image_hash:
            raise ValueError(f"lead_site_carousel_creative card {index} requires image_hash")
        card_link = str(source_card.get("link") or link)
        card: dict[str, Any] = {
            "link": card_link,
            "image_hash": image_hash,
            "call_to_action": {"type": cta_type, "value": {"link": card_link}},
        }
        for field in ("name", "description"):
            if field in source_card:
                card[field] = source_card[field]
        child_attachments.append(card)

    link_data: dict[str, Any] = {
        "link": link,
        "message": message,
        "child_attachments": child_attachments,
        "multi_share_end_card": bool(multi_share_end_card),
        "call_to_action": {"type": cta_type, "value": {"link": link}},
    }
    if multi_share_optimized is not None:
        link_data["multi_share_optimized"] = bool(multi_share_optimized)

    oss: dict[str, Any] = {"page_id": str(page_id), "link_data": link_data}
    if instagram_user_id:
        oss["instagram_user_id"] = str(instagram_user_id)
    return {"name": name, "url_tags": url_tags, "object_story_spec": oss}


def lead_site_video_creative(
    *,
    name: str,
    page_id: str,
    link: str,
    message: str,
    video_id: str,
    image_url: str | None = None,
    image_hash: str | None = None,
    title: str | None = None,
    instagram_user_id: str | None = None,
    cta_type: str = "LEARN_MORE",
    url_tags: str = DEFAULT_URL_TAGS,
) -> dict[str, Any]:
    video_data: dict[str, Any] = {
        "video_id": str(video_id),
        "message": message,
        "call_to_action": {"type": cta_type, "value": {"link": link}},
    }
    if title is not None:
        video_data["title"] = title
    if image_url:
        video_data["image_url"] = image_url
    if image_hash:
        video_data["image_hash"] = image_hash

    oss: dict[str, Any] = {"page_id": str(page_id), "video_data": video_data}
    if instagram_user_id:
        oss["instagram_user_id"] = str(instagram_user_id)
    return {"name": name, "url_tags": url_tags, "object_story_spec": oss}


def ig_direct_static_creative(
    *,
    name: str,
    page_id: str,
    message: str,
    image_hash: str,
    instagram_user_id: str,
    link: str = "https://www.instagram.com/",
    headline: str | None = None,
) -> dict[str, Any]:
    link_data: dict[str, Any] = {
        "link": link,
        "message": message,
        "image_hash": image_hash,
        "call_to_action": {"type": "INSTAGRAM_MESSAGE", "value": {"link": link}},
    }
    if headline is not None:
        link_data["name"] = headline
    return {
        "name": name,
        "object_story_spec": {
            "page_id": str(page_id),
            "instagram_user_id": str(instagram_user_id),
            "link_data": link_data,
        },
    }


def ig_direct_video_creative(
    *,
    name: str,
    page_id: str,
    message: str,
    video_id: str,
    instagram_user_id: str,
    image_url: str | None = None,
    image_hash: str | None = None,
    link: str = "https://www.instagram.com/",
    title: str | None = None,
) -> dict[str, Any]:
    video_data: dict[str, Any] = {
        "video_id": str(video_id),
        "message": message,
        "call_to_action": {"type": "INSTAGRAM_MESSAGE", "value": {"link": link}},
    }
    if title is not None:
        video_data["title"] = title
    if image_url:
        video_data["image_url"] = image_url
    if image_hash:
        video_data["image_hash"] = image_hash
    return {
        "name": name,
        "object_story_spec": {
            "page_id": str(page_id),
            "instagram_user_id": str(instagram_user_id),
            "video_data": video_data,
        },
    }


def ctwa_static_link_data_creative(
    *,
    name: str,
    page_id: str,
    message: str,
    image_hash: str,
    headline: str = "",
    instagram_user_id: str | None = None,
    page_welcome_message: dict[str, Any] | str | None = None,
) -> dict[str, Any]:
    link_data: dict[str, Any] = {
        "link": WHATSAPP_LINK,
        "name": headline,
        "message": message,
        "image_hash": image_hash,
        "call_to_action": {
            "type": "WHATSAPP_MESSAGE",
            "value": {"app_destination": "WHATSAPP", "link": WHATSAPP_LINK},
        },
    }
    if page_welcome_message:
        link_data["page_welcome_message"] = (
            page_welcome_message
            if isinstance(page_welcome_message, str)
            else json.dumps(page_welcome_message, ensure_ascii=False)
        )

    oss: dict[str, Any] = {"page_id": str(page_id), "link_data": link_data}
    if instagram_user_id:
        oss["instagram_user_id"] = str(instagram_user_id)
    return {"name": name, "object_story_spec": oss}


def ctwa_static_creative(**kwargs: Any) -> dict[str, Any]:
    return ctwa_static_link_data_creative(**kwargs)


def ctwa_video_creative(
    *,
    name: str,
    page_id: str,
    message: str,
    video_id: str,
    image_url: str | None = None,
    image_hash: str | None = None,
    title: str | None = None,
    instagram_user_id: str | None = None,
    page_welcome_message: dict[str, Any] | str | None = None,
) -> dict[str, Any]:
    video_data: dict[str, Any] = {
        "video_id": str(video_id),
        "message": message,
        "call_to_action": {
            "type": "WHATSAPP_MESSAGE",
            "value": {"app_destination": "WHATSAPP", "link": WHATSAPP_LINK},
        },
    }
    if title is not None:
        video_data["title"] = title
    if image_url:
        video_data["image_url"] = image_url
    if image_hash:
        video_data["image_hash"] = image_hash
    if page_welcome_message:
        video_data["page_welcome_message"] = (
            page_welcome_message
            if isinstance(page_welcome_message, str)
            else json.dumps(page_welcome_message, ensure_ascii=False)
        )

    oss: dict[str, Any] = {"page_id": str(page_id), "video_data": video_data}
    if instagram_user_id:
        oss["instagram_user_id"] = str(instagram_user_id)
    return {"name": name, "object_story_spec": oss}


def _instagram_profile_cta(instagram_username: str, instagram_user_id: str | None, link_format: str) -> dict[str, Any]:
    username = str(instagram_username).lstrip("@")
    app_link = f"instagram://user?username={username}"
    value = {"link": f"http://instagram.com/{username}", "app_link": app_link, "link_format": link_format}
    return {"type": "VIEW_INSTAGRAM_PROFILE", "value": value}


def ig_profile_video_creative(
    *,
    name: str,
    page_id: str,
    instagram_user_id: str,
    instagram_username: str,
    message: str,
    video_id: str,
    image_url: str | None = None,
    image_hash: str | None = None,
) -> dict[str, Any]:
    """Видео → визит в профиль Instagram (схема рабочей кампании 30.08.2026)."""
    video_data: dict[str, Any] = {
        "video_id": str(video_id),
        "message": message,
        "call_to_action": _instagram_profile_cta(instagram_username, instagram_user_id, "VIDEO_LPP"),
    }
    if image_url:
        video_data["image_url"] = image_url
    if image_hash:
        video_data["image_hash"] = image_hash
    return {
        "name": name,
        "object_story_spec": {
            "page_id": str(page_id),
            "instagram_user_id": str(instagram_user_id),
            "video_data": video_data,
        },
    }


def ig_profile_static_creative(
    *,
    name: str,
    page_id: str,
    instagram_user_id: str,
    instagram_username: str,
    message: str,
    image_hash: str,
    image_crops: dict[str, list[list[int]]] | None = None,
) -> dict[str, Any]:
    """Картинка → визит в профиль Instagram."""
    username = str(instagram_username).lstrip("@")
    link_data: dict[str, Any] = {
        "link": f"http://instagram.com/{username}",
        "message": message,
        "image_hash": image_hash,
        "call_to_action": _instagram_profile_cta(instagram_username, instagram_user_id, "IMAGE_LPP"),
    }
    if image_crops:
        link_data["image_crops"] = image_crops
    return {
        "name": name,
        "object_story_spec": {
            "page_id": str(page_id),
            "instagram_user_id": str(instagram_user_id),
            "link_data": link_data,
        },
    }


def ig_profile_post_creative(
    *,
    name: str,
    page_id: str,
    instagram_user_id: str,
    instagram_username: str,
    source_instagram_media_id: str,
) -> dict[str, Any]:
    """Готовый пост/рилс Instagram → визит в профиль. Текст и медиа берутся из поста."""
    username = str(instagram_username).lstrip("@")
    return {
        "name": name,
        "object_id": str(page_id),
        "instagram_user_id": str(instagram_user_id),
        "source_instagram_media_id": str(source_instagram_media_id),
        "call_to_action": {
            "type": "VIEW_INSTAGRAM_PROFILE",
            "value": {"link": f"https://www.instagram.com/{username}/", "app_link": f"instagram://user?username={username}"},
        },
    }


def _asset_label(name: str) -> dict[str, str]:
    return {"name": name}


def lead_form_asset_feed_creative(
    *,
    name: str,
    page_id: str,
    message: str,
    lead_gen_form_id: str,
    image_hash: str | None = None,
    video_id: str | None = None,
    thumbnail_url: str | None = None,
    instagram_user_id: str | None = None,
    headline: str = "",
    description: str = " ",
    link: str = "http://fb.me/",
    url_tags: str = DEFAULT_URL_TAGS,
    asset_customization_rules: list[dict[str, Any]] | None = None,
    image_crops: dict[str, list[list[int]]] | None = None,
) -> dict[str, Any]:
    """asset_feed лид-формы. image_crops (только static) даёт два image-ассета с одним
    hash — оригинал для Stories/Reels и кадрированный для Feed — плюс обязательные
    для optimization_type=PLACEMENT правила (без правил Meta показывает 0 раз)."""
    if bool(image_hash) == bool(video_id):
        raise ValueError("lead_form_asset_feed_creative requires exactly one of image_hash or video_id")

    media_label = "video_main" if video_id else "image_main"
    body_label = "body_main"
    link_label = "link_main"
    title_label = "title_main"

    asset_feed: dict[str, Any] = {
        "bodies": [{"adlabels": [_asset_label(body_label)], "text": message}],
        "call_to_action_types": ["LEARN_MORE"],
        "call_to_actions": [
            {"type": "LEARN_MORE", "value": {"lead_gen_form_id": str(lead_gen_form_id), "link": link}}
        ],
        "descriptions": [{"text": description}],
        "link_urls": [{"adlabels": [_asset_label(link_label)], "website_url": link, "display_url": ""}],
        "titles": [{"adlabels": [_asset_label(title_label)], "text": headline}],
        "optimization_type": "PLACEMENT",
        "additional_data": {"multi_share_end_card": False, "is_click_to_message": False},
    }
    if image_hash and image_crops:
        images, rules = placement_image_assets(image_hash, image_crops)
        for rule in rules:
            rule["body_label"] = _asset_label(body_label)
            rule["title_label"] = _asset_label(title_label)
            rule["link_url_label"] = _asset_label(link_label)
        asset_feed["images"] = images
        asset_feed["ad_formats"] = ["SINGLE_IMAGE"]
        asset_customization_rules = asset_customization_rules or rules
    elif image_hash:
        asset_feed["images"] = [{"adlabels": [_asset_label(media_label)], "hash": image_hash}]
        asset_feed["ad_formats"] = ["SINGLE_IMAGE"]
    else:
        if not thumbnail_url:
            raise ValueError("lead_form_asset_feed_creative video mode requires thumbnail_url")
        video: dict[str, Any] = {"adlabels": [_asset_label(media_label)], "video_id": str(video_id)}
        video["thumbnail_url"] = thumbnail_url
        asset_feed["videos"] = [video]
        asset_feed["ad_formats"] = ["SINGLE_VIDEO"]

    if asset_customization_rules:
        asset_feed["asset_customization_rules"] = asset_customization_rules

    oss: dict[str, Any] = {"page_id": str(page_id)}
    if instagram_user_id:
        oss["instagram_user_id"] = str(instagram_user_id)
    return {
        "name": name,
        "creative_mode": "asset_feed",
        "url_tags": url_tags,
        "object_story_spec": oss,
        "asset_feed_spec": asset_feed,
    }


def lead_form_static_creative(
    *,
    name: str,
    page_id: str,
    message: str,
    lead_gen_form_id: str,
    image_hash: str,
    instagram_user_id: str | None = None,
    headline: str = "",
    creative_mode: str = "asset_feed",
    link: str = "http://fb.me/",
    url_tags: str = DEFAULT_URL_TAGS,
    image_crops: dict[str, list[list[int]]] | None = None,
) -> dict[str, Any]:
    if creative_mode == "asset_feed":
        return lead_form_asset_feed_creative(
            name=name,
            page_id=page_id,
            message=message,
            lead_gen_form_id=lead_gen_form_id,
            image_hash=image_hash,
            instagram_user_id=instagram_user_id,
            headline=headline,
            link=link,
            url_tags=url_tags,
            image_crops=image_crops,
        )
    if creative_mode != "simple_story":
        raise ValueError(f"unsupported lead_form_static creative_mode: {creative_mode}")

    link_data: dict[str, Any] = {
        "link": link,
        "message": message,
        "name": headline,
        "image_hash": image_hash,
        "call_to_action": {
            "type": "LEARN_MORE",
            "value": {"lead_gen_form_id": str(lead_gen_form_id), "link": link},
        },
    }
    if image_crops:
        link_data["image_crops"] = image_crops
    oss: dict[str, Any] = {"page_id": str(page_id), "link_data": link_data}
    if instagram_user_id:
        oss["instagram_user_id"] = str(instagram_user_id)
    return {"name": name, "creative_mode": "simple_story", "url_tags": url_tags, "object_story_spec": oss}


def lead_form_video_creative(
    *,
    name: str,
    page_id: str,
    message: str,
    lead_gen_form_id: str,
    video_id: str,
    image_url: str | None = None,
    image_hash: str | None = None,
    thumbnail_url: str | None = None,
    instagram_user_id: str | None = None,
    headline: str = "",
    creative_mode: str = "asset_feed",
    link: str = "http://fb.me/",
    url_tags: str = DEFAULT_URL_TAGS,
) -> dict[str, Any]:
    if creative_mode == "asset_feed":
        return lead_form_asset_feed_creative(
            name=name,
            page_id=page_id,
            message=message,
            lead_gen_form_id=lead_gen_form_id,
            video_id=video_id,
            thumbnail_url=thumbnail_url or image_url,
            instagram_user_id=instagram_user_id,
            headline=headline,
            link=link,
            url_tags=url_tags,
        )
    if creative_mode != "simple_story":
        raise ValueError(f"unsupported lead_form_video creative_mode: {creative_mode}")

    video_data: dict[str, Any] = {
        "video_id": str(video_id),
        "message": message,
        "title": headline,
        "call_to_action": {
            "type": "LEARN_MORE",
            "value": {"lead_gen_form_id": str(lead_gen_form_id), "link": link},
        },
    }
    if image_url:
        video_data["image_url"] = image_url
    if image_hash:
        video_data["image_hash"] = image_hash
    oss: dict[str, Any] = {"page_id": str(page_id), "video_data": video_data}
    if instagram_user_id:
        oss["instagram_user_id"] = str(instagram_user_id)
    return {"name": name, "creative_mode": "simple_story", "url_tags": url_tags, "object_story_spec": oss}


def _as_dict(value: Any) -> dict[str, Any]:
    value = _decode_maybe_json(value)
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    value = _decode_maybe_json(value)
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def extract_lead_gen_form_ids(params: dict[str, Any]) -> list[str]:
    """Extract lead form IDs from creative CTA values.

    Lead forms belong to the creative CTA, not to adset promoted_object. This
    matches real working lead form campaigns.
    """
    ids: list[str] = []
    seen: set[str] = set()
    roots = [
        _decode_maybe_json(params.get("object_story_spec")),
        _decode_maybe_json(params.get("asset_feed_spec")),
    ]
    for root in roots:
        for item in _walk(root):
            value = item.get("value")
            if isinstance(value, dict):
                form_id = value.get("lead_gen_form_id") or value.get("lead_form_id")
                if form_id and str(form_id) not in seen:
                    ids.append(str(form_id))
                    seen.add(str(form_id))
            form_id = item.get("lead_gen_form_id") or item.get("lead_form_id")
            if form_id and str(form_id) not in seen:
                ids.append(str(form_id))
                seen.add(str(form_id))
    return ids


def extract_cta_types(params: dict[str, Any]) -> list[str]:
    ctas: list[str] = []
    seen: set[str] = set()
    roots = [
        _decode_maybe_json(params.get("object_story_spec")),
        _decode_maybe_json(params.get("asset_feed_spec")),
    ]
    for root in roots:
        for item in _walk(root):
            cta_type = item.get("type")
            if isinstance(cta_type, str) and cta_type not in seen:
                ctas.append(cta_type)
                seen.add(cta_type)
    return ctas


def _asset_feed_counts(asset_feed: Any) -> dict[str, int]:
    asset_feed = _decode_maybe_json(asset_feed)
    if not isinstance(asset_feed, dict):
        return {}
    return {
        key: len(asset_feed.get(key) or [])
        for key in ("images", "videos", "bodies", "titles", "descriptions", "call_to_actions")
        if isinstance(asset_feed.get(key), list)
    }


def _asset_feed_video_thumbnail_issues(asset_feed: Any, scenario: str) -> list[str]:
    asset_feed = _decode_maybe_json(asset_feed)
    if not isinstance(asset_feed, dict):
        return []
    issues: list[str] = []
    for index, video in enumerate(asset_feed.get("videos") or []):
        if not isinstance(video, dict):
            issues.append(f"{scenario} asset_feed_spec.videos[{index}] must be object")
            continue
        if not (video.get("thumbnail_url") or video.get("image_url") or video.get("image_hash")):
            issues.append(f"{scenario} asset_feed_spec.videos[{index}] needs thumbnail_url, image_url, or image_hash")
    return issues


def _creative_mode(params: dict[str, Any], matrix: dict[str, Any]) -> str | None:
    explicit = params.get("creative_mode")
    if explicit:
        return str(explicit)
    if _decode_maybe_json(params.get("asset_feed_spec")):
        return "asset_feed"
    # Прочитанный из Meta креатив поля creative_mode не несёт. Если у него нет
    # asset_feed_spec, но есть готовая история в object_story_spec, то это
    # simple_story — иначе сценарий с asset_feed по умолчанию (lead_form_*)
    # объявлял бы собственный корректный креатив невалидным при обратной сверке.
    story = _decode_maybe_json(params.get("object_story_spec")) or {}
    if isinstance(story, dict) and (story.get("link_data") or story.get("video_data")):
        return "simple_story"
    return matrix.get("default_creative_mode")


def validate_campaign_params(scenario: str, params: dict[str, Any]) -> list[str]:
    matrix = get_scenario_config(scenario)
    if not matrix:
        return [f"unknown scenario: {scenario}"]
    expected = matrix.get("objective")
    if expected and params.get("objective") != expected:
        return [f"campaign objective must be {expected}"]
    return []


def validate_adset_params(
    scenario: str,
    params: dict[str, Any],
    bindings: dict[str, Any] | None = None,
) -> list[str]:
    scenario = canonical_scenario(scenario)
    matrix = get_scenario_config(scenario)
    if not matrix:
        return [f"unknown scenario: {scenario}"]

    bindings = bindings or {}
    issues: list[str] = []
    expected_goal = matrix.get("optimization_goal")
    if expected_goal and params.get("optimization_goal") != expected_goal:
        issues.append(f"optimization_goal must be {expected_goal}")

    expected_destination = matrix.get("destination_type")
    if expected_destination and params.get("destination_type") != expected_destination:
        issues.append(f"destination_type must be {expected_destination}")

    if params.get("optimization_goal") in {"OFFSITE_CONVERSIONS", "CONVERSATIONS"}:
        if not params.get("bid_strategy") and not params.get("bid_amount"):
            issues.append("bid_strategy=LOWEST_COST_WITHOUT_CAP is required unless bid_amount is set")

    issues.extend(validate_targeting(params.get("targeting")))

    promoted_object = _as_dict(params.get("promoted_object"))
    if scenario in {"lead_site_static", "lead_site_video"}:
        pixel_id = str(bindings.get("pixel_id") or "")
        if not promoted_object.get("pixel_id"):
            issues.append(f"{scenario} promoted_object.pixel_id is required")
        elif pixel_id and str(promoted_object.get("pixel_id")) != pixel_id:
            issues.append(f"{scenario} promoted_object.pixel_id does not match bindings.pixel_id")
        if promoted_object.get("custom_event_type") != "LEAD":
            issues.append(f"{scenario} promoted_object.custom_event_type must be LEAD")

    elif scenario in {"lead_form_static", "lead_form_video"}:
        page_id = str(bindings.get("page_id") or "")
        if not promoted_object.get("page_id"):
            issues.append(f"{scenario} promoted_object.page_id is required")
        elif page_id and str(promoted_object.get("page_id")) != page_id:
            issues.append(f"{scenario} promoted_object.page_id does not match bindings.page_id")
        if promoted_object.get("lead_gen_form_id") or promoted_object.get("lead_form_id"):
            issues.append(f"{scenario} lead form id belongs in creative CTA value, not adset promoted_object")

    elif scenario in {"ctwa_static", "ctwa_video", "ctwa_native_copy"}:
        page_id = str(bindings.get("page_id") or "")
        whatsapp_phone = str(bindings.get("whatsapp_phone_number") or "")
        if not promoted_object.get("page_id"):
            issues.append(f"{scenario} promoted_object.page_id is required")
        elif page_id and str(promoted_object.get("page_id")) != page_id:
            issues.append(f"{scenario} promoted_object.page_id does not match bindings.page_id")
        if whatsapp_phone and str(promoted_object.get("whatsapp_phone_number") or "") != whatsapp_phone:
            issues.append(f"{scenario} promoted_object.whatsapp_phone_number must match bindings.whatsapp_phone_number")

    elif scenario in {"ig_direct_static", "ig_direct_video"}:
        page_id = str(bindings.get("page_id") or "")
        if promoted_object.get("page_id") and page_id and str(promoted_object.get("page_id")) != page_id:
            issues.append(f"{scenario} promoted_object.page_id does not match bindings.page_id")

    elif scenario in {"ig_profile_static", "ig_profile_video", "ig_profile_post"}:
        page_id = str(bindings.get("page_id") or "")
        if not promoted_object.get("page_id"):
            issues.append(f"{scenario} promoted_object.page_id is required")
        elif page_id and str(promoted_object.get("page_id")) != page_id:
            issues.append(f"{scenario} promoted_object.page_id does not match bindings.page_id")
        platforms = _as_dict(params.get("targeting")).get("publisher_platforms")
        if isinstance(platforms, list) and platforms and platforms != ["instagram"]:
            issues.append(f"{scenario} targeting.publisher_platforms should be [\"instagram\"] only")

    return issues


def validate_creative_params(
    scenario: str,
    params: dict[str, Any],
    bindings: dict[str, Any] | None = None,
) -> list[str]:
    scenario = canonical_scenario(scenario)
    matrix = get_scenario_config(scenario)
    if not matrix:
        return [f"unknown scenario: {scenario}"]

    bindings = bindings or {}
    issues: list[str] = []
    for field in matrix.get("forbidden_creative_fields", []):
        if params.get(field):
            issues.append(f"{scenario} must not use {field}")

    if matrix.get("creative_schema") == "source_instagram_media_id":
        if not params.get("source_instagram_media_id"):
            issues.append(f"{scenario} source_instagram_media_id is required")
        if bindings.get("page_id") and str(params.get("object_id") or "") != str(bindings["page_id"]):
            issues.append("object_id does not match bindings.page_id")
        if bindings.get("instagram_user_id") and str(params.get("instagram_user_id") or "") != str(bindings["instagram_user_id"]):
            issues.append("instagram_user_id does not match bindings.instagram_user_id")
        if (params.get("call_to_action") or {}).get("type") != "VIEW_INSTAGRAM_PROFILE":
            issues.append(f"{scenario} CTA must be VIEW_INSTAGRAM_PROFILE")
        return issues

    oss = _decode_maybe_json(params.get("object_story_spec"))
    if not isinstance(oss, dict):
        issues.append("object_story_spec is required")
        return issues

    page_id = str(bindings.get("page_id") or "")
    ig_id = str(bindings.get("instagram_user_id") or "")
    if page_id and str(oss.get("page_id") or "") != page_id:
        issues.append("object_story_spec.page_id does not match bindings.page_id")
    should_check_ig = "instagram_user_id" in matrix.get("required_bindings", []) or bool(oss.get("instagram_user_id"))
    if ig_id and should_check_ig and str(oss.get("instagram_user_id") or "") != ig_id:
        issues.append("object_story_spec.instagram_user_id does not match bindings.instagram_user_id")

    schema = matrix.get("creative_schema")
    asset_feed = _decode_maybe_json(params.get("asset_feed_spec"))
    asset_counts = _asset_feed_counts(asset_feed)
    mode = _creative_mode(params, matrix)

    if schema == "object_story_spec.link_data":
        link_data = oss.get("link_data")
        if not isinstance(link_data, dict):
            issues.append("object_story_spec.link_data is required")
        else:
            child_attachments = link_data.get("child_attachments")
            has_carousel_media = (
                isinstance(child_attachments, list)
                and len(child_attachments) >= 2
                and all(
                    isinstance(card, dict) and (card.get("image_hash") or card.get("picture"))
                    for card in child_attachments
                )
            )
            if matrix.get("media_type") == "static" and not (
                link_data.get("image_hash") or link_data.get("picture") or has_carousel_media
            ):
                issues.append(f"{scenario} link_data needs image_hash, picture, or at least two carousel cards")
            cta = link_data.get("call_to_action") or {}
            if scenario == "ctwa_static":
                if cta.get("type") != "WHATSAPP_MESSAGE":
                    issues.append("ctwa_static CTA must be WHATSAPP_MESSAGE")
                if "page_welcome_message" in (cta.get("value") or {}):
                    issues.append("page_welcome_message belongs in link_data, not call_to_action.value")
            elif scenario == "ig_direct_static" and cta.get("type") not in {"INSTAGRAM_MESSAGE", "MESSAGE_PAGE"}:
                issues.append("ig_direct_static CTA should be INSTAGRAM_MESSAGE or MESSAGE_PAGE")
            elif scenario == "ig_profile_static" and cta.get("type") != "VIEW_INSTAGRAM_PROFILE":
                issues.append("ig_profile_static CTA must be VIEW_INSTAGRAM_PROFILE")
            if link_data.get("image_crops"):
                issues.append(
                    "link_data.image_crops применяется Meta и к Reels/Stories (проверено 10.09.2026): "
                    "для simple_story не использовать; placement-кадрирование только через asset_feed_spec"
                )
    elif schema == "object_story_spec.video_data":
        video_data = oss.get("video_data")
        if not isinstance(video_data, dict):
            issues.append("object_story_spec.video_data is required")
        else:
            if not video_data.get("video_id"):
                issues.append(f"{scenario} video_data.video_id is required")
            if not video_data.get("image_url") and not video_data.get("image_hash"):
                issues.append("video_data needs image_url or image_hash thumbnail")
            cta = video_data.get("call_to_action") or {}
            if scenario == "ig_direct_video" and cta.get("type") not in {"INSTAGRAM_MESSAGE", "MESSAGE_PAGE"}:
                issues.append("ig_direct_video CTA should be INSTAGRAM_MESSAGE or MESSAGE_PAGE")
            elif scenario == "ig_profile_video" and cta.get("type") != "VIEW_INSTAGRAM_PROFILE":
                issues.append("ig_profile_video CTA must be VIEW_INSTAGRAM_PROFILE")
    elif schema == "object_story_spec.video_data_or_asset_feed":
        video_data = oss.get("video_data")
        if isinstance(asset_feed, dict):
            if asset_counts.get("images", 0) and not asset_counts.get("videos", 0):
                issues.append("ctwa_video asset_feed_spec must contain videos, not image-only static assets")
            issues.extend(_asset_feed_video_thumbnail_issues(asset_feed, "ctwa_video"))
            if "WHATSAPP_MESSAGE" not in extract_cta_types(params):
                issues.append("ctwa_video asset_feed_spec CTA must include WHATSAPP_MESSAGE")
        elif not isinstance(video_data, dict):
            issues.append("ctwa_video requires object_story_spec.video_data or video asset_feed_spec")
        else:
            if not video_data.get("video_id"):
                issues.append("ctwa_video video_data.video_id is required")
            if not video_data.get("image_url") and not video_data.get("image_hash"):
                issues.append("video_data needs image_url or image_hash thumbnail")
            cta = video_data.get("call_to_action") or {}
            if cta.get("type") != "WHATSAPP_MESSAGE":
                issues.append("ctwa_video CTA must be WHATSAPP_MESSAGE")
    elif schema in {"lead_form_static", "lead_form_video"}:
        lead_form_ids = extract_lead_gen_form_ids(params)
        expected_form_id = str(bindings.get("lead_gen_form_id") or "")
        if not lead_form_ids:
            issues.append(f"{scenario} creative CTA value must contain lead_gen_form_id")
        elif expected_form_id and expected_form_id not in lead_form_ids:
            issues.append(f"{scenario} lead_gen_form_id does not match bindings.lead_gen_form_id")

        if mode == "asset_feed":
            if not isinstance(asset_feed, dict):
                issues.append(f"{scenario} creative_mode=asset_feed requires asset_feed_spec")
            else:
                if not asset_counts.get("call_to_actions", 0):
                    issues.append(f"{scenario} asset_feed_spec.call_to_actions is required")
                if schema == "lead_form_static":
                    if not asset_counts.get("images", 0):
                        issues.append("lead_form_static asset_feed_spec.images is required")
                    if asset_counts.get("videos", 0):
                        issues.append("lead_form_static must not use asset_feed_spec.videos")
                if schema == "lead_form_video":
                    if not asset_counts.get("videos", 0):
                        issues.append("lead_form_video asset_feed_spec.videos is required")
                    if asset_counts.get("images", 0) and not asset_counts.get("videos", 0):
                        issues.append("lead_form_video must not be image-only")
                    issues.extend(_asset_feed_video_thumbnail_issues(asset_feed, "lead_form_video"))
        elif mode == "simple_story":
            if schema == "lead_form_static":
                link_data = oss.get("link_data")
                if not isinstance(link_data, dict):
                    issues.append("lead_form_static simple_story requires object_story_spec.link_data")
                elif not (link_data.get("image_hash") or link_data.get("picture")):
                    issues.append("lead_form_static simple_story needs image_hash or picture")
            elif schema == "lead_form_video":
                video_data = oss.get("video_data")
                if not isinstance(video_data, dict):
                    issues.append("lead_form_video simple_story requires object_story_spec.video_data")
                else:
                    if not video_data.get("video_id"):
                        issues.append("lead_form_video video_data.video_id is required")
                    if not video_data.get("image_url") and not video_data.get("image_hash"):
                        issues.append("video_data needs image_url or image_hash thumbnail")
        else:
            issues.append(f"unsupported creative_mode for {scenario}: {mode}")

    if scenario == "ctwa_static" and isinstance(asset_feed, dict):
        issues.append("static CTWA must use link_data, not asset_feed_spec")

    return issues


# ── Именование (метод, зафиксирован 10.09.2026) ──────────────────────────
# Campaign: "[гео] | цель | обозначение | DD.MM"   → "[almaty] | whatsapp | кофе с собой | 10.09"
# Adset:    "креатив | аудитория | DD.MM"           → "Капучино баннер | 25+ все | 10.09"
# Ad:       ровно первый сегмент имени adset        → "Капучино баннер"
# Дата у campaign и adset одна. Цели только из словаря, "wp" в новых запусках не использовать.
NAMING_GOALS = ("lead site", "lead form", "whatsapp", "direct", "profile", "трафик")
_CAMPAIGN_RE = re.compile(r"^\[[^\]]+\] \| (?P<goal>[^|]+?) \| (?P<label>[^|]+?) \| (?P<date>\d{2}\.\d{2})$")
_ADSET_RE = re.compile(r"^(?P<creative>[^|]+?) \| (?P<audience>[^|]+?) \| (?P<date>\d{2}\.\d{2})$")


def validate_names(campaign_name: str | None, adset_name: str | None, ad_name: str | None) -> list[str]:
    """Предупреждения по именам (не блокировка). Пустой список = всё по методу."""
    issues: list[str] = []
    camp_date = None
    if campaign_name:
        m = _CAMPAIGN_RE.match(campaign_name.strip())
        if not m:
            issues.append(f"campaign «{campaign_name}»: ожидается «[гео] | цель | обозначение | DD.MM»")
        else:
            camp_date = m.group("date")
            goal = m.group("goal").strip().lower()
            if goal not in NAMING_GOALS:
                issues.append(f"campaign «{campaign_name}»: цель «{goal}» не из словаря {list(NAMING_GOALS)}")
    creative_segment = None
    if adset_name:
        m = _ADSET_RE.match(adset_name.strip())
        if not m:
            issues.append(f"adset «{adset_name}»: ожидается «креатив | аудитория | DD.MM»")
        else:
            creative_segment = m.group("creative").strip()
            if camp_date and m.group("date") != camp_date:
                issues.append(f"adset «{adset_name}»: дата {m.group('date')} не совпадает с датой кампании {camp_date}")
            if len(creative_segment.split()) > 4:
                issues.append(f"adset «{adset_name}»: сегмент креатива длиннее четырёх слов")
    if ad_name and creative_segment is not None and ad_name.strip() != creative_segment:
        issues.append(f"ad «{ad_name}»: должно совпадать с первым сегментом adset «{creative_segment}»")
    return issues


def scenario_summary() -> dict[str, Any]:
    return copy.deepcopy(SCENARIO_MATRIX)
