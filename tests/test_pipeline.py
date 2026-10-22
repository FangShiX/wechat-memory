import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from wechat_memory import ai
from wechat_memory.analysis import aggregate, search
from wechat_memory.chunking import ChunkConfig, build_chunks
from wechat_memory.cli import answer, main, parser, run_pipeline
from wechat_memory.dashboard import render
from wechat_memory.importers import import_messages


DEMO = Path(__file__).resolve().parents[1] / "examples" / "demo.jsonl"


def model_environment(values):
    # Preserve SystemRoot and other OS/runtime variables needed by Windows TLS.
    # Only model settings are isolated; no real API credentials enter test requests.
    clean = {key: "" for key in ("OPENAI_MODEL", "OPENAI_BASE_URL", "OPENAI_API_KEY")}
    return patch.dict(os.environ, {**clean, **values})


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def args(self, *extra):
        return parser().parse_args(["run", str(DEMO), "--output", str(self.root / "output"), *extra])

    def test_offline_complete_pipeline_no_network_and_query(self):
        with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("network forbidden")):
            result = run_pipeline(self.args())
            self.assertEqual(result["messages"], 6)
            workspace = self.root / "output"
            analysis = json.loads((workspace / "analysis.json").read_text(encoding="utf-8"))
            self.assertEqual(analysis["chat_count"], 2)
            self.assertEqual(len(analysis["timeline"]), 2)
            args = parser().parse_args(["ask", "公共交通", "--workspace", str(workspace), "--chat", "demo-reading"])
            response = answer(args)
            self.assertTrue(response["evidence"])
            self.assertTrue(all(m["chat_id"] == "demo-reading" for m in response["evidence"]))
            args.question = "unrelated-quantum-xyz"
            self.assertEqual(answer(args)["evidence"], [])

    def test_no_overwrite_and_no_partial_output(self):
        args = self.args()
        args.output.mkdir()
        marker = args.output / "private.txt"
        marker.write_text("preserve")
        with self.assertRaises(ValueError):
            run_pipeline(args)
        self.assertEqual(marker.read_text(), "preserve")
        args.output = self.root / "failed"
        with patch("wechat_memory.cli.render", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                run_pipeline(args)
        self.assertFalse(args.output.exists())
        self.assertFalse(list(self.root.glob(".wechat-memory-*")))

    def test_chunk_isolation_coverage_and_determinism(self):
        messages = import_messages([DEMO]).messages
        chunks = build_chunks(messages)
        mapping = {m["id"]: m for m in messages}
        covered = []
        for chunk in chunks:
            linked = [mapping[mid] for mid in chunk["message_ids"]]
            self.assertEqual(len({m["chat_id"] for m in linked}), 1)
            self.assertEqual(len({m["quarter"] for m in linked}), 1)
            covered.extend(chunk["message_ids"])
        self.assertCountEqual(covered, mapping)
        self.assertEqual(chunks, build_chunks(list(reversed(messages))))

    def test_gap_bound_and_long_message_not_lost(self):
        records = import_messages([DEMO]).messages[:2]
        records[0]["text"] = "a" * 5000
        chunks = build_chunks(records, ChunkConfig(max_chars=10))
        self.assertEqual(sum(c["message_count"] for c in chunks), 2)
        self.assertTrue(any("a" * 5000 == c["combined_text"] for c in chunks))

    def test_dashboard_escapes_script_and_attributes(self):
        messages = import_messages([DEMO]).messages
        attack = '</script><script>alert("private")</script><img src=x onerror=alert(1)>'
        messages[0]["text"] = attack
        messages[0]["sender_name"] = attack
        analysis = aggregate(messages, build_chunks(messages), [])
        path = self.root / "dashboard.html"
        render(path, analysis, messages)
        html = path.read_text(encoding="utf-8")
        self.assertNotIn(attack, html)
        self.assertIn("&lt;img", html)
        self.assertIn("default-src 'none'", html)
        self.assertEqual(html.count("<script>"), 1)

    def test_search_filters_and_no_unsupported_claims(self):
        messages = import_messages([DEMO]).messages
        hits = search(messages, "公共交通", sender="reader-b", quarter="2025-Q1")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["sender_id"], "reader-b")
        analysis = aggregate(messages, build_chunks(messages), [])
        self.assertTrue(all(c["method"] == "lexical_occurrence" for c in analysis["concept_cards"]))
        traffic = next(c for c in analysis["concept_cards"] if c["concept"] == "公共交通")
        self.assertEqual(traffic["message_count"], 3)
        self.assertEqual([q["quarter"] for q in traffic["quarters"]], ["2025-Q1", "2025-Q2"])
        self.assertEqual(analysis["non_text_or_empty_count"], 1)
        self.assertFalse(any(c["concept"] == "图片" for c in analysis["concept_cards"]))

    def test_cli_module_version_and_error_exit(self):
        process = subprocess.run([sys.executable, "-m", "wechat_memory", "--version"], capture_output=True, text=True)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(process.stdout.strip(), "1.0.0")
        process = subprocess.run([sys.executable, "-m", "wechat_memory", "run", "does-not-exist.json"],
                                 cwd=self.root, capture_output=True, text=True,
                                 env={**os.environ, "PYTHONPATH": str(DEMO.parents[1])})
        self.assertEqual(process.returncode, 1)
        self.assertNotIn("Traceback", process.stderr)

    def test_large_synthetic_multi_chat(self):
        seed = import_messages([DEMO]).messages[0]
        messages = [{**seed, "id": f"msg-{i}", "timestamp": seed["timestamp"] + i,
                     "chat_id": f"room-{i % 10}"} for i in range(10000)]
        chunks = build_chunks(messages)
        self.assertEqual(sum(c["message_count"] for c in chunks), 10000)
        self.assertEqual(aggregate(messages, chunks, [])["chat_count"], 10)


