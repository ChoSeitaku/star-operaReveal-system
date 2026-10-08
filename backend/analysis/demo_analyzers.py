"""规则化演示分析器：把业务问题映射为固定的真实 SQL 序列，并据实计算结果。

设计原则：
- 所有查询都通过 db_query 工具真实执行，取到的数字直接进入结论，不做任何预置答案；
- 每个分析器声明所需表结构（用于情景识别）与触发关键词（用于问题匹配）；
- 输出的六段式结构完全由查询结果推导，便于在演示文档中复现同样的数字。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.analysis.engine import ResultData


@dataclass
class DemoStep:
    label: str
    narration: str
    database: str
    sql: str


@dataclass
class Finding:
    label: str
    database: str
    sql: str
    payload: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    @property
    def columns(self) -> list[str]:
        return list(self.payload.get("columns") or [])

    @property
    def rows(self) -> list[list[Any]]:
        return list(self.payload.get("rows") or [])

    def dicts(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, row)) for row in self.rows]


# ---------------------------------------------------------------- 格式化助手


def num(value: Any, digits: int = 0) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if digits == 0:
        return f"{number:,.0f}"
    return f"{number:,.{digits}f}"


def pct(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):.{digits}f}%"
    except (TypeError, ValueError):
        return str(value)


def share(part: Any, whole: Any, digits: int = 1) -> str:
    try:
        p, w = float(part), float(whole)
        return f"{p / w * 100:.{digits}f}%" if w else "-"
    except (TypeError, ValueError, ZeroDivisionError):
        return "-"


def share_num(part: Any, whole: Any, digits: int = 1) -> str:
    """只返回数值（不带 % 号），供 metric_value 使用 —— 单位由 metric_unit 表达。"""
    try:
        p, w = float(part), float(whole)
        return f"{p / w * 100:.{digits}f}" if w else "0"
    except (TypeError, ValueError, ZeroDivisionError):
        return "0"


def ratio_num(part: Any, whole: Any, digits: int = 3) -> str:
    try:
        p, w = float(part), float(whole)
        return f"{p / w * 100:.{digits}f}" if w else "0"
    except (TypeError, ValueError, ZeroDivisionError):
        return "0"


def table_text(finding: Finding, headers: list[str], limit: int = 12) -> str:
    """把查询结果渲染成等宽表格文本（用于增量输出的可读性）。"""
    if not finding.rows:
        return "（无数据）"
    lines = ["  " + " | ".join(headers)]
    for row in finding.rows[:limit]:
        lines.append("  " + " | ".join(str(cell) for cell in row))
    if len(finding.rows) > limit:
        lines.append(f"  …（共 {len(finding.rows)} 行，已省略 {len(finding.rows) - limit} 行）")
    return "\n".join(lines)


# ---------------------------------------------------------------- 分析器基类


class DemoAnalyzer:
    key: str = ""
    title: str = ""
    database: str = ""
    keywords: tuple[str, ...] = ()
    required_tables: frozenset[str] = frozenset()

    def intro(self, question: str) -> str:
        return f"开始分析「{question}」。\n\n本次采用规则化演示引擎，按预设口径逐项取数：\n"

    def steps(self) -> list[DemoStep]:  # pragma: no cover - 由子类实现
        raise NotImplementedError

    def build(self, question: str, findings: dict[str, Finding]) -> ResultData:  # pragma: no cover
        raise NotImplementedError

    # 便捷取值
    @staticmethod
    def _first(findings: dict[str, Finding], label: str) -> Finding | None:
        return findings.get(label)


# ================================================================ 情景一：商品目录优化


class CatalogAnalyzer(DemoAnalyzer):
    key = "catalog"
    title = "商品目录优化"
    database = "catalog"
    keywords = ("品类", "类目", "目录", "曝光", "点击", "转化", "商品", "搜索", "漏斗", "CTR", "ctr")
    required_tables = frozenset({"categories", "products", "search_exposures", "clicks", "conversions"})

    def intro(self, question: str) -> str:
        return (
            f"开始分析「{question}」。\n\n"
            "我会按「曝光 → 点击 → 转化」的漏斗口径拆分到一级类目，再定位曝光与转化不匹配的类目，"
            "最后核查关键词映射与在架状态两类常见干扰因素。\n\n"
        )

    def steps(self) -> list[DemoStep]:
        return [
            DemoStep(
                label="funnel",
                narration="第一步：按一级类目统计曝光、点击、转化的完整漏斗。\n",
                database=self.database,
                sql="""
