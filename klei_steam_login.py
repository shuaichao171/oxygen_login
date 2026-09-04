#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
klei_steam_login.py — 不开游戏登录 Klei（模拟 ONI 客户端 ① 账号登录流程）

原理（来自 Assembly-CSharp-firstpass/KleiAccount.cs 反编译 + DST 实测）:
  1. SteamUser()->GetAuthSessionTicket(buf, 0x800, &len)        得到 Steam 会话票据
  2. 票据 -> 每字节大写 HEX（KleiAccount.EncodeToAsciiHEX 用 "%02X" 逐个字节转）
  3. POST https://login.kleientertainment.com/login/LoginViaSteam
     body: {"SteamTicket":<HEX>, "Game":"ONI",
            "FriendlyHostDescriptor":<desc>, "NoEmail":true}
     （KleiAccount.BuildLoginRequest: SteamTicket / Game=CLIENT_KEY("ONI") / NoEmail:true）
  4. 响应 JSON 取 Token(GameSessionToken) / UserID(KU_/OU_) / Username / AnalyticsJWT

前置条件:
  - Steam 客户端已运行并登录（不必开 ONI）
  - 脚本会自动定位 ONI 的 steam_api64.dll:
        <游戏根目录>/OxygenNotIncluded_Data/Plugins/x86_64/steam_api64.dll
    （或通过 --dll 指定）
  - appid 457140 通过 steam_appid.txt 声明（脚本自动写、结束删除）

用法:
  python klei_steam_login.py            # 登录并打印凭证
  python klei_steam_login.py --json out  # 把响应原样存到 out.json
  python klei_steam_login.py --desc "mypc"   # 自定义 FriendlyHostDescriptor
"""

import argparse
import ctypes
import ctypes.wintypes as w
import json
import os
import ssl
import sys
import tempfile
import urllib.request
import urllib.error

APPID = "457140"                      # Oxygen Not Included (Steam)
BASE = "https://login.kleientertainment.com"
ENDPOINT = BASE + "/login/LoginViaSteam"
GAME = "ONI"                          # 游戏代码（KleiAccount.CLIENT_KEY，服务端识别用）
# 游戏内 UA：ONI 走 .NET HttpRequestMessage（默认 UA），服务端不校验；这里给个可辨识值
UA = "OxygenNotIncluded"
TICKET_SIZE = 0x800


def _ensure_utf8():
    """GBK 控制台下兜底：stdout/stderr 强制 UTF-8，emoji/中文不再报 UnicodeEncodeError。"""
    for _stream in (sys.stdout, sys.stderr):
        _rc = getattr(_stream, "reconfigure", None)
        if _rc is not None:
            try:
                _rc(encoding="utf-8", errors="replace")
            except Exception:
                pass


_ensure_utf8()


def default_steam_dll():
    """自动定位 ONI 自带的 steam_api64.dll（向上查找游戏根目录布局）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    # 常见布局: <game root>/OxygenNotIncluded_Data/Plugins/x86_64/steam_api64.dll
    for _ in range(6):
        cand = os.path.join(here, "OxygenNotIncluded_Data", "Plugins", "x86_64",
                            "steam_api64.dll")
        if os.path.exists(cand):
            return cand
        cand = os.path.join(here, "steam_api64.dll")
        if os.path.exists(cand):
            return cand
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    return None


