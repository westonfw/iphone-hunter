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


class BoostAndSpreadTests(unittest.TestCase):
    """冲刺要真的把每条出口都提上来，新出口也不该被别人的退避拖住。"""

    CFG = {'region': 'cn', 'pacing': {'base_interval': 75, 'min_interval': 4,
                                      'budget_per_hour': 9999, 'burst': 999}}

    def pool(self, now):
        def mk(**k):
            c = Mock()
            c.breakers = {}
            return c
        self._patches = [
            patch('scout.exits.AppleClient', side_effect=mk),
            patch.object(ExitPool, '_proxy_url',
                         side_effect=lambda i, p: f'http://u:k@{i}:{p}')]
        for x in self._patches:
            x.start()
            self.addCleanup(x.stop)
        return ExitPool(self.CFG, log=lambda *a: None, clock=lambda: now[0])

    def test_boost_pulls_every_exit_forward(self):
        """**只改节奏器是不够的。** 备用出口的 due_at 还是按 75 秒排的，
        目标间隔压到 4 秒对它完全没生效，当前这条被拦时也顶不上来。"""
        now = [1000.0]
        p = self.pool(now)
        p.enlist('b', '192.168.1.9', 48712, False)
        p._exits['b'].due_at = now[0] + 75
        p.boost(180)
        self.assertLess(p._exits['b'].due_at - now[0], 10.0,
                        '冲刺没有把备用出口的排期提上来')

    def test_boost_does_not_push_an_earlier_exit_back(self):
        now = [1000.0]
        p = self.pool(now)
        p._exits['direct'].due_at = now[0]
        p.boost(180)
        self.assertLessEqual(p._exits['direct'].due_at, now[0])

    def test_a_newcomer_is_not_held_up_by_someone_elses_backoff(self):
        """旧出口退避 300 秒，新来的健康出口被排到 315 秒后——它立刻就能跑。"""
        now = [1000.0]
        p = self.pool(now)
        p._exits['direct'].due_at = now[0] + 300
        p.enlist('b', '192.168.1.9', 48712, False)
        self.assertLess(p._exits['b'].due_at - now[0], 5.0,
                        '新出口被一条退避中的旧出口拖到后面去了')

    def test_a_newcomer_still_avoids_a_live_exit(self):
        now = [1000.0]
        p = self.pool(now)
        p._exits['direct'].due_at = now[0]
        p.enlist('b', '192.168.1.9', 48712, False)
        self.assertGreater(p._exits['b'].due_at - now[0], 5.0,
                           '跟现有出口撞在一起了')


class ReservedNameTests(unittest.TestCase):
    """direct 是内置直连的名字，谁也不能顶掉它。

    顶掉之后它的 permanent 也一起没了——那个子程序一掉线，池子里可能一条出口
    都不剩，主程序整个停摆。
    """

    def pool(self):
        def mk(**k):
            c = Mock()
            c.breakers = {}
            return c
        for x in (patch('scout.exits.AppleClient', side_effect=mk),
                  patch.object(ExitPool, '_proxy_url',
                               side_effect=lambda i, p: f'http://u:k@{i}:{p}')):
            x.start()
            self.addCleanup(x.stop)
        return ExitPool({'region': 'cn'}, log=lambda *a: None)

    def test_a_child_cannot_take_the_name(self):
        p = self.pool()
        before = p._exits['direct']
        p.enlist('direct', '192.168.1.9', 48712, False)
        self.assertIs(before, p._exits['direct'])
        self.assertEqual(1, len(p))

    def test_the_builtin_stays_permanent(self):
        p = self.pool()
        p.enlist('direct', '192.168.1.9', 48712, False)
        self.assertTrue(p._exits['direct'].permanent)
        self.assertTrue(p._exits['direct'].direct)

    def test_it_says_why(self):
        logs = []
        p = self.pool()
        p.log = logs.append
        p.enlist('direct', '192.168.1.9', 48712, False)
        self.assertTrue(any('direct' in x for x in logs))