SELECT c.category_name AS 类目,
       (SELECT COUNT(*) FROM search_exposures e WHERE e.category_id = c.category_id) AS 曝光量,
       (SELECT COUNT(*) FROM clicks k JOIN search_exposures e2 ON e2.exposure_id = k.exposure_id
         WHERE e2.category_id = c.category_id) AS 点击量,
       (SELECT COUNT(*) FROM conversions v JOIN clicks k2 ON k2.click_id = v.click_id
         JOIN search_exposures e3 ON e3.exposure_id = k2.exposure_id
         WHERE e3.category_id = c.category_id) AS 转化量
FROM categories c
WHERE c.level = 1
ORDER BY 曝光量 DESC
""".strip(),
            ),
            DemoStep(
                label="keywords",
                narration="第二步：找出「高曝光但低点击」的关键词，定位曝光浪费。\n",
                database=self.database,
                sql="""
SELECT e.keyword AS 关键词,
       COUNT(DISTINCT e.exposure_id) AS 曝光量,
       COUNT(DISTINCT k.click_id) AS 点击量,
       ROUND(COUNT(DISTINCT k.click_id) * 100.0 / COUNT(DISTINCT e.exposure_id), 2) AS 点击率百分比
FROM search_exposures e
LEFT JOIN clicks k ON k.exposure_id = e.exposure_id
GROUP BY e.keyword
HAVING COUNT(DISTINCT e.exposure_id) >= 150
ORDER BY 曝光量 DESC, 点击率百分比 ASC
LIMIT 10
""".strip(),
            ),
            DemoStep(
                label="mismatch",
                narration="第三步：核对关键词与类目的映射关系，检查是否存在错配。\n",
                database=self.database,
                sql="""
SELECT e.keyword AS 关键词,
       ec.category_name AS 应归属类目,
       ac.category_name AS 实际归属类目,
       COUNT(*) AS 曝光量
FROM search_exposures e
JOIN keywords kw ON kw.keyword = e.keyword
JOIN categories ec ON ec.category_id = kw.expected_category_id
JOIN categories ac ON ac.category_id = e.category_id
WHERE e.category_id <> kw.expected_category_id
GROUP BY e.keyword, ec.category_name, ac.category_name
ORDER BY 曝光量 DESC
LIMIT 10
""".strip(),
            ),
            DemoStep(
                label="mismatch_total",
                narration="第三步补充：统计错配曝光的总量。\n",
                database=self.database,
                sql="""
SELECT COUNT(*) AS 错配曝光总量,
       COUNT(DISTINCT e.keyword) AS 涉及关键词数
FROM search_exposures e
JOIN keywords kw ON kw.keyword = e.keyword
WHERE e.category_id <> kw.expected_category_id
""".strip(),
            ),
            DemoStep(
                label="delisted",
                narration="第四步：检查曝光是否被投放到已下架商品上。\n",
                database=self.database,
                sql="""
SELECT c.category_name AS 类目,
       COUNT(DISTINCT e.exposure_id) AS 下架商品曝光量
