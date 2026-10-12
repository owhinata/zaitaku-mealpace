"""UNO Q の Linux 側から MCU を呼ぶ RPC（Issue #38、docs/decisions/0035）。

arduino-router（systemd のサービス）の UNIX ソケットに MessagePack-RPC
（rpclib と同じ形。要求 [0, msgid, method, params]、応答
[1, msgid, error, result]）で話す。MCU 側は firmware/unoq_mcu/rpc_server.cpp
が Bridge.provide で名前を登録している。

このプロジェクトで router のソケットと MessagePack を扱うのはこのモジュール
だけ（CLAUDE.md「実装方針」、0025）。外に出すのは McuLink.set_led(bool) だけで、
引数・戻り値に MessagePack の型を出さない。

MessagePack の符号化・復号は使う型だけ（nil・bool・整数・str・bin・array・
map）をここに書く（UNO Q に pip と python3-msgpack が無い。R0、10/13）。
標準ライブラリだけ。音声は扱わない。ファイルは書かない。
"""

import socket
import struct
import time

# router のソケット（R0、10/13: /var/run/arduino-router.sock、srw-rw-rw-）
SOCKET_PATH = "/var/run/arduino-router.sock"
# MCU 側の呼び出し名（rpc_server.cpp の RPC_SET_LED と同じ）
_METHOD_SET_LED = "zm_set_led"

_REQUEST = 0
_RESPONSE = 1
_RECV_CHUNK = 4096


class McuRpcError(Exception):
    """router または MCU が誤りを返した、または応答の形が違う。"""


class McuRpcTimeout(McuRpcError):
    """timeout_s の間に同じ msgid の応答が来なかった。"""


class _NeedMore(Exception):
    """復号の途中でバイト列が足りない。"""


# ---- MessagePack（使う型だけ）---------------------------------------------


def _pack_int(v, out):
    if v >= 0:
        if v < 0x80:
            out.append(v)
        elif v < 0x100:
            out += b"\xcc" + struct.pack(">B", v)
        elif v < 0x10000:
            out += b"\xcd" + struct.pack(">H", v)
        elif v < 0x100000000:
            out += b"\xce" + struct.pack(">I", v)
        elif v < 0x10000000000000000:
            out += b"\xcf" + struct.pack(">Q", v)
        else:
            raise ValueError("整数が大きすぎる")
    else:
        if v >= -32:
            out += struct.pack(">b", v)
        elif v >= -0x80:
            out += b"\xd0" + struct.pack(">b", v)
        elif v >= -0x8000:
            out += b"\xd1" + struct.pack(">h", v)
        elif v >= -0x80000000:
            out += b"\xd2" + struct.pack(">i", v)
        elif v >= -0x8000000000000000:
            out += b"\xd3" + struct.pack(">q", v)
        else:
            raise ValueError("整数が小さすぎる")


def _pack_len(n, fix_base, fix_max, codes, out):
    """長さの見出し。codes は (8 ビット, 16 ビット, 32 ビット) の型の番号。"""
    if fix_base is not None and n <= fix_max:
        out.append(fix_base | n)
    elif codes[0] is not None and n < 0x100:
        out += bytes([codes[0]]) + struct.pack(">B", n)
    elif n < 0x10000:
        out += bytes([codes[1]]) + struct.pack(">H", n)
    elif n < 0x100000000:
        out += bytes([codes[2]]) + struct.pack(">I", n)
    else:
        raise ValueError("長すぎる")


def _pack(obj, out):
    if obj is None:
        out.append(0xC0)
    elif obj is True:
        out.append(0xC3)
    elif obj is False:
        out.append(0xC2)
    elif isinstance(obj, int):
        _pack_int(obj, out)
    elif isinstance(obj, str):
        b = obj.encode("utf-8")
        _pack_len(len(b), 0xA0, 31, (0xD9, 0xDA, 0xDB), out)
        out += b
    elif isinstance(obj, (bytes, bytearray)):
        _pack_len(len(obj), None, -1, (0xC4, 0xC5, 0xC6), out)
        out += obj
    elif isinstance(obj, (list, tuple)):
        _pack_len(len(obj), 0x90, 15, (None, 0xDC, 0xDD), out)
        for x in obj:
            _pack(x, out)
    elif isinstance(obj, dict):
        _pack_len(len(obj), 0x80, 15, (None, 0xDE, 0xDF), out)
        for k, x in obj.items():
            _pack(k, out)
            _pack(x, out)
    else:
        raise TypeError(f"符号化できない型: {type(obj).__name__}")


def packb(obj):
    """obj を MessagePack のバイト列にする（使う型だけ）。"""
    out = bytearray()
    _pack(obj, out)
    return bytes(out)


def _take(buf, pos, n):
    end = pos + n
    if end > len(buf):
        raise _NeedMore
    return buf[pos:end], end


def _unpack_n(buf, pos, n, fmt):
    raw, pos = _take(buf, pos, n)
    return struct.unpack(fmt, raw)[0], pos


