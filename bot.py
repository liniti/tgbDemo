#!/usr/bin/env python3
"""Сбор заявок из JSON и доставка уведомлений в Telegram и вебхук."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from transports import send_telegram, send_webhook, telegram_text


DESTINATIONS = ("telegram", "webhook")
FIELDS = {"id", "title", "amount", "currency", "created_at"}


def validate_item(item: object, where: str = "заявка") -> dict[str, str]:
    if not isinstance(item, dict) or set(item) != FIELDS:
        raise ValueError(f"{where}: ожидаются ровно поля {', '.join(sorted(FIELDS))}")
    
    if not isinstance(item["id"], str) or not item["id"].strip():
        raise ValueError(f"{where}: id должен быть непустой строкой")
    
    for key in FIELDS - {"id"}:
        if not isinstance(item[key], str):
            raise ValueError(f"{where}: поле {key} должно быть строкой")
        
    try:
        datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{where}: created_at должен быть датой ISO 8601") from exc
    
    return item


def read_source(path: str | Path) -> list[dict[str, str]]:
    
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"не удалось прочитать JSON-экспорт {path}: {exc}") from exc
    
    if not isinstance(data, list):
        raise ValueError("JSON-экспорт должен содержать массив заявок")
    
    return [
        validate_item(item, f"заявка #{index}")
        for index, item in enumerate(data, 1)
    ]


def open_database(path: str | Path) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=30)
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS requests (
            id TEXT PRIMARY KEY,
            payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS deliveries (
            request_id TEXT NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
            destination TEXT NOT NULL CHECK (destination IN ('telegram', 'webhook')),
            delivered_at TEXT,
            PRIMARY KEY (request_id, destination)
        );
    """)
    return db


def store_items(db: sqlite3.Connection, items: list[dict[str, str]]) -> tuple[int, int]:
    """Сохраняет новые заявки и обе pending-доставки одной транзакцией."""
    added = 0
    duplicates = 0
    
    with db:
        for item in items:
            payload = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            cursor = db.execute(
                "INSERT OR IGNORE INTO requests(id, payload) VALUES (?, ?)",
                (item["id"], payload),
            )
            
            added += cursor.rowcount
            duplicates += cursor.rowcount == 0
            
            for destination in DESTINATIONS:
                db.execute(
                    "INSERT OR IGNORE INTO deliveries(request_id, destination) "
                    "VALUES (?, ?)",
                    (item["id"], destination),
                )
    
    return added, duplicates


Sender = Callable[[dict[str, str]], None]


def process_pending(
    db: sqlite3.Connection, senders: dict[str, Sender]
) -> tuple[int, int]:
    """Отправляет pending по каждому адресу отдельно; фиксирует только успех."""
    
    rows = db.execute("""
        SELECT r.id, r.payload, d.destination
        FROM deliveries AS d JOIN requests AS r ON r.id = d.request_id
        WHERE d.delivered_at IS NULL
        ORDER BY r.rowid, CASE d.destination WHEN 'telegram' THEN 0 ELSE 1 END
    """).fetchall()
    succeeded = failed = 0
    
    for request_id, payload, destination in rows:
        item = json.loads(payload)
        try:
            sender = senders[destination]
            sender(item)
        except Exception as exc:
            failed += 1
            print(
                f"Ошибка доставки {destination} для заявки {request_id}: {exc}",
                file=sys.stderr,
            )
            continue
        
        with db:
            db.execute(
                "UPDATE deliveries "
                "SET delivered_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE request_id = ? AND destination = ? AND delivered_at IS NULL",
                (request_id, destination),
            )
        
        succeeded += 1
    
    return succeeded, failed


def live_senders() -> dict[str, Sender]:
    def telegram(item: dict[str, str]) -> None:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        
        if not token or not chat_id:
            raise RuntimeError("задайте TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID")
        send_telegram(item, token, chat_id)

    def webhook(item: dict[str, str]) -> None:
        url = os.environ.get("INTERNAL_WEBHOOK_URL", "").strip()
        
        if not url:
            raise RuntimeError("задайте INTERNAL_WEBHOOK_URL")
        send_webhook(item, url)

    return {"telegram": telegram, "webhook": webhook}


