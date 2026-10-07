"""A RESP wire parser and command kernel built on the standard library only.

The module never touches a socket: callers hand raw bytes to a
:class:`Parser`, run the commands it produces through a :class:`Session`,
and write the returned bytes back to the wire.

Two client encodings are accepted, as in the original protocol: inline
commands (whitespace separated text terminated by CRLF) and arrays of bulk
strings.  Bulk string values travel as text and are encoded as UTF-8 on the
way out.
"""

import time

CRLF = b"\r\n"

#: Null bulk string, the reply for a missing key.
NIL_REPLY = b"$-1\r\n"

#: Upper bound of a bulk string, mirroring the 512 MB protocol limit.
MAX_BULK_LENGTH = 512 * 1024 * 1024

#: Signed 64 bit range used by the numeric commands.
INT64_MIN = -(2 ** 63)
INT64_MAX = 2 ** 63 - 1

_DIGITS = frozenset("0123456789")


class ProtocolError(Exception):
    """Raised when a byte stream does not follow the RESP grammar."""


class CommandError(Exception):
    """Raised by a command to turn into an error reply."""

    def __init__(self, message):
        super().__init__(message)
        self.message = message


# ---------------------------------------------------------------------------
# Reply encoding
# ---------------------------------------------------------------------------

def encode_simple(message):
    """Encode a RESP simple string."""
    return b"+" + message.encode("utf-8", "surrogateescape") + CRLF


def encode_error(message):
    """Encode a RESP error."""
    return b"-" + message.encode("utf-8", "surrogateescape") + CRLF


def encode_integer(number):
    """Encode a RESP integer."""
    return b":" + str(int(number)).encode("ascii") + CRLF


def encode_bulk(text):
    """Encode a RESP bulk string, or the null bulk string for None."""
    if text is None:
        return NIL_REPLY
    payload = text.encode("utf-8", "surrogateescape")
    return b"$" + str(len(payload)).encode("ascii") + CRLF + payload + CRLF


def encode_array(items):
    """Encode a RESP array of bulk strings."""
    chunks = [b"*" + str(len(items)).encode("ascii") + CRLF]
    for item in items:
        chunks.append(encode_bulk(item))
    return b"".join(chunks)


def parse_integer(text):
    """Parse a plain decimal integer, the way the commands expect one."""
    body = text[1:] if text[:1] in ("+", "-") else text
    if not body or any(character not in _DIGITS for character in body):
        raise CommandError("ERR value is not an integer or out of range")
    return int(text)


# ---------------------------------------------------------------------------
# Request parsing
# ---------------------------------------------------------------------------

class Parser:
    """Incremental parser for incoming client bytes.

    ``feed`` appends bytes to the buffer and ``read_command`` takes the next
    complete command off it as a list of text arguments.  While the buffered
    bytes do not hold a complete command, ``read_command`` returns None and
    leaves the buffer untouched.
    """

    def __init__(self):
        self._buffer = bytearray()

    def feed(self, data):
        """Append raw bytes to the buffer."""
        if isinstance(data, str):
            raise TypeError("feed() takes bytes, not text")
        self._buffer.extend(data)

    def pending(self):
        """Number of buffered bytes that have not been consumed yet."""
        return len(self._buffer)

    def read_command(self):
        """Return the next command, or None while more bytes are needed."""
        while self._buffer:
            if self._buffer[:1] == b"*":
                args, consumed = self._read_array()
            else:
                args, consumed = self._read_inline()
            if args is None:
                return None
            del self._buffer[:consumed]
            if args:
                return args
        return None

    def _read_line(self, start):
        """Return (line, next_offset) for the CRLF terminated line at start."""
        end = self._buffer.find(CRLF, start)
        if end < 0:
            return None, 0
        line = bytes(self._buffer[start:end]).decode("utf-8", "surrogateescape")
        return line, end + len(CRLF)

    def _read_bulk(self, start):
        """Return (value, next_offset) for the bulk string that starts there."""
        header, payload_at = self._read_line(start)
        if header is None:
            return None, 0
        if not header.startswith("$"):
            raise ProtocolError("expected a bulk string")
        length = _bulk_length(header[1:])
        end = payload_at + length
        if len(self._buffer) < end + len(CRLF):
            return None, 0
        value = bytes(self._buffer[payload_at:end]).decode("utf-8", "surrogateescape")
        return value, end + len(CRLF)

    def _read_array(self):
        """Return (arguments, next_offset) for the array command at the head."""
        header, offset = self._read_line(0)
        if header is None:
            return None, 0
        if not header.startswith("*"):
            raise ProtocolError("expected an array header")
        count = _array_count(header[1:])
        if count < 0:
            return None, offset
        args = []
        for _ in range(count):
            value, offset = self._read_bulk(offset)
            if value is None:
                return None, 0
            args.append(value)
        return args, offset

    def _read_inline(self):
        """Return (arguments, next_offset) for the inline command at the head."""
        line, offset = self._read_line(0)
        if line is None:
            return None, 0
        return _split_inline(line), offset


def _bulk_length(text):
    """Parse the length field of a bulk string header."""
    try:
        length = int(text)
    except ValueError:
        raise ProtocolError("invalid bulk length")
    if length < 0 or length > MAX_BULK_LENGTH:
        raise ProtocolError("invalid bulk length")
    return length