class BoostFloorTests(unittest.TestCase):
    """冲刺只压缩「常规间隔」那一段，硬等待一秒都不能少。

    预算还差多少令牌、退避排到了哪儿、熔断还剩多久——三样都是真的要等的。
    拿冲刺去顶限流，正是 541 续期的原因。
    """

    CFG = {'region': 'cn', 'pacing': {'base_interval': 30, 'min_interval': 4,
                                      'budget_per_hour': 90, 'burst': 1}}

    def pool(self, now, cfg=None):
        def mk(**k):
            c = Mock()
            c.breakers = {}
            return c
        for x in (patch('scout.exits.AppleClient', side_effect=mk),
                  patch.object(ExitPool, '_proxy_url',
                               side_effect=lambda i, p: f'http://u:k@{i}:{p}')):
            x.start()
            self.addCleanup(x.stop)
        return ExitPool(cfg or self.CFG, log=lambda *a: None,
                        clock=lambda: now[0])

    def test_the_budget_still_has_to_be_waited_out(self):
        """冲刺会按 `boost_budget_per_hour` 抬高预算，那是配置的意图；
        但**抬高之后的**那点预算还是要等——不能一个令牌都没有就发。"""
        now = [1000.0]
        cfg = {'region': 'cn',
               'pacing': {'base_interval': 30, 'min_interval': 4,
                          'budget_per_hour': 90, 'boost_budget_per_hour': 90,
                          'burst': 1}}
        p = self.pool(now, cfg)
        e = p._exits['direct']
        e.pacer.bucket.tokens = 0.0            # 预算见底，要等一个令牌
        e.due_at = now[0] + 40
        p.boost(180)
        self.assertGreater(e.due_at - now[0], 30.0,
                           '冲刺把预算等待也一起压掉了')

    def test_a_failure_cooldown_is_not_compressed(self):
        now = [1000.0]
        p = self.pool(now)
        e = p._exits['direct']
        e.pacer.bucket.tokens = 99
        p.blocked(e, 30.0)                     # 连不上，晾 30 秒
        floor = e.due_at
        p.boost(180)
        self.assertGreaterEqual(e.due_at, floor,
                                '三十秒的故障冷却被冲刺压成了几秒')

    def test_a_healthy_exit_is_still_pulled_forward(self):
        now = [1000.0]
        p = self.pool(now)
        e = p._exits['direct']
        e.pacer.bucket.tokens = 99
        e.due_at = now[0] + 75
        p.boost(180)
        self.assertLess(e.due_at - now[0], 10.0, '健康的出口没能提上来')

    def test_a_success_clears_the_floor(self):
        now = [1000.0]
        p = self.pool(now)
        e = p._exits['direct']
        e.pacer.bucket.tokens = 99
        p.blocked(e, 30.0)
        p.ok(e)
        self.assertEqual(0.0, e.floor_at, '打通了，上次退避的下限还压着')


class SameExitLogTests(unittest.TestCase):
    """标了同出口的子程序每 20 秒报一次到，那行提示只该出现一次。

    拿「在不在池子里」当「说过没说过」是不行的——它们永远进不了池子，于是
    每次报到都刷一行，一天四千多行，真正的出口变化被埋在里面。
    """

    def pool(self):
        def mk(**k):
            c = Mock()
            c.breakers = {}
            return c
        for x in (patch('scout.exits.AppleClient', side_effect=mk),
                  patch.object(ExitPool, '_proxy_url',
                               side_effect=lambda i, p: f'http://u:k@{i}:{p}')):
            x.start()
            self.addCleanup(x.stop)
        p = ExitPool({'region': 'cn'}, log=lambda *a: None)
        self.logs = []
        p.log = self.logs.append
        return p

    def test_it_says_so_once_not_every_beat(self):
        p = self.pool()
        for _ in range(10):
            p.enlist('buyerA', '192.168.1.9', 0, True)
        self.assertEqual(1, sum('同出口' in x for x in self.logs))

    def test_it_never_becomes_an_exit(self):
        p = self.pool()
        p.enlist('buyerA', '192.168.1.9', 0, True)
        self.assertEqual(1, len(p))

    def test_switching_to_same_exit_removes_the_old_one(self):
        """本来借着口，config 改成同出口之后那条出口不能还留着。"""
        p = self.pool()
        p.enlist('buyerA', '192.168.1.9', 48712, False)
        self.assertEqual(2, len(p))
        p.enlist('buyerA', '192.168.1.9', 0, True)
        self.assertEqual(1, len(p))

    def test_switching_back_says_so_again(self):
        p = self.pool()
        p.enlist('buyerA', '192.168.1.9', 0, True)
        p.enlist('buyerA', '192.168.1.9', 48712, False)
        p.enlist('buyerA', '192.168.1.9', 0, True)
        self.assertEqual(2, sum('同出口' in x for x in self.logs))


