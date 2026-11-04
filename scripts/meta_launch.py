#!/usr/bin/env python3
"""meta_launch.py — запуск дерева campaign → adsets → creatives → ads из JSON-спеки.

Заменяет одноразовые launch-скрипты: спека описывает структуру, билдеры собирают
схемы по сценарию, meta_api.py делает write с паузами и staging-gate, state-файл
даёт резюмируемость (повторный запуск той же спеки досоздаёт только недостающее).

  python3 meta_launch.py --spec spec.json --check-previews --activate   # обычный запуск одной командой
  python3 meta_launch.py --spec spec.json                # staged: campaign PAUSED, дети ACTIVE, без включения
  python3 meta_launch.py --spec spec.json --validate-only # только серверная проверка, ничего не создаёт
  python3 meta_launch.py --spec spec.json --preview-only  # лист картинок превью до запуска, ничего не создаёт
  python3 meta_launch.py --spec spec.json --dry-run       # локально, без запросов к Meta

  Обычный запуск делает всё подряд: локальная проверка спеки и имён → медиа → серверная
  проверка Meta (execution_options=validate_only, сущности не создаются) → создание дерева →
  превью → включение. Отдельного шага «валидация» больше нет; если Meta что-то отвергла,
  запуск останавливается до создания, в отчёте видно что именно. --no-validate пропускает
  серверную проверку (например, адсеты в работающую кампанию с теми же биндингами).
  Для проверки adset нужна реальная кампания: campaign.id в спеке или spec.validate_campaign_id.

Запускать из папки scripts/ или через абсолютный путь; в фоне (Bash режет на 2 минуте):
  nohup python3 <skill>/scripts/meta_launch.py --spec ... > /tmp/launch.log 2>&1 &

Формат спеки (минимум):
{
  "client": "coffee-house", "label": "wa-launch", "account_id": "act_…", "scenario": "ctwa_static",
  "bindings": {"page_id": "…", "instagram_user_id": "…", "whatsapp_phone_number": "7700…",
               "pixel_id": "…", "lead_gen_form_id": "…", "instagram_username": "…"},
  "media": {"poster1": {"file": "/path/a.png", "focus": "top"}, "vid1": {"file": "/path/b.mp4"}},
  "defaults": {"adset": {"daily_budget": 1200, "targeting": {…}, "start_time": "…"},
               "creative": {"message": "…", "headline": "…", "link": "https://…",
                            "page_welcome_message": {…}, "url_tags": "…", "cta_type": "LEARN_MORE"}},
  "campaigns": [{"name": "…", "adsets": [{"name": "…", "ads": [{"name": "…", "media": "poster1",
                 "creative": {"message": "…", "focus": "0.35"}}]}]}]
}
Любой уровень принимает "params" (сырые поля Meta поверх собранных). У ad вместо
"creative" можно дать "creative_params" целиком; внутри строки "$media:poster1.hash",
"$media:vid1.id", "$media:vid1.thumbnail" подставляются после загрузки.
Существующая кампания: {"id": "1202…", "adsets": [...]} — адсеты создаются в неё.
Видео со своей обложкой: "vid1": {"file": "/path/b.mp4", "thumbnail": "/path/cover.jpg"}.
Продвижение готового поста (scenario ig_profile_post): "post1": {"ig_media_id": "1810…"},
id публикаций — meta_api.py ig-media <instagram_user_id>.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from meta_api import MetaAdsAPI, cli_print  # noqa: E402
import meta_launch_builders as B  # noqa: E402
from meta_launch_state import (  # noqa: E402
    default_state_path,
    load_state,
    mark_activated,
    new_launch_state,
    save_state,
)

SITE_SCENARIOS = {"lead_site_static", "lead_site_video"}
FORM_SCENARIOS = {"lead_form_static", "lead_form_video"}
CTWA_SCENARIOS = {"ctwa_static", "ctwa_video"}
DIRECT_SCENARIOS = {"ig_direct_static", "ig_direct_video"}
PROFILE_SCENARIOS = {"ig_profile_static", "ig_profile_video", "ig_profile_post"}
VIDEO_SCENARIOS = {s for s in B.CANONICAL_SCENARIO_MATRIX if B.CANONICAL_SCENARIO_MATRIX[s].get("media_type") == "video"}


class LaunchError(RuntimeError):
    pass


def _merge(base: dict[str, Any] | None, *layers: dict[str, Any] | None) -> dict[str, Any]:
    out = copy.deepcopy(base or {})
    for layer in layers:
        for key, value in (layer or {}).items():
            out[key] = copy.deepcopy(value)
    return out


def _file_sha1(path: str) -> str:
    digest = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _substitute_media(value: Any, media: dict[str, dict[str, Any]]) -> Any:
    if isinstance(value, dict):
        return {k: _substitute_media(v, media) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_media(v, media) for v in value]
    if isinstance(value, str) and value.startswith("$media:"):
        key, _, field = value[len("$media:"):].partition(".")
        item = media.get(key) or {}
        field_map = {"hash": "hash", "id": "video_id", "thumbnail": "thumbnail_url", "url": "url"}
        resolved = item.get(field_map.get(field, field))
        if resolved is None:
            raise LaunchError(f"media placeholder {value}: нет значения (медиа не загружено?)")
        return resolved
    return value


class Launcher:
    def __init__(self, spec: dict[str, Any], api: MetaAdsAPI, state_path: Path, state: dict[str, Any] | None = None):
        self.spec = spec
        self.api = api
        self.state_path = state_path
        self.scenario = B.canonical_scenario(spec.get("scenario"))
        self.matrix = B.get_scenario_config(self.scenario)
        if not self.matrix:
            raise LaunchError(f"unknown scenario: {spec.get('scenario')}")
        self.account_id = str(spec["account_id"])
        if not self.account_id.startswith("act_"):
            self.account_id = f"act_{self.account_id}"
        self.bindings = dict(spec.get("bindings") or {})
        self.defaults = spec.get("defaults") or {}
        self.state = state or new_launch_state(
            client=spec.get("client", "unknown"),
            account_id=self.account_id,
            scenario=self.scenario,
            source_request={"spec_file": spec.get("_spec_file"), "label": spec.get("label"),
                            "task": spec.get("task")},
        )
        self.state.setdefault("bindings", self.bindings)  # preflight --state verify читает отсюда
        self.state.setdefault("media", {})
        self.state.setdefault("index", {"campaigns": {}, "adsets": {}, "creatives": {}, "ads": {}})
        self.state.setdefault("previews", {})
        self.errors: list[dict[str, Any]] = []

    # ── state ────────────────────────────────────────
    def save(self) -> None:
        save_state(self.state_path, self.state)

    def _remember(self, kind: str, key: str, entity_id: str, extra: dict[str, Any] | None = None) -> None:
        self.state["index"][kind][key] = str(entity_id)
        self.state["created_ids"].setdefault(kind, []).append({"key": key, "id": str(entity_id), **(extra or {})})
        self.save()

    def _fail(self, stage: str, key: str, response: Any) -> None:
        item = {"stage": stage, "key": key, "response": response, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        self.errors.append(item)
        self.state.setdefault("errors", []).append(item)
        self.save()
        cli_print(f"❌ {stage} «{key}»: {json.dumps(response, ensure_ascii=False)[:600]}")

    # ── media ────────────────────────────────────────
    def upload_media(self) -> None:
        for key, item in (self.spec.get("media") or {}).items():
            done = self.state["media"].get(key)
            file_path = item.get("file")
            if item.get("hash") or item.get("video_id") or item.get("ig_media_id"):
                # заранее загруженное медиа или готовый пост Instagram (ig_profile_post)
                record = {k: v for k, v in item.items() if k != "file"} | {"file": file_path}
                if item.get("video_id") and not item.get("thumbnail_url") and not self.api.dry_run:
                    # без обложки Meta отвергает видео-креатив (1443226): берём кадр, который
                    # Meta сама выбрала при загрузке (проверено 05.10.2026)
                    known = (self.state["media"].get(key) or {}).get("thumbnail_url")
                    record["thumbnail_url"] = known or self.api.video_thumbnail(item["video_id"])
                self.state["media"][key] = record
                continue
            if not file_path or not os.path.exists(file_path):
                raise LaunchError(f"media {key}: файл не найден: {file_path}")
            sha1 = _file_sha1(file_path)
            thumb_file = item.get("thumbnail")
            if (done and done.get("sha1") == sha1 and (done.get("hash") or done.get("video_id"))
                    and (not thumb_file or done.get("thumbnail_hash"))):
                continue
            is_video = Path(file_path).suffix.lower() in {".mp4", ".mov", ".m4v"}
            if is_video:
                res = self.api.upload_video(self.account_id, file_path)
                if not res.get("id"):
                    raise LaunchError(f"media {key}: upload_video failed: {res}")
                record = {"file": file_path, "sha1": sha1, "video_id": str(res["id"]),
                          "thumbnail_url": res.get("thumbnail_url", "")}
                if thumb_file:
                    # своя обложка: автокадр Meta часто размытый (лист превью колеса 04.10.2026)
                    if not os.path.exists(thumb_file):
                        raise LaunchError(f"media {key}: обложка не найдена: {thumb_file}")
                    thumb = self.api.upload_image(self.account_id, thumb_file)
                    if not thumb.get("hash"):
                        raise LaunchError(f"media {key}: upload обложки failed: {thumb}")
                    record["thumbnail_hash"] = thumb["hash"]
            else:
                res = self.api.upload_image(self.account_id, file_path)
                if not res.get("hash"):
                    raise LaunchError(f"media {key}: upload_image failed: {res}")
                width, height = B.image_size(file_path)
                record = {"file": file_path, "sha1": sha1, "hash": res["hash"], "url": res.get("url", ""),
                          "width": width, "height": height}
            self.state["media"][key] = record
            self.save()
            cli_print(f"📤 media {key}: {record.get('hash') or record.get('video_id')}")

    def media(self, key: str | None) -> dict[str, Any]:
        if not key:
            return {}
        item = self.state["media"].get(key)
        if not item:
            raise LaunchError(f"media «{key}» не описано в spec.media")
        return item

    # ── builders ─────────────────────────────────────
    def campaign_params(self, camp: dict[str, Any]) -> dict[str, Any]:
        objective = camp.get("objective") or self.matrix.get("objective")
        params = B.campaign_params(camp["name"], objective, camp.get("buying_type", "AUCTION"))
        return _merge(params, self.defaults.get("campaign"), camp.get("params"))

    def adset_params(self, camp: dict[str, Any], adset: dict[str, Any]) -> dict[str, Any]:
        d = self.defaults.get("adset") or {}
        base = _merge(d, {k: v for k, v in adset.items() if k not in {"ads", "params", "name"}})
        targeting = base.get("targeting")
        if not targeting:
            raise LaunchError(f"adset «{adset['name']}»: нет targeting (в адсете или defaults.adset)")
        targeting = copy.deepcopy(targeting)
        if B.apply_default_placements(targeting, threads=self.scenario not in PROFILE_SCENARIOS):
            cli_print(f"📍 adset «{adset['name']}»: площадки не заданы → {targeting['publisher_platforms']}, Instagram во всех позициях")
        budget = base.get("daily_budget")
        if budget is None and not camp.get("cbo"):
            raise LaunchError(f"adset «{adset['name']}»: нет daily_budget")
        common = {"name": adset["name"], "daily_budget": budget or 0, "targeting": targeting,
                  "account_id": self.account_id}
        s = self.scenario
        if s in SITE_SCENARIOS:
            params = B.lead_site_adset_params(pixel_id=self._need("pixel_id"), attribution_spec=base.get("attribution_spec") or B.CLICK_ATTRIBUTION_7D, **common)
        elif s in FORM_SCENARIOS:
            params = B.lead_form_adset_params(page_id=self._need("page_id"), bid_strategy=base.get("bid_strategy"), **common)
        elif s in CTWA_SCENARIOS:
            params = B.ctwa_adset_params(page_id=self._need("page_id"), whatsapp_phone_number=self.bindings.get("whatsapp_phone_number"), attribution_spec=base.get("attribution_spec"), **common)
        elif s in DIRECT_SCENARIOS:
            params = B.ig_direct_adset_params(page_id=self._need("page_id"), **common)
        elif s in PROFILE_SCENARIOS:
            params = B.ig_profile_adset_params(page_id=self._need("page_id"), attribution_spec=base.get("attribution_spec"), **common)
        else:
            raise LaunchError(f"scenario {s}: запуск через meta_launch.py не поддержан, нужен свой скрипт")
        if camp.get("cbo"):
            params.pop("daily_budget", None)
        for key in ("start_time", "end_time", "bid_amount", "adset_schedule", "attribution_spec"):
            if base.get(key) is not None and key not in params:
                params[key] = base[key]
        if base.get("attribution_spec") is not None:
            params["attribution_spec"] = base["attribution_spec"]
        return _merge(params, adset.get("params"))

    def _need(self, key: str) -> str:
        value = self.bindings.get(key)
        if not value:
            raise LaunchError(f"bindings.{key} обязателен для сценария {self.scenario}")
        return str(value)

    def _crops(self, media: dict[str, Any], focus: Any) -> dict[str, list[list[int]]] | None:
        if focus in (None, "", False) or not media.get("hash"):
            return None
        # link_data.image_crops Meta применяет и к Reels/Stories (проверено 10.09.2026:
        # в Reels показалась обрезка 4:5). Кадрирование по плейсментам работает только
        # в asset_feed_spec с правилами, поэтому focus учитываем лишь там.
        if self.scenario not in FORM_SCENARIOS:
            cli_print(f"⚠️ focus={focus} проигнорирован: сценарий {self.scenario} использует link_data, "
                      "а image_crops там режут и Reels/Stories. Лента кропается Meta по центру.")
            return None
        width, height = media.get("width"), media.get("height")
        if not width or not height:
            if media.get("file") and os.path.exists(media["file"]):
                width, height = B.image_size(media["file"])
            else:
                cli_print(f"⚠️ crops: нет размеров картинки для focus={focus}, пропускаю")
                return None
        if isinstance(focus, str) and focus.replace(".", "", 1).isdigit():
            focus = float(focus)
        return B.image_crops_for(int(width), int(height), focus, B.CROP_KEYS_FEED)

    def creative_params(self, camp: dict[str, Any], adset: dict[str, Any], ad: dict[str, Any]) -> dict[str, Any]:
        media = self.media(ad.get("media"))
        if ad.get("creative_params"):
            params = _substitute_media(copy.deepcopy(ad["creative_params"]), self.state["media"])
            params.setdefault("name", f"{ad['name']} | {adset['name']} | {time.strftime('%d.%m')}")
        else:
            c = _merge(self.defaults.get("creative"), ad.get("creative"))
            name = c.get("name") or f"{ad['name']} | {adset['name']} | {time.strftime('%d.%m')}"
            page_id = self._need("page_id")
            ig = self.bindings.get("instagram_user_id")
            message = c.get("message")
            if self.scenario == "ig_profile_post":
                # текст и медиа берутся из самого поста; message не нужен
                if not media.get("ig_media_id"):
                    raise LaunchError(f"ad «{ad['name']}»: ig_profile_post требует media.ig_media_id (meta_api.py ig-media)")
                params = B.ig_profile_post_creative(name=name, page_id=page_id, instagram_user_id=self._need("instagram_user_id"),
                                                    instagram_username=self._need("instagram_username"),
                                                    source_instagram_media_id=media["ig_media_id"])
                params["degrees_of_freedom_spec"] = B.advantage_creative_opt_out()
                return _merge(params, ad.get("params"))
            if message is None:
                raise LaunchError(f"ad «{ad['name']}»: нет creative.message")
            crops = self._crops(media, c.get("focus"))
            s = self.scenario
            is_video = s in VIDEO_SCENARIOS
            if is_video and not media.get("video_id"):
                raise LaunchError(f"ad «{ad['name']}»: сценарий {s} требует видео-медиа")
            if not is_video and not media.get("hash"):
                raise LaunchError(f"ad «{ad['name']}»: сценарий {s} требует image-медиа")
            url_tags = c.get("url_tags", B.DEFAULT_URL_TAGS)
            # обложка видео: своя картинка (media.thumbnail) важнее автокадра Meta
            thumb_hash = media.get("thumbnail_hash")
            thumb_url = None if thumb_hash else media.get("thumbnail_url")
            if s == "lead_site_static":
                params = B.lead_site_static_creative(name=name, page_id=page_id, link=c["link"], message=message,
                                                     image_hash=media["hash"], instagram_user_id=ig,
                                                     cta_type=c.get("cta_type", "LEARN_MORE"), url_tags=url_tags)
                if crops:
                    params["object_story_spec"]["link_data"]["image_crops"] = crops
                if c.get("headline"):
                    params["object_story_spec"]["link_data"]["name"] = c["headline"]
            elif s == "lead_site_video":
                params = B.lead_site_video_creative(name=name, page_id=page_id, link=c["link"], message=message,
                                                    video_id=media["video_id"], image_url=thumb_url, image_hash=thumb_hash,
                                                    title=c.get("headline"), instagram_user_id=ig,
                                                    cta_type=c.get("cta_type", "LEARN_MORE"), url_tags=url_tags)
            elif s == "ctwa_static":
                params = B.ctwa_static_link_data_creative(name=name, page_id=page_id, message=message,
                                                          image_hash=media["hash"], headline=c.get("headline", ""),
                                                          instagram_user_id=ig,
                                                          page_welcome_message=c.get("page_welcome_message"))
                if crops:
                    params["object_story_spec"]["link_data"]["image_crops"] = crops
            elif s == "ctwa_video":
                params = B.ctwa_video_creative(name=name, page_id=page_id, message=message, video_id=media["video_id"],
                                               image_url=thumb_url, image_hash=thumb_hash, title=c.get("headline"),
                                               instagram_user_id=ig, page_welcome_message=c.get("page_welcome_message"))
            elif s == "ig_direct_static":
                params = B.ig_direct_static_creative(name=name, page_id=page_id, message=message, image_hash=media["hash"],
                                                     instagram_user_id=self._need("instagram_user_id"),
                                                     headline=c.get("headline"))
                if crops:
                    params["object_story_spec"]["link_data"]["image_crops"] = crops
            elif s == "ig_direct_video":
                params = B.ig_direct_video_creative(name=name, page_id=page_id, message=message, video_id=media["video_id"],
                                                    instagram_user_id=self._need("instagram_user_id"),
                                                    image_url=thumb_url, image_hash=thumb_hash, title=c.get("headline"))
            elif s == "ig_profile_static":
                params = B.ig_profile_static_creative(name=name, page_id=page_id, instagram_user_id=self._need("instagram_user_id"),
                                                      instagram_username=self._need("instagram_username"), message=message,
                                                      image_hash=media["hash"], image_crops=crops)
            elif s == "ig_profile_video":
                params = B.ig_profile_video_creative(name=name, page_id=page_id, instagram_user_id=self._need("instagram_user_id"),
                                                     instagram_username=self._need("instagram_username"), message=message,
                                                     video_id=media["video_id"], image_url=thumb_url, image_hash=thumb_hash)
            elif s == "lead_form_static":
                mode = c.get("creative_mode", "asset_feed")
                if crops and mode != "asset_feed":
                    cli_print("⚠️ focus для lead_form_static работает только в creative_mode=asset_feed; crops убраны")
                    crops = None
                params = B.lead_form_static_creative(name=name, page_id=page_id, message=message,
                                                     lead_gen_form_id=self._need("lead_gen_form_id"), image_hash=media["hash"],
                                                     instagram_user_id=ig, headline=c.get("headline", ""),
                                                     creative_mode=mode, url_tags=url_tags, image_crops=crops)
            elif s == "lead_form_video":
                params = B.lead_form_video_creative(name=name, page_id=page_id, message=message,
                                                    lead_gen_form_id=self._need("lead_gen_form_id"), video_id=media["video_id"],
                                                    thumbnail_url=media.get("thumbnail_url"), image_url=media.get("thumbnail_url"),
                                                    instagram_user_id=ig, headline=c.get("headline", ""),
                                                    creative_mode=c.get("creative_mode", "simple_story"), url_tags=url_tags)
            else:
                raise LaunchError(f"scenario {s}: креатив через meta_launch.py не поддержан")
            params.pop("creative_mode", None)
            if c.get("degrees_of_freedom_spec") is not None:
                params["degrees_of_freedom_spec"] = c["degrees_of_freedom_spec"]
            elif not self.spec.get("advantage_creative"):
                params["degrees_of_freedom_spec"] = B.advantage_creative_opt_out()
            if s in CTWA_SCENARIOS | DIRECT_SCENARIOS | PROFILE_SCENARIOS:
                params.pop("url_tags", None)
        return _merge(params, ad.get("params"))

    # ── local validation ──────────────────────────────
    def naming_warnings(self) -> list[str]:
        """Именование по методу (см. builders.validate_names). Предупреждения, не блокировка."""
        warnings: list[str] = []
        for camp in self.spec.get("campaigns") or []:
            camp_name = None if camp.get("id") else camp.get("name")
            for adset in camp.get("adsets") or []:
                ads = adset.get("ads") or []
                ad_name = ads[0].get("name") if ads else None
                warnings += B.validate_names(camp_name, adset.get("name"), ad_name)
                if len(ads) > 1:
                    warnings.append(f"adset «{adset['name']}»: {len(ads)} объявлений, по методу одно объявление на группу")
        return warnings

    def validate_local(self) -> list[str]:
        issues: list[str] = []
        for camp in self.spec.get("campaigns") or []:
            if not camp.get("id"):
                issues += [f"campaign «{camp.get('name')}»: {i}" for i in B.validate_campaign_params(self.scenario, self.campaign_params(camp))]
            for adset in camp.get("adsets") or []:
                params = self.adset_params(camp, adset)
                issues += [f"adset «{adset['name']}»: {i}" for i in B.validate_adset_params(self.scenario, params, self.bindings)]
                for ad in adset.get("ads") or []:
                    media = self.spec.get("media", {}).get(ad.get("media") or "", {})
                    if ad.get("media") and not media:
                        issues.append(f"ad «{ad['name']}»: media «{ad['media']}» не описано")
                    if (self.scenario != "ig_profile_post" and not ad.get("creative")
                            and not ad.get("creative_params") and not self.defaults.get("creative")):
                        issues.append(f"ad «{ad['name']}»: нет creative/creative_params")
        return issues

    # ── execution ─────────────────────────────────────
    def run(self, activate: bool = False, check_previews: bool = False, validate: bool = True) -> dict[str, Any]:
        self.upload_media()
        idx = self.state["index"]
        already_created = any(idx[k] for k in ("campaigns", "adsets", "ads"))
        if validate and not already_created and not self.api.dry_run:
            report = self.validate_only(quiet=True)
            if not report.get("ok"):
                bad = {g: {k: v for k, v in items.items() if not (v.get("success") or v.get("skipped"))}
                       for g, items in report.items() if isinstance(items, dict)}
                cli_print(f"🚫 Meta отвергла часть спеки, ничего не создано: {json.dumps(bad, ensure_ascii=False)[:1500]}")
                summary = self.summary()
                summary["validate_only"] = bad
                summary["ok"] = False
                self.errors.append({"stage": "validate_only", "key": "spec", "response": bad})
                return summary
            total = sum(len(items) for g, items in report.items() if isinstance(items, dict))
            cli_print(f"✅ Meta приняла схему: {total} сущностей проверены, создаю дерево")
        for camp in self.spec.get("campaigns") or []:
            camp_key = camp.get("name") or camp.get("id")
            campaign_id = camp.get("id") or idx["campaigns"].get(camp_key)
            if not campaign_id:
                res = self.api.create_campaign(self.account_id, self.campaign_params(camp))
                if not res.get("id"):
                    self._fail("campaign", camp_key, res)
                    continue
                campaign_id = str(res["id"])
                self._remember("campaigns", camp_key, campaign_id)
            for adset in camp.get("adsets") or []:
                adset_key = f"{camp_key} | {adset['name']}"
                adset_id = idx["adsets"].get(adset_key)
                if not adset_id:
                    res = self.api.create_adset(campaign_id, self.adset_params(camp, adset))
                    if not res.get("id"):
                        self._fail("adset", adset_key, res)
                        continue
                    adset_id = str(res["id"])
                    self._remember("adsets", adset_key, adset_id, {"launch_gate": res.get("launch_gate")})
                for ad in adset.get("ads") or []:
                    ad_key = f"{adset_key} | {ad['name']}"
                    if idx["ads"].get(ad_key):
                        continue
                    creative_id = idx["creatives"].get(ad_key)
                    if not creative_id:
                        res = self.api.create_creative(self.account_id, self.creative_params(camp, adset, ad))
                        if not res.get("id"):
                            self._fail("creative", ad_key, res)
                            continue
                        creative_id = str(res["id"])
                        self._remember("creatives", ad_key, creative_id)
                    res = self.api.create_ad(adset_id, creative_id, ad["name"], account_id=self.account_id)
                    if not res.get("id"):
                        self._fail("ad", ad_key, res)
                        continue
                    self._remember("ads", ad_key, str(res["id"]), {"launch_gate": res.get("launch_gate")})
                    self.api._batch_breath(len(idx["ads"]))

        summary = self.summary()
        if idx["creatives"] and not self.api.dry_run:
            opted_in, unverified = self.verify_creatives()
            if unverified:
                cli_print(f"⚠️ Meta не отдала настройки автоулучшений, проверить в Ads Manager: {unverified}")
            if opted_in:
                cli_print(f"🚫 У креативов включены автоулучшения, активацию не делаю: {opted_in}")
                summary["creatives_opt_in"] = opted_in
                self.errors.append({"stage": "verify_creatives", "key": "dof", "response": opted_in})
        if check_previews and not self.api.dry_run:
            bad = []
            for ad_key, ad_id in idx["ads"].items():
                result = self.api.check_previews(ad_id)
                self.state["previews"][ad_id] = result
                if not result.get("ok"):
                    bad.append(ad_key)
            self.save()
            summary["previews_failed"] = bad
            summary["preview_sheet"] = self.preview_sheet(created=True).get("image")
            if bad:
                cli_print(f"⚠️ Preview-гейт не прошли: {bad}. Активацию не делаю.")
                activate = False

        if activate and not self.errors:
            self.activate()
            summary = self.summary()
        elif activate:
            cli_print(f"⚠️ Есть ошибки ({len(self.errors)}), --activate пропущен; дерево остаётся за gate.")
        return summary

    def activate(self) -> None:
        idx = self.state["index"]
        gates: list[tuple[str, str]] = []
        created_campaigns = set(idx["campaigns"].values())
        for camp in self.spec.get("campaigns") or []:
            camp_key = camp.get("name") or camp.get("id")
            campaign_id = camp.get("id") or idx["campaigns"].get(camp_key)
            if campaign_id in created_campaigns:
                gates.append(("campaign", campaign_id))
            else:
                # адсеты внутри существующей (активной) кампании: gate на уровне адсета
                for adset in camp.get("adsets") or []:
                    adset_id = idx["adsets"].get(f"{camp_key} | {adset['name']}")
                    if adset_id:
                        gates.append(("adset", adset_id))
        for kind, entity_id in gates:
            res = self.api.activate(kind, entity_id)
            self.state.setdefault("activation", []).append({"type": kind, "id": entity_id, "ok": "error" not in res})
        mark_activated(self.state)
        self.state["activation_command"] = " ; ".join(f"activate {k} {i}" for k, i in gates)
        self.save()

    def preview_sheet(self, created: bool = False) -> dict[str, Any]:
        """Лист картинок превью: строка = объявление, колонки = плейсменты адсета.

        До создания (created=False) превью рисуются из спеки через act/generatepreviews:
        в кабинете не создаётся ничего, кроме загрузки медиа в библиотеку. Это лист,
        который агент смотрит до включения. После создания — по реальным объявлениям.
        """
        if not created:
            self.upload_media()
        idx = self.state["index"]
        items: list[dict[str, Any]] = []
        for camp in self.spec.get("campaigns") or []:
            camp_key = camp.get("name") or camp.get("id")
            for adset in camp.get("adsets") or []:
                formats = B.preview_formats_for(self.adset_params(camp, adset).get("targeting"))
                for ad in adset.get("ads") or []:
                    if created:
                        ad_id = idx["ads"].get(f"{camp_key} | {adset['name']} | {ad['name']}")
                        if not ad_id:
                            continue
                        source: Any = ad_id
                    else:
                        source = self.creative_params(camp, adset, ad)
                    items.append({"label": ad["name"], "source": source, "formats": formats})
        out = self.state_path.with_name(self.state_path.stem + "-previews.jpg")
        result = self.api.render_preview_sheet(items, out, account_id=self.account_id)
        self.state["preview_sheet"] = result.get("image")
        self.save()
        return result

    def verify_creatives(self) -> tuple[list[str], list[str]]:
        """Правило 13.09.2026: читаем у созданных креативов автоулучшения из Meta.
        Возвращает (включённые, непроверяемые — Meta не отдала настройки)."""
        opted_in, unverified = [], []
        for key, creative_id in self.state["index"]["creatives"].items():
            res = self.api._request("GET", creative_id, {"fields": "degrees_of_freedom_spec,contextual_multi_ads"}, is_write=False)
            if (res.get("contextual_multi_ads") or {}).get("enroll_status") == "OPT_IN":
                opted_in.append(f"{key}: contextual_multi_ads")
            features = ((res.get("degrees_of_freedom_spec") or {}).get("creative_features_spec") or {})
            if not features:
                unverified.append(key)
                continue
            on = sorted(k for k, v in features.items() if (v or {}).get("enroll_status") != "OPT_OUT")
            if on:
                opted_in.append(f"{key}: {on}")
        return opted_in, unverified

    def validate_only(self, quiet: bool = False) -> dict[str, Any]:
        """Серверная проверка спеки: campaign/adset/creative через validate_only."""
        self.upload_media()
        idx = self.state["index"]
        report: dict[str, Any] = {"campaigns": {}, "adsets": {}, "creatives": {}}
        for camp in self.spec.get("campaigns") or []:
            camp_key = camp.get("name") or camp.get("id")
            if not camp.get("id"):
                params = self.campaign_params(camp)
                params.update({"status": "PAUSED", "special_ad_categories": "[]"})
                params.setdefault("is_adset_budget_sharing_enabled", "false")
                res = self.api._request("POST", f"{self.account_id}/campaigns", params, validate_only=True)
                report["campaigns"][camp_key] = res
                if not quiet or not res.get("success"):
                    cli_print(f"{'✅' if res.get('success') else '❌'} campaign «{camp_key}»: {json.dumps(res, ensure_ascii=False)[:300]}")
            probe_campaign = camp.get("id") or idx["campaigns"].get(camp_key) or self.spec.get("validate_campaign_id")
            for adset in camp.get("adsets") or []:
                adset_key = f"{camp_key} | {adset['name']}"
                if probe_campaign:
                    params = self.adset_params(camp, adset)
                    if self.spec.get("validate_campaign_id") and not camp.get("id"):
                        # чужая кампания-пробник: убираем расписание, оно может быть в прошлом
                        params.pop("start_time", None)
                        params.pop("end_time", None)
                    res = self.api.validate_adset(probe_campaign, params, account_id=self.account_id)
                else:
                    res = {"skipped": "нет кампании для validate_only adset (validate_campaign_id или campaign.id)"}
                    cli_print(f"⚠️ adset «{adset_key}»: {res['skipped']}")
                report["adsets"][adset_key] = res
                for ad in adset.get("ads") or []:
                    ad_key = f"{adset_key} | {ad['name']}"
                    res = self.api.validate_creative(self.account_id, self.creative_params(camp, adset, ad))
                    report["creatives"][ad_key] = res
        ok = all(
            r.get("success") or r.get("skipped")
            for group in report.values() for r in group.values()
        )
        report["ok"] = ok
        self.state.setdefault("verify_results", []).append({"validate_only": report, "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
        self.save()
        return report

    def summary(self) -> dict[str, Any]:
        idx = self.state["index"]
        return {
            "state": str(self.state_path),
            "account_id": self.account_id,
            "scenario": self.scenario,
            "campaigns": idx["campaigns"],
            "adsets": len(idx["adsets"]),
            "creatives": len(idx["creatives"]),
            "ads": len(idx["ads"]),
            "errors": len(self.errors),
            "activated": bool(self.state.get("activated")),
            "activation_command": self.state.get("activation_command"),
        }


def load_spec(path: str) -> dict[str, Any]:
    spec_file = Path(path).resolve()
    spec = json.loads(spec_file.read_text(encoding="utf-8"))
    spec["_spec_file"] = str(spec_file)
    # относительные пути медиа считаются от папки спеки
    for item in (spec.get("media") or {}).values():
        for key in ("file", "thumbnail"):
            if isinstance(item, dict) and item.get(key) and not os.path.isabs(os.path.expanduser(item[key])):
                item[key] = str(spec_file.parent / item[key])
            elif isinstance(item, dict) and item.get(key):
                item[key] = os.path.expanduser(item[key])
    return spec


def main() -> None:
    parser = argparse.ArgumentParser(description="Meta launch from JSON spec")
    parser.add_argument("--spec", required=True)
    parser.add_argument("--state", help="путь state-файла (по умолчанию ~/.local/share/meta-ads-manager/state/<client>/…)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--validate-only", action="store_true", help="только серверная проверка, без создания")
    parser.add_argument("--no-validate", action="store_true", help="пропустить серверную проверку перед созданием")
    parser.add_argument("--activate", action="store_true")
    parser.add_argument("--check-previews", action="store_true",
                        help="после создания: проверка превью на ошибки + лист картинок превью")
    parser.add_argument("--preview-only", action="store_true",
                        help="лист картинок превью из спеки до запуска; кампании/объявления не создаются")
    parser.add_argument("--json", action="store_true", help="печатать итог как JSON")
    args = parser.parse_args()

    spec = load_spec(args.spec)
    api = MetaAdsAPI(dry_run=args.dry_run)
    state_path = Path(args.state) if args.state else default_state_path(
        spec.get("client", "unknown"), B.canonical_scenario(spec.get("scenario")), spec.get("label")
    )
    if args.dry_run and not args.state:
        state_path = state_path.with_suffix(".dry.json")
    state = load_state(state_path) if state_path.exists() else None
    if state:
        cli_print(f"↩️ Продолжаю по state {state_path}")

    launcher = Launcher(spec, api, state_path, state)
    issues = launcher.validate_local()
    if issues:
        cli_print("🚫 Локальная проверка спеки не прошла:")
        for issue in issues:
            cli_print(f"  - {issue}")
        sys.exit(2)
    cli_print(f"✅ Локальная схема ок: scenario={launcher.scenario}, account={launcher.account_id}, state={state_path}")
    for warning in launcher.naming_warnings():
        cli_print(f"⚠️ имя: {warning}")

    try:
        if args.preview_only:
            result = launcher.preview_sheet()
        elif args.validate_only:
            result = launcher.validate_only()
        else:
            result = launcher.run(activate=args.activate, check_previews=args.check_previews,
                                  validate=not args.no_validate)
    except LaunchError as exc:
        cli_print(f"🚫 {exc}")
        launcher.save()
        sys.exit(2)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        cli_print("\n📋 Итог: " + json.dumps(result, ensure_ascii=False))
    sys.exit(0 if not launcher.errors and result.get("ok", True) else 2)


if __name__ == "__main__":
    main()
