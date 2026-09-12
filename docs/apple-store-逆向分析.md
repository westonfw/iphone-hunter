# Apple 中国在线商店接口逆向分析

> 这是一份**观察性**逆向分析：说明 Apple 在线商店各接口的作用、字段含义,以及它的
> 反自动化防护是怎么设计的。目的是「看懂它如何运作」,不是「造一个绕过它的下单机」。
>
> 所有结论都来自对 `apple.com.cn` 前端公开接口的实测(见本仓库 `hunter/apple.py`、
> `hunter/checkout.py` 与 `README.md` 的实测记录),不涉及伪造防护令牌、不涉及自动付款。

---

## 0. 边界与方法

- **只碰公开、只读的接口。** 库存/目录类接口(`availability-message`、`pickup-message`、
  机型页 HTML)本就是浏览器打开页面时会发的请求,拿来观察字段是安全的。
- **写操作(加购、结账、下单)只做「机制分析」,不做「协议复现」。** 这些请求带防自动化
  令牌,复现即绕过,本文只解释它为什么绕不过。
- **绝不涉及付款。** 下面提到的结账链路,分析止于「创建待付款订单」之前的机制层面。

抓包方式:Chrome DevTools 的 Network 面板 + 浏览器内 `fetch` 对照实验(用抓到的参数
重放,看服务端是否认账)。这两步就能把「哪些参数是死的、哪些是活的」区分清楚。

---

## 1. 抓包流程全景:一次「加购 → 结账 → 待付款」都发了什么

把 DevTools 挂在一次真实的人工下单上,请求大致是这个顺序(域名从主站 `www.apple.com.cn`
切到结账专用的 `secure8.www.apple.com.cn`):

```
【产品页 · 主站】
 1. GET  /shop/buy-iphone/<slug>/<part>        ← 产品页 HTML(约 700KB),内含 fnode / as_sfa
 2. GET  /shop/sba/d/init?fnode=&product=      ← 初始化配置会话
 3. GET  /shop/api/purchase-options            ← 折抵/AppleCare 等可选项
 4. GET  /shop/api/applecare/data

【加购 · 主站】
 5. GET  /shop/buy-iphone/<slug>/<part>?add-to-cart=add-to-cart&atbtoken=...
                                               ← 关键:atbtoken 由页面 JS 现算
 6. GET  /shop/bag                             ← 读购物袋,响应里带 x-aos-stk

【结账 · 切到 secure8】
 7. POST /shop/bagx/checkout_now
 8. GET  {secure8}/shop/checkout?_s=Fulfillment-init   ← 结账步骤机入口
 9. POST {secure8}/shop/checkoutx/fulfillment          ← 配送/取货
10. POST {secure8}/shop/checkoutx/...                  ← 联系人、付款方式、检查订单
11.  _s=Review → 确认 → thankyou(等待付款 / 掌上生活扫码 / 30 分钟窗口)
```

几个关键观察:

- **域名会切换。** 结账在 `secure8.www.apple.com.cn` 上进行,是独立的一套会话。点「结账」
  时 Apple 会**新开一个标签页**跳过去(本项目坑 7 就是没跟上这个新标签页导致假成功)。
- **步骤机靠 URL 上的 `_s=` 驱动。** `Fulfillment-init` → `PickupContact-init` →
  付款方式 → `Review`,每一步的 `_s` 值就是当前处在哪一环。见 `checkout.py::step_key()`。
- **加购是个 GET,不是 POST。** 没有 CSRF header,唯一的「门槛」就是那个 `atbtoken`。

---

## 2. 公开只读接口详解

这一节是可以放心研究和使用的部分——这些接口不带防护令牌,前端匿名就能调。

### 2.1 `/shop/sba/availability-message` — 能不能下单 + 发货时间

**用途:** 批量查一组 part number 的「可购买状态」和「预计发货」。

**请求:**
```
GET /shop/sba/availability-message?parts.0=MG6X4CH/A&parts.1=MG724CH/A
```
- `parts.N`:part number,一次可带多个(本项目按 20 个一批切)。

**响应关键字段(`body.content[]`):**
```jsonc
{
  "partNumber": "MG6X4CH/A",
  "deliveryMessage": {
    "subHeader": "iPhone 17 256GB 白色",
    "buyability": { "isBuyable": true, "reason": "" },   // ← 能不能下单
    "deliveryOptionMessages": [ { "displayName": "3-5 个工作日" } ]  // ← 发货时效
  }
}
```

**⚠️ 坑(实测):** 这个接口**也**返回 `availableAtAnyStore` / `partAvailableStoresCount`
两个门店字段,但**不带门店上下文时,对明明有货的机型也恒返回 `false` / `0`**。同一时刻:

