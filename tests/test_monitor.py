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
