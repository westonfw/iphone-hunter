"""出口池：轮转、熔断隔离、子程序上下线。

「零失明」全靠这里：一条出口被 541 拉黑时跳过它用下一条，而不是整个停摆。
"""
import unittest
from unittest.mock import Mock, patch

from scout.exits import ExitPool

PATH = '/shop/retail/pickup-message'
CFG = {'region': 'cn', 'timeout': 15}


class PoolTests(unittest.TestCase):
    def pool(self, **kw):
        # 不去真开 curl 会话，只要一个带 breakers 的壳
        kw.setdefault('clock', lambda: self.now[0])
        self.now = getattr(self, 'now', [1000.0])
        with patch('scout.exits.AppleClient', side_effect=lambda **k: self.client()):
            return ExitPool(CFG, log=lambda *a: None, **kw)

    def setUp(self):
        self.now = [1000.0]

    def ready_now(self, p):
        """把所有出口的下一次时间拨到现在——这组用例验的是挑谁，不是什么时候挑。"""
        for e in p._exits.values():
            e.due_at = self.now[0]

    @staticmethod
    def client():
        c = Mock()
        c.breakers = {}
        return c

    def add(self, p, who, ip='192.168.1.9', port=48712, direct=False):
        with patch('scout.exits.AppleClient', side_effect=lambda **k: self.client()), \
             patch.object(ExitPool, '_proxy_url',
                          side_effect=lambda i, pt: f'http://u:k@{i}:{pt}'):
            p.enlist(who, ip, port, direct)

    def block(self, p, who, left=90.0):
        """把某条出口按进熔断。"""
        br = Mock()
        br.ready.return_value = False
        br.left.return_value = left
        p._exits[who].client.breakers[PATH] = br

    # ---------- 基本 ----------

    def test_direct_is_always_there(self):
        """一个子程序都没上线时，主程序也得能干活。"""
        p = self.pool()
        self.assertEqual(1, len(p))
        self.ready_now(p)
        self.assertTrue(p.pick(PATH).direct)

    def test_a_child_adds_an_exit(self):
        p = self.pool()
        self.add(p, 'b')
        self.assertEqual(2, len(p))

    def test_it_rotates_across_exits(self):
        p = self.pool()
        self.add(p, 'b')
        self.add(p, 'c', ip='192.168.1.10')
        self.ready_now(p)                     # 三条都到点了
        got = []
        for _ in range(3):
            e = p.pick(PATH)
            got.append(e.id)
            e.due_at = self.now[0] + 999      # 打过的退场，看下一个轮到谁
        self.assertEqual({'direct', 'b', 'c'}, set(got),
                         f'三条出口没轮全：{got}')
        self.assertIsNone(p.pick(PATH), '都打过一轮了，不该还有人到点')

    def test_re_enlisting_from_the_same_place_is_just_a_heartbeat(self):
        p = self.pool()
        self.add(p, 'b')
        first = p._exits['b'].client
        self.add(p, 'b')
        self.assertIs(first, p._exits['b'].client, '不该重建会话')

    def test_moving_to_a_new_port_rebuilds_the_exit(self):
        p = self.pool()
        self.add(p, 'b', port=48712)
        first = p._exits['b'].client
        self.add(p, 'b', port=48999)
        self.assertIsNot(first, p._exits['b'].client)

    # ---------- 同出口标记 ----------

    def test_a_same_exit_child_is_not_added(self):
        """借它的口出去等于绕回自己的网关，白搭一跳。"""
        p = self.pool()
        self.add(p, 'b', direct=True)
        self.assertEqual(1, len(p))

    # ---------- 熔断隔离（零失明） ----------

    def test_a_blocked_exit_is_skipped_not_fatal(self):
        p = self.pool()
        self.add(p, 'b')
        self.block(p, 'direct')
        for _ in range(4):
            self.ready_now(p)
            self.assertEqual('b', p.pick(PATH).id)

    def test_all_blocked_means_wait_not_hammer(self):
        """全在静默期里就该等——硬打只会给封禁续期。"""
        p = self.pool()
        self.add(p, 'b')
        self.block(p, 'direct', left=90.0)
        self.block(p, 'b', left=30.0)
        self.ready_now(p)
        self.assertIsNone(p.pick(PATH))
        self.assertEqual(30.0, p.soonest(PATH))   # 等最早解封的那条

    def test_breakers_are_per_exit(self):
        """541 是按「出口 IP + 端点」判的，一条被拉黑跟另一条没关系。"""
        p = self.pool()
        self.add(p, 'b')
        self.block(p, 'direct')
        self.assertTrue(p._exits['b'].ready(PATH))

    # ---------- 上下线 ----------

    def test_a_silent_child_is_dropped(self):
        now = self.now
        p = self.pool(stale=60.0)
        self.add(p, 'b')
        now[0] = 1100.0
        self.assertEqual(['b'], p.prune())
        self.assertEqual(1, len(p))

    def test_direct_never_goes_stale(self):
        now = self.now
        p = self.pool(stale=60.0)
        now[0] = 99999.0
        self.assertEqual([], p.prune())
        self.assertEqual(1, len(p))

    def test_a_child_that_comes_back_is_taken_again(self):
        now = self.now
        p = self.pool(stale=60.0)
        self.add(p, 'b')
        now[0] = 1100.0
        p.prune()
        self.add(p, 'b')
        self.assertEqual(2, len(p))

    def test_picking_prunes_first(self):
        """不能把请求发给一台已经不在的机器。"""
        now = self.now
        p = self.pool(stale=60.0)
        self.add(p, 'b')
        now[0] = 1100.0
        for _ in range(3):
            self.ready_now(p)
            self.assertEqual('direct', p.pick(PATH).id)


