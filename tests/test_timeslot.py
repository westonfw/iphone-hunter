"""自提要选的那个「具体时间」。

2026-09-17 的 HAR 里，`continueFromFulfillmentToPickupContact` 比 09-14 那份多出
13 个 `...timeSlot.dateTimeSlots.*` 字段——Apple 给自提加了时段选择。其中
`timeSlotId` / `signKey` 是服务端签发的，编不出来，只能从选门店那一步的响应里
原样取出来回传。这些用例锁的就是「取对、带全、老流程不受影响」。
"""

import unittest

from hunter.fastpath import FastCheckout, Stalled, clock_minutes, slot_minutes
from test_fastpath import BANKS, CONTACT, MONTHS, REVIEW, FakePage, placer, resp


def slot(value, start, end, **kw):
    """造一档时段，字段名跟真实响应一致（大写的 SlotId 是 Apple 自己的写法）。"""
    s = {"SlotId": f"ID-{value}", "Label": f"{start} – {end}", "enabled": True,
         "timeSlotValue": value, "checkInStart": start, "checkInEnd": end,
         "signKey": f"KEY-{value}", "timeZone": "Asia/Shanghai",
         "timeSlotType": "", "isRestricted": None}
    s.update(kw)
    return s


#: 18 号三档、19 号两档。形状照着 checkout.js 里 `p[a][t.dayOfMonth]` 的读法造。
DAY18 = [slot("18-10:00-10:15", "10:00 AM", "10:15 AM"),
         slot("18-12:30-12:45", "12:30 PM", "12:45 PM"),
         slot("18-17:00-17:15", "5:00 PM", "5:15 PM")]
DAY19 = [slot("19-09:30-09:45", "9:30 AM", "9:45 AM")]

SLOT_MODEL = {"d": {
    "dayRadio": "18",
    "pickUpDates": [{"dayOfMonth": "18", "date": "2026-09-18"},
                    {"dayOfMonth": "19", "date": "2026-09-19"}],
    "timeSlotWindows": [{"18": DAY18}, {"19": DAY19}],
}}

#: 时段字段在请求体里的公共前缀。
P = "checkout.fulfillment.pickupTab.pickup.timeSlot.dateTimeSlots."


def ful_with_slots(model=SLOT_MODEL):
    """选门店那一步的响应：fulfillment 这一节里挂着时段模块。"""
    return resp("fulfillment", {"pickupTab": {"pickup": {"timeSlot": {
        "dateTimeSlots": model}}}})


FUL_SLOTS = ful_with_slots()
FUL_PLAIN = resp("fulfillment")          # 2026-09-14 那版：根本没有时段这一节


def fields(chosen) -> dict:
    return dict(FastCheckout.slot_fields(chosen))


class ClockTests(unittest.TestCase):
    def test_reads_twelve_hour_clock(self):
        """响应里的时刻是「12:30 PM」这种写法。按 24 小时制硬读会把下午读成凌晨，
        于是配了 13:00 反而选中最早那档——这一条就是防它。"""
        self.assertEqual(12 * 60 + 30, clock_minutes("12:30 PM"))
        self.assertEqual(17 * 60, clock_minutes("5:00 PM"))
        self.assertEqual(0, clock_minutes("12:00 AM"))
        self.assertEqual(9 * 60 + 30, clock_minutes("9:30 AM"))

    def test_reads_plain_clock(self):
        """用户在 config 里写的是 24 小时制的 13:00。"""
        self.assertEqual(13 * 60, clock_minutes("13:00"))
        self.assertEqual(0, clock_minutes("00:00"))

    def test_rejects_nonsense(self):
        for bad in ("", "中午", "25:00", "12:99", "12", None):
            self.assertEqual(-1, clock_minutes(bad), bad)

    def test_falls_back_to_slot_value(self):
        """checkInStart 缺席时，从 `18-12:30-12:45` 里拆开始时刻。"""
        self.assertEqual(12 * 60 + 30,
                         slot_minutes({"timeSlotValue": "18-12:30-12:45"}))


