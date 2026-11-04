#!/usr/bin/env python3
"""
meta_api.py — безопасная обёртка для write-операций Meta Marketing API.

Все write-операции (бюджеты, кампании, адсеты, креативы, объявления)
идут ТОЛЬКО через этот модуль. Прямой curl по write-эндпоинтам запрещён.

Встроенная защита:
- Без фиксированных пауз: лимиты Meta (Full access) на порядки выше нашего темпа
- Лимит бюджетных изменений: макс 3 на адсет в час (официальный лимит Meta — 4)
- Staging-gate статусы: при создании с нуля выключена кампания, при дубле
  внутри активной кампании выключен только адсет; дочерние объявления можно
  готовить ACTIVE за выключенным родителем
- Логирование каждого write-запроса
- Usage Header tracking + warning при >70% лимита
- HARD STOP при фактическом 429 / User request limit reached
- validate_only: сухая проверка adset/creative на стороне Meta без создания сущностей

CLI:
  python3 meta_api.py budget <adset_id> <amount_cents>
  python3 meta_api.py budget <campaign|adset> <entity_id> <amount_cents>
  python3 meta_api.py pause <campaign|adset|ad> <entity_id>
  python3 meta_api.py activate <campaign|adset|ad> <entity_id>
  python3 meta_api.py delete <campaign|adset|ad> <entity_id>
  python3 meta_api.py create-campaign <account_id> <params_json>
  python3 meta_api.py create-adset <campaign_id> <params_json>
  python3 meta_api.py create-creative <account_id> <params_json>
  python3 meta_api.py validate-adset <campaign_id> <params_json>        # validate_only, ничего не создаёт
  python3 meta_api.py validate-creative <account_id> <params_json>      # validate_only, ничего не создаёт
  python3 meta_api.py validate-ctwa <campaign_id> <page_id> <phone>     # проверка связки Page ↔ WhatsApp
  python3 meta_api.py whatsapp-numbers [--business=<id>]                # номера WABA по бизнес-порталам (read-only)
  python3 meta_api.py image-crops <file> [--focus=top|center|bottom|0.35] [--keys=100x100,191x100]
  python3 meta_api.py ensure-page-backed-instagram <page_id>
  python3 meta_api.py preview-sheet <ad_id[,ad_id…]> [--out=/abs/sheet.jpg]  # картинки превью одним листом
  python3 meta_api.py ig-media <instagram_user_id> [limit]               # id публикаций для ig_profile_post
  python3 meta_api.py insights <act|campaign|adset id> [--preset=last_7d] [--level=campaign]  # расход и результаты
  python3 meta_api.py upload-image <account_id> <file_path>
  python3 meta_api.py upload-video <account_id> <file_path>
  python3 meta_api.py create-ad <adset_id> <creative_id> <name> [--account-id=<act>] [--status=ACTIVE|PAUSED]
  python3 meta_api.py replace-creative <ad_id> <account_id> <image_path> [--no-crop]
  python3 meta_api.py duplicate-adset <adset_id> <new_name> <new_budget_cents> [--video-id=<vid>] [--activate]
  python3 meta_api.py copy-adset-native <adset_id> <new_name> <new_budget_cents> [--campaign-id=<cid>] [--activate]
  python3 meta_api.py create-whatsapp-from-template ...                 # legacy fallback, см. docstring метода
  python3 meta_api.py copy-campaign-native <campaign_id> <new_name> [--activate]
  python3 meta_api.py duplicate-campaign-chunked <campaign_id> <new_name> [--activate]
  python3 meta_api.py batch-pause <campaign|adset|ad> <id1,id2,id3>
  python3 meta_api.py batch-activate <campaign|adset|ad> <id1,id2,id3>
  python3 meta_api.py update <entity_id> <params_json>
  python3 meta_api.py status <campaign|adset|ad> <entity_id>
  python3 meta_api.py log [n]

  Полный запуск дерева campaign → adsets → creatives → ads из JSON-спеки:
  python3 meta_launch.py --spec <spec.json> [--activate] [--validate-only] [--dry-run]

  Добавь --dry-run к любой команде для тестового прогона без реальных запросов.
"""

import sys
import os
import re
import html
import json
import time
import logging
try:
    import fcntl  # Linux/macOS; на Windows без WSL блокировка бюджета внутри одного процесса
except ImportError:  # pragma: no cover
    fcntl = None
import requests
from datetime import datetime, timedelta
from pathlib import Path

# ── Конфиг и пути ─────────────────────────────────────
from config import META_TOKEN, GRAPH_VERSION, LOGS_DIR
import meta_launch_builders as builders

LOG_FILE = LOGS_DIR / "meta_api_writes.log"
BUDGET_STATE_FILE = LOGS_DIR / "meta_api_budget_state.json"
USAGE_STATE_FILE = LOGS_DIR / "meta_api_usage_state.json"

# v26.0 — последняя (29.07.2026), v24.0 отключается 06.10.2026, v25.0 — до объявления Meta.
# Explore снят в v26 (на всех версиях с 27.10.2026), _guard_placements его вычищает.
# Если у клиента появится Shop: на v26 креатив по умолчанию ведёт в WEBSITE_AND_SHOP.
API_VERSION = GRAPH_VERSION
BASE_URL = f"https://graph.facebook.com/{API_VERSION}"

# Preview-гейт: форматы, которые обязаны открываться без ошибки перед запуском.
PREVIEW_FORMATS = ("MOBILE_FEED_STANDARD", "INSTAGRAM_STANDARD", "INSTAGRAM_STORY", "INSTAGRAM_REELS")
PREVIEW_FORMAT_LABELS = {
    "INSTAGRAM_STANDARD": "IG лента", "INSTAGRAM_STORY": "IG stories", "INSTAGRAM_REELS": "IG reels",
    "MOBILE_FEED_STANDARD": "FB лента", "FACEBOOK_STORY_MOBILE": "FB stories",
    "FACEBOOK_REELS_MOBILE": "FB reels", "THREADS_STREAM": "Threads",
}
PREVIEW_ERROR_MARKERS = (
    "ad asset feed invalid", "asset feed invalid target rule count",
    "this ad can't be previewed", "this preview isn't available",
    "preview is not available", "couldn't load preview",
)

# ── Лимиты ───────────────────────────────────────────
# Документация Marketing API → Rate Limiting (сверено 04.10.2026). Full access: кабинет —
# 9000 очков за 5 минут (write = 3, т.е. ~3000 изменений), BUC ads_management — 100000 + 40 ×
# активных объявлений в час, мутации — 100 запросов/сек, бюджет адсета — 4 смены в час.
# Limited access (новое приложение): 60 очков за 5 минут, блок 300 с — см. references/app-setup.md.
# Фиксированных пауз Meta не требует: write идут по одному, это ≤1–2 в секунду.
# Защита — стоп на реальной ошибке лимита и бюджетный лимит ниже Meta.
WRITE_DELAY_SEC = 0          # было 4 до 10.09.2026, 1 до 04.10.2026; документацией не требуется
BUDGET_MAX_PER_HOUR = 3      # макс изменений бюджета на адсет в час (Meta: 4, дальше блок на час)
USAGE_THROTTLE_PCT = 70      # только warning; автосон отключён, чтобы не делать ложную "защиту секундами"
USAGE_HARD_STOP_PCT = 95      # СТОП: не продолжаем API-вызовы при >=95% usage
USAGE_HARD_STOP_COOLDOWN_SEC = 30 * 60  # защита между процессами/сессиями
RATE_LIMIT_COOLDOWN_SEC = 5 * 60        # лимит кабинета: Meta блокирует на 60 с (Full) или 300 с (Limited)
BUDGET_LIMIT_COOLDOWN_SEC = 60 * 60     # 1487632: бюджет адсета заблокирован Meta на час
MAX_RETRIES = 3              # макс ретраев для network/5xx; rate limit не ретраим
# Коды и подкоды лимитов из документации Marketing API → Rate Limiting (сверено 04.10.2026):
# 4 app, 17 user/account (2446079), 32 page, 613 (1487742 вызовы кабинета, 1487632 бюджет
# адсета 4/час, 5044001 100 QPS мутаций, 1487225 создание объявлений по дневному лимиту),
# 1885172 смена spend_cap 10/день, 1504022/1504039 Insights, 800xx — BUC.
RATE_LIMIT_CODES = {4, 17, 32, 613, 80000, 80001, 80002, 80003, 80004, 80005, 80006, 80008, 80009, 80014}
RATE_LIMIT_SUBCODES = {2446079, 1487742, 1487632, 5044001, 1487225, 1885172, 1504022, 1504039}
VIDEO_POLL_INTERVAL = 5      # секунд между проверками video_status
VIDEO_POLL_MAX_WAIT = 120    # макс ожидание ready (секунд)
BATCH_HARD_CAP = 25          # макс ID в batch-команде: защита от ошибки человека, не лимит Meta
BATCH_GROUP_SIZE = 10        # после каждых N write в серии — передышка (выключена: 0 сек)
BATCH_GROUP_PAUSE_SEC = 0    # было 30, потом 5; документацией не требуется

# ── Логирование ───────────────────────────────────────
LOGS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)

write_logger = logging.getLogger("meta_api_writes")
write_logger.setLevel(logging.INFO)
_fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
write_logger.addHandler(_fh)

# stdout logger для CLI
cli_logger = logging.getLogger("meta_api_cli")
cli_logger.setLevel(logging.INFO)
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(logging.Formatter("%(message)s"))
cli_logger.addHandler(_sh)

# Поля которые безопасно логировать (ID, имена, статусы, суммы)
_LOG_SAFE_KEYS = {"status", "daily_budget", "lifetime_budget", "name", "campaign_id",
                  "adset_id", "creative_id", "optimization_goal", "billing_event",
                  "objective", "buying_type", "fields", "limit", "id"}


def _short(value, max_len=220):
    text = str(value or "")
    return text if len(text) <= max_len else text[:max_len] + "…"


def _summarize_meta_error(err):
    if not isinstance(err, dict):
        return ""
    parts = []
    if err.get("type"):
        parts.append(f"type={err.get('type')}")
    if err.get("code") is not None:
        parts.append(f"code={err.get('code')}")
    if err.get("error_subcode") is not None:
        parts.append(f"subcode={err.get('error_subcode')}")
    if err.get("error_user_title"):
        parts.append(f"user_title={_short(err.get('error_user_title'), 160)}")
    if err.get("error_user_msg"):
        parts.append(f"user_msg={_short(err.get('error_user_msg'), 260)}")
    if err.get("error_data"):
        try:
            error_data = err.get("error_data")
            if isinstance(error_data, str):
                error_data = json.loads(error_data)
            blame = error_data.get("blame_field_specs") or error_data.get("blame_field")
            if blame:
                parts.append(f"blame_field={_short(json.dumps(blame, ensure_ascii=False), 260)}")
        except (TypeError, json.JSONDecodeError):
            parts.append(f"error_data={_short(err.get('error_data'), 260)}")
    if err.get("fbtrace_id"):
        parts.append(f"fbtrace_id={err.get('fbtrace_id')}")
    if err.get("message"):
        parts.append(f"message={_short(err.get('message'), 320)}")
    return " ".join(parts)


def log_write(endpoint, method, params, response_code, response_body=""):
    # Минималистичный лог: только safe-ключи, без targeting/object_story_spec/текстов
    safe_params = {k: v for k, v in params.items()
                   if k in _LOG_SAFE_KEYS}
    # Для остальных ключей логируем только факт наличия
    redacted = [k for k in params if k not in _LOG_SAFE_KEYS and k != "access_token"]
    redacted_str = f" | redacted_fields={redacted}" if redacted else ""
    # Из response берём только id и success/error code
    resp_summary = ""
    try:
        rd = json.loads(response_body) if isinstance(response_body, str) and response_body.startswith("{") else {}
        if rd.get("id"):
            resp_summary = f"id={rd['id']}"
        elif rd.get("success"):
            resp_summary = "success=true"
        elif rd.get("error"):
            resp_summary = "error " + _summarize_meta_error(rd["error"])
        else:
            resp_summary = response_body[:80]
    except (json.JSONDecodeError, TypeError):
        resp_summary = str(response_body)[:80]
    write_logger.info(f"{method} {endpoint} | {json.dumps(safe_params, ensure_ascii=False)}{redacted_str} | {response_code} | {resp_summary}")

def cli_print(msg):
    cli_logger.info(msg)


class MetaUsageHardStop(RuntimeError):
    """Raised when Meta usage headers reach the hard-stop threshold."""


# ── Usage hard-stop state ─────────────────────────────

