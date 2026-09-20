"""把 watch 接到总线上：时钟换算、回声、过滤、分工。"""
import time
import unittest
from unittest.mock import Mock

from hunter.purchase_worker import Offer
from hunter2.bus import Sighting
from hunter2.link import LinkedWatcher, mono_of, rotate, wall_of


def offer(part='MJYA4CH/A', store='R359', observed=100.0, pri=(0, 0)):
    return Offer(part, store, '南京东路', 'url', part, observed, pri)


class ClockTests(unittest.TestCase):
    """Offer.observed 是单调时钟，跨机器没有意义，必须换算成墙上时钟再传。"""

    def test_a_timestamp_survives_the_round_trip(self):
        now_m, now_w = 5000.0, 1_700_000_000.0
        w = wall_of(4990.0, now_m, now_w)
        self.assertAlmostEqual(now_w - 10, w, places=3)
        back = mono_of(w, now_m, now_w)
        self.assertAlmostEqual(4990.0, back, places=3)

    def test_the_age_is_what_carries_across(self):
        """两台机器的单调时钟毫无关系，能对上的只有「多久以前」。"""
        a_m, a_w = 100.0, 1_700_000_000.0
        b_m, b_w = 999_999.0, 1_700_000_000.0        # B 的单调时钟完全不同
        wall = wall_of(a_m - 7.0, a_m, a_w)          # A 在 7 秒前看到的
        self.assertAlmostEqual(7.0, b_m - mono_of(wall, b_m, b_w), places=3)

    def test_a_skewed_peer_shifts_the_age(self):
        """时钟差多少年龄就偏多少——所以总线那边必须挡掉偏差大的包。"""
        b_m, b_w = 500.0, 1_700_000_000.0
        wall = 1_700_000_000.0 - 3.0 + 30.0          # 对端快 30 秒
        self.assertLess(b_m - mono_of(wall, b_m, b_w), 0)


class RotateTests(unittest.TestCase):
    """8 个型号同时放货时，几份部署不该挤在同一个上。"""

    def test_offset_zero_changes_nothing(self):
        o = offer(pri=(3, 1))
        self.assertIs(o, rotate(o, 0, 8))

    def test_offset_moves_the_preference(self):
        self.assertEqual((4, 1), rotate(offer(pri=(3, 1)), 1, 8).priority)
        self.assertEqual((0, 1), rotate(offer(pri=(7, 1)), 1, 8).priority)

    def test_two_deployments_pick_different_models(self):
        """第 0 号和第 1 号部署对同一批候选的首选不一样。"""
        cands = [offer(part=f'P{i}', pri=(i, 0)) for i in range(4)]
        a = min(cands, key=lambda o: rotate(o, 0, 4).priority)
        b = min(cands, key=lambda o: rotate(o, 1, 4).priority)
        self.assertNotEqual(a.part, b.part)

    def test_a_broken_priority_is_left_alone(self):
        o = Offer('P', 'S', 'S', 'u', 'P', 0.0, ('x', 1))
        self.assertEqual(('x', 1), rotate(o, 1, 4).priority)

    def test_no_span_means_no_rotation(self):
        o = offer(pri=(3, 1))
        self.assertIs(o, rotate(o, 2, 0))


