import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from p0_database import ProcurementDatabase
from p1_matching import P1Processor
from p2_lifecycle import P2LifecycleProcessor


@dataclass
class FakeNotice:
    notice_id: str
    url: str
    stage: str
    title: str
    publish_time: str
    project_code: str = ""
    project_name: str = "设备采购项目"
    buyer: str = "采购单位甲"
    supplier_names: str = ""
    raw_text: str = ""
    source: str = "测试源"
    category: str = "公开招标"
    province: str = "北京"
    amount_yuan: float | None = None
    budget_yuan: float | None = None
    supplier_count: int | None = None
    is_cancelled: int = 0
    is_failed_bid: int = 0
    is_delayed: int = 0
    content_hash: str = ""
    crawl_time: str = "2026-07-16T12:00:00+08:00"
    error: str = ""

    def __post_init__(self):
        if not self.raw_text:
            self.raw_text = self.title


class P2LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.sqlite"

    def tearDown(self):
        self.tmp.cleanup()

    def ingest(self, notices):
        ProcurementDatabase(self.db_path).ingest(notices)
        P1Processor(self.db_path).ingest(notices)
        return P2LifecycleProcessor(self.db_path).ingest(notices)

    def connect(self):
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        return con

    def test_correction_links_to_affected_attempt(self):
        notices = [
            FakeNotice("1", "https://x/tender", "招标公告", "设备采购项目第1包公开招标公告", "2026-01-01", "XM-P2-1", raw_text="第1包"),
            FakeNotice("2", "https://x/correction", "更正", "设备采购项目第1包更正公告", "2026-01-05", "XM-P2-1", category="更正", raw_text="第1包"),
        ]
        counts = self.ingest(notices)
        self.assertEqual(counts["lifecycle_links"], 1)
        with closing(self.connect()) as con:
            link = con.execute("SELECT * FROM current_lifecycle_link").fetchone()
        self.assertEqual(link["relation_type"], "AMENDS")
        self.assertEqual(link["affected_object_type"], "attempt")
        self.assertIsNotNone(link["affected_object_uid"])
        self.assertEqual(link["review_status"], "AUTO_ACCEPTED")

    def test_deadline_extension_has_distinct_relation(self):
        notices = [
            FakeNotice("1", "https://x/tender-delay", "招标公告", "设备采购项目公开招标公告", "2026-01-01", "XM-P2-2"),
            FakeNotice("2", "https://x/delay", "更正", "设备采购项目延期公告", "2026-01-06", "XM-P2-2", category="更正", is_delayed=1),
        ]
        self.ingest(notices)
        with closing(self.connect()) as con:
            relation = con.execute("SELECT relation_type FROM current_lifecycle_link").fetchone()[0]
        self.assertEqual(relation, "EXTENDS_DEADLINE")

    def test_failed_notice_links_to_package(self):
        notices = [
            FakeNotice("1", "https://x/tender-fail", "招标公告", "设备采购项目第2包公开招标公告", "2026-01-01", "XM-P2-3", raw_text="第2包"),
            FakeNotice("2", "https://x/fail", "终止", "设备采购项目第2包废标公告", "2026-02-01", "XM-P2-3", category="终止", is_failed_bid=1, raw_text="第2包"),
        ]
        self.ingest(notices)
        with closing(self.connect()) as con:
            link = con.execute("SELECT relation_type, affected_object_type, affected_object_uid FROM current_lifecycle_link").fetchone()
        self.assertEqual(link["relation_type"], "FAILS")
        self.assertEqual(link["affected_object_type"], "package")
        self.assertIsNotNone(link["affected_object_uid"])

    def test_second_tender_creates_retry_relation_between_attempts(self):
        notices = [
            FakeNotice("1", "https://x/tender-first", "招标公告", "设备采购项目第一次公开招标公告", "2026-01-01", "XM-P2-4"),
            FakeNotice("2", "https://x/tender-second", "招标公告", "设备采购项目第二次公开招标公告", "2026-03-01", "XM-P2-4"),
        ]
        self.ingest(notices)
        with closing(self.connect()) as con:
            link = con.execute("SELECT * FROM current_lifecycle_link").fetchone()
        self.assertEqual(link["relation_type"], "RETRY_OF")
        self.assertEqual(link["affected_object_type"], "attempt")
        self.assertEqual(link["event_object_type"], "attempt")
        self.assertNotEqual(link["affected_object_uid"], link["event_object_uid"])

    def test_project_code_conflict_goes_to_review_without_link(self):
        notices = [
            FakeNotice("1", "https://x/tender-conflict", "招标公告", "设备采购项目公开招标公告", "2026-01-01", "XM-A"),
            FakeNotice("2", "https://x/end-conflict", "终止", "设备采购项目终止公告", "2026-02-01", "XM-B", category="终止", is_cancelled=1),
        ]
        self.ingest(notices)
        with closing(self.connect()) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM lifecycle_link").fetchone()[0], 0)
            reason = con.execute("SELECT reason_codes_json FROM lifecycle_review_queue").fetchone()[0]
        self.assertIn("NO_CANDIDATE", reason)

    def test_incremental_event_matches_historical_tender_and_is_idempotent(self):
        tender = FakeNotice("1", "https://x/tender-history", "招标公告", "设备采购项目公开招标公告", "2026-01-01", "XM-P2-5")
        termination = FakeNotice("2", "https://x/end-later", "终止", "设备采购项目终止公告", "2026-02-01", "XM-P2-5", category="终止", is_cancelled=1)
        self.ingest([tender])
        later = self.ingest([termination])
        repeated = self.ingest([termination])
        self.assertEqual(later["lifecycle_links"], 1)
        self.assertTrue(all(value == 0 for value in repeated.values()))

    def test_quality_metrics_and_exports_are_created(self):
        notices = [
            FakeNotice("1", "https://x/tender-export", "招标公告", "设备采购项目公开招标公告", "2026-01-01", "XM-P2-6"),
            FakeNotice("2", "https://x/end-export", "终止", "设备采购项目终止公告", "2026-02-01", "XM-P2-6", category="终止", is_cancelled=1),
        ]
        self.ingest(notices)
        processor = P2LifecycleProcessor(self.db_path)
        processor.export_csv(self.tmp.name)
        self.assertTrue((Path(self.tmp.name) / "lifecycle_links.csv").exists())
        self.assertTrue((Path(self.tmp.name) / "object_lifecycle_state.csv").exists())
        self.assertTrue((Path(self.tmp.name) / "data_quality_metrics.csv").exists())
        metrics = {row["metric"] for row in processor.quality_metrics()}
        self.assertIn("p2_current_lifecycle_links", metrics)


if __name__ == "__main__":
    unittest.main()
