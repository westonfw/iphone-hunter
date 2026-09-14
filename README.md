# iphone-hunter

盯 Apple 中国官网，**新 iPhone 一放货就把你叫醒**，并在你自己已登录的浏览器里自动走完「加购 → 结账」，只把付款那一下留给你。

---

## 先把时间账算清楚

「收到提醒再去点，早就没货了」——这个担心是对的。从「有货」到「付款成功」，纯手工：

| 环节 | 耗时 |
|---|---|
| 轮询间隔（平均等半个周期） | 2.5s |
| Bark 推送送达 | 1–5s |
| 你看到手机并拿起来 | 5–60s |
| **打开页面 → 选配置 → 加购 → 结账** | **60–90s** |

合计 70–150 秒，而补货窗口常常只有几十秒。**瓶颈不在监控，在最后那 90 秒。**

工具分三层解决：

1. **监控层** — 秒级盯机型上架、能否下单、哪家直营店今天能取货；持续库存提醒只在直营店可提货时触发。
2. **预热层** — 开卖**之前**就把产品页加载好、必选项选好、购物袋清空。
3. **自动下单层** — 放货瞬间只剩「点加购 + 跳结账」，停在付款页响铃叫你。

### 实测耗时

| 环节 | 冷启动 | 预热后 |
|---|---|---|
| 检测到有货 → 加入购物袋 | 8.7–11.1s | **3.1–4.4s** |
| 加购 → 结账页 | ~19s | ~19s（Apple 服务端，压不动） |
| **合计** | ~33s | **~23.5s** |

那 19 秒是 `secure8` 结账的重定向链，属于服务器侧，客户端优化不动。

> **硬性边界：绝不提交付款。** 流程停在结账页就交还给你。付款必须你本人确认，而且 Apple 结算页有风控，脚本化提交容易触发。这一条不可配置。

---

## 快速开始

```bash
cd iphone-hunter
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

之后所有命令用 `.venv/bin/python -m hunter ...`。（只做监控、不要自动下单的话，系统装个 `requests` 就够，用 `python3 -m hunter ...`。）

第一次跑任意命令会自动从 `config.example.json` 生成 `config.json`。

### 1. 配好通知

编辑 `config.json` 的 `notifiers`。**强烈建议开 Bark**：iOS 上装 [Bark](https://apps.apple.com/cn/app/bark/id1403753865)，复制 App 首页的 key 填进去。

```json
"bark": { "enabled": true, "key": "你的key", "sound": "alarm", "volume": 10 }
```

Bark 走 `level=critical`：**静音、勿扰、专注模式都拦不住，会持续响铃**，点通知直接打开购买页。半夜放货就靠它。

```bash
.venv/bin/python -m hunter test
```

### 2. 接上你自己的浏览器

```bash
.venv/bin/python -m hunter connect --launch
```

会开一个带调试端口的 Chrome 并停在购物袋页。**在里面登录你的 Apple ID**，确认收货地址和付款方式都在。工具挂上去（CDP 连接），**不读取也不复制你的 profile 目录**。

详见下面的[浏览器接入的三个坑](#浏览器接入的三个坑)。

### 3. 找门店编号

```bash
.venv/bin/python -m hunter stores 200000 --part MJT74CH/A
```

```
200000 附近 12 家直营店：
  R390   香港广场    上海    Apple 香港广场
  R581   五角场      上海    Apple 五角场
  ...