class RebalanceTests(unittest.TestCase):
    """出口数一变，冲刺的分摊倍数就要跟着变——加入、掉线、改标同出口都算。"""

    CFG = {'region': 'cn', 'pacing': {'base_interval': 30, 'min_interval': 4,
                                      'budget_per_hour': 220, 'burst': 20}}

    def pool(self, now=None):
        now = now or [1000.0]
        def mk(**k):
            c = Mock(); c.breakers = {}; return c
        for x in (patch('scout.exits.AppleClient', side_effect=mk),
                  patch.object(ExitPool, '_proxy_url',
                               side_effect=lambda i, p: f'http://u:k@{i}:{p}')):
            x.start(); self.addCleanup(x.stop)
        return ExitPool(self.CFG, log=lambda *a: None, clock=lambda: now[0], stale=60.0)

    def shares(self, p):
        return sorted((e.id, e.pacer.boost_share) for e in p._exits.values())

    def test_a_lone_exit_has_share_one(self):
        p = self.pool(); p.boost(180)
        self.assertEqual([('direct', 1.0)], self.shares(p))

    def test_a_second_exit_splits_the_sprint_in_two(self):
        p = self.pool()
        p.enlist('b', '192.168.1.9', 48712, False)
        self.assertEqual([('b', 2.0), ('direct', 2.0)], self.shares(p))

    def test_sprinting_with_two_exits_keeps_each_at_twice_min(self):
        """合并 4s，单条 8s——这才是多出口该有的用法。"""
        p = self.pool()
        p.enlist('b', '192.168.1.9', 48712, False)
        p.boost(180)
        for e in p._exits.values():
            self.assertEqual(8.0, e.pacer.target(), e.id)

    def test_a_dropped_exit_gives_the_share_back(self):
        now = [1000.0]
        p = self.pool(now)
        p.enlist('b', '192.168.1.9', 48712, False)
        now[0] = 1100.0
        p.prune()
        self.assertEqual([('direct', 1.0)], self.shares(p))

    def test_switching_to_same_exit_gives_the_share_back(self):
        p = self.pool()
        p.enlist('b', '192.168.1.9', 48712, False)
        p.enlist('b', '192.168.1.9', 0, True)
        self.assertEqual([('direct', 1.0)], self.shares(p))

    def test_the_spread_period_follows_the_fastest_exit(self):
        """错峰周期按最快的那条算。原来取 dict 第一条（直连），直连一退避，
        周期跟着放大，别的健康出口被越推越远。"""
        p = self.pool()
        p.enlist('b', '192.168.1.9', 48712, False)
        for _ in range(3):
            p._exits['direct'].pacer.on_blocked()
        self.assertEqual(30.0, p._period())


class AllBlockedTests(unittest.TestCase):
    """「全盲」的判据：每条出口都在熔断静默里。"""

    PATH = '/shop/retail/pickup-message'

    def pool(self):
        def mk(**k):
            c = Mock(); c.breakers = {}; return c
        for x in (patch('scout.exits.AppleClient', side_effect=mk),
                  patch.object(ExitPool, '_proxy_url',
                               side_effect=lambda i, p: f'http://u:k@{i}:{p}')):
            x.start(); self.addCleanup(x.stop)
        p = ExitPool({'region': 'cn'}, log=lambda *a: None)
        p.enlist('b', '192.168.1.9', 48712, False)
        return p

    def block(self, p, who, left):
        br = Mock(); br.ready.return_value = False; br.left.return_value = left
        p._exits[who].client.breakers[self.PATH] = br

    def test_nobody_blocked_is_not_blind(self):
        self.assertEqual(0.0, self.pool().all_blocked(self.PATH))

    def test_one_blocked_is_not_blind(self):
        """一条被封另一条顶上——那正是多出口的意义，不算盲。"""
        p = self.pool(); self.block(p, 'direct', 90.0)
        self.assertEqual(0.0, p.all_blocked(self.PATH))

    def test_everyone_blocked_is_blind_and_says_how_long(self):
        p = self.pool()
        self.block(p, 'direct', 90.0); self.block(p, 'b', 30.0)
        self.assertEqual(30.0, p.all_blocked(self.PATH))


