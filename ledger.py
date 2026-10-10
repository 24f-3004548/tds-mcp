import os, json, threading, time
from datetime import datetime, timezone, timedelta, date
from collections import defaultdict
import httpx
from fastapi import APIRouter, Request

router = APIRouter()
IST = timezone(timedelta(hours=5, minutes=30))
BASE = os.environ.get("LEDGER_BASE", "")
KEY = os.environ.get("GEMINI_API_KEY", "")
MODELS = [m for m in [os.environ.get("GEMINI_MODEL"), "gemini-3.8-flash", "gemini-flash-latest"] if m]

_lock = threading.Lock()
DATA = {"rows": None, "rates": None, "meta": None, "t": 0}

def pdt(s):
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)

def load():
    with _lock:
        if DATA["rows"] is not None and time.time() - DATA["t"] < 900:
            return
        with httpx.Client(timeout=40) as c:
            rates = c.get(BASE + "&path=%2Frates").json()["usd_per_unit"]
            raw = c.get(BASE + "&path=%2Fexport").text.strip()
        recs = []
        if raw.startswith("["):
            recs = json.loads(raw)
        else:
            for ln in raw.splitlines():
                ln = ln.strip()
                if ln:
                    try: recs.append(json.loads(ln))
                    except Exception: pass
        latest = {}
        for r in recs:
            k = r["id"]
            if k not in latest or pdt(r["updated_at"]) >= pdt(latest[k]["updated_at"]):
                latest[k] = r
        rows = []
        for r in latest.values():
            r = dict(r)
            r["_d"] = pdt(r["created_at"]).astimezone(IST).date()
            r["_usd"] = float(r["amount"]) * rates[r["currency"]]
            r["_st"] = str(r["status"]).strip().lower()
            rows.append(r)
        DATA["rows"], DATA["rates"], DATA["t"] = rows, rates, time.time()
        DATA["meta"] = {
            "regions": sorted({r["region"] for r in rows}),
            "products": sorted({r["product"] for r in rows}),
            "statuses": sorted({r["status"] for r in rows}),
            "currencies": sorted(rates),
            "min_date": str(min(r["_d"] for r in rows)),
            "max_date": str(max(r["_d"] for r in rows)),
        }

SYSTEM = """You convert questions about an order ledger into a JSON query. Output ONLY JSON.
Dataset values (use these exact spellings): {meta}
Schema:
{{"metric": "revenue_usd|order_count|units|distinct_customers|distinct_products|avg_order_usd",
 "status": null or list of statuses (revenue questions -> ["paid"]; refund questions -> ["refunded"]; null = all statuses),
 "region": null|string, "product": null|string, "customer": null|string (e.g. "C1162"), "currency": null|string,
 "start": null|"YYYY-MM-DD" (inclusive), "end": null|"YYYY-MM-DD" (inclusive),
 "group_by": null|"product|region|customer|currency|month|status",
 "order": "desc|asc", "limit": integer (1 if asking for a single top/bottom item),
 "output": "value|key|list|pairs"}}
Rules: 'value' = a single number. 'key' = the name of the top group (when asked WHICH product/region/customer). 'list' = list of group names. 'pairs' = group->number map.
Refund amounts/counts: metric revenue_usd or order_count with status ["refunded"]. A month like March 2026 means start 2026-03-01, end 2026-03-31. Q1 = Jan-Mar etc.
Customers rank by revenue_usd unless stated. 'How many customers' = distinct_customers.
Question: {q}"""

def ask_llm(q):
    prompt = SYSTEM.format(meta=json.dumps(DATA["meta"]), q=q)
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}}
    err = None
    for m in MODELS:
        try:
            r = httpx.post(f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent",
                           params={"key": KEY}, json=body, timeout=10)
            j = r.json()
            if "candidates" not in j:
                print("GEMINI", m, str(j)[:300])
                raise RuntimeError(str(j)[:200])
            parts = j["candidates"][0]["content"]["parts"]
            t = "".join(p.get("text", "") for p in parts if not p.get("thought"))
            sp = json.loads(t)
            return sp[0] if isinstance(sp, list) else sp
        except Exception as e:
            err = e
    raise err

def eq(a, b): return str(a).strip().casefold() == str(b).strip().casefold()

def metric(rows, m):
    if m == "revenue_usd": return sum(r["_usd"] for r in rows)
    if m == "order_count": return len(rows)
    if m == "units": return sum(r["qty"] for r in rows)
    if m == "distinct_customers": return len({r["customer"] for r in rows})
    if m == "distinct_products": return len({r["product"] for r in rows})
    if m == "avg_order_usd": return sum(r["_usd"] for r in rows) / len(rows) if rows else 0
    raise ValueError(m)

def fmt(v):
    if isinstance(v, float):
        v = round(v + 1e-9, 2) if v >= 0 else round(v - 1e-9, 2)
        return int(v) if v == int(v) else v
    return v

def run(s):
    rows = DATA["rows"]
    if s.get("status"):
        st = {x.strip().lower() for x in s["status"]}
        rows = [r for r in rows if r["_st"] in st]
    for k in ("region", "product", "customer", "currency"):
        if s.get(k): rows = [r for r in rows if eq(r[k], s[k])]
    if s.get("start"):
        d0 = date.fromisoformat(s["start"]); rows = [r for r in rows if r["_d"] >= d0]
    if s.get("end"):
        d1 = date.fromisoformat(s["end"]); rows = [r for r in rows if r["_d"] <= d1]
    m, g = s.get("metric", "revenue_usd"), s.get("group_by")
    if not g:
        return fmt(metric(rows, m))
    groups = defaultdict(list)
    for r in rows:
        k = r["_d"].strftime("%Y-%m") if g == "month" else (r["status"] if g == "status" else r[g])
        groups[k].append(r)
    res = {k: metric(v, m) for k, v in groups.items()}
    rev = s.get("order", "desc") != "asc"
    items = sorted(res.items(), key=lambda kv: (-kv[1] if rev else kv[1], kv[0]))
    items = items[: int(s.get("limit") or 1)]
    out = s.get("output", "key")
    if out == "value": return fmt(items[0][1]) if items else 0
    if out == "key": return items[0][0] if items else None
    if out == "list": return [k for k, _ in items]
    return {k: fmt(v) for k, v in items}

@router.on_event("startup")
def _warm():
    threading.Thread(target=lambda: _safe(load), daemon=True).start()

def _safe(f):
    try: f()
    except Exception as e: print("load error", e)

@router.post("/ask")
async def ask(request: Request):
    body = await request.json()
    q = body.get("question", "")
    try:
        load()
        spec = ask_llm(q)
        print(q, "->", spec)
        return {"answer": run(spec)}
    except Exception as e:
        print("error", repr(e))
        return {"answer": None, "error": str(e)}

@router.get("/debug")
async def debug():
    load()
    return {"rows": len(DATA["rows"]), **DATA["meta"], "rates": DATA["rates"]}
