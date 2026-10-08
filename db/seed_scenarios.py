"""生成 6 个情景演示库（确定性：random.Random(20261008)，可重复构建）。

情景列表：
  1. catalog.db            商品目录优化   —— 两个完整演示场景之一
  2. customer_behavior.db  客户行为分析   —— 两个完整演示场景之一
  3. inventory.db          库存异常分析
  4. reviews.db            评论反馈分析
  5. market.db             市场表现分析
  6. refunds.db            退款模式分析

每个库都在 db/demo/ 下重建。业务时间统一为 2026-09-08 ~ 2026-10-07 共 30 天。
"""
from __future__ import annotations

import random
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

SEED = 20261008
DAY_END = date(2026, 10, 7)
DAYS = 30
DAY_START = DAY_END - timedelta(days=DAYS - 1)
DEMO_DIR = Path(__file__).resolve().parent / "demo"


# ------------------------------------------------------------------ 通用工具


def _open(name: str) -> sqlite3.Connection:
    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    path = DEMO_DIR / name
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _weighted(rng: random.Random, pairs: list[tuple]):
    """按权重抽取：pairs = [(value, weight), ...]"""
    values = [p[0] for p in pairs]
    weights = [p[1] for p in pairs]
    return rng.choices(values, weights=weights, k=1)[0]


def _day(rng: random.Random) -> date:
    return DAY_START + timedelta(days=rng.randrange(DAYS))