class ConfiguredExitTests(PoolTests):
    """config 里直接配的代理出口：不经过任何买手，永不过期。"""

    def cfg(self, **link):
        return dict(CFG, link=link)

    def cpool(self, **link):
        self.now = [1000.0]
        with patch('scout.exits.AppleClient', side_effect=lambda **k: self.client()):
            return ExitPool(self.cfg(**link), log=lambda *a: None,
                            clock=lambda: self.now[0])

    def test_configured_proxies_are_there_from_the_start(self):
        p = self.cpool(exits=['http://u:p@1.2.3.4:8080', 'socks5://5.6.7.8:1080'])
        self.assertEqual(3, len(p))
        self.assertEqual('http://u:p@1.2.3.4:8080', p._exits['proxy1'].proxy)
        self.assertEqual('socks5://5.6.7.8:1080', p._exits['proxy2'].proxy)
        self.assertTrue(p._exits['proxy1'].configured)

    def test_dict_form_names_the_exit(self):
        p = self.cpool(exits=[{'id': 'hk', 'proxy': '1.2.3.4:8080'}])
        self.assertIn('hk', p._exits)
        # 没写 scheme 按 http 代理
        self.assertEqual('http://1.2.3.4:8080', p._exits['hk'].proxy)

    def test_bad_entries_are_skipped_not_fatal(self):
        said = []
        with patch('scout.exits.AppleClient', side_effect=lambda **k: self.client()):
            p = ExitPool(self.cfg(exits=['', 42, {'id': 'direct', 'proxy': 'http://x:1'},
                                        'http://ok:1', {'id': 'dup', 'proxy': 'http://a:1'},
                                        {'id': 'dup', 'proxy': 'http://b:1'}]),
                         log=said.append, clock=lambda: 1000.0)
        self.assertEqual({'direct', 'proxy4', 'dup'}, set(p._exits))
        self.assertEqual('http://a:1', p._exits['dup'].proxy)
        self.assertTrue(any('direct' in x for x in said))

    def test_configured_exits_never_go_stale(self):
        p = self.cpool(exits=['http://1.2.3.4:8080'])
        self.add(p, 'b')
        self.now[0] += 10_000
        p.prune()
        self.assertEqual({'direct', 'proxy1'}, set(p._exits))

    def test_configured_exits_rotate_like_any_other(self):
        p = self.cpool(exits=['http://1.2.3.4:8080'])
        self.ready_now(p)
        picked = set()
        for _ in range(2):
            e = p.pick(PATH)
            picked.add(e.id)
            e.due_at = self.now[0] + 100
        self.assertEqual({'direct', 'proxy1'}, picked)

    def test_a_child_cannot_take_a_configured_name(self):
        p = self.cpool(exits=[{'id': 'hk', 'proxy': 'http://1.2.3.4:8080'}])
        self.add(p, 'hk', ip='10.0.0.9')
        self.assertEqual('http://1.2.3.4:8080', p._exits['hk'].proxy)
        self.assertEqual(2, len(p))

    def test_not_borrowing_keeps_children_out(self):
        said = []
        with patch('scout.exits.AppleClient', side_effect=lambda **k: self.client()):
            p = ExitPool(self.cfg(exits=['http://1.2.3.4:8080'], use_buyer=False),
                         log=said.append, clock=lambda: 1000.0)
        self.add(p, 'b')
        self.add(p, 'b')
        self.assertEqual({'direct', 'proxy1'}, set(p._exits))
        self.assertEqual(1, sum('use_buyer=false' in x for x in said))

    def test_the_old_use_buyer_exits_name_still_works(self):
        """老配置写的是 use_buyer_exits，兼容着读。"""
        with patch('scout.exits.AppleClient', side_effect=lambda **k: self.client()):
            p = ExitPool(self.cfg(exits=['http://1.2.3.4:8080'], use_buyer_exits=False),
                         log=lambda *a: None, clock=lambda: 1000.0)
        self.add(p, 'b')
        self.assertEqual({'direct', 'proxy1'}, set(p._exits))

    def test_use_direct_false_drops_the_builtin(self):
        """只走干净代理：把本机直连从池子里拿掉。"""
        p = self.cpool(exits=['http://1.2.3.4:8080'], use_direct=False)
        self.assertNotIn('direct', p._exits)
        self.assertEqual({'proxy1'}, set(p._exits))

    def test_use_direct_false_still_takes_buyers(self):
        p = self.cpool(exits=['http://1.2.3.4:8080'], use_direct=False)
        self.add(p, 'b')
        self.assertEqual({'proxy1', 'b'}, set(p._exits))

    def test_no_direct_no_proxy_no_buyer_refuses_to_start(self):
        """三样出口全关，主程序等于瞎子——启动就报错，别静默空转。"""
        with self.assertRaises(SystemExit):
            with patch('scout.exits.AppleClient', side_effect=lambda **k: self.client()):
                ExitPool(self.cfg(use_direct=False, use_buyer=False),
                         log=lambda *a: None, clock=lambda: 1000.0)

    def test_no_direct_but_buyers_allowed_starts_empty_with_a_warning(self):
        """关了直连、没配代理，但允许买手：起来时是空池，等买手报到，但要吼一声。"""
        said = []
        with patch('scout.exits.AppleClient', side_effect=lambda **k: self.client()):
            p = ExitPool(self.cfg(use_direct=False, use_buyer=True),
                         log=said.append, clock=lambda: 1000.0)
        self.assertEqual(0, len(p))
        self.assertTrue(any('一条出口都没有' in x for x in said))
        # 空池不能让主循环崩：pick 给 None，soonest 给个正数
        self.assertIsNone(p.pick(PATH))
        self.assertGreater(p.soonest(PATH), 0.0)

    def test_describe_marks_them(self):
        p = self.cpool(exits=['http://1.2.3.4:8080'])
        self.assertIn('proxy1(代理)', p.describe())
        self.assertIn('direct(直连)', p.describe())


