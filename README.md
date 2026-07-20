# Public Resources Transaction Relationship Diagram

政府采购、公共资源交易及央国企采购关系研究项目的初版代码。

当前版本实现中国政府采购网公开公告的低频采集，已完成 P0 数据正确性、P1 企业实体与链路匹配、P2 生命周期归并，以及 P3 采购意向需求侧信号。代码仅使用 Python 标准库。

## 当前能力

- 采集公开招标、中标、成交、合同、更正和终止公告；
- 提取项目编号、采购人、供应商、预算、中标金额和履约期等字段；
- 保存公告原文、来源 URL、内容哈希和抓取时间；
- 按项目编号优先、标题与采购人辅助的规则关联公告；
- 区分取消/终止、废标/流标和延期事件；
- 输出公告、项目链路、供应商汇总、指标和追加式 SQLite 事件数据库。
- 相同内容重复入库保持幂等，网页内容变化时保留新旧版本。
- 所有关联记录匹配方法、分数、置信度和复核状态。
- 归一采购人、供应商和配置别名，不使用激进的企业名称模糊合并；
- 项目编号或标包号冲突时阻断链路，歧义匹配进入人工复核；
- 支持导入人工金标并计算自动匹配的精确率、召回率和 F1。
- 将更正、延期、废标和终止公告关联到受影响的采购轮次或标包；
- 建立第一轮与二次/重新采购之间的 `RETRY_OF` 关系；
- 解析采购意向表格，并将意向预算严格保留在需求侧；
- 输出当前对象状态和 P1/P2/P3 数据质量快照。

## 快速开始

复制示例配置：

```powershell
Copy-Item config.example.json config.json
```

运行采集：

```powershell
python procurement_crawler.py --config config.json --output-dir data
```

测试时限制详情数量：

```powershell
python procurement_crawler.py --config config.json --output-dir data --max-details 10
```

使用已有 `notices.csv` 的原文重新计算结构化字段，不访问网站：

```powershell
python procurement_crawler.py --config config.json --output-dir data --rebuild-from-csv
```

生成 P1 人工标注模板：

```powershell
python procurement_crawler.py --config config.json --output-dir data --rebuild-from-csv --write-gold-template
```

## 输出文件

文件默认写入 `data/`。CSV 是本次运行后的当前快照，SQLite 是持续增量、保留历史版本的主库。

| 文件 | 主要指标（中文 / English） |
|---|---|
| `notices.csv` | 发布时间 / Publish Time、采购人 / Buyer、项目编号 / Project Code、供应商 / Supplier、预算 / Budget、中标或合同金额 / Award or Contract Amount |
| `projects.csv` | 项目状态 / Project Status、公告数 / Notice Count、招标到中标天数 / Tender-to-Award Days、中标到合同天数 / Award-to-Contract Days、预算折价率 / Budget Discount Rate |
| `supplier_summary.csv` | 中标金额 / Award Amount、中标次数 / Award Count、新采购人数 / New Buyer Count、首次合作采购人占比 / First-Cooperation Buyer Ratio、客户集中度 / Buyer Concentration、客户HHI / Buyer HHI、覆盖省份数 / Province Count |
| `metrics.csv` | 公告数 / Notice Count、项目数 / Project Count、招标到中标转化率 / Tender-to-Award Conversion Rate、平均预算折价率 / Average Budget Discount Rate、取消终止率 / Cancellation Rate、废标流标率 / Failed-Bid Rate、延期率 / Delay Rate |
| `organizations.csv` | 标准名称 / Canonical Name、机构类型 / Organization Type、证券代码 / Listed Code |
| `chain_links.csv` | 链路类型 / Link Type、匹配方法 / Match Method、匹配分数 / Match Score、置信度 / Confidence、复核状态 / Review Status |
| `match_review_queue.csv` | 复核原因 / Review Reason、任务状态 / Review Status、人工决定 / Decision |
| `lifecycle_links.csv` | 生命周期关系 / Lifecycle Relation、受影响对象 / Affected Object、匹配分数 / Match Score、置信度 / Confidence |
| `lifecycle_review_queue.csv` | 生命周期复核原因 / Lifecycle Review Reason、任务状态 / Review Status、人工决定 / Decision |
| `object_lifecycle_state.csv` | 当前状态 / Current State、状态事件 / State Event、状态时间 / State Time |
| `intent_items.csv` | 采购意向明细 / Procurement Intent Items：采购人 / Buyer、项目名称 / Project Name、采购品目 / Category、预计采购时间 / Expected Purchase Date、意向预算 / Planned Budget |
| `intent_tender_links.csv` | 意向招标关系 / Intent-to-Tender Links：匹配分数 / Match Score、匹配方法 / Match Method、复核状态 / Review Status |
| `intent_match_review_queue.csv` | 意向匹配复核队列 / Intent Match Review Queue：复核原因 / Review Reason、候选分数 / Candidate Score、任务状态 / Review Status |
| `demand_signals.csv` | 需求侧信号 / Demand-side Signals：意向数量 / Intent Count、计划预算 / Planned Budget，按月份、采购人和品类汇总且不含供应商归因 |
| `data_quality_metrics.csv` | 文档数 / Document Count、版本数 / Version Count、链路数 / Link Count、待复核数 / Pending Review Count |
| `run_summary.json` | 抓取时间 / Crawl Time、公告数 / Notice Count、错误数 / Error Count、数据库新增量 / Database Changes、匹配评估 / Matching Evaluation |
| `procurement.sqlite` | P0—P3 全部明细、历史版本、事件、金额、实体、链路与复核数据 / Full P0–P3 Data |

## 配置说明

- `scope`：`zygg` 为中央公告，`dfgg` 为地方公告；
- `categories`：需要采集的公告类别；
- `crawl_mode`：默认 `previous_day`，按北京时间抓取前一天全部公告并动态翻页；`daily` 抓取当天；`pages` 按固定页数抓取；
- `max_pages_per_category`：每日模式下每类最大翻页数，防止异常页面导致无限翻页；
- `intent_urls`：无需验证码即可访问的采购意向详情页；
- `intent_seed_csv`：结构化采购意向种子文件路径；
- `pages_per_category`：仅在 `crawl_mode=pages` 时生效；
- `start_date` / `end_date`：仅在 `crawl_mode=pages` 时作为公告日期过滤；
- `keywords`：公司名称、采购人或主题关键词；
- `companies`：上市公司别名及上年营业收入，用于金额/收入指标；
- `metric_period_start`：新增采购人等指标的分析期起点。

## 重要口径

1. 一条公告不等于一笔新增订单。同一经济需求可能产生招标、中标、更正、终止和合同等多份公告。
2. 采购意向和招标属于需求侧，不能提前归因给最终中标供应商。
3. 预算、中标、合同和验收金额不能跨阶段相加。
4. 空字段表示源网页没有披露或当前未识别，不等同于数值零。
5. 新增采购人和首次合作指标需要分析期之前的历史数据；历史不足时保持为空。

## P0 数据库

P0 新增以下核心表：

- `source_document` / `source_document_version`
- `root_project` / `procurement_attempt` / `package`
- `contract` / `acceptance`
- `procurement_event`
- `amount_observation`
- `document_object_link`

当前状态通过 `current_event` 和 `current_object_state` 视图计算，不覆盖历史事件。详细结构和查询示例见 [P0 数据模型](docs/p0-schema.md)。

## P1 匹配层

P1 在同一个 SQLite 中追加以下表，不修改 P0 历史：

- `organization` / `organization_alias`
- `document_organization_role`
- `document_match_feature`
- `document_chain_candidate` / `document_chain_link`
- `match_review_queue` / `match_gold_label`

详细评分规则、复核流程和金标格式见 [P1 实体与链路匹配](docs/p1-matching.md)。

## P2 生命周期

P2 在同一个 SQLite 中追加：

- `lifecycle_document_feature`
- `lifecycle_link_candidate` / `lifecycle_link`
- `lifecycle_review_queue`
- `current_lifecycle_link`

普通更正和延期不会被误记为新增订单；废标落在标包层；二次或重新采购建立新旧轮次关系。详细规则见 [P2 生命周期模型](docs/p2-lifecycle.md)。

## P3 采购意向

P3 将采购意向作为项目级需求证据，生成 `INTENT_PUBLISHED` 和 `INTENT_BUDGET`，不生成标包或供应商收入。意向与后续招标按采购人、标题、品类和时间保守匹配，模糊候选进入人工复核。详细规则见 [P3 采购意向模型](docs/p3-intentions.md)。

运行测试：

```powershell
python -m unittest discover -s tests -v
```

## 当前限制

当前版本仍是验证型实现：

- 尚未抓取附件、验收、候选供应商和统一社会信用代码；
- P1 评分阈值仍是保守的工程初值，必须用300—500条人工标注链校准；
- P2 标题辅助关联同样需要人工样本校准，缺项目编号时默认进入复核；
- P3 意向匹配需要用不同地区和公告模板的人工样本持续校准；
- 标包号提取已阻止明确冲突，但复杂多包表格仍需附件解析；
- 多供应商总金额在正式数据库中标记为未分配；旧版供应商CSV仍采用均分，仅可用于初筛；
- 尚未实现供应商、品牌、上市公司和时点有效股权关系图。

下一步应扩大 P1—P3 人工金标集，按采购方式和公告模板校准阈值，再接入验收公告和合同履约信息。

## 合规

仅采集公开页面。使用时请遵守目标网站的 robots.txt、使用条款和适用法律，保持低频请求，不绕过验证码、登录或访问控制。
