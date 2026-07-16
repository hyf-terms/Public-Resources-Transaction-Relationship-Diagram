# Public Resources Transaction Relationship Diagram

政府采购、公共资源交易及央国企采购关系研究项目的初版代码。

当前版本首先实现中国政府采购网公开公告的低频采集、字段提取、公告去重、项目链路初步关联和供应商指标计算。代码仅使用 Python 标准库。

## 当前能力

- 采集公开招标、中标、成交、合同、更正和终止公告；
- 提取项目编号、采购人、供应商、预算、中标金额和履约期等字段；
- 保存公告原文、来源 URL、内容哈希和抓取时间；
- 按项目编号优先、标题与采购人辅助的规则关联公告；
- 区分取消/终止、废标/流标和延期事件；
- 输出公告、项目链路、供应商汇总、指标和 SQLite 数据库。

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

## 输出文件

- `notices.csv`：一行一条公告，是事件证据，不等同于订单；
- `projects.csv`：公告关联后的项目级初步视图；
- `supplier_summary.csv`：供应商金额、客户集中度和地区覆盖；
- `metrics.csv`：项目及供应商指标长表；
- `procurement.sqlite`：便于查询的 SQLite 数据库；
- `run_summary.json`：运行参数与质量摘要。

## 配置说明

- `scope`：`zygg` 为中央公告，`dfgg` 为地方公告；
- `categories`：需要采集的公告类别；
- `pages_per_category`：每类列表页数量；
- `start_date` / `end_date`：公告日期过滤；
- `keywords`：公司名称、采购人或主题关键词；
- `companies`：上市公司别名及上年营业收入，用于金额/收入指标；
- `metric_period_start`：新增采购人等指标的分析期起点。

## 重要口径

1. 一条公告不等于一笔新增订单。同一经济需求可能产生招标、中标、更正、终止和合同等多份公告。
2. 采购意向和招标属于需求侧，不能提前归因给最终中标供应商。
3. 预算、中标、合同和验收金额不能跨阶段相加。
4. 空字段表示源网页没有披露或当前未识别，不等同于数值零。
5. 新增采购人和首次合作指标需要分析期之前的历史数据；历史不足时保持为空。

## 初版限制

当前版本仍是验证型实现：

- 数据结构尚未拆分为项目根、采购轮次、标包、合同和验收批次；
- SQLite 目前按批次重建，并非追加式原始文档版本库；
- 尚未抓取附件、采购意向、验收、候选供应商和统一社会信用代码；
- 标题辅助匹配尚未输出匹配方法、分数和置信度；
- 多供应商但只披露总金额时采用均分，仅可用于初筛；
- 尚未实现供应商、品牌、上市公司和时点有效股权关系图。

后续将优先升级为：原始文档版本层 → 项目根 → 采购轮次 → 标包 → 合同 → 验收的追加式事件数据库。

## 合规

仅采集公开页面。使用时请遵守目标网站的 robots.txt、使用条款和适用法律，保持低频请求，不绕过验证码、登录或访问控制。
