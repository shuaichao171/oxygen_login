# Protocol Notes — ONI 心跳/登录协议还原（源码依据）

> 本文档列出 ONI 侧每个协议字段在**反编译源码**中的出处，作为移植与排障依据。
> 反编译产物：`Oxygen_login/decompiled-src-firstpass/`（Assembly-CSharp-firstpass.dll）
> 与 `Oxygen_login/decompiled-src/`（Assembly-CSharp.dll）。

## 1. 登录（KleiAccount.cs）

| 协议项 | 值 | 源码出处 |
|---|---|---|
| Host | `login.kleientertainment.com` | `KleiAccount.cs: LIVE_ENDPOINT = "login.kleientertainment.com" + DistributionPlatform.Inst.AccountLoginEndpoint` |
| 路径（Steam） | `/login/LoginViaSteam` | `SteamDistributionPlatform.cs: AccountLoginEndpoint => "/login/LoginViaSteam"` |
| SteamTicket | 大写 HEX | `KleiAccount.EncodeToAsciiHEX`（`ToString("X2")`） |
| Game | `ONI` | `KleiAccount: CLIENT_KEY = "ONI"`（构造器），`BuildLoginRequest("Game", CLIENT_KEY)` |
| NoEmail | `true` | `BuildLoginRequest` |
| 响应 Token | GameSessionToken | `AccountReply{UserID, Token, Error, SupplementaryData}` |

## 2. 心跳（KleiItems.cs）— 核心

```csharp
// KleiItems.cs
private const float SECONDS_PER_TICK = 360f;      // 心跳周期
private float TimeToNextTick;                      // 每帧递减，到 0 发 Tick
// Update():
TimeToNextTick -= Time.unscaledDeltaTime;
if (TimeToNextTick <= 0f) { AddRequestTick(); TimeToNextTick += 360f; }
```

- 端点：`KleiItemsConfig.SERVER_URL` + `"clientitems/ONI/Tick"`
- body：`{ "Token": <KleiAccount.KleiToken> }`（`RequestTick`）
- 响应结构：`TickReply { bool Error; string ErrorCode; bool GiftReceived; }`（`OnTickReply`）
  - `GiftReceived == true` → 触发 `GetAllItems` 刷新（`OnTickReply`）
- token 失效：`E_EXPIRED_TOKEN` / `E_INVALID_TOKEN` → 重登（`HandleError`）

## 3. 库存与领取（KleiItems.cs）

| 端点 | body | 未开判定 |
|---|---|---|
| `clientitems/ONI/GetAllItems` | `{ ClientToken }` | `IsOpened = Context != 3 && Context != 4`（`OnInventoryRecieved`）→ 未开 = Context 3 或 4 |
| `clientitems/ONI/SetItemOpened` | `{ ClientToken, ItemID }` | 成功 `Error:false` |
| `clientitems/ONI/OpenMysteryBox` | `{ ClientToken, ItemID }` | 成功返回 `Items[]` |

- 响应 InventoryReply：`{ bool Error; string ErrorCode; Item[] Items; Dictionary<string,ulong> CurrencyMap; }`
  - Item：`{ ulong ItemID; string ItemType; int Modified; int Context; }`
  - CurrencyMap 键 `"FILAMENT"` → 兑换货币（脚本未使用）
- 本地缓存：`Util.RootFolder()/<KleiItemUserDataFolder>/<userid>.json`（脚本不依赖）

## 4. UI 层佐证

- `KleiItemDropScreen.cs`：游戏内"礼物舱"（THANKS_FOR_PLAYING / NOTHING_AVAILABLE），
  `PermitItems.HasUnopenedItem()` → `QueueRequestOpenOrUnboxItem`。
- `PermitItems.cs`：628 个 ItemType→Permit 映射 + 8 个 `MYSTERYBOX_*` 箱子定义。
- `Global.cs`：`Global.Update()` 每帧 `ThreadedHttps<KleiItems>.Instance.Update()` 驱动心跳，
  主菜单即可累积 360s。

## 5. ONI vs DST 协议差异

| 差异 | DST | ONI |
|---|---|---|
| Tick 响应给不给 Key | `GiftReceived:{Key}` | `GiftReceived:bool`（无 Key） |
| 领取回执 | 有 `VerifyGiftingReceipt` | 无 |
| 未开 Context | 3 | 3 和 4 |
| 兑换系统 | spools 等 | `iap/ONI/Barter*`（FILAMENT） |

---
*依据 ilspycmd 11 反编译产物；字段以游戏当前版本（Unity 6.0.35f2）为准。*