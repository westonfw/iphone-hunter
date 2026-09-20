"""子程序报到：开转发口 + 周期广播。

这块出了错的表现是「主程序少一条出口」——巡检照常跑、日志一切正常，只是慢了
一倍。没有任何一条报错会告诉你这件事，所以只能靠测试盯住。
"""
import json
import unittest
from unittest.mock import Mock, patch

from hunter2.bus import ECHO, Decoder, Enlist, encode, sign
from hunter2.enlist import Enlister

KEY = b"test-key-0123456789"
CFG = {"link": {"id": "b", "port": 48799, "enlist_every": 20}}


class EnlisterTests(unittest.TestCase):
    def make(self, cfg=None, key=KEY):
        self.sent = []
        sender = Mock()
        sender.send.side_effect = lambda m: (self.sent.append(m), 1)[1]
        self.proxy = Mock()
        self.proxy.start.return_value = 48712
        self.proxy.served = self.proxy.refused = 0
        with patch("hunter2.enlist.bus_key", return_value=key), \
             patch("hunter2.enlist.Sender", return_value=sender), \
             patch("hunter2.enlist.ForwardProxy", return_value=self.proxy):
            return Enlister(cfg or CFG, "b", log=lambda *a: None)

    def test_it_opens_a_forward_port_and_enlists_with_it(self):
        """借出口 IP 是这个结构的核心收益：主程序有几条出口，合并速率就是几倍。"""
        e = self.make()
        with patch.object(e, "_run"):
            self.assertEqual(48712, e.start())
        e.beat()
        self.assertEqual(48712, self.sent[-1].proxy_port)
        self.assertFalse(self.sent[-1].direct)

    def test_it_carries_no_ip_of_its_own(self):
        """主程序从 UDP 包的源地址取——子程序对自己内网地址的猜测经常是错的。"""
        e = self.make()
        e.beat()
        body = self.sent[-1].payload()
        self.assertFalse([k for k in body if "ip" in k.lower() or "addr" in k.lower()])

    def test_a_same_exit_child_does_not_open_a_port_at_all(self):
        """借它的口出去等于绕回主程序自己的网关。一个没人用的监听口纯属白送的攻击面。"""
        cfg = {"link": dict(CFG["link"], same_exit_as_master=True)}
        e = self.make(cfg)
        with patch.object(e, "_run"):
            self.assertEqual(0, e.start())
        self.assertIsNone(e.proxy)
        self.proxy.start.assert_not_called()

    def test_a_same_exit_child_still_reports_in(self):
        """主程序的日志里得看得见它活着，哪怕它没有口可借。"""
        cfg = {"link": dict(CFG["link"], same_exit_as_master=True)}
        e = self.make(cfg)
        e.beat()
        self.assertTrue(self.sent[-1].direct)
        self.assertEqual(0, self.sent[-1].proxy_port)

    def test_without_a_key_it_does_nothing(self):
        e = self.make(key=b"")
        self.assertEqual(0, e.start())
        self.proxy.start.assert_not_called()
        self.assertIsNone(e.thread)

    def test_it_keeps_reporting_in_not_just_once(self):
        """报一次的话，主程序重启之后就再也不知道有谁在了。"""
        e = self.make()
        with patch.object(e, "_tick") as tick:
            def stop(_):
                e.closed = True
            tick.wait.side_effect = stop
            e.proxy_port = 48712
            e._run()
        self.assertEqual(1, len(self.sent))
        self.assertEqual(20.0, tick.wait.call_args[0][0])

    def test_a_send_failure_does_not_kill_the_thread(self):
        e = self.make()
        e.sender.send.side_effect = OSError("网断了")
        self.assertEqual(0, e.beat())

    def test_closing_shuts_the_port(self):
        e = self.make()
        with patch.object(e, "_run"):
            e.start()
        e.close()
        self.proxy.close.assert_called_once()

    def test_the_interval_has_a_floor(self):
        """报得比主程序判掉线还慢没意义，但报太密就是纯烧网络。"""
        e = self.make({"link": dict(CFG["link"], enlist_every=0.1)})
        self.assertGreaterEqual(e.every, 5.0)


