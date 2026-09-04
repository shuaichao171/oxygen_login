#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
klei_heartbeat_auto.py — 双击运行：黑窗口实时日志 + 每 360 秒心跳 + 有礼物自动领取。

工作流（KleiItems.cs 反编译 + DST 实测，2026-09-05）:
  每 360s（KleiItems.SECONDS_PER_TICK）
    ├─ POST items.kleientertainment.com/clientitems/ONI/Tick  {"Token": <token>}
    │     → 服务端按心跳推进游玩礼物掉落
    │     → 有礼: {"Error":false,"GiftReceived":true}    （注意是布尔，无 DST 的 Key）
    └─ 有礼 → 领取（2 步，对应 ONI 客户端）:
          1) POST clientitems/ONI/GetAllItems
               {"ClientToken":..}  → 找 Context∈{3,4} 的未开礼物
          2) 循环 POST clientitems/ONI/SetItemOpened
               {"ItemID":..,"ClientToken":..}            （KleiItems.RequestItemOpened）
             Context→1，逐个揭示直至队列清空（Tick 门控 E_UNOPENED_ITEMS 解除）
  （ONI 无 DST 的 VerifyGiftingReceipt 回执步骤。）
  领取后立即再 Tick 一次，榨干可能堆叠的礼物（最多连领 5 次）。

Token 管理: LoginViaSteam 一次给 ~60min；到期前 5 分钟 TokenPurpose 续期，
           续期失败整轮重登（Steam 票据）。首次运行会弹 Steam 客户端登录。

用法:
  直接双击「心跳自动领取.bat」　（或）
  python klei_heartbeat_auto.py [--interval 360] [--once] [--no-logfile]
