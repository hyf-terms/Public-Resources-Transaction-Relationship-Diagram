"""P3 procurement-intention storage and conservative intent-to-tender matching."""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable

from p1_matching import canonical_title, normalize_org_name, now_iso, parse_time, uid


P3_SCHEMA_VERSION = 4


@dataclass(frozen=True)
class IntentRecord:
    intent_uid: str
    doc_uid: str
    version_uid: str
    buyer_name: str
    buyer_key: str
    project_name: str
    title_key: str
    procurement_category: str
    demand_summary: str
    budget_amount: float | None
    expected_purchase_date: str
    publish_time: str
    source_page_url: str


@dataclass(frozen=True)
class TenderRecord:
    doc_uid: str
    version_uid: str
    title: str
    title_key: str
    buyer_key: str
    publish_time: str
    url: str


P3_DDL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS procurement_intent_item (
    intent_uid TEXT PRIMARY KEY,
    source_doc_uid TEXT NOT NULL REFERENCES source_document(doc_uid),
    source_version_uid TEXT NOT NULL REFERENCES source_document_version(version_uid),
    item_no TEXT,
    buyer_name TEXT NOT NULL,
    buyer_key TEXT NOT NULL,
    project_name TEXT NOT NULL,
    title_key TEXT NOT NULL,
    procurement_category TEXT,
    demand_summary TEXT,
    planned_budget REAL,
    currency TEXT NOT NULL DEFAULT 'CNY',
    expected_purchase_date TEXT,
    remarks TEXT,
    publish_time TEXT,
    source_page_url TEXT,
    attribution_status TEXT NOT NULL DEFAULT 'DEMAND_SIDE_ONLY',
    created_at TEXT NOT NULL,
    UNIQUE(source_version_uid)
);

CREATE TABLE IF NOT EXISTS intent_tender_candidate (
    candidate_uid TEXT PRIMARY KEY,
    intent_uid TEXT NOT NULL REFERENCES procurement_intent_item(intent_uid),
    tender_doc_uid TEXT NOT NULL REFERENCES source_document(doc_uid),
    tender_version_uid TEXT NOT NULL REFERENCES source_document_version(version_uid),
    rank_no INTEGER NOT NULL,
    match_method TEXT NOT NULL,
    match_score REAL NOT NULL CHECK(match_score >= 0 AND match_score <= 1),
    evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(intent_uid, tender_version_uid)
);

CREATE TABLE IF NOT EXISTS intent_tender_link (
    intent_tender_link_uid TEXT PRIMARY KEY,
    intent_uid TEXT NOT NULL REFERENCES procurement_intent_item(intent_uid),
    tender_doc_uid TEXT NOT NULL REFERENCES source_document(doc_uid),
    tender_version_uid TEXT NOT NULL REFERENCES source_document_version(version_uid),
    tender_attempt_uid TEXT,
    match_method TEXT NOT NULL,
    match_score REAL NOT NULL CHECK(match_score >= 0 AND match_score <= 1),
    confidence TEXT NOT NULL,
    review_status TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(intent_uid, tender_version_uid)
);

CREATE TABLE IF NOT EXISTS intent_match_review_queue (
    review_uid TEXT PRIMARY KEY,
    intent_tender_link_uid TEXT REFERENCES intent_tender_link(intent_tender_link_uid),
    tender_version_uid TEXT NOT NULL REFERENCES source_document_version(version_uid),
    reason_codes_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    reviewer TEXT,
    decision TEXT,
    note TEXT,
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    UNIQUE(tender_version_uid)
);

CREATE TABLE IF NOT EXISTS intent_match_gold_label (
    label_uid TEXT PRIMARY KEY,
    intent_source_url TEXT NOT NULL,
    tender_url TEXT NOT NULL,
    is_match INTEGER NOT NULL CHECK(is_match IN (0, 1)),
    annotator TEXT,
    note TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(intent_source_url, tender_url)
);

CREATE INDEX IF NOT EXISTS idx_intent_buyer_date ON procurement_intent_item(buyer_key, publish_time);
CREATE INDEX IF NOT EXISTS idx_intent_candidate_tender ON intent_tender_candidate(tender_version_uid, rank_no);
CREATE INDEX IF NOT EXISTS idx_intent_review_status ON intent_match_review_queue(status);

