#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
klei_heartbeat.py — 克雷心跳（签到/签礼）自动脚本：不开游戏持续向 Klei Item Server 发 Tick。

原理（来自 Assembly-CSharp-firstpass/KleiItems.cs 反编译 + DST 实测）:
  * ONI 客户端每累积 360 秒真实游玩时间，POST
        https://items.kleientertainment.com/clientitems/ONI/Tick
    body 只有:  {"Token" : "<GameSessionToken>"}        （无时间/秒数字段）
  * 服务端用 Token 识别账户，按 Tick 次数推进「游玩/掉落进度」，可能返回:
        {"Error":false,"GiftReceived":true}             -> 有新礼（进库存，待领）
        {"Error":true,"ErrorCode":"E_UNOPENED_ITEMS"}   -> 仓库有未开礼物，先开礼再收新礼
        {"Error":false,"GiftReceived":false}            -> 无礼、心跳正常
    （注意：ONI 的 GiftReceived 是布尔值，没有 DST 的 {"Key":...} 字段；
     收到礼后用 klei_open_gifts.py / klei_heartbeat_auto.py 领取即可。）
  * GameSessionToken 有效期约 60 分钟，到期前经 /login/TokenPurpose 续期；
    续期失败则整轮重新 LoginViaSteam（Steam 票据）。
  * 主菜单挂机也能累积 Tick（Global.Update() 每帧驱动 KleiItems.Update()），
    脚本则完全独立于游戏进程，直接与克雷通信。

用法:
  python klei_heartbeat.py                      # 登录 -> 每 360s 一次 Tick，无限循环
  python klei_heartbeat.py --once               # 只发一次 Tick（适合定时任务）
  python klei_heartbeat.py --interval 600       # 自定义间隔（秒）
  python klei_heartbeat.py --token file.json    # 复用已保存的登录响应（未过期则跳过重新登录）

安全注意:
  * 别把间隔压太短（协议本意是 360s；高频刷可能触发风控/限流）。
  * Token 只属于当前 Steam 账号（等于你自己登录 ONI），勿外泄。
