"""生成演示数据库（可重复执行）。

电商场景：12 张业务表 + 1 张敏感表（users_password，演示 blocked_tables 拦截）。
表间有外键关联，可演示多表 JOIN。固定随机种子，时间锚定运行日期回溯 180 天。
"""

from __future__ import annotations

import random
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent
DB_PATH = DATA_DIR / "demo.db"
SCHEMA_PATH = DATA_DIR / "schema.sql"

RNG_SEED = 42
DAYS_SPAN = 180

BUSINESS_TABLES = [
    "categories", "suppliers", "users", "addresses", "products", "inventory",
    "orders", "order_items", "payments", "shipments", "reviews", "coupons",
]
SENSITIVE_TABLES = ["users_password"]

DDL = """
CREATE TABLE categories (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL,
    parent_id INTEGER REFERENCES categories(id), created_at TEXT NOT NULL
);
CREATE TABLE suppliers (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL,
    contact_email TEXT, country TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE users (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
    city TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE addresses (
    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
    receiver TEXT NOT NULL, phone TEXT NOT NULL, province TEXT NOT NULL,
    city TEXT NOT NULL, detail TEXT NOT NULL, is_default INTEGER NOT NULL
);
CREATE TABLE products (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL,
    category_id INTEGER NOT NULL REFERENCES categories(id),
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
    price REAL NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE inventory (
    id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id),
    warehouse TEXT NOT NULL, quantity INTEGER NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE orders (
    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
    status TEXT NOT NULL, total_amount REAL NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE order_items (
    id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL, unit_price REAL NOT NULL
);
CREATE TABLE payments (
    id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id),
    method TEXT NOT NULL, amount REAL NOT NULL, status TEXT NOT NULL, paid_at TEXT NOT NULL
);
CREATE TABLE shipments (
    id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id),
    carrier TEXT NOT NULL, tracking_no TEXT NOT NULL, status TEXT NOT NULL,
    shipped_at TEXT NOT NULL, delivered_at TEXT
);
CREATE TABLE reviews (
    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    rating INTEGER NOT NULL, comment TEXT, created_at TEXT NOT NULL
);
CREATE TABLE coupons (
    id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE, discount_type TEXT NOT NULL,
    discount_value REAL NOT NULL, min_amount REAL NOT NULL, expires_at TEXT NOT NULL
);
CREATE TABLE users_password (
    user_id INTEGER PRIMARY KEY REFERENCES users(id), password_hash TEXT NOT NULL
);
"""

SURNAMES = "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许"
GIVEN = ["伟", "芳", "娜", "敏", "静", "磊", "洋", "勇", "艳", "杰", "涛", "明", "超", "霞", "平"]
CITIES = ["北京", "上海", "深圳", "杭州", "成都", "武汉", "西安", "南京", "广州", "苏州"]
PROV = {"北京": "北京", "上海": "上海", "深圳": "广东", "广州": "广东", "杭州": "浙江",
        "成都": "四川", "武汉": "湖北", "西安": "陕西", "南京": "江苏", "苏州": "江苏"}
STATUSES = ["paid", "paid", "paid", "paid", "pending", "cancelled", "refunded"]
METHODS = ["alipay", "wechat", "card"]
CARRIERS = ["顺丰", "中通", "圆通", "京东", "韵达"]
WAREHOUSES = ["华北仓", "华东仓", "华南仓"]
COUNTRIES = ["中国", "中国", "中国", "美国", "德国", "日本"]
PRODUCTS = [
    ("无线蓝牙耳机", "电子产品", 199.0), ("机械键盘", "电子产品", 349.0),
    ("27寸显示器", "电子产品", 1299.0), ("USB-C扩展坞", "电子产品", 159.0),
    ("智能手环", "电子产品", 269.0), ("降噪头戴耳机", "电子产品", 899.0),
    ("北欧风台灯", "家居", 89.0), ("记忆棉枕头", "家居", 129.0),
    ("香薰加湿器", "家居", 149.0), ("懒人沙发", "家居", 499.0), ("四件套床品", "家居", 259.0),
    ("Python编程入门", "图书", 69.0), ("SQL必知必会", "图书", 49.0),
    ("深入理解计算机系统", "图书", 139.0), ("设计模式", "图书", 89.0),
    ("纯棉T恤", "服饰", 79.0), ("牛仔裤", "服饰", 199.0),
    ("运动卫衣", "服饰", 229.0), ("防晒外套", "服饰", 159.0),
    ("坚果礼盒", "食品", 128.0), ("挂耳咖啡", "食品", 68.0),
    ("有机燕麦片", "食品", 45.0), ("黑巧克力", "食品", 58.0), ("冻干水果脆", "食品", 36.0),
]
CATEGORY_NAMES = ["电子产品", "家居", "图书", "服饰", "食品"]
SUPPLIER_NAMES = ["华强北电子", "云栖智造", "锦绣家居", "书香文化", "优选食品",
                  "潮流服饰", "环球贸易", "极速供应链", "品质优选", "东方甄选"]
COMMENTS = ["质量不错", "性价比高", "包装精美", "符合预期", "会回购", "客服好", "发货慢", "很满意", None, None]

N_USERS = 60
N_ORDERS = 400


def _dt(rng, anchor, max_ago, min_ago=0):
    d = anchor - timedelta(days=rng.uniform(min_ago, max_ago), hours=rng.uniform(0, 24))
    return d.strftime("%Y-%m-%d %H:%M:%S")


