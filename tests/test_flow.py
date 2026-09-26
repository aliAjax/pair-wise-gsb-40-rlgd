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

    def _clue_event(self, event_id, confidence=0.6):
        return {"type": "clue", "client_event_id": event_id, "incident_id": self.incident["id"],
                "latitude": 31.05, "longitude": 122.05, "confidence": confidence, "source": "radio"}

    def test_offline_batch_receipts_retry_and_idempotency(self):
        first = self.service.merge_offline_batch(
            "field1", "field", "batch-r1",
            [self._clue_event("ok-1"), self._clue_event("bad-1", confidence=2.0)],
        )
        self.assertEqual("partial", first["status"])
        self.assertEqual(1, first["summary"]["accepted"])
        self.assertEqual(1, first["summary"]["rejected"])
        receipts = {e["client_event_id"]: e for e in first["summary"]["events"]}
        self.assertEqual("merged", receipts["ok-1"]["status"])
        self.assertEqual("rejected", receipts["bad-1"]["status"])
        self.assertTrue(receipts["bad-1"]["error"])
        # 改正后重传同一批次：被拒记录可再合并，成功记录不重复写入
        retry = self.service.merge_offline_batch(
            "field1", "field", "batch-r1",
            [self._clue_event("ok-1"), self._clue_event("bad-1", confidence=0.4)],
        )
        self.assertFalse(retry["idempotent"])
        self.assertEqual("merged", retry["status"])
        self.assertEqual(2, retry["summary"]["accepted"])
        self.assertEqual(0, retry["summary"]["rejected"])
        receipts = {e["client_event_id"]: e for e in retry["summary"]["events"]}
        self.assertEqual("merged", receipts["bad-1"]["status"])
        self.assertTrue(receipts["ok-1"]["idempotent"])
        clues = [c for c in self.service.state()["clues"] if c["client_event_id"] in {"ok-1", "bad-1"}]
        self.assertEqual(2, len(clues))
        # 原样再重传：无任何变化
        again = self.service.merge_offline_batch(
            "field1", "field", "batch-r1",
            [self._clue_event("ok-1"), self._clue_event("bad-1", confidence=0.4)],
        )
        self.assertTrue(again["idempotent"])
        # 页面状态：按批次统计 + 逐条回执
        state = self.service.state()
        batch_row = [b for b in state["offline_batches"] if b["client_batch_id"] == "batch-r1"][0]
        self.assertEqual(2, batch_row["merged"])
        self.assertEqual(0, batch_row["rejected"])
        self.assertEqual(0, batch_row["pending"])
        self.assertEqual(2, len([r for r in state["offline_records"] if r["batch_id"] == batch_row["id"]]))

    def test_offline_review_flow(self):
        batch = self.service.merge_offline_batch(
            "field1", "field", "batch-r2", [self._clue_event("bad-2", confidence=9.0)]
        )
        self.assertEqual(1, batch["summary"]["rejected"])
        record = self.service.state()["offline_records"][0]
        with self.assertRaises(DomainError):
            self.service.request_offline_review("analyst1", "analyst", record["id"])
        pending = self.service.request_offline_review("field1", "field", record["id"], "已核对原始坐标")
        self.assertEqual("pending", pending["status"])
        batch_row = [b for b in self.service.state()["offline_batches"] if b["client_batch_id"] == "batch-r2"][0]
        self.assertEqual(1, batch_row["pending"])
        # 复查中的记录不受重传影响
        retry = self.service.merge_offline_batch("field1", "field", "batch-r2", [self._clue_event("bad-2", confidence=0.3)])
        self.assertTrue(retry["idempotent"])
        self.assertEqual("pending", retry["summary"]["events"][0]["status"])
        # field 无权处理复查
        with self.assertRaises(DomainError):
            self.service.resolve_offline_review("field1", "field", record["id"], "merge")
        merged = self.service.resolve_offline_review(
            "analyst1", "analyst", record["id"], "merge", corrections={"confidence": 0.3}
        )
        self.assertEqual("merged", merged["status"])
        self.assertEqual("", merged["error"])
        clue = [c for c in self.service.state()["clues"] if c["client_event_id"] == "bad-2"][0]
        self.assertAlmostEqual(0.3, clue["confidence"])
        # 复查也可以维持拒绝并留下原因
        self.service.merge_offline_batch("field1", "field", "batch-r3", [self._clue_event("bad-3", confidence=5.0)])
        rec2 = [r for r in self.service.state()["offline_records"] if r["client_event_id"] == "bad-3"][0]
        self.service.request_offline_review("op1", "operator", rec2["id"])
        rejected = self.service.resolve_offline_review("coord1", "coordinator", rec2["id"], "reject", reason="现场确认误报")
        self.assertEqual("rejected", rejected["status"])
        self.assertEqual("现场确认误报", rejected["error"])

    def test_closed_incident_keeps_rejection_and_freezes_timeline(self):
        self.service.merge_offline_batch("field1", "field", "batch-c1", [self._clue_event("late-1", confidence=7.0)])
        record = self.service.state()["offline_records"][0]
        closed = self.service.close_incident("coord1", "coordinator", self.incident["id"], "resolved", self.incident["version"])
        self.assertEqual("closed", closed["status"])
        before = len(self.service.incident_timeline(self.incident["id"]))
        # 事件结束后改正重传：只留下拒绝原因，不能合并
        retry = self.service.merge_offline_batch("field1", "field", "batch-c1", [self._clue_event("late-1", confidence=0.5)])
        self.assertEqual(1, retry["summary"]["rejected"])
        self.assertIn("结束", retry["summary"]["events"][0]["error"])
        # 离线时间线事件同样被拒，事件时间线保持不变
        other = self.service.merge_offline_batch(
            "field1", "field", "batch-c2",
            [{"type": "timeline", "client_event_id": "note-1", "incident_id": self.incident["id"],
              "action": "offline.note", "details": {"text": "补录"}}],
        )
        self.assertEqual("rejected", other["summary"]["events"][0]["status"])
        self.assertEqual(before, len(self.service.incident_timeline(self.incident["id"])))
        # 结束后不能发起复查
        with self.assertRaises(DomainError):
            self.service.request_offline_review("field1", "field", record["id"])


if __name__ == "__main__":
    unittest.main()
