import tempfile
import unittest
from pathlib import Path

from hunter.apple import Availability, Stock, StorePickup
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
        self.assertIn("30 分钟", body)

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
        self.assertNotIn("期", body.split("\n")[1])


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
