"""P0 append-only event database for procurement documents.

The crawler produces evidence documents.  This module persists immutable document
versions, derives business objects, and records append-only events.  Current state
is exposed through SQL views rather than by overwriting historical events.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Iterable


SCHEMA_VERSION = 1

AMOUNT_TYPES = {
    "INTENT_BUDGET",
    "TENDER_BUDGET",
    "CEILING_PRICE",
    "AWARD_AMOUNT",
    "CONTRACT_AMOUNT",
    "ACCEPTED_AMOUNT",
    "UNIT_PRICE",
    "DISCOUNT_RATE",
    "FRAMEWORK_ESTIMATE",
}


def uid(prefix: str, *parts: object) -> str:
    raw = "\x1f".join("" if value is None else str(value).strip().casefold() for value in parts)
    return f"{prefix}_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def parse_time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace(" ", "T"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone(dt.timedelta(hours=8)))
        return parsed
    except ValueError:
        return None


def available_time(publish_time: str | None, first_seen_at: str) -> str:
    published = parse_time(publish_time)
    seen = parse_time(first_seen_at)
    if published and seen:
        return max(published, seen).isoformat(timespec="seconds")
    return (seen or published or dt.datetime.now().astimezone()).isoformat(timespec="seconds")


_CN_NUMBERS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def detect_round(title: str) -> int:
    patterns = [
        r"第\s*(\d+)\s*次",
        r"[（(]\s*(\d+)\s*次\s*[）)]",
        r"第\s*([一二三四五六七八九十])\s*次",
        r"[（(]\s*([一二三四五六七八九十])\s*次\s*[）)]",
    ]
    for pattern in patterns:
        match = re.search(pattern, title)
        if match:
            token = match.group(1)
            return int(token) if token.isdigit() else _CN_NUMBERS.get(token, 1)
    return 2 if re.search(r"二次|重新招标|重新采购|重招", title) else 1


def detect_package(title: str, raw_text: str) -> tuple[str, str]:
    sample = f"{title}\n{raw_text[:3000]}"
    patterns = [
        r"(?:第\s*)?(\d+)\s*(?:包|标包)",
        r"(?:包|标包)\s*(\d+)",
        r"(?:第\s*)?([一二三四五六七八九十])\s*(?:包|标包)",
    ]
    for pattern in patterns:
        match = re.search(pattern, sample)
        if match:
            token = match.group(1)
            number = str(_CN_NUMBERS.get(token, token))
            return number, f"包{number}"
    return "UNSPECIFIED", "未披露标包"


def canonical_title(value: str) -> str:
    title = value or ""
    title = re.sub(r"[（(](?:第?\s*[一二三四五六七八九十\d]+\s*次|重招|重新招标|重新采购)[）)]", "", title)
    title = re.sub(r"(?:第?\s*[一二三四五六七八九十\d]+\s*次|二次|重新招标|重新采购|重招)", "", title)
    title = re.sub(r"(?:第\s*)?[一二三四五六七八九十\d]+\s*(?:包|标包)", "", title)
    title = re.sub(r"(?:包|标包)\s*[一二三四五六七八九十\d]+", "", title)
    title = re.sub(r"(?:公开招标|竞争性磋商|竞争性谈判|询价|单一来源)?(?:采购)?(?:中标|成交|合同|更正|终止|废标|流标|失败|结果)?(?:公告|公示)$", "", title)
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", title).casefold()


def event_spec(notice) -> tuple[str, str, str | None]:
    stage = getattr(notice, "stage", "")
    if stage == "招标公告":
        return "TENDER_OPENED", "package", "OPEN"
    if stage == "中标结果":
        return "AWARD_PUBLISHED", "package", "AWARDED"
    if stage == "采购合同":
        return "CONTRACT_SIGNED", "contract", "CONTRACTED"
    if stage == "更正":
        if int(getattr(notice, "is_delayed", 0) or 0):
            return "DEADLINE_EXTENDED", "attempt", None
        return "CORRECTION_PUBLISHED", "attempt", None
    if stage == "终止":
        if int(getattr(notice, "is_failed_bid", 0) or 0):
            return "PACKAGE_FAILED", "package", "FAILED"
        return "PROJECT_TERMINATED", "attempt", "TERMINATED"
    return "DOCUMENT_PUBLISHED", "attempt", None


DDL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_metadata (
    schema_version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_document (
    doc_uid TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    source_native_id TEXT,
    url TEXT NOT NULL,
    list_url TEXT,
    document_type TEXT NOT NULL,
    title TEXT NOT NULL,
    publish_time TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    available_time TEXT NOT NULL,
    UNIQUE(source_id, url)
);

CREATE TABLE IF NOT EXISTS source_document_version (
    version_uid TEXT PRIMARY KEY,
    doc_uid TEXT NOT NULL REFERENCES source_document(doc_uid),
    version_no INTEGER NOT NULL,
    crawl_time TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    raw_text TEXT,
    raw_html_path TEXT,
    attachment_paths_json TEXT NOT NULL DEFAULT '[]',
    http_status INTEGER,
    parser_version TEXT NOT NULL,
    UNIQUE(doc_uid, version_no),
    UNIQUE(doc_uid, content_hash)
);

CREATE TABLE IF NOT EXISTS root_project (
    project_uid TEXT PRIMARY KEY,
    canonical_title TEXT NOT NULL,
    buyer_name TEXT NOT NULL,
    region_code TEXT,
    category_code TEXT,
    first_intent_time TEXT,
    planned_budget REAL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS procurement_attempt (
    attempt_uid TEXT PRIMARY KEY,
    project_uid TEXT NOT NULL REFERENCES root_project(project_uid),
    source_project_no TEXT,
    procurement_plan_no TEXT,
    transaction_no TEXT,
    round_no INTEGER NOT NULL,
    procurement_method TEXT,
    agency_name TEXT,
    notice_time TEXT,
    bid_deadline TEXT,
    open_time TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS package (
    package_uid TEXT PRIMARY KEY,
    attempt_uid TEXT NOT NULL REFERENCES procurement_attempt(attempt_uid),
    package_no TEXT NOT NULL,
    package_name TEXT,
    category_code TEXT,
    budget_amount REAL,
    ceiling_amount REAL,
    quantity REAL,
    amount_unit TEXT NOT NULL DEFAULT 'CNY',
    created_at TEXT NOT NULL,
    UNIQUE(attempt_uid, package_no)
);

CREATE TABLE IF NOT EXISTS contract (
    contract_uid TEXT PRIMARY KEY,
    package_uid TEXT NOT NULL REFERENCES package(package_uid),
    contract_no TEXT,
    contract_name TEXT,
    buyer_name TEXT,
    supplier_names TEXT,
    sign_date TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS acceptance (
    acceptance_uid TEXT PRIMARY KEY,
    contract_uid TEXT NOT NULL REFERENCES contract(contract_uid),
    batch_no TEXT,
    acceptance_type TEXT,
    acceptance_date TEXT,
    accepted_ratio REAL,
    result TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS procurement_event (
    event_uid TEXT PRIMARY KEY,
    logical_event_key TEXT NOT NULL,
    event_type TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_uid TEXT NOT NULL,
    event_time TEXT,
    publish_time TEXT,
    available_time TEXT NOT NULL,
    state_after TEXT,
    reason_code TEXT,
    old_value_json TEXT,
    new_value_json TEXT,
    source_doc_uid TEXT NOT NULL REFERENCES source_document(doc_uid),
    source_version_uid TEXT NOT NULL REFERENCES source_document_version(version_uid),
    link_method TEXT NOT NULL,
    link_score REAL NOT NULL CHECK(link_score >= 0 AND link_score <= 1),
    link_confidence TEXT NOT NULL,
    review_status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(logical_event_key, source_version_uid)
);

CREATE TABLE IF NOT EXISTS amount_observation (
    amount_uid TEXT PRIMARY KEY,
    event_uid TEXT NOT NULL REFERENCES procurement_event(event_uid),
    target_type TEXT NOT NULL,
    target_uid TEXT NOT NULL,
    amount_type TEXT NOT NULL,
    amount REAL NOT NULL,
    currency TEXT NOT NULL DEFAULT 'CNY',
    allocation_status TEXT NOT NULL DEFAULT 'DIRECT',
    created_at TEXT NOT NULL,
    CHECK(amount_type IN ('INTENT_BUDGET','TENDER_BUDGET','CEILING_PRICE','AWARD_AMOUNT','CONTRACT_AMOUNT','ACCEPTED_AMOUNT','UNIT_PRICE','DISCOUNT_RATE','FRAMEWORK_ESTIMATE')),
    UNIQUE(event_uid, amount_type, target_uid)
);

CREATE TABLE IF NOT EXISTS document_object_link (
    link_uid TEXT PRIMARY KEY,
    source_doc_uid TEXT NOT NULL REFERENCES source_document(doc_uid),
    source_version_uid TEXT NOT NULL REFERENCES source_document_version(version_uid),
    object_type TEXT NOT NULL,
    object_uid TEXT NOT NULL,
    link_method TEXT NOT NULL,
    match_score REAL NOT NULL CHECK(match_score >= 0 AND match_score <= 1),
    confidence TEXT NOT NULL,
    review_status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(source_version_uid, object_type, object_uid)
);

CREATE INDEX IF NOT EXISTS idx_document_publish_time ON source_document(publish_time);
CREATE INDEX IF NOT EXISTS idx_version_doc ON source_document_version(doc_uid, version_no);
CREATE INDEX IF NOT EXISTS idx_attempt_project ON procurement_attempt(project_uid);
CREATE INDEX IF NOT EXISTS idx_package_attempt ON package(attempt_uid);
CREATE INDEX IF NOT EXISTS idx_event_target ON procurement_event(target_type, target_uid, available_time);
CREATE INDEX IF NOT EXISTS idx_event_logical ON procurement_event(logical_event_key, available_time);
CREATE INDEX IF NOT EXISTS idx_amount_target ON amount_observation(target_type, target_uid, amount_type);

CREATE VIEW IF NOT EXISTS current_event AS
SELECT event_uid, logical_event_key, event_type, target_type, target_uid,
       event_time, publish_time, available_time, state_after, reason_code,
       source_doc_uid, source_version_uid, link_method, link_score,
       link_confidence, review_status, created_at
FROM (
    SELECT e.*,
           ROW_NUMBER() OVER (
               PARTITION BY logical_event_key
               ORDER BY available_time DESC, created_at DESC, event_uid DESC
           ) AS rn
    FROM procurement_event e
)
WHERE rn = 1;

CREATE VIEW IF NOT EXISTS current_object_state AS
SELECT target_type, target_uid, state_after AS current_state,
       event_type AS state_event_type, event_time AS state_event_time,
       available_time, event_uid
FROM (
    SELECT e.*,
           ROW_NUMBER() OVER (
               PARTITION BY target_type, target_uid
               ORDER BY available_time DESC, created_at DESC, event_uid DESC
           ) AS rn
    FROM current_event e
    WHERE state_after IS NOT NULL
)
WHERE rn = 1;

CREATE VIEW IF NOT EXISTS current_amount_observation AS
SELECT a.*
FROM amount_observation a
JOIN current_event e ON e.event_uid = a.event_uid;
"""