class CandidateTests(unittest.TestCase):
    def test_flattens_days_in_order(self):
        got = FastCheckout.slot_candidates(FUL_SLOTS["json"])
        self.assertEqual(["18", "18", "18", "19"], [c["dayOfMonth"] for c in got])
        self.assertEqual("2026-09-18", got[0]["date"])
        self.assertEqual("2026-09-19", got[-1]["date"])

    def test_drops_greyed_out_slots(self):
        """enabled:false 是页面上灰掉的那些，选了也结不了账。"""
        model = {"d": {**SLOT_MODEL["d"], "timeSlotWindows": [
            {"18": [slot("18-10:00-10:15", "10:00 AM", "10:15 AM", enabled=False),
                    DAY18[1]]}, {"19": DAY19}]}}
        got = FastCheckout.slot_candidates(ful_with_slots(model)["json"])
        self.assertEqual(["18-12:30-12:45", "19-09:30-09:45"],
                         [c["slot"]["timeSlotValue"] for c in got])

    def test_finds_windows_when_index_does_not_line_up(self):
        """两个列表按下标配对是 Apple 的实现细节，顺序一变不能整条路走不通。"""
        model = {"d": {**SLOT_MODEL["d"],
                       "timeSlotWindows": [{"19": DAY19}, {"18": DAY18}]}}
        got = FastCheckout.slot_candidates(ful_with_slots(model)["json"])
        self.assertEqual(4, len(got))
        self.assertEqual("18", got[0]["dayOfMonth"])

    def test_old_flow_has_no_slots(self):
        self.assertEqual([], FastCheckout.slot_candidates(FUL_PLAIN["json"]))


class ChooseTests(unittest.TestCase):
    def pick(self, want=""):
        return placer(pickup_time=want).choose_slot(FUL_SLOTS["json"])

    def test_default_is_the_earliest(self):
        """抢购场景默认越早拿到越好。"""
        self.assertEqual("18-10:00-10:15", self.pick()["slot"]["timeSlotValue"])
        self.assertEqual("18-10:00-10:15",
                         self.pick("earliest")["slot"]["timeSlotValue"])

    def test_latest_stays_on_the_first_day(self):
        """latest 是「最早那天的最后一档」。真挑到最后一天就等于替人把取货日
        推后了几天，那不是这个工具该做的决定。"""
        self.assertEqual("18-17:00-17:15", self.pick("latest")["slot"]["timeSlotValue"])

    def test_clock_picks_first_slot_not_earlier(self):
        self.assertEqual("18-12:30-12:45", self.pick("12:00")["slot"]["timeSlotValue"])
        self.assertEqual("18-12:30-12:45", self.pick("12:30")["slot"]["timeSlotValue"])
        self.assertEqual("18-17:00-17:15", self.pick("13:00")["slot"]["timeSlotValue"])

    def test_too_late_falls_back_to_last_slot_that_day(self):
        """当天没有更晚的了就退到最后一档，而不是空手而归——
        取不到时段这一步就过不去。"""
        self.assertEqual("18-17:00-17:15", self.pick("23:00")["slot"]["timeSlotValue"])

    def test_garbage_preference_falls_back_to_earliest(self):
        self.assertEqual("18-10:00-10:15", self.pick("下午")["slot"]["timeSlotValue"])

    def test_nothing_to_choose_when_flow_has_no_slots(self):
        self.assertEqual({}, placer().choose_slot(FUL_PLAIN["json"]))


class FieldTests(unittest.TestCase):
    def test_sends_the_same_thirteen_fields_as_the_har(self):
        """字段名逐个对 2026-09-17 的 HAR，少一个都可能让服务端静默不认。"""
        got = fields(placer().choose_slot(FUL_SLOTS["json"]))
        want = {"startTime", "endTime", "displayStartTime", "displayEndTime",
                "timeSlotType", "timeSlotId", "signKey", "timeZone",
                "timeSlotValue", "isRestricted", "date", "dayRadio",
                "isRecommended"}
        self.assertTrue(all(k.startswith(P) for k in got), got)
        self.assertEqual(want, {k[len(P):] for k in got})

    def test_values_come_from_the_server(self):
        got = fields(placer(pickup_time="12:30").choose_slot(FUL_SLOTS["json"]))
        self.assertEqual("ID-18-12:30-12:45", got[P + "timeSlotId"])
        self.assertEqual("KEY-18-12:30-12:45", got[P + "signKey"])
        self.assertEqual("12:30 PM", got[P + "startTime"])
        self.assertEqual("12:45 PM", got[P + "endTime"])
        self.assertEqual("2026-09-18", got[P + "date"])
        self.assertEqual("18", got[P + "dayRadio"])
        self.assertEqual("Asia/Shanghai", got[P + "timeZone"])

    def test_empty_stays_empty(self):
        """HAR 里 isRestricted / timeSlotType / displayStartTime 就是空串，
        不能变成 'None' 发过去。"""
        got = fields(placer().choose_slot(FUL_SLOTS["json"]))
        self.assertEqual("", got[P + "isRestricted"])
        self.assertEqual("", got[P + "timeSlotType"])
        self.assertEqual("", got[P + "displayStartTime"])
        self.assertEqual("false", got[P + "isRecommended"])

    def test_recommended_slot_is_flagged(self):
        model = {"d": {**SLOT_MODEL["d"], "timeSlotWindows": [
            {"18": [slot("18-10:00-10:15", "10:00 AM", "10:15 AM",
                         recommendationLabel="推荐")]}, {"19": DAY19}]}}
        got = fields(placer().choose_slot(ful_with_slots(model)["json"]))
        self.assertEqual("true", got[P + "isRecommended"])

    def test_no_slot_means_no_fields(self):
        """老流程上多发一堆字段是白给风控送特征，宁可一个都不发。"""
        self.assertEqual([], FastCheckout.slot_fields({}))


