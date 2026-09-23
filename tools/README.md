# tools/rotating-proxy —— 多 IP 轮换出站代理

给 iphone-hunter 的**主程序（scout）刷库存**用的一台代理：一台服务器挂了很多个
公网 IP，代理让每个请求从不同的 IP 出站，摊薄 Apple 对 `(IP + 端点)` 的累计限流。
scout 那边把它配成 `{"rotating": true}` 的出口，就能「只走这台、单线程尽快刷」。

> **轮换口只喂库存监控。** 结账（买手）需要**固定一个 IP 全程不变**——中途换 IP
> 会立刻被 Akamai 判会话作废。买手走 `--buyer` 开出来的固定口，见下面「结账也走它」。

## 它解决什么

- 单个出口 IP 连续打 `pickup-message` 几小时会开始吃 541（按累计速率判）。轮换 IP
  后每个 IP 的速率都很低，limit 摊平，scout 就能 1 秒一轮地刷。
- **单个坏 IP 不连累整体**：选中的 IP 连不上就内部换一个再试，连试几个才回 502。
  不这么做的话，200 个 IP 里坏一个就会触发 scout 那条轮换出口的 30 秒故障退避。

## 前提：IP 得真的能用

这 200 个 IP 必须**真配在网卡上、且回程能路由**。跨子网时把 `rp_filter` 设成 2 或 0，
否则绑了源 IP 也会被内核丢包。建议用**国内 IP**：监控打的是金山云国内 CDN，
延迟低、也更像真人。

不用手工列 + 逐个 curl，直接用 `list-ips.py`：它枚举本机所有 global scope 的
IPv4，**绑每个 IP 当源地址去连 apple.com.cn:443**，连得上才算真能出网（附加 IP
常有「挂着但出不去」的，绑源一试就现原形），并发测、几秒测完，只留通过的：

```bash
python3 tools/list-ips.py               # 枚举 + 验证，能用的打到屏幕，坏的说明原因
python3 tools/list-ips.py -o ips.txt    # 直接写进 ips.txt（只写通过的）
python3 tools/list-ips.py --no-check    # 只枚举、不验证（全列出来）
```

## 快速起

```bash
# 1. IP 池
cp tools/ips.txt.example tools/ips.txt && vim tools/ips.txt   # 换成你的真实 IP

# 2. 密码走环境变量，别写进文件
export ROTPROXY_PASS='一个强密码'

# 3. 跑
python3 tools/rotating-proxy.py --port 8080 --user hunter
```

## 长期跑（Ubuntu 22.04 + systemd）

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin rotproxy
sudo mkdir -p /opt/rotating-proxy
sudo cp tools/rotating-proxy.py /opt/rotating-proxy/
sudo cp tools/ips.txt           /opt/rotating-proxy/          # 你改好的那份
echo "ROTPROXY_PASS=一个强密码" | sudo tee /opt/rotating-proxy/proxy.env
sudo chmod 600 /opt/rotating-proxy/proxy.env
sudo chown -R rotproxy:rotproxy /opt/rotating-proxy
sudo cp tools/rotating-proxy.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rotating-proxy
sudo systemctl status rotating-proxy
journalctl -u rotating-proxy -f          # 看转发/拒绝/坏 IP 统计
```

## scout 那边怎么填

`config.json`：

```json
"link": {
  "exits": [{"id": "pool",
             "proxy": "http://hunter:你的强密码@代理服务器IP:8080",
             "rotating": true}],
  "use_direct": false,
  "use_buyer": false
}
```

`rotating: true` 让主程序对它不做流控；`use_direct: false` 把本机直连摘掉，库存请求
全走这台代理。跑起来后 scout 日志里这条出口显示成 `pool(代理·多IP)`，库存行尾是
`(pool)`。

## 结账也走它：给每个买手一个固定 IP 口

IP 分成两类文件，都是一行一个：

```
tools/ips.txt        监控库存的轮换池
tools/buyer-a.txt    buyerA 结账用的：第一行当前用，后面备用
tools/buyer-b.txt
tools/buyer-c.txt
```

```bash
cp tools/buyer-a.txt.example tools/buyer-a.txt   # 每个账号一份，填自己的 IP
python3 tools/rotating-proxy.py --port 8080 --user hunter \
    --buyer 8081=buyer-a.txt --buyer 8082=buyer-b.txt --buyer 8083=buyer-c.txt \
    --allow 买手A机器的公网IP --allow 买手B和C机器的公网IP
