#!/usr/bin/env python3
"""Enrich the Atlas `acme` demo tenant with finance/sales edge cases — via the Atlas HTTP API only.

Adds (each step is independently idempotent, keyed on an 'enrich-demo' marker in a
description/notes/reason field, so re-running skips anything already created):
  1. a customer receipt fully paying one posted invoice + one partially paying another
  2. a customer credit note (via a posted sales return against an invoiced sales order)
  3. a reversal of a manual JOURNAL entry (original -> REVERSED)
  4. one DRAFT sales order + one CANCELLED sales order
  5. a vendor payment fully paying one vendor bill
  6. a new customer invoice dated 2026-07 (due 2026-08), left unpaid -> overdue as of 2026-09-27

Stdlib only. Usage:  python3 infra/enrich_demo_data.py   (ATLAS_API overrides the base URL)
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from decimal import Decimal

BASE = os.environ.get("ATLAS_API", "http://localhost:18000/api/v1")
SLUG = "acme"
PASSWORD = "correct-horse-battery"
MARK = "enrich-demo"
TOKEN = None


def _req(method, path, body=None, params=None):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    if method in ("POST", "PATCH", "PUT"):
        headers["Idempotency-Key"] = uuid.uuid4().hex
    data = json.dumps(body if body is not None else {}).encode() if method != "GET" else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            txt = r.read().decode()
            return json.loads(txt) if txt else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {path} -> {e.code}: {e.read().decode()[:600]}") from None


def get(path, **params):
    return _req("GET", path, params=params)


def post(path, body=None):
    return _req("POST", path, body)


def items(path, **params):
    """All rows of a cursor-paginated list endpoint."""
    out, cursor = [], None
    while True:
        p = dict(params, limit=200)
        if cursor:
            p["cursor"] = cursor
        data = get(path, **p)
        if isinstance(data, list):
            return data
        out.extend(data.get("items", []))
        cursor = data.get("next_cursor")
        if not cursor:
            return out


def marked(rows, *fields):
    return [r for r in rows if any(MARK in (r.get(f) or "") for f in fields)]


def login():
    global TOKEN
    r = post("/auth/login", {"tenant_slug": SLUG, "email": f"owner@{SLUG}.test",
                             "password": PASSWORD})
    TOKEN = r["access_token"]


def account_id(code):
    for a in items("/finance/accounts"):
        if a.get("code") == code:
            return a["id"]
    raise RuntimeError(f"account {code} not found")


# ── 1. customer receipts ────────────────────────────────────────────────────
def step_receipts(cash_id):
    if marked(items("/finance/customer-receipts"), "description"):
        print("1. receipts: already present — skip")
        return
    invs = [i for i in items("/finance/customer-invoices")
            if i["status"] == "POSTED" and Decimal(i["open_amount"]) > 0
            and (i.get("invoice_number") or "").startswith("INV-")]
    invs.sort(key=lambda i: i["invoice_date"])  # oldest first
    full, part = invs[0], next(i for i in invs[1:] if i["partner_id"] != invs[0]["partner_id"])
    for inv, amt, tag in ((full, Decimal(full["open_amount"]), "full"),
                          (part, (Decimal(part["open_amount"]) * Decimal("0.4")).quantize(
                              Decimal("0.01")), "partial")):
        rc = post("/finance/customer-receipts", {
            "partner_id": inv["partner_id"], "partner_name": inv["partner_name"],
            "receipt_date": "2026-07-15", "currency_code": inv["currency_code"],
            "bank_account_id": cash_id, "amount": str(amt),
            "description": f"{MARK}: {tag} payment of {inv['invoice_number']}",
            "allocations": [{"invoice_id": inv["id"], "amount": str(amt)}],
        })
        print(f"1. receipt {rc.get('receipt_number')} {tag}: {amt} -> {inv['invoice_number']}")


# ── 2. credit note via sales return ─────────────────────────────────────────
def step_credit_note():
    if marked(items("/sales/returns"), "reason", "notes"):
        print("2. credit note (sales return): already present — skip")
        return
    orders = [o for o in items("/sales/orders") if o["status"] == "CLOSED"]
    orders.sort(key=lambda o: o.get("order_date") or "")
    order = get(f"/sales/orders/{orders[-1]['id']}")  # newest fully billed order
    line = order["lines"][0]
    wh = next(w for w in items("/inventory/warehouses") if w["code"] == "WH1")
    bin_ = next(b for b in items("/inventory/bins") if b["warehouse_id"] == wh["id"])
    rt = post("/sales/returns", {
        "sales_order_id": order["id"], "warehouse_id": wh["id"], "return_date": "2026-07-20",
        "reason": f"{MARK}: damaged units", "notes": f"{MARK} credit note",
        "lines": [{"sales_order_line_id": line["id"], "bin_id": bin_["id"], "quantity": "3"}],
    })
    rt = post(f"/sales/returns/{rt['id']}/post")
    print(f"2. return {rt.get('return_number') or rt['id']} posted on order "
          f"{order.get('order_number')} (3 units) -> status {rt.get('status')}")


# ── 3. reverse a manual journal ─────────────────────────────────────────────
def step_reversal():
    jes = items("/finance/journal-entries")
    if any(j["status"] == "REVERSED" for j in jes) or marked(jes, "description"):
        print("3. reversal: already present — skip")
        return
    cands = [j for j in jes if j.get("document_type") == "JOURNAL" and j["status"] == "POSTED"
             and "Rent" in (j.get("description") or "")]
    cands.sort(key=lambda j: j["posting_date"])
    orig = cands[-1]
    rev = post(f"/finance/journal-entries/{orig['id']}/reverse", {
        "reversal_date": "2026-07-31",
        "description": f"{MARK}: reversal of {orig.get('entry_number')} (booked in error)",
    })
    print(f"3. reversed {orig.get('entry_number')} ({orig.get('description')}) "
          f"with {rev.get('entry_number')}")


# ── 4. draft + cancelled sales orders ───────────────────────────────────────
def step_orders():
    existing = marked(items("/sales/orders"), "notes")
    if existing:
        print("4. sales orders: already present — skip")
        return
    custs = {c["customer_code"] if "customer_code" in c else c.get("code"): c
             for c in items("/sales/customers")}
    item = next(i for i in items("/inventory/items") if i["item_code"] == "ITM-GADGET")
    uom = next(u for u in items("/inventory/uoms") if u["code"] == "EA")

    def mk(cust_code, qty, note):
        return post("/sales/orders", {
            "customer_id": custs[cust_code]["id"], "currency_code": "USD",
            "order_date": "2026-09-20", "requested_date": "2026-10-05", "notes": note,
            "lines": [{"item_id": item["id"], "quantity": str(qty), "uom_id": uom["id"],
                       "unit_price": "40.00"}],
        })

    d = mk("C-CONTOSO", 12, f"{MARK}: draft order awaiting customer PO")
    c = mk("C-FABRIKAM", 8, f"{MARK}: cancelled — customer withdrew")
    c = post(f"/sales/orders/{c['id']}/cancel")
    print(f"4. draft order {d.get('order_number')} ({d['status']}), "
          f"cancelled order {c.get('order_number')} ({c['status']})")


# ── 5. vendor payment ───────────────────────────────────────────────────────
def step_vendor_payment(cash_id):
    if marked(items("/finance/vendor-payments"), "description"):
        print("5. vendor payment: already present — skip")
        return
    bills = [b for b in items("/finance/vendor-bills")
             if b["status"] == "POSTED" and Decimal(b["open_amount"]) > 0]
    bills.sort(key=lambda b: b["bill_date"])
    b = bills[0]
    amt = Decimal(b["open_amount"])
    vp = post("/finance/vendor-payments", {
        "partner_id": b["partner_id"], "partner_name": b["partner_name"],
        "payment_date": "2026-07-10", "currency_code": b["currency_code"],
        "bank_account_id": cash_id, "amount": str(amt),
        "description": f"{MARK}: full payment of {b.get('bill_number')}",
        "allocations": [{"bill_id": b["id"], "amount": str(amt)}],
    })
    print(f"5. vendor payment {vp.get('payment_number')}: {amt} -> {b.get('bill_number')}")


# ── 6. overdue unpaid invoice ───────────────────────────────────────────────
def step_overdue_invoice(ar_id, rev_id):
    if marked(items("/finance/customer-invoices"), "description"):
        print("6. overdue invoice: already present — skip")
        return
    cust = next(c for c in items("/sales/customers")
                if (c.get("customer_code") or c.get("code")) == "C-NORTH")
    inv = post("/finance/customer-invoices", {
        "partner_id": cust["id"], "partner_name": cust["name"],
        "invoice_date": "2026-07-28", "due_date": "2026-08-27", "currency_code": "USD",
        "ar_account_id": ar_id, "external_ref": "PO-NW-7781",
        "description": f"{MARK}: installation services July 2026 (unpaid)",
        "lines": [{"account_id": rev_id, "description": "On-site installation, 14h",
                   "net_amount": "1260.00"}],
    })
    inv = post(f"/finance/customer-invoices/{inv['id']}/post")
    print(f"6. invoice {inv.get('invoice_number')} {inv['gross_amount']} due "
          f"{inv['due_date']} status {inv['status']}")


def main():
    login()
    cash_id, ar_id, rev_id = account_id("1000"), account_id("1100"), account_id("4000")
    steps = [lambda: step_receipts(cash_id), step_credit_note, step_reversal, step_orders,
             lambda: step_vendor_payment(cash_id), lambda: step_overdue_invoice(ar_id, rev_id)]
    failed = 0
    for s in steps:
        try:
            s()
        except Exception as e:  # keep going; report at end
            failed += 1
            print(f"   step failed: {e}", file=sys.stderr)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