```
availability-message  →  availableAtAnyStore=False,门店有货数=0
pickup-message        →  北京 9 家店全部「今天可取货」
```

**所以门店库存绝不能信这个接口,只用它判断「能不能在线下单」和「发货多久」。**
门店一律以下面的 `pickup-message` 为准。

### 2.2 `/shop/retail/pickup-message` — 到店取货库存(门店级真相)

**用途:** 查某个 part 在哪些直营店今天/明天能取货。这是全项目**最可信**的库存源。

**请求:**
```
GET /shop/retail/pickup-message?pl=true&mts.0=regular&parts.0=MG6X4CH/A&location=200000
                                                                        (或 &store=R581)
```
- `location`:**必须是邮政编码**。中文城市名(`location=北京`)会被拒,返回
  `errorMessage: 请输入有效的省/市名称或邮政编码`。`location=100000` 正常返回 9 家店。
- `store`:门店编号(如 `R581`),只查这一家。
- `parts.N`:同样支持多个;Apple 会对**每家门店**返回**全部 part** 的状态——
  所以盯 1 个配置和盯 10 个配置,请求数完全一样。

**响应关键字段(`body.stores[]`):**
```jsonc
{
  "storeNumber": "R581",
  "storeName": "五角场",
  "city": "上海",
  "address": { "address": "..." },
  "partsAvailability": {
    "MG6X4CH/A": {
      "pickupDisplay": "available",          // available / unavailable / 其它=未知
      "pickupSearchQuote": "明天可取货"        // 给用户看的文案
    }
  }
}
```

**三态原则(重要设计):** `pickupDisplay` 只有明确等于 `available` / `unavailable` 才判
有货/无货,**其它任何值、拿不到 `stores`、`errorMessage` 非空——一律归「未知」并记录原因,
绝不折叠成「无货」**。理由:一个把失败当无货的程序看上去一切正常(日志在滚、时间戳在跳),
却永远显示无货,你会以为今天没货然后错过。见 `apple.py::StorePickup.__post_init__`
强制要求 UNKNOWN 必须带原因。

### 2.3 机型目录页 — 从 HTML 抠 part number

**用途:** 拿到某机型的全部配置(part number / 名称 / 价格)。没有专门的目录 JSON,
配置清单直接内嵌在购买页 HTML 里。

**请求:**
```
GET /shop/buy-iphone/<slug>            例如 /shop/buy-iphone/iphone-17
```

**解析:** 页面里有形如下面的内嵌 JSON,用正则抠出来即可(见 `apple.py::catalog`):
```
{"sku":"MG6W4","partNumber":"MG6W4CH/A","price":{"fullPrice":5999.00},...,"name":"iPhone 17 256GB Black"}
```

**上架检测的小技巧:** 机型没上线时,Apple 会 **301 跳回** iPhone 落地页。所以判断「是否
已开卖」不看 200/404,而看**最终 URL 里还在不在 `/shop/buy-iphone/<slug>`**——跳走了就是
还没上线。

### 2.4 频率与拦截

- `pickup-message` 连打 20 次(间隔约 1.2s)全部 200,平均 0.8s,**没有观察到明显限流**。
  轮询间隔 3–5 秒是安全的。
- 边缘节点拦截时返回 `403 / 429 / 503 / 541`。合理做法是**指数退避**(本项目最多退到 5 分钟),
  而不是硬打。

---

## 3. 防护令牌分析:为什么写操作复现不了

这一节是纯机制分析。结论先行:**加购和结账各有一道令牌,都由活的浏览器会话现场产生,
脱离页面就失效。想用协议复现,本质就是复刻这两个令牌的生成——那正是防护要防的东西。**

### 3.1 `atbtoken` — 加购令牌(add-to-bag token)

**在哪出现:** 加购请求的 query 参数里。
```
GET /shop/buy-iphone/iphone-17/mg6x4ch/a?product=MG6X4CH%2FA&purchaseOption=fullPrice
    &acpart=none&step=select&atbtoken=<每次点击现算>
```

**生成时机(观察结论):** 它**不在 DOM,也不在任何全局 JS 变量里**。是页面 JS 在你**点击
「添加到购物袋」的那一刻**才计算出来的,和当前页面会话绑定。

**决定性的对照实验:** 把抓到的完整加购 URL(含 `atbtoken`)拿去用 `fetch` 重放——
```
返回:HTTP 200,一个 354KB 的完整页面
购物袋:没有任何变化
```
服务端**收下了请求、回了 200,却拒绝把东西加进袋子**。这说明 `atbtoken` 是**有服务端校验的
一次性/会话绑定令牌**,不是走个形式的固定串。抓来的值对服务端无效。

