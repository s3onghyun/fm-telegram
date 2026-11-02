"""Primary/standby failover: heartbeat, single poller, offsets and queue handoff (fakes only)."""
import io
import json
import logging
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import fm_telegram as fm
from test_fm_telegram import OWNER, FakeInbox, FakeTelegram, callback_update, update

STRANGER = 777
MINI = {"미니": {"commands": ["/m", "/p"], "receipt": "🖥️ → 미니에게 전달했어요",
                "home": "~/homes/personal",
                "homes": {"insta": {"home": "~/homes/insta", "tag": "인스타 담당",
                                    "keywords": ["인스타", "릴스", "캐러셀", "altearth", "만약의 지구"]}}}}


def mini_routes(personal=None, insta=None):
    """The 미니 route from MINI, with fake inboxes standing in for the standby's homes."""
    route = fm.parse_routes(MINI, local=False)["미니"]
    homes = (replace(route.homes[0], inbox=personal), replace(route.homes[1], inbox=insta))
    return {"미니": replace(route, homes=homes)}


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
    """Stands in for `ssh minipc fm_telegram.py`: runs standby claim/ack/deliver on the standby."""
    def __init__(self, state_dir, fail=False, standby=None):
        self.state_dir, self.fail, self.calls, self.standby = state_dir, fail, [], standby

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        if self.fail:
            return fm.subprocess.CompletedProcess(args, 255, "", "ssh: connect to host minipc")
        out = io.StringIO()
        with redirect_stdout(out):
            if args[-1] == "deliver":  # main() would build the standby from its config and token.
                print(json.dumps(fm.standby_deliver(self.standby, json.loads(kwargs["input"]))))
                code = 0
            else:
                code = fm.main(["--state-dir", str(self.state_dir), *args[args.index("standby"):]])
        return fm.subprocess.CompletedProcess(args, code, out.getvalue(), "")


def logger():
    log = logging.getLogger("fm-telegram-failover-test")
    log.handlers[:] = [logging.NullHandler()]
    return log


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.server = Server()
        self.routed, self.insta = FakeInbox(), FakeInbox()
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
                             logger(), mini_routes(self.routed, self.insta), 180, "맥이 자고 있어요 💤")
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


class FailoverTests(Fixture):
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
                             update(3, OWNER, "  미니야 오늘 진도"),
                             callback_update(4, OWNER, "cb-1", "fmchoice:r:1:a")]
        self.standby_iteration()
        self.assertEqual([0], self.server.offsets)
        self.assertEqual([1, 3, 4], [item["update_id"] for item in self.queued()])
        self.assertEqual({"routed": "미니", "routed_home": None},
                         {key: self.queued()[1][key] for key in ("routed", "routed_home")})
        self.assertEqual([("tg:3", "오늘 진도")], self.routed.requests)
        self.assertEqual([(OWNER, "맥이 자고 있어요 💤"), (OWNER, "🖥️ → 미니에게 전달했어요")],
                         self.server.sent)  # One reply per owner message, none to the stranger.
        self.assertEqual([(OWNER, 3, "👨‍💻")], self.server.reactions)
        self.assertEqual([("cb-1", "맥이 자고 있어요 💤")], self.server.callback_answers)
        self.assertEqual(5, self.standby.state.data["offset"])
        # A redelivered update (offset lost in a crash) is neither queued nor answered twice.
        self.standby.process_update(update(1, OWNER, "hello"))
        self.assertEqual(3, len(self.queued()))
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
        self.server.queue += [update(3, OWNER, "three"), update(4, OWNER, "미니야 four"),
                              update(5, OWNER, "five")]
        self.age_heartbeat(181)
        self.standby_iteration()
        # Worst case: 1 and 2 were never confirmed, so the standby receives them again.
        # 4 went to 미니's home at once and waits in the queue only as an FYI copy.
        self.assertEqual([1, 2, 3, 4, 5], [item["update_id"] for item in self.queued()])
        # The standby's last batch is still unconfirmed when the primary wakes up.
        self.server.queue += [update(6, OWNER, "six")]
        self.primary.failover.last = time.time() - 3600
        self.primary.once()
        self.assertEqual(["tg:1", "tg:2", "tg:3", "tg:4", "tg:5", "tg:6"],
                         [r for r, _ in self.main_inbox.requests])  # Queue first, then new; 1, 2 once.
        self.assertTrue(self.main_inbox.requests[3][1].startswith("[FYI routed-to:미니] "))
        self.assertTrue(self.main_inbox.requests[3][1].endswith("\nfour"))
        self.assertNotIn("note-tg:4", self.primary.state.data["telegram_note_ids"])
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
                                      "standby_routes": {"미니": {"home": str(self.root / "nowhere")}}}))
        config.chmod(0o600)
        with patch.dict(os.environ, {fm.TOKEN_ENV: "123:fake"}):
            with self.assertRaisesRegex(RuntimeError, "no bin/fm-inbox.sh"):
                fm.main(["--config", str(config), "--state-dir", str(self.root / "s"), "standby", "run"])