# ---------------------------------------------------------------- steam_api 调用
class SteamAPIBridge:
    """ctypes 调用 steam_api64.dll 的 flat API 拿到 Steam 会话票据。"""

    def __init__(self, dll_path=None):
        if dll_path is None:
            dll_path = default_steam_dll()
        if not dll_path or not os.path.exists(dll_path):
            raise FileNotFoundError(
                "找不到 steam_api64.dll: %s（可用 --dll 指定 ONI 的 "
                "OxygenNotIncluded_Data/Plugins/x86_64/steam_api64.dll）" % dll_path)
        self.dll = ctypes.WinDLL(dll_path)
        self.pipe_ok = False
        self.user = None
        self.hTicket = 0
        self._resolve()

    def _resolve(self):
        d = self.dll
        # ---- 初始化类型签名 ----
        d.SteamAPI_RestartAppIfNecessary.restype = w.BOOL
        d.SteamAPI_RestartAppIfNecessary.argtypes = [w.UINT]
        d.SteamAPI_Init.restype = w.BOOL
        d.SteamAPI_Init.argtypes = []
        try:
            d.SteamAPI_InitEx.argtypes = [ctypes.POINTER(ctypes.c_int)]
        except AttributeError:
            pass
        d.SteamAPI_Shutdown.argtypes = []
        getuser = getattr(d, "SteamAPI_SteamUser_v021", getattr(d, "SteamAPI_SteamUser", None))
        if getuser is None:
            raise RuntimeError("DLL 中没有 SteamAPI_SteamUser 导出")
        getuser.restype = ctypes.c_void_p
        getuser.argtypes = []
        self._getuser = getuser
        # ISteamUser::GetSteamID -> uint64（打印用）
        if hasattr(d, "SteamAPI_ISteamUser_GetSteamID"):
            d.SteamAPI_ISteamUser_GetSteamID.restype = ctypes.c_uint64
            d.SteamAPI_ISteamUser_GetSteamID.argtypes = [ctypes.c_void_p]
            self._getsteamid = d.SteamAPI_ISteamUser_GetSteamID
        else:
            self._getsteamid = None
        d.SteamAPI_ISteamUser_GetAuthSessionTicket.restype = w.UINT
        d.SteamAPI_ISteamUser_GetAuthSessionTicket.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(w.UINT)]
        if hasattr(d, "SteamAPI_ISteamUser_CancelAuthTicket"):
            d.SteamAPI_ISteamUser_CancelAuthTicket.argtypes = [ctypes.c_void_p, w.UINT]

    # -- 需要一个能说明"我是 457140"的 steam_appid.txt；Steam 以此识别 appid --
    def _ensure_appid(self, workdir):
        p = os.path.join(workdir, "steam_appid.txt")
        if os.path.exists(p):
            with open(p) as f:
                if f.read().strip().isdigit():
                    return None  # 已有合法 appid，不动它
        with open(p, "w") as f:
            f.write(APPID)
        return p

    def get_ticket(self):
        """初始化 steamapi 并请求会话票据；返回 (hex_ticket, steamid_str)。"""
        # 关键：Steam 按当前工作目录下的 steam_appid.txt 认定 appid
        old = os.getcwd()
        tmpappid = None
        try:
            os.chdir(os.path.dirname(os.path.abspath(__file__)))
            tmpappid = self._ensure_appid(os.getcwd())
            if self.dll.SteamAPI_RestartAppIfNecessary(int(APPID)):
                raise RuntimeError("需要先以 appid=%s 通过 Steam 启动进程" % APPID)
            if not self.dll.SteamAPI_Init():
                raise RuntimeError("SteamAPI_Init 失败：Steam 客户端未运行或未登录？")
            self.pipe_ok = True
            self.user = self._getuser()
            if not self.user:
                raise RuntimeError("SteamUser() 返回空")

            buf = ctypes.create_string_buffer(TICKET_SIZE)
            nlen = w.UINT(0)
            self.hTicket = self.dll.SteamAPI_ISteamUser_GetAuthSessionTicket(
                self.user, buf, TICKET_SIZE, ctypes.byref(nlen))
            if self.hTicket == 0 or nlen.value == 0:
                raise RuntimeError("GetAuthSessionTicket 失败 (handle=%#x len=%d)"
                                   % (self.hTicket, nlen.value))
            raw = bytes(buf.raw[:nlen.value])
            steamid = None
            if self._getsteamid:
                steamid = self._getsteamid(self.user)
            return raw.hex().upper(), steamid
        finally:
            if tmpappid and os.path.exists(tmpappid):
                try:
                    os.remove(tmpappid)
                except OSError:
                    pass
            os.chdir(old)

    def cancel_ticket(self):
        try:
            if self.pipe_ok and self.user and self.hTicket and \
                    hasattr(self.dll, "SteamAPI_ISteamUser_CancelAuthTicket"):
                self.dll.SteamAPI_ISteamUser_CancelAuthTicket(self.user, self.hTicket)
        except Exception:
            pass

    def shutdown(self):
        try:
            if self.pipe_ok and hasattr(self.dll, "SteamAPI_Shutdown"):
                self.dll.SteamAPI_Shutdown()
        except Exception:
            pass


