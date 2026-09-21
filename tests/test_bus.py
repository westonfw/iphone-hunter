"""信号总线：签名、重放、时钟偏差、以及「只传看到了」这条规矩。

这条总线会触发花钱的操作，所以这里的每一条都是安全性测试，不是功能测试。
"""
import json
import socket
import time
import unittest
from unittest.mock import Mock

from hunter2.bus import (WRONG_KIND, Decoder, Receiver, Seen, Sender, Sighting,
                         encode, sign, verify)

KEY = b"test-key-0123456789"


def seen(part="MJYA4CH/A", store="R359", at=1000.0, src="a"):
    return Sighting(part=part, store=store, name="南京东路", at=at, src=src)


class SignatureTests(unittest.TestCase):
    def test_a_good_message_round_trips(self):
        d = Decoder(key=KEY, clock=lambda: 1000.0)
        got, why = d.decode(encode(seen(), KEY))
        self.assertEqual('', why)
        self.assertEqual('MJYA4CH/A', got.part)
        self.assertEqual('R359', got.store)
        self.assertEqual(1000.0, got.at)

    def test_a_forged_message_is_refused(self):
        """局域网不是可信边界——一台被攻陷的设备就在同一个广播域里。"""
        d = Decoder(key=KEY, clock=lambda: 1000.0)
        got, why = d.decode(encode(seen(), b"wrong-key"))
        self.assertIsNone(got)
        self.assertIn('签名', why)

    def test_tampering_with_the_body_breaks_the_signature(self):
        raw = json.loads(encode(seen(), KEY))
        raw['body']['store'] = 'R581'          # 改成另一家店
        d = Decoder(key=KEY, clock=lambda: 1000.0)
        got, why = d.decode(json.dumps(raw).encode())
        self.assertIsNone(got)
        self.assertIn('签名', why)

    def test_no_key_means_the_bus_is_off(self):
        got, why = Decoder(key=b"").decode(encode(seen(), KEY))
        self.assertIsNone(got)
        self.assertIn('HUNTER_BUS_KEY', why)

    def test_verify_uses_a_constant_time_compare(self):
        """普通的 == 会按字节短路，泄漏正确前缀的长度。"""
        import inspect
        from hunter2 import bus
        self.assertIn('compare_digest', inspect.getsource(bus.verify))

    def test_the_signature_does_not_depend_on_key_order(self):
        a = {'v': 2, 'kind': 'seen', 'part': 'P', 'store': 'S'}
        b = {'store': 'S', 'part': 'P', 'kind': 'seen', 'v': 2}
        self.assertEqual(sign(a, KEY), sign(b, KEY))
        self.assertTrue(verify(b, sign(a, KEY), KEY))


class ReplayTests(unittest.TestCase):
    def test_the_same_packet_is_only_taken_once(self):
        """签名只证明「是我们的人发的」，挡不住有人把旧包重发一万遍。"""
        d = Decoder(key=KEY, clock=lambda: 1000.0)
        raw = encode(seen(), KEY)
        self.assertEqual('', d.decode(raw)[1])
        got, why = d.decode(raw)
        self.assertIsNone(got)
        self.assertIn('重放', why)

    def test_two_different_sightings_both_go_through(self):
        d = Decoder(key=KEY, clock=lambda: 1000.0)
        self.assertEqual('', d.decode(encode(seen(), KEY))[1])
        self.assertEqual('', d.decode(encode(seen(store='R581'), KEY))[1])

    def test_a_message_without_a_nonce_is_refused(self):
        body = seen().payload()                       # 故意不加 nonce
        raw = json.dumps({'body': body, 'mac': sign(body, KEY)}).encode()
        got, why = Decoder(key=KEY, clock=lambda: 1000.0).decode(raw)
        self.assertIsNone(got)
        self.assertIn('nonce', why)

    def test_the_nonce_table_does_not_grow_without_bound(self):
        now = [0.0]
        s = Seen(ttl=10.0, clock=lambda: now[0])
        for i in range(5000):
            s.fresh(f'n{i}')
        now[0] = 100.0
        s.fresh('later')
        self.assertLess(len(s._at), 5000)