```

不想手敲这一长串就用启动脚本 `tools/run-proxy.sh`：它自动把同目录的 `buyer-*.txt` 按文件名
顺序挂到 8081、8082、8083…，密码、端口、免密来源从同目录的 `proxy.env` 读：

```bash
cat > tools/proxy.env <<'EOT'
ROTPROXY_PASS=强密码
ROTPROXY_ALLOW="买手A机器的公网IP 买手B和C机器的公网IP"
EOT
chmod 600 tools/proxy.env
tools/run-proxy.sh
```

- `--buyer 端口=文件`：**一个账号一个口、一份文件。** 这个端口只从自己文件里的 IP 出站，
  平时用第一行那个，不会自己换。账号之间、账号和 ips.txt 之间的 IP **不能重叠**，重叠拒绝
  启动：结账会话绑 IP，两个账号走同一个 IP 就是同一个 IP 上开两条结账链路；541 也按 IP
  记，一个账号撞的会连累另一个。当前 IP 连不上不会换别的顶上（换了等于把会话作废），只回
  502 并在日志里喊。
- **被 541 之后换 IP**：买手对固定口发 `GET /rotate`，它把当前 IP 标为烧过、切到最久没烧
  的那个、掐断这个口上所有在转的隧道（Chrome 会复用旧隧道，不掐断新请求还从旧 IP 走），
  买手立刻回主站重建会话，静默期从 120~300 秒变成 3 秒。`GET /ip` 看当前。每个账号的
  文件里放两三个 IP 才有得换，只有一行的口被 541 只能干等。
- `--allow IP|CIDR`：这些来源免密。Chrome 的代理设置（--proxy-server / PAC）带不了
  密码，不放行的话每次起 Chrome 都弹密码框。轮换口照旧要密码。控制请求（/ip、/rotate）
  也认 `Authorization: Basic`，所以 `autobuy.proxy` 写成 `http://hunter:密码@IP:8081`
  也行：PAC 只取主机和端口，密码只给控制请求用。
- 固定口多放行了 `cdn-apple.com` / `mzstatic.com`：结账页的静态资源、Apple ID 登录框、
  Apple Pay 脚本在那儿，不放行页面就残缺。

买手那边 `config.json`：

```json
"autobuy": { "proxy": "http://代理服务器IP:8081" }
```

然后 `hunter connect --launch` 重新起 Chrome：它会带一段 PAC，只把 Apple 的域指到这个
口，其余流量直连。已经开着的 Chrome 改不了代理，得重起。买手启动时会读
chrome://version 核对启动参数，没带代理就在日志里喊。

## 安全边界

- 只转 `CONNECT`，只放行 `apple.com.cn` / `apple.com` / `icloud.com.cn` 的 443。
  TLS 在 scout 和 Apple 之间端到端，这台只看字节流，看不见 cookie 也改不了请求。
- Basic 鉴权，常量时间比较。**密码走环境变量**，别提交进仓库。
- 并发有上限（`--max-conns`，默认 128），满了直接拒，不排队占内存。
- 别把 8080 直接暴露到公网无鉴权——它虽然只转 Apple，但仍是一台开放代理。
  能用防火墙只放行 scout 那台的 IP 最稳。

## 性能

负载是 scout 单线程、约 1 秒 1 个请求，这台严重过剩：thread-per-connection 在这个
量级毫无压力，转发是纯 I/O（`selectors` 一个线程管一条连接的两个方向）。真正的成本
是每个请求都要新做一次 TLS 握手——那是「每次换 IP」的固有代价，任何轮换实现都躲不掉。