CREATE VIEW IF NOT EXISTS current_intent_tender_link AS
SELECT *
FROM (
    SELECT l.*,
           ROW_NUMBER() OVER (
               PARTITION BY tender_version_uid
               ORDER BY match_score DESC, created_at DESC, intent_tender_link_uid DESC
           ) AS rn
    FROM intent_tender_link l
)
WHERE rn = 1;
"""


def _bigrams(value: str) -> set[str]:
    value = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", value or "").casefold()
    return {value[index : index + 2] for index in range(max(0, len(value) - 1))}


def _expected_month(value: str) -> str:
    match = re.search(r"(\d{4})\D+(\d{1,2})", value or "")
    return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}" if match else ""


class P3IntentProcessor:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        return con

    def initialize(self, con: sqlite3.Connection) -> None:
        con.executescript(P3_DDL)
        con.execute(
            "INSERT OR IGNORE INTO schema_metadata(schema_version, applied_at) VALUES (?, ?)",
            (P3_SCHEMA_VERSION, now_iso()),
        )

    def ingest(self, notices: Iterable[object]) -> dict[str, int]:
        counts = {"intent_items": 0, "intent_candidates": 0, "intent_tender_links": 0, "review_items": 0}
        with closing(self.connect()) as con, con:
            self.initialize(con)
            for notice in notices:
                if str(getattr(notice, "stage", "")) != "采购意向":
                    continue
                if self._store_intent(con, notice):
                    counts["intent_items"] += 1
            delta = self._match(con, self._load_intents(con), self._load_tenders(con))
            for key, value in delta.items():
                counts[key] += value
        return counts

    def _store_intent(self, con: sqlite3.Connection, notice: object) -> bool:
        source = str(getattr(notice, "source", "unknown"))
        url = str(getattr(notice, "url", ""))
        doc_uid = uid("doc", source, url)
        row = con.execute(
            "SELECT version_uid FROM source_document_version WHERE doc_uid=? ORDER BY version_no DESC LIMIT 1",
            (doc_uid,),
        ).fetchone()
        if not row:
            return False
        version_uid = row["version_uid"]
        if con.execute("SELECT 1 FROM procurement_intent_item WHERE source_version_uid=?", (version_uid,)).fetchone():
            return False
        buyer = str(getattr(notice, "buyer", "") or "")
        project_name = str(getattr(notice, "project_name", "") or getattr(notice, "title", ""))
        intent_uid = uid("int", version_uid)
        con.execute(
            """INSERT INTO procurement_intent_item(
                   intent_uid, source_doc_uid, source_version_uid, item_no,
                   buyer_name, buyer_key, project_name, title_key,
                   procurement_category, demand_summary, planned_budget,
                   currency, expected_purchase_date, remarks, publish_time,
                   source_page_url, attribution_status, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'CNY', ?, ?, ?, ?, 'DEMAND_SIDE_ONLY', ?)""",
            (
                intent_uid, doc_uid, version_uid, str(getattr(notice, "intent_item_no", "") or ""),
                buyer, normalize_org_name(buyer), project_name, canonical_title(project_name),
                str(getattr(notice, "procurement_category", "") or ""),
                str(getattr(notice, "demand_summary", "") or ""),
                getattr(notice, "budget_yuan", None),
                str(getattr(notice, "expected_purchase_date", "") or ""),
                str(getattr(notice, "intent_remarks", "") or ""),
                str(getattr(notice, "publish_time", "") or ""),
                str(getattr(notice, "source_page_url", "") or ""), now_iso(),
            ),
        )
        return True

    @staticmethod
    def _load_intents(con: sqlite3.Connection) -> list[IntentRecord]:
        return [
            IntentRecord(
                intent_uid=row["intent_uid"], doc_uid=row["source_doc_uid"], version_uid=row["source_version_uid"],
                buyer_name=row["buyer_name"], buyer_key=row["buyer_key"], project_name=row["project_name"],
                title_key=row["title_key"], procurement_category=row["procurement_category"] or "",
                demand_summary=row["demand_summary"] or "", budget_amount=row["planned_budget"],
                expected_purchase_date=row["expected_purchase_date"] or "", publish_time=row["publish_time"] or "",
                source_page_url=row["source_page_url"] or "",
            )
            for row in con.execute("SELECT * FROM procurement_intent_item")
        ]

    @staticmethod
    def _load_tenders(con: sqlite3.Connection) -> list[TenderRecord]:
        rows = con.execute(
            """SELECT f.* FROM document_match_feature f
               JOIN source_document_version v ON v.version_uid=f.source_version_uid
               JOIN (SELECT doc_uid, MAX(version_no) AS version_no FROM source_document_version GROUP BY doc_uid) latest
                 ON latest.doc_uid=v.doc_uid AND latest.version_no=v.version_no
               WHERE f.stage='招标公告'"""
        ).fetchall()
        return [
            TenderRecord(
                doc_uid=row["source_doc_uid"], version_uid=row["source_version_uid"], title=row["title"],
                title_key=row["title_key"], buyer_key=row["buyer_key"] or "",
                publish_time=row["publish_time"] or "", url=row["url"],
            )
            for row in rows
        ]

    @staticmethod
    def _score(intent: IntentRecord, tender: TenderRecord) -> tuple[float, str, dict[str, object], str]:
        intent_time, tender_time = parse_time(intent.publish_time), parse_time(tender.publish_time)
        days = (tender_time - intent_time).days if intent_time and tender_time else None
        evidence: dict[str, object] = {"days_between": days}
        if days is not None and (days < 0 or days > 730):
            return 0.0, "time_window_rejected", evidence, "OUTSIDE_TIME_WINDOW"
        buyer_similarity = SequenceMatcher(None, intent.buyer_key, tender.buyer_key).ratio() if intent.buyer_key and tender.buyer_key else 0.0
        if intent.buyer_key and tender.buyer_key and buyer_similarity < 0.75:
            return 0.0, "buyer_conflict", evidence, "BUYER_CONFLICT"
        title_similarity = SequenceMatcher(None, intent.title_key, tender.title_key).ratio() if intent.title_key and tender.title_key else 0.0
        demand_tokens = _bigrams(f"{intent.procurement_category}{intent.demand_summary}")
        tender_tokens = _bigrams(tender.title)
        category_overlap = len(demand_tokens & tender_tokens) / len(tender_tokens) if demand_tokens and tender_tokens else 0.0
        buyer_exact = bool(intent.buyer_key and intent.buyer_key == tender.buyer_key)
        score = 0.28 * buyer_similarity + 0.58 * title_similarity + 0.06 * min(category_overlap, 1.0)
        methods = ["buyer_title_similarity"]
        if days is not None:
            score += max(0.0, 0.08 * (1 - days / 730))
            methods.append("time_proximity")
        evidence.update({
            "buyer_exact": buyer_exact, "buyer_similarity": round(buyer_similarity, 4),
            "title_similarity": round(title_similarity, 4), "category_overlap": round(category_overlap, 4),
        })
        return round(min(score, 1.0), 6), "+".join(methods), evidence, ""

    def _match(self, con: sqlite3.Connection, intents: list[IntentRecord], tenders: list[TenderRecord]) -> dict[str, int]:
        counts = {"intent_candidates": 0, "intent_tender_links": 0, "review_items": 0}
        for tender in tenders:
            scored = []
            for intent in intents:
                score, method, evidence, rejected = self._score(intent, tender)
                if not rejected and score >= 0.55:
                    scored.append((score, method, evidence, intent))
            valid = sorted(scored, key=lambda item: (-item[0], item[3].publish_time, item[3].intent_uid))[:3]
            for rank, (score, method, evidence, intent) in enumerate(valid, 1):
                candidate_uid = uid("itc", intent.intent_uid, tender.version_uid)
                if not con.execute("SELECT 1 FROM intent_tender_candidate WHERE candidate_uid=?", (candidate_uid,)).fetchone():
                    con.execute(
                        """INSERT INTO intent_tender_candidate(
                               candidate_uid, intent_uid, tender_doc_uid, tender_version_uid,
                               rank_no, match_method, match_score, evidence_json, created_at
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (candidate_uid, intent.intent_uid, tender.doc_uid, tender.version_uid, rank, method, score, json.dumps(evidence, ensure_ascii=False, sort_keys=True), now_iso()),
                    )
                    counts["intent_candidates"] += 1
            if not valid:
                # Only queue tenders when at least one intention exists in the database.
                if intents and self._queue_review(con, None, tender, ["NO_CANDIDATE"]):
                    counts["review_items"] += 1
                continue
            score, method, evidence, best = valid[0]
            margin = score - valid[1][0] if len(valid) > 1 else 1.0
            auto_accept = score >= 0.90 and bool(evidence.get("buyer_exact")) and float(evidence.get("title_similarity", 0)) >= 0.90 and margin >= 0.08
            confidence = "HIGH" if score >= 0.90 else "MEDIUM" if score >= 0.75 else "LOW"
            review_status = "AUTO_ACCEPTED" if auto_accept else "NEEDS_REVIEW"
            link_uid = uid("itl", best.intent_uid, tender.version_uid)
            attempt_uid = self._attempt_uid(con, tender.version_uid)
            full_evidence = dict(evidence)
            full_evidence.update({"candidate_count": len(valid), "runner_up_margin": round(margin, 6), "demand_side_only": True})
            if not con.execute("SELECT 1 FROM intent_tender_link WHERE intent_tender_link_uid=?", (link_uid,)).fetchone():
                con.execute(
                    """INSERT INTO intent_tender_link(
                           intent_tender_link_uid, intent_uid, tender_doc_uid,
                           tender_version_uid, tender_attempt_uid, match_method,
                           match_score, confidence, review_status, evidence_json,
                           created_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (link_uid, best.intent_uid, tender.doc_uid, tender.version_uid, attempt_uid, method, score, confidence, review_status, json.dumps(full_evidence, ensure_ascii=False, sort_keys=True), now_iso()),
                )
                counts["intent_tender_links"] += 1
            if auto_accept:
                con.execute(
                    """UPDATE intent_match_review_queue SET intent_tender_link_uid=?, status='RESOLVED_AUTO',
                           decision='ACCEPT', reviewed_at=? WHERE tender_version_uid=? AND status='PENDING'""",
                    (link_uid, now_iso(), tender.version_uid),
                )
            else:
                reasons = []
                if not evidence.get("buyer_exact"):
                    reasons.append("BUYER_NOT_EXACT")
                if float(evidence.get("title_similarity", 0)) < 0.90:
                    reasons.append("TITLE_SIMILARITY_BELOW_AUTO_THRESHOLD")
                if score < 0.90:
                    reasons.append("SCORE_BELOW_AUTO_THRESHOLD")
                if margin < 0.08:
                    reasons.append("AMBIGUOUS_TOP_CANDIDATES")
                if self._queue_review(con, link_uid, tender, reasons):
                    counts["review_items"] += 1
        return counts

    @staticmethod
    def _attempt_uid(con: sqlite3.Connection, version_uid: str) -> str | None:
        row = con.execute(
            "SELECT object_uid FROM document_object_link WHERE source_version_uid=? AND object_type='attempt' ORDER BY match_score DESC LIMIT 1",
            (version_uid,),
        ).fetchone()
        return row["object_uid"] if row else None

    @staticmethod
    def _queue_review(con: sqlite3.Connection, link_uid: str | None, tender: TenderRecord, reasons: list[str]) -> bool:
        review_uid = uid("irv", tender.version_uid)
        if con.execute("SELECT 1 FROM intent_match_review_queue WHERE review_uid=?", (review_uid,)).fetchone():
            con.execute(
                "UPDATE intent_match_review_queue SET intent_tender_link_uid=COALESCE(?, intent_tender_link_uid), reason_codes_json=? WHERE review_uid=? AND status='PENDING'",
                (link_uid, json.dumps(reasons, ensure_ascii=False), review_uid),
            )
            return False
        con.execute(
            "INSERT INTO intent_match_review_queue(review_uid, intent_tender_link_uid, tender_version_uid, reason_codes_json, status, created_at) VALUES (?, ?, ?, ?, 'PENDING', ?)",
            (review_uid, link_uid, tender.version_uid, json.dumps(reasons, ensure_ascii=False), now_iso()),
        )
        return True

    def import_gold_labels(self, path: str | Path) -> int:
        count = 0
        with Path(path).open(encoding="utf-8-sig", newline="") as fh, closing(self.connect()) as con, con:
            self.initialize(con)
            for row in csv.DictReader(fh):
                intent_url, tender_url = str(row.get("intent_source_url", "")).strip(), str(row.get("tender_url", "")).strip()
                if not intent_url or not tender_url:
                    continue
                result = con.execute(
                    "INSERT OR IGNORE INTO intent_match_gold_label(label_uid, intent_source_url, tender_url, is_match, annotator, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (uid("igl", intent_url, tender_url), intent_url, tender_url, int(row.get("is_match", 0)), row.get("annotator", ""), row.get("note", ""), now_iso()),
                )
                count += result.rowcount
        return count

    def evaluate(self) -> dict[str, float | int | None]:
        with closing(self.connect()) as con:
            con.row_factory = sqlite3.Row
            labels = con.execute("SELECT * FROM intent_match_gold_label").fetchall()
            if not labels:
                return {"labeled_pairs": 0, "precision": None, "recall": None, "f1": None}
            predicted = {(row["intent_source_url"], row["tender_url"]) for row in con.execute(
                """SELECT i.source_page_url AS intent_source_url, d.url AS tender_url
                   FROM current_intent_tender_link l
                   JOIN procurement_intent_item i ON i.intent_uid=l.intent_uid
                   JOIN source_document d ON d.doc_uid=l.tender_doc_uid
                   WHERE l.review_status='AUTO_ACCEPTED'"""
            )}
            positive = {(row["intent_source_url"], row["tender_url"]) for row in labels if row["is_match"]}
            labeled_tenders = {row["tender_url"] for row in labels}
            predicted = {pair for pair in predicted if pair[1] in labeled_tenders}
            tp, fp, fn = len(predicted & positive), len(predicted - positive), len(positive - predicted)
            precision = tp / (tp + fp) if tp + fp else None
            recall = tp / (tp + fn) if tp + fn else None
            f1 = 2 * precision * recall / (precision + recall) if precision is not None and recall is not None and precision + recall else None
            return {"labeled_pairs": len(labels), "true_positive": tp, "false_positive": fp, "false_negative": fn, "precision": precision, "recall": recall, "f1": f1}

    def export_csv(self, output_dir: str | Path) -> None:
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        queries = {
            "intent_items.csv": "SELECT * FROM procurement_intent_item ORDER BY publish_time, intent_uid",
            "intent_tender_links.csv": """SELECT l.*, i.buyer_name, i.project_name AS intent_project_name,
                                                  i.planned_budget, i.expected_purchase_date, i.source_page_url,
                                                  d.title AS tender_title, d.url AS tender_url
                                           FROM current_intent_tender_link l
                                           JOIN procurement_intent_item i ON i.intent_uid=l.intent_uid
                                           JOIN source_document d ON d.doc_uid=l.tender_doc_uid
                                           ORDER BY d.publish_time, l.tender_doc_uid""",
            "intent_match_review_queue.csv": """SELECT q.*, l.intent_uid, l.match_score, l.match_method
                                                  FROM intent_match_review_queue q
                                                  LEFT JOIN intent_tender_link l ON l.intent_tender_link_uid=q.intent_tender_link_uid
                                                  ORDER BY q.status, q.created_at""",
        }
        with closing(self.connect()) as con:
            con.row_factory = sqlite3.Row
            for filename, query in queries.items():
                rows = con.execute(query).fetchall()
                fields = list(rows[0].keys()) if rows else ["empty"]
                with (out / filename).open("w", encoding="utf-8-sig", newline="") as fh:
                    writer = csv.DictWriter(fh, fieldnames=fields)
                    writer.writeheader()
                    writer.writerows(dict(row) for row in rows)
            signals = con.execute(
                """SELECT COALESCE(NULLIF(substr(expected_purchase_date,1,7),''), substr(publish_time,1,7)) AS period_month,
                          buyer_name, COALESCE(procurement_category,'') AS procurement_category,
                          COUNT(*) AS intention_count, SUM(planned_budget) AS planned_budget_yuan
                   FROM procurement_intent_item
                   GROUP BY period_month, buyer_name, procurement_category
                   ORDER BY period_month, buyer_name, procurement_category"""
            ).fetchall()
        with (out / "demand_signals.csv").open("w", encoding="utf-8-sig", newline="") as fh:
            fields = ["period_month", "buyer_name", "procurement_category", "intention_count", "planned_budget_yuan"]
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            writer.writerows(dict(row) for row in signals)


def write_intent_gold_template(path: str | Path) -> None:
    with Path(path).open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["intent_source_url", "tender_url", "is_match", "annotator", "note"])
        writer.writeheader()
