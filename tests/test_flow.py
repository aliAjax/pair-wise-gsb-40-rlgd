import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import DomainError, MaritimeSARService  # noqa: E402


class MaritimeSARFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = MaritimeSARService(Path(self.tmp.name) / "test.db")
        self.incident = self.service.create_incident(
            "coord1", "coordinator", "SAR-001", "海燕号", 31.0, 122.0, 10.0, 3, "东海中心"
        )
        self.asset = self.service.add_asset(
            "coord1", "coordinator", "海巡01", "vessel", ["surface", "night"], 31.0, 122.0, 20, 100, 5
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_complete_assignment_clue_offline_and_close_flow(self):
        area = self.service.create_search_area(
            "coord1", "coordinator", self.incident["id"], "A-01", "surface", 31.1, 122.1, 8, 1
        )
        assigned = self.service.assign_area("coord1", "coordinator", area["id"], self.asset["id"], self.asset["version"])
        self.assertEqual("assigned", assigned["status"])
        clue = self.service.record_clue(
            "field1", "field", self.incident["id"], "evt-1", 31.1, 122.1, 0.9, "visual", area["id"]
        )
        self.assertEqual("verified", self.service.verify_clue("analyst1", "analyst", clue["id"], "verified")["status"])
        batch = self.service.merge_offline_batch(
            "field1", "field", "batch-1",
            [{"type": "clue", "client_event_id": "off-1", "incident_id": self.incident["id"],
              "latitude": 31.11, "longitude": 122.11, "confidence": 0.7, "source": "radio"}],
        )
        self.assertEqual(1, batch["summary"]["accepted"])
        self.assertTrue(self.service.merge_offline_batch("field1", "field", "batch-1", [])["idempotent"])
        updated_asset = self.service.list_assets()[0]
        self.service.withdraw_asset("coord1", "coordinator", self.asset["id"], "任务移交", updated_asset["version"])
        current_area = self.service.state()["search_areas"][0]
        self.service.complete_area("coord1", "coordinator", area["id"], "abandoned", current_area["version"])
        current_incident = [x for x in self.service.state()["incidents"] if x["id"] == self.incident["id"]][0]
        closed = self.service.close_incident("coord1", "coordinator", self.incident["id"], "resolved", current_incident["version"])
        self.assertEqual("closed", closed["status"])
        self.assertGreaterEqual(len(self.service.incident_timeline(self.incident["id"])), 6)

    def test_duplicate_alarm_and_invalid_position_are_controlled(self):
        duplicate = self.service.create_incident(
            "op1", "operator", "SAR-002", "海燕号", 31.01, 122.01, 5.0, 3, "东海中心"
        )
        self.assertEqual("duplicate", duplicate["status"])
        self.assertEqual(self.incident["id"], duplicate["duplicate_of"])
        invalid = self.service.record_clue(
            "field1", "field", self.incident["id"], "evt-far", 45.0, 130.0, 0.8, "radio"
        )
        self.assertEqual("invalid", invalid["status"])
        with self.assertRaises(DomainError):
            self.service.verify_clue("field1", "field", invalid["id"], "verified")

    def test_assignment_conflict_and_permission(self):
        area = self.service.create_search_area(
            "coord1", "coordinator", self.incident["id"], "A-02", "surface", 31.1, 122.1, 5
        )
        self.service.assign_area("coord1", "coordinator", area["id"], self.asset["id"], self.asset["version"])
        area2 = self.service.create_search_area(
            "coord1", "coordinator", self.incident["id"], "A-03", "surface", 31.2, 122.2, 5
        )
        with self.assertRaises(DomainError) as ctx:
            self.service.assign_area("coord1", "coordinator", area2["id"], self.asset["id"], self.asset["version"])
        self.assertEqual(409, ctx.exception.status)
        with self.assertRaises(DomainError) as ctx2:
            self.service.create_search_area("field1", "field", self.incident["id"], "A-04", "surface", 31, 122, 5)
        self.assertEqual(403, ctx2.exception.status)

    def _clue_event(self, event_id, confidence=0.7):
        return {"type": "clue", "client_event_id": event_id, "incident_id": self.incident["id"],
                "latitude": 31.05, "longitude": 122.05, "confidence": confidence, "source": "radio"}

    def test_offline_batch_per_record_receipts_and_retry(self):
        batch = self.service.merge_offline_batch(
            "field1", "field", "batch-9", [self._clue_event("ok-1"), self._clue_event("bad-1", confidence=1.8)]
        )
        self.assertEqual(1, batch["summary"]["accepted"])
        self.assertEqual(1, batch["summary"]["rejected"])
        self.assertEqual(0, batch["summary"]["pending"])
        self.assertEqual("partial", batch["status"])
        receipts = {r["client_event_id"]: r for r in batch["receipts"]}
        self.assertEqual("merged", receipts["ok-1"]["status"])
        self.assertEqual("rejected", receipts["bad-1"]["status"])
        self.assertIn("置信度", receipts["bad-1"]["reason"])

        # 原样重传：已成功记录幂等返回，不重复写入
        replay = self.service.merge_offline_batch(
            "field1", "field", "batch-9", [self._clue_event("ok-1"), self._clue_event("bad-1", confidence=1.8)]
        )
        replay_receipts = {r["client_event_id"]: r for r in replay["receipts"]}
        self.assertTrue(replay_receipts["ok-1"]["idempotent"])
        self.assertEqual(1, len(self.service.state()["clues"]))

        # 改正后重传：被拒记录可再合并，批次转为全部成功
        fixed = self.service.merge_offline_batch(
            "field1", "field", "batch-9", [self._clue_event("bad-1", confidence=0.8)]
        )
        self.assertEqual("merged", fixed["receipts"][0]["status"])
        self.assertEqual(0, fixed["summary"]["rejected"])
        self.assertEqual("merged", fixed["status"])
        self.assertEqual(2, len(self.service.state()["clues"]))

        # 跨批次幂等：另一批次重复同一 client_event_id 不再写入
        other = self.service.merge_offline_batch("field1", "field", "batch-10", [self._clue_event("ok-1")])
        self.assertTrue(other["receipts"][0]["idempotent"])
        self.assertEqual(2, len(self.service.state()["clues"]))

    def test_offline_upload_after_close_only_leaves_rejection(self):
        current = [x for x in self.service.state()["incidents"] if x["id"] == self.incident["id"]][0]
        self.service.close_incident("coord1", "coordinator", self.incident["id"], "resolved", current["version"])
        before = self.service.incident_timeline(self.incident["id"])
        batch = self.service.merge_offline_batch(
            "field1", "field", "batch-late",
            [self._clue_event("late-1"),
             {"type": "timeline", "client_event_id": "late-2", "incident_id": self.incident["id"],
              "action": "field.note", "details": {"text": "已撤离"}}],
        )
        self.assertEqual(2, batch["summary"]["rejected"])
        self.assertEqual("rejected", batch["status"])
        for receipt in batch["receipts"]:
            self.assertEqual("rejected", receipt["status"])
            self.assertIn("已结束", receipt["reason"])
        # 时间线不被改动，只留下回执里的拒绝原因
        self.assertEqual(before, self.service.incident_timeline(self.incident["id"]))
        self.assertEqual(0, len(self.service.state()["clues"]))

    def test_offline_review_flow(self):
        batch = self.service.merge_offline_batch(
            "field1", "field", "batch-r", [self._clue_event("fix-1", confidence=2.5)]
        )
        record_id = batch["receipts"][0]["id"]
        with self.assertRaises(DomainError) as ctx:
            self.service.review_offline_record("field1", "field", record_id)
        self.assertEqual(403, ctx.exception.status)

        # 从拒绝记录发起复查：状态转为待处理
        reopened = self.service.review_offline_record("coord1", "coordinator", record_id, note="请核对置信度")
        self.assertEqual("pending", reopened["record"]["status"])
        self.assertEqual(1, reopened["summary"]["pending"])
        self.assertEqual("pending", reopened["batch_status"])

        # 复查时附带改正内容，直接合并
        resolved = self.service.review_offline_record(
            "analyst1", "analyst", record_id,
            event={"type": "clue", "incident_id": self.incident["id"], "latitude": 31.05,
                   "longitude": 122.05, "confidence": 0.7, "source": "radio"},
        )
        self.assertEqual("merged", resolved["record"]["status"])
        self.assertEqual(1, resolved["summary"]["accepted"])
        clue = self.service.state()["clues"][0]
        self.assertEqual("fix-1", clue["client_event_id"])
        self.assertAlmostEqual(0.7, clue["confidence"])

        # 已合并记录不能再复查
        with self.assertRaises(DomainError) as ctx2:
            self.service.review_offline_record("coord1", "coordinator", record_id)
        self.assertEqual(409, ctx2.exception.status)


if __name__ == "__main__":
    unittest.main()