"""

import argparse
import json
import os
import random
import ssl
import sys
import time
import urllib.request
import urllib.error

import klei_steam_login as K

ITEMS_BASE = "https://items.kleientertainment.com"
TICK_PATH = "/clientitems/ONI/Tick"

DEFAULT_INTERVAL = 360          # 游戏内累加周期（秒），KleiItems.SECONDS_PER_TICK
TOKEN_LIFETIME = 60 * 60        # LoginViaSteam token 时长（实测 ~3605s）
REFRESH_EARLY = 5 * 60          # 到期前 N 秒续期

_UTF8_DONE = False


def _ensure_utf8():
    global _UTF8_DONE
    if _UTF8_DONE:
        return
    for _stream in (sys.stdout, sys.stderr):
        _rc = getattr(_stream, "reconfigure", None)
        if _rc is not None:
            try:
                _rc(encoding="utf-8", errors="replace")
            except Exception:
                pass
    _UTF8_DONE = True


_ensure_utf8()


HERE = os.path.dirname(os.path.abspath(__file__))
HERE = os.path.dirname(os.path.abspath(__file__))
SECRETS_DIR = os.path.join(HERE, "credentials")   # 凭证目录（已被 .gitignore 排除）
os.makedirs(SECRETS_DIR, exist_ok=True)


def http_post_json(base, path, body: dict, timeout=30):
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json",
                 "Accept": "application/json",
                 "User-Agent": K.UA})
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError) as e:
        return 0, ("NETERR: %s" % e)


def tick(token, timeout=30):
    """发一次心跳。返回 (http_status, parsed_json_or_None, raw)。"""
    st, raw = http_post_json(ITEMS_BASE, TICK_PATH, {"Token": token}, timeout)
    try:
        return st, json.loads(raw), raw
    except json.JSONDecodeError:
        return st, None, raw


def has_gift(response):
    """ONI 心跳响应里 GiftReceived 是布尔值：
    True = 有新礼（进库存待领）；False/缺失 = 无礼。"""
    if not isinstance(response, dict):
        return False
    return response.get("GiftReceived") is True


def log(msg, logfile=None):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    if logfile:
        try:
            d = os.path.dirname(logfile)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(logfile, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def full_login(dll_path, desc):
    """整轮登录：取 Steam 票据 -> LoginViaSteam -> 返回 (token, userid, 响应JSON, steamid)。"""
    bridge = K.SteamAPIBridge(dll_path)
    try:
        ticket_hex, steamid = bridge.get_ticket()
        log("Steam 票据 %d 字节  SteamID=%s" % (len(ticket_hex) // 2, steamid or "?"))
        st, raw = K.login(ticket_hex, desc)
        j = json.loads(raw) if raw.lstrip().startswith(("{", "[")) else None
        if j is None or j.get("Error"):
            raise RuntimeError("LoginViaSteam 失败 HTTP=%s %s" % (st, raw[:300]))
        tok = j.get("Token") or ""
        if not tok:
            raise RuntimeError("响应无 Token: %s" % raw[:300])
        return tok, j.get("UserID", "?"), j, steamid
    finally:
        bridge.cancel_ticket()
        bridge.shutdown()


def refresh_or_relogin(token, dll_path, desc):
    """优先 TokenPurpose 续期；失败则整轮重登。
    返回 (新token, userid, source, 最新响应JSON)。"""
    st, raw = K.refresh_token(token)
    try:
        j = json.loads(raw)
    except json.JSONDecodeError:
        j = None
    if j and not j.get("Error") and j.get("Token"):
        return j["Token"], j.get("UserID", "?"), "refresh", j
    log("续期失败 (%s %s)，整轮重新登录 ..." % (st, raw[:120]))
    tok, uid, j2, _ = full_login(dll_path, desc)
    return tok, uid, "relogin", j2


def main():
    ap = argparse.ArgumentParser(description="克雷心跳：自动 Tick 签到（拿游玩礼物）")
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                    help="Tick 间隔秒（默认 %g，等于游戏内 6 分钟）" % DEFAULT_INTERVAL)
    ap.add_argument("--once", action="store_true", help="只发一次 Tick 就退出")
    ap.add_argument("--dll", default=None, help="steam_api64.dll 路径（默认自动定位 ONI 安装）")
    ap.add_argument("--desc", default="standalone_heartbeat", help="FriendlyHostDescriptor")
    ap.add_argument("--token", metavar="FILE", default=None,
                    help="登录响应 JSON 文件（含 Token），未过期则直接复用；--save 会写它")
    ap.add_argument("--save", metavar="FILE", default=None,
                    help="把每次(重新)登录响应保存到该文件，供 --token 复用")
    ap.add_argument("--log", metavar="FILE", default=os.path.join(SECRETS_DIR, "klei_heartbeat.log"),
                    help="日志文件（默认 credentials/klei_heartbeat.log，传空串关闭）")
    args = ap.parse_args()

    if args.interval < 30:
        log("[!] 间隔过短会被服务器当 flooding，强制 30s")
        args.interval = 30
    logfile = args.log or None

    # ---- 0. 取得 token ----
    token, userid, j, src = None, None, None, None
    if args.token and os.path.exists(args.token):
        try:
            j = json.load(open(args.token, encoding="utf-8"))
            token = j.get("Token") or j.get("token")
            if token:
                userid = j.get("UserID") or "?"
                src = "file:" + args.token
        except Exception:
            pass
    if not token:
        token, userid, j, steamid = full_login(args.dll, args.desc)
        src = "login"
        if args.save:
            try:
                open(args.save, "w", encoding="utf-8").write(json.dumps(j))
            except OSError:
                pass
    log("Token 就绪 [%s] UserID=%s" % (src, userid))

    # ---- 1. 心跳循环 ----
    born = time.time()
    n = 0
    while True:
        n += 1
        age = time.time() - born
        # 到期前自动续期/重登
        if age >= TOKEN_LIFETIME - REFRESH_EARLY:
            log("Token 快到期（已用 %.0f 分钟），续期 ..." % (age / 60))
            token, userid, src, resp = refresh_or_relogin(token, args.dll, args.desc)
            j = resp
            born = time.time()
            age = 0
            if args.save:
                try:
                    open(args.save, "w", encoding="utf-8").write(json.dumps(resp))
                except OSError:
                    pass

        st, jj, raw = tick(token)
        if st == 0:                                   # 网络错误
            log("Tick 网络错误: %s（%d 秒后重试）" % (raw[:120], args.interval))
        else:
            # 业务错误码都是 HTTP 200；登录态失效才是 E_INVALID_TOKEN
            ec = jj.get("ErrorCode") if isinstance(jj, dict) else None
            if ec == "E_INVALID_TOKEN":
                log("Token 已失效，续期/重登 ...")
                token, userid, src, resp = refresh_or_relogin(token, args.dll, args.desc)
                j = resp
                born = time.time()
                if args.save:
                    try:
                        open(args.save, "w", encoding="utf-8").write(json.dumps(resp))
                    except OSError:
                        pass
            else:
                if has_gift(jj):
                    log("★ 收到新礼物！响应: %s（用 klei_open_gifts.py / "
                        "klei_heartbeat_auto.py 领取）" % raw[:200])
                    with open(os.path.join(SECRETS_DIR, "klei_gifts.log"), "a",
                              encoding="utf-8") as gf:
                        gf.write("%s %s gift\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                                   userid))
                elif ec:
                    log("Tick[%d] HTTP %d  code=%s  （该状态正常不构成错误）" % (n, st, ec))
                    if ec == "E_UNOPENED_ITEMS":
                        log("  提示：账号有未开的礼物，先开礼（klei_open_gifts.py），"
                            "之后心跳才能继续收新礼")
                elif not isinstance(jj, dict):
                    log("Tick[%d] HTTP %d 响应格式异常: %s" % (n, st, raw[:200]))
                else:
                    log("Tick[%d] HTTP %d 无礼物" % (n, st))
        if args.once:
            log("--once 已发送，退出")
            return 0
        time.sleep(args.interval + random.uniform(-5, 5))   # 微量抖动，贴近人类节奏


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[!] 退出")