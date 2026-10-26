"""`fm_telegram.py setup` against the fake Telegram API, a fake Keychain and fake installers."""
import argparse
import io
import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import fm_telegram as fm
from fake_telegram_api import FakeTelegramServer

TOKEN = "123456:fake-setup-token-not-real-0000000"
OWNER, STRANGER = 424242, 999


def private(update_id, chat_id, name="Ada"):
    return {"update_id": update_id, "message": {"message_id": update_id, "text": "hi", "chat": {
        "id": chat_id, "type": "private", "first_name": name}}}


class FakeRunner:
    """Records commands; plays the macOS Keychain (`security`) and the installer."""
    def __init__(self, keychain=True):
        self.calls, self.stored, self.keychain = [], None, keychain

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs.get("input")))
        if args[0] == "security" and not self.keychain:
            raise FileNotFoundError(2, "No such file", "security")
        if args[:2] == ["security", "-i"]:
            self.stored = kwargs["input"].split('-w "')[1].split('"')[0]
        elif args[:2] == ["security", "find-generic-password"] and not self.stored:
            raise subprocess.CalledProcessError(44, args)
        return subprocess.CompletedProcess(args, 0, (self.stored or "") + "\n", "")


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.server = FakeTelegramServer(TOKEN)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.home = self.root / "firstmate"
        (self.home / "bin").mkdir(parents=True)
        (self.home / "bin/fm-inbox.sh").write_text("#!/bin/sh\n")
        (self.home / "bin/fm-inbox.sh").chmod(0o700)
        self.config = self.root / "config/config.json"
        self.config.parent.mkdir()
        # Point setup at the fake API; a real install would leave api_base at its default.
        self.config.write_text(json.dumps({"api_base": self.server.base}))
        self.environment = patch.dict(os.environ, {"HOME": str(self.root), fm.TOKEN_ENV: "",
                                                   "NO_PROXY": "*", "no_proxy": "*"})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def args(self, **overrides):
        values = dict(config=self.config, home=None, state_dir=self.root / "state",
                      firstmate_home=self.home, run_mode="none", yes=True, token_only=False, wait=5)
        values.update(overrides)
        return argparse.Namespace(**values)

    def setup(self, args=None, **kwargs):
        out = io.StringIO()
        kwargs.setdefault("interactive", False)
        kwargs.setdefault("runner", FakeRunner())
        code = fm.Setup(args or self.args(), out=out, **kwargs).run()
        return code, out.getvalue()

    def test_detects_private_owner_writes_private_config_and_reruns_unchanged(self):
        self.server.updates.append(private(1, STRANGER, "Old"))  # Backlog: never the owner.
        def owner_writes(_seconds):
            self.server.updates.extend([
                {"update_id": 2, "message": {"chat": {"id": -5, "type": "group"}}},
                private(3, OWNER)])
        with patch.dict(os.environ, {fm.TOKEN_ENV: TOKEN}):
            code, output = self.setup(sleep=owner_writes)
            self.assertEqual(0, code, output)
            config = json.loads(self.config.read_text())
            self.assertEqual(OWNER, config["owner_chat_id"])
            self.assertEqual(str(self.home.resolve()), config["firstmate_home"])
            self.assertEqual(0o600, self.config.stat().st_mode & 0o777)
            self.assertIn("setup owner skip: ignored a message from a group chat", output)
            self.assertIn("setup token ok: bot @fake_fm_bot", output)
            self.assertEqual([(OWNER, fm.SELF_TEST)],
                             [(row["chat_id"], row["text"]) for row in self.server.sent])
            before = self.config.read_bytes()
            code, output = self.setup(sleep=lambda _s: self.fail("waited for an owner again"))
        self.assertEqual(0, code, output)
        self.assertIn("setup owner ok: owner_chat_id already set; kept", output)
        self.assertIn("unchanged", output)
        self.assertEqual(before, self.config.read_bytes())

    def test_owner_detection_without_confirmation_needs_a_human(self):
        with patch.dict(os.environ, {fm.TOKEN_ENV: TOKEN}):
            code, output = self.setup(self.args(yes=False))
        self.assertEqual(fm.SETUP_NEEDS_HUMAN, code)
        self.assertIn("setup owner need-human:", output)

    def test_prompted_token_is_hidden_verified_and_kept_out_of_argv_and_output(self):
        runner, prompts = FakeRunner(), iter(["not-a-token", "999999:" + "x" * 35, TOKEN])
        code, output = self.setup(self.args(token_only=True), interactive=True, runner=runner,
                                  platform="darwin", secret=lambda _prompt: next(prompts))
        self.assertEqual(0, code, output)
        self.assertEqual(TOKEN, runner.stored)
        self.assertEqual(2, output.count("setup token retry"))
        self.assertTrue(all(TOKEN not in " ".join(args) for args, _input in runner.calls))
        self.assertNotIn(TOKEN, output)
        # A rerun uses the stored token and asks for nothing.
        code, output = self.setup(self.args(token_only=True), runner=runner, platform="darwin")
        self.assertEqual(0, code, output)

    def test_linux_token_goes_to_private_runtime_env_read_by_the_bridge(self):
        runner = FakeRunner(keychain=False)
        # --token-only needs no Firstmate home: the person may run it from any directory.
        args = self.args(token_only=True, firstmate_home=self.root / "no-home-needed")
        code, output = self.setup(args, interactive=True, runner=runner, platform="linux",
                                  secret=lambda _prompt: TOKEN)
        self.assertEqual(0, code, output)
        env_file = self.config.parent / "runtime.env"
        self.assertEqual(0o600, env_file.stat().st_mode & 0o777)
        self.assertEqual(TOKEN, fm.read_token("service", env_file, runner))
        self.assertNotIn(TOKEN, output)

    def test_missing_token_without_a_terminal_asks_for_the_human_step(self):
        code, output = self.setup()
        self.assertEqual(fm.SETUP_NEEDS_HUMAN, code)
        self.assertIn("setup token need-human:", output)
        self.assertIn("setup --token-only", output)

    def test_named_home_without_fm_inbox_is_an_error_even_if_another_exists(self):
        (self.root / "firstmate").mkdir(exist_ok=True)
        with patch.dict(os.environ, {fm.TOKEN_ENV: TOKEN}):
            code, output = self.setup(self.args(firstmate_home=self.root / "elsewhere"))
        self.assertEqual(1, code)
        self.assertIn("--firstmate-home", output)

    def test_tcc_protected_folders_are_detected_on_macos_only(self):
        documents = self.root / "Documents/work/firstmate"
        self.assertEqual("~/Documents", fm.tcc_protected(documents, "darwin"))
        self.assertEqual("~/Library/Mobile Documents",
                         fm.tcc_protected(self.root / "Library/Mobile Documents/fm", "darwin"))
        self.assertIsNone(fm.tcc_protected(documents, "linux"))
        self.assertIsNone(fm.tcc_protected(self.home, "darwin"))

    def test_service_is_refused_for_a_tcc_home_and_auto_falls_back_to_a_terminal(self):
        protected = self.root / "Documents/firstmate"
        runner = FakeRunner()
        setup = fm.Setup(self.args(run_mode="auto"), out=io.StringIO(), runner=runner,
                         interactive=False, platform="darwin")
        self.assertEqual("terminal", setup.install(protected))
        self.assertTrue(all("--launchd" not in args for args, _input in runner.calls))
        self.assertIn("setup run-mode warn:", setup.out.getvalue())
        self.assertIn("tmux new-session -d -s fm-telegram python3 ", setup.out.getvalue())
        with self.assertRaises(RuntimeError):
            fm.Setup(self.args(run_mode="service"), out=io.StringIO(), runner=runner,
                     interactive=False, platform="darwin").install(protected)
        fm.Setup(self.args(run_mode="service"), out=io.StringIO(), runner=runner,
                 interactive=False, platform="darwin").install(self.home)
        self.assertTrue(runner.calls[-1][0][0].endswith("deploy/install.sh"))
        self.assertEqual("--launchd", runner.calls[-1][0][1])


if __name__ == "__main__":
    unittest.main()
