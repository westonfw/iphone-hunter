import tempfile
import unittest
from pathlib import Path

from hunter.apple import (PICKUP_PATH, Availability, CoolingDown, Stock,
                          StorePickup)
from hunter.pacing import Breaker
from hunter.monitor import State, StockWatcher


class StockWatcherNotificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.watcher = StockWatcher.__new__(StockWatcher)
        self.watcher.note_of = {"PART": "测试机型"}
        self.watcher.only_stores = []
        self.watcher.state = State(Path(self.tmp.name) / "state.json")
        self.watcher.log = lambda *_: None
        self.hits = []
        self.sends = []
        self.watcher.hit = lambda *args, **kw: self.hits.append((args, kw))
        self.watcher.bc = type(
            "BroadcasterSpy",
            (),
            {"send": lambda _, *args, **kwargs: self.sends.append((args, kwargs))},
        )()
        self.watcher._buy_url = lambda part: f"https://example.test/{part}"

    def tearDown(self):
        self.tmp.cleanup()

    def test_becoming_buyable_does_not_notify(self):
        self.watcher.state.set("stock:PART", [False, "不可用", ""])

        self.watcher._check_buyable(
            "PART",
            Availability(part="PART", buyable=True, reason="", delivery="今天"),
        )

        self.assertEqual([], self.hits)
        self.assertEqual([], self.sends)
        self.assertEqual([True, "", "今天"], self.watcher.state.get("stock:PART"))

    def test_new_pickup_availability_notifies(self):
        store = StorePickup(
            part="PART",
            store_number="R001",
            store_name="测试直营店",
            city="上海",
            state=Stock.AVAILABLE,
            quote="今天可取货",
        )

        self.watcher._check_pickup("PART", [store])

        self.assertEqual(1, len(self.hits))
        args, kw = self.hits[0]
        self.assertIn("启动时已有货", args[0])
        # 有货的门店要交给自动下单，否则它只会去配置里写死的那家
        self.assertEqual(["测试直营店"], kw["in_stock"])


if __name__ == "__main__":
    unittest.main()


class PaymentAlertTests(unittest.TestCase):
    """订单创建成功、等待付款时的专门提醒。"""

    def setUp(self):
        from hunter.autobuy import BuyResult
        self.BuyResult = BuyResult
        self.w = StockWatcher.__new__(StockWatcher)
        self.w.cfg = {"autobuy": {"payment_method": "招商银行",
                                  "installment_months": 24}}
        self.w.log = lambda *_: None
        self.sends = []
        self.w.bc = type("B", (), {
            "send": lambda _s, *a, **k: self.sends.append((a, k))})()

    def fire(self, **kw):
        r = self.BuyResult(True, "已创建待付款订单",
                           "https://secure7.www.apple.com.cn/shop/checkout/thankyou",
                           "订单号 W1452578807。", order_id="W1452578807", **kw)
        self.w._notify_pay(r, "🚨 刚放货：iPhone 18 Pro Max 256GB 银色")
        return self.sends[0]

    def test_title_says_go_pay_with_order_id(self):
        (title, body, url), kw = self.fire()
        self.assertIn("去付款", title)
        self.assertIn("W1452578807", title)

    def test_body_names_the_model_and_payment(self):
        (title, body, url), kw = self.fire()
        self.assertIn("iPhone 18 Pro Max 256GB 银色", body)
        self.assertIn("招商银行 24 期", body)
        self.assertIn("以订单页面为准", body)

    def test_links_to_checkout_not_the_product_page(self):
        # 点错了会跳去再买一台
        (title, body, url), kw = self.fire()
        self.assertIn("/shop/checkout", url)
        self.assertNotIn("buy-iphone", url)

    def test_is_critical_so_it_breaks_through_silent_mode(self):
        (title, body, url), kw = self.fire()
        self.assertTrue(kw.get("critical"))

    def test_works_without_installments(self):
        self.w.cfg["autobuy"]["installment_months"] = 0
        (title, body, url), kw = self.fire()
        self.assertIn("招商银行", body)
        self.assertNotIn("24 期", body)