def _array_count(text):
    """Parse the element count of an array header."""
    try:
        return int(text)
    except ValueError:
        raise ProtocolError("invalid multibulk length")


def _split_inline(line):
    """Split an inline command into its arguments."""
    return line.split()


# ---------------------------------------------------------------------------
# Keyspace
# ---------------------------------------------------------------------------

class Store:
    """In-memory keyspace with expiries.

    ``clock`` is a callable returning the current time in seconds; callers
    can inject a clock of their own to move time by hand.
    """

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._values = {}
        self._expires = {}

    def _now(self):
        return self._clock()

    def _is_expired(self, key):
        """True when key carries a deadline that the clock has passed."""
        deadline = self._expires.get(key)
        if deadline is None:
            return False
        return deadline <= self._now()

    def _purge(self, key):
        """Drop key when it has expired, reporting whether it was dropped."""
        if not self._is_expired(key):
            return False
        del self._expires[key]
        del self._values[key]
        return True

    def get(self, key):
        """Return the value stored at key, or None when it is not live."""
        if self._purge(key):
            return None
        return self._values.get(key)

    def exists(self, key):
        """True when key holds a live value."""
        return self.get(key) is not None

    def keys(self):
        """Every live key, sorted."""
        for key in list(self._values):
            self._purge(key)
        return sorted(self._values)

    def set(self, key, value, ttl=None):
        """Store value at key.  ttl is a lifetime in seconds."""
        self._values[key] = value
        if ttl is None:
            self._expires.pop(key, None)
        else:
            self._expires[key] = self._now() + ttl

    def delete(self, *keys):
        """Delete keys, returning how many of them were live."""
        removed = 0
        for key in keys:
            if self.get(key) is None:
                continue
            self._values.pop(key, None)
            self._expires.pop(key, None)
            removed += 1
        return removed

    def expire(self, key, seconds):
        """Give key a lifetime of seconds; False when key does not exist."""
        if self.get(key) is None:
            return False
        self._expires[key] = self._now() + seconds
        return True

    def incr(self, key, amount=1):
        """Add amount to the integer at key, creating it at zero if missing."""
        current = self.get(key)
        if current is None:
            current = "0"
        value = parse_integer(current) + amount
        if not INT64_MIN <= value <= INT64_MAX:
            raise CommandError("ERR increment or decrement would overflow")
        self._values[key] = str(value)
        return value


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def command_set(session, args):
    """SET key value [EX seconds]"""
    key, value = args[1], args[2]
    ttl = None
    index = 3
    while index < len(args):
        if args[index].upper() != "EX":
            raise CommandError("ERR syntax error")
        if index + 1 >= len(args):
            raise CommandError("ERR syntax error")
        seconds = parse_integer(args[index + 1])
        if seconds <= 0:
            raise CommandError("ERR invalid expire time in 'set' command")
        ttl = seconds
        index += 2
    session.store.set(key, value, ttl)
    return encode_simple("OK")


def command_get(session, args):
    """GET key"""
    return encode_bulk(session.store.get(args[1]))


def command_del(session, args):
    """DEL key [key ...]"""
    return encode_integer(session.store.delete(*args[1:]))


def command_expire(session, args):
    """EXPIRE key seconds"""
    seconds = parse_integer(args[2])
    if session.store.expire(args[1], seconds):
        return encode_integer(1)
    return encode_integer(0)


def command_incr(session, args):
    """INCR key"""
    return encode_integer(session.store.incr(args[1]))


_COMMANDS = {
    "SET": command_set,
    "GET": command_get,
    "DEL": command_del,
    "EXPIRE": command_expire,
    "INCR": command_incr,
}

_ARITY = {
    "SET": (3, None),
    "GET": (2, 2),
    "DEL": (2, None),
    "EXPIRE": (3, 3),
    "INCR": (2, 2),
}


class Session:
    """Runs parsed commands against a store and encodes their replies."""

    def __init__(self, store=None, parser=None):
        self.store = store if store is not None else Store()
        self.parser = parser if parser is not None else Parser()

    def feed(self, data):
        """Queue bytes that arrived from the client."""
        self.parser.feed(data)

    def pending(self):
        """Number of queued bytes still waiting to be parsed."""
        return self.parser.pending()

    def read_reply(self):
        """Run the next queued command and return its reply bytes.

        Returns None when the queued bytes do not hold a complete command.
        """
        while True:
            try:
                args = self.parser.read_command()
            except ProtocolError as error:
                return encode_error("ERR Protocol error: %s" % error)
            if args is None:
                return None
            return self.execute(args)

    def execute(self, args):
        """Run one command given as a list of arguments."""
        if not args:
            raise ValueError("a command needs a name")
        name = args[0].upper()
        handler = _COMMANDS.get(name)
        if handler is None:
            return encode_error("ERR unknown command '%s'" % args[0])
        low, high = _ARITY[name]
        if len(args) < low or (high is not None and len(args) > high):
            return encode_error(
                "ERR wrong number of arguments for '%s' command" % name.lower()
            )
        try:
            return handler(self, args)
        except CommandError as error:
            return encode_error(error.message)
