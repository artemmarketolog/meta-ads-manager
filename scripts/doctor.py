#!/usr/bin/env python3
"""doctor.py — проверка подключения к Meta и сбор биндингов клиентов. Только чтение.

  doctor.py                         # токен, разрешения, кабинеты, уровень доступа Marketing API
  doctor.py discover                # страницы, Instagram, пиксели, WhatsApp по всем кабинетам
  doctor.py discover --account=act_123 --write   # дописать черновик клиента в clients.json

Токен не печатается: только тип, приложение, срок и последние 4 символа.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone

import requests

from config import CLIENTS_FILE, ENV_FILE, GRAPH_VERSION, META_TOKEN

BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"

# Разрешения токена: что без чего не работает (подробно — references/permissions.md).
REQUIRED = {
    "ads_management": "создание и изменение кампаний, групп, объявлений",
    "ads_read": "статистика и чтение кабинетов",
    "business_management": "активы бизнес-портфолио, номера WhatsApp",
    "pages_show_list": "список страниц",
    "pages_read_engagement": "чтение страницы и связанного Instagram",
    "pages_manage_ads": "реклама от имени страницы, лид-формы",
}
RECOMMENDED = {
    "instagram_basic": "Instagram-аккаунты и публикации (продвижение поста, трафик на профиль)",
    "leads_retrieval": "выгрузка заявок из лид-форм",
    "whatsapp_business_management": "номера WhatsApp для рекламы в WhatsApp",
}
ACCOUNT_STATUS = {1: "активен", 2: "отключён", 3: "долг по оплате", 7: "на проверке", 8: "закрывается",
                  9: "льготный период", 100: "закрыт", 101: "закрыт", 201: "активен (любой)", 202: "закрыт (любой)"}


def get(path, **params):
    params["access_token"] = META_TOKEN
    r = requests.get(f"{BASE}/{path}", params=params, timeout=60)
    try:
        data = r.json()
    except ValueError:
        data = {"error": {"message": f"HTTP {r.status_code}"}}
    return data, r.headers


def paged(path, **params):
    rows = []
    params.setdefault("limit", "100")
    data, _ = get(path, **params)
    while True:
        if "error" in data:
            return rows, data["error"]
        rows += data.get("data", []) or []
        nxt = (data.get("paging") or {}).get("next")
        if not nxt or len(rows) >= 1000:
            return rows, None
        data = requests.get(nxt, timeout=60).json()


def when(ts):
    if not ts:
        return "бессрочно"
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%d.%m.%Y")


def err_text(err):
    return f"code {err.get('code')}/{err.get('error_subcode', '-')}: {err.get('message', '')[:200]}"


def check():
    ok = True
    print(f"Конфиг: {ENV_FILE}")
    if not META_TOKEN:
        print("❌ META_TOKEN не задан. Подключение: python3 setup.py token")
        return False
    print(f"🔑 токен …{META_TOKEN[-4:]}")
    dbg, _ = get("debug_token", input_token=META_TOKEN)
    info = dbg.get("data") or {}
    if "error" in dbg or not info.get("is_valid"):
        msg = err_text(dbg["error"]) if "error" in dbg else (info.get("error") or {}).get("message", "токен недействителен")
        print(f"❌ токен не принят Meta: {msg}")
        print("   Выпустите новый (prompts/desktop/04-system-user-token.md) и сохраните: python3 setup.py token")
        return False
    print(f"✅ тип {info.get('type')} | приложение {info.get('application')} ({info.get('app_id')}) | "
          f"срок: {when(info.get('expires_at'))} | доступ к данным: {when(info.get('data_access_expires_at'))}")
    if info.get("type") == "USER":
        print("⚠️ Это личный токен пользователя: доступ к данным кончается раз в 90 дней. "
              "Надёжнее токен системного пользователя (references/app-setup.md).")
    scopes = set(info.get("scopes") or [])
    for perm, why in REQUIRED.items():
        if perm not in scopes:
            ok = False
            print(f"❌ нет разрешения {perm} — {why}")
    missing_rec = [p for p in RECOMMENDED if p not in scopes]
    for perm in missing_rec:
        print(f"⚠️ нет {perm} — {RECOMMENDED[perm]}")
    if ok and not missing_rec:
        print(f"✅ разрешения: все {len(REQUIRED) + len(RECOMMENDED)} нужных на месте")
    elif ok:
        print("✅ обязательные разрешения на месте")

    accounts, err = paged("me/adaccounts", fields="id,name,account_status,currency,timezone_name,business{name}")
    if err:
        print(f"❌ кабинеты не читаются: {err_text(err)}")
        return False
    if not accounts:
        print("❌ токену не назначен ни один рекламный кабинет. Назначьте кабинеты системному пользователю "
              "(prompts/desktop/04-system-user-token.md, шаг «Активы»).")
        return False
    print(f"✅ кабинетов: {len(accounts)}")
    for a in accounts[:40]:
        status = ACCOUNT_STATUS.get(a.get("account_status"), a.get("account_status"))
        biz = (a.get("business") or {}).get("name", "—")
        print(f"   {a['id']} | {a.get('name')} | {status} | {a.get('currency')} | {a.get('timezone_name')} | портфолио: {biz}")
    if len(accounts) > 40:
        print(f"   … и ещё {len(accounts) - 40}")

    active = next((a for a in accounts if a.get("account_status") == 1), accounts[0])
    _, headers = get(f"{active['id']}/insights", fields="spend", date_preset="yesterday")
    tier = None
    for value in (headers.get("x-business-use-case-usage") or "", headers.get("x-ad-account-usage") or ""):
        m = re.search(r'"ads_api_access_tier"\s*:\s*"([a-z_]+)"', value)
        if m:
            tier = m.group(1)
    if tier == "standard_access":
        print("✅ Marketing API: Full access (заголовок standard_access) — лимиты большие")
    elif tier:
        print(f"⚠️ Marketing API: {tier} — Limited access: ~20 изменений за 5 минут на кабинет. Большие запуски "
              "делите на части; после 500 вызовов за 15 дней нажмите Upgrade (prompts/desktop/08-full-access.md)")
    else:
        print("ℹ️ уровень доступа Marketing API не виден в заголовках; посмотреть: prompts/desktop/01-audit.md")
    return ok


def discover(account=None, write=False):
    if not META_TOKEN:
        print("❌ META_TOKEN не задан. Подключение: python3 setup.py token")
        return False
    if account:
        account = account if account.startswith("act_") else f"act_{account}"
        acc, _ = get(account, fields="id,name,currency,timezone_name")
        if "error" in acc:
            print(f"❌ {account}: {err_text(acc['error'])}")
            return False
        accounts = [acc]
    else:
        accounts, err = paged("me/adaccounts", fields="id,name,currency,timezone_name,account_status")
        if err:
            print(f"❌ {err_text(err)}")
            return False
        accounts = [a for a in accounts if a.get("account_status") == 1]

    phones = whatsapp_phones()
    drafts = {}
    for acc in accounts:
        print(f"\n📁 {acc['id']} «{acc.get('name')}» {acc.get('currency', '')}")
        pages, err = paged(f"{acc['id']}/promote_pages", fields="id,name,instagram_business_account{id,username}")
        if err:
            print(f"   ⚠️ страницы: {err_text(err)}")
        for p in pages:
            ig = p.get("instagram_business_account") or {}
            print(f"   📄 страница {p['id']} «{p.get('name')}» | Instagram: "
                  + (f"{ig.get('id')} @{ig.get('username')}" if ig else "не привязан"))
        pixels, err = paged(f"{acc['id']}/adspixels", fields="id,name,last_fired_time")
        for px in pixels:
            print(f"   🎯 пиксель {px['id']} «{px.get('name')}» | последнее событие: {(px.get('last_fired_time') or 'нет')[:10]}")
        page = pages[0] if len(pages) == 1 else None
        ig = (page or {}).get("instagram_business_account") or {}
        drafts[slug(acc.get("name") or acc["id"])] = {
            "name": acc.get("name"),
            "aliases": [],
            "account_id": acc["id"],
            "currency": acc.get("currency"),
            "timezone": acc.get("timezone_name"),
            "page_id": (page or {}).get("id", ""),
            "instagram_user_id": ig.get("id", ""),
            "instagram_username": ig.get("username", ""),
            "whatsapp_phone_number": "",
            "pixel_id": pixels[0]["id"] if len(pixels) == 1 else "",
            "lead_gen_form_id": "",
            "geo": "",
            "notes": "черновик doctor.py discover: проверьте страницу, номер WhatsApp и пиксель",
        }
        if len(pages) > 1:
            print("   ℹ️ страниц несколько: впишите нужную в clients.json сами")
    if phones:
        print("\n📱 Номера WhatsApp (для whatsapp_phone_number):")
        for ph in phones:
            print(f"   {ph['digits']} | {ph['verified_name']} | бизнес «{ph['business']}» | {ph['status']}")
    else:
        print("\n📱 Номера WhatsApp не видны токену (нет whatsapp_business_management или номер в чужом бизнесе) — "
              "впишите номер клиента вручную, проверка: meta_api.py validate-ctwa")

    if write:
        CLIENTS_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        current = json.loads(CLIENTS_FILE.read_text()) if CLIENTS_FILE.exists() else {"clients": {}}
        added = [k for k in drafts if k not in current["clients"]]
        for k in added:
            current["clients"][k] = drafts[k]
        CLIENTS_FILE.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n")
        CLIENTS_FILE.chmod(0o600)
        print(f"\n💾 {CLIENTS_FILE}: добавлено {len(added)} (существующие записи не тронуты)")
    else:
        print("\nЧтобы записать черновики клиентов: doctor.py discover --write (или --account=act_… --write)")
    return True


def whatsapp_phones():
    businesses, err = paged("me/businesses", fields="id,name")
    rows = []
    for b in businesses if not err else []:
        for edge in ("owned_whatsapp_business_accounts", "client_whatsapp_business_accounts"):
            wabas, err = paged(f"{b['id']}/{edge}", fields="id,name,phone_numbers{display_phone_number,verified_name,status}")
            for w in wabas:
                for ph in (w.get("phone_numbers") or {}).get("data", []):
                    rows.append({"digits": re.sub(r"\D", "", ph.get("display_phone_number") or ""),
                                 "verified_name": ph.get("verified_name"), "status": ph.get("status"),
                                 "business": b.get("name")})
    return rows


def slug(name):
    s = re.sub(r"[^a-z0-9а-яё]+", "-", name.lower()).strip("-")
    return s or "client"


def main():
    args = sys.argv[1:]
    opts = {k[2:]: v for k, _, v in (a.partition("=") for a in args if a.startswith("--"))}
    if args and args[0] == "discover":
        ok = discover(opts.get("account"), write="write" in opts)
    elif not args or args[0] == "check":
        ok = check()
    else:
        print(__doc__)
        ok = False
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
