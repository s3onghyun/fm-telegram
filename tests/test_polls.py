"""Polls: notify --poll sends one into a topic, and only the owner's votes become notes."""
import io
import logging
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stderr
from pathlib import Path

import fm_telegram as fm
from test_fm_telegram import OWNER, FakeInbox
from test_topics import TOPICS, TopicTelegram


class PollTelegram(TopicTelegram):
    def __init__(self):
        super().__init__()
        self.polls = []

    def call(self, method, payload):
        if method != "sendPoll":
            return super().call(method, payload)
        self.polls.append(payload)
        return {"message_id": 900 + len(self.polls), "poll": {"id": "poll-%d" % len(self.polls)}}


def vote(number, option_ids, user=OWNER, poll_id="poll-1"):
    return {"update_id": number, "poll_answer": {"poll_id": poll_id, "user": {"id": user},
                                                 "option_ids": option_ids}}


class PollTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.temp.name) / "state"
        # fm.main defaults --home to FM_HOME; never let a test reach a real Firstmate home.
        environment = patch.dict(fm.os.environ, {"FM_HOME": self.temp.name})
        environment.start()
        self.addCleanup(environment.stop)
        self.api, self.inbox = PollTelegram(), FakeInbox()
        self.log = logging.getLogger("test-polls")
        self.log.handlers[:] = [logging.StreamHandler(io.StringIO())]
        self.bridge = self.make()
        self.bridge.ensure_topics()

    def tearDown(self):
        self.temp.cleanup()

    def make(self):
        return fm.Bridge(self.api, self.inbox, fm.State(self.state_dir), OWNER, fm.Redactor(),
                         self.log, topics=TOPICS)

    def send(self, multi=True):
        # notify runs in its own process: a separate Bridge on the same state directory.
        self.make().send_poll("이번 주 운동", ["월 스쿼트", "수 데드", "금 벤치"], multi,
                              self.bridge.topic_thread("운동"))

    def test_poll_is_sent_non_anonymous_into_the_topic(self):
        self.send()
        self.assertEqual([{"chat_id": OWNER, "question": "이번 주 운동",
                           "options": [{"text": "월 스쿼트"}, {"text": "수 데드"}, {"text": "금 벤치"}],
                           "is_anonymous": False, "allows_multiple_answers": True,
                           "message_thread_id": 101}], self.api.polls)

    def test_owner_vote_becomes_a_topic_note_and_reply_quotes_the_poll(self):
        self.send()  # The running bridge has not reloaded state since: it must read polls fresh.
        self.bridge.process_update(vote(7, [0, 2]))
        self.assertEqual([("tg:7", "[topic:운동] 📊 투표: 이번 주 운동 → 월 스쿼트, 금 벤치")],
                         self.inbox.requests)
        self.assertEqual([], self.api.sent)  # No receipt per vote.
        self.bridge.process_update(vote(7, [0, 2]))  # A replay is not noted twice.
        self.assertEqual(1, len(self.inbox.requests))
        self.inbox.reply_rows = [{"id": "note-tg:7", "body": "기록했어요", "cursor": "1"}]
        self.bridge.check_replies()
        self.assertEqual(([101], 901), (self.api.threads, self.api.reply_to_message_ids[-1]))

    def test_retracted_vote_is_cancel(self):
        self.send()
        self.bridge.process_update(vote(8, []))
        self.assertEqual([("tg:8", "[topic:운동] 📊 투표: 이번 주 운동 → 취소")], self.inbox.requests)

    def test_other_voters_and_unknown_polls_are_ignored(self):
        self.send()
        self.bridge.process_update(vote(9, [1], user=12345))
        self.bridge.process_update(vote(10, [1], poll_id="someone-elses"))
        self.bridge.process_update({"update_id": 11, "poll": {"id": "poll-1", "is_closed": True}})
        self.assertEqual(([], []), (self.inbox.requests, self.api.sent))

    def test_general_chat_poll_has_no_topic_tag(self):
        self.make().send_poll("Q", ["a", "b"])
        self.assertNotIn("message_thread_id", self.api.polls[0])
        self.bridge.process_update(vote(12, [1]))
        self.assertEqual([("tg:12", "📊 투표: Q → b")], self.inbox.requests)

    def test_remembered_polls_are_bounded(self):
        for _ in range(fm.MAX_POLLS + 3):
            self.make().send_poll("Q", ["a", "b"])
        polls = fm.State(self.state_dir).data["polls"]
        self.assertEqual(fm.MAX_POLLS, len(polls))
        self.assertNotIn("poll-1", polls)

    def test_notify_needs_text_or_poll(self):
        for argv in (["notify"], ["notify", "hi", "--poll", "Q"]):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                fm.main(["--state-dir", str(self.state_dir)] + argv)


if __name__ == "__main__":
    unittest.main()