```

**中国大陆必须用邮政编码**，中文城市名 Apple 直接拒（`请输入有效的省/市名称或邮政编码`）。北京 `100000`、上海 `200000`、广州 `510000`、深圳 `518000`、成都 `610000`、杭州 `310000`。

把邮编填进 `pickup.location`，想盯的门店编号填进 `pickup.stores`（留空 = 该邮编附近全部）。

### 4. 选定目标机型

```bash
.venv/bin/python -m hunter parts iphone-18-pro                       # 看 Pro / Pro Max 全部配置
.venv/bin/python -m hunter parts iphone-18-pro --save --filter 512GB  # 存进监控列表
```

`slug` 就是购买页 URL 的最后一段（`apple.com.cn/shop/buy-iphone/<slug>`）。

#### 暂时不盯某些型号：`enabled: false`

```json
"watch": [
  { "part": "MJY64CH/A", "note": "iPhone 18 Pro Max 256GB 黑色",
    "model_slug": "iphone-18-pro", "request_group": "iphone-18-pro-max",
    "enabled": true },
  { "part": "MK2N4CH/A", "note": "iPhone Duo 256GB Night Sky",
    "model_slug": "iphone-duo", "request_group": "iphone-duo",
    "enabled": false }
]
```

留着配置但不盯它。盯一堆用不上的型号既白烧请求预算（`budget_per_hour` 是
真正决定「能盯多久」的东西），又让日志刷满噪音，真正在等的那个反而看不见。

**不写 `enabled` 视为启用**，老配置不受影响；只有显式的 `false` 才会关掉。

#### iPhone 18 Pro 系列（中国大陆）

根据 Apple 中国大陆官网 2026-09-10 公布的配置，iPhone 18 Pro 和 iPhone 18 Pro Max 共用购买页 slug `iphone-18-pro`。官网当前没有 `iphone-18` 购买页。两款机型都有黑色、银色、冰川蓝色和勃艮第酒红色，每款均为 256GB、512GB、1TB 和 2TB，合计 32 个 SKU。

| 容量 | iPhone 18 Pro | iPhone 18 Pro Max |
|---|---:|---:|
| 256GB | RMB 9,999 | RMB 10,999 |
| 512GB | RMB 11,999 | RMB 12,999 |
| 1TB | RMB 15,499 | RMB 16,499 |
| 2TB | RMB 20,499 | RMB 21,499 |

Part number 由官网实时目录解析，不在代码里写死。运行上面的 `parts` 命令即可查看全部 32 个实际 part number；例如 `MJT74CH/A` 是 iPhone 18 Pro 256GB 黑色。官方信息见 [选购页](https://www.apple.com.cn/shop/buy-iphone/iphone-18-pro) 和 [Apple 新闻稿](https://www.apple.com.cn/newsroom/2026/09/apple-debuts-iphone-18-pro-and-iphone-18-pro-max/)。

### 5. 抢购前必做：彩排

```bash
.venv/bin/python -m hunter rehearse
```

走到「加入购物袋」前一步就停，**不会真的加购**。确认选择器还有效、页面没改版。

**发布会之前一定要跑一次。** 别等抢购当天才发现 Apple 改版了。

### 6. 开卖

```bash
./run.sh
```

`launch` 盯新机型页面上线，`watch` 记录下单状态并盯门店 + 预热；只有直营店可提货时才提醒并触发自动下单。

---

## 只盯库存和取货状态

不需要自动下单、只想「直营店可提货就叫我」的话，直接用 `watch`，别用 `run.sh`（那个会连带跑 `launch` 盯上架）。`watch` 仍会在终端打印能否下单和预计发货时间，但不会为这些状态发送通知。

```bash
python3 -m hunter watch            # 常规，自适应节奏（见「怎么不被 block」）
python3 -m hunter watch --sprint   # 开卖前 10 分钟，全速 5s 一轮
```

`config.json` 里保证这几项，`watch` 就是纯监控形态：

| 字段 | 值 | 效果 |
|---|---|---|
| `autobuy.enabled` | `false` | 命中只推送，不碰浏览器 |
| `open_browser_on_hit` | `false` | 也不会自动弹浏览器 |
| `pickup.enabled` | `true` | 查门店取货状态 |
| `pickup.location` | 邮编，如 `200000` | 必填，否则跳过门店监控 |
| `pickup.stores` | 如 `["R581","R359"]` | 只报这几家；留空 = 附近全部 |

纯监控**不需要 venv 和 Playwright**，系统装个 `requests` 就能跑：

```bash
pip3 install requests
```

监控多个 SKU 时给它们填同一个 `request_group`，Apple 一次请求就能带回全部型号在每家门店的状态——**盯 12 个配置和盯 1 个，请求数一样**。

想临时查一次就退出：

```bash
python3 -m hunter check --location 200000
```

---

## 怎么不被 block（跑得久比跑得快重要）

固定间隔是最好认的机器特征。原来 20s 一轮 + ±30% 抖动，看着"随机"，但把请求时刻画成一条时间轴，它仍然是一轮一格的，统计上一眼就能挑出来——而且一整天下来累计请求量摆在那儿。**决定你能盯多久的不是间隔，是每小时总请求数。**

现在换成四件事叠起来：

| 手段 | 做什么 | 为什么 |
|---|---|---|
| **泊松间隔** | 间隔服从平移指数分布，均值等于目标间隔 | 无记忆性，形状跟人的点击流一致，抽不出周期 |
| **每小时预算** | 令牌桶硬限总请求数，超了就自动拉长间隔 | 这才是真正决定「能盯多久」的量 |
| **AIMD 退避** | 被拦一次速率减半，之后每成功一轮加回 1/4 | 见下 |
| **冷热时段** | 只在会放货的时段全速，其余时段降速 5 倍 | 把有限的配额花在有用的时候 |

### AIMD：为什么不能一成功就满速冲回去

老逻辑 `fail_streak` 一次成功就清零，于是「被拦 → 退避 → 立刻满速 → 再被拦」来回震荡，每震荡一次就在对方那边多记一笔，越撞越黑。

改成 TCP 的拥塞控制：**乘性减、加性增**。被拦一次间隔翻倍，之后每成功一轮只把倍率减 0.25——从被拦一次恢复到满速要 4 轮，被拦三次要 28 轮。慢，但这正是重点：它会自己收敛到对方能接受的那个速率上待着，而不是反复试探。

对方返回 `Retry-After` 时照它睡，那比我们自己猜的权威。另外每次被拦都会换一套完整的浏览器身份（UA + 匹配的 client hints + 全新 cookie）重开会话——被标记之后拿同一个会话接着打，只会一路 541。

> 平时**不**轮换身份：同一个 IP 上老换 UA、老丢 cookie，本身就是异常信号。只在已经被标记之后换才是净收益。

### 砍掉一半请求

`availability-message` 只用来在终端打印「能不能下单 / 几天发货」，**提醒和自动下单完全由 `pickup-message` 触发**。每轮都打它等于把预算白花一半，所以默认 6 轮才打一次（`pacing.availability_every`）。门店监控关掉时它会自动恢复每轮查询，因为那时它是唯一的信息来源。

配上「同 `request_group` 的 SKU 合并成一个请求」，盯 12 个配置的稳态成本是**每轮 1.17 个请求**。

### 配置

```jsonc
"pacing": {
  "base_interval": 30,            // 热时段目标平均间隔（秒）
  "sprint_interval": 5,           // --sprint 时的目标间隔
  "min_interval": 4,              // 下限，再急也不会比这更快
  "max_interval": 900,            // 上限，退避退到头就是 15 分钟
  "budget_per_hour": 150,         // 每小时请求硬上限 ← 想盯更久就调小这个
  "sprint_budget_per_hour": 900,  // 冲刺是短跑，配额放宽
  "burst": 20,                    // 允许攒出多长的突发
  "hot_windows": ["01:00-03:00", "06:00-09:00",   // 全速时段，支持跨零点
                  "15:00-18:00", "20:00-22:00"],
  "cold_multiplier": 5,           // 热时段之外降速几倍
  "recover_step": 0.25,           // 每成功一轮，退避倍率减多少
  "availability_every": 6         // 每几轮查一次发货状态，0 = 关掉
}
```

几条经验：

- **想盯得更久，先调小 `budget_per_hour`，别去调大 `base_interval`。** 预算是硬约束，间隔只是目标值；预算不够时 Pacer 会自动把间隔拉长，反过来不行。
- `hot_windows` 留空 = 全天等速，此时纯靠预算兜底。默认那四段（凌晨 1–3 点、早 6–9 点、下午 3–6 点、晚 8–10 点）是实际盯下来的补货规律，按你自己观察到的改。
- 开卖当天用 `--sprint`：无视冷热时段、配额放宽到 900/小时。它是给「你人就守在旁边的那十分钟」用的，不要拿它跑通宵。
- 退出时会打印这次一共发了多少请求、被拦几次，用它来校准预算。

老的 `poll_interval` / `jitter` 还认，但只在没配 `pacing` 时当默认间隔用。

---

## 日志：终端 + 每个请求都落盘

监控是挂通宵的活，终端 scrollback 留不住——而「凌晨三点被拦了几次、什么时候恢复的、
放货那一刻请求耗时多少」恰恰是事后唯一有用的东西。所以每次运行都写两份：

```
logs/watch-2026-09-14.log            # 终端上看到的一切，每行带日期+秒级时间戳
logs/watch-2026-09-14.requests.jsonl # 每个请求一行：URL / 参数 / 状态码 / 耗时 / 字节数 / 用的哪套身份
```

文件名带命令名，因为 `run.sh` 会同时跑 `launch` 和 `watch` 两个进程，写同一个文件会互相插行。
按天滚动，超过 `keep_days` 天的自动删掉，不用配 logrotate。

请求明细**只落盘、不打终端**：每轮一两条，打出来会把库存变化那几行刷没。被拦的请求照样记
（`status: 541` + `error`）——校准预算靠的就是这些。

```jsonl
{"ts":"2026-09-14T01:12:03.118","cmd":"watch","n":88,"url":"https://www.apple.com.cn/shop/retail/pickup-message","params":{"parts.0":"MJT74CH/A","location":"200000"},"ms":794,"status":200,"bytes":57432,"ua":"Chrome/152.0.0.0"}
```

几个现成的用法：

```bash
# 这一天每小时发了多少请求 —— 拿它调 budget_per_hour
jq -r .ts logs/watch-*.requests.jsonl | cut -c1-13 | uniq -c

