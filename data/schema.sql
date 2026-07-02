
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
