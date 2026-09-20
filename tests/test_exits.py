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
        with patch('scout.exits.AppleClient', side_effect=lambda **k: self.client()):
            return ExitPool(CFG, log=lambda *a: None, **kw)

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
        self.assertTrue(p.pick(PATH).direct)

    def test_a_child_adds_an_exit(self):
        p = self.pool()
        self.add(p, 'b')
        self.assertEqual(2, len(p))

    def test_it_rotates_across_exits(self):
        p = self.pool()
        self.add(p, 'b')
        self.add(p, 'c', ip='192.168.1.10')
        got = [p.pick(PATH).id for _ in range(6)]
        self.assertEqual({'direct', 'b', 'c'}, set(got))
        self.assertEqual(2, got.count('direct'), got)

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
            self.assertEqual('b', p.pick(PATH).id)

    def test_all_blocked_means_wait_not_hammer(self):
        """全在静默期里就该等——硬打只会给封禁续期。"""
        p = self.pool()
        self.add(p, 'b')
        self.block(p, 'direct', left=90.0)
        self.block(p, 'b', left=30.0)
        self.assertIsNone(p.pick(PATH))
        self.assertEqual(30.0, p.soonest(PATH))

    def test_breakers_are_per_exit(self):
        """541 是按「出口 IP + 端点」判的，一条被拉黑跟另一条没关系。"""
        p = self.pool()
        self.add(p, 'b')
        self.block(p, 'direct')
        self.assertTrue(p._exits['b'].ready(PATH))

    # ---------- 上下线 ----------

    def test_a_silent_child_is_dropped(self):
        now = [1000.0]
        p = self.pool(clock=lambda: now[0], stale=60.0)
        self.add(p, 'b')
        now[0] = 1100.0
        self.assertEqual(['b'], p.prune())
        self.assertEqual(1, len(p))

    def test_direct_never_goes_stale(self):
        now = [1000.0]
        p = self.pool(clock=lambda: now[0], stale=60.0)
        now[0] = 99999.0
        self.assertEqual([], p.prune())
        self.assertEqual(1, len(p))

    def test_a_child_that_comes_back_is_taken_again(self):
        now = [1000.0]
        p = self.pool(clock=lambda: now[0], stale=60.0)
        self.add(p, 'b')
        now[0] = 1100.0
        p.prune()
        self.add(p, 'b')
        self.assertEqual(2, len(p))

    def test_picking_prunes_first(self):
        """不能把请求发给一台已经不在的机器。"""
        now = [1000.0]
        p = self.pool(clock=lambda: now[0], stale=60.0)
        self.add(p, 'b')
        now[0] = 1100.0
        for _ in range(3):
            self.assertEqual('direct', p.pick(PATH).id)


if __name__ == '__main__':
    unittest.main()