class PhaseTests(unittest.TestCase):
    """几条出口必须岔开。不岔开的话，加一条出口等于白加——
    2026-09-20 两台独立跑就是这样：合并中位 24.4s 而不是 15s，7% 的巡检同时打。
    """

    PATH = '/shop/retail/pickup-message'
    CFG = {'region': 'cn', 'pacing': {'base_interval': 30, 'min_interval': 4,
                                      'budget_per_hour': 9999, 'burst': 999}}

    def run_pool(self, n_children: int, horizon: float = 600.0):
        now = [1000.0]

        def mk(**k):
            c = Mock()
            c.breakers = {}
            return c

        with patch('scout.exits.AppleClient', side_effect=mk), \
             patch.object(ExitPool, '_proxy_url',
                          side_effect=lambda i, p: f'http://u:k@{i}:{p}'):
            pool = ExitPool(self.CFG, log=lambda *a: None, clock=lambda: now[0])
            kids = [f'c{i}' for i in range(n_children)]
            for i, who in enumerate(kids):
                pool.enlist(who, f'192.168.1.{10 + i}', 48712, False)
            seq = []
            for _ in range(60000):
                if now[0] - 1000 > horizon:
                    break
                for i, who in enumerate(kids):
                    pool.enlist(who, f'192.168.1.{10 + i}', 48712, False)  # 心跳
                e = pool.pick(self.PATH)
                if e is None:
                    now[0] += 0.25
                    continue
                seq.append((now[0] - 1000, e.id))
                pool.done(e)
        return seq

    @staticmethod
    def gaps(seq):
        return [seq[i][0] - seq[i - 1][0] for i in range(1, len(seq))]

    def test_two_exits_halve_the_system_interval(self):
        g = sorted(self.gaps(self.run_pool(1)))
        med = g[len(g) // 2]
        self.assertLess(med, 20.0, f'合并中位 {med:.1f}s，没有真的翻倍')
        self.assertGreater(med, 10.0, f'合并中位 {med:.1f}s，比预期还快，是不是超发了')

    def test_each_exit_keeps_its_own_rate(self):
        """合并变快不能靠让单个 IP 打得更凶——那正是会被 541 的原因。"""
        seq = self.run_pool(1)
        per = {}
        for t, who in seq:
            per.setdefault(who, []).append(t)
        for who, ts in per.items():
            g = sorted(ts[i] - ts[i - 1] for i in range(1, len(ts)))
            self.assertGreater(g[len(g) // 2], 24.0, f'{who} 自己打得太快了')

    def test_no_two_exits_fire_at_the_same_moment(self):
        """同一瞬间打两次 = 白费一次。两台独立跑时这个比例是 7%。"""
        g = self.gaps(self.run_pool(1))
        wasted = sum(1 for x in g if x < 3.0) / len(g)
        self.assertLess(wasted, 0.02, f'{wasted:.0%} 的巡检是撞在一起的')

    def test_three_exits_spread_too(self):
        g = sorted(self.gaps(self.run_pool(2)))
        self.assertLess(g[len(g) // 2], 14.0)

    def test_a_newcomer_lands_in_the_biggest_gap(self):
        """插进最空的那段，而不是从此刻起步——从此刻起步就跟现有的撞上了。"""
        now = [1000.0]

        def mk(**k):
            c = Mock()
            c.breakers = {}
            return c

        with patch('scout.exits.AppleClient', side_effect=mk), \
             patch.object(ExitPool, '_proxy_url',
                          side_effect=lambda i, p: f'http://u:k@{i}:{p}'):
            pool = ExitPool(self.CFG, log=lambda *a: None, clock=lambda: now[0])
            pool._exits['direct'].due_at = 1000.0
            pool.enlist('b', '192.168.1.9', 48712, False)
            self.assertGreater(pool._exits['b'].due_at, 1005.0,
                               '新出口跟现有的撞在一起了')


if __name__ == '__main__':
    unittest.main()