class WizardTests(unittest.TestCase):
    """整条链路：第 2 步的响应挑时段，第 3 步把它带上。"""

    def run_wizard(self, ful, **kw):
        page = FakePage([ful, ful, CONTACT, BANKS, MONTHS, REVIEW])
        fc = placer(**kw)
        ok, stage, detail = fc.run(page)
        return page, fc, ok, stage, detail

    def body(self, page, action):
        return next(c["body"] for c in page.calls if action in c["query"])

    def test_slot_rides_along_to_pickup_contact(self):
        page, fc, ok, stage, _ = self.run_wizard(FUL_SLOTS, pickup_time="12:30")
        self.assertTrue(ok, stage)
        body = self.body(page, "continueFromFulfillmentToPickupContact")
        self.assertIn("timeSlotValue=18-12%3A30-12%3A45", body)
        self.assertIn("signKey=KEY-18-12%3A30-12%3A45", body)
        self.assertIn("date=2026-09-18", body)
        # 门店那几个字段一个都没被挤掉
        self.assertIn("selectStore=R581", body)

    def test_slot_is_not_sent_on_the_store_search(self):
        """时段是第 3 步的字段。第 2 步（search）多带它没意义，别顺手加进去。"""
        page, _, ok, stage, _ = self.run_wizard(FUL_SLOTS)
        self.assertTrue(ok, stage)
        self.assertNotIn("timeSlot", self.body(page, "_a=search"))

    def test_old_flow_sends_nothing_extra(self):
        """老流程（2026-09-14 的 HAR）整个时段模块都没有。

        现在默认认定「没有时段 = 拿不到这家店的货」并换店，所以要走老流程必须
        显式 require_slot=False。这个开关存在的唯一理由就是 Apple 万一回退。
        """
        page, _, ok, stage, _ = self.run_wizard(FUL_PLAIN, require_slot=False)
        self.assertTrue(ok, stage)
        self.assertNotIn("timeSlot",
                         self.body(page, "continueFromFulfillmentToPickupContact"))

    def test_missing_slot_module_stops_instead_of_burning_step3(self):
        """没有时段模块时，默认必须当场停住而不是撞进必死的第 3 步。

        2026-09-16 起 9/9 的实测：带着空时段发 continueFromFulfillmentToPickupContact
        一律返回 200 但没有 pickupContact，白烧一个 10 秒的请求还把状态改脏。
        """
        page, _, ok, stage, detail = self.run_wizard(FUL_PLAIN)
        self.assertFalse(ok)
        self.assertIn("取货时段", detail)
        # 关键：第 3 步压根没发出去
        self.assertFalse([c for c in page.calls
                          if "continueFromFulfillmentToPickupContact" in c["query"]])

    def test_store_that_cannot_be_scheduled_stops_with_a_reason(self):
        """有时段模块、却一档都排不上 = 这家店当下取不了货。继续发包只会换来
        一个 200 却不推进的响应，日志上看着像代码坏了——所以在这儿就说清楚。"""
        empty = ful_with_slots({"d": {"pickUpDates": [{"dayOfMonth": "18",
                                                       "date": "2026-09-18"}],
                                      "timeSlotWindows": [{"18": []}]}})
        page, _, ok, stage, detail = self.run_wizard(empty)
        self.assertFalse(ok)
        self.assertIn("取货时段", detail)
        # 停在第 3 步之前：没有把「继续」发出去
        self.assertFalse([c for c in page.calls
                          if "continueFromFulfillmentToPickupContact" in c["query"]])

    def test_stalled_is_what_it_raises(self):
        empty = ful_with_slots({"d": {"pickUpDates": [], "timeSlotWindows": [{}]}})
        with self.assertRaises(Stalled):
            placer().take_slot(empty["json"])


if __name__ == "__main__":
    unittest.main()
