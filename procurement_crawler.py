#!/usr/bin/env python3
"""中国政府采购网公开公告采集、项目链路关联与指标计算（仅标准库）。"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable

from p0_database import ProcurementDatabase
from p1_matching import P1Processor, write_gold_template
from p2_lifecycle import P2LifecycleProcessor
from p3_intentions import P3IntentProcessor, write_intent_gold_template


BASE = "https://www.ccgp.gov.cn"
CATEGORIES = {
    "公开招标": ("gkzb", "招标公告"),
    "询价": ("xjgg", "招标公告"),
    "竞争性谈判": ("jzxtpgg", "招标公告"),
    "竞争性磋商": ("jzxcs", "招标公告"),
    "单一来源": ("dylygg", "招标公告"),
    "资格预审": ("zgysgg", "招标公告"),
    "中标": ("zbgg", "中标结果"),
    "成交": ("cjgg", "中标结果"),
    # 中央单位合同常以“其他公告”发布；列表层再按标题中的“合同”筛选。
    "合同": ("qtgg", "采购合同"),
    "更正": ("gzgg", "更正"),
    "终止": ("fblbgg", "终止"),
    "其他": ("qtgg", "其他"),
}


def clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", html.unescape(s or "")).strip()


def calendar_date(timezone_name: str, day_offset: int = 0) -> str:
    """Return a calendar date in the configured supported timezone."""

    if timezone_name != "Asia/Shanghai":
        raise ValueError("当前仅支持 timezone=Asia/Shanghai")
    china_time = dt.timezone(dt.timedelta(hours=8), name="Asia/Shanghai")
    target = dt.datetime.now(china_time).date() + dt.timedelta(days=day_offset)
    return target.isoformat()


def daily_page_is_complete(page_dates: list[str], target_date: str) -> bool:
    """Stop after a descending list page has crossed into an earlier date."""

    valid_dates = [value for value in page_dates if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)]
    return bool(valid_dates) and max(valid_dates) <= target_date and min(valid_dates) < target_date


class ListParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_list = False
        self.in_li = False
        self.in_a = False
        self.in_em = False
        self.item = None
        self.items = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "ul" and "c_list_bid" in attrs.get("class", "").split():
            self.in_list = True
        elif self.in_list and tag == "li":
            self.in_li = True
            self.item = {"href": "", "title": "", "text": [], "ems": []}
        elif self.in_li and tag == "a":
            self.in_a = True
            self.item["href"] = attrs.get("href", "")
            self.item["title"] = attrs.get("title", "")
        elif self.in_li and tag == "em":
            self.in_em = True

    def handle_endtag(self, tag):
        if tag == "a":
            self.in_a = False
        elif tag == "em":
            self.in_em = False
        elif tag == "li" and self.in_li:
            self.items.append(self.item)
            self.item = None
            self.in_li = False
        elif tag == "ul" and self.in_list:
            self.in_list = False

    def handle_data(self, data):
        if not self.in_li:
            return
        self.item["text"].append(data)
        if self.in_a and not self.item["title"]:
            self.item["title"] += data
        if self.in_em:
            self.item["ems"].append(clean(data))


class DetailParser(HTMLParser):
    BLOCKS = {"p", "div", "tr", "td", "th", "li", "br", "h1", "h2", "h3"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "div" and "vF_detail_content" in attrs.get("class", "").split():
            self.depth = 1
            return
        if self.depth:
            if tag == "div":
                self.depth += 1
            if tag in self.BLOCKS:
                self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.depth:
            if tag in self.BLOCKS:
                self.parts.append("\n")
            if tag == "div":
                self.depth -= 1

    def handle_data(self, data):
        if self.depth:
            self.parts.append(data)

    def text(self):
        lines = [clean(x) for x in "".join(self.parts).splitlines()]
        return "\n".join(x for x in lines if x)


class TableParser(HTMLParser):
    """Minimal table reader used for public procurement-intention pages."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_cell = False
        self.cell_parts: list[str] = []
        self.row: list[str] = []
        self.rows: list[list[str]] = []

    def handle_starttag(self, tag, attrs):
        if tag in {"td", "th"}:
            self.in_cell = True
            self.cell_parts = []

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self.in_cell:
            self.row.append(clean("".join(self.cell_parts)))
            self.in_cell = False
        elif tag == "tr" and self.row:
            self.rows.append(self.row)
            self.row = []

    def handle_data(self, data):
        if self.in_cell:
            self.cell_parts.append(data)


