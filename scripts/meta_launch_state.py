#!/usr/bin/env python3
"""State helpers for Meta launch workflow v2."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# State запусков живёт вне репозитория: ~/.local/share/meta-ads-manager/state (META_ADS_STATE_DIR).
from config import STATE_DIR as _DEFAULT_STATE_DIR

STATE_DIR = Path(os.environ.get("META_ADS_STATE_DIR") or _DEFAULT_STATE_DIR)
STATE_VERSION = 2


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9а-яё._-]+", "-", value, flags=re.IGNORECASE)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value or "launch"


def default_state_path(client: str, scenario: str, label: str | None = None) -> Path:
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    suffix = slugify(label) if label else date
    return STATE_DIR / slugify(client) / f"{date}-{slugify(scenario)}-{suffix}.json"


def new_launch_state(
    *,
    client: str,
    account_id: str,
    scenario: str,
    source_request: dict[str, Any] | None = None,
    source_ids: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "client": client,
        "account_id": account_id,
        "scenario": scenario,
        "created_at": utc_now_iso(),
        "source_request": source_request or {},
        "source_ids": source_ids or {},
        "created_ids": {
            "campaigns": [],
            "adsets": [],
            "creatives": [],
            "ads": [],
            "images": [],
            "videos": [],
        },
        "creative_sources": [],
        "targeting_snapshot": {},
        "launch_gate": {},
        "verify_results": [],
        "errors": [],
        "approved_at": None,
        "activation_command": None,
        "activated": False,
    }


def load_state(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: str | Path, state: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def append_verify_result(state: dict[str, Any], result: dict[str, Any]) -> None:
    state.setdefault("verify_results", []).append({"checked_at": utc_now_iso(), **result})


def mark_approved(state: dict[str, Any], activation_command: str) -> None:
    state["approved_at"] = utc_now_iso()
    state["activation_command"] = activation_command


def mark_activated(state: dict[str, Any]) -> None:
    state["activated"] = True
    state.setdefault("activated_at", utc_now_iso())