# ---------------------------------------------------------------- 续期
def refresh_token(token, timeout=30):
    """用现有 GameSessionToken 调 /login/TokenPurpose 换取新的（对 ONI 与 DST 同为
    login server 的标准续期动作，body {"Token": <当前token>}）。成功每次可再续 ~60 分钟。"""
    body = {"Token": token}
    req = urllib.request.Request(
        BASE + "/login/TokenPurpose", data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json",
                 "Accept": "application/json",
                 "User-Agent": UA})
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
            status, raw = resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read().decode("utf-8", errors="replace")
    return status, raw


# ---------------------------------------------------------------- HTTP
def login(ticket_hex, desc, token_expiry=None):
    body = {
        "SteamTicket": ticket_hex,
        "Game": GAME,
        "FriendlyHostDescriptor": desc,
        "NoEmail": True,
    }
    if token_expiry is not None:   # 游戏内该字段为 0 时整键省略
        body["TokenExpiry"] = token_expiry
    data = json.dumps(body).encode("utf-8")
    # 与游戏一致：仅标准头（Content-Type/Accept/UA）
    req = urllib.request.Request(
        ENDPOINT, data=data, method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": UA,
        })
    ctx = ssl.create_default_context()  # 系统 CA 链，与游戏一致（无证书固定）
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            status = resp.status
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        status = e.code
        raw = e.read().decode("utf-8", errors="replace")
    return status, raw


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="不开游戏登录 Klei（模拟 ONI Steam 登录）")
    ap.add_argument("--dll", default=None, help="steam_api64.dll 路径（默认自动定位 ONI 安装）")
    ap.add_argument("--desc", default="standalone_python_login", help="FriendlyHostDescriptor")
    ap.add_argument("--json", metavar="OUT", default=None, help="把 HTTP 响应原样保存到文件")
    ap.add_argument("--tokenexpiry", type=int, default=None,
                    help="带上 TokenExpiry=<秒数> 字段（游戏默认省略）")
    ap.add_argument("--refresh", metavar="TOKEN", default=None,
                    help="不重新登录，直接用现有 GameSessionToken 走 /login/TokenPurpose 续期")
    args = ap.parse_args()

    if args.refresh:
        print("[*] 续期模式：TokenPurpose with %s..." % args.refresh[:24])
        st, raw = refresh_token(args.refresh)
        print("[*] HTTP %d" % st, raw[:300])
        return

    print("[*] 1/3 请求 Steam 会话票据 ...")
    bridge = SteamAPIBridge(args.dll)
    try:
        ticket_hex, steamid = bridge.get_ticket()
        print("[*]    票据长度: %d 字节 (hex %d 字符)  SteamID: %s"
              % (len(ticket_hex) // 2, len(ticket_hex), steamid or "?"))

        print("[*] 2/3 POST %s" % ENDPOINT)
        status, raw = login(ticket_hex, args.desc, args.tokenexpiry)

        print("[*] 3/3 HTTP %d" % status)
        if args.json:
            with open(args.json, "w", encoding="utf-8") as f:
                f.write(raw)
            print("[*]    响应原文 -> %s" % args.json)

        try:
            j = json.loads(raw)
        except json.JSONDecodeError:
            j = None

        if j is not None:
            tok = j.get("Token") or j.get("token")
            uid = j.get("UserID") or j.get("userid")
            print("=" * 60)
            print("登录成功！Klei 帐号凭证：")
            if uid:    print("  UserID        : %s" % uid)
            if tok:    print("  GameSessionToken: %s" % tok)
            if j.get("Username"):  print("  Username      : %s" % j["Username"])
            if j.get("AnalyticsJWT"): print("  AnalyticsJWT  : %s" % j["AnalyticsJWT"])
            if j.get("ErrorCode") is not None:
                print("  接口返回错误: code=%s  %s" % (j["ErrorCode"], j.get("ErrorMessage", "")))
            print("=" * 60)
            if tok:
                print("[*] 该 Token 即 GameSessionToken；ONI 内 KleiAccount.KleiToken")
                print("    用它走 items.kleientertainment.com 心跳/领礼。")
        else:
            print("[!] 非 JSON 响应（HTTP %d）：" % status)
            print(raw[:2000])
    finally:
        bridge.cancel_ticket()
        bridge.shutdown()


if __name__ == "__main__":
    main()