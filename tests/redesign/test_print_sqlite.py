import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@pytest.mark.parametrize("truncated", [False, True])
def test_print_pipeline_returns_one_json_and_resumes_jsonl(tmp_path, truncated):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.end_headers()
            chunks = [
                {
                    "id": "test",
                    "object": "chat.completion.chunk",
                    "model": "gpt-4o-mini",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": "离线回答"},
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "id": "test",
                    "object": "chat.completion.chunk",
                    "model": "gpt-4o-mini",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                },
            ]
            if self.path.endswith("/responses"):
                chunks = [
                    {"type": "response.output_text.delta", "delta": "离线回答"},
                    {
                        "type": "response.completed",
                        "response": {
                            "status": "completed",
                            "output": [],
                            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                        },
                    },
                ]
                if truncated:
                    chunks = chunks[:1]
            for chunk in chunks:
                self.wfile.write(
                    ("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode()
                )
            self.wfile.write(b"data: [DONE]\n\n")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = {
        **os.environ,
        "OPENAI_API_KEY": "offline-test-key",
        "OPENAI_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
        # The truncated case is a Responses stream cut before completion.
        "OPENAI_API": "openai-responses",
    }
    command = [
        sys.executable,
        "-m",
        "run_agent_entry",
        "--print",
        "--mode",
        "json",
        "--provider",
        "openai",
        "--model",
        "gpt-4o-mini",
        "--no-extensions",
        "--state-dir",
        str(tmp_path / "state"),
    ]
    try:
        first = subprocess.run(
            [*command, "first"],
            input="piped context",
            text=True,
            encoding="utf-8",
            capture_output=True,
            cwd=tmp_path,
            env=env,
            timeout=15,
        )
        assert not list(tmp_path.rglob("*.sqlite3"))
        if truncated:
            assert first.returncode == 1, first.stdout
            assert json.loads(first.stdout)["status"] == "failed"
            sessions = list((tmp_path / ".run" / "sessions").glob("*.jsonl"))
            assert sessions
            return
        assert first.returncode == 0, first.stderr + first.stdout
        payload = json.loads(first.stdout)
        assert payload["text"] == "离线回答"
        assert payload["status"] == "succeeded"
        assert "\x1b" not in first.stdout
        second = subprocess.run(
            [*command, "--session", payload["session_id"], "second"],
            input="",
            text=True,
            encoding="utf-8",
            capture_output=True,
            cwd=tmp_path,
            env=env,
            timeout=15,
        )
        assert second.returncode == 0, second.stderr + second.stdout
        resumed = json.loads(second.stdout)
        assert resumed["session_id"] == payload["session_id"]
        assert resumed["run_id"] != payload["run_id"]
        session_files = list((tmp_path / ".run" / "sessions").glob("*.jsonl"))
        assert any(path.name == f"{payload['session_id']}.jsonl" for path in session_files)
        assert list((tmp_path / "state").rglob("*.sqlite3")) == []
        assert any(
            "piped context" in json.dumps(request, ensure_ascii=False) for request in requests
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
