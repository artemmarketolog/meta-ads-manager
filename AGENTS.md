# Работа над этим репозиторием

Этот файл — для агентов и людей, которые меняют сам скилл. Чтобы *пользоваться* скиллом — SKILL.md, установить — README.md.

- Ни токенов, ни ID реальных кабинетов, страниц, Instagram, номеров WhatsApp, имён клиентов в репозитории.
  В примерах — только явные заглушки (`act_000000000000000`, `100000000000000`, `coffee-house`).
  Данные пользователя живут в `~/.config/meta-ads-manager/` и `~/.local/share/meta-ads-manager/`.
- Пути — относительно репозитория или из переменных окружения (`scripts/config.py`); домашний каталог не зашивать.
- Write-запросы к Meta — только через публичные методы `MetaAdsAPI`; правила метода (OPT_OUT автоулучшений,
  запрет Audience Network/Marketplace, staging-gate) живут в коде и покрыты тестами — не ослаблять ради прохождения ошибки.
- Тесты без сети и трат: `.venv/bin/python -m unittest discover -s tests -v`; примеры:
  `for f in examples/specs/*.json; do .venv/bin/python scripts/meta_launch.py --spec "$f" --dry-run; done`.
- Промпты для Claude Desktop (`prompts/desktop/`) всегда начинаются с `{{RULES}}` и заканчиваются форматом отчёта без секретов.
- SKILL.md и references/ пишутся по-русски, коротко и проверяемо; новые разделы — в том же стиле.
