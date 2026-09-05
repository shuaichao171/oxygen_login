# oxygen_login (ONI) — 不开游戏登缺氧 + 挂机心跳领游玩礼物

> **目标项目**：`OxygenNotIncluded.exe`（缺氧，Unity 6.0.35f2 Mono 版）+ Klei 服务端
> 通过**反编译托管侧源码**（`Assembly-CSharp-firstpass/KleiAccount.cs`、`KleiItems.cs`、
> `KleiItemsConfig.cs`、`PermitItems.cs`）还原「登录 Klei 账号」和「心跳获取游玩礼物」
> 两条链路，做成**可独立运行、无需启动游戏**的脚本。
> 与饥荒联机版（DST）`klei_login` 同源：DST 实例验证过整套协议，ONI 按源码逐字段移植。
> ⚠ 说明：ONI 侧协议字段严格按反编译源码填写；尚未在真实账号上跑通 Tick（见 §7）。

---

## 1. 快速开始

```bat
cd oxygen_login
:: 方式一：常驻黑窗口（推荐，登录一次后挂机）
双击「心跳自动领取.bat」

:: 方式二：命令行
python klei_heartbeat_auto.py            :: 无限循环：每 360s 心跳 + 有礼自动领取
python klei_heartbeat_auto.py --once     :: 只跑一轮
```

先决条件（一次性）：
- Steam 客户端已安装并**登录**（脚本只取当前账号的 Steam 票据，不占用/不冲突游戏进程）
- Python 3.8+（用到了 `ctypes`/`urllib`/`ssl`，无第三方依赖）
- Steam 有缺氧（AppID **457140**）

---

## 2. 核心结论（一句话版）

| # | 结论 |
|---|------|
| 1 | **账号登录 = 纯 HTTP**。Steam 票据（`ISteamUser::GetAuthSessionTicket`，hex 大写）→ `POST login.kleientertainment.com/login/LoginViaSteam` body `{SteamTicket, Game:"ONI", FriendlyHostDescriptor, NoEmail:true}` → 返回 `Token`(GameSessionToken)/`UserID`(KU_…)/`AnalyticsJWT`，与游戏内 KleiAccount 登录**一模一样**。 |
| 2 | **GameSessionToken 寿命 ≈ 60 分钟**（DST 实测服务端 `ExpirationTime` = 签发 + 3605s）。到期前用 `POST /login/TokenPurpose` body `{"Token":…}` 换新。 |
| 3 | **游玩礼 = 周期性心跳**。客户端每累积 360s 向 `items.kleientertainment.com/clientitems/ONI/Tick` POST `{"Token":…}`；服务端按心跳推进掉落，命中时回 `{"Error":false,"GiftReceived":true}`（**布尔值，无 DST 的 Key 字段**）。不需要启动游戏/进存档，只要 token 有效（主菜单挂机同理）。 |
| 4 | **领礼 = SetItemOpened**。`Tick` 报 `GiftReceived:true` 后：`GetAllItems` 找出 `Context∈{3,4}` 的未开礼物（逐个揭示）→ 循环 `SetItemOpened{ItemID,ClientToken}`（Context→1）。队列清空后 Tick 的 `E_UNOPENED_ITEMS` 门控解除，新礼能继续掉。 |
| 5 | **神秘箱不挡心跳**。`MYSTERYBOX_*` 是 `Context==1` 容器物，走 `OpenMysteryBox`，与门控无关（默认不动它们）。 |
| 6 | **ONI 无 `VerifyGiftingReceipt`**（DST 有回执防伪，ONI 的 KleiItems 里没有这个请求），脚本已去掉该步骤。 |

---

## 3. 文件清单

### 脚本（可直接运行）
| 文件 | 作用 |
|------|------|
| `心跳自动领取.bat` | **双击入口**：黑窗口 + `chcp 65001` UTF-8 日志 + 跑 `klei_heartbeat_auto.py`，Ctrl+C / pause 退出 |
| `klei_heartbeat_auto.py` | **常驻主程序**：每 360s 心跳 + 有礼自动领取（GetAllItems→SetItemOpened）+ token 自动续期/重登，日志打屏并落盘 |
| `klei_steam_login.py` | **登录脚本**：Steam 票据 → LoginViaSteam → 保存 token/UserID/AnalyticsJWT |
| `klei_heartbeat.py` | **纯心跳脚本**：登录后每 360s 一发 Tick，收礼记录落盘，可计划任务定时调用 |
| `klei_open_gifts.py` | **开礼脚本**：列出/清空 `Context∈{3,4}` 未开礼物队列，可选开神秘箱 |