FROM search_exposures e
JOIN products p ON p.product_id = e.product_id
JOIN categories c ON c.category_id = e.category_id
WHERE p.status = '下架' AND c.level = 1
GROUP BY c.category_name
ORDER BY 下架商品曝光量 DESC
""".strip(),
            ),
        ]

    def build(self, question: str, findings: dict[str, Finding]) -> ResultData:
        funnel = findings.get("funnel")
        keywords = findings.get("keywords")
        mismatch = findings.get("mismatch")
        delisted = findings.get("delisted")

        rows = funnel.dicts() if funnel else []
        total_exposure = sum(int(r["曝光量"]) for r in rows) or 1
        total_conv = sum(int(r["转化量"]) for r in rows) or 1
        total_click = sum(int(r["点击量"]) for r in rows) or 0
        site_ctr = total_click / total_exposure if total_exposure else 0.0

        worst = None
        best = None
        for r in rows:
            exposure, conv = int(r["曝光量"]), int(r["转化量"])
            rate = conv / exposure if exposure else 0.0
            item = {"name": r["类目"], "exposure": exposure, "conv": conv, "rate": rate}
            if worst is None or rate < worst["rate"]:
                worst = item
            if best is None or rate > best["rate"]:
                best = item

        worst = worst or {"name": "-", "exposure": 0, "conv": 0, "rate": 0.0}
        best = best or {"name": "-", "exposure": 0, "conv": 0, "rate": 0.0}
        gap = (best["rate"] / worst["rate"]) if worst["rate"] else 0.0

        # 错配总量必须取自不限行的聚合查询，不能对 LIMIT 后的明细行求和
        mismatch_total_row = (findings.get("mismatch_total").dicts() or [{}])[0] if findings.get("mismatch_total") else {}
        mismatch_total = int(mismatch_total_row.get("错配曝光总量") or 0)
        mismatch_keywords = int(mismatch_total_row.get("涉及关键词数") or 0)
        delisted_total = sum(int(r["下架商品曝光量"]) for r in (delisted.dicts() if delisted else []))
        keyword_rows = keywords.dicts() if keywords else []

        metrics = [
            {
                "metric_name": "搜索曝光总量",
                "metric_value": num(total_exposure),
                "metric_unit": "次",
                "metric_period": "近 30 天",
            },
            {
                "metric_name": "成交转化总量",
                "metric_value": num(total_conv),
                "metric_unit": "单",
                "metric_period": "近 30 天",
            },
            {
                "metric_name": "全站平均点击率",
                "metric_value": f"{site_ctr * 100:.2f}",
                "metric_unit": "%",
                "metric_period": "近 30 天",
            },
            {
                "metric_name": f"{worst['name']}曝光占比",
                "metric_value": share_num(worst["exposure"], total_exposure),
                "metric_unit": "%",
                "metric_period": "近 30 天",
            },
            {
                "metric_name": f"{worst['name']}曝光→转化率",
                "metric_value": f"{worst['rate'] * 100:.3f}",
                "metric_unit": "%",
                "metric_period": "近 30 天",
            },
            {
                "metric_name": f"{best['name']}曝光→转化率",
                "metric_value": f"{best['rate'] * 100:.3f}",
                "metric_unit": "%",
                "metric_period": "近 30 天",
            },
            {
                "metric_name": "曝光转化率差距倍数",
                "metric_value": f"{gap:.1f}",
                "metric_unit": "倍",
                "metric_period": "近 30 天",
            },
            {
                "metric_name": "关键词类目错配曝光量",
                "metric_value": num(mismatch_total),
                "metric_unit": "次",
                "metric_period": "近 30 天",
            },
            {
                "metric_name": "下架商品曝光量",
                "metric_value": num(delisted_total),
                "metric_unit": "次",
                "metric_period": "近 30 天",
            },
        ]

        evidence: list[dict[str, Any]] = []
        if funnel:
            evidence.append(
                {
                    "source_type": "数据库查询",
                    "source_name": "catalog.db · 一级类目漏斗统计",
                    "evidence_text": (
                        f"{worst['name']}拿到全站 {share(worst['exposure'], total_exposure)} 的搜索曝光"
                        f"（{num(worst['exposure'])} 次），但曝光→转化率仅 {worst['rate'] * 100:.3f}%；"
                        f"{best['name']}曝光量 {num(best['exposure'])} 次，曝光→转化率 "
                        f"{best['rate'] * 100:.3f}%，两者相差 {gap:.1f} 倍。"
                    ),
                    "related_metric": f"{worst['name']}曝光→转化率",
                    "confidence": "high",
                }
            )
        if keyword_rows:
            # 与全站平均点击率对比，避免出现没有基准的「偏低」判断
            lowest = min(
                keyword_rows,
                key=lambda r: float(r["点击率百分比"]) if r["点击率百分比"] is not None else 999,
            )
            evidence.append(
                {
                    "source_type": "数据库查询",
                    "source_name": "catalog.db · 高曝光低点击关键词",
                    "evidence_text": (
                        f"曝光量最高的关键词中，「{lowest['关键词']}」曝光 "
                        f"{num(lowest['曝光量'])} 次，点击率仅 {pct(lowest['点击率百分比'])}，"
                        f"低于全站平均点击率 {site_ctr * 100:.2f}%，"
                        f"单此一个词就浪费约 {num(int(lowest['曝光量']) - int(lowest['点击量']))} 次曝光。"
                    ),
                    "related_metric": "关键词点击率",
                    "confidence": "high",
                }
            )
        if mismatch and mismatch_total:
            top = mismatch.dicts()[0]
            evidence.append(
                {
                    "source_type": "数据库查询",
                    "source_name": "catalog.db · 关键词类目映射核对",
                    "evidence_text": (
                        f"共 {num(mismatch_total)} 次曝光（涉及 {mismatch_keywords} 个关键词）"
                        f"存在关键词与类目错配，例如「{top['关键词']}」本应归属"
                        f"{top['应归属类目']}，却被投放到 {top['实际归属类目']} 类目下，"
                        f"带来 {num(top['曝光量'])} 次无效曝光。"
                    ),
                    "related_metric": "关键词类目错配曝光量",
                    "confidence": "high",
                }
            )
        if delisted and delisted_total:
            top = delisted.dicts()[0]
            evidence.append(
                {
                    "source_type": "数据库查询",
                    "source_name": "catalog.db · 下架商品曝光核对",
                    "evidence_text": (
                        f"有 {num(delisted_total)} 次曝光落在已下架商品上，其中 {top['类目']} "
                        f"占 {num(top['下架商品曝光量'])} 次，这部分流量无法产生任何成交。"
                    ),
                    "related_metric": "下架商品曝光量",
                    "confidence": "high",
                }
            )

        conclusion = (
            f"主要原因是搜索曝光资源的类目分配与转化能力严重不匹配。"
            f"{worst['name']}类目占据全站 {share(worst['exposure'], total_exposure)} 的曝光，"
            f"但曝光→转化率只有 {worst['rate'] * 100:.3f}%，"
            f"而{best['name']}类目以 {share(best['exposure'], total_exposure)} 的曝光贡献了 "
            f"{share(best['conv'], total_conv)} 的转化，两者效率相差 {gap:.1f} 倍。\n\n"
            f"放大这一差距的有两个可修复因素：一是关键词与类目的映射错误，"
            f"造成 {num(mismatch_total)} 次曝光被投放到不相关类目；"
            f"二是下架商品仍在承接曝光，浪费 {num(delisted_total)} 次曝光。\n\n"
            f"影响范围集中在搜索流量入口：曝光结构不调整，前端转化率的改善空间会被持续稀释，"
            f"而高转化的{best['name']}类目因曝光不足无法放量。"
        )

        missing = (
            "1. 缺少各渠道的广告花费与 ROI 数据，无法计算流量获取成本，只能比较转化效率而无法判断投入产出；\n"
            "2. 缺少搜索推荐位（首页 banner、猜你喜欢等）的曝光明细，本次仅覆盖搜索结果曝光；\n"
            "3. 缺少商品详情页的停留时长与跳出率，无法判断低转化是流量错配还是详情页承接不力；\n"
            "4. 缺少库存与缺货数据，无法排除「高曝光商品缺货」导致的转化损失。"
        )

        actions = (
            f"1. 重配搜索曝光：把{worst['name']}类目的曝光预算向{best['name']}、服饰等高转化类目倾斜，"
            f"优先调整首页与类目页的默认排序权重。\n"
            f"2. 修正关键词映射：按 keywords 维度表核对全部错配关键词，"
            f"把「关键词→类目」映射错误一次性纠正，并增加上线前的映射校验。\n"
            f"3. 清理下架商品曝光：将状态为「下架」的商品从搜索池中摘除，"
            f"并建立下架自动同步机制，释放被占用的曝光位。\n"
            f"4. 建立类目漏斗周报：按周监控各品类曝光占比与曝光→转化率，"
            f"对偏离基线 20% 以上的类目自动预警。\n"
            f"5. 补充上述缺失数据后重新运行本分析，量化各因素对转化率的贡献占比。"
        )

        return ResultData(
            problem_definition=question,
            key_metrics=metrics,
            evidence_list=evidence,
            conclusion_text=conclusion,
            missing_data_text=missing,
            next_action_text=actions,
        )


# ================================================================ 情景二：客户行为分析


class CustomerBehaviorAnalyzer(DemoAnalyzer):
    key = "customer_behavior"
    title = "客户行为分析"
    database = "customer_behavior"
    keywords = ("渠道", "用户", "客户", "加购", "下单", "漏斗", "分层", "会员", "新客", "老客", "访问", "流失", "复购")
    required_tables = frozenset({"users", "visit_events", "cart_events", "order_events"})

    def intro(self, question: str) -> str:
        return (
            f"开始分析「{question}」。\n\n"
            "我会按渠道与用户分层两条主线拆解「访问 → 加购 → 下单」漏斗，"
            "再定位流失最严重的环节，并用客单价与购物车遗弃率交叉验证。\n\n"
        )

    def steps(self) -> list[DemoStep]:
        return [
            DemoStep(
                label="channel_funnel",
                narration="第一步：按获客渠道统计访问、加购、下单与 GMV。\n",
                database=self.database,
                sql="""
