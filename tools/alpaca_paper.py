#!/usr/bin/env python3
"""Alpaca PAPER trading from a cloud session (no Mac, no MCP server).

    python3 tools/alpaca_paper.py account
    python3 tools/alpaca_paper.py positions
    python3 tools/alpaca_paper.py orders
    python3 tools/alpaca_paper.py place --index 1      # only after ticket.py approve
    python3 tools/alpaca_paper.py close SYMBOL         # cancels its open orders, sells at market

Paper only: the host is hard-coded to paper-api.alpaca.markets. The keys never
touch this script or the chat: the session's network proxy adds the
APCA-API-KEY-ID and APCA-API-SECRET-KEY headers (saved as network secrets in
the environment settings). The placeholder values below are replaced there.

`place` sends only an order that passed all of these, checked again here:
trader-risk PASS today for this exact order, and the user's explicit yes
recorded by trader-broker's ticket.py within the last 15 minutes. Claude's
permission card for this command is the third gate (it is on "ask" in
.claude/settings.json). `close` is an exit: never blocked by a day limit, but
still needs the user's yes in the chat and the permission card.
"""
import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

HOST = "https://paper-api.alpaca.markets"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TICKET = os.path.join(ROOT, ".claude", "skills", "trader-broker", "ticket.py")
sys.path.insert(0, os.path.dirname(TICKET))
import ticket  # noqa: E402  (trader-broker's own fingerprint, state and risk logic)

