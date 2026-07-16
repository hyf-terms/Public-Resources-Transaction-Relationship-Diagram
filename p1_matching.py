"""P1 entity resolution and tender-award-contract document matching.

P0 preserves evidence and business events.  P1 adds a conservative, auditable
reconciliation layer: organizations are normalized without fuzzy auto-merging,
later-stage documents are compared with earlier-stage candidates, and ambiguous
links are sent to a review queue.
"""

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


P1_SCHEMA_VERSION = 2
STAGE_ORDER = {"招标公告": 1, "中标结果": 2, "采购合同": 3}
LINK_TYPES = {
    ("招标公告", "中标结果"): "TENDER_TO_AWARD",
    ("中标结果", "采购合同"): "AWARD_TO_CONTRACT",
    ("招标公告", "采购合同"): "TENDER_TO_CONTRACT",
}


def uid(prefix: str, *parts: object) -> str:
    raw = "\x1f".join("" if value is None else str(value).strip().casefold() for value in parts)
    return f"{prefix}_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def normalize_identifier(value: str | None) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", value or "", flags=re.I).casefold()


def normalize_org_name(value: str | None) -> str:
    """Normalize presentation differences but retain legal suffixes.

    Removing 有限公司/集团 etc. would over-merge distinct legal entities, so P1
    deliberately keeps those tokens.  Cross-name merges require configured aliases
    or manual review.
    """

    text = (value or "").strip().replace("（", "(").replace("）", ")")
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[·•,，。;；:：'\"“”‘’]", "", text)
    return text.casefold()


def canonical_title(value: str | None) -> str:
    text = value or ""
    text = re.sub(r"[（(](?:第?\s*[一二三四五六七八九十\d]+\s*次|重招|重新招标|重新采购)[）)]", "", text)
    text = re.sub(r"(?:第?\s*[一二三四五六七八九十\d]+\s*次|二次|重新招标|重新采购|重招)", "", text)
    text = re.sub(r"(?:第\s*)?[一二三四五六七八九十\d]+\s*(?:包|标包)", "", text)
    text = re.sub(r"(?:包|标包)\s*[一二三四五六七八九十\d]+", "", text)
    text = re.sub(r"(?:公开招标|竞争性磋商|竞争性谈判|询价|单一来源)?(?:采购)?(?:中标|成交|合同|更正|终止|废标|流标|失败|结果)?(?:公告|公示)$", "", text)
    return normalize_identifier(text)


def detect_package(title: str, raw_text: str = "") -> str:
    sample = f"{title}\n{raw_text[:3000]}"
    cn = {"一": "1", "二": "2", "三": "3", "四": "4", "五": "5", "六": "6", "七": "7", "八": "8", "九": "9", "十": "10"}
    for pattern in (
        r"(?:第\s*)?(\d+)\s*(?:包|标包)",
        r"(?:包|标包)\s*(\d+)",
        r"(?:第\s*)?([一二三四五六七八九十])\s*(?:包|标包)",
    ):
        match = re.search(pattern, sample)
        if match:
            return cn.get(match.group(1), match.group(1)).lstrip("0") or "0"
    return ""


def parse_time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace(" ", "T"))
        return parsed.replace(tzinfo=None)
    except ValueError:
        return None


def split_names(value: str | None) -> list[str]:
    return [part.strip() for part in re.split(r"\s*\|\s*", value or "") if part.strip()]


@dataclass(frozen=True)
class DocumentRecord:
    doc_uid: str
    version_uid: str
    stage: str
    title: str
    title_key: str
    project_code: str
    buyer_key: str
    package_no: str
    supplier_keys: frozenset[str]
    publish_time: str
    url: str


@dataclass(frozen=True)
class CandidateScore:
    source: DocumentRecord
    target: DocumentRecord
    link_type: str
    score: float
    method: str
    evidence: dict[str, object]
    disqualified_reason: str = ""


P1_DDL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS organization (
    org_uid TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    org_type TEXT NOT NULL,
    unified_social_credit_code TEXT,
    listed_code TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(normalized_name, org_type)
);

