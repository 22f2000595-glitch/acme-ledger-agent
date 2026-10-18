import os, re, json, logging
from datetime import datetime
from zoneinfo import ZoneInfo
from decimal import Decimal, ROUND_HALF_UP
from functools import lru_cache
import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
app = FastAPI(title="Acme Appliances Ledger Agent", version="1.0.0")
EXPORT_URL = os.getenv("LEDGER_EXPORT_URL", "").strip()
RATES = {"USD": Decimal("1"), "EUR": Decimal("1.06"), "INR": Decimal("0.01223")}
TZ = ZoneInfo("Asia/Kolkata")
MONTHS = {"january":1,"jan":1,"february":2,"feb":2,"march":3,"mar":3,"april":4,"apr":4,"may":5,"june":6,"jun":6,"july":7,"jul":7,"august":8,"aug":8,"september":9,"sep":9,"sept":9,"october":10,"oct":10,"november":11,"nov":11,"december":12,"dec":12}
REGIONS = ["North", "South", "East", "West", "Central"]
PRODUCTS = ["Air Fryer", "Rice Cooker", "Mixer", "Blender", "Grinder", "Kettle", "Toaster", "Juicer"]

class Question(BaseModel):
    question: str = Field(min_length=1, max_length=2000)

def parse_dt(value):
    if not value: return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None: dt = dt.replace(tzinfo=TZ)
        return dt.astimezone(TZ)
    except (ValueError, TypeError): return None

@lru_cache(maxsize=1)
def load_orders():
    if not EXPORT_URL: raise RuntimeError("LEDGER_EXPORT_URL is not configured")
    response = requests.get(EXPORT_URL, timeout=(3, 10)); response.raise_for_status()
    records = []
    for line in response.text.splitlines():
        if line.strip():
            row = json.loads(line)
            if isinstance(row, dict) and row.get("id"): records.append(row)
    latest = {}
    for row in records:
        oid = str(row["id"])
        new_dt = parse_dt(row.get("updated_at")) or datetime.min.replace(tzinfo=TZ)
        old = latest.get(oid)
        old_dt = (parse_dt(old.get("updated_at")) if old else None) or datetime.min.replace(tzinfo=TZ)
        if old is None or new_dt >= old_dt: latest[oid] = row
    logging.info("Loaded %d rows; %d unique orders", len(records), len(latest))
    return list(latest.values())

def usd(row):
    rate = RATES.get(str(row.get("currency", "USD")).upper())
    if rate is None: return Decimal("0")
    return Decimal(str(row.get("amount", 0) or 0)) * rate

def parse_filters(question):
    low = question.lower(); f = {}
    for region in REGIONS:
        if re.search(r"\b" + re.escape(region.lower()) + r"\b", low): f["region"] = region; break
    for product in sorted(PRODUCTS, key=len, reverse=True):
        if product.lower() in low: f["product"] = product; break
    m = re.search(r"\b(?:customer\s*)?(C\d{3,5})\b", question, re.I)
    if m: f["customer"] = m.group(1).upper()
    m = re.search(r"\b(ORD-\d+)\b", question, re.I)
    if m: f["id"] = m.group(1).upper()
    for status in ["refunded", "refund", "void", "paid"]:
        if re.search(r"\b" + status + r"\b", low): f["status"] = "refunded" if status in ("refund", "refunded") else status; break
    for name, month in MONTHS.items():
        if re.search(r"\b" + name + r"\b", low):
            f["month"] = month
            year = re.search(r"\b(20\d{2})\b", low)
            if year: f["year"] = int(year.group(1))
            break
    if "year" not in f:
        year = re.search(r"\b(20\d{2})\b", low)
        if year: f["year"] = int(year.group(1))
    dates = re.findall(r"\b(20\d{2}-\d{2}-\d{2})\b", low)
    if dates: f["start_date"], f["end_date"] = min(dates), max(dates)
    return f

