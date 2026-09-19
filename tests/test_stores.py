import unittest
from pathlib import Path

from hunter.autobuy import AutoBuy, _store_list


def make(cfg):
    return AutoBuy(cfg, Path("/tmp"), log=lambda *_: None)


class StoreListTests(unittest.TestCase):
    def test_accepts_list_and_legacy_string(self):
        self.assertEqual(["五角场", "浦东"], _store_list(["五角场", "浦东"], None))
        self.assertEqual(["五角场"], _store_list(None, "五角场"))

    def test_splits_comma_separated_legacy_value(self):
        self.assertEqual(["五角场", "浦东"], _store_list(None, "五角场，浦东"))

    def test_dedupes_and_keeps_order(self):
        self.assertEqual(["五角场", "浦东"],
                         _store_list(["五角场", " 浦东 ", "五角场"], "浦东"))

    def test_empty_means_no_preference(self):
        self.assertEqual([], _store_list(None, ""))


class StoreCandidateTests(unittest.TestCase):
    ALLOW = ["五角场", "南京东路", "浦东", "上海环贸 iapm"]

    def test_in_stock_store_jumps_the_queue(self):
        ab = make({"pickup_stores": self.ALLOW})
        # 只有浦东放货：它必须排第一，哪怕配置里五角场在前
        self.assertEqual("浦东", ab.store_candidates(["浦东"])[0])

    def test_several_in_stock_keep_configured_preference(self):
        ab = make({"pickup_stores": self.ALLOW})
        got = ab.store_candidates(["浦东", "五角场"])
        self.assertEqual(["五角场", "浦东"], got[:2])

    def test_unmatched_display_name_does_not_hide_in_stock_store(self):
        ab = make({"pickup_stores": self.ALLOW})
        got = ab.store_candidates(["静安"])
        self.assertEqual(["静安"] + self.ALLOW, got)

    def test_empty_allowlist_takes_whatever_has_stock(self):
        ab = make({"pickup_stores": []})
        self.assertEqual(["静安", "七宝"], ab.store_candidates(["静安", "七宝"]))

    def test_no_stock_info_falls_back_to_configured_order(self):
        ab = make({"pickup_stores": self.ALLOW})
        self.assertEqual(self.ALLOW, ab.store_candidates(None))

    def test_legacy_single_store_still_works(self):
        ab = make({"pickup_store_name": "五角场"})
        self.assertEqual(["五角场"], ab.store_candidates(None))

    def test_partial_name_match_counts_as_in_stock(self):
        # Apple 返回的 storeName 可能带前缀，配置里写的是短名
        ab = make({"pickup_stores": ["上海环贸 iapm"]})
        self.assertEqual(["上海环贸 iapm"],
                         ab.store_candidates(["Apple 上海环贸 iapm"]))


if __name__ == "__main__":
    unittest.main()


class PickupSwitchTests(unittest.TestCase):
    """结账页「切到取货」的文案匹配。

    页面按钮是 2026-09-13 从真实结账页（secure7 Fulfillment-init）读到的。
    """

    REAL_BUTTONS = [
        "显示订单摘要： RMB 6,799", "为我送货", "我要取货", "上海 杨浦区",
        "继续填写送货地址", "送货与取货常见问题解答",
        "我可以到 Apple Store 零售店提取订购的商品吗？",
    ]

    def first_match(self, needles):
        from hunter.checkout import PICKUP_SWITCH
        needles = needles or PICKUP_SWITCH
        for text in self.REAL_BUTTONS:
            if any(n in text for n in needles):
                return text
        return None

    def test_matches_the_real_pickup_button(self):
        self.assertEqual("我要取货", self.first_match(None))

    def test_old_keywords_missed_it_entirely(self):
        # 回归用：改版前那组词一个都匹配不上，会静默走成送货
        self.assertIsNone(
            self.first_match(("到店取货", "零售店取货", "门店取货", "自提")))

    def test_never_matches_the_delivery_button(self):
        from hunter.checkout import PICKUP_SWITCH
        self.assertFalse(any(n in "为我送货" for n in PICKUP_SWITCH))

    def test_bare_keyword_is_ambiguous(self):
        # 裸「取货」在这个页面上同时命中按钮和 FAQ 链接。这次按钮碰巧排在前面，
        # 但那是 DOM 顺序的运气——页面一改版就会点到 FAQ 上。所以不用裸词。
        hits = [t for t in self.REAL_BUTTONS if "取货" in t]
        self.assertGreater(len(hits), 1, "裸词有歧义才是不用它的理由")
        self.assertIn("送货与取货常见问题解答", hits)

        from hunter.checkout import PICKUP_SWITCH
        exact = [t for t in self.REAL_BUTTONS if any(n in t for n in PICKUP_SWITCH)]
        self.assertEqual(["我要取货"], exact, "实际用的这组词必须唯一命中")


class LocalizedColorTests(unittest.TestCase):
    """页面内嵌的商品清单永远是英文（中文站也一样），中文色名只在配色图的 alt 里。
    不做这一步，监控日志和推送里就全是 Star White / Glacier Blue。"""

    COLORS = {"star-white": "星光白色", "night-sky": "夜空色", "glacier": "冰川蓝色",
              "black": "黑色", "burgundy": "勃艮第酒红色"}

    def test_replaces_two_word_color(self):
        from hunter.apple import AppleClient
        self.assertEqual("iPhone Duo 256GB 星光白色",
                         AppleClient._localize("iPhone Duo 256GB Star White", self.COLORS))

    def test_matches_on_first_word_when_slug_is_shorter(self):
        """「Glacier Blue」的 slug 是 glacier，不是 glacier-blue。"""
        from hunter.apple import AppleClient
        self.assertEqual("iPhone 18 Pro 256GB 冰川蓝色",
                         AppleClient._localize("iPhone 18 Pro 256GB Glacier Blue",
                                               self.COLORS))

    def test_single_word_color(self):
        from hunter.apple import AppleClient
        self.assertEqual("iPhone 18 Pro Max 1TB 黑色",
                         AppleClient._localize("iPhone 18 Pro Max 1TB Black", self.COLORS))

    def test_unknown_color_stays_english(self):
        """对不上就原样返回——宁可英文，也别瞎猜一个中文名出来。"""
        from hunter.apple import AppleClient
        self.assertEqual("iPhone 17 256GB Lavender",
                         AppleClient._localize("iPhone 17 256GB Lavender", self.COLORS))

    def test_no_colors_found_is_a_noop(self):
        from hunter.apple import AppleClient
        self.assertEqual("iPhone Duo 256GB Star White",
                         AppleClient._localize("iPhone Duo 256GB Star White", {}))

    def test_extracts_pairs_from_page_markup(self):
        from hunter.apple import AppleClient
        html = ('"imageName":"iphone-duo-finish-select-star-white-202609_AV2",'
                '"originalImageName":"x","alt":"星光白色 iPhone Duo，呈折叠状态"')
        self.assertEqual({"star-white": "星光白色"},
                         AppleClient._localized_colors(html))