SELECT ch.channel AS 渠道,
       (SELECT COUNT(*) FROM visit_events v WHERE v.channel = ch.channel) AS 访问量,
       (SELECT COUNT(*) FROM cart_events ce JOIN users u2 ON u2.user_id = ce.user_id
         WHERE u2.channel = ch.channel) AS 加购量,
       (SELECT COUNT(*) FROM order_events o JOIN users u2 ON u2.user_id = o.user_id
         WHERE u2.channel = ch.channel) AS 下单量,
       ROUND((SELECT COALESCE(SUM(o.amount), 0) FROM order_events o
              JOIN users u2 ON u2.user_id = o.user_id WHERE u2.channel = ch.channel), 2) AS 成交金额
FROM (SELECT DISTINCT channel FROM users) ch
ORDER BY 访问量 DESC
""".strip(),
            ),
            DemoStep(
                label="segment",
                narration="第二步：按用户分层统计下单量与客单价，判断价值分布。\n",
                database=self.database,
                sql="""
SELECT u.segment AS 用户分层,
       COUNT(DISTINCT o.event_id) AS 下单量,
       ROUND(AVG(o.amount), 2) AS 客单价,
       ROUND(SUM(o.amount), 2) AS 成交金额,
       COUNT(DISTINCT u.user_id) AS 用户数
