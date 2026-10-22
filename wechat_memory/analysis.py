"""Offline aggregates and lexical evidence retrieval, with no inferred psychology."""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from .chunking import compute_seed_topics, extract_similarity_tokens


def dump(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def write_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in records:
            handle.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def aggregate(messages: list[dict], chunks: list[dict], annotations: list[dict]) -> dict:
    by_quarter = defaultdict(list)
    for message in messages:
        by_quarter[message["quarter"]].append(message)
    by_chat = defaultdict(list)
    for message in messages:
        by_chat[message["chat_id"]].append(message)
    timeline = []
    concept_map = defaultdict(list)
    for quarter, records in sorted(by_quarter.items()):
        texts = [m["text"] for m in records if m["has_text"] and not m.get("is_media")]
        topics = compute_seed_topics(texts, limit=12)
        item = {"quarter": quarter, "message_count": len(records), "text_message_count": len(texts),
                "chat_count": len({m["chat_id"] for m in records}),
                "sender_count": len({m["sender_id"] for m in records}),
                "start_date": records[0]["date"], "end_date": records[-1]["date"],
                "keywords": topics, "small_sample": len(texts) < 20,
                "chunk_ids": [c["chunk_id"] for c in chunks if c["quarter"] == quarter]}
        timeline.append(item)
    # Build a shared candidate vocabulary, then recount against every quarter.
    # Restricting counts to each quarter's top words would hide earlier occurrences.
    candidates = {topic for item in timeline for topic in item["keywords"]}
    for topic in sorted(candidates):
        for quarter, records in sorted(by_quarter.items()):
            matching = [m for m in records if not m.get("is_media") and topic.casefold() in m["text"].casefold()]
            if matching:
                concept_map[topic].append({"quarter": quarter, "message_ids": [m["id"] for m in matching],
                                           "count": len(matching)})
    # Occurrence cards, not claims about a person's beliefs or changes of stance.
    concepts = [{"concept": topic, "method": "lexical_occurrence", "quarters": occurrences,
                 "message_count": sum(o["count"] for o in occurrences)}
                for topic, occurrences in sorted(concept_map.items())]
    concepts.sort(key=lambda c: (-len(c["quarters"]), -c["message_count"], c["concept"]))
    return {"version": "1.0.0", "method": "local_lexical", "message_count": len(messages),
            "chunk_count": len(chunks), "chat_count": len(by_chat),
            "non_text_or_empty_count": sum(not m["has_text"] or bool(m.get("is_media")) for m in messages),
            "chats": [{"chat_id": chat, "message_count": len(rows),
                       "senders": dict(Counter(m["sender_id"] for m in rows))} for chat, rows in sorted(by_chat.items())],
            "timeline": timeline, "concept_cards": concepts, "annotations": annotations,
            "limitations": ["关键词复现是检索线索，不代表观念、立场或心理变化。",
                            "群聊与双方对话包含多位参与者；个人分析请先使用发送者筛选。",
                            "图片、语音、视频保留导出的内容或占位符，不执行OCR、转写或媒体下载。",
                            "AI标注仅在显式启用时生成，置信度未经校准，需结合引用核查。"]}


def query_terms(question: str) -> list[str]:
    terms = set(extract_similarity_tokens(question))
    terms.update(re.findall(r"[a-zA-Z][a-zA-Z0-9_-]+", question.lower()))
    for span in re.findall(r"[\u4e00-\u9fff]{2,}", question):
        # Bigrams provide recall for natural-language Chinese questions.
        terms.update(span[i:i + 2] for i in range(len(span) - 1))
    return sorted(terms)


def search(messages: list[dict], question: str, *, chat: str | None = None, sender: str | None = None,
           quarter: str | None = None, limit: int = 8) -> list[dict]:
    terms = query_terms(question)
    ranked = []
    for message in messages:
        if chat and message["chat_id"] != chat:
            continue
        if sender and sender not in {message["sender_id"], message["sender_name"]}:
            continue
        if quarter and message["quarter"] != quarter:
            continue
        text = message["text"].casefold()
        score = sum(min(len(term), 12) for term in terms if term.casefold() in text)
        if question.casefold() in text:
            score += 20
        if score:
            ranked.append((score, message))
    ranked.sort(key=lambda item: (-item[0], item[1]["timestamp"], item[1]["id"]))
    return [{**record, "retrieval_score": score} for score, record in ranked[:limit]]