def _dt(rng: random.Random, day: date) -> str:
    return datetime(day.year, day.month, day.day, rng.randrange(24), rng.randrange(60), rng.randrange(60)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _money(rng: random.Random, low: float, high: float, decimals: int = 2) -> float:
    return round(rng.uniform(low, high), decimals)


def _lognormal(rng: random.Random, mean: float, sigma: float = 0.45) -> float:
    """围绕 mean 的正偏分布（用于客单价、时长等）。"""
    return max(1.0, rng.lognormvariate(0, sigma) * mean)


# ================================================================== 情景一：商品目录优化


# (类目码, 类目名, 曝光份额, 点击率, 点击→转化率, 品牌池, 价格区间, 下架率, 叶子类目)
CATALOG_TOP = [
    (
        "C01", "服饰", 0.20, 0.155, 0.45,
        ["棉致", "衣念", "南岛", "白鹿", "简行"],
        (80, 600), 0.06,
        ["女装", "男装", "童装", "内衣", "鞋靴"],
    ),
    (
        "C02", "数码", 0.18, 0.215, 0.62,
        ["声阔", "雷柏", "京造", "绿联", "北弧"],
        (1500, 8000), 0.05,
        ["耳机", "键鼠", "智能穿戴", "存储", "影音"],
    ),
    (
        "C03", "家居", 0.38, 0.050, 0.15,
        ["宜巢", "木言", "暖居", "简宜", "安居"],
        (100, 2000), 0.15,
        ["收纳", "床品", "灯具", "家具", "厨具"],
    ),
    (
        "C04", "美妆", 0.14, 0.175, 0.50,
        ["花漾", "润本", "素颜", "青柚", "momo"],
        (50, 500), 0.08,
        ["护肤", "彩妆", "香氛", "面膜", "身体护理"],
    ),
    (
        "C05", "食品", 0.10, 0.130, 0.55,
        ["三只鹿", "良果", "禾田", "原野", "百味"],
        (10, 200), 0.07,
        ["坚果", "咖啡", "乳品", "茶饮", "零食"],
    ),
]

CATALOG_EXPOSURES = 12000  # 30 天搜索曝光总量

# 关键词池（关键词 → 其真实归属的类目码），用于识别"关键词类目错配"
CATALOG_KEYWORDS = {
    "C01": ["连衣裙", "牛仔裤", "针织衫", "羽绒服", "运动鞋", "卫衣", "衬衫", "半身裙"],
    "C02": ["蓝牙耳机", "机械键盘", "智能手表", "移动电源", "显示器", "路由器", "平板电脑", "摄像头"],
    "C03": ["收纳箱", "防水收纳", "四件套", "落地灯", "置物架", "沙发套", "保温杯", "晾衣架"],
    "C04": ["精华液", "口红", "面膜", "防晒霜", "粉底液", "眼影", "卸妆水", "香水"],
    "C05": ["坚果礼盒", "咖啡豆", "进口牛奶", "巧克力", "茶叶", "蜂蜜", "饼干", "速食面"],
}

MISMATCH_RATE = 0.06  # 搜索曝光中被错配到其他类目的比例


def build_catalog() -> dict:
    rng = random.Random(SEED)
    conn = _open("catalog.db")
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE categories (
            category_id TEXT PRIMARY KEY,
            category_name TEXT NOT NULL,
            parent_id TEXT,
            level INTEGER NOT NULL
        );
        CREATE TABLE keywords (
            keyword_id TEXT PRIMARY KEY,
            keyword TEXT NOT NULL,
            expected_category_id TEXT NOT NULL
        );
        CREATE TABLE products (
            product_id TEXT PRIMARY KEY,
            product_name TEXT NOT NULL,
            category_id TEXT NOT NULL,
            brand TEXT NOT NULL,
            price REAL NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE search_exposures (
            exposure_id TEXT PRIMARY KEY,
            keyword TEXT NOT NULL,
            category_id TEXT NOT NULL,
            product_id TEXT NOT NULL,
            exposure_date TEXT NOT NULL
        );
        CREATE TABLE clicks (
            click_id TEXT PRIMARY KEY,
            exposure_id TEXT NOT NULL,
            product_id TEXT NOT NULL,
            click_date TEXT NOT NULL
        );
        CREATE TABLE conversions (
            conversion_id TEXT PRIMARY KEY,
            click_id TEXT NOT NULL,
            product_id TEXT NOT NULL,
            order_id TEXT NOT NULL,
            amount REAL NOT NULL,
            conversion_date TEXT NOT NULL
        );
        CREATE INDEX idx_exp_cat ON search_exposures(category_id);
        CREATE INDEX idx_exp_kw ON search_exposures(keyword);
        CREATE INDEX idx_click_exp ON clicks(exposure_id);
        CREATE INDEX idx_conv_click ON conversions(click_id);
        """
    )

    # --- 类目 ---
    leaf_categories: dict[str, list[str]] = {}  # 顶级类目码 -> 叶子类目码列表
    cat_rows: list[tuple] = []
    cat_meta: dict[str, dict] = {}
    leaf_seq = 0
    for code, name, share, ctr, cvr, brands, price_range, delist, leaves in CATALOG_TOP:
        cat_rows.append((code, name, None, 1))
        cat_meta[code] = {
            "name": name, "share": share, "ctr": ctr, "cvr": cvr,
            "brands": brands, "price": price_range, "delist": delist,
        }
        leaf_codes = []
        for leaf_name in leaves:
            leaf_seq += 1
            leaf_code = f"{code}-{leaf_seq:02d}"
            cat_rows.append((leaf_code, f"{name}/{leaf_name}", code, 2))
            leaf_codes.append(leaf_code)
        leaf_categories[code] = leaf_codes
    cur.executemany("INSERT INTO categories VALUES (?,?,?,?)", cat_rows)

    # --- 关键词维度 ---
    kw_rows, kw_expected = [], {}
    kw_seq = 0
    for code, words in CATALOG_KEYWORDS.items():
        for word in words:
            kw_seq += 1
            kw_rows.append((f"K{kw_seq:03d}", word, code))
            kw_expected[word] = code
    cur.executemany("INSERT INTO keywords VALUES (?,?,?)", kw_rows)

    # --- 商品：每个顶级类目 100 个，均分到 5 个叶子上 ---
    products: list[dict] = []
    prod_seq = 0
    for code, name, *_ in CATALOG_TOP:
        meta = cat_meta[code]
        low, high = meta["price"]
        for i in range(100):
            prod_seq += 1
            leaf = leaf_categories[code][i % len(leaf_categories[code])]
            brand = meta["brands"][i % len(meta["brands"])]
            leaf_name = next(r[1] for r in cat_rows if r[0] == leaf).split("/")[-1]
            status = "下架" if rng.random() < meta["delist"] else "上架"
            products.append(
                {
                    "product_id": f"P{prod_seq:04d}",
                    "product_name": f"{brand} {leaf_name} {i + 1:03d}",
                    "category_id": leaf,
                    "top_category": code,
                    "brand": brand,
                    "price": round(rng.uniform(low, high), 2),
                    "status": status,
                    "created_at": (DAY_START - timedelta(days=rng.randrange(30, 400))).isoformat(),
                }
            )
    cur.executemany(
        "INSERT INTO products VALUES (?,?,?,?,?,?,?)",
        [(p["product_id"], p["product_name"], p["category_id"], p["brand"], p["price"], p["status"], p["created_at"]) for p in products],
    )
    products_by_top: dict[str, list[dict]] = {code: [] for code, *_ in CATALOG_TOP}
    for p in products:
        products_by_top[p["top_category"]].append(p)

    # --- 每个关键词的点击率抖动系数（制造"高曝光低点击"关键词） ---
    kw_mult = {word: rng.uniform(0.45, 1.55) for words in CATALOG_KEYWORDS.values() for word in words}

    # --- 搜索曝光 ---
    share_pairs = [(code, cat_meta[code]["share"]) for code, *_ in CATALOG_TOP]
    exposures: list[dict] = []
    for i in range(CATALOG_EXPOSURES):
        matched_code = _weighted(rng, share_pairs)
        mismatched = rng.random() < MISMATCH_RATE
        if mismatched:
            other_codes = [c for c, *_ in CATALOG_TOP if c != matched_code]
            keyword_code = rng.choice(other_codes)
        else:
            keyword_code = matched_code
        keyword = rng.choice(CATALOG_KEYWORDS[keyword_code])
        product = rng.choice(products_by_top[matched_code])
        day = _day(rng)
        exposures.append(
            {
                "exposure_id": f"E{i + 1:06d}",
                "keyword": keyword,
                "category_id": matched_code,
                "product_id": product["product_id"],
                "exposure_date": day.isoformat(),
                "mismatched": mismatched,
                "product": product,
            }
        )
    cur.executemany(
        "INSERT INTO search_exposures VALUES (?,?,?,?,?)",
        [(e["exposure_id"], e["keyword"], e["category_id"], e["product_id"], e["exposure_date"]) for e in exposures],
    )

    # --- 点击：按类目点击率 × 关键词系数 × 错配惩罚 ---
    clicks: list[dict] = []
    click_seq = 0
    for e in exposures:
        meta = cat_meta[e["category_id"]]
        p = meta["ctr"] * kw_mult[e["keyword"]] * (0.4 if e["mismatched"] else 1.0)
        if rng.random() < min(p, 0.9):
            click_seq += 1
            clicks.append(
                {
                    "click_id": f"C{click_seq:06d}",
                    "exposure_id": e["exposure_id"],
                    "product_id": e["product_id"],
                    "click_date": e["exposure_date"],
                    "category_id": e["category_id"],
                    "product": e["product"],
                }
            )
    cur.executemany(
        "INSERT INTO clicks VALUES (?,?,?,?)",
        [(c["click_id"], c["exposure_id"], c["product_id"], c["click_date"]) for c in clicks],
    )

    # --- 转化：按类目转化率 × 商品质量系数（下架商品大幅降低） ---
    quality = {p["product_id"]: rng.uniform(0.6, 1.4) for p in products}
    conversions: list[dict] = []
    conv_seq = 0
    order_seq = 0
    for c in clicks:
        meta = cat_meta[c["category_id"]]
        q = quality[c["product_id"]]
        if c["product"]["status"] == "下架":
            q *= 0.3
        p = meta["cvr"] * q
        if rng.random() < min(p, 0.95):
            conv_seq += 1
            order_seq += 1
            qty = rng.choices([1, 2, 3], weights=[0.8, 0.15, 0.05])[0]
            conversions.append(
                {
                    "conversion_id": f"V{conv_seq:06d}",
                    "click_id": c["click_id"],
                    "product_id": c["product_id"],
                    "order_id": f"ORD{order_seq:06d}",
                    "amount": round(c["product"]["price"] * qty, 2),
                    "conversion_date": c["click_date"],
                }
            )
    cur.executemany(
        "INSERT INTO conversions VALUES (?,?,?,?,?,?)",
        [
            (v["conversion_id"], v["click_id"], v["product_id"], v["order_id"], v["amount"], v["conversion_date"])
            for v in conversions
        ],
    )

    conn.commit()
    stats = _catalog_stats(cur)
    conn.close()
    return stats


def _catalog_stats(cur: sqlite3.Cursor) -> dict:
    cur.execute(
        """
        SELECT c.category_id, c.category_name,
               (SELECT COUNT(*) FROM search_exposures e WHERE e.category_id = c.category_id) AS exp,
               (SELECT COUNT(*) FROM clicks k JOIN search_exposures e2 ON e2.exposure_id = k.exposure_id
                 WHERE e2.category_id = c.category_id) AS clk,
               (SELECT COUNT(*) FROM conversions v JOIN clicks k2 ON k2.click_id = v.click_id
                 JOIN search_exposures e3 ON e3.exposure_id = k2.exposure_id
                 WHERE e3.category_id = c.category_id) AS conv
        FROM categories c WHERE c.level = 1 ORDER BY exp DESC
        """
    )
    funnel = [
        {
            "category": r[1],
            "exposures": r[2],
            "clicks": r[3],
            "conversions": r[4],
            "ctr": round(r[3] / r[2] * 100, 2) if r[2] else 0,
            "cvr": round(r[4] / r[3] * 100, 2) if r[3] else 0,
            "e2e": round(r[4] / r[2] * 100, 3) if r[2] else 0,
        }
        for r in cur.fetchall()
    ]
    cur.execute(
        """
        SELECT COUNT(*) FROM search_exposures e JOIN keywords k ON k.keyword = e.keyword
        WHERE e.category_id <> k.expected_category_id
        """
    )
    mismatch = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM products WHERE status = '下架'")
    delisted = cur.fetchone()[0]
    return {"funnel": funnel, "mismatch_exposures": mismatch, "delisted_products": delisted}


# ================================================================== 情景二：客户行为分析


CHANNELS = [("社交媒体", 0.45), ("自然搜索", 0.20), ("付费搜索", 0.20), ("邮件", 0.15)]
CHANNEL_SEGMENT_MIX = {
    "社交媒体": [("新客", 0.70), ("老客", 0.22), ("会员", 0.08)],
    "自然搜索": [("新客", 0.52), ("老客", 0.33), ("会员", 0.15)],
    "付费搜索": [("新客", 0.58), ("老客", 0.28), ("会员", 0.14)],
    "邮件": [("新客", 0.30), ("老客", 0.35), ("会员", 0.35)],
}
CART_PROB = {"社交媒体": 0.14, "自然搜索": 0.32, "付费搜索": 0.38, "邮件": 0.45}
ORDER_PROB = {"新客": 0.30, "老客": 0.45, "会员": 0.55}
DURATION_MEAN = {"社交媒体": 25.0, "自然搜索": 90.0, "付费搜索": 70.0, "邮件": 180.0}
AOV_MEAN = {"新客": 180.0, "老客": 260.0, "会员": 420.0}
CITIES = ["上海", "北京", "深圳", "广州", "杭州", "成都", "武汉", "南京", "西安", "重庆"]
PAGE_TYPES = [("首页", 0.25), ("列表页", 0.35), ("详情页", 0.30), ("购物车页", 0.10)]


def build_customer_behavior() -> dict:
    rng = random.Random(SEED + 1)
    conn = _open("customer_behavior.db")
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE users (
            user_id TEXT PRIMARY KEY,
            channel TEXT NOT NULL,
            segment TEXT NOT NULL,
            city TEXT NOT NULL,
            signup_date TEXT NOT NULL
        );
        CREATE TABLE visit_events (
            event_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            channel TEXT NOT NULL,
            visit_time TEXT NOT NULL,
            page_type TEXT NOT NULL,
            duration_seconds INTEGER NOT NULL
        );
        CREATE TABLE cart_events (
            event_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            product_id TEXT NOT NULL,
            add_time TEXT NOT NULL
        );
        CREATE TABLE order_events (
            event_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            order_id TEXT NOT NULL,
            amount REAL NOT NULL,
            order_time TEXT NOT NULL
        );
        CREATE INDEX idx_visit_user ON visit_events(user_id);
        CREATE INDEX idx_visit_session ON visit_events(session_id);
        CREATE INDEX idx_cart_session ON cart_events(session_id);
        CREATE INDEX idx_order_session ON order_events(session_id);
        """
    )

    # --- 用户 2000 个 ---
    users: list[dict] = []
    for i in range(2000):
        channel = _weighted(rng, CHANNELS)
        segment = _weighted(rng, CHANNEL_SEGMENT_MIX[channel])
        signup = DAY_START - timedelta(days=rng.randrange(1, 720))
        users.append(
            {
                "user_id": f"U{i + 1:05d}",
                "channel": channel,
                "segment": segment,
                "city": rng.choice(CITIES),
                "signup_date": signup.isoformat(),
            }
        )
    cur.executemany(
        "INSERT INTO users VALUES (?,?,?,?,?)",
        [(u["user_id"], u["channel"], u["segment"], u["city"], u["signup_date"]) for u in users],
    )

    # --- 访问事件：每用户 2~5 个会话，每会话 2~5 个页面 ---
    visits: list[tuple] = []
    cart_rows: list[tuple] = []
    order_rows: list[tuple] = []
    visit_seq = cart_seq = order_seq = 0
    session_seq = 0
    page_choices = [(2, 0.30), (3, 0.35), (4, 0.25), (5, 0.10)]

    for u in users:
        channel = u["channel"]
        segment = u["segment"]
        for _ in range(rng.randint(2, 5)):
            session_seq += 1
            session_id = f"S{session_seq:06d}"
            day = _day(rng)
            pages = _weighted(rng, page_choices)
            has_cart = rng.random() < CART_PROB[channel]
            for _page in range(pages):
                visit_seq += 1
                duration = max(3, int(_lognormal(rng, DURATION_MEAN[channel], 0.6)))
                visits.append(
                    (
                        f"VE{visit_seq:07d}",
                        u["user_id"],
                        session_id,
                        channel,
                        _dt(rng, day),
                        _weighted(rng, PAGE_TYPES),
                        duration,
                    )
                )
            if not has_cart:
                continue
            # 加购 1~2 件
            for _ in range(rng.choices([1, 2], weights=[0.72, 0.28])[0]):
                cart_seq += 1
                product_id = f"SKU{rng.randrange(1, 3001):05d}"
                cart_rows.append(
                    (f"CE{cart_seq:07d}", u["user_id"], session_id, product_id, _dt(rng, day))
                )
            # 每个会话至多一单
            if rng.random() < ORDER_PROB[segment]:
                order_seq += 1
                order_rows.append(
                    (
                        f"OE{order_seq:07d}",
                        u["user_id"],
                        session_id,
                        f"SO{order_seq:07d}",
                        round(_lognormal(rng, AOV_MEAN[segment], 0.45), 2),
                        _dt(rng, day),
                    )
                )

    cur.executemany("INSERT INTO visit_events VALUES (?,?,?,?,?,?,?)", visits)
    cur.executemany("INSERT INTO cart_events VALUES (?,?,?,?,?)", cart_rows)
    cur.executemany("INSERT INTO order_events VALUES (?,?,?,?,?,?)", order_rows)

    conn.commit()
    stats = _customer_stats(cur)
    conn.close()
    return stats


def _customer_stats(cur: sqlite3.Cursor) -> dict:
    cur.execute(
        """
        SELECT u.channel,
               COUNT(DISTINCT v.event_id) AS visits,
               COUNT(DISTINCT c.event_id) AS carts,
               COUNT(DISTINCT o.event_id) AS orders,
               COALESCE(SUM(o.amount), 0) AS gmv
        FROM users u
        LEFT JOIN visit_events v ON v.user_id = u.user_id
        LEFT JOIN cart_events c ON c.user_id = u.user_id
        LEFT JOIN order_events o ON o.user_id = u.user_id
        GROUP BY u.channel ORDER BY visits DESC
        """
    )
    by_channel = [
        {
            "channel": r[0],
            "visits": r[1],
            "carts": r[2],
            "orders": r[3],
            "gmv": round(r[4], 2),
            "visit_to_cart": round(r[2] / r[1] * 100, 3) if r[1] else 0,
            "cart_to_order": round(r[3] / r[2] * 100, 2) if r[2] else 0,
        }
        for r in cur.fetchall()
    ]
    cur.execute(
        """
        SELECT segment, COUNT(*) AS orders, ROUND(AVG(amount), 2) AS aov
        FROM order_events o JOIN users u ON u.user_id = o.user_id
        GROUP BY segment ORDER BY aov DESC
        """
    )
    by_segment = [{"segment": r[0], "orders": r[1], "aov": r[2]} for r in cur.fetchall()]
    return {"by_channel": by_channel, "by_segment": by_segment}


# ================================================================== 情景三：库存异常分析


def build_inventory() -> dict:
    rng = random.Random(SEED + 2)
    conn = _open("inventory.db")
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE products (
            product_id TEXT PRIMARY KEY, product_name TEXT NOT NULL,
            category TEXT NOT NULL, cost_price REAL NOT NULL
        );
        CREATE TABLE warehouses (
            warehouse_id TEXT PRIMARY KEY, warehouse_name TEXT NOT NULL,
            region TEXT NOT NULL, capacity INTEGER NOT NULL
        );
        CREATE TABLE stock_items (
            item_id TEXT PRIMARY KEY, product_id TEXT NOT NULL, warehouse_id TEXT NOT NULL,
            stock_qty INTEGER NOT NULL, safety_stock INTEGER NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE stock_inbound (
            inbound_id TEXT PRIMARY KEY, item_id TEXT NOT NULL, qty INTEGER NOT NULL, inbound_date TEXT NOT NULL
        );
        CREATE TABLE stock_outbound (
            outbound_id TEXT PRIMARY KEY, item_id TEXT NOT NULL, qty INTEGER NOT NULL, outbound_date TEXT NOT NULL
        );
        CREATE TABLE sales_volume (
            sale_id TEXT PRIMARY KEY, product_id TEXT NOT NULL, warehouse_id TEXT NOT NULL,
            qty INTEGER NOT NULL, sale_date TEXT NOT NULL
        );
        CREATE INDEX idx_stock_item ON stock_items(warehouse_id, product_id);
        """
    )

    categories = ["服饰", "数码", "家居", "美妆", "食品"]
    products = []
    for i in range(300):
        cat = categories[i % len(categories)]
        products.append((f"P{i + 1:04d}", f"{cat}商品{i + 1:03d}", cat, round(rng.uniform(20, 3000), 2)))
    cur.executemany("INSERT INTO products VALUES (?,?,?,?)", products)

    warehouses = [
        ("W01", "华东仓", "华东", 50000),
        ("W02", "华北仓", "华北", 40000),
        ("W03", "华南仓", "华南", 35000),
        ("W04", "西南仓", "西南", 25000),
    ]
    cur.executemany("INSERT INTO warehouses VALUES (?,?,?,?)", warehouses)

    # 库存：W01（华东仓）集中出现低于安全库存的异常
    stock_rows, item_index = [], []
    for wi, (wid, _wn, _rg, _cap) in enumerate(warehouses):
        for p in products:
            qty_base = rng.randint(50, 900)
            safety = rng.randint(80, 200)
            if wid == "W01" and rng.random() < 0.12:
                qty = rng.randint(0, max(1, safety - 20))  # 异常：低于安全库存
            elif wid == "W01" and rng.random() < 0.08:
                qty = rng.randint(1500, 3000)  # 异常：呆滞积压
            else:
                qty = max(qty_base, safety + rng.randint(10, 200))
            item_id = f"I{len(stock_rows) + 1:05d}"
            stock_rows.append((item_id, p[0], wid, qty, safety, DAY_END.isoformat()))
            item_index.append((item_id, p[0], wid))
    cur.executemany("INSERT INTO stock_items VALUES (?,?,?,?,?,?)", stock_rows)

    inbound, outbound, sales = [], [], []
    for idx, (item_id, product_id, wid) in enumerate(item_index):
        for _ in range(rng.randint(1, 4)):
            day = _day(rng)
            inbound.append((f"IN{len(inbound) + 1:06d}", item_id, rng.randint(20, 400), day.isoformat()))
        for _ in range(rng.randint(2, 6)):
            day = _day(rng)
            outbound.append((f"OUT{len(outbound) + 1:06d}", item_id, rng.randint(5, 200), day.isoformat()))
        for _ in range(rng.randint(1, 5)):
            day = _day(rng)
            sales.append((f"SL{len(sales) + 1:06d}", product_id, wid, rng.randint(1, 150), day.isoformat()))

    cur.executemany("INSERT INTO stock_inbound VALUES (?,?,?,?)", inbound)
    cur.executemany("INSERT INTO stock_outbound VALUES (?,?,?,?)", outbound)
    cur.executemany("INSERT INTO sales_volume VALUES (?,?,?,?,?)", sales)
    conn.commit()

    cur.execute("SELECT COUNT(*) FROM stock_items WHERE stock_qty < safety_stock")
    below = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM stock_items WHERE stock_qty >= 1500")
    dead = cur.fetchone()[0]
    cur.execute(
        "SELECT warehouse_id, COUNT(*) FROM stock_items WHERE stock_qty < safety_stock GROUP BY warehouse_id"
    )
    by_wh = {r[0]: r[1] for r in cur.fetchall()}
    conn.close()
    return {"below_safety": below, "dead_stock": dead, "below_by_warehouse": by_wh}


# ================================================================== 情景四：评论反馈分析


def build_reviews() -> dict:
    rng = random.Random(SEED + 3)
    conn = _open("reviews.db")
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE products (
            product_id TEXT PRIMARY KEY, product_name TEXT NOT NULL,
            category TEXT NOT NULL, price REAL NOT NULL
        );
        CREATE TABLE reviews (
            review_id TEXT PRIMARY KEY, product_id TEXT NOT NULL, rating INTEGER NOT NULL,
            review_text TEXT NOT NULL, sentiment TEXT NOT NULL, topic TEXT NOT NULL,
            review_date TEXT NOT NULL
        );
        CREATE TABLE ratings (
            rating_id TEXT PRIMARY KEY, product_id TEXT NOT NULL,
            avg_rating REAL NOT NULL, rating_count INTEGER NOT NULL, low_rating_ratio REAL NOT NULL,
            stat_date TEXT NOT NULL
        );
        CREATE TABLE refunds (
            refund_id TEXT PRIMARY KEY, product_id TEXT NOT NULL, order_id TEXT NOT NULL,
            refund_amount REAL NOT NULL, refund_date TEXT NOT NULL
        );
        CREATE INDEX idx_reviews_product ON reviews(product_id);
        CREATE INDEX idx_reviews_topic ON reviews(topic);
        """
    )

    categories = ["服饰", "数码", "家居", "美妆", "食品"]
    products = []
    for i in range(200):
        cat = categories[i % len(categories)]
        products.append((f"P{i + 1:04d}", f"{cat}商品{i + 1:03d}", cat, round(rng.uniform(30, 2500), 2)))
    cur.executemany("INSERT INTO products VALUES (?,?,?,?)", products)

    # 话题分布：数码类"描述不符"差评突出
    topics = ["物流", "质量", "价格", "客服", "描述不符"]
    texts = {
        "物流": ["发货很快，第二天就到了", "物流太慢，等了五天才收到", "包装完好，配送小哥态度好", "快递延误，没有任何通知"],
        "质量": ["做工扎实，用料不错", "用了两天就坏了，质量堪忧", "材质手感很好", "细节粗糙，有毛刺"],
        "价格": ["性价比很高，回购了", "刚买就降价，有点亏", "价格偏贵但值", "同款别家更便宜"],
        "客服": ["客服响应及时，问题解决得快", "客服一直不在线，没人处理", "售后态度很好", "咨询半天没人回复"],
        "描述不符": ["和详情页描述一致，满意", "实物和图片色差很大", "尺寸和标注不符合", "功能比宣传的少很多"],
    }
    negative_texts = {
        "物流": ["物流太慢，等了五天才收到", "快递延误，没有任何通知"],
        "质量": ["用了两天就坏了，质量堪忧", "细节粗糙，有毛刺"],
        "价格": ["刚买就降价，有点亏", "同款别家更便宜"],
        "客服": ["客服一直不在线，没人处理", "咨询半天没人回复"],
        "描述不符": ["实物和图片色差很大", "尺寸和标注不符合", "功能比宣传的少很多"],
    }

    reviews = []
    for i in range(1500):
        product = rng.choice(products)
        is_digital = product[2] == "数码"
        topic = _weighted(
            rng,
            [(t, 0.30 if (t == "描述不符" and is_digital) else 0.175) for t in topics],
        )
        # 差评率：数码"描述不符"最高
        neg_p = 0.40 if (topic == "描述不符" and is_digital) else 0.18
        negative = rng.random() < neg_p
        if negative:
            rating = rng.choices([1, 2, 3], weights=[0.45, 0.35, 0.20])[0]
            sentiment = "负"
            text = rng.choice(negative_texts[topic])
        else:
            rating = rng.choices([3, 4, 5], weights=[0.15, 0.45, 0.40])[0]
            sentiment = "中" if rating == 3 else "正"
            text = rng.choice([t for t in texts[topic] if t not in negative_texts[topic]])
        reviews.append(
            (f"R{i + 1:05d}", product[0], rating, text, sentiment, topic, _day(rng).isoformat())
        )
    cur.executemany("INSERT INTO reviews VALUES (?,?,?,?,?,?,?)", reviews)

    ratings = []
    for pi, product in enumerate(products):
        rows = [r for r in reviews if r[1] == product[0]]
        if not rows:
            continue
        avg = round(sum(r[2] for r in rows) / len(rows), 2)
        low = round(sum(1 for r in rows if r[2] <= 2) / len(rows), 4)
        ratings.append((f"RT{pi + 1:05d}", product[0], avg, len(rows), low, DAY_END.isoformat()))
    cur.executemany("INSERT INTO ratings VALUES (?,?,?,?,?,?)", ratings)

    refunds = []
    for i in range(400):
        product = rng.choice(products)
        refunds.append(
            (
                f"RF{i + 1:05d}",
                product[0],
                f"ORD{rng.randrange(1, 5000):06d}",
                round(product[3] * rng.uniform(0.6, 1.0), 2),
                _day(rng).isoformat(),
            )
        )
    cur.executemany("INSERT INTO refunds VALUES (?,?,?,?,?)", refunds)
    conn.commit()

    cur.execute("SELECT topic, COUNT(*) FROM reviews WHERE sentiment = '负' GROUP BY topic ORDER BY 2 DESC")
    neg_by_topic = {r[0]: r[1] for r in cur.fetchall()}
    cur.execute("SELECT COUNT(*) FROM reviews WHERE rating <= 2")
    low_count = cur.fetchone()[0]
    conn.close()
    return {"total_reviews": len(reviews), "low_rating": low_count, "negative_by_topic": neg_by_topic}


# ================================================================== 情景五：市场表现分析


def build_market() -> dict:
    rng = random.Random(SEED + 4)
    conn = _open("market.db")
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE channels (
            channel_id TEXT PRIMARY KEY, channel_name TEXT NOT NULL, channel_type TEXT NOT NULL
        );
        CREATE TABLE regions (
            region_id TEXT PRIMARY KEY, region_name TEXT NOT NULL
        );
        CREATE TABLE campaigns (
            campaign_id TEXT PRIMARY KEY, campaign_name TEXT NOT NULL, region_id TEXT NOT NULL,
            channel_id TEXT NOT NULL, spend REAL NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL
        );
        CREATE TABLE orders (
            order_id TEXT PRIMARY KEY, region_id TEXT NOT NULL, channel_id TEXT NOT NULL,
            category TEXT NOT NULL, amount REAL NOT NULL, order_date TEXT NOT NULL
        );
        CREATE TABLE sales_summary (
            summary_id TEXT PRIMARY KEY, region_id TEXT NOT NULL, category TEXT NOT NULL,
            sales_amount REAL NOT NULL, order_count INTEGER NOT NULL, stat_month TEXT NOT NULL
        );
        """
    )

    channels = [
        ("CH01", "搜索引擎", "线上"),
        ("CH02", "社交媒体", "线上"),
        ("CH03", "线下门店", "线下"),
        ("CH04", "电商平台", "线上"),
    ]
    cur.executemany("INSERT INTO channels VALUES (?,?,?)", channels)
    regions = [("RG01", "华东"), ("RG02", "华北"), ("RG03", "华南"), ("RG04", "西南"), ("RG05", "华中")]
    cur.executemany("INSERT INTO regions VALUES (?,?)", regions)

    # 华北区投放最高但订单平平 —— 演示叙事
    campaigns = []
    for i in range(40):
        region = _weighted(rng, [("RG02", 0.35), ("RG01", 0.20), ("RG03", 0.20), ("RG04", 0.15), ("RG05", 0.10)])
        channel = rng.choice(channels)[0]
        spend = round(rng.uniform(80000, 260000) if region == "RG02" else rng.uniform(20000, 120000), 2)
        start = DAY_START + timedelta(days=rng.randrange(0, 20))
        campaigns.append(
            (
                f"CP{i + 1:03d}",
                f"{dict((r[0], r[1]) for r in regions)[region]}{dict((c[0], c[1]) for c in channels)[channel]}投放{i + 1:02d}",
                region,
                channel,
                spend,
                start.isoformat(),
                (start + timedelta(days=rng.randint(7, 21))).isoformat(),
            )
        )
    cur.executemany("INSERT INTO campaigns VALUES (?,?,?,?,?,?,?)", campaigns)

    categories = ["服饰", "数码", "家居", "美妆", "食品"]
    orders = []
    for i in range(3000):
        region = _weighted(rng, [("RG02", 0.14), ("RG01", 0.28), ("RG03", 0.24), ("RG04", 0.18), ("RG05", 0.16)])
        base = 1200 if region == "RG01" else (900 if region == "RG02" else 1000)
        orders.append(
            (
                f"ORD{i + 1:06d}",
                region,
                rng.choice(channels)[0],
                rng.choice(categories),
                round(_lognormal(rng, base, 0.5), 2),
                _day(rng).isoformat(),
            )
        )
    cur.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?)", orders)

    summary = []
    months = ["2026-07", "2026-08", "2026-09"]
    for ri, (rid, _rn) in enumerate(regions):
        for cat in categories:
            for month in months:
                base = 380000 if rid == "RG01" else (240000 if rid == "RG02" else 300000)
                summary.append(
                    (
                        f"SS{len(summary) + 1:05d}",
                        rid,
                        cat,
                        round(base * rng.uniform(0.7, 1.25), 2),
                        rng.randint(200, 1400),
                        month,
                    )
                )
    cur.executemany("INSERT INTO sales_summary VALUES (?,?,?,?,?,?)", summary)
    conn.commit()

    cur.execute(
        """
        SELECT c.region_id, SUM(c.spend) AS spend,
               (SELECT COALESCE(SUM(o.amount), 0) FROM orders o WHERE o.region_id = c.region_id) AS gmv
        FROM campaigns c GROUP BY c.region_id ORDER BY spend DESC
        """
    )
    roi = [
        {"region": r[0], "spend": round(r[1], 2), "gmv": round(r[2], 2), "roi": round(r[2] / r[1], 2) if r[1] else 0}
        for r in cur.fetchall()
    ]
    conn.close()
    return {"roi_by_region": roi}


# ================================================================== 情景六：退款模式分析


def build_refunds() -> dict:
    rng = random.Random(SEED + 5)
    conn = _open("refunds.db")
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE users (
            user_id TEXT PRIMARY KEY, username TEXT NOT NULL, segment TEXT NOT NULL, register_date TEXT NOT NULL
        );
        CREATE TABLE products (
            product_id TEXT PRIMARY KEY, product_name TEXT NOT NULL, category TEXT NOT NULL, price REAL NOT NULL
        );
        CREATE TABLE orders (
            order_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, product_id TEXT NOT NULL,
            amount REAL NOT NULL, order_status TEXT NOT NULL, order_date TEXT NOT NULL
        );
        CREATE TABLE refund_reasons (
            reason_id TEXT PRIMARY KEY, reason_name TEXT NOT NULL, reason_type TEXT NOT NULL
        );
        CREATE TABLE refund_requests (
            refund_id TEXT PRIMARY KEY, order_id TEXT NOT NULL, user_id TEXT NOT NULL,
            reason_id TEXT NOT NULL, refund_amount REAL NOT NULL, refund_status TEXT NOT NULL,
            refund_date TEXT NOT NULL
        );
        CREATE INDEX idx_refund_order ON refund_requests(order_id);
        CREATE INDEX idx_refund_reason ON refund_requests(reason_id);
        """
    )

    users = []
    for i in range(800):
        users.append(
            (
                f"U{i + 1:05d}",
                f"用户{i + 1:04d}",
                _weighted(rng, [("新客", 0.5), ("老客", 0.32), ("会员", 0.18)]),
                (DAY_START - timedelta(days=rng.randrange(30, 800))).isoformat(),
            )
        )
    cur.executemany("INSERT INTO users VALUES (?,?,?,?)", users)

    categories = ["服饰", "数码", "家居", "美妆", "食品"]
    products = []
    for i in range(250):
        cat = categories[i % len(categories)]
        products.append((f"P{i + 1:04d}", f"{cat}商品{i + 1:03d}", cat, round(rng.uniform(30, 4000), 2)))
    cur.executemany("INSERT INTO products VALUES (?,?,?,?)", products)

    reasons = [
        ("RS01", "质量问题", "商品"),
        ("RS02", "描述不符", "商品"),
        ("RS03", "尺寸不合适", "商品"),
        ("RS04", "发货太慢", "物流"),
        ("RS05", "七天无理由", "服务"),
        ("RS06", "重复下单", "服务"),
        ("RS07", "商品破损", "物流"),
    ]
    cur.executemany("INSERT INTO refund_reasons VALUES (?,?,?)", reasons)

    orders, refunds = [], []
    refund_seq = 0
    for i in range(1800):
        user = rng.choice(users)
        product = rng.choice(products)
        order_id = f"ORD{i + 1:06d}"
        day = _day(rng)
        refunded = rng.random() < 0.42
        orders.append(
            (
                order_id,
                user[0],
                product[0],
                round(product[3] * rng.uniform(0.8, 2.0), 2),
                "已退款" if refunded else rng.choice(["已完成", "已发货", "已签收"]),
                day.isoformat(),
            )
        )
        if not refunded:
            continue
        refund_seq += 1
        # 质量问题在数码类集中（演示叙事）
        if product[2] == "数码":
            reason = _weighted(rng, [("RS01", 0.35), ("RS02", 0.20), ("RS07", 0.15), ("RS03", 0.10), ("RS05", 0.20)])
        else:
            reason = _weighted(
                rng,
                [("RS01", 0.12), ("RS02", 0.14), ("RS03", 0.20), ("RS04", 0.16), ("RS05", 0.26), ("RS06", 0.05), ("RS07", 0.07)],
            )
        refunds.append(
            (
                f"RF{refund_seq:05d}",
                order_id,
                user[0],
                reason,
                round(product[3] * rng.uniform(0.5, 1.0), 2),
                _weighted(rng, [("已通过", 0.72), ("处理中", 0.18), ("已驳回", 0.10)]),
                (day + timedelta(days=rng.randint(1, 10))).isoformat(),
            )
        )
    cur.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?)", orders)
    cur.executemany("INSERT INTO refund_requests VALUES (?,?,?,?,?,?,?)", refunds)
    conn.commit()

    cur.execute(
        """
        SELECT rr.reason_name, COUNT(*) FROM refund_requests r
        JOIN refund_reasons rr ON rr.reason_id = r.reason_id GROUP BY rr.reason_name ORDER BY 2 DESC
        """
    )
    by_reason = {r[0]: r[1] for r in cur.fetchall()}
    cur.execute(
        """
        SELECT p.category, rr.reason_name, COUNT(*) FROM refund_requests r
        JOIN orders o ON o.order_id = r.order_id JOIN products p ON p.product_id = o.product_id
        JOIN refund_reasons rr ON rr.reason_id = r.reason_id
        WHERE rr.reason_name = '质量问题' GROUP BY p.category ORDER BY 3 DESC
        """
    )
    quality_by_cat = {r[0]: r[2] for r in cur.fetchall()}
    conn.close()
    return {"orders": len(orders), "refunds": len(refunds), "by_reason": by_reason, "quality_by_category": quality_by_cat}


# ================================================================== 入口


BUILDERS = [
    ("catalog.db", "商品目录优化", build_catalog),
    ("customer_behavior.db", "客户行为分析", build_customer_behavior),
    ("inventory.db", "库存异常分析", build_inventory),
    ("reviews.db", "评论反馈分析", build_reviews),
    ("market.db", "市场表现分析", build_market),
    ("refunds.db", "退款模式分析", build_refunds),
]


def build_all(verbose: bool = True) -> dict:
    results = {}
    for filename, title, builder in BUILDERS:
        stats = builder()
        results[filename] = stats
        if verbose:
            print(f"[seed_scenarios] {filename:24s} {title} 生成完成")
    return results


if __name__ == "__main__":
    import json

    print(json.dumps(build_all(), ensure_ascii=False, indent=2, default=str))
