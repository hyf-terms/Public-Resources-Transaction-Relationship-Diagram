import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from p0_database import ProcurementDatabase, table_counts


@dataclass
class FakeNotice:
    notice_id: str
    source: str = "test-source"
    category: str = "公开招标"
    stage: str = "招标公告"
    title: str = "视频监控升级项目第一包公开招标公告"
    publish_time: str = "2026-01-02 09:00"
    province: str = "北京"
    buyer: str = "某市公安局"
    url: str = "https://example.test/notice/1"
    project_code: str = "A2026001"
    project_name: str = "视频监控升级项目"
    supplier_names: str = ""
    amount_yuan: float | None = None
    budget_yuan: float | None = 10_000_000
    is_cancelled: int = 0
    is_failed_bid: int = 0
    is_delayed: int = 0
    raw_text: str = "项目编号：A2026001 第一包 预算金额1000万元"
    content_hash: str = ""
    crawl_time: str = "2026-01-02T10:00:00+08:00"
    error: str = ""

    def __post_init__(self):
        if not self.content_hash:
            self.content_hash = hashlib.sha256(self.raw_text.encode()).hexdigest()


class P0DatabaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "procurement.sqlite"
        self.db = ProcurementDatabase(self.db_path, parser_version="test")

    def tearDown(self):
        self.tmp.cleanup()

    def test_identical_document_is_idempotent(self):
        notice = FakeNotice("n1")
        first = self.db.ingest([notice])
        second = self.db.ingest([notice])
        counts = table_counts(self.db_path)
        self.assertEqual(first["documents"], 1)
        self.assertEqual(second["versions"], 0)
        self.assertEqual(counts["source_document"], 1)
        self.assertEqual(counts["source_document_version"], 1)
        self.assertEqual(counts["procurement_event"], 1)

    def test_changed_content_creates_version_not_new_document(self):
        original = FakeNotice("n1")
        changed = FakeNotice(
            "n1",
            raw_text="项目编号：A2026001 第一包 预算金额900万元（更正后）",
            budget_yuan=9_000_000,
            crawl_time="2026-01-03T10:00:00+08:00",
        )
        self.db.ingest([original])
        self.db.ingest([changed])
        counts = table_counts(self.db_path)
        self.assertEqual(counts["source_document"], 1)
        self.assertEqual(counts["source_document_version"], 2)
        self.assertEqual(counts["procurement_event"], 2)
        with closing(sqlite3.connect(self.db_path)) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM current_event").fetchone()[0], 1)

    def test_packages_do_not_merge_and_amount_types_do_not_sum_stages(self):
        tender_one = FakeNotice("t1")
        tender_two = FakeNotice(
            "t2",
            title="视频监控升级项目第二包公开招标公告",
            url="https://example.test/notice/2",
            raw_text="项目编号：A2026001 第二包 预算金额500万元",
            budget_yuan=5_000_000,
        )
        award_one = FakeNotice(
            "a1",
            category="中标",
            stage="中标结果",
            title="视频监控升级项目第一包中标公告",
            url="https://example.test/notice/3",
            supplier_names="供应商甲",
            amount_yuan=8_800_000,
            budget_yuan=None,
            raw_text="项目编号：A2026001 第一包 中标金额880万元",
        )
        self.db.ingest([tender_one, tender_two, award_one])
        counts = table_counts(self.db_path)
        self.assertEqual(counts["root_project"], 1)
        self.assertEqual(counts["procurement_attempt"], 1)
        self.assertEqual(counts["package"], 2)
        with closing(sqlite3.connect(self.db_path)) as con:
            types = dict(con.execute("SELECT amount_type, COUNT(*) FROM amount_observation GROUP BY amount_type"))
            self.assertEqual(types, {"AWARD_AMOUNT": 1, "TENDER_BUDGET": 2})

    def test_correction_is_patch_event_not_lifecycle_state(self):
        tender = FakeNotice("t1")
        correction = FakeNotice(
            "c1",
            category="更正",
            stage="更正",
            title="视频监控升级项目第一包延期更正公告",
            url="https://example.test/notice/4",
            is_delayed=1,
            budget_yuan=None,
            raw_text="项目编号：A2026001 第一包 开标时间延期",
        )
        self.db.ingest([tender, correction])
        with closing(sqlite3.connect(self.db_path)) as con:
            events = dict(con.execute("SELECT event_type, COUNT(*) FROM procurement_event GROUP BY event_type"))
            states = con.execute("SELECT current_state FROM current_object_state").fetchall()
            self.assertEqual(events, {"DEADLINE_EXTENDED": 1, "TENDER_OPENED": 1})
            self.assertIn(("OPEN",), states)
            self.assertNotIn(("CORRECTED",), states)

    def test_low_confidence_fallback_requires_review(self):
        notice = FakeNotice("n1", project_code="")
        self.db.ingest([notice])
        with closing(sqlite3.connect(self.db_path)) as con:
            rows = con.execute(
                "SELECT DISTINCT link_method, match_score, confidence, review_status FROM document_object_link"
            ).fetchall()
            self.assertEqual(rows, [("title_buyer_normalized", 0.72, "MEDIUM", "NEEDS_REVIEW")])

    def test_multi_supplier_amount_remains_unallocated(self):
        award = FakeNotice(
            "a1",
            category="中标",
            stage="中标结果",
            title="视频监控升级项目第一包中标公告",
            supplier_names="供应商甲 | 供应商乙",
            amount_yuan=8_800_000,
            budget_yuan=None,
            raw_text="项目编号：A2026001 第一包 联合中标金额880万元",
        )
        self.db.ingest([award])
        with closing(sqlite3.connect(self.db_path)) as con:
            rows = con.execute(
                "SELECT amount, allocation_status FROM amount_observation WHERE amount_type='AWARD_AMOUNT'"
            ).fetchall()
            self.assertEqual(rows, [(8_800_000, "UNALLOCATED_MULTI_SUPPLIER")])


if __name__ == "__main__":
    unittest.main()