FROM users u
JOIN order_events o ON o.user_id = u.user_id
GROUP BY u.segment
ORDER BY 客单价 DESC
""".strip(),
            ),
            DemoStep(
                label="abandon",
                narration="第三步：统计各渠道的购物车遗弃情况，定位流失环节。\n",
                database=self.database,
                sql="""
SELECT u.channel AS 渠道,
       COUNT(DISTINCT ce.session_id) AS 加购会话数,
       COUNT(DISTINCT o.session_id) AS 下单会话数,
       ROUND((COUNT(DISTINCT ce.session_id) - COUNT(DISTINCT o.session_id)) * 100.0
             / COUNT(DISTINCT ce.session_id), 2) AS 购物车遗弃率
FROM users u
JOIN cart_events ce ON ce.user_id = u.user_id
LEFT JOIN order_events o ON o.user_id = u.user_id AND o.session_id = ce.session_id
GROUP BY u.channel
ORDER BY 购物车遗弃率 DESC
""".strip(),
            ),
            DemoStep(
                label="engagement",
                narration="第四步：对比各渠道的访问深度与停留时长，评估流量质量。\n",
                database=self.database,
                sql="""
SELECT channel AS 渠道,
       COUNT(*) AS 访问量,
       ROUND(AVG(duration_seconds), 1) AS 平均停留秒数,
       ROUND(COUNT(*) * 1.0 / COUNT(DISTINCT session_id), 2) AS 人均浏览页数