class RotatingExitTests(PoolTests):
    """多 IP 轮换代理：不做流控，请求回来立刻发下一个；541 不深退避。"""

    def cfg(self, **link):
        return dict(CFG, link=link)

    def rpool(self, **link):
        self.now = [1000.0]
        with patch('scout.exits.AppleClient', side_effect=lambda **k: self.client()):
            return ExitPool(self.cfg(**link), log=lambda *a: None,
                            clock=lambda: self.now[0])

    def test_rotating_is_parsed_from_the_dict(self):
        p = self.rpool(exits=[{'id': 'pool', 'proxy': 'http://1.2.3.4:8080',
                               'rotating': True}])
        self.assertTrue(p._exits['pool'].rotating)

    def test_multi_ip_is_an_alias(self):
        p = self.rpool(exits=[{'id': 'pool', 'proxy': 'http://1.2.3.4:8080',
                               'multi_ip': True}])
        self.assertTrue(p._exits['pool'].rotating)

    def test_a_plain_proxy_is_not_rotating(self):
        p = self.rpool(exits=['http://1.2.3.4:8080'])
        self.assertFalse(p._exits['proxy1'].rotating)

    def test_done_fires_the_next_request_immediately(self):
        p = self.rpool(exits=[{'id': 'pool', 'proxy': 'http://1.2.3.4:8080',
                               'rotating': True}], use_direct=False)
        e = p._exits['pool']
        self.now[0] = 2000.0
        p.done(e, cost=1.0)
        self.assertEqual(2000.0, e.due_at)   # 立刻可用，不推后

    def test_a_541_retries_at_once_not_after_a_long_cooldown(self):
        p = self.rpool(exits=[{'id': 'pool', 'proxy': 'http://1.2.3.4:8080',
                               'rotating': True}], use_direct=False)
        e = p._exits['pool']
        self.now[0] = 2000.0
        p.blocked(e, retry_after=0.0, cost=1.0)   # pickup 的 541 通常没 Retry-After
        self.assertEqual(2000.0, e.due_at)

    def test_a_dead_proxy_still_backs_off(self):
        p = self.rpool(exits=[{'id': 'pool', 'proxy': 'http://1.2.3.4:8080',
                               'rotating': True}], use_direct=False)
        e = p._exits['pool']
        self.now[0] = 2000.0
        p.blocked(e, retry_after=30.0, cost=1.0)   # scout.poll 的 FAIL_COOLDOWN
        self.assertEqual(2030.0, e.due_at)

    def test_rotating_breaker_never_silences(self):
        """轮换出口的熔断冷却为 0：被 541 也照样 ready，不会把整条代理停掉。"""
        from scout.exits import ROTATING_BREAKER
        from hunter.pacing import Breaker
        br = Breaker(**ROTATING_BREAKER)
        br.trip(0.0)
        self.assertTrue(br.ready())

    def test_describe_marks_multi_ip(self):
        p = self.rpool(exits=[{'id': 'pool', 'proxy': 'http://1.2.3.4:8080',
                               'rotating': True}])
        self.assertIn('pool(代理·多IP)', p.describe())