def run_cycle(
    db: sqlite3.Connection, source: str, senders: dict[str, Sender]
) -> tuple[int, int, int, int]:
    items = read_source(source)
    
    added, duplicates = store_items(db, items)
    delivered, failed = process_pending(db, senders)
    
    return added, duplicates, delivered, failed


def demo() -> int:
    
    fixture = Path(__file__).resolve().parent / "examples" / "requests.json"
    items = read_source(fixture)
    with tempfile.TemporaryDirectory(prefix="telegram-demo-") as temp:
        db = open_database(Path(temp) / "demo.sqlite3")
        added, duplicates = store_items(db, items)
    
        print(f"Демо: новых заявок {added}, уже известных {duplicates}")

        def local_telegram(item: dict[str, str]) -> None:
            print(f"  Telegram (локальная имитация): {telegram_text(item)}")

        def local_webhook(item: dict[str, str]) -> None:
            body = json.dumps(item, ensure_ascii=False)
            print(f"  Вебхук (локальная имитация): {body}")

        delivered, failed = process_pending(
            db, {"telegram": local_telegram, "webhook": local_webhook}
        )
        print(f"Первый проход: успешно доставок {delivered}, ошибок {failed}")
        again, again_failed = process_pending(
            db, {"telegram": local_telegram, "webhook": local_webhook}
        )
        
        pending = db.execute(
            "SELECT count(*) FROM deliveries WHERE delivered_at IS NULL"
        ).fetchone()[0]
        print(
            f"Второй проход: новых отправок {again}, ошибок {again_failed}; "
            "повторов нет"
        )
        db.close()
        return int(failed > 0 or again_failed > 0 or again != 0 or pending != 0)


def positive_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "интервал должен быть положительным числом секунд"
        ) from exc
    
    if seconds <= 0:
        raise argparse.ArgumentTypeError("интервал должен быть больше нуля")
    
    return seconds


def command_run(args: argparse.Namespace) -> int:
    try:
        db = open_database(args.db)
    except sqlite3.Error as exc:
        print(f"Не удалось открыть SQLite: {exc}", file=sys.stderr)
        return 2
    
    senders = live_senders()
    exit_code = 0
    
    try:
        while True:
            try:
                added, duplicates, delivered, failed = run_cycle(
                    db, args.source, senders
                )
                print(
                    f"Цикл: заявок новых {added}, дублей {duplicates}; "
                    f"доставок успешно {delivered}, ошибок {failed}"
                )
                if failed:
                    exit_code = 1
            except (ValueError, sqlite3.Error) as exc:
                print(f"Ошибка цикла: {exc}", file=sys.stderr)
                exit_code = 2
            if args.once:
                break
    
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("Остановка по запросу пользователя")
    
    finally:
        db.close()
    
    return exit_code


def make_parser() -> argparse.ArgumentParser:
    
    parser = argparse.ArgumentParser(description="Мониторинг заявок из JSON-экспорта")
    
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("demo", help="локально показать сбор и обе имитации доставки")
    
    run = sub.add_parser("run", help="запустить периодический сбор или один цикл")
    run.add_argument("--source", required=True, help="путь к JSON-массиву заявок")
    run.add_argument("--db", required=True, help="путь к SQLite-базе")
    run.add_argument(
        "--once", action="store_true", help="выполнить один цикл и завершиться"
    )
    
    run.add_argument(
        "--interval",
        type=positive_seconds,
        default=60.0,
        help="пауза между циклами в секундах (по умолчанию: 60)",
    )
    
    return parser


def main(argv: list[str] | None = None) -> int:
    
    args = make_parser().parse_args(argv)
    
    if args.command == "demo":
        return demo()
    
    return command_run(args)


if __name__ == "__main__":
    raise SystemExit(main())