class ProcurementDatabase:
    def __init__(self, path: str | Path, parser_version: str = "p0.1"):
        self.path = Path(path)
        self.parser_version = parser_version

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        return con

    def initialize(self, con: sqlite3.Connection) -> None:
        con.executescript(DDL)
        con.execute(
            "INSERT OR IGNORE INTO schema_metadata(schema_version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, now_iso()),
        )

    def ingest(self, notices: Iterable[object]) -> dict[str, int]:
        counts = {"documents": 0, "versions": 0, "projects": 0, "attempts": 0, "packages": 0, "contracts": 0, "events": 0, "amounts": 0}
        with closing(self.connect()) as con, con:
            self.initialize(con)
            for notice in notices:
                delta = self._ingest_notice(con, notice)
                for key, value in delta.items():
                    counts[key] += value
        return counts

    def _ingest_notice(self, con: sqlite3.Connection, notice: object) -> dict[str, int]:
        created = {"documents": 0, "versions": 0, "projects": 0, "attempts": 0, "packages": 0, "contracts": 0, "events": 0, "amounts": 0}
        source = str(getattr(notice, "source", "unknown"))
        url = str(getattr(notice, "url", ""))
        title = str(getattr(notice, "title", ""))
        raw_text = str(getattr(notice, "raw_text", ""))
        crawl_time = str(getattr(notice, "crawl_time", "") or now_iso())
        publish_time = str(getattr(notice, "publish_time", "") or "")
        content_hash = str(getattr(notice, "content_hash", "") or hashlib.sha256(raw_text.encode("utf-8")).hexdigest())
        doc_uid = uid("doc", source, url)
        native_id = str(getattr(notice, "notice_id", "") or "")

        exists = con.execute("SELECT 1 FROM source_document WHERE doc_uid = ?", (doc_uid,)).fetchone()
        if not exists:
            created["documents"] = 1
            con.execute(
                """INSERT INTO source_document(
                       doc_uid, source_id, source_native_id, url, list_url,
                       document_type, title, publish_time, first_seen_at,
                       last_seen_at, available_time
                   ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)""",
                (
                    doc_uid, source, native_id, url, str(getattr(notice, "stage", "其他")),
                    title, publish_time, crawl_time, crawl_time,
                    available_time(publish_time, crawl_time),
                ),
            )
        else:
            con.execute(
                "UPDATE source_document SET last_seen_at = ? WHERE doc_uid = ?",
                (crawl_time, doc_uid),
            )

        version = con.execute(
            "SELECT version_uid FROM source_document_version WHERE doc_uid = ? AND content_hash = ?",
            (doc_uid, content_hash),
        ).fetchone()
        if version:
            version_uid = version["version_uid"]
        else:
            version_no = con.execute(
                "SELECT COALESCE(MAX(version_no), 0) + 1 FROM source_document_version WHERE doc_uid = ?",
                (doc_uid,),
            ).fetchone()[0]
            version_uid = uid("ver", doc_uid, content_hash)
            con.execute(
                """INSERT INTO source_document_version(
                       version_uid, doc_uid, version_no, crawl_time, content_hash,
                       raw_text, raw_html_path, attachment_paths_json, http_status,
                       parser_version
                   ) VALUES (?, ?, ?, ?, ?, ?, NULL, '[]', ?, ?)""",
                (version_uid, doc_uid, version_no, crawl_time, content_hash, raw_text, 200 if not getattr(notice, "error", "") else None, self.parser_version),
            )
            created["versions"] = 1

        buyer = str(getattr(notice, "buyer", "") or "UNKNOWN_BUYER")
        project_title = str(getattr(notice, "project_name", "") or title)
        canonical = canonical_title(project_title)
        project_uid = uid("prj", buyer, canonical)
        if not con.execute("SELECT 1 FROM root_project WHERE project_uid = ?", (project_uid,)).fetchone():
            created["projects"] = 1
            con.execute(
                """INSERT INTO root_project(
                       project_uid, canonical_title, buyer_name, region_code,
                       category_code, first_intent_time, planned_budget, created_at
                   ) VALUES (?, ?, ?, ?, NULL, NULL, ?, ?)""",
                (project_uid, canonical, buyer, str(getattr(notice, "province", "") or ""), getattr(notice, "budget_yuan", None), crawl_time),
            )

        project_no = str(getattr(notice, "project_code", "") or "")
        round_no = detect_round(title)
        method = str(getattr(notice, "category", "") or "")
        attempt_uid = uid("att", project_uid, project_no or "NO_PROJECT_NO", round_no, method if getattr(notice, "stage", "") == "招标公告" else "")
        # Result/correction documents often use a different category label; reuse a
        # unique existing attempt for the same project number and round when possible.
        candidate = None
        if project_no:
            candidate = con.execute(
                """SELECT attempt_uid FROM procurement_attempt
                   WHERE project_uid = ? AND source_project_no = ? AND round_no = ?
                   ORDER BY created_at LIMIT 1""",
                (project_uid, project_no, round_no),
            ).fetchone()
        if candidate:
            attempt_uid = candidate["attempt_uid"]
            if getattr(notice, "stage", "") == "招标公告":
                con.execute(
                    "UPDATE procurement_attempt SET procurement_method = ? WHERE attempt_uid = ?",
                    (method, attempt_uid),
                )
        elif not con.execute("SELECT 1 FROM procurement_attempt WHERE attempt_uid = ?", (attempt_uid,)).fetchone():
            created["attempts"] = 1
            con.execute(
                """INSERT INTO procurement_attempt(
                       attempt_uid, project_uid, source_project_no,
                       procurement_plan_no, transaction_no, round_no,
                       procurement_method, agency_name, notice_time,
                       bid_deadline, open_time, created_at
                   ) VALUES (?, ?, ?, NULL, NULL, ?, ?, NULL, ?, NULL, NULL, ?)""",
                (attempt_uid, project_uid, project_no or None, round_no, method, publish_time, crawl_time),
            )

        package_no, package_name = detect_package(title, raw_text)
        package_uid = uid("pkg", attempt_uid, package_no)
        if not con.execute("SELECT 1 FROM package WHERE package_uid = ?", (package_uid,)).fetchone():
            created["packages"] = 1
            con.execute(
                """INSERT INTO package(
                       package_uid, attempt_uid, package_no, package_name,
                       category_code, budget_amount, ceiling_amount, quantity,
                       amount_unit, created_at
                   ) VALUES (?, ?, ?, ?, NULL, ?, NULL, NULL, 'CNY', ?)""",
                (package_uid, attempt_uid, package_no, package_name, getattr(notice, "budget_yuan", None), crawl_time),
            )

        link_method = "project_no_exact" if project_no else "title_buyer_normalized"
        link_score = 0.98 if project_no else 0.72
        confidence = "HIGH" if link_score >= 0.85 else "MEDIUM"
        review_status = "AUTO_ACCEPTED" if link_score >= 0.85 else "NEEDS_REVIEW"
        for object_type, object_uid in (("project", project_uid), ("attempt", attempt_uid), ("package", package_uid)):
            con.execute(
                """INSERT OR IGNORE INTO document_object_link(
                       link_uid, source_doc_uid, source_version_uid,
                       object_type, object_uid,
                       link_method, match_score, confidence, review_status,
                       created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (uid("lnk", version_uid, object_type, object_uid), doc_uid, version_uid, object_type, object_uid, link_method, link_score, confidence, review_status, crawl_time),
            )

        event_type, target_type, state_after = event_spec(notice)
        target_uid = package_uid if target_type == "package" else attempt_uid
        if target_type == "contract":
            contract_uid = uid("con", package_uid, project_no, doc_uid)
            target_uid = contract_uid
            if not con.execute("SELECT 1 FROM contract WHERE contract_uid = ?", (contract_uid,)).fetchone():
                created["contracts"] = 1
                con.execute(
                    """INSERT INTO contract(
                           contract_uid, package_uid, contract_no, contract_name,
                           buyer_name, supplier_names, sign_date, created_at
                       ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?)""",
                    (contract_uid, package_uid, title, buyer, str(getattr(notice, "supplier_names", "") or ""), publish_time, crawl_time),
                )

        # A new version of the same source document supersedes the prior parsed
        # target instead of creating an additional current economic event.
        logical_event_key = uid("evtkey", doc_uid, event_type)
        event_uid = uid("evt", logical_event_key, version_uid)
        if not con.execute("SELECT 1 FROM procurement_event WHERE event_uid = ?", (event_uid,)).fetchone():
            created["events"] = 1
            con.execute(
                """INSERT INTO procurement_event(
                       event_uid, logical_event_key, event_type, target_type,
                       target_uid, event_time, publish_time, available_time,
                       state_after, reason_code, old_value_json, new_value_json,
                       source_doc_uid, source_version_uid, link_method, link_score,
                       link_confidence, review_status, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_uid, logical_event_key, event_type, target_type, target_uid,
                    publish_time, publish_time, available_time(publish_time, crawl_time),
                    state_after,
                    json.dumps({"title": title, "is_delayed": int(getattr(notice, "is_delayed", 0) or 0)}, ensure_ascii=False) if "CORRECTION" in event_type or "EXTENDED" in event_type else None,
                    doc_uid, version_uid, link_method, link_score, confidence,
                    review_status, crawl_time,
                ),
            )

        observations: list[tuple[str, float | None, str]] = []
        budget = getattr(notice, "budget_yuan", None)
        amount = getattr(notice, "amount_yuan", None)
        if budget is not None:
            observations.append(("TENDER_BUDGET", float(budget), "DIRECT"))
        if amount is not None and event_type == "AWARD_PUBLISHED":
            allocation = "UNALLOCATED_MULTI_SUPPLIER" if "|" in str(getattr(notice, "supplier_names", "")) else "DIRECT"
            observations.append(("AWARD_AMOUNT", float(amount), allocation))
        if amount is not None and event_type == "CONTRACT_SIGNED":
            observations.append(("CONTRACT_AMOUNT", float(amount), "DIRECT"))
        for amount_type, value, allocation_status in observations:
            if amount_type not in AMOUNT_TYPES or value is None:
                continue
            amount_uid = uid("amt", event_uid, amount_type, target_uid)
            if not con.execute("SELECT 1 FROM amount_observation WHERE amount_uid = ?", (amount_uid,)).fetchone():
                created["amounts"] += 1
                con.execute(
                    """INSERT INTO amount_observation(
                           amount_uid, event_uid, target_type, target_uid,
                           amount_type, amount, currency, allocation_status,
                           created_at
                       ) VALUES (?, ?, ?, ?, ?, ?, 'CNY', ?, ?)""",
                    (amount_uid, event_uid, target_type, target_uid, amount_type, value, allocation_status, crawl_time),
                )
        return created


def table_counts(path: str | Path) -> dict[str, int]:
    tables = [
        "source_document", "source_document_version", "root_project",
        "procurement_attempt", "package", "contract", "acceptance",
        "procurement_event", "amount_observation", "document_object_link",
    ]
    con = sqlite3.connect(path)
    try:
        return {table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}
    finally:
        con.close()
