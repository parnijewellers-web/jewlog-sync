"""
JwelLog -> WooCommerce sync (v3).

Every run:
  - existing WooCommerce products (matched by SKU): update stock if it changed
  - UPDATE_PRICES=true: also update price if it changed (slower, 1 extra call per product)
  - SYNC_MODE=create: products missing from WooCommerce are created LIVE (NEW_STATUS=publish)
    unless they have no price or are inactive in JwelLog; those stay draft until a price appears
DRY_RUN=true (default) only prints what it would do.

Env vars: JEWLOG_USER, JEWLOG_PASS, WC_KEY, WC_SECRET
Optional: SYNC_MODE (stock|create), DRY_RUN (true|false), UPDATE_PRICES (true|false),
          PRICE_FIELD (customer_mrp|mrp), JEWLOG_BASE, WP_SITE
"""
import os
import time
import requests

JEWLOG_BASE = os.getenv("JEWLOG_BASE", "https://jwellog.com")
WP_SITE = os.getenv("WP_SITE", "https://parnijewellers.com")
PRICE_FIELD = os.getenv("PRICE_FIELD", "customer_mrp")
MODE = os.getenv("SYNC_MODE", "stock")
DRY_RUN = os.getenv("DRY_RUN", "true").lower() != "false"
UPDATE_PRICES = os.getenv("UPDATE_PRICES", "false").lower() == "true"
NEW_STATUS = os.getenv("NEW_STATUS", "publish")      # "publish" = live, "draft" = hidden

WC_API = f"{WP_SITE}/wp-json/wc/v3"
WC_AUTH = (os.getenv("WC_KEY"), os.getenv("WC_SECRET"))


def jewlog_login():
    r = requests.post(
        f"{JEWLOG_BASE}/api/login",
        json={"username": os.getenv("JEWLOG_USER"), "password": os.getenv("JEWLOG_PASS")},
        headers={"Accept": "application/json"}, timeout=30)
    r.raise_for_status()
    d = r.json()
    return d.get("token") or d.get("access_token")


_last_call = 0.0
MIN_GAP = float(os.getenv("JEWLOG_MIN_GAP", "1.2"))   # seconds between JwelLog calls


def jewlog_get(token, path):
    """GET from JwelLog politely: space calls out and retry when it says 'too many requests'."""
    global _last_call
    for attempt in range(6):
        wait = MIN_GAP - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()
        r = requests.get(
            f"{JEWLOG_BASE}{path}",
            headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
            timeout=60)
        if r.status_code == 429:
            pause = int(r.headers.get("Retry-After", 30))
            print(f"JwelLog says slow down (429), waiting {pause}s ...")
            time.sleep(pause)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"JwelLog kept returning 429 for {path}")


def jewlog_price(token, pid):
    """The list has no prices; the single-product endpoint does."""
    return num(jewlog_get(token, f"/api/products/{pid}").get("product", {}).get(PRICE_FIELD))


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def wc_by_sku(sku):
    r = requests.get(f"{WC_API}/products", params={"sku": sku}, auth=WC_AUTH, timeout=30)
    r.raise_for_status()
    found = r.json()
    return found[0] if found else None


_cats = None


def category_id(name):
    """Return the WooCommerce category id for a product name, creating it if needed."""
    global _cats
    name = (name or "").strip()
    if not name:
        return None
    if _cats is None:
        _cats, page = {}, 1
        while True:
            r = requests.get(f"{WC_API}/products/categories",
                             params={"per_page": 100, "page": page}, auth=WC_AUTH, timeout=30)
            r.raise_for_status()
            rows = r.json()
            for c in rows:
                _cats[c["name"].strip().lower()] = c["id"]
            if len(rows) < 100:
                break
            page += 1
    key = name.lower()
    if key not in _cats:
        if DRY_RUN:
            print(f"[dry] would create category: {name}")
            return None
        r = requests.post(f"{WC_API}/products/categories", json={"name": name},
                          auth=WC_AUTH, timeout=30)
        r.raise_for_status()
        _cats[key] = r.json()["id"]
        print(f"created category: {name}")
    return _cats[key]


def sync(p, token):
    sku = (p.get("sku") or "").strip()
    if not sku:
        return
    stock = int(num(p.get("current_stock")))
    existing = wc_by_sku(sku)
    tag = "[dry] " if DRY_RUN else ""

    if existing:
        changes = {}
        if existing.get("stock_quantity") != stock or not existing.get("manage_stock"):
            changes.update({"manage_stock": True, "stock_quantity": stock})
        if UPDATE_PRICES:
            price = jewlog_price(token, p["id"])
            if price > 0 and num(existing.get("regular_price")) != price:
                changes["regular_price"] = str(price)
            held = any(m.get("key") == "jewlog_hold" and m.get("value") == "1"
                       for m in existing.get("meta_data", []))
            if price > 0 and held and NEW_STATUS == "publish":
                changes["status"] = "publish"
                changes["meta_data"] = [{"key": "jewlog_hold", "value": "0"}]
        cur = existing.get("categories", [])
        if all(c.get("slug") == "uncategorized" for c in cur):
            cid = category_id(p.get("product"))
            if cid:
                changes["categories"] = [{"id": cid}]
        if not changes:
            return
        print(f"{tag}update {sku}: {changes}")
        if not DRY_RUN:
            requests.put(f"{WC_API}/products/{existing['id']}", json=changes,
                         auth=WC_AUTH, timeout=30).raise_for_status()
        return

    if MODE != "create":
        print(f"skip (not in WooCommerce): {sku}")
        return
    if p.get("total") is None:      # blank "parent/template" rows in JwelLog
        print(f"skip (template row): {sku}")
        return

    price = jewlog_price(token, p["id"])
    inactive = bool(p.get("is_inactive") or p.get("not_for_selling"))
    live = NEW_STATUS == "publish" and price > 0 and not inactive
    hold = NEW_STATUS == "publish" and price <= 0 and not inactive   # wait for a price
    body = {
        "name": p.get("product") or sku, "sku": sku, "type": "simple",
        "status": "publish" if live else "draft",
        "manage_stock": True, "stock_quantity": stock,
        "meta_data": [{"key": "jewlog_id", "value": str(p.get("id"))},
                      {"key": "jewlog_category", "value": str(p.get("category"))},
                      {"key": "jewlog_hold", "value": "1" if hold else "0"}],
    }
    cid = category_id(p.get("product"))
    if cid:
        body["categories"] = [{"id": cid}]
    if price > 0:
        body["regular_price"] = str(price)
    print(f"{tag}create {body['status']} {sku} ({body['name']}) price={price} stock={stock}")
    if not DRY_RUN:
        requests.post(f"{WC_API}/products", json=body, auth=WC_AUTH, timeout=30).raise_for_status()


if __name__ == "__main__":
    print(f"MODE={MODE} DRY_RUN={DRY_RUN} UPDATE_PRICES={UPDATE_PRICES} NEW_STATUS={NEW_STATUS}")
    tok = jewlog_login()
    for prod in jewlog_get(tok, "/api/products").get("products", []):
        try:
            sync(prod, tok)
        except requests.HTTPError as e:
            print(f"failed {prod.get('sku')}: {e}")
