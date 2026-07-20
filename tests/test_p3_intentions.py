import datetime as dt
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from p0_database import ProcurementDatabase
from p1_matching import P1Processor
from p2_lifecycle import P2LifecycleProcessor
from p3_intentions import P3IntentProcessor
from procurement_crawler import Crawler, calendar_date, daily_page_is_complete, parse_intention_page


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
    crawl_time: str = "2026-07-20T12:00:00+08:00"
    error: str = ""
    source_page_url: str = ""
    intent_item_no: str = ""
    procurement_category: str = ""
    demand_summary: str = ""
    expected_purchase_date: str = ""
    intent_remarks: str = ""

    def __post_init__(self):
        if not self.raw_text:
            self.raw_text = self.title


def intent(url="https://intent/1", title="设备采购项目", buyer="采购单位甲"):
    return FakeNotice(
        "i1", f"{url}#intent-item-1", "采购意向", title, "2026-01-01 10:00",
        project_name=title, buyer=buyer, category="采购意向", source="测试源-采购意向",
        budget_yuan=2_000_000, source_page_url=url, intent_item_no="1",
        procurement_category="A02000000设备", demand_summary="采购设备一套",
        expected_purchase_date="2026年03月",
    )


def tender(url="https://tender/1", title="设备采购项目", buyer="采购单位甲"):
    return FakeNotice(
        "t1", url, "招标公告", f"{title}公开招标公告", "2026-03-01 10:00",
        project_name=title, buyer=buyer, project_code="XM-P3-1",
    )