"""

import argparse
import json
import os
import sys
import time

import klei_heartbeat as HB
import klei_steam_login as K
import klei_open_gifts as G

MAX_DRAIN = 5        # 一次领礼后最多连续再查/再领次数
HERE = os.path.dirname(os.path.abspath(__file__))
SECRETS_DIR = os.path.join(HERE, "credentials")   # 凭证目录（已被 .gitignore 排除）
os.makedirs(SECRETS_DIR, exist_ok=True)
SAVE_FILE = os.path.join(SECRETS_DIR, ".hb_auto_session.json")


def open_pending_gifts(token, logfile):
    """Open every currently queued unopened gift (Context 3/4) and return the count."""
    opened = 0
    for _ in range(G.MAX_CHAIN):
        _st, items, raw = G.get_items(token)
        if items is None:
            HB.log("GetAllItems 失败: %s" % raw[:120], logfile)
            break
        pending = [item for item in items if G.is_unopened(item)]
        if not pending:
            break
        item = pending[0]
        try:
            item_id = int(item["ItemID"])
            item_type = item["ItemType"]
        except (KeyError, TypeError, ValueError):
            HB.log("未开礼物字段异常，停止处理: %s" % str(item)[:120], logfile)
            break
        st, raw = HB.http_post_json(
            G.ITEMS_BASE, G.PATH_OPENED,
            {"ItemID": item_id, "ClientToken": token})
        try:
            response = json.loads(raw)
        except json.JSONDecodeError:
            response = None
        if not isinstance(response, dict) or response.get("Error"):
            HB.log("SetItemOpened 失败(HTTP=%s) %s" % (st, raw[:120]), logfile)
            break
        HB.log("★ 领取礼物 %s (%d)" % (item_type, item_id), logfile)
        opened += 1
        time.sleep(0.35)
    HB.log("本轮共领取 %d 个礼物" % opened, logfile)
    return opened


def main():
    ap = argparse.ArgumentParser(description="心跳+自动领取（360s 一轮）")
    ap.add_argument("--interval", type=int, default=HB.DEFAULT_INTERVAL,
                    help="心跳间隔秒（默认 360）")
    ap.add_argument("--once", action="store_true", help="只跑一轮后退出")
    ap.add_argument("--no-logfile", action="store_true", help="不写日志文件只打屏幕")
    ap.add_argument("--dll", default=None, help="steam_api64.dll 路径")
    ap.add_argument("--desc", default="standalone_hb_auto", help="FriendlyHostDescriptor")
    args = ap.parse_args()
    interval = max(30, args.interval)
    logfile = None if args.no_logfile else os.path.join(SECRETS_DIR, "hb_auto_console.log")

    HB.log("══════ Klei 心跳+自动领取 开始 ══════")
    HB.log("间隔=%ds  日志=%s" % (interval, logfile or "仅屏幕"), logfile)

    # ---- 登录（优先复用/续期）----
    token, userid = None, "?"
    if os.path.exists(SAVE_FILE):
        try:
            token = json.load(open(SAVE_FILE, encoding="utf-8")).get("Token")
        except Exception:
            token = None
        if token:
            HB.log("复用会话 %s" % SAVE_FILE, logfile)
    if not token:
        token, userid, j, _st = HB.full_login(args.dll, args.desc)
        HB.log("登录完成 UserID=%s" % userid, logfile)
        json.dump(j, open(SAVE_FILE, "w", encoding="utf-8"))

    born = time.time()
    fam = 1                       # 第几轮心跳
    while True:
        since = int(time.time() - born)
        HB.log("── 第 %d 轮心跳（登录后 %s）──" % (fam, time.strftime("%H:%M:%S", time.gmtime(since))))

        # ---- 心跳 ----
        st, j, raw = HB.tick(token)
        err = j.get("ErrorCode") if isinstance(j, dict) else ""
        if st == 401 or err == "E_INVALID_TOKEN":
            HB.log("心跳鉴权失效 %s → 续期/重登" % raw[:140], logfile)
            token, userid, src, jj = HB.refresh_or_relogin(token, args.dll, args.desc)
            HB.log("已%s（%s）" % ("续期" if src == "refresh" else "重登", userid), logfile)
            json.dump(jj, open(SAVE_FILE, "w", encoding="utf-8"))
            continue
        if isinstance(j, dict) and j.get("Error"):
            if err == "E_UNOPENED_ITEMS":
                HB.log("检测到未开礼物，自动清理队列", logfile)
                open_pending_gifts(token, logfile)
            else:
                HB.log("心跳业务错误 HTTP=%s %s" % (st, raw[:200]), logfile)
        else:
            if HB.has_gift(j):
                HB.log("🎁 心跳报告收到礼物，开始领取", logfile)
                open_pending_gifts(token, logfile)
                # 榨干堆叠礼物：立即再查
                for _ in range(MAX_DRAIN):
                    _st2, j2, raw2 = HB.tick(token)
                    if HB.has_gift(j2):
                        HB.log("🎁 还有礼物，继续领取", logfile)
                        open_pending_gifts(token, logfile)
                        time.sleep(1)
                    else:
                        ec2 = j2.get("ErrorCode") if isinstance(j2, dict) else None
                        if ec2 == "E_UNOPENED_ITEMS":
                            HB.log("后续 Tick 报未开门控，再清一次队列", logfile)
                            open_pending_gifts(token, logfile)
                        break
            elif isinstance(j, dict) and j.get("GiftReceived") not in (None, False):
                HB.log("心跳 GiftReceived 格式异常，已忽略：%s" % raw[:200], logfile)
            else:
                HB.log("心跳正常，本轮无礼物（GiftReceived=%s）" % bool(j and j.get("GiftReceived")), logfile)

        # ---- token 快到期续期 ----
        age = time.time() - born
        if age >= HB.TOKEN_LIFETIME - HB.REFRESH_EARLY:
            HB.log("Token 将到期，续期中…", logfile)
            token, userid, src, jj = HB.refresh_or_relogin(token, args.dll, args.desc)
            HB.log("已%s（%s），重新计时" % ("续期" if src == "refresh" else "重登", userid), logfile)
            json.dump(jj, open(SAVE_FILE, "w", encoding="utf-8"))
            born = time.time()

        if args.once:
            HB.log("--once 完成，退出", logfile)
            return 0
        fam += 1
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            HB.log("用户中断", logfile)
            return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[!] 已退出")