# 被拦了几次、什么时候
jq -r 'select(.status>=400) | "\(.ts) \(.status) \(.ua)"' logs/watch-*.requests.jsonl

# 响应耗时的分布（Apple 那边卡不卡）
jq -r .ms logs/watch-*.requests.jsonl | sort -n | awk '{a[NR]=$1} END{print "中位",a[int(NR/2)],"p95",a[int(NR*0.95)]}'
```

配置在 `logging` 段，不配就是下面这些默认值，开箱即写：

```jsonc
"logging": {
  "enabled": true,      // 整个关掉就只打终端
  "dir": "logs",        // 相对仓库根目录，也可以给绝对路径
  "requests": true,     // 关掉就只留终端日志，不记请求明细
  "keep_days": 14,      // 超过这么多天的日志自动删，0 = 永久保留
  "echo": true          // false = 只写文件、终端不打（nohup 挂后台时用）
}
```

日志里不会出现密码、身份证号、卡号：请求明细只记 Apple 的公开查询接口，结账那条链路走的是
浏览器、本来就不经过这里，而终端输出里的这些字段早就是打码的。

---

## 命令

| 命令 | 用途 |
|---|---|
| `parts <机型>` | 列出该机型全部配置的 part number / 价格；`--save` 存进监控列表，`--filter 关键词` 只存匹配的 |
| `check [part...]` | 查一次就退出；`--location <邮编>` 顺带查门店 |
| `stores <邮编>` | 列出附近直营店的编号 / 名称 / 地址 |
| `launch --slug <机型>` | 持续盯机型购买页何时上线（**发布会当天用这个**） |
| `watch` | 持续盯库存和门店，命中则预热 + 自动下单；`autobuy.enabled: false` 时就是[纯监控](#只盯库存和取货状态) |
| `connect` | 诊断能否挂到你已登录的 Chrome；`--launch` 直接开一个 |
| `rehearse` | 彩排自动下单流程（不会真的下单） |
| `inspect-checkout` | 只读查看结账页的配送方式控件 |
| `fastpath` | 在已打开的结账页上跑发包流程；默认停在 Review，`--confirm` 才真下单；`--store R581` |
| `har <文件>` | 解析 Chrome 导出的 HAR，看结账每一步发了什么（**抓结账链路首选这个**） |
| `record` | 挂到 Chrome 录人工结账；`--attach` 只挂钩不跳转。**CDP 下会静默卡死，优先用 `har`** |
| `session-probe` | 长时间记录登录/会话状态，测它到底能挂多久（见[会话保活](#会话保活挂几天的话什么会先掉)） |
| `test` | 发一条测试通知 |

`launch` / `watch` 支持 `--sprint`（全速短跑，无视冷热时段，配额也放宽）。全局 `--region` 可切区域（`cn` `hk` `tw` `us` `jp` `sg`）。

---

## 配置说明

| 字段 | 说明 |
|---|---|
| `region` | 区域，默认 `cn` |
| `pacing.*` | 请求节奏，见[怎么不被 block](#怎么不被-block跑得久比跑得快重要) |
| `poll_interval` / `sprint_interval` | 老字段，只在没配 `pacing` 时当默认间隔用 |
| `logging.*` | 日志落盘，见[日志](#日志终端--每个请求都落盘)。不配就用默认值 |
| `open_browser_on_hit` | 命中时自动开浏览器（`autobuy` 开启时不用） |
| `launch_watch.slugs` | `launch` 默认盯的机型 |
| `watch[].request_group` | 可选请求分组；同组 SKU 合并查询，不同组分开查询 |
| `pickup.location` | 查门店取货用的**邮政编码**，必填否则跳过门店监控 |
| `pickup.stores` | 只盯这几家门店（如 `["R581"]`），留空 = 附近全部 |
| `autobuy.enabled` | 命中时是否自动走到结账页，默认 `false`（**先跑 `rehearse`**） |
| `autobuy.warm` | 是否预热产品页，**默认 `false`**。只在知道几点开卖时才开（发布会当晚提前半小时）；补货监控不知道什么时候放货，挂久了预热页的会话会过期 |
| `autobuy.pickup_stores` | 可接受的取货门店名（如 `["五角场","浦东"]`）。放货那一刻真有货的会自动排到最前，留空 = 有货的都能下。老字段 `pickup_store_name` 仍兼容 |
| `autobuy.clear_bag_before_add` | 加购前先清空购物袋，默认 `true`。**别关**，见[限购](#坑-6限购-2-台超限时静默卡死) |
| `autobuy.pickup_store_name` | 结账时尝试选的取货门店名，如 `"五角场"` |
| `autobuy.mode` | `auto`（默认）/ `cdp` / `profile` |
| `autobuy.cdp_url` | 锁定调试端口地址；留空自动探测 |
| `autobuy.trade_in` / `autobuy.applecare` | 两个必选项选哪个，默认「不折抵」「不加 AppleCare」 |

---

## 实测发现汇总

这一节是这个项目最有价值的部分。每一条都是实际撞上并验证过的，不是猜的。

### 接口层

#### 坑 1：`/shop/fulfillment-messages` 已经彻底不可用

网上绝大多数教程和老项目（包括曾经最流行的 `hteen/apple-store-helper`）都在用这个接口。它现在对**任意**请求恒定返回 **HTTP 541 + 一个 128002 字节的拦截页**。实测中国大陆站和美国站响应完全一致，同一时刻 `apple.com.cn` 首页正常 200，排除 IP 被封。

**本项目改用 `/shop/retail/pickup-message`。**

#### 坑 2：`sba/availability-message` 的门店字段不可信

它也返回 `availableAtAnyStore` / `partAvailableStoresCount`，但**不带门店上下文时，对有货的机型也恒返回 `false` / `0`**。

实测同一时刻的对照：

```
sba/availability-message  →  availableAtAnyStore=False, 门店有货数=0
retail/pickup-message     →  北京 9 家店全部「今天可取货」
```

照它做判断会**永远收不到通知**。本项目只用它判断「能不能下单」和发货时间，门店库存一律以 `pickup-message` 为准。

#### 坑 3：门店查询必须用邮政编码

`location=北京` 会被拒（`errorMessage: 请输入有效的省/市名称或邮政编码`），`location=100000` 正常返回 9 家门店。也可以用 `store=R448` 只查一家。

一次请求可带多个 `parts.N`，Apple 会对每家门店返回全部型号的状态——**盯 10 个配置和盯 1 个，请求数一样**。

#### 频率：短时间不限流，长时间会被掐

`pickup-message` 连打 20 次（间隔约 1.2s）全部 200，平均 0.8s——**短跑没问题**。但按固定间隔连打几个小时就会开始收 541，那是 Akamai 按累计速率和请求指纹判的，不是按瞬时速率。怎么跑得久见下一节。

### 「走接口直接发包」为什么不行

这是个很自然的想法：首发时几十万人同时访问，页面根本打不开，那就绕过 UI 直接构造请求包。实测三条硬事实说明这条路走不通：

**1. 加购请求确实抓到了**，而且比想象的简单——是个 GET，不是 POST，没有 CSRF 头：

```
GET /shop/buy-iphone/iphone-18-pro/mjt74ch/a
    ?product=MJT74CH%2FA&purchaseOption=fullPrice&step=select
    &acpart=none&atbtoken=8