class WatchEnabledTests(unittest.TestCase):
    """watch 条目的 enabled 开关。盯着一堆用不上的型号既白烧请求预算，
    又让日志刷满噪音，真正在等的那个反而看不见。"""

    def test_skips_disabled_entries(self):
        from hunter.monitor import watch_items
        cfg = {"watch": [{"part": "A", "enabled": True},
                         {"part": "B", "enabled": False},
                         {"part": "C", "enabled": True}]}
        self.assertEqual(["A", "C"], [i["part"] for i in watch_items(cfg)])

    def test_missing_flag_means_enabled(self):
        """老配置不写这个字段，不能因为加了开关就静默停掉别人的监控。"""
        from hunter.monitor import watch_items
        cfg = {"watch": [{"part": "A"}, {"part": "B", "enabled": False}]}
        self.assertEqual(["A"], [i["part"] for i in watch_items(cfg)])

    def test_only_enabled_false_can_switch_off(self):
        """别把 0/""/None 之类也当成关闭——只认显式的 false。"""
        from hunter.monitor import watch_items
        cfg = {"watch": [{"part": "A", "enabled": 1}, {"part": "B", "enabled": "yes"}]}
        self.assertEqual(["A", "B"], [i["part"] for i in watch_items(cfg)])

    def test_can_ask_for_everything(self):
        from hunter.monitor import watch_items
        cfg = {"watch": [{"part": "A"}, {"part": "B", "enabled": False}]}
        self.assertEqual(2, len(watch_items(cfg, only_enabled=False)))

    def test_drops_entries_without_a_part(self):
        from hunter.monitor import watch_items
        cfg = {"watch": [{"note": "只是条注释"}, {"part": "A"}, "不是字典"]}
        self.assertEqual(["A"], [i["part"] for i in watch_items(cfg)])


class PickupCoolingDownTests(unittest.TestCase):
    """门店接口熔断时，这一轮该做什么、更重要的是**不该**做什么。

    背景见 pacing.Breaker：541 是端点级的，pickup 被拦时 availability 照常通。
    所以静默期里不能整轮空转，但也绝不能把「没查」写成「没货」。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        w = StockWatcher.__new__(StockWatcher)
        w.parts = ["PART"]
        w.part_groups = {"g": ["PART"]}
        w.note_of = {"PART": "测试机型"}
        w.only_stores = []
        w.state = State(Path(self.tmp.name) / "state.json")
        w.logs = []
        w.log = w.logs.append
        w.pickup_on = True
        w.location = "200000"
        w.avail_every = 6
        w.round_no = 0
        w.autobuy = None
        w.warm_enabled = False
        w.autobuy_done = False
        w._ensure_warm = lambda: None
        self.checked = []
        w._check_pickup = lambda part, stores: self.checked.append((part, stores))
        self.buyable = []
        w._check_buyable = lambda part, av: self.buyable.append((part, av))
        self.w = w

    def tearDown(self):
        self.tmp.cleanup()

    def _client(self, cooling: bool):
        br = Breaker(clock=lambda: 0.0)
        if cooling:
            br.trip()
        outer = self

        class ClientStub:
            requests_made = 0

            def breaker(self, path):
                return br

            def availability(self, parts):
                outer.avail_calls = getattr(outer, "avail_calls", 0) + 1
                return {p: Availability(part=p, buyable=True) for p in parts}

            def pickup(self, parts, location="", store=""):
                if cooling:
                    raise CoolingDown(PICKUP_PATH, br.left())
                return {p: [] for p in parts}

        return ClientStub()

    def test_cooling_down_never_looks_like_out_of_stock(self):
        """熔断这一轮必须完全跳过门店判定。

        走进 _check_pickup 就会拿空列表当成「查过了，都没货」，于是程序看起来
        一切正常、状态也被写进 state.json，真放货时反而不叫你——这是这个项目
        最不能犯的一类错。
        """
        self.w.client = self._client(cooling=True)

        self.w.run()

        self.assertEqual([], self.checked)
        self.assertIsNone(self.w.state.get("pickup:PART"))
        self.assertTrue(any("熔断中" in line for line in self.w.logs))

    def test_cooling_down_does_not_increase_optional_query_rate(self):
        """静默期里 availability 要顶上，不能整轮空转。

        它从没被拦过（同期 89 次请求 0 次 541），是这段时间唯一的信息源。
        注意第 1 轮本来就会查 availability，所以这里要跑到第 2 轮才说明问题。
        """
        self.w.client = self._client(cooling=True)
        self.w.run()
        self.w.run()

        self.assertEqual(1, self.avail_calls)
        self.assertEqual(1, len(self.buyable))

    def test_normal_round_still_skips_availability(self):
        """没熔断时 availability 照旧降频，别把预算白花一半。"""
        self.w.client = self._client(cooling=False)
        self.w.run()      # 第 1 轮打个底
        self.w.run()      # 第 2 轮只查门店

        self.assertEqual(1, self.avail_calls)
        self.assertEqual(2, len(self.checked))