def reply_to(number, text, replied):
    message = update(number, OWNER, text)
    message["message"]["reply_to_message"] = {"message_id": 900, "text": replied}
    return message


class NameCallTests(Fixture):
    """미니야 …, /m, /p and replies to 미니's messages reach the standby's homes (fakes only)."""
    def setUp(self):
        super().setUp()
        self.routes = mini_routes(self.routed, self.insta)
        self.server.set_commands = lambda commands: self.server.__dict__.setdefault(
            "commands", []).append(commands)
        self.primary.routes = self.routes
        self.primary.failover.runner = self.link
        self.link.standby = self.standby
        fm.standby_claim(self.standby.state, 0)  # The primary is alive: the standby only delivers.

    def routed_text(self, text):
        matched = fm.match_route(self.routes, update(1, OWNER, text)["message"])
        return matched and matched[1]["text"]

    def test_calls_at_the_start_are_stripped(self):
        for text, rest in [("미니야 오늘 뭐해", "오늘 뭐해"), ("미니아 오늘 뭐해", "오늘 뭐해"),
                           ("미니, 오늘 뭐해", "오늘 뭐해"), ("  미니 오늘 뭐해", "오늘 뭐해"),
                           ("미니야! 뭐해", "뭐해"), ("/p 여기서 내가 할 수 있는 프로젝트가 뭐야",
                                                  "여기서 내가 할 수 있는 프로젝트가 뭐야"),
                           ("/m@fm_bot 안녕", "안녕"), ("/M 안녕", "안녕"), ("미니야", "미니야")]:
            self.assertEqual(rest, self.routed_text(text), text)

    def test_lookalikes_are_not_calls(self):
        for text in ["미니멀 라이프", "미니아폴리스 여행", "인스타그램 링크", "/pass", "/mp3 변환",
                     "오늘 미니야 뭐해", "미니언즈 보러 가자", "/status"]:
            self.assertIsNone(fm.match_route(self.routes, update(1, OWNER, text)["message"]), text)

    def test_reply_to_a_mini_message_routes_back_to_the_home_that_sent_it(self):
        route = self.routes["미니"]
        for replied, key in [("[미니] 오늘 할 일이에요", None), ("[미니] (인스타 담당) 초안이에요", "insta"),
                             ("🖥️ → 미니에게 전달했어요 (인스타 담당)", "insta"),
                             ("🖥️ → 미니에게 전달했어요", None)]:
            message = reply_to(1, "좋아 진행해", replied)["message"]
            matched_route, routed = fm.match_route(self.routes, message)
            self.assertEqual(("좋아 진행해", key), (routed["text"], route.home_for(routed).key), replied)
        self.assertIsNone(fm.match_route(self.routes, reply_to(1, "응", "Received. Note ID: x")["message"]))

    def test_keywords_pick_the_insta_home_and_everything_else_goes_to_personal(self):
        route = self.routes["미니"]
        for text, key in [("릴스 아이디어", "insta"), ("만약의 지구 캐러셀", "insta"),
                          ("ALTEARTH 업로드", "insta"), ("장보기 목록", None)]:
            self.assertEqual(key, route.home_for({"text": text}).key, text)

    def test_primary_hands_a_named_message_to_the_standby_and_keeps_an_fyi(self):
        self.server.queue = [update(1, OWNER, "미니야 릴스 기획 세 개")]
        self.primary.once()
        self.assertEqual([("tg:1", "릴스 기획 세 개")], self.insta.requests)
        self.assertEqual([], self.routed.requests)
        self.assertEqual(["ssh", "minipc", "fm_telegram.py", "standby", "deliver"], self.link.calls[-1])
        # One receipt, from the standby: 👨‍💻 plus the delivery line, never "Received. Note ID".
        self.assertEqual([(OWNER, "🖥️ → 미니에게 전달했어요 (인스타 담당)")], self.server.sent)
        self.assertEqual([(OWNER, 1, "👨‍💻")], self.server.reactions)
        [(request_id, body)] = self.main_inbox.requests
        self.assertEqual("tg:1", request_id)
        self.assertTrue(body.startswith("[FYI routed-to:미니/insta] "), body)
        self.assertTrue(body.endswith("\n릴스 기획 세 개"), body)
        self.assertEqual([], self.queued())  # Delivered live: nothing for a later handoff.
        # Redelivered (the primary crashed before saving its offset): no second note or receipt.
        self.primary.process_update(update(1, OWNER, "미니야 릴스 기획 세 개"))
        self.standby.process_update(update(1, OWNER, "미니야 릴스 기획 세 개"))
        self.assertEqual((1, 1, 1), (len(self.insta.requests), len(self.main_inbox.requests),
                                     len(self.server.sent)))

    def test_fyi_copy_never_produces_a_telegram_reply(self):
        self.primary.process_update(update(1, OWNER, "/p 오늘 뭐 할 수 있어?"))
        self.main_inbox.reply_rows = [{"id": "note-tg:1", "body": "FYI 확인", "cursor": "1"}]
        sent = len(self.server.sent)
        self.primary.check_replies()
        self.assertEqual(sent, len(self.server.sent))
        self.assertEqual([("tg:1", "오늘 뭐 할 수 있어?")], self.routed.requests)

    def test_reply_to_mini_routes_on_the_primary_too(self):
        self.primary.process_update(reply_to(2, "이걸로 해줘", "[미니] (인스타 담당) 초안 두 개"))
        self.assertEqual([("tg:2", "이걸로 해줘")], self.insta.requests)
        self.assertEqual([(OWNER, "🖥️ → 미니에게 전달했어요 (인스타 담당)")], self.server.sent)

    def test_unnamed_text_still_goes_to_main(self):
        self.primary.process_update(update(3, OWNER, "미니멀하게 정리해줘"))
        self.assertEqual([("tg:3", "미니멀하게 정리해줘")], self.main_inbox.requests)
        self.assertEqual([(OWNER, "Received. Note ID: note-tg:3")], self.server.sent)
        self.assertNotIn("deliver", [call[-1] for call in self.link.calls])

    def test_unreachable_standby_keeps_a_named_message_in_main(self):
        self.primary.failover.runner = Link(self.root / "standby", fail=True)
        self.primary.process_update(update(4, OWNER, "미니야 장보기"))
        self.assertEqual([("tg:4", "미니야 장보기")], self.main_inbox.requests)  # Call kept: for 미니.
        self.assertEqual(["⚠️ 미니에게 전달하지 못해서 메인에 남길게요", "Received. Note ID: note-tg:4"],
                         [text for _chat, text in self.server.sent])

    def test_short_commands_are_registered_with_status_and_help(self):
        self.primary.register_commands()
        self.assertEqual([["m", "p", "status", "help"]],
                         [[item["command"] for item in commands] for commands in self.server.commands])
        bare = fm.Bridge(self.server, self.main_inbox, self.primary.state, OWNER, fm.Redactor(), logger())
        bare.register_commands()  # No routes: the menu is left alone.
        self.assertEqual(1, len(self.server.commands))

    def test_relayed_answers_carry_the_label_and_route_back(self):
        self.assertEqual("[미니] 오늘 할 일", self.primary.labelled("미니", "오늘 할 일"))
        labelled = self.primary.labelled("미니/insta", "초안 두 개")
        self.assertEqual("[미니] (인스타 담당) 초안 두 개", labelled)
        self.assertIn("[미니] (인스타 담당) 초안 두 개", fm.markdown_to_telegram_html(labelled))
        self.assertEqual("insta", self.routes["미니"].home_for(reply_to(1, "응", labelled)["message"]).key)
        for sender in ("인스타", "미니/nowhere"):
            with self.assertRaises(RuntimeError):
                self.primary.labelled(sender, "x")

    def test_route_config_is_validated(self):
        with self.assertRaisesRegex(RuntimeError, "commands"):
            fm.parse_routes({"미니": {"home": "~/h", "commands": ["/P"]}}, local=False)
        with self.assertRaisesRegex(RuntimeError, "home path"):
            fm.parse_routes({"미니": {"homes": {}}}, local=False)
        config = self.root / "primary.json"
        config.write_text(json.dumps({"owner_chat_id": OWNER, "firstmate_home": str(self.root),
                                      "standby_routes": MINI}))
        config.chmod(0o600)
        with patch.dict(os.environ, {fm.TOKEN_ENV: "123:fake"}):
            with self.assertRaisesRegex(RuntimeError, "needs failover_command"):
                fm.main(["--config", str(config), "--state-dir", str(self.root / "p"), "run"])


if __name__ == "__main__":
    unittest.main()
