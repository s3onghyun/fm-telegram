#!/usr/bin/env python3
"""A local stand-in for the Telegram Bot API, for end-to-end tests without a real bot.

Bot methods: getUpdates, sendMessage, getFile and the /file/ download path. Test control:
POST /_control/updates (a JSON update to queue), POST /_control/files ({"path", "text" or "base64"}),
GET /_control/sent (messages the bridge sent). Requests with the wrong token get HTTP 401.
"""
from __future__ import annotations

import argparse
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class FakeTelegramServer(ThreadingHTTPServer):
    def __init__(self, token: str, port: int = 0) -> None:
        super().__init__(("127.0.0.1", port), Handler)
        self.token = token
        self.updates: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []
        self.files: dict[str, bytes] = {}
        self.lock = threading.Lock()

    @property
    def base(self) -> str:
        return "http://127.0.0.1:%d" % self.server_address[1]


class Handler(BaseHTTPRequestHandler):
    server: FakeTelegramServer

    def log_message(self, *_args: Any) -> None:  # Keep test output quiet.
        pass

    def reply(self, status: int, body: Any, raw: bool = False) -> None:
        data = body if raw else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        server = self.server
        if self.path == "/_control/sent":
            with server.lock:
                return self.reply(200, server.sent)
        prefix = "/file/bot%s/" % server.token
        if self.path.startswith(prefix) and self.path[len(prefix):] in server.files:
            return self.reply(200, server.files[self.path[len(prefix):]], raw=True)
        self.reply(404, {"ok": False})

    def do_POST(self) -> None:
        server = self.server
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        with server.lock:
            if self.path == "/_control/updates":
                server.updates.append(body)
                return self.reply(200, {"ok": True})
            if self.path == "/_control/files":
                server.files[body["path"]] = (base64.b64decode(body["base64"]) if "base64" in body
                                              else body["text"].encode())
                return self.reply(200, {"ok": True})
            prefix = "/bot%s/" % server.token
            if not self.path.startswith(prefix):
                return self.reply(401, {"ok": False, "description": "Unauthorized"})
            method = self.path[len(prefix):]
            if method == "getUpdates":
                pending = [item for item in server.updates if item["update_id"] >= body.get("offset", 0)]
                return self.reply(200, {"ok": True, "result": pending})
            if method == "sendMessage":
                if body.get("parse_mode") == "HTML" and "REJECT-HTML" in body.get("text", ""):
                    return self.reply(400, {"ok": False, "description": "can't parse entities"})
                server.sent.append(body)
                return self.reply(200, {"ok": True, "result": {"message_id": len(server.sent)}})
            if method == "getFile" and body.get("file_id") in server.files:
                path = body["file_id"]
                return self.reply(200, {"ok": True, "result": {
                    "file_path": path, "file_size": len(server.files[path])}})
        self.reply(400, {"ok": False, "description": "unsupported"})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token", required=True)
    args = parser.parse_args()
    fake = FakeTelegramServer(args.token, args.port)
    print("fake Telegram API on %s" % fake.base, flush=True)
    fake.serve_forever()