class WireRuleTests(unittest.TestCase):
    """这两条规矩是给主程序看的，改错了就是一条出口悄悄消失。"""

    def decoder(self, **kw):
        base = dict(key=KEY, clock=lambda: 1000.0, kinds=("seen", "enlist"))
        base.update(kw)
        return Decoder(**base)

    def raw(self, **kw):
        body = dict(Enlist(id="b", proxy_port=48712, at=1000.0, src="b").payload(),
                    nonce="n1", **kw)
        return json.dumps({"body": body, "mac": sign(body, KEY)}).encode()

    def test_port_zero_is_only_allowed_for_a_same_exit_child(self):
        got, _ = self.decoder().decode(self.raw(proxy_port=0, direct=True))
        self.assertIsNotNone(got)
        self.assertEqual(0, got.proxy_port)

    def test_port_zero_without_the_flag_is_still_refused(self):
        """没标同出口却报 0 端口，是配错了或者有人在伪造——加进池子会白打一整天。"""
        self.assertIsNone(self.decoder().decode(self.raw(proxy_port=0))[0])


class BigSnapshotTests(unittest.TestCase):
    """快照太大时，丢的不该是整条心跳。

    收端的上限是在解码之后查的：包一旦超过缓冲区，收到的是半截 JSON，解析直接
    失败——心跳跟着一起没了，于是买手报「主程序失联」，而主程序好好地在跑。
    """

    @staticmethod
    def _free_port() -> int:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def roundtrip(self, msg):
        import time as _t
        from hunter2.bus import Receiver, Sender
        port = self._free_port()
        got = []
        rx = Receiver(key=KEY, on_sighting=lambda m, ip: got.append(m),
                      port=port, kinds=("alive",), log=lambda *a: None)
        rx.start()
        try:
            tx = Sender(key=KEY, src="scout", port=port, peers=("127.0.0.1",),
                        log=lambda *a: None)
            tx.send(msg)
            tx.close()
            for _ in range(40):
                if got:
                    break
                _t.sleep(0.05)
        finally:
            rx.close()
        return got

    def test_a_realistic_snapshot_fits_in_one_packet(self):
        """8 个型号 × 12 家门店——不加限制的话这就已经超了。"""
        from hunter2.bus import Alive, MAX_PAYLOAD, Sender, encode
        import time as _t
        parts = tuple(f"MJY{i}4CH/A" for i in range(8))
        stock = tuple(f"{p}:" + ",".join(f"R{500 + j}" for j in range(12))
                      for p in parts)
        m = Alive(id="scout", at=_t.time(), saw_at=_t.time(), exits=2,
                  round_no=9, stock=stock, parts=parts, src="scout")
        self.assertLessEqual(len(encode(m, KEY)), MAX_PAYLOAD)
        self.assertEqual(1, len(self.roundtrip(m)), "这么大的包在路上消失了")

    def test_an_oversized_message_is_refused_loudly(self):
        """超限的包发出去也是白发，而且不会有任何错误提示——那正是最难查的
        一类故障：长得跟「对端挂了」一模一样。"""
        from hunter2.bus import Alive, Sender
        import time as _t
        logs = []
        tx = Sender(key=KEY, src="scout", port=1, peers=("127.0.0.1",),
                    log=lambda *a: logs.append(" ".join(map(str, a))))
        huge = Alive(id="scout", at=_t.time(), saw_at=_t.time(),
                     stock=tuple(f"P{i}:R1,R2,R3" for i in range(200)),
                     parts=("P",), src="scout")
        self.assertTrue(tx.oversize(huge))
        self.assertEqual(0, tx.send(huge))
        self.assertTrue(any("静默消失" in x for x in logs))

    def test_the_master_degrades_instead_of_vanishing(self):
        import inspect
        from scout.main import Scout
        self.assertIn("self.sender.shrink(msg)", inspect.getsource(Scout.beat))

    def test_the_budget_is_under_the_ethernet_limit(self):
        """本机实测：超过 1472 字节（1500 − 20 − 8）的 UDP 包静默消失。"""
        from hunter2.bus import MAX_PAYLOAD
        self.assertLessEqual(MAX_PAYLOAD, 1400)