### 文档
| 文件 | 作用 |
|------|------|
| `README.md` | 本文件（总览 / 用法 / 协议 / 移植说明） |
| `PROTOCOL_NOTES.md` | 协议还原笔记：ONI 源码依据（KleiItems.cs 等）+ ONI/DST 差异对照 |

### 数据与凭证（统一放 `credentials/`，已被 .gitignore 排除）
运行期产生的所有凭证 / 会话 / 库存快照 / 含账号信息日志，**一律写入 `credentials/` 子目录**。

| 文件（`credentials/` 内） | 说明 |
|------|------|
| `.hb_auto_session.json` / `.gift_session.json` | 复用的登录响应（token 60min 即过期，仅本地中转） |
| `klei_heartbeat.log` / `hb_auto_console.log` / `klei_opened.log` / `klei_gifts.log` | 运行日志（自动生成） |

---

## 4. 脚本用法

### 4.1 登录（拿凭证）
```bat
python klei_steam_login.py                        :: 打印 token/UserID/AnalyticsJWT
python klei_steam_login.py --json credentials\out.json  :: 原始响应存盘（放凭证目录）
python klei_steam_login.py --tokenexpiry 3600     :: 让服务端签发带过期时间的 token
python klei_steam_login.py --refresh <TOKEN>      :: 用旧 token 调 TokenPurpose 换新
```

### 4.2 纯心跳（适合计划任务）
```bat
python klei_heartbeat.py                  :: 无限：每 360s 一发 Tick
python klei_heartbeat.py --once           :: 发一次就退出（可配 Windows 计划任务）
python klei_heartbeat.py --interval 600   :: 自定义间隔
python klei_heartbeat.py --token credentials\sess.json :: 复用会话（--save 生成的响应）
python klei_heartbeat.py --log my.log     :: 指定日志文件
```
- 收礼时打印 `★ 收到新礼物！` 并追加 `credentials\klei_gifts.log`
- token 到期前 5 分钟自动续期，续期失败整轮重登；`E_INVALID_TOKEN` 同样自动恢复

### 4.3 开已收礼物（清未开队列）
```bat
python klei_open_gifts.py --dry-run       :: 只列出 Context∈{3,4} 的未开礼物
python klei_open_gifts.py                 :: 循环打开全部（逐个揭示直至清空）
python klei_open_gifts.py --max 5         :: 最多开 5 个
python klei_open_gifts.py --verify        :: 开完发一次 Tick 验证门控是否解除
python klei_open_gifts.py --open-boxes    :: 可选：把 MYSTERYBOX_* 神秘箱也开掉（OpenMysteryBox）
```

### 4.4 常驻：心跳 + 自动领取（日常首选）
```bat
双击「心跳自动领取.bat」或 python klei_heartbeat_auto.py
python klei_heartbeat_auto.py --once --no-logfile --interval 1200
```
每轮：`Tick` → `GiftReceived:true` → `GetAllItems` 找 `Context∈{3,4}` → 逐个 `SetItemOpened`
→ 立即再 Tick 榨干堆叠礼物（最多连领 5 次）→ 到期前续期。`E_UNOPENED_ITEMS` 也会自动清队列。

---

## 5. 接口 / 协议速查

### 认证（login.kleientertainment.com）
| 端点 | 用途 | 请求 body | 响应要点 |
|------|------|-----------|----------|
| `/login/LoginViaSteam` | 登录 | `{SteamTicket, Game:"ONI", FriendlyHostDescriptor, NoEmail, TokenExpiry?}` | `Token`(GameSessionToken), `UserID`, `Username`, `AnalyticsJWT` |
| `/login/TokenPurpose` | 续期/取短时 token | `{"Token":<当前token>}` | 新 `Token`, `ExpirationTime`(≈+3605s), `Platform:"STEAM"` |