def _load_usage_state():
    if USAGE_STATE_FILE.exists():
        try:
            return json.loads(USAGE_STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_usage_state(pct, header=None, cooldown_sec=None):
    state = {
        "pct": int(pct or 0),
        "ts": time.time(),
        "hard_stop_pct": USAGE_HARD_STOP_PCT,
        "cooldown_sec": int(cooldown_sec or USAGE_HARD_STOP_COOLDOWN_SEC),
    }
    if header is not None:
        state["header"] = header if isinstance(header, str) else json.dumps(header, ensure_ascii=False)
    USAGE_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def _usage_hard_stop_message(pct, source="header", cooldown_sec=None):
    cooldown_sec = int(cooldown_sec or USAGE_HARD_STOP_COOLDOWN_SEC)
    return (
        f"API лимиты Meta: usage {pct}% >= {USAGE_HARD_STOP_PCT}% "
        f"({source}). Я остановлен и не делаю дальнейшие API-вызовы к Meta, чтобы не добить "
        f"кабинет до User request limit reached. Подожди минимум "
        f"{cooldown_sec // 60} минут или продолжай вручную в Ads Manager."
    )


# ── Budget rate limiter ───────────────────────────────

BUDGET_LOCK_FILE = LOGS_DIR / "meta_api_budget.lock"


def _with_budget_lock(fn):
    """Выполняет fn() под file lock чтобы два процесса не обошли лимит одновременно."""
    if fcntl is None:
        return fn()
    BUDGET_LOCK_FILE.touch(exist_ok=True)
    with open(BUDGET_LOCK_FILE, "r") as lockf:
        fcntl.flock(lockf, fcntl.LOCK_EX)
        try:
            return fn()
        finally:
            fcntl.flock(lockf, fcntl.LOCK_UN)


def _load_budget_state():
    if BUDGET_STATE_FILE.exists():
        try:
            return json.loads(BUDGET_STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}

def _save_budget_state(state):
    BUDGET_STATE_FILE.write_text(json.dumps(state, indent=2))

def _check_budget_limit(entity_key):
    """Проверить лимит под lock (без записи). Возвращает (ok, msg)."""
    def _check():
        state = _load_budget_state()
        now = datetime.now()
        key = str(entity_key)
        cutoff = (now - timedelta(hours=1)).isoformat()

        if key not in state:
            state[key] = []
        state[key] = [e for e in state[key] if e > cutoff]
        _save_budget_state(state)

        if len(state[key]) >= BUDGET_MAX_PER_HOUR:
            oldest = state[key][0]
            return False, f"Лимит: бюджет {entity_key} уже менялся {len(state[key])} раз за час. Следующее изменение доступно после {oldest[:19]}+1h"
        return True, ""

    return _with_budget_lock(_check)


def _record_budget_change(entity_key):
    """Записать успешное изменение бюджета под lock. Вызывать ТОЛЬКО после успешного POST."""
    def _record():
        state = _load_budget_state()
        key = str(entity_key)
        now = datetime.now()
        cutoff = (now - timedelta(hours=1)).isoformat()

        if key not in state:
            state[key] = []
        state[key] = [e for e in state[key] if e > cutoff]
        state[key].append(now.isoformat())
        _save_budget_state(state)

    _with_budget_lock(_record)


# ── Валидация ─────────────────────────────────────────

VALID_ENTITY_TYPES = {"campaign", "adset", "ad"}

def _validate_entity_type(entity_type):
    """Проверяет entity_type. Возвращает (ok, msg)."""
    if entity_type not in VALID_ENTITY_TYPES:
        return False, f"Неизвестный entity_type: '{entity_type}'. Допустимые: {', '.join(sorted(VALID_ENTITY_TYPES))}"
    return True, ""


# ── Базовый API клиент ────────────────────────────────

class MetaAdsAPI:
    """Безопасная обёртка Meta Marketing API с принудительными задержками и лимитами."""

    def __init__(self, token=None, dry_run=False):
        self.token = token or META_TOKEN
        self.dry_run = dry_run
        self._last_usage_pct = 0
        # Кэш status/account_id родителей внутри одного процесса: при запуске
        # 6 адсетов × 1 объявление это экономит ~12 GET. Сбрасывается при любом
        # write по этой сущности (pause/activate/update).
        self._entity_cache = {}

    def _forget(self, entity_id):
        self._entity_cache.pop(str(entity_id), None)

    @staticmethod
    def _batch_breath(writes_done):
        """Передышка после каждых BATCH_GROUP_SIZE write в серии (при 0 сек выключена)."""
        if BATCH_GROUP_PAUSE_SEC > 0 and writes_done and writes_done % BATCH_GROUP_SIZE == 0:
            cli_print(f"🛡 {writes_done} write подряд — пауза {BATCH_GROUP_PAUSE_SEC} сек")
            time.sleep(BATCH_GROUP_PAUSE_SEC)

    def _request(self, method, endpoint, params=None, files=None, is_write=True, validate_only=False):
        """Базовый запрос с задержкой, логированием, backoff.

        validate_only=True добавляет execution_options=["validate_only"]: Meta прогоняет
        полную серверную валидацию и отвечает {"success": true} или обычной ошибкой,
        но ничего не создаёт. Это не write, паузы после него нет.
        """
        url = f"{BASE_URL}/{endpoint}" if not endpoint.startswith("http") else endpoint
        params = params or {}
        params["access_token"] = self.token
        if validate_only:
            params["execution_options"] = '["validate_only"]'
            is_write = False
            method_label = "VALIDATE"
        else:
            method_label = method

        # Важно: не блокируем новые процессы по cached usage_state глобально.
        # Meta usage/rate-limit может быть scoped по app/business-use-case/ad-account,
        # а один ответ User request limit reached не означает, что все кабинеты
        # или даже этот кабинет заблокированы на час. Реальный stop делаем только
        # на фактический HTTP 429 / rate-limit ответ текущего запроса.

        if self.dry_run:
            safe = {k: v for k, v in params.items() if k != "access_token"}
            cli_print(f"[DRY-RUN] {method_label} {url}")
            cli_print(f"  params: {json.dumps(safe, ensure_ascii=False, indent=2)}")
            log_write(endpoint, method_label, params, "DRY-RUN", "dry run - no request sent")
            if validate_only:
                return {"success": True, "dry_run": True, "validate_only": True}
            # Фейковый ответ с минимальной структурой для корректной работы цепочек
            return {
                "success": True, "id": "dry_run_id_12345", "dry_run": True,
                "account_id": "act_dry_run", "campaign_id": "dry_run_campaign",
                "name": "dry_run",
                "data": [{"creative": {"id": "dry_run_creative_id"}}],
                "object_story_spec": {"video_data": {"video_id": "dry_run_vid"}},
                "targeting": {"publisher_platforms": ["instagram"], "instagram_positions": ["stream"]},
                "optimization_goal": "CONVERSATIONS",
                "billing_event": "IMPRESSIONS", "daily_budget": "600",
                "status": "PAUSED",
                "thumbnails": {"data": [{"uri": "https://example.com/thumb.jpg", "is_preferred": True}]},
            }

        last_error = None
        for attempt in range(MAX_RETRIES):
            try:
                if method == "GET":
                    r = requests.get(url, params=params, timeout=60)
                elif method == "POST":
                    if files:
                        # для загрузки файлов params идут в data
                        r = requests.post(url, data=params, files=files, timeout=120)
                    else:
                        r = requests.post(url, data=params, timeout=60)
                else:
                    raise ValueError(f"Unsupported method: {method}")

                body = r.text[:4000]
                log_write(endpoint, method_label, params, r.status_code, body)

                # Usage Header tracking. Логируем ответ ДО hard-stop, чтобы write не потерялся.
                usage_header = r.headers.get("x-business-use-case-usage") or r.headers.get("x-app-usage")
                if usage_header:
                    self._parse_usage(usage_header)

                # 429 = уже упёрлись в лимит. Не ретраим, чтобы не добивать usage.
                if r.status_code == 429:
                    msg = _usage_hard_stop_message(
                        USAGE_HARD_STOP_PCT,
                        source="HTTP 429",
                        cooldown_sec=RATE_LIMIT_COOLDOWN_SEC,
                    )
                    cli_print(f"🚫 {msg}")
                    return {"error": msg, "blocked": True, "usage_hard_stop": True, "rate_limit": True}

                # HTTP 5xx можно ретраить: это серверная ошибка, не явный лимит.
                if r.status_code >= 500:
                    wait = 2 ** (attempt + 1)
                    cli_print(f"⚠️ HTTP {r.status_code} (attempt {attempt+1}/{MAX_RETRIES}). Жду {wait} сек...")
                    time.sleep(wait)
                    last_error = f"HTTP {r.status_code}"
                    continue

                data = r.json()

                # Meta API rate limit ошибки (расширенный список)
                error = data.get("error", {})
                error_code = error.get("code", 0)
                error_subcode = error.get("error_subcode", 0)
                message = error.get("message", "").lower()
                # Только официальные коды и точные фразы: подстрока "rate" ловила
                # "generate"/"separate" и останавливала запуск на час по обычной ошибке.
                is_rate_limit = (
                    error_code in RATE_LIMIT_CODES or
                    error_subcode in RATE_LIMIT_SUBCODES or
                    "too many calls" in message or
                    "request limit reached" in message
                )
                # 613/4841018 — не лимит приложения, а «1 запрос за 30 с» к одному
                # объекту: так Meta отвечает на правку только что скопированной
                # группы (usage кабинета при этом ~1%). Ждём и повторяем.
                if error_subcode == 4841018 and attempt < MAX_RETRIES - 1:
                    cli_print("⏳ Meta: не чаще 1 запроса за 30 с к этому объекту (свежая копия). Жду 31 с и повторяю...")
                    time.sleep(31)
                    last_error = error.get("message")
                    continue
                if is_rate_limit:
                    msg = _usage_hard_stop_message(
                        USAGE_HARD_STOP_PCT,
                        source=f"rate limit code={error_code} sub={error_subcode}",
                        cooldown_sec=BUDGET_LIMIT_COOLDOWN_SEC if error_subcode == 1487632 else RATE_LIMIT_COOLDOWN_SEC,
                    )
                    cli_print(f"🚫 {msg}")
                    return {"error": msg, "blocked": True, "usage_hard_stop": True, "rate_limit": True, "meta_error": error}

                if is_write and WRITE_DELAY_SEC > 0:
                    time.sleep(WRITE_DELAY_SEC)

                return data

            except requests.exceptions.RequestException as e:
                log_write(endpoint, method_label, params, "EXCEPTION", str(e))
                last_error = str(e)
                if attempt < MAX_RETRIES - 1:
                    time.sleep(2 ** (attempt + 1))
                continue

        # Все network/5xx ретраи исчерпаны (rate limit выше останавливается сразу)
        cli_print(f"❌ Все {MAX_RETRIES} попытки исчерпаны. Последняя ошибка: {last_error}")
        return {"error": last_error}

    def _parse_usage(self, header):
        """Парсит оба формата: x-app-usage (плоский) и x-business-use-case-usage (вложенный BUC)."""
        try:
            data = json.loads(header) if isinstance(header, str) else header
            pct = 0

            if isinstance(data, dict):
                # Формат 1: x-app-usage — плоский {call_count, total_cputime, total_time}
                if "call_count" in data or "total_cputime" in data or "total_time" in data:
                    pct = max(
                        data.get("call_count", 0),
                        data.get("total_cputime", 0),
                        data.get("total_time", 0),
                    )
                else:
                    # Формат 2: x-business-use-case-usage — {account_id: [{type, call_count, ...}]}
                    # Берём максимум по всем аккаунтам и всем BUC внутри
                    for _account_id, buc_list in data.items():
                        if isinstance(buc_list, list):
                            for buc in buc_list:
                                if isinstance(buc, dict):
                                    pct = max(pct,
                                              buc.get("call_count", 0),
                                              buc.get("total_cputime", 0),
                                              buc.get("total_time", 0))
                                    # BUC может содержать estimated_time_to_regain_access
                                    if buc.get("estimated_time_to_regain_access", 0) > 0:
                                        pct = max(pct, 95)  # почти заблокированы

            self._last_usage_pct = pct

            # Header usage сам по себе не является надёжным основанием для hard-stop:
            # Ads Manager может показывать remaining, а BUC header — текущую нагрузку;
            # плюс лимит scoped не глобально. Поэтому здесь только предупреждаем.
            # Жёстко останавливаемся выше только на фактический 429 / rate-limit error.
            if pct > USAGE_THROTTLE_PCT:
                cli_print(f"⚠️ Usage header {pct}% > {USAGE_THROTTLE_PCT}%: продолжаю; hard stop только на фактический rate-limit ответ Meta")
        except (json.JSONDecodeError, TypeError):
            pass

    # ── Read (безопасные) ─────────────────────────────

    def get_status(self, entity_type, entity_id):
        """Read back the fields that matter after a write."""
        ok, msg = _validate_entity_type(entity_type)
        if not ok:
            cli_print(f"🚫 {msg}")
            return {"error": msg, "blocked": True}
        fields = "id,name,status,effective_status"
        if entity_type == "campaign":
            fields += ",account_id,objective,daily_budget,lifetime_budget,bid_strategy,special_ad_categories"
        elif entity_type == "adset":
            fields += (
                ",account_id,campaign_id,daily_budget,lifetime_budget,optimization_goal,"
                "billing_event,bid_strategy,targeting,destination_type,promoted_object,attribution_spec"
            )
        elif entity_type == "ad":
            fields += (
                ",account_id,campaign_id,adset_id,"
                "creative{id,object_story_spec,asset_feed_spec,effective_object_story_id}"
            )

        return self._request("GET", f"{entity_id}", {"fields": fields}, is_write=False)

    def _readback(self, entity_type, entity_id, operation):
        """Read and report the resulting external state without changing it."""
        state = self.get_status(entity_type, entity_id)
        if "error" in state:
            cli_print(f"⚠️ Readback failed after {operation}: {json.dumps(state, ensure_ascii=False)}")
        else:
            cli_print(f"🔎 Readback after {operation}: {entity_type} {entity_id}")
        return state

    # ── Budget ────────────────────────────────────────

    def update_budget(self, entity_id, amount_cents, entity_type="adset"):
        """Изменить daily_budget campaign/adset. Лимит: 3 раза в час на сущность.
        Слот записывается ТОЛЬКО после успешного POST."""
        if entity_type not in {"campaign", "adset"}:
            msg = "budget поддерживает только campaign или adset; на уровне ad бюджет inherited read-only"
            cli_print(f"🚫 БЛОК: {msg}")
            return {"error": msg, "blocked": True}

        budget_key = f"{entity_type}:{entity_id}"
        ok, msg = _check_budget_limit(budget_key)
        if not ok:
            cli_print(f"🚫 БЛОК: {msg}")
            return {"error": msg, "blocked": True}

        self._forget(entity_id)
        result = self._request("POST", str(entity_id), {
            "daily_budget": str(amount_cents),
        })

        if result.get("success") or result.get("dry_run"):
            # Записываем слот ТОЛЬКО после реального успешного POST, dry-run лимит не тратит
            if not result.get("dry_run"):
                _record_budget_change(budget_key)
            cli_print(f"✅ Бюджет {entity_type} {entity_id} → ${int(amount_cents)/100:.0f}/день")
            result["readback"] = self._readback(entity_type, entity_id, "budget")
        else:
            cli_print(f"❌ Ошибка (слот НЕ потрачен): {json.dumps(result, ensure_ascii=False)}")

        return result

    # ── Pause / Activate ──────────────────────────────

    def pause(self, entity_type, entity_id):
        """Поставить на паузу."""
        ok, msg = _validate_entity_type(entity_type)
        if not ok:
            cli_print(f"🚫 {msg}")
            return {"error": msg, "blocked": True}
        self._forget(entity_id)
        result = self._request("POST", str(entity_id), {"status": "PAUSED"})
        if result.get("success") or result.get("dry_run"):
            cli_print(f"⏸ {entity_type} {entity_id} → PAUSED")
            result["readback"] = self._readback(entity_type, entity_id, "pause")
        else:
            cli_print(f"❌ Ошибка: {json.dumps(result, ensure_ascii=False)}")
        return result

    def delete(self, entity_type, entity_id):
        """Удалить конкретно указанную сущность Meta через статус DELETED."""
        ok, msg = _validate_entity_type(entity_type)
        if not ok:
            cli_print(f"🚫 {msg}")
            return {"error": msg, "blocked": True}
        self._forget(entity_id)
        result = self._request("POST", str(entity_id), {"status": "DELETED"})
        if result.get("success") or result.get("dry_run"):
            cli_print(f"🗑 {entity_type} {entity_id} → DELETED")
            result["readback"] = self._readback(entity_type, entity_id, "delete")
        else:
            cli_print(f"❌ Ошибка: {json.dumps(result, ensure_ascii=False)}")
        return result

    def activate(self, entity_type, entity_id, check_parents=True, name=None):
        """Включить (ACTIVE) указанную сущность и нужных неактивных родителей."""
        ok, msg = _validate_entity_type(entity_type)
        if not ok:
            cli_print(f"🚫 {msg}")
            return {"error": msg, "blocked": True}
        # Собираем closure set: все объекты которые будут активированы.
        # Для staged/batch launch caller может передать check_parents=False после отдельной
        # проверки/активации campaign, чтобы не делать повторные safety GET на каждый ad/adset.
        if check_parents:
            closure = self._build_activation_closure(entity_type, entity_id)
        else:
            closure = [{"type": entity_type, "id": entity_id, "name": name or "?", "auto": False}]

        # Показываем полный scope активации
        if len(closure) > 1:
            cli_print(f"📋 Полный scope активации ({len(closure)} объектов):")
            for item in closure:
                marker = " (авто — родитель)" if item.get("auto") else ""
                cli_print(f"  ▶️ {item['type']} {item['id']} «{item.get('name', '?')}»{marker}")

        # Активируем: сначала родителей (сверху вниз), потом сам объект
        results = []
        readbacks = []
        for item in closure:
            self._forget(item["id"])
            result = self._request("POST", str(item["id"]), {"status": "ACTIVE"})
            if result.get("success") or result.get("dry_run"):
                marker = " (авто)" if item.get("auto") else ""
                cli_print(f"▶️ {item['type']} {item['id']} → ACTIVE{marker}")
                readbacks.append({
                    "type": item["type"],
                    "id": str(item["id"]),
                    "state": self._readback(item["type"], item["id"], "activate"),
                })
            else:
                cli_print(f"❌ Ошибка {item['type']} {item['id']}: {json.dumps(result, ensure_ascii=False)}")
            results.append(result)

        if not results:
            return {"error": "empty closure"}
        final = results[-1]
        if readbacks:
            final["readback"] = readbacks[-1]["state"]
            final["activation_readbacks"] = readbacks
        return final

    def _build_activation_closure(self, entity_type, entity_id):
        """Собрать полный closure set: объект + все родители которых надо включить.
        Возвращает список в порядке активации (родители первые)."""
        closure = []
        try:
            if entity_type == "ad":
                info = self._request("GET", str(entity_id),
                    {"fields": "adset_id,campaign_id,name"}, is_write=False)
                campaign_id = info.get("campaign_id")
                adset_id = info.get("adset_id")
                if campaign_id:
                    camp = self._request("GET", str(campaign_id), {"fields": "status,name"}, is_write=False)
                    if camp.get("status") == "PAUSED":
                        closure.append({"type": "campaign", "id": campaign_id, "name": camp.get("name", "?"), "auto": True})
                if adset_id:
                    adset = self._request("GET", str(adset_id), {"fields": "status,name"}, is_write=False)
                    if adset.get("status") == "PAUSED":
                        closure.append({"type": "adset", "id": adset_id, "name": adset.get("name", "?"), "auto": True})
                closure.append({"type": "ad", "id": entity_id, "name": info.get("name", "?"), "auto": False})

            elif entity_type == "adset":
                info = self._request("GET", str(entity_id),
                    {"fields": "campaign_id,name"}, is_write=False)
                campaign_id = info.get("campaign_id")
                if campaign_id:
                    camp = self._request("GET", str(campaign_id), {"fields": "status,name"}, is_write=False)
                    if camp.get("status") == "PAUSED":
                        closure.append({"type": "campaign", "id": campaign_id, "name": camp.get("name", "?"), "auto": True})
                closure.append({"type": "adset", "id": entity_id, "name": info.get("name", "?"), "auto": False})

            else:
                info = self._request("GET", str(entity_id), {"fields": "name"}, is_write=False)
                closure.append({"type": entity_type, "id": entity_id, "name": info.get("name", "?"), "auto": False})

        except Exception as e:
            cli_print(f"⚠️ Не удалось собрать closure set: {e} (активирую только запрошенный объект)")
            closure = [{"type": entity_type, "id": entity_id, "name": "?", "auto": False}]

        return closure

    # ── Create Campaign ───────────────────────────────

    def create_pixel(self, account_id, name):
        """Create an account pixel through the standard logged, rate-limited writer."""
        account_id = str(account_id)
        if not account_id.startswith("act_"):
            account_id = f"act_{account_id}"
        return self._request("POST", f"{account_id}/adspixels", {"name": name})

    def create_campaign(self, account_id, params):
        """Создать кампанию. params — dict с name, objective, special_ad_categories и т.д."""
        if not account_id.startswith("act_"):
            account_id = f"act_{account_id}"

        # ЖЁСТКО: всегда PAUSED, перезаписываем любое значение
        params["status"] = "PAUSED"
        # ЖЁСТКО: никогда не ставить EMPLOYMENT и др. — лишние ограничения таргетинга
        params["special_ad_categories"] = "[]"
        params.setdefault("is_adset_budget_sharing_enabled", "false")

        result = self._request("POST", f"{account_id}/campaigns", params)
        if "id" in result:
            cli_print(f"✅ Кампания создана: {result['id']} (PAUSED)")
        else:
            cli_print(f"❌ Ошибка: {json.dumps(result, ensure_ascii=False)}")
        return result

    # ── Create Adset ──────────────────────────────────

    @staticmethod
    def _serialize_nested(params):
        """Авто-сериализация вложенных dict/list в JSON-строки для Meta API.
        Meta API ожидает promoted_object, targeting, object_story_spec, asset_feed_spec,
        creative и т.д. как JSON-строки, а не как вложенные объекты."""
        for key in list(params.keys()):
            if isinstance(params[key], (dict, list)):
                params[key] = json.dumps(params[key])
        return params

    @staticmethod
    def _guard_targeting_expansion(params):
        """Never allow Meta to expand manually selected age/gender/geo targeting."""
        targeting = params.get("targeting")
        if isinstance(targeting, str):
            try:
                targeting = json.loads(targeting)
            except json.JSONDecodeError:
                return
        if not isinstance(targeting, dict):
            return

        manual_dimensions = []
        if "age_min" in targeting or "age_max" in targeting:
            manual_dimensions.append("age")
        if targeting.get("genders") is not None:
            manual_dimensions.append("gender")

        geo_locations = targeting.get("geo_locations") or {}
        if any(geo_locations.get(k) for k in ("cities", "custom_locations", "regions", "countries", "zips")):
            manual_dimensions.append("geo")

        # С v23 Meta требует явный advantage_audience при создании адсета; без него
        # аудитория расширяется по умолчанию. Всегда ставим 0, если caller не задал.
        automation = targeting.setdefault("targeting_automation", {})
        if automation.get("advantage_audience") is None:
            automation["advantage_audience"] = 0
        params["targeting"] = targeting
        if not manual_dimensions:
            return

        automation["advantage_audience"] = 0
        individual = automation.setdefault("individual_setting", {})
        changed = []
        for dimension in sorted(set(manual_dimensions)):
            if individual.get(dimension) != 0:
                changed.append(dimension)
            individual[dimension] = 0
        if changed:
            cli_print(
                "🛡 Manual targeting detected — forcing "
                f"targeting_automation.individual_setting {changed}=0 (no Advantage expansion)"
            )
        params["targeting"] = targeting

    @staticmethod
    def _guard_placements(params):
        """Плейсменты перед create/validate адсета.

        Explore Meta сняла в v26 (29.07.2026; на всех версиях с 27.10.2026) — вычищаем молча.
        Audience Network, Marketplace и автоплейсменты запрещены методом — блок.
        Площадки не заданы → META_DEFAULT_PLATFORMS (по умолчанию Instagram во всех позициях + Threads).
        """
        targeting = MetaAdsAPI._decode_maybe_json(params.get("targeting"))
        if not isinstance(targeting, dict):
            return None
        if builders.apply_default_placements(targeting, threads=params.get("destination_type") != "INSTAGRAM_PROFILE"):
            cli_print(f"📍 площадки не заданы → {targeting['publisher_platforms']}, Instagram во всех позициях")
        removed = builders.strip_removed_placements(targeting)
        if removed:
            cli_print(f"🧹 убраны instagram_positions {removed}: Meta сняла Explore")
        if "facebook" in (targeting.get("publisher_platforms") or []):
            cli_print("⚠️ Facebook в площадках: метод ставит его только по прямой просьбе владельца кабинета")
        params["targeting"] = targeting
        violations = builders.placement_violations(targeting)
        if violations:
            msg = "запрещённые площадки: " + "; ".join(violations)
            cli_print(f"🚫 БЛОК: {msg}")
            return {"error": msg, "blocked": True}
        return None

    @staticmethod
    def _guard_bid_strategy(params):
        """Avoid Meta asking for a bid amount on common conversion/conversation goals."""
        goal = params.get("optimization_goal")
        if goal in {"OFFSITE_CONVERSIONS", "CONVERSATIONS"} and not params.get("bid_strategy") and not params.get("bid_amount"):
            params["bid_strategy"] = "LOWEST_COST_WITHOUT_CAP"
            cli_print(f"🛡 optimization_goal={goal}: setting bid_strategy=LOWEST_COST_WITHOUT_CAP")

    @staticmethod
    def _decode_maybe_json(value):
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        return value

    @staticmethod
    def _guard_creative_schema(params):
        """Block creative schemas that repeatedly create UI/editor issues."""
        asset_feed = MetaAdsAPI._decode_maybe_json(params.get("asset_feed_spec"))
        object_story = MetaAdsAPI._decode_maybe_json(params.get("object_story_spec"))

        # Meta с 2026-08 не принимает обобщённый standard_enhancements при create
        # (code 100, subcode 3858504): нужны только отдельные creative_features.
        # Ключ часто приезжает из прочитанных старых креативов при дубле.
        dof = MetaAdsAPI._decode_maybe_json(params.get("degrees_of_freedom_spec"))
        if isinstance(dof, dict):
            features = dof.get("creative_features_spec")
            if isinstance(features, dict) and "standard_enhancements" in features:
                features.pop("standard_enhancements", None)
                cli_print("🧹 degrees_of_freedom_spec: убран deprecated standard_enhancements")

        # Правило метода: автоизменения креатива всегда выключены, в любом пути
        # создания (лаунчер, replace, дубли, шаблон, сырой create-creative). Отсутствие
        # поля Meta трактует как включение, поэтому базовый OPT_OUT ставим всегда, а
        # переданные функции могут быть только OPT_OUT.
        given = (dof or {}).get("creative_features_spec") if isinstance(dof, dict) else None
        given = given if isinstance(given, dict) else {}
        opted_in = sorted(k for k, v in given.items() if (v or {}).get("enroll_status") != "OPT_OUT")
        if opted_in:
            msg = f"автоулучшения креатива запрещены методом, а включены: {opted_in}"
            cli_print(f"🚫 БЛОК: {msg}")
            return {"error": msg, "blocked": True}
        merged = builders.advantage_creative_opt_out()
        merged["creative_features_spec"].update(given)
        if not given:
            cli_print("🛡 degrees_of_freedom_spec не задан: ставлю OPT_OUT всех автоулучшений")
        params["degrees_of_freedom_spec"] = merged

        # Мультиобъявления (показ в подборке с рекламой других компаний) выключены методом.
        # Без поля Meta включает их всем объявлениям (с 19.08.2024).
        multi = MetaAdsAPI._decode_maybe_json(params.get("contextual_multi_ads")) or {}
        if isinstance(multi, dict) and multi.get("enroll_status") == "OPT_IN":
            msg = "мультиобъявления запрещены методом: contextual_multi_ads OPT_IN"
            cli_print(f"🚫 БЛОК: {msg}")
            return {"error": msg, "blocked": True}
        params["contextual_multi_ads"] = {"enroll_status": "OPT_OUT"}

        if isinstance(object_story, dict):
            video_data = object_story.get("video_data") or {}
            if isinstance(video_data, dict) and video_data.get("image_hash") and video_data.get("image_url"):
                # Legacy creatives can be read back with both thumbnail fields even
                # though current create endpoints accept exactly one of them.
                video_data.pop("image_url", None)
                object_story["video_data"] = video_data
                params["object_story_spec"] = object_story
                cli_print("🧹 Legacy video thumbnail normalized: keeping image_hash, dropping image_url")

            link_data = object_story.get("link_data") or {}
            cta = link_data.get("call_to_action") or {}
            cta_value = cta.get("value") or {}
            if isinstance(cta_value, dict) and "page_welcome_message" in cta_value:
                msg = "page_welcome_message must be placed in link_data, not call_to_action.value"
                cli_print(f"🚫 БЛОК: {msg}")
                return {"error": msg, "blocked": True}
            # Проверено 10.09.2026: link_data.image_crops Meta применяет и к Reels/Stories
            # (в Reels показалась обрезка 4:5 с полосами вместо 9:16). Placement-specific
            # кадрирование только через asset_feed_spec + asset_customization_rules.
            if link_data.get("image_crops") and not params.pop("allow_link_data_crops", False):
                msg = (
                    "link_data.image_crops обрезает картинку и в Reels/Stories. Для 9:16 со смешанными "
                    "плейсментами убрать image_crops (Meta сама кропает ленту по центру) или собрать "
                    "asset_feed_spec с placement_image_assets. Обход: allow_link_data_crops=true."
                )
                cli_print(f"🚫 БЛОК: {msg}")
                return {"error": msg, "blocked": True}

        if not isinstance(asset_feed, dict):
            return None

        cta_types = asset_feed.get("call_to_action_types") or []
        ctas = asset_feed.get("call_to_actions") or []
        has_whatsapp_cta = "WHATSAPP_MESSAGE" in cta_types or any(
            isinstance(cta, dict) and cta.get("type") == "WHATSAPP_MESSAGE"
            for cta in ctas
        )
        has_images = bool(asset_feed.get("images"))
        has_videos = bool(asset_feed.get("videos"))
        # Explicit placement adaptations keep one ad and one fixed copy/CTA.
        # They are not dynamic creative: each image is selected by a placement rule.
        images = asset_feed.get("images") or []
        rules = asset_feed.get("asset_customization_rules") or []
        labels = {label.get("name") for image in images for label in image.get("adlabels", [])}
        fixed_placement_images = (
            asset_feed.get("optimization_type") == "PLACEMENT"
            # AUTOMATIC_FORMAT + age-only default rule is what Ads Manager itself saves;
            # the SINGLE_IMAGE variant opens in the editor with #1487212/#2016052/#1373054.
            and asset_feed.get("ad_formats") in (["SINGLE_IMAGE"], ["AUTOMATIC_FORMAT"])
            and len(images) == 2 and len(labels) == 2 and len(rules) == 2
            and {r.get("image_label", {}).get("name") for r in rules} == labels
            and sum(not set(r.get("customization_spec") or {}) - {"age_min", "age_max"} for r in rules) == 1
            and all(len(asset_feed.get(k) or []) == 1 for k in ("bodies", "titles", "link_urls"))
            and asset_feed.get("call_to_action_types") == ["WHATSAPP_MESSAGE"]
        )
        if has_whatsapp_cta and has_images and not has_videos and not fixed_placement_images:
            msg = (
                "Static CTWA creatives must use object_story_spec.link_data with "
                "call_to_action=WHATSAPP_MESSAGE; asset_feed_spec is reserved for "
                "native/template/dynamic CTWA."
            )
            cli_print(f"🚫 БЛОК: {msg}")
            return {"error": msg, "blocked": True}

        return None

    @staticmethod
    def _normalize_create_status(status):
        if status is None or status == "":
            return None
        normalized = str(status).upper()
        if normalized not in {"ACTIVE", "PAUSED"}:
            return None
        return normalized

    def _campaign_status(self, campaign_id):
        key = str(campaign_id)
        cached = self._entity_cache.get(key)
        if cached and "error" not in cached:
            return cached
        info = self._request(
            "GET",
            key,
            {"fields": "account_id,status,name"},
            is_write=False,
        )
        if "error" not in info:
            self._entity_cache[key] = info
        return info

    def _choose_adset_create_status(self, campaign_id, requested_status=None, campaign_info=None):
        """Выбрать статус нового адсета по staging-gate правилу.

        Если кампания PAUSED — адсет можно готовить ACTIVE за этой кампанией.
        Если кампания ACTIVE — новый адсет остаётся PAUSED до явной команды activate.
        """
        requested = self._normalize_create_status(requested_status)
        if requested_status is not None and not requested:
            msg = f"Некорректный status для create-adset: {requested_status}. Разрешены ACTIVE/PAUSED"
            cli_print(f"🚫 БЛОК: {msg}")
            return None, {"error": msg, "blocked": True}

        campaign_info = campaign_info or self._campaign_status(campaign_id)
        campaign_status = campaign_info.get("status")
        campaign_paused = campaign_status == "PAUSED"

        if requested == "ACTIVE" and not campaign_paused:
            msg = (
                "Нельзя создать ACTIVE адсет в ACTIVE кампании через API: он может начать тратить. "
                "Сначала нужен выключенный campaign-gate или создавай адсет PAUSED."
            )
            cli_print(f"🚫 БЛОК: {msg}")
            return None, {"error": msg, "blocked": True}

        if requested:
            return requested, None
        return ("ACTIVE" if campaign_paused else "PAUSED"), None

    def create_adset(self, campaign_id, params):
        """Создать адсет по staging-gate правилу.

        - parent campaign PAUSED → adset ACTIVE (запуск gated кампанией)
        - parent campaign ACTIVE → adset PAUSED (запуск gated адсетом)
        Явный status=ACTIVE разрешён только за PAUSED campaign.
        """
        self._guard_targeting_expansion(params)
        blocked = self._guard_placements(params)
        if blocked:
            return blocked
        self._guard_bid_strategy(params)

        requested_status = params.pop("status", None)
        camp = self._campaign_status(campaign_id)
        if "error" in camp:
            cli_print(f"❌ Не удалось получить campaign для create-adset: {json.dumps(camp, ensure_ascii=False)}")
            return camp

        # Определить account_id из campaign
        if "account_id" not in params and "account_id" in camp:
            params["account_id"] = camp["account_id"]

        account_id = params.pop("account_id", None)
        if not account_id:
            cli_print("❌ Не удалось определить account_id")
            return {"error": "account_id not found"}

        if not account_id.startswith("act_"):
            account_id = f"act_{account_id}"

        status, blocked = self._choose_adset_create_status(campaign_id, requested_status, camp)
        if blocked:
            return blocked

        params["campaign_id"] = str(campaign_id)
        params["status"] = status
        self._serialize_nested(params)

        result = self._request("POST", f"{account_id}/adsets", params)
        if "id" in result:
            result["requested_status"] = status
            result["campaign_status"] = camp.get("status")
            gate = "campaign" if camp.get("status") == "PAUSED" else "adset"
            result["launch_gate"] = gate
            cli_print(f"✅ Адсет создан: {result['id']} ({status}, launch gate: {gate})")
        else:
            cli_print(f"❌ Ошибка: {json.dumps(result, ensure_ascii=False)}")
        return result

    # ── Upload Image ─────────────────────────────────

    def upload_image(self, account_id, file_path):
        """Загрузить изображение. Возвращает {hash, url}."""
        if not account_id.startswith("act_"):
            account_id = f"act_{account_id}"

        if self.dry_run:
            cli_print(f"[DRY-RUN] upload image {file_path} → {account_id}")
            return {"hash": "dry_run_hash", "url": "https://example.com/img.jpg"}

        if not os.path.exists(file_path):
            cli_print(f"❌ Файл не найден: {file_path}")
            return {"error": f"File not found: {file_path}"}

        with open(file_path, "rb") as f:
            result = self._request("POST", f"{account_id}/adimages", {},
                                   files={"filename": (os.path.basename(file_path), f)})

        images = result.get("images", {})
        if images:
            img_data = list(images.values())[0]
            cli_print(f"✅ Изображение загружено: hash={img_data['hash']}")
            return {"hash": img_data["hash"], "url": img_data.get("url", "")}
        else:
            cli_print(f"❌ Ошибка загрузки: {json.dumps(result, ensure_ascii=False)}")
            return result

    # ── Replace Creative ─────────────────────────────

    def replace_creative(self, ad_id, account_id, file_path, auto_crop=True, focus="center"):
        """Заменить креатив в объявлении: upload image → read old creative → create new → update ad.
        Сохраняет текст, CTA, лид-форму, object_story_spec. Заменяет только изображение.
        auto_crop=True (по умолчанию): один hash, два image элемента — оригинал для stories/reels
        и image_crops 1:1 + 4:5 для feed; focus задаёт, где акцент (top/center/bottom/0..1)."""
        if not account_id.startswith("act_"):
            account_id = f"act_{account_id}"

        # 1. Upload image
        img = self.upload_image(account_id, file_path)
        if "error" in img:
            return img
        new_hash = img["hash"]

        # 1b. Рассчитать crop для feed (если auto_crop)
        feed_crop = None
        if auto_crop:
            try:
                from meta_launch_builders import CROP_KEYS_FEED, image_crops_for, image_size
                w, h = image_size(file_path)
                if h > w:
                    feed_crop = image_crops_for(w, h, focus, CROP_KEYS_FEED)
                    cli_print(f"📐 Auto-crop feed {list(CROP_KEYS_FEED)} из {w}x{h}, focus={focus}: {json.dumps(feed_crop)}")
                else:
                    cli_print(f"⚠️ Изображение {w}x{h} не вертикальное, crop не нужен")
            except Exception as e:
                cli_print(f"⚠️ Не удалось рассчитать crop ({e}), crop не применён")

        # 2. Read existing ad's creative
        ad_data = self._request("GET", str(ad_id),
                                {"fields": "name,creative{id,object_story_spec,asset_feed_spec,url_tags}"},
                                is_write=False)
        old_creative = ad_data.get("creative", {})
        ad_name = ad_data.get("name", "unknown")
        if not old_creative:
            cli_print(f"❌ Не удалось прочитать креатив объявления {ad_id}")
            return {"error": "Cannot read creative"}

        old_spec = old_creative.get("asset_feed_spec", {})
        obj_story = old_creative.get("object_story_spec", {})

        # 3. Build new creative
        if old_spec:
            # Dynamic creative с asset_feed_spec
            body_text = old_spec.get("bodies", [{}])[0].get("text", "")
            cta_types = old_spec.get("call_to_action_types", [])
            ctas = old_spec.get("call_to_actions", [])
            descriptions = [{"text": d.get("text", " ")} for d in old_spec.get("descriptions", [{"text": " "}])]
            link_urls = [{"website_url": lu.get("website_url", "http://fb.me/"),
                          "display_url": lu.get("display_url", "")}
                         for lu in old_spec.get("link_urls", [{"website_url": "http://fb.me/"}])]
            titles = [{"text": t.get("text", "")} for t in old_spec.get("titles", [{"text": ""}])]
            ad_formats = old_spec.get("ad_formats", ["SINGLE_IMAGE"])

            body_label = "body_new"
            link_label = "link_new"
            title_label = "title_new"

            if feed_crop:
                # Один hash, два image элемента: оригинал + crop для feed
                img_label_story = "img_story"
                img_label_feed = "img_feed"

                images = [
                    {"adlabels": [{"name": img_label_story}], "hash": new_hash},
                    {"adlabels": [{"name": img_label_feed}], "hash": new_hash, "image_crops": feed_crop},
                ]

                # Feed плейсменты → кропнутое изображение
                feed_placements = {
                    "publisher_platforms": ["facebook", "instagram"],
                    "facebook_positions": ["feed", "facebook_reels_overlay", "profile_feed",
                                           "notification", "instream_video"],
                    "instagram_positions": ["stream", "profile_feed"],
                }

                new_rules = [
                    {
                        "customization_spec": feed_placements,
                        "image_label": {"name": img_label_feed},
                        "body_label": {"name": body_label},
                        "link_url_label": {"name": link_label},
                        "title_label": {"name": title_label},
                        "priority": 1,
                    },
                    {
                        "customization_spec": {},
                        "image_label": {"name": img_label_story},
                        "body_label": {"name": body_label},
                        "link_url_label": {"name": link_label},
                        "title_label": {"name": title_label},
                        "priority": 2,
                    },
                ]
            else:
                # Одно изображение без crop
                img_label = "img_new"
                images = [{"adlabels": [{"name": img_label}], "hash": new_hash}]

                old_rules = old_spec.get("asset_customization_rules", [])
                if old_rules:
                    new_rules = []
                    for rule in old_rules:
                        new_rules.append({
                            "customization_spec": rule.get("customization_spec", {}),
                            "image_label": {"name": img_label},
                            "body_label": {"name": body_label},
                            "link_url_label": {"name": link_label},
                            "title_label": {"name": title_label},
                            "priority": rule.get("priority", 1)
                        })
                else:
                    new_rules = [
                        {"customization_spec": {}, "image_label": {"name": img_label},
                         "body_label": {"name": body_label}, "link_url_label": {"name": link_label},
                         "title_label": {"name": title_label}, "priority": 1}
                    ]
                if len(new_rules) < 2:
                    new_rules.append({
                        "customization_spec": {},
                        "image_label": {"name": img_label}, "body_label": {"name": body_label},
                        "link_url_label": {"name": link_label}, "title_label": {"name": title_label},
                        "priority": len(new_rules) + 1
                    })

            new_feed = {
                "images": images,
                "bodies": [{"adlabels": [{"name": body_label}], "text": body_text}],
                "call_to_action_types": cta_types,
                "call_to_actions": ctas,
                "descriptions": descriptions,
                "link_urls": [dict(lu, adlabels=[{"name": link_label}]) for lu in link_urls],
                "titles": [dict(t, adlabels=[{"name": title_label}]) for t in titles],
                "ad_formats": ad_formats,
                "asset_customization_rules": new_rules,
                "optimization_type": "PLACEMENT"
            }

            creative_params = {
                "name": f"{ad_name}-replaced-{time.strftime('%m%d')}",
                "object_story_spec": json.dumps(obj_story),
                "asset_feed_spec": json.dumps(new_feed)
            }
        else:
            # Simple creative — just object_story_spec with link_data
            cli_print("⚠️ Простой креатив (без asset_feed_spec). Создаю link_data.")
            link_data = obj_story.get("link_data", {})
            link_data["image_hash"] = new_hash
            obj_story["link_data"] = link_data
            creative_params = {
                "name": f"{ad_name}-replaced-{time.strftime('%m%d')}",
                "object_story_spec": json.dumps(obj_story)
            }

        if old_creative.get("url_tags"):  # иначе UTM объявления теряются при замене
            creative_params["url_tags"] = old_creative["url_tags"]

        # 4. Create new creative
        result = self.create_creative(account_id, creative_params)
        if "id" not in result:
            return result
        new_creative_id = result["id"]

        # 5. Update ad
        update_result = self.update_entity(ad_id, {"creative": json.dumps({"creative_id": new_creative_id})})
        if update_result.get("success"):
            cli_print(f"🎯 Объявление «{ad_name}» обновлено → креатив {new_creative_id}")
        return {"ad_id": ad_id, "creative_id": new_creative_id, "image_hash": new_hash}

    # ── Create Creative ───────────────────────────────

    def create_creative(self, account_id, params):
        """Создать креатив (object_story_spec)."""
        if not account_id.startswith("act_"):
            account_id = f"act_{account_id}"

        blocked = self._guard_creative_schema(params)
        if blocked:
            return blocked

        self._serialize_nested(params)
        result = self._request("POST", f"{account_id}/adcreatives", params)
        if "id" in result:
            cli_print(f"✅ Креатив создан: {result['id']}")
        else:
            cli_print(f"❌ Ошибка: {json.dumps(result, ensure_ascii=False)}")
        return result

    # ── Page-backed Instagram ────────────────────────

    def ensure_page_backed_instagram(self, page_id):
        """Вернуть/создать Page-backed Instagram actor для IG placements.

        В Ads Manager это режим Instagram profile → «Использовать Страницу
        Facebook». В Marketing API для Instagram placements всё равно нужен
        instagram_user_id, но им может быть Page-backed Instagram Account
        (PBIA), созданный от Facebook Page.
        """
        pages = self._request(
            "GET",
            "me/accounts",
            {"fields": "id,name,access_token", "limit": "200"},
            is_write=False,
        )
        page_token = None
        page_name = ""
        for page in pages.get("data", []):
            if str(page.get("id")) == str(page_id):
                page_token = page.get("access_token")
                page_name = page.get("name", "")
                break

        if not page_token:
            cli_print(f"❌ Page Access Token не найден для страницы {page_id}")
            return {"error": "page_access_token_not_found", "page_id": str(page_id)}

        page_api = MetaAdsAPI(token=page_token, dry_run=self.dry_run)
        existing = page_api._request(
            "GET",
            f"{page_id}/page_backed_instagram_accounts",
            {"fields": "id,username,name"},
            is_write=False,
        )
        data = existing.get("data") or []
        if data:
            actor = data[0]
            cli_print(f"✅ Page-backed Instagram уже есть: {actor.get('id')} ({page_name})")
            return {"id": actor.get("id"), "page_id": str(page_id), "page_name": page_name, "existing": True}

        created = page_api._request(
            "POST",
            f"{page_id}/page_backed_instagram_accounts",
            {},
            is_write=True,
        )
        if "id" in created:
            cli_print(f"✅ Page-backed Instagram создан: {created['id']} ({page_name})")
            return {"id": created["id"], "page_id": str(page_id), "page_name": page_name, "existing": False}

        cli_print(f"❌ Ошибка PBIA: {json.dumps(created, ensure_ascii=False)}")
        return created

    # ── Upload Video ──────────────────────────────────

    def upload_video(self, account_id, file_path):
        """Загрузить видео и дождаться ready. Возвращает {id, thumbnail_url}."""
        if not account_id.startswith("act_"):
            account_id = f"act_{account_id}"

        if self.dry_run:
            cli_print(f"[DRY-RUN] upload video {file_path} → {account_id}")
            return {"id": "dry_run_video_id", "thumbnail_url": "https://example.com/thumb.jpg"}

        if not os.path.exists(file_path):
            cli_print(f"❌ Файл не найден: {file_path}")
            return {"error": f"File not found: {file_path}"}

        with open(file_path, "rb") as f:
            result = self._request("POST", f"{account_id}/advideos", {},
                                   files={"source": (os.path.basename(file_path), f)})

        if "id" not in result:
            cli_print(f"❌ Ошибка загрузки видео: {json.dumps(result, ensure_ascii=False)}")
            return result

        video_id = result["id"]
        cli_print(f"📤 Видео загружено: {video_id}. Жду ready...")

        # Ждать video_status: ready
        elapsed = 0
        while elapsed < VIDEO_POLL_MAX_WAIT:
            time.sleep(VIDEO_POLL_INTERVAL)
            elapsed += VIDEO_POLL_INTERVAL

            status = self._request("GET", str(video_id),
                                   {"fields": "status"}, is_write=False)
            vs = status.get("status", {})
            if isinstance(vs, dict):
                video_status = vs.get("video_status", "processing")
            else:
                video_status = "processing"

            if video_status == "ready":
                cli_print(f"✅ Видео {video_id} ready ({elapsed} сек)")
                break
            elif video_status == "error":
                cli_print(f"❌ Видео {video_id} — ошибка обработки")
                return {"error": "Video processing failed", "id": video_id}
        else:
            cli_print(f"⚠️ Видео {video_id} не готово за {VIDEO_POLL_MAX_WAIT} сек. Проверь вручную.")

        # Получить thumbnail
        thumbs = self._request("GET", str(video_id),
                               {"fields": "thumbnails"}, is_write=False)
        thumbnail_url = ""
        thumbs_data = thumbs.get("thumbnails", {}).get("data", [])
        for t in thumbs_data:
            if t.get("is_preferred"):
                thumbnail_url = t.get("uri", "")
                break
        if not thumbnail_url and thumbs_data:
            thumbnail_url = thumbs_data[0].get("uri", "")

        return {"id": video_id, "thumbnail_url": thumbnail_url}

    def video_thumbnail(self, video_id):
        """Обложка, выбранная Meta для загруженного видео (is_preferred), иначе первая."""
        thumbs = self._request("GET", str(video_id), {"fields": "thumbnails"}, is_write=False)
        data = (thumbs.get("thumbnails") or {}).get("data") or []
        preferred = [t.get("uri", "") for t in data if t.get("is_preferred")]
        return (preferred or [t.get("uri", "") for t in data] or [""])[0]

    # ── Create Ad ─────────────────────────────────────

    def _choose_ad_create_status(self, adset_info, campaign_info=None, requested_status=None):
        """Выбрать статус нового объявления по staging-gate правилу.

        ACTIVE объявление безопасно готовить только если выше есть выключенный gate:
        PAUSED адсет или PAUSED кампания. Если оба родителя ACTIVE — объявление создаём
        PAUSED до явной команды activate.
        """
        requested = self._normalize_create_status(requested_status)
        if requested_status is not None and not requested:
            msg = f"Некорректный status для create-ad: {requested_status}. Разрешены ACTIVE/PAUSED"
            cli_print(f"🚫 БЛОК: {msg}")
            return None, {"error": msg, "blocked": True}, None

        adset_paused = adset_info.get("status") == "PAUSED"
        campaign_paused = False
        campaign_id = adset_info.get("campaign_id")
        if not adset_paused and campaign_id:
            campaign_info = campaign_info or self._campaign_status(campaign_id)
            campaign_paused = campaign_info.get("status") == "PAUSED"
        elif campaign_info:
            campaign_paused = campaign_info.get("status") == "PAUSED"

        has_gate = adset_paused or campaign_paused
        gate = "adset" if adset_paused else ("campaign" if campaign_paused else "ad")

        if requested == "ACTIVE" and not has_gate:
            msg = (
                "Нельзя создать ACTIVE объявление в ACTIVE адсете/кампании через API: оно может сразу начать тратить. "
                "Нужен выключенный adset/campaign-gate или создавай объявление PAUSED."
            )
            cli_print(f"🚫 БЛОК: {msg}")
            return None, {"error": msg, "blocked": True}, gate

        if requested:
            return requested, None, gate
        return ("ACTIVE" if has_gate else "PAUSED"), None, gate

    def create_ad(self, adset_id, creative_id, name, account_id=None, status=None):
        """Создать объявление по staging-gate правилу.

        - parent adset PAUSED или campaign PAUSED → ad ACTIVE
        - parent adset+campaign ACTIVE → ad PAUSED
        Явный status=ACTIVE разрешён только за выключенным родителем.
        """
        adset_info = self._entity_cache.get(str(adset_id))
        if not adset_info:
            adset_info = self._request(
                "GET",
                str(adset_id),
                {"fields": "account_id,status,name,campaign_id"},
                is_write=False,
            )
            if "error" in adset_info:
                cli_print(f"❌ Не удалось получить adset для create-ad: {json.dumps(adset_info, ensure_ascii=False)}")
                return adset_info
            self._entity_cache[str(adset_id)] = adset_info

        # account_id можно передать из caller'а, чтобы не делать лишний GET на каждый ad,
        # но adset status всё равно читаем для staging-gate защиты.
        if not account_id:
            account_id = adset_info.get("account_id")
        if not account_id:
            cli_print("❌ Не удалось определить account_id из адсета")
            return {"error": "account_id not found"}

        if not account_id.startswith("act_"):
            account_id = f"act_{account_id}"

        chosen_status, blocked, gate = self._choose_ad_create_status(adset_info, requested_status=status)
        if blocked:
            return blocked

        result = self._request("POST", f"{account_id}/ads", {
            "adset_id": str(adset_id),
            "creative": json.dumps({"creative_id": str(creative_id)}),
            "name": name,
            "status": chosen_status,
        })
        if "id" in result:
            result["requested_status"] = chosen_status
            result["launch_gate"] = gate
            cli_print(f"✅ Объявление создано: {result['id']} ({chosen_status}, launch gate: {gate})")
        else:
            cli_print(f"❌ Ошибка: {json.dumps(result, ensure_ascii=False)}")
        return result

    # ── Duplicate Adset ───────────────────────────────

    @staticmethod
    def _sanitize_asset_feed_spec(feed):
        """Подготовить asset_feed_spec из существующего креатива к повторному созданию.

        Meta возвращает в asset_feed_spec служебные id у adlabels/label refs и read-only поля.
        Если отправить их обратно как есть, создание креатива часто падает. Для дубля нужны
        сами ассеты/тексты/CTA и связи по label.name — id можно и нужно выкинуть.
        """
        if not feed:
            return {}

        allowed = {
            "images", "videos", "bodies", "call_to_action_types", "call_to_actions",
            "descriptions", "link_urls", "titles", "ad_formats",
            "asset_customization_rules", "optimization_type",
        }
        cleaned = json.loads(json.dumps({k: v for k, v in feed.items() if k in allowed}))

        def walk(value):
            if isinstance(value, dict):
                # label objects look like {"name":"...", "id":"..."}; id is not reusable
                if "name" in value and "id" in value:
                    value.pop("id", None)
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(cleaned)
        return cleaned

    def _replace_asset_feed_video(self, feed, new_video_id):
        """Заменить video_id во всех placement videos внутри asset_feed_spec."""
        if not feed or not new_video_id:
            return feed

        thumbs = self._request("GET", str(new_video_id), {"fields": "thumbnails"}, is_write=False)
        thumbs_data = thumbs.get("thumbnails", {}).get("data", [])
        thumbnail_url = ""
        for t in thumbs_data:
            if t.get("is_preferred"):
                thumbnail_url = t.get("uri", "")
                break
        if not thumbnail_url and thumbs_data:
            thumbnail_url = thumbs_data[0].get("uri", "")

        for video in feed.get("videos", []):
            video["video_id"] = str(new_video_id)
            if thumbnail_url:
                video["thumbnail_url"] = thumbnail_url
        return feed

    @staticmethod
    def _normalize_targeting_for_create(targeting):
        """Нормализовать старый targeting перед новым create.

        Meta иногда разрешает исторически существующие комбинации, но запрещает создать
        новый адсет с тем же JSON. Самый частый случай: снятые плейсменты Instagram
        explore/explore_home в старых адсетах.
        """
        targeting = json.loads(json.dumps(targeting or {}))
        builders.strip_removed_placements(targeting)
        return targeting

    def _staging_activate(self, entity_type, entity_id, name=None, index=None):
        """Активировать дочернюю сущность за выключенным родителем во время staging.

        Это НЕ запуск в эфир: caller обязан перед вызовом проверить, что выше есть
        PAUSED campaign/adset gate.
        """
        result = self._request("POST", str(entity_id), {"status": "ACTIVE"})
        if result.get("success") or result.get("dry_run"):
            prefix = f"{index}. " if index is not None else ""
            suffix = f" «{name}»" if name else ""
            cli_print(f"▶️ staging: {prefix}{entity_type} {entity_id}{suffix} → ACTIVE")
            result["readback"] = self._readback(entity_type, entity_id, "staging activate")
        else:
            cli_print(f"❌ Ошибка staging-активации {entity_type} {entity_id}: {json.dumps(result, ensure_ascii=False)}")
        return result

    def _activate_ads_behind_paused_adset(self, adset_id, ads_data=None):
        """Сделать объявления ACTIVE, пока сам адсет PAUSED.

        Используется после native copy адсета в активной кампании: запуск потом одним
        действием — activate adset.
        """
        adset = self._request("GET", str(adset_id), {"fields": "status,name"}, is_write=False)
        if adset.get("status") != "PAUSED":
            msg = f"adset {adset_id} status={adset.get('status')} — нельзя staging-активировать ads без PAUSED adset-gate"
            cli_print(f"🚫 БЛОК: {msg}")
            return {"error": msg, "blocked": True}

        if ads_data is None:
            ads = self._request("GET", f"{adset_id}/ads", {
                "fields": "id,name,status,effective_status",
                "limit": "50",
            }, is_write=False)
            ads_data = ads.get("data", [])

        activated = []
        for idx, ad in enumerate(ads_data, 1):
            ad_id = ad.get("id")
            if not ad_id or ad.get("status") == "ACTIVE":
                continue
            self._batch_breath(len(activated))
            res = self._staging_activate("ad", ad_id, name=ad.get("name"), index=idx)
            activated.append({"id": ad_id, "result": res})
        return {"activated_ads": activated, "gate": "adset", "adset_id": str(adset_id)}

    def _activate_campaign_children_behind_paused_campaign(self, campaign_id):
        """Сделать adsets/ads ACTIVE, пока campaign PAUSED.

        Используется при создании/дублировании кампании: запуск потом одним действием —
        activate campaign.
        """
        campaign = self._request("GET", str(campaign_id), {"fields": "status,name"}, is_write=False)
        if campaign.get("status") != "PAUSED":
            msg = f"campaign {campaign_id} status={campaign.get('status')} — нельзя staging-активировать детей без PAUSED campaign-gate"
            cli_print(f"🚫 БЛОК: {msg}")
            return {"error": msg, "blocked": True}

        adsets = self._request("GET", f"{campaign_id}/adsets", {
            "fields": "id,name,status,effective_status",
            "limit": "100",
        }, is_write=False)
        adsets_data = adsets.get("data", [])
        activated = []
        writes = 0

        for idx, adset in enumerate(adsets_data, 1):
            adset_id = adset.get("id")
            if not adset_id:
                continue
            if adset.get("status") != "ACTIVE":
                self._batch_breath(writes)
                res = self._staging_activate("adset", adset_id, name=adset.get("name"), index=idx)
                activated.append({"type": "adset", "id": adset_id, "result": res})
                writes += 1

            ads = self._request("GET", f"{adset_id}/ads", {
                "fields": "id,name,status,effective_status",
                "limit": "50",
            }, is_write=False)
            for ad in ads.get("data", []):
                ad_id = ad.get("id")
                if not ad_id or ad.get("status") == "ACTIVE":
                    continue
                self._batch_breath(writes)
                res = self._staging_activate("ad", ad_id, name=ad.get("name"))
                activated.append({"type": "ad", "id": ad_id, "result": res})
                writes += 1

        cli_print(f"🧷 Campaign-gate готов: детей ACTIVE, campaign {campaign_id} остаётся PAUSED. Writes: {writes}")
        return {"activated": activated, "gate": "campaign", "campaign_id": str(campaign_id), "writes": writes}

    def copy_adset_native(self, adset_id, new_name, new_budget_cents, target_campaign_id=None):
        """Нативный Meta copy endpoint: лучший путь для WhatsApp adset'ов.

        В отличие от ручного recreate, /copies переносит скрытую CTWA/WhatsApp-конфигурацию,
        которую Graph API не всегда отдаёт полностью. Статусы ставятся по staging-gate:
        если destination campaign PAUSED — копия может быть ACTIVE; если campaign ACTIVE —
        адсет остаётся PAUSED, а объявления активируются за ним.
        """
        cli_print(f"🧬 Native copy адсета {adset_id}...")

        source = self._request("GET", str(adset_id), {"fields": "campaign_id"}, is_write=False)
        if "error" in source:
            cli_print(f"❌ Не удалось получить source adset: {json.dumps(source, ensure_ascii=False)}")
            return source

        destination_campaign_id = str(target_campaign_id or source.get("campaign_id") or "")
        destination_campaign = self._campaign_status(destination_campaign_id) if destination_campaign_id else {}
        destination_campaign_paused = destination_campaign.get("status") == "PAUSED"
        status_option = "ACTIVE" if destination_campaign_paused else "PAUSED"
        launch_gate = "campaign" if destination_campaign_paused else "adset"

        params = {
            "deep_copy": "true",
            "status_option": status_option,
            "rename_options": json.dumps({
                "rename_strategy": "DEEP_RENAME",
                "suffix": " — copy"
            }),
        }
        if target_campaign_id:
            params["campaign_id"] = str(target_campaign_id)

        copied = self._request("POST", f"{adset_id}/copies", params)
        new_adset_id = copied.get("copied_adset_id") or copied.get("id") or copied.get("adset_id")
        if not new_adset_id:
            cli_print(f"❌ Native copy не вернул ID адсета: {json.dumps(copied, ensure_ascii=False)}")
            return copied

        cli_print(f"✅ Native copy создан: {new_adset_id} (status_option={status_option}, launch gate: {launch_gate})")

        rename = self.update_entity(new_adset_id, {"name": new_name})
        budget = self.update_budget(new_adset_id, new_budget_cents)
        if "error" in rename or "error" in budget:
            cli_print(f"⚠️ Copy создан, но rename/budget применились не полностью. Проверь адсет {new_adset_id}")
            return {"adset_id": new_adset_id, "copy": copied, "rename": rename, "budget": budget}

        ads = self._request("GET", f"{new_adset_id}/ads", {
            "fields": "id,name,status,effective_status",
            "limit": "50"
        }, is_write=False)
        ads_data = ads.get("data", [])
        ad_ids = [a.get("id") for a in ads_data if a.get("id")]

        staging = None
        if launch_gate == "adset":
            staging = self._activate_ads_behind_paused_adset(new_adset_id, ads_data)

        cli_print(f"\n🧬 Native copy готов! Адсет: {new_adset_id}, объявлений: {len(ad_ids)}")
        if launch_gate == "campaign":
            cli_print(f"🧷 Дети ACTIVE за PAUSED campaign. Для запуска: activate campaign {destination_campaign_id}")
        else:
            cli_print(f"🧷 Объявления ACTIVE за PAUSED adset. Для запуска: activate adset {new_adset_id}")

        readback = {
            "adset": self.get_status("adset", new_adset_id),
            "ads": [self.get_status("ad", ad_id) for ad_id in ad_ids],
        }

        return {
            "adset_id": new_adset_id,
            "campaign_id": destination_campaign_id,
            "ads": ad_ids,
            "status": status_option,
            "launch_gate": launch_gate,
            "staging": staging,
            "readback": readback,
        }

    @staticmethod
    def _copied_placement_violations(result):
        """Native /copies переносят targeting источника как есть, мимо _guard_placements:
        проверяем readback копий на запрещённые площадки."""
        result = result if isinstance(result, dict) else {}
        readback = result.get("readback") or {}
        adsets = [readback["adset"]] if isinstance(readback.get("adset"), dict) else []
        adsets += [a for a in readback.get("adsets") or [] if isinstance(a, dict)]
        for item in result.get("created") or []:
            adset = ((item or {}).get("readback") or {}).get("adset")
            if isinstance(adset, dict):
                adsets.append(adset)
        found = {}
        for adset in adsets:
            issues = builders.placement_violations(adset.get("targeting"))
            if issues:
                found[str(adset.get("id"))] = issues
        if found:
            cli_print(f"🚫 Копия унаследовала запрещённые площадки (запускать нельзя, править в Ads Manager или пересоздать): {json.dumps(found, ensure_ascii=False)}")
        return found

    def _activate_after_duplicate(self, result):
        """Activate a freshly copied tree when the same command requested --activate."""
        violations = self._copied_placement_violations(result)
        if violations:
            msg = f"копия с запрещёнными площадками не включается: {violations}"
            result["activation"] = {"error": msg, "blocked": True}
            result["activated"] = False
            return result
        gate = None
        entity_id = None
        if isinstance(result, dict):
            gate = result.get("launch_gate")
            if gate == "campaign":
                entity_id = result.get("campaign_id")
            elif gate == "adset":
                entity_id = result.get("adset_id")
        if gate not in VALID_ENTITY_TYPES or not entity_id:
            msg = "copy/duplicate result does not identify an activation target"
            cli_print(f"❌ {msg}")
            return {"error": msg, "result": result}
        activation = self.activate(gate, entity_id)
        result["activation"] = activation
        result["activated"] = "error" not in activation
        return result

    def create_whatsapp_from_template(self, template_adset_id, template_ad_id,
                                      campaign_name, adset_name, ad_name, budget_cents,
                                      new_video_id=None, account_id=None):
        """Создать WhatsApp/CTWA рекламу с нуля, используя рабочий adset/ad как шаблон.

        Создаёт новую campaign → adset → creative → ad. Campaign остаётся PAUSED,
        adset/ad готовятся ACTIVE за campaign-gate. Шаблон нужен не для дубля сущностей,
        а чтобы безопасно взять валидные Page/Instagram/WA binding, targeting и CTWA
        creative schema (`WHATSAPP_MESSAGE`).
        """
        if int(budget_cents) < 200:
            msg = "Meta minimum для WA adset обычно $2: budget_cents должен быть >= 200"
            cli_print(f"🚫 БЛОК: {msg}")
            return {"error": msg, "blocked": True}

        cli_print(f"🆕 Создание WhatsApp/CTWA рекламы с нуля по шаблону adset={template_adset_id}, ad={template_ad_id}...")

        template_adset = self._request("GET", str(template_adset_id), {
            "fields": "targeting,optimization_goal,billing_event,bid_strategy,"
                      "destination_type,promoted_object,account_id"
        }, is_write=False)
        if "error" in template_adset:
            cli_print(f"❌ Не удалось получить template adset: {json.dumps(template_adset, ensure_ascii=False)}")
            return template_adset

        if self.dry_run and not template_adset.get("destination_type"):
            template_adset.update({
                "destination_type": "WHATSAPP",
                "optimization_goal": "CONVERSATIONS",
                "billing_event": "IMPRESSIONS",
                "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
                "promoted_object": {"page_id": "dry_page_id", "smart_pse_enabled": False},
                "account_id": account_id or "act_dry_run",
                "targeting": {
                    "publisher_platforms": ["facebook", "instagram", "whatsapp"],
                    "instagram_positions": ["stream", "story", "reels", "explore_home"],
                    "device_platforms": ["mobile"],
                    "whatsapp_positions": ["status"],
                },
            })

        if template_adset.get("destination_type") != "WHATSAPP":
            msg = f"template adset destination_type={template_adset.get('destination_type')} вместо WHATSAPP"
            cli_print(f"🚫 БЛОК: {msg}")
            return {"error": msg, "blocked": True}

        if not account_id:
            account_id = template_adset.get("account_id", "")
        if not account_id:
            cli_print("❌ Не удалось определить account_id")
            return {"error": "account_id not found"}
        if not account_id.startswith("act_"):
            account_id = f"act_{account_id}"

        template_ad = self._request("GET", str(template_ad_id), {
            "fields": "creative{id,object_story_spec,asset_feed_spec}"
        }, is_write=False)
        creative = template_ad.get("creative", {})
        if self.dry_run and not creative:
            creative = {
                "object_story_spec": {"page_id": "dry_page_id", "instagram_user_id": "dry_ig_id"},
                "asset_feed_spec": {
                    "videos": [{"adlabels": [{"name": "video_main"}], "video_id": "dry_video_id", "thumbnail_url": "https://example.com/thumb.jpg"}],
                    "bodies": [{"adlabels": [{"name": "body_main"}], "text": "dry run whatsapp ad text"}],
                    "call_to_action_types": ["WHATSAPP_MESSAGE"],
                    "call_to_actions": [{"type": "WHATSAPP_MESSAGE", "value": {"app_destination": "WHATSAPP", "link": "https://api.whatsapp.com/send"}}],
                    "link_urls": [{"adlabels": [{"name": "link_main"}], "website_url": "https://api.whatsapp.com/send", "display_url": ""}],
                    "titles": [{"adlabels": [{"name": "title_main"}], "text": "dry run"}],
                    "ad_formats": ["AUTOMATIC_FORMAT"],
                    "optimization_type": "PLACEMENT",
                }
            }
        if not creative:
            cli_print(f"❌ Не удалось получить creative из template ad {template_ad_id}")
            return {"error": "template creative not found"}

        asset_feed = self._sanitize_asset_feed_spec(creative.get("asset_feed_spec", {}))
        if not asset_feed or "WHATSAPP_MESSAGE" not in asset_feed.get("call_to_action_types", []):
            msg = "template creative не содержит asset_feed_spec с WHATSAPP_MESSAGE"
            cli_print(f"🚫 БЛОК: {msg}")
            return {"error": msg, "blocked": True}
        if new_video_id and asset_feed.get("videos"):
            asset_feed = self._replace_asset_feed_video(asset_feed, new_video_id)

        targeting = self._normalize_targeting_for_create(template_adset.get("targeting", {}))
        targeting.setdefault("publisher_platforms", ["facebook", "instagram", "whatsapp"])
        targeting.setdefault("device_platforms", ["mobile"])
        if "whatsapp" in targeting.get("publisher_platforms", []) and "whatsapp_positions" not in targeting:
            targeting["whatsapp_positions"] = ["status"]

        # 1. Campaign
        campaign = self.create_campaign(account_id, {
            "name": campaign_name,
            "objective": "OUTCOME_ENGAGEMENT",
            "buying_type": "AUCTION",
        })
        if "id" not in campaign:
            return {"campaign": campaign, "error": "campaign create failed"}

        # 2. Adset
        adset_params = {
            "account_id": account_id,
            "name": adset_name,
            "daily_budget": str(budget_cents),
            "optimization_goal": "CONVERSATIONS",
            "billing_event": "IMPRESSIONS",
            "destination_type": "WHATSAPP",
            "promoted_object": json.dumps(template_adset.get("promoted_object", {})),
            "targeting": json.dumps(targeting),
        }
        if template_adset.get("bid_strategy"):
            adset_params["bid_strategy"] = template_adset["bid_strategy"]
        adset = self.create_adset(campaign["id"], adset_params)
        if "id" not in adset:
            return {"campaign": campaign, "adset": adset, "error": "adset create failed"}

        # 3. Creative
        new_creative = self.create_creative(account_id, {
            "name": f"{ad_name} — creative",
            "object_story_spec": json.dumps(creative.get("object_story_spec", {})),
            "asset_feed_spec": json.dumps(asset_feed),
        })
        if "id" not in new_creative:
            return {"campaign": campaign, "adset": adset, "creative": new_creative, "error": "creative create failed"}

        # 4. Ad
        ad = self.create_ad(adset["id"], new_creative["id"], ad_name, account_id=account_id)
        if "id" not in ad:
            return {"campaign": campaign, "adset": adset, "creative": new_creative, "ad": ad, "error": "ad create failed"}

        cli_print("\n🆕 WhatsApp/CTWA реклама создана с нуля!")
        cli_print(f"🧷 Campaign: {campaign['id']} PAUSED | Adset: {adset['id']} {adset.get('requested_status', '?')} | Creative: {new_creative['id']} | Ad: {ad['id']} {ad.get('requested_status', '?')}")
        cli_print("Запуск: activate campaign <id> после проверки показанного readback.")

        return {
            "campaign_id": campaign["id"],
            "adset_id": adset["id"],
            "creative_id": new_creative["id"],
            "ad_id": ad["id"],
            "status": "PAUSED_CAMPAIGN_GATE",
            "launch_gate": "campaign",
        }

    def duplicate_adset(self, adset_id, new_name, new_budget_cents, new_video_id=None, target_campaign_id=None):
        """Полный дубль: GET оригинал → создать адсет + креатив + объявления.

        Статусы по staging-gate: если destination campaign PAUSED — адсет/объявления
        ACTIVE и запуск gated кампанией; если campaign ACTIVE — адсет PAUSED, объявления
        ACTIVE и запуск gated адсетом.
        """
        cli_print(f"🔄 Дублирование адсета {adset_id}...")

        # 1. Получить оригинальный адсет
        original = self._request("GET", str(adset_id), {
            "fields": "targeting,optimization_goal,billing_event,bid_strategy,"
                      "destination_type,promoted_object,daily_budget,campaign_id,"
                      "account_id,name"
        }, is_write=False)

        if "error" in original:
            cli_print(f"❌ Не удалось получить адсет: {json.dumps(original, ensure_ascii=False)}")
            return original

        account_id = original.get("account_id", "")
        campaign_id = str(target_campaign_id or original.get("campaign_id", ""))
        if not account_id.startswith("act_"):
            account_id = f"act_{account_id}"

        # 2. Получить ВСЕ объявления из адсета (не только первое)
        ads = self._request("GET", f"{adset_id}/ads", {
            "fields": "creative{id},name",
            "limit": "50"
        }, is_write=False)

        ads_data = ads.get("data", [])
        if not ads_data:
            cli_print("❌ Нет объявлений в оригинальном адсете")
            return {"error": "No ads found in original adset"}

        cli_print(f"📋 Найдено объявлений в оригинале: {len(ads_data)}")

        # 3. Собрать все креативы
        original_creatives = []
        for ad in ads_data:
            cid = ad.get("creative", {}).get("id")
            ad_name = ad.get("name", "ad")
            if cid:
                creative_data = self._request("GET", str(cid), {
                    "fields": "object_story_spec,asset_feed_spec,name"
                }, is_write=False)
                oss = creative_data.get("object_story_spec", {})
                asset_feed = self._sanitize_asset_feed_spec(creative_data.get("asset_feed_spec", {}))

                # Подменить video_id если передан новый
                if new_video_id and "video_data" in oss:
                    oss["video_data"]["video_id"] = str(new_video_id)
                    thumbs = self._request("GET", str(new_video_id),
                                           {"fields": "thumbnails"}, is_write=False)
                    thumbs_data = thumbs.get("thumbnails", {}).get("data", [])
                    thumb_found = False
                    for t in thumbs_data:
                        if t.get("is_preferred"):
                            oss["video_data"]["image_url"] = t.get("uri", "")
                            thumb_found = True
                            break
                    if not thumb_found and thumbs_data:
                        oss["video_data"]["image_url"] = thumbs_data[0].get("uri", "")
                if new_video_id and asset_feed.get("videos"):
                    asset_feed = self._replace_asset_feed_video(asset_feed, new_video_id)

                original_creatives.append({
                    "object_story_spec": oss,
                    "asset_feed_spec": asset_feed,
                    "ad_name": ad_name,
                })

        if not original_creatives:
            cli_print("❌ Не удалось получить креативы из объявлений")
            return {"error": "No creatives found"}

        # 4. Создать новый адсет по staging-gate правилу
        adset_params = {
            "account_id": account_id,
            "name": new_name,
            "daily_budget": str(new_budget_cents),
            "optimization_goal": original.get("optimization_goal", ""),
            "billing_event": original.get("billing_event", ""),
            "targeting": self._normalize_targeting_for_create(original.get("targeting", {})),
        }
        if original.get("bid_strategy"):
            adset_params["bid_strategy"] = original["bid_strategy"]
        if original.get("destination_type"):
            adset_params["destination_type"] = original["destination_type"]
        if original.get("promoted_object"):
            adset_params["promoted_object"] = original["promoted_object"]

        new_adset = self.create_adset(campaign_id, adset_params)
        if "id" not in new_adset:
            cli_print(f"❌ Ошибка создания адсета: {json.dumps(new_adset, ensure_ascii=False)}")
            return new_adset
        new_adset_id = new_adset["id"]
        launch_gate = new_adset.get("launch_gate") or "adset"

        # 5-6. Создать креатив + объявление для КАЖДОГО оригинального объявления
        created_ads = []
        orphan_creatives = []  # креативы без привязанного объявления
        for i, orig_c in enumerate(original_creatives):
            creative_params = {
                "name": f"{new_name} — креатив {i+1}" if len(original_creatives) > 1 else f"{new_name} — креатив",
            "object_story_spec": json.dumps(orig_c["object_story_spec"]),
            }
            if orig_c.get("asset_feed_spec"):
                creative_params["asset_feed_spec"] = json.dumps(orig_c["asset_feed_spec"])
            new_creative = self.create_creative(account_id, creative_params)
            if "id" not in new_creative:
                cli_print(f"❌ Ошибка создания креатива {i+1}: {json.dumps(new_creative, ensure_ascii=False)}")
                continue
            cli_print(f"✅ Креатив {i+1}/{len(original_creatives)}: {new_creative['id']}")

            ad_name = f"{new_name} | {orig_c['ad_name']}" if len(original_creatives) > 1 else f"{new_name} | ad"
            new_ad = self.create_ad(new_adset_id, new_creative["id"], ad_name, account_id=account_id)
            if "id" in new_ad:
                cli_print(f"✅ Объявление {i+1}/{len(original_creatives)}: {new_ad['id']} ({new_ad.get('requested_status', '?')})")
                created_ads.append({"creative_id": new_creative["id"], "ad_id": new_ad["id"]})
            else:
                cli_print(f"❌ Ошибка создания объявления {i+1}: {json.dumps(new_ad, ensure_ascii=False)}")
                orphan_creatives.append(new_creative["id"])

        # Rollback: если ни одного объявления не создалось, удаляем всё
        if not created_ads:
            cli_print(f"⚠️ Ни одного объявления не создано. Останавливаю staged adset без DELETED rollback...")
            rollback = self.pause("adset", new_adset_id)
            if orphan_creatives:
                cli_print(f"⚠️ Осиротевшие креативы оставлены без удаления: {orphan_creatives}")
            return {
                "error": "No ads created; staged adset paused; orphan creatives left for review",
                "paused_adset": new_adset_id,
                "orphan_creatives": orphan_creatives,
                "rollback": rollback,
            }

        # Partial success: предупредить об orphan-сущностях
        if orphan_creatives:
            cli_print(f"⚠️ PARTIAL: {len(orphan_creatives)} креатив(а) без объявления: {orphan_creatives}")
            cli_print(f"  Эти креативы осиротели. Проверь и удали вручную если не нужны.")

        ad_ids = [a["ad_id"] for a in created_ads]
        cli_print(f"\n🔄 Дубль готов! Адсет: {new_adset_id}, Объявлений: {len(created_ads)}/{len(original_creatives)}")
        if launch_gate == "campaign":
            cli_print(f"🧷 Дети ACTIVE за PAUSED campaign. Для запуска: activate campaign {campaign_id}")
        else:
            cli_print(f"🧷 Объявления ACTIVE за PAUSED adset. Для запуска: activate adset {new_adset_id}")

        readback = {
            "adset": self.get_status("adset", new_adset_id),
            "ads": [self.get_status("ad", ad_id) for ad_id in ad_ids],
        }

        return {
            "adset_id": new_adset_id,
            "campaign_id": campaign_id,
            "ads": created_ads,
            "status": new_adset.get("requested_status"),
            "launch_gate": launch_gate,
            "readback": readback,
        }

    # ── Duplicate Campaign ───────────────────────────

    def copy_campaign_native(self, campaign_id, new_name):
        """Нативный Meta copy endpoint для целой кампании.

        Это первый и самый человеческий путь для дубля кампании с несколькими адсетами:
        один запрос Meta копирует дерево campaign → adsets → ads → creatives. Если Meta
        падает из-за старых/legacy креативов или слишком большого payload — использовать
        duplicate_campaign_chunked().
        """
        cli_print(f"🧬 Native copy кампании {campaign_id}...")
        params = {
            "deep_copy": "true",
            "status_option": "PAUSED",
            "rename_options": json.dumps({
                "rename_strategy": "DEEP_RENAME",
                "suffix": " — copy"
            }),
        }
        copied = self._request("POST", f"{campaign_id}/copies", params)
        new_campaign_id = copied.get("copied_campaign_id") or copied.get("id") or copied.get("campaign_id")
        if not new_campaign_id:
            cli_print(f"❌ Native campaign copy не вернул ID: {json.dumps(copied, ensure_ascii=False)}")
            return copied

        cli_print(f"✅ Кампания скопирована: {new_campaign_id} (PAUSED)")
        rename = self.update_entity(new_campaign_id, {"name": new_name})
        if "error" in rename:
            cli_print(f"⚠️ Copy создан, но rename не применился. Проверь кампанию {new_campaign_id}")

        staging = self._activate_campaign_children_behind_paused_campaign(new_campaign_id)

        adsets = self._request("GET", f"{new_campaign_id}/adsets", {
            "fields": "id,name,status,effective_status,daily_budget,lifetime_budget,targeting,destination_type,promoted_object",
            "limit": "100"
        }, is_write=False)
        adsets_data = adsets.get("data", [])
        ads = self._request("GET", f"{new_campaign_id}/ads", {
            "fields": "id,name,status,effective_status,adset_id,creative{id,object_story_spec,asset_feed_spec,effective_object_story_id}",
            "limit": "200",
        }, is_write=False)
        cli_print(f"\n🧬 Native copy кампании готов! Кампания: {new_campaign_id}, адсетов: {len(adsets_data)}")
        cli_print("🧷 Дети ACTIVE за PAUSED campaign. Readback готов; запуск — командой activate или флагом --activate.")
        return {
            "campaign_id": new_campaign_id,
            "adsets": adsets_data,
            "status": "PAUSED",
            "launch_gate": "campaign",
            "staging": staging,
            "readback": {
                "campaign": self.get_status("campaign", new_campaign_id),
                "adsets": adsets_data,
                "ads": ads.get("data", []),
            },
        }

    def duplicate_campaign_chunked(self, campaign_id, new_name):
        """Fallback для больших/legacy кампаний: создать кампанию один раз и копировать адсеты по одному.

        Решает старый баг, когда при ошибке на 3-5 адсете агент создавал новую кампанию заново.
        Тут кампания создаётся один раз; каждый адсет копируется отдельно. Если один адсет упал,
        уже созданные сущности остаются PAUSED, а результат честно помечается partial.
        """
        cli_print(f"🧩 Chunked дубль кампании {campaign_id}...")
        original = self._request("GET", str(campaign_id), {
            "fields": "account_id,name,objective,buying_type,daily_budget,lifetime_budget,bid_strategy"
        }, is_write=False)
        if "error" in original:
            cli_print(f"❌ Не удалось получить кампанию: {json.dumps(original, ensure_ascii=False)}")
            return original

        account_id = original.get("account_id", "")
        if not account_id.startswith("act_"):
            account_id = f"act_{account_id}"

        campaign_params = {
            "name": new_name,
            "buying_type": original.get("buying_type", "AUCTION"),
        }
        if original.get("objective"):
            campaign_params["objective"] = original["objective"]
        if original.get("bid_strategy"):
            campaign_params["bid_strategy"] = original["bid_strategy"]

        new_campaign = self.create_campaign(account_id, campaign_params)
        new_campaign_id = new_campaign.get("id")
        if not new_campaign_id:
            return new_campaign

        adsets = self._request("GET", f"{campaign_id}/adsets", {
            "fields": "id,name,daily_budget,lifetime_budget",
            "limit": "100"
        }, is_write=False)
        adsets_data = adsets.get("data", [])
        cli_print(f"📦 Найдено адсетов: {len(adsets_data)}")

        created = []
        failed = []
        for idx, adset in enumerate(adsets_data, 1):
            source_adset_id = adset.get("id")
            source_name = adset.get("name") or f"adset {idx}"
            budget = int(adset.get("daily_budget") or adset.get("lifetime_budget") or 0)
            if budget <= 0:
                budget = 200  # Meta minimum fallback; campaign budget may cover real spend
            cli_print(f"\n[{idx}/{len(adsets_data)}] Дублирую адсет {source_adset_id} → {source_name}")

            # Сначала пробуем native adset copy в новую кампанию, затем manual fallback.
            res = self.copy_adset_native(source_adset_id, source_name, budget, target_campaign_id=new_campaign_id)
            if "error" in res or not res.get("adset_id"):
                cli_print("↪️ Native adset copy не сработал, пробую manual duplicate-adset...")
                res = self.duplicate_adset(source_adset_id, source_name, budget, target_campaign_id=new_campaign_id)

            if res.get("adset_id"):
                created.append(res)
            else:
                failed.append({"source_adset_id": source_adset_id, "result": res})

        staging = self._activate_campaign_children_behind_paused_campaign(new_campaign_id)

        cli_print(f"\n🧩 Chunked дубль готов: кампания {new_campaign_id}, адсетов {len(created)}/{len(adsets_data)}")
        if failed:
            cli_print(f"⚠️ PARTIAL: упало адсетов: {len(failed)}. Кампанию НЕ пересоздавать; чинить только failed.")
        cli_print("🧷 Дети ACTIVE за PAUSED campaign. Запуск — командой activate или флагом --activate.")
        return {
            "campaign_id": new_campaign_id,
            "created": created,
            "failed": failed,
            "status": "PAUSED",
            "launch_gate": "campaign",
            "staging": staging,
            "readback": {"campaign": self.get_status("campaign", new_campaign_id)},
        }

    # ── Batch операции ────────────────────────────────

    def batch_pause(self, entity_type, entity_ids):
        """Массовая пауза с задержкой между каждым. Hard cap: 10."""
        ok, msg = _validate_entity_type(entity_type)
        if not ok:
            cli_print(f"🚫 {msg}")
            return {"error": msg, "blocked": True}
        if len(entity_ids) > BATCH_HARD_CAP:
            cli_print(f"🚫 БЛОК: {len(entity_ids)} > {BATCH_HARD_CAP} (hard cap). Разбей на пачки по {BATCH_HARD_CAP}.")
            return {"error": f"Batch cap exceeded: {len(entity_ids)} > {BATCH_HARD_CAP}", "blocked": True}
        results = []
        for eid in entity_ids:
            self._batch_breath(len(results))
            r = self.pause(entity_type, eid)
            results.append({"id": eid, "result": r})
        cli_print(f"\n⏸ Batch pause: {len(results)} {entity_type}(s)")
        return results

    def batch_activate(self, entity_type, entity_ids):
        """Массовое включение с задержкой между каждым. Hard cap: 10."""
        ok, msg = _validate_entity_type(entity_type)
        if not ok:
            cli_print(f"🚫 {msg}")
            return {"error": msg, "blocked": True}
        if len(entity_ids) > BATCH_HARD_CAP:
            cli_print(f"🚫 БЛОК: {len(entity_ids)} > {BATCH_HARD_CAP} (hard cap). Разбей на пачки по {BATCH_HARD_CAP}.")
            return {"error": f"Batch cap exceeded: {len(entity_ids)} > {BATCH_HARD_CAP}", "blocked": True}
        results = []
        for eid in entity_ids:
            self._batch_breath(len(results))
            r = self.activate(entity_type, eid)
            results.append({"id": eid, "result": r})
        cli_print(f"\n▶️ Batch activate: {len(results)} {entity_type}(s)")
        return results

    # ── Update (generic) ──────────────────────────────

    # Whitelist: ТОЛЬКО эти поля можно менять через update.
    # Всё остальное — через отдельные методы или с явным расширением whitelist.
    _UPDATE_ALLOWED_FIELDS = {
        "name",                   # переименование
        "end_time",               # дата окончания
        "start_time",             # дата старта
        "adset_schedule",         # расписание показов
        "pacing_type",            # тип распределения бюджета
        "attribution_spec",       # атрибуция
        "is_dynamic_creative",    # динамический креатив (флаг)
        "creative",               # замена креатива в объявлении
    }

    def update_entity(self, entity_id, params):
        """Обновить сущность. Только поля из whitelist.
        status → pause/activate. budget → budget command. targeting и прочее — через Ads Manager."""
        unknown = set(params.keys()) - self._UPDATE_ALLOWED_FIELDS
        if unknown:
            cli_print(f"🚫 БЛОК: поля {unknown} не в whitelist для update.")
            cli_print(f"  Разрешены: {', '.join(sorted(self._UPDATE_ALLOWED_FIELDS))}")
            cli_print(f"  status → pause / activate")
            cli_print(f"  daily_budget → budget <adset_id> <amount>")
            cli_print(f"  targeting/promoted_object/bid_strategy → через Ads Manager")
            return {"error": f"Fields not in whitelist: {unknown}", "blocked": True}
        if str(params.get("is_dynamic_creative", "false")).lower() == "true":
            msg = "is_dynamic_creative=true запрещён методом (Dynamic/Flexible Creative выключены)"
            cli_print(f"🚫 БЛОК: {msg}")
            return {"error": msg, "blocked": True}

        self._serialize_nested(params)
        self._forget(entity_id)
        result = self._request("POST", str(entity_id), params)
        if result.get("success") or result.get("dry_run"):
            cli_print(f"✅ Обновлено: {entity_id}")
            fields = ["id", "name", "status", "effective_status"]
            for key in params:
                if key == "creative":
                    fields.append("creative{id,object_story_spec,asset_feed_spec,effective_object_story_id}")
                elif key not in fields:
                    fields.append(key)
            result["readback"] = self._request(
                "GET",
                str(entity_id),
                {"fields": ",".join(fields)},
                is_write=False,
            )
        else:
            cli_print(f"❌ Ошибка: {json.dumps(result, ensure_ascii=False)}")
        return result

    # ── Validate-only (серверная проверка без создания) ───────────────

    def validate_adset(self, campaign_id, params, account_id=None):
        """Прогнать adset через Meta с execution_options=validate_only.

        Meta проверяет всё: схему, бюджет, таргетинг, promoted_object, связку
        Page ↔ WhatsApp — и отвечает {"success": true}, не создавая адсет.
        """
        params = dict(params)
        self._guard_targeting_expansion(params)
        blocked = self._guard_placements(params)
        if blocked:
            return blocked
        self._guard_bid_strategy(params)
        params.pop("status", None)
        account_id = params.pop("account_id", None) or account_id
        if not account_id:
            camp = self._campaign_status(campaign_id)
            if "error" in camp:
                return camp
            account_id = camp.get("account_id")
        if not account_id:
            return {"error": "account_id not found"}
        if not str(account_id).startswith("act_"):
            account_id = f"act_{account_id}"
        params["campaign_id"] = str(campaign_id)
        params["status"] = "PAUSED"
        self._serialize_nested(params)
        result = self._request("POST", f"{account_id}/adsets", params, validate_only=True)
        if result.get("success"):
            cli_print("✅ validate-only adset: Meta приняла схему (ничего не создано)")
        else:
            cli_print(f"❌ validate-only adset: {json.dumps(result, ensure_ascii=False)}")
        return result

    def validate_creative(self, account_id, params):
        """Прогнать креатив через Meta с validate_only (проверено 10.09.2026: креатив не создаётся)."""
        if not account_id.startswith("act_"):
            account_id = f"act_{account_id}"
        params = dict(params)
        blocked = self._guard_creative_schema(params)
        if blocked:
            return blocked
        self._serialize_nested(params)
        result = self._request("POST", f"{account_id}/adcreatives", params, validate_only=True)
        if result.get("success"):
            cli_print("✅ validate-only creative: Meta приняла схему (ничего не создано)")
        else:
            cli_print(f"❌ validate-only creative: {json.dumps(result, ensure_ascii=False)}")
        return result

    def validate_ctwa(self, campaign_id, page_id, whatsapp_phone_number, targeting=None):
        """Проверить связку Page ↔ WhatsApp-номер для CTWA без создания сущностей.

        Чужой/неподвязанный номер даёт subcode 1487246 «This WhatsApp phone number
        is not linked to your account». Нужна реальная campaign_id того же кабинета.
        """
        camp = self._request(
            "GET", str(campaign_id),
            {"fields": "account_id,name,status,daily_budget,lifetime_budget"},
            is_write=False,
        )
        if "error" in camp:
            return camp
        params = {
            "name": "validate-only ctwa binding",
            "optimization_goal": "CONVERSATIONS",
            "billing_event": "IMPRESSIONS",
            "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
            "destination_type": "WHATSAPP",
            "promoted_object": {
                "page_id": str(page_id),
                "whatsapp_phone_number": str(whatsapp_phone_number).lstrip("+"),
            },
            "attribution_spec": [{"event_type": "CLICK_THROUGH", "window_days": 1}],
            "targeting": targeting or {
                "geo_locations": {"countries": ["KZ"]}, "age_min": 18,
                "publisher_platforms": ["facebook", "instagram"],
                "facebook_positions": ["feed"], "instagram_positions": ["stream"],
            },
        }
        if not (camp.get("daily_budget") or camp.get("lifetime_budget")):
            params["daily_budget"] = "500"  # у CBO-кампании бюджет адсета передавать нельзя
        cli_print(f"🔎 validate-only CTWA: page {page_id} + WhatsApp {whatsapp_phone_number} в кампании {campaign_id} «{camp.get('name')}»")
        return self.validate_adset(campaign_id, params, account_id=camp.get("account_id"))

    # ── WhatsApp (read-only) ─────────────────────────

    def whatsapp_numbers(self, business_id=None):
        """Номера WhatsApp Business по бизнес-порталам, доступным токену.

        Поле Page.whatsapp_number пустое даже у рабочих страниц, поэтому единственный
        путь через API: business → WABA → phone_numbers. Номер из приложения
        WhatsApp Business виден как status=DISCONNECTED / platform_type=ON_PREMISE —
        для CTWA это нормально.
        """
        if business_id:
            businesses = [{"id": str(business_id), "name": ""}]
        else:
            businesses = self._request(
                "GET", "me/businesses", {"fields": "id,name", "limit": "100"}, is_write=False
            ).get("data", [])
        rows = []
        for business in businesses:
            for edge in ("owned_whatsapp_business_accounts", "client_whatsapp_business_accounts"):
                res = self._request(
                    "GET", f"{business['id']}/{edge}",
                    {"fields": "id,name,phone_numbers{id,display_phone_number,verified_name,status,platform_type}",
                     "limit": "50"},
                    is_write=False,
                )
                for waba in res.get("data", []) or []:
                    for phone in (waba.get("phone_numbers") or {}).get("data", []) or []:
                        rows.append({
                            "business_id": business["id"], "business": business.get("name", ""),
                            "waba_id": waba.get("id"), "waba": waba.get("name"),
                            "phone_id": phone.get("id"),
                            "phone": phone.get("display_phone_number"),
                            "promoted_object_value": re.sub(r"\D", "", phone.get("display_phone_number") or ""),
                            "verified_name": phone.get("verified_name"),
                            "status": phone.get("status"), "platform_type": phone.get("platform_type"),
                        })
        for row in rows:
            cli_print(
                f"📱 {row['phone']} → whatsapp_phone_number={row['promoted_object_value']} | "
                f"{row['verified_name']} | WABA {row['waba_id']} «{row['waba']}» | "
                f"бизнес {row['business_id']} «{row['business']}» | {row['status']}/{row['platform_type']}"
            )
        if not rows:
            cli_print("⚠️ Номеров не найдено: либо бизнес не виден токену, либо у WABA нет номеров")
        return rows

    # ── Preview gate (read-only) ─────────────────────

    def check_previews(self, ad_id, formats=PREVIEW_FORMATS):
        """Открыть iframe-превью объявления по форматам и поймать маркеры ошибок.

        ACTIVE в readback не доказывает, что креатив показывается: у asset_feed
        с пустыми правилами Meta отдаёт «Asset Feed Invalid Target Rule Count».
        """
        results = {}
        for fmt in formats:
            res = self._request("GET", f"{ad_id}/previews", {"ad_format": fmt}, is_write=False)
            body = ((res.get("data") or [{}])[0] or {}).get("body", "")
            match = re.search(r'src="([^"]+)"', body or "")
            if not match:
                results[fmt] = {"ok": False, "reason": "no iframe", "meta": res.get("error")}
                continue
            src = html.unescape(match.group(1))
            try:
                page = requests.get(src, timeout=30).text.lower()
            except requests.RequestException as exc:
                results[fmt] = {"ok": False, "reason": str(exc)}
                continue
            bad = [marker for marker in PREVIEW_ERROR_MARKERS if marker in page]
            results[fmt] = {"ok": not bad, "markers": bad, "src": src}
        ok_all = all(item.get("ok") for item in results.values())
        for fmt, item in results.items():
            mark = "✅" if item.get("ok") else "❌"
            cli_print(f"{mark} preview {fmt}: {item.get('markers') or item.get('reason') or 'ok'}")
        return {"ok": ok_all, "formats": results}

    # ── Preview sheet (read-only) ─────────────────────

    def _preview_src(self, source, fmt, account_id=None):
        """iframe превью: source — id объявления/креатива или dict-спека креатива.
        Спека идёт в act/generatepreviews: Meta рисует превью, ничего не создавая."""
        if isinstance(source, dict):
            spec = json.loads(json.dumps(source))
            video_data = (spec.get("object_story_spec") or {}).get("video_data") or {}
            if video_data.get("image_hash") and video_data.get("image_url"):
                video_data.pop("image_url")  # generatepreviews принимает только одно из двух
            res = self._request("GET", f"{account_id}/generatepreviews",
                                {"creative": json.dumps(spec), "ad_format": fmt}, is_write=False)
        else:
            res = self._request("GET", f"{source}/previews", {"ad_format": fmt}, is_write=False)
        body = ((res.get("data") or [{}])[0] or {}).get("body", "")
        match = re.search(r'src="([^"]+)"', body or "")
        if not match:
            return None, _summarize_meta_error(res.get("error")) or "no iframe"
        return html.unescape(match.group(1)), None

    @staticmethod
    def _chrome_binary():
        import glob
        import shutil
        if os.environ.get("META_CHROME"):
            return os.environ["META_CHROME"]
        found = sorted(glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome")))
        found += [p for p in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                              "/Applications/Chromium.app/Contents/MacOS/Chromium") if os.path.exists(p)]
        return found[-1] if found else (shutil.which("chromium") or shutil.which("google-chrome")
                                        or shutil.which("chromium-browser") or shutil.which("chrome"))

    @staticmethod
    def _screenshot(chrome, url, out_png):
        # Без --user-data-dir: headless сам берёт временный профиль, а с явным каталогом
        # Chrome на VPS висит до таймаута (проверено 04.10.2026). Кадр снимается за ~5 сек.
        import subprocess
        try:
            subprocess.run(
                [chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
                 "--window-size=420,860", "--virtual-time-budget=8000", "--lang=ru-RU",
                 f"--screenshot={out_png}", url],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, check=False,
            )
        except subprocess.TimeoutExpired:
            return False
        return os.path.exists(out_png) and os.path.getsize(out_png) > 0

    def render_preview_sheet(self, items, out_path, account_id=None):
        """Картинки превью одним листом: строка = объявление, колонка = формат.

        items: [{"label": "Тсс комикс", "source": ad_id | creative_id | dict-спека, "formats": [...]}].
        Read-only: для спеки Meta рисует превью без создания объектов. Лист — то, что
        агент смотрит до запуска (и после, для проверки обрезки Reels/ленты).
        """
        from concurrent.futures import ThreadPoolExecutor
        from PIL import Image, ImageChops, ImageDraw, ImageFont

        chrome = self._chrome_binary()
        if not chrome:
            return {"error": "Chrome не найден: установите Google Chrome или укажите путь в META_CHROME"}
        out_path = Path(out_path)
        shots_dir = out_path.with_suffix("")
        shots_dir.mkdir(parents=True, exist_ok=True)

        jobs, failed = [], []
        for row, item in enumerate(items):
            for col, fmt in enumerate(item.get("formats") or PREVIEW_FORMATS):
                src, err = self._preview_src(item["source"], fmt, account_id)
                if not src:
                    failed.append({"label": item["label"], "format": fmt, "reason": err})
                    continue
                jobs.append((row, col, fmt, src, shots_dir / f"{row:02d}-{fmt}.png"))
        if self.dry_run:
            return {"dry_run": True, "jobs": len(jobs), "failed": failed}

        with ThreadPoolExecutor(max_workers=4) as pool:
            done = list(pool.map(lambda job: self._screenshot(chrome, job[3], str(job[4])), jobs))

        tile_w, label_h, gap = 300, 34, 8
        tiles = {}
        for job, ok in zip(jobs, done):
            row, col, fmt, _src, png = job
            if not ok:
                failed.append({"label": items[row]["label"], "format": fmt, "reason": "скриншот не снят"})
                continue
            img = Image.open(png).convert("RGB")
            bbox = ImageChops.difference(img, Image.new("RGB", img.size, (255, 255, 255))).getbbox()
            img = img.crop(bbox) if bbox else img
            tiles[(row, col)] = img.resize((tile_w, round(tile_w * img.height / img.width)))
        if not tiles:
            return {"error": "ни одного превью не снято", "failed": failed}

        cols = max(col for _row, col in tiles) + 1
        row_h = {row: max(t.height for (r, _c), t in tiles.items() if r == row) for row in {r for r, _c in tiles}}
        width = gap + cols * (tile_w + gap)
        height = gap + sum(h + label_h + gap for h in row_h.values())
        sheet = Image.new("RGB", (width, height), (24, 24, 24))
        draw = ImageDraw.Draw(sheet)
        font_path = next((f for f in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                                      "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                                      "/Library/Fonts/Arial Bold.ttf") if os.path.exists(f)), None)
        font = ImageFont.truetype(font_path, 15) if font_path else ImageFont.load_default()
        y = gap
        for row in sorted(row_h):
            for col in range(cols):
                tile = tiles.get((row, col))
                if not tile:
                    continue
                x = gap + col * (tile_w + gap)
                fmt = next(job[2] for job in jobs if job[0] == row and job[1] == col)
                short = PREVIEW_FORMAT_LABELS.get(fmt, fmt.lower())
                draw.text((x, y + 8), f"{items[row]['label'][:24 - len(short)]} · {short}",
                          fill=(255, 255, 255), font=font)
                sheet.paste(tile, (x, y + label_h))
            y += row_h[row] + label_h + gap
        sheet.save(out_path, quality=88)
        for item in failed:
            cli_print(f"⚠️ превью {item['label']} / {item['format']}: {item['reason']}")
        cli_print(f"🖼 Лист превью: {out_path}")
        return {"image": str(out_path), "shots": str(shots_dir), "failed": failed}

    def ig_media(self, instagram_user_id, limit=12):
        """Последние публикации Instagram-аккаунта: id для ig_profile_post (read-only)."""
        res = self._request("GET", f"{instagram_user_id}/media", {
            "fields": "id,media_product_type,permalink,timestamp,caption", "limit": str(limit),
        }, is_write=False)
        for item in res.get("data", []) or []:
            caption = (item.get("caption") or "").replace("\n", " ")[:70]
            cli_print(f"🎞 {item['id']} | {item.get('media_product_type')} | {item.get('timestamp', '')[:10]} | {item.get('permalink')} | {caption}")
        if "error" in res:
            cli_print(f"❌ {json.dumps(res['error'], ensure_ascii=False)}")
        return res

    # ── Insights (read-only) ──────────────────────────

    RESULT_ACTIONS = (
        ("лиды", ("lead", "onsite_conversion.lead_grouped", "offsite_conversion.fb_pixel_lead")),
        ("переписки", ("onsite_conversion.messaging_conversation_started_7d",)),
        ("визиты профиля", ("onsite_conversion.instagram_profile_visit", "instagram_profile_visit")),
        ("клики по ссылке", ("link_click",)),
    )
    # главный результат зависит от цели кампании: у WhatsApp-кампаний «лиды» — это пометки в переписках
    MAIN_RESULT = {"OUTCOME_LEADS": ("лиды", "переписки"), "OUTCOME_ENGAGEMENT": ("переписки",),
                   "OUTCOME_TRAFFIC": ("визиты профиля", "клики по ссылке")}

    def insights(self, object_id, date_preset="last_7d", level=None):
        """Расход и результаты кабинета/кампании/группы за период (read-only, один запрос)."""
        params = {
            "fields": "campaign_name,adset_name,ad_name,objective,spend,impressions,reach,clicks,actions,account_currency",
            "date_preset": date_preset, "limit": "200",
        }
        if level:
            params["level"] = level
            params["filtering"] = json.dumps([{"field": f"{level}.effective_status", "operator": "IN",
                                               "value": ["ACTIVE", "PAUSED", "CAMPAIGN_PAUSED", "ADSET_PAUSED"]}])
        res = self._request("GET", f"{object_id}/insights", params, is_write=False)
        if "error" in res:
            cli_print(f"❌ {json.dumps(res['error'], ensure_ascii=False)}")
            return res
        rows = res.get("data", []) or []
        if not rows:
            cli_print(f"Нет показов за {date_preset}.")
        for row in rows:
            actions = {a.get("action_type"): float(a.get("value") or 0) for a in row.get("actions") or []}
            results = [(label, max(actions.get(t, 0) for t in types)) for label, types in self.RESULT_ACTIONS]
            results = [(label, value) for label, value in results if value]
            spend = float(row.get("spend") or 0)
            cur = row.get("account_currency", "")
            title = row.get("ad_name") or row.get("adset_name") or row.get("campaign_name") or object_id
            found = ", ".join(f"{label} {value:g}" for label, value in results) or "результатов нет"
            main = next(((label, value) for label in self.MAIN_RESULT.get(row.get("objective"), ())
                         for found_label, value in results if found_label == label), None)
            cpr = f" | {spend / main[1]:.2f} {cur} за 1 ({main[0]})" if main else ""
            cli_print(f"📊 {title}: {spend:.2f} {cur} | показы {row.get('impressions', 0)} | "
                      f"охват {row.get('reach', 0)} | {found}{cpr}")
        return res

    # ── Show Log ──────────────────────────────────────

    @staticmethod
    def show_log(n=20):
        """Показать последние N записей из лога."""
        if not LOG_FILE.exists():
            cli_print("Лог пуст.")
            return
        lines = LOG_FILE.read_text().strip().split("\n")
        for line in lines[-n:]:
            cli_print(line)


# ── CLI ───────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    if dry_run:
        args.remove("--dry-run")

    if not args:
        print(__doc__)
        sys.exit(0)

    cmd = args[0]
    api = MetaAdsAPI(dry_run=dry_run)

    if dry_run:
        cli_print("🧪 DRY-RUN режим — запросы к Meta API НЕ отправляются\n")
    elif not api.token and cmd not in {"log", "image-crops"}:
        cli_print("🔑 META_TOKEN не задан. Подключение: python3 setup.py token (README → «Подключение»).")
        sys.exit(2)

    try:
        if cmd == "budget":
            if len(args) < 3:
                cli_print("Usage: budget <adset_id> <amount_cents> OR budget <campaign|adset> <entity_id> <amount_cents>")
                sys.exit(1)
            if args[1] in {"campaign", "adset"}:
                if len(args) < 4:
                    cli_print("Usage: budget <campaign|adset> <entity_id> <amount_cents>")
                    sys.exit(1)
                api.update_budget(args[2], int(args[3]), entity_type=args[1])
            else:
                api.update_budget(args[1], int(args[2]), entity_type="adset")

        elif cmd == "pause":
            if len(args) < 3:
                cli_print("Usage: pause <campaign|adset|ad> <entity_id>")
                sys.exit(1)
            api.pause(args[1], args[2])

        elif cmd == "delete":
            if len(args) < 3:
                cli_print("Usage: delete <campaign|adset|ad> <entity_id>")
                sys.exit(1)
            api.delete(args[1], args[2])

        elif cmd == "activate":
            if len(args) < 3:
                cli_print("Usage: activate <campaign|adset|ad> <entity_id>")
                sys.exit(1)
            api.activate(args[1], args[2])

        elif cmd == "create-campaign":
            if len(args) < 3:
                cli_print("Usage: create-campaign <account_id> <params_json>")
                sys.exit(1)
            params = json.loads(args[2])
            api.create_campaign(args[1], params)

        elif cmd == "create-adset":
            if len(args) < 3:
                cli_print("Usage: create-adset <campaign_id> <params_json>")
                sys.exit(1)
            params = json.loads(args[2])
            api.create_adset(args[1], params)

        elif cmd == "upload-image":
            if len(args) < 3:
                cli_print("Usage: upload-image <account_id> <file_path>")
                sys.exit(1)
            result = api.upload_image(args[1], args[2])
            cli_print(f"\nРезультат: {json.dumps(result, indent=2)}")

        elif cmd == "replace-creative":
            if len(args) < 4:
                cli_print("Usage: replace-creative <ad_id> <account_id> <image_path> [--no-crop]")
                cli_print("  Полный цикл: upload → read old creative → create new → update ad")
                cli_print("  По умолчанию: auto-crop 1:1 из центра для feed (как в Ads Manager)")
                cli_print("  --no-crop  Отключить авто-обрезку (одно изображение на все плейсменты)")
                sys.exit(1)
            auto_crop = "--no-crop" not in args
            focus = "center"
            for a in args[4:]:
                if a.startswith("--focus="):
                    raw = a.split("=", 1)[1]
                    focus = float(raw) if re.fullmatch(r"[0-9.]+", raw) else raw
            api.replace_creative(args[1], args[2], args[3], auto_crop=auto_crop, focus=focus)

        elif cmd == "validate-adset":
            if len(args) < 3:
                cli_print("Usage: validate-adset <campaign_id> <params_json>")
                sys.exit(1)
            api.validate_adset(args[1], json.loads(args[2]))

        elif cmd == "validate-creative":
            if len(args) < 3:
                cli_print("Usage: validate-creative <account_id> <params_json>")
                sys.exit(1)
            api.validate_creative(args[1], json.loads(args[2]))

        elif cmd == "validate-ctwa":
            if len(args) < 4:
                cli_print("Usage: validate-ctwa <campaign_id> <page_id> <whatsapp_phone_number>")
                cli_print("  Проверяет связку Page ↔ WhatsApp через validate_only; ничего не создаёт.")
                sys.exit(1)
            api.validate_ctwa(args[1], args[2], args[3])

        elif cmd == "whatsapp-numbers":
            business_id = None
            for a in args[1:]:
                if a.startswith("--business="):
                    business_id = a.split("=", 1)[1]
            api.whatsapp_numbers(business_id)

        elif cmd == "check-previews":
            if len(args) < 2:
                cli_print("Usage: check-previews <ad_id>")
                sys.exit(1)
            result = api.check_previews(args[1])
            cli_print(f"\nРезультат: {'OK' if result['ok'] else 'ОШИБКИ'}")

        elif cmd == "preview-sheet":
            if len(args) < 2:
                cli_print("Usage: preview-sheet <ad_id[,ad_id…]> [--out=/abs/sheet.jpg] [--formats=INSTAGRAM_REELS,…]")
                cli_print("  Картинки превью объявлений одним листом (плейсменты берутся из адсета).")
                sys.exit(1)
            out = f"/tmp/meta-previews-{time.strftime('%Y%m%d-%H%M%S')}.jpg"
            formats = None
            for a in args[2:]:
                if a.startswith("--out="):
                    out = a.split("=", 1)[1]
                elif a.startswith("--formats="):
                    formats = [f for f in a.split("=", 1)[1].split(",") if f]
            items = []
            for ad_id in args[1].split(","):
                info = api._request("GET", ad_id, {"fields": "name,adset{targeting}"}, is_write=False)
                items.append({"label": info.get("name", ad_id), "source": ad_id,
                              "formats": formats or builders.preview_formats_for((info.get("adset") or {}).get("targeting"))})
            result = api.render_preview_sheet(items, out)
            cli_print(json.dumps(result, ensure_ascii=False))

        elif cmd == "insights":
            if len(args) < 2:
                cli_print("Usage: insights <act_id|campaign_id|adset_id> [--preset=last_7d|today|yesterday|this_month|maximum] [--level=campaign|adset|ad]")
                sys.exit(1)
            opts = {k[2:]: v for k, _, v in (a.partition("=") for a in args[2:] if a.startswith("--"))}
            api.insights(args[1], date_preset=opts.get("preset", "last_7d"), level=opts.get("level"))

        elif cmd == "ig-media":
            if len(args) < 2:
                cli_print("Usage: ig-media <instagram_user_id> [limit]")
                sys.exit(1)
            api.ig_media(args[1], int(args[2]) if len(args) > 2 else 12)

        elif cmd == "image-crops":
            if len(args) < 2:
                cli_print("Usage: image-crops <file> [--focus=top|center|bottom|0.35] [--keys=100x100,400x500]")
                cli_print("  Печатает image_crops для link_data / asset_feed_spec.images[]. focus — где акцент:")
                cli_print("  top/center/bottom или доля высоты (0.35 = центр акцента на 35% от верха).")
                sys.exit(1)
            from meta_launch_builders import CROP_KEYS_FEED, image_crops_for, image_size
            focus = "center"
            keys = CROP_KEYS_FEED
            for a in args[2:]:
                if a.startswith("--focus="):
                    raw = a.split("=", 1)[1]
                    focus = float(raw) if re.fullmatch(r"[0-9.]+", raw) else raw
                elif a.startswith("--keys="):
                    keys = tuple(k.strip() for k in a.split("=", 1)[1].split(",") if k.strip())
            width, height = image_size(args[1])
            crops = image_crops_for(width, height, focus, keys)
            cli_print(f"📐 {width}x{height}, focus={focus}")
            cli_print(json.dumps(crops))

        elif cmd == "create-creative":
            if len(args) < 3:
                cli_print("Usage: create-creative <account_id> <params_json>")
                sys.exit(1)
            params = json.loads(args[2])
            api.create_creative(args[1], params)

        elif cmd == "ensure-page-backed-instagram":
            if len(args) < 2:
                cli_print("Usage: ensure-page-backed-instagram <page_id>")
                cli_print("  Для IG placements без реального IG профиля: создаёт/возвращает Page-backed Instagram actor.")
                sys.exit(1)
            result = api.ensure_page_backed_instagram(args[1])
            cli_print(f"\nРезультат: {json.dumps(result, indent=2, ensure_ascii=False)}")

        elif cmd == "upload-video":
            if len(args) < 3:
                cli_print("Usage: upload-video <account_id> <file_path>")
                sys.exit(1)
            result = api.upload_video(args[1], args[2])
            cli_print(f"\nРезультат: {json.dumps(result, indent=2)}")

        elif cmd == "create-ad":
            if len(args) < 4:
                cli_print("Usage: create-ad <adset_id> <creative_id> <name> [--account-id=<act>] [--status=ACTIVE|PAUSED]")
                sys.exit(1)
            account_id = None
            status = None
            name_parts = []
            for a in args[3:]:
                if a.startswith("--account-id="):
                    account_id = a.split("=", 1)[1]
                elif a.startswith("--status="):
                    status = a.split("=", 1)[1]
                else:
                    name_parts.append(a)
            api.create_ad(args[1], args[2], " ".join(name_parts), account_id=account_id, status=status)

        elif cmd == "duplicate-adset":
            if len(args) < 4:
                cli_print("Usage: duplicate-adset <adset_id> <new_name> <new_budget_cents> [--video-id=<vid>] [--campaign-id=<cid>] [--activate]")
                sys.exit(1)
            video_id = None
            campaign_id = None
            activate_after = False
            for a in args[4:]:
                if a.startswith("--video-id="):
                    video_id = a.split("=", 1)[1]
                elif a.startswith("--campaign-id="):
                    campaign_id = a.split("=", 1)[1]
                elif a == "--activate":
                    activate_after = True
            result = api.duplicate_adset(args[1], args[2], int(args[3]), video_id, campaign_id)
            if activate_after and "error" not in result:
                result = api._activate_after_duplicate(result)

        elif cmd == "copy-adset-native":
            if len(args) < 4:
                cli_print("Usage: copy-adset-native <adset_id> <new_name> <new_budget_cents> [--campaign-id=<cid>] [--activate]")
                cli_print("  Нативный /copies endpoint. Рекомендовано для WhatsApp/CTWA адсетов.")
                sys.exit(1)
            campaign_id = None
            activate_after = False
            for a in args[4:]:
                if a.startswith("--campaign-id="):
                    campaign_id = a.split("=", 1)[1]
                elif a == "--activate":
                    activate_after = True
            result = api.copy_adset_native(args[1], args[2], int(args[3]), campaign_id)
            if activate_after and "error" not in result:
                result = api._activate_after_duplicate(result)
            else:
                api._copied_placement_violations(result)

        elif cmd == "create-whatsapp-from-template":
            if len(args) < 7:
                cli_print("Usage: create-whatsapp-from-template <template_adset_id> <template_ad_id> <campaign_name> <adset_name> <ad_name> <budget_cents> [--video-id=<vid>] [--account-id=<act>]")
                cli_print("  Создаёт новую OUTCOME_ENGAGEMENT → WHATSAPP рекламу с нуля: campaign PAUSED, adset/ad ACTIVE за ним.")
                sys.exit(1)
            video_id = None
            account_id = None
            for a in args[7:]:
                if a.startswith("--video-id="):
                    video_id = a.split("=", 1)[1]
                elif a.startswith("--account-id="):
                    account_id = a.split("=", 1)[1]
            api.create_whatsapp_from_template(
                args[1], args[2], args[3], args[4], args[5], int(args[6]),
                new_video_id=video_id, account_id=account_id
            )

        elif cmd == "copy-campaign-native":
            if len(args) < 3:
                cli_print("Usage: copy-campaign-native <campaign_id> <new_name> [--activate]")
                cli_print("  Нативный /copies endpoint для campaign → adsets → ads → creatives.")
                sys.exit(1)
            result = api.copy_campaign_native(args[1], args[2])
            if "--activate" in args[3:] and "error" not in result:
                result = api._activate_after_duplicate(result)
            else:
                api._copied_placement_violations(result)

        elif cmd == "duplicate-campaign-chunked":
            if len(args) < 3:
                cli_print("Usage: duplicate-campaign-chunked <campaign_id> <new_name> [--activate]")
                cli_print("  Fallback: создать кампанию один раз и копировать адсеты по одному.")
                sys.exit(1)
            result = api.duplicate_campaign_chunked(args[1], args[2])
            if "--activate" in args[3:] and "error" not in result:
                result = api._activate_after_duplicate(result)
            else:
                api._copied_placement_violations(result)

        elif cmd == "batch-pause":
            if len(args) < 3:
                cli_print("Usage: batch-pause <campaign|adset|ad> <id1,id2,id3>")
                sys.exit(1)
            ids = args[2].split(",")
            api.batch_pause(args[1], ids)

        elif cmd == "batch-activate":
            if len(args) < 3:
                cli_print("Usage: batch-activate <campaign|adset|ad> <id1,id2,id3>")
                sys.exit(1)
            ids = args[2].split(",")
            api.batch_activate(args[1], ids)

        elif cmd == "status":
            if len(args) < 3:
                cli_print("Usage: status <campaign|adset|ad> <entity_id>")
                sys.exit(1)
            result = api.get_status(args[1], args[2])
            cli_print(json.dumps(result, indent=2, ensure_ascii=False))

        elif cmd == "update":
            if len(args) < 3:
                cli_print("Usage: update <entity_id> <params_json>")
                sys.exit(1)
            params = json.loads(args[2])
            api.update_entity(args[1], params)

        elif cmd == "log":
            n = int(args[1]) if len(args) > 1 else 20
            MetaAdsAPI.show_log(n)

        else:
            cli_print(f"Неизвестная команда: {cmd}")
            cli_print("Доступные: budget, pause, activate, delete, create-campaign, create-adset, "
                      "create-creative, validate-adset, validate-creative, validate-ctwa, whatsapp-numbers, "
                      "check-previews, preview-sheet, ig-media, image-crops, upload-image, upload-video, replace-creative, "
                      "ensure-page-backed-instagram, create-ad, duplicate-adset, copy-adset-native, create-whatsapp-from-template, "
                      "copy-campaign-native, duplicate-campaign-chunked, batch-pause, "
                      "batch-activate, status, update, log")
            sys.exit(1)

    except json.JSONDecodeError as e:
        cli_print(f"❌ Ошибка парсинга JSON: {e}")
        sys.exit(1)
    except MetaUsageHardStop as e:
        cli_print(f"🚫 {e}")
        sys.exit(90)
    except Exception as e:
        cli_print(f"❌ Ошибка: {e}")
        log_write("CLI", cmd, {"args": args}, "EXCEPTION", str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