class AITests(unittest.TestCase):
    def setUp(self):
        self.messages = import_messages([DEMO]).messages[:1]
        self.good = {"summary": "关于城市交通的讨论", "topics": ["城市交通"], "confidence": 0.8,
                     "evidence": [{"message_id": self.messages[0]["id"], "quote": "城市交通"}]}

    def test_quote_and_id_and_confidence_validation(self):
        self.assertEqual(ai.validate_reply(self.good, self.messages)["method"], "ai")
        for change in ({"confidence": float("nan")}, {"confidence": 1.1}, {"confidence": True},
                       {"evidence": []}, {"evidence": [{"message_id": "fabricated", "quote": "城市交通"}]},
                       {"evidence": [{"message_id": self.messages[0]["id"], "quote": "不存在的引文"}]}):
            with self.subTest(change=change), self.assertRaises(ai.AIError):
                ai.validate_reply({**self.good, **change}, self.messages)

    def test_opt_in_configuration_and_insecure_endpoint(self):
        with model_environment({}), self.assertRaises(ai.AIError):
            ai.configuration()
        with model_environment({"OPENAI_MODEL": "local", "OPENAI_BASE_URL": "http://localhost:8080/v1"}):
            self.assertEqual(ai.configuration()[2], "local")
        with model_environment({"OPENAI_MODEL": "x", "OPENAI_API_KEY": "SECRET", "OPENAI_BASE_URL": "http://example.com/v1"}):
            with self.assertRaises(ai.AIError):
                ai.configuration()

    def test_provider_payload_and_reply(self):
        response = json.dumps({"choices": [{"message": {"content": json.dumps(self.good)}}]}).encode()
        def open_request(req, timeout):
            self.assertEqual(req.get_method(), "POST")
            self.assertNotIn("source_file", req.data.decode())
            self.assertEqual(timeout, 60)
            return io.BytesIO(response)
        with model_environment({"OPENAI_MODEL": "local", "OPENAI_BASE_URL": "http://localhost:8080/v1"}):
            with patch("urllib.request.OpenerDirector.open", side_effect=open_request):
                result = ai.summarize(self.messages)
                self.assertEqual(result["model"], "local")

    def test_redirect_rejected(self):
        with self.assertRaises(ai.AIError):
            ai.NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://other.example")

    def test_invalid_model_response_retries_and_fails(self):
        with model_environment({"OPENAI_MODEL": "local", "OPENAI_BASE_URL": "http://localhost:8080/v1"}):
            with patch("urllib.request.OpenerDirector.open", side_effect=lambda *a, **k: io.BytesIO(b"not json")) as mocked:
                with self.assertRaises(ai.AIError):
                    ai.summarize(self.messages)
                self.assertEqual(mocked.call_count, 2)

    def test_http_provider_full_pipeline(self):
        received = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                data = json.loads(body["messages"][1]["content"])
                received.extend(data["messages"])
                first = data["messages"][0]
                reply = {"summary": "本地测试端点的有引用概括", "topics": [], "confidence": 0.7,
                         "evidence": [{"message_id": first["id"], "quote": first["text"][:8]}]}
                raw = json.dumps({"choices": [{"message": {"content": json.dumps(reply)}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(raw)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                workspace = Path(folder) / "ai"
                env = {"OPENAI_MODEL": "synthetic-local-test", "OPENAI_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1"}
                with model_environment(env):
                    run_pipeline(parser().parse_args(["run", str(DEMO), "--output", str(workspace), "--ai"]))
                    analysis = json.loads((workspace / "analysis.json").read_text(encoding="utf-8"))
                    self.assertEqual(len(analysis["annotations"]), 3)
                    result = answer(parser().parse_args(["ask", "公共交通", "--workspace", str(workspace), "--ai"]))
                    self.assertIn("generation", result)
                    self.assertFalse(any(m["text"] == "[图片]" for m in received))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