CREATE TABLE IF NOT EXISTS organization_alias (
    alias_uid TEXT PRIMARY KEY,
    org_uid TEXT NOT NULL REFERENCES organization(org_uid),
    alias_name TEXT NOT NULL,
    normalized_alias TEXT NOT NULL,
    alias_source TEXT NOT NULL,
    confidence TEXT NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(normalized_alias, org_uid)
);

CREATE TABLE IF NOT EXISTS document_organization_role (
    role_uid TEXT PRIMARY KEY,
    source_doc_uid TEXT NOT NULL REFERENCES source_document(doc_uid),
    source_version_uid TEXT NOT NULL REFERENCES source_document_version(version_uid),
    org_uid TEXT NOT NULL REFERENCES organization(org_uid),
    role_type TEXT NOT NULL,
    raw_name TEXT NOT NULL,
    match_method TEXT NOT NULL,
    match_score REAL NOT NULL CHECK(match_score >= 0 AND match_score <= 1),
    review_status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(source_version_uid, org_uid, role_type, raw_name)
);

CREATE TABLE IF NOT EXISTS document_match_feature (
    source_version_uid TEXT PRIMARY KEY REFERENCES source_document_version(version_uid),
    source_doc_uid TEXT NOT NULL REFERENCES source_document(doc_uid),
    stage TEXT NOT NULL,
    title TEXT NOT NULL,
    title_key TEXT NOT NULL,
    project_code TEXT,
    buyer_key TEXT,
    package_no TEXT,
    supplier_keys_json TEXT NOT NULL DEFAULT '[]',
    publish_time TEXT,
    url TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document_chain_candidate (
    candidate_uid TEXT PRIMARY KEY,
    link_type TEXT NOT NULL,
    source_doc_uid TEXT NOT NULL REFERENCES source_document(doc_uid),
    source_version_uid TEXT NOT NULL REFERENCES source_document_version(version_uid),
    target_doc_uid TEXT NOT NULL REFERENCES source_document(doc_uid),
    target_version_uid TEXT NOT NULL REFERENCES source_document_version(version_uid),
    rank_no INTEGER NOT NULL,
    match_method TEXT NOT NULL,
    match_score REAL NOT NULL CHECK(match_score >= 0 AND match_score <= 1),
    evidence_json TEXT NOT NULL,
    disqualified_reason TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(link_type, source_version_uid, target_version_uid)
);

CREATE TABLE IF NOT EXISTS document_chain_link (
    chain_link_uid TEXT PRIMARY KEY,
    link_type TEXT NOT NULL,
    source_doc_uid TEXT NOT NULL REFERENCES source_document(doc_uid),
    source_version_uid TEXT NOT NULL REFERENCES source_document_version(version_uid),
    target_doc_uid TEXT NOT NULL REFERENCES source_document(doc_uid),
    target_version_uid TEXT NOT NULL REFERENCES source_document_version(version_uid),
    match_method TEXT NOT NULL,
    match_score REAL NOT NULL CHECK(match_score >= 0 AND match_score <= 1),
    confidence TEXT NOT NULL,
    review_status TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(link_type, source_version_uid, target_version_uid)
);

CREATE TABLE IF NOT EXISTS match_review_queue (
    review_uid TEXT PRIMARY KEY,
    chain_link_uid TEXT REFERENCES document_chain_link(chain_link_uid),
    target_version_uid TEXT NOT NULL REFERENCES source_document_version(version_uid),
    link_type TEXT NOT NULL,
    reason_codes_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    reviewer TEXT,
    decision TEXT,
    note TEXT,
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    UNIQUE(target_version_uid, link_type)
);

CREATE TABLE IF NOT EXISTS match_gold_label (
    label_uid TEXT PRIMARY KEY,
    link_type TEXT NOT NULL,
    source_url TEXT,
    target_url TEXT NOT NULL,
    is_match INTEGER NOT NULL CHECK(is_match IN (0, 1)),
    package_same INTEGER,
    annotator TEXT,
    note TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(link_type, source_url, target_url)
);

CREATE INDEX IF NOT EXISTS idx_org_normalized ON organization(normalized_name);
CREATE INDEX IF NOT EXISTS idx_alias_normalized ON organization_alias(normalized_alias);
CREATE INDEX IF NOT EXISTS idx_role_doc ON document_organization_role(source_version_uid, role_type);
CREATE INDEX IF NOT EXISTS idx_feature_stage ON document_match_feature(stage, publish_time);
CREATE INDEX IF NOT EXISTS idx_candidate_target ON document_chain_candidate(target_version_uid, link_type, rank_no);
CREATE INDEX IF NOT EXISTS idx_chain_source ON document_chain_link(source_version_uid, link_type);
CREATE INDEX IF NOT EXISTS idx_review_status ON match_review_queue(status, link_type);

CREATE VIEW IF NOT EXISTS current_document_chain_link AS
SELECT *
FROM (
    SELECT l.*,
           ROW_NUMBER() OVER (
               PARTITION BY link_type, target_version_uid
               ORDER BY match_score DESC, created_at DESC, chain_link_uid DESC
           ) AS rn
    FROM document_chain_link l
)
WHERE rn = 1;
"""


class P1Processor:
    def __init__(self, path: str | Path, companies: Iterable[dict] = ()):
        self.path = Path(path)
        self.companies = list(companies or [])
        self.alias_map: dict[str, tuple[str, str, str]] = {}
        for company in self.companies:
            canonical = str(company.get("name", "")).strip()
            if not canonical:
                continue
            listed_code = str(company.get("listed_code", "") or "")
            for alias in [canonical, *company.get("aliases", [])]:
                key = normalize_org_name(str(alias))
                if key:
                    self.alias_map[key] = (canonical, "LISTED_COMPANY", listed_code)

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        return con

    def initialize(self, con: sqlite3.Connection) -> None:
        con.executescript(P1_DDL)
        con.execute(
            "INSERT OR IGNORE INTO schema_metadata(schema_version, applied_at) VALUES (?, ?)",
            (P1_SCHEMA_VERSION, now_iso()),
        )

    def ingest(self, notices: Iterable[object]) -> dict[str, int]:
        rows = list(notices)
        counts = {"organizations": 0, "aliases": 0, "organization_roles": 0, "match_features": 0, "chain_candidates": 0, "chain_links": 0, "review_items": 0}
        with closing(self.connect()) as con, con:
            self.initialize(con)
            for notice in rows:
                doc_uid, version_uid = self._document_ids(con, notice)
                if not doc_uid or not version_uid:
                    continue
                delta = self._ingest_organizations(con, notice, doc_uid, version_uid)
                for key, value in delta.items():
                    counts[key] += value
                record = self._record(notice, doc_uid, version_uid)
                if record.stage in STAGE_ORDER:
                    if self._store_feature(con, record):
                        counts["match_features"] += 1
            records = self._load_features(con)
            delta = self._match_chains(con, records)
            for key, value in delta.items():
                counts[key] += value
        return counts

    def _store_feature(self, con: sqlite3.Connection, record: DocumentRecord) -> bool:
        if con.execute("SELECT 1 FROM document_match_feature WHERE source_version_uid = ?", (record.version_uid,)).fetchone():
            return False
        con.execute(
            """INSERT INTO document_match_feature(
                   source_version_uid, source_doc_uid, stage, title, title_key,
                   project_code, buyer_key, package_no, supplier_keys_json,
                   publish_time, url, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.version_uid, record.doc_uid, record.stage, record.title,
                record.title_key, record.project_code or None,
                record.buyer_key or None, record.package_no or None,
                json.dumps(sorted(record.supplier_keys), ensure_ascii=False),
                record.publish_time or None, record.url, now_iso(),
            ),
        )
        return True

    def _load_features(self, con: sqlite3.Connection) -> list[DocumentRecord]:
        """Load only the latest stored version of each logical source document."""

        rows = con.execute(
            """SELECT f.*
               FROM document_match_feature f
               JOIN source_document_version v ON v.version_uid = f.source_version_uid
               JOIN (
                   SELECT doc_uid, MAX(version_no) AS version_no
                   FROM source_document_version
                   GROUP BY doc_uid
               ) latest ON latest.doc_uid = v.doc_uid AND latest.version_no = v.version_no"""
        ).fetchall()
        return [
            DocumentRecord(
                doc_uid=row["source_doc_uid"], version_uid=row["source_version_uid"],
                stage=row["stage"], title=row["title"], title_key=row["title_key"],
                project_code=row["project_code"] or "", buyer_key=row["buyer_key"] or "",
                package_no=row["package_no"] or "",
                supplier_keys=frozenset(json.loads(row["supplier_keys_json"] or "[]")),
                publish_time=row["publish_time"] or "", url=row["url"],
            )
            for row in rows
        ]

    def _document_ids(self, con: sqlite3.Connection, notice: object) -> tuple[str, str]:
        source = str(getattr(notice, "source", "unknown"))
        url = str(getattr(notice, "url", ""))
        doc_uid = uid("doc", source, url)
        content_hash = str(getattr(notice, "content_hash", "") or hashlib.sha256(str(getattr(notice, "raw_text", "")).encode("utf-8")).hexdigest())
        version_uid = uid("ver", doc_uid, content_hash)
        found = con.execute(
            "SELECT version_uid FROM source_document_version WHERE version_uid = ?",
            (version_uid,),
        ).fetchone()
        return (doc_uid, version_uid) if found else ("", "")

    def _resolve_org(self, raw_name: str, role_type: str) -> tuple[str, str, str, str, str]:
        normalized = normalize_org_name(raw_name)
        configured = self.alias_map.get(normalized)
        if configured:
            canonical, org_type, listed_code = configured
            return canonical, normalize_org_name(canonical), org_type, listed_code, "configured_alias"
        org_type = "BUYER" if role_type == "BUYER" else "SUPPLIER"
        return raw_name.strip(), normalized, org_type, "", "normalized_exact"

    def _ingest_organizations(self, con: sqlite3.Connection, notice: object, doc_uid: str, version_uid: str) -> dict[str, int]:
        counts = {"organizations": 0, "aliases": 0, "organization_roles": 0}
        names: list[tuple[str, str]] = []
        buyer = str(getattr(notice, "buyer", "") or "").strip()
        if buyer:
            names.append((buyer, "BUYER"))
        names.extend((name, "SUPPLIER") for name in split_names(getattr(notice, "supplier_names", "")))
        for raw_name, role_type in names:
            canonical, normalized, org_type, listed_code, method = self._resolve_org(raw_name, role_type)
            if not normalized:
                continue
            row = con.execute(
                "SELECT org_uid FROM organization WHERE normalized_name = ? AND org_type = ?",
                (normalized, org_type),
            ).fetchone()
            org_uid = row["org_uid"] if row else uid("org", org_type, normalized)
            if not row:
                con.execute(
                    "INSERT INTO organization(org_uid, canonical_name, normalized_name, org_type, unified_social_credit_code, listed_code, created_at) VALUES (?, ?, ?, ?, NULL, ?, ?)",
                    (org_uid, canonical, normalized, org_type, listed_code or None, now_iso()),
                )
                counts["organizations"] += 1
            alias_uid = uid("als", org_uid, normalize_org_name(raw_name))
            if not con.execute("SELECT 1 FROM organization_alias WHERE alias_uid = ?", (alias_uid,)).fetchone():
                con.execute(
                    "INSERT INTO organization_alias(alias_uid, org_uid, alias_name, normalized_alias, alias_source, confidence, valid_from, valid_to, created_at) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?)",
                    (alias_uid, org_uid, raw_name, normalize_org_name(raw_name), "CONFIG" if method == "configured_alias" else "DOCUMENT", "HIGH", now_iso()),
                )
                counts["aliases"] += 1
            role_uid = uid("rol", version_uid, org_uid, role_type, raw_name)
            if not con.execute("SELECT 1 FROM document_organization_role WHERE role_uid = ?", (role_uid,)).fetchone():
                con.execute(
                    "INSERT INTO document_organization_role(role_uid, source_doc_uid, source_version_uid, org_uid, role_type, raw_name, match_method, match_score, review_status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (role_uid, doc_uid, version_uid, org_uid, role_type, raw_name, method, 1.0, "AUTO_ACCEPTED", now_iso()),
                )
                counts["organization_roles"] += 1
        return counts

    def _record(self, notice: object, doc_uid: str, version_uid: str) -> DocumentRecord:
        title = str(getattr(notice, "project_name", "") or getattr(notice, "title", ""))
        return DocumentRecord(
            doc_uid=doc_uid,
            version_uid=version_uid,
            stage=str(getattr(notice, "stage", "")),
            title=str(getattr(notice, "title", "")),
            title_key=canonical_title(title),
            project_code=normalize_identifier(str(getattr(notice, "project_code", ""))),
            buyer_key=normalize_org_name(str(getattr(notice, "buyer", ""))),
            package_no=detect_package(str(getattr(notice, "title", "")), str(getattr(notice, "raw_text", ""))),
            supplier_keys=frozenset(normalize_org_name(x) for x in split_names(getattr(notice, "supplier_names", ""))),
            publish_time=str(getattr(notice, "publish_time", "")),
            url=str(getattr(notice, "url", "")),
        )

    def _score(self, source: DocumentRecord, target: DocumentRecord) -> CandidateScore:
        link_type = LINK_TYPES[(source.stage, target.stage)]
        evidence: dict[str, object] = {}
        source_time, target_time = parse_time(source.publish_time), parse_time(target.publish_time)
        days = (target_time - source_time).days if source_time and target_time else None
        evidence["days_between"] = days
        if days is not None and (days < 0 or days > (730 if target.stage == "采购合同" else 365)):
            return CandidateScore(source, target, link_type, 0.0, "time_window_rejected", evidence, "OUTSIDE_TIME_WINDOW")
        if source.project_code and target.project_code and source.project_code != target.project_code:
            return CandidateScore(source, target, link_type, 0.0, "project_code_conflict", evidence, "PROJECT_CODE_CONFLICT")
        if source.package_no and target.package_no and source.package_no != target.package_no:
            return CandidateScore(source, target, link_type, 0.0, "package_conflict", evidence, "PACKAGE_CONFLICT")

        title_similarity = SequenceMatcher(None, source.title_key, target.title_key).ratio() if source.title_key and target.title_key else 0.0
        buyer_similarity = SequenceMatcher(None, source.buyer_key, target.buyer_key).ratio() if source.buyer_key and target.buyer_key else 0.0
        project_exact = bool(source.project_code and source.project_code == target.project_code)
        package_exact = bool(source.package_no and source.package_no == target.package_no)
        supplier_overlap = bool(source.supplier_keys and target.supplier_keys and source.supplier_keys & target.supplier_keys)
        evidence.update({
            "project_code_exact": project_exact,
            "package_exact": package_exact,
            "title_similarity": round(title_similarity, 4),
            "buyer_similarity": round(buyer_similarity, 4),
            "supplier_overlap": supplier_overlap,
        })

        if project_exact:
            score = 0.62 + 0.14 * buyer_similarity + 0.12 * title_similarity
            method_parts = ["project_code_exact"]
            if package_exact:
                score += 0.08
                method_parts.append("package_exact")
            if supplier_overlap and link_type == "AWARD_TO_CONTRACT":
                score += 0.04
                method_parts.append("supplier_overlap")
        else:
            score = 0.34 * buyer_similarity + 0.46 * title_similarity
            method_parts = ["title_buyer_similarity"]
            if package_exact:
                score += 0.12
                method_parts.append("package_exact")
            if supplier_overlap and link_type == "AWARD_TO_CONTRACT":
                score += 0.05
                method_parts.append("supplier_overlap")
        if days is not None:
            score += max(0.0, 0.08 * (1 - days / (730 if target.stage == "采购合同" else 365)))
            method_parts.append("time_proximity")
        return CandidateScore(source, target, link_type, round(min(score, 1.0), 6), "+".join(method_parts), evidence)

    def _match_chains(self, con: sqlite3.Connection, records: list[DocumentRecord]) -> dict[str, int]:
        counts = {"chain_candidates": 0, "chain_links": 0, "review_items": 0}
        sources_by_stage = {stage: [r for r in records if r.stage == stage] for stage in STAGE_ORDER}
        for target in records:
            source_stages = []
            if target.stage == "中标结果":
                source_stages = ["招标公告"]
            elif target.stage == "采购合同":
                source_stages = ["中标结果", "招标公告"]
            for source_stage in source_stages:
                scores = [self._score(source, target) for source in sources_by_stage[source_stage] if source.version_uid != target.version_uid]
                valid = sorted((x for x in scores if not x.disqualified_reason and x.score >= 0.45), key=lambda x: (-x.score, x.source.publish_time, x.source.doc_uid))[:3]
                for rank, candidate in enumerate(valid, 1):
                    candidate_uid = uid("can", candidate.link_type, candidate.source.version_uid, target.version_uid)
                    if not con.execute("SELECT 1 FROM document_chain_candidate WHERE candidate_uid = ?", (candidate_uid,)).fetchone():
                        con.execute(
                            "INSERT INTO document_chain_candidate(candidate_uid, link_type, source_doc_uid, source_version_uid, target_doc_uid, target_version_uid, rank_no, match_method, match_score, evidence_json, disqualified_reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                            (candidate_uid, candidate.link_type, candidate.source.doc_uid, candidate.source.version_uid, target.doc_uid, target.version_uid, rank, candidate.method, candidate.score, json.dumps(candidate.evidence, ensure_ascii=False, sort_keys=True), now_iso()),
                        )
                        counts["chain_candidates"] += 1
                if not valid:
                    if self._queue_review(con, None, target, LINK_TYPES[(source_stage, target.stage)], ["NO_CANDIDATE"]):
                        counts["review_items"] += 1
                    continue
                best = valid[0]
                margin = best.score - valid[1].score if len(valid) > 1 else 1.0
                exact_key = bool(best.evidence.get("project_code_exact"))
                ambiguous = margin < 0.05
                auto_accept = best.score >= 0.85 and exact_key and not ambiguous
                confidence = "HIGH" if best.score >= 0.85 else "MEDIUM" if best.score >= 0.70 else "LOW"
                review_status = "AUTO_ACCEPTED" if auto_accept else "NEEDS_REVIEW"
                chain_link_uid = uid("chn", best.link_type, best.source.version_uid, target.version_uid)
                if not con.execute("SELECT 1 FROM document_chain_link WHERE chain_link_uid = ?", (chain_link_uid,)).fetchone():
                    evidence = dict(best.evidence)
                    evidence.update({"candidate_count": len(valid), "runner_up_margin": round(margin, 6)})
                    con.execute(
                        "INSERT INTO document_chain_link(chain_link_uid, link_type, source_doc_uid, source_version_uid, target_doc_uid, target_version_uid, match_method, match_score, confidence, review_status, evidence_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (chain_link_uid, best.link_type, best.source.doc_uid, best.source.version_uid, target.doc_uid, target.version_uid, best.method, best.score, confidence, review_status, json.dumps(evidence, ensure_ascii=False, sort_keys=True), now_iso()),
                    )
                    counts["chain_links"] += 1
                if not auto_accept:
                    reasons = []
                    if not exact_key:
                        reasons.append("NO_EXACT_PROJECT_CODE")
                    if best.score < 0.85:
                        reasons.append("SCORE_BELOW_AUTO_THRESHOLD")
                    if ambiguous:
                        reasons.append("AMBIGUOUS_TOP_CANDIDATES")
                    if self._queue_review(con, chain_link_uid, target, best.link_type, reasons):
                        counts["review_items"] += 1
                else:
                    con.execute(
                        """UPDATE match_review_queue
                           SET chain_link_uid = ?, status = 'RESOLVED_AUTO',
                               decision = 'ACCEPT', reviewed_at = ?
                           WHERE target_version_uid = ? AND link_type = ? AND status = 'PENDING'""",
                        (chain_link_uid, now_iso(), target.version_uid, best.link_type),
                    )
                # A contract linked to an award does not also need a tender fallback.
                if target.stage == "采购合同" and source_stage == "中标结果" and best.score >= 0.70:
                    break
        return counts

    def _queue_review(self, con: sqlite3.Connection, chain_link_uid: str | None, target: DocumentRecord, link_type: str, reasons: list[str]) -> bool:
        review_uid = uid("rev", target.version_uid, link_type)
        if con.execute("SELECT 1 FROM match_review_queue WHERE review_uid = ?", (review_uid,)).fetchone():
            con.execute(
                """UPDATE match_review_queue
                   SET chain_link_uid = COALESCE(?, chain_link_uid),
                       reason_codes_json = ?
                   WHERE review_uid = ? AND status = 'PENDING'""",
                (chain_link_uid, json.dumps(reasons, ensure_ascii=False), review_uid),
            )
            return False
        con.execute(
            "INSERT INTO match_review_queue(review_uid, chain_link_uid, target_version_uid, link_type, reason_codes_json, status, created_at) VALUES (?, ?, ?, ?, ?, 'PENDING', ?)",
            (review_uid, chain_link_uid, target.version_uid, link_type, json.dumps(reasons, ensure_ascii=False), now_iso()),
        )
        return True

    def import_gold_labels(self, path: str | Path) -> int:
        count = 0
        with Path(path).open(encoding="utf-8-sig", newline="") as fh, closing(self.connect()) as con, con:
            self.initialize(con)
            for row in csv.DictReader(fh):
                link_type = str(row.get("link_type", "")).strip()
                target_url = str(row.get("target_url", "")).strip()
                source_url = str(row.get("source_url", "")).strip()
                if link_type not in LINK_TYPES.values() or not target_url:
                    continue
                label_uid = uid("gld", link_type, source_url, target_url)
                result = con.execute(
                    "INSERT OR IGNORE INTO match_gold_label(label_uid, link_type, source_url, target_url, is_match, package_same, annotator, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (label_uid, link_type, source_url or None, target_url, int(row.get("is_match", 0)), int(row["package_same"]) if row.get("package_same", "") != "" else None, row.get("annotator", ""), row.get("note", ""), now_iso()),
                )
                count += result.rowcount
        return count

    def evaluate(self) -> dict[str, float | int | None]:
        with closing(self.connect()) as con:
            labels = con.execute("SELECT * FROM match_gold_label").fetchall()
            if not labels:
                return {"labeled_pairs": 0, "precision": None, "recall": None, "f1": None}
            predicted = {(row["link_type"], row["source_url"], row["target_url"]) for row in con.execute(
                """SELECT l.link_type, s.url AS source_url, t.url AS target_url
                   FROM current_document_chain_link l
                   JOIN source_document s ON s.doc_uid = l.source_doc_uid
                   JOIN source_document t ON t.doc_uid = l.target_doc_uid
                   WHERE l.review_status = 'AUTO_ACCEPTED'"""
            )}
            positive = {(row["link_type"], row["source_url"], row["target_url"]) for row in labels if row["is_match"]}
            universe_targets = {(row["link_type"], row["target_url"]) for row in labels}
            predicted = {item for item in predicted if (item[0], item[2]) in universe_targets}
            tp = len(predicted & positive)
            fp = len(predicted - positive)
            fn = len(positive - predicted)
            precision = tp / (tp + fp) if tp + fp else None
            recall = tp / (tp + fn) if tp + fn else None
            f1 = 2 * precision * recall / (precision + recall) if precision is not None and recall is not None and precision + recall else None
            return {"labeled_pairs": len(labels), "true_positive": tp, "false_positive": fp, "false_negative": fn, "precision": precision, "recall": recall, "f1": f1}

    def export_csv(self, output_dir: str | Path) -> None:
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        queries = {
            "organizations.csv": "SELECT * FROM organization ORDER BY org_type, canonical_name",
            "chain_links.csv": """SELECT l.*, s.url AS source_url, s.title AS source_title, t.url AS target_url, t.title AS target_title
                                  FROM current_document_chain_link l JOIN source_document s ON s.doc_uid=l.source_doc_uid JOIN source_document t ON t.doc_uid=l.target_doc_uid
                                  ORDER BY l.link_type, t.publish_time, l.target_doc_uid""",
            "match_review_queue.csv": """SELECT q.*, l.source_doc_uid, l.target_doc_uid, l.match_score, l.match_method
                                            FROM match_review_queue q LEFT JOIN document_chain_link l ON l.chain_link_uid=q.chain_link_uid
                                            ORDER BY q.status, q.link_type, q.created_at""",
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


def write_gold_template(path: str | Path) -> None:
    fields = ["link_type", "source_url", "target_url", "is_match", "package_same", "annotator", "note"]
    with Path(path).open("w", encoding="utf-8-sig", newline="") as fh:
        csv.DictWriter(fh, fieldnames=fields).writeheader()
