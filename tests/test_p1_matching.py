import csv
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from p0_database import ProcurementDatabase
from p1_matching import P1Processor


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
    is_delayed: int = 0
    is_failed_bid: int = 0
    content_hash: str = ""
    crawl_time: str = "2026-07-16T12:00:00+08:00"
    error: str = ""

    def __post_init__(self):
        if not self.raw_text:
            self.raw_text = self.title


class P1MatchingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.sqlite"

    def tearDown(self):
        self.tmp.cleanup()

    def ingest(self, notices, companies=()):
        ProcurementDatabase(self.db_path).ingest(notices)
        return P1Processor(self.db_path, companies).ingest(notices)

    def connect(self):
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        return con

    def test_exact_project_and_package_builds_two_stage_chain(self):
        notices = [
            FakeNotice("1", "https://x/tender", "招标公告", "设备采购项目第1包公开招标公告", "2026-01-01", "XM-001", raw_text="第1包"),
            FakeNotice("2", "https://x/award", "中标结果", "设备采购项目第1包中标公告", "2026-02-01", "XM-001", supplier_names="供应商甲", raw_text="第1包"),
            FakeNotice("3", "https://x/contract", "采购合同", "设备采购项目第1包合同公告", "2026-03-01", "XM-001", supplier_names="供应商甲", raw_text="第1包"),
        ]
        counts = self.ingest(notices)
        self.assertEqual(counts["chain_links"], 2)
        with closing(self.connect()) as con:
            links = con.execute("SELECT link_type, review_status, match_score FROM document_chain_link ORDER BY link_type").fetchall()
        self.assertEqual({row["link_type"] for row in links}, {"TENDER_TO_AWARD", "AWARD_TO_CONTRACT"})
        self.assertTrue(all(row["review_status"] == "AUTO_ACCEPTED" for row in links))
        self.assertTrue(all(row["match_score"] >= 0.85 for row in links))

    def test_package_conflict_prevents_false_link(self):
        notices = [
            FakeNotice("1", "https://x/tender-p1", "招标公告", "设备采购项目第1包公开招标公告", "2026-01-01", "XM-002", raw_text="第1包"),
            FakeNotice("2", "https://x/award-p2", "中标结果", "设备采购项目第2包中标公告", "2026-02-01", "XM-002", raw_text="第2包"),
        ]
        self.ingest(notices)
        with closing(self.connect()) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM document_chain_link").fetchone()[0], 0)
            reasons = con.execute("SELECT reason_codes_json FROM match_review_queue").fetchone()[0]
        self.assertIn("NO_CANDIDATE", reasons)

    def test_title_only_ambiguous_match_requires_review(self):
        notices = [
            FakeNotice("1", "https://x/tender-a", "招标公告", "设备采购项目公开招标公告", "2026-01-01"),
            FakeNotice("2", "https://x/tender-b", "招标公告", "设备采购项目公开招标公告", "2026-01-02"),
            FakeNotice("3", "https://x/award-a", "中标结果", "设备采购项目中标公告", "2026-02-01"),
        ]
        self.ingest(notices)
        with closing(self.connect()) as con:
            link = con.execute("SELECT review_status, evidence_json FROM document_chain_link").fetchone()
            review = con.execute("SELECT reason_codes_json FROM match_review_queue").fetchone()
        self.assertEqual(link["review_status"], "NEEDS_REVIEW")
        self.assertIn("AMBIGUOUS_TOP_CANDIDATES", review["reason_codes_json"])

    def test_configured_company_aliases_resolve_to_one_entity(self):
        companies = [{"name": "甲科技股份有限公司", "aliases": ["甲科技"], "listed_code": "600001"}]
        notices = [
            FakeNotice("1", "https://x/award-org", "中标结果", "设备采购项目中标公告", "2026-02-01", "XM-003", supplier_names="甲科技"),
            FakeNotice("2", "https://x/contract-org", "采购合同", "设备采购项目合同公告", "2026-03-01", "XM-003", supplier_names="甲科技股份有限公司"),
        ]
        self.ingest(notices, companies)
        with closing(self.connect()) as con:
            orgs = con.execute("SELECT canonical_name, org_type, listed_code FROM organization WHERE org_type='LISTED_COMPANY'").fetchall()
            aliases = con.execute("SELECT COUNT(*) FROM organization_alias WHERE org_uid=?", (con.execute("SELECT org_uid FROM organization WHERE org_type='LISTED_COMPANY'").fetchone()[0],)).fetchone()[0]
        self.assertEqual(len(orgs), 1)
        self.assertEqual(orgs[0]["canonical_name"], "甲科技股份有限公司")
        self.assertEqual(orgs[0]["listed_code"], "600001")
        self.assertEqual(aliases, 2)

    def test_repeat_ingestion_is_idempotent(self):
        notices = [
            FakeNotice("1", "https://x/tender-idem", "招标公告", "设备采购项目第1包公开招标公告", "2026-01-01", "XM-004", raw_text="第1包"),
            FakeNotice("2", "https://x/award-idem", "中标结果", "设备采购项目第1包中标公告", "2026-02-01", "XM-004", raw_text="第1包"),
        ]
        first = self.ingest(notices)
        second = self.ingest(notices)
        self.assertEqual(first["chain_links"], 1)
        self.assertTrue(all(value == 0 for value in second.values()))

    def test_incremental_award_matches_tender_from_historical_batch(self):
        tender = FakeNotice("1", "https://x/tender-history", "招标公告", "设备采购项目第1包公开招标公告", "2026-01-01", "XM-006", raw_text="第1包")
        award = FakeNotice("2", "https://x/award-later", "中标结果", "设备采购项目第1包中标公告", "2026-02-01", "XM-006", raw_text="第1包")
        self.ingest([tender])
        later = self.ingest([award])
        self.assertEqual(later["chain_links"], 1)
        with closing(self.connect()) as con:
            row = con.execute(
                "SELECT link_type, review_status FROM current_document_chain_link"
            ).fetchone()
        self.assertEqual(row["link_type"], "TENDER_TO_AWARD")
        self.assertEqual(row["review_status"], "AUTO_ACCEPTED")

    def test_gold_labels_calculate_precision_and_recall(self):
        notices = [
            FakeNotice("1", "https://x/tender-gold", "招标公告", "设备采购项目第1包公开招标公告", "2026-01-01", "XM-005", raw_text="第1包"),
            FakeNotice("2", "https://x/award-gold", "中标结果", "设备采购项目第1包中标公告", "2026-02-01", "XM-005", raw_text="第1包"),
        ]
        self.ingest(notices)
        labels = Path(self.tmp.name) / "gold.csv"
        with labels.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=["link_type", "source_url", "target_url", "is_match", "package_same", "annotator", "note"])
            writer.writeheader()
            writer.writerow({"link_type": "TENDER_TO_AWARD", "source_url": "https://x/tender-gold", "target_url": "https://x/award-gold", "is_match": 1, "package_same": 1, "annotator": "tester", "note": ""})
        processor = P1Processor(self.db_path)
        self.assertEqual(processor.import_gold_labels(labels), 1)
        result = processor.evaluate()
        self.assertEqual(result["precision"], 1.0)
        self.assertEqual(result["recall"], 1.0)
        self.assertEqual(result["f1"], 1.0)


if __name__ == "__main__":
    unittest.main()
