import unittest
from hunter.apple import AppleClient, MAX_PARTS_PER_QUERY, Stock


class PickupChunkTests(unittest.TestCase):
    """超过 20 个 part 时必须切批查询，绝不能截断。

    原来是 parts[:20]：第 21 个往后既不报错也不进结果，那几个型号放货时
    永远没人叫你。这跟「查询失败不能折叠成无货」是同一类错——程序看起来
    一切正常，恰恰是最危险的形态。
    """

    def setUp(self):
        self.c = AppleClient("cn")
        self.sent = []

        def fake_get(path, params=None, **kw):
            got = [v for k, v in sorted(params.items()) if k.startswith("parts.")]
            self.sent.append(got)
            return {"body": {"stores": [{
                "storeNumber": "R581", "storeName": "五角场", "city": "上海",
                "partsAvailability": {p: {"pickupDisplay": "available"} for p in got},
            }]}}

        self.c._get = fake_get

    def test_every_part_is_queried_when_over_the_limit(self):
        parts = [f"M{i:04d}CH/A" for i in range(28)]
        out = self.c.pickup(parts, location="200000")
        self.assertEqual(2, len(self.sent))
        self.assertEqual(set(parts), set(p for batch in self.sent for p in batch))
        # 结果里每个 part 都在，且都拿到了门店状态
        self.assertEqual(set(parts), set(out))
        self.assertTrue(all(out[p] and out[p][0].state is Stock.AVAILABLE for p in parts))

    def test_single_batch_still_sends_one_request(self):
        parts = [f"M{i:04d}CH/A" for i in range(MAX_PARTS_PER_QUERY)]
        self.c.pickup(parts, location="200000")
        self.assertEqual(1, len(self.sent))

    def test_still_requires_a_location_or_store(self):
        with self.assertRaises(ValueError):
            self.c.pickup(["MJT74CH/A"])


if __name__ == "__main__":
    unittest.main()