class ClockTests(unittest.TestCase):
    def test_a_sighting_from_the_future_is_refused(self):
        """跨机器就得对时。拿一个「来自未来」的时刻算新鲜度只会自欺。"""
        d = Decoder(key=KEY, clock=lambda: 1000.0, max_skew=5.0)
        got, why = d.decode(encode(seen(at=1020.0), KEY))
        self.assertIsNone(got)
        self.assertIn('未来', why)

    def test_a_small_skew_is_tolerated(self):
        d = Decoder(key=KEY, clock=lambda: 1000.0, max_skew=5.0)
        self.assertEqual('', d.decode(encode(seen(at=1003.0), KEY))[1])

    def test_a_stale_sighting_is_dropped(self):
        d = Decoder(key=KEY, clock=lambda: 1000.0, max_age=120.0)
        got, why = d.decode(encode(seen(at=800.0), KEY))
        self.assertIsNone(got)
        self.assertIn('旧', why)

    def test_the_senders_own_timestamp_is_kept(self):
        """用收到的时刻会让 candidate_max_age 把过期信号当新鲜的。"""
        d = Decoder(key=KEY, clock=lambda: 1030.0)
        got, _ = d.decode(encode(seen(at=1000.0), KEY))
        self.assertEqual(1000.0, got.at)


class ShapeTests(unittest.TestCase):
    def test_only_sightings_are_carried(self):
        """**不传「没货了」**：对端的失明不是真相，拿来踩刹车会误杀真放货。"""
        body = {'v': 2, 'kind': 'gone', 'part': 'P', 'store': 'S',
                'at': 1000.0, 'nonce': 'x'}
        raw = json.dumps({'body': body, 'mac': sign(body, KEY)}).encode()
        got, why = Decoder(key=KEY, clock=lambda: 1000.0).decode(raw)
        self.assertIsNone(got)
        # 种类不收是「按设计忽略」，返回静默哨兵，不当错误刷屏
        self.assertEqual(WRONG_KIND, why)

    def test_an_unknown_version_is_refused(self):
        body = {'v': 99, 'kind': 'seen', 'part': 'P', 'store': 'S',
                'at': 1000.0, 'nonce': 'x'}
        raw = json.dumps({'body': body, 'mac': sign(body, KEY)}).encode()
        self.assertIsNone(Decoder(key=KEY, clock=lambda: 1000.0).decode(raw)[0])

    def test_garbage_never_raises(self):
        d = Decoder(key=KEY, clock=lambda: 1000.0)
        for raw in (b'', b'{', b'\xff\xfe', b'null', b'{"body":1,"mac":"x"}'):
            got, why = d.decode(raw)
            self.assertIsNone(got)
            self.assertTrue(why)

    def test_part_and_store_are_normalised(self):
        d = Decoder(key=KEY, clock=lambda: 1000.0)
        got, _ = d.decode(encode(seen(part='mjya4ch/a', store='r359'), KEY))
        self.assertEqual('MJYA4CH/A', got.part)
        self.assertEqual('R359', got.store)

    def test_an_unlisted_source_can_be_refused(self):
        d = Decoder(key=KEY, clock=lambda: 1000.0, allow=('sensor-a',))
        self.assertIsNone(d.decode(encode(seen(src='stranger'), KEY))[0])
        self.assertEqual('', d.decode(encode(seen(src='sensor-a'), KEY))[1])


