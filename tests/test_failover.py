"""Primary/standby failover: heartbeat, single poller, offsets and queue handoff (fakes only)."""
import io
import json
import logging
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import fm_telegram as fm
from test_fm_telegram import OWNER, FakeInbox, FakeTelegram, callback_update, update

STRANGER = 777


class Server(FakeTelegram):
    """getUpdates with Telegram's rule: an offset confirms, and drops, every lower update id."""
    def __init__(self, updates=()):
        super().__init__()
        self.queue = list(updates)

    def updates(self, offset):
        self.offsets.append(offset)
        self.queue = [item for item in self.queue if item["update_id"] >= offset]
        return list(self.queue)


class Link:
    """Stands in for `ssh minipc fm_telegram.py`: runs standby claim/ack on the standby's state."""
    def __init__(self, state_dir, fail=False):
        self.state_dir, self.fail, self.calls = state_dir, fail, []

    def __call__(self, args, **_kwargs):
        self.calls.append(args)
        if self.fail:
            return fm.subprocess.CompletedProcess(args, 255, "", "ssh: connect to host minipc")
        out = io.StringIO()
        with redirect_stdout(out):
            code = fm.main(["--state-dir", str(self.state_dir), *args[args.index("standby"):]])
        return fm.subprocess.CompletedProcess(args, code, out.getvalue(), "")


def logger():
    log = logging.getLogger("fm-telegram-failover-test")
    log.handlers[:] = [logging.NullHandler()]
    return log


class FailoverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.server = Server()
        self.routed = FakeInbox()
        self.standby = self.make_standby()
        self.main_inbox = FakeInbox()
        self.link = Link(self.root / "standby")
        self.primary = fm.Bridge(self.server, self.main_inbox, fm.State(self.root / "primary"), OWNER,
                                 fm.Redactor(), logger(),
                                 failover=fm.Failover(["ssh", "minipc", "fm_telegram.py"], 60,
                                                      runner=self.link))

    def tearDown(self):
        self.temp.cleanup()

    def make_standby(self):
        standby = fm.Standby(self.server, fm.State(self.root / "standby"), OWNER, fm.Redactor(),
                             logger(), {"불침번:": self.routed}, 180, "맥이 자고 있어요 💤")
        standby.started = 0.0  # Long running: only the heartbeat file can keep it idle.
        return standby

    def standby_iteration(self, standby=None):
        standby = standby or self.standby
        with patch.object(fm.time, "sleep", side_effect=lambda _s: setattr(standby, "stopping", True)):
            standby.stopping = False
            standby.run(0)

    def age_heartbeat(self, seconds):
        path = self.root / "standby" / fm.HEARTBEAT_FILE
        os.utime(path, (time.time() - seconds, time.time() - seconds))

    def queued(self):
        return fm.update_queue(self.standby.state, list)

    def test_standby_does_not_poll_while_heartbeat_is_fresh_or_just_started(self):
        fm.standby_claim(self.standby.state, 0)
        self.standby_iteration()
        self.assertEqual([], self.server.offsets)
        fresh = self.make_standby()
        fresh.started = time.time()  # No heartbeat yet: a restart still waits stale_seconds.
        (self.root / "standby" / fm.HEARTBEAT_FILE).unlink()
        self.standby_iteration(fresh)
        self.assertEqual([], self.server.offsets)

    def test_stale_heartbeat_polls_queues_replies_and_routes(self):
        fm.standby_claim(self.standby.state, 0)
        self.age_heartbeat(181)
        self.server.queue = [update(1, OWNER, "hello"), update(2, STRANGER, "spam"),
                             update(3, OWNER, "  불침번: 오늘 진도"),
                             callback_update(4, OWNER, "cb-1", "fmchoice:r:1:a")]
        self.standby_iteration()
        self.assertEqual([0], self.server.offsets)
        self.assertEqual([1, 4], [item["update_id"] for item in self.queued()])
        self.assertEqual([("tg:3", "  불침번: 오늘 진도")], self.routed.requests)
        self.assertEqual([(OWNER, "맥이 자고 있어요 💤"), (OWNER, "Received. Note ID: note-tg:3")],
                         self.server.sent)  # One reply per owner message, none to the stranger.
        self.assertEqual([("cb-1", "맥이 자고 있어요 💤")], self.server.callback_answers)
        self.assertEqual(5, self.standby.state.data["offset"])
        # A redelivered update (offset lost in a crash) is neither queued nor answered twice.
        self.standby.process_update(update(1, OWNER, "hello"))
        self.assertEqual(2, len(self.queued()))
        self.assertEqual(2, len(self.server.sent))

    def test_standby_never_polls_behind_the_primary_offset(self):
        fm.standby_claim(self.standby.state, 42)
        self.age_heartbeat(181)
        self.standby_iteration()
        self.assertEqual([42], self.server.offsets)

    def test_conflict_makes_the_standby_yield(self):
        self.server.updates = lambda _offset: (_ for _ in ()).throw(
            fm.TelegramAPIError("Telegram API rejected request (HTTP 409)", 409))
        self.standby_iteration()
        self.assertTrue(self.standby.yield_until > time.time() + 170)
        self.assertTrue(self.standby.primary_alive())

    def test_claim_waits_for_the_active_poll_to_finish(self):
        state = self.standby.state
        with state._locked(state.directory / fm.ACTIVE_LOCK):
            with self.assertRaises(RuntimeError):
                fm.standby_claim(state, 0, wait=0.6)
        self.assertEqual({"offset": 0, "updates": []}, fm.standby_claim(state, 0, wait=0.6))

    def test_ack_drops_only_taken_over_updates(self):
        for number in (5, 6, 7):
            fm.update_queue(self.standby.state, lambda items, n=number: items.append(update(n, OWNER, "x")))
        fm.standby_ack(self.standby.state, 6)
        self.assertEqual([7], [item["update_id"] for item in self.queued()])

    def test_primary_takes_over_in_order_without_double_handling(self):
        # The primary handled 1 and 2, then slept before confirming them with another poll.
        self.server.queue = [update(1, OWNER, "one"), update(2, OWNER, "two")]
        self.primary.once()
        self.assertEqual(["tg:1", "tg:2"], [r for r, _ in self.main_inbox.requests])
        self.server.queue += [update(3, OWNER, "three"), update(4, OWNER, "불침번: four"),
                              update(5, OWNER, "five")]
        self.age_heartbeat(181)
        self.standby_iteration()
        # Worst case: 1 and 2 were never confirmed, so the standby receives them again.
        self.assertEqual([1, 2, 3, 5], [item["update_id"] for item in self.queued()])
        # The standby's last batch is still unconfirmed when the primary wakes up.
        self.server.queue += [update(6, OWNER, "six")]
        self.primary.failover.last = time.time() - 3600
        self.primary.once()
        self.assertEqual(["tg:1", "tg:2", "tg:3", "tg:5", "tg:6"],
                         [r for r, _ in self.main_inbox.requests])  # Queue first, then new; 1, 2, 4 once.
        self.assertEqual(["tg:4"], [r for r, _ in self.routed.requests])
        self.assertEqual([], self.queued())
        self.assertEqual(7, self.primary.state.data["offset"])
        self.assertEqual(["ssh", "minipc", "fm_telegram.py", "standby", "claim", "3"], self.link.calls[-2])
        self.assertEqual(["ssh", "minipc", "fm_telegram.py", "standby", "ack", "5"], self.link.calls[-1])
        polls = len(self.server.offsets)
        self.standby_iteration()  # The claim refreshed the heartbeat: the standby stays idle.
        self.assertEqual(polls, len(self.server.offsets))

    def test_unreachable_standby_does_not_stop_the_primary(self):
        self.primary.failover.runner = Link(self.root / "standby", fail=True)
        self.server.queue = [update(1, OWNER, "one")]
        self.primary.once()
        self.assertEqual(["tg:1"], [r for r, _ in self.main_inbox.requests])
        self.assertFalse(self.primary.failover.due())  # No ssh attempt on every poll.

    def test_failed_queue_handoff_is_retried_before_the_next_poll(self):
        fm.update_queue(self.standby.state, lambda items: items.append(update(3, OWNER, "three")))
        self.main_inbox.note = lambda *_args: (_ for _ in ()).throw(RuntimeError("fm-inbox command failed"))
        with self.assertRaises(RuntimeError):
            self.primary.once()
        self.assertTrue(self.primary.failover.due())
        self.assertEqual([3], [item["update_id"] for item in self.queued()])  # Not acknowledged.
        self.assertEqual([], self.server.offsets)  # Never polled past the queued update.
        del self.main_inbox.note
        self.primary.once()
        self.assertEqual(["tg:3"], [r for r, _ in self.main_inbox.requests])
        self.assertEqual([], self.queued())

    def test_late_callback_is_recorded_even_when_telegram_rejects_the_answer(self):
        def reject(*_args):
            raise fm.TelegramAPIError("Telegram API rejected request (HTTP 400)", 400)
        self.server.answer_callback = reject
        fm.update_queue(self.standby.state, lambda items: items.append(
            callback_update(8, OWNER, "cb-old", "fmchoice:r:1:a")))
        self.primary.once()
        self.assertEqual([], self.queued())  # A stale toast never blocks the queue.

    def test_claim_output_is_valid_for_the_failover_parser(self):
        fm.update_queue(self.standby.state, lambda items: items.append(update(9, OWNER, "한글")))
        claim = self.primary.failover.claim(0)
        self.assertEqual([9], [item["update_id"] for item in claim["updates"]])
        broken = fm.Failover(["x"], 60, runner=lambda args, **_k: fm.subprocess.CompletedProcess(
            args, 0, '{"offset": "1", "updates": []}', ""))
        with self.assertRaises(RuntimeError):
            broken.claim(0)

    def test_standby_files_are_private(self):
        fm.standby_claim(self.standby.state, 0)
        fm.update_queue(self.standby.state, lambda items: items.append(update(1, OWNER, "x")))
        for name in (fm.HEARTBEAT_FILE, fm.QUEUE_FILE, fm.ACTIVE_LOCK):
            self.assertEqual(0o600, (self.root / "standby" / name).stat().st_mode & 0o777, name)

    def test_routes_must_point_at_a_firstmate_home(self):
        config = self.root / "config.json"
        config.write_text(json.dumps({"owner_chat_id": OWNER,
                                      "standby_routes": {"인스타:": str(self.root / "nowhere")}}))
        config.chmod(0o600)
        with patch.dict(os.environ, {fm.TOKEN_ENV: "123:fake"}):
            with self.assertRaisesRegex(RuntimeError, "no bin/fm-inbox.sh"):
                fm.main(["--config", str(config), "--state-dir", str(self.root / "s"), "standby", "run"])


if __name__ == "__main__":
    unittest.main()