```

**2. 但 `atbtoken` 是有效校验的防自动化令牌。** 用抓来的值发 `fetch` → 返回 200、354KB 的完整页面，但**购物袋没有任何变化**。这个 token 由页面 JS 在点击时现算，不在 DOM 也不在全局变量里。要程序化生成它，就是在绕过这个防护机制——**本项目不做**。

**3. 而且发包省不掉网络开销，也绕不过排队：**

- 加购本来就只是**一个 GET**。点击和发包都是一个请求，成本完全一样。
- Apple 的排队和限流做在**边缘层（CDN/WAF）**，拦的是请求本身，不区分你是浏览器点击还是 curl 发包。坑 1 里那个对所有请求恒返 541 的接口就是证据。

**真正的开销在加载产品页那 700KB**——而那页必须加载，因为 `atbtoken` 要靠它的 JS 生成。

**所以正解不是换协议，是把页面加载挪到开卖之前**，也就是下面的预热。

#### 顺带验证过的无效路径

| 尝试 | 结果 |
|---|---|
| URL 参数预置 `purchaseOption` / `acpart` 免掉点击 | ✗ 按钮仍是灰的，而且慢到 19.5s |
| `rtsid=R581` cookie 预置取货门店 | ✗ 无效 |
| URL 参数 `?pickupStore=R581` / `?store=R581` | ✗ 无效 |
| `/shop/bag/add?product=X` 直接加购 | ✗ `CSRF_ERROR` |
| 产品页 / 购物袋页上选取货门店 | ✗ 没有这个控件，只在结账流程里 |

### 结账向导能不能改成发包？——能，但**不会更快**

先说结论，免得照着「优化」白干一场：**发包和点页面耗时几乎一样**，
因为瓶颈完全在 Apple 服务端。2026-09-14 同一条链路 A/B 实测：

| 跑法 | 向导耗时 | 加购到 Review 总耗时 |
|---|---|---|
| 发包（六步） | 58.3s / 59.0s | 79.1s / 77.7s |
| **点页面** | ~58s | **75.5s** |

分段计时把原因钉死了——每一步的时间**全是 TTFB**：

```
search ✓ 10361ms （等服务端 10183ms / 收包 1ms · TTFB 10164ms / 排队 17ms）
continueFromBillingToReview ✓ 10517ms （TTFB 10349ms / 排队 1ms / 收包 0ms）
```

`checkoutx` 每步固定 7.5~10.5 秒的服务端处理，排队 1ms、收包 0ms。
客户端不管怎么优化都省不掉这段，**两条路付一样的钱**。

> 早期版本的 README 在这里写过「能把结账从 19s 压到 5s」。那是**估计，不是测量**，
> 已被上面的数据推翻。留着这段是为了记住：没量过就别写成结论。

#### 那还留着发包这条路吗

速度上没理由，但有三个真实好处：

0. **不依赖页面渲染出来。** `x-aos-stk` 写在服务端返回的 HTML 内联 JSON 里，
   `domcontentloaded` 就能读到；点页面那条路必须等 React 把「继续」按钮真的
   画出来才能操作（`on_checkout` 最多等 9s）。页面渲染越慢、越卡，这个差距越大——
   极端情况下向导根本没渲染出来，点页面彻底没辙，发包只要 HTML 到了就能干活。

   > 为此 `wait_for_stk()` 是**轮询等**的，不是读一次拿不到就放弃。早期版本
   > 就是读一次即放弃，等于在页面最慢、最需要这条路的时候主动退回到更慢的
   > 那条路上去——正好把这个优势抵消掉了。
   >
   > 注意边界：它省掉的是**每一步的重绘等待**，省不掉**第一次加载结账页**，
   > 因为令牌得从那份 HTML 里读。而且这个好处在平时测不出来——上面的 A/B
   > 是在闲时跑的，两条路都不卡，所以打平。放货高峰能差多少，**没有实测数据**。

1. **请求数少一半。** 点页面那次日志里，「继续填写取货详情」点了 3 次、
   取货人那步点了 3 次、付款方式反复弹了好几轮，一趟下来十几个请求；
   发包固定 6 个。在会返 541 的这族端点上（坑 9），请求少就是风险低。
2. **不跟 DOM 搏斗。** 点页面要处理「点上又被冲掉了」「已选中但没提交上去，
   先弹到别的选项再选回来」这类 React 重绘问题，发包没有这些。

默认 `fast_path` 可开可不开，失败会自动退回点页面，两条路都保留。

#### 副产品：修正了一个凭感觉拍的常数

`STEP_COOLDOWN_S`（同一步点完之后多久才允许再点）原来是 **2.8s**，拍脑袋定的。
实测服务端每步要 10s，于是页面还没翻页冷却就到期了——日志里同一个按钮
连点 3 次，等于把同一个动作重复提交。按实测上限改成 **12s**。

这个 bug 在发包这条路上不存在（它是同步等响应的），只影响点页面那条路，
但正是做 A/B 才暴露出来的。

#### 关键：`x-aos-stk` 是发下来的，不是算出来的

| 观察 | 结果 |
|---|---|
| 长度 | 27 字符 |
| 6 次请求里是否轮换 | **恒定不变** |
| 是否在响应头里下发 | 否 |
| 是否在响应体里 | **是**——结账页 HTML 的内联 JSON |

```
..."x-aos-model-page":"checkoutPage","modelVersion":"v2","x-aos-stk":"<值>"
```

所以它是**页面发下来、原样回传的会话令牌**，读出来复用跟伪造 `atbtoken` 是
两码事。结账页本来就必须加载（要建会话），顺手把它读出来零成本。

> 找它的正则别写错：键名是 `x-aos-stk`，不是 `xAosStk`；也**不在** `<meta>` 或
> `<input>` 里（这两处我都找过，零命中）。`JS_AOS_POST` 里那条
> `x-aos-stk["']?\s*[:=]\s*["']([^"']+)` 兜底正则是对的，meta/input 那两条是白写的。

#### 六步的完整形状

每一步都带这组头：`x-aos-stk`、`x-aos-model-page: checkoutPage`、
`x-requested-with`、`syntax`、`modelversion`（后三个是常量）。

| # | `_a=` | path | 关键字段 |
|---|---|---|---|
| 1 | `selectFulfillmentLocationAction` | `/checkoutx/fulfillment` | `selectFulfillmentLocation=RETAIL` |
| 2 | `search` | `/checkoutx/fulfillment` | `selectStore=R581`、`city`/`state`/`district` |
| 3 | `continueFromFulfillmentToPickupContact` | `/checkoutx/fulfillment` | 同上全带一遍 |
| 4 | `continueFromPickupContactToBilling` | `/checkoutx` | 姓名、身份证后四位、`selectFapiao` |
| 5 | `selectBillingOptionAction` | `/checkoutx/billing` | `selectBillingOption`、`locationConsent=true` |
| 6 | `continueFromBillingToReview` | `/checkoutx/billing` | `selectBillingOption`、`selectInstallmentOption` |
| 7 | `continueFromReviewToProcess` | `/checkoutx/review` | **空请求体**（`_m=checkout.review.placeOrder`） |
| 8 | `checkStatus` | `/checkoutx/statusX` | **空请求体**（`_m=spinner`），轮询结果 |

第 7 步就是「立即下单」。它只创建**待付款订单**，二维码留给人扫——
本工具永远不代付款，信用卡这类即时扣款的付款方式在代码里硬拒（`_may_place`）。

#### 下单成功与否：只认跳转，别看文案

`checkStatus` 返回 `{"head":{"status":302,"data":{"url": <去处>}}}`，**那个 URL 才是结论**：

| 去处 | 含义 |
|---|---|
| `.../checkout/thankyou`、`/shop/order` | ✅ 订单已创建 |
| `.../shop/checkout` | ❌ 被驳回，退回结账页 |
| `.../checkout/status` | ⏳ 还在处理，继续轮询 |

判定写成「回结账页=失败，其余=成功」会制造**假成功**：轮询超时时最后拿到的
还是 status 页，按那种写法会报下单成功，于是不重试也不提醒，而购物袋还在那儿。
所以 `order_rejected()` 反过来写——**只有明确跳到 thankyou/订单页才算成功**。

#### 驳回长什么样（2026-09-14 实测）

六步全部 200、Review 页正常显示取货 + 招商银行 24 期，提交后仍可能被打回：

```
continueFromReviewToProcess → 302 /shop/checkout/status   （"正在处理"）
checkStatus                 → 302 .../shop/checkout        ← 驳回
```

结账页上随后出现：

> 你所选择的"送货与取货"选项已不再为本订单提供。请在下方选择一个新选项。

模型里的指标是 `transaction.offer.forcehomeshipping.alert`（`slot: "Pickup"`）。
注意**选店那一步的接口仍然回报 `今天 可取货` / `availableNowForLine: true`**——
所以「选店时说有货」不能推出「下单时能履约」，这两个判断在 Apple 侧不是一回事。

字段名全是自解释的模型路径（`checkout.fulfillment.pickupTab.pickup.storeLocator.selectStore`
这种），**请求体里没有任何来路不明的长令牌**——`verificationToken` 实测是空值。

#### 已实现：`hunter/fastpath.py`

```bash
# 先在浏览器里走到结账第一步（购物袋 → 结账），再跑
.venv/bin/python -m hunter fastpath
```

打开 `autobuy.fast_path: true` 后，`watch` / `buy` 会自动走这条路，
**任何一步对不上就自动退回点页面的老路**——快车道是优化，不是依赖。

```json
"autobuy": {
  "fast_path": true,
  "payment_method": "招商银行",
  "installment_months": 24,
  "pickup_store_numbers": ["R581", "R359", "R389"],
  "pickup_city": "上海", "pickup_state": "上海", "pickup_district": "杨浦区"
}
```

请求走的是**页面自己的 `fetch`**，不是 Python 的 `requests`：这样复用浏览器的
cookie、TLS 指纹和 UA。换成外部客户端，指纹对不上反而更容易被风控盯上，
而这里打的正是最敏感的那族端点。

#### 坑 A：HTTP 200 **不等于**这一步生效了

实测栽过一次：四步全返回 200，服务端却一直停在 Fulfillment，于是第 4 步拿不到
付款选项；再退回点页面时，DOM 和服务端状态已经错位，变成「反复点同一个继续
按钮、`_s` 一直是 Fulfillment-init」的死循环，空转到超时。

硬判据是**响应里有没有产出下一节**：

| 步骤 | `body.checkout` 里必须出现 |
|---|---|
| `continueFromFulfillmentToPickupContact` | `pickupContact` |
| `continueFromPickupContactToBilling` | `billing` |
| `continueFromBillingToReview` | `review` |

少了就抛 `Stalled` 立刻停，并且**退回点页面之前先重新加载结账页**让 DOM 对齐。
`place()` 里另有一道 stuck 计数：同一步连点 5 次不前进就收手。

#### 坑 B：取货人信息要用 Apple 预填的那份，别自己编

第 4 步要回传姓名、邮箱、电话、身份证后四位。**除了身份证后四位，其余都是
Apple 按账号预填好的**，第 3 步的响应里就带着：

```
selfPickupContact.selfContact.address.d.lastName      len=1
selfPickupContact.selfContact.address.d.firstName     len=1
selfPickupContact.selfContact.address.d.emailAddress  len=18
selfPickupContact.nationalIdSelf.d.nationalIdSelf     len=0   ← 只有这个要自己填
```

点页面那条路之所以能过，是因为它只是点「继续」、没碰这些预填值。发包时如果
按 config 去填而 config 是空的，就会把空姓名发过去——服务端返回 200、状态不动，
正是坑 A 的现场。所以 `contact_fields()` 从第 3 步响应里取回当前值，config
填了才覆盖。

> 模型把当前值放在 `d` 下、上一次的值放在 `was` 下。取值**只能认 `d`**，
> 认错了会把旧值发回去。

#### 坑 C：购物袋也有接口，但相对地址会打错主机

清空和进结账都不用加载那个 259KB 的购物袋页：

```
POST /shop/bagx?_a=delete&_m=shoppingCart.items.item-<uuid>   空体，删一条
POST /shop/bagx/checkout_now?_a=checkout&_m=shoppingCart.actions  空体，进结账
```

都用**购物袋作用域**的令牌（43 字符，`x-aos-model-page: cart`），跟结账页那个
27 字符的不通用。条目 uuid 从购物袋 HTML 里取，只认 `shoppingCart.items.*`——
`bagSavedListItems.*` 是用户的「稍后购买」，误删不好交代。

> **接口清空必须等产品页加载完再做。** `_run` 给 `_drive` 的是一个**全新的空白
> 标签**，而清空购物袋原本排在打开产品页之前——于是 `fetch("/shop/bag")` 是在
> `about:blank` 上发的，直接失败、被当成「袋子是空的」，跳过清空又加了一台，
> 最后袋里两台（2026-09-14 实测）。
>
> 相对地址跟着 `location.origin` 走这件事本身也要防：JS 回报 `location.origin`，
> Python 侧校验它确实是主站，**读数不可信时什么都不做**，而不是按错误读数去决策。
> （顺带澄清一个当时的误判：`secure8.www.apple.com.cn/shop/bag` 其实是能正常
> 返回购物袋的，所以「打到 secureN」不是那次失败的原因，空白页才是。）
>
> 真删了东西之后要**重开产品页**再加购：删购物袋会动服务端的会话状态，而加购
> 要的 `atbtoken` 是这页 JS 现算的，别拿过期的去点。袋子本来就空时（实盘走预热
> 就是这种）什么都不删，也就没有这个开销。

#### 坑 D：比对型号不等于比对数量

「袋里已经是目标型号就跳过加购」这个优化，判断条件写成比对 sku 集合是不够的：
**两台同型号时集合仍然只有一个元素**，照样放行，于是又加一台变成两台。

条件必须是三个都满足：**只有一条 + 型号对 + 数量是 1**（数量在
`itemQuantity.d.quantity`，同一条目也可能是 2）。进结账前还要再校验一次总台数。

顺带修了同一族的另一个洞：`added_to_bag` 是个布尔量，只记「加过了」。监控盯着
十几个配置时，A 色放货加购后失败重试、下一轮命中 B 色，这时清袋和加购都会被
跳过，等于拿 A 色去给 B 色结账。改成记 `bagged_part`（加的是哪个 part），
型号不同就重新清袋加购。

#### 三个必须注意的坑

**0. `selectStore` 只认门店编号（R581），不认名字（五角场）。** 喂名字**不会报错**，
只是静默选不中——然后你以为选了五角场，实际走成了送货。配置里这两者是分开的：
`autobuy.pickup_stores` 是名字（给点页面那条路按文本匹配用的），
`autobuy.pickup_store_numbers` / `pickup.stores` 才是编号。拿不到编号时
代码直接跳过快车道，宁可慢也不赌。



**1. `selectBillingOption` 的值不能写死。** 实测是 `installments0001321713` 这样的
ID，随会话/商品变化，必须从第 4 步的响应里抓。认的是银行 logo 的 `labelImageAlt`
（"招商银行"）。分期期数同理，在第 5 步响应的 `installmentOptions.d.options[]` 里，
形如 `{"value": 24, "label": "24 期"}`。

**2. 这正是会被 541 拦的那族端点（见坑 9）。** 改成发包意味着同样的请求打得更快
更密，风控风险比点页面更高。务必保留退避，别把「快」换成「被拉黑」。

#### 自己抓一份：用 Chrome 的 HAR，别用 `hunter record`

### 会话保活：挂几天的话，什么会先掉？

捡漏不像首发——你不知道哪天放货，监控得挂上好几天。那「登录态能挂多久、
靠什么能续」就成了硬问题：放货那一刻才发现会话掉了，前面所有优化都白做。

这一节全是 2026-09-14 用 CDP 实测出来的，**不是推测**。方法很简单：读浏览器的
cookie jar（零请求），做一个动作，再读一次，看到期时间戳跳不跳。

#### Apple 大陆站的会话不是一个东西，是四层

| cookie | 作用 | TTL | 靠什么续 |
|---|---|---|---|
| `as_dc` | 路由到哪台 `secureN` | 2h | **任何请求**，一个 XHR 就够 |
| `as_sfa` | 店面会话标识 | 180d | **只有真实导航 + 跑 JS**；fetch 整个 HTML 都不算 |
| `shld_bt_m` / `shld_bt_ck` | 反爬 shield 令牌 | 20~35min | 有效期内怎么都续不动；**过期后由下次请求重铸** |
| `DES<hash>`（`.idmsa.apple.com.cn`） | 真正的登录凭证 | 15d | 登录时签发 |

对照实验（干等 6 秒不发请求 → 三项全部纹丝不动，基线可靠）：

| 动作 | `as_dc` | `as_sfa` | `shld_bt_*` |
|---|---|---|---|
| 什么都不做 | — | — | — |
| XHR 打 `availability-message` | ✅ 重置为 +2h | ❌ | ❌ |
| `fetch('/shop/bag')` 取 259KB HTML、不跑 JS | ✅ | ❌ | ❌ |
| 真实导航到 `/shop/bag` 并跑完 JS | ✅ | ✅ 重置为 +180d | ❌ |

**越「像浏览器」续得越多，而且是连续梯度，不是二选一。** 把 259KB 的 HTML 整个
下下来都不够——必须真的把页面 JS 跑起来，`as_sfa` 才动。

#### 三个反直觉的点

**1. 大陆站没有 `myacinfo`。** 那是 `.apple.com` 的，大陆走 `.idmsa.apple.com.cn` 的
`DES<hash>`，15 天。所以「登录态」本身比想象中耐命，真正短命的是路由和反爬 cookie。

**2. shield 令牌不续期，但会自愈。** 它在有效期内对任何动作都无反应，过期之后由
下一个请求重新签发（实测从「剩 10 分钟」倒数到期，之后再看变成「剩 117 分钟」）。
所以它不是挂几天的风险点——但这条是从前后两次读数推断的，`session-probe` 记录
`gone` / `reissued` 就是为了直接抓到重铸那一刻。

**3. 登录态只能从渲染后的 DOM 判断。** 购物袋页是 React SPA，fetch 回来的 HTML
外壳里没有任何账号状态——`data-autom` 账号钩子、`isLoggedIn`、`signIn` 链接、
「退出登录」字样，四组正则全部零命中。判据得用渲染后的 DOM：账号入口会是指向
`secureN` 的绝对地址（`https://secure7.www.apple.com.cn/shop/account/home`），
同时页面上「登录」字样为 0。这跟 shield 那条是同一个教训：**fetch 到的 HTML ≠ 跑完 JS 的页面。**