class WireTests(unittest.TestCase):
    """真的走一次 UDP，确认两端能对上。"""

    def test_a_sighting_crosses_the_wire(self):
        port = self._free_port()
        got = []
        rx = Receiver(key=KEY, on_sighting=lambda s, ip: got.append(s),
                      port=port, log=lambda *a: None)
        rx.start()
        try:
            tx = Sender(key=KEY, src='sensor-a', port=port,
                        peers=('127.0.0.1',), log=lambda *a: None)
            tx.send(Sighting(part='MJYA4CH/A', store='R359', at=time.time(),
                             src='sensor-a'))
            tx.close()
            for _ in range(40):
                if got:
                    break
                time.sleep(0.05)
        finally:
            rx.close()
        self.assertEqual(1, len(got), '信号没过来')
        self.assertEqual('MJYA4CH/A', got[0].part)

    def test_a_wrong_kind_packet_is_skipped_without_logging(self):
        """买手收到别的买手的 enlist 是常态，别刷屏——静默跳过、不记日志。"""
        from hunter2.bus import Enlist
        port = self._free_port()
        log = Mock()
        # 这个接收方只收 seen（模拟买手不收 enlist——其实买手收 seen/alive，
        # 这里用 seen 一种就够验「种类不收时静默」）
        rx = Receiver(key=KEY, on_sighting=lambda s, ip: None, port=port,
                      log=log, kinds=('seen',))
        rx.start()
        try:
            tx = Sender(key=KEY, src='buyerB', port=port, peers=('127.0.0.1',),
                        log=lambda *a: None)
            for _ in range(3):
                tx.send(Enlist(id='buyerB', proxy_port=48712, direct=False,
                               at=time.time(), src='buyerB'))
            tx.close()
            time.sleep(0.3)
        finally:
            rx.close()
        self.assertEqual(3, rx.skipped, '种类不收的包应被静默跳过并计数')
        # 一行「丢弃」日志都不该有
        self.assertFalse(any('丢弃' in str(c) for c in log.call_args_list),
                         '种类不收不该刷丢弃日志')

    def test_a_handler_that_raises_does_not_kill_the_receiver(self):
        port = self._free_port()
        log = Mock()
        rx = Receiver(key=KEY, on_sighting=Mock(side_effect=RuntimeError('boom')),
                      port=port, log=log)
        rx.start()
        try:
            tx = Sender(key=KEY, src='a', port=port, peers=('127.0.0.1',),
                        log=lambda *a: None)
            tx.send(Sighting(part='P', store='S', at=time.time(), src='a'))
            tx.close()
            for _ in range(40):
                if rx.taken:
                    break
                time.sleep(0.05)
        finally:
            rx.close()
        self.assertEqual(1, rx.taken)
        self.assertTrue(any('处理信号出错' in str(c) for c in log.call_args_list))

    def test_sending_without_a_key_is_a_no_op(self):
        tx = Sender(key=b'', src='a', port=1, log=lambda *a: None)
        self.assertEqual(0, tx.send(Sighting(part='P', store='S')))

    @staticmethod
    def _free_port() -> int:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(('', 0))
        port = s.getsockname()[1]
        s.close()
        return port


if __name__ == '__main__':
    unittest.main()


class EnlistTests(unittest.TestCase):
    """子程序报到。它**不带自己的 IP**——主程序从 UDP 包的源地址取，
    因为多网卡 / 容器 / WSL 下子程序对自己内网地址的猜测经常是错的。"""

    def enlist(self, **kw):
        from hunter2.bus import Enlist
        base = dict(id='b', proxy_port=48712, direct=False, at=1000.0, src='b')
        base.update(kw)
        return Enlist(**base)

    def decoder(self, **kw):
        base = dict(key=KEY, clock=lambda: 1000.0, kinds=('seen', 'enlist'))
        base.update(kw)
        return Decoder(**base)

    def test_it_round_trips(self):
        got, why = self.decoder().decode(encode(self.enlist(), KEY))
        self.assertEqual('', why)
        self.assertEqual('b', got.id)
        self.assertEqual(48712, got.proxy_port)
        self.assertFalse(got.direct)

    def test_the_same_exit_flag_survives(self):
        got, _ = self.decoder().decode(encode(self.enlist(direct=True), KEY))
        self.assertTrue(got.direct)

    def test_it_carries_no_ip_of_its_own(self):
        """带 IP 的话就得让子程序去猜自己叫什么，那个猜测经常是错的。"""
        body = self.enlist().payload()
        self.assertFalse([k for k in body if 'ip' in k.lower() or 'addr' in k.lower()])

    def test_a_nonsense_port_is_refused(self):
        from hunter2.bus import Enlist, sign
        for port in (0, -1, 70000):
            body = dict(self.enlist().payload(), proxy_port=port, nonce='n')
            raw = json.dumps({'body': body, 'mac': sign(body, KEY)}).encode()
            self.assertIsNone(self.decoder().decode(raw)[0], port)

    def test_a_forged_enlist_is_refused(self):
        """伪造的报到会让主程序把流量转给攻击者的机器。"""
        got, why = self.decoder().decode(encode(self.enlist(), b'wrong'))
        self.assertIsNone(got)
        self.assertIn('签名', why)

    def test_a_buyer_refuses_enlist_by_default(self):
        """能处理的消息种类越少，能出错的地方越少。种类不收返回静默哨兵，
        不当错误刷屏（买手收到别的买手的 enlist 是常态）。"""
        got, why = Decoder(key=KEY, clock=lambda: 1000.0).decode(
            encode(self.enlist(), KEY))
        self.assertIsNone(got)
        self.assertEqual(WRONG_KIND, why)
