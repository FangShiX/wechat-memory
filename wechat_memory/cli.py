"""Command-line entry point. Offline by default; all workspaces stay local."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfoNotFoundError

from . import __version__
from .ai import AIError, configuration, summarize
from .analysis import aggregate, dump, read_jsonl, search, write_jsonl
from .chunking import ChunkConfig, build_chunks
from .dashboard import render
from .importers import ImportFailure, import_messages


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="WeChat Memory — 本地微信聊天导出分析与证据检索")
    root.add_argument("--version", action="version", version=__version__)
    sub = root.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="导入导出文件，构建会话片段、季度概览和本地网页")
    run.add_argument("inputs", type=Path, nargs="+", help="JSON/JSONL/CSV/TSV/TXT/已解密SQLite文件或目录（不递归）")
    run.add_argument("--output", type=Path, default=Path("outputs"), help="新的本地工作目录；拒绝覆盖现有目录")
    run.add_argument("--timezone", default="Asia/Shanghai", help="无时区时间及季度归属使用的 IANA 时区")
    run.add_argument("--encoding", help="指定文本编码；默认自动处理UTF-8/BOM/UTF-16/GB18030")
    run.add_argument("--mapping", type=Path, help="字段映射JSON文件，如 {\"text\":\"body\",\"timestamp\":\"sent_at\"}")
    run.add_argument("--table", help="SQLite消息表名")
    run.add_argument("--chat-id", help="将本次输入指定为同一个会话；合并同会话分片时使用")
    run.add_argument("--sender", action="append", help="仅保留该发送者ID/昵称；可多次使用")
    run.add_argument("--chat", action="append", help="仅保留该会话ID；可多次使用")
    run.add_argument("--skip-invalid", action="store_true", help="跳过坏记录，并在导入统计中记录文件和行号")
    run.add_argument("--gap-minutes", type=int, default=60, help="分片的时间间隔阈值")
    run.add_argument("--max-chars", type=int, default=1400, help="分片字符目标上限；单条长消息原样保留")
    run.add_argument("--ai", action="store_true", help="显式启用模型标注，会向配置的模型端点发送所选聊天原文")
    ask = sub.add_parser("ask", help="从本地工作目录检索原文证据；可选AI回答")
    ask.add_argument("question", help="问题或关键词")
    ask.add_argument("--workspace", type=Path, default=Path("outputs"))
    ask.add_argument("--chat", help="会话ID")
    ask.add_argument("--sender", help="发送者ID或昵称")
    ask.add_argument("--quarter", help="季度，例如2025-Q1")
    ask.add_argument("--limit", type=int, default=8)
    ask.add_argument("--json", action="store_true", help="输出结构化证据（包含所选消息原文）")
    ask.add_argument("--ai", action="store_true", help="向配置模型端点发送检索到的消息并生成引用回答")
    return root


def run_pipeline(args: argparse.Namespace) -> dict:
    if args.gap_minutes < 1 or args.max_chars < 1:
        raise ValueError("--gap-minutes and --max-chars must be positive")
    target = args.output.resolve()
    if target.exists():
        raise ValueError("output already exists; choose a new --output directory")
    if args.ai:
        configuration()  # Validate before reading any private inputs.
    mapping = json.loads(args.mapping.read_text(encoding="utf-8-sig")) if args.mapping else {}
    if not isinstance(mapping, dict):
        raise ValueError("mapping must be a JSON object")
    result = import_messages(args.inputs, timezone_name=args.timezone, encoding=args.encoding,
                             mapping=mapping, table=args.table, chat_id=args.chat_id,
                             senders=args.sender, chats=args.chat, skip_invalid=args.skip_invalid)
    config = ChunkConfig(max_gap_minutes=args.gap_minutes,
                         strong_break_minutes=args.gap_minutes * 4,
                         max_chars=args.max_chars, target_chars=min(700, args.max_chars))
    chunks = build_chunks(result.messages, config)
    annotations = []
    if args.ai:
        by_id = {m["id"]: m for m in result.messages}
        for chunk in chunks:
            selected = [by_id[mid] for mid in chunk["message_ids"] if by_id[mid]["has_text"] and not by_id[mid].get("is_media")]
            if selected:
                annotation = summarize(selected)
                annotations.append({"chunk_id": chunk["chunk_id"], **annotation})
    analysis = aggregate(result.messages, chunks, annotations)
    analysis["timezone"] = args.timezone
    report = {"messages": len(result.messages), "duplicates": result.duplicates,
              "metadata": result.metadata, "filtered": result.filtered, "invalid": result.invalid,
              "ai_enabled": args.ai}
    # Stage all outputs, then rename as a unit. Failed runs never leave a usable-looking partial workspace.
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".wechat-memory-", dir=target.parent))
    try:
        os.chmod(staging, 0o700)
        write_jsonl(staging / "messages.jsonl", result.messages)
        write_jsonl(staging / "thought_chunks.jsonl", chunks)
        dump(staging / "analysis.json", analysis)
        dump(staging / "timeline.json", analysis["timeline"])
        dump(staging / "concept_cards.json", analysis["concept_cards"])
        dump(staging / "import_report.json", report)
        render(staging / "dashboard.html", analysis, result.messages)
        # No replacement of an existing populated workspace is permitted.
        if target.exists():
            raise ValueError("output appeared during the run; choose another directory")
        staging.rename(target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return {"messages": len(result.messages), "chunks": len(chunks), "chats": analysis["chat_count"],
            "duplicates": result.duplicates, "skipped_invalid": len(result.invalid), "dashboard": str(target / "dashboard.html")}


def answer(args: argparse.Namespace) -> dict:
    if not args.question.strip() or not 1 <= args.limit <= 100:
        raise ValueError("provide a nonempty question and a --limit between 1 and 100")
    if args.ai:
        configuration()
    messages = read_jsonl(args.workspace / "messages.jsonl")
    hits = search(messages, args.question.strip(), chat=args.chat, sender=args.sender,
                  quarter=args.quarter, limit=args.limit)
    result = {"question": args.question, "method": "lexical_retrieval", "evidence": hits,
              "answer": "找到以下字面匹配证据，请结合上下文阅读。" if hits else "证据不足：未找到匹配的聊天内容。"}
    if args.ai and hits:
        result["generation"] = summarize(hits, args.question)
        result["answer"] = result["generation"]["summary"]
    return result


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    try:
        if args.command == "run":
            result = run_pipeline(args)
            print(f"完成：{result['messages']} 条消息，{result['chunks']} 个片段，{result['chats']} 个会话。")
            if result["duplicates"] or result["skipped_invalid"]:
                print(f"去重 {result['duplicates']} 条；跳过坏记录 {result['skipped_invalid']} 条。")
            print(f"本地页面：{result['dashboard']}")
        else:
            result = answer(args)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
            else:
                print(result["answer"])
                for hit in result["evidence"]:
                    print(f"\n[{hit['id']}] {hit['datetime']} · {hit['chat_id']} · {hit['sender_name']}\n{hit['text'][:500]}")
                if "generation" in result:
                    print("\n" + result["generation"]["uncertainty"])
    except (ValueError, OSError, LookupError, ZoneInfoNotFoundError) as exc:
        # Avoid dumping private data, API keys, response bodies or local absolute paths from exceptions.
        if isinstance(exc, (ImportFailure, AIError)):
            message = str(exc)
        elif isinstance(exc, ZoneInfoNotFoundError):
            message = "unknown timezone; use an IANA name and install tzdata on Windows"
        elif isinstance(exc, (json.JSONDecodeError, UnicodeError)):
            message = "invalid JSON or text encoding; check the input and --encoding"
        elif type(exc) is ValueError:
            message = str(exc)
        else:
            message = "cannot read/write the requested files; check paths, permissions and export format"
        print(f"错误：{message}", file=sys.stderr)
        raise SystemExit(1) from None
