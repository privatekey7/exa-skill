#!/usr/bin/env python3
"""Exa из командной строки: поиск, чтение страниц, Agent и пул API-ключей.

Прямые вызовы REST API Exa (без MCP), только стандартная библиотека Python.
Вывод компактный: столько текста, сколько попросили, — экономит токены агента.

    python exa.py search "запрос" [-n 5] [--chars 400] [--type auto|fast|instant|deep]
                                  [--since 2026-10-01] [--domain github.com ...]
    python exa.py fetch URL [URL ...] [--chars 3000]
    python exa.py agent "задача" [--effort medium] [--schema schema.json|'{...}']
    python exa.py agent --resume agent_run_...
    python exa.py status | add [имя] | reset

Ключи — в .env в корне скилла, строки EXA_KEY_1=..., EXA_KEY_2=...
(add без имени берёт следующий номер), порядок строк = приоритет. Переменная окружения EXA_API_KEY добавляется последней
как «env». При 401/402/403/429 запрос повторяется со следующим ключом, упавший
ключ откладывается. Значения ключей не печатаются и не пишутся в лог.

Журнал вызовов (время, цена, объём ответа, запрос) пишется, только если задан
EXA_USAGE_FILE или уже есть файл data/usage.jsonl в корне скилла.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # папка скилла
ENV_FILE = Path(os.environ.get("EXA_POOL_ENV", ROOT / ".env"))
STATE_FILE = Path(os.environ.get("EXA_POOL_STATE", ROOT / ".tmp" / "state.json"))
LOG_FILE = ROOT / ".tmp" / "pool.log"
# Журнал вызовов для сравнения с WebSearch (scripts/report.py). Только метаданные.
USAGE_FILE = Path(os.environ.get("EXA_USAGE_FILE", ROOT / "data" / "usage.jsonl"))
API = "https://api.exa.ai"
# Cloudflare перед Exa режет дефолтный User-Agent urllib (ошибка 1010).
USER_AGENT = "exa-skill/1.0 (+claude-code)"

# Сколько секунд не трогать ключ после ошибки.
# 429 — лимит 10 запросов в секунду на команду, поэтому отсрочка короткая.
COOLDOWN = {401: 7 * 86400, 403: 7 * 86400, 402: 6 * 3600, 429: 2}
RATE_LIMIT_ROUNDS = 3  # сколько раз ждать и повторять, если 429 вернули все ключи
NAME = re.compile(r"^[A-Za-z0-9_]+$")
POLL_SECONDS = 5
AGENT_TIMEOUT = 15 * 60
LOCK_TIMEOUT = 30  # сколько секунд ждать файловую блокировку

_lock = threading.Lock()
# Что узнали о текущем вызове: стоимость, ключ, число результатов, объём вывода.
usage: dict = {"chars": 0}


def emit(text: str = "") -> None:
    """Вывод для агента; объём считается — это и есть расход токенов на поиск."""
    print(text)
    usage["chars"] += len(text) + 1


@dataclass(frozen=True)
class Key:
    name: str
    value: str


class ExaError(Exception):
    pass


# --- пул ключей ---------------------------------------------------------------

@contextmanager
def file_lock(path: Path):
    """Межпроцессная блокировка через соседний .lock-файл. Нужна, потому что exa.py
    часто запускают пачкой параллельно, а _lock защищает только потоки одного процесса."""
    path.parent.mkdir(exist_ok=True)
    with _lock, path.with_name(path.name + ".lock").open("a+b") as f:
        if os.name == "nt":
            import msvcrt
            # LK_LOCK ждёт по секунде и сдаётся после 10 попыток — под нагрузкой медленно и падает.
            deadline = time.time() + LOCK_TIMEOUT
            while True:
                f.seek(0)
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.time() > deadline:
                        raise ExaError(f"не дождался блокировки {path.name}") from None
                    time.sleep(0.01)
            try:
                yield
            finally:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX)
            yield


def append_line(path: Path, line: str) -> None:
    # На Windows режим "a" не атомарен между процессами: без блокировки записи затирались.
    with file_lock(path), path.open("ab") as f:
        f.write((line + "\n").encode("utf-8"))


def log(msg: str) -> None:
    append_line(LOG_FILE, f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}")


def load_keys() -> list[Key]:
    keys: list[Key] = []
    if ENV_FILE.exists():
        for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            var, value = (part.strip().strip('"') for part in line.split("=", 1))
            if var.startswith("EXA_KEY_") and value:
                keys.append(Key(var.removeprefix("EXA_KEY_").lower(), value))
    env_value = os.environ.get("EXA_API_KEY", "")
    if env_value and env_value not in {k.value for k in keys}:
        keys.append(Key("env", env_value))
    return keys


def load_state() -> dict:
    with file_lock(STATE_FILE):
        return _read_state()


def _read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"dead": {}, "runs": {}}


def update_state(change) -> None:
    with file_lock(STATE_FILE):  # чтение и запись одним куском, иначе процессы теряют правки
        state = _read_state()
        change(state)
        STATE_FILE.write_text(json.dumps(state, indent=1), encoding="utf-8")


def mark_dead(name: str, status: int) -> None:
    until = time.time() + COOLDOWN[status]
    update_state(lambda s: s["dead"].__setitem__(name, {"until": until, "status": status}))
    log(f"key {name}: {status}, отложен на {COOLDOWN[status]} с")


def key_order(pinned: str | None = None) -> list[Key]:
    """Живые ключи по приоритету, затем упавшие (вдруг уже ожили)."""
    keys = load_keys()
    dead = load_state()["dead"]
    now = time.time()
    alive = [k for k in keys if dead.get(k.name, {}).get("until", 0) <= now]
    order = alive + sorted((k for k in keys if k not in alive),
                           key=lambda k: dead[k.name]["until"])
    if pinned:  # agent run виден только ключу той команды, что его запустила
        order = [k for k in order if k.name == pinned] or order
    return order


def http(method: str, path: str, key: Key, body: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(
        API + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"x-api-key": key.value, "Content-Type": "application/json",
                 "User-Agent": USER_AGENT},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"error": raw[:300]}
    except OSError as e:
        raise ExaError(f"нет связи с {API}: {e}") from e


def is_key_error(status: int, data: dict) -> bool:
    # 403 от Cloudflare (error 1010) — не про ключ.
    return status in COOLDOWN and "1010" not in json.dumps(data)[:500]


def call(method: str, path: str, body: dict | None = None,
         pinned: str | None = None) -> tuple[Key, dict]:
    keys = key_order(pinned)
    if not keys:
        raise ExaError(f"нет ключей. Добавь: python exa.py add  (файл {ENV_FILE})")
    last = ""
    for round_no in range(RATE_LIMIT_ROUNDS + 1):
        only_rate_limited = True
        for i, key in enumerate(keys):
            status, data = http(method, path, key, body)
            if 200 <= status < 300:
                if i or round_no:
                    log(f"{path}: key {key.name} после {i} неудачных, круг {round_no + 1}")
                return key, data
            last = f"HTTP {status}: {json.dumps(data, ensure_ascii=False)[:300]}"
            if not is_key_error(status, data):
                raise ExaError(last)
            mark_dead(key.name, status)
            only_rate_limited &= status == 429
            if pinned:
                break
        # Все ключи упёрлись в лимит в секунду — подождать и пройти по кругу ещё раз.
        if not only_rate_limited or round_no == RATE_LIMIT_ROUNDS:
            break
        time.sleep(1.5 * (round_no + 1))
    raise ExaError(f"все ключи отказали, последний ответ {last}")


# --- команды ------------------------------------------------------------------

def cost(data: dict, key: Key) -> str:
    total = (data.get("costDollars") or {}).get("total")
    usage["cost"] = total if isinstance(total, (int, float)) else None
    usage["key"] = key.name
    return f"${total:.3f}" if isinstance(total, (int, float)) else "?"


def cmd_search(a: argparse.Namespace) -> None:
    a.query = a.query.strip()  # \r из строк Windows ломает и запрос, и журнал
    body: dict = {
        "query": a.query,
        "type": a.type,
        "numResults": a.n,
        "contents": {"highlights": {"maxCharacters": a.chars}},
    }
    if a.since:
        body["startPublishedDate"] = f"{a.since}T00:00:00.000Z"
    if a.domain:
        body["includeDomains"] = a.domain
    key, data = call("POST", "/search", body)
    results = data.get("results", [])
    usage["results"] = len(results)
    for i, r in enumerate(results, 1):
        date = (r.get("publishedDate") or "")[:10]
        emit(f"{i}. {r.get('title') or '—'} | {r.get('url')}" + (f" | {date}" if date else ""))
        text = " … ".join(h.strip() for h in r.get("highlights") or [])
        if text:
            emit("   " + re.sub(r"\s+", " ", text)[: a.chars])
    emit(f"[key {key.name}, {cost(data, key)}]")


def cmd_fetch(a: argparse.Namespace) -> None:
    key, data = call("POST", "/contents", {"urls": a.urls, "text": {"maxCharacters": a.chars}})
    results = data.get("results", [])
    usage["results"] = len(results)
    for r in results:
        emit(f"## {r.get('title') or '—'} | {r.get('url')}")
        emit((r.get("text") or "").strip() + "\n")
    for s in data.get("statuses", []):
        if s.get("status") != "success":
            emit(f"! {s.get('id')}: {s.get('error') or s.get('status')}")
    emit(f"[key {key.name}, {cost(data, key)}]")


def print_run(run: dict, key: Key) -> None:
    out = run.get("output") or {}
    if out.get("structured") is not None:
        emit(json.dumps(out["structured"], ensure_ascii=False, indent=1))
    elif out.get("text"):
        emit(out["text"])
    urls = {c["url"] for g in out.get("grounding") or [] for c in g.get("citations", [])}
    usage["results"] = len(urls)
    if urls and out.get("structured") is None:
        emit("\nИсточники:\n" + "\n".join(f"- {u}" for u in sorted(urls)))
    emit(f"[{run.get('id')}, key {key.name}, {run.get('stopReason')}, {cost(run, key)}]")


def cmd_agent(a: argparse.Namespace) -> None:
    if a.resume:
        run_id = a.resume
        pinned = load_state()["runs"].get(run_id)
    else:
        if not a.query:
            raise ExaError("нужна задача или --resume")
        body: dict = {"query": a.query, "effort": a.effort}
        if a.schema:
            text = Path(a.schema).read_text(encoding="utf-8") if Path(a.schema).is_file() else a.schema
            try:
                body["outputSchema"] = json.loads(text)
            except ValueError as e:
                raise ExaError(f"--schema: не файл и не JSON ({e})") from e
        key, run = call("POST", "/agent/runs", body)
        run_id = run["id"]
        update_state(lambda s: s["runs"].__setitem__(run_id, key.name))
        pinned = key.name
        print(f"[запущен {run_id}, key {key.name}, effort {a.effort}]", file=sys.stderr)

    deadline = time.time() + AGENT_TIMEOUT
    while True:
        key, run = call("GET", f"/agent/runs/{run_id}", pinned=pinned)
        if run.get("status") in ("completed", "failed", "cancelled"):
            break
        if time.time() > deadline:
            print(f"Ещё идёт. Продолжить: python exa.py agent --resume {run_id}")
            return
        time.sleep(POLL_SECONDS)
    if run["status"] != "completed":
        raise ExaError(f"run {run['status']}: {run.get('error')}")
    print_run(run, key)


def check(key: Key) -> int:
    status, _ = http("POST", "/search", key, {"query": "exa api", "numResults": 1, "type": "instant"})
    return status


def cmd_status(_: argparse.Namespace) -> None:
    keys = load_keys()
    if not keys:
        print(f"Ключей нет. Добавь: python exa.py add  (файл {ENV_FILE})")
        return
    dead = load_state()["dead"]
    print(f"Файл ключей: {ENV_FILE}")
    for k in keys:
        code = check(k)
        verdict = {200: "работает", 401: "неверный/отозван", 402: "нет баланса",
                   429: "лимит запросов"}.get(code, f"HTTP {code}")
        note = ""
        if k.name in dead:
            note = f", был отложен до {time.strftime('%d.%m %H:%M', time.localtime(dead[k.name]['until']))}"
        print(f"  {k.name:<12} {k.value[:4]}…  {verdict}{note}")
        if code == 200 and k.name in dead:
            update_state(lambda s, n=k.name: s["dead"].pop(n, None))


def next_number() -> str:
    numbers = [int(k.name) for k in load_keys() if k.name.isdigit()]
    return str(max(numbers, default=0) + 1)


def cmd_add(a: argparse.Namespace) -> None:
    name = a.name or next_number()
    if not NAME.match(name):
        raise ExaError("имя — латиница, цифры, _")
    var = f"EXA_KEY_{name.upper()}"
    if any(k.name == name.lower() for k in load_keys()):
        raise ExaError(f"{var} уже есть в {ENV_FILE}")
    value = getpass.getpass(f"Ключ для {var} (ввод скрыт): ").strip()
    if not value:
        raise ExaError("пустой ключ")
    code = check(Key(name, value))
    if code not in (200, 402):
        raise ExaError(f"Exa не принял ключ (HTTP {code}), не сохраняю")
    # Без перевода строки в конце .env новый ключ склеился бы с последней строкой.
    tail = ENV_FILE.read_bytes()[-1:] if ENV_FILE.exists() else b"\n"
    with ENV_FILE.open("a", encoding="utf-8") as f:
        f.write(("" if tail in (b"\n", b"") else "\n") + f"{var}={value}\n")
    print(f"Добавлен {var}" + (" (баланса нет, но ключ верный)" if code == 402 else ""))


def cmd_reset(_: argparse.Namespace) -> None:
    update_state(lambda s: s.__setitem__("dead", {}))
    print("Отметки «ключ упал» сняты")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(prog="exa.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="поиск с выдержками")
    s.add_argument("query")
    s.add_argument("-n", type=int, default=5, help="результатов (по умолчанию 5)")
    s.add_argument("--chars", type=int, default=400, help="символов выдержки на результат")
    s.add_argument("--type", default="auto", choices=["auto", "fast", "instant", "deep"])
    s.add_argument("--since", help="не раньше даты публикации, ГГГГ-ММ-ДД")
    s.add_argument("--domain", nargs="+", help="только эти домены")
    s.set_defaults(func=cmd_search)

    f = sub.add_parser("fetch", help="текст страниц по URL")
    f.add_argument("urls", nargs="+")
    f.add_argument("--chars", type=int, default=3000, help="символов на страницу")
    f.set_defaults(func=cmd_fetch)

    g = sub.add_parser("agent", help="многошаговый ресёрч Exa Agent")
    g.add_argument("query", nargs="?")
    g.add_argument("--effort", default="medium",
                   choices=["minimal", "low", "medium", "high", "xhigh"])
    g.add_argument("--schema", help="JSON Schema: путь к файлу или строка")
    g.add_argument("--resume", help="дождаться уже запущенного run")
    g.set_defaults(func=cmd_agent)

    sub.add_parser("status", help="проверить ключи").set_defaults(func=cmd_status)
    ad = sub.add_parser("add", help="добавить ключ (скрытый ввод)")
    ad.add_argument("name", nargs="?", help="по умолчанию следующий номер: EXA_KEY_3, ...")
    ad.set_defaults(func=cmd_add)
    sub.add_parser("reset", help="снять отметки «ключ упал»").set_defaults(func=cmd_reset)

    args = p.parse_args()
    started = time.time()
    error = None
    try:
        args.func(args)
    except ExaError as e:
        error = str(e)
        sys.exit(f"exa: {e}")
    finally:
        if args.cmd in ("search", "fetch", "agent"):
            record_usage(args, time.time() - started, error)


def record_usage(args: argparse.Namespace, seconds: float, error: str | None) -> None:
    """Одна строка на вызов: для сравнения Exa и WebSearch по цене, скорости, объёму."""
    detail = {"search": lambda: args.query, "fetch": lambda: " ".join(args.urls),
              "agent": lambda: args.query or f"resume {args.resume}"}[args.cmd]()
    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "cmd": args.cmd + (f":{args.effort}" if args.cmd == "agent" else ""),
        "project": os.getcwd(),
        "query": (detail or "")[:200],
        "results": usage.get("results"),
        "chars": usage["chars"],
        "cost": usage.get("cost"),
        "key": usage.get("key"),
        "seconds": round(seconds, 1),
        "error": error[:200] if error else None,
        # EXA_USAGE_TAG=bench помечает вызовы бенчмарка: report.py не смешивает их с работой.
        "tag": os.environ.get("EXA_USAGE_TAG") or None,
    }
    if "EXA_USAGE_FILE" in os.environ or USAGE_FILE.exists():  # журнал только по согласию
        append_line(USAGE_FILE, json.dumps(row, ensure_ascii=False))


if __name__ == "__main__":
    main()