def seed(db_path=DB_PATH):
    rng = random.Random(RNG_SEED)
    anchor = datetime.now()
    SCHEMA_PATH.write_text(DDL, encoding="utf-8")
    db_path.unlink(missing_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(DDL)

        cats = [(i, n, None, _dt(rng, anchor, DAYS_SPAN + 200)) for i, n in enumerate(CATEGORY_NAMES, 1)]
        conn.executemany("INSERT INTO categories VALUES (?,?,?,?)", cats)
        cat_id = {n: i for i, n, *_ in cats}

        sups = [(i, n, f"supplier{i:02d}@vendor.com", rng.choice(COUNTRIES), _dt(rng, anchor, DAYS_SPAN + 200))
                for i, n in enumerate(SUPPLIER_NAMES, 1)]
        conn.executemany("INSERT INTO suppliers VALUES (?,?,?,?,?)", sups)

        users = [(i, rng.choice(SURNAMES) + rng.choice(GIVEN), f"user{i:03d}@example.com",
                  rng.choice(CITIES), _dt(rng, anchor, DAYS_SPAN + 180)) for i in range(1, N_USERS + 1)]
        conn.executemany("INSERT INTO users VALUES (?,?,?,?,?)", users)

        aid = 1
        addrs = []
        for uid in range(1, N_USERS + 1):
            for k in range(rng.randint(1, 2)):
                c = rng.choice(CITIES)
                addrs.append((aid, uid, users[uid - 1][1],
                              f"13{rng.randint(100000000, 999999999)}", PROV[c], c,
                              f"{rng.choice(['科技园', '中心大道', '人民路'])}{rng.randint(1, 999)}号",
                              1 if k == 0 else 0))
                aid += 1
        conn.executemany("INSERT INTO addresses VALUES (?,?,?,?,?,?,?,?)", addrs)

        prods = [(i, n, cat_id[c], rng.randint(1, len(SUPPLIER_NAMES)), p, _dt(rng, anchor, DAYS_SPAN + 90))
                 for i, (n, c, p) in enumerate(PRODUCTS, 1)]
        conn.executemany("INSERT INTO products VALUES (?,?,?,?,?,?)", prods)

        iid = 1
        invs = []
        for pid in range(1, len(PRODUCTS) + 1):
            for wh in rng.sample(WAREHOUSES, rng.randint(1, 2)):
                invs.append((iid, pid, wh, rng.randint(0, 500), _dt(rng, anchor, 30)))
                iid += 1
        conn.executemany("INSERT INTO inventory VALUES (?,?,?,?,?)", invs)

        item_id = pay_id = ship_id = 1
        for oid in range(1, N_ORDERS + 1):
            items = []
            for pid in rng.sample(range(1, len(PRODUCTS) + 1), rng.randint(1, 4)):
                q = rng.randint(1, 3)
                items.append((item_id, oid, pid, q, PRODUCTS[pid - 1][2]))
                item_id += 1
            total = round(sum(q * p for *_, q, p in items), 2)
            st = rng.choice(STATUSES)
            ct = _dt(rng, anchor, DAYS_SPAN)
            conn.execute("INSERT INTO orders VALUES (?,?,?,?,?)", (oid, rng.randint(1, N_USERS), st, total, ct))
            conn.executemany("INSERT INTO order_items VALUES (?,?,?,?,?)", items)
            if st in ("paid", "refunded"):
                conn.execute("INSERT INTO payments VALUES (?,?,?,?,?,?)",
                             (pay_id, oid, rng.choice(METHODS), total,
                              "refunded" if st == "refunded" else "success", ct))
                pay_id += 1
            if st == "paid":
                dv = rng.random() < 0.75
                conn.execute("INSERT INTO shipments VALUES (?,?,?,?,?,?,?)",
                             (ship_id, oid, rng.choice(CARRIERS), f"SF{rng.randint(10**11, 10**12 - 1)}",
                              "delivered" if dv else "shipped", ct, _dt(rng, anchor, DAYS_SPAN) if dv else None))
                ship_id += 1

        revs = [(r, rng.randint(1, N_USERS), rng.randint(1, len(PRODUCTS)), rng.randint(1, 5),
                 rng.choice(COMMENTS), _dt(rng, anchor, DAYS_SPAN)) for r in range(1, 201)]
        conn.executemany("INSERT INTO reviews VALUES (?,?,?,?,?,?)", revs)

        coups = []
        for c in range(1, 16):
            fx = rng.random() < 0.6
            coups.append((c, f"SAVE{rng.randint(1000, 9999)}", "fixed" if fx else "percent",
                          rng.choice([10, 20, 30, 50]) if fx else rng.choice([0.8, 0.85, 0.9]),
                          rng.choice([99, 199, 299, 0]), _dt(rng, anchor, 0, -60)))
        conn.executemany("INSERT INTO coupons VALUES (?,?,?,?,?,?)", coups)

        conn.executemany("INSERT INTO users_password VALUES (?,?)",
                         [(i, f"$2b$12$fakehash{i:04d}") for i in range(1, N_USERS + 1)])
        conn.commit()
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in BUSINESS_TABLES + SENSITIVE_TABLES}
    finally:
        conn.close()
    return counts


if __name__ == "__main__":
    counts = seed()
    print("SEED_DONE table_count=%d" % len(counts))
    for t, n in counts.items():
        print("  %-16s %5d" % (t, n))
