"""Behaviour tests for the RESP parser and command kernel.

Run them from the project root:

    python3 -m unittest discover -s tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from resp.core import Parser, Session, Store, encode_array

TEXT = "\u6c49\u5b57"
TEXT_BYTES = TEXT.encode("utf-8")


class FakeClock:
    """A clock that only moves when the test moves it."""

    def __init__(self, start=0.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class WireFormatTest(unittest.TestCase):
    def test_a_bulk_string_leaves_the_parser_aligned(self):
        parser = Parser()
        raw = b"*2\r\n$3\r\nGET\r\n$6\r\n" + TEXT_BYTES + b"\r\n"
        parser.feed(raw)
        self.assertEqual(parser.read_command(), ["GET", TEXT])
        self.assertEqual(encode_array(["GET", TEXT]), raw)
        self.assertEqual(parser.pending(), 0)

    def test_pipelined_commands_each_get_their_own_reply(self):
        session = Session()
        session.feed(
            b"*3\r\n$3\r\nSET\r\n$1\r\nk\r\n$6\r\n" + TEXT_BYTES + b"\r\n"
            b"*2\r\n$3\r\nGET\r\n$1\r\nk\r\n"
        )
        self.assertEqual(session.read_reply(), b"+OK\r\n")
        self.assertEqual(session.read_reply(), b"$6\r\n" + TEXT_BYTES + b"\r\n")
        self.assertEqual(session.pending(), 0)

    def test_an_inline_command_follows_an_array_command(self):
        session = Session()
        session.feed(b"*3\r\n$3\r\nSET\r\n$1\r\nk\r\n$2\r\nok\r\n" b"GET \t k\r\n")
        self.assertEqual(session.read_reply(), b"+OK\r\n")
        self.assertEqual(session.read_reply(), b"$2\r\nok\r\n")

    def test_inline_commands_ignore_extra_whitespace(self):
        parser = Parser()
        parser.feed(b"GET k\r\n")
        self.assertEqual(parser.read_command(), ["GET", "k"])
        parser.feed(b"GET   k\r\n")
        self.assertEqual(parser.read_command(), ["GET", "k"])
        parser.feed(b"GET\tk\r\n")
        self.assertEqual(parser.read_command(), ["GET", "k"])


class ExpiryTest(unittest.TestCase):
    def test_set_with_ex_keeps_the_key_for_the_requested_seconds(self):
        clock = FakeClock()
        session = Session(Store(clock=clock))
        self.assertEqual(
            session.execute(["SET", "token", "abc", "EX", "10"]), b"+OK\r\n"
        )
        clock.advance(1)
        self.assertEqual(session.execute(["GET", "token"]), b"$3\r\nabc\r\n")
        clock.advance(9)
        self.assertEqual(session.execute(["GET", "token"]), b"$-1\r\n")

    def test_expiring_with_zero_seconds_removes_the_key_at_once(self):
        clock = FakeClock()
        session = Session(Store(clock=clock))
        session.execute(["SET", "job", "abc"])
        self.assertEqual(session.execute(["EXPIRE", "job", "0"]), b":1\r\n")
        self.assertEqual(session.execute(["GET", "job"]), b"$-1\r\n")
        self.assertEqual(session.execute(["EXPIRE", "missing", "10"]), b":0\r\n")

    def test_an_expired_key_stays_gone(self):
        clock = FakeClock()
        store = Store(clock=clock)
        session = Session(store)
        store.set("k", "v", ttl=5)
        clock.advance(6)
        self.assertEqual(session.execute(["GET", "k"]), b"$-1\r\n")
        self.assertEqual(session.execute(["GET", "k"]), b"$-1\r\n")
        self.assertEqual(store.keys(), [])
        self.assertEqual(session.execute(["SET", "k", "new"]), b"+OK\r\n")
        self.assertEqual(session.execute(["GET", "k"]), b"$3\r\nnew\r\n")


class CommandTest(unittest.TestCase):
    def test_incr_overflow_reports_an_error_and_keeps_the_value(self):
        session = Session()
        self.assertEqual(
            session.execute(["SET", "big", "9223372036854775807"]), b"+OK\r\n"
        )
        self.assertTrue(session.execute(["INCR", "big"]).startswith(b"-ERR"))
        self.assertEqual(
            session.execute(["GET", "big"]), b"$19\r\n9223372036854775807\r\n"
        )
        self.assertEqual(session.execute(["INCR", "fresh"]), b":1\r\n")
        session.execute(["SET", "text", "abc"])
        self.assertTrue(session.execute(["INCR", "text"]).startswith(b"-ERR"))
        self.assertEqual(session.execute(["GET", "text"]), b"$3\r\nabc\r\n")

    def test_unknown_commands_are_answered_with_an_error(self):
        session = Session()
        reply = session.execute(["BOGUS"])
        self.assertTrue(reply.startswith(b"-ERR"), reply)
        self.assertIn(b"BOGUS", reply)
        reply = session.execute(["GET"])
        self.assertTrue(reply.startswith(b"-ERR"), reply)
        self.assertIn(b"arguments", reply)
        self.assertEqual(session.execute(["SET", "k", "v"]), b"+OK\r\n")
        session.feed(b"*x\r\n")
        self.assertTrue(session.read_reply().startswith(b"-ERR"))


if __name__ == "__main__":
    unittest.main()
