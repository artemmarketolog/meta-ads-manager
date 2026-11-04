#!/usr/bin/env python3
"""Установка meta-ads-manager: своё окружение .venv, личный конфиг и безопасный приём токена.

  python3 setup.py                     # .venv + зависимости + ~/.config/meta-ads-manager/{meta.env,clients.json}
  python3 setup.py --check             # только показать, что установлено; ничего не меняет
  python3 setup.py token               # вставить токен скрытым вводом (в отдельном окне терминала)
  python3 setup.py token --from-clipboard   # взять токен из буфера обмена, не показывая его

Токен никогда не печатается и не уходит никуда, кроме graph.facebook.com (проверка /debug_token).
"""
import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_DIR = Path(os.environ.get("META_ADS_CONFIG_DIR")
                  or Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "meta-ads-manager").expanduser()
ENV_FILE = CONFIG_DIR / "meta.env"
CLIENTS_FILE = CONFIG_DIR / "clients.json"
GRAPH = os.environ.get("GRAPH_VERSION", "v26.0")
VENV_PY = ROOT / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")


def env_state():
    if not ENV_FILE.is_file():
        return "нет файла"
    values = dict(line.split("=", 1) for line in ENV_FILE.read_text().splitlines()
                  if "=" in line and not line.lstrip().startswith("#"))
    return "META_TOKEN задан" if values.get("META_TOKEN", "").strip() else "META_TOKEN пустой"


def install():
    if sys.version_info < (3, 10):
        raise SystemExit("Нужен Python 3.10+")
    if not VENV_PY.is_file():
        venv.EnvBuilder(with_pip=True).create(ROOT / ".venv")
    subprocess.run([str(VENV_PY), "-m", "pip", "install", "--disable-pip-version-check", "-q",
                    "-r", str(ROOT / "requirements.txt")], check=True)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not ENV_FILE.exists():
        shutil.copyfile(ROOT / "meta.env.example", ENV_FILE)
        ENV_FILE.chmod(0o600)
        print(f"Создан {ENV_FILE} (chmod 600).")
    if not CLIENTS_FILE.exists():
        CLIENTS_FILE.write_text('{\n  "clients": {}\n}\n')
        CLIENTS_FILE.chmod(0o600)
        print(f"Создан {CLIENTS_FILE} — сюда doctor.py discover --write запишет ваших клиентов.")
    print(f"Готово: {VENV_PY}")
    print(f"Конфиг: {ENV_FILE} — {env_state()}")
    if env_state() != "META_TOKEN задан":
        print("Дальше: подключение к Meta — скажите агенту «подключи Meta Ads» (README → Подключение).")


def check():
    print(f"python .venv: {'есть' if VENV_PY.is_file() else 'нет (запустите python3 setup.py)'}")
    print(f"конфиг {ENV_FILE}: {env_state()}")
    print(f"клиенты {CLIENTS_FILE}: {'есть' if CLIENTS_FILE.is_file() else 'нет'}")
    candidates = sorted(Path.home().glob(".cache/ms-playwright/chromium-*/chrome-linux*/chrome"))
    candidates += [Path(p) for p in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",) if os.path.exists(p)]
    chrome = (os.environ.get("META_CHROME") or (str(candidates[-1]) if candidates else None)
              or shutil.which("google-chrome") or shutil.which("chromium"))
    print(f"Chrome для листа превью: {chrome or 'не найден (необязательно; путь можно задать в META_CHROME)'}")


def read_clipboard():
    commands = (["pbpaste"], ["wl-paste", "-n"], ["xclip", "-selection", "clipboard", "-o"],
                ["xsel", "-b", "-o"], ["powershell.exe", "-NoProfile", "-Command", "Get-Clipboard"])
    for cmd in commands:
        if shutil.which(cmd[0]):
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
    raise SystemExit("Буфер обмена пуст или недоступен. Скопируйте токен кнопкой «Копировать» и повторите, "
                     "или запустите python3 setup.py token в отдельном окне терминала.")


def debug_token(token):
    query = urllib.parse.urlencode({"input_token": token, "access_token": token})
    try:
        with urllib.request.urlopen(f"https://graph.facebook.com/{GRAPH}/debug_token?{query}", timeout=30) as r:
            return json.load(r).get("data") or {}
    except urllib.error.HTTPError as e:
        try:
            return {"is_valid": False, "error": json.load(e).get("error", {})}
        except ValueError:
            return {"is_valid": False, "error": {"message": f"HTTP {e.code}"}}


def save_token(token):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else \
        (ROOT / "meta.env.example").read_text().splitlines()
    out, done = [], False
    for line in lines:
        if line.split("=", 1)[0].strip() == "META_TOKEN":
            out.append(f"META_TOKEN={token}")
            done = True
        else:
            out.append(line)
    if not done:
        out.append(f"META_TOKEN={token}")
    fd = os.open(ENV_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(out) + "\n")
    ENV_FILE.chmod(0o600)


def token(from_clipboard):
    raw = read_clipboard() if from_clipboard else getpass.getpass("Вставьте токен Meta (символы не видны) и нажмите Enter: ")
    value = raw.strip().strip('"').strip("'")
    if value.startswith("META_TOKEN="):
        value = value.split("=", 1)[1]
    if len(value) < 50 or " " in value or not value.startswith("EA"):
        raise SystemExit("Это не похоже на токен Meta (он начинается с EA и длиннее 100 символов). Ничего не сохранено.")
    info = debug_token(value)
    if not info.get("is_valid"):
        msg = (info.get("error") or {}).get("message", "токен недействителен")
        raise SystemExit(f"Meta не приняла токен: {msg[:200]}. Ничего не сохранено.")
    save_token(value)
    expires = "бессрочный" if not info.get("expires_at") else "с ограниченным сроком"
    print(f"✅ Сохранено в {ENV_FILE} (chmod 600): токен …{value[-4:]}, тип {info.get('type')}, "
          f"приложение «{info.get('application')}», {expires}, разрешений: {len(info.get('scopes') or [])}.")
    if from_clipboard:
        for cmd in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"], ["clip.exe"]):
            if shutil.which(cmd[0]):
                subprocess.run(cmd, input="", text=True, timeout=10)
                print("🧹 Буфер обмена очищен.")
                break
    print(f"Проверка: {VENV_PY} {ROOT / 'scripts/doctor.py'}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", nargs="?", choices=["token"], help="token — сохранить токен Meta")
    p.add_argument("--check", action="store_true", help="только показать состояние")
    p.add_argument("--from-clipboard", action="store_true", help="для token: взять токен из буфера обмена")
    a = p.parse_args()
    if a.check:
        check()
    elif a.command == "token":
        token(a.from_clipboard)
    else:
        install()


if __name__ == "__main__":
    main()
