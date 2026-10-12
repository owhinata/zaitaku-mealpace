// rpc_server.h — Linux 側（arduino-router）からの RPC の受け口（Issue #38）。
// Arduino_RouterBridge を include するのは rpc_server.cpp だけ
// （CLAUDE.md「実装方針」、docs/decisions/0025）。この見出しは引数・戻り値に
// Arduino と MsgPack の型を出さない。
#pragma once

// Bridge を始め、zm_set_led(bool) を登録する。led_init() の後に呼ぶ。
void rpc_begin();
