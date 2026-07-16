"""P2 procurement lifecycle reconciliation and quality monitoring.

P2 links corrections, deadline extensions, failed/terminated notices and
re-procurement tenders back to the affected attempt or package.  All decisions
retain candidate evidence and ambiguous cases are queued for manual review.
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable

from p0_database import detect_round
from p1_matching import (
    canonical_title,
    detect_package,
    normalize_identifier,
    normalize_org_name,
    now_iso,
    parse_time,
    uid,
)


P2_SCHEMA_VERSION = 3
BASE_STAGES = {"招标公告", "中标结果"}


def lifecycle_event_type(notice: object) -> str:
    stage = str(getattr(notice, "stage", ""))
    if stage == "更正":
        return "DEADLINE_EXTENDED" if int(getattr(notice, "is_delayed", 0) or 0) else "CORRECTION_PUBLISHED"
    if stage == "终止":
        return "PACKAGE_FAILED" if int(getattr(notice, "is_failed_bid", 0) or 0) else "PROJECT_TERMINATED"
    if stage == "招标公告" and (
        detect_round(str(getattr(notice, "title", ""))) > 1
        or re.search(r"重新(?:招标|采购)|二次|重招", str(getattr(notice, "title", "")))
    ):
        return "REPROCUREMENT_OPENED"
    return ""


@dataclass(frozen=True)
class LifecycleRecord:
    doc_uid: str
    version_uid: str
    stage: str
    event_type: str
    title: str
    title_key: str
    project_code: str
    buyer_key: str
    package_no: str
    round_no: int
    publish_time: str
    url: str


@dataclass(frozen=True)
class LifecycleCandidate:
    predecessor: LifecycleRecord
    event: LifecycleRecord
    relation_type: str
    score: float
    method: str
    evidence: dict[str, object]
    rejected_reason: str = ""


P2_DDL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS lifecycle_document_feature (
    source_version_uid TEXT PRIMARY KEY REFERENCES source_document_version(version_uid),
    source_doc_uid TEXT NOT NULL REFERENCES source_document(doc_uid),
    stage TEXT NOT NULL,
    lifecycle_event_type TEXT,
    title TEXT NOT NULL,
    title_key TEXT NOT NULL,
    project_code TEXT,
    buyer_key TEXT,
    package_no TEXT,
    round_no INTEGER NOT NULL,
    publish_time TEXT,
    url TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lifecycle_link_candidate (
    candidate_uid TEXT PRIMARY KEY,
    relation_type TEXT NOT NULL,
    predecessor_version_uid TEXT NOT NULL REFERENCES source_document_version(version_uid),
    event_version_uid TEXT NOT NULL REFERENCES source_document_version(version_uid),
    rank_no INTEGER NOT NULL,
    match_method TEXT NOT NULL,
    match_score REAL NOT NULL CHECK(match_score >= 0 AND match_score <= 1),
    evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(relation_type, predecessor_version_uid, event_version_uid)
);

CREATE TABLE IF NOT EXISTS lifecycle_link (
    lifecycle_link_uid TEXT PRIMARY KEY,
    relation_type TEXT NOT NULL,
    predecessor_doc_uid TEXT NOT NULL REFERENCES source_document(doc_uid),
    predecessor_version_uid TEXT NOT NULL REFERENCES source_document_version(version_uid),
    event_doc_uid TEXT NOT NULL REFERENCES source_document(doc_uid),
    event_version_uid TEXT NOT NULL REFERENCES source_document_version(version_uid),
    affected_object_type TEXT,
    affected_object_uid TEXT,
    event_object_type TEXT,
    event_object_uid TEXT,
    match_method TEXT NOT NULL,
    match_score REAL NOT NULL CHECK(match_score >= 0 AND match_score <= 1),
    confidence TEXT NOT NULL,
    review_status TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(relation_type, predecessor_version_uid, event_version_uid)
);

CREATE TABLE IF NOT EXISTS lifecycle_review_queue (
    review_uid TEXT PRIMARY KEY,
    lifecycle_link_uid TEXT REFERENCES lifecycle_link(lifecycle_link_uid),
    event_version_uid TEXT NOT NULL REFERENCES source_document_version(version_uid),
    relation_type TEXT NOT NULL,
    reason_codes_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    reviewer TEXT,
    decision TEXT,
    note TEXT,
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    UNIQUE(event_version_uid, relation_type)
);

CREATE INDEX IF NOT EXISTS idx_lifecycle_feature_stage ON lifecycle_document_feature(stage, publish_time);
CREATE INDEX IF NOT EXISTS idx_lifecycle_candidate_event ON lifecycle_link_candidate(event_version_uid, relation_type, rank_no);
CREATE INDEX IF NOT EXISTS idx_lifecycle_link_event ON lifecycle_link(event_version_uid, relation_type);
CREATE INDEX IF NOT EXISTS idx_lifecycle_review_status ON lifecycle_review_queue(status, relation_type);

CREATE VIEW IF NOT EXISTS current_lifecycle_link AS
SELECT *
FROM (
    SELECT l.*,
           ROW_NUMBER() OVER (
               PARTITION BY relation_type, event_version_uid
               ORDER BY match_score DESC, created_at DESC, lifecycle_link_uid DESC
           ) AS rn
    FROM lifecycle_link l
)
WHERE rn = 1;
"""


