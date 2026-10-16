import io
import json
import logging
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fm_telegram as fm

OWNER = 424242  # A fake chat id; real ids never belong in tests.

class FakeTelegram:
    def __init__(self, updates=None):
        self.pending_updates = updates or []
        self.sent = []
        self.parse_modes = []
        self.offsets = []
        self.files = {}
        self.file_requests = []

    def get_file(self, file_id, limit):
        self.file_requests.append(file_id)
        path, data = self.files[file_id]
        if len(data) > limit:
            raise fm.AttachmentTooLarge()
        return path, data

    def updates(self, offset):
        self.offsets.append(offset)
        return list(self.pending_updates)

    def send(self, chat_id, text, parse_mode=None):
        self.sent.append((chat_id, text))
        self.parse_modes.append(parse_mode)


class FakeInbox:
    def __init__(self, replies=None):
        self.requests, self.by_request = [], {}
        self.reply_rows = replies or []

    def note(self, request_id, text):
        self.requests.append((request_id, text))
        return self.by_request.setdefault(request_id, "note-" + request_id)

    def status(self):
        return "healthy"

    def receipts(self, cursor):
        rows = [row for row in self.reply_rows if row["cursor"] > cursor]
        return {"replies": rows, "reply_cursor": rows[-1]["cursor"] if rows else cursor}


class FakeHerdr:
    def __init__(self, panes=None, fail=False):
        self.calls = []
        self.panes = panes if panes is not None else []
        self.fail = fail

    def __call__(self, args, **_kwargs):
        self.calls.append(args)
        if self.fail:
            raise subprocess.CalledProcessError(1, args)
        if args[2:] == ["list"]:
            return subprocess.CompletedProcess(
                args, 0, json.dumps({"id": "cli:pane:list", "result": {"panes": self.panes}}), "")
        return subprocess.CompletedProcess(args, 0, "", "")


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def update(number, chat_id, text):
    return {"update_id": number, "message": {"chat": {"id": chat_id}, "text": text}}


def media_update(number, chat_id, caption=None, **fields):
    message = {"chat": {"id": chat_id}, **fields}
    if caption is not None:
        message["caption"] = caption
    return {"update_id": number, "message": message}


PHOTO_SIZES = [
    {"file_id": "small", "file_unique_id": "u-small", "file_size": 100, "width": 90, "height": 90},
    {"file_id": "large", "file_unique_id": "u-large", "file_size": 9000, "width": 1280, "height": 960},
]


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = fm.State(Path(self.temp.name) / "state")
        self.api, self.inbox = FakeTelegram(), FakeInbox()
        self.stream = io.StringIO()
        logger = logging.getLogger("test-fm-telegram-%s" % id(self))
        logger.handlers[:] = [logging.StreamHandler(self.stream)]
        logger.setLevel(logging.INFO)
        self.bridge = fm.Bridge(self.api, self.inbox, self.state, OWNER, fm.Redactor(), logger)

    def tearDown(self):
        self.temp.cleanup()

    def test_allowlist_ignores_unknown_chat(self):
        self.bridge.process_update(update(1, 123, "do work"))
        self.assertEqual([], self.inbox.requests)
        self.assertEqual([], self.api.sent)
        self.assertIn("id=123", self.stream.getvalue())

    def test_photo_downloads_largest_size_privately(self):
        self.api.files["large"] = ("photos/file_7.jpg", b"jpeg-bytes")
        self.bridge.process_update(media_update(70, OWNER, "look at this screen", photo=PHOTO_SIZES))
        self.assertEqual(["large"], self.api.file_requests)
        media = self.state.directory / "media"
        target = media / "70-u-large.jpg"
        self.assertEqual(b"jpeg-bytes", target.read_bytes())
        self.assertEqual(0o700, media.stat().st_mode & 0o777)
        self.assertEqual(0o600, target.stat().st_mode & 0o777)
        self.assertEqual([("tg:70", "look at this screen\nattachment: %s" % target.resolve())], self.inbox.requests)
        self.assertIn("note-tg:70", self.api.sent[-1][1])

    def test_image_document_without_caption(self):
        self.api.files["doc"] = ("documents/file_8.png", b"png")
        self.bridge.process_update(media_update(71, OWNER, document={
            "file_id": "doc", "file_unique_id": "u-doc", "file_size": 3, "mime_type": "image/png"}))
        target = (self.state.directory / "media/71-u-doc.png").resolve()
        self.assertEqual([("tg:71", "(photo)\nattachment: %s" % target)], self.inbox.requests)

    def test_pdf_document_uses_mime_extension_when_path_has_none(self):
        self.api.files["pdf"] = ("documents/file_9", b"%PDF")
        self.bridge.process_update(media_update(72, OWNER, "quote", document={
            "file_id": "pdf", "file_unique_id": "u-pdf", "file_size": 4, "mime_type": "application/pdf"}))
        target = (self.state.directory / "media/72-u-pdf.pdf").resolve()
        self.assertEqual(b"%PDF", target.read_bytes())
        self.assertEqual([("tg:72", "quote\nattachment: %s" % target)], self.inbox.requests)

    def test_any_document_keeps_sanitised_original_name(self):
        self.api.files["zip"] = ("documents/file_10.zip", b"PK")
        self.bridge.process_update(media_update(73, OWNER, document={
            "file_id": "zip", "file_unique_id": "u-zip", "file_size": 2,
            "file_name": "../../etc/견적 v2.ZIP", "mime_type": "application/zip"}))
        target = (self.state.directory / "media/73-u-zip-견적_v2.zip").resolve()
        self.assertEqual(b"PK", target.read_bytes())
        self.assertEqual([("tg:73", "(file)\nattachment: %s" % target)], self.inbox.requests)

    def test_unsupported_message_is_reported_not_dropped(self):
        self.bridge.process_update(media_update(76, OWNER, sticker={
            "file_id": "s", "file_unique_id": "u-s", "file_size": 2048}))
        self.assertEqual([], self.api.file_requests)
        self.assertEqual([("tg:76", "(attachment not received: sticker, size=2KB — unsupported type)")],
                         self.inbox.requests)
        self.assertIn("Could not save", self.api.sent[-1][1])
        self.assertIn("note-tg:76", self.api.sent[-1][1])

    def test_oversize_document_is_reported_before_download(self):
        self.bridge.process_update(media_update(74, OWNER, "this one", document={
            "file_id": "big", "file_unique_id": "u-big", "file_size": fm.MAX_ATTACHMENT_BYTES + 1,
            "file_name": "dump.tar.gz", "mime_type": "application/gzip"}))
        self.assertEqual([], self.api.file_requests)
        self.assertEqual([("tg:74", "(attachment not received: document, name=dump.tar.gz, size=20,480KB"
                                    " — over 20 MB)\nthis one")], self.inbox.requests)
        self.assertIn("20 MB", self.api.sent[-1][1])

    def test_oversize_download_is_reported_and_not_saved(self):
        self.api.files["large"] = ("photos/x.jpg", b"x" * (fm.MAX_ATTACHMENT_BYTES + 1))
        self.bridge.process_update(media_update(75, OWNER, photo=PHOTO_SIZES))
        self.assertFalse((self.state.directory / "media").exists())
        [(request_id, text)] = self.inbox.requests
        self.assertEqual("tg:75", request_id)
        self.assertTrue(text.startswith("(attachment not received: photo"))
        self.assertIn("20 MB", self.api.sent[-1][1])

    def test_voice_note_delivers_transcript_first_with_attachment(self):
        heard = []
        self.bridge.transcriber = lambda path: heard.append(path) or " 기록, 오늘 회고 \n"
        self.bridge.transcript_prefixes = ["기록"]
        self.api.files["v1"] = ("voice/file_3.oga", b"opus-bytes")
        voice = {"file_id": "v1", "file_unique_id": "u-v1", "file_size": 10, "mime_type": "audio/ogg"}
        self.bridge.process_update(media_update(90, OWNER, voice=voice))
        target = self.state.directory / "media" / "90-u-v1.oga"
        self.assertEqual([target], heard)
        self.assertEqual(b"opus-bytes", target.read_bytes())
        self.assertEqual([("tg:90", "기록: 오늘 회고\n(voice)\nattachment: %s" % target.resolve())],
                         self.inbox.requests)

    def test_voice_transcription_failure_still_delivers_attachment(self):
        def broken(_path):
            raise subprocess.CalledProcessError(1, ["whisper-cli"])
        self.bridge.transcriber = broken
        self.api.files["v2"] = ("voice/file_4", b"opus")
        voice = {"file_id": "v2", "file_unique_id": "u-v2", "file_size": 4}
        self.bridge.process_update(media_update(91, OWNER, video_note=voice))
        target = self.state.directory / "media" / "91-u-v2.mp4"
        [(request_id, text)] = self.inbox.requests
        self.assertEqual("tg:91", request_id)
        self.assertTrue(text.startswith("(transcription failed"))
        self.assertTrue(text.endswith("(voice)\nattachment: %s" % target.resolve()))
        self.assertIn("CalledProcessError", self.stream.getvalue())

    def test_photo_from_unknown_chat_is_ignored(self):
        self.api.files["large"] = ("photos/file_7.jpg", b"jpeg")
        self.bridge.process_update(media_update(76, 123, "hi", photo=PHOTO_SIZES))
        self.assertEqual([], self.api.file_requests)
        self.assertEqual([], self.inbox.requests)
        self.assertEqual([], self.api.sent)

    def test_api_get_file_uses_file_endpoint_and_caps_size(self):
        token, urls = "123:secret", []
        def opener(request, timeout):
            urls.append(request.full_url)
            if request.full_url.endswith("/getFile"):
                return FakeResponse(json.dumps({"ok": True, "result": {"file_path": "photos/a.jpg"}}).encode())
            return FakeResponse(b"abcdef")
        api = fm.TelegramAPI(token, opener)
        self.assertEqual(("photos/a.jpg", b"abcdef"), api.get_file("fid", 6))
        self.assertEqual("https://api.telegram.org/file/bot123:secret/photos/a.jpg", urls[-1])
        with self.assertRaises(fm.AttachmentTooLarge) as caught:
            api.get_file("fid", 5)
        self.assertNotIn(token, str(caught.exception))

    def test_request_id_is_update_id_and_replay_is_safe(self):
        item = update(44, OWNER, "same task")
        self.bridge.process_update(item)
        self.bridge.process_update(item)
        self.assertEqual([("tg:44", "same task"), ("tg:44", "same task")], self.inbox.requests)
        self.assertEqual(["note-tg:44"], self.state.data["telegram_note_ids"])

    def test_replay_does_not_ring_doorbell(self):
        herdr, clock = FakeHerdr(), Clock()
        doorbell = fm.Doorbell(Path("/firstmate"), True, self.bridge.log, herdr, clock)
        self.bridge.doorbell = doorbell
        self.inbox.by_request["tg:44"] = fm.Note("note-tg:44", created=False)
        self.bridge.process_update(update(44, OWNER, "same task"))
        self.assertEqual([], herdr.calls)

    def test_replay_outcome_from_fm_inbox_is_not_created(self):
        script = Path(self.temp.name) / "bin/fm-inbox.sh"
        script.parent.mkdir()
        script.write_text('#!/bin/sh\necho \'{"id":"n1","outcome":"replay"}\'\n')
        script.chmod(0o700)
        self.assertEqual(fm.Note("n1", created=False), fm.Inbox(Path(self.temp.name)).note("tg:1", "x"))

    def test_offset_persists_after_handling(self):
        self.api.pending_updates = [update(9, OWNER, "task")]
        self.bridge.once()
        again = fm.State(self.state.directory)
        self.assertEqual(10, again.data["offset"])
        self.assertEqual([0], self.api.offsets)

    def test_only_telegram_reply_is_delivered_and_cursor_moves(self):
        self.state.data["telegram_note_ids"] = ["ours"]
        self.inbox.reply_rows = [{"id": "other", "body": "private elsewhere", "cursor": "000000000001"}, {"id": "ours", "body": "complete", "cursor": "000000000002"}]
        self.bridge.check_replies()
        self.assertEqual([(OWNER, "complete")], self.api.sent)
        self.assertEqual("000000000002", self.state.data["receipt_cursor"])

    def test_splitting_and_redaction(self):
        self.bridge.send("ghp_abcdefghijklmnop " + "x" * 5000)
        self.assertGreaterEqual(len(self.api.sent), 2)
        self.assertTrue(all(len(text) <= 4096 for _, text in self.api.sent))
        self.assertNotIn("ghp_abcdefghijklmnop", "".join(text for _, text in self.api.sent))

    def test_outbound_messages_use_telegram_html(self):
        self.bridge.send("- **Done**: *readability* improved")
        self.assertEqual("• <b>Done</b>: <i>readability</i> improved", self.api.sent[0][1])
        self.assertEqual(["HTML"], self.api.parse_modes)

    def test_falls_back_to_plain_text_when_html_is_rejected(self):
        class RejectingTelegram(FakeTelegram):
            def send(self, chat_id, text, parse_mode=None):
                if parse_mode == "HTML":
                    raise fm.TelegramAPIError("bad entity")
                super().send(chat_id, text, parse_mode)
        self.bridge.api = RejectingTelegram()
        self.bridge.send("**passed** <raw>")
        self.assertEqual([(OWNER, "passed <raw>")], self.bridge.api.sent)
        self.assertEqual([None], self.bridge.api.parse_modes)

    def test_token_never_enters_logs(self):
        token, secure_stream = "ghp_abcdefghijklmnopqrst", io.StringIO()
        logger = logging.getLogger("secret-test-%s" % id(self))
        handler = logging.StreamHandler(secure_stream)
        handler.setFormatter(fm.logger_for(fm.Redactor()).handlers[0].formatter)
        logger.handlers[:] = [handler]
        logger.setLevel(logging.INFO)
        class BrokenAPI(FakeTelegram):
            def updates(self, offset):
                raise RuntimeError(token)
        bridge = fm.Bridge(BrokenAPI(), self.inbox, self.state, OWNER, fm.Redactor(), logger)
        with patch.object(fm.time, "sleep", side_effect=lambda _delay: setattr(bridge, "stopping", True)):
            bridge.run(0)
        self.assertNotIn(token, secure_stream.getvalue())
        self.assertIn("[redacted]", secure_stream.getvalue())


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write_config(self, data, mode=0o600):
        path = self.root / "config.json"
        path.write_text(json.dumps(data))
        path.chmod(mode)
        return path

    def test_config_requires_numeric_owner_chat_id_and_applies_defaults(self):
        config = fm.load_config(self.write_config({"owner_chat_id": OWNER, "firstmate_home": "/fm"}))
        self.assertEqual(OWNER, config["owner_chat_id"])
        self.assertTrue(config["doorbell"])
        self.assertEqual("auto", config["whisper_language"])
        for bad in ({}, {"owner_chat_id": "424242"}, {"owner_chat_id": True}):
            with self.assertRaises(RuntimeError):
                fm.load_config(self.write_config(bad))

    def test_config_writable_by_others_is_refused(self):
        with self.assertRaises(RuntimeError) as caught:
            fm.load_config(self.write_config({"owner_chat_id": OWNER}, mode=0o622))
        self.assertIn("chmod 600", str(caught.exception))

    def test_legacy_config_in_state_dir_is_used_when_new_path_is_missing(self):
        legacy = self.write_config({"owner_chat_id": OWNER})
        self.assertEqual(OWNER, fm.load_config(self.root / "missing.json", legacy)["owner_chat_id"])

    def test_api_base_must_be_https_unless_loopback(self):
        self.assertEqual("http://127.0.0.1:8080", fm._check_api_base("http://127.0.0.1:8080/"))
        self.assertEqual("https://api.telegram.org", fm._check_api_base("https://api.telegram.org"))
        for bad in ("http://example.com", "file:///etc/passwd", "ftp://localhost"):
            with self.assertRaises(RuntimeError):
                fm._check_api_base(bad)

    def test_token_comes_from_environment_before_keychain(self):
        with patch.dict(fm.os.environ, {fm.TOKEN_ENV: " 123:abc \n"}), \
                patch.object(fm.subprocess, "run", side_effect=AssertionError("keychain used")):
            self.assertEqual("123:abc", fm.read_token("service"))

    def test_missing_keychain_tool_gives_actionable_error(self):
        with patch.dict(fm.os.environ, {fm.TOKEN_ENV: ""}), \
                patch.object(fm.subprocess, "run", side_effect=FileNotFoundError("security")):
            with self.assertRaises(RuntimeError) as caught:
                fm.read_token("service")
        self.assertIn(fm.TOKEN_ENV, str(caught.exception))

    def test_redactor_hides_bot_tokens_literal_secrets_and_private_keys(self):
        redactor = fm.Redactor(secrets=["s3cr3t-literal"])
        text = redactor.redact("bot 123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawZ s3cr3t-literal "
                               "github_pat_ABCDEFGH123 -----BEGIN RSA PRIVATE KEY-----\nabc\n"
                               "-----END RSA PRIVATE KEY----- tail")
        self.assertEqual("bot [redacted] [redacted] [redacted] [redacted] tail", text)

    def test_denylist_terms_are_redacted(self):
        denylist = self.root / "deny.txt"
        denylist.write_text("# comment\nAcme Corp\n\n")
        self.assertEqual("[redacted] report", fm.Redactor(denylist).redact("Acme Corp report"))

    def test_outbound_text_is_capped(self):
        api, state = FakeTelegram(), fm.State(self.root / "state")
        bridge = fm.Bridge(api, FakeInbox(), state, OWNER, fm.Redactor(), logging.getLogger("cap"))
        with patch.object(fm.time, "sleep"):
            bridge.send("word " * 100000)
        self.assertLessEqual(len(api.sent), 6)
        self.assertTrue(api.sent[-1][1].endswith("(truncated)"))

    def test_state_files_are_private(self):
        state = fm.State(self.root / "state")
        state.save()
        self.assertEqual(0o700, state.directory.stat().st_mode & 0o777)
        self.assertEqual(0o600, state.path.stat().st_mode & 0o777)

    def test_http_rejection_raises_api_error_without_token(self):
        token = "123456:secret-token"
        def opener(request, timeout):
            raise fm.HTTPError(request.full_url, 400, "Bad Request", {}, io.BytesIO(b'{"ok":false}'))
        with self.assertRaises(fm.TelegramAPIError) as caught:
            fm.TelegramAPI(token, opener).send(OWNER, "<b>x", parse_mode="HTML")
        self.assertNotIn(token, str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)


class FormattingTests(unittest.TestCase):
    def test_table_becomes_compact_bullets_with_formatting(self):
        rendered = fm.markdown_to_telegram_html(
            "| 항목 | 결과 |\n| --- | --- |\n| 검사 | **통과** |"
        )
        self.assertEqual("• 항목: 검사 · 결과: <b>통과</b>", rendered)

    def test_nested_bold_in_list_and_korean_are_rendered(self):
        rendered = fm.markdown_to_telegram_html("- **핵심 결과**\n  - 다음 단계")
        self.assertEqual("• <b>핵심 결과</b>\n  • 다음 단계", rendered)

    def test_code_escapes_html_characters(self):
        rendered = fm.markdown_to_telegram_html("```python\na < b & c > d\n```")
        self.assertEqual('<pre><code class="language-python">a &lt; b &amp; c &gt; d</code></pre>',
                         rendered)

    def test_inline_code_link_and_untrusted_html_are_safe(self):
        rendered = fm.markdown_to_telegram_html(
            "`a < b & c` [문서](https://example.com?a=1&b=2) <script>"
        )
        self.assertEqual('<code>a &lt; b &amp; c</code> <a href="https://example.com?a=1&amp;b=2">문서</a> '
                         '&lt;script&gt;', rendered)

    def test_long_html_is_split_at_safe_boundaries(self):
        rendered = fm.markdown_to_telegram_html("**" + ("가나다 " * 1200) + "**")
        parts = fm.split_telegram_html(rendered)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(part) <= fm.TELEGRAM_LIMIT for part in parts))
        self.assertTrue(all(part.startswith("<b>") and part.endswith("</b>") for part in parts))
        self.assertEqual(fm._plain_html(rendered), "".join(fm._plain_html(part) for part in parts))


class DoorbellTests(unittest.TestCase):
    def setUp(self):
        self.stream = io.StringIO()
        self.logger = logging.getLogger("doorbell-test-%s" % id(self))
        self.logger.handlers[:] = [logging.StreamHandler(self.stream)]
        self.logger.setLevel(logging.INFO)
        self.clock = Clock()
        self.home = Path("/firstmate")
        self.herdr = FakeHerdr([
            {"pane_id": "wrong-agent", "agent": "other", "cwd": "/firstmate"},
            {"pane_id": "wrong-home", "agent": "pi", "cwd": "/somewhere"},
            {"pane_id": "right", "agent": "pi", "cwd": "/firstmate"},
        ])
        self.doorbell = fm.Doorbell(self.home, True, self.logger, self.herdr, self.clock, agent="pi")

    def test_without_agent_setting_first_agent_pane_in_home_wins_but_never_a_shell(self):
        herdr = FakeHerdr([{"pane_id": "shell", "agent": None, "cwd": "/firstmate"},
                           {"pane_id": "claude", "agent": "claude", "cwd": "/firstmate"}])
        fm.Doorbell(self.home, True, self.logger, herdr, self.clock).queue("note-1")
        self.assertEqual("claude", herdr.calls[1][3])

    def test_unexpected_note_id_is_never_typed_into_a_pane(self):
        self.doorbell.queue("note-1; rm -rf ~\n")
        self.assertEqual([], self.herdr.calls)

    def test_selects_matching_pane_without_message_body(self):
        secret_body = "message body must never enter a terminal"
        self.doorbell.queue("note-7")
        self.assertEqual(["herdr", "pane", "send-text", "right",
                          "[telegram] new captain note note-7 in fm-inbox — drain and reply"],
                         self.herdr.calls[1])
        self.assertEqual(["herdr", "pane", "send-keys", "right", "Enter"], self.herdr.calls[2])
        self.assertNotIn(secret_body, " ".join(" ".join(call) for call in self.herdr.calls))

    def test_coalesces_to_latest_note_after_cooldown(self):
        self.doorbell.queue("one")
        self.clock.now = 5
        self.doorbell.queue("two")
        self.clock.now = 10
        self.doorbell.queue("three")
        self.assertEqual(3, len(self.herdr.calls))
        self.clock.now = 20
        self.doorbell.flush()
        sent = [call for call in self.herdr.calls if call[2] == "send-text"]
        self.assertEqual(2, len(sent))
        self.assertIn("three", sent[-1][-1])

    def test_herdr_failure_is_nonfatal_and_logs_only_note_id(self):
        failing = fm.Doorbell(self.home, True, self.logger, FakeHerdr(fail=True), self.clock)
        failing.queue("note-9")
        self.assertIn("note-9", self.stream.getvalue())

    def test_unexpected_herdr_shape_is_nonfatal(self):
        def bad_shape(args, **_kwargs):
            return subprocess.CompletedProcess(args, 0, "{}", "")
        fm.Doorbell(self.home, True, self.logger, bad_shape, self.clock).queue("note-10")
        self.assertIn("note-10", self.stream.getvalue())


if __name__ == "__main__":
    unittest.main()