def filter_rows(rows, f):
    out=[]
    for r in rows:
        if f.get("region") and r.get("region", "").lower() != f["region"].lower(): continue
        if f.get("product") and r.get("product", "").lower() != f["product"].lower(): continue
        if f.get("customer") and str(r.get("customer", "")).upper() != f["customer"]: continue
        if f.get("id") and str(r.get("id", "")).upper() != f["id"]: continue
        if f.get("status") and str(r.get("status", "")).lower() != f["status"]: continue
        dt = parse_dt(r.get("created_at"))
        if f.get("year") or f.get("month"):
            if not dt or (f.get("year") and dt.year != f["year"]) or (f.get("month") and dt.month != f["month"]): continue
        if f.get("start_date") and (not dt or dt.date().isoformat() < f["start_date"]): continue
        if f.get("end_date") and (not dt or dt.date().isoformat() > f["end_date"]): continue
        out.append(r)
    return out

def numeric(v):
    v = Decimal(v).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(v) if v == v.to_integral_value() else float(v)


def answer_question(question, rows):
    q = question.lower().strip()
    f = parse_filters(question)
    matching = filter_rows(rows, f)

    refund = any(x in q for x in ["refund", "refunded", "money returned"])
    void = any(x in q for x in ["void", "cancelled", "canceled"])
    revenue_query = any(x in q for x in [
        "revenue", "sales", "sold", "earn", "earned", "earning",
        "income", "turnover", "net", "by revenue"
    ])

    if refund:
        matching = [r for r in matching if str(r.get("status", "")).lower() == "refunded"]
    elif void:
        matching = [r for r in matching if str(r.get("status", "")).lower() == "void"]
    elif revenue_query:
        matching = [r for r in matching if str(r.get("status", "")).lower() == "paid"]

    count = any(x in q for x in [
        "how many", "number of", "count of", "count the", "number?"
    ])
    qty = any(x in q for x in [
        "units", "items", "pieces", "quantity", "how many products"
    ])
    average = any(x in q for x in ["average", "avg", "mean"])
    top = any(x in q for x in [
        "most ", "highest", "top ", "top-selling", "top selling",
        "best-selling", "best selling", "bestselling", "largest",
        "which product", "which customer"
    ])
    total = any(x in q for x in [
        "total", "sum", "revenue", "sales", "refund", "refunded",
        "amount", "value", "earned", "spent", "net"
    ])

    if re.search(r"\b(list|show|name)\b", q) and ("product" in q or "customer" in q):
        field = "product" if "product" in q else "customer"
        return sorted({str(r.get(field)) for r in matching if r.get(field)})

    if top:
        key = "customer" if ("customer" in q or "client" in q) else (
            "region" if "region" in q else "product"
        )
        sums = {}
        for r in matching:
            name = str(r.get(key, ""))
            if not name:
                continue
            if qty:
                val = Decimal(str(r.get("qty", 0) or 0))
            elif any(x in q for x in ["orders", "count", "number"]):
                val = Decimal("1")
            else:
                val = usd(r)
            sums[name] = sums.get(name, Decimal("0")) + val

        if not sums:
            return 0
        best = max(sums.values())
        winners = sorted(k for k, v in sums.items() if v == best)
        return winners[0] if len(winners) == 1 else winners

    if count and not total:
        return len(matching)

    if qty and not total:
        return sum(int(r.get("qty", 0) or 0) for r in matching)

    if average:
        return numeric(
            sum((usd(r) for r in matching), Decimal("0")) / Decimal(len(matching))
        ) if matching else 0

    if total or not count:
        if qty and not any(x in q for x in [
            "revenue", "sales", "amount", "value", "usd", "money"
        ]):
            return sum(int(r.get("qty", 0) or 0) for r in matching)
        return numeric(sum((usd(r) for r in matching), Decimal("0")))

    return len(matching)

@app.get("/")
def root(): return {"service":"Acme Appliances order ledger", "status":"ok", "endpoint":"POST /"}

@app.get("/health")
def health(): return {"status":"ok"}

@app.post("/")
@app.post("/ask")
def ask(body: Question):
    try: return {"answer": answer_question(body.question, load_orders())}
    except requests.RequestException as e:
        logging.exception("Ledger export fetch failed"); raise HTTPException(status_code=502, detail="Could not load ledger data") from e
    except RuntimeError as e: raise HTTPException(status_code=503, detail=str(e)) from e
    except (ValueError, json.JSONDecodeError) as e:
        logging.exception("Ledger export parse failed"); raise HTTPException(status_code=502, detail="Ledger export was not valid NDJSON") from e