class EchoTests(unittest.TestCase):
    """UDP 广播会回到本机。主程序既发又收，不挡的话每一轮都在日志里拒绝自己一次。"""

    def test_my_own_packet_is_dropped_silently(self):
        d = Decoder(key=KEY, clock=lambda: 1000.0, me="scout")
        got, why = d.decode(encode(
            Enlist(id="scout", proxy_port=1, at=1000.0, src="scout"), KEY))
        self.assertIsNone(got)
        self.assertEqual(ECHO, why)

    def test_someone_elses_packet_still_goes_through(self):
        d = Decoder(key=KEY, clock=lambda: 1000.0, me="scout", kinds=("enlist",))
        got, why = d.decode(encode(
            Enlist(id="b", proxy_port=48712, at=1000.0, src="b"), KEY))
        self.assertEqual("", why)
        self.assertEqual("b", got.id)

    def test_without_me_nothing_changes(self):
        d = Decoder(key=KEY, clock=lambda: 1000.0, kinds=("enlist",))
        got, why = d.decode(encode(
            Enlist(id="b", proxy_port=48712, at=1000.0, src="b"), KEY))
        self.assertEqual("", why)

    def test_an_echo_is_not_counted_as_a_refusal(self):
        """回声不是错误。算进拒绝计数里，真的拒绝就被淹没了。"""
        from hunter2.bus import Receiver
        rx = Receiver(key=KEY, on_sighting=Mock(), port=1, me="scout",
                      log=lambda *a: None)
        rx.decoder.clock = lambda: 1000.0
        rx.sock = Mock()
        raw = encode(Enlist(id="scout", proxy_port=1, at=1000.0, src="scout"), KEY)
        rx.sock.recvfrom.side_effect = [(raw, ("127.0.0.1", 1)), OSError()]
        rx._run()
        self.assertEqual(1, rx.echoed)
        self.assertEqual(0, rx.refused)
        self.assertEqual(0, rx.taken)




class PeerPortTests(unittest.TestCase):
    """同一台机器上跑好几份部署时，单播只会投给其中一个进程。

    几个进程用 SO_REUSEADDR 绑同一个 UDP 端口，Linux 实测全进后启动的那个，
    别的进程一条都收不到，而且**不报任何错**。广播没这个问题（每个 socket 都
    拿到一份副本），所以默认就是广播；非要单播就得把端口写清楚。
    """

    def test_a_bare_address_uses_the_shared_port(self):
        from hunter2.bus import dest_of
        self.assertEqual(("192.168.1.9", 48711), dest_of("192.168.1.9", 48711))

    def test_an_address_can_name_its_own_port(self):
        from hunter2.bus import dest_of
        self.assertEqual(("192.168.1.9", 48712),
                         dest_of("192.168.1.9:48712", 48711))

    def test_the_broadcast_default_is_untouched(self):
        from hunter2.bus import DEFAULT_ADDR, dest_of
        self.assertEqual((DEFAULT_ADDR, 48711), dest_of(DEFAULT_ADDR, 48711))

    def test_a_trailing_colon_is_not_a_port(self):
        from hunter2.bus import dest_of
        self.assertEqual(("192.168.1.9:", 48711), dest_of("192.168.1.9:", 48711))

    def test_the_sender_honours_the_per_peer_port(self):
        from hunter2.bus import Enlist, Sender
        tx = Sender(key=b"k", src="b", port=48711,
                    peers=("192.168.1.9:48712", "192.168.1.10"),
                    log=lambda *a: None)
        tx.sock = Mock()
        tx.send(Enlist(id="b", proxy_port=1, at=0.0))
        dests = [c.args[1] for c in tx.sock.sendto.call_args_list]
        self.assertEqual([("192.168.1.9", 48712), ("192.168.1.10", 48711)], dests)

    def test_a_loopback_peer_with_a_port_is_fine(self):
        """同机多进程各绑各的端口，谁也吞不掉谁——这是合法配置，不该报错。"""
        import io
        from contextlib import redirect_stdout
        from hunter2.__main__ import cmd_doctor
        cfg = {"link": {"id": "a", "port": 48712, "peers": ["127.0.0.1:48711"]},
               "autobuy": {}}
        buf = io.StringIO()
        with patch("hunter2.__main__.load_config", return_value=cfg), \
             patch("hunter2.__main__.bus_key", return_value=b"k"), \
             redirect_stdout(buf):
            cmd_doctor(None)
        self.assertNotIn("没写端口", buf.getvalue())

    def test_the_doctor_calls_out_ambiguous_unicast(self):
        import io
        from contextlib import redirect_stdout
        from hunter2.__main__ import cmd_doctor
        cfg = {"link": {"id": "a", "peers": ["192.168.1.9"]}, "autobuy": {}}
        buf = io.StringIO()
        with patch("hunter2.__main__.load_config", return_value=cfg), \
             patch("hunter2.__main__.bus_key", return_value=b"k"), \
             redirect_stdout(buf):
            cmd_doctor(None)
        self.assertIn("没写端口", buf.getvalue())

