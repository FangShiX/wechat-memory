"""Export adapters. Never opens a live WeChat profile or decrypts a database."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo


ALIASES = {
    "timestamp": ["timestamp", "time", "createTime", "CreateTime", "create_time", "StrTime", "MsgTime", "datetime", "时间", "消息时间"],
    "text": ["content", "text", "StrContent", "strContent", "Content", "message", "内容", "消息内容"],
    "sender": ["sender", "sender_id", "senderId", "Sender", "wxid", "talker", "发送人", "发送者"],
    "sender_name": ["sender_name", "senderName", "accountName", "NickName", "nickname", "昵称", "发送者昵称"],
    "chat_id": ["chat_id", "chatId", "chatroom", "chatroom_id", "conversation_id", "StrTalker", "strTalker", "TalkerId", "会话", "群名"],
    "platform_id": ["platformMessageId", "msgSvrId", "MsgSvrID", "msg_id", "msgId", "localId", "id", "消息ID"],
    "type": ["type", "Type", "MsgType", "消息类型"],
}
SUFFIXES = {".json", ".jsonl", ".ndjson", ".csv", ".tsv", ".txt", ".db", ".sqlite", ".sqlite3"}
TEXT_HEADERS = (
    re.compile(r"^\[?(\d{4}[-/]\d{1,2}[-/]\d{1,2}[ T]\d{1,2}:\d{2}(?::\d{2})?)\]?\s+(.+?)(?:[：:]\s*(.*))?$"),
    re.compile(r"^(.+?)\s+(\d{4}[-/]\d{1,2}[-/]\d{1,2}[ T]\d{1,2}:\d{2}(?::\d{2})?)\s*$"),
)


class ImportFailure(ValueError):
    pass


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]


def parse_time(value: Any, tz: ZoneInfo) -> int:
    if isinstance(value, bool) or value is None:
        raise ValueError("missing or invalid timestamp")
    text = str(value).strip()
    if re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        number = float(text)
        if not math.isfinite(number):
            raise ValueError("non-finite timestamp")
        # Seconds, milliseconds, microseconds and nanoseconds.
        while abs(number) >= 100_000_000_000:
            number /= 1000
        result = int(number)
        datetime.fromtimestamp(result, timezone.utc)
        return result
    dt = datetime.fromisoformat(text.replace("/", "-").replace("Z", "+00:00"))
    if dt.tzinfo is None:
        # Reject DST gaps and folds: an explicit offset disambiguates them.
        first = dt.replace(tzinfo=tz, fold=0)
        second = dt.replace(tzinfo=tz, fold=1)
        roundtrip = first.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None)
        if roundtrip != dt or first.utcoffset() != second.utcoffset():
            raise ValueError("ambiguous/nonexistent local time; supply an explicit UTC offset")
        dt = first
    return int(dt.timestamp())


def decode(path: Path, encoding: str | None) -> str:
    raw = path.read_bytes()
    if encoding:
        return raw.decode(encoding)
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("gb18030")


def choose(row: dict, key: str, mapping: dict) -> Any:
    candidates = [mapping[key]] if key in mapping else ALIASES[key]
    for candidate in candidates:
        if candidate in row and row[candidate] is not None and row[candidate] != "":
            return row[candidate]
    return None


def envelope(payload: Any, default_chat: str) -> tuple[list, str]:
    if isinstance(payload, list):
        return payload, default_chat
    if isinstance(payload, dict):
        meta = payload.get("meta", {})
        if not isinstance(meta, dict):
            meta = {}
        chat = str(payload.get("chat_id") or payload.get("chatId") or meta.get("name") or default_chat)
        for key in ("messages", "records", "data"):
            if key in payload:
                value = payload[key]
                if isinstance(value, dict):
                    return envelope(value, chat)
                if isinstance(value, list):
                    return value, chat
                raise ImportFailure(f"{key} must be a list or object")
        return [payload], chat
    raise ImportFailure("JSON must contain an object or array")


def text_records(text: str) -> Iterator[tuple[int, Any]]:
    current = None
    for line_no, line in enumerate(text.splitlines(), 1):
        match = TEXT_HEADERS[0].match(line)
        reverse = False
        if not match:
            match = TEXT_HEADERS[1].match(line)
            reverse = True
        if match:
            if current:
                yield current
            if reverse:
                sender, stamp = match.groups()
                body = ""
            else:
                stamp, sender, body = match.groups()
            current = (line_no, {"timestamp": stamp, "sender": sender.strip(), "content": body or ""})
        elif current:
            current[1]["content"] += "\n" + line
        elif line.strip():
            yield line_no, ImportFailure("unrecognized TXT header")
    if current:
        yield current


def sqlite_records(path: Path, mapping: dict, table: str | None) -> Iterator[tuple[int, Any]]:
    # URI read-only prevents accidental creation or mutation, including WAL handling.
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        tables = [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        matches = []
        for name in tables:
            if table and name != table:
                continue
            quoted = '"' + name.replace('"', '""') + '"'
            columns = {r[1] for r in connection.execute(f"PRAGMA table_info({quoted})")}
            if all(any(c in columns for c in ([mapping[k]] if k in mapping else ALIASES[k])) for k in ("timestamp", "text")):
                matches.append(quoted)
        if len(matches) != 1:
            raise ImportFailure("select one compatible decrypted SQLite table with --table and/or --mapping")
        for index, row in enumerate(connection.execute(f"SELECT * FROM {matches[0]}"), 1):
            yield index, dict(row)
    except sqlite3.DatabaseError as exc:
        raise ImportFailure("cannot read SQLite; use a decrypted exported copy with WAL files, not a live/encrypted WeChat database") from exc
    finally:
        connection.close()


def read_records(path: Path, encoding: str | None, mapping: dict, table: str | None) -> Iterator[tuple[int, Any, str]]:
    suffix = path.suffix.lower()
    default_chat = path.stem
    if suffix in {".sqlite", ".sqlite3", ".db"}:
        iterator = sqlite_records(path, mapping, table)
    elif suffix == ".json":
        records, default_chat = envelope(json.loads(decode(path, encoding)), default_chat)
        iterator = enumerate(records, 1)
    elif suffix in {".jsonl", ".ndjson"}:
        def lines():
            for number, line in enumerate(decode(path, encoding).splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    yield number, json.loads(line)
                except json.JSONDecodeError:
                    yield number, ImportFailure("invalid JSON line")
        iterator = lines()
    elif suffix in {".csv", ".tsv"}:
        text = decode(path, encoding)
        delimiter = "\t" if suffix == ".tsv" else ","
        try:
            delimiter = csv.Sniffer().sniff(text[:8192], delimiters=",\t;").delimiter
        except csv.Error:
            pass
        iterator = enumerate(csv.DictReader(io.StringIO(text, newline=""), delimiter=delimiter), 2)
    elif suffix == ".txt":
        iterator = text_records(decode(path, encoding))
    else:
        raise ImportFailure(f"unsupported export extension: {suffix}")
    for number, row in iterator:
        yield number, row, default_chat


@dataclass
class ImportResult:
    messages: list[dict] = field(default_factory=list)
    duplicates: int = 0
    metadata: int = 0
    filtered: int = 0
    invalid: list[dict] = field(default_factory=list)


def normalize(row: Any, path: Path, number: int, chat: str, tz: ZoneInfo, mapping: dict, chat_override: str | None) -> dict | None:
    if isinstance(row, Exception):
        raise row
    if not isinstance(row, dict):
        raise ValueError("message must be an object")
    if row.get("_type") in {"header", "member", "footer", "metadata"}:
        return None
    stamp = parse_time(choose(row, "timestamp", mapping), tz)
    value = choose(row, "text", mapping)
    if isinstance(value, (dict, list)):
        # Keep structured payload visible instead of discarding non-text messages.
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    sender = choose(row, "sender", mapping)
    if isinstance(sender, dict):
        sender = sender.get("id") or sender.get("name") or "unknown"
    if sender is None:
        is_sender = row.get("IsSender", row.get("is_sender"))
        sender = "self" if str(is_sender).lower() in {"1", "true"} else "unknown"
    sender_name = str(choose(row, "sender_name", mapping) or sender)
    chat_id = str(chat_override or choose(row, "chat_id", mapping) or chat)
    kind = str(choose(row, "type", mapping) or "text")
    is_media = (kind.lower() in {"3", "34", "43", "47", "image", "voice", "video", "audio", "sticker"}
                or text in {"[图片]", "[语音]", "[视频]", "[动画表情]", "[表情]"})
    if text.startswith("[语音转文字]"):
        is_media = False
    platform_id = str(choose(row, "platform_id", mapping) or "")
    identity = [chat_id, platform_id] if platform_id else [chat_id, stamp, str(sender), text, kind]
    dt = datetime.fromtimestamp(stamp, timezone.utc)
    local = dt.astimezone(tz)
    return {
        "id": "msg-" + digest(identity), "platform_id": platform_id,
        "timestamp": stamp, "datetime": dt.isoformat().replace("+00:00", "Z"),
        "date": local.date().isoformat(), "quarter": f"{local.year}-Q{(local.month - 1) // 3 + 1}",
        "chat_id": chat_id, "sender_id": str(sender), "sender_name": sender_name,
        "text": text, "type": kind, "has_text": bool(text), "is_media": is_media,
        "source_file": path.name, "source_line": number,
        "content_hash": digest(text), "source": "wechat",
    }


def import_messages(paths: list[Path], *, timezone_name: str = "Asia/Shanghai", encoding: str | None = None,
                    mapping: dict | None = None, table: str | None = None, chat_id: str | None = None,
                    senders: list[str] | None = None, chats: list[str] | None = None,
                    skip_invalid: bool = False) -> ImportResult:
    tz = ZoneInfo(timezone_name)
    mapping = mapping or {}
    if any(k not in ALIASES or not isinstance(v, str) for k, v in mapping.items()):
        raise ImportFailure("mapping must map canonical field names to source column names")
    files = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted(p for p in path.iterdir() if p.is_file() and p.suffix.lower() in SUFFIXES))
        else:
            files.append(path)
    if not files:
        raise ImportFailure("no supported export files")
    result = ImportResult()
    known = {}
    for path in files:
        for number, row, default_chat in read_records(path, encoding, mapping, table):
            try:
                record = normalize(row, path, number, default_chat, tz, mapping, chat_id)
            except (ValueError, TypeError, OverflowError, OSError) as exc:
                # Do not echo the offending message or database contents into logs.
                issue = {"file": path.name, "line": number, "reason": "invalid message fields or timestamp"}
                if not skip_invalid:
                    raise ImportFailure(f"{path.name}:{number}: invalid message; check field mapping, timestamp and encoding") from exc
                result.invalid.append(issue)
                continue
            if record is None:
                result.metadata += 1
                continue
            if senders and record["sender_id"] not in senders and record["sender_name"] not in senders:
                result.filtered += 1
                continue
            if chats and record["chat_id"] not in chats:
                result.filtered += 1
                continue
            previous = known.get(record["id"])
            if previous:
                if any(previous[k] != record[k] for k in ("timestamp", "text", "sender_id", "type")):
                    raise ImportFailure(f"{path.name}:{number}: conflicting message ID; check --chat-id or export IDs")
                result.duplicates += 1
                continue
            known[record["id"]] = record
            result.messages.append(record)
    result.messages.sort(key=lambda m: (m["timestamp"], m["chat_id"], m["id"]))
    if not result.messages:
        raise ImportFailure("no messages imported; check the export format, mapping and filters")
    return result
