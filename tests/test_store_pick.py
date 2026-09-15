import unittest
from pathlib import Path

from hunter.autobuy import AutoBuy


class InStockStoreRoutingTests(unittest.TestCase):
    """放货的那家店必须真的被选中。

    2026-09-16 静安放货，程序却去试了五角场/南京东路/浦东（三家都没货）。
    两处各有一个 bug，这两组用例分别钉住它们。
    """

    def make(self, **cfg):
        c = {"enabled": True, "pickup_store_numbers": ["R581", "R359", "R389"]}
        c.update(cfg)
        self.logs = []
        return AutoBuy(c, Path("/tmp"), log=self.logs.append)

    # ---------- bug 1：白名单静默吃掉有货门店 ----------

    def test_warns_when_a_stocked_store_is_not_whitelisted(self):
        ab = self.make(pickup_stores=["五角场", "南京东路"])
        out = ab.store_candidates(["静安"])
        self.assertEqual(["五角场", "南京东路"], out)
        self.assertTrue(any("静安" in m and "pickup_stores" in m for m in self.logs),
                        "有货门店被白名单滤掉却不吭声，等于让整单白跑")

    def test_stocked_store_goes_first_when_whitelisted(self):
        ab = self.make(pickup_stores=["五角场", "南京东路", "静安"])
        self.assertEqual(["静安", "五角场", "南京东路"], ab.store_candidates(["静安"]))
        self.assertEqual([], [m for m in self.logs if "⚠️" in m])

    def test_empty_whitelist_means_anything_in_stock(self):
        ab = self.make(pickup_stores=[])
        self.assertEqual(["静安"], ab.store_candidates(["静安"]))

    # ---------- bug 2：门店编号没传到快车道 ----------

    def test_fast_path_gets_the_stocked_store_number_first(self):
        """快车道用 store_numbers[0] 发包，所以有货那家的编号必须排第一。

        原来这里传的是 in_stock（门店名），而 OrderPlacer 用 R\\d+ 过滤，
        名字被整个丢掉——于是永远拿配置里的第一家 R581。
        """
        ab = self.make(pickup_stores=[])
        seen = {}
        ab._drive = lambda *a, **kw: seen.update(kw)
        ab.warmed = True
        ab._ctx = object()
        ab._page = type("P", (), {"url": "", "is_closed": lambda s: False})()

        ab.fire("", in_stock=["静安"], in_stock_numbers=["R678"])

        self.assertEqual(["R678"], seen["in_stock_numbers"])
        # 模拟 _drive 里那行拼接，确认有货的排在配置前面
        merged = [s for s in (seen["in_stock_numbers"] + ab.pickup_store_numbers) if s]
        self.assertEqual("R678", merged[0])


if __name__ == "__main__":
    unittest.main()