class LinkedWatcherTests(unittest.TestCase):
    def watcher(self, **kw):
        """不跑 __init__，只装配 _heard / _wrap_observe 要用到的那几样。"""
        w = LinkedWatcher.__new__(LinkedWatcher)
        w.log = Mock()
        w.parts = kw.get('parts', ['MJY64CH/A', 'MJYA4CH/A'])
        w.only_stores = kw.get('only_stores', [])
        w.note_of = {}
        w.offset = kw.get('offset', 0)
        w.bus_id = 'a'
        w.sender = kw.get('sender')
        w._buy_url = lambda p: f'https://x/{p}'
        w.purchase_worker = kw.get('worker', Mock())
        return w

    # ---------- 回声 ----------

    def test_a_heard_sighting_is_never_rebroadcast(self):
        """喂的是包过的 observe 的话，A→B→A→B 会无限回声。"""
        worker = Mock()
        sender = Mock()
        w = self.watcher(worker=worker, sender=sender)
        inner = worker.observe                  # 包之前的原始 observe
        w._wrap_observe()                       # 套上广播层
        sender.send.reset_mock()
        w._heard(Sighting(part='MJYA4CH/A', store='R359', at=time.time(), src='b'))
        inner.assert_called_once()              # 买手收到了
        sender.send.assert_not_called()         # 但没有再广播出去

    def test_our_own_sighting_does_go_out(self):
        worker = Mock()
        sender = Mock()
        w = self.watcher(worker=worker, sender=sender)
        w._wrap_observe()
        worker.observe('MJYA4CH/A', [offer(observed=time.monotonic())])
        sender.send.assert_called_once()
        self.assertEqual('MJYA4CH/A', sender.send.call_args.args[0].part)

    def test_the_local_worker_still_gets_fed_first(self):
        worker = Mock()
        w = self.watcher(worker=worker, sender=Mock())
        inner = worker.observe
        w._wrap_observe()
        worker.observe('MJYA4CH/A', [offer()])
        inner.assert_called_once()

    # ---------- 过滤 ----------

    def test_a_model_we_do_not_watch_is_ignored(self):
        w = self.watcher(parts=['MJY64CH/A'])
        w._feed = Mock()
        w._heard(Sighting(part='MJYE4CH/A', store='R359', at=time.time(), src='b'))
        w._feed.assert_not_called()

    def test_a_store_we_do_not_go_to_is_ignored(self):
        w = self.watcher(only_stores=['R581'])
        w._feed = Mock()
        w._heard(Sighting(part='MJYA4CH/A', store='R359', at=time.time(), src='b'))
        w._feed.assert_not_called()

    def test_a_watched_model_and_store_goes_through(self):
        w = self.watcher(only_stores=['R359'])
        w._feed = Mock()
        w._heard(Sighting(part='MJYA4CH/A', store='R359', at=time.time(), src='b'))
        w._feed.assert_called_once()
        part, offers = w._feed.call_args.args[:2]
        self.assertEqual('MJYA4CH/A', part)
        self.assertEqual('R359', offers[0].store)

    def test_only_sightings_are_fed_never_absences(self):
        """_feed 只收一个 offers 列表，从来没有 unavailable——别人的失明不是真相。"""
        w = self.watcher()
        w._feed = Mock()
        w._heard(Sighting(part='MJYA4CH/A', store='R359', at=time.time(), src='b'))
        self.assertEqual(2, len(w._feed.call_args.args))

    def test_the_age_survives_into_the_offer(self):
        """对端 8 秒前看到的，喂进来也得是 8 秒前，不能变成「刚刚」。"""
        w = self.watcher()
        w._feed = Mock()
        w._heard(Sighting(part='MJYA4CH/A', store='R359',
                          at=time.time() - 8.0, src='b'))
        got = w._feed.call_args.args[1][0]
        self.assertAlmostEqual(8.0, time.monotonic() - got.observed, delta=1.0)

    def test_no_worker_means_nothing_to_feed(self):
        w = self.watcher(worker=None)
        w._heard(Sighting(part='MJYA4CH/A', store='R359', at=time.time(), src='b'))

    def test_a_heard_offer_carries_this_deployments_offset(self):
        w = self.watcher(offset=1, parts=['MJY64CH/A', 'MJYA4CH/A'])
        w._feed = Mock()
        w._heard(Sighting(part='MJY64CH/A', store='R359', at=time.time(), src='b'))
        self.assertEqual(1, w._feed.call_args.args[1][0].priority[0])

    # ---------- 降级 ----------

    def test_a_send_failure_never_breaks_the_local_path(self):
        worker = Mock()
        sender = Mock()
        sender.send.side_effect = OSError('网络没了')
        w = self.watcher(worker=worker, sender=sender)
        inner = worker.observe
        w._wrap_observe()
        worker.observe('MJYA4CH/A', [offer()])     # 不抛
        inner.assert_called_once()

    def test_without_a_sender_the_wrapper_is_harmless(self):
        worker = Mock()
        w = self.watcher(worker=worker, sender=None)
        inner = worker.observe
        w._wrap_observe()
        worker.observe('MJYA4CH/A', [offer()])
        inner.assert_called_once()


if __name__ == '__main__':
    unittest.main()