**这意味着什么:** 要程序化产生一个「服务端认账」的 `atbtoken`,就必须复刻页面 JS 的计算逻辑
(且要带上正确的会话上下文)。这一步就是在绕过 Apple 的反自动化设计——所以本项目**不做**,
改为让**真实浏览器去点那个按钮**,由页面自己算 token。

### 3.2 `x-aos-stk` — 结账会话令牌

**在哪出现:** 切到 `secure8` 后,结账相关的 POST(`checkoutx/fulfillment` 等)带这个
**请求头**。它不在 URL 上。

**存放位置(实测多个来源,见 `checkout.py::JS_AOS_POST` 的 `findStk`):**
```js
document.querySelector('meta[name="x-aos-stk"]').content          // ① meta 标签
document.querySelector('input[name="x-aos-stk"]').value           // ② 隐藏 input
document.documentElement.innerHTML.match(/"xAosStk":"([^"]+)"/)   // ③ 内联在页面 JSON 里
```

**性质:** 它是**结账会话的认证参数**,和当前 `secure8` 会话绑定,由服务端下发到结账页里。
和 `atbtoken` 一样——只能从**活的结账会话**里提取,不能凭空构造。脱离会话或跨会话使用即失效。

### 3.3 `fnode` / `as_sfa` — 会话前置参数

产品页首次加载的 HTML 里会带 `fnode`(唯一且动态)和 `as_sfa`(cookie 类标识)。后续
`sba/d/init`、加购等请求需要它们串起同一条会话。它们同样是「从页面里提取、跟着会话走」的量,
不是算法能离线生成的固定值。

### 3.4 已验证无效的「抄近路」

下面这些都是很自然的绕过尝试,实测**全部无效**(见 `README.md`):

| 尝试 | 结果 |
|---|---|
| URL 预置 `purchaseOption` / `acpart` 免掉点击选项 | ✗ 加购按钮仍是灰的,页面还慢到 19.5s |
| `?pickupStore=R581` / `?store=R581` 预选取货门店 | ✗ 无效 |
| `rtsid=R581` cookie 预置门店 | ✗ 无效 |
| `/shop/bag/add?product=X` 直接加购 | ✗ 返回 `CSRF_ERROR` |
| 重放抓到的 `atbtoken` | ✗ 200 但袋子不变 |

---

## 4. 防护层次总结

Apple 这套在线商店的反自动化,是**分层**的,不是单点:

1. **边缘层(CDN / WAF):** 拦的是**请求本身**,不区分浏览器点击还是 `curl`。抢购时对
   `fulfillment-messages` 这类接口甚至**对所有请求恒返 541 + 拦截页**。换协议、换 UA
   都过不去——因为它根本没到业务逻辑那一层就被挡了。
2. **令牌层(atbtoken / x-aos-stk / fnode):** 写操作必须带**会话绑定、服务端校验**的
   动态令牌。这些令牌由页面 JS 现场生成,脱离活会话即失效。
3. **业务层(限购 / 登录墙):** 每人限购 2 台,超限时点结账**静默卡死**(既不跳转也不报错);
   未登录会被重定向到 `signIn` / `idmsa.apple.com` 登录墙。

**一个关键推论:走协议直发省不掉开销,也绕不过排队。** 加购本来就只是一个 GET,点击和发包
成本一样;真正的开销在**加载产品页那 700KB**——而这页**必须加载**,因为 `atbtoken` 要靠它的
JS 生成。所以「绕过 UI 直接发包」既不更快,也过不了边缘层。

---

## 5. 结论

- **可以研究、可以自动化的部分:** 库存与目录类**只读**接口(`availability-message`、
  `pickup-message`、机型页 HTML)。字段清晰、无令牌、无明显限流,拿来做监控与提醒完全站得住。
- **能看懂但不该复现的部分:** 加购与结账的写操作。它们的门槛(`atbtoken` / `x-aos-stk`)
  是**专门为阻止协议化下单而设计的动态令牌**,复现即绕过。
- **本项目因此选择的路线:** 监控用只读接口;真要下单时,让**真实浏览器**去点按钮、由页面
  自己生成令牌(半自动),而不是伪造令牌发包。付款始终由本人完成。

> 逆向的价值在于**看懂一个系统如何设计防护**,这份文档做的就是这件事。至于把它变成一个
> 绕过防护的下单机——那既跨了技术红线,也会挤占真实消费者的名额,不在本项目范围内。