### 物品与礼物（items.kleientertainment.com，表头 `ClientToken`，心跳用 `Token`）
| 端点 | 用途 | 请求 body | 响应要点 |
|------|------|-----------|----------|
| `/clientitems/ONI/Tick` | 心跳（360s） | `{"Token":…}` | `{"Error":false,"GiftReceived":true\|false}`；未开堆积时 `ErrorCode:"E_UNOPENED_ITEMS"` |
| `/clientitems/ONI/GetAllItems` | 全量库存 | `{"ClientToken":…}` | `Items:[{ItemID,ItemType,Modified,Context}]`；**Context 3 或 4 = 未开礼物**；`CurrencyMap` 含 `FILAMENT` |
| `/clientitems/ONI/SetItemOpened` | 领取（开礼物） | `{"ClientToken":…,"ItemID":…}` | `{"Error":false}`（Context→1） |
| `/clientitems/ONI/OpenMysteryBox` | 开神秘箱 | `{"ClientToken":…,"ItemID":…}` | `{"Error":false,"Items":[...]}` |

### 常见错误码
`E_INVALID_TOKEN` / `E_EXPIRED_TOKEN`（token 过期/无效，脚本自动续期/重登）/
`E_UNKNOWNE_GAME` 类（Game 必须写 `ONI`）/ `E_UNOPENED_ITEMS`（有未开礼，先开礼）。

> ONI 特有的 Barter（`iap/ONI/GetPricingInfo` / `BarterGainItem` / `BarterLoseItem`，
> FILAMENT 货币兑换装扮）不在本套脚本范围内（心跳链路不需要）。

---

## 6. ONI/DST 差异对照（移植依据）

| 项 | DST | ONI | 依据 |
|---|---|---|---|
| Steam AppID | 322330 | **457140** | Steam 商店 |
| 登录 Game | `DontStarveTogether` | **`ONI`** | `KleiAccount.CLIENT_KEY="ONI"` |
| 心跳端点 | `clientitems/DST/Tick` | **`clientitems/ONI/Tick`** | `KleiItems.RequestTick` |
| 心跳响应 | `GiftReceived:{Key}` | **`GiftReceived:true`（布尔）** | `KleiItems.TickReply{GiftReceived:bool}` |
| 未开标记 | `Context==3` | **`Context∈{3,4}`** | `OnInventoryRecieved: IsOpened = Context!=3 && Context!=4` |
| 领取 | `SetItemOpened` | `SetItemOpened`（同名） | `KleiItems.RequestItemOpened` |
| 开箱 | `OpenMysteryBox` | `OpenMysteryBox`（同名） | `KleiItems.RequestOpenMysteryBox` |
| 回执 | `VerifyGiftingReceipt` | **无**（已删） | ONI `KleiItems` 无此请求类型 |
| 周期 | 360s | **360s** | `KleiItems.SECONDS_PER_TICK=360f` |
| steam_api64.dll | `bin64/` | `OxygenNotIncluded_Data/Plugins/x86_64/`（自动定位） | ONI 安装目录 |

---

## 7. 状态与注意事项

- ✅ 移植完成：登录链路（LoginViaSteam/TokenPurpose）协议与 DST 完全同构，DST 已实测有效；
- 🚧 心跳/领礼的 ONI 字段严格取自反编译源码（`KleiItems.cs`），**尚未在真实 ONI 账号上端到端验证**，见 §2 说明。
  首次运行报 `E_UNKNOWN_GAME` 类错误 → 检查 `klei_steam_login.GAME`；报 `E_INVALID_TOKEN` → 正常续期/重登即可。
- ⚠️ **Token 等于你 Steam 账号的 Klei 登录凭证**——不要外传；所有凭证/会话/日志已统一收进 `credentials/` 并被 `.gitignore` 排除，请勿改回。
- 心跳间隔下限 30s（脚本默认/建议 360s，对齐游戏行为），高频刷新可能触发 Klei 风控。
- 首次运行 `steam_api64.dll`/Steam 客户端会向 stderr 打印 `Setting breakpad minidump AppID` 等行，可忽略。
- 本套脚本**不改游戏文件、不注入进程、不伪造请求体字段**，只做「与游戏完全相同的正规 HTTP 请求」；
  但登录/礼物行为仍受 Klei 服务条款约束，仅供学习与自用。

---

*Unity 6.0.35f2 · 反编译工具链：ilspycmd 11 · 2026-09-05*