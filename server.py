# Unified Product Research Dashboard - local server
#
# Run with: python server.py
# Open: http://127.0.0.1:8765
#
# This file renders the new English 5-tab dashboard and receives data from the
# Chrome extension. It deliberately does NOT import or use ebay_api.py because
# the API-key experiment was abandoned. Data comes only from public browser DOM
# pages read by the extension.

from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from pathlib import Path
import csv
import json
import re
import time
import traceback
import threading
import webbrowser
import urllib.request
import urllib.error

# SERVER ROUTE MAP
# ----------------
# GET /                         -> renders the dashboard shell and tabs.
# GET /api/products/cards       -> Store Tracker cards, backed by store_products.
# GET /api/search/products      -> eBay Search cards, backed by search_results.
# GET /api/dashboard/products   -> Tab 1 selected-product cards, backed by dashboard_products.
# POST /api/collect             -> Store/product-page ingestion from content.js.
#                                  Only verified Store URLs may create Store links.
# POST /api/collect-search      -> eBay Search ingestion. Never creates Store links.
# POST /api/collect-sales       -> Purchase History ingestion for sales metrics.
# POST /api/scan-status         -> background.js scan progress for dashboard counters.

from database import (
    init_db,
    upsert_store,
    upsert_products,
    upsert_sales,
    save_snapshot,
    list_sales,
    product_sales_cards,
    store_summary,
    delete_store_data,
    link_store_products,
    search_group_summary,
    search_product_cards,
    upsert_search_group,
    link_search_results,
    delete_search_group,
    merge_search_groups,
    alibaba_group_summary,
    alibaba_product_cards,
    upsert_alibaba_products,
    upsert_alibaba_search_group,
    link_alibaba_search_results,
    delete_alibaba_group,
    merge_alibaba_groups,
    update_alibaba_product_fields,
    dashboard_product_cards,
    select_dashboard_product,
    select_dashboard_alibaba_product,
    remove_dashboard_product,
    connect_dashboard_products,
    list_dashboard_product_links,
    delete_dashboard_product_link,
    guess_seller_from_url,
    list_fittings,
    add_fitting,
    update_fitting,
    delete_fitting,
    get_fitting_stats,
    save_gemini_key,
    get_gemini_key,
    add_fitting_from_product,
    variation_stats,
    connect,
    list_chat_conversations,
    create_chat_conversation,
    list_chat_messages,
    add_chat_message,
    delete_chat_conversation,
    search_ebay_products,
    search_alibaba_products,
    get_dashboard_data_summary,
    list_gemini_conversations,
    create_gemini_conversation,
    list_gemini_messages,
    add_gemini_message,
    delete_gemini_conversation,
)
from ai_analyzer import (
    ask_ai,
    quick_analysis,
    get_data_stats,
    QUICK_PROMPTS,
)

BASE_DIR = Path(__file__).resolve().parent
EXPORT_DIR = BASE_DIR / "exports"
EXPORT_DIR.mkdir(exist_ok=True)

# Saves a raw copy of every payload received from the Alibaba extension,
# so the owner can inspect exactly what the server received.
RAW_DUMP_DIR = BASE_DIR / "raw_alibaba_dumps"
RAW_DUMP_DIR.mkdir(exist_ok=True)

# Saves the RAW, unfiltered page DOM data captured by the extension's
# "Capture raw page data" button — before any extraction or cleaning.
RAW_PAGE_DUMP_DIR = BASE_DIR / "raw_page_dumps"
RAW_PAGE_DUMP_DIR.mkdir(exist_ok=True)

PORT = 8765

# ---------------------------------------------------------------------------
# Ollama auto-detection helper — works with ANY model (qwen2.5, llama3, mistral, etc.)
# ---------------------------------------------------------------------------
def detect_ollama_model():
    """Return (model_name, models_list, error). Uses whatever model is installed."""
    import urllib.request
    try:
        req = urllib.request.Request("http://localhost:11434/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            available_models = [m.get("name", "") for m in data.get("models", [])]
    except Exception as e:
        return None, [], f"Could not reach Ollama at localhost:11434. Detail: {str(e)}"
    if not available_models:
        return None, [], None
    return available_models[0], available_models, None

# The extension updates this object while scanning product pages and Purchase
# History pages. The dashboard reads it to show progress counters.
SCAN_STATUS = {
    "running": False,
    "total": 0,
    "done": 0,
    "remaining": 0,
    "current": None,
    "inserted": 0,
    "updated": 0,
    "errors": [],
    "updated_at": None,
}

def is_store_collection_url(url):
    """Return True only for URLs that represent a real eBay Store context.

    Generic eBay Search pages contain seller names, but those sellers must not
    become Store Tracker entries unless the URL is actually a Store or
    Store-search URL.
    """
    url = str(url or "")
    return bool(re.search(r"/str/|[?&]_ssn=|/usr/", url, re.I))


def send_json(handler, data, status=200):
    """Send JSON with CORS headers so the Chrome extension can call the server."""
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def send_html(handler, html, status=200):
    """Send the dashboard HTML page."""
    payload = html.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)



# ---------------------------------------------------------------------------
# FITTING EXTRACTION — eBay product page → Gemini AI → Fittings Library
# ---------------------------------------------------------------------------

FITTING_IMAGES_DIR = BASE_DIR / "fitting_images"
FITTING_IMAGES_DIR.mkdir(exist_ok=True)

def download_fitting_image(image_url, fitting_id, variation_index=0):
    """Download an image from URL and save it locally in fitting_images/.
    Returns the local filename (served via /fitting_images/<filename>)."""
    if not image_url or not str(image_url).startswith("http"):
        return None
    try:
        req = urllib.request.Request(image_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
        ext = ".jpg"
        ct = resp.headers.get("Content-Type", "")
        if "png" in ct: ext = ".png"
        elif "webp" in ct: ext = ".webp"
        elif "gif" in ct: ext = ".gif"
        filename = f"fitting_{fitting_id}_{variation_index}{ext}"
        filepath = FITTING_IMAGES_DIR / filename
        with open(filepath, "wb") as f:
            f.write(data)
        return filename
    except Exception as e:
        print(f"[fitting-image] Download failed for {image_url}: {e}")
        return None


def analyze_fitting_with_gemini(title, full_text, variations, page_text_excerpt, api_key=None, model="gemini-3-flash-preview"):
    """Send product data to Gemini AI for structured fitting analysis.
    Returns a dict with: name, category, material, grade, thread, pressure, temp, notes, and cleaned variations."""
    import os as _os
    if not api_key:
        api_key = _os.environ.get("GEMINI_API_KEY", "").strip()

    # Build a compact summary of variations for the prompt
    var_summary = []
    for i, v in enumerate(variations):
        var_summary.append(f"  {i+1}. Name: {v.get('name','')} | Size: {v.get('size','')} | Price: {v.get('price','')} | Image: {'yes' if v.get('image_url') else 'no'}")
    var_text = "\n".join(var_summary)

    system_prompt = (
        "You are an expert in hose fittings, connectors, and plumbing parts. "
        "You receive raw eBay product data (title, page text, and variation list). "
        "Your job is to extract structured fitting data for a parts catalogue.\n\n"
        "Return ONLY valid JSON (no markdown, no explanation). The JSON must have this exact structure:\n"
        "{\n"
        '  "name": "short product name (e.g. \\"Brass Y-Piece\\")",\n'
        '  "category": "one of: Hose, Straight Joiner, Elbow 90\u00b0, Tee / Y-Piece, Cross Joiner, Bulkhead Fitting, or empty string if unknown",\n'
        '  "material": "one of: Brass, Stainless Steel, PVC, Nylon, Aluminum, Copper, or empty string",\n'
        '  "grade": "one of: Industrial, Food Grade, Pneumatic, General, or empty string",\n'
        '  "thread": "thread spec if found (e.g. NPT 1/8\", M10x1), or empty string",\n'
        '  "pressure": "pressure rating if found (e.g. 10 BAR), or empty string",\n'
        '  "temp": "temperature range if found (e.g. -20 to 120), or empty string",\n'
        '  "notes": "any useful additional info, max 200 chars",\n'
        '  "variations": [\n'
        '    {"size": "standardized size (e.g. 6mm, 8mm, 1/4\")", "sku": "if determinable, else empty"}\n'
        '  ]\n'
        "}\n\n"
        "Rules:\n"
        "- Extract the CORE product name from the eBay title (remove brand noise, listing IDs, shipping info)\n"
        "- Map each eBay variation to a clean size. If the variation name contains a size, extract just the size.\n"
        "- If there are no real variations, return one entry in variations with the size from the title\n"
        "- Be conservative: only fill fields you are confident about. Use empty string for unknowns\n"
        "- Do NOT include images, prices, or eBay-specific data in the JSON"
    )

    user_msg = (
        f"eBay Product Title: {title}\n\n"
        f"Page Text Excerpt (first 2000 chars):\n{page_text_excerpt[:2000]}\n\n"
        f"Variations found on page ({len(variations)} total):\n{var_text}"
    )

    payload = json.dumps({
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_msg}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 2048, "responseMimeType": "application/json"}
    }).encode("utf-8")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as he:
        err_body = he.read().decode("utf-8", errors="replace")[:500]
        raise ValueError(f"Gemini API error {he.code}: {err_body}")

    answer = ""
    candidates = result.get("candidates", [])
    if candidates:
        parts = candidates[0].get("content", {}).get("parts", [])
        answer = "".join(p.get("text", "") for p in parts)
    if not answer:
        raise ValueError(f"Gemini returned empty. Raw: {json.dumps(result)[:500]}")

    # Parse the JSON response
    parsed = json.loads(answer)
    return parsed


def handle_fitting_extraction(data):
    """Main handler for /api/fittings/extract.
    Receives variation data from the extension, sends to Gemini,
    downloads images, and creates ONE fitting card per product.
    Each size variation becomes a row (fitting_variations) inside that card,
    with an auto-incremented SKU starting from 1."""
    import os as _os

    title = data.get("title", "")
    full_text = data.get("full_text", "")
    variations = data.get("variations", [])
    page_excerpt = data.get("page_text_excerpt", "")
    main_image = data.get("main_image", "")

    if not variations:
        return {"ok": False, "error": "No variations found in the data"}

    # --- Get Gemini API key ---
    gemini_key = data.get("gemini_key", "").strip()
    if not gemini_key:
        gemini_key = _os.environ.get("GEMINI_API_KEY", "").strip()
    if not gemini_key:
        gemini_key = get_gemini_key() or ""
    gemini_model_req = data.get("gemini_model", "gemini-3-flash-preview").strip()

    if not gemini_key:
        return {"ok": False, "error": "Gemini API key not found. Save your key in the Gemini tab or set GEMINI_API_KEY env var."}

    # --- Call Gemini for structured analysis ---
    try:
        analysis = analyze_fitting_with_gemini(
            title, full_text, variations, page_excerpt,
            api_key=gemini_key, model=gemini_model_req
        )
    except Exception as e:
        traceback.print_exc()
        return {"ok": False, "error": f"Gemini analysis failed: {str(e)}"}

    fitting_name = analysis.get("name", title[:80])
    ai_variations = analysis.get("variations", [])
    source_url = data.get("product_url", "")

    # --- Delete existing fittings with the same source_url (re-extraction) ---
    # We use source_url as the dedup key, NOT eBay item_id.
    if source_url:
        try:
            conn = connect()
            conn.execute(
                "DELETE FROM fitting_variations WHERE fitting_id IN "
                "(SELECT id FROM fittings WHERE source_url = ?)",
                (source_url,)
            )
            conn.execute("DELETE FROM fittings WHERE source_url = ?", (source_url,))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[fitting] Failed to clean old fittings: {e}")

    images_downloaded = 0

    # --- Build variation rows with auto-incremented SKU (1, 2, 3, ...) ---
    var_rows = []
    for i, ai_var in enumerate(ai_variations):
        size = ai_var.get("size", "")

        # Match with DOM variation for image
        orig_var = None
        if i < len(variations):
            orig_var = variations[i]
        elif size:
            for v in variations:
                if v.get("size", "").lower() == size.lower():
                    orig_var = v
                    break

        var_image_url = orig_var.get("image_url", "") if orig_var else ""
        image_to_download = var_image_url or main_image

        var_rows.append({
            "size": size,
            "sku": str(i + 1),  # auto-incremented SKU starting from 1
            "pressure": analysis.get("pressure", ""),
            "image_url": "",
            "_image_to_download": image_to_download,
        })

    # --- Create ONE fitting card with all variations inside ---
    fitting_data = {
        "name": fitting_name,
        "sku": "",  # will be set to fitting's own DB id
        "source_url": source_url,
        "source_item_id": "",  # no longer using eBay item_id
        "image_url": "",
        "category": analysis.get("category", ""),
        "material": analysis.get("material", ""),
        "thread": analysis.get("thread", ""),
        "pressure": analysis.get("pressure", ""),
        "temp": analysis.get("temp", ""),
        "grade": analysis.get("grade", ""),
        "notes": analysis.get("notes", ""),
        "variations": [{"size": v["size"], "sku": v["sku"], "pressure": v["pressure"]} for v in var_rows],
    }

    result = add_fitting(fitting_data)
    if not result.get("ok"):
        return {"ok": False, "error": f"Failed to create fitting: {result.get('error', 'unknown')}"}

    fitting_id = result["id"]

    # Set the fitting's own SKU to its DB id
    try:
        conn = connect()
        conn.execute("UPDATE fittings SET sku=? WHERE id=?", (str(fitting_id), fitting_id))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[fitting] Failed to set SKU for id {fitting_id}: {e}")

    # Download images for each variation
    for i, var in enumerate(var_rows):
        image_to_download = var["_image_to_download"]
        if image_to_download:
            local_img = download_fitting_image(image_to_download, fitting_id, i + 1)
            if local_img:
                try:
                    conn = connect()
                    # Update the variation row's image_url
                    conn.execute(
                        "UPDATE fitting_variations SET image_url=? WHERE fitting_id=? AND sku=?",
                        (f"/fitting_images/{local_img}", fitting_id, var["sku"])
                    )
                    conn.commit()
                    conn.close()
                    images_downloaded += 1
                except Exception as e:
                    print(f"[fitting] Failed to update image_url for variation {var['sku']}: {e}")

    # Also set the fitting's main image to the first variation's image
    if var_rows and var_rows[0]["_image_to_download"]:
        main_image_local = download_fitting_image(var_rows[0]["_image_to_download"], fitting_id, 0)
        if main_image_local:
            try:
                conn = connect()
                conn.execute("UPDATE fittings SET image_url=? WHERE id=?",
                             (f"/fitting_images/{main_image_local}", fitting_id))
                conn.commit()
                conn.close()
                images_downloaded += 1
            except Exception as e:
                print(f"[fitting] Failed to set main image: {e}")

    return {
        "ok": True,
        "message": f"Created 1 fitting card with {len(var_rows)} variation(s).",
        "fitting_id": fitting_id,
        "fitting_name": fitting_name,
        "fittings_created": 1,
        "variations_count": len(var_rows),
        "images_downloaded": images_downloaded,
        "gemini_analysis": {
            "category": analysis.get("category", ""),
            "material": analysis.get("material", ""),
            "grade": analysis.get("grade", ""),
            "thread": analysis.get("thread", ""),
        }
    }


class Handler(BaseHTTPRequestHandler):
    """HTTP routes for dashboard UI and extension data collection."""

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args))

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)

            if path == "/":
                return send_html(self, dashboard_html())

            if path == "/api/health":
                return send_json(self, {"ok": True, "message": "Local server is running"})

            if path == "/api/scan-status":
                return send_json(self, SCAN_STATUS)

            if path == "/api/stores/summary":
                return send_json(self, {"stores": store_summary()})

            if path == "/api/products/cards":
                seller = qs.get("seller", [None])[0]
                return send_json(self, {"products": product_sales_cards(seller)})

            if path == "/api/search-groups/summary":
                return send_json(self, {"groups": search_group_summary()})

            if path == "/api/search/products/cards":
                group = qs.get("group", [None])[0]
                return send_json(self, {"products": search_product_cards(group)})

            if path == "/api/alibaba-groups/summary":
                return send_json(self, {"groups": alibaba_group_summary()})

            if path == "/api/alibaba/products/cards":
                group = qs.get("group", [None])[0]
                return send_json(self, {"products": alibaba_product_cards(group)})

            if path == "/api/dashboard/products/cards":
                return send_json(self, {"products": dashboard_product_cards()})

            if path == "/api/dashboard/links":
                return send_json(self, {"links": list_dashboard_product_links()})

            if path == "/api/sales":
                item_id = qs.get("item_id", [None])[0]
                return send_json(self, {"sales": list_sales(item_id)})

            if path == "/api/export/products.csv":
                seller = qs.get("seller", [None])[0]
                products = product_sales_cards(seller)
                file_path = EXPORT_DIR / "products.csv"
                # Extra metric columns are included so testing/export is easier.
                fields = [
                    "item_id", "title", "price_text", "product_url", "image_url", "seller_username",
                    "total_sold_text", "total_sold", "available_text", "available_quantity",
                    "postage_text", "watch_count_text", "sold_yesterday", "sold_7_days",
                    "sold_30_days", "revenue_30_days", "tracked_total_quantity",
                    "tracked_total_revenue", "first_seen_at", "last_seen_at",
                ]
                with open(file_path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=fields)
                    writer.writeheader()
                    for p in products:
                        writer.writerow({k: p.get(k) for k in fields})
                return self._send_file(file_path, "text/csv; charset=utf-8", "products.csv")

            if path == "/api/export/sales.csv":
                sales = list_sales(None, 100000)
                file_path = EXPORT_DIR / "sales.csv"
                fields = ["item_id", "buyer_id", "variation", "price_text", "price", "currency", "quantity", "sold_at", "sold_at_text", "location", "source_page_url", "collected_at"]
                with open(file_path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=fields)
                    writer.writeheader()
                    for row in sales:
                        writer.writerow({k: row.get(k) for k in fields})
                return self._send_file(file_path, "text/csv; charset=utf-8", "sales.csv")

            if path == "/api/ollama-status":
                try:
                    import urllib.request as ur
                    req = ur.Request("http://localhost:11434/api/tags", method="GET")
                    with ur.urlopen(req, timeout=3) as resp:
                        data = json.loads(resp.read())
                        models = [m.get("name","") for m in data.get("models",[])]
                        return send_json(self, {"ok": True, "models": models})
                except Exception as ex:
                    return send_json(self, {"ok": False, "error": str(ex)})

            if path == "/api/variation-stats":
                item_id = qs.get("item_id", [None])[0]
                if not item_id:
                    return send_json(self, {"ok": False, "error": "Missing item_id"}, 400)
                return send_json(self, variation_stats(item_id))

            if path == "/api/ai/stats":
                return send_json(self, get_data_stats())

            if path == "/api/ai/quick-prompts":
                return send_json(self, {"prompts": QUICK_PROMPTS})

            if path == "/api/chat/conversations":
                return send_json(self, {"ok": True, "conversations": list_chat_conversations()})

            if path == "/api/chat/messages":
                conv_id = qs.get("conversation_id", [None])[0]
                if not conv_id:
                    return send_json(self, {"ok": False, "error": "Missing conversation_id"}, 400)
                return send_json(self, {"ok": True, "messages": list_chat_messages(int(conv_id))})

            if path == "/api/gemini/conversations":
                return send_json(self, {"ok": True, "conversations": list_gemini_conversations()})

            if path == "/api/gemini/messages":
                conv_id = qs.get("conversation_id", [None])[0]
                if not conv_id:
                    return send_json(self, {"ok": False, "error": "Missing conversation_id"}, 400)
                return send_json(self, {"ok": True, "messages": list_gemini_messages(int(conv_id))})

            if path.startswith("/fitting_images/"):
                import mimetypes
                filename = path.split("/")[-1]
                file_path = FITTING_IMAGES_DIR / filename
                if file_path.exists():
                    ct, _ = mimetypes.guess_type(str(file_path))
                    return self._send_file(file_path, ct or "application/octet-stream", filename)
                return send_json(self, {"ok": False, "error": "Image not found"}, 404)

            if path == "/api/gemini/get-key":
                key = get_gemini_key() or ""
                return send_json(self, {"ok": bool(key), "key": key})

            if path == "/api/fittings":
                return send_json(self, {"fittings": list_fittings(category=qs.get("category",[None])[0],material=qs.get("material",[None])[0],grade=qs.get("grade",[None])[0],size=qs.get("size",[None])[0],search=qs.get("search",[None])[0])})
            if path == "/api/fittings/stats":
                return send_json(self, get_fitting_stats())

            return send_json(self, {"ok": False, "error": f"Unknown GET route: {path}"}, 404)
        except Exception as e:
            traceback.print_exc()
            return send_json(self, {"ok": False, "error": "Server failed while handling GET request", "detail": str(e)}, 500)

    def do_DELETE(self):
        try:
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            if parsed.path == '/api/chat/conversations':
                conv_id = qs.get('conversation_id', [None])[0]
                if conv_id:
                    delete_chat_conversation(int(conv_id))
                    return send_json(self, {'ok': True})
                return send_json(self, {'ok': False, 'error': 'Missing conversation_id'}, 400)
            if parsed.path == '/api/gemini/conversations':
                conv_id = qs.get('conversation_id', [None])[0]
                if conv_id:
                    delete_gemini_conversation(int(conv_id))
                    return send_json(self, {'ok': True})
                return send_json(self, {'ok': False, 'error': 'Missing conversation_id'}, 400)
            if parsed.path == '/api/fittings':
                fit_id = qs.get('id', [None])[0]
                if fit_id:
                    result = delete_fitting(int(fit_id))
                    return send_json(self, result)
                return send_json(self, {'ok': False, 'error': 'Missing fitting id'}, 400)
            return send_json(self, {'ok': False, 'error': 'Not found'}, 404)
        except Exception as e:
            return send_json(self, {'ok': False, 'error': str(e)}, 500)

    def do_POST(self):
        try:
            parsed = urlparse(self.path)
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            try:
                body = json.loads(raw)
            except Exception:
                return send_json(self, {"ok": False, "error": "Invalid JSON body", "hint": "Check the extension console or payload format."}, 400)

            if parsed.path == "/api/scan-status":
                SCAN_STATUS.update(body or {})
                SCAN_STATUS["remaining"] = max(0, int(SCAN_STATUS.get("total") or 0) - int(SCAN_STATUS.get("done") or 0))
                SCAN_STATUS["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                return send_json(self, {"ok": True, "scan_status": SCAN_STATUS})

            # Dashboard selection endpoints. These only add/remove a membership
            # row in dashboard_products; they never copy products or alter Store/Search links.
            if parsed.path == "/api/dashboard/select":
                if body.get("source") == "alibaba_search":
                    result = select_dashboard_alibaba_product(body.get("product_key") or body.get("item_id"), body.get("source_group"))
                else:
                    result = select_dashboard_product(body.get("item_id"), body.get("source_group"))
                return send_json(self, result, 200 if result.get("ok") else 400)

            if parsed.path == "/api/dashboard/remove":
                result = remove_dashboard_product(body.get("item_id"))
                return send_json(self, result, 200 if result.get("ok") else 400)

            if parsed.path == "/api/dashboard/connect":
                result = connect_dashboard_products(body.get("first_item_id"), body.get("second_item_id"))
                return send_json(self, result, 200 if result.get("ok") else 400)

            if parsed.path == "/api/dashboard/unlink":
                result = delete_dashboard_product_link(body.get("link_id"))
                return send_json(self, result, 200 if result.get("ok") else 400)

            # Store/product collection endpoint.
            # Guardrail: generic eBay Search pages can contain seller names, but
            # seller names alone must not create Store Tracker entries.
            if parsed.path == "/api/collect":
                page_url = body.get("page_url")
                seller = body.get("seller_username") or guess_seller_from_url(page_url) or "unknown"
                products = body.get("products") or []
                raw_text = body.get("raw_text") or ""
                is_store_page = bool(body.get("is_store_page")) or is_store_collection_url(page_url)

                if not products:
                    save_snapshot(page_url, seller, raw_text, 0)
                    return send_json(self, {
                        "ok": False,
                        "error": "No products were extracted from this page.",
                        "hint": "Open a fully loaded eBay store/search/product page and click the extension again.",
                        "seller_username": seller,
                    }, 422)

                # Only a real Store/Store-search URL may create Store Tracker context.
                # Seller names seen on generic Search cards are product metadata only.
                if is_store_page and seller and seller != "unknown":
                    upsert_store(page_url, seller)

                for product in products:
                    if is_store_page and product.get("metadata_source") != "product_page_public_dom" and seller and seller != "unknown":
                        product["store_collected"] = 1
                    else:
                        product["store_collected"] = 0

                result = upsert_products(products, source_page_url=page_url, seller_username=seller)
                store_links = link_store_products(seller, products, page_url) if is_store_page else {"inserted": 0, "updated": 0, "skipped": len(products)}
                save_snapshot(page_url, seller, raw_text[:200000], len(products))
                return send_json(self, {"ok": True, "seller_username": seller, "is_store_page": is_store_page, "result": result, "store_links": store_links})

            # eBay Search collection endpoint.
            # Products saved here are linked only through search_results. They
            # may keep seller_username as metadata, but never create Store rows.
            if parsed.path == "/api/collect-search":
                page_url = body.get("page_url")
                products = body.get("products") or []
                group_name = body.get("search_group_name") or "eBay image search"
                raw_text = body.get("raw_text") or ""

                if not products:
                    save_snapshot(page_url, "SEARCH:" + group_name, raw_text, 0)
                    return send_json(self, {
                        "ok": False,
                        "error": "No similar eBay search products were extracted.",
                        "hint": "The page may not be fully loaded, or the results may not share enough title words to form a product group.",
                        "rejected_count": body.get("rejected_count", 0),
                    }, 422)

                # Search result cards are deliberately kept out of the Store tab.
                # They still update shared product facts, especially price ranges from
                # the search card, but store_collected remains 0.
                for product in products:
                    product["metadata_source"] = "search_card_public_dom"
                    product["store_collected"] = 0
                result = upsert_products(products, source_page_url=page_url, seller_username=None)
                group_id = upsert_search_group(group_name, body.get("search_query"), page_url, body.get("dominant_tokens"))
                link_result = link_search_results(group_id, products)
                save_snapshot(page_url, "SEARCH:" + group_name, raw_text[:200000], len(products))
                return send_json(self, {
                    "ok": True,
                    "search_group_name": group_name,
                    "dominant_tokens": body.get("dominant_tokens") or [],
                    "rejected_count": body.get("rejected_count", 0),
                    "result": result,
                    "links": link_result,
                })

            # Alibaba Search collection endpoint.
            # Alibaba products are stored in independent tables so eBay Store/Search logic stays untouched.
            # Raw page dump endpoint — saves whatever the extension sends,
            # no processing, no filtering, no extraction. Pure raw DOM capture.
            if parsed.path == "/api/raw-dump":
                try:
                    import datetime as _rdt
                    _rts = _rdt.datetime.now().strftime("%Y%m%d_%H%M%S")
                    _rdump_path = RAW_PAGE_DUMP_DIR / f"raw_page_{_rts}.json"
                    with open(_rdump_path, "w", encoding="utf-8") as _rf:
                        json.dump(body, _rf, ensure_ascii=False, indent=2)
                    print(f"[raw page dump] Saved raw page data to {_rdump_path} (links={body.get('link_count')}, images={body.get('image_count')})")
                    return send_json(self, {
                        "ok": True,
                        "saved_to": str(_rdump_path),
                        "link_count": body.get("link_count"),
                        "image_count": body.get("image_count"),
                        "body_text_length": len(body.get("body_inner_text") or ""),
                    })
                except Exception as e:
                    traceback.print_exc()
                    return send_json(self, {"ok": False, "error": str(e)}, 500)

            if parsed.path == "/api/collect-alibaba":
                # Save a raw copy of exactly what the server received, before any processing.
                try:
                    import datetime as _dt
                    _ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
                    _dump_path = RAW_DUMP_DIR / f"alibaba_raw_{_ts}.json"
                    with open(_dump_path, "w", encoding="utf-8") as _f:
                        json.dump(body, _f, ensure_ascii=False, indent=2)
                    print(f"[raw dump] Saved copy of incoming Alibaba payload to {_dump_path}")
                except Exception as _e:
                    print(f"[raw dump] Failed to save raw copy: {_e}")
                page_url = body.get("page_url")
                products = body.get("products") or []
                group_name = body.get("search_group_name") or "Alibaba image search"
                raw_text = body.get("raw_text") or ""

                if not products:
                    save_snapshot(page_url, "ALIBABA:" + group_name, raw_text, 0)
                    return send_json(self, {
                        "ok": False,
                        "error": "No Alibaba products were extracted.",
                        "hint": "Open a fully loaded Alibaba search/image-search page and click the extension again.",
                        "rejected_count": body.get("rejected_count", 0),
                    }, 422)

                result = upsert_alibaba_products(products, source_page_url=page_url)
                group_id = upsert_alibaba_search_group(group_name, body.get("search_query"), page_url, body.get("dominant_tokens"))
                link_result = link_alibaba_search_results(group_id, products)
                save_snapshot(page_url, "ALIBABA:" + group_name, raw_text[:200000], len(products))
                return send_json(self, {
                    "ok": True,
                    "search_group_name": group_name,
                    "dominant_tokens": body.get("dominant_tokens") or [],
                    "rejected_count": body.get("rejected_count", 0),
                    "result": result,
                    "links": link_result,
                })

            # Purchase History collection endpoint.
            # These rows are context-neutral: both Store and Search cards read
            # sales by item_id to calculate yesterday/7-day/30-day metrics.
            if parsed.path == "/api/collect-sales":
                page_url = body.get("page_url")
                sales = body.get("sales") or []
                result = upsert_sales(sales, source_page_url=page_url)
                return send_json(self, {
                    "ok": True,
                    "warning": body.get("warning"),
                    "result": result,
                })

            if parsed.path == "/api/stores/delete":
                seller = body.get("seller_username")
                result = delete_store_data(seller)
                return send_json(self, {"ok": "error" not in result, "seller_username": seller, "result": result})

            if parsed.path == "/api/search-groups/delete":
                group = body.get("group_name")
                result = delete_search_group(group)
                return send_json(self, {"ok": "error" not in result, "group_name": group, "result": result})

            if parsed.path == "/api/search-groups/merge":
                result = merge_search_groups(body.get("group_names") or [], body.get("target_group_name"), body.get("new_group_name"))
                return send_json(self, result, 200 if result.get("ok") else 400)

            if parsed.path == "/api/alibaba-groups/delete":
                group = body.get("group_name")
                result = delete_alibaba_group(group)
                return send_json(self, {"ok": "error" not in result, "group_name": group, "result": result})

            if parsed.path == "/api/alibaba-groups/merge":
                result = merge_alibaba_groups(body.get("group_names") or [], body.get("target_group_name"), body.get("new_group_name"))
                return send_json(self, result, 200 if result.get("ok") else 400)

            if parsed.path == "/api/alibaba/product/update-fields":
                result = update_alibaba_product_fields(body.get("product_key"), body.get("shipping_text"), body.get("delivery_text"))
                return send_json(self, result, 200 if result.get("ok") else 400)

            # AI Analysis endpoint - sends database data + question to OpenAI
            if parsed.path == "/api/ai/analyze":
                question = body.get("question", "").strip()
                api_key = body.get("api_key", "").strip() or None
                model = body.get("model", "gpt-4o").strip()

                if not question:
                    return send_json(self, {"ok": False, "error": "No question provided."}, 400)

                result = ask_ai(question, api_key=api_key, model=model)
                return send_json(self, result, 200 if result.get("ok") else 500)

            # Quick analysis with pre-defined prompts
            if parsed.path == "/api/ai/quick":
                prompt_key = body.get("prompt_key", "").strip()
                api_key = body.get("api_key", "").strip() or None
                model = body.get("model", "gpt-4o").strip()

                if not prompt_key:
                    return send_json(self, {"ok": False, "error": "No prompt_key provided."}, 400)

                result = quick_analysis(prompt_key, api_key=api_key, model=model)
                return send_json(self, result, 200 if result.get("ok") else 500)

            # Variation analysis via local Ollama
            if parsed.path == "/api/variation-analysis":
                import urllib.request
                item_id = body.get("item_id", "").strip()
                if not item_id:
                    return send_json(self, {"ok": False, "error": "Missing item_id"}, 400)

                stats_data = variation_stats(item_id)
                if not stats_data.get("variations"):
                    return send_json(self, {"ok": False, "error": "No sales data found for this product."}, 400)

                # Build a human-readable summary for Ollama
                lines = []
                lines.append(f"Product: {stats_data.get('product_title') or item_id}")
                lines.append(f"Total variations: {stats_data['total_variations']}")
                lines.append(f"Total sales: {stats_data['total_sales']}")
                lines.append(f"Total quantity sold: {stats_data['total_quantity']}")
                lines.append(f"Total revenue: £{stats_data['total_revenue']}")
                lines.append("")
                lines.append("Per-variation breakdown:")
                for i, v in enumerate(stats_data["variations"], 1):
                    lines.append(
                        f"  {i}. Variation: {v['variation_name']}"
                        f" | Sales: {v['sales_count']}"
                        f" | Qty: {v['total_quantity']}"
                        f" | Revenue: £{v['total_revenue']}"
                        f" | First sale: {v.get('earliest_sale_text') or 'N/A'}"
                        f" | Last sale: {v.get('latest_sale_text') or 'N/A'}"
                    )
                data_summary = "\n".join(lines)

                ai_provider = body.get("ai_provider", "ollama")
                gemini_key = body.get("gemini_key", "").strip()
                if not gemini_key:
                    import os as _os
                    gemini_key = _os.environ.get("GEMINI_API_KEY", "").strip()

                _user_msg = f"Here is the variation sales data for an eBay product:\n\n{data_summary}\n\nAnalyse this data using the format specified."

                # --- GEMINI ---
                if ai_provider == "gemini":
                    if not gemini_key:
                        return send_json(self, {"ok": False, "error": "Gemini API key is required. Save it in the Gemini tab first."}, 400)
                    _req_model = body.get("gemini_model", "").strip()
                    gemini_model = _req_model if _req_model else "gemini-3-flash-preview"
                    try:
                        gemini_sys = "You are an expert eBay product sales analyst. You receive variation-level sales data. Your job is to produce a structured, actionable report.\n\nFormat your response as:\n1) Executive Summary — 2 sentences max.\n2) Top Variations — rank the top 3 variations by revenue. For each, show: variation name, units sold, revenue, and percentage share of total revenue.\n3) Underperforming Variations — list any variations below 10% of total revenue. Explain why they underperform.\n4) Trend Analysis — based on first/last sale dates, determine if sales are accelerating, steady, or declining. Explain your reasoning.\n5) Inventory Recommendation — state clearly: which variations to restock, which to reduce, and which to drop. Give specific reasons.\n\nRules:\n- Calculate all percentages yourself.\n- Be concise. Use bullet points.\n- If data is insufficient for any section, say \"Insufficient data\" and move on.\n- Do not repeat the raw data back to the user."
                        gemini_payload = json.dumps({
                            "system_instruction": {"parts": [{"text": gemini_sys}]},
                            "contents": [{"role": "user", "parts": [{"text": _user_msg}]}],
                            "generationConfig": {"temperature": 0.7, "maxOutputTokens": 4096}
                        }).encode("utf-8")
                        gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/{gemini_model}:generateContent?key={gemini_key}"
                        req = urllib.request.Request(gemini_url, data=gemini_payload, headers={"Content-Type": "application/json"}, method="POST")
                        with urllib.request.urlopen(req, timeout=120) as resp:
                            gemini_result = json.loads(resp.read().decode("utf-8"))
                        answer = ""
                        candidates = gemini_result.get("candidates", [])
                        if candidates:
                            parts = candidates[0].get("content", {}).get("parts", [])
                            answer = "".join(p.get("text", "") for p in parts)
                        if not answer:
                            return send_json(self, {"ok": False, "error": f"Gemini returned empty. Response: {json.dumps(gemini_result)[:500]}"}, 500)
                        return send_json(self, {"ok": True, "analysis": answer.strip(), "model_used": gemini_model})
                    except urllib.error.HTTPError as e:
                        err_body = e.read().decode("utf-8") if hasattr(e, 'read') else ""
                        return send_json(self, {"ok": False, "error": f"Gemini HTTP error {e.code}: {err_body[:300]}"}, 502)
                    except Exception as e:
                        return send_json(self, {"ok": False, "error": f"Gemini request failed: {str(e)}"}, 500)

                # --- OLLAMA (default) ---
                _model, _available_models, _ollama_err = detect_ollama_model()
                if _ollama_err:
                    return send_json(self, {"ok": False, "error": _ollama_err}, 502)
                if not _model:
                    return send_json(self, {"ok": False, "error": "Ollama is running but no models are installed. Run: ollama pull <model_name>"}, 502)

                ollama_payload = json.dumps({
                    "model": _model,
                    "messages": [
                        {"role": "system", "content": "You are an expert eBay product sales analyst. You receive variation-level sales data. Your job is to produce a structured, actionable report.\n\nFormat your response as:\n1) Executive Summary — 2 sentences max.\n2) Top Variations — rank the top 3 variations by revenue. For each, show: variation name, units sold, revenue, and percentage share of total revenue.\n3) Underperforming Variations — list any variations below 10% of total revenue. Explain why they underperform.\n4) Trend Analysis — based on first/last sale dates, determine if sales are accelerating, steady, or declining. Explain your reasoning.\n5) Inventory Recommendation — state clearly: which variations to restock, which to reduce, and which to drop. Give specific reasons.\n\nRules:\n- Calculate all percentages yourself.\n- Be concise. Use bullet points.\n- If data is insufficient for any section, say \"Insufficient data\" and move on.\n- Do not repeat the raw data back to the user."},
                        {"role": "user", "content": _user_msg}
                    ],
                    "stream": False
                }).encode("utf-8")

                try:
                    req = urllib.request.Request(
                        "http://localhost:11434/api/chat",
                        data=ollama_payload,
                        headers={"Content-Type": "application/json"},
                        method="POST"
                    )
                    with urllib.request.urlopen(req, timeout=120) as resp:
                        ollama_result = json.loads(resp.read().decode("utf-8"))
                    answer = ollama_result.get("message", {}).get("content", "")
                    if not answer:
                        return send_json(self, {"ok": False, "error": f"Ollama model '{_model}' returned an empty response."}, 500)
                    return send_json(self, {"ok": True, "analysis": answer.strip(), "model_used": _model})
                except urllib.error.HTTPError as e:
                    err_body = e.read().decode("utf-8") if hasattr(e, 'read') else ""
                    return send_json(self, {"ok": False, "error": f"Ollama HTTP error {e.code}: {err_body[:300]}"}, 502)
                except urllib.error.URLError as e:
                    return send_json(self, {"ok": False, "error": f"Could not reach Ollama: {str(e.reason)}"}, 502)
                except Exception as e:
                    return send_json(self, {"ok": False, "error": f"Ollama request failed: {str(e)}"}, 500)

            # ── Gemini models listing endpoint ────────────────────────────────
            if parsed.path == "/api/gemini/models":
                api_key = body.get("api_key", "").strip()
                if not api_key:
                    return send_json(self, {"ok": False, "error": "Missing Gemini API Key"}, 400)
                try:
                    from google import genai
                    client = genai.Client(api_key=api_key)
                    available = []
                    for m in client.models.list():
                        name = m.name if hasattr(m, 'name') else str(m)
                        # Strip "models/" prefix if present
                        if name.startswith("models/"):
                            name = name[len("models/"):]
                        # Only show gemini models that support generateContent
                        methods = getattr(m, 'supported_generation_methods', []) or []
                        if not methods or 'generateContent' in methods:
                            available.append(name)
                    return send_json(self, {"ok": True, "models": available})
                except ImportError:
                    return send_json(self, {"ok": False, "error": "google-genai package not installed. Run: pip install --upgrade google-genai"}, 500)
                except Exception as e:
                    return send_json(self, {"ok": False, "error": f"Failed to list models: {str(e)}"}, 500)

            # ── Gemini chat endpoint ──────────────────────────────────────────
            if parsed.path == "/api/gemini/chat":
                api_key = body.get("api_key", "").strip()
                model_name = body.get("model", "gemini-3-flash-preview").strip()
                message = body.get("message", "").strip()
                history = body.get("history", [])

                if not api_key:
                    return send_json(self, {"ok": False, "error": "Missing Gemini API Key. Get one from https://aistudio.google.com/apikey"}, 400)
                if not message:
                    return send_json(self, {"ok": False, "error": "Missing message"}, 400)

                dashboard_summary = get_dashboard_data_summary()
                # Truncate to first 3000 chars to save Gemini tokens
                if len(dashboard_summary) > 3000:
                    dashboard_summary = dashboard_summary[:3000] + "\n... (truncated, more data available in database)"

                # Build full data context for Gemini
                import sqlite3 as _sqlite3
                _conn = connect()
                _ebay_rows = _conn.execute(
                    "SELECT item_id, title, price_text, seller_username FROM products ORDER BY last_seen_at DESC LIMIT 200"
                ).fetchall()
                _ali_rows = _conn.execute(
                    "SELECT product_key, title, price_text, supplier_name FROM alibaba_products ORDER BY last_seen_at DESC LIMIT 200"
                ).fetchall()
                _conn.close()
                _ebay_list = "\n".join(
                    f'  - item_id={r["item_id"]} | {(r["title"] or "")[:60]} | {r["price_text"]} | seller: {r["seller_username"]}'
                    for r in _ebay_rows
                )
                _ali_list = "\n".join(
                    f'  - product_key={r["product_key"]} | {(r["title"] or "")[:60]} | {r["price_text"]} | supplier: {r["supplier_name"]}'
                    for r in _ali_rows
                )
                gemini_system = (
                    "You are an autonomous product research agent for an eBay reseller who sources products from Alibaba to sell on eBay UK.\n\n"
                    "=== YOUR PURPOSE ===\n"
                    "1. FIND BEST-SELLING EBAY PRODUCTS: Identify which products have the highest sales volume.\n"
                    "2. FIND BEST ALIBABA SUPPLIERS: lowest price, high rating, high sold_count, ships to UK.\n"
                    "3. ADD TO DASHBOARD: When the user asks to add product(s) to dashboard, you MUST emit action commands.\n\n"
                    "=== ACTIONS — VERY IMPORTANT ===\n"
                    "When the user asks to add products to dashboard, you MUST include actions in your reply using EXACTLY this format:\n"
                    "For eBay products: <<ACTION:ADD_EBAY:item_id_here>>\n"
                    "For Alibaba products: <<ACTION:ADD_ALIBABA:product_key_here>>\n"
                    "You can include multiple actions in one reply. Always pick the REAL item_id or product_key from the database lists below.\n"
                    "Example reply: 'من محصول X را به داشبورد اضافه کردم. <<ACTION:ADD_EBAY:123456789012>>'\n\n"
                    "=== DATABASE STRUCTURE ===\n"
                    "- eBay products: item_id, title, price, seller, sales metrics.\n"
                    "- Alibaba products: product_key, title, price range, supplier, rating, sold_count.\n"
                    "- Dashboard: selected products for active research.\n\n"
                    "=== HOW TO HELP ===\n"
                    "- Always work with ACTUAL DATA from the lists below — never make up item_id or product_key.\n"
                    "- Respond in the SAME language as the user. Persian → Persian. English → English.\n"
                    "- When adding to dashboard: ALWAYS include <<ACTION:...>> tags so the server can execute the action.\n\n"
                    "=== AVAILABLE eBay PRODUCTS ===\n"
                    + _ebay_list + "\n\n"
                    "=== AVAILABLE Alibaba PRODUCTS ===\n"
                    + _ali_list + "\n\n"
                    "Current dashboard summary:\n" + dashboard_summary
                )

                try:
                    import time as _time
                    _time.sleep(1)  # Rate limit: 1 second delay between requests
                    from google import genai
                    client = genai.Client(api_key=api_key)

                    # Build conversation — limit to last 5 messages to save tokens
                    recent = (history or [])[-5:]
                    contents = []
                    for m in recent:
                        role = "user" if m.get("role") == "user" else "model"
                        contents.append({"role": role, "parts": [{"text": m.get("content", "")}]})
                    contents.append({"role": "user", "parts": [{"text": message}]})

                    response = client.models.generate_content(
                        model=model_name,
                        contents=contents,
                        config={
                            "system_instruction": gemini_system,
                            "temperature": 0.7,
                        }
                    )

                    answer = response.text if response.text else ""
                    if not answer:
                        return send_json(self, {"ok": False, "error": "Gemini returned an empty response."}, 500)

                    # ── Parse and execute <<ACTION:...>> tags from Gemini reply ──
                    import re as _re
                    executed_actions = []
                    action_errors = []
                    action_pattern = _re.compile(r'<<ACTION:(ADD_EBAY|ADD_ALIBABA):([^>]+)>>')
                    for _match in action_pattern.finditer(answer):
                        _atype, _aid = _match.group(1), _match.group(2).strip()
                        try:
                            if _atype == "ADD_EBAY":
                                _result = select_dashboard_product(_aid)
                            else:
                                _result = select_dashboard_alibaba_product(_aid)
                            if _result.get("ok"):
                                executed_actions.append({"type": _atype, "id": _aid, "ok": True})
                            else:
                                action_errors.append({"type": _atype, "id": _aid, "error": _result.get("error")})
                        except Exception as _ae:
                            action_errors.append({"type": _atype, "id": _aid, "error": str(_ae)})
                    # Strip ACTION tags from the visible reply text
                    clean_answer = action_pattern.sub("", answer).strip()

                    # ── Save conversation to database ────────────────────────────
                    conv_id = body.get("conversation_id")
                    if conv_id:
                        try:
                            add_gemini_message(int(conv_id), "user", message)
                            add_gemini_message(int(conv_id), "assistant", clean_answer)
                        except Exception as _se:
                            print(f"[GEMINI] Failed to save messages: {_se}")
                    else:
                        # Auto-create conversation if none specified
                        try:
                            _new_conv = create_gemini_conversation(message[:50])
                            conv_id = _new_conv["id"]
                            add_gemini_message(conv_id, "user", message)
                            add_gemini_message(conv_id, "assistant", clean_answer)
                        except Exception as _se:
                            print(f"[GEMINI] Failed to auto-create conversation: {_se}")

                    return send_json(self, {
                        "ok": True,
                        "response": clean_answer,
                        "model_used": model_name,
                        "actions_executed": executed_actions,
                        "action_errors": action_errors,
                        "dashboard_updated": len(executed_actions) > 0,
                        "conversation_id": conv_id
                    })

                except ImportError:
                    return send_json(self, {"ok": False, "error": "google-genai package not installed or outdated. Run: pip install --upgrade google-genai"}, 500)
                except Exception as e:
                    err_msg = str(e)
                    if "API_KEY" in err_msg.upper() or "api key" in err_msg.lower():
                        return send_json(self, {"ok": False, "error": f"Invalid API key: {err_msg}"}, 401)
                    # On 404 (model not found), auto-list available models
                    if "404" in err_msg or "NOT_FOUND" in err_msg or "not found" in err_msg.lower():
                        available_models = []
                        try:
                            from google import genai as _genai
                            _client = _genai.Client(api_key=api_key)
                            for m in _client.models.list():
                                name = m.name if hasattr(m, 'name') else str(m)
                                if name.startswith("models/"):
                                    name = name[len("models/"):]
                                methods = getattr(m, 'supported_generation_methods', []) or []
                                if not methods or 'generateContent' in methods:
                                    available_models.append(name)
                        except Exception:
                            pass
                        hint = f" Available models: {available_models}" if available_models else " Could not list models."
                        return send_json(self, {
                            "ok": False,
                            "error": f"Gemini error: {err_msg}",
                            "available_models": available_models,
                            "hint": f"Model '{model_name}' is not available. Try one of: {', '.join(available_models)}" if available_models else "Check your API key and google-genai version."
                        }, 500)
                    return send_json(self, {"ok": False, "error": f"Gemini error: {err_msg}"}, 500)

            # Alibaba batch analysis - analyse all displayed Alibaba products via Ollama
            if parsed.path == "/api/alibaba-analysis-batch":
                import urllib.request
                product_keys = body.get("product_keys", [])
                if not product_keys or not isinstance(product_keys, list):
                    return send_json(self, {"ok": False, "error": "Missing or invalid product_keys list"}, 400)

                conn = connect()
                placeholders = ",".join(["?"] * len(product_keys))
                rows = conn.execute(
                    f"SELECT product_key, title, price_text, min_price, supplier_name, country, years_text, "
                    f"min_order_text, shipping_text, delivery_text, sold_text, sold_count, rating, rating_text, "
                    f"review_count, badges_text, has_add_to_cart "
                    f"FROM alibaba_products WHERE product_key IN ({placeholders})",
                    product_keys
                ).fetchall()
                conn.close()

                if not rows:
                    return send_json(self, {"ok": False, "error": "No Alibaba products found for the given keys."}, 400)

                product_summaries = []
                analysed_count = 0
                for r in rows:
                    d = dict(r)
                    if not d.get("title") and not d.get("supplier_name"):
                        continue
                    analysed_count += 1
                    lines = []
                    lines.append(f"Product: {d.get('title') or 'Untitled'} (Key: {d.get('product_key', '')})")
                    if d.get("price_text"): lines.append(f"  Price: {d['price_text']}")
                    if d.get("min_price") is not None: lines.append(f"  Min price (numeric): {d['min_price']}")
                    if d.get("supplier_name"): lines.append(f"  Supplier: {d['supplier_name']}")
                    if d.get("country"): lines.append(f"  Country: {d['country']}")
                    if d.get("years_text"): lines.append(f"  Years on Alibaba: {d['years_text']}")
                    if d.get("min_order_text"): lines.append(f"  Min order: {d['min_order_text']}")
                    if d.get("shipping_text"): lines.append(f"  Shipping: {d['shipping_text']}")
                    if d.get("delivery_text"): lines.append(f"  Delivery: {d['delivery_text']}")
                    if d.get("sold_text"): lines.append(f"  Sold: {d['sold_text']}")
                    if d.get("sold_count") is not None: lines.append(f"  Sold count: {d['sold_count']}")
                    if d.get("rating_text"): lines.append(f"  Rating: {d['rating_text']}")
                    elif d.get("rating") is not None: lines.append(f"  Rating: {d['rating']}")
                    if d.get("review_count") is not None: lines.append(f"  Reviews: {d['review_count']}")
                    if d.get("badges_text"): lines.append(f"  Badges: {d['badges_text']}")
                    if d.get("has_add_to_cart"): lines.append(f"  Add to cart: Yes (ready to ship)")
                    product_summaries.append("\n".join(lines))

                if analysed_count == 0:
                    return send_json(self, {"ok": False, "error": "No products had enough data to analyse."}, 400)

                data_summary = "\n\n---\n\n".join(product_summaries)
                product_count_text = f"({analysed_count} products analysed)"

                ai_provider = body.get("ai_provider", "ollama")
                gemini_key = body.get("gemini_key", "").strip()
                if not gemini_key:
                    import os as _os
                    gemini_key = _os.environ.get("GEMINI_API_KEY", "").strip()

                alibaba_system_prompt = (
                    "You are an expert Alibaba sourcing analyst for an eBay reseller. "
                    "You receive product and supplier data for MULTIPLE Alibaba products at once. "
                    "Your job is to produce a structured, actionable sourcing report.\n\n"
                    "Format your response as:\n"
                    "1) Executive Summary \u2014 2-3 sentences about the overall supplier/product landscape.\n"
                    "2) Supplier Overview \u2014 group products by supplier. For each supplier: number of products, price range, average rating, total sold, years on Alibaba, country, and notable badges. Highlight the most established suppliers.\n"
                    "3) Price Comparison \u2014 rank products by price (lowest to highest). Identify the best value products (low price + good rating + good sold count). Note any significant price gaps between similar products.\n"
                    "4) Quality & Trust Signals \u2014 rank suppliers by trust indicators (years on Alibaba, rating, review count, badges like \"Verified\" or \"Trade Assurance\"). Flag any suppliers that look risky (low reviews, no badges, short history).\n"
                    "5) Shipping & Delivery \u2014 compare shipping options and delivery times. Highlight products with faster delivery or free shipping. Note MOQ (minimum order quantity) differences.\n"
                    "6) Top Recommendations \u2014 rank the top 5 products to source based on: best price-to-quality ratio, supplier reliability, shipping speed, and sales volume. For each, give the product name, supplier, price, and why it is recommended.\n"
                    "7) Risk Warnings \u2014 flag any products or suppliers with red flags (no ratings, very high MOQ, unknown country, no shipping info, etc.).\n\n"
                    "Rules:\n"
                    "- Be concise. Use bullet points.\n"
                    "- If data is insufficient for any section, say \"Insufficient data\" and move on.\n"
                    "- Do not repeat the raw data back to the user.\n"
                    "- Focus on actionable insights for someone deciding which Alibaba products to source for resale on eBay."
                )

                user_message = f"Here is the Alibaba product/supplier data {product_count_text}:\n\n{data_summary}\n\nAnalyse this data using the format specified."

                # --- GEMINI ---
                if ai_provider == "gemini":
                    if not gemini_key:
                        return send_json(self, {"ok": False, "error": "Gemini API key is required. Enter it in the field next to the AI provider selector, or set GEMINI_API_KEY environment variable."}, 400)
                    try:
                        gemini_payload = json.dumps({
                            "system_instruction": {"parts": [{"text": alibaba_system_prompt}]},
                            "contents": [{"role": "user", "parts": [{"text": user_message}]}],
                            "generationConfig": {"temperature": 0.7, "maxOutputTokens": 8192}
                        }).encode("utf-8")
                        _req_model = body.get("gemini_model", "").strip()
                        gemini_model = _req_model if _req_model else "gemini-3-flash-preview"
                        gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/{gemini_model}:generateContent?key={gemini_key}"
                        req = urllib.request.Request(
                            gemini_url,
                            data=gemini_payload,
                            headers={"Content-Type": "application/json"},
                            method="POST"
                        )
                        with urllib.request.urlopen(req, timeout=300) as resp:
                            gemini_result = json.loads(resp.read().decode("utf-8"))
                        answer = ""
                        candidates = gemini_result.get("candidates", [])
                        if candidates:
                            parts = candidates[0].get("content", {}).get("parts", [])
                            answer = "".join(p.get("text", "") for p in parts)
                        if not answer:
                            return send_json(self, {"ok": False, "error": f"Gemini returned an empty response. Full response: {json.dumps(gemini_result)[:500]}"}, 500)
                        return send_json(self, {
                            "ok": True,
                            "analysis": answer.strip(),
                            "model_used": gemini_model,
                            "analysed_count": analysed_count,
                            "skipped_count": 0
                        })
                    except urllib.error.HTTPError as e:
                        err_body = e.read().decode("utf-8") if hasattr(e, 'read') else ""
                        return send_json(self, {"ok": False, "error": f"Gemini HTTP error {e.code}: {err_body[:300]}"}, 502)
                    except urllib.error.URLError as e:
                        return send_json(self, {"ok": False, "error": f"Could not reach Gemini API: {str(e.reason)}"}, 502)
                    except Exception as e:
                        return send_json(self, {"ok": False, "error": f"Gemini request failed: {str(e)}"}, 500)

                # --- OLLAMA (default) ---
                _model, _available_models, _ollama_err = detect_ollama_model()
                if _ollama_err:
                    return send_json(self, {"ok": False, "error": _ollama_err}, 502)
                if not _model:
                    return send_json(self, {"ok": False, "error": "Ollama is running but no models are installed. Run: ollama pull <model_name>"}, 502)

                ollama_payload = json.dumps({
                    "model": _model,
                    "messages": [
                        {"role": "system", "content": alibaba_system_prompt},
                        {"role": "user", "content": user_message}
                    ],
                    "stream": False
                }).encode("utf-8")

                try:
                    req = urllib.request.Request(
                        "http://localhost:11434/api/chat",
                        data=ollama_payload,
                        headers={"Content-Type": "application/json"},
                        method="POST"
                    )
                    with urllib.request.urlopen(req, timeout=300) as resp:
                        ollama_result = json.loads(resp.read().decode("utf-8"))
                    answer = ollama_result.get("message", {}).get("content", "")
                    if not answer:
                        return send_json(self, {"ok": False, "error": f"Ollama model '{_model}' returned an empty response."}, 500)
                    return send_json(self, {
                        "ok": True,
                        "analysis": answer.strip(),
                        "model_used": _model,
                        "analysed_count": analysed_count,
                        "skipped_count": 0
                    })
                except urllib.error.HTTPError as e:
                    err_body = e.read().decode("utf-8") if hasattr(e, 'read') else ""
                    return send_json(self, {"ok": False, "error": f"Ollama HTTP error {e.code}: {err_body[:300]}"}, 502)
                except urllib.error.URLError as e:
                    return send_json(self, {"ok": False, "error": f"Could not reach Ollama: {str(e.reason)}"}, 502)
                except Exception as e:
                    return send_json(self, {"ok": False, "error": f"Ollama request failed: {str(e)}"}, 500)

            # Batch variation analysis - analyse variations for multiple products at once
            if parsed.path == "/api/variation-analysis-batch":
                import urllib.request
                item_ids = body.get("item_ids", [])
                if not item_ids or not isinstance(item_ids, list):
                    return send_json(self, {"ok": False, "error": "Missing or invalid item_ids list"}, 400)

                all_product_summaries = []
                analysed_count = 0
                skipped_count = 0

                for item_id in item_ids:
                    item_id = str(item_id).strip()
                    if not item_id:
                        continue
                    stats_data = variation_stats(item_id)
                    if not stats_data.get("variations"):
                        skipped_count += 1
                        continue

                    analysed_count += 1
                    lines = []
                    lines.append(f"Product: {stats_data.get('product_title') or item_id} (Item ID: {item_id})")
                    lines.append(f"  Total variations: {stats_data['total_variations']}")
                    lines.append(f"  Total sales: {stats_data['total_sales']}")
                    lines.append(f"  Total quantity sold: {stats_data['total_quantity']}")
                    lines.append(f"  Total revenue: \u00a3{stats_data['total_revenue']}")
                    lines.append("  Per-variation breakdown:")
                    for i, v in enumerate(stats_data["variations"], 1):
                        lines.append(
                            f"    {i}. {v['variation_name']}"
                            f" | Sales: {v['sales_count']}"
                            f" | Qty: {v['total_quantity']}"
                            f" | Revenue: \u00a3{v['total_revenue']}"
                            f" | First sale: {v.get('earliest_sale_text') or 'N/A'}"
                            f" | Last sale: {v.get('latest_sale_text') or 'N/A'}"
                        )
                    all_product_summaries.append("\n".join(lines))

                if analysed_count == 0:
                    return send_json(self, {"ok": False, "error": "No products had variation/sales data to analyse."}, 400)

                data_summary = "\n\n---\n\n".join(all_product_summaries)
                product_count_text = f"({analysed_count} products analysed, {skipped_count} skipped due to no data)"

                ai_provider = body.get("ai_provider", "ollama")
                gemini_key = body.get("gemini_key", "").strip()
                if not gemini_key:
                    import os as _os
                    gemini_key = _os.environ.get("GEMINI_API_KEY", "").strip()

                batch_system_prompt = (
                    "You are an expert eBay product sales analyst. You receive variation-level sales data for MULTIPLE products at once. "
                    "Your job is to produce a structured, actionable cross-product report.\n\n"
                    "Format your response as:\n"
                    "1) Executive Summary \u2014 2-3 sentences summarising the overall portfolio.\n"
                    "2) Per-Product Highlights \u2014 for each product, give: product name, total revenue, top variation by revenue, and one key insight. Keep it to 2-3 lines per product.\n"
                    "3) Cross-Product Comparison \u2014 rank all products by total revenue. Highlight which products are the best performers and which are underperforming.\n"
                    "4) Trend Analysis \u2014 based on first/last sale dates across products, identify overall trends (accelerating, steady, or declining).\n"
                    "5) Inventory Recommendations \u2014 state clearly: which product variations to restock, which to reduce, and which to drop. Give specific reasons.\n\n"
                    "Rules:\n"
                    "- Calculate all percentages yourself.\n"
                    "- Be concise. Use bullet points.\n"
                    "- If data is insufficient for any section, say \"Insufficient data\" and move on.\n"
                    "- Do not repeat the raw data back to the user.\n"
                    "- Organise by product, then by variation where relevant."
                )

                _user_msg = f"Here is the variation sales data for multiple eBay products {product_count_text}:\n\n{data_summary}\n\nAnalyse this data using the format specified."

                # --- GEMINI ---
                if ai_provider == "gemini":
                    if not gemini_key:
                        return send_json(self, {"ok": False, "error": "Gemini API key is required. Save it in the Gemini tab first."}, 400)
                    _req_model = body.get("gemini_model", "").strip()
                    gemini_model = _req_model if _req_model else "gemini-3-flash-preview"
                    try:
                        gemini_payload = json.dumps({
                            "system_instruction": {"parts": [{"text": batch_system_prompt}]},
                            "contents": [{"role": "user", "parts": [{"text": _user_msg}]}],
                            "generationConfig": {"temperature": 0.7, "maxOutputTokens": 8192}
                        }).encode("utf-8")
                        gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/{gemini_model}:generateContent?key={gemini_key}"
                        req = urllib.request.Request(gemini_url, data=gemini_payload, headers={"Content-Type": "application/json"}, method="POST")
                        with urllib.request.urlopen(req, timeout=300) as resp:
                            gemini_result = json.loads(resp.read().decode("utf-8"))
                        answer = ""
                        candidates = gemini_result.get("candidates", [])
                        if candidates:
                            parts = candidates[0].get("content", {}).get("parts", [])
                            answer = "".join(p.get("text", "") for p in parts)
                        if not answer:
                            return send_json(self, {"ok": False, "error": f"Gemini returned empty. Response: {json.dumps(gemini_result)[:500]}"}, 500)
                        return send_json(self, {"ok": True, "analysis": answer.strip(), "model_used": gemini_model, "analysed_count": analysed_count, "skipped_count": skipped_count})
                    except urllib.error.HTTPError as e:
                        err_body = e.read().decode("utf-8") if hasattr(e, 'read') else ""
                        return send_json(self, {"ok": False, "error": f"Gemini HTTP error {e.code}: {err_body[:300]}"}, 502)
                    except Exception as e:
                        return send_json(self, {"ok": False, "error": f"Gemini request failed: {str(e)}"}, 500)

                # --- OLLAMA (default) ---
                _model, _available_models, _ollama_err = detect_ollama_model()
                if _ollama_err:
                    return send_json(self, {"ok": False, "error": _ollama_err}, 502)
                if not _model:
                    return send_json(self, {"ok": False, "error": "Ollama is running but no models are installed. Run: ollama pull <model_name>"}, 502)

                ollama_payload = json.dumps({
                    "model": _model,
                    "messages": [
                        {"role": "system", "content": batch_system_prompt},
                        {"role": "user", "content": _user_msg}
                    ],
                    "stream": False
                }).encode("utf-8")

                try:
                    req = urllib.request.Request(
                        "http://localhost:11434/api/chat",
                        data=ollama_payload,
                        headers={"Content-Type": "application/json"},
                        method="POST"
                    )
                    with urllib.request.urlopen(req, timeout=300) as resp:
                        ollama_result = json.loads(resp.read().decode("utf-8"))
                    answer = ollama_result.get("message", {}).get("content", "")
                    if not answer:
                        return send_json(self, {"ok": False, "error": f"Ollama model '{_model}' returned an empty response."}, 500)
                    return send_json(self, {
                        "ok": True,
                        "analysis": answer.strip(),
                        "model_used": _model,
                        "analysed_count": analysed_count,
                        "skipped_count": skipped_count
                    })
                except urllib.error.HTTPError as e:
                    err_body = e.read().decode("utf-8") if hasattr(e, 'read') else ""
                    return send_json(self, {"ok": False, "error": f"Ollama HTTP error {e.code}: {err_body[:300]}"}, 502)
                except urllib.error.URLError as e:
                    return send_json(self, {"ok": False, "error": f"Could not reach Ollama: {str(e.reason)}"}, 502)
                except Exception as e:
                    return send_json(self, {"ok": False, "error": f"Ollama request failed: {str(e)}"}, 500)


            # Store best-seller analysis - analyse top store products via Ollama/Gemini
            if parsed.path == "/api/store-analysis-batch":
                import urllib.request
                item_ids = body.get("item_ids", [])
                if not item_ids or not isinstance(item_ids, list):
                    return send_json(self, {"ok": False, "error": "Missing or invalid item_ids list"}, 400)

                conn = connect()
                placeholders = ",".join(["?"] * len(item_ids))
                rows = conn.execute(
                    "SELECT p.item_id, p.title, sp.seller_username, "
                    "COALESCE(SUM(CASE WHEN DATE(s.sold_at) = DATE('now', '-1 day') THEN s.quantity ELSE 0 END), 0) AS sold_yesterday, "
                    "COALESCE(SUM(CASE WHEN DATE(s.sold_at) >= DATE('now', '-7 days') THEN s.quantity ELSE 0 END), 0) AS sold_7_days, "
                    "COALESCE(SUM(CASE WHEN DATE(s.sold_at) >= DATE('now', '-30 days') THEN s.quantity ELSE 0 END), 0) AS sold_30_days, "
                    "ROUND(COALESCE(SUM(CASE WHEN DATE(s.sold_at) >= DATE('now', '-30 days') THEN COALESCE(s.price,0) * s.quantity ELSE 0 END), 0), 2) AS revenue_30_days, "
                    "ROUND(COALESCE(SUM(COALESCE(s.price,0) * s.quantity), 0), 2) AS tracked_total_revenue, "
                    "COALESCE(SUM(s.quantity), 0) AS tracked_total_quantity, "
                    "MIN(DATE(s.sold_at)) AS tracked_first_sale_date, "
                    "MAX(DATE(s.sold_at)) AS tracked_last_sale_date "
                    "FROM store_products sp "
                    "JOIN products p ON p.item_id = sp.item_id "
                    "LEFT JOIN sales s ON s.item_id = p.item_id "
                    "WHERE p.item_id IN (" + placeholders + ") "
                    "GROUP BY sp.seller_username, p.item_id "
                    "ORDER BY sold_30_days DESC, tracked_total_revenue DESC",
                    item_ids
                ).fetchall()
                conn.close()

                if not rows:
                    return send_json(self, {"ok": False, "error": "No store products found for the given IDs."}, 400)

                product_summaries = []
                analysed_count = 0
                for r in rows:
                    d = dict(r)
                    if not d.get("title"):
                        continue
                    analysed_count += 1
                    lines = []
                    lines.append("Product: " + (d["title"] or "Untitled") + " (Item ID: " + d["item_id"] + ")")
                    if d.get("seller_username"): lines.append("  Seller: " + d["seller_username"])
                    lines.append("  Sold yesterday: " + str(d["sold_yesterday"]))
                    lines.append("  Sold 7 days: " + str(d["sold_7_days"]))
                    lines.append("  Sold 30 days: " + str(d["sold_30_days"]))
                    lines.append("  Revenue 30 days: \u00a3" + str(d["revenue_30_days"]))
                    lines.append("  Total revenue (tracked): \u00a3" + str(d["tracked_total_revenue"]))
                    lines.append("  Total quantity sold (tracked): " + str(d["tracked_total_quantity"]))
                    if d.get("tracked_first_sale_date"): lines.append("  First sale: " + str(d["tracked_first_sale_date"]))
                    if d.get("tracked_last_sale_date"): lines.append("  Last sale: " + str(d["tracked_last_sale_date"]))
                    product_summaries.append("\n".join(lines))

                if analysed_count == 0:
                    return send_json(self, {"ok": False, "error": "No products had enough data to analyse."}, 400)

                data_summary = "\n\n---\n\n".join(product_summaries)
                product_count_text = "(" + str(analysed_count) + " store products analysed)"

                ai_provider = body.get("ai_provider", "ollama")
                gemini_key = body.get("gemini_key", "").strip()
                if not gemini_key:
                    import os as _os
                    gemini_key = _os.environ.get("GEMINI_API_KEY", "").strip()

                store_system_prompt = (
                    "You are an expert eBay store performance analyst. You receive sales data for MULTIPLE products from an eBay store. "
                    "Your job is to produce a structured, actionable best-sellers report.\n\n"
                    "Format your response as:\n"
                    "1) Store Overview \u2014 2-3 sentences about the overall store performance. Total products, total revenue, and general health.\n"
                    "2) Top 10 Best Sellers \u2014 rank the top 10 products by 30-day sales volume. For each: product name, sold 30 days, sold 7 days, sold yesterday, revenue 30 days, and total revenue. Highlight which ones are accelerating (7-day > 30-day/4.3 average).\n"
                    "3) Revenue Champions \u2014 rank the top 5 products by total tracked revenue. For each: product name, total revenue, total quantity sold, and average price per unit.\n"
                    "4) Momentum Analysis \u2014 identify products where yesterday's sales or 7-day sales are disproportionately high compared to their 30-day average (accelerating). Also flag products that were selling well but have stalled (decelerating).\n"
                    "5) Slow Movers \u2014 list products with zero or very low sales in the last 30 days. Recommend whether to keep, discount, or delist each.\n"
                    "6) Pricing Insights \u2014 calculate the implied average selling price for each product (total revenue / total quantity). Identify which price ranges sell best.\n"
                    "7) Action Recommendations \u2014 give 5 specific, prioritised recommendations: which products to restock, which to promote more, which to bundle, which to drop, and any pricing changes.\n\n"
                    "Rules:\n"
                    "- Calculate all percentages and averages yourself.\n"
                    "- Be concise. Use bullet points.\n"
                    "- If data is insufficient for any section, say \"Insufficient data\" and move on.\n"
                    "- Do not repeat the raw data back to the user.\n"
                    "- Focus on actionable insights for someone managing an eBay store."
                )

                _user_msg = "Here is the store sales data " + product_count_text + ":\n\n" + data_summary + "\n\nAnalyse this data using the format specified."

                # --- GEMINI ---
                if ai_provider == "gemini":
                    if not gemini_key:
                        return send_json(self, {"ok": False, "error": "Gemini API key is required. Save it in the Gemini tab first."}, 400)
                    _req_model = body.get("gemini_model", "").strip()
                    gemini_model = _req_model if _req_model else "gemini-3-flash-preview"
                    try:
                        gemini_payload = json.dumps({
                            "system_instruction": {"parts": [{"text": store_system_prompt}]},
                            "contents": [{"role": "user", "parts": [{"text": _user_msg}]}],
                            "generationConfig": {"temperature": 0.7, "maxOutputTokens": 8192}
                        }).encode("utf-8")
                        gemini_url = "https://generativelanguage.googleapis.com/v1beta/models/" + gemini_model + ":generateContent?key=" + gemini_key
                        req = urllib.request.Request(gemini_url, data=gemini_payload, headers={"Content-Type": "application/json"}, method="POST")
                        with urllib.request.urlopen(req, timeout=300) as resp:
                            gemini_result = json.loads(resp.read().decode("utf-8"))
                        answer = ""
                        candidates = gemini_result.get("candidates", [])
                        if candidates:
                            parts = candidates[0].get("content", {}).get("parts", [])
                            answer = "".join(p.get("text", "") for p in parts)
                        if not answer:
                            return send_json(self, {"ok": False, "error": "Gemini returned empty. Response: " + json.dumps(gemini_result)[:500]}, 500)
                        return send_json(self, {"ok": True, "analysis": answer.strip(), "model_used": gemini_model, "analysed_count": analysed_count})
                    except urllib.error.HTTPError as e:
                        err_body = e.read().decode("utf-8") if hasattr(e, 'read') else ""
                        return send_json(self, {"ok": False, "error": "Gemini HTTP error " + str(e.code) + ": " + err_body[:300]}, 502)
                    except Exception as e:
                        return send_json(self, {"ok": False, "error": "Gemini request failed: " + str(e)}, 500)

                # --- OLLAMA (default) ---
                _model, _available_models, _ollama_err = detect_ollama_model()
                if _ollama_err:
                    return send_json(self, {"ok": False, "error": _ollama_err}, 502)
                if not _model:
                    return send_json(self, {"ok": False, "error": "Ollama is running but no models are installed. Run: ollama pull <model_name>"}, 502)

                ollama_payload = json.dumps({
                    "model": _model,
                    "messages": [
                        {"role": "system", "content": store_system_prompt},
                        {"role": "user", "content": _user_msg}
                    ],
                    "stream": False
                }).encode("utf-8")

                try:
                    req = urllib.request.Request(
                        "http://localhost:11434/api/chat",
                        data=ollama_payload,
                        headers={"Content-Type": "application/json"},
                        method="POST"
                    )
                    with urllib.request.urlopen(req, timeout=300) as resp:
                        ollama_result = json.loads(resp.read().decode("utf-8"))
                    answer = ollama_result.get("message", {}).get("content", "")
                    if not answer:
                        return send_json(self, {"ok": False, "error": "Ollama model '" + _model + "' returned an empty response."}, 500)
                    return send_json(self, {"ok": True, "analysis": answer.strip(), "model_used": _model, "analysed_count": analysed_count})
                except urllib.error.HTTPError as e:
                    err_body = e.read().decode("utf-8") if hasattr(e, 'read') else ""
                    return send_json(self, {"ok": False, "error": "Ollama HTTP error " + str(e.code) + ": " + err_body[:300]}, 502)
                except urllib.error.URLError as e:
                    return send_json(self, {"ok": False, "error": "Could not reach Ollama: " + str(e.reason)}, 502)
                except Exception as e:
                    return send_json(self, {"ok": False, "error": "Ollama request failed: " + str(e)}, 500)

            # ===== Chat endpoints =====
            # ── Gemini conversation creation ─────────────────────────────────
            if parsed.path == "/api/gemini/conversations":
                title = body.get("title", "New Chat").strip()
                result = create_gemini_conversation(title)
                return send_json(self, {"ok": True, "conversation_id": result["id"]})


            if parsed.path == "/api/chat/conversations":
                if self.command == "POST":
                    title = body.get("title", "").strip()
                    result = create_chat_conversation(title)
                    return send_json(self, {"ok": True, "conversation_id": result["id"], "title": result["title"]})
                if self.command == "DELETE":
                    conv_id = qs.get("conversation_id", [None])[0]
                    if conv_id:
                        delete_chat_conversation(int(conv_id))
                        return send_json(self, {"ok": True})
                    return send_json(self, {"ok": False, "error": "Missing conversation_id"}, 400)

            if parsed.path == "/api/chat/send":
                import urllib.request
                conv_id = body.get("conversation_id")
                message = body.get("message", "").strip()
                if not conv_id or not message:
                    return send_json(self, {"ok": False, "error": "Missing conversation_id or message"}, 400)
                add_chat_message(conv_id, "user", message)
                history = list_chat_messages(conv_id)
                dashboard_summary = get_dashboard_data_summary()

                # ── Auto-detect whatever Ollama model is currently installed ──
                _model, _available_models, _ollama_err = detect_ollama_model()
                if _ollama_err:
                    return send_json(self, {"ok": False, "error": _ollama_err}, 502)
                if not _model:
                    return send_json(self, {"ok": False, "error": "Ollama is running but no models are installed. Run: ollama pull <model_name>"}, 502)

                # ── System prompt with tool instructions ─────────────────────────
                chat_system = (
                    "You are an autonomous product research agent for an eBay reseller who sources products from Alibaba to sell on eBay UK.\n\n"

                    "=== YOUR PURPOSE ===\n"
                    "1. FIND BEST-SELLING EBAY PRODUCTS: Identify which products in the database have the highest sales volume (sold_30_days, sold_7_days, total_sold) and best revenue. The user wants to know WHAT sells well on eBay.\n"
                    "2. FIND BEST ALIBABA SUPPLIERS: For any product the user wants to sell, find the best supplier on Alibaba — best means: lowest price, high rating (4.0+), high sold_count, established (years on platform), low minimum order, ships to UK.\n"
                    "3. CONNECT PRODUCTS: The user links an eBay product (to understand demand/price) with an Alibaba product (the source/supplier). This is the core workflow.\n\n"

                    "=== DATABASE STRUCTURE ===\n"
                    "- eBay products: collected from eBay UK store pages and search results. Each has item_id, title, price, seller, and sales metrics (sold yesterday/7 days/30 days, total revenue). Sales come from Purchase History scans.\n"
                    "- Alibaba products: collected from Alibaba.com search pages. Each has product_key, title, price range, supplier name, country, years on platform, minimum order, rating, sold_count, shipping info.\n"
                    "- Dashboard: user-selected eBay + Alibaba products the user is actively researching. eBay and Alibaba products can be 'connected' as pairs (one eBay listing → one Alibaba supplier).\n"
                    "- Sales: individual purchase history records with buyer, quantity, price, date. Used to calculate daily/weekly/monthly sales velocity.\n\n"

                    "=== HOW TO HELP THE USER ===\n"
                    "- If user asks 'what sells well?' or 'best product?' → use search_ebay_products, look at sold_30_days and total_revenue, recommend top products.\n"
                    "- If user asks 'find a supplier' or 'Alibaba for X' → use search_alibaba_products, rank by: rating DESC, sold_count DESC, price ASC, filter out suppliers with 0 rating.\n"
                    "- If user asks 'add to dashboard' → search first, then call the select tool.\n"
                    "- Always work with ACTUAL DATA from the tools — never make up product names or prices.\n\n"

                    "=== STRICT RULES ===\n"
                    "1. ALWAYS call tools when the user asks to find, search, pick, add, or analyse products. Never just describe what you would do.\n"
                    "2. To add eBay product to dashboard: call search_ebay_products → pick best match → call select_ebay_to_dashboard(item_id).\n"
                    "3. To add Alibaba product to dashboard: call search_alibaba_products → pick best supplier → call select_alibaba_to_dashboard(product_key).\n"
                    "4. Respond in the SAME language as the user. Persian → Persian. English → English.\n"
                    "5. When recommending a supplier, always mention: price, rating, sold count, years on Alibaba, and minimum order.\n"
                    "6. After executing tools, confirm to the user what you did (product name, price, etc.).\n\n"

                    "Current dashboard data:\n" + dashboard_summary
                )
                ollama_messages = [{"role": "system", "content": chat_system}]
                for m in history:
                    ollama_messages.append({"role": m["role"], "content": m["content"]})

                # ── Tool definitions ───────────────────────────────────────────
                tools = [
                    {
                        "type": "function",
                        "function": {
                            "name": "search_ebay_products",
                            "description": "Search eBay products in the database by keyword in title. Returns matching products with item_id, title, price, seller.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "query": {"type": "string", "description": "Search keyword to find in product titles"}
                                },
                                "required": ["query"]
                            }
                        }
                    },
                    {
                        "type": "function",
                        "function": {
                            "name": "search_alibaba_products",
                            "description": "Search Alibaba products in the database by keyword. Results are sorted by cheapest price first. Returns product_key, title, price, supplier, country, sold_count, rating.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "query": {"type": "string", "description": "Search keyword to find in product titles"}
                                },
                                "required": ["query"]
                            }
                        }
                    },
                    {
                        "type": "function",
                        "function": {
                            "name": "select_alibaba_to_dashboard",
                            "description": "Add an Alibaba product to the dashboard by its product_key. Use this after searching to add a specific product.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "product_key": {"type": "string", "description": "The product_key from the search_alibaba_products results"}
                                },
                                "required": ["product_key"]
                            }
                        }
                    },
                    {
                        "type": "function",
                        "function": {
                            "name": "select_ebay_to_dashboard",
                            "description": "Add an eBay product to the dashboard by its item_id. Use this after searching to add a specific product.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "item_id": {"type": "string", "description": "The item_id from the search_ebay_products results"}
                                },
                                "required": ["item_id"]
                            }
                        }
                    }
                ]

                # ── Tool execution dispatcher ──────────────────────────────────
                def execute_tool(func_name, args):
                    """Run a tool function and return its result as a dict."""
                    try:
                        if func_name == "search_ebay_products":
                            results = search_ebay_products(args.get("query", ""))
                            return {"ok": True, "count": len(results), "products": results}
                        elif func_name == "search_alibaba_products":
                            results = search_alibaba_products(args.get("query", ""))
                            return {"ok": True, "count": len(results), "products": results}
                        elif func_name == "select_alibaba_to_dashboard":
                            pk = args.get("product_key", "")
                            result = select_dashboard_alibaba_product(pk)
                            return result
                        elif func_name == "select_ebay_to_dashboard":
                            iid = args.get("item_id", "")
                            result = select_dashboard_product(iid)
                            return result
                        else:
                            return {"ok": False, "error": f"Unknown function: {func_name}"}
                    except Exception as ex:
                        return {"ok": False, "error": str(ex)}

                # ── Conversation loop with tool calls ─────────────────────────
                max_tool_rounds = 6
                answer = ""
                try:
                    for _round in range(max_tool_rounds + 1):
                        ollama_payload = json.dumps({
                            "model": _model,
                            "messages": ollama_messages,
                            "stream": False,
                            "tools": tools,
                            "options": {"temperature": 0.1}
                        }).encode("utf-8")
                        req = urllib.request.Request(
                            "http://localhost:11434/api/chat",
                            data=ollama_payload,
                            headers={"Content-Type": "application/json"},
                            method="POST"
                        )
                        with urllib.request.urlopen(req, timeout=120) as resp:
                            ollama_result = json.loads(resp.read().decode("utf-8"))

                        msg = ollama_result.get("message", {})
                        tool_calls = msg.get("tool_calls")
                        # Debug log — print raw response to server console
                        import sys
                        print(f"[OLLAMA ROUND {_round}] model={_model} | tool_calls={bool(tool_calls)} | content_len={len(msg.get('content',''))}", file=sys.stderr, flush=True)
                        if tool_calls:
                            for _tc in tool_calls:
                                print(f"  -> tool: {_tc.get('function',{}).get('name','')} args={_tc.get('function',{}).get('arguments','')}", file=sys.stderr, flush=True)

                        if tool_calls:
                            # Ollama wants to call one or more tools
                            ollama_messages.append(msg)
                            for tc in tool_calls:
                                fn = tc.get("function", {}).get("name", "")
                                fn_args = tc.get("function", {}).get("arguments", {})
                                if isinstance(fn_args, str):
                                    try:
                                        fn_args = json.loads(fn_args)
                                    except Exception:
                                        fn_args = {}
                                tool_result = execute_tool(fn, fn_args)
                                ollama_messages.append({
                                    "role": "tool",
                                    "content": json.dumps(tool_result, ensure_ascii=False, default=str)
                                })
                            continue  # send back to Ollama for next step

                        # No tool calls — this is the final answer
                        answer = msg.get("content", "")
                        break

                    if not answer:
                        # Check if any tool actions were taken and summarize them
                        tool_msgs = [m for m in ollama_messages if m.get("role") == "tool"]
                        if tool_msgs:
                            answer = f"Actions completed. {len(tool_msgs)} tool(s) were executed successfully."
                        else:
                            answer = "I completed the requested actions but could not generate a text response."

                    add_chat_message(conv_id, "assistant", answer.strip())
                    return send_json(self, {"ok": True, "response": answer.strip(), "model_used": _model})

                except urllib.error.HTTPError as e:
                    err_body = e.read().decode("utf-8") if hasattr(e, 'read') else ""
                    return send_json(self, {"ok": False, "error": f"Ollama HTTP error {e.code}: {err_body[:300]}"}, 502)
                except urllib.error.URLError as e:
                    return send_json(self, {"ok": False, "error": f"Could not reach Ollama: {str(e.reason)}"}, 502)
                except Exception as e:
                    traceback.print_exc()
                    return send_json(self, {"ok": False, "error": f"Ollama request failed: {str(e)}"}, 500)

            if parsed.path == "/api/gemini/save-key":
                key = body.get("gemini_key", "").strip()
                if not key:
                    return send_json(self, {"ok": False, "error": "No key provided"}, 400)
                save_gemini_key(key)
                return send_json(self, {"ok": True, "message": "Gemini key saved to server"})

            if parsed.path == "/api/fittings/extract":
                return send_json(self, handle_fitting_extraction(body))

            if parsed.path == "/api/fittings":
                return send_json(self, add_fitting(body), 201)
            if parsed.path == "/api/fittings/from-product":
                iid = body.get("item_id")
                if not iid: return send_json(self, {"ok": False, "error": "Missing item_id"}, 400)
                return send_json(self, add_fitting_from_product(iid, body.get("overrides")), 201)
            if parsed.path == "/api/fittings/update":
                fid = body.get("id")
                if not fid: return send_json(self, {"ok": False, "error": "Missing id"}, 400)
                return send_json(self, update_fitting(int(fid), body))

            return send_json(self, {"ok": False, "error": f"Unknown POST route: {parsed.path}"}, 404)
        except Exception as e:
            traceback.print_exc()
            return send_json(self, {"ok": False, "error": "Server failed while handling POST request", "detail": str(e)}, 500)

    def _send_file(self, file_path, content_type, download_name):
        data = Path(file_path).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def dashboard_html():
    """Return the unified dashboard.

    The Store Tracker and eBay Search tabs share the same card grid/stat area.
    The left panel changes meaning by mode:
    - Store Tracker: left list = seller/store list.
    - eBay Search: left list = inferred search/product groups.
    """
    return r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/><title>Unified Product Research Dashboard</title>
<style>
:root{--bg:#0b1220;--side:#020617;--panel:#111827;--text:#e5e7eb;--muted:#94a3b8;--border:#293449;--brand:#2563eb}*{box-sizing:border-box;font-weight:400!important}body{margin:0;font-family:Arial,sans-serif;background:var(--bg);color:var(--text)}.layout{display:grid;grid-template-columns:300px 1fr;min-height:100vh}.layout.dashboardMode{grid-template-columns:300px 1fr}.sidebar{background:var(--side);border-right:1px solid var(--border);padding:18px;height:100vh;position:sticky;top:0;overflow:auto}.brand{font-size:20px;margin-bottom:8px}.sideNote{color:var(--muted);font-size:12px;line-height:1.6;margin-bottom:16px}.sideTitle{font-size:13px;color:#fff;margin-bottom:8px}.row{display:grid;grid-template-columns:1fr 40px;gap:8px;margin-bottom:8px}.rowBtn{background:#1f2937;color:#fff;border:1px solid #374151;border-radius:14px;padding:11px;cursor:pointer;text-align:left}.rowBtn.active{background:var(--brand);border-color:#60a5fa}.rowName{direction:ltr;text-align:left}.rowMeta{color:#d1d5db;font-size:12px;margin-top:5px;direction:ltr}.delBtn{background:#7f1d1d;color:white;border:1px solid #991b1b;border-radius:14px;cursor:pointer;font-size:17px}.row.mergeRow{grid-template-columns:34px 1fr 40px}.mergePick{background:#111827;color:#cbd5e1;border:1px solid #334155;border-radius:12px;cursor:pointer}.mergePick.active{background:#2563eb;color:#fff;border-color:#60a5fa}.mergeControls{border:1px solid #334155;background:#0f172a;border-radius:14px;padding:10px;margin-bottom:10px}.mergeControls .mergeNote{color:var(--muted);font-size:11px;line-height:1.4;margin-bottom:8px}.mergeBtn{width:100%;background:#1f2937;color:#dbeafe;border:1px solid #334155;border-radius:12px;padding:9px;margin-top:6px;cursor:pointer}.mergeBtn.primary{background:#2563eb;color:#fff;border-color:#60a5fa}.rowBtn.mergeSelected{border-color:#60a5fa}.main{min-width:0}.topbar{height:78px;background:#111827;border-bottom:1px solid var(--border);display:flex;align-items:center;padding:0 24px;position:sticky;top:0;z-index:10}.title h1{font-size:20px;margin:0}.title p{margin:5px 0 0;color:var(--muted);font-size:13px}.tabs{display:flex;gap:8px;justify-content:center;position:fixed;left:50%;top:39px;transform:translate(-50%,-50%);z-index:11}.tabBtn{background:#1f2937;color:#cbd5e1;border:1px solid #334155;border-radius:12px;padding:9px 14px;cursor:pointer}.tabBtn.active{background:var(--brand);color:#fff;border-color:#60a5fa}.wrap{max-width:1280px;margin:0 auto;padding:24px}.hidden{display:none!important}.fixedControls{position:fixed;top:78px;left:300px;right:0;z-index:20;background:var(--bg);padding:10px 24px 14px;border-bottom:1px solid rgba(41,52,73,.7)}#work{padding-top:128px}#alibaba{padding-top:128px}#fittings{padding-top:128px}.fixedControls .stats,.fixedControls .toolbar{max-width:1280px;margin-left:auto;margin-right:auto}.stats{display:grid;grid-template-columns:repeat(10,minmax(0,1fr));gap:10px;margin-bottom:12px}.stat{background:var(--panel);border:1px solid #334155;border-radius:12px;padding:10px;min-height:58px}.stat .label{color:var(--muted);font-size:11px;margin-bottom:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.stat .value{font-size:18px;direction:ltr;text-align:right}.toolbar{background:var(--panel);border:1px solid #334155;border-radius:16px;padding:12px;display:grid;grid-template-columns:1fr 190px 190px;gap:10px}input,select{border:1px solid #d1d5db;border-radius:10px;padding:10px 12px;font-size:14px}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:18px}.card{background:var(--panel);border:1px solid #334155;border-radius:18px;overflow:hidden;display:flex;flex-direction:column}.imageBox{height:210px;background:#1e293b;display:flex;align-items:center;justify-content:center}.imageBox img{width:100%;height:100%;object-fit:contain}.body{padding:13px;flex:1;display:flex;flex-direction:column}.pTitle{font-size:14px;line-height:1.35;height:54px;overflow:hidden}.topLine{display:flex;justify-content:space-between;gap:8px;margin-top:10px}.sellerTag{font-size:11px;background:#263244;border:1px solid #3c4b63;border-radius:999px;padding:3px 8px}.price{font-size:14px}.metric{display:flex;justify-content:space-between;gap:8px;margin-top:9px;font-size:12px}.meta{color:var(--muted);font-size:12px;margin-top:7px;line-height:1.35}.metaArea{min-height:72px;height:auto;overflow:hidden}.sales{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:auto;padding-top:12px}.pill{background:#0f172a;border:1px solid #334155;border-radius:12px;padding:8px;text-align:center}.pill.hot{background:#065f46;border-color:#10b981}.pill strong{display:block;font-size:18px}.pill span{font-size:10px;color:#cbd5e1}.footer{display:flex;border-top:1px solid #334155;margin-top:auto}.footer a,.footer button{flex:1;min-height:34px;padding:0 6px;display:flex;align-items:center;justify-content:center;text-align:center;color:#bfdbfe;text-decoration:none;font-size:11px;line-height:1.15;background:none;border:0;border-left:1px solid #334155;cursor:pointer;font-family:Arial,sans-serif}.footer a:first-child{border-left:0}.footer a:hover,.footer button:hover{color:#fff;background:#1f2937}.footer .calcDashboardBtn{color:#fff;background:#2563eb}.footer .calcDashboardBtn:hover{background:#1d4ed8}.footer button:disabled{color:#64748b;cursor:not-allowed;background:#111827}.empty,.placeholder{background:var(--panel);border:1px dashed #475569;border-radius:18px;padding:34px;text-align:center;color:var(--muted)}.layout.withFilters{grid-template-columns:300px 1fr 340px}.layout.withFilters .fixedControls{right:340px}.filterPanel{background:var(--side);border-left:1px solid var(--border);padding:18px;height:100vh;position:sticky;top:0;overflow:auto}.filterHead{font-size:20px;margin-bottom:6px}.filterNote{color:var(--muted);font-size:12px;line-height:1.55;margin-bottom:14px}.filterSection{border-top:1px solid var(--border);padding-top:14px;margin-top:14px}.filterTitle{font-size:13px;color:var(--muted);margin-bottom:8px;text-align:right}.filterBtn{width:100%;display:grid;grid-template-columns:46px 1fr;gap:8px;align-items:center;background:#111827;color:var(--text);border:1px solid #334155;border-radius:13px;padding:9px 10px;margin:7px 0;cursor:pointer;text-align:right}.filterBtn.active{background:var(--brand);border-color:#60a5fa}.filterCount{text-align:left;color:#93c5fd;font-size:12px}.filterLabel{overflow:hidden;text-overflow:ellipsis}.clearFilters{width:100%;background:#1f2937;color:#dbeafe;border:1px solid #334155;border-radius:12px;padding:10px;margin-top:8px;cursor:pointer}.alibabaFacts{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:10px}.factBox{background:#0f172a;border:1px solid #334155;border-radius:10px;padding:7px;text-align:right;font-size:11px;min-height:39px}.factBox span{display:block;color:var(--muted);font-size:10px}.factInput{width:100%;margin-top:3px;background:transparent!important;color:var(--text)!important;border:0!important;border-bottom:1px solid #334155!important;border-radius:0!important;padding:1px 0!important;text-align:right;font-size:11px;outline:none}.factInput:focus{border-bottom-color:#60a5fa!important}.factInput.saving{border-bottom-color:#f59e0b!important}.smartTags{margin-top:9px;display:flex;flex-wrap:wrap;gap:6px}.fitBtn{background:#1f2937;color:#dbeafe;border:1px solid #334155;border-radius:10px;padding:9px 14px;cursor:pointer;font-size:13px}.fitBtn:hover{background:#374151}
.smartTag{border:1px solid #334155;background:#0f172a;border-radius:999px;padding:4px 7px;font-size:10px;color:#cbd5e1}.rawDataBox{border:1px solid #22c55e;border-radius:10px;padding:8px;margin-top:10px;background:#0a1a0a}.rawRow{display:flex;justify-content:space-between;align-items:flex-start;gap:6px;padding:3px 0;border-bottom:1px solid #1a2e1a}.rawRow:last-child{border-bottom:0}.rawLabel{color:#4ade80;font-size:12px;min-width:90px;flex-shrink:0;font-family:monospace}.rawVal{color:#e5e7eb;font-size:13px;text-align:right;word-break:break-all}.rawEmpty{color:#4b5563;font-size:11px}
.aiWrap{max-width:900px;margin:0 auto;padding:20px}.aiHeader{margin-bottom:20px}.aiTitleRow{display:flex;align-items:center;gap:12px}.aiTitleRow h2{margin:0;font-size:22px}.aiBadge{background:#1f2937;border:1px solid #334155;border-radius:999px;padding:4px 12px;font-size:12px;color:#93c5fd}.aiSubtitle{color:var(--muted);font-size:13px;margin-top:6px}.aiQuickRow{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px}.aiQuickBtn{background:#1f2937;color:#dbeafe;border:1px solid #334155;border-radius:12px;padding:9px 14px;cursor:pointer;font-size:13px}.aiQuickBtn:hover{background:#2563eb;color:#fff;border-color:#60a5fa}.aiInputRow{display:grid;grid-template-columns:1fr 160px auto;gap:10px;margin-bottom:10px}.aiInputRow input,.aiInputRow select{border:1px solid #334155;background:#111827;color:var(--text);border-radius:12px;padding:12px 14px;font-size:14px}.aiInputRow input::placeholder{color:#64748b}.aiAskBtn{background:var(--brand);color:#fff;border:1px solid #60a5fa;border-radius:12px;padding:0 24px;cursor:pointer;font-size:14px;white-space:nowrap}.aiAskBtn:hover{background:#1d4ed8}.aiAskBtn:disabled{background:#374151;color:#64748b;cursor:not-allowed}.aiKeyRow{display:flex;align-items:center;gap:10px;margin-bottom:20px}.aiKeyRow input{flex:1;border:1px solid #334155;background:#111827;color:var(--text);border-radius:10px;padding:10px 14px;font-size:13px}.aiKeyRow input::placeholder{color:#64748b}.aiKeyHint{color:var(--muted);font-size:11px;white-space:nowrap}.aiResult{background:var(--panel);border:1px solid #334155;border-radius:16px;padding:24px;min-height:200px;overflow:auto}.aiPlaceholder{color:var(--muted);text-align:center;padding:40px;font-size:14px}.aiAnswer{white-space:pre-wrap;line-height:1.7;font-size:14px}.aiError{color:#fca5a5;background:#7f1d1d;border:1px solid #991b1b;border-radius:12px;padding:14px}.aiLoading{display:flex;align-items:center;gap:10px;color:var(--muted);padding:20px}.aiSpin{width:20px;height:20px;border:2px solid #334155;border-top-color:#60a5fa;border-radius:50%;animation:aiSpin 0.8s linear infinite}@keyframes aiSpin{to{transform:rotate(360deg)}}
.chatPage{display:flex;flex-direction:column;height:calc(100vh - 140px);padding:0;overflow:hidden}.sideChatNav{display:none;flex-direction:column;height:calc(100vh - 36px);overflow:hidden;padding-top:8px}.sideChatView{display:flex;flex-direction:column;height:100vh;padding:14px;overflow:hidden}.chatHeader{display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-shrink:0}.chatNewBtn{background:#2563eb;color:#fff;border:0;border-radius:10px;padding:8px 14px;cursor:pointer;font-size:13px;font-weight:600;white-space:nowrap}.chatNewBtn:hover{background:#1d4ed8}.chatConvList{max-height:110px;overflow-y:auto;margin-bottom:8px;border-bottom:1px solid var(--border);padding-bottom:6px;flex-shrink:0}.chatConvItem{display:flex;align-items:center;justify-content:space-between;padding:8px 10px;border-radius:10px;cursor:pointer;margin-bottom:4px;font-size:12px}.chatConvItem:hover{background:#1f2937}.chatConvItem.active{background:#1e3a5f;border:1px solid #3b82f6}.chatConvTitle{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#dbeafe}.chatConvDel{background:none;border:0;color:#ef4444;cursor:pointer;font-size:16px;padding:0 4px;flex-shrink:0}.chatArea{flex:1;overflow-y:auto;border:1px solid #334155;border-radius:14px;background:#0a0f1c;padding:12px;min-height:0}.chatEmpty{color:var(--muted);text-align:center;padding:40px 20px;font-size:13px}.chatMsg{margin-bottom:10px;padding:10px 14px;border-radius:14px;font-size:13px;line-height:1.6;white-space:pre-wrap;word-break:break-word;unicode-bidi:plaintext;text-align:start}.chatMsg.user{background:#1e3a5f;border:1px solid #3b82f6;color:#dbeafe;margin-left:20px}.chatMsg.ai{background:#0f172a;border:1px solid #334155;color:#e5e7eb;margin-right:20px}.chatMsg.sys{background:#1c1917;border:1px solid #57534e;color:#fbbf24;font-size:11px}.chatInputRow{display:flex;gap:8px;margin-top:8px;align-items:center;flex-shrink:0}.chatInputRow textarea{flex:1;background:#111827;color:#e5e7eb;border:1px solid #334155;border-radius:12px;padding:10px 12px;font-size:13px;resize:none;height:80px;max-height:160px;overflow-y:auto;line-height:1.4;unicode-bidi:plaintext;font-family:Arial,sans-serif;display:block}.chatInputRow textarea::placeholder{color:#64748b}.chatSendBtn{background:#2563eb;color:#fff;border:1px solid #60a5fa;border-radius:12px;padding:0 16px;cursor:pointer;font-size:14px;white-space:nowrap;height:40px;flex-shrink:0}.chatSendBtn:hover{background:#1d4ed8}.chatSendBtn:disabled{background:#374151;color:#64748b;cursor:not-allowed}.chatLoading{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:13px;padding:10px 14px}.chatSpin{width:16px;height:16px;border:2px solid #334155;border-top-color:#60a5fa;border-radius:50%;animation:varSpin .8s linear infinite}@keyframes varSpin{to{transform:rotate(360deg)}}@media(max-width:1050px){.layout{grid-template-columns:1fr}.sidebar{position:relative;height:auto}.topbar{height:auto;grid-template-columns:1fr;padding:14px;gap:12px}.tabs{justify-content:flex-start;flex-wrap:wrap}.fixedControls{position:static;padding:0;border-bottom:0}#work{padding-top:0}.stats{grid-template-columns:repeat(2,1fr)}.toolbar{grid-template-columns:1fr}.layout.withFilters{grid-template-columns:1fr}.filterPanel{position:relative;height:auto;border-left:0;border-top:1px solid var(--border)}.layout.withFilters .fixedControls{right:0}}
.dashboardLayout{display:block}.dashboardTop{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:14px}.dashboardTop .emptyPairs{max-width:520px}.calcHint{color:var(--muted);font-size:12px;line-height:1.5}.pairCard{border:1px solid #334155;background:#0f172a;border-radius:14px;padding:10px;margin-bottom:10px}.pairTitle{font-size:13px;color:#fff;margin-bottom:8px}.pairLine{font-size:12px;color:#cbd5e1;line-height:1.4;margin-bottom:4px}.connectedPreview{display:grid;grid-template-columns:1fr 34px 1fr;gap:12px;align-items:stretch;margin:10px 0}.miniProductBox{border:1px solid #334155;background:#0f172a;border-radius:12px;padding:10px;min-width:0}.linkArrows{display:flex;flex-direction:column;align-items:center;justify-content:center;color:#60a5fa;font-size:18px;gap:4px}.miniImgRow{display:grid;grid-template-columns:62px 1fr;gap:10px;align-items:center;margin:8px 0}.miniImgRow img{width:62px;height:62px;object-fit:cover;border-radius:10px;background:#020617}.calcBtn,.connectDashboardBtn{background:#2563eb;color:#fff;border:0;border-radius:10px;padding:8px 10px;cursor:pointer}.connectDashboardBtn.waiting{background:#f59e0b}.connectDashboardBtn.connected{background:#166534}.unlinkBtn{background:#7f1d1d;color:#fff;border:0;border-radius:10px;padding:8px 10px;cursor:pointer;margin-left:6px}.calcForm{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-top:12px}.calcForm label,.priceInline label{font-size:12px;color:#cbd5e1}.priceInline{margin-top:8px}.priceInline input,.calcForm input{width:100%;background:#020617;color:#e5e7eb;border:1px solid #334155;border-radius:10px;padding:8px}.postageInputRow{display:grid;grid-template-columns:minmax(0,1fr) 34px 34px;gap:6px;align-items:center}.calcForm .postageInputRow input{min-width:0}.unitBtn{height:34px;background:#1f2937;color:#cbd5e1;border:1px solid #334155;border-radius:10px;cursor:pointer;font-size:12px;padding:0}.unitBtn.active{background:#2563eb;color:#fff;border-color:#60a5fa}.calcResult{background:#052e16;border:1px solid #166534;border-radius:14px;padding:14px;margin-top:12px}.calcProfitTop{text-align:center;margin-bottom:14px}.calcResult .big{font-size:26px;color:#86efac;text-align:center;margin-top:4px}.calcDetails{color:#cbd5e1;font-size:12px;line-height:1.7;margin-top:8px;max-width:360px}.emptyPairs{color:#94a3b8;font-size:12px;line-height:1.5;border:1px dashed #334155;border-radius:12px;padding:10px}.modalOverlay{position:fixed;inset:0;background:rgba(2,6,23,.72);display:flex;align-items:center;justify-content:center;padding:22px;z-index:50}.modalOverlay.hidden{display:none}.calcModal{width:min(980px,96vw);max-height:92vh;overflow:auto;background:#111827;border:1px solid var(--border);border-radius:18px;padding:16px;box-shadow:0 24px 80px rgba(0,0,0,.45)}.modalHead{display:flex;align-items:flex-start;justify-content:space-between;gap:14px;margin-bottom:10px}.modalHead h2{font-size:18px;margin:0 0 4px}.modalClose{background:#1f2937;color:#fff;border:1px solid #334155;border-radius:10px;padding:8px 10px;cursor:pointer}.modalPairList{display:flex;gap:8px;flex-wrap:wrap;align-items:center}@media(max-width:900px){.connectedPreview,.calcForm{grid-template-columns:1fr}.linkArrows{flex-direction:row}.calcModal{width:96vw}}

.ollamaBadge{display:flex;align-items:center;padding:0;border-radius:50%;cursor:default;position:fixed;top:32px;right:20px;z-index:100;flex-shrink:0}.ollamaDot{width:12px;height:12px;border-radius:50%;background:#f59e0b;box-shadow:0 0 8px #f59e0b;transition:all .3s;display:block}.ollamaBadge.ok{background:rgba(34,197,94,.15)}.ollamaBadge.ok .ollamaDot{background:#22c55e;box-shadow:0 0 8px #22c55e}.ollamaBadge.fail{background:rgba(239,68,68,.15)}.ollamaBadge.fail .ollamaDot{background:#ef4444;box-shadow:0 0 6px #ef4444}.ollamaBadge .ollamaModels{color:#94a3b8;font-size:10px}.ollamaBadge.ok .ollamaModels{color:#22c55e}.ollamaBadge.fail .ollamaModels{color:#ef4444}.varBtn{color:#60a5fa!important}.varBtn:hover{background:#1e3a5f!important}
.varModal{width:min(820px,95vw);max-height:90vh;overflow:auto;background:#111827;border:1px solid var(--border);border-radius:18px;padding:18px;box-shadow:0 24px 80px rgba(0,0,0,.45)}
.varSummary{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:14px 0}
.varSummary .stat{background:#0f172a;border:1px solid #334155;border-radius:12px;padding:10px;text-align:center}
.varSummary .stat .label{color:var(--muted);font-size:11px;margin-bottom:4px}
.varSummary .stat .value{font-size:20px;color:#93c5fd}
.varTable{width:100%;border-collapse:collapse;margin:14px 0;font-size:13px}
.varTable th{background:#1e293b;color:#cbd5e1;padding:10px 8px;text-align:left;font-size:12px;border-bottom:1px solid #334155}
.varTable td{padding:9px 8px;border-bottom:1px solid #1e293b;color:#e5e7eb}
.varTable tr:hover td{background:#0f172a}
.varTable .num{text-align:right;direction:ltr}
.varTable .barCell{width:120px}
.varBar{height:8px;background:#1e293b;border-radius:4px;overflow:hidden}
.varBarFill{height:100%;background:#2563eb;border-radius:4px}
.varAiSection{margin-top:0}.varAiSection:empty{display:none}
.varAiBtn{background:#2563eb;color:#fff;border:0;border-radius:12px;padding:11px 20px;cursor:pointer;font-size:14px;font-weight:600}.modalHeadActions{display:flex;align-items:center;gap:10px;flex-shrink:0}
.varAiBtn:hover{background:#1d4ed8}
.varAiBtn:disabled{background:#1f2937;color:#64748b;cursor:not-allowed}
.varAiResult{background:#0f172a;border:1px solid #334155;border-radius:12px;padding:14px;margin-top:12px;font-size:13px;line-height:1.7;color:#cbd5e1;white-space:pre-wrap;display:none}
.varAiResult.show{display:block}
.varAiLoading{display:none;align-items:center;gap:10px;color:var(--muted);font-size:13px;margin-top:12px;display:none}
.varAiLoading.show{display:flex}
.varSpin{width:18px;height:18px;border:2px solid #334155;border-top-color:#60a5fa;border-radius:50%;animation:varSpin .8s linear infinite}@keyframes varSpin{to{transform:rotate(360deg)}}
.batchVarBtn{background:#7c3aed;color:#fff;border:0;border-radius:10px;padding:8px 14px;cursor:pointer;font-size:13px;font-weight:600;white-space:nowrap;margin-left:auto}.batchVarBtn:hover{background:#6d28d9}.batchVarBtn:disabled{background:#1f2937;color:#64748b;cursor:not-allowed}.varMiniBtn{display:inline-flex;align-items:center;justify-content:center;border:0;border-radius:6px;padding:3px 7px;cursor:pointer;font-size:11px;font-weight:700;white-space:nowrap;margin-left:3px;height:24px;line-height:1}.varMiniOl{background:#1e3a5f;color:#93c5fd}.varMiniOl:hover{background:#1d4ed8;color:#fff}.varMiniGe{background:#1a2e1a;color:#4ade80}.varMiniGe:hover{background:#16a34a;color:#fff}
.batchModalOverlay{position:fixed;inset:0;background:rgba(2,6,23,.72);display:flex;align-items:center;justify-content:center;padding:22px;z-index:50}.batchModalOverlay.hidden{display:none}
.batchModal{width:min(900px,95vw);max-height:90vh;overflow:auto;background:#111827;border:1px solid var(--border);border-radius:18px;padding:18px;box-shadow:0 24px 80px rgba(0,0,0,.45)}
.batchProgress{color:#94a3b8;font-size:13px;margin:14px 0;line-height:1.6}.batchProgress .done{color:#22c55e}.batchProgress .skip{color:#f59e0b}
.batchAiResult{background:#0f172a;border:1px solid #334155;border-radius:12px;padding:14px;margin-top:12px;font-size:13px;line-height:1.7;color:#cbd5e1;white-space:pre-wrap;display:none}.batchAiResult.show{display:block}
</style></head><body>
<div class="layout dashboardMode"><aside class="sidebar"><div id="sideAiButtons" style="display:flex;gap:8px;margin-bottom:14px"><div style="font-size:11px;color:var(--muted);width:100%;margin-bottom:4px">AI Provider for Analysis</div><button id="sideOllamaBtn" onclick="window._setAiProvider('ollama');window._runAnalysis()" style="flex:1;background:#1e3a5f;color:#93c5fd;border:1px solid #3b82f6;border-radius:12px;padding:9px 6px;cursor:pointer;font-size:13px;font-weight:700">🦙 Ollama</button><button id="sideGeminiBtn" onclick="window._setAiProvider('gemini');window._runAnalysis()" style="flex:1;background:#1f2937;color:#64748b;border:1px solid #334155;border-radius:12px;padding:9px 6px;cursor:pointer;font-size:13px;font-weight:700">✦ Gemini</button></div><div id="sideStoreView"><div class="brand" id="sideBrand">Stores</div><div class="sideNote" id="sideNote">Stores are listed by last scan time.</div><div class="sideTitle" id="sideTitle">Store list</div><div id="sideList"></div></div><div id="sideChatNav" class="sideChatNav">
<div id="sideChatOllama" style="display:flex;flex-direction:column;height:100%">
<div class="brand" style="margin-bottom:8px">Ollama Chats</div>
<button class="chatNewBtn" id="chatNewBtn" style="width:100%;margin-bottom:8px">+ New Chat</button>
<div class="chatConvList" id="chatConvList" style="max-height:none;flex:1;overflow-y:auto;border-bottom:none;margin-bottom:0;padding-bottom:0"></div>
</div>
<div id="sideChatGemini" style="display:none;flex-direction:column;height:100%">
<div class="brand" style="margin-bottom:8px">Gemini Chats</div>
<button class="chatNewBtn" id="geminiNewBtn" style="width:100%;margin-bottom:8px">+ New Chat</button>
<div class="chatConvList" id="geminiConvList" style="max-height:none;flex:1;overflow-y:auto;border-bottom:none;margin-bottom:0;padding-bottom:0"></div>
</div>
</div></aside><main class="main"><div class="topbar"><div class="title"><h1 id="pageTitle">Unified Product Research Dashboard</h1><p id="pageSub">Selected eBay and Alibaba products.</p></div><nav class="tabs"><button class="tabBtn active" data-tab="tab1">Dashboard</button><button class="tabBtn" data-tab="store">Store Tracker</button><button class="tabBtn" data-tab="ebaySearch">eBay Search</button><button class="tabBtn" data-tab="alibaba">Alibaba Search</button><button class="tabBtn" data-tab="ai">AI Assistant</button><button class="tabBtn" data-tab="tab5">Ollama</button><button class="tabBtn" data-tab="tab6">Gemini</button><button class="tabBtn" data-tab="fittings">🔩 Fittings</button></nav></div><div class="wrap"><section id="tab1" class="tabPage"><div class="dashboardLayout"><div class="dashboardTop hidden"><div><div class="calcHint" id="connectHint">Click Connect on one Dashboard card, then click Connect on the matching card from the other source.</div></div><div id="pairList" class="modalPairList"></div></div><div class="grid" id="dashboardGrid"></div><div class="empty" id="dashboardEmpty">No selected products yet. Use Select on eBay Search or Alibaba cards.</div></div><div id="calcModalOverlay" class="modalOverlay hidden"><div class="calcModal"><div class="modalHead"><div><h2>Profit Calculator</h2><div class="calcHint">Connected pair profit calculation</div></div><button class="modalClose" id="calcModalClose">Close</button></div><div id="calcBox" class="hidden"><div class="pairTitle" id="calcPairTitle">Selected pair</div><div class="connectedPreview"><div class="miniProductBox"><div class="miniImgRow"><img id="calcEbayImg"/><div><div class="pairLine"><b>eBay Product</b></div><div class="pairLine" id="calcEbayTitle"></div></div></div><div class="priceInline"><label>Selling price seen by buyer (£)<input type="number" id="calcTargetPrice" value="0" step="0.01" oninput="calculateProfitPair()"></label></div></div><div class="linkArrows"><span>→</span><span>←</span></div><div class="miniProductBox"><div class="miniImgRow"><img id="calcAliImg"/><div><div class="pairLine"><b>Alibaba Supplier</b></div><div class="pairLine" id="calcAliTitle"></div></div></div><div class="priceInline"><label>Supplier purchase price (£)<input type="number" id="calcActualPurchasePrice" value="0" step="0.01" oninput="calculateProfitPair()"></label></div></div></div><div class="calcForm"><label><span id="calcPostageUnitLabel">Product weight</span><div class="postageInputRow"><input type="number" id="calcWeight" value="0.5" step="0.1" oninput="syncPostageUnitValue()"><button type="button" id="calcKgBtn" class="unitBtn active" onclick="setPostageMode('kg')">kg</button><button type="button" id="calcGbpBtn" class="unitBtn" onclick="setPostageMode('gbp')">£</button></div></label><label>China freight to UK customs (£)<input type="number" id="calcChinaFreight" value="1.50" step="0.1" oninput="calculateProfitPair()"></label><label>UK inbound freight (£)<input type="number" id="calcUkInbound" value="0.50" step="0.1" oninput="calculateProfitPair()"></label><label>Standard promoted listings rate (%)<input type="number" id="calcAdRate" value="2" step="0.5" oninput="calculateProfitPair()"></label><label>PPC cost per sale (£)<input type="number" id="calcPpcTotal" value="0.00" step="0.01" oninput="calculateProfitPair()"></label><label>Packaging cost (£)<input type="number" id="calcPkgCost" value="0.25" step="0.01" oninput="calculateProfitPair()"></label></div><div class="calcResult"><div class="calcProfitTop"><div class="pairLine">Final net profit</div><div class="big" id="calcProfit">£0.00</div></div><div class="calcDetails" id="calcDetails"></div></div></div></div></div></section><section id="work" class="tabPage hidden"><div class="fixedControls"><div class="stats"><div class="stat"><div class="label">Products</div><div class="value" id="stProducts">0</div></div><div class="stat"><div class="label">Yesterday</div><div class="value" id="stYesterday">0</div></div><div class="stat"><div class="label">7 Days</div><div class="value" id="st7Qty">0</div></div><div class="stat"><div class="label">30 Days</div><div class="value" id="st30Qty">0</div></div><div class="stat"><div class="label">30D Revenue</div><div class="value" id="st30Rev">£0.00</div></div><div class="stat"><div class="label">Total Revenue</div><div class="value" id="stTotalRev">£0.00</div></div><div class="stat"><div class="label">Scanned</div><div class="value" id="stDone">0</div></div><div class="stat"><div class="label">Remaining</div><div class="value" id="stRemain">0</div></div><div class="stat"><div class="label">Queue Total</div><div class="value" id="stTotalQueue">0</div></div><div class="stat"><div class="label">Status</div><div class="value" id="stRunState">idle</div></div></div><div class="toolbar"><input id="search" placeholder="Search title or Item ID..."/><select id="sort"><option value="sold30">Sort: 30-day sales</option><option value="sold7">7-day sales</option><option value="yesterday">Yesterday sales</option><option value="price">Price</option><option value="title">Title</option></select><select id="filter"><option value="all">All products</option><option value="sold">Has 30-day sales</option><option value="nosales">No 30-day sales</option></select></div></div><div id="grid" class="grid"></div><div id="empty" class="empty hidden"></div></section><section id="alibaba" class="tabPage hidden"><div class="fixedControls"><div class="stats"><div class="stat"><div class="label">Products</div><div class="value" id="aliStProducts">0</div></div><div class="stat"><div class="label">Suppliers</div><div class="value" id="aliStSuppliers">0</div></div><div class="stat"><div class="label">Min Price</div><div class="value" id="aliStMinPrice">0</div></div><div class="stat"><div class="label">Avg Price</div><div class="value" id="aliStAvgPrice">0</div></div><div class="stat"><div class="label">Sold/Orders</div><div class="value" id="aliStSold">0</div></div><div class="stat"><div class="label">Verified</div><div class="value" id="aliStVerified">0</div></div><div class="stat"><div class="label">Scanned</div><div class="value" id="aliStDone">0</div></div><div class="stat"><div class="label">Remaining</div><div class="value" id="aliStRemain">0</div></div><div class="stat"><div class="label">Queue Total</div><div class="value" id="aliStTotalQueue">0</div></div><div class="stat"><div class="label">Status</div><div class="value" id="aliStRunState">idle</div></div></div><div class="toolbar"><input id="aliSearch" placeholder="Search title or supplier..."/><select id="aliSort"><option value="default">Sort: Default</option><option value="price_asc">Price: Low to High</option><option value="moq_asc">MOQ: Low to High</option><option value="supplier">Supplier Name</option></select><select id="aliFilter"><option value="all">All products</option><option value="needs_review">Needs review</option><option value="verified">Verified</option></select></div></div><div id="aliGrid" class="grid"></div><div id="aliEmpty" class="empty"></div></section><section id="ai" class="tabPage hidden"><div class="aiWrap"><div class="aiHeader"><div class="aiTitleRow"><h2>AI Product Analysis</h2><span class="aiBadge" id="aiDataBadge">Loading data...</span></div><p class="aiSubtitle">Ask AI about your products, compare suppliers, find the best items to sell.</p></div><div class="aiQuickRow" id="aiQuickRow"></div><div class="aiInputRow"><input id="aiQuestion" placeholder="Ask AI anything about your products... (e.g. What is the best product to sell?)" autocomplete="off"/><select id="aiModel"><option value="gpt-4o">GPT-4o</option><option value="gpt-4o-mini">GPT-4o Mini (cheaper)</option><option value="gpt-4-turbo">GPT-4 Turbo</option></select><button id="aiAskBtn" class="aiAskBtn">Ask AI</button></div><div class="aiKeyRow"><input id="aiApiKey" type="password" placeholder="OpenAI API Key (optional if set as env var)" autocomplete="off"/><span class="aiKeyHint">Get one from platform.openai.com/api-keys</span></div><div id="aiResult" class="aiResult"><div class="aiPlaceholder">Ask a question and AI will analyze your entire database.</div></div></div></section>
<section id="tab5" class="tabPage hidden"><div class="chatPage"><div class="chatArea" id="chatArea" style="flex:1;overflow-y:auto;border:1px solid #334155;border-radius:14px;background:#0a0f1c;padding:12px;min-height:0"><div class="chatEmpty">Click &quot;+ New Chat&quot; to start a conversation with AI about your products.</div></div><div class="chatInputRow" style="margin-top:10px;align-items:flex-end"><textarea id="chatInput" placeholder="پیام بنویس..." rows="3"></textarea><button class="chatSendBtn" id="chatSendBtn" style="height:80px">Send</button></div></div></section>
<section id="tab6" class="tabPage hidden"><div class="chatPage"><div style="display:flex;gap:10px;align-items:center;margin-bottom:10px;flex-shrink:0"><input id="geminiApiKey" type="password" placeholder="Google Gemini API Key..." style="flex:1;background:#111827;color:#e5e7eb;border:1px solid #334155;border-radius:10px;padding:8px 12px;font-size:13px" autocomplete="off"/><select id="geminiModel" style="background:#111827;color:#e5e7eb;border:1px solid #334155;border-radius:10px;padding:8px 12px;font-size:13px"><option value="gemini-3-flash-preview">Gemini 3 Flash Preview</option><option value="gemini-2.5-flash">Gemini 2.5 Flash</option></select><button id="geminiTestModels" style="background:#059669;color:#fff;border:0;border-radius:10px;padding:8px 14px;cursor:pointer;font-size:13px;white-space:nowrap">Test Models</button><button id="geminiSaveKey" style="background:#2563eb;color:#fff;border:0;border-radius:10px;padding:8px 14px;cursor:pointer;font-size:13px;white-space:nowrap">Save Key</button></div><div class="chatArea" id="geminiChatArea" style="flex:1;overflow-y:auto;border:1px solid #334155;border-radius:14px;background:#0a0f1c;padding:12px;min-height:0"><div class="chatEmpty">Type a message below to chat with Google Gemini about your products.</div></div><div class="chatInputRow" style="margin-top:10px;align-items:flex-end"><textarea id="geminiChatInput" placeholder="پیام بنویس..." rows="3"></textarea><button class="chatSendBtn" id="geminiSendBtn" style="height:80px">Send</button></div></div></section>
<section id="fittings" class="tabPage hidden"><div class="fixedControls"><div class="stats"><div class="stat"><div class="label">Total</div><div class="value" id="fitStTotal">0</div></div><div class="stat"><div class="label">Brass</div><div class="value" id="fitStBrass">0</div></div><div class="stat"><div class="label">Stainless</div><div class="value" id="fitStSS">0</div></div><div class="stat"><div class="label">PVC</div><div class="value" id="fitStPVC">0</div></div><div class="stat"><div class="label">Food Grade</div><div class="value" id="fitStFood">0</div></div><div class="stat"><div class="label">Industrial</div><div class="value" id="fitStInd">0</div></div><div class="stat"><div class="label">Pneumatic</div><div class="value" id="fitStPneu">0</div></div></div><div class="toolbar" style="grid-template-columns:1fr 170px 170px 170px 130px"><input id="fitSearch" placeholder="Search name or SKU..."/><select id="fitFilterCategory"><option value="">All Categories</option></select><select id="fitFilterMaterial"><option value="">All Materials</option></select><select id="fitFilterGrade"><option value="">All Grades</option></select><button class="fitBtn" style="background:#2563eb;color:#fff;border-color:#60a5fa" onclick="fitOpenModal(null)">+ Add Fitting</button></div></div><div id="fitGrid" class="grid"></div><div id="fitEmpty" class="empty">No fittings yet. Click "+ Add Fitting" to create your first part.</div></section>
<div id="varModalOverlay" class="modalOverlay hidden"><div class="varModal"><div class="modalHead"><div><h2>Variation Sales Analysis</h2><div class="calcHint" id="varModalSubtitle">Per-variation sales breakdown</div></div><div class="modalHeadActions"><button class="varAiBtn" id="varAiBtn" onclick="analyzeVariations()">Analyse with AI</button><div class="varAiLoading" id="varAiLoading"><div class="varSpin"></div> Analysing...</div><button class="modalClose" id="varModalClose">Close</button></div></div><div id="varModalBody"><div class="calcHint">Loading...</div></div><div class="varAiSection"><div class="varAiResult" id="varAiResult"></div></div></div></div></div><div id="batchVarModalOverlay" class="batchModalOverlay hidden"><div class="batchModal"><div class="modalHead"><div><h2>Batch Variation Analysis</h2><div class="calcHint" id="batchVarSubtitle">Analysing variations for all displayed products</div></div><div class="modalHeadActions"><button class="modalClose" id="batchVarModalClose">Close</button></div></div><div id="batchVarBody"><div class="calcHint">Click "Start Analysis" to analyse all products currently shown in eBay Search.</div></div><div class="varAiSection"><div class="batchAiResult" id="batchAiResult"></div></div></div></div><div id="aliBatchModalOverlay" class="batchModalOverlay hidden"><div class="batchModal"><div class="modalHead"><div><h2>Alibaba Supplier Analysis</h2><div class="calcHint" id="aliBatchSubtitle">Analysing all displayed Alibaba products</div></div><div class="modalHeadActions"><button class="modalClose" id="aliBatchModalClose">Close</button></div></div><div id="aliBatchBody"><div class="calcHint">Click "Start Analysis" to analyse all products currently shown in Alibaba Search.</div></div><div class="varAiSection"><div class="batchAiResult" id="aliBatchAiResult"></div></div></div></div><div id="storeAnalysisModalOverlay" class="batchModalOverlay hidden"><div class="batchModal"><div class="modalHead"><div><h2>Store Best Sellers Analysis</h2><div class="calcHint" id="storeAnalysisSubtitle">Analysing top performing products in your store</div></div><div class="modalHeadActions"><button class="modalClose" id="storeAnalysisModalClose">Close</button></div></div><div id="storeAnalysisBody"><div class="calcHint">Click "Start Analysis" to analyse best sellers from your store.</div></div><div class="varAiSection"><div class="batchAiResult" id="storeAnalysisAiResult"></div></div></div></div></main><aside id="filterPanel" class="filterPanel hidden"><div class="filterHead">Alibaba Filters</div><div class="filterNote">Local filters are generated from the current Alibaba titles and visible supplier fields. No external database is used.</div><div id="filterList"></div></aside></div>
<script>
let sideBrand=document.getElementById('sideBrand'),sideNote=document.getElementById('sideNote'),sideTitle=document.getElementById('sideTitle');let activeTab='tab1', currentSeller='__all__', currentGroup='__all__', currentAlibabaGroup='__all__', allCards=[], stores=[], groups=[], alibabaGroups=[], searchMergeMode=false, searchMergeSelected=new Set(), alibabaMergeMode=false, alibabaMergeSelected=new Set(), activeAlibabaFilters=new Set(), alibabaFilterIndex=new Map(), dashboardItems=[], dashboardPairs=[], pendingConnectItem=null, activeCalcPair=null, calcPostageMode='kg', calcPostageKgValue='0.5', calcPostageGbpValue='';
const titles={tab1:['Dashboard','Selected eBay and Alibaba products'],store:['eBay Store Tracker','All stores'],ebaySearch:['eBay Search','All search groups'],alibaba:['Alibaba Search','Alibaba image/text search results'],tab5:['Ollama','Chat with Ollama AI'],tab6:['Gemini','Chat with Google Gemini AI'],fittings:['Fittings Library','Hose & connector parts catalogue']};
function isWorkTab(){return activeTab==='store'||activeTab==='ebaySearch'}function isAlibaba(){return activeTab==='alibaba'}function money(n){return '£'+Number(n||0).toFixed(2)}function priceNum(v){const m=String(v||'').replace(/,/g,'').match(/\d+(?:\.\d+)?/);return m?Number(m[0]):0}function esc(s){return String(s||'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]))}function escAttr(s){return esc(s).replace(/'/g,'&#39;')}function shortTitle(t){t=t||'Untitled product';return t.length>95?t.slice(0,95)+'...':t}function parsePrice(s){const m=String(s||'').replace(/,/g,'').match(/[0-9]+(\.[0-9]+)?/);return m?Number(m[0]):0}function firstNumber(txt){const m=String(txt||'').match(/(\d[\d,]*)/);return m?Number(m[1].replace(/,/g,'')):0}
function meaningfulQueryFromTitle(title){const stop=new Set(['for','and','the','with','without','new','brand','unbranded','buy','now','free','delivery','shipping','from','read','desc','description','uk','only','sale','hot','latest','genuine','compatible','cover']);const tokens=String(title||'').toLowerCase().replace(/[^a-z0-9]+/g,' ').split(/\s+/).filter(Boolean);const kept=[];for(const t of tokens){if(stop.has(t))continue;if(t.length<3&&!/\d/.test(t))continue;if(!kept.includes(t))kept.push(t);if(kept.length>=9)break;}return kept.join(' ').trim()||String(title||'').slice(0,80).trim()}function storeSearchFilterParams(){return 'LH_BIN=1&LH_ItemCondition=1000&LH_PrefLoc=1&_sop=16'}function openSimilarTitleSearch(itemId,title){const query=meaningfulQueryFromTitle(title);if(!query){alert('No usable title for eBay title search.');return;}const hash='#b44AutoSearch=1&source_item='+encodeURIComponent(itemId||'');const url='https://www.ebay.co.uk/sch/i.html?_nkw='+encodeURIComponent(query)+'&'+storeSearchFilterParams()+hash;window.open(url,'_blank')}function openSimilarImageSearch(itemId,title,imageUrl){if(!imageUrl){alert('This product has no image URL for eBay image search.');return;}const fallback=meaningfulQueryFromTitle(title);const hash='#b44ImageSearch=1&source_item='+encodeURIComponent(itemId||'')+'&imageUrl='+encodeURIComponent(imageUrl)+'&fallbackTitle='+encodeURIComponent(fallback);const url='https://www.ebay.co.uk/sch/i.html'+hash;window.open(url,'_blank')}function openAlibabaTitleSearch(itemId,title){const query=meaningfulQueryFromTitle(title);if(!query){alert('No usable title for Alibaba title search.');return;}const hash='#b44AlibabaAutoSearch=1&source_item='+encodeURIComponent(itemId||'')+'&fallbackTitle='+encodeURIComponent(query);const url='https://www.alibaba.com/search/page?SearchScene=proSearch&from=b44Dashboard&SearchText='+encodeURIComponent(query)+hash;window.open(url,'_blank')}function openAlibabaImageSearch(itemId,title,imageUrl){const fallback=meaningfulQueryFromTitle(title);if(!imageUrl){openAlibabaTitleSearch(itemId,title);return;}const hash='#b44AlibabaImageSearch=1&source_item='+encodeURIComponent(itemId||'')+'&imageUrl='+encodeURIComponent(imageUrl)+'&fallbackTitle='+encodeURIComponent(fallback);const url='https://www.alibaba.com/?from=b44DashboardImage'+hash;window.open(url,'_blank')}
function soldText(p){const n=Number(p.total_sold||0)||firstNumber(p.total_sold_text);return n?`sold (${n.toLocaleString()})`:'sold (unknown)'}function availableText(p){const txt=String(p.available_text||'');const n=Number(p.available_quantity||0)||firstNumber(txt);if(!n)return 'available (unknown)';return /^more than/i.test(txt)?`More than (${n.toLocaleString()}) available`:`(${n.toLocaleString()}) available`}function spanText(p){const d=Number(p.tracked_days_span||0);return d>0?`All tracked (${d}d)`:'All tracked'}
function setWorkLabels(){const section=isAlibaba()?document.getElementById('alibaba'):document.getElementById('work');if(!section)return;const labels=[...section.querySelectorAll('.fixedControls .stat .label')];const ebay=['Products','Yesterday','7 Days','30 Days','30D Revenue','Total Revenue','Scanned','Remaining','Queue Total','Status'];const ali=['Products','Suppliers','Min Price','Avg Price','Sold/Orders','Verified','Scanned','Remaining','Queue Total','Status'];(isAlibaba()?ali:ebay).forEach((t,i)=>{if(labels[i])labels[i].textContent=t});}
function setToolbarOptions(){const sortEl=document.getElementById(isAlibaba()?'aliSort':'sort'),filterEl=document.getElementById(isAlibaba()?'aliFilter':'filter');if(!sortEl||!filterEl)return;if(isAlibaba()){sortEl.innerHTML='<option value="price_asc">Price: low to high</option><option value="moq_asc">MOQ: low to high</option><option value="supplier">Supplier</option>';filterEl.innerHTML='<option value="all">All products</option><option value="needs_review">Needs review</option><option value="verified">Verified only</option>';}else{sortEl.innerHTML='<option value="sold30">Sort: 30-day sales</option><option value="sold7">7-day sales</option><option value="yesterday">Yesterday sales</option><option value="price">Price</option><option value="title">Title</option>';filterEl.innerHTML='<option value="all">All products</option><option value="sold">Has 30-day sales</option><option value="nosales">No 30-day sales</option>';}}
function normText(s){return String(s||'').toLowerCase().replace(/[_\/|,+()\[\]{}:;]+/g,' ').replace(/\s+/g,' ').trim()}
function firstNumber(s){const m=String(s||'').replace(/,/g,'').match(/\d+(?:\.\d+)?/);return m?Number(m[0]):0}
function minOrderNumber(p){return firstNumber(p.min_order_text||p.moq_text||'')}
function yearsNumber(p){const m=String(p.years_text||'').match(/\d+/);return m?Number(m[0]):0}
function ratingNumber(p){return Number(p.rating||firstNumber(p.rating_text)||0)}
function present(v){const t=String(v||'').trim().toLowerCase();return !!t&&!['unknown','n/a','na','none','null'].includes(t)}
function isAlibabaVerified(p){return /verified|trade assurance|guaranteed/i.test(String(p.badges_text||''))}
function needsAlibabaReview(p){return !present(p.shipping_text)||!present(p.delivery_text)||!present(p.min_order_text)||!present(p.supplier_name)||!present(p.price_text)}
function titleHas(p,words){const t=normText([p.title,p.badges_text,p.supplier_name].join(' '));return words.some(w=>t.includes(w))}
function capacityValues(p){const vals=[];const t=String(p.title||'');let m;const re=/(\d{3,6})\s*mah/ig;while((m=re.exec(t))){const v=Number(m[1]);if(v>=500&&!vals.includes(v))vals.push(v)}return vals}
function wattValues(p){const vals=[];const t=String(p.title||'');let m;const re=/(\d+(?:\.\d+)?)\s*w\b/ig;while((m=re.exec(t))){const v=Number(m[1]);if(v>=2&&v<=300&&!vals.includes(v))vals.push(v)}return vals}
function priceValue(p){return parsePrice(p.price_text)}
function smartTagsForAlibaba(p){const tags=[];const caps=capacityValues(p);if(caps.length)tags.push(caps.slice(0,2).map(v=>v+'mAh').join('/'));const watts=wattValues(p);if(watts.length)tags.push(watts.slice(0,2).map(v=>v+'W').join('/'));const checks=[['Wireless',['wireless']],['Magnetic',['magnetic','magsafe']],['Built-in Cable',['built in cable','built-in cable','with cable','4 cables','dual cable']],['USB-C',['usb c','usb-c','type c','type-c']],['Fast Charge',['fast charging','quick charge','pd','qc']],['Custom/OEM',['custom logo','oem','customized']],['Mini/Slim',['mini','slim','ultra thin','ultra-thin','pocket','capsule']],['Stand/Holder',['stand','holder','bracket']],['LED Display',['led display','digital display']],['Solar',['solar']]];for(const [label,words] of checks){if(titleHas(p,words))tags.push(label)}return tags.slice(0,6)}
function makeAlibabaFilterDefs(items){const sections=[];const defs=[];function add(section,key,label,match,minCount=1){const count=items.filter(match).length;if(count>=minCount&&count<items.length){defs.push({key,label,count,match});let sec=sections.find(s=>s.title===section);if(!sec){sec={title:section,filters:[]};sections.push(sec)}sec.filters.push({key,label,count})}}
const capacities=new Map();const watts=new Map();for(const p of items){for(const v of capacityValues(p))capacities.set(v,(capacities.get(v)||0)+1);for(const v of wattValues(p))watts.set(v,(watts.get(v)||0)+1)}
[...capacities.entries()].sort((a,b)=>b[1]-a[1]||a[0]-b[0]).slice(0,8).forEach(([v])=>add('Title: Capacity','cap_'+v,v+'mAh',p=>capacityValues(p).includes(v),2));
[...watts.entries()].sort((a,b)=>b[1]-a[1]||a[0]-b[0]).slice(0,8).forEach(([v])=>add('Title: Charging Power','watt_'+String(v).replace('.','_'),v+'W',p=>wattValues(p).includes(v),2));
add('Title: Charging Type','feat_wireless','Wireless',p=>titleHas(p,['wireless']),2);add('Title: Charging Type','feat_magnetic','Magnetic / MagSafe',p=>titleHas(p,['magnetic','magsafe']),2);add('Title: Charging Type','feat_builtin_cable','Built-in / With Cable',p=>titleHas(p,['built in cable','built-in cable','with cable','4 cables','dual cable']),2);add('Title: Charging Type','feat_usb_c','USB-C / Type-C',p=>titleHas(p,['usb c','usb-c','type c','type-c']),2);add('Title: Charging Type','feat_fast','Fast / Quick / PD',p=>titleHas(p,['fast charging','quick charge','pd','qc']),2);
add('Title: Design','feat_mini','Mini / Slim / Pocket',p=>titleHas(p,['mini','slim','ultra thin','ultra-thin','pocket','capsule']),2);add('Title: Design','feat_stand','Stand / Holder / Bracket',p=>titleHas(p,['stand','holder','bracket']),2);add('Title: Design','feat_led','LED / Digital Display',p=>titleHas(p,['led display','digital display','led digital']),2);add('Title: Business','feat_oem','OEM / Custom Logo',p=>titleHas(p,['oem','custom logo','customized','custom']),2);add('Title: Business','feat_wholesale','Wholesale / Factory',p=>titleHas(p,['wholesale','factory','manufacturer']),2);
add('Price / MOQ','price_under_3','Price ≤ £3',p=>{const x=priceValue(p);return x>0&&x<=3});add('Price / MOQ','price_3_5','£3 < Price ≤ £5',p=>{const x=priceValue(p);return x>3&&x<=5});add('Price / MOQ','price_5_8','£5 < Price ≤ £8',p=>{const x=priceValue(p);return x>5&&x<=8});add('Price / MOQ','moq_5','MOQ ≤ 5',p=>{const x=minOrderNumber(p);return x>0&&x<=5});add('Price / MOQ','moq_10','MOQ ≤ 10',p=>{const x=minOrderNumber(p);return x>0&&x<=10});add('Price / MOQ','moq_50','MOQ ≤ 50',p=>{const x=minOrderNumber(p);return x>0&&x<=50});
add('Supplier Trust','sold_any','Has sold/orders',p=>Number(p.sold_count||0)>0);add('Supplier Trust','rating_45','Rating ≥ 4.5',p=>ratingNumber(p)>=4.5);add('Supplier Trust','years_5','Supplier years ≥ 5',p=>yearsNumber(p)>=5);add('Supplier Trust','verified','Verified / Trade Assurance',p=>titleHas(p,['verified supplier','trade assurance','alibaba guaranteed'])||/verified|trade assurance|guaranteed/i.test(String(p.badges_text||'')));
const countries=new Map();for(const p of items){const c=String(p.country||'').trim().toUpperCase();if(c)countries.set(c,(countries.get(c)||0)+1)}[...countries.entries()].sort((a,b)=>b[1]-a[1]).slice(0,6).forEach(([c])=>add('Country','country_'+c,'Country: '+c,p=>String(p.country||'').trim().toUpperCase()===c));
alibabaFilterIndex=new Map(defs.map(d=>[d.key,d]));for(const k of [...activeAlibabaFilters])if(!alibabaFilterIndex.has(k))activeAlibabaFilters.delete(k);return sections}
function renderAlibabaFilters(baseItems){const panel=document.getElementById('filterPanel'),list=document.getElementById('filterList');if(!panel||!list)return;panel.classList.toggle('hidden',!isAlibaba());if(!isAlibaba()){list.innerHTML='';return}const sections=makeAlibabaFilterDefs(baseItems);let html=`<button class="clearFilters" id="clearAlibabaFilters">Clear filters (${activeAlibabaFilters.size})</button>`;if(!sections.length)html+='<div class="filterNote">No strong local filters were found yet. Extract more products from Alibaba.</div>';for(const sec of sections){html+=`<div class="filterSection"><div class="filterTitle">${esc(sec.title)}</div>`;for(const f of sec.filters){html+=`<button class="filterBtn${activeAlibabaFilters.has(f.key)?' active':''}" data-filter-key="${escAttr(f.key)}"><span class="filterCount">${f.count}</span><span class="filterLabel">${esc(f.label)}</span></button>`}html+='</div>'}list.innerHTML=html;const clear=document.getElementById('clearAlibabaFilters');if(clear)clear.onclick=()=>{activeAlibabaFilters.clear();renderCards()};list.querySelectorAll('[data-filter-key]').forEach(btn=>btn.addEventListener('click',()=>{const k=btn.dataset.filterKey;if(activeAlibabaFilters.has(k))activeAlibabaFilters.delete(k);else activeAlibabaFilters.add(k);renderCards()}))}
function applyAlibabaLocalFilters(items){for(const k of activeAlibabaFilters){const def=alibabaFilterIndex.get(k);if(def)items=items.filter(def.match)}return items}
function showTab(id){activeTab=id;const layout=document.querySelector('.layout');layout.classList.toggle('dashboardMode',id==='tab1');layout.classList.toggle('withFilters',id==='alibaba');document.getElementById('filterPanel').classList.toggle('hidden',id!=='alibaba');document.querySelectorAll('.tabPage').forEach(x=>x.classList.add('hidden'));const visible=id==='alibaba'?'alibaba':(isWorkTab()?'work':id);document.getElementById(visible).classList.remove('hidden');document.querySelectorAll('.tabBtn').forEach(x=>x.classList.toggle('active',x.dataset.tab===id));document.getElementById('pageTitle').textContent=titles[id][0];document.getElementById('pageSub').textContent=id==='store'?(currentSeller==='__all__'?'All stores':'Store: '+currentSeller):id==='ebaySearch'?(currentGroup==='__all__'?'All search groups':'Search: '+currentGroup):id==='alibaba'?(currentAlibabaGroup==='__all__'?'All Alibaba groups':'Alibaba: '+currentAlibabaGroup):titles[id][1];setWorkLabels();setToolbarOptions();updateSideLabels();if(isWorkTab()||id==='alibaba')loadData();if(id==='fittings')loadFittings();if(id==='tab1')loadDashboardCards();var _scn=document.getElementById('sideChatNav');var _ssv=document.getElementById('sideStoreView');if(id==='tab5'){if(_ssv)_ssv.style.display='none';if(_scn){_scn.style.display='flex';var _so=document.getElementById('sideChatOllama');var _sg=document.getElementById('sideChatGemini');if(_so)_so.style.display='flex';if(_sg)_sg.style.display='none';loadChatConversations();}}else if(id==='tab6'){if(_ssv)_ssv.style.display='none';if(_scn){_scn.style.display='flex';var _so2=document.getElementById('sideChatOllama');var _sg2=document.getElementById('sideChatGemini');if(_so2)_so2.style.display='none';if(_sg2)_sg2.style.display='flex';loadGeminiConversations();}}else{if(_ssv)_ssv.style.display='';if(_scn)_scn.style.display='none';}if(window._updateSidebarButtons)window._updateSidebarButtons();}
function updateSideLabels(){var storeView=document.getElementById('sideStoreView');if(activeTab==='tab5'){return;}storeView.style.display='';if(activeTab==='ebaySearch'){sideBrand.textContent='eBay Searches';sideNote.textContent='Groups are inferred from typed search words or repeated words in similar product titles.';sideTitle.textContent='Search groups';}else if(activeTab==='alibaba'){sideBrand.textContent='Alibaba Searches';sideNote.textContent='Groups are inferred from Alibaba image/text search result titles.';sideTitle.textContent='Alibaba groups';}else if(activeTab==='store'){sideBrand.textContent='Stores';sideNote.textContent='Stores are listed by last scan time.';sideTitle.textContent='Store list';}else{sideBrand.textContent='';sideNote.textContent='';sideTitle.textContent='';document.getElementById('sideList').innerHTML='';}}
document.querySelectorAll('.tabBtn').forEach(btn=>btn.addEventListener('click',()=>showTab(btn.dataset.tab)));document.getElementById('search').addEventListener('input',renderCards);document.getElementById('sort').addEventListener('change',renderCards);document.getElementById('filter').addEventListener('change',renderCards);document.getElementById('aliSearch')?.addEventListener('input',renderCards);document.getElementById('aliSort')?.addEventListener('change',renderCards);document.getElementById('aliFilter')?.addEventListener('change',renderCards);
function bindAlibabaManualInputs(root){root.querySelectorAll('.aliManualInput').forEach(inp=>{inp.addEventListener('change',()=>saveAlibabaManualFields(inp));inp.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();inp.blur();}})})}
async function saveAlibabaManualFields(input){const key=input.dataset.key;if(!key)return;const card=input.closest('.card');const fields={product_key:key,shipping_text:'',delivery_text:''};card.querySelectorAll('.aliManualInput').forEach(inp=>{fields[inp.dataset.field]=inp.value.trim()});input.classList.add('saving');const res=await fetch('/api/alibaba/product/update-fields',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(fields)}).then(r=>r.json()).catch(e=>({ok:false,error:String(e)}));input.classList.remove('saving');if(!res.ok){alert('Save failed: '+(res.error||JSON.stringify(res)));return}for(const list of [allCards,dashboardItems]){for(const p of list||[]){if(String(p.product_key||'')===key||String(p.item_id||'')==='ali:'+key){p.shipping_text=res.shipping_text||'';p.delivery_text=res.delivery_text||'';}}}}
async function loadDashboardCards(){const [cardsRes,linksRes]=await Promise.all([fetch('/api/dashboard/products/cards?t='+Date.now()).then(r=>r.json()),fetch('/api/dashboard/links?t='+Date.now()).then(r=>r.json())]);dashboardItems=cardsRes.products||[];dashboardPairs=linksRes.links||[];renderDashboardCards(dashboardItems);renderDashboardPairs();}
function renderDashboardCards(items){const grid=document.getElementById('dashboardGrid'),empty=document.getElementById('dashboardEmpty');const linkByItem=new Map(),itemByKey=new Map(),usedItems=new Set(),orderedItems=[];for(const p of items){itemByKey.set(String(p.item_id),p)}for(const pair of dashboardPairs){const ebayKey=String(pair.ebay_item_id||''),aliKey='ali:'+String(pair.alibaba_product_key||'');linkByItem.set(ebayKey,pair.link_id);linkByItem.set(aliKey,pair.link_id);const ebayItem=itemByKey.get(ebayKey),aliItem=itemByKey.get(aliKey);if(ebayItem&&aliItem){orderedItems.push(ebayItem,aliItem);usedItems.add(ebayKey);usedItems.add(aliKey)}}for(const p of items){const key=String(p.item_id);if(!usedItems.has(key))orderedItems.push(p)}grid.innerHTML='';empty.classList.toggle('hidden',items.length>0);for(const p of orderedItems){const item={...p,calc_link_id:linkByItem.get(String(p.item_id))||p.connected_link_id||''};grid.insertAdjacentHTML('beforeend',item.dashboard_source==='alibaba_search'?alibabaDashboardCardHtml(item):cardHtml(item,'dashboard'));}grid.querySelectorAll('.varBtn').forEach(btn=>btn.addEventListener('click',(e)=>{e.preventDefault();e.stopPropagation();openVariationModal(btn.dataset.varItem,btn.dataset.varTitle);}));grid.querySelectorAll('.connectDashboardBtn').forEach(btn=>btn.addEventListener('click',()=>handleDashboardConnect(btn.dataset.item,btn.dataset.source,btn)));grid.querySelectorAll('.calcDashboardBtn').forEach(btn=>btn.addEventListener('click',()=>openProfitCalculator(btn.dataset.link)));grid.querySelectorAll('.unlinkDashboardBtn').forEach(btn=>btn.addEventListener('click',()=>unlinkDashboardPair(btn.dataset.link)));grid.querySelectorAll('.removeDashboardBtn').forEach(btn=>btn.addEventListener('click',()=>removeFromDashboard(btn.dataset.item)));bindAlibabaManualInputs(grid);}
async function handleDashboardConnect(itemId,source,btn){if(!itemId)return;if(!pendingConnectItem){pendingConnectItem={item_id:itemId,source};document.querySelectorAll('.connectDashboardBtn').forEach(b=>b.classList.remove('waiting'));btn.classList.add('waiting');btn.textContent='Choose second card';document.getElementById('connectHint').textContent='Now click Connect on the matching card from the other source.';return}if(pendingConnectItem.item_id===itemId){pendingConnectItem=null;btn.classList.remove('waiting');btn.textContent='Connect';document.getElementById('connectHint').textContent='Connection cancelled. Click Connect on one eBay card and one Alibaba card.';return}if(pendingConnectItem.source===source){alert('Please connect one eBay card with one Alibaba card.');return}const res=await fetch('/api/dashboard/connect',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({first_item_id:pendingConnectItem.item_id,second_item_id:itemId})}).then(r=>r.json());if(!res.ok){alert('Connect failed: '+(res.error||JSON.stringify(res)));return}pendingConnectItem=null;document.getElementById('connectHint').textContent='Connected. Profit calculator is open for the connected pair.';await loadDashboardCards();if(res.link_id)openProfitCalculator(res.link_id);}
function renderDashboardPairs(){const wrap=document.getElementById('pairList');if(!wrap)return;if(!dashboardPairs.length){wrap.innerHTML='<div class="emptyPairs">No connected pair yet.</div>';const box=document.getElementById('calcBox');if(box)box.classList.add('hidden');closeProfitModal();return}wrap.innerHTML=dashboardPairs.map(p=>`<div class="pairCard"><div class="pairLine"><b>Connected:</b> ${esc(shortTitle(p.ebay_title||''))} ⇄ ${esc(shortTitle(p.alibaba_title||''))}</div><button class="calcBtn" data-link="${p.link_id}">Calculate Profit</button><button class="unlinkBtn" data-link="${p.link_id}">Unlink</button></div>`).join('');wrap.querySelectorAll('.calcBtn').forEach(btn=>btn.addEventListener('click',()=>openProfitCalculator(btn.dataset.link)));wrap.querySelectorAll('.unlinkBtn').forEach(btn=>btn.addEventListener('click',()=>unlinkDashboardPair(btn.dataset.link)));}
async function unlinkDashboardPair(linkId){const res=await fetch('/api/dashboard/unlink',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({link_id:linkId})}).then(r=>r.json());if(!res.ok){alert('Unlink failed: '+(res.error||JSON.stringify(res)));return}activeCalcPair=null;closeProfitModal();document.getElementById('connectHint').textContent='Pair unlinked. Click Connect on one eBay card and one Alibaba card to create a new calculator pair.';await loadDashboardCards();}
function updatePostageUnitUi(){const input=document.getElementById('calcWeight'),kgBtn=document.getElementById('calcKgBtn'),gbpBtn=document.getElementById('calcGbpBtn'),label=document.getElementById('calcPostageUnitLabel');if(!input)return;input.value=calcPostageMode==='kg'?calcPostageKgValue:calcPostageGbpValue;input.step=calcPostageMode==='kg'?'0.1':'0.01';input.placeholder=calcPostageMode==='kg'?'kg':'£';if(label)label.textContent=calcPostageMode==='kg'?'Product weight':'Postage cost';if(kgBtn)kgBtn.classList.toggle('active',calcPostageMode==='kg');if(gbpBtn)gbpBtn.classList.toggle('active',calcPostageMode==='gbp');}
function syncPostageUnitValue(){const input=document.getElementById('calcWeight');if(calcPostageMode==='kg')calcPostageKgValue=input?.value||'';else calcPostageGbpValue=input?.value||'';calculateProfitPair()}
function setPostageMode(mode){const input=document.getElementById('calcWeight');if(input){if(calcPostageMode==='kg')calcPostageKgValue=input.value;else calcPostageGbpValue=input.value;}calcPostageMode=mode==='gbp'?'gbp':'kg';updatePostageUnitUi();calculateProfitPair()}
function closeProfitModal(){const modal=document.getElementById('calcModalOverlay');if(modal)modal.classList.add('hidden')}function openProfitCalculator(linkId){const pair=dashboardPairs.find(p=>String(p.link_id)===String(linkId));if(!pair)return;activeCalcPair=pair;document.getElementById('calcModalOverlay').classList.remove('hidden');document.getElementById('calcBox').classList.remove('hidden');document.getElementById('calcPairTitle').textContent='Profit calculation';document.getElementById('calcEbayImg').src=pair.ebay_image_url||'';document.getElementById('calcAliImg').src=pair.alibaba_image_url||'';document.getElementById('calcEbayTitle').textContent=pair.ebay_title||'';document.getElementById('calcAliTitle').textContent=pair.alibaba_title||'';document.getElementById('calcTargetPrice').value=priceNum(pair.ebay_price_text).toFixed(2);document.getElementById('calcActualPurchasePrice').value=Number(pair.alibaba_min_price||priceNum(pair.alibaba_price_text)||0).toFixed(2);updatePostageUnitUi();calculateProfitPair();}
function calculateProfitPair(){const total=Number(document.getElementById('calcTargetPrice')?.value||0);const postageInput=Number(document.getElementById('calcWeight')?.value||0);const chinaFreight=Number(document.getElementById('calcChinaFreight')?.value||0);const ukInbound=Number(document.getElementById('calcUkInbound')?.value||0);const adRate=Number(document.getElementById('calcAdRate')?.value||0)/100;const ppc=Number(document.getElementById('calcPpcTotal')?.value||0);const pkg=Number(document.getElementById('calcPkgCost')?.value||0);const actualPurchase=Number(document.getElementById('calcActualPurchasePrice')?.value||0);let itemPrice=0;if(total<=20)itemPrice=(total-0.10)/1.07;else if(total<=50)itemPrice=(total-0.60)/1.04;else itemPrice=(total-0.70)/1.03;const bpf=total-itemPrice;let postage=postageInput,postageNote='manual';if(calcPostageMode==='kg'){const weight=postageInput;postage=7.19;if(weight<=1)postage=2.96;else if(weight<=2)postage=3.38;else if(weight<=10)postage=5.21;else if(weight<=20)postage=7.19;postageNote=`from ${weight||0}kg`;}const ppcWithVat=ppc*1.20;const adsPercentageCost=total*adRate;const logistics=chinaFreight+ukInbound;const fixedCosts=postage+ppcWithVat+adsPercentageCost+pkg+logistics;const finalProfit=itemPrice-fixedCosts-actualPurchase;document.getElementById('calcProfit').textContent=money(finalProfit);document.getElementById('calcDetails').innerHTML=`Your item price share: ${money(itemPrice)}<br>eBay buyer fee impact: ${money(bpf)}<br>Postage: ${money(postage)} (${postageNote})<br>PPC + VAT: ${money(ppcWithVat)}<br>Ads percentage: ${money(adsPercentageCost)}<br>Packaging: ${money(pkg)}<br>Logistics: ${money(logistics)}`;}
async function selectForDashboard(itemId,sourceGroup){const res=await fetch('/api/dashboard/select',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({item_id:itemId,source_group:sourceGroup})}).then(r=>r.json());if(!res.ok){alert('Select failed: '+(res.error||JSON.stringify(res)));return}await loadCards();}
async function selectAlibabaForDashboard(productKey,sourceGroup){const res=await fetch('/api/dashboard/select',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source:'alibaba_search',product_key:productKey,source_group:sourceGroup})}).then(r=>r.json());if(!res.ok){alert('Select failed: '+(res.error||JSON.stringify(res)));return}await loadCards();}
async function removeFromDashboard(itemId){const res=await fetch('/api/dashboard/remove',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({item_id:itemId})}).then(r=>r.json());if(!res.ok){alert('Remove failed: '+(res.error||JSON.stringify(res)));return}if(activeTab==='tab1')await loadDashboardCards();}

async function loadData(){await Promise.all([loadSideList(),loadCards(),loadProgress()]);}async function loadSideList(){if(activeTab==='ebaySearch'){const res=await fetch('/api/search-groups/summary?t='+Date.now()).then(r=>r.json());groups=res.groups||[];}else if(activeTab==='alibaba'){const res=await fetch('/api/alibaba-groups/summary?t='+Date.now()).then(r=>r.json());alibabaGroups=res.groups||[];}else{const res=await fetch('/api/stores/summary?t='+Date.now()).then(r=>r.json());stores=res.stores||[];}renderSideList();}
function renderSideList(){const box=document.getElementById('sideList');box.innerHTML='';if(activeTab==='ebaySearch'){box.insertAdjacentHTML('beforeend',searchMergeControls());box.insertAdjacentHTML('beforeend',sideRow('__all__','All searches',groups.reduce((a,g)=>a+Number(g.product_count||0),0),currentGroup==='__all__',false));for(const g of groups)box.insertAdjacentHTML('beforeend',sideRow(g.group_name,g.group_name,g.product_count,currentGroup===g.group_name,true));}else if(activeTab==='alibaba'){box.insertAdjacentHTML('beforeend',alibabaMergeControls());box.insertAdjacentHTML('beforeend',sideRow('__all__','All Alibaba',alibabaGroups.reduce((a,g)=>a+Number(g.product_count||0),0),currentAlibabaGroup==='__all__',false));for(const g of alibabaGroups)box.insertAdjacentHTML('beforeend',sideRow(g.group_name,g.group_name,g.product_count,currentAlibabaGroup===g.group_name,true));}else{box.insertAdjacentHTML('beforeend',sideRow('__all__','All stores',stores.reduce((a,s)=>a+Number(s.product_count||0),0),currentSeller==='__all__',false));for(const s of stores)box.insertAdjacentHTML('beforeend',sideRow(s.seller_username,s.seller_username,s.product_count,currentSeller===s.seller_username,true));}box.querySelectorAll('[data-value]').forEach(b=>b.addEventListener('click',()=>selectSide(b.dataset.value)));box.querySelectorAll('[data-delete]').forEach(b=>b.addEventListener('click',()=>deleteSide(b.dataset.delete)));box.querySelectorAll('[data-merge-pick]').forEach(b=>b.addEventListener('click',e=>{e.stopPropagation();toggleActiveMergePick(b.dataset.mergePick)}));box.querySelectorAll('[data-merge-toggle]').forEach(b=>b.addEventListener('click',toggleActiveMergeMode));box.querySelectorAll('[data-merge-cancel]').forEach(b=>b.addEventListener('click',cancelActiveMergeMode));box.querySelectorAll('[data-merge-run]').forEach(b=>b.addEventListener('click',mergeSelectedActiveGroups));}
function searchMergeControls(){if(activeTab!=='ebaySearch')return '';const count=searchMergeSelected.size;if(!searchMergeMode)return `<div class="mergeControls"><button class="mergeBtn" data-merge-toggle="1">Merge lists</button></div>`;return `<div class="mergeControls"><div class="mergeNote">Select two or more eBay Search lists, then merge them into one list.</div><button class="mergeBtn primary" data-merge-run="1">Merge selected (${count})</button><button class="mergeBtn" data-merge-cancel="1">Cancel</button></div>`}
function alibabaMergeControls(){if(activeTab!=='alibaba')return '';const count=alibabaMergeSelected.size;if(!alibabaMergeMode)return `<div class="mergeControls"><button class="mergeBtn" data-merge-toggle="1">Merge lists</button></div>`;return `<div class="mergeControls"><div class="mergeNote">Select two or more Alibaba lists, then merge them into one list.</div><button class="mergeBtn primary" data-merge-run="1">Merge selected (${count})</button><button class="mergeBtn" data-merge-cancel="1">Cancel</button></div>`}
function activeMergeSet(){return activeTab==='ebaySearch'?searchMergeSelected:alibabaMergeSelected}function activeMergeMode(){return activeTab==='ebaySearch'?searchMergeMode:alibabaMergeMode}
function sideRow(value,name,count,active,canDelete){const merging=(activeTab==='alibaba'&&alibabaMergeMode&&canDelete)||(activeTab==='ebaySearch'&&searchMergeMode&&canDelete);const picked=activeTab==='ebaySearch'?searchMergeSelected.has(value):alibabaMergeSelected.has(value);if(merging)return `<div class="row mergeRow"><button class="mergePick${picked?' active':''}" data-merge-pick="${escAttr(value)}">${picked?'✓':''}</button><button class="rowBtn${active?' active':''}${picked?' mergeSelected':''}" data-value="${escAttr(value)}"><div class="rowName">${esc(name||'unknown')}</div><div class="rowMeta">${count||0} items</div></button>${canDelete?`<button class="delBtn" data-delete="${escAttr(value)}">×</button>`:'<span></span>'}</div>`;return `<div class="row"><button class="rowBtn${active?' active':''}" data-value="${escAttr(value)}"><div class="rowName">${esc(name||'unknown')}</div><div class="rowMeta">${count||0} items</div></button>${canDelete?`<button class="delBtn" data-delete="${escAttr(value)}">×</button>`:'<span></span>'}</div>`}
async function selectSide(value){if(((activeTab==='alibaba'&&alibabaMergeMode)||(activeTab==='ebaySearch'&&searchMergeMode))&&value&&value!=='__all__'){toggleActiveMergePick(value);return}if(activeTab==='ebaySearch'){currentGroup=value}else if(activeTab==='alibaba'){currentAlibabaGroup=value;activeAlibabaFilters.clear()}else{currentSeller=value}await loadCards();renderSideList();showTab(activeTab);}
function toggleActiveMergeMode(){if(activeTab==='ebaySearch'){searchMergeMode=true;searchMergeSelected.clear()}else if(activeTab==='alibaba'){alibabaMergeMode=true;alibabaMergeSelected.clear()}renderSideList()}function cancelActiveMergeMode(){if(activeTab==='ebaySearch'){searchMergeMode=false;searchMergeSelected.clear()}else if(activeTab==='alibaba'){alibabaMergeMode=false;alibabaMergeSelected.clear()}renderSideList()}function toggleActiveMergePick(value){if(!value||value==='__all__')return;const set=activeMergeSet();if(set.has(value))set.delete(value);else set.add(value);renderSideList()}async function mergeSelectedActiveGroups(){const isEbay=activeTab==='ebaySearch';const names=[...(isEbay?searchMergeSelected:alibabaMergeSelected)];if(names.length<2){alert('Select at least two lists to merge.');return}const defaultName=names[0];const newName=prompt('Merged list name:',defaultName);if(newName===null)return;const url=isEbay?'/api/search-groups/merge':'/api/alibaba-groups/merge';const res=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({group_names:names,target_group_name:defaultName,new_group_name:newName.trim()||defaultName})}).then(r=>r.json());if(!res.ok){alert('Merge failed: '+(res.error||JSON.stringify(res)));return}if(isEbay){searchMergeMode=false;searchMergeSelected.clear();currentGroup=res.target_group_name||defaultName}else{alibabaMergeMode=false;alibabaMergeSelected.clear();currentAlibabaGroup=res.target_group_name||defaultName;activeAlibabaFilters.clear()}await loadData();}
async function deleteSide(value){if(!value||value==='__all__')return;const ok=confirm('Delete '+value+' from this list?');if(!ok)return;let url,body;if(activeTab==='ebaySearch'){url='/api/search-groups/delete';body={group_name:value};}else if(activeTab==='alibaba'){url='/api/alibaba-groups/delete';body={group_name:value};}else{url='/api/stores/delete';body={seller_username:value};}const res=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json());if(!res.ok){alert('Delete failed: '+JSON.stringify(res.result||res.error));return}if(activeTab==='ebaySearch'){searchMergeSelected.delete(value);if(currentGroup===value)currentGroup='__all__';}if(activeTab==='alibaba'){alibabaMergeSelected.delete(value);if(currentAlibabaGroup===value)currentAlibabaGroup='__all__';}if(activeTab==='store'&&currentSeller===value)currentSeller='__all__';await loadData();}
async function loadCards(){let url;if(activeTab==='ebaySearch')url=currentGroup==='__all__'?'/api/search/products/cards?t='+Date.now():'/api/search/products/cards?group='+encodeURIComponent(currentGroup)+'&t='+Date.now();else if(activeTab==='alibaba')url=currentAlibabaGroup==='__all__'?'/api/alibaba/products/cards?t='+Date.now():'/api/alibaba/products/cards?group='+encodeURIComponent(currentAlibabaGroup)+'&t='+Date.now();else url=currentSeller==='__all__'?'/api/products/cards?t='+Date.now():'/api/products/cards?seller='+encodeURIComponent(currentSeller)+'&t='+Date.now();const res=await fetch(url).then(r=>r.json());allCards=res.products||[];renderCards();}
function alibabaCardHtml(p){const tags=smartTagsForAlibaba(p);const selected=Number(p.dashboard_selected||0)>0;const aliKey=p.product_key||'';
// Raw fields — shown exactly as received from the extension, no formatting
const rawCountry=p.country||null;
const rawYears=p.years_text||null;
const rawSoldText=p.sold_text||null;
const rawSoldCount=p.sold_count!=null?p.sold_count:null;
const rawRating=p.rating!=null?p.rating:null;
const rawReviewCount=p.review_count!=null?p.review_count:null;
const rawRatingText=p.rating_text||null;
const rawMoq=p.min_order_text||null;
const rawSupplier=p.supplier_name||null;
const rawBadges=String(p.badges_text||'').split(',').map(x=>x.trim()).filter(Boolean);
const rawPrice=p.price_text||null;
const rawMinPrice=p.min_price!=null?p.min_price:null;
// Build raw info rows - each field as a separate row, label + raw value
function rawRow(label,val){if(val===null||val===undefined||val==='')return '';return `<div class="rawRow"><span class="rawLabel">${esc(label)}</span><span class="rawVal">${esc(String(val))}</span></div>`;}
const rawHasAddToCart = Number(p.has_add_to_cart||0)>0;
const rawShipping = p.shipping_text||null;
const rawDelivery = p.delivery_text||null;
const rawBlock=[
  rawRow('country',rawCountry),
  rawRow('years',rawYears),
  rawRow('price',rawPrice),
  // rawRow('min_price',rawMinPrice!=null?String(rawMinPrice):null),  // hidden: redundant with price
  rawRow('min_order',rawMoq),
  rawRow('sold',rawSoldText),
  // rawRow('sold_count',rawSoldCount!=null?String(rawSoldCount):null),  // hidden: redundant with sold
  // rawRow('rating',rawRating!=null?String(rawRating):null),  // hidden: redundant with rating_text
  // rawRow('review_count',rawReviewCount!=null?String(rawReviewCount):null),  // hidden: redundant with rating_text
  rawRow('rating',rawRatingText),
  rawRow('supplier',rawSupplier),
  rawRow('badges',rawBadges.length?rawBadges.join(', '):null),
  rawRow('shipping', rawShipping),
  rawRow('delivery', rawDelivery),
].filter(Boolean).join('');
return `<div class="card"><div class="imageBox">${p.image_url?`<img src="${esc(p.image_url)}" loading="lazy"/>`:'<div class="noimg">No image</div>'}</div><div class="body"><div class="pTitle">${esc(shortTitle(p.title))}</div><div class="rawDataBox">${rawBlock||'<span class="rawEmpty">no raw data</span>'}</div><div class="smartTags">${tags.map(t=>`<span class="smartTag">${esc(t)}</span>`).join('')}</div><div class="footer"><a href="${escAttr(p.supplier_url||'#')}" target="_blank">Supplier</a><a href="${escAttr(p.product_url||'#')}" target="_blank">Product</a><button class="selectAlibabaDashboardBtn" data-key="${esc(p.product_key||'')}" data-group="${esc(p.search_group_name||'')}" ${selected?'disabled':''}>${selected?'Selected':'Select'}</button></div></div></div>`}
function alibabaDashboardCardHtml(p){const tags=smartTagsForAlibaba(p);const connected=Number(p.is_connected||0)>0;const aliKey=p.product_key||String(p.item_id||'').replace(/^ali:/,'');const calcAction=connected&&p.calc_link_id?`<button class="calcDashboardBtn" data-link="${esc(p.calc_link_id)}">Calculator</button>`:'';const connectAction=connected&&p.calc_link_id?`<button class="unlinkDashboardBtn" data-link="${esc(p.calc_link_id)}">Unlink</button>`:`<button class="connectDashboardBtn" data-item="${esc(p.item_id||'')}" data-source="alibaba_search">Connect</button>`;
// Raw fields - same format as Alibaba Search tab cards
const rawCountry=p.country||null;const rawYears=p.years_text||null;const rawPrice=p.price_text||null;const rawMoq=p.min_order_text||p.available_text||null;const rawSoldText=p.sold_text||p.total_sold_text||((Number(p.total_sold||0)>0)?`${p.total_sold} sold`:null);const rawRatingText=p.rating_text||p.watch_count_text||null;const rawSupplier=p.supplier_name||p.seller_username||null;const rawBadges=String(p.badges_text||'').split(',').map(x=>x.trim()).filter(Boolean);const rawShipping=p.shipping_text||null;const rawDelivery=p.delivery_text||null;
function rawRow(label,val){if(val===null||val===undefined||val==='')return '';return `<div class="rawRow"><span class="rawLabel">${esc(label)}</span><span class="rawVal">${esc(String(val))}</span></div>`;}
const rawBlock=[rawRow('country',rawCountry),rawRow('years',rawYears),rawRow('price',rawPrice),rawRow('min_order',rawMoq),rawRow('sold',rawSoldText),rawRow('rating',rawRatingText),rawRow('supplier',rawSupplier),rawRow('badges',rawBadges.length?rawBadges.join(', '):null),rawRow('shipping',rawShipping),rawRow('delivery',rawDelivery),].filter(Boolean).join('');
return `<div class="card"><div class="imageBox">${p.image_url?`<img src="${esc(p.image_url)}" loading="lazy"/>`:'<div class="noimg">No image</div>'}</div><div class="body"><div class="pTitle">${esc(shortTitle(p.title))}</div><div class="rawDataBox">${rawBlock||'<span class="rawEmpty">no raw data</span>'}</div><div class="smartTags">${tags.map(t=>`<span class="smartTag">${esc(t)}</span>`).join('')}</div><div class="footer"><a href="${escAttr(p.product_url||'#')}" target="_blank">Product</a>${connectAction}${calcAction}<button class="removeDashboardBtn" data-item="${esc(p.item_id||'')}">Remove</button></div></div></div>`}
function cardHtml(p,context){const isAliDash=context==='dashboard'&&p.dashboard_source==='alibaba_search';const ph=`https://www.ebay.co.uk/bin/purchaseHistory?item=${encodeURIComponent(p.item_id)}`;const ship=isAliDash?'Alibaba product':(p.shipping_type==='free'?'Free postage':(p.shipping_type==='paid'?'Postage '+(p.shipping_cost_text||''):(p.postage_text||'Postage unknown')));const watch=isAliDash?(p.watch_count_text||'Alibaba selected'):(p.watch_count?('Watching: '+p.watch_count):'Watching: unknown');const pageMeta=p.search_group_name?`<div class="meta">Group: ${esc(p.search_group_name)}</div>`:'';const storeActions=context==='store'?`<button class="similarTitleBtn" data-item="${esc(p.item_id||'')}" data-title="${esc(p.title||'')}">Title Search</button><button class="similarImageBtn" data-item="${esc(p.item_id||'')}" data-title="${esc(p.title||'')}" data-image="${esc(p.image_url||'')}" ${p.image_url?'':'disabled'}>Image Search</button>`:'';const searchActions=context==='search'?`<button class="alibabaTitleBtn" data-item="${esc(p.item_id||'')}" data-title="${esc(p.title||'')}">Ali Title</button><button class="alibabaImageBtn" data-item="${esc(p.item_id||'')}" data-title="${esc(p.title||'')}" data-image="${esc(p.image_url||'')}" ${p.image_url?'':'disabled'}>Ali Image</button><button class="selectDashboardBtn" data-item="${esc(p.item_id||'')}" data-group="${esc(p.search_group_name||'')}" ${Number(p.dashboard_selected||0)?'disabled':''}>${Number(p.dashboard_selected||0)?'Selected':'Select'}</button>`:'';const calcAction=context==='dashboard'&&Number(p.is_connected||0)&&p.calc_link_id?`<button class="calcDashboardBtn" data-link="${esc(p.calc_link_id)}">Calculator</button>`:'';const dashConnectAction=context==='dashboard'&&Number(p.is_connected||0)&&p.calc_link_id?`<button class="unlinkDashboardBtn" data-link="${esc(p.calc_link_id)}">Unlink</button>`:`<button class="connectDashboardBtn" data-item="${esc(p.item_id||'')}" data-source="${esc(p.dashboard_source||'ebay_search')}">Connect</button>`;const dashActions=context==='dashboard'?`${dashConnectAction}${calcAction}<button class="removeDashboardBtn" data-item="${esc(p.item_id||'')}">Remove</button>`:'';const historyLink=isAliDash?'':`<a href="${ph}" target="_blank">Purchase History</a>`;const varBtn=isAliDash?'':`<button class="varMiniBtn varMiniOl" data-var-item="${esc(p.item_id||'')}" data-var-title="${esc(p.title||'')}" title="Analyse with Ollama">OL</button><button class="varMiniBtn varMiniGe" data-var-item="${esc(p.item_id||'')}" data-var-title="${esc(p.title||'')}" title="Analyse with Gemini">GE</button>`;return `<div class="card"><div class="imageBox">${p.image_url?`<img src="${esc(p.image_url)}" loading="lazy"/>`:'<div class="noimg">No image</div>'}</div><div class="body"><div class="pTitle">${esc(shortTitle(p.title))}</div><div class="topLine"><span class="sellerTag">${esc(p.seller_username||'unknown')}</span><span class="price">${esc(p.price_text||'Price unknown')}</span></div><div class="metric"><span>${esc(soldText(p))}</span><span>${esc(availableText(p))}</span></div><div class="metaArea"><div class="meta">${esc(ship)}</div><div class="meta">${esc(watch)}</div>${pageMeta}</div><div class="sales"><div class="pill${Number(p.sold_yesterday)>0?' hot':''}"><strong>${p.sold_yesterday||0}</strong><span>Yesterday</span></div><div class="pill${Number(p.sold_7_days)>0?' hot':''}"><strong>${p.sold_7_days||0}</strong><span>7 Days</span></div><div class="pill${Number(p.sold_30_days)>0?' hot':''}"><strong>${p.sold_30_days||0}</strong><span>30 Days</span></div><div class="pill${Number(p.tracked_total_quantity)>0?' hot':''}"><strong>${p.tracked_total_quantity||0}</strong><span>${esc(spanText(p))}</span></div></div></div><div class="footer"><a href="${esc(p.product_url||'#')}" target="_blank">Product</a>${historyLink}${varBtn}${storeActions}${searchActions}${dashActions}</div></div>`;}
function renderCards(){const ali=isAlibaba();const q=document.getElementById(ali?'aliSearch':'search').value.toLowerCase().trim();const sort=document.getElementById(ali?'aliSort':'sort').value;const filter=document.getElementById(ali?'aliFilter':'filter').value;let items=[...allCards];if(q)items=items.filter(p=>String(p.title||'').toLowerCase().includes(q)||String(p.item_id||p.product_key||'').includes(q)||String(p.supplier_name||'').toLowerCase().includes(q));if(isAlibaba()){renderAlibabaFilters(items);items=applyAlibabaLocalFilters(items);if(filter==='needs_review')items=items.filter(needsAlibabaReview);if(filter==='verified')items=items.filter(isAlibabaVerified);items.sort((a,b)=>{if(sort==='price_asc')return Number(a.min_price||parsePrice(a.price_text)||999999)-Number(b.min_price||parsePrice(b.price_text)||999999);if(sort==='moq_asc')return (minOrderNumber(a)||999999)-(minOrderNumber(b)||999999);if(sort==='supplier')return String(a.supplier_name||'').localeCompare(String(b.supplier_name||''))||String(a.title||'').localeCompare(String(b.title||''));return 0});}else{renderAlibabaFilters([]);if(filter==='sold')items=items.filter(p=>Number(p.sold_30_days||0)>0);if(filter==='nosales')items=items.filter(p=>Number(p.sold_30_days||0)===0);items.sort((a,b)=>{if(sort==='sold30')return Number(b.sold_30_days||0)-Number(a.sold_30_days||0);if(sort==='sold7')return Number(b.sold_7_days||0)-Number(a.sold_7_days||0);if(sort==='yesterday')return Number(b.sold_yesterday||0)-Number(a.sold_yesterday||0);if(sort==='price')return parsePrice(b.price_text)-parsePrice(a.price_text);if(sort==='title')return String(a.title||'').localeCompare(String(b.title||''));return 0});}
const stId=ali?'aliSt':'st';document.getElementById(stId+'Products').textContent=items.length;if(ali){const suppliers=new Set(items.map(p=>p.supplier_name).filter(Boolean));const prices=items.map(p=>Number(p.min_price||parsePrice(p.price_text))).filter(Boolean);document.getElementById('aliStSuppliers').textContent=suppliers.size;document.getElementById('aliStMinPrice').textContent=prices.length?money(Math.min(...prices)):'£0.00';document.getElementById('aliStAvgPrice').textContent=prices.length?money(prices.reduce((a,b)=>a+b,0)/prices.length):'£0.00';document.getElementById('aliStSold').textContent=items.reduce((a,p)=>a+Number(p.sold_count||0),0);document.getElementById('aliStVerified').textContent=items.filter(isAlibabaVerified).length;}else{document.getElementById('stYesterday').textContent=items.reduce((a,p)=>a+Number(p.sold_yesterday||0),0);document.getElementById('st7Qty').textContent=items.reduce((a,p)=>a+Number(p.sold_7_days||0),0);document.getElementById('st30Qty').textContent=items.reduce((a,p)=>a+Number(p.sold_30_days||0),0);document.getElementById('st30Rev').textContent=money(items.reduce((a,p)=>a+Number(p.revenue_30_days||0),0));document.getElementById('stTotalRev').textContent=money(items.reduce((a,p)=>a+Number(p.tracked_total_revenue||0),0));}
const grid=document.getElementById(ali?'aliGrid':'grid'),empty=document.getElementById(ali?'aliEmpty':'empty');grid.innerHTML='';empty.classList.toggle('hidden',items.length>0);empty.textContent=q||activeAlibabaFilters.size?'No products match current filters.':(activeTab==='alibaba'?'No Alibaba products yet. Open an Alibaba search page and click the extension.':activeTab==='ebaySearch'?'No eBay Search products yet. Open an eBay search page and click the extension.':'No products yet. Open an eBay store/search page, run the server, then click the extension.');for(const p of items){grid.insertAdjacentHTML('beforeend',isAlibaba()?alibabaCardHtml(p):cardHtml(p,activeTab==='store'?'store':'search'));}grid.querySelectorAll('.varBtn').forEach(btn=>btn.addEventListener('click',(e)=>{e.preventDefault();e.stopPropagation();openVariationModal(btn.dataset.varItem,btn.dataset.varTitle);}));grid.querySelectorAll('.similarTitleBtn').forEach(btn=>btn.addEventListener('click',()=>openSimilarTitleSearch(btn.dataset.item,btn.dataset.title)));grid.querySelectorAll('.similarImageBtn').forEach(btn=>btn.addEventListener('click',()=>openSimilarImageSearch(btn.dataset.item,btn.dataset.title,btn.dataset.image)));grid.querySelectorAll('.alibabaTitleBtn').forEach(btn=>btn.addEventListener('click',()=>openAlibabaTitleSearch(btn.dataset.item,btn.dataset.title)));grid.querySelectorAll('.alibabaImageBtn').forEach(btn=>btn.addEventListener('click',()=>openAlibabaImageSearch(btn.dataset.item,btn.dataset.title,btn.dataset.image)));grid.querySelectorAll('.selectDashboardBtn').forEach(btn=>btn.addEventListener('click',()=>selectForDashboard(btn.dataset.item,btn.dataset.group)));grid.querySelectorAll('.selectAlibabaDashboardBtn').forEach(btn=>btn.addEventListener('click',()=>selectAlibabaForDashboard(btn.dataset.key,btn.dataset.group)));bindAlibabaManualInputs(grid);}
async function loadProgress(){try{const st=await fetch('/api/scan-status?t='+Date.now()).then(r=>r.json());const ali=isAlibaba();const match=ali?(st.queue_type==='alibaba'):(st.queue_type!=='alibaba');const pfx=ali?'aliSt':'st';document.getElementById(pfx+'Done').textContent=match?(st.done||0):0;document.getElementById(pfx+'Remain').textContent=match?(st.remaining||0):0;document.getElementById(pfx+'TotalQueue').textContent=match?(st.total||0):0;document.getElementById(pfx+'RunState').textContent=match?(st.running?'running':'idle'):'idle';}catch(e){console.warn('Could not load scan status',e)}}document.getElementById('calcModalClose')?.addEventListener('click',closeProfitModal);document.getElementById('calcModalOverlay')?.addEventListener('click',e=>{if(e.target.id==='calcModalOverlay')closeProfitModal()});document.addEventListener('keydown',e=>{if(e.key==='Escape')closeProfitModal()});document.querySelectorAll('#calcModalOverlay input[type="number"]').forEach(inp=>{inp.addEventListener('focus',()=>setTimeout(()=>inp.select(),0));inp.addEventListener('click',()=>inp.select())});updateSideLabels();loadSideList();setInterval(loadProgress,2500);
showTab('tab1');

// ===== AI ASSISTANT TAB =====
var aiApiKey = '';
async function loadAiStats(){
  try{
    var r = await fetch('/api/ai/stats'); var d = await r.json();
    document.getElementById('aiDataBadge').textContent =
      d.ebay_products + ' eBay products | ' + d.alibaba_products + ' Alibaba products | ' + d.sales + ' sales';
  }catch(e){ document.getElementById('aiDataBadge').textContent = 'Data unavailable'; }
}
async function loadQuickPrompts(){
  try{
    var r = await fetch('/api/ai/quick-prompts'); var d = await r.json();
    var row = document.getElementById('aiQuickRow'); row.innerHTML='';
    Object.keys(d.prompts).forEach(function(key){
      var btn = document.createElement('button');
      btn.className='aiQuickBtn';
      var labels = {best_product:'Best Products to Sell',compare_suppliers:'Compare Suppliers',profit_analysis:'Profit Analysis',trending:'Trending Products',market_gaps:'Find Market Gaps'};
      btn.textContent = labels[key] || key;
      btn.onclick = function(){ runQuickAnalysis(key); };
      row.appendChild(btn);
    });
  }catch(e){}
}
async function askAi(question){
  var result = document.getElementById('aiResult');
  var model = document.getElementById('aiModel').value;
  var key = document.getElementById('aiApiKey').value.trim();
  result.innerHTML = '<div class="aiLoading"><div class="aiSpin"></div>AI is analyzing your database...</div>';
  try{
    var r = await fetch('/api/ai/analyze',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({question:question, api_key:key, model:model})
    });
    var d = await r.json();
    if(d.ok){
      result.innerHTML = '<div class="aiAnswer">'+d.answer.replace(/\n/g,'<br>')+'</div>';
    } else {
      result.innerHTML = '<div class="aiError">'+d.error+'</div>';
    }
  }catch(e){
    result.innerHTML = '<div class="aiError">Connection error: '+e.message+'</div>';
  }
}
async function runQuickAnalysis(key){
  var result = document.getElementById('aiResult');
  var model = document.getElementById('aiModel').value;
  var keyInput = document.getElementById('aiApiKey').value.trim();
  result.innerHTML = '<div class="aiLoading"><div class="aiSpin"></div>AI is analyzing your database...</div>';
  try{
    var r = await fetch('/api/ai/quick',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({prompt_key:key, api_key:keyInput, model:model})
    });
    var d = await r.json();
    if(d.ok){
      result.innerHTML = '<div class="aiAnswer">'+d.answer.replace(/\n/g,'<br>')+'</div>';
    } else {
      result.innerHTML = '<div class="aiError">'+d.error+'</div>';
    }
  }catch(e){
    result.innerHTML = '<div class="aiError">Connection error: '+e.message+'</div>';
  }
}
document.addEventListener('DOMContentLoaded', function(){
  loadAiStats(); loadQuickPrompts();
  var askBtn = document.getElementById('aiAskBtn');
  var questionInput = document.getElementById('aiQuestion');
  if(askBtn){
    askBtn.onclick = function(){
      var q = questionInput.value.trim();
      if(q) askAi(q);
    };
    questionInput.addEventListener('keydown', function(e){
      if(e.key==='Enter'){ var q=questionInput.value.trim(); if(q) askAi(q); }
    });
  }
});


// ===== OLLAMA STATUS CHECK =====
async function checkOllama() {
  var badge = document.getElementById('ollamaStatus');
  if(!badge) return;
  try {
    var r = await fetch('/api/ollama-status');
    var d = await r.json();
    if(d.ok) {
      badge.className = 'ollamaBadge ok';
      badge.title = 'Ollama: Connected' + (d.models && d.models.length ? ' (' + d.models.join(', ') + ')' : '');
    } else {
      badge.className = 'ollamaBadge fail';
      badge.title = 'Ollama: Not connected';
    }
  } catch(e) {
    badge.className = 'ollamaBadge fail';
    badge.title = 'Ollama: Not connected';
  }
}
checkOllama();
setInterval(checkOllama, 30000);

// ===== CHAT SIDEBAR =====
var _chatConvId=null,_chatSending=false;
async function loadChatConversations(){
  try{
    var r=await fetch('/api/chat/conversations');var d=await r.json();
    var box=document.getElementById('chatConvList');if(!box)return;
    box.innerHTML='';
    if(!d.ok||!d.conversations||d.conversations.length===0){
      box.innerHTML='<div style="color:var(--muted);font-size:11px;padding:6px">No conversations yet.</div>';
      return;
    }
    d.conversations.forEach(function(c){
      var active=(_chatConvId&&String(_chatConvId)===String(c.id));
      var div=document.createElement('div');
      div.className='chatConvItem'+(active?' active':'');
      div.innerHTML='<span class="chatConvTitle">'+esc(c.title||'Untitled')+'</span><button class="chatConvDel" onclick="deleteChatConversation('+c.id+');event.stopPropagation();" title="Delete">&times;</button>';
      div.onclick=(function(cid){return function(){openChatConversation(cid);};})(c.id);
      box.appendChild(div);
    });
  }catch(e){console.warn('loadChatConversations error',e)}
}
function openChatConversation(convId){
  _chatConvId=convId;
  var area=document.getElementById('chatArea');if(!area)return;
  area.innerHTML='<div class="chatLoading"><div class="chatSpin"></div> Loading messages...</div>';
  fetch('/api/chat/messages?conversation_id='+encodeURIComponent(convId)).then(r=>r.json()).then(function(d){
    if(!d.ok){area.innerHTML='<div class="chatMsg sys">Error: '+esc(d.error||'')+'</div>';return}
    area.innerHTML='';
    if(!d.messages||d.messages.length===0){
      area.innerHTML='<div class="chatEmpty">No messages yet. Type below to start.</div>';
    }else{
      d.messages.forEach(function(m){
        var cls=m.role==='user'?'user':(m.role==='assistant'?'ai':'sys');
        var _d=/[\u0600-\u06FF]/.test(m.content)?'rtl':'ltr';
        area.insertAdjacentHTML('beforeend','<div class="chatMsg '+cls+'" style="direction:'+_d+';text-align:'+(_d==='rtl'?'right':'left')+'">'+esc(m.content)+'</div>');
      });
    }
    area.scrollTop=area.scrollHeight;
    loadChatConversations();
  }).catch(function(e){
    area.innerHTML='<div class="chatMsg sys">Connection error: '+esc(e.message)+'</div>';
  });
}
async function startNewChat(){
  var input=document.getElementById('chatInput');
  var msg=input?input.value.trim():'';
  if(!msg)return;
  _chatSending=true;var sendBtn=document.getElementById('chatSendBtn');if(sendBtn)sendBtn.disabled=true;input.value='';
  try{
    var cr=await fetch('/api/chat/conversations',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:msg.slice(0,50)})});
    var cd=await cr.json();
    if(!cd.ok){alert('Could not create conversation: '+esc(cd.error||''));_chatSending=false;if(sendBtn)sendBtn.disabled=false;return}
    _chatConvId=cd.conversation_id;
    var area=document.getElementById('chatArea');
    area.innerHTML='';
    var _dir1=/[\u0600-\u06FF]/.test(msg)?'rtl':'ltr';area.insertAdjacentHTML('beforeend','<div class="chatMsg user" style="direction:'+_dir1+';text-align:'+(_dir1==='rtl'?'right':'left')+'">'+esc(msg)+'</div>');
    area.insertAdjacentHTML('beforeend','<div class="chatLoading" id="chatLoadingIndicator"><div class="chatSpin"></div> AI is thinking...</div>');
    area.scrollTop=area.scrollHeight;
    var res=await fetch('/api/chat/send',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({conversation_id:_chatConvId,message:msg})});
    var d=await res.json();
    var le=document.getElementById('chatLoadingIndicator');if(le)le.remove();
    if(d.ok){var _dir2=/[\u0600-\u06FF]/.test(d.response)?'rtl':'ltr';area.insertAdjacentHTML('beforeend','<div class="chatMsg ai" style="direction:'+_dir2+';text-align:'+(_dir2==='rtl'?'right':'left')+'">'+esc(d.response)+'</div>');area.scrollTop=area.scrollHeight;loadChatConversations();}
    else{area.insertAdjacentHTML('beforeend','<div class="chatMsg sys">Error: '+esc(d.error||'Unknown')+'</div>');area.scrollTop=area.scrollHeight;}
  }catch(e){
    var le2=document.getElementById('chatLoadingIndicator');if(le2)le2.remove();
    var area2=document.getElementById('chatArea');
    area2.insertAdjacentHTML('beforeend','<div class="chatMsg sys">Connection error: '+esc(e.message)+'</div>');
  }
  _chatSending=false;if(sendBtn)sendBtn.disabled=false;if(input)input.focus();
}
async function sendChatMessage(){
  if(_chatSending)return;
  var input=document.getElementById('chatInput'),sendBtn=document.getElementById('chatSendBtn'),area=document.getElementById('chatArea');
  if(!input||!input.value.trim())return;
  var msg=input.value.trim();_chatSending=true;sendBtn.disabled=true;input.value='';
  if(!_chatConvId){
    try{
      var cr=await fetch('/api/chat/conversations',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:msg.slice(0,50)})});
      var cd=await cr.json();
      if(cd.ok){_chatConvId=cd.conversation_id}
      else{area.insertAdjacentHTML('beforeend','<div class="chatMsg sys">Error: '+esc(cd.error||'')+'</div>');_chatSending=false;sendBtn.disabled=false;return}
    }catch(e){area.insertAdjacentHTML('beforeend','<div class="chatMsg sys">Error: '+esc(e.message)+'</div>');_chatSending=false;sendBtn.disabled=false;return}
  }
  if(area.querySelector('.chatEmpty'))area.innerHTML='';
  var _dir1=/[\u0600-\u06FF]/.test(msg)?'rtl':'ltr';area.insertAdjacentHTML('beforeend','<div class="chatMsg user" style="direction:'+_dir1+';text-align:'+(_dir1==='rtl'?'right':'left')+'">'+esc(msg)+'</div>');
  area.insertAdjacentHTML('beforeend','<div class="chatLoading" id="chatLoadingIndicator"><div class="chatSpin"></div> AI is thinking...</div>');
  area.scrollTop=area.scrollHeight;
  try{
    var res=await fetch('/api/chat/send',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({conversation_id:_chatConvId,message:msg})});
    var d=await res.json();
    var le=document.getElementById('chatLoadingIndicator');if(le)le.remove();
    if(d.ok){var _dir2=/[\u0600-\u06FF]/.test(d.response)?'rtl':'ltr';area.insertAdjacentHTML('beforeend','<div class="chatMsg ai" style="direction:'+_dir2+';text-align:'+(_dir2==='rtl'?'right':'left')+'">'+esc(d.response)+'</div>');area.scrollTop=area.scrollHeight;loadChatConversations();}
    else{area.insertAdjacentHTML('beforeend','<div class="chatMsg sys">Error: '+esc(d.error||'Unknown')+'</div>');area.scrollTop=area.scrollHeight;}
  }catch(e){
    var le2=document.getElementById('chatLoadingIndicator');if(le2)le2.remove();
    area.insertAdjacentHTML('beforeend','<div class="chatMsg sys">Connection error: '+esc(e.message)+'</div>');
  }
  _chatSending=false;sendBtn.disabled=false;if(input)input.focus();
}
async function deleteChatConversation(convId){
  if(!confirm('Delete this conversation?'))return;
  try{
    await fetch('/api/chat/conversations?conversation_id='+encodeURIComponent(convId),{method:'DELETE'});
    if(_chatConvId&&String(_chatConvId)===String(convId)){
      _chatConvId=null;
      var area=document.getElementById('chatArea');
      if(area)area.innerHTML='<div class="chatEmpty">Conversation deleted. Click "+ New Chat" to start a new one.</div>';
    }
    loadChatConversations();
  }catch(e){console.warn('Failed to delete conversation',e)}
}
document.addEventListener('DOMContentLoaded',function(){
  var newBtn=document.getElementById('chatNewBtn');
  var sendBtn=document.getElementById('chatSendBtn');
  var input=document.getElementById('chatInput');
  if(newBtn)newBtn.addEventListener('click',function(){
    _chatConvId=null;
    var area=document.getElementById('chatArea');
    if(area)area.innerHTML='<div class="chatEmpty">Type a message below to start a new chat.</div>';
    if(input)input.focus();
  });
  if(sendBtn)sendBtn.addEventListener('click',sendChatMessage);
  if(input){
    input.addEventListener('keydown',function(e){
      if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendChatMessage();}
    });
  }
});


// ===== GEMINI CHAT =====
var _geminiHistory=[],_geminiSending=false,_geminiConvId=null;

// ===== GEMINI CHAT SIDEBAR =====
async function loadGeminiConversations(){
  try{
    var r=await fetch('/api/gemini/conversations');var d=await r.json();
    var box=document.getElementById('geminiConvList');if(!box)return;
    box.innerHTML='';
    if(!d.ok||!d.conversations||d.conversations.length===0){
      box.innerHTML='<div style="color:var(--muted);font-size:11px;padding:6px">No Gemini conversations yet.</div>';
      return;
    }
    d.conversations.forEach(function(c){
      var active=(_geminiConvId&&String(_geminiConvId)===String(c.id));
      var div=document.createElement('div');
      div.className='chatConvItem'+(active?' active':'');
      div.innerHTML='<span class="chatConvTitle">'+esc(c.title||'Untitled')+'</span><button class="chatConvDel" onclick="deleteGeminiConversation('+c.id+');event.stopPropagation();" title="Delete">&times;</button>';
      div.onclick=(function(cid){return function(){openGeminiConversation(cid);};})(c.id);
      box.appendChild(div);
    });
  }catch(e){console.warn('loadGeminiConversations error',e)}
}

function openGeminiConversation(convId){
  _geminiConvId=convId;
  var area=document.getElementById('geminiChatArea');if(!area)return;
  area.innerHTML='<div class="chatLoading"><div class="chatSpin"></div> Loading Gemini messages...</div>';
  fetch('/api/gemini/messages?conversation_id='+encodeURIComponent(convId)).then(r=>r.json()).then(function(d){
    if(!d.ok){area.innerHTML='<div class="chatMsg sys">Error: '+esc(d.error||'')+'</div>';return}
    area.innerHTML='';
    _geminiHistory=[];
    if(!d.messages||d.messages.length===0){
      area.innerHTML='<div class="chatEmpty">Type a message below to chat with Google Gemini about your products.</div>';
    }else{
      d.messages.forEach(function(m){
        var cls=m.role==='user'?'user':'ai';
        var _d=/[؀-ۿ]/.test(m.content)?'rtl':'ltr';
        area.insertAdjacentHTML('beforeend','<div class="chatMsg '+cls+'" style="direction:'+_d+';text-align:'+(_d==='rtl'?'right':'left')+'">'+esc(m.content)+'</div>');
        _geminiHistory.push({role:m.role==='user'?'user':'model',content:m.content});
      });
    }
    area.scrollTop=area.scrollHeight;
    loadGeminiConversations();
  }).catch(function(e){
    area.innerHTML='<div class="chatMsg sys">Connection error: '+esc(e.message)+'</div>';
  });
}

async function deleteGeminiConversation(convId){
  if(!confirm('Delete this Gemini conversation?'))return;
  try{
    await fetch('/api/gemini/conversations?conversation_id='+encodeURIComponent(convId),{method:'DELETE'});
    if(_geminiConvId&&String(_geminiConvId)===String(convId)){
      _geminiConvId=null;
      _geminiHistory=[];
      var area=document.getElementById('geminiChatArea');
      if(area)area.innerHTML='<div class="chatEmpty">Conversation deleted. Click "+ New Chat" to start a new one.</div>';
    }
    loadGeminiConversations();
  }catch(e){console.warn('Failed to delete Gemini conversation',e)}
}

function geminiAddMsg(role,text){
  var area=document.getElementById('geminiChatArea');if(!area)return;
  if(area.querySelector('.chatEmpty'))area.innerHTML='';
  var cls=role==='user'?'user':'ai';
  var d=/[؀-ۿ]/.test(text)?'rtl':'ltr';
  area.insertAdjacentHTML('beforeend','<div class="chatMsg '+cls+'" style="direction:'+d+';text-align:'+(d==='rtl'?'right':'left')+'">'+esc(text)+'</div>');
  area.scrollTop=area.scrollHeight;
}
async function sendGeminiMessage(){
  if(_geminiSending)return;
  var input=document.getElementById('geminiChatInput'),sendBtn=document.getElementById('geminiSendBtn'),area=document.getElementById('geminiChatArea');
  if(!input||!input.value.trim())return;
  var msg=input.value.trim();
  var apiKey=document.getElementById('geminiApiKey')?document.getElementById('geminiApiKey').value.trim():'';
  var model=document.getElementById('geminiModel')?document.getElementById('geminiModel').value:'gemini-3-flash-preview';
  if(!apiKey){alert('Please enter your Gemini API Key first.');return;}
  _geminiSending=true;sendBtn.disabled=true;input.value='';

  // Create conversation if none selected
  if(!_geminiConvId){
    try{
      var cr=await fetch('/api/gemini/conversations',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:msg.slice(0,50)})});
      var cd=await cr.json();
      if(cd.ok){_geminiConvId=cd.conversation_id;loadGeminiConversations();}
      else{area.insertAdjacentHTML('beforeend','<div class="chatMsg sys">Error: '+esc(cd.error||'')+'</div>');_geminiSending=false;sendBtn.disabled=false;return}
    }catch(e){area.insertAdjacentHTML('beforeend','<div class="chatMsg sys">Error: '+esc(e.message)+'</div>');_geminiSending=false;sendBtn.disabled=false;return}
  }

  if(area.querySelector('.chatEmpty'))area.innerHTML='';
  geminiAddMsg('user',msg);
  area.insertAdjacentHTML('beforeend','<div class="chatLoading" id="geminiLoadingIndicator"><div class="chatSpin"></div> Gemini is thinking...</div>');
  area.scrollTop=area.scrollHeight;
  try{
    var res=await fetch('/api/gemini/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg,api_key:apiKey,model:model,history:_geminiHistory,conversation_id:_geminiConvId})});
    var d=await res.json();
    var le=document.getElementById('geminiLoadingIndicator');if(le)le.remove();
    if(d.ok){
      geminiAddMsg('ai',d.response);
      _geminiHistory.push({role:'user',content:msg});
      _geminiHistory.push({role:'model',content:d.response});
      if(d.conversation_id){_geminiConvId=d.conversation_id;loadGeminiConversations();}
      // If Gemini executed dashboard actions, refresh the dashboard tab
      if(d.dashboard_updated && d.actions_executed && d.actions_executed.length > 0){
        var actionSummary = d.actions_executed.map(function(a){
          return (a.type === 'ADD_EBAY' ? 'eBay' : 'Alibaba') + ': ' + a.id;
        }).join(', ');
        // Auto-refresh dashboard products if the dashboard tab is loaded
        if(typeof loadDashboardProducts === 'function') loadDashboardProducts();
        else if(typeof renderDashboard === 'function') renderDashboard();
        // Append action confirmation below the message
        var confirmDiv = document.createElement('div');
        confirmDiv.style.cssText = 'background:#064e3b;border:1px solid #059669;border-radius:8px;padding:8px 12px;margin-top:6px;font-size:12px;color:#6ee7b7';
        confirmDiv.textContent = '✓ داشبورد آپدیت شد: ' + d.actions_executed.length + ' محصول اضافه شد (' + actionSummary + ')';
        var lastMsg = area.lastElementChild;
        if(lastMsg) lastMsg.appendChild(confirmDiv);
      }
      if(d.action_errors && d.action_errors.length > 0){
        var errDiv = document.createElement('div');
        errDiv.style.cssText = 'background:#7f1d1d;border:1px solid #b91c1c;border-radius:8px;padding:8px 12px;margin-top:4px;font-size:12px;color:#fca5a5';
        errDiv.textContent = '⚠ برخی محصولات اضافه نشدند: ' + d.action_errors.map(function(e){return e.id + ' (' + e.error + ')';}).join(', ');
        var lastMsg2 = area.lastElementChild;
        if(lastMsg2) lastMsg2.appendChild(errDiv);
      }
    }else{
      area.insertAdjacentHTML('beforeend','<div class="chatMsg sys">Error: '+esc(d.error||'Unknown')+'</div>');
      area.scrollTop=area.scrollHeight;
    }
  }catch(e){
    var le2=document.getElementById('geminiLoadingIndicator');if(le2)le2.remove();
    area.insertAdjacentHTML('beforeend','<div class="chatMsg sys">Connection error: '+esc(e.message)+'</div>');
  }
  _geminiSending=false;sendBtn.disabled=false;if(input)input.focus();
}
document.addEventListener('DOMContentLoaded',function(){
  var geminiSend=document.getElementById('geminiSendBtn');
  var geminiInput=document.getElementById('geminiChatInput');
  var geminiNewBtn=document.getElementById('geminiNewBtn');
  if(geminiNewBtn)geminiNewBtn.addEventListener('click',function(){
    _geminiConvId=null;
    _geminiHistory=[];
    var area=document.getElementById('geminiChatArea');
    if(area)area.innerHTML='<div class="chatEmpty">Type a message below to chat with Google Gemini about your products.</div>';
    if(geminiInput)geminiInput.focus();
    loadGeminiConversations();
  });
  var geminiSaveKey=document.getElementById('geminiSaveKey');
  if(geminiSend)geminiSend.addEventListener('click',sendGeminiMessage);
  if(geminiInput){
    geminiInput.addEventListener('keydown',function(e){
      if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendGeminiMessage();}
    });
  }
  if(geminiSaveKey){
    geminiSaveKey.addEventListener('click',function(){
      var keyEl=document.getElementById('geminiApiKey');
      if(keyEl&&keyEl.value.trim()){
        try{var _gk=keyEl.value.trim();localStorage.setItem('gemini_api_key',_gk);var modelEl=document.getElementById('geminiModel');if(modelEl)localStorage.setItem('gemini_model',modelEl.value);fetch('/api/gemini/save-key',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({gemini_key:_gk})}).then(function(r){return r.json()}).then(function(d){}).catch(function(e){});geminiSaveKey.textContent='Saved!';setTimeout(function(){geminiSaveKey.textContent='Save Key';},1500);}catch(e){alert('Saved in session (localStorage not available)');}
      } else { alert('Please enter an API key first.'); }
    });
  }
  // Load saved key from localStorage
  try{
    var savedKey=localStorage.getItem('gemini_api_key');
    if(savedKey){var keyEl=document.getElementById('geminiApiKey');if(keyEl)keyEl.value=savedKey;} var savedModel=localStorage.getItem('gemini_model');if(savedModel){var modelEl=document.getElementById('geminiModel');if(modelEl){for(var i=0;i<modelEl.options.length;i++){if(modelEl.options[i].value===savedModel){modelEl.selectedIndex=i;break;}}}}

  // Test Models button: fetch available models and update dropdown
  var testModelsBtn=document.getElementById('geminiTestModels');
  if(testModelsBtn) testModelsBtn.addEventListener('click', async function(){
    var keyEl=document.getElementById('geminiApiKey');
    var apiKey=keyEl?keyEl.value.trim():'';
    if(!apiKey){alert('Please enter your Gemini API Key first.');return;}
    testModelsBtn.textContent='Loading...';testModelsBtn.disabled=true;
    try{
      var res=await fetch('/api/gemini/models',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({api_key:apiKey})});
      var d=await res.json();
      if(d.ok&&d.models&&d.models.length){
        var modelEl=document.getElementById('geminiModel');
        if(modelEl){
          // Keep current selection
          var currentVal=modelEl.value;
          modelEl.innerHTML='';
          for(var i=0;i<d.models.length;i++){
            var opt=document.createElement('option');
            opt.value=d.models[i];
            opt.textContent=d.models[i];
            modelEl.appendChild(opt);
          }
          // Restore selection if still available
          var found=false;
          for(var i=0;i<modelEl.options.length;i++){
            if(modelEl.options[i].value===currentVal){modelEl.selectedIndex=i;found=true;break;}
          }
          if(!found&&modelEl.options.length>0){modelEl.selectedIndex=0;}
          alert('Found '+d.models.length+' available models. Dropdown updated.\n\n'+d.models.join(', '));
        }
      } else {
        alert('Error: '+(d.error||'No models found'));
      }
    } catch(e){
      alert('Failed to fetch models: '+e.message);
    } finally {
      testModelsBtn.textContent='Test Models';testModelsBtn.disabled=false;
    }
  });
  }catch(e){}
});

// ===== VARIATION STATS MODAL =====
var _varModalItemId = null;
function openVariationModal(itemId, title) {
  _varModalItemId = itemId;
  var overlay = document.getElementById('varModalOverlay');
  var subtitle = document.getElementById('varModalSubtitle');
  var body = document.getElementById('varModalBody');
  var aiResult = document.getElementById('varAiResult');
  var aiLoading = document.getElementById('varAiLoading');
  var aiBtn = document.getElementById('varAiBtn');
  subtitle.textContent = title ? shortTitle(title) : 'Per-variation sales breakdown';
  body.innerHTML = '<div class="calcHint">Loading variation data...</div>';
  aiResult.classList.remove('show');
  aiResult.innerHTML = '';
  aiLoading.classList.remove('show');
  aiBtn.disabled = false;
  overlay.classList.remove('hidden');
  loadVariationData(itemId);
}
function closeVariationModal() {
  document.getElementById('varModalOverlay').classList.add('hidden');
  _varModalItemId = null;
}
async function loadVariationData(itemId) {
  try {
    var r = await fetch('/api/variation-stats?item_id=' + encodeURIComponent(itemId));
    var d = await r.json();
    var body = document.getElementById('varModalBody');
    if (!d.variations || d.variations.length === 0) {
      body.innerHTML = '<div class="empty">No sales/variation data found for this product. Make sure Purchase History has been scanned.</div>';
      document.getElementById('varAiBtn').disabled = true;
      return;
    }
    // Summary tiles
    var summary = '<div class="varSummary">' +
      '<div class="stat"><div class="label">Variations</div><div class="value">' + d.total_variations + '</div></div>' +
      '<div class="stat"><div class="label">Total Sales</div><div class="value">' + d.total_sales + '</div></div>' +
      '<div class="stat"><div class="label">Total Qty</div><div class="value">' + d.total_quantity + '</div></div>' +
      '<div class="stat"><div class="label">Revenue</div><div class="value">\u00A3' + Number(d.total_revenue).toFixed(2) + '</div></div>' +
      '</div>';
    // Table
    var maxQty = Math.max.apply(null, d.variations.map(function(v){return v.total_quantity || 0;}));
    var rows = d.variations.map(function(v) {
      var pct = maxQty > 0 ? Math.round((v.total_quantity / maxQty) * 100) : 0;
      return '<tr>' +
        '<td>' + esc(v.variation_name) + '</td>' +
        '<td class="num">' + v.sales_count + '</td>' +
        '<td class="num">' + v.total_quantity + '</td>' +
        '<td class="num">\u00A3' + Number(v.total_revenue).toFixed(2) + '</td>' +
        '<td class="barCell"><div class="varBar"><div class="varBarFill" style="width:' + pct + '%"></div></div></td>' +
        '<td>' + esc(v.earliest_sale_text || 'N/A') + '</td>' +
        '<td>' + esc(v.latest_sale_text || 'N/A') + '</td>' +
        '</tr>';
    }).join('');
    var table = '<table class="varTable"><thead><tr><th>Variation</th><th>Sales</th><th>Qty</th><th>Revenue</th><th>Distribution</th><th>First Sale</th><th>Last Sale</th></tr></thead><tbody>' + rows + '</tbody></table>';
    body.innerHTML = summary + table;
  } catch(e) {
    document.getElementById('varModalBody').innerHTML = '<div class="aiError">Error loading data: ' + e.message + '</div>';
  }
}
async function analyzeVariations() {
  if (!_varModalItemId) return;
  var aiBtn = document.getElementById('varAiBtn');
  var aiLoading = document.getElementById('varAiLoading');
  var aiResult = document.getElementById('varAiResult');
  aiBtn.disabled = true;
  aiLoading.classList.add('show');
  aiResult.classList.remove('show');
  aiResult.innerHTML = '';
  try {
    var r = await fetch('/api/variation-analysis', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({item_id: _varModalItemId})
    });
    var d = await r.json();
    aiLoading.classList.remove('show');
    if (d.ok) {
      aiResult.innerHTML = d.analysis.replace(/\n/g, '<br>');
      aiResult.classList.add('show');
    } else {
      aiResult.innerHTML = '<span style="color:#fca5a5">' + esc(d.error || 'Unknown error') + '</span>';
      aiResult.classList.add('show');
    }
  } catch(e) {
    aiLoading.classList.remove('show');
    aiResult.innerHTML = '<span style="color:#fca5a5">Connection error: ' + e.message + '</span>';
    aiResult.classList.add('show');
  }
  aiBtn.disabled = false;
}
document.getElementById('varModalClose')?.addEventListener('click', closeVariationModal);
document.getElementById('varModalOverlay')?.addEventListener('click', function(e){if(e.target.id==='varModalOverlay')closeVariationModal();});

// ===== BATCH VARIATION ANALYSIS (all cards in eBay Search) =====
var _batchVarRunning = false;
function openBatchVariationModal() {
  var overlay = document.getElementById('batchVarModalOverlay');
  var body = document.getElementById('batchVarBody');
  var aiResult = document.getElementById('batchAiResult');
  aiResult.classList.remove('show');
  aiResult.innerHTML = '';
  // Collect all item_ids from currently displayed cards
  var ids = [];
  var grid = document.getElementById('grid');
  if (grid) {
    grid.querySelectorAll('.varBtn').forEach(function(btn) {
      var id = btn.getAttribute('data-var-item');
      if (id) ids.push(id);
    });
  }
  if (!ids.length) {
    body.innerHTML = '<div class="calcHint" style="color:#fca5a5">No eBay products found on this page. Open an eBay search page and extract products first.</div>';
    overlay.classList.remove('hidden');
    return;
  }
  document.getElementById('batchVarSubtitle').textContent = 'Found ' + ids.length + ' products to analyse';
  body.innerHTML = '<div class="calcHint">Found <b>' + ids.length + '</b> products. Click the button below to send all of them to Ollama for a combined variation analysis.</div>' +
    '<button class="varAiBtn" id="batchStartBtn" onclick="startBatchVariationAnalysis()">Start Analysis</button>' +
    '<div class="batchProgress" id="batchProgress"></div>';
  overlay.classList.remove('hidden');
}
function closeBatchVariationModal() {
  document.getElementById('batchVarModalOverlay').classList.add('hidden');
  _batchVarRunning = false;
}
async function startBatchVariationAnalysis() {
  if (_batchVarRunning) return;
  _batchVarRunning = true;
  var startBtn = document.getElementById('batchStartBtn');
  if (startBtn) startBtn.disabled = true;
  var progressEl = document.getElementById('batchProgress');
  var aiResult = document.getElementById('batchAiResult');
  aiResult.classList.remove('show');
  aiResult.innerHTML = '';
  if (progressEl) progressEl.innerHTML = '<div class="varSpin" style="display:inline-block;vertical-align:middle;margin-right:8px"></div> Collecting item IDs from displayed cards...';
  // Collect item_ids from the DOM (what the user actually sees)
  var ids = [];
  var grid = document.getElementById('grid');
  if (grid) {
    grid.querySelectorAll('.varBtn').forEach(function(btn) {
      var id = btn.getAttribute('data-var-item');
      if (id && ids.indexOf(id) === -1) ids.push(id);
    });
  }
  if (!ids.length) {
    if (progressEl) progressEl.innerHTML = '<span style="color:#fca5a5">No products found.</span>';
    _batchVarRunning = false;
    if (startBtn) startBtn.disabled = false;
    return;
  }
  if (progressEl) progressEl.innerHTML = '<div class="varSpin" style="display:inline-block;vertical-align:middle;margin-right:8px"></div> Sending ' + ids.length + ' products to Ollama for batch analysis...';
  try {
    var _aiProvider = window._aliAiProvider || 'ollama';
  var _geminiKey = '';
  try { _geminiKey = localStorage.getItem('gemini_api_key') || ''; } catch(e) {}
  var _geminiModel = '';
  try { _geminiModel = localStorage.getItem('gemini_model') || ''; } catch(e) {}
  if (!_geminiModel) { var _gmEl = document.getElementById('geminiModel'); if (_gmEl) _geminiModel = _gmEl.value; }
  var r = await fetch('/api/variation-analysis-batch', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({item_ids: ids, ai_provider: _aiProvider, gemini_key: _geminiKey, gemini_model: _geminiModel})
    });
    var d = await r.json();
    if (progressEl) {
      var parts = [];
      parts.push('<span class="done">Analysed: ' + (d.analysed_count || 0) + ' products</span>');
      if (d.skipped_count) parts.push('<span class="skip">Skipped (no data): ' + d.skipped_count + '</span>');
      parts.push('<span>Model: ' + esc(d.model_used || 'unknown') + '</span>');
      progressEl.innerHTML = parts.join(' &nbsp;|&nbsp; ');
    }
    if (d.ok) {
      aiResult.innerHTML = d.analysis.replace(/\n/g, '<br>');
      aiResult.classList.add('show');
    } else {
      aiResult.innerHTML = '<span style="color:#fca5a5">' + esc(d.error || 'Unknown error') + '</span>';
      aiResult.classList.add('show');
    }
  } catch(e) {
    if (progressEl) progressEl.innerHTML = '<span style="color:#fca5a5">Connection error: ' + e.message + '</span>';
  }
  _batchVarRunning = false;
  if (startBtn) startBtn.disabled = false;
}
document.getElementById('batchVarModalClose')?.addEventListener('click', closeBatchVariationModal);
// OL / GE mini buttons on eBay cards
document.addEventListener('click', function(e) {
  var btn = e.target.closest('.varMiniBtn');
  if (!btn) return;
  var itemId = btn.getAttribute('data-var-item');
  var title = btn.getAttribute('data-var-title');
  if (!itemId) return;
  var isGemini = btn.classList.contains('varMiniGe');
  openVarAnalysisWithProvider(itemId, title, isGemini ? 'gemini' : 'ollama');
});
async function openVarAnalysisWithProvider(itemId, title, provider) {
  // Open the existing variation modal first
  openVariationModal(itemId, title);
  // Then auto-trigger AI analysis with the chosen provider
  // Wait for modal to render stats, then fire AI
  setTimeout(async function() {
    var aiResult = document.getElementById('varAiResult');
    var loadingEl = document.getElementById('varAiLoading');
    var aiBtn = document.getElementById('varAiBtn');
    if (!aiResult) return;
    aiResult.innerHTML = '';
    aiResult.classList.remove('show');
    if (loadingEl) loadingEl.style.display = 'flex';
    if (aiBtn) aiBtn.disabled = true;
    try {
      var geminiKey = '';
      var geminiModel = '';
      if (provider === 'gemini') {
        try { geminiKey = localStorage.getItem('gemini_api_key') || ''; } catch(e) {}
        try { geminiModel = localStorage.getItem('gemini_model') || ''; } catch(e) {}
        if (!geminiModel) { var gm = document.getElementById('geminiModel'); if (gm) geminiModel = gm.value; }
        if (!geminiModel) geminiModel = 'gemini-3-flash-preview';
      }
      var r = await fetch('/api/variation-analysis', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({item_id: itemId, ai_provider: provider, gemini_key: geminiKey, gemini_model: geminiModel})
      });
      var d = await r.json();
      if (loadingEl) loadingEl.style.display = 'none';
      if (aiBtn) aiBtn.disabled = false;
      if (d.ok && d.analysis) {
        aiResult.innerHTML = '<div class="varAiModel">Model: ' + esc(d.model_used || provider) + '</div>' + d.analysis.replace(/\n/g, '<br>');
        aiResult.classList.add('show');
      } else if (d.error) {
        aiResult.innerHTML = '<span style="color:#fca5a5">Error: ' + esc(d.error) + '</span>';
        aiResult.classList.add('show');
      }
    } catch(err) {
      if (loadingEl) loadingEl.style.display = 'none';
      if (aiBtn) aiBtn.disabled = false;
      if (aiResult) { aiResult.innerHTML = '<span style="color:#fca5a5">Error: ' + esc(err.message) + '</span>'; aiResult.classList.add('show'); }
    }
  }, 800);
}
document.getElementById('batchVarModalOverlay')?.addEventListener('click', function(e){if(e.target.id==='batchVarModalOverlay')closeBatchVariationModal();});

// ===== ALIBABA BATCH ANALYSIS (all cards in Alibaba Search) =====
var _aliBatchRunning = false;
function openAlibabaBatchModal() {
  var overlay = document.getElementById('aliBatchModalOverlay');
  var body = document.getElementById('aliBatchBody');
  var aiResult = document.getElementById('aliBatchAiResult');
  aiResult.classList.remove('show');
  aiResult.innerHTML = '';
  // Collect all product_keys from currently displayed Alibaba cards
  var keys = [];
  var grid = document.getElementById('aliGrid');
  if (grid) {
    grid.querySelectorAll('.selectAlibabaDashboardBtn').forEach(function(btn) {
      var k = btn.getAttribute('data-key');
      if (k && keys.indexOf(k) === -1) keys.push(k);
    });
  }
  if (!keys.length) {
    body.innerHTML = '<div class="calcHint" style="color:#fca5a5">No Alibaba products found on this page. Open an Alibaba search page and extract products first.</div>';
    overlay.classList.remove('hidden');
    return;
  }
  document.getElementById('aliBatchSubtitle').textContent = 'Found ' + keys.length + ' products to analyse';
  body.innerHTML = '<div class="calcHint">Found <b>' + keys.length + '</b> Alibaba products. Click the button below to send all of them to Ollama for a combined supplier/product analysis.</div>' +
    '<button class="varAiBtn" id="aliBatchStartBtn" onclick="startAlibabaBatchAnalysis()">Start Analysis</button>' +
    '<div class="batchProgress" id="aliBatchProgress"></div>';
  overlay.classList.remove('hidden');
}
function closeAlibabaBatchModal() {
  document.getElementById('aliBatchModalOverlay').classList.add('hidden');
  _aliBatchRunning = false;
}
async function startAlibabaBatchAnalysis() {
  if (_aliBatchRunning) return;
  _aliBatchRunning = true;
  var startBtn = document.getElementById('aliBatchStartBtn');
  if (startBtn) startBtn.disabled = true;
  var progressEl = document.getElementById('aliBatchProgress');
  var aiResult = document.getElementById('aliBatchAiResult');
  aiResult.classList.remove('show');
  aiResult.innerHTML = '';
  // Collect product_keys from the DOM
  var keys = [];
  var grid = document.getElementById('aliGrid');
  if (grid) {
    grid.querySelectorAll('.selectAlibabaDashboardBtn').forEach(function(btn) {
      var k = btn.getAttribute('data-key');
      if (k && keys.indexOf(k) === -1) keys.push(k);
    });
  }
  if (!keys.length) {
    if (progressEl) progressEl.innerHTML = '<span style="color:#fca5a5">No products found.</span>';
    _aliBatchRunning = false;
    if (startBtn) startBtn.disabled = false;
    return;
  }
  if (progressEl) progressEl.innerHTML = '<div class="varSpin" style="display:inline-block;vertical-align:middle;margin-right:8px"></div> Sending ' + keys.length + ' Alibaba products to Ollama for analysis...';
  try {
    var _aiProvider = window._aliAiProvider || 'ollama';
  var _geminiKey = '';
  var _keyInput = document.getElementById('aliGeminiKey');
  if (_keyInput) _geminiKey = _keyInput.value.trim();
  // If no key in field, use the saved key from the Gemini tab (localStorage)
  if (!_geminiKey) {
    try { _geminiKey = localStorage.getItem('gemini_api_key') || ''; } catch(e) {}
  }
  // Also sync model from Gemini tab if using gemini
  if (_aiProvider === 'gemini') {
    var _geminiTabModel = '';
    try { _geminiTabModel = localStorage.getItem('gemini_model') || ''; } catch(e) {}
    if (!_geminiTabModel) {
      var _gmEl = document.getElementById('geminiModel');
      if (_gmEl) _geminiTabModel = _gmEl.value;
    }
    if (_geminiTabModel) {
      // pass model via a temp variable for the fetch
      window._aliBatchGeminiModel = _geminiTabModel;
    }
  }
  var r = await fetch('/api/alibaba-analysis-batch', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({product_keys: keys, ai_provider: _aiProvider, gemini_key: _geminiKey, gemini_model: (window._aliBatchGeminiModel||'')})
    });
    var d = await r.json();
    if (progressEl) {
      var parts = [];
      parts.push('<span class="done">Analysed: ' + (d.analysed_count || 0) + ' products</span>');
      if (d.skipped_count) parts.push('<span class="skip">Skipped: ' + d.skipped_count + '</span>');
      parts.push('<span>Model: ' + esc(d.model_used || 'unknown') + '</span>');
      progressEl.innerHTML = parts.join(' &nbsp;|&nbsp; ');
    }
    if (d.ok) {
      aiResult.innerHTML = d.analysis.replace(/\n/g, '<br>');
      aiResult.classList.add('show');
    } else {
      aiResult.innerHTML = '<span style="color:#fca5a5">' + esc(d.error || 'Unknown error') + '</span>';
      aiResult.classList.add('show');
    }
  } catch(e) {
    if (progressEl) progressEl.innerHTML = '<span style="color:#fca5a5">Connection error: ' + e.message + '</span>';
  }
  _aliBatchRunning = false;
  if (startBtn) startBtn.disabled = false;
}
document.getElementById('aliBatchModalClose')?.addEventListener('click', closeAlibabaBatchModal);

// === Store Analysis Modal ===
var _storeAnalysisRunning = false;
function openStoreAnalysisModal() {
  var overlay = document.getElementById('storeAnalysisModalOverlay');
  var body = document.getElementById('storeAnalysisBody');
  var aiResult = document.getElementById('storeAnalysisAiResult');
  aiResult.classList.remove('show');
  aiResult.innerHTML = '';
  var ids = [];
  var grid = document.getElementById('grid');
  if (grid) {
    grid.querySelectorAll('.varBtn').forEach(function(btn) {
      var id = btn.getAttribute('data-var-item');
      if (id && ids.indexOf(id) === -1) ids.push(id);
    });
  }
  if (!ids.length) {
    body.innerHTML = '<div class="calcHint" style="color:#fca5a5">No store products found. Select a store and load products first.</div>';
    overlay.classList.remove('hidden');
    return;
  }
  document.getElementById('storeAnalysisSubtitle').textContent = 'Found ' + ids.length + ' products to analyse';
  var _prov = window._aliAiProvider || 'ollama';
  body.innerHTML = '<div class="calcHint">Found <b>' + ids.length + '</b> store products. Click the button below to analyse best sellers using <b>' + (_prov === 'gemini' ? 'Gemini' : 'Ollama') + '</b>.</div>' +
    '<button class="varAiBtn" id="storeAnalysisStartBtn" onclick="startStoreAnalysis()">Start Analysis</button>' +
    '<div class="batchProgress" id="storeAnalysisProgress"></div>';
  overlay.classList.remove('hidden');
}
function closeStoreAnalysisModal() {
  document.getElementById('storeAnalysisModalOverlay').classList.add('hidden');
  _storeAnalysisRunning = false;
}
async function startStoreAnalysis() {
  if (_storeAnalysisRunning) return;
  _storeAnalysisRunning = true;
  var startBtn = document.getElementById('storeAnalysisStartBtn');
  if (startBtn) startBtn.disabled = true;
  var progressEl = document.getElementById('storeAnalysisProgress');
  var aiResult = document.getElementById('storeAnalysisAiResult');
  aiResult.classList.remove('show');
  aiResult.innerHTML = '';
  var ids = [];
  var grid = document.getElementById('grid');
  if (grid) {
    grid.querySelectorAll('.varBtn').forEach(function(btn) {
      var id = btn.getAttribute('data-var-item');
      if (id && ids.indexOf(id) === -1) ids.push(id);
    });
  }
  if (!ids.length) {
    if (progressEl) progressEl.innerHTML = '<span style="color:#fca5a5">No products found.</span>';
    _storeAnalysisRunning = false;
    if (startBtn) startBtn.disabled = false;
    return;
  }
  var _aiProvider = window._aliAiProvider || 'ollama';
  var _geminiKey = '';
  try { _geminiKey = localStorage.getItem('gemini_api_key') || ''; } catch(e) {}
  var _geminiModel = '';
  try { _geminiModel = localStorage.getItem('gemini_model') || ''; } catch(e) {}
  if (!_geminiModel) { var _gmEl = document.getElementById('geminiModel'); if (_gmEl) _geminiModel = _gmEl.value; }
  if (progressEl) progressEl.innerHTML = '<div class="varSpin" style="display:inline-block;vertical-align:middle;margin-right:8px"></div> Sending ' + ids.length + ' store products to ' + (_aiProvider === 'gemini' ? 'Gemini' : 'Ollama') + ' for best-seller analysis...';
  try {
    var r = await fetch('/api/store-analysis-batch', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({item_ids: ids, ai_provider: _aiProvider, gemini_key: _geminiKey, gemini_model: _geminiModel})
    });
    var d = await r.json();
    if (progressEl) {
      var parts = [];
      parts.push('<span class="done">Analysed: ' + (d.analysed_count || 0) + ' products</span>');
      parts.push('<span>Model: ' + esc(d.model_used || 'unknown') + '</span>');
      progressEl.innerHTML = parts.join(' &nbsp;|&nbsp; ');
    }
    if (d.ok) {
      aiResult.innerHTML = d.analysis.replace(/\n/g, '<br>');
      aiResult.classList.add('show');
    } else {
      aiResult.innerHTML = '<span style="color:#fca5a5">' + esc(d.error || 'Unknown error') + '</span>';
      aiResult.classList.add('show');
    }
  } catch(err) {
    if (progressEl) progressEl.innerHTML = '<span style="color:#fca5a5">Error: ' + esc(err.message) + '</span>';
  }
  _storeAnalysisRunning = false;
  if (startBtn) startBtn.disabled = false;
}
document.getElementById('storeAnalysisModalClose')?.addEventListener('click', closeStoreAnalysisModal);
document.getElementById('storeAnalysisModalOverlay')?.addEventListener('click', function(e){if(e.target.id==='storeAnalysisModalOverlay')closeStoreAnalysisModal();});

// Sidebar AI provider selector + tab-aware analysis dispatch
window._aliAiProvider = 'ollama';
window._setAiProvider = function(provider) {
  window._aliAiProvider = provider;
  var oBtn = document.getElementById('sideOllamaBtn');
  var gBtn = document.getElementById('sideGeminiBtn');
  if (provider === 'gemini') {
    if (oBtn) { oBtn.style.background = '#1f2937'; oBtn.style.color = '#64748b'; oBtn.style.borderColor = '#334155'; }
    if (gBtn) { gBtn.style.background = '#1a2e1a'; gBtn.style.color = '#4ade80'; gBtn.style.borderColor = '#22c55e'; }
  } else {
    if (oBtn) { oBtn.style.background = '#1e3a5f'; oBtn.style.color = '#93c5fd'; oBtn.style.borderColor = '#3b82f6'; }
    if (gBtn) { gBtn.style.background = '#1f2937'; gBtn.style.color = '#64748b'; gBtn.style.borderColor = '#334155'; }
  }
};
// Show/hide sidebar buttons based on active tab
window._updateSidebarButtons = function() {
  var box = document.getElementById('sideAiButtons');
  if (!box) return;
  var show = (activeTab === 'alibaba' || activeTab === 'store' || activeTab === 'ebaySearch');
  box.style.display = show ? 'block' : 'none';
};
// Dispatch analysis to the correct function based on active tab
window._runAnalysis = function() {
  if (activeTab === 'alibaba') {
    openAlibabaBatchModal();
  } else if (activeTab === 'store') {
    openStoreAnalysisModal();
  } else if (activeTab === 'ebaySearch') {
    openBatchVariationModal();
  }
};

document.getElementById('aliBatchModalOverlay')?.addEventListener('click', function(e){if(e.target.id==='aliBatchModalOverlay')closeAlibabaBatchModal();});

// Global click delegation for .varBtn (fallback in case addEventListener binding fails)
document.addEventListener('click', function(e){
  var btn = e.target.closest('.varBtn');
  if(btn){
    e.preventDefault();
    e.stopPropagation();
    openVariationModal(btn.getAttribute('data-var-item'), btn.getAttribute('data-var-title'));
  }
});


// FITTINGS LIBRARY — Client-side JavaScript
// Manages the Fittings tab: grid, filters, add/edit modal, CSV export.
// API: GET/POST /api/fittings, DELETE /api/fittings?id=N

var fitCards=[],fitEditingId=null,fitVariations=[];
var FIT_CATS=['Hose','Straight Joiner','Elbow 90°','Tee / Y-Piece','Cross Joiner','Bulkhead Fitting'];
var FIT_MATS=['Brass','Stainless Steel','PVC','Nylon','Aluminum','Copper'];
var FIT_GRADES=['Industrial','Food Grade','Pneumatic','General'];

async function loadFittings(){
  try {
    var cat=document.getElementById('fitFilterCategory').value||'';
    var mat=document.getElementById('fitFilterMaterial').value||'';
    var grade=document.getElementById('fitFilterGrade').value||'';
    var search=document.getElementById('fitSearch').value||'';
    var p=new URLSearchParams();
    if(cat)p.set('category',cat);
    if(mat)p.set('material',mat);
    if(grade)p.set('grade',grade);
    if(search)p.set('search',search);
    var res=await fetch('/api/fittings?'+p.toString()).then(function(r){return r.json();});
    fitCards=res.fittings||[];
    console.log('[Fittings] Loaded '+fitCards.length+' fittings from server');
    renderFittings();
    loadFittingStats();
  } catch(e) {
    console.error('[Fittings] loadFittings error:', e);
    var grid=document.getElementById('fitGrid');
    if(grid) grid.innerHTML='<div class="empty" style="color:#ef4444">Error loading fittings: '+e.message+'</div>';
  }
}
async function loadFittingStats(){
  var res=await fetch('/api/fittings/stats').then(r=>r.json());
  var el=function(id){return document.getElementById(id)};if(!el('fitStTotal'))return;
  el('fitStTotal').textContent=res.total||0;
  el('fitStBrass').textContent=(res.by_material||{}).Brass||0;
  el('fitStSS').textContent=(res.by_material||{})['Stainless Steel']||0;
  el('fitStPVC').textContent=(res.by_material||{}).PVC||0;
  el('fitStFood').textContent=(res.by_grade||{})['Food Grade']||0;
  el('fitStInd').textContent=(res.by_grade||{}).Industrial||0;
  el('fitStPneu').textContent=(res.by_grade||{}).Pneumatic||0;
}
function renderFittings(){
  var grid=document.getElementById('fitGrid'),empty=document.getElementById('fitEmpty');if(!grid)return;
  if(!fitCards.length){grid.innerHTML='';empty.classList.remove('hidden');return;}
  empty.classList.add('hidden');
  grid.innerHTML=fitCards.map(function(f){
    var tags='';
    if(f.category)tags+='<span class="sellerTag">'+esc(f.category)+'</span>';
    if(f.material)tags+='<span class="sellerTag" style="background:#451a03;color:#fbbf24">'+esc(f.material)+'</span>';
    if(f.barb_size)tags+='<span class="sellerTag">'+esc(f.barb_size)+'</span>';
    if(f.grade)tags+='<span class="sellerTag" style="background:#064e3b;color:#34d399">'+esc(f.grade)+'</span>';
    var vars=(f.variations&&f.variations.length)?'<div style="font-size:10px;color:#94a3b8;margin-top:4px">'+f.variations.length+' variations</div>':'';
    return '<div class="card" style="position:relative;overflow:visible"><button onclick="fitDeleteDirect('+f.id+',event)" title="Delete" style="position:absolute;top:8px;right:8px;z-index:10;background:#7f1d1d;color:#fca5a5;border:none;border-radius:6px;width:26px;height:26px;font-size:14px;cursor:pointer;padding:0;line-height:26px;text-align:center">✕</button><div onclick="fitOpenModal('+f.id+')" style="cursor:pointer"><div class="imageBox">'+(f.image_url?'<img src="'+esc(f.image_url)+'" onerror="this.style.display=\'none\'">':'<span style="font-size:32px;color:#475569">🔩</span>')+'</div><div class="body"><div class="pTitle">'+esc(f.name||'Untitled')+'</div><div style="font-size:11px;color:#94a3b8;font-family:monospace;margin-top:4px">'+esc(f.sku||'—')+'</div><div class="topLine">'+tags+'</div>'+vars+'</div></div></div>';
  }).join('');
}
function fitOpenModal(id){
  fitEditingId=id||null;fitVariations=[];
  var modal=document.getElementById('fittingModal');
  if(!modal){modal=document.createElement('div');modal.id='fittingModal';modal.className='modalOverlay';modal.innerHTML=fitModalHtml();document.body.appendChild(modal);}
  ['fitName','fitSku','fitSize','fitThread','fitPressure','fitTemp','fitNotes','fitImage'].forEach(function(x){var el=document.getElementById(x);if(el)el.value='';});
  ['fitCategory','fitMaterial','fitGrade'].forEach(function(x){var el=document.getElementById(x);if(el)el.value='';});
  document.getElementById('fitModalTitle').textContent=id?'Edit Fitting':'Add New Fitting';
  document.getElementById('fitDeleteBtn').style.display=id?'':'none';
  if(id){var f=fitCards.find(function(x){return x.id===id;});if(f){
    document.getElementById('fitName').value=f.name||'';document.getElementById('fitSku').value=f.sku||'';
    document.getElementById('fitCategory').value=f.category||'';document.getElementById('fitMaterial').value=f.material||'';
    document.getElementById('fitSize').value=f.barb_size||'';document.getElementById('fitThread').value=f.thread||'';
    document.getElementById('fitPressure').value=f.pressure||'';document.getElementById('fitTemp').value=f.temp||'';
    document.getElementById('fitGrade').value=f.grade||'';document.getElementById('fitImage').value=f.image_url||'';
    document.getElementById('fitNotes').value=f.notes||'';
    fitVariations=(f.variations||[]).map(function(v){return{size:v.size||'',sku:v.sku||'',pressure:v.pressure||''};});
  }}
  fitRenderVariations();modal.classList.remove('hidden');
}
function fitModalHtml(){
  return '<div class="calcModal" style="max-width:520px;max-height:90vh;overflow-y:auto"><div class="modalHead"><div><h2 id="fitModalTitle">Add Fitting</h2></div><button class="modalClose" onclick="document.getElementById(\'fittingModal\').classList.add(\'hidden\')">Close</button></div><div style="padding:16px"><div style="margin-bottom:10px"><label style="font-size:11px;color:#94a3b8;display:block;margin-bottom:3px">Name *</label><input id="fitName" style="width:100%;direction:ltr;text-align:left" placeholder="e.g. Brass Y-Piece 8mm"></div><div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px"><div><label style="font-size:11px;color:#94a3b8;display:block;margin-bottom:3px">SKU</label><input id="fitSku" style="width:100%;direction:ltr;text-align:left" placeholder="BR-Y-08"></div><div><label style="font-size:11px;color:#94a3b8;display:block;margin-bottom:3px">Barb Size</label><input id="fitSize" style="width:100%;direction:ltr;text-align:left" placeholder="8mm"></div></div><div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px"><div><label style="font-size:11px;color:#94a3b8;display:block;margin-bottom:3px">Category</label><select id="fitCategory" style="width:100%"><option value="">Select...</option>'+FIT_CATS.map(function(c){return'<option value="'+c+'">'+c+'</option>'}).join('')+'</select></div><div><label style="font-size:11px;color:#94a3b8;display:block;margin-bottom:3px">Material</label><select id="fitMaterial" style="width:100%"><option value="">Select...</option>'+FIT_MATS.map(function(m){return'<option value="'+m+'">'+m+'</option>'}).join('')+'</select></div></div><div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px"><div><label style="font-size:11px;color:#94a3b8;display:block;margin-bottom:3px">Pressure</label><input id="fitPressure" style="width:100%;direction:ltr;text-align:left" placeholder="10 Bar"></div><div><label style="font-size:11px;color:#94a3b8;display:block;margin-bottom:3px">Temp</label><input id="fitTemp" style="width:100%;direction:ltr;text-align:left" placeholder="-20 to 120"></div></div><div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px"><div><label style="font-size:11px;color:#94a3b8;display:block;margin-bottom:3px">Thread</label><input id="fitThread" style="width:100%;direction:ltr;text-align:left" placeholder="NPT 1/8&quot;"></div><div><label style="font-size:11px;color:#94a3b8;display:block;margin-bottom:3px">Grade</label><select id="fitGrade" style="width:100%"><option value="">Select...</option>'+FIT_GRADES.map(function(g){return'<option value="'+g+'">'+g+'</option>'}).join('')+'</select></div></div><div style="margin-bottom:10px"><label style="font-size:11px;color:#94a3b8;display:block;margin-bottom:3px">Image URL</label><input id="fitImage" style="width:100%;direction:ltr;text-align:left" placeholder="https://..."></div><div style="margin-bottom:10px"><label style="font-size:11px;color:#94a3b8;display:block;margin-bottom:3px">Notes</label><textarea id="fitNotes" rows="2" style="width:100%;direction:ltr;text-align:left"></textarea></div><div style="border-top:1px solid #334155;padding-top:10px;margin-bottom:10px"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px"><span style="font-size:13px;font-weight:600">Variations (sizes)</span><button class="fitBtn" onclick="fitAddVariation()">+ Add Size</button></div><div id="fitVariationList"></div></div><div style="display:flex;gap:8px;margin-top:12px"><button id="fitDeleteBtn" class="fitBtn" style="background:#7f1d1d" onclick="fitDelete()">Delete</button><div style="flex:1"></div><button class="fitBtn" onclick="document.getElementById(\'fittingModal\').classList.add(\'hidden\')">Cancel</button><button class="fitBtn" style="background:#2563eb" onclick="fitSave()">Save</button></div></div></div>';
}
function fitAddVariation(s,k,p){fitVariations.push({size:s||'',sku:k||'',pressure:p||''});fitRenderVariations();}
function fitRemoveVariation(i){fitVariations.splice(i,1);fitRenderVariations();}
function fitUpdateVar(i,f,v){fitVariations[i][f]=v;}
function fitRenderVariations(){
  var l=document.getElementById('fitVariationList');if(!l)return;
  if(!fitVariations.length){l.innerHTML='<div style="color:#64748b;font-size:11px;font-style:italic">No variations.</div>';return;}
  l.innerHTML=fitVariations.map(function(v,i){return '<div style="display:flex;gap:6px;margin-bottom:6px"><input placeholder="Size" value="'+esc(v.size)+'" style="flex:1;font-size:11px" oninput="fitUpdateVar('+i+',\'size\',this.value)"><input placeholder="SKU" value="'+esc(v.sku)+'" style="width:80px;font-size:11px" oninput="fitUpdateVar('+i+',\'sku\',this.value)"><input placeholder="Pressure" value="'+esc(v.pressure)+'" style="width:70px;font-size:11px" oninput="fitUpdateVar('+i+',\'pressure\',this.value)"><button class="fitBtn" style="background:#7f1d1d;padding:4px 8px" onclick="fitRemoveVariation('+i+')">✕</button></div>';}).join('');
}
async function fitSave(){
  var name=document.getElementById('fitName').value.trim();if(!name){alert('Name required.');return;}
  var data={name:name,sku:document.getElementById('fitSku').value.trim(),category:document.getElementById('fitCategory').value,material:document.getElementById('fitMaterial').value,barb_size:document.getElementById('fitSize').value.trim(),thread:document.getElementById('fitThread').value.trim(),pressure:document.getElementById('fitPressure').value.trim(),temp:document.getElementById('fitTemp').value.trim(),grade:document.getElementById('fitGrade').value,image_url:document.getElementById('fitImage')?document.getElementById('fitImage').value.trim():'',notes:document.getElementById('fitNotes').value.trim(),variations:fitVariations.filter(function(v){return v.size||v.sku;})};
  if(fitEditingId){data.id=fitEditingId;await fetch('/api/fittings/update',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}).then(r=>r.json());}
  else{await fetch('/api/fittings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}).then(r=>r.json());}
  document.getElementById('fittingModal').classList.add('hidden');loadFittings();
}
async function fitDelete(){if(!fitEditingId)return;if(!confirm('Delete?'))return;await fetch('/api/fittings?id='+fitEditingId,{method:'DELETE'});document.getElementById('fittingModal').classList.add('hidden');loadFittings();}
async function fitDeleteDirect(id,e){e.stopPropagation();if(!confirm('Delete this fitting and all its variations?'))return;var res=await fetch('/api/fittings?id='+id,{method:'DELETE'});if(res.ok){var data=await res.json();if(data.ok!==false)loadFittings();}else{alert('Delete failed');}}
function fitExportCSV(){
  if(!fitCards.length){alert('Nothing to export.');return;}
  var h=['name','sku','category','material','barb_size','thread','pressure','temp','grade','notes'],rows=[h.join(',')];
  fitCards.forEach(function(f){rows.push(h.map(function(x){var v=(f[x]||'').toString().replace(/"/g,'""');if(v.includes(',')||v.includes('"'))v='"'+v+'"';return v;}).join(','));});
  var b=new Blob(['\uFEFF'+rows.join('\n')],{type:'text/csv;charset=utf-8;'});var a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='fittings_'+new Date().toISOString().slice(0,10)+'.csv';a.click();
}
async function fitAddFromProduct(itemId,attrs){
  var body={item_id:itemId};if(attrs)body.overrides=attrs;
  var res=await fetch('/api/fittings/from-product',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json());
  if(res.ok)alert('Added to Fittings Library!');else alert('Failed: '+(res.error||'Unknown'));
}
(function(){var _fc=document.getElementById('fitFilterCategory');if(_fc){FIT_CATS.forEach(function(c){var o=document.createElement('option');o.value=c;o.textContent=c;_fc.appendChild(o);});}var _fm=document.getElementById('fitFilterMaterial');if(_fm){FIT_MATS.forEach(function(m){var o=document.createElement('option');o.value=m;o.textContent=m;_fm.appendChild(o);});}var _fg=document.getElementById('fitFilterGrade');if(_fg){FIT_GRADES.forEach(function(g){var o=document.createElement('option');o.value=g;o.textContent=g;_fg.appendChild(o);});}})();
var _fs=document.getElementById('fitSearch');if(_fs)_fs.addEventListener('input',loadFittings);
var _fc=document.getElementById('fitFilterCategory');if(_fc)_fc.addEventListener('change',loadFittings);
var _fm=document.getElementById('fitFilterMaterial');if(_fm)_fm.addEventListener('change',loadFittings);
var _fg=document.getElementById('fitFilterGrade');if(_fg)_fg.addEventListener('change',loadFittings);

</script><div id="ollamaStatus" class="ollamaBadge" title="Ollama: Checking..."><span class="ollamaDot"></span></div></body></html>"""

def main():
    # Create the database/tables before the browser opens. If a developer changes
    # the schema later, init_db() is the safe place to add migrations.
    init_db()

    dashboard_url = f"http://127.0.0.1:{PORT}"
    print(f"Unified Product Research Dashboard running: {dashboard_url}")

    # Build the HTTP server object first, then open the browser shortly after.
    # The small delay gives Windows/Python time to start listening on the port,
    # so the browser does not show a temporary connection error.
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Timer(0.8, lambda: webbrowser.open(dashboard_url)).start()

    # serve_forever() blocks here and keeps the local dashboard alive until the
    # terminal/batch file is closed by the user.
    server.serve_forever()


if __name__ == "__main__":
    main()
