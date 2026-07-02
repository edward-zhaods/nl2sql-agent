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