@dataclass
class Notice:
    notice_id: str
    source: str
    category: str
    stage: str
    title: str
    publish_time: str
    province: str
    buyer: str
    url: str
    project_code: str = ""
    project_name: str = ""
    project_key: str = ""
    supplier_names: str = ""
    supplier_addresses: str = ""
    amount_yuan: float | None = None
    budget_yuan: float | None = None
    supplier_count: int | None = None
    payment_terms: str = ""
    warranty_terms: str = ""
    delivery_terms: str = ""
    is_cancelled: int = 0
    is_failed_bid: int = 0
    is_delayed: int = 0
    raw_text: str = ""
    content_hash: str = ""
    crawl_time: str = ""
    error: str = ""
    source_page_url: str = ""
    intent_item_no: str = ""
    procurement_category: str = ""
    demand_summary: str = ""
    expected_purchase_date: str = ""
    intent_remarks: str = ""


class Crawler:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.delay = float(cfg.get("request_delay_seconds", 1.5))
        self.timeout = int(cfg.get("timeout_seconds", 30))
        self.retries = int(cfg.get("retries", 2))
        self.last_request = 0.0
        self.user_agent = cfg.get(
            "user_agent", "PublicProcurementResearchBot/1.0 (low-frequency; public pages only)"
        )

    def fetch(self, url: str) -> str:
        wait = self.delay - (time.monotonic() - self.last_request)
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    self.last_request = time.monotonic()
                    return resp.read().decode("utf-8", "replace")
            except (urllib.error.URLError, TimeoutError) as exc:
                self.last_request = time.monotonic()
                if attempt == self.retries:
                    raise
                time.sleep(2 ** attempt)
        raise RuntimeError("unreachable")

    def list_url(self, code: str, page: int, scope: str) -> str:
        name = "index.htm" if page == 0 else f"index_{page}.htm"
        return f"{BASE}/cggg/{scope}/{code}/{name}"

    def collect(self) -> list[Notice]:
        scope = self.cfg.get("scope", "zygg")
        pages = int(self.cfg.get("pages_per_category", 1))
        crawl_mode = str(self.cfg.get("crawl_mode", "previous_day")).strip().lower()
        if crawl_mode not in {"previous_day", "daily", "pages"}:
            raise ValueError("crawl_mode 只能是 previous_day、daily 或 pages")
        timezone_name = str(self.cfg.get("timezone", "Asia/Shanghai"))
        configured_date = str(self.cfg.get("target_date", "") or self.cfg.get("daily_date", ""))
        day_offset = -1 if crawl_mode == "previous_day" else 0
        target_date = configured_date or calendar_date(timezone_name, day_offset)
        max_daily_pages = max(1, int(self.cfg.get("max_pages_per_category", 50)))
        date_mode = crawl_mode in {"previous_day", "daily"}
        page_limit = max_daily_pages if date_mode else max(0, pages)
        enabled = self.cfg.get("categories", list(CATEGORIES))
        start = self.cfg.get("start_date", "")
        end = self.cfg.get("end_date", "")
        terms = [x.casefold() for x in self.cfg.get("keywords", []) if x]
        seen = set()
        notices = []
        for category in enabled:
            if category == "采购意向":
                continue
            if category not in CATEGORIES:
                print(f"跳过未知类别：{category}", file=sys.stderr)
                continue
            code, stage = CATEGORIES[category]
            for page in range(page_limit):
                url = self.list_url(code, page, scope)
                try:
                    body = self.fetch(url)
                except Exception as exc:
                    print(f"列表失败 {url}: {exc}", file=sys.stderr)
                    continue
                parser = ListParser()
                parser.feed(body)
                if not parser.items:
                    break
                page_dates = []
                for row in parser.items:
                    href = urllib.parse.urljoin(url, row["href"])
                    title = clean(row["title"])
                    text = clean(" ".join(row["text"]))
                    ems = row["ems"]
                    publish_time = ems[0] if ems else first(text, r"发布时间：\s*([^地]+)")
                    province = ems[1] if len(ems) > 1 else first(text, r"地域：\s*([^采]+)")
                    buyer = ems[2] if len(ems) > 2 else first(text, r"采购人：\s*(.+)$")
                    date = publish_time[:10]
                    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                        page_dates.append(date)
                    if not href or href in seen:
                        continue
                    if category == "合同" and "合同" not in title:
                        continue
                    if category == "终止" and not any(x in title for x in ("终止", "废标", "流标", "失败")):
                        continue
                    if date_mode and date != target_date:
                        continue
                    if crawl_mode == "pages" and (start and date < start or end and date > end):
                        continue
                    haystack = f"{title} {buyer}".casefold()
                    if terms and not any(t in haystack for t in terms):
                        continue
                    seen.add(href)
                    notices.append(
                        Notice(
                            notice_id=hashlib.sha1(href.encode()).hexdigest()[:16],
                            source="中国政府采购网",
                            category=category,
                            stage=stage,
                            title=title,
                            publish_time=publish_time,
                            province=province,
                            buyer=buyer,
                            url=href,
                        )
                    )
                if date_mode and daily_page_is_complete(page_dates, target_date):
                    break
        if "采购意向" in enabled or self.cfg.get("intent_urls") or self.cfg.get("intent_seed_csv"):
            notices.extend(self.collect_intentions())
        return notices

    def collect_intentions(self) -> list[Notice]:
        """Collect public intention details without automating the CAPTCHA search page."""

        notices: list[Notice] = []
        urls = [str(url).strip() for url in self.cfg.get("intent_urls", []) if str(url).strip()]
        seed_name = str(self.cfg.get("intent_seed_csv", "") or "").strip()
        if seed_name:
            seed_path = Path(seed_name)
            if not seed_path.is_absolute():
                seed_path = Path(self.cfg.get("_config_dir", Path.cwd())) / seed_path
            if seed_path.exists():
                with seed_path.open(encoding="utf-8-sig", newline="") as fh:
                    for row_no, row in enumerate(csv.DictReader(fh), 1):
                        if row.get("project_name") and row.get("buyer"):
                            notices.append(intent_notice_from_row(row, row_no))
                        elif row.get("url"):
                            urls.append(row["url"].strip())
            else:
                print(f"采购意向种子文件不存在：{seed_path}", file=sys.stderr)
        for url in dict.fromkeys(urls):
            try:
                body = self.fetch(url)
                notices.extend(parse_intention_page(body, url))
            except Exception as exc:
                print(f"采购意向详情失败 {url}: {exc}", file=sys.stderr)
        return notices

    def enrich(self, notice: Notice) -> Notice:
        if notice.stage == "采购意向" and notice.raw_text:
            return notice
        try:
            body = self.fetch(notice.url)
            p = DetailParser()
            p.feed(body)
            notice.raw_text = p.text()
            extract_fields(notice)
            notice.content_hash = hashlib.sha256(notice.raw_text.encode()).hexdigest()
        except Exception as exc:
            notice.error = f"{type(exc).__name__}: {exc}"
        notice.crawl_time = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        return notice


def first(text: str, pattern: str, flags=0) -> str:
    m = re.search(pattern, text, flags)
    return clean(m.group(1)) if m else ""


def many(text: str, pattern: str) -> list[str]:
    out = []
    for value in re.findall(pattern, text):
        value = clean(value)
        if value and value not in out:
            out.append(value)
    return out


def money_yuan(text: str, labels: Iterable[str]) -> float | None:
    label = "|".join(re.escape(x) for x in labels)
    patterns = [
        rf"(?:{label})[^\d]{{0,20}}([\d,，.]+)\s*[（(]?万元",
        rf"(?:{label})[^\d]{{0,20}}([\d,，.]+)\s*[（(]?元",
    ]
    for idx, pattern in enumerate(patterns):
        m = re.search(pattern, text)
        if m:
            try:
                value = float(m.group(1).replace(",", "").replace("，", ""))
                return value * 10000 if idx == 0 else value
            except ValueError:
                pass
    return None


def parse_budget_value(value: str, unit_hint: str = "") -> float | None:
    match = re.search(r"[\d,，.]+", value or "")
    if not match:
        return None
    try:
        amount = float(match.group(0).replace(",", "").replace("，", ""))
    except ValueError:
        return None
    return amount * 10000 if "万元" in f"{unit_hint}{value}" else amount


def normalize_cn_datetime(value: str) -> str:
    match = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日(?:\s*(\d{1,2}):(\d{1,2}))?", value or "")
    if not match:
        return clean(value)
    year, month, day, hour, minute = match.groups()
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d} {int(hour or 0):02d}:{int(minute or 0):02d}"


def intent_notice_from_row(row: dict, row_no: int = 1, page_text: str = "") -> Notice:
    item_no = clean(row.get("item_no") or row.get("序号") or str(row_no))
    page_url = clean(row.get("source_page_url") or row.get("url"))
    project_name = clean(row.get("project_name") or row.get("采购项目名称"))
    buyer = clean(row.get("buyer") or row.get("采购单位"))
    category = clean(row.get("procurement_category") or row.get("采购品目"))
    demand = clean(row.get("demand_summary") or row.get("采购需求概况"))
    expected = clean(row.get("expected_purchase_date") or row.get("预计采购日期"))
    remarks = clean(row.get("intent_remarks") or row.get("备注"))
    publish_time = normalize_cn_datetime(clean(row.get("publish_time") or row.get("发布日期")))
    budget = parse_budget_value(
        clean(row.get("budget_yuan") or row.get("budget_wan") or row.get("预算金额") or ""),
        "万元" if row.get("budget_wan") else clean(row.get("budget_unit") or row.get("预算单位") or ""),
    )
    if row.get("budget_yuan"):
        budget = parse_budget_value(clean(row.get("budget_yuan")), "元")
    stable_source = page_url or f"intent-seed:{buyer}:{project_name}:{expected}"
    virtual_url = f"{stable_source}#intent-item-{item_no or row_no}"
    payload = {
        "buyer": buyer, "project_name": project_name, "procurement_category": category,
        "demand_summary": demand, "budget_yuan": budget,
        "expected_purchase_date": expected, "remarks": remarks,
    }
    raw_text = page_text or json.dumps(payload, ensure_ascii=False, sort_keys=True)
    crawl_time = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    notice = Notice(
        notice_id=hashlib.sha1(virtual_url.encode()).hexdigest()[:16],
        source="中国政府采购网-采购意向", category="采购意向", stage="采购意向",
        title=project_name, publish_time=publish_time, province=clean(row.get("province") or row.get("地区")),
        buyer=buyer, url=virtual_url, project_name=project_name, budget_yuan=budget,
        raw_text=raw_text, content_hash=hashlib.sha256(raw_text.encode()).hexdigest(), crawl_time=crawl_time,
        source_page_url=page_url, intent_item_no=item_no,
        procurement_category=category, demand_summary=demand,
        expected_purchase_date=expected, intent_remarks=remarks,
    )
    extract_fields(notice)
    notice.project_name = project_name
    notice.budget_yuan = budget
    notice.project_key = hashlib.sha1(f"{normalize_title(project_name)}|{clean(buyer)}".encode()).hexdigest()[:16]
    return notice


def parse_intention_page(body: str, url: str) -> list[Notice]:
    detail = DetailParser()
    detail.feed(body)
    page_text = detail.text()
    if not page_text:
        page_text = clean(re.sub(r"<[^>]+>", " ", html.unescape(body)))
    table = TableParser()
    table.feed(body)
    publish_match = re.search(r"\d{4}年\d{1,2}月\d{1,2}日(?:\s*\d{1,2}:\d{1,2})?", page_text)
    publish_time = normalize_cn_datetime(publish_match.group(0)) if publish_match else ""
    results: list[Notice] = []
    for header_index, header in enumerate(table.rows):
        normalized = [re.sub(r"\s+", "", value) for value in header]
        if not any("采购项目名称" in value for value in normalized) or not any("预算金额" in value for value in normalized):
            continue
        for row_no, values in enumerate(table.rows[header_index + 1 :], 1):
            if len(values) < len(header):
                continue
            mapped = {normalized[index]: values[index] for index in range(len(header))}
            project_name = next((v for k, v in mapped.items() if "采购项目名称" in k), "")
            buyer = next((v for k, v in mapped.items() if "采购单位" in k), "")
            if not project_name or not buyer:
                continue
            budget_header = next((k for k in mapped if "预算金额" in k), "")
            item = {
                "item_no": next((v for k, v in mapped.items() if "序号" in k), str(row_no)),
                "url": url, "source_page_url": url, "publish_time": publish_time,
                "buyer": buyer, "project_name": project_name,
                "procurement_category": next((v for k, v in mapped.items() if "采购品目" in k), ""),
                "demand_summary": next((v for k, v in mapped.items() if "采购需求概况" in k), ""),
                "预算金额": mapped.get(budget_header, ""), "预算单位": "万元" if "万元" in budget_header else "元",
                "expected_purchase_date": next((v for k, v in mapped.items() if "预计采购日期" in k), ""),
                "intent_remarks": next((v for k, v in mapped.items() if "备注" in k), ""),
            }
            results.append(intent_notice_from_row(item, row_no, json.dumps(item, ensure_ascii=False, sort_keys=True)))
        break
    return results


def sentence_with(text: str, words: list[str]) -> str:
    for line in text.splitlines():
        line = clean(line)
        if any(w in line for w in words) and 2 < len(line) <= 300:
            return line
    return ""


def normalize_title(title: str) -> str:
    value = re.sub(r"[（(](?:第?[一二三四五六七八九十\d]+次|重招|重新招标)[）)]", "", title)
    value = re.sub(r"(?:公开招标|竞争性磋商|竞争性谈判|询价|单一来源)?(?:采购)?(?:中标|成交|合同|更正|终止|废标|结果)?(?:公告|公示)$", "", value)
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", value).casefold()


def extract_fields(n: Notice):
    t = n.raw_text
    n.project_code = first(t, r"(?:项目编号|采购项目编号)\s*[：:]\s*([^\n（(]+)")
    n.project_name = first(t, r"项目名称\s*[：:]\s*([^\n]+)") or n.title
    suppliers = many(t, r"供应商名称\s*[：:]\s*([^\n]+)")
    addresses = many(t, r"供应商地址\s*[：:]\s*([^\n]+)")
    n.supplier_names = " | ".join(suppliers)
    n.supplier_addresses = " | ".join(addresses)
    n.amount_yuan = money_yuan(t, ["中标（成交）金额", "中标(成交)金额", "合同金额", "成交金额"])
    n.budget_yuan = money_yuan(t, ["预算金额", "预算总金额", "采购预算"])
    count = first(t, r"(?:供应商数量|有效供应商(?:家数|数量)|通过(?:资格|符合性)审查的供应商(?:家数|数量))\s*[：:]?\s*(\d+)")
    n.supplier_count = int(count) if count else None
    n.payment_terms = sentence_with(t, ["付款", "支付方式", "支付条件"])
    n.warranty_terms = sentence_with(t, ["质保", "保修"])
    n.delivery_terms = sentence_with(t, ["交付", "交货", "服务时间", "履约期限"])
    status_text = f"{n.title}\n{t[:2000]}"
    n.is_failed_bid = int(n.stage == "终止" and bool(re.search(r"废标|流标|失败", n.title)))
    n.is_cancelled = int(n.stage == "终止" and not n.is_failed_bid)
    n.is_delayed = int(n.stage == "更正" and bool(re.search(r"延期|延长|推迟|顺延", status_text)))
    key_base = re.sub(r"\s+", "", n.project_code).casefold() if n.project_code else f"{normalize_title(n.project_name)}|{clean(n.buyer)}"
    n.project_key = hashlib.sha1(key_base.encode()).hexdigest()[:16]


NOTICE_FIELDS = list(Notice.__dataclass_fields__)


def write_csv(path: Path, rows: Iterable[dict], fields: list[str]):
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def save_sqlite(
    path: Path, notices: list[Notice], cfg: dict, output_dir: Path,
    gold_labels: str = "", intent_gold_labels: str = "",
) -> dict[str, dict]:
    """Append P0 evidence, then build the auditable P1 reconciliation layer.

    Re-ingesting identical content is idempotent.  A changed content hash creates
    a new immutable document version and a superseding logical event version.
    """
    p0_changes = ProcurementDatabase(path).ingest(notices)
    p1 = P1Processor(path, cfg.get("companies", []))
    p1_changes = p1.ingest(notices)
    imported_labels = 0
    if gold_labels:
        label_path = Path(gold_labels)
        if label_path.exists():
            imported_labels = p1.import_gold_labels(label_path)
        else:
            raise FileNotFoundError(f"金标文件不存在：{label_path}")
    p1.export_csv(output_dir)
    p2 = P2LifecycleProcessor(path)
    p2_changes = p2.ingest(notices)
    p3 = P3IntentProcessor(path)
    p3_changes = p3.ingest(notices)
    imported_intent_labels = 0
    if intent_gold_labels:
        intent_label_path = Path(intent_gold_labels)
        if intent_label_path.exists():
            imported_intent_labels = p3.import_gold_labels(intent_label_path)
        else:
            raise FileNotFoundError(f"采购意向金标文件不存在：{intent_label_path}")
    p3.export_csv(output_dir)
    p2.export_csv(output_dir)
    return {
        "p0": p0_changes,
        "p1": p1_changes,
        "p2": p2_changes,
        "p3": p3_changes,
        "gold_labels_imported": imported_labels,
        "matching_evaluation": p1.evaluate(),
        "intent_gold_labels_imported": imported_intent_labels,
        "intent_matching_evaluation": p3.evaluate(),
    }


def project_rows(notices: list[Notice]) -> list[dict]:
    groups = defaultdict(list)
    for n in notices:
        groups[n.project_key].append(n)
    out = []
    for key, xs in groups.items():
        xs.sort(key=lambda x: x.publish_time)
        stages = {x.stage for x in xs}
        intent_budgets = [x.budget_yuan for x in xs if x.budget_yuan is not None and x.stage == "采购意向"]
        tender_budgets = [x.budget_yuan for x in xs if x.budget_yuan is not None and x.stage == "招标公告"]
        amounts = [x.amount_yuan for x in xs if x.amount_yuan is not None and x.stage in {"中标结果", "采购合同"}]
        intent_budget = max(intent_budgets) if intent_budgets else None
        budget = max(tender_budgets) if tender_budgets else None
        amount = max(amounts) if amounts else None
        intent_times = [parse_time(x.publish_time) for x in xs if x.stage == "采购意向" and parse_time(x.publish_time)]
        tender_times = [parse_time(x.publish_time) for x in xs if x.stage == "招标公告" and parse_time(x.publish_time)]
        award_times = [parse_time(x.publish_time) for x in xs if x.stage == "中标结果" and parse_time(x.publish_time)]
        contract_times = [parse_time(x.publish_time) for x in xs if x.stage == "采购合同" and parse_time(x.publish_time)]
        first_intent = min(intent_times) if intent_times else None
        first_tender = min(tender_times) if tender_times else None
        first_award = min(award_times) if award_times else None
        first_contract = min(contract_times) if contract_times else None
        has_failed = int(any(x.is_failed_bid for x in xs))
        has_cancelled = int(any(x.is_cancelled for x in xs))
        has_delay = int(any(x.is_delayed for x in xs))
        if "采购合同" in stages:
            status = "已签合同"
        elif "中标结果" in stages:
            status = "已中标"
        elif has_failed:
            status = "废标/流标"
        elif has_cancelled:
            status = "取消/终止"
        elif has_delay:
            status = "延期"
        elif "更正" in stages:
            status = "已更正"
        elif "采购意向" in stages:
            status = "采购计划中"
        else:
            status = "招标中"
        out.append({
            "project_key": key,
            "project_code": next((x.project_code for x in xs if x.project_code), ""),
            "project_name": next((x.project_name for x in xs if x.project_name), xs[0].title),
            "buyer": xs[0].buyer,
            "province": xs[0].province,
            "first_publish_time": xs[0].publish_time,
            "last_publish_time": xs[-1].publish_time,
            "notice_count": len(xs),
            "stages": " → ".join(x.stage for x in xs),
            "status": status,
            "has_intent": int("采购意向" in stages),
            "has_tender": int("招标公告" in stages),
            "has_award": int("中标结果" in stages),
            "has_contract": int("采购合同" in stages),
            "has_correction": int("更正" in stages),
            "has_termination": int("终止" in stages),
            "has_cancelled": has_cancelled,
            "has_failed_bid": has_failed,
            "has_delay": has_delay,
            "days_intent_to_tender": (first_tender.date() - first_intent.date()).days if first_intent and first_tender else None,
            "days_tender_to_award": (first_award.date() - first_tender.date()).days if first_tender and first_award else None,
            "days_award_to_contract": (first_contract.date() - first_award.date()).days if first_award and first_contract else None,
            "intent_budget_yuan": intent_budget,
            "budget_yuan": budget,
            "award_or_contract_yuan": amount,
            "discount_rate": (1 - amount / budget) if budget and amount is not None else None,
            "supplier_names": " | ".join(dict.fromkeys(x.supplier_names for x in xs if x.supplier_names)),
            "delivery_terms": next((x.delivery_terms for x in reversed(xs) if x.delivery_terms), ""),
            "source_urls": " | ".join(x.url for x in xs),
        })
    return sorted(out, key=lambda r: r["last_publish_time"], reverse=True)


def parse_time(value: str):
    try:
        return dt.datetime.fromisoformat(value.replace(" ", "T"))
    except (TypeError, ValueError):
        return None


def supplier_rows(notices: list[Notice], cfg: dict) -> list[dict]:
    data = defaultdict(lambda: {"amount": 0.0, "count": 0, "buyers": defaultdict(float), "provinces": set(), "dates": [], "buyer_first": {}, "province_first": {}})
    for n in notices:
        if n.stage != "中标结果" or not n.supplier_names:
            continue
        names = [x.strip() for x in n.supplier_names.split("|") if x.strip()]
        when = parse_time(n.publish_time)
        for name in names:
            d = data[name]
            allocated = (n.amount_yuan or 0) / max(1, len(names))
            d["amount"] += allocated
            d["count"] += 1
            d["buyers"][n.buyer] += allocated
            d["provinces"].add(n.province)
            if when:
                d["dates"].append(when)
                d["buyer_first"][n.buyer] = min(when, d["buyer_first"].get(n.buyer, when))
                d["province_first"][n.province] = min(when, d["province_first"].get(n.province, when))
    revenue_map = {}
    for company in cfg.get("companies", []):
        for alias in company.get("aliases", []) + [company.get("name", "")]:
            if alias:
                revenue_map[alias] = company.get("last_year_revenue_yuan")
    period_start = parse_time(cfg.get("metric_period_start", ""))
    history_start = parse_time(cfg.get("start_date", ""))
    history_sufficient = bool(period_start and history_start and history_start.date() < period_start.date())
    out = []
    for supplier, d in data.items():
        revenue = next((v for alias, v in revenue_map.items() if alias in supplier or supplier in alias), None)
        buyer_amounts = sorted(d["buyers"].values(), reverse=True)
        total = d["amount"]
        shares = [v / total for v in buyer_amounts] if total > 0 else []
        new_buyers = sum(x.date() >= period_start.date() for x in d["buyer_first"].values()) if history_sufficient else None
        new_provinces = sum(x.date() >= period_start.date() for x in d["province_first"].values()) if history_sufficient else None
        out.append({
            "supplier": supplier,
            "award_amount_yuan": total,
            "last_year_revenue_yuan": revenue,
            "award_to_revenue_ratio": total / revenue if revenue else None,
            "award_count": d["count"],
            "buyer_count": len(d["buyers"]),
            "new_buyer_count": new_buyers,
            "first_cooperation_buyer_ratio": new_buyers / len(d["buyers"]) if new_buyers is not None and d["buyers"] else None,
            "top1_buyer_concentration": sum(shares[:1]) if shares else None,
            "top3_buyer_concentration": sum(shares[:3]) if shares else None,
            "buyer_hhi": sum(x * x for x in shares) if shares else None,
            "province_count": len(d["provinces"]),
            "new_province_count": new_provinces,
            "first_award_date": min(d["dates"]).date().isoformat() if d["dates"] else "",
            "last_award_date": max(d["dates"]).date().isoformat() if d["dates"] else "",
            "history_sufficient_for_new_metrics": int(history_sufficient),
        })
    return sorted(out, key=lambda r: r["award_amount_yuan"], reverse=True)


def metrics_rows(notices: list[Notice], projects: list[dict], suppliers: list[dict], cfg: dict) -> list[dict]:
    metrics = []
    intents = sum(p["has_intent"] for p in projects)
    intent_converted = sum(p["has_intent"] and p["has_tender"] for p in projects)
    tenders = sum(p["has_tender"] for p in projects)
    converted = sum(p["has_tender"] and p["has_award"] for p in projects)
    cancelled = sum(p["has_cancelled"] for p in projects)
    failed = sum(p["has_failed_bid"] for p in projects)
    delayed = sum(p["has_delay"] for p in projects)
    discounts = [p["discount_rate"] for p in projects if p["discount_rate"] is not None]
    metrics.extend([
        {"scope": "全样本", "metric": "公告数", "value": len(notices), "unit": "条", "definition": "去重后的公告URL数"},
        {"scope": "全样本", "metric": "项目数", "value": len(projects), "unit": "个", "definition": "项目编号优先、标题+采购人辅助关联"},
        {"scope": "全样本", "metric": "采购意向数", "value": intents, "unit": "个", "definition": "采购意向项目数；仅表示需求侧计划"},
        {"scope": "全样本", "metric": "意向到招标转化率", "value": intent_converted / intents if intents else None, "unit": "%", "definition": "同时含采购意向和招标公告的项目数/采购意向项目数；需覆盖完整观察窗"},
        {"scope": "全样本", "metric": "招标到中标转化率", "value": converted / tenders if tenders else None, "unit": "%", "definition": "同时含招标公告和中标结果的项目数/含招标公告项目数；需覆盖完整观察窗"},
        {"scope": "全样本", "metric": "平均预算折价率", "value": sum(discounts) / len(discounts) if discounts else None, "unit": "%", "definition": "同时披露预算和成交金额项目的简单平均"},
        {"scope": "全样本", "metric": "取消/终止比例", "value": cancelled / len(projects) if projects else None, "unit": "%", "definition": "取消或终止项目数/项目数，不含废标流标"},
        {"scope": "全样本", "metric": "废标/流标比例", "value": failed / len(projects) if projects else None, "unit": "%", "definition": "废标、流标或失败项目数/项目数"},
        {"scope": "全样本", "metric": "延期比例", "value": delayed / len(projects) if projects else None, "unit": "%", "definition": "更正公告中识别到延期、延长、推迟或顺延的项目数/项目数"},
    ])
    for d in suppliers:
        scope = f"供应商:{d['supplier']}"
        metrics.extend([
            {"scope": scope, "metric": "中标金额", "value": d["award_amount_yuan"], "unit": "元", "definition": "多供应商公告金额按供应商数均分，仅作初筛"},
            {"scope": scope, "metric": "中标次数", "value": d["award_count"], "unit": "次", "definition": "中标/成交公告次数"},
            {"scope": scope, "metric": "采购人数量", "value": d["buyer_count"], "unit": "个", "definition": "去重采购人"},
            {"scope": scope, "metric": "Top1采购人集中度", "value": d["top1_buyer_concentration"], "unit": "%", "definition": "最大采购人金额/供应商样本中标金额"},
            {"scope": scope, "metric": "覆盖省份数量", "value": d["province_count"], "unit": "个", "definition": "去重地域"},
            {"scope": scope, "metric": "中标金额/上年收入", "value": d["award_to_revenue_ratio"], "unit": "%", "definition": "需在配置中填写上年营业收入"},
        ])
    return metrics


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="", help="默认依次查找当前目录和脚本目录下的 config.json")
    ap.add_argument("--output-dir", default="data")
    ap.add_argument("--max-details", type=int, default=0, help="0=不限；测试时可限制详情数")
    ap.add_argument("--rebuild-from-csv", action="store_true", help="使用输出目录现有 notices.csv 原文重算字段，不访问网络")
    ap.add_argument("--gold-labels", default="", help="可选：导入P1人工标注CSV并计算匹配指标")
    ap.add_argument("--write-gold-template", action="store_true", help="在输出目录生成P1人工标注模板")
    ap.add_argument("--intent-gold-labels", default="", help="可选：导入P3意向—招标人工标注CSV")
    ap.add_argument("--write-intent-gold-template", action="store_true", help="在输出目录生成P3人工标注模板")
    args = ap.parse_args()
    if args.config:
        cfg_path = Path(args.config)
    else:
        candidates = [Path.cwd() / "config.json", Path(__file__).resolve().with_name("config.json")]
        cfg_path = next((path for path in candidates if path.exists()), candidates[0])
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"配置文件不存在：{cfg_path}。请将 config.example.json 复制为 config.json，"
            "或使用 --config 指定路径。"
        )
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["_config_dir"] = str(cfg_path.resolve().parent)
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    if args.write_gold_template:
        write_gold_template(out / "gold_labels_template.csv")
    if args.write_intent_gold_template:
        write_intent_gold_template(out / "intent_gold_labels_template.csv")
    if args.rebuild_from_csv:
        source_csv = out / "notices.csv"
        rows = list(csv.DictReader(source_csv.open(encoding="utf-8-sig")))
        notices = []
        for row in rows:
            values = {k: row.get(k, "") for k in NOTICE_FIELDS if k in row}
            for key in ("amount_yuan", "budget_yuan"):
                values[key] = float(values[key]) if values.get(key) not in ("", None) else None
            for key in ("supplier_count",):
                values[key] = int(values[key]) if values.get(key) not in ("", None) else None
            n = Notice(**values)
            extract_fields(n)
            notices.append(n)
        print(f"从现有 CSV 重算：{len(notices)} 条")
    else:
        crawler = Crawler(cfg)
        notices = crawler.collect()
        if args.max_details > 0:
            notices = notices[: args.max_details]
        print(f"待采集详情：{len(notices)} 条")
        for idx, notice in enumerate(notices, 1):
            crawler.enrich(notice)
            print(f"[{idx}/{len(notices)}] {notice.category} {notice.title[:45]}")
    projects = project_rows(notices)
    suppliers = supplier_rows(notices, cfg)
    metrics = metrics_rows(notices, projects, suppliers, cfg)
    write_csv(out / "notices.csv", (asdict(x) for x in notices), NOTICE_FIELDS)
    write_csv(out / "projects.csv", projects, list(projects[0]) if projects else ["project_key"])
    write_csv(out / "metrics.csv", metrics, ["scope", "metric", "value", "unit", "definition"])
    supplier_fields = list(suppliers[0]) if suppliers else ["supplier"]
    write_csv(out / "supplier_summary.csv", suppliers, supplier_fields)
    database_changes = save_sqlite(
        out / "procurement.sqlite", notices, cfg, out,
        args.gold_labels, args.intent_gold_labels,
    )
    summary = {
        "crawl_time": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "notice_count": len(notices),
        "project_count": len(projects),
        "error_count": sum(bool(x.error) for x in notices),
        "source": BASE,
        "database_changes": database_changes,
        "database_model": "P3_PROCUREMENT_INTENTIONS",
        "config": {key: value for key, value in cfg.items() if not key.startswith("_")},
    }
    (out / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
