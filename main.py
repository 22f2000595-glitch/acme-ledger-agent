import json
import logging
import os
import re
import sqlite3
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from functools import lru_cache
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
app = FastAPI(title="Acme Appliances Ledger Agent", version="2.0.0")

EXPORT_URL = os.getenv("LEDGER_EXPORT_URL", "").strip()
TZ = ZoneInfo("Asia/Kolkata")
REQUEST_HEADERS = {"User-Agent": "Acme-Ledger-Agent/2.0"}

MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2,
    "march": 3, "mar": 3, "april": 4, "apr": 4,
    "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}
REGIONS = ["North", "South", "East", "West", "Central"]
PRODUCTS = [
    "Air Fryer", "Rice Cooker", "Juicer", "Toaster",
    "Mixer", "Kettle", "Blender", "Grinder",
]

class Question(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


def make_rates_url(export_url: str) -> str:
    """Build the matching /rates endpoint while preserving assignment credentials."""
    parts = urlsplit(export_url)

    # Assignment URLs often encode the endpoint as ?path=/export on questionData.
    query_items = parse_qsl(parts.query, keep_blank_values=True)
    has_path_param = any(key == "path" for key, _ in query_items)

    if has_path_param:
        query_items = [
            (key, "/rates" if key == "path" else value)
            for key, value in query_items
        ]
        return urlunsplit((
            parts.scheme, parts.netloc, parts.path,
            urlencode(query_items), parts.fragment
        ))

    # Also support URLs where /export is the actual URL path.
    path = parts.path.rstrip("/")
    if path.endswith("/export"):
        path = path[:-len("/export")] + "/rates"
    else:
        raise ValueError(
            "Cannot derive /rates URL: LEDGER_EXPORT_URL must end in /export "
            "or contain the query parameter path=/export"
        )
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def parse_dt(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        return dt.astimezone(TZ)
    except (ValueError, TypeError, AttributeError):
        return None


def money(value):
    amount = Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(amount) if amount == amount.to_integral_value() else float(amount)


def normalize_status(value):
    return str(value or "").strip().lower()


@lru_cache(maxsize=1)
def load_database():
    if not EXPORT_URL:
        raise RuntimeError("LEDGER_EXPORT_URL is not configured in Render")

    timeout = (5, 30)
    rates_url = make_rates_url(EXPORT_URL)
    logging.info("Fetching currency rates")
    rates_response = requests.get(rates_url, headers=REQUEST_HEADERS, timeout=timeout)
    rates_response.raise_for_status()
    rates_payload = rates_response.json()
    rates = rates_payload.get("usd_per_unit")
    if not isinstance(rates, dict) or not rates:
        raise ValueError("The /rates response does not contain usd_per_unit")

    logging.info("Fetching order ledger")
    export_response = requests.get(EXPORT_URL, headers=REQUEST_HEADERS, timeout=timeout)
    export_response.raise_for_status()
    records = []
    for line in export_response.text.splitlines():
        if line.strip():
            row = json.loads(line)
            if isinstance(row, dict) and row.get("id") is not None:
                records.append(row)

    # Latest updated version of each order wins.
    latest = {}
    min_dt = datetime.min.replace(tzinfo=TZ)
    for row in records:
        order_id = str(row["id"])
        updated = parse_dt(row.get("updated_at")) or min_dt
        prior = latest.get(order_id)
        prior_updated = (parse_dt(prior.get("updated_at")) if prior else None) or min_dt
        if prior is None or updated >= prior_updated:
            latest[order_id] = row

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE orders (
            id TEXT PRIMARY KEY,
            customer TEXT,
            product TEXT,
            region TEXT,
            status TEXT,
            currency TEXT,
            amount REAL,
            amount_usd REAL,
            qty REAL,
            created_at TEXT,
            created_date TEXT,
            created_month TEXT,
            created_year INTEGER,
            updated_at TEXT
        )
    """)

    insert_sql = """INSERT OR REPLACE INTO orders
        (id, customer, product, region, status, currency, amount, amount_usd,
         qty, created_at, created_date, created_month, created_year, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
    inserted = 0
    for row in latest.values():
        created = parse_dt(row.get("created_at"))
        currency = str(row.get("currency", "USD")).upper()
        try:
            amount = Decimal(str(row.get("amount", 0) or 0))
            rate = Decimal(str(rates.get(currency, 0) or 0))
            amount_usd = amount * rate
        except Exception:
            amount_usd = Decimal("0")
        try:
            qty = float(row.get("qty", 0) or 0)
        except (ValueError, TypeError):
            qty = 0
        conn.execute(insert_sql, (
            str(row.get("id", "")),
            str(row.get("customer", "") or ""),
            str(row.get("product", "") or ""),
            str(row.get("region", "") or ""),
            normalize_status(row.get("status")),
            currency,
            float(row.get("amount", 0) or 0),
            float(amount_usd),
            qty,
            str(row.get("created_at", "") or ""),
            created.date().isoformat() if created else None,
            created.strftime("%Y-%m") if created else None,
            created.year if created else None,
            str(row.get("updated_at", "") or ""),
        ))
        inserted += 1
    conn.commit()
    logging.info("Loaded %d raw records; %d unique orders", len(records), inserted)
    return conn


def build_where(question):
    q = question.lower()
    clauses = []
    params = []

    # The ledger questions normally ask about paid sales unless a status is named.
    if re.search(r"\b(refund|refunded|refunds|money returned)\b", q):
        clauses.append("status = ?")
        params.append("refunded")
    elif re.search(r"\b(void|voided|cancel|cancelled|canceled)\b", q):
        clauses.append("status = ?")
        params.append("void")
    elif re.search(r"\b(paid|revenue|sales|sold|earn|earned|earning|income|turnover|net|top-selling|top selling|best-selling|best selling)\b", q):
        clauses.append("status = ?")
        params.append("paid")
    else:
        # Default to paid orders for ordinary ledger questions as in the assignment's reference approach.
        clauses.append("status = ?")
        params.append("paid")

    for region in REGIONS:
        if re.search(r"\b" + re.escape(region.lower()) + r"\b", q):
            clauses.append("region = ?")
            params.append(region)
            break

    for product in PRODUCTS:
        if re.search(r"\b" + re.escape(product.lower()) + r"s?\b", q):
            clauses.append("product = ?")
            params.append(product)
            break

    customer_match = re.search(r"\b(c\d{4,5})\b", q, re.I)
    if customer_match:
        clauses.append("UPPER(customer) = ?")
        params.append(customer_match.group(1).upper())

    order_match = re.search(r"\b(ord-\d+)\b", q, re.I)
    if order_match:
        clauses.append("UPPER(id) = ?")
        params.append(order_match.group(1).upper())

    year_match = re.search(r"\b(20\d{2})\b", q)
    year = int(year_match.group(1)) if year_match else None
    month = None
    for name, number in MONTHS.items():
        if re.search(r"\b" + re.escape(name) + r"\b", q):
            month = number
            break
    if month is not None:
        if year is None:
            year = 2026
        clauses.append("created_month = ?")
        params.append(f"{year:04d}-{month:02d}")
    elif year is not None and not re.search(r"\b(last|past)\s+\d+\s+days?\b", q):
        clauses.append("created_year = ?")
        params.append(year)

    dates = sorted(set(re.findall(r"\b(20\d{2}-\d{2}-\d{2})\b", q)))
    if len(dates) == 1:
        clauses.append("created_date = ?")
        params.append(dates[0])
    elif len(dates) >= 2:
        clauses.append("created_date >= ? AND created_date <= ?")
        params.extend([dates[0], dates[-1]])

    return " AND ".join(clauses) if clauses else "1=1", params


def answer_question(question, conn):
    q = question.lower().strip()
    where, params = build_where(question)

    # Count distinct customers before generic count/order logic.
    if any(phrase in q for phrase in [
        "different customers", "different customer", "distinct customers",
        "distinct customer", "unique customers", "unique customer",
    ]):
        sql = f"SELECT COUNT(DISTINCT customer) FROM orders WHERE {where} AND TRIM(COALESCE(customer, '')) <> ''"
        return int(conn.execute(sql, params).fetchone()[0] or 0)

    if any(phrase in q for phrase in [
        "different products", "distinct products", "unique products",
    ]):
        sql = f"SELECT COUNT(DISTINCT product) FROM orders WHERE {where} AND TRIM(COALESCE(product, '')) <> ''"
        return int(conn.execute(sql, params).fetchone()[0] or 0)

    # Rank products/customers/regions using the requested measure.
    is_top = any(phrase in q for phrase in [
        "top", "highest", "most", "best-selling", "best selling",
        "top-selling", "top selling", "largest", "earned the most",
        "spent the most",
    ])
    if is_top:
        if "customer" in q or "client" in q:
            group_col = "customer"
        elif "region" in q:
            group_col = "region"
        else:
            group_col = "product"
        if any(word in q for word in ["unit", "units", "quantity", "qty", "pieces", "items"]):
            metric = "SUM(qty)"
        elif "order" in q or "orders" in q:
            metric = "COUNT(*)"
        else:
            metric = "SUM(amount_usd)"
        sql = f"SELECT {group_col} FROM orders WHERE {where} AND TRIM(COALESCE({group_col}, '')) <> '' GROUP BY {group_col} ORDER BY {metric} DESC, {group_col} ASC LIMIT 1"
        value = conn.execute(sql, params).fetchone()
        return value[0] if value else 0

    if re.search(r"\b(list|show|name)\b", q) and ("product" in q or "customer" in q):
        column = "product" if "product" in q else "customer"
        sql = f"SELECT DISTINCT {column} FROM orders WHERE {where} AND TRIM(COALESCE({column}, '')) <> '' ORDER BY {column}"
        return [row[0] for row in conn.execute(sql, params).fetchall()]

    if "average" in q or re.search(r"\b(avg|mean)\b", q):
        sql = f"SELECT ROUND(COALESCE(AVG(amount_usd), 0), 2) FROM orders WHERE {where}"
        return money(conn.execute(sql, params).fetchone()[0] or 0)

    if any(phrase in q for phrase in ["how many orders", "number of orders", "count of orders", "count the orders"]):
        sql = f"SELECT COUNT(*) FROM orders WHERE {where}"
        return int(conn.execute(sql, params).fetchone()[0] or 0)

    if any(phrase in q for phrase in ["how many units", "total quantity", "total units", "number of units", "how many items", "how many pieces"]):
        sql = f"SELECT COALESCE(SUM(qty), 0) FROM orders WHERE {where}"
        return int(conn.execute(sql, params).fetchone()[0] or 0)

    # For revenue / amount questions, use USD-normalized amounts.
    sql = f"SELECT ROUND(COALESCE(SUM(amount_usd), 0), 2) FROM orders WHERE {where}"
    return money(conn.execute(sql, params).fetchone()[0] or 0)


@app.get("/")
def root():
    return {
        "service": "Acme Appliances order ledger",
        "status": "ok",
        "endpoint": "POST /",
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/")
@app.post("/ask")
def ask(body: Question):
    try:
        conn = load_database()
        return {"answer": answer_question(body.question, conn)}
    except requests.RequestException as exc:
        logging.exception("Ledger source request failed")
        raise HTTPException(status_code=502, detail="Could not load ledger data") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (ValueError, json.JSONDecodeError, sqlite3.Error) as exc:
        logging.exception("Ledger processing failed")
        raise HTTPException(status_code=502, detail="Ledger data could not be processed") from exc