def _unpack(buf, pos):
    """buf[pos:] から 1 つ復号し (値, 次の位置) を返す。足りなければ _NeedMore。"""
    if pos >= len(buf):
        raise _NeedMore
    c = buf[pos]
    pos += 1
    if c <= 0x7F:
        return c, pos
    if c >= 0xE0:
        return c - 0x100, pos
    if 0xA0 <= c <= 0xBF:
        return _str(buf, pos, c & 0x1F)
    if 0x90 <= c <= 0x9F:
        return _array(buf, pos, c & 0x0F)
    if 0x80 <= c <= 0x8F:
        return _map(buf, pos, c & 0x0F)
    if c == 0xC0:
        return None, pos
    if c == 0xC2:
        return False, pos
    if c == 0xC3:
        return True, pos
    ints = {
        0xCC: (1, ">B"),
        0xCD: (2, ">H"),
        0xCE: (4, ">I"),
        0xCF: (8, ">Q"),
        0xD0: (1, ">b"),
        0xD1: (2, ">h"),
        0xD2: (4, ">i"),
        0xD3: (8, ">q"),
    }
    if c in ints:
        return _unpack_n(buf, pos, *ints[c])
    lens = {1: ">B", 2: ">H", 4: ">I"}
    sized = {
        0xD9: (1, "str"),
        0xDA: (2, "str"),
        0xDB: (4, "str"),
        0xC4: (1, "bin"),
        0xC5: (2, "bin"),
        0xC6: (4, "bin"),
        0xDC: (2, "array"),
        0xDD: (4, "array"),
        0xDE: (2, "map"),
        0xDF: (4, "map"),
    }
    if c in sized:
        width, kind = sized[c]
        n, pos = _unpack_n(buf, pos, width, lens[width])
        if kind == "str":
            return _str(buf, pos, n)
        if kind == "bin":
            raw, pos = _take(buf, pos, n)
            return bytes(raw), pos
        if kind == "array":
            return _array(buf, pos, n)
        return _map(buf, pos, n)
    raise McuRpcError(f"復号できない MessagePack の型: 0x{c:02x}")


def _str(buf, pos, n):
    raw, pos = _take(buf, pos, n)
    return bytes(raw).decode("utf-8"), pos


def _array(buf, pos, n):
    items = []
    for _ in range(n):
        x, pos = _unpack(buf, pos)
        items.append(x)
    return items, pos


def _map(buf, pos, n):
    d = {}
    for _ in range(n):
        k, pos = _unpack(buf, pos)
        x, pos = _unpack(buf, pos)
        d[k] = x
    return d, pos


def unpackb(data):
    """バイト列 1 つ分の MessagePack を復号する（使う型だけ）。"""
    try:
        obj, pos = _unpack(data, 0)
    except _NeedMore:
        raise ValueError("MessagePack のバイト列が途中で切れている") from None
    if pos != len(data):
        raise ValueError("MessagePack のバイト列の後ろに余りがある")
    return obj


# ---- router との接続 ------------------------------------------------------


class _SocketTransport:
    """UNIX ソケットを McuLink の transport（send・recv・settimeout）にする。"""

    def __init__(self, sock):
        self._sock = sock

    def send(self, data):
        self._sock.sendall(data)

    def recv(self, n):
        return self._sock.recv(n)

    def settimeout(self, t):
        self._sock.settimeout(t)

    def close(self):
        self._sock.close()


class McuLink:
    """MCU への RPC。公開するのは set_led だけ。

    transport は send(bytes)・recv(n) -> bytes（閉じたら b""）を持つもの。
    settimeout(秒) があれば、応答を待つ残り時間をそのつど渡す。
    """

    def __init__(self, transport, timeout_s=1.0):
        self._t = transport
        self._timeout_s = timeout_s
        self._msgid = 0
        self._buf = bytearray()

    @classmethod
    def open_default(cls, path=SOCKET_PATH, timeout_s=1.0):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(timeout_s)
            sock.connect(path)
        except BaseException:
            sock.close()
            raise
        return cls(_SocketTransport(sock), timeout_s=timeout_s)

    def close(self):
        close = getattr(self._t, "close", None)
        if close is not None:
            close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def set_led(self, on):
        """MCU の LED を点ける（True）か消す（False）。応答を待って戻る。"""
        result = self._call(_METHOD_SET_LED, [bool(on)])
        if result is not True:
            raise McuRpcError(f"zm_set_led の応答が true でない: {result!r}")

    def _call(self, method, params):
        msgid = self._msgid
        self._msgid = (self._msgid + 1) & 0xFFFFFFFF
        self._t.send(packb([_REQUEST, msgid, method, params]))
        deadline = time.monotonic() + self._timeout_s
        while True:
            msg = self._next_message(deadline)
            if (
                isinstance(msg, list)
                and len(msg) == 4
                and msg[0] == _RESPONSE
                and msg[1] == msgid
            ):
                if msg[2] is not None:
                    raise McuRpcError(f"{method} の誤り: {msg[2]!r}")
                return msg[3]
            # 別の msgid の応答（前の時間切れの残り）や通知は読み飛ばす

    def _next_message(self, deadline):
        while True:
            try:
                obj, pos = _unpack(self._buf, 0)
            except _NeedMore:
                pass
            else:
                del self._buf[:pos]
                return obj
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise McuRpcTimeout("router の応答が時間内に来なかった")
            settimeout = getattr(self._t, "settimeout", None)
            if settimeout is not None:
                settimeout(remaining)
            try:
                chunk = self._t.recv(_RECV_CHUNK)
            except TimeoutError:
                raise McuRpcTimeout(
                    "router の応答が時間内に来なかった"
                ) from None
            if not chunk:
                raise McuRpcError("router のソケットが閉じた")
            self._buf += chunk
