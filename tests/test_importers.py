import csv
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

from wechat_memory.importers import ImportFailure, import_messages, parse_time


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, value, encoding="utf-8"):
        path = self.root / name
        path.write_text(value, encoding=encoding)
        return path

    def messages(self, name, records):
        return self.write(name, json.dumps(records, ensure_ascii=False))

    def row(self, **kwargs):
        return {"timestamp": "2025-01-01 08:00:00", "content": "你好\n第二行", "sender": "alice", **kwargs}

    def test_json_envelopes_and_chatlab(self):
        for key in ("messages", "records", "data"):
            with self.subTest(key=key):
                path = self.messages("chat.json", {"meta": {"name": "reading"}, key: [self.row(sender={"id": "member"})]})
                msg = import_messages([path]).messages[0]
                self.assertEqual(msg["chat_id"], "reading")
                self.assertEqual(msg["sender_id"], "member")

    def test_jsonl_metadata_and_empty_media(self):
        rows = [{"_type": "header"}, {"_type": "member"}, self.row(content="", type=3)]
        path = self.write("chat.jsonl", "\n".join(json.dumps(r) for r in rows), "utf-8-sig")
        result = import_messages([path])
        self.assertEqual(result.metadata, 2)
        self.assertFalse(result.messages[0]["has_text"])
        self.assertEqual(result.messages[0]["type"], "3")

    def test_csv_quoted_newlines_and_chinese_gb18030(self):
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(["时间", "内容", "发送者", "会话"])
        writer.writerow(["2025-01-01 12:00:00", "hello, world\n你好", "朋友", "读书群"])
        path = self.write("chat.csv", stream.getvalue(), "gb18030")
        msg = import_messages([path]).messages[0]
        self.assertEqual(msg["text"], "hello, world\n你好")
        self.assertEqual(msg["chat_id"], "读书群")

    def test_tsv_utf16_and_issender(self):
        path = self.write("chat.tsv", "CreateTime\tStrContent\tIsSender\n1735689600\ttest\t1\n", "utf-16")
        self.assertEqual(import_messages([path]).messages[0]["sender_id"], "self")

    def test_txt_headers_multiline(self):
        path = self.write("chat.txt", "2025-01-01 08:00:00 Alice\n第一行\n第二行\n2025-01-01 08:01:00 Bob: 收到\nAlice 2025-01-01 08:02:00\n再次回复\n")
        records = import_messages([path]).messages
        self.assertEqual(len(records), 3)
        self.assertEqual(records[0]["sender_id"], "Alice")
        self.assertEqual(records[0]["text"], "第一行\n第二行")
        self.assertEqual(records[1]["text"], "收到")
        self.assertEqual(records[2]["sender_id"], "Alice")

    def test_txt_unknown_preamble_fails(self):
        path = self.write("chat.txt", "unsupported header\n2025-01-01 08:00:00 A\nbody")
        with self.assertRaises(ImportFailure):
            import_messages([path])
        self.assertEqual(len(import_messages([path], skip_invalid=True).invalid), 1)

    def test_bad_line_strict_or_skip_without_leaking(self):
        path = self.write("chat.jsonl", 'private invalid text\n' + json.dumps(self.row()))
        with self.assertRaises(ImportFailure) as context:
            import_messages([path])
        self.assertNotIn("private invalid text", str(context.exception))
        result = import_messages([path], skip_invalid=True)
        self.assertEqual(len(result.messages), 1)
        self.assertEqual(result.invalid[0]["line"], 1)

    def test_same_platform_id_different_chats(self):
        path = self.messages("chat.json", [self.row(id="1", chat_id="a"), self.row(id="1", chat_id="b")])
        result = import_messages([path])
        self.assertEqual(len({m["id"] for m in result.messages}), 2)

    def test_duplicate_and_conflicting_ids(self):
        row = self.row(id="a")
        path = self.messages("chat.json", [row, row])
        self.assertEqual(import_messages([path]).duplicates, 1)
        path = self.messages("chat.json", [row, {**row, "content": "changed"}])
        with self.assertRaises(ImportFailure):
            import_messages([path])

    def test_custom_mapping_and_filters(self):
        path = self.messages("chat.json", [{"when": 1735689600, "body": "abc", "who": "a"},
                                           {"when": 1735689601, "body": "def", "who": "b"}])
        result = import_messages([path], mapping={"timestamp": "when", "text": "body", "sender": "who"},
                                 senders=["a"], chat_id="shared")
        self.assertEqual(result.filtered, 1)
        self.assertEqual(result.messages[0]["chat_id"], "shared")

    def test_time_units_offsets_and_quarter(self):
        tz = ZoneInfo("Asia/Shanghai")
        for value in (1735689600, 1735689600000, 1735689600000000, 1735689600000000000,
                      "2025-01-01T00:00:00Z", "2025-01-01T08:00:00+08:00", "2025/01/01 08:00:00"):
            with self.subTest(value=value):
                self.assertEqual(parse_time(value, tz), 1735689600)
        path = self.messages("chat.json", [self.row(timestamp="2025-03-31T20:00:00Z")])
        self.assertEqual(import_messages([path]).messages[0]["quarter"], "2025-Q2")
        self.assertEqual(import_messages([path], timezone_name="UTC").messages[0]["quarter"], "2025-Q1")

    def test_dst_fold_and_gap_need_offsets(self):
        tz = ZoneInfo("America/New_York")
        for value in ("2025-11-02 01:30:00", "2025-03-09 02:30:00"):
            with self.assertRaises(ValueError):
                parse_time(value, tz)
        self.assertIsInstance(parse_time("2025-11-02T01:30:00-04:00", tz), int)

    def test_sqlite_readonly_and_table_selection(self):
        path = self.root / "chat.db"
        connection = sqlite3.connect(path)
        connection.execute('CREATE TABLE "odd table" (CreateTime INTEGER, StrContent TEXT, StrTalker TEXT)')
        connection.execute('INSERT INTO "odd table" VALUES (1735689600, ?, ?)', ("hello", "room"))
        connection.commit()
        connection.close()
        before = path.read_bytes()
        result = import_messages([path])
        self.assertEqual(result.messages[0]["chat_id"], "room")
        self.assertEqual(path.read_bytes(), before)
        connection = sqlite3.connect(path)
        connection.execute('CREATE TABLE another (CreateTime INTEGER, StrContent TEXT)')
        connection.commit()
        connection.close()
        with self.assertRaises(ImportFailure):
            import_messages([path])
        self.assertEqual(len(import_messages([path], table="odd table").messages), 1)

    def test_sqlite_wal_committed_messages(self):
        path = self.root / "wal.db"
        connection = sqlite3.connect(path)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.execute("CREATE TABLE messages (timestamp INTEGER, content TEXT)")
            connection.execute("INSERT INTO messages VALUES (1735689600, 'committed WAL')")
            connection.commit()
            self.assertEqual(import_messages([path]).messages[0]["text"], "committed WAL")
            self.assertEqual(connection.execute("SELECT count(*) FROM messages").fetchone()[0], 1)
        finally:
            connection.close()

    def test_empty_input_and_invalid_mapping(self):
        path = self.messages("empty.json", [])
        with self.assertRaises(ImportFailure):
            import_messages([path])
        with self.assertRaises(ImportFailure):
            import_messages([path], mapping={"typo": "body"})

    def test_directory_import_stable_ids(self):
        self.messages("a.json", [self.row()])
        self.messages("b.json", [self.row()])
        first = import_messages([self.root]).messages
        second = import_messages([self.root]).messages
        self.assertEqual([m["id"] for m in first], [m["id"] for m in second])
        self.assertEqual(len(first), 2)


if __name__ == "__main__":
    unittest.main()