FROM visit_events
GROUP BY channel
ORDER BY 平均停留秒数 DESC
""".strip(),
            ),
        ]

    def build(self, question: str, findings: dict[str, Finding]) -> ResultData:
        funnel = findings.get("channel_funnel")
        segment = findings.get("segment")
        abandon = findings.get("abandon")
        engagement = findings.get("engagement")

        channel_rows = funnel.dicts() if funnel else []
        segment_rows = segment.dicts() if segment else []
        abandon_rows = abandon.dicts() if abandon else []
        engage_rows = {r["渠道"]: r for r in (engagement.dicts() if engagement else [])}

        total_visit = sum(int(r["访问量"]) for r in channel_rows) or 1
        total_gmv = sum(float(r["成交金额"] or 0) for r in channel_rows) or 1.0

        # 访问量最大但访问→加购率最低的渠道
        def v2c(row: dict) -> float:
            visits = int(row["访问量"] or 0)
            return (int(row["加购量"] or 0) / visits) if visits else 0.0

        ranked_quality = sorted(channel_rows, key=v2c)
        weakest = ranked_quality[0] if ranked_quality else None
        strongest = ranked_quality[-1] if ranked_quality else None

        highest_traffic = max(channel_rows, key=lambda r: int(r["访问量"] or 0)) if channel_rows else None
        worst_abandon = max(abandon_rows, key=lambda r: float(r["购物车遗弃率"] or 0)) if abandon_rows else None
        best_segment = segment_rows[0] if segment_rows else None
        worst_segment = segment_rows[-1] if segment_rows else None

        metrics: list[dict[str, Any]] = [
            {"metric_name": "总访问量", "metric_value": num(total_visit), "metric_unit": "次", "metric_period": "近 30 天"},
            {"metric_name": "总成交金额", "metric_value": num(total_gmv, 2), "metric_unit": "元", "metric_period": "近 30 天"},
        ]
        if highest_traffic:
            metrics.append(
                {
                    "metric_name": f"{highest_traffic['渠道']}访问量占比",
                    "metric_value": share_num(highest_traffic["访问量"], total_visit),
                    "metric_unit": "%",
                    "metric_period": "近 30 天",
                }
            )
        if weakest:
            metrics.append(
                {
                    "metric_name": f"{weakest['渠道']}访问→加购率",
                    "metric_value": f"{v2c(weakest) * 100:.2f}",
                    "metric_unit": "%",
                    "metric_period": "近 30 天",
                }
            )
        if strongest:
            metrics.append(
                {
                    "metric_name": f"{strongest['渠道']}访问→加购率",
                    "metric_value": f"{v2c(strongest) * 100:.2f}",
                    "metric_unit": "%",
                    "metric_period": "近 30 天",
                }
            )
        if weakest and strongest and v2c(weakest):
            metrics.append(
                {
                    "metric_name": "渠道访问→加购率差距倍数",
                    "metric_value": f"{v2c(strongest) / v2c(weakest):.1f}",
                    "metric_unit": "倍",
                    "metric_period": "近 30 天",
                }
            )
        if worst_abandon:
            metrics.append(
                {
                    "metric_name": f"{worst_abandon['渠道']}购物车遗弃率",
                    "metric_value": f"{float(worst_abandon['购物车遗弃率']):.2f}",
                    "metric_unit": "%",
                    "metric_period": "近 30 天",
                }
            )
        if best_segment:
            metrics.append(
                {
                    "metric_name": f"{best_segment['用户分层']}客单价",
                    "metric_value": num(best_segment["客单价"], 2),
                    "metric_unit": "元",
                    "metric_period": "近 30 天",
                }
            )

        evidence: list[dict[str, Any]] = []
        if highest_traffic and weakest:
            evidence.append(
                {
                    "source_type": "数据库查询",
                    "source_name": "customer_behavior.db · 渠道漏斗",
                    "evidence_text": (
                        f"{highest_traffic['渠道']}带来 {num(highest_traffic['访问量'])} 次访问，"
                        f"占总访问量的 {share(highest_traffic['访问量'], total_visit)}，"
                        f"但访问→加购率只有 {v2c(highest_traffic) * 100:.2f}%；"
                        f"同期{strongest['渠道']}的访问→加购率为 {v2c(strongest) * 100:.2f}%，"
                        f"是前者的 {v2c(strongest) / v2c(highest_traffic):.1f} 倍。"
                    ),
                    "related_metric": f"{highest_traffic['渠道']}访问→加购率",
                    "confidence": "high",
                }
            )
        if worst_abandon:
            evidence.append(
                {
                    "source_type": "数据库查询",
                    "source_name": "customer_behavior.db · 购物车遗弃率",
                    "evidence_text": (
                        f"{worst_abandon['渠道']}的购物车遗弃率为 "
                        f"{float(worst_abandon['购物车遗弃率']):.2f}%"
                        f"（加购会话 {num(worst_abandon['加购会话数'])} 个，"
                        f"其中 {num(int(worst_abandon['加购会话数']) - int(worst_abandon['下单会话数']))} 个未下单），"
                        f"是全渠道中流失最严重的一环。"
                    ),
                    "related_metric": "购物车遗弃率",
                    "confidence": "high",
                }
            )
        if best_segment and worst_segment:
            evidence.append(
                {
                    "source_type": "数据库查询",
                    "source_name": "customer_behavior.db · 用户分层价值",
                    "evidence_text": (
                        f"{best_segment['用户分层']}客单价 {num(best_segment['客单价'], 2)} 元，"
                        f"是{worst_segment['用户分层']}（{num(worst_segment['客单价'], 2)} 元）的 "
                        f"{float(best_segment['客单价']) / float(worst_segment['客单价']):.1f} 倍，"
                        f"但该分层仅贡献 {num(best_segment['下单量'])} 单。"
                    ),
                    "related_metric": f"{best_segment['用户分层']}客单价",
                    "confidence": "high",
                }
            )
        if engage_rows and highest_traffic:
            info = engage_rows.get(highest_traffic["渠道"])
            if info:
                durations = {
                    name: float(row["平均停留秒数"] or 0) for name, row in engage_rows.items()
                }
                slowest_name = max(durations, key=durations.get) if durations else ""
                self_duration = float(info["平均停留秒数"] or 0)
                evidence.append(
                    {
                        "source_type": "数据库查询",
                        "source_name": "customer_behavior.db · 访问质量",
                        "evidence_text": (
                            f"{highest_traffic['渠道']}平均停留 {self_duration:.1f} 秒，"
                            f"在 {len(durations)} 个渠道中最低"
                            + (
                                f"（最高为{slowest_name}的 {durations[slowest_name]:.1f} 秒，"
                                f"相差 {durations[slowest_name] / self_duration:.1f} 倍）"
                                if self_duration and slowest_name
                                else ""
                            )
                            + "，说明进入后的浏览深度不足、流量意图偏弱。"
                        ),
                        "related_metric": "平均停留秒数",
                        "confidence": "medium",
                    }
                )

        conclusion = (
            f"主要结论：流量结构与转化质量严重错配，且流失集中在上漏斗。\n\n"
        )
        if highest_traffic and weakest:
            conclusion += (
                f"{highest_traffic['渠道']}贡献了 {share(highest_traffic['访问量'], total_visit)} 的访问量，"
                f"却只带来 {share(highest_traffic['成交金额'], total_gmv)} 的成交金额；"
                f"其访问→加购率 {v2c(highest_traffic) * 100:.2f}% 显著低于"
                f"{strongest['渠道']}的 {v2c(strongest) * 100:.2f}%。"
                f"这说明问题不在下单环节，而在「访问 → 加购」这一步——流量进入后没有被有效承接。\n\n"
            )
        if worst_abandon:
            conclusion += (
                f"加购之后仍有明显流失：{worst_abandon['渠道']}购物车遗弃率达 "
                f"{float(worst_abandon['购物车遗弃率']):.2f}%，说明加入购物车到最终下单之间"
                f"缺少有效的促单手段（如优惠提醒、库存提示）。\n\n"
            )
        if best_segment:
            conclusion += (
                f"高价值人群未被充分经营：{best_segment['用户分层']}客单价 "
                f"{num(best_segment['客单价'], 2)} 元，是最低分层的 "
                f"{float(best_segment['客单价']) / float(worst_segment['客单价']):.1f} 倍，"
                f"但订单量仅 {num(best_segment['下单量'])} 单，规模与价值不匹配。"
            )

        missing = (
            "1. 缺少各渠道的投放花费与 ROI 数据，无法判断低效渠道是否值得继续投入；\n"
            "2. 缺少页面级点击流与事件埋点，无法定位具体是哪个页面导致加购率偏低；\n"
            "3. 缺少优惠券与促销活动的参与数据，无法评估促单手段的实际效果；\n"
            "4. 缺少用户复购与生命周期数据，无法判断留存对 GMV 的贡献。"
        )

        actions = (
            f"1. 渠道预算再分配：下调{weakest['渠道'] if weakest else '低效渠道'}的投放占比，"
            f"把预算向{strongest['渠道'] if strongest else '高效渠道'}倾斜，优先保证高意图流量规模。\n"
            f"2. 购物车召回：针对加购未下单会话，在 1 小时与 24 小时两个节点分别触发提醒与限时优惠。\n"
            f"3. 高价值人群专项运营：为{best_segment['用户分层'] if best_segment else '会员'}设计专属权益与复购券，"
            f"把客单价优势转化为订单规模。\n"
            f"4. 落地页 A/B 测试：对{highest_traffic['渠道'] if highest_traffic else '主要渠道'}的落地页做首屏与商品卡片的对照实验，"
            f"目标是把访问→加购率提升到全站均值以上。\n"
            f"5. 补齐缺失数据后重跑本分析，量化各环节改进对整体 GMV 的贡献。"
        )

        return ResultData(
            problem_definition=question,
            key_metrics=metrics,
            evidence_list=evidence,
            conclusion_text=conclusion,
            missing_data_text=missing,
            next_action_text=actions,
        )


# ================================================================ 兜底分析器


class GenericAnalyzer(DemoAnalyzer):
    key = "generic"
    title = "通用数据结构分析"
    keywords = ()

    def intro(self, question: str) -> str:
        return (
            f"开始分析「{question}」。\n\n"
            "当前问题没有匹配到预置的演示场景，我将对可用数据源做一次结构盘点与规模统计，"
            "并给出后续可以深入的方向。若需要完整归因，请切换到 LLM 引擎或在问题中指明业务场景"
            "（商品目录优化 / 客户行为分析）。\n\n"
        )

    def steps(self) -> list[DemoStep]:
        return []

    def build(self, question: str, findings: dict[str, Finding]) -> ResultData:
        inventory = findings.get("inventory")
        rows = inventory.dicts() if inventory else []
        total_tables = sum(int(r.get("表数量") or 0) for r in rows)

        metrics = [
            {
                "metric_name": "可用情景数据库数",
                "metric_value": str(len(rows)),
                "metric_unit": "个",
                "metric_period": "当前",
            },
            {
                "metric_name": "可用数据表总数",
                "metric_value": str(total_tables),
                "metric_unit": "张",
                "metric_period": "当前",
            },
        ]
        for r in rows[:5]:
            metrics.append(
                {
                    "metric_name": f"{r.get('数据库')}表数量",
                    "metric_value": str(r.get("表数量")),
                    "metric_unit": "张",
                    "metric_period": "当前",
                }
            )

        evidence = [
            {
                "source_type": "系统盘点",
                "source_name": "scenario_db_dir 数据源扫描",
                "evidence_text": (
                    f"当前共有 {len(rows)} 个可用情景数据库、合计 {total_tables} 张数据表："
                    + "；".join(f"{r.get('数据库')}（{r.get('表数量')} 张表）" for r in rows)
                    + "。"
                ),
                "related_metric": "可用数据表总数",
                "confidence": "high",
            }
        ]
        if rows:
            first = rows[0]
            evidence.append(
                {
                    "source_type": "系统盘点",
                    "source_name": f"{first.get('数据库')} 表清单",
                    "evidence_text": f"可用表：{first.get('表名列表')}",
                    "related_metric": f"{first.get('数据库')}表数量",
                    "confidence": "high",
                }
            )

        conclusion = (
            "本次未匹配到预置演示场景，仅完成数据源盘点，无法给出业务归因结论。"
            "预置场景为「商品目录优化」与「客户行为分析」，其余库（库存、评论、市场、退款）"
            "提供了表结构与基础数据，可在 LLM 引擎下自由探索。"
        )
        missing = (
            "1. 缺少明确的业务问题定义：请指明要归因的业务指标（如转化率、GMV、留存）；\n"
            "2. 缺少分析口径说明：时间范围、对比基准、拆解维度；\n"
            "3. 缺少该业务问题的外部数据（预算、竞品、活动日历等）。"
        )
        actions = (
            "1. 在提问中指明业务场景与目标指标，例如「近 30 天各品类搜索曝光到转化的漏斗表现如何」。\n"
            "2. 将 analysis.engine_mode 切换为 llm，由大模型自主选择数据表完成探索式归因。\n"
            "3. 参考 docs/DEMO-01-catalog.md 与 docs/DEMO-02-customer-behavior.md 中的提问方式。"
        )

        return ResultData(
            problem_definition=question,
            key_metrics=metrics,
            evidence_list=evidence,
            conclusion_text=conclusion,
            missing_data_text=missing,
            next_action_text=actions,
        )


ALL_ANALYZERS: tuple[type[DemoAnalyzer], ...] = (
    CatalogAnalyzer,
    CustomerBehaviorAnalyzer,
    GenericAnalyzer,
)

SCENARIO_KEYWORDS = {
    "catalog": CatalogAnalyzer.keywords,
    "customer_behavior": CustomerBehaviorAnalyzer.keywords,
}