APPROVAL_TTL_MIN = 15


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(HOST + path, data=data, method=method, headers={
        "APCA-API-KEY-ID": "placeholder-replaced-by-proxy",
        "APCA-API-SECRET-KEY": "placeholder-replaced-by-proxy",
        "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read().decode() or "null"
            return r.status, json.loads(raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"message": raw[:300]}


def die(msg, code=1):
    print(msg)
    sys.exit(code)


def check(status, out, what):
    if status in (401, 403):
        die("%s: HTTP %d %s. The paper keys are not reaching Alpaca: check the two network "
            "secrets for paper-api.alpaca.markets in the environment settings." % (what, status, out))
    if status >= 300:
        die("%s failed: HTTP %d %s" % (what, status, json.dumps(out)[:400]))
    return out


def rest_body(order):
    """trader-budget's place_stock_order fields -> Alpaca REST /v2/orders body."""
    t = ticket.tool_input(order)
    body = {k: t[k] for k in ("symbol", "side", "qty", "type", "time_in_force", "limit_price",
                              "stop_price", "extended_hours", "client_order_id") if k in t}
    if "take_profit_limit_price" in t or "stop_loss_stop_price" in t:
        body["order_class"] = "bracket"
        if "take_profit_limit_price" in t:
            body["take_profit"] = {"limit_price": t["take_profit_limit_price"]}
        sl = {}
        if "stop_loss_stop_price" in t:
            sl["stop_price"] = t["stop_loss_stop_price"]
        if "stop_loss_limit_price" in t:
            sl["limit_price"] = t["stop_loss_limit_price"]
        if sl:
            body["stop_loss"] = sl
    elif t.get("order_class"):
        body["order_class"] = t["order_class"]
    return body


def cmd_account(a):
    acc = check(*call("GET", "/v2/account"), "account")
    print("Paper account %s: status %s, equity $%s, cash $%s, buying power $%s, day trades %s"
          % (acc.get("account_number"), acc.get("status"), acc.get("equity"), acc.get("cash"),
             acc.get("buying_power"), acc.get("daytrade_count")))


def cmd_positions(a):
    pos = check(*call("GET", "/v2/positions"), "positions")
    if not pos:
        print("No open positions.")
    for p in pos:
        print("%s %s shares @ %s, now %s, P/L $%s" % (p["symbol"], p["qty"], p["avg_entry_price"],
                                                     p.get("current_price"), p.get("unrealized_pl")))


def cmd_orders(a):
    od = check(*call("GET", "/v2/orders?status=open&nested=true&limit=50"), "orders")
    if not od:
        print("No open orders.")
    for o in od:
        print("%s %s %s %s qty %s limit %s stop %s [%s] id %s" % (
            o["symbol"], o["side"], o["type"], o.get("order_class") or "", o["qty"],
            o.get("limit_price"), o.get("stop_price"), o["status"], o["id"]))
        for leg in o.get("legs") or []:
            print("    leg %s %s limit %s stop %s [%s]" % (leg["side"], leg["type"], leg.get("limit_price"),
                                                       leg.get("stop_price"), leg["status"]))


def cmd_place(a):
    os.chdir(a.workdir)
    cfg = ticket.read_json("trader/config.json", "config", required=False) or {}
    if not cfg.get("paper", True):
        die("trader/config.json says paper: false. This tool is paper only; it will not send.")
    orders = ticket.read_json("trader/orders.json", "orders")
    orders = orders.get("orders", orders) if isinstance(orders, dict) else orders
    if not 1 <= a.index <= len(orders):
        die("no order %d in trader/orders.json" % a.index)
    order = orders[a.index - 1]
    now = dt.datetime.now(dt.timezone.utc)
    today = ticket.to_eastern(now.replace(tzinfo=None)).strftime("%Y-%m-%d")
    fp = ticket.fingerprint(order)
    risk = ticket.read_json("trader/state/risk.json", "risk", required=False)
    rv, why = ticket.risk_verdict(risk, fp, today)
    if rv is None or rv.get("verdict") != "PASS":
        die("Not sent: no trader-risk PASS today for this exact order (%s)." % (why or rv.get("verdict")))
    state = ticket.load_state("trader/state/broker.json")
    rec = ticket.latest(state, fp, today)
    if not rec or rec.get("status") != "approved":
        die("Not sent: no yes recorded for order %d. Run ticket.py approve with the user's words first." % a.index)
    at = dt.datetime.fromisoformat(rec["at"])
    if at.tzinfo is None:
        at = ticket.eastern_to_utc(at).replace(tzinfo=dt.timezone.utc)
    age = (now - at).total_seconds() / 60
    if age > APPROVAL_TTL_MIN:
        die("Not sent: the yes is %.0f minutes old (limit %d). Show the ticket and ask again." % (age, APPROVAL_TTL_MIN))
    body = rest_body(order)
    print("Sending to Alpaca PAPER:\n" + json.dumps(body, indent=2))
    status, out = call("POST", "/v2/orders", body)
    if status >= 300:
        subprocess.run([sys.executable, TICKET, "result", "--index", str(a.index), "--status", "failed",
                        "--note", "HTTP %d %s" % (status, json.dumps(out)[:200])])
        check(status, out, "order")
    print("Placed: id %s, status %s" % (out.get("id"), out.get("status")))
    subprocess.run([sys.executable, TICKET, "result", "--index", str(a.index), "--status", "placed",
                    "--broker-id", str(out.get("id"))])


def cmd_close(a):
    sym = a.symbol.upper()
    od = check(*call("GET", "/v2/orders?status=open&symbols=%s" % sym), "orders")
    for o in od:
        s, _ = call("DELETE", "/v2/orders/%s" % o["id"])
        print("Canceled open order %s (%s %s): HTTP %d" % (o["id"], o["side"], o["type"], s))
    status, out = call("DELETE", "/v2/positions/%s" % sym)
    if status == 404:
        die("No open %s position." % sym, 0)
    check(status, out, "close %s" % sym)
    print("Closing %s: market sell order %s, qty %s, status %s" % (sym, out.get("id"), out.get("qty"), out.get("status")))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("account")
    sub.add_parser("positions")
    sub.add_parser("orders")
    p = sub.add_parser("place")
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--workdir", default=".", help="folder that holds trader/")
    p = sub.add_parser("close")
    p.add_argument("symbol")
    a = ap.parse_args()
    {"account": cmd_account, "positions": cmd_positions, "orders": cmd_orders,
     "place": cmd_place, "close": cmd_close}[a.cmd](a)


if __name__ == "__main__":
    main()