class P3IntentionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.sqlite"

    def tearDown(self):
        self.tmp.cleanup()

    def ingest(self, notices):
        ProcurementDatabase(self.db_path).ingest(notices)
        P1Processor(self.db_path).ingest(notices)
        P2LifecycleProcessor(self.db_path).ingest(notices)
        return P3IntentProcessor(self.db_path).ingest(notices)

    def test_daily_page_stop_rule_crosses_into_previous_day(self):
        self.assertFalse(daily_page_is_complete(["2026-07-20"] * 20, "2026-07-20"))
        self.assertTrue(
            daily_page_is_complete(["2026-07-20", "2026-07-19"], "2026-07-20")
        )
        self.assertTrue(daily_page_is_complete(["2026-07-19"] * 20, "2026-07-20"))

    def test_previous_day_mode_uses_yesterday_by_default(self):
        today = dt.date.fromisoformat(calendar_date("Asia/Shanghai"))
        previous_day = dt.date.fromisoformat(calendar_date("Asia/Shanghai", -1))
        self.assertEqual(dt.timedelta(days=1), today - previous_day)

    def test_daily_collection_paginates_until_older_notice(self):
        pages = {
            "index.htm": self._list_html([("a1", "2026-07-20"), ("a2", "2026-07-20")]),
            "index_1.htm": self._list_html([("a3", "2026-07-20"), ("old", "2026-07-19")]),
            "index_2.htm": self._list_html([("should-not-fetch", "2026-07-19")]),
        }
        crawler = Crawler(
            {
                "categories": ["公开招标"],
                "crawl_mode": "daily",
                "daily_date": "2026-07-20",
                "max_pages_per_category": 50,
            }
        )
        fetched = []

        def fake_fetch(url):
            name = url.rsplit("/", 1)[-1]
            fetched.append(name)
            return pages[name]

        crawler.fetch = fake_fetch
        notices = crawler.collect()
        self.assertEqual(["index.htm", "index_1.htm"], fetched)
        self.assertEqual({"a1", "a2", "a3"}, {notice.title for notice in notices})

    @staticmethod
    def _list_html(rows):
        items = "".join(
            f'<li><a href="/{title}" title="{title}">{title}</a>'
            f'<em>{date} 10:00:00</em><em>北京</em><em>采购单位甲</em></li>'
            for title, date in rows
        )
        return f'<ul class="c_list_bid">{items}</ul>'

    def connect(self):
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        return con

    def test_intention_is_project_level_demand_only(self):
        self.ingest([intent()])
        with closing(self.connect()) as con:
            item = con.execute("SELECT attribution_status, planned_budget FROM procurement_intent_item").fetchone()
            amount = con.execute("SELECT target_type, amount_type, allocation_status FROM amount_observation").fetchone()
            attempts = con.execute("SELECT COUNT(*) FROM procurement_attempt").fetchone()[0]
            packages = con.execute("SELECT COUNT(*) FROM package").fetchone()[0]
            supplier_roles = con.execute("SELECT COUNT(*) FROM document_organization_role WHERE role_type='SUPPLIER'").fetchone()[0]
        self.assertEqual(item["attribution_status"], "DEMAND_SIDE_ONLY")
        self.assertEqual(item["planned_budget"], 2_000_000)
        self.assertEqual((amount["target_type"], amount["amount_type"], amount["allocation_status"]), ("project", "INTENT_BUDGET", "DEMAND_SIDE_ONLY"))
        self.assertEqual(attempts, 0)
        self.assertEqual(packages, 0)
        self.assertEqual(supplier_roles, 0)

    def test_exact_buyer_and_title_auto_link_to_tender(self):
        self.ingest([intent(), tender()])
        with closing(self.connect()) as con:
            link = con.execute("SELECT * FROM current_intent_tender_link").fetchone()
        self.assertEqual(link["review_status"], "AUTO_ACCEPTED")
        self.assertGreaterEqual(link["match_score"], 0.90)
        self.assertIsNotNone(link["tender_attempt_uid"])

    def test_buyer_conflict_does_not_link(self):
        self.ingest([intent(), tender(buyer="另一采购单位")])
        with closing(self.connect()) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM intent_tender_link").fetchone()[0], 0)
            reason = con.execute("SELECT reason_codes_json FROM intent_match_review_queue").fetchone()[0]
        self.assertIn("NO_CANDIDATE", reason)

    def test_ambiguous_intentions_require_review(self):
        self.ingest([
            intent("https://intent/a"),
            intent("https://intent/b"),
            tender(),
        ])
        with closing(self.connect()) as con:
            link = con.execute("SELECT review_status FROM current_intent_tender_link").fetchone()
            reason = con.execute("SELECT reason_codes_json FROM intent_match_review_queue").fetchone()[0]
        self.assertEqual(link["review_status"], "NEEDS_REVIEW")
        self.assertIn("AMBIGUOUS_TOP_CANDIDATES", reason)

    def test_incremental_tender_matches_historical_intention_and_is_idempotent(self):
        self.ingest([intent()])
        later = self.ingest([tender()])
        repeated = self.ingest([tender()])
        self.assertEqual(later["intent_tender_links"], 1)
        self.assertTrue(all(value == 0 for value in repeated.values()))

    def test_intention_html_table_splits_items_and_converts_wan(self):
        html = """
        <html><body><div>2026年04月10日 17:19</div><table>
        <tr><th>序号</th><th>采购单位</th><th>采购项目名称</th><th>采购品目</th><th>采购需求概况</th><th>预算金额(万元)</th><th>预计采购日期</th><th>备注</th></tr>
        <tr><td>1</td><td>采购单位甲</td><td>设备采购项目</td><td>A02000000设备</td><td>采购设备一套</td><td>310.5</td><td>2026年06月</td><td>无</td></tr>
        <tr><td>2</td><td>采购单位甲</td><td>软件采购项目</td><td>C16000000服务</td><td>采购软件服务</td><td>80</td><td>2026年07月</td><td></td></tr>
        </table></body></html>
        """
        rows = parse_intention_page(html, "https://intent/page")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].budget_yuan, 3_105_000)
        self.assertEqual(rows[0].publish_time, "2026-04-10 17:19")
        self.assertNotEqual(rows[0].url, rows[1].url)

    def test_demand_signal_export_contains_no_supplier_dimension(self):
        self.ingest([intent()])
        P3IntentProcessor(self.db_path).export_csv(self.tmp.name)
        header = (Path(self.tmp.name) / "demand_signals.csv").read_text(encoding="utf-8-sig").splitlines()[0]
        self.assertEqual(header, "period_month,buyer_name,procurement_category,intention_count,planned_budget_yuan")
        self.assertNotIn("supplier", header)


if __name__ == "__main__":
    unittest.main()
