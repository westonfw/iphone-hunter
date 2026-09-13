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

### 页面层

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

### 浏览器接入的三个坑

#### 坑 9：Chrome 136+ 不允许调试默认 profile

[Chrome 136 起，`--remote-debugging-port` 对默认 profile 直接失效](https://developer.chrome.com/blog/remote-debugging-port)——防止攻击者挂上真实 profile 偷 cookie。**你日常在用的那个 Chrome 挂不上去**，必须配一个非默认的 `--user-data-dir`。

**但这不影响你**：Apple 的收货地址和付款方式存在**你的 Apple ID 账号里**，不在浏览器里。在独立 profile 里登录一次 Apple ID，该有的全都有，而且这个 profile 会一直留着。

`connect --launch` 会替你开好。

#### 坑 10：WSL 要够到 Windows Chrome 的调试端口

Windows 侧 Chrome 的调试端口只绑在 **Windows 的 127.0.0.1** 上。WSL 默认（NAT 模式）够不着，需要开镜像网络——在 `%USERPROFILE%\.wslconfig` 里：

```ini
[wsl2]
networkingMode=mirrored
```

改完 `wsl --shutdown` 重启。`hunter connect` 看到 `✓ http://127.0.0.1:9222` 就说明通了。

（也可以干脆用 Linux 侧的 Chrome，WSLg 会正常显示窗口，`--launch` 找不到 Windows Chrome 时会自动用它。）

#### 坑 11：库存三态，绝不把「失败」当「无货」

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
hunter/pacing.py    请求节奏：泊松间隔、每小时预算、AIMD 退避、冷热时段
hunter/logbook.py   日志落盘：终端输出按天滚动 + 每个请求一行 jsonl
hunter/__main__.py  命令行入口
config.json         你的配置（已 gitignore）
state.json          上一轮状态，避免重启后重复通知
logs/               运行日志和请求明细（已 gitignore）
.browser-profile/   退回模式用的独立 profile（挂你自己的 Chrome 时不用）
```

## 同类项目

- [ENCHIGO/apple-pickup-watcher](https://github.com/ENCHIGO/apple-pickup-watcher) — Rust/Tauri 桌面版，7 个地区，维护活跃。坑 1 和坑 11 最早是从它的 README 里得到的线索。
- [hteen/apple-store-helper](https://github.com/hteen/apple-store-helper) — 中文圈曾经最流行的 Go 版，**已停止维护且因坑 1 实际失效**。
- [insanoid/Apple-Store-Reserve-Monitor](https://github.com/insanoid/Apple-Store-Reserve-Monitor) — 老牌 Python 版，2023 年后未更新。
