# exa-skill

Скилл для Claude Code (и других агентов с Agent Skills): веб-поиск через [Exa](https://exa.ai)
вместо встроенного WebSearch. Один Python-скрипт без зависимостей, прямой REST API, пул из
нескольких ключей с автоматическим переключением.

*An Agent Skill for Claude Code: web search via the Exa API instead of built-in WebSearch.
Single dependency-free Python script, multi-key pool with automatic failover. Docs are in
Russian; the CLI is self-explanatory (`python scripts/exa.py -h`).*

> Неофициальный проект, с Exa Labs не связан.

## Зачем

Мой эксперимент в Claude Code (ноябрь 2026), Exa против встроенного WebSearch:

| | Exa `search` | WebSearch |
|---|---|---|
| Бенчмарк, 10 фактических вопросов (из 30) | **28** | 23 |
| Первоисточник в выдаче | 9 / 10 | 6 / 10 |
| Ответ агенту, символов | **~2 300** | ~3 250 |
| Время, медиана | **1,8 с** | 8,0 с |

Exa отдаёт выдержки из страниц с датой и URL, а не пересказ, поэтому видно, откуда факт.
Слабое место — новости последних дней: иногда пустая выдача.

## Быстрый старт

```bash
git clone https://github.com/privatekey7/exa-skill ~/.claude/skills/exa
python ~/.claude/skills/exa/scripts/exa.py add      # вставить ключ: ввод скрыт, ключ проверяется
python ~/.claude/skills/exa/scripts/exa.py status   # проверить все ключи
```

На Windows путь `%USERPROFILE%\.claude\skills\exa`, на macOS/Linux вместо `python` — `python3`.
Нужен Python 3.10+. Ключ: [dashboard.exa.ai/api-keys](https://dashboard.exa.ai/api-keys).

Чтобы Claude не тянулся к WebSearch, добавь в `~/.claude/CLAUDE.md`:

```markdown
## Поиск в интернете
- Любой поиск в интернете — только через скилл `exa`. Встроенный WebSearch не используй.
```

## Команды

```bash
E="python ~/.claude/skills/exa/scripts/exa.py"
$E search "описание идеальной страницы" -n 5 --chars 400 [--since 2026-10-01] [--domain github.com]
$E fetch https://example.com/a https://example.com/b --chars 3000
$E agent "задача для многошагового ресёрча" --effort medium [--schema schema.json]
$E agent --resume agent_run_...
$E status | add [имя] | reset
```

Каждая команда печатает в конце `[key <имя>, $цена]`. Как выбирать команду и сколько это стоит —
в [SKILL.md](SKILL.md).

## Несколько ключей

Ключи лежат в `.env` рядом с `SKILL.md` (пример — [.env.example](.env.example)):

```dotenv
EXA_KEY_1=...
EXA_KEY_2=...
EXA_KEY_3=...
```

- Порядок строк = приоритет. Сначала берётся верхний ключ.
- 402 (кончился баланс) → следующий ключ, этот откладывается на 6 часов.
- 429 (лимит 10 запросов в секунду) → следующий ключ; если упёрлись все — пауза и до 3 повторов.
- 401/403 (ключ неверный или отозван) → откладывается на неделю.
- `add` сам берёт следующий номер, проверяет ключ запросом и только потом пишет в `.env`.
- Переменная окружения `EXA_API_KEY` подхватывается последней.
- Значения ключей нигде не печатаются; в журнале пула только имена.

Важно: ключи одной команды (team) в Exa делят и баланс, и лимит запросов. Пул имеет смысл для
ключей из разных команд — например, личной и рабочей.

## Бесплатные кредиты

По [документации Exa](https://exa.ai/docs/admin/billing): бесплатный баланс $10 обновляется 1-го
числа каждого месяца (не копится), плюс разовый бонус $10 за онбординг — итого $20 на старте,
карта не нужна. Оба — только для первой команды пользователя.

Промокоды вводятся в [Billing](https://dashboard.exa.ai/billing) → Redeem, каждый один раз на
команду. На 4 ноября 2026 работают `EXA20FREEBUFF` и `EXA20FIRESHIP` (+$20 каждый), то есть
$60 на старте.

## Журнал расходов

Выключен по умолчанию. Включить: создать `data/usage.jsonl` в папке скилла или задать
`EXA_USAGE_FILE=путь`. Тогда каждый вызов пишет строку JSON: время, команда, запрос, цена, объём
ответа, папка проекта.

## Лицензия

MIT. Методички в `references/` — из [exa-labs/exa-mcp-server](https://github.com/exa-labs/exa-mcp-server),
MIT, © Exa Labs.
