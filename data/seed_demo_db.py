"""生成演示数据库（可重复执行）。

电商场景 4 张业务表 + 1 张敏感表（users_password，用于演示 blocked_tables 拦截）。
固定随机种子保证结构可复现；订单时间锚定运行日期回溯 180 天，
保证「最近 N 天」类时间范围查询任何时候运行都有数据。

用法：
    python data/seed_demo_db.py            # 生成 data/demo.db + data/schema.sql
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

DDL = """\
-- NL2SQL Agent 演示库 DDL（电商场景）
-- 本文件由 seed_demo_db.py 生成，用于 schema.source=ddl 模式

-- 用户表：注册用户基本信息
CREATE TABLE users (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,            -- 用户姓名
    email      TEXT NOT NULL UNIQUE,     -- 邮箱
    city       TEXT NOT NULL,            -- 所在城市
    created_at TEXT NOT NULL             -- 注册时间（ISO8601）
);

-- 商品表
CREATE TABLE products (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,            -- 商品名称
    category   TEXT NOT NULL,            -- 类目：电子产品/家居/图书/服饰/食品
    price      REAL NOT NULL,            -- 单价（元）
    created_at TEXT NOT NULL             -- 上架时间
);

-- 订单表
CREATE TABLE orders (
    id           INTEGER PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id),
    status       TEXT NOT NULL,          -- paid / pending / cancelled / refunded
    total_amount REAL NOT NULL,          -- 订单总金额（= 明细行金额之和）
    created_at   TEXT NOT NULL           -- 下单时间
);

-- 订单明细表
CREATE TABLE order_items (
    id         INTEGER PRIMARY KEY,
    order_id   INTEGER NOT NULL REFERENCES orders(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity   INTEGER NOT NULL,         -- 购买数量
    unit_price REAL NOT NULL             -- 成交单价（下单时快照）
);

-- 敏感表：用于演示 security.blocked_tables 拦截，业务查询不应触碰
CREATE TABLE users_password (
    user_id       INTEGER PRIMARY KEY REFERENCES users(id),
    password_hash TEXT NOT NULL
);
"""

SURNAMES = "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张"
GIVEN = ["伟", "芳", "娜", "敏", "静", "磊", "洋", "勇", "艳", "杰", "涛", "明", "超", "秀兰", "霞", "平", "刚", "桂英"]
CITIES = ["北京", "上海", "深圳", "杭州", "成都", "武汉", "西安", "南京", "广州", "苏州"]
ORDER_STATUSES = ["paid", "paid", "paid", "paid", "pending", "cancelled", "refunded"]  # 权重：多数已支付

PRODUCTS = [
    ("无线蓝牙耳机", "电子产品", 199.0), ("机械键盘", "电子产品", 349.0),
    ("27寸显示器", "电子产品", 1299.0), ("USB-C 扩展坞", "电子产品", 159.0),
    ("智能手环", "电子产品", 269.0), ("降噪头戴耳机", "电子产品", 899.0),
    ("北欧风台灯", "家居", 89.0), ("记忆棉枕头", "家居", 129.0),
    ("香薰加湿器", "家居", 149.0), ("懒人沙发", "家居", 499.0),
    ("四件套床品", "家居", 259.0),
    ("Python编程入门", "图书", 69.0), ("SQL必知必会", "图书", 49.0),
    ("深入理解计算机系统", "图书", 139.0), ("设计模式", "图书", 89.0),
    ("纯棉T恤", "服饰", 79.0), ("牛仔裤", "服饰", 199.0),
    ("运动卫衣", "服饰", 229.0), ("防晒外套", "服饰", 159.0),
    ("坚果礼盒", "食品", 128.0), ("挂耳咖啡", "食品", 68.0),
    ("有机燕麦片", "食品", 45.0), ("黑巧克力", "食品", 58.0),
    ("冻干水果脆", "食品", 36.0),
]

N_USERS = 50
N_ORDERS = 300


def _rand_dt(rng: random.Random, anchor: datetime, max_days_ago: int) -> str:
    dt = anchor - timedelta(
        days=rng.uniform(0, max_days_ago), hours=rng.uniform(0, 24)
    )
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def seed(db_path: Path = DB_PATH) -> dict[str, int]:
    rng = random.Random(RNG_SEED)
    anchor = datetime.now()

    SCHEMA_PATH.write_text(DDL, encoding="utf-8")

    db_path.unlink(missing_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(DDL)

        users = [
            (
                i,
                rng.choice(SURNAMES) + rng.choice(GIVEN),
                f"user{i:03d}@example.com",
                rng.choice(CITIES),
                _rand_dt(rng, anchor, DAYS_SPAN + 180),  # 注册时间可早于订单窗口
            )
            for i in range(1, N_USERS + 1)
        ]
        conn.executemany("INSERT INTO users VALUES (?,?,?,?,?)", users)

        products = [
            (i, name, cat, price, _rand_dt(rng, anchor, DAYS_SPAN + 90))
            for i, (name, cat, price) in enumerate(PRODUCTS, start=1)
        ]
        conn.executemany("INSERT INTO products VALUES (?,?,?,?,?)", products)

        item_id = 1
        for order_id in range(1, N_ORDERS + 1):
            items = []
            for product_id in rng.sample(range(1, len(PRODUCTS) + 1), rng.randint(1, 4)):
                qty = rng.randint(1, 3)
                price = PRODUCTS[product_id - 1][2]
                items.append((item_id, order_id, product_id, qty, price))
                item_id += 1
            total = round(sum(q * p for _, _, _, q, p in items), 2)
            conn.execute(
                "INSERT INTO orders VALUES (?,?,?,?,?)",
                (
                    order_id,
                    rng.randint(1, N_USERS),
                    rng.choice(ORDER_STATUSES),
                    total,
                    _rand_dt(rng, anchor, DAYS_SPAN),
                ),
            )
            conn.executemany("INSERT INTO order_items VALUES (?,?,?,?,?)", items)

        conn.executemany(
            "INSERT INTO users_password VALUES (?,?)",
            [(i, f"$2b$12$fakehash{i:04d}") for i in range(1, N_USERS + 1)],
        )
        conn.commit()

        counts = {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ["users", "products", "orders", "order_items", "users_password"]
        }
    finally:
        conn.close()
    return counts


if __name__ == "__main__":
    counts = seed()
    print(f"演示库已生成：{DB_PATH}")
    print(f"DDL 已生成：{SCHEMA_PATH}")
    for table, n in counts.items():
        print(f"  {table:<16} {n:>5} 行")