#### `hunter session-probe`：把猜测变成一张表

```bash
# 纯观测，零请求，看自然衰减曲线
.venv/bin/python -m hunter session-probe --every 5m

# 每 30 分钟探一次登录态（会真实导航，因此顺带续上 as_dc/as_sfa），掉了就推送
.venv/bin/python -m hunter session-probe --every 5m --login-every 30m --notify

# 测「轻动作」够不够：每次采样后发一个 API 请求
.venv/bin/python -m hunter session-probe --act xhr --hours 12
```

设计上的一条线：**采样本身零请求**，cookie 直接从 CDP 的 cookie jar 读，不碰 Apple。
只有 `--act` 和 `--login-every` 会发请求，而且每次都在样本里标成 `none+login` 这样的
干预记号——否则读数据的人会把探针自己造成的续期当成 Apple 的自然行为。

每行样本落到 `logs/session-probe-<日期>.samples.jsonl`，**只记 cookie 的到期时间和
值长度，绝不记值本身**（那些等同于凭证，落盘就是把账号写进日志）。

配合 `--notify`：登录态掉了立刻推送，而且只在**状态翻转**时叫一次。这条才是真正
的收益——脚本永远过不了双重认证（验证码在你手机上，脚本不代劳），所以关键不是
「别掉线」，而是**掉线发生在下午三点你有空处理的时候，而不是凌晨放货那一刻**。

