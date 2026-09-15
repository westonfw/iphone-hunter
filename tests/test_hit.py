import unittest
from pathlib import Path

from hunter.autobuy import SOLD_OUT_MARKS, AutoBuy, BuyResult, _part_of


class PartFromUrlTests(unittest.TestCase):
    BASE = "https://www.apple.com.cn/shop/buy-iphone/iphone-18-pro"

    def test_reads_part_from_buy_url(self):
        self.assertEqual("MJY74CH/A", _part_of(f"{self.BASE}/MJY74CH/A"))

    def test_ignores_query_string(self):
        self.assertEqual("MJY64CH/A", _part_of(f"{self.BASE}/MJY64CH/A?step=attach"))

    def test_empty_when_no_part(self):
        self.assertEqual("", _part_of(self.BASE))
        self.assertEqual("", _part_of(""))


class WarmedPartMismatchTests(unittest.TestCase):
    """回归：预热的是列表第一个型号，放货的可能是任何一个。

    不核对就会「提示银色、袋里进黑色」——用户实际遇到过。
    """

    BLACK = "https://www.apple.com.cn/shop/buy-iphone/iphone-18-pro/MJY64CH/A"
    SILVER = "https://www.apple.com.cn/shop/buy-iphone/iphone-18-pro/MJY74CH/A"

    def make(self, warmed_url):
        ab = AutoBuy({"enabled": True}, Path("/tmp"), log=lambda *_: None)
        ab.warmed = True
        ab._ctx = object()
        ab._page = type("P", (), {"url": warmed_url, "is_closed": lambda s: False})()
        self.drove = []
        ab._drive = lambda ctx, page, url, dry_run, in_stock=None, in_stock_numbers=None: (
            self.drove.append(url) or BuyResult(True, "ok"))
        return ab

    def test_navigates_when_warmed_page_is_a_different_model(self):
        ab = self.make(self.BLACK)
        ab.fire(self.SILVER)
        self.assertEqual([self.SILVER], self.drove,
                         "型号对不上必须跳转，不能拿预热页直接买")

    def test_keeps_the_warm_shortcut_when_it_matches(self):
        ab = self.make(self.SILVER)
        ab.fire(self.SILVER)
        self.assertEqual([None], self.drove, "型号一致才走预热加速")

    def test_no_url_falls_back_to_warm_page(self):
        ab = self.make(self.BLACK)
        ab.fire("")
        self.assertEqual([None], self.drove)


class SoldOutTests(unittest.TestCase):
    def test_known_wordings_are_covered(self):
        for real in ("此商品暂无供应", "目前无法购买", "已售罄"):
            self.assertTrue(any(m in real for m in SOLD_OUT_MARKS), real)

    def test_normal_page_is_not_sold_out(self):
        for ok in ("加入购物袋", "预计送达日期：2026/09/18", "明天 可取货"):
            self.assertFalse(any(m in ok for m in SOLD_OUT_MARKS), ok)

    def test_sold_out_result_is_not_retriable(self):
        r = BuyResult(False, "⚠️ 已经买不到了", "", "", retriable=False)
        self.assertFalse(r.retriable)

    def test_other_failures_stay_retriable(self):
        self.assertTrue(BuyResult(False, "加购按钮不可用").retriable)


if __name__ == "__main__":
    unittest.main()
