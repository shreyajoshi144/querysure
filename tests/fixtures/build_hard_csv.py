"""CSV stand-in exercising the hard cases: reserved-word headers, long near-identical categorical values (>40 chars),
more than 40 distinct values, apostrophes / double spaces / case variants, NULLs, ISO dates."""
import csv
import random
from datetime import date, timedelta

LONG_A = "Enterprise Software Licensing and Maintenance Agreement - Premium Tier (Annual)"
LONG_B = "Enterprise Software Licensing and Maintenance Agreement - Premium Tier (Monthly)"
TRICKY = ["O'Brien & Sons Ltd", "Smith  and  Co", "smith and co", "Zoë Café"]
FILLER = [f"Standard Service Package {i:02d} with extended coverage" for i in range(40)]
PLANS = [LONG_A, LONG_B] + FILLER
CUSTOMERS = TRICKY + [f"Customer {i}" for i in range(10)]


def make_rows(n=240, seed=11):
    rnd = random.Random(seed)
    rows = []
    for i in range(n):
        d = date(2023, 1, 1) + timedelta(days=rnd.randint(0, 364))
        rows.append({
            "order": i + 1,
            "group": rnd.choice(["g1", "g2", "g3"]),
            "plan_name": PLANS[i % len(PLANS)],
            "customer": CUSTOMERS[i % len(CUSTOMERS)],
            "amount": None if i % 9 == 0 else round(rnd.uniform(10, 900), 2),
            "units": rnd.randint(1, 20),
            "status": rnd.choice(["Active", "Cancelled", "Pending"]),
            "start_date": d,
        })
    rows[0]["start_date"] = date(2023, 3, 31)   # boundary rows so inclusive/exclusive range ends differ
    rows[1]["start_date"] = date(2023, 3, 1)
    return rows


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Order", "Group", "Plan Name", "Customer", "Amount", "Units", "Status", "Start Date"])
        for r in rows:
            w.writerow([r["order"], r["group"], r["plan_name"], r["customer"], "" if r["amount"] is None else r["amount"],
                        r["units"], r["status"], r["start_date"].isoformat()])