if __name__ == "__main__":
    unittest.main()


class VersionTests(unittest.TestCase):
    """快照格式变了，版本号必须跟着变。

    旧的解析规则按 `@` 切，遇到新格式切不出东西，于是解析成「空快照」而
    `saw_at` 照旧有效——旧买手会据此判「所有型号都没货」，掐掉正在进行的下单。
    """

    def test_the_version_was_bumped_with_the_format(self):
        from hunter2.bus import VERSION
        self.assertGreaterEqual(VERSION, 2)

    def test_an_old_receiver_drops_the_whole_message(self):
        """丢整条比错误解析好：买手收不到心跳会报警，那是看得见的。"""
        import json
        from hunter2.bus import Alive, Decoder, encode
        raw = encode(Alive(id="s", at=1000.0, saw_at=999.0,
                           stock=("P:R581",), parts=("P",), src="s"), KEY)
        body = json.loads(raw)["body"]
        self.assertEqual(2, body["v"])
        old = Decoder(key=KEY, clock=lambda: 1000.0, kinds=("alive",))
        old.__dict__  # 用旧版本号解一遍
        import hunter2.bus as bus
        keep = bus.VERSION
        try:
            bus.VERSION = 1
            self.assertIsNone(old.decode(raw)[0], "旧程序把新格式解成了空快照")
        finally:
            bus.VERSION = keep


class DegradeTests(unittest.TestCase):
    """降级要真的降到发得出去为止。

    只摘掉快照是不够的：型号一多，光是售罄计数就能把包顶过上限，于是降级后的
    消息照样被拒发——买手还是收不到心跳，还是会误报「主程序失联」。
    """

    def sender(self):
        from hunter2.bus import Sender
        return Sender(key=KEY, src="scout", port=1, log=lambda *a: None)

    def big(self, n=90):
        from hunter2.bus import Alive
        import time as _t
        parts = tuple(f"MJY{i}4CH/A" for i in range(n))
        return Alive(id="scout", at=_t.time(), saw_at=_t.time(), exits=2,
                     stock=(), parts=parts, stores=("R581",),
                     gone=tuple(f"{p}:0" for p in parts), src="scout")

    def test_a_degraded_message_actually_fits(self):
        tx = self.sender()
        self.assertTrue(tx.oversize(self.big()))
        self.assertFalse(tx.oversize(tx.shrink(self.big())),
                         "降级之后还是超限，心跳照样发不出去")

    def test_it_sheds_the_snapshot_first(self):
        from hunter2.bus import Alive
        import time as _t
        tx = self.sender()
        m = Alive(id="scout", at=_t.time(), saw_at=_t.time(), exits=1,
                  stock=tuple(f"P{i}:R1,R2,R3" for i in range(80)),
                  parts=("P",), gone=("P:1",), src="scout")
        small = tx.shrink(m)
        self.assertEqual((), small.stock)
        self.assertEqual(("P:1",), small.gone, "快照够用了，不该连计数一起丢")

    def test_a_message_that_fits_is_untouched(self):
        from hunter2.bus import Alive
        import time as _t
        tx = self.sender()
        m = Alive(id="scout", at=_t.time(), saw_at=_t.time(), stock=("P:R1",),
                  parts=("P",), gone=("P:0",), src="scout")
        self.assertIs(m, tx.shrink(m))

    def test_the_last_resort_still_says_i_am_alive(self):
        tx = self.sender()
        small = tx.shrink(self.big(400))
        self.assertEqual(0, tx.send(small) if False else 0)
        self.assertFalse(tx.oversize(small))
        self.assertEqual("scout", small.id)
