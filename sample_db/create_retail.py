"""
Creates sample_db/retail.db — a realistic e-commerce SQLite database.
Run once: python sample_db/create_retail.py
"""

import sqlite3
import os
import random
from datetime import datetime, timedelta

DB_PATH = os.path.join(os.path.dirname(__file__), "retail.db")

random.seed(42)

CATEGORIES  = ["Electronics", "Clothing", "Home & Kitchen", "Books", "Sports", "Beauty", "Toys"]
REGIONS     = ["North", "South", "East", "West", "Central"]
STORE_NAMES = [
    "Downtown", "Midtown", "Uptown", "Westside", "Eastgate",
    "Lakeside", "Hillcrest", "Riverside", "Northpark", "Southmall",
]

def random_date(start, end):
    delta = end - start
    return start + timedelta(days=random.randint(0, delta.days))

def create():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()

    # ── stores ──────────────────────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE stores (
            store_id   INTEGER PRIMARY KEY,
            store_name TEXT    NOT NULL,
            region     TEXT    NOT NULL,
            city       TEXT    NOT NULL,
            opened_on  DATE    NOT NULL
        )
    """)
    stores = []
    cities = ["New York", "Los Angeles", "Chicago", "Houston", "Phoenix",
              "Philadelphia", "San Antonio", "San Diego", "Dallas", "Austin"]
    for i, name in enumerate(STORE_NAMES, 1):
        stores.append((i, f"{name} Store", random.choice(REGIONS), cities[i-1],
                       random_date(datetime(2015,1,1), datetime(2021,12,31)).date()))
    cur.executemany("INSERT INTO stores VALUES (?,?,?,?,?)", stores)

    # ── products ─────────────────────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE products (
            product_id   INTEGER PRIMARY KEY,
            product_name TEXT    NOT NULL,
            category     TEXT    NOT NULL,
            cost_price   REAL    NOT NULL,
            sell_price   REAL    NOT NULL,
            supplier     TEXT    NOT NULL
        )
    """)
    product_templates = [
        ("Wireless Headphones Pro", "Electronics", 45, 129),
        ("Smart Watch Series 5",    "Electronics", 80, 249),
        ("USB-C Hub 7-in-1",        "Electronics", 15, 49),
        ("Running Shoes X3",        "Clothing", 30, 89),
        ("Yoga Mat Premium",        "Sports", 12, 39),
        ("Coffee Maker Deluxe",     "Home & Kitchen", 35, 99),
        ("Non-stick Pan Set",       "Home & Kitchen", 18, 55),
        ("Python Programming",      "Books", 8, 35),
        ("Data Science Handbook",   "Books", 10, 45),
        ("Face Moisturizer SPF50",  "Beauty", 7, 28),
        ("Building Blocks 200pcs",  "Toys", 14, 42),
        ("Laptop Stand Adjustable", "Electronics", 12, 45),
        ("Winter Jacket Thermal",   "Clothing", 40, 119),
        ("Protein Powder Vanilla",  "Sports", 25, 59),
        ("Blender Pro 1200W",       "Home & Kitchen", 28, 79),
    ]
    products = []
    suppliers = ["TechSource Ltd", "GlobalGoods Co", "PrimeParts Inc",
                 "ValueVenture", "EliteExport GmbH"]
    for i, (name, cat, cost, sell) in enumerate(product_templates, 1):
        products.append((i, name, cat, float(cost), float(sell), random.choice(suppliers)))
    cur.executemany("INSERT INTO products VALUES (?,?,?,?,?,?)", products)

    # ── customers ────────────────────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE customers (
            customer_id   INTEGER PRIMARY KEY,
            full_name     TEXT    NOT NULL,
            email         TEXT    NOT NULL UNIQUE,
            signup_date   DATE    NOT NULL,
            loyalty_tier  TEXT    NOT NULL
        )
    """)
    first_names = ["James","Maria","David","Sarah","Michael","Jennifer","Robert","Linda","William","Barbara"]
    last_names  = ["Smith","Johnson","Williams","Brown","Jones","Garcia","Miller","Davis","Wilson","Martinez"]
    tiers       = ["Bronze","Silver","Gold","Platinum"]
    customers   = []
    for i in range(1, 501):
        fn = random.choice(first_names)
        ln = random.choice(last_names)
        customers.append((
            i, f"{fn} {ln}",
            f"{fn.lower()}.{ln.lower()}{i}@example.com",
            random_date(datetime(2019,1,1), datetime(2023,6,30)).date(),
            random.choice(tiers),
        ))
    cur.executemany("INSERT INTO customers VALUES (?,?,?,?,?)", customers)

    # ── orders ───────────────────────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE orders (
            order_id    INTEGER PRIMARY KEY,
            customer_id INTEGER NOT NULL REFERENCES customers(customer_id),
            store_id    INTEGER NOT NULL REFERENCES stores(store_id),
            order_date  DATE    NOT NULL,
            status      TEXT    NOT NULL
        )
    """)
    statuses = ["completed","completed","completed","completed","returned","pending"]
    orders   = []
    for i in range(1, 3001):
        orders.append((
            i,
            random.randint(1, 500),
            random.randint(1, 10),
            random_date(datetime(2022,1,1), datetime(2024,3,31)).date(),
            random.choice(statuses),
        ))
    cur.executemany("INSERT INTO orders VALUES (?,?,?,?,?)", orders)

    # ── order_items ──────────────────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE order_items (
            item_id    INTEGER PRIMARY KEY,
            order_id   INTEGER NOT NULL REFERENCES orders(order_id),
            product_id INTEGER NOT NULL REFERENCES products(product_id),
            quantity   INTEGER NOT NULL,
            unit_price REAL    NOT NULL
        )
    """)
    items = []
    item_id = 1
    for order_id in range(1, 3001):
        n_items = random.randint(1, 4)
        chosen  = random.sample(range(1, 16), min(n_items, 15))
        for pid in chosen:
            price = products[pid-1][4] * random.uniform(0.9, 1.1)
            items.append((item_id, order_id, pid, random.randint(1, 3), round(price, 2)))
            item_id += 1
    cur.executemany("INSERT INTO order_items VALUES (?,?,?,?,?)", items)

    conn.commit()
    conn.close()
    print(f"✅  retail.db created at {DB_PATH}")
    print(f"    stores={len(stores)}  products={len(products)}  customers={len(customers)}")
    print(f"    orders={len(orders)}  order_items={len(items)}")

if __name__ == "__main__":
    create()