### 页面层

#### 坑 3.5：内嵌商品清单里的颜色**永远是英文**

中文站也一样：

```json
{"sku":"MK2M4","partNumber":"MK2M4CH/A","name":"iPhone Duo 256GB Star White"}
```

中文色名在配色图的 `alt` 里，靠 `finish-select-<slug>` 跟商品对起来：

```json
"imageName":"iphone-duo-finish-select-star-white-202609_AV2",
"originalImageName":"…","alt":"星光白色 iPhone Duo，呈折叠状态…"
```

不做这一步，`parts` 存进配置的 note、监控日志、Bark 推送里就全是
`Star White` / `Glacier Blue`，半夜看一眼根本分不清是哪台。

> **slug 不是简单把英文连字符化。** 「Glacier Blue」的 slug 是 `glacier`，
> 不是 `glacier-blue`。所以要从长到短试：先整段颜色，再退到第一个词。
> 对不上就保留英文——宁可英文，也别瞎猜一个中文名出来。

#### 坑 4：SKU 深链只能带出颜色和容量

`/shop/buy-iphone/iphone-18-pro/MJT74CH/A` 能直接落在选好配置的页面，但**「添加到购物袋」按钮是 `disabled` 的**——页面上「折抵换购」和「AppleCare+」两组单选不选，按钮永远点不了。