class P2LifecycleProcessor:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        return con

    def initialize(self, con: sqlite3.Connection) -> None:
        con.executescript(P2_DDL)
        con.execute(
            "INSERT OR IGNORE INTO schema_metadata(schema_version, applied_at) VALUES (?, ?)",
            (P2_SCHEMA_VERSION, now_iso()),
        )

    def ingest(self, notices: Iterable[object]) -> dict[str, int]:
        counts = {"lifecycle_features": 0, "lifecycle_candidates": 0, "lifecycle_links": 0, "review_items": 0}
        with closing(self.connect()) as con, con:
            self.initialize(con)
            for notice in notices:
                record = self._record(con, notice)
                if record and self._store_feature(con, record):
                    counts["lifecycle_features"] += 1
            records = self._load_latest_features(con)
            delta = self._link_events(con, records)
            for key, value in delta.items():
                counts[key] += value
        return counts

    def _record(self, con: sqlite3.Connection, notice: object) -> LifecycleRecord | None:
        source = str(getattr(notice, "source", "unknown"))
        url = str(getattr(notice, "url", ""))
        doc_uid = uid("doc", source, url)
        row = con.execute(
            """SELECT v.version_uid
               FROM source_document_version v
               WHERE v.doc_uid = ?
               ORDER BY v.version_no DESC LIMIT 1""",
            (doc_uid,),
        ).fetchone()
        if not row:
            return None
        title = str(getattr(notice, "title", ""))
        project_name = str(getattr(notice, "project_name", "") or title)
        return LifecycleRecord(
            doc_uid=doc_uid,
            version_uid=row["version_uid"],
            stage=str(getattr(notice, "stage", "")),
            event_type=lifecycle_event_type(notice),
            title=title,
            title_key=canonical_title(project_name),
            project_code=normalize_identifier(str(getattr(notice, "project_code", ""))),
            buyer_key=normalize_org_name(str(getattr(notice, "buyer", ""))),
            package_no=detect_package(title, str(getattr(notice, "raw_text", ""))),
            round_no=detect_round(title),
            publish_time=str(getattr(notice, "publish_time", "")),
            url=url,
        )

    def _store_feature(self, con: sqlite3.Connection, record: LifecycleRecord) -> bool:
        if con.execute("SELECT 1 FROM lifecycle_document_feature WHERE source_version_uid = ?", (record.version_uid,)).fetchone():
            return False
        con.execute(
            """INSERT INTO lifecycle_document_feature(
                   source_version_uid, source_doc_uid, stage, lifecycle_event_type,
                   title, title_key, project_code, buyer_key, package_no,
                   round_no, publish_time, url, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.version_uid, record.doc_uid, record.stage, record.event_type or None,
                record.title, record.title_key, record.project_code or None,
                record.buyer_key or None, record.package_no or None, record.round_no,
                record.publish_time or None, record.url, now_iso(),
            ),
        )
        return True

    def _load_latest_features(self, con: sqlite3.Connection) -> list[LifecycleRecord]:
        rows = con.execute(
            """SELECT f.*
               FROM lifecycle_document_feature f
               JOIN source_document_version v ON v.version_uid=f.source_version_uid
               JOIN (
                   SELECT doc_uid, MAX(version_no) AS version_no
                   FROM source_document_version GROUP BY doc_uid
               ) latest ON latest.doc_uid=v.doc_uid AND latest.version_no=v.version_no"""
        ).fetchall()
        return [
            LifecycleRecord(
                doc_uid=row["source_doc_uid"], version_uid=row["source_version_uid"],
                stage=row["stage"], event_type=row["lifecycle_event_type"] or "",
                title=row["title"], title_key=row["title_key"],
                project_code=row["project_code"] or "", buyer_key=row["buyer_key"] or "",
                package_no=row["package_no"] or "", round_no=row["round_no"],
                publish_time=row["publish_time"] or "", url=row["url"],
            )
            for row in rows
        ]

    @staticmethod
    def _relation_type(event: LifecycleRecord) -> str:
        return {
            "CORRECTION_PUBLISHED": "AMENDS",
            "DEADLINE_EXTENDED": "EXTENDS_DEADLINE",
            "PACKAGE_FAILED": "FAILS",
            "PROJECT_TERMINATED": "TERMINATES",
            "REPROCUREMENT_OPENED": "RETRY_OF",
        }.get(event.event_type, "")

    def _score(self, predecessor: LifecycleRecord, event: LifecycleRecord) -> LifecycleCandidate:
        relation_type = self._relation_type(event)
        evidence: dict[str, object] = {}
        before, after = parse_time(predecessor.publish_time), parse_time(event.publish_time)
        days = (after - before).days if before and after else None
        evidence["days_between"] = days
        if days is not None and (days < 0 or days > 730):
            return LifecycleCandidate(predecessor, event, relation_type, 0.0, "time_window_rejected", evidence, "OUTSIDE_TIME_WINDOW")
        if predecessor.project_code and event.project_code and predecessor.project_code != event.project_code:
            return LifecycleCandidate(predecessor, event, relation_type, 0.0, "project_code_conflict", evidence, "PROJECT_CODE_CONFLICT")
        if predecessor.package_no and event.package_no and predecessor.package_no != event.package_no:
            return LifecycleCandidate(predecessor, event, relation_type, 0.0, "package_conflict", evidence, "PACKAGE_CONFLICT")
        if relation_type == "RETRY_OF" and predecessor.round_no >= event.round_no:
            return LifecycleCandidate(predecessor, event, relation_type, 0.0, "round_order_rejected", evidence, "ROUND_NOT_INCREASING")

        title_similarity = SequenceMatcher(None, predecessor.title_key, event.title_key).ratio() if predecessor.title_key and event.title_key else 0.0
        buyer_similarity = SequenceMatcher(None, predecessor.buyer_key, event.buyer_key).ratio() if predecessor.buyer_key and event.buyer_key else 0.0
        project_exact = bool(predecessor.project_code and predecessor.project_code == event.project_code)
        package_exact = bool(predecessor.package_no and predecessor.package_no == event.package_no)
        evidence.update({
            "project_code_exact": project_exact,
            "package_exact": package_exact,
            "title_similarity": round(title_similarity, 4),
            "buyer_similarity": round(buyer_similarity, 4),
            "predecessor_round": predecessor.round_no,
            "event_round": event.round_no,
        })
        if project_exact:
            score = 0.65 + 0.12 * buyer_similarity + 0.12 * title_similarity
            methods = ["project_code_exact"]
            if package_exact:
                score += 0.07
                methods.append("package_exact")
        else:
            score = 0.35 * buyer_similarity + 0.50 * title_similarity
            methods = ["title_buyer_similarity"]
            if package_exact:
                score += 0.10
                methods.append("package_exact")
        if days is not None:
            score += max(0.0, 0.04 * (1 - days / 730))
            methods.append("time_proximity")
        return LifecycleCandidate(predecessor, event, relation_type, round(min(score, 1.0), 6), "+".join(methods), evidence)

    def _link_events(self, con: sqlite3.Connection, records: list[LifecycleRecord]) -> dict[str, int]:
        counts = {"lifecycle_candidates": 0, "lifecycle_links": 0, "review_items": 0}
        bases = [record for record in records if record.stage in BASE_STAGES]
        events = [record for record in records if record.event_type]
        for event in events:
            if event.event_type == "REPROCUREMENT_OPENED":
                predecessors = [record for record in bases if record.stage == "招标公告" and record.version_uid != event.version_uid]
            else:
                predecessors = [record for record in bases if record.version_uid != event.version_uid]
            scored = [self._score(record, event) for record in predecessors]
            valid = sorted((item for item in scored if not item.rejected_reason and item.score >= 0.45), key=lambda item: (-item.score, item.predecessor.publish_time, item.predecessor.doc_uid))[:3]
            relation_type = self._relation_type(event)
            for rank, item in enumerate(valid, 1):
                candidate_uid = uid("lca", relation_type, item.predecessor.version_uid, event.version_uid)
                if not con.execute("SELECT 1 FROM lifecycle_link_candidate WHERE candidate_uid=?", (candidate_uid,)).fetchone():
                    con.execute(
                        """INSERT INTO lifecycle_link_candidate(
                               candidate_uid, relation_type, predecessor_version_uid,
                               event_version_uid, rank_no, match_method, match_score,
                               evidence_json, created_at
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (candidate_uid, relation_type, item.predecessor.version_uid, event.version_uid, rank, item.method, item.score, json.dumps(item.evidence, ensure_ascii=False, sort_keys=True), now_iso()),
                    )
                    counts["lifecycle_candidates"] += 1
            if not valid:
                if self._queue_review(con, None, event, relation_type, ["NO_CANDIDATE"]):
                    counts["review_items"] += 1
                continue

            best = valid[0]
            margin = best.score - valid[1].score if len(valid) > 1 else 1.0
            exact_code = bool(best.evidence.get("project_code_exact"))
            ambiguous = margin < 0.05
            auto_accept = best.score >= 0.85 and exact_code and not ambiguous
            confidence = "HIGH" if best.score >= 0.85 else "MEDIUM" if best.score >= 0.70 else "LOW"
            review_status = "AUTO_ACCEPTED" if auto_accept else "NEEDS_REVIEW"
            affected_type = "package" if relation_type == "FAILS" else "attempt"
            affected_uid = self._object_uid(con, best.predecessor.version_uid, affected_type)
            event_object_uid = self._object_uid(con, event.version_uid, "attempt") if relation_type == "RETRY_OF" else None
            link_uid = uid("lif", relation_type, best.predecessor.version_uid, event.version_uid)
            evidence = dict(best.evidence)
            evidence.update({"candidate_count": len(valid), "runner_up_margin": round(margin, 6)})
            if not con.execute("SELECT 1 FROM lifecycle_link WHERE lifecycle_link_uid=?", (link_uid,)).fetchone():
                con.execute(
                    """INSERT INTO lifecycle_link(
                           lifecycle_link_uid, relation_type, predecessor_doc_uid,
                           predecessor_version_uid, event_doc_uid, event_version_uid,
                           affected_object_type, affected_object_uid,
                           event_object_type, event_object_uid, match_method,
                           match_score, confidence, review_status, evidence_json,
                           created_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        link_uid, relation_type, best.predecessor.doc_uid,
                        best.predecessor.version_uid, event.doc_uid, event.version_uid,
                        affected_type if affected_uid else None, affected_uid,
                        "attempt" if event_object_uid else None, event_object_uid,
                        best.method, best.score, confidence, review_status,
                        json.dumps(evidence, ensure_ascii=False, sort_keys=True), now_iso(),
                    ),
                )
                counts["lifecycle_links"] += 1
            if auto_accept:
                con.execute(
                    """UPDATE lifecycle_review_queue
                       SET lifecycle_link_uid=?, status='RESOLVED_AUTO', decision='ACCEPT', reviewed_at=?
                       WHERE event_version_uid=? AND relation_type=? AND status='PENDING'""",
                    (link_uid, now_iso(), event.version_uid, relation_type),
                )
            else:
                reasons = []
                if not exact_code:
                    reasons.append("NO_EXACT_PROJECT_CODE")
                if best.score < 0.85:
                    reasons.append("SCORE_BELOW_AUTO_THRESHOLD")
                if ambiguous:
                    reasons.append("AMBIGUOUS_TOP_CANDIDATES")
                if not affected_uid:
                    reasons.append("AFFECTED_OBJECT_NOT_RESOLVED")
                if self._queue_review(con, link_uid, event, relation_type, reasons):
                    counts["review_items"] += 1
        return counts

    @staticmethod
    def _object_uid(con: sqlite3.Connection, version_uid: str, object_type: str) -> str | None:
        row = con.execute(
            """SELECT object_uid FROM document_object_link
               WHERE source_version_uid=? AND object_type=?
               ORDER BY match_score DESC, created_at DESC LIMIT 1""",
            (version_uid, object_type),
        ).fetchone()
        return row["object_uid"] if row else None

    @staticmethod
    def _queue_review(con: sqlite3.Connection, link_uid: str | None, event: LifecycleRecord, relation_type: str, reasons: list[str]) -> bool:
        review_uid = uid("lrv", event.version_uid, relation_type)
        if con.execute("SELECT 1 FROM lifecycle_review_queue WHERE review_uid=?", (review_uid,)).fetchone():
            con.execute(
                """UPDATE lifecycle_review_queue
                   SET lifecycle_link_uid=COALESCE(?, lifecycle_link_uid), reason_codes_json=?
                   WHERE review_uid=? AND status='PENDING'""",
                (link_uid, json.dumps(reasons, ensure_ascii=False), review_uid),
            )
            return False
        con.execute(
            """INSERT INTO lifecycle_review_queue(
                   review_uid, lifecycle_link_uid, event_version_uid,
                   relation_type, reason_codes_json, status, created_at
               ) VALUES (?, ?, ?, ?, ?, 'PENDING', ?)""",
            (review_uid, link_uid, event.version_uid, relation_type, json.dumps(reasons, ensure_ascii=False), now_iso()),
        )
        return True

    def quality_metrics(self) -> list[dict[str, object]]:
        metrics: list[dict[str, object]] = []
        with closing(self.connect()) as con:
            con.row_factory = sqlite3.Row

            def add(metric: str, value: object, definition: str) -> None:
                metrics.append({"metric": metric, "value": value, "definition": definition})

            add("source_documents", con.execute("SELECT COUNT(*) FROM source_document").fetchone()[0], "来源逻辑文档数")
            add("document_versions", con.execute("SELECT COUNT(*) FROM source_document_version").fetchone()[0], "不可变文档版本数")
            add("documents_with_multiple_versions", con.execute("SELECT COUNT(*) FROM (SELECT doc_uid FROM source_document_version GROUP BY doc_uid HAVING COUNT(*)>1)").fetchone()[0], "发生网页内容变化的文档数")
            add("p1_current_chain_links", con.execute("SELECT COUNT(*) FROM current_document_chain_link").fetchone()[0], "P1当前招标—中标—合同链路数")
            add("p1_pending_reviews", con.execute("SELECT COUNT(*) FROM match_review_queue WHERE status='PENDING'").fetchone()[0], "P1待人工复核数")
            add("p2_current_lifecycle_links", con.execute("SELECT COUNT(*) FROM current_lifecycle_link").fetchone()[0], "P2当前更正、终止和重采关联数")
            add("p2_pending_reviews", con.execute("SELECT COUNT(*) FROM lifecycle_review_queue WHERE status='PENDING'").fetchone()[0], "P2待人工复核数")
            for row in con.execute("SELECT document_type, COUNT(*) AS n FROM source_document GROUP BY document_type ORDER BY document_type"):
                add(f"documents_stage_{row['document_type']}", row["n"], "按标准公告阶段统计的逻辑文档数")
        return metrics

    def export_csv(self, output_dir: str | Path) -> None:
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        queries = {
            "lifecycle_links.csv": """SELECT l.*, p.url AS predecessor_url, p.title AS predecessor_title,
                                              e.url AS event_url, e.title AS event_title
                                       FROM current_lifecycle_link l
                                       JOIN source_document p ON p.doc_uid=l.predecessor_doc_uid
                                       JOIN source_document e ON e.doc_uid=l.event_doc_uid
                                       ORDER BY e.publish_time, l.relation_type""",
            "lifecycle_review_queue.csv": """SELECT q.*, l.predecessor_doc_uid, l.event_doc_uid,
                                                     l.match_score, l.match_method
                                              FROM lifecycle_review_queue q
                                              LEFT JOIN lifecycle_link l ON l.lifecycle_link_uid=q.lifecycle_link_uid
                                              ORDER BY q.status, q.relation_type, q.created_at""",
            "object_lifecycle_state.csv": """SELECT s.*, p.package_no, a.source_project_no,
                                                      r.project_uid, r.canonical_title, r.buyer_name
                                               FROM current_object_state s
                                               LEFT JOIN package p ON s.target_type='package' AND p.package_uid=s.target_uid
                                               LEFT JOIN procurement_attempt a ON (
                                                   (s.target_type='attempt' AND a.attempt_uid=s.target_uid) OR
                                                   (s.target_type='package' AND a.attempt_uid=p.attempt_uid)
                                               )
                                               LEFT JOIN root_project r ON r.project_uid=a.project_uid
                                               ORDER BY s.available_time, s.target_type, s.target_uid""",
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
        quality = self.quality_metrics()
        with (out / "data_quality_metrics.csv").open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=["metric", "value", "definition"])
            writer.writeheader()
            writer.writerows(quality)
