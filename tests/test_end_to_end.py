"""Bridge + real HTTP client against the fake Telegram API and a scripted fm-inbox.sh."""
import argparse
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import fm_telegram as fm
from fake_telegram_api import FakeTelegramServer

OWNER, STRANGER, TOKEN = 424242, 999, "123456:fake-test-token-not-real-000000000"

# Minimal fm-inbox.sh: idempotent notes keyed by request id, and replies read from a file.
FAKE_INBOX = r'''#!/usr/bin/env python3
import json, sys
from pathlib import Path
home = Path(__file__).resolve().parents[1]
notes_path, replies_path = home / "notes.json", home / "replies.json"
notes = json.loads(notes_path.read_text()) if notes_path.exists() else {}
args = sys.argv[1:]
if args[0] == "note":
    request = args[args.index("--request-id") + 1]
    outcome = "replay" if request in notes else "created"
    notes.setdefault(request, {"id": "n%d" % (len(notes) + 1), "body": sys.stdin.read()})
    notes_path.write_text(json.dumps(notes))
    print(json.dumps({"id": notes[request]["id"], "outcome": outcome}))
elif args[0] == "receipts":
    after = args[2] if len(args) > 2 else ""
    rows = [row for row in json.loads(replies_path.read_text() or "[]") if row["cursor"] > after] \
        if replies_path.exists() else []
    print(json.dumps({"replies": rows, "reply_cursor": rows[-1]["cursor"] if rows else after}))
elif args[0] == "status":
    print("1 pending note")
'''


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.server = FakeTelegramServer(TOKEN)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.home = root / "firstmate"
        (self.home / "bin").mkdir(parents=True)
        inbox = self.home / "bin/fm-inbox.sh"
        inbox.write_text(FAKE_INBOX)
        inbox.chmod(0o700)
        config = root / "config.json"
        config.write_text(json.dumps({"owner_chat_id": OWNER, "firstmate_home": str(self.home),
                                      "doorbell": False, "api_base": self.server.base}))
        config.chmod(0o600)
        args = argparse.Namespace(config=config, home=None, state_dir=root / "state")
        with patch.dict(fm.os.environ, {fm.TOKEN_ENV: TOKEN}):
            self.bridge, _config = fm.build_bridge(args)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def push(self, update_id, chat_id, **message):
        self.server.updates.append({"update_id": update_id,
                                    "message": {"chat": {"id": chat_id}, **message}})

    def notes(self):
        return json.loads((self.home / "notes.json").read_text())

    def test_receive_forward_and_reply_round_trip(self):
        self.push(1, STRANGER, text="let me in")
        self.push(2, OWNER, text="check the build")
        self.server.files["documents/a.txt"] = b"hello"
        self.push(3, OWNER, caption="see file", document={
            "file_id": "documents/a.txt", "file_unique_id": "u3", "file_size": 5, "file_name": "a.txt"})
        self.bridge.once()
        notes = self.notes()
        self.assertEqual(["tg:2", "tg:3"], sorted(notes))
        self.assertEqual("check the build", notes["tg:2"]["body"])
        attachment = Path(notes["tg:3"]["body"].split("attachment: ")[1])
        self.assertEqual(b"hello", attachment.read_bytes())
        self.assertEqual(["Received. Note ID: n1", "Received. Note ID: n2"],
                         [row["text"] for row in self.server.sent])
        self.assertTrue(all(row["chat_id"] == OWNER for row in self.server.sent))

        # A replayed update (offset lost) creates no second note.
        self.bridge.state.data["offset"] = 0
        self.bridge.once()
        self.assertEqual(2, len(self.notes()))

        (self.home / "replies.json").write_text(json.dumps([
            {"id": "n1", "cursor": "0001", "body": "**Build** is green. token " + TOKEN},
            {"id": "elsewhere", "cursor": "0002", "body": "not for Telegram"}]))
        self.bridge.once()
        last = self.server.sent[-1]
        self.assertEqual("<b>Build</b> is green. token [redacted]", last["text"])
        self.assertEqual("HTML", last["parse_mode"])
        self.assertNotIn("not for Telegram", json.dumps(self.server.sent))
        self.assertEqual("0002", self.bridge.state.data["receipt_cursor"])

    def test_html_rejected_by_telegram_is_resent_as_plain_text(self):
        self.bridge.send("**REJECT-HTML** result")
        self.assertEqual([{"chat_id": OWNER, "text": "REJECT-HTML result",
                           "disable_web_page_preview": True}], self.server.sent)

    def test_wrong_token_is_reported_without_leaking_it(self):
        api = fm.TelegramAPI("999:wrong-token", base=self.server.base)
        with self.assertRaises(fm.TelegramAPIError) as caught:
            api.updates(0)
        self.assertIn("HTTP 401", str(caught.exception))
        self.assertNotIn("wrong-token", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