```
[tradein]    4 个选项，已选 0
[applecare]  2 个选项，已选 0
```

这两组约 6 次点击，正是抢购时最容易手忙脚乱的地方，所以交给脚本。

#### 坑 5：React 重绘会让元素句柄失效

选完选项后整块 DOM 重绘，之前抓到的 `ElementHandle` 立刻 detach：

```
Error: ElementHandle.is_enabled: Element is not attached to the DOM
```

这个 bug 在前几次测试里没暴露，纯粹是重绘时机碰巧错开了——**抢购当天碰上就是直接丢单**。

**修法**：一律用 `locator`（每次操作重新定位），不用 `ElementHandle`。选单选项用 JS 点 `label`，不用 Playwright 的 `click()`（同样会被判「不可点击」然后重试到超时）。

#### 坑 6：限购 2 台，超限时静默卡死

> 每位顾客限购 2 部 iPhone 18 Pro 和 2 部 iPhone 18 Pro Max。

**Apple 超限时点「结账」既不跳转也不弹错，只是原地不动。** 代码会一路空等到超时——实测白烧 35 秒还报不出原因。抢购当天这就是直接出局。

**修法**：`clear_bag_before_add`（默认开）在**预热阶段**就清空购物袋，把问题解决在开卖之前；结账没跳转时也会主动查限购提示并明确报出来。

#### 坑 7：点「结账」会开新标签页

Apple 在**新标签页**打开 `secure8.www.apple.com.cn/shop/checkout?_s=Fulfillment-init`。旧代码还盯着原来的购物袋页，导致取货门店选择在**错误的页面**上执行——购物袋页上也有「五角场」字样，于是**报成功但什么都没选中**（假阳性）。

**修法**：用 `expect_page` 精确捕获新标签页（按「不在旧列表里」判断会被遗留标签页干扰，白等到超时）；取货选择加 URL 守卫，不在 `/shop/checkout` 上就拒绝执行。

#### 坑 8：未登录时结账被重定向到登录墙

```
https://secure8.www.apple.com.cn/shop/signIn?ssi=...
```

早期版本把这种情况报成「✅ 已到结账页」——**假成功**。抢购当天你会以为一切就绪，实际卡在登录页现场输密码等双重认证，那几十秒足够货没了。

**修法**：识别 `signIn` / `idmsa.apple.com`，明确报「⚠️ 卡在登录页」。

#### 坑 9：结账被限流时，表现成「页面不存在」

2026-09-14 实测，最能把人带偏的一个。症状是**在购物袋点「结账」后落到 `/shop/404`**，
看起来像地址错了、型号下架了、slug 写错了——去查这些全是白查。真实链路：

```
POST /shop/bagx/checkout_now              200  ✅ 结账请求发出去了
302  secure7/shop/checkout/start          302  ✅ 跳转正常
GET  secure7/shop/checkout                200  ✅ 结账页真的加载了
GET  secure7/shop/checkoutx/fulfillment   541  ❌ ←← 真正的故障点
→    /shop/404                                 前端拿不到响应，把你踢到这
```

**541 是 Akamai 的拦截码，不是「找不到」。** 结账页本身开得好好的，是它自己发的
那个 `selectFulfillmentLocationAction` XHR 被边缘节点拦了，React 前端没有兜底
就重定向到了 404 页。于是一个**限流**问题伪装成了**路由**问题。

怎么区分（不用猜）：

| 现象 | 限流 | 真的页面不存在 |
|---|---|---|
| 干净 HTTP 客户端访问同一地址 | 200 | 404 |
| 浏览器里访问**其它**商品页 | 正常 | 正常 |
| 监控接口（`pickup-message` 等） | 可能仍然全 200 | 全 200 |
| `checkoutx/*` 的响应码 | **541 / 503 / 429** | 不会走到这一步 |

最后一行是决定性的：拦截只打在 `checkoutx` 这一族端点上，浏览、购物袋、登录态
可以完全正常，所以「其它都好好的」**不能**用来排除限流。

**修法**：`watch_checkout_block()` 挂响应监听专盯 `/shop/checkoutx/`，拦到 541/503/429
就报「⚠️ 结账被限流（不是页面不存在）」并读出 `Retry-After`。

> **被拦之后最糟的反应是「再换个地址试试」。** 拦截在边缘层，对所有 `secureN`
> 一视同仁——换主机解决不了，只会把退避撞得更深（Akamai 按累计速率判，不是按
> 单个路径判）。所以 `enter()` 的候选循环和 `JS_AOS_POST` 的路径循环都改成了
> **一见拦截立即中断**：原来最多会连撞 8 + 4 = 12 次。
>
> 人也一样：**看到 404 别反复点结账。** 每点一次都在加深退避。停手等它自己解。

### 浏览器接入的三个坑

#### 坑 10：Chrome 136+ 不允许调试默认 profile

[Chrome 136 起，`--remote-debugging-port` 对默认 profile 直接失效](https://developer.chrome.com/blog/remote-debugging-port)——防止攻击者挂上真实 profile 偷 cookie。**你日常在用的那个 Chrome 挂不上去**，必须配一个非默认的 `--user-data-dir`。

**但这不影响你**：Apple 的收货地址和付款方式存在**你的 Apple ID 账号里**，不在浏览器里。在独立 profile 里登录一次 Apple ID，该有的全都有，而且这个 profile 会一直留着。

`connect --launch` 会替你开好。

#### 坑 11：WSL 要够到 Windows Chrome 的调试端口

Windows 侧 Chrome 的调试端口只绑在 **Windows 的 127.0.0.1** 上。WSL 默认（NAT 模式）够不着，需要开镜像网络——在 `%USERPROFILE%\.wslconfig` 里：

```ini
[wsl2]
networkingMode=mirrored
```

改完 `wsl --shutdown` 重启。`hunter connect` 看到 `✓ http://127.0.0.1:9222` 就说明通了。

（也可以干脆用 Linux 侧的 Chrome，WSLg 会正常显示窗口，`--launch` 找不到 Windows Chrome 时会自动用它。）

#### 坑 12：库存三态，绝不把「失败」当「无货」

库存是**有货 / 无货 / 未知**三态。请求被拦、解析失败、响应结构变了——全部归为「未知」并明确报出原因，**绝不折叠成「无货」**。

这不是洁癖。一个把失败当无货的程序看上去一切正常：日志在滚、时间戳在跳，只是永远显示无货，你会以为今天没货然后错过。代码里 `Stock.UNKNOWN` 构造时**强制要求给出原因**，就是为了堵死这条路。

> 猜错成「无货」会让你错过机会，猜错成「未知」只是让你多看一眼。
> 这两种错误的代价完全不对等。

---

## 自动下单的完整流程

```
【开卖前 · 预热】
  打开 /shop/buy-iphone/<机型>/<part>     ← 颜色容量已选好
  JS 点选「不折抵换购」
  JS 点选「不加 AppleCare+ 服务计划」
  清空购物袋（避开限购）
  标签页一直留着 ────────────────────┐
                                      │
【放货瞬间 · 开火】                     │
  点 [data-autom="add-to-cart"]  ←─────┘  省掉 700KB 页面加载
  expect_page 捕获新开的结账标签页
  在结账页尝试选「到店取货 → <门店>」
  停在付款页，响铃叫人                     ← 到此为止
```

取货门店选择的原则：**只在文本精确包含配置的门店名时才点，绝不兜底选第一家**——选错门店比没选更糟。三种结果都会推送给你，失败时明确说「请手动选」，绝不静默假装成功。

---

## 验证状态

| 环节 | 状态 |
|---|---|
| 库存监控（含门店过滤） | ✅ 实测，只报指定门店 |
| 机型上架检测 | ✅ 实测，未上线正确报 404 |
| SKU 深链直达已选配置页 | ✅ 实测 |
| 两个必选项卡住加购按钮 | ✅ 实测确认 |
| 挂到 Windows Chrome 驱动流程 | ✅ 实测（CDP，镜像网络） |
| 预热 → 加购 | ✅ 实测 3.1–4.4s |
| 加购 → 真实结账页 | ✅ 实测，URL 为 `secure8.../shop/checkout` |
| 限购检测与自动清空 | ✅ 实测 |
| 登录墙识别 | ✅ 实测 |
| **取货门店是否真的选中** | ⚠️ **未最终确认**——代码会点，但需要你在付款前肉眼核对页面上确实是那家店 |

---

## 抢购当天的人工准备

工具只解决「及时知道 + 快速送到付款页」，下面这些决定你能不能真的付出去：

1. **跑 `connect --launch` 并在弹出的窗口里登录 Apple ID**，勾选记住我。
2. **收货地址、发票信息提前存好并设为默认**（存在 Apple ID 账号里）。
3. **绑定 Apple Pay 或存好银行卡**，确认额度够、当天没有限额问题。
4. **清空购物袋**（工具会自动做，但自己确认一遍更稳）。
5. **提前想好第二、第三选择**（颜色/容量），首选没货时立刻切。
6. **两条网**：手机 5G + 电脑宽带。
7. **Apple Store App 单独登录一次**做备份，通常比网页快。
8. **别开 VPN/代理**，容易被判风控。
9. **提前 10 分钟挂上 `--sprint`**，让预热跑完。

iPhone 18 Pro 系列的官方时间：2026 年 9 月 12 日北京时间 20:00 开启预购，9 月 18 日正式发售。

**关于首发预购**：中国大陆是**定时开售**，大家都知道时间，拼的是上面这些准备。**工具真正的主场是开售之后**——首批秒光后的补货、门店随机放货，都是随机时点掉出来的，那时候「被叫醒 + 4 秒加购」就是全部。

---

## 用到的接口

都是 Apple 自己前端在调的公开接口，没有绕过任何鉴权或风控：

- `GET /shop/retail/pickup-message?parts.N=<part>&location=<邮编>` — **门店取货库存**（主力）
- `GET /shop/sba/availability-message?parts.N=<part>` — 能否下单、发货时间
- `GET /shop/buy-iphone/<机型>` — 抓 part number 目录 / 判断是否上线
- 直达链接：`/shop/buy-iphone/<机型>/<part>` 跳到已选好该配置的购买页
- 结账页：`secure8.www.apple.com.cn/shop/checkout?_s=Fulfillment-init`

---

## 结构

```
hunter/apple.py     Apple 接口客户端、三态库存、目录解析、直达链接
hunter/autobuy.py   预热 + 半自动下单（挂你自己的 Chrome），停在付款页
hunter/notify.py    各通知渠道 + 打开浏览器
hunter/monitor.py   轮询循环、状态指纹、状态落盘
hunter/checkout.py  结账向导：分步推进、登录判据、限流识别、/shop/404 守卫
hunter/session.py   会话探针：cookie 衰减曲线、登录态监测（采样零请求）
hunter/record.py    录制人工结账：网络事件 + 点击（只记字段名和长度）
hunter/fastpath.py  结账快车道：6 个同源 POST 走完向导，停在 Review
hunter/harscan.py   解析 Chrome 导出的 HAR，抓结账链路首选
hunter/pacing.py    请求节奏：泊松间隔、每小时预算、AIMD 退避、冷热时段
hunter/logbook.py   日志落盘：终端输出按天滚动 + 每个请求一行 jsonl
hunter/__main__.py  命令行入口
config.json         你的配置（已 gitignore）
state.json          上一轮状态，避免重启后重复通知
logs/               运行日志、请求明细、会话采样（已 gitignore）
checkout-trace.jsonl  record 的输出：人工结账每一步做了什么
.browser-profile/   退回模式用的独立 profile（挂你自己的 Chrome 时不用）
```

## 同类项目

- [ENCHIGO/apple-pickup-watcher](https://github.com/ENCHIGO/apple-pickup-watcher) — Rust/Tauri 桌面版，7 个地区，维护活跃。坑 1 和坑 12 最早是从它的 README 里得到的线索。
- [hteen/apple-store-helper](https://github.com/hteen/apple-store-helper) — 中文圈曾经最流行的 Go 版，**已停止维护且因坑 1 实际失效**。
- [insanoid/Apple-Store-Reserve-Monitor](https://github.com/insanoid/Apple-Store-Reserve-Monitor) — 老牌 Python 版，2023 年后未更新。
