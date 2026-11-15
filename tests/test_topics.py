"""Private-chat topics (Bot API 9.3 Threaded mode): routing in, routing out, and degrading to one chat."""
import io
import json
import logging
import tempfile
import unittest
from pathlib import Path

import fm_telegram as fm
from test_fm_telegram import OWNER, FakeInbox, FakeTelegram, ledger_line, status_line

TOPICS = {"운동": ["fitness-log"], "OSS 스타": ["personal"], "회사": [], "인스타": ["insta"]}


class TopicTelegram(FakeTelegram):
    def __init__(self, enabled=True):
        super().__init__()
        self.enabled, self.created, self.threads, self.broken_threads = enabled, [], [], set()

    def call(self, method, payload):
        assert method == "getMe", method
        return {"id": 1, "is_bot": True, "has_topics_enabled": self.enabled}

    def create_topic(self, chat_id, name):
        self.created.append(name)
        return 100 + len(self.created)

    def send(self, chat_id, text, parse_mode=None, reply_markup=None, reply_to_message_id=None,
             message_thread_id=None):
        if message_thread_id in self.broken_threads:
            raise fm.TelegramAPIError("Telegram API rejected request (HTTP 400)", 400)
        self.threads.append(message_thread_id)
        return super().send(chat_id, text, parse_mode, reply_markup, reply_to_message_id)


def topic_update(number, text, thread, topic=True):
    message = {"message_id": number, "chat": {"id": OWNER}, "text": text,
               "message_thread_id": thread, "is_topic_message": topic}
    return {"update_id": number, "message": message}


class TopicTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.temp.name) / "state"
        self.api, self.inbox = TopicTelegram(), FakeInbox()
        self.log = logging.getLogger("test-topics")
        self.log.handlers[:] = [logging.StreamHandler(io.StringIO())]
        self.bridge = self.make()

    def tearDown(self):
        self.temp.cleanup()

    def make(self, fleet=None):
        return fm.Bridge(self.api, self.inbox, fm.State(self.state_dir), OWNER, fm.Redactor(),
                         self.log, fleet=fleet, topics=TOPICS)

    def test_disabled_bot_behaves_like_one_chat(self):
        self.api.enabled = False
        self.bridge.ensure_topics()
        self.bridge.process_update(topic_update(5, "squat plan", 77))
        self.assertEqual([], self.api.created)
        self.assertEqual([("tg:5", "squat plan")], self.inbox.requests)
        self.assertEqual([None], self.api.threads)
        self.assertNotIn("thread_id", self.bridge.state.data["telegram_note_messages"]["note-tg:5"])
        self.assertIsNone(self.bridge.topic_thread("운동"))

    def test_enabled_bot_creates_configured_topics_once(self):
        self.bridge.ensure_topics()
        self.assertEqual(list(TOPICS), self.api.created)
        self.make().ensure_topics()  # A restart reuses the saved thread ids.
        self.assertEqual(list(TOPICS), self.api.created)
        self.assertEqual(101, self.make().topic_thread("운동"))
        self.assertIsNone(self.bridge.topic_thread("일반"))

    def test_turning_threaded_mode_off_forgets_threads(self):
        self.bridge.ensure_topics()
        self.api.enabled = False
        self.bridge.ensure_topics()
        self.assertEqual({}, fm.State(self.state_dir).data["topic_threads"])

    def test_topic_note_is_tagged_and_reply_returns_to_the_topic_quoting_it(self):
        self.bridge.ensure_topics()
        self.bridge.process_update(topic_update(5, "squat plan", 101))
        self.assertEqual([("tg:5", "[topic:운동] squat plan")], self.inbox.requests)
        self.assertEqual([101], self.api.threads)  # The receipt.
        self.inbox.reply_rows = [{"id": "note-tg:5", "body": "done", "cursor": "1"}]
        self.bridge.check_replies()
        self.assertEqual([101, 101], self.api.threads)
        self.assertEqual(5, self.api.reply_to_message_ids[-1])

    def test_general_chat_and_unknown_topic(self):
        self.bridge.ensure_topics()
        self.bridge.process_update(topic_update(6, "plain", None, topic=False))
        self.bridge.process_update(topic_update(7, "own topic", 555))
        self.assertEqual([("tg:6", "plain"), ("tg:7", "own topic")], self.inbox.requests)
        self.assertEqual([None, 555], self.api.threads)
        self.inbox.reply_rows = [{"id": "note-tg:6", "body": "a", "cursor": "1"}]
        self.bridge.check_replies()
        self.assertEqual((None, None), (self.api.threads[-1], self.api.reply_to_message_ids[-1]))

    def test_topic_service_messages_make_no_note_or_reply(self):
        self.bridge.ensure_topics()
        for number, (kind, thread) in enumerate([("forum_topic_created", 101), ("forum_topic_edited", 101),
                                                 ("forum_topic_closed", 102), ("forum_topic_reopened", 102),
                                                 ("general_forum_topic_hidden", None)], 20):
            message = {"message_id": number, "chat": {"id": OWNER}, kind: {"name": "운동"}}
            if thread:
                message.update(message_thread_id=thread, is_topic_message=True)
            self.bridge.process_update({"update_id": number, "message": message})
        self.assertEqual(([], []), (self.inbox.requests, self.api.sent))

    def test_commands_answer_in_the_sending_topic(self):
        self.bridge.ensure_topics()
        self.bridge.process_update(topic_update(8, "/status", 103))
        self.assertEqual([(OWNER, "healthy")], self.api.sent)
        self.assertEqual([103], self.api.threads)
        self.assertEqual([], self.inbox.requests)

    def test_vanished_topic_falls_back_to_general_chat(self):
        self.bridge.ensure_topics()
        self.api.broken_threads.add(101)
        self.bridge.send("still arrives", thread_id=101)
        self.assertEqual([(OWNER, "still arrives")], self.api.sent)
        self.assertEqual([None], self.api.threads)

    def test_choices_go_to_the_topic_and_the_callback_reply_follows(self):
        self.bridge.ensure_topics()
        self.bridge.send_choices("pick", fm.parse_choices(["Q1:A=yes"]), self.bridge.topic_thread("회사"))
        self.assertEqual([103], self.api.threads)
        request_id = next(iter(self.bridge.state.choices()))
        self.bridge.process_update({"update_id": 9, "callback_query": {
            "id": "cb1", "data": "fmchoice:%s:Q1:A" % request_id,
            "message": {"message_id": 1, "chat": {"id": OWNER}, "message_thread_id": 103,
                        "is_topic_message": True}}})
        self.inbox.reply_rows = [{"id": "note-tg:callback:cb1", "body": "ok", "cursor": "1"}]
        self.bridge.check_replies()
        self.assertEqual(103, self.api.threads[-1])

    def test_fleet_alerts_follow_task_project_and_pr_repo(self):
        ledger = Path(self.temp.name) / "fleet-ledger.jsonl"
        ledger.write_text("")
        fleet = fm.FleetAlerts(ledger, fm.State(self.state_dir), topics=TOPICS)
        self.bridge = self.make(fleet)
        self.bridge.ensure_topics()
        self.bridge.once()  # First enable starts at the end of the ledger.
        with ledger.open("a", encoding="utf-8") as handle:
            handle.write(ledger_line("task.dispatched", task="log-sets", project="fitness-log")
                         + status_line("blocked", "need a key", task="log-sets")
                         + status_line("done", "shipped", task="personal")
                         + ledger_line("task.pr_ready", task="x", pr="https://github.com/me/insta/pull/3")
                         + status_line("failed", "boom", task="unrelated"))
        self.bridge.once()
        self.assertEqual([101, 102, 104, None], self.api.threads)


class ParseTopicsTests(unittest.TestCase):
    def test_validates_shape(self):
        self.assertEqual(TOPICS, fm.parse_topics(TOPICS))
        for bad in ([], {"운동": "fitness"}, {"": []}, {"운동": [1]}):
            with self.assertRaises(RuntimeError):
                fm.parse_topics(bad)


if __name__ == "__main__":
    unittest.main()
