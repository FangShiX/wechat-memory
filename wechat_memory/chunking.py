#!/usr/bin/env python3
"""Build thought chunks and quarter index from normalized messages."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Dict, List, Sequence, Set


ASCII_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_./+-]{1,31}")
NUMBER_WORD_RE = re.compile(r"\b\d[\d\-./]{1,15}\b")
CHINESE_SPAN_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
SEGMENT_SPLIT_RE = re.compile(r"[\s,，。；;：:！!？?\(\)\[\]【】<>《》\"“”'‘’/\\|]+")
GENERIC_TOPIC_STOPWORDS = {
    "",
    "的",
    "了",
    "是",
    "我",
    "你",
    "他",
    "她",
    "它",
    "们",
    "就",
    "也",
    "都",
    "很",
    "和",
    "与",
    "及",
    "在",
    "有",
    "把",
    "被",
    "着",
    "而",
    "但",
    "并",
    "并且",
    "如果",
    "因为",
    "所以",
    "一个",
    "一种",
    "这个",
    "那个",
    "自己",
    "我们",
    "你们",
    "他们",
    "一个人",
    "一种人",
    "这个人",
    "那个时候",
    "就是说",
    "为什么",
    "怎么回事",
    "什么东西",
    "有时候",
    "的时候",
    "这样的话",
    "转发的聊天记录",
    "聊天记录",
    "群聊的聊天记录",
    "消息",
}
CHINESE_BOUNDARY_WORDS = {
    "的",
    "了",
    "和",
    "与",
    "及",
    "在",
    "是",
    "把",
    "被",
    "对",
    "就",
    "而",
    "但",
    "并",
    "并且",
    "因为",
    "所以",
    "如果",
    "或者",
    "以及",
    "还有",
    "然后",
    "就是",
    "这是",
    "那个",
    "这个",
}
MATERIAL_MARKERS = {
    "[转发的聊天记录]",
    "[聊天记录]",
    "聊天记录",
    "群聊的聊天记录",
}


@dataclass(frozen=True)
class ChunkConfig:
    max_gap_minutes: int = 60
    strong_break_minutes: int = 2880
    target_chars: int = 700
    max_chars: int = 1400
    min_messages: int = 2
    min_chars: int = 80
    similarity_threshold: float = 0.18
    low_similarity_threshold: float = 0.05
    long_message_chars: int = 120
    lookback_messages: int = 6
    standalone_material_chars: int = 800
    material_newline_threshold: int = 8
    material_colon_line_threshold: int = 4
    material_line_threshold: int = 12
    chunk_version: str = "v1.0"

    @property
    def max_gap_seconds(self) -> int:
        return self.max_gap_minutes * 60

    @property
    def strong_break_seconds(self) -> int:
        return self.strong_break_minutes * 60


def split_chinese_span(span: str) -> List[str]:
    pieces = [span]
    for boundary in sorted(CHINESE_BOUNDARY_WORDS, key=lambda s: (-len(s), s)):
        updated: List[str] = []
        for piece in pieces:
            updated.extend(part for part in piece.split(boundary) if part)
        pieces = updated or pieces
    return [piece for piece in pieces if 2 <= len(piece) <= 12]


def is_bad_topic(token: str) -> bool:
    if not token:
        return True
    if token in GENERIC_TOPIC_STOPWORDS:
        return True
    if "聊天记录" in token:
        return True
    if token.endswith("与") or token.endswith("的"):
        return True
    if len(token) == 1:
        return True
    if len(token) > 24:
        return True
    if token.isdigit() and len(token) < 2:
        return True
    if all(char == token[0] for char in token):
        return True
    return False


def extract_topic_candidates(text: str) -> List[str]:
    if not text:
        return []

    candidates: List[str] = []
    segments = [
        segment.strip(" -•0123456789.()（）")
        for segment in SEGMENT_SPLIT_RE.split(text.replace("\n", " "))
        if segment.strip()
    ]
    for segment in segments:
        lower_segment = segment.lower()
        for token in ASCII_WORD_RE.findall(segment):
            token = token.lower()
            if len(token) >= 2:
                candidates.append(token)
        for token in NUMBER_WORD_RE.findall(segment):
            candidates.append(token)
        for span in CHINESE_SPAN_RE.findall(segment):
            if 2 <= len(span) <= 8:
                candidates.append(span)
                continue
            for piece in split_chinese_span(span):
                if 2 <= len(piece) <= 8:
                    candidates.append(piece)
        if (
            2 <= len(segment) <= 12
            and CHINESE_SPAN_RE.fullmatch(segment)
            and lower_segment not in GENERIC_TOPIC_STOPWORDS
        ):
            candidates.append(segment)

    deduped: List[str] = []
    seen = set()
    for candidate in candidates:
        if is_bad_topic(candidate):
            continue
        if candidate not in seen:
            deduped.append(candidate)
            seen.add(candidate)
    return deduped


def extract_similarity_tokens(text: str) -> Set[str]:
    return set(extract_topic_candidates(text))


def token_estimate(text: str) -> int:
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 4))


def compute_seed_topics(texts: Sequence[str], limit: int = 6) -> List[str]:
    counts: Counter[str] = Counter()
    message_presence: Counter[str] = Counter()

    for text in texts:
        candidates = extract_topic_candidates(text)
        counts.update(candidates)
        message_presence.update(set(candidates))

    if not counts:
        return []

    ranked = sorted(
        counts.items(),
        key=lambda item: (
            item[1] * 3 + message_presence[item[0]] * 2,
            min(len(item[0]), 8),
            -len(item[0]),
            item[0],
        ),
        reverse=True,
    )

    topics: List[str] = []
    for token, _ in ranked:
        if any(token in existing or existing in token for existing in topics):
            continue
        topics.append(token)
        if len(topics) >= limit:
            break
    return topics


def build_chunk_id(message_ids: Sequence[str], chunk_version: str) -> str:
    digest = hashlib.sha1(
        json.dumps({"message_ids": list(message_ids), "chunk_version": chunk_version}, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return f"chunk-{chunk_version}-{digest[:16]}"


def jaccard_similarity(left: Set[str], right: Set[str]) -> float:
    if not left or not right:
        return 0.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def chunk_text_length(messages: Sequence[Dict[str, object]]) -> int:
    return sum(len(str(message["text"])) for message in messages)


def recent_chunk_tokens(messages: Sequence[Dict[str, object]], lookback_messages: int) -> Set[str]:
    tokens: Set[str] = set()
    for message in messages[-lookback_messages:]:
        tokens |= extract_similarity_tokens(str(message["text"]))
    return tokens


def is_material_message(message: Dict[str, object], config: ChunkConfig) -> bool:
    text = str(message["text"])
    line_count = int(message.get("line_count", text.count("\n") + 1 if text else 0))
    url_count = int(message.get("url_count", text.count("http://") + text.count("https://")))
    colon_line_count = sum(1 for line in text.splitlines() if ":" in line or "：" in line)
    return bool(
        message.get("is_material")
        or any(marker in text for marker in MATERIAL_MARKERS)
        or len(text) >= config.standalone_material_chars
        or (len(text) >= 300 and line_count >= config.material_newline_threshold)
        or (line_count >= config.material_line_threshold and colon_line_count >= config.material_colon_line_threshold)
        or (line_count >= config.material_newline_threshold and url_count >= 2)
    )


def chunk_has_material(messages: Sequence[Dict[str, object]], config: ChunkConfig) -> bool:
    return any(is_material_message(message, config) for message in messages)


def should_split(
    current_chunk: Sequence[Dict[str, object]],
    candidate: Dict[str, object],
    config: ChunkConfig,
) -> bool:
    if not current_chunk:
        return False

    previous = current_chunk[-1]
    gap = int(candidate["timestamp"]) - int(previous["timestamp"])
    if str(candidate["quarter"]) != str(current_chunk[0]["quarter"]):
        return True
    if gap >= config.strong_break_seconds:
        return True

    candidate_text = str(candidate["text"])
    candidate_len = len(candidate_text)
    current_len = chunk_text_length(current_chunk)
    current_count = len(current_chunk)
    candidate_is_material = is_material_message(candidate, config)
    current_has_material = chunk_has_material(current_chunk, config)
    previous_is_material = is_material_message(previous, config)

    recent_tokens = recent_chunk_tokens(current_chunk, config.lookback_messages)
    candidate_tokens = extract_similarity_tokens(candidate_text)
    similarity = jaccard_similarity(recent_tokens, candidate_tokens)

    enough_context = current_count >= config.min_messages or current_len >= config.min_chars
    too_long = current_len >= config.max_chars
    target_reached = current_len >= config.target_chars
    weak_link = similarity <= config.low_similarity_threshold and candidate_len >= config.long_message_chars
    low_link = similarity < config.similarity_threshold
    exceeded_gap = gap >= config.max_gap_seconds

    if too_long or current_len + candidate_len > config.max_chars:
        return True
    if current_has_material and not candidate_is_material:
        return True
    if candidate_is_material and current_count > 0:
        return True
    if previous_is_material:
        return True
    if exceeded_gap and enough_context:
        return True
    if weak_link and enough_context:
        return True
    if target_reached and low_link:
        return True
    return False


def finalize_chunk(messages: Sequence[Dict[str, object]], config: ChunkConfig, build_run_id: str) -> Dict[str, object]:
    texts = [str(message["text"]) for message in messages]
    message_ids = [str(message["id"]) for message in messages]
    combined_text = "\n".join(texts)
    start = messages[0]
    end = messages[-1]
    quarter = str(start["quarter"])
    year = int(quarter[:4])
    has_material = chunk_has_material(messages, config)
    return {
        "chunk_id": build_chunk_id(message_ids, config.chunk_version),
        "start_timestamp": int(start["timestamp"]),
        "end_timestamp": int(end["timestamp"]),
        "year": year,
        "quarter": quarter,
        "message_ids": message_ids,
        "texts": texts,
        "combined_text": combined_text,
        "token_estimate": token_estimate(combined_text),
        "seed_topics": compute_seed_topics([str(m["text"]) for m in messages if not m.get("is_media")]),
        "message_count": len(messages),
        "start_message_id": str(start["id"]),
        "end_message_id": str(end["id"]),
        "time_span_seconds": int(end["timestamp"]) - int(start["timestamp"]),
        "chunk_strategy": "time_semantic_material_v2",
        "chunk_version": config.chunk_version,
        "source": "wechat",
        "chat_id": str(start["chat_id"]),
        "sender_ids": sorted({str(m["sender_id"]) for m in messages}),
        "upstream_message_hashes": [str(message["content_hash"]) for message in messages],
        "build_run_id": build_run_id,
        "contains_material_message": has_material,
        "material_message_ids": [
            str(message["id"]) for message in messages if is_material_message(message, config)
        ],
    }


def _build_chat_chunks(messages: Sequence[Dict[str, object]], config: ChunkConfig, build_run_id: str) -> List[Dict[str, object]]:
    chunks: List[Dict[str, object]] = []
    current: List[Dict[str, object]] = []
    for message in messages:
        if should_split(current, message, config):
            chunks.append(finalize_chunk(current, config, build_run_id))
            current = []
        current.append(message)
    if current:
        chunks.append(finalize_chunk(current, config, build_run_id))
    return chunks


def build_chunks(messages, config=None, build_run_id="local"):
    config = config or ChunkConfig()
    if config.max_chars < 1 or config.max_gap_minutes < 1:
        raise ValueError("chunk limits must be positive")
    chats = defaultdict(list)
    for message in messages:
        chats[message["chat_id"]].append(message)
    chunks = []
    for records in chats.values():
        records.sort(key=lambda m: (m["timestamp"], m["id"]))
        chunks.extend(_build_chat_chunks(records, config, build_run_id))
    return sorted(chunks, key=lambda c: (c["start_timestamp"], c["chat_id"], c["chunk_id"]))
