# Unified Product Research Dashboard - database layer
#
# This module stores data collected from public browser pages.
# IMPORTANT: the new program does not use eBay Developer API keys.
# The Chrome extension reads the visible eBay page, sends the extracted products
# and Purchase History rows to server.py, and this file saves/aggregates them.
# Future developers: keep item_id as the unique product key. Sales rows use
# unique_key to avoid duplicate Purchase History rows on repeated scans.

import sqlite3
from pathlib import Path
from datetime import datetime, timedelta
import re

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / 'data'
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / 'ebay_tracker.db'

# TAB DATA OWNERSHIP MAP
# ----------------------
# products: central product catalogue shared by all tabs. Do not use this table
#           alone to decide where a product appears in the UI.
# store_products: Store Tracker membership table. A product appears in the
#                 Store tab only if this link exists. seller_username alone is
#                 not enough, because Search results also expose sellers.
# search_groups/search_results: eBay Search membership tables. These links drive
#                               the eBay Search tab and must not create stores.
# dashboard_products: user-selected products for Tab 1/Dashboard. This is a
#                     separate membership link so selections do not alter Store
#                     or Search ownership.
# sales: Purchase History rows linked by item_id. These rows feed yesterday,
#        7-day, 30-day, and revenue metrics in both Store and Search contexts.
# page_snapshots: raw page text for diagnostics/debugging extraction problems.


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# Creates the SQLite schema and performs safe migrations/cleanup for existing
# local databases. Keep migrations additive: users may already have data.
def init_db():
    conn = connect()
    cur = conn.cursor()
    cur.executescript('''
    CREATE TABLE IF NOT EXISTS stores (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        store_url TEXT,
        seller_username TEXT UNIQUE,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        last_seen_at TEXT
    );

    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id TEXT UNIQUE NOT NULL,
        product_url TEXT,
        title TEXT,
        price_text TEXT,
        image_url TEXT,
        seller_username TEXT,
        source_page_url TEXT,
        first_seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
        last_seen_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS sales (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id TEXT NOT NULL,
        buyer_id TEXT,
        variation TEXT,
        price REAL,
        price_text TEXT,
        currency TEXT,
        quantity INTEGER DEFAULT 1,
        sold_at TEXT,
        sold_at_text TEXT,
        location TEXT,
        source_page_url TEXT,
        unique_key TEXT UNIQUE,
        collected_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS page_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        page_url TEXT,
        seller_username TEXT,
        product_count INTEGER DEFAULT 0,
        raw_text TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );


    CREATE TABLE IF NOT EXISTS store_products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        seller_username TEXT NOT NULL,
        item_id TEXT NOT NULL,
        source_page_url TEXT,
        position INTEGER,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        last_seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(seller_username, item_id)
    );

    CREATE INDEX IF NOT EXISTS idx_store_products_seller ON store_products(seller_username);
    CREATE INDEX IF NOT EXISTS idx_store_products_item ON store_products(item_id);

    CREATE TABLE IF NOT EXISTS search_groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_name TEXT UNIQUE NOT NULL,
        search_query TEXT,
        search_page_url TEXT,
        dominant_tokens TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        last_seen_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS search_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER NOT NULL,
        item_id TEXT NOT NULL,
        source_page_url TEXT,
        position INTEGER,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        last_seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(group_id, item_id)
    );

    CREATE TABLE IF NOT EXISTS dashboard_products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id TEXT NOT NULL UNIQUE,
        source TEXT DEFAULT 'ebay_search',
        source_group TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        selected_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE INDEX IF NOT EXISTS idx_dashboard_products_item ON dashboard_products(item_id);
    CREATE INDEX IF NOT EXISTS idx_dashboard_products_selected_at ON dashboard_products(selected_at);

    CREATE TABLE IF NOT EXISTS alibaba_products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_key TEXT UNIQUE NOT NULL,
        product_url TEXT,
        title TEXT,
        price_text TEXT,
        min_price REAL,
        image_url TEXT,
        supplier_name TEXT,
        country TEXT,
        years_text TEXT,
        min_order_text TEXT,
        shipping_text TEXT,
        delivery_text TEXT,
        sold_text TEXT,
        sold_count INTEGER,
        rating REAL,
        rating_text TEXT,
        review_count INTEGER,
        badges_text TEXT,
        has_add_to_cart INTEGER DEFAULT 0,
        source_page_url TEXT,
        metadata_source TEXT,
        first_seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
        last_seen_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS alibaba_search_groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_name TEXT UNIQUE NOT NULL,
        search_query TEXT,
        search_page_url TEXT,
        dominant_tokens TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        last_seen_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS alibaba_search_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER NOT NULL,
        product_key TEXT NOT NULL,
        source_page_url TEXT,
        position INTEGER,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        last_seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(group_id, product_key)
    );

    CREATE INDEX IF NOT EXISTS idx_alibaba_products_key ON alibaba_products(product_key);
    CREATE INDEX IF NOT EXISTS idx_alibaba_products_supplier ON alibaba_products(supplier_name);
    CREATE INDEX IF NOT EXISTS idx_alibaba_groups_name ON alibaba_search_groups(group_name);
    CREATE INDEX IF NOT EXISTS idx_alibaba_results_group ON alibaba_search_results(group_id);
    CREATE INDEX IF NOT EXISTS idx_alibaba_results_product ON alibaba_search_results(product_key);

    CREATE TABLE IF NOT EXISTS dashboard_product_links (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ebay_item_id TEXT NOT NULL,
        alibaba_product_key TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(ebay_item_id),
        UNIQUE(alibaba_product_key),
        UNIQUE(ebay_item_id, alibaba_product_key)
    );

    CREATE INDEX IF NOT EXISTS idx_dashboard_links_ebay ON dashboard_product_links(ebay_item_id);
    CREATE INDEX IF NOT EXISTS idx_dashboard_links_alibaba ON dashboard_product_links(alibaba_product_key);

    CREATE INDEX IF NOT EXISTS idx_search_groups_name ON search_groups(group_name);
    CREATE INDEX IF NOT EXISTS idx_search_results_group ON search_results(group_id);
    CREATE INDEX IF NOT EXISTS idx_search_results_item ON search_results(item_id);

    CREATE INDEX IF NOT EXISTS idx_products_item_id ON products(item_id);
    CREATE INDEX IF NOT EXISTS idx_products_seller ON products(seller_username);
    CREATE INDEX IF NOT EXISTS idx_sales_item_id ON sales(item_id);
    CREATE INDEX IF NOT EXISTS idx_sales_sold_at ON sales(sold_at);
    CREATE INDEX IF NOT EXISTS idx_sales_unique ON sales(unique_key);
    CREATE INDEX IF NOT EXISTS idx_snapshots_seller ON page_snapshots(seller_username);
    ''')
    # Lightweight migrations for existing databases
    for stmt in [
        "ALTER TABLE products ADD COLUMN total_sold_text TEXT",
        "ALTER TABLE products ADD COLUMN total_sold INTEGER",
        "ALTER TABLE products ADD COLUMN listing_started_at_text TEXT",
        "ALTER TABLE products ADD COLUMN available_text TEXT",
        "ALTER TABLE products ADD COLUMN available_quantity INTEGER",
        "ALTER TABLE products ADD COLUMN postage_text TEXT",
        "ALTER TABLE products ADD COLUMN shipping_type TEXT",
        "ALTER TABLE products ADD COLUMN shipping_cost_text TEXT",
        "ALTER TABLE products ADD COLUMN metadata_source TEXT",
        "ALTER TABLE products ADD COLUMN metadata_error TEXT",
        "ALTER TABLE products ADD COLUMN metadata_checked_at TEXT",
        "ALTER TABLE products ADD COLUMN watch_count_text TEXT",
        "ALTER TABLE products ADD COLUMN watch_count INTEGER",
        "ALTER TABLE products ADD COLUMN condition_text TEXT",
        "ALTER TABLE products ADD COLUMN variation_text TEXT",
        "ALTER TABLE products ADD COLUMN urgency_text TEXT",
        "ALTER TABLE products ADD COLUMN trending_text TEXT",
        "ALTER TABLE products ADD COLUMN watchlist_text TEXT",
        "ALTER TABLE products ADD COLUMN delivery_text TEXT",
        "ALTER TABLE alibaba_products ADD COLUMN shipping_text TEXT",
        "ALTER TABLE alibaba_products ADD COLUMN delivery_text TEXT",
        "ALTER TABLE alibaba_products ADD COLUMN has_add_to_cart INTEGER DEFAULT 0",
        "ALTER TABLE products ADD COLUMN store_collected INTEGER DEFAULT 0",
        "ALTER TABLE store_products ADD COLUMN source_page_url TEXT"
    ]:
        try:
            cur.execute(stmt)
        except sqlite3.OperationalError:
            pass

    # Build contextual Store memberships for older rows.
    # Dashboard Store views must read from store_products, not directly from seller_username,
    # otherwise Search-captured products can leak into Store Tracker.
    try:
        cur.execute("""
            INSERT OR IGNORE INTO store_products (seller_username, item_id, source_page_url, last_seen_at)
            SELECT COALESCE(seller_username, 'unknown'), item_id, source_page_url, COALESCE(last_seen_at, CURRENT_TIMESTAMP)
            FROM products
            WHERE COALESCE(store_collected, 0) = 1
              AND item_id IS NOT NULL
              AND COALESCE(seller_username, 'unknown') != 'unknown'
        """)
    except sqlite3.OperationalError:
        pass

    # Backfill only obvious store/seller captures. Search-page products stay out of the Store tab.
    try:
        cur.execute("""
            UPDATE products
            SET store_collected = 1
            WHERE COALESCE(store_collected, 0) = 0
              AND (source_page_url LIKE '%/str/%' OR source_page_url LIKE '%_ssn=%')
        """)
    except sqlite3.OperationalError:
        pass

    # Backfill Store memberships only from true Store URLs after older rows are marked.
    # A generic eBay Search page may expose seller names, but that is not a Store scan.
    try:
        cur.execute("""
            INSERT OR IGNORE INTO store_products (seller_username, item_id, source_page_url, last_seen_at)
            SELECT COALESCE(seller_username, 'unknown'), item_id, source_page_url, COALESCE(last_seen_at, CURRENT_TIMESTAMP)
            FROM products
            WHERE COALESCE(store_collected, 0) = 1
              AND item_id IS NOT NULL
              AND COALESCE(seller_username, 'unknown') != 'unknown'
              AND (source_page_url LIKE '%/str/%' OR source_page_url LIKE '%_ssn=%' OR source_page_url LIKE '%/usr/%')
        """)
    except sqlite3.OperationalError:
        pass

    # Cleanup Store memberships accidentally created from generic Search pages.
    # The Store tab must be driven only by true Store/Store-search URLs.
    try:
        cur.execute("""
            DELETE FROM store_products
            WHERE LOWER(seller_username) IN (
                SELECT LOWER(seller_username)
                FROM stores
                WHERE NOT (COALESCE(store_url, '') LIKE '%/str/%'
                           OR COALESCE(store_url, '') LIKE '%_ssn=%'
                           OR COALESCE(store_url, '') LIKE '%/usr/%')
            )
            AND NOT (COALESCE(source_page_url, '') LIKE '%/str/%'
                     OR COALESCE(source_page_url, '') LIKE '%_ssn=%'
                     OR COALESCE(source_page_url, '') LIKE '%/usr/%')
        """)
        cur.execute("""
            DELETE FROM stores
            WHERE NOT (COALESCE(store_url, '') LIKE '%/str/%'
                       OR COALESCE(store_url, '') LIKE '%_ssn=%'
                       OR COALESCE(store_url, '') LIKE '%/usr/%')
               OR NOT EXISTS (SELECT 1 FROM store_products sp WHERE LOWER(sp.seller_username) = LOWER(stores.seller_username))
        """)
        cur.execute("""
            DELETE FROM sales
            WHERE item_id IN (
                SELECT p.item_id
                FROM products p
                LEFT JOIN store_products sp ON sp.item_id = p.item_id
                LEFT JOIN search_results sr ON sr.item_id = p.item_id
                WHERE sp.item_id IS NULL AND sr.item_id IS NULL
            )
        """)
        cur.execute("""
            DELETE FROM products
            WHERE item_id IN (
                SELECT p.item_id
                FROM products p
                LEFT JOIN store_products sp ON sp.item_id = p.item_id
                LEFT JOIN search_results sr ON sr.item_id = p.item_id
                WHERE sp.item_id IS NULL AND sr.item_id IS NULL
            )
        """)
    except sqlite3.OperationalError:
        pass

    conn.commit()
    conn.close()


def guess_seller_from_url(url):
    if not url:
        return None
    patterns = [
        r'ebay\.[^/]+/str/([^/?#]+)',
        r'[?&]_ssn=([^&#]+)',
        r'ebay\.[^/]+/usr/([^/?#]+)',
    ]
    for pat in patterns:
        m = re.search(pat, url, re.I)
        if m:
            return m.group(1)
    return None


def parse_item_id(url_or_text):
    if not url_or_text:
        return None
    patterns = [
        r'/itm/(?:[^/?#]+/)?(\d{9,15})(?=[/?#]|$)',
        r'[?&]item=(\d{9,15})(?=[&#]|$)',
        r'Item number\s*(\d{9,15})',
    ]
    for pat in patterns:
        m = re.search(pat, url_or_text, re.I)
        if m:
            return m.group(1)
    return None


def parse_price(price_text):
    if not price_text:
        return None, None
    currency = None
    if '£' in price_text:
        currency = 'GBP'
    elif '$' in price_text:
        currency = 'USD'
    elif '€' in price_text:
        currency = 'EUR'
    m = re.search(r'(\d+[\d,.]*)', price_text.replace(',', ''))
    if not m:
        return None, currency
    try:
        return float(m.group(1)), currency
    except Exception:
        return None, currency


def parse_ebay_date(date_text):
    if not date_text:
        return None
    clean = date_text.strip()
    clean = clean.replace(' at ', ' ')
    clean = re.sub(r'\s+(BST|GMT|PDT|PST|EDT|EST|CDT|CST|MDT|MST)$', '', clean, flags=re.I)
    clean = re.sub(r'\s+', ' ', clean)
    # Normalise common separators: "12-Jul-2024" → "12 Jul 2024"
    clean = re.sub(r'[-/]', ' ', clean)
    formats = [
        # "15 Jul 2024 3:42:07PM"
        '%d %b %Y %I:%M:%S%p',
        # "15 Jul 2024 3:42PM"
        '%d %b %Y %I:%M%p',
        # "15 July 2024 3:42:07PM"
        '%d %B %Y %I:%M:%S%p',
        # "15 July 2024 3:42PM"
        '%d %B %Y %I:%M%p',
        # "15 Jul 2024" (date only, no time)
        '%d %b %Y',
        '%d %B %Y',
        # "Jul 15, 2024" (US-style, date only)
        '%b %d, %Y',
        '%B %d, %Y',
        # "2024-07-15" or "2024 07 15" (ISO-style after dash replacement)
        '%Y %m %d',
    ]
    for fmt in formats:
        try:
            return datetime.strptime(clean, fmt).strftime('%Y-%m-%d %H:%M:%S')
        except Exception:
            pass
    return None


def upsert_store(store_url=None, seller_username=None):
    if not seller_username and store_url:
        seller_username = guess_seller_from_url(store_url)
    if not seller_username:
        return None

    conn = connect()
    conn.execute('''
        INSERT INTO stores (store_url, seller_username, last_seen_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(seller_username) DO UPDATE SET
            store_url = COALESCE(excluded.store_url, stores.store_url),
            last_seen_at = CURRENT_TIMESTAMP
    ''', (store_url, seller_username))
    conn.commit()
    conn.close()
    return seller_username


def save_snapshot(page_url, seller_username, raw_text, product_count):
    conn = connect()
    conn.execute('''
        INSERT INTO page_snapshots (page_url, seller_username, raw_text, product_count)
        VALUES (?, ?, ?, ?)
    ''', (page_url, seller_username, raw_text, product_count))
    conn.commit()
    conn.close()


def upsert_products(products, source_page_url=None, seller_username=None):
    conn = connect()
    inserted = 0
    updated = 0

    def val(p, k):
        v = p.get(k)
        if isinstance(v, str):
            v = v.strip()
            return v if v else None
        return v

    for p in products:
        item_id = str(p.get('item_id') or '').strip()
        if not item_id:
            continue

        existing = conn.execute('SELECT id FROM products WHERE item_id=?', (item_id,)).fetchone()
        conn.execute('''
            INSERT INTO products (
                item_id, product_url, title, price_text, image_url, seller_username, source_page_url,
                total_sold_text, total_sold, listing_started_at_text, available_text, available_quantity,
                postage_text, shipping_type, shipping_cost_text, metadata_source, metadata_error,
                metadata_checked_at, watch_count_text, watch_count, condition_text, variation_text,
                urgency_text, trending_text, watchlist_text, delivery_text, store_collected, last_seen_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(item_id) DO UPDATE SET
                product_url = CASE WHEN excluded.product_url IS NOT NULL THEN excluded.product_url ELSE products.product_url END,
                title = CASE WHEN excluded.title IS NOT NULL THEN excluded.title ELSE products.title END,
                price_text = CASE
                    -- Store/search cards are the best place to capture variation price ranges.
                    WHEN excluded.metadata_source IN ('store_card_public_dom', 'search_card_public_dom') AND excluded.price_text IS NOT NULL THEN excluded.price_text
                    -- If a range is already saved, do not overwrite it with a single selected product-page price.
                    WHEN products.price_text LIKE '% to %' THEN products.price_text
                    WHEN excluded.price_text IS NOT NULL THEN excluded.price_text
                    ELSE products.price_text
                END,
                image_url = CASE
                    WHEN excluded.metadata_source IN ('store_card_public_dom', 'search_card_public_dom') AND excluded.image_url IS NOT NULL THEN excluded.image_url
                    WHEN products.image_url IS NULL OR products.image_url = '' THEN excluded.image_url
                    ELSE products.image_url
                END,
                seller_username = CASE
                    WHEN excluded.metadata_source IN ('store_card_public_dom', 'search_card_public_dom') AND excluded.seller_username IS NOT NULL THEN excluded.seller_username
                    WHEN products.seller_username IS NULL OR products.seller_username = '' OR products.seller_username = 'unknown' THEN excluded.seller_username
                    ELSE products.seller_username
                END,
                source_page_url = CASE WHEN excluded.source_page_url IS NOT NULL THEN excluded.source_page_url ELSE products.source_page_url END,
                total_sold_text = CASE
                    WHEN excluded.metadata_source = 'product_page_public_dom' THEN excluded.total_sold_text
                    WHEN excluded.total_sold_text IS NOT NULL THEN excluded.total_sold_text
                    ELSE products.total_sold_text
                END,
                total_sold = CASE
                    WHEN excluded.metadata_source = 'product_page_public_dom' THEN excluded.total_sold
                    WHEN excluded.total_sold IS NOT NULL THEN excluded.total_sold
                    ELSE products.total_sold
                END,
                listing_started_at_text = CASE WHEN excluded.listing_started_at_text IS NOT NULL THEN excluded.listing_started_at_text ELSE products.listing_started_at_text END,
                available_text = CASE WHEN excluded.available_text IS NOT NULL THEN excluded.available_text ELSE products.available_text END,
                available_quantity = CASE WHEN excluded.available_quantity IS NOT NULL THEN excluded.available_quantity ELSE products.available_quantity END,
                postage_text = CASE WHEN excluded.postage_text IS NOT NULL THEN excluded.postage_text ELSE products.postage_text END,
                shipping_type = CASE WHEN excluded.shipping_type IS NOT NULL THEN excluded.shipping_type ELSE products.shipping_type END,
                shipping_cost_text = CASE WHEN excluded.shipping_cost_text IS NOT NULL THEN excluded.shipping_cost_text ELSE products.shipping_cost_text END,
                metadata_source = CASE WHEN excluded.metadata_source IS NOT NULL THEN excluded.metadata_source ELSE products.metadata_source END,
                metadata_error = CASE WHEN excluded.metadata_error IS NOT NULL THEN excluded.metadata_error ELSE products.metadata_error END,
                metadata_checked_at = CASE WHEN excluded.metadata_checked_at IS NOT NULL THEN excluded.metadata_checked_at ELSE products.metadata_checked_at END,
                watch_count_text = CASE WHEN excluded.watch_count_text IS NOT NULL THEN excluded.watch_count_text ELSE products.watch_count_text END,
                watch_count = CASE WHEN excluded.watch_count IS NOT NULL THEN excluded.watch_count ELSE products.watch_count END,
                condition_text = CASE WHEN excluded.condition_text IS NOT NULL THEN excluded.condition_text ELSE products.condition_text END,
                variation_text = CASE WHEN excluded.variation_text IS NOT NULL THEN excluded.variation_text ELSE products.variation_text END,
                urgency_text = CASE WHEN excluded.urgency_text IS NOT NULL THEN excluded.urgency_text ELSE products.urgency_text END,
                trending_text = CASE WHEN excluded.trending_text IS NOT NULL THEN excluded.trending_text ELSE products.trending_text END,
                watchlist_text = CASE WHEN excluded.watchlist_text IS NOT NULL THEN excluded.watchlist_text ELSE products.watchlist_text END,
                delivery_text = CASE WHEN excluded.delivery_text IS NOT NULL THEN excluded.delivery_text ELSE products.delivery_text END,
                store_collected = CASE WHEN COALESCE(excluded.store_collected, 0) = 1 OR COALESCE(products.store_collected, 0) = 1 THEN 1 ELSE 0 END,
                last_seen_at = CURRENT_TIMESTAMP
        ''', (
            item_id,
            val(p, 'product_url'),
            val(p, 'title'),
            val(p, 'price_text'),
            val(p, 'image_url'),
            seller_username or val(p, 'seller_username'),
            source_page_url or val(p, 'source_page_url'),
            val(p, 'total_sold_text'),
            val(p, 'total_sold'),
            val(p, 'listing_started_at_text'),
            val(p, 'available_text'),
            val(p, 'available_quantity'),
            val(p, 'postage_text'),
            val(p, 'shipping_type'),
            val(p, 'shipping_cost_text'),
            val(p, 'metadata_source'),
            val(p, 'metadata_error'),
            val(p, 'metadata_checked_at'),
            val(p, 'watch_count_text'),
            val(p, 'watch_count'),
            val(p, 'condition_text'),
            val(p, 'variation_text'),
            val(p, 'urgency_text'),
            val(p, 'trending_text'),
            val(p, 'watchlist_text'),
            val(p, 'delivery_text'),
            1 if val(p, 'store_collected') in (1, '1', True) else 0
        ))
        if existing:
            updated += 1
        else:
            inserted += 1

    conn.commit()
    conn.close()
    return {'inserted': inserted, 'updated': updated, 'total_received': len(products)}

def upsert_sales(sales, source_page_url=None):
    conn = connect()
    inserted = 0
    updated = 0

    for s in sales:
        item_id = str(s.get('item_id') or '').strip()
        if not item_id:
            continue

        price_text = s.get('price_text') or s.get('price')
        price, currency = parse_price(str(price_text or ''))
        qty_raw = str(s.get('quantity') or '1')
        qty_match = re.search(r'\d+', qty_raw)
        quantity = int(qty_match.group(0)) if qty_match else 1
        sold_at_text = s.get('sold_at_text') or s.get('date_text')
        sold_at = s.get('sold_at') or parse_ebay_date(sold_at_text)
        buyer = s.get('buyer_id') or s.get('user_id')
        variation = s.get('variation')
        location = s.get('location')
        unique_key = f'{item_id}|{sold_at_text}|{quantity}|{buyer}|{variation}'

        # Ensure product exists even if phase 1 did not collect it yet.
        conn.execute('''
            INSERT INTO products (item_id, product_url, title, source_page_url, last_seen_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(item_id) DO NOTHING
        ''', (item_id, f'https://www.ebay.co.uk/itm/{item_id}', s.get('product_title'), source_page_url))

        existing = conn.execute('SELECT id FROM sales WHERE unique_key=?', (unique_key,)).fetchone()
        conn.execute('''
            INSERT INTO sales (item_id, buyer_id, variation, price, price_text, currency, quantity, sold_at, sold_at_text, location, source_page_url, unique_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(unique_key) DO UPDATE SET
                buyer_id = excluded.buyer_id,
                variation = excluded.variation,
                price = excluded.price,
                price_text = excluded.price_text,
                currency = excluded.currency,
                quantity = excluded.quantity,
                sold_at = excluded.sold_at,
                sold_at_text = excluded.sold_at_text,
                location = excluded.location,
                source_page_url = excluded.source_page_url,
                collected_at = CURRENT_TIMESTAMP
        ''', (item_id, buyer, variation, price, str(price_text or ''), currency, quantity, sold_at, sold_at_text, location, source_page_url, unique_key))

        if existing:
            updated += 1
        else:
            inserted += 1

    conn.commit()
    conn.close()
    return {'inserted': inserted, 'updated': updated, 'total_received': len(sales)}


def list_products(seller_username=None):
    conn = connect()
    if seller_username:
        rows = conn.execute('SELECT * FROM products WHERE seller_username=? ORDER BY last_seen_at DESC', (seller_username,)).fetchall()
    else:
        rows = conn.execute('SELECT * FROM products ORDER BY last_seen_at DESC').fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_sales(item_id=None, limit=500):
    conn = connect()
    if item_id:
        rows = conn.execute('SELECT * FROM sales WHERE item_id=? ORDER BY sold_at DESC, collected_at DESC LIMIT ?', (item_id, limit)).fetchall()
    else:
        rows = conn.execute('SELECT * FROM sales ORDER BY sold_at DESC, collected_at DESC LIMIT ?', (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_stores():
    conn = connect()
    rows = conn.execute('SELECT * FROM stores ORDER BY last_seen_at DESC').fetchall()
    conn.close()
    return [dict(r) for r in rows]


def stats():
    conn = connect()
    product_count = conn.execute('SELECT COUNT(*) c FROM products').fetchone()['c']
    store_count = conn.execute('SELECT COUNT(*) c FROM stores').fetchone()['c']
    snapshot_count = conn.execute('SELECT COUNT(*) c FROM page_snapshots').fetchone()['c']
    sales_count = conn.execute('SELECT COALESCE(SUM(quantity),0) c FROM sales').fetchone()['c']
    sales_rows = conn.execute('SELECT COUNT(*) c FROM sales').fetchone()['c']
    conn.close()
    return {
        'stores': store_count,
        'products': product_count,
        'sales_quantity_total': sales_count,
        'sales_rows': sales_rows,
        'snapshots': snapshot_count,
        'db_path': str(DB_PATH)
    }


def sales_report(days=30):
    conn = connect()
    daily = conn.execute('''
        SELECT DATE(sold_at) day, SUM(quantity) quantity, ROUND(SUM(COALESCE(price,0) * quantity), 2) revenue, COUNT(DISTINCT item_id) products
        FROM sales
        WHERE sold_at IS NOT NULL AND DATE(sold_at) >= DATE('now', '-' || ? || ' days')
        GROUP BY DATE(sold_at)
        ORDER BY day DESC
    ''', (days,)).fetchall()

    weekly = conn.execute('''
        SELECT strftime('%Y-W%W', sold_at) week, SUM(quantity) quantity, ROUND(SUM(COALESCE(price,0) * quantity), 2) revenue, COUNT(DISTINCT item_id) products
        FROM sales
        WHERE sold_at IS NOT NULL
        GROUP BY strftime('%Y-W%W', sold_at)
        ORDER BY week DESC
        LIMIT 20
    ''').fetchall()

    last_30 = conn.execute('''
        SELECT COALESCE(SUM(quantity),0) quantity, ROUND(COALESCE(SUM(COALESCE(price,0) * quantity),0), 2) revenue, COUNT(DISTINCT item_id) products
        FROM sales
        WHERE sold_at IS NOT NULL AND DATE(sold_at) >= DATE('now', '-30 days')
    ''').fetchone()

    top_products = conn.execute('''
        SELECT s.item_id, p.title, SUM(s.quantity) quantity, ROUND(SUM(COALESCE(s.price,0) * s.quantity), 2) revenue
        FROM sales s
        LEFT JOIN products p ON p.item_id = s.item_id
        GROUP BY s.item_id
        ORDER BY quantity DESC
        LIMIT 20
    ''').fetchall()

    conn.close()
    return {
        'last_30_days': dict(last_30),
        'daily': [dict(r) for r in daily],
        'weekly': [dict(r) for r in weekly],
        'top_products': [dict(r) for r in top_products]
    }




# Builds product cards for the Store Tracker tab.
# IMPORTANT: this query is intentionally driven by store_products, not products.seller_username.
# Otherwise products found through eBay Search would leak into Store Tracker.
def product_sales_cards(seller_username=None):
    '''Return Store Tracker cards from store_products memberships only.'''
    conn = connect()
    where = ""
    params = []
    if seller_username and seller_username != "__all__":
        where = "WHERE LOWER(sp.seller_username) = LOWER(?)"
        params.append(seller_username)
    query = f'''
        SELECT
            p.item_id,
            p.title,
            p.price_text,
            p.image_url,
            p.product_url,
            p.total_sold_text,
            p.total_sold,
            p.listing_started_at_text,
            p.available_text,
            p.available_quantity,
            p.postage_text,
            p.shipping_type,
            p.shipping_cost_text,
            p.metadata_source,
            p.metadata_error,
            p.metadata_checked_at,
            p.watch_count_text,
            p.watch_count,
            p.condition_text,
            p.variation_text,
            p.urgency_text,
            p.trending_text,
            p.watchlist_text,
            p.delivery_text,
            sp.seller_username AS seller_username,
            MAX(p.last_seen_at, sp.last_seen_at) AS last_seen_at,
            COALESCE(SUM(CASE WHEN DATE(s.sold_at) = DATE('now', '-1 day') THEN s.quantity ELSE 0 END), 0) AS sold_yesterday,
            COALESCE(SUM(CASE WHEN DATE(s.sold_at) >= DATE('now', '-7 days') THEN s.quantity ELSE 0 END), 0) AS sold_7_days,
            COALESCE(SUM(CASE WHEN DATE(s.sold_at) >= DATE('now', '-30 days') THEN s.quantity ELSE 0 END), 0) AS sold_30_days,
            ROUND(COALESCE(SUM(CASE WHEN DATE(s.sold_at) >= DATE('now', '-30 days') THEN COALESCE(s.price,0) * s.quantity ELSE 0 END), 0), 2) AS revenue_30_days,
            ROUND(COALESCE(SUM(COALESCE(s.price,0) * s.quantity), 0), 2) AS tracked_total_revenue,
            COALESCE(SUM(s.quantity), 0) AS tracked_total_quantity,
            MIN(DATE(s.sold_at)) AS tracked_first_sale_date,
            MAX(DATE(s.sold_at)) AS tracked_last_sale_date,
            CASE WHEN MIN(DATE(s.sold_at)) IS NOT NULL THEN CAST((julianday('now') - julianday(MIN(DATE(s.sold_at))) + 1) AS INTEGER) ELSE 0 END AS tracked_days_span
        FROM store_products sp
        JOIN products p ON p.item_id = sp.item_id
        LEFT JOIN sales s ON s.item_id = p.item_id
        {where}
        GROUP BY sp.seller_username, p.item_id
        ORDER BY sold_30_days DESC, sold_7_days DESC, sp.position ASC, p.last_seen_at DESC
    '''
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# Builds the left Store list and the "All stores" summary.
# A store exists for UI purposes only when it has store_products links.
def store_summary():
    '''Return Stores from explicit store_products memberships only.'''
    conn = connect()
    query = '''
        SELECT
            sp.seller_username AS seller_username,
            COUNT(DISTINCT sp.item_id) AS product_count,
            COALESCE(SUM(CASE WHEN DATE(s.sold_at) >= DATE('now', '-30 days') THEN s.quantity ELSE 0 END), 0) AS sold_30_days,
            MAX(sp.last_seen_at) AS last_seen_at
        FROM store_products sp
        JOIN products p ON p.item_id = sp.item_id
        LEFT JOIN sales s ON s.item_id = p.item_id
        GROUP BY LOWER(sp.seller_username)
        ORDER BY last_seen_at DESC, seller_username ASC
    '''
    rows = conn.execute(query).fetchall()
    conn.close()
    return [dict(r) for r in rows]




# Links products to a real Store scan.
# Call this only from server.py after the URL has been verified as a Store/Store-search URL.
def link_store_products(seller_username, products, source_page_url=None):
    '''Link products to the exact Store context that captured them.

    This prevents products collected from eBay Search from appearing in Store
    Tracker just because they have the same seller_username.
    '''
    seller_username = (seller_username or 'unknown').strip()
    if not seller_username or seller_username == 'unknown':
        return {'inserted': 0, 'updated': 0, 'skipped': len(products or [])}
    conn = connect()
    inserted = updated = skipped = 0
    for pos, product in enumerate(products or [], start=1):
        item_id = str(product.get('item_id') or '').strip()
        if not item_id:
            skipped += 1
            continue
        existing = conn.execute(
            'SELECT id FROM store_products WHERE seller_username=? AND item_id=?',
            (seller_username, item_id)
        ).fetchone()
        conn.execute('''
            INSERT INTO store_products (seller_username, item_id, source_page_url, position, last_seen_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(seller_username, item_id) DO UPDATE SET
                source_page_url = excluded.source_page_url,
                position = excluded.position,
                last_seen_at = CURRENT_TIMESTAMP
        ''', (seller_username, item_id, source_page_url, pos))
        if existing:
            updated += 1
        else:
            inserted += 1
    conn.commit()
    conn.close()
    return {'inserted': inserted, 'updated': updated, 'skipped': skipped}


def prune_orphan_products(conn=None):
    '''Delete products that no longer belong to any Store or Search context.

    Deleting a Store or Search Group should remove exactly what that context
    saved. Shared products remain while at least one context still references
    them.
    '''
    own_conn = conn is None
    if own_conn:
        conn = connect()
    rows = conn.execute('''
        SELECT p.item_id
        FROM products p
        LEFT JOIN store_products sp ON sp.item_id = p.item_id
        LEFT JOIN search_results sr ON sr.item_id = p.item_id
        LEFT JOIN dashboard_products dp ON dp.item_id = p.item_id
        WHERE sp.item_id IS NULL AND sr.item_id IS NULL AND dp.item_id IS NULL
    ''').fetchall()
    item_ids = [r['item_id'] for r in rows]
    deleted_sales = deleted_products = 0
    if item_ids:
        placeholders = ','.join(['?'] * len(item_ids))
        cur = conn.execute(f'DELETE FROM sales WHERE item_id IN ({placeholders})', item_ids)
        deleted_sales = cur.rowcount if cur.rowcount is not None else 0
        cur = conn.execute(f'DELETE FROM products WHERE item_id IN ({placeholders})', item_ids)
        deleted_products = cur.rowcount if cur.rowcount is not None else 0
    if own_conn:
        conn.commit()
        conn.close()
    return {'deleted_orphan_products': deleted_products, 'deleted_orphan_sales': deleted_sales}

def upsert_search_group(group_name, search_query=None, search_page_url=None, dominant_tokens=None):
    """Create/update an eBay Search group shown in the left panel.

    group_name is usually typed search text. For image searches/no keyword, it is
    inferred from common title words across similar result cards.
    """
    group_name = (group_name or 'eBay image search').strip()[:90]
    tokens_text = ', '.join(dominant_tokens or []) if isinstance(dominant_tokens, list) else (dominant_tokens or None)
    conn = connect()
    conn.execute("""
        INSERT INTO search_groups (group_name, search_query, search_page_url, dominant_tokens, last_seen_at)
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(group_name) DO UPDATE SET
            search_query = COALESCE(excluded.search_query, search_groups.search_query),
            search_page_url = COALESCE(excluded.search_page_url, search_groups.search_page_url),
            dominant_tokens = COALESCE(excluded.dominant_tokens, search_groups.dominant_tokens),
            last_seen_at = CURRENT_TIMESTAMP
    """, (group_name, search_query, search_page_url, tokens_text))
    row = conn.execute('SELECT id FROM search_groups WHERE group_name=?', (group_name,)).fetchone()
    conn.commit()
    conn.close()
    return row['id'] if row else None


# Links products to an eBay Search group.
# This must remain separate from store_products so Search tabs and Store tabs stay isolated.
def link_search_results(group_id, products):
    """Link product item_ids to a search group without duplicating products."""
    conn = connect()
    inserted = updated = skipped = 0
    for pos, product in enumerate(products or [], start=1):
        item_id = str(product.get('item_id') or '').strip()
        if not group_id or not item_id:
            skipped += 1
            continue
        existing = conn.execute('SELECT id FROM search_results WHERE group_id=? AND item_id=?', (group_id, item_id)).fetchone()
        conn.execute("""
            INSERT INTO search_results (group_id, item_id, position, last_seen_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(group_id, item_id) DO UPDATE SET
                position = excluded.position,
                last_seen_at = CURRENT_TIMESTAMP
        """, (group_id, item_id, pos))
        if existing:
            updated += 1
        else:
            inserted += 1
    conn.commit()
    conn.close()
    return {'inserted': inserted, 'updated': updated, 'skipped': skipped}


# Builds the left eBay Search group list and the "All searches" summary.
def search_group_summary():
    """Return the eBay Search groups for the left panel."""
    conn = connect()
    rows = conn.execute("""
        SELECT
            g.id,
            g.group_name,
            g.search_query,
            g.search_page_url,
            g.dominant_tokens,
            COUNT(DISTINCT sr.item_id) AS product_count,
            MAX(sr.last_seen_at) AS last_seen_at
        FROM search_groups g
        LEFT JOIN search_results sr ON sr.group_id = g.id
        GROUP BY g.id
        ORDER BY last_seen_at DESC, g.group_name ASC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# Builds product cards for the eBay Search tab.
# IMPORTANT: this query is intentionally driven by search_results, not seller_username.
def search_product_cards(group_name=None):
    """Return dashboard-ready cards for one search group or all search groups."""
    conn = connect()
    where = ''
    params = []
    if group_name and group_name != '__all__':
        where = 'WHERE g.group_name = ?'
        params.append(group_name)
    rows = conn.execute(f"""
        SELECT
            p.item_id, p.title, p.price_text, p.image_url, p.product_url,
            p.total_sold_text, p.total_sold, p.available_text, p.available_quantity,
            p.postage_text, p.shipping_type, p.shipping_cost_text,
            p.watch_count_text, p.watch_count, p.condition_text, p.variation_text,
            p.urgency_text, p.trending_text, p.watchlist_text, p.delivery_text,
            COALESCE(p.seller_username, 'unknown') AS seller_username,
            g.group_name AS search_group_name,
            CASE WHEN dp.item_id IS NULL THEN 0 ELSE 1 END AS dashboard_selected,
            p.last_seen_at,
            COALESCE(SUM(CASE WHEN DATE(s.sold_at) = DATE('now', '-1 day') THEN s.quantity ELSE 0 END), 0) AS sold_yesterday,
            COALESCE(SUM(CASE WHEN DATE(s.sold_at) >= DATE('now', '-7 days') THEN s.quantity ELSE 0 END), 0) AS sold_7_days,
            COALESCE(SUM(CASE WHEN DATE(s.sold_at) >= DATE('now', '-30 days') THEN s.quantity ELSE 0 END), 0) AS sold_30_days,
            ROUND(COALESCE(SUM(CASE WHEN DATE(s.sold_at) >= DATE('now', '-30 days') THEN COALESCE(s.price,0) * s.quantity ELSE 0 END), 0), 2) AS revenue_30_days,
            ROUND(COALESCE(SUM(COALESCE(s.price,0) * s.quantity), 0), 2) AS tracked_total_revenue,
            COALESCE(SUM(s.quantity), 0) AS tracked_total_quantity,
            CASE WHEN MIN(DATE(s.sold_at)) IS NOT NULL THEN CAST((julianday('now') - julianday(MIN(DATE(s.sold_at))) + 1) AS INTEGER) ELSE 0 END AS tracked_days_span
        FROM search_results sr
        JOIN search_groups g ON g.id = sr.group_id
        JOIN products p ON p.item_id = sr.item_id
        LEFT JOIN sales s ON s.item_id = p.item_id
        LEFT JOIN dashboard_products dp ON dp.item_id = p.item_id
        {where}
        GROUP BY p.item_id, g.group_name
        ORDER BY sold_30_days DESC, sold_7_days DESC, sr.position ASC, p.last_seen_at DESC
    """, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]



# Builds product cards for Tab 1/Dashboard.
# IMPORTANT: this query is driven by dashboard_products. Selecting a product is
# a user-curation action and must not rewrite Store or Search memberships.
def dashboard_product_cards():
    """Return products the user selected for Dashboard review.

    eBay and Alibaba remain stored in separate product tables. Dashboard only
    stores membership rows; Alibaba dashboard keys are prefixed with ali: so
    they cannot collide with numeric eBay item IDs.
    """
    conn = connect()
    rows = conn.execute("""
        SELECT * FROM (
            SELECT
                p.item_id AS item_id,
                p.title AS title,
                p.price_text AS price_text,
                p.image_url AS image_url,
                p.product_url AS product_url,
                p.total_sold_text AS total_sold_text,
                p.total_sold AS total_sold,
                p.available_text AS available_text,
                p.available_quantity AS available_quantity,
                p.postage_text AS postage_text,
                p.shipping_type AS shipping_type,
                p.shipping_cost_text AS shipping_cost_text,
                p.shipping_cost_text AS shipping_text,
                p.watch_count_text AS watch_count_text,
                p.watch_count AS watch_count,
                p.condition_text AS condition_text,
                p.variation_text AS variation_text,
                p.urgency_text AS urgency_text,
                p.trending_text AS trending_text,
                p.watchlist_text AS watchlist_text,
                p.delivery_text AS delivery_text,
                COALESCE(p.seller_username, 'unknown') AS seller_username,
                dp.source_group AS search_group_name,
                dp.selected_at AS selected_at,
                1 AS dashboard_selected,
                p.last_seen_at AS last_seen_at,
                COALESCE(SUM(CASE WHEN DATE(s.sold_at) = DATE('now', '-1 day') THEN s.quantity ELSE 0 END), 0) AS sold_yesterday,
                COALESCE(SUM(CASE WHEN DATE(s.sold_at) >= DATE('now', '-7 days') THEN s.quantity ELSE 0 END), 0) AS sold_7_days,
                COALESCE(SUM(CASE WHEN DATE(s.sold_at) >= DATE('now', '-30 days') THEN s.quantity ELSE 0 END), 0) AS sold_30_days,
                ROUND(COALESCE(SUM(CASE WHEN DATE(s.sold_at) >= DATE('now', '-30 days') THEN COALESCE(s.price,0) * s.quantity ELSE 0 END), 0), 2) AS revenue_30_days,
                ROUND(COALESCE(SUM(COALESCE(s.price,0) * s.quantity), 0), 2) AS tracked_total_revenue,
                COALESCE(SUM(s.quantity), 0) AS tracked_total_quantity,
                CASE WHEN MIN(DATE(s.sold_at)) IS NOT NULL THEN CAST((julianday('now') - julianday(MIN(DATE(s.sold_at))) + 1) AS INTEGER) ELSE 0 END AS tracked_days_span,
                'ebay_search' AS dashboard_source,
                NULL AS alibaba_product_key,
                NULL AS product_key,
                NULL AS supplier_name,
                NULL AS country,
                NULL AS years_text,
                NULL AS min_order_text,
                NULL AS badges_text,
                NULL AS sold_text,
                NULL AS rating_text,
                NULL AS min_price,
                link.id AS connected_link_id,
                CASE WHEN link.id IS NULL THEN 0 ELSE 1 END AS is_connected,
                link.alibaba_product_key AS connected_other_key
            FROM dashboard_products dp
            JOIN products p ON p.item_id = dp.item_id
            LEFT JOIN sales s ON s.item_id = p.item_id
            LEFT JOIN dashboard_product_links link ON link.ebay_item_id = p.item_id
            WHERE COALESCE(dp.source, 'ebay_search') != 'alibaba_search'
            GROUP BY p.item_id, dp.id, link.id

            UNION ALL

            SELECT
                'ali:' || p.product_key AS item_id,
                p.title AS title,
                p.price_text AS price_text,
                p.image_url AS image_url,
                p.product_url AS product_url,
                p.sold_text AS total_sold_text,
                p.sold_count AS total_sold,
                p.min_order_text AS available_text,
                NULL AS available_quantity,
                NULL AS postage_text,
                NULL AS shipping_type,
                NULL AS shipping_cost_text,
                p.shipping_text AS shipping_text,
                p.rating_text AS watch_count_text,
                NULL AS watch_count,
                NULL AS condition_text,
                NULL AS variation_text,
                NULL AS urgency_text,
                NULL AS trending_text,
                p.badges_text AS watchlist_text,
                p.delivery_text AS delivery_text,
                COALESCE(p.supplier_name, 'Alibaba supplier') AS seller_username,
                dp.source_group AS search_group_name,
                dp.selected_at AS selected_at,
                1 AS dashboard_selected,
                p.last_seen_at AS last_seen_at,
                0 AS sold_yesterday,
                0 AS sold_7_days,
                COALESCE(p.sold_count, 0) AS sold_30_days,
                0 AS revenue_30_days,
                0 AS tracked_total_revenue,
                COALESCE(p.sold_count, 0) AS tracked_total_quantity,
                0 AS tracked_days_span,
                'alibaba_search' AS dashboard_source,
                p.product_key AS alibaba_product_key,
                p.product_key AS product_key,
                p.supplier_name AS supplier_name,
                p.country AS country,
                p.years_text AS years_text,
                p.min_order_text AS min_order_text,
                p.badges_text AS badges_text,
                p.sold_text AS sold_text,
                p.rating_text AS rating_text,
                p.min_price AS min_price,
                link.id AS connected_link_id,
                CASE WHEN link.id IS NULL THEN 0 ELSE 1 END AS is_connected,
                link.ebay_item_id AS connected_other_key
            FROM dashboard_products dp
            JOIN alibaba_products p ON dp.item_id = 'ali:' || p.product_key
            LEFT JOIN dashboard_product_links link ON link.alibaba_product_key = p.product_key
            WHERE dp.source = 'alibaba_search'
        )
        ORDER BY selected_at DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def select_dashboard_product(item_id, source_group=None):
    """Add one eBay Search product to Tab 1/Dashboard without duplicating it."""
    item_id = str(item_id or '').strip()
    if not item_id:
        return {'ok': False, 'error': 'Missing item_id'}
    conn = connect()
    exists = conn.execute('SELECT item_id FROM products WHERE item_id=?', (item_id,)).fetchone()
    if not exists:
        conn.close()
        return {'ok': False, 'error': 'Product not found'}
    conn.execute("""
        INSERT INTO dashboard_products (item_id, source, source_group, selected_at)
        VALUES (?, 'ebay_search', ?, CURRENT_TIMESTAMP)
        ON CONFLICT(item_id) DO UPDATE SET
            source = 'ebay_search',
            source_group = COALESCE(excluded.source_group, dashboard_products.source_group),
            selected_at = CURRENT_TIMESTAMP
    """, (item_id, source_group))
    conn.commit()
    conn.close()
    return {'ok': True, 'item_id': item_id}



def select_dashboard_alibaba_product(product_key, source_group=None):
    """Add one Alibaba product to Tab 1/Dashboard without touching eBay data."""
    product_key = str(product_key or '').strip()
    if not product_key:
        return {'ok': False, 'error': 'Missing product_key'}
    dashboard_key = 'ali:' + product_key
    conn = connect()
    exists = conn.execute('SELECT product_key FROM alibaba_products WHERE product_key=?', (product_key,)).fetchone()
    if not exists:
        conn.close()
        return {'ok': False, 'error': 'Alibaba product not found'}
    conn.execute("""
        INSERT INTO dashboard_products (item_id, source, source_group, selected_at)
        VALUES (?, 'alibaba_search', ?, CURRENT_TIMESTAMP)
        ON CONFLICT(item_id) DO UPDATE SET
            source = 'alibaba_search',
            source_group = COALESCE(excluded.source_group, dashboard_products.source_group),
            selected_at = CURRENT_TIMESTAMP
    """, (dashboard_key, source_group))
    conn.commit()
    conn.close()
    return {'ok': True, 'item_id': dashboard_key, 'product_key': product_key}

def remove_dashboard_product(item_id):
    """Remove one product from Tab 1/Dashboard only; product/search data remains."""
    item_id = str(item_id or '').strip()
    if not item_id:
        return {'ok': False, 'error': 'Missing item_id'}
    conn = connect()
    deleted = conn.execute('DELETE FROM dashboard_products WHERE item_id=?', (item_id,)).rowcount
    conn.commit()
    conn.close()
    return {'ok': True, 'item_id': item_id, 'deleted': deleted}



def _dashboard_item_source(conn, dashboard_item_id):
    dashboard_item_id = str(dashboard_item_id or '').strip()
    if not dashboard_item_id:
        return None
    row = conn.execute('SELECT item_id, source FROM dashboard_products WHERE item_id=?', (dashboard_item_id,)).fetchone()
    if not row:
        return None
    source = row['source'] or 'ebay_search'
    if source == 'alibaba_search' or dashboard_item_id.startswith('ali:'):
        return {'source': 'alibaba_search', 'key': dashboard_item_id[4:] if dashboard_item_id.startswith('ali:') else dashboard_item_id}
    return {'source': 'ebay_search', 'key': dashboard_item_id}


def connect_dashboard_products(first_item_id, second_item_id):
    """Connect exactly one eBay Dashboard card with one Alibaba Dashboard card."""
    conn = connect()
    first = _dashboard_item_source(conn, first_item_id)
    second = _dashboard_item_source(conn, second_item_id)
    if not first or not second:
        conn.close()
        return {'ok': False, 'error': 'Both cards must already be in Dashboard'}
    if first['source'] == second['source']:
        conn.close()
        return {'ok': False, 'error': 'Connect one eBay card with one Alibaba card'}
    ebay_item_id = first['key'] if first['source'] == 'ebay_search' else second['key']
    alibaba_product_key = first['key'] if first['source'] == 'alibaba_search' else second['key']
    exists_ebay = conn.execute('SELECT item_id FROM products WHERE item_id=?', (ebay_item_id,)).fetchone()
    exists_ali = conn.execute('SELECT product_key FROM alibaba_products WHERE product_key=?', (alibaba_product_key,)).fetchone()
    if not exists_ebay or not exists_ali:
        conn.close()
        return {'ok': False, 'error': 'Connected products were not found'}
    # A card belongs to one active calculator pair only. Reconnecting replaces
    # the older pair involving either card, keeping every pair exactly two cards.
    conn.execute('DELETE FROM dashboard_product_links WHERE ebay_item_id=? OR alibaba_product_key=?', (ebay_item_id, alibaba_product_key))
    conn.execute("""
        INSERT INTO dashboard_product_links (ebay_item_id, alibaba_product_key, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
    """, (ebay_item_id, alibaba_product_key))
    conn.commit()
    row = conn.execute('SELECT id FROM dashboard_product_links WHERE ebay_item_id=? AND alibaba_product_key=?', (ebay_item_id, alibaba_product_key)).fetchone()
    conn.close()
    return {'ok': True, 'link_id': row['id'] if row else None, 'ebay_item_id': ebay_item_id, 'alibaba_product_key': alibaba_product_key}


def list_dashboard_product_links():
    """Return connected eBay+Alibaba pairs for the calculator sidebar."""
    conn = connect()
    rows = conn.execute("""
        SELECT
            l.id AS link_id,
            l.created_at,
            l.updated_at,
            e.item_id AS ebay_item_id,
            e.title AS ebay_title,
            e.price_text AS ebay_price_text,
            e.image_url AS ebay_image_url,
            e.product_url AS ebay_product_url,
            COALESCE(e.seller_username, 'unknown') AS ebay_seller,
            a.product_key AS alibaba_product_key,
            a.title AS alibaba_title,
            a.price_text AS alibaba_price_text,
            a.min_price AS alibaba_min_price,
            a.image_url AS alibaba_image_url,
            a.product_url AS alibaba_product_url,
            COALESCE(a.supplier_name, 'Alibaba supplier') AS alibaba_supplier,
            a.country AS alibaba_country,
            a.min_order_text AS alibaba_min_order_text
        FROM dashboard_product_links l
        JOIN products e ON e.item_id = l.ebay_item_id
        JOIN alibaba_products a ON a.product_key = l.alibaba_product_key
        ORDER BY l.updated_at DESC, l.id DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_dashboard_product_link(link_id):
    conn = connect()
    deleted = conn.execute('DELETE FROM dashboard_product_links WHERE id=?', (link_id,)).rowcount
    conn.commit()
    conn.close()
    return {'ok': True, 'deleted': deleted, 'link_id': link_id}

def merge_search_groups(group_names, target_group_name=None, new_group_name=None):
    names = []
    for name in group_names or []:
        name = str(name or '').strip()
        if name and name != '__all__' and name not in names:
            names.append(name)
    if len(names) < 2:
        return {'ok': False, 'error': 'Select at least two eBay Search lists to merge'}
    target_group_name = str(target_group_name or names[0]).strip()
    new_group_name = str(new_group_name or target_group_name).strip()[:90] or target_group_name
    conn = connect()
    cur = conn.cursor()
    placeholders = ','.join(['?'] * len(names))
    rows = cur.execute('SELECT id, group_name FROM search_groups WHERE group_name IN (' + placeholders + ')', names).fetchall()
    by_name = {r['group_name']: r for r in rows}
    missing = [n for n in names if n not in by_name]
    if missing:
        conn.close()
        return {'ok': False, 'error': 'Some lists were not found', 'missing': missing}
    target_row = by_name.get(target_group_name) or by_name[names[0]]
    target_id = target_row['id']
    existing_new = cur.execute('SELECT id, group_name FROM search_groups WHERE group_name=?', (new_group_name,)).fetchone()
    if existing_new and existing_new['id'] != target_id:
        target_id = existing_new['id']
        target_group_name = existing_new['group_name']
    elif new_group_name != target_row['group_name']:
        cur.execute('UPDATE search_groups SET group_name=?, last_seen_at=CURRENT_TIMESTAMP WHERE id=?', (new_group_name, target_id))
        target_group_name = new_group_name
    merged_products = 0
    removed_groups = 0
    for name in names:
        source_id = by_name[name]['id']
        if source_id == target_id:
            continue
        product_rows = cur.execute('SELECT item_id, source_page_url, position FROM search_results WHERE group_id=?', (source_id,)).fetchall()
        for idx, r in enumerate(product_rows, start=1):
            cur.execute('INSERT INTO search_results (group_id, item_id, source_page_url, position, last_seen_at) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP) ON CONFLICT(group_id, item_id) DO UPDATE SET source_page_url=COALESCE(excluded.source_page_url, source_page_url), last_seen_at=CURRENT_TIMESTAMP', (target_id, r['item_id'], r['source_page_url'], r['position'] or idx))
            merged_products += 1
        cur.execute('DELETE FROM search_results WHERE group_id=?', (source_id,))
        cur.execute('DELETE FROM search_groups WHERE id=?', (source_id,))
        removed_groups += 1
    cur.execute('UPDATE search_groups SET last_seen_at=CURRENT_TIMESTAMP WHERE id=?', (target_id,))
    conn.commit()
    final_count = cur.execute('SELECT COUNT(*) AS c FROM search_results WHERE group_id=?', (target_id,)).fetchone()['c']
    conn.close()
    return {'ok': True, 'target_group_name': target_group_name, 'merged_products': merged_products, 'removed_groups': removed_groups, 'final_count': final_count}

# Deletes one eBay Search group and then prunes products that are no longer
# linked to any Store or Search context. Shared products are preserved.
def delete_search_group(group_name):
    '''Delete one Search Group and then remove products no context uses.'''
    if not group_name or group_name == '__all__':
        return {'error': 'Select a real search group before deleting.'}
    conn = connect()
    row = conn.execute('SELECT id FROM search_groups WHERE group_name=?', (group_name,)).fetchone()
    if not row:
        conn.close()
        return {'error': 'Search group not found.'}
    deleted_links = conn.execute('DELETE FROM search_results WHERE group_id=?', (row['id'],)).rowcount
    deleted_groups = conn.execute('DELETE FROM search_groups WHERE id=?', (row['id'],)).rowcount
    orphan_result = prune_orphan_products(conn)
    conn.commit()
    conn.close()
    return {'groups': deleted_groups, 'links': deleted_links, **orphan_result}

# Deletes one Store context and then prunes products that are no longer linked
# anywhere else. If a product is also in a Search group, it remains in products.
def delete_store_data(seller_username):
    '''Delete one Store context and then remove products no context uses.'''
    if not seller_username or seller_username == '__all__':
        return {'deleted_store_links': 0, 'deleted_snapshots': 0, 'deleted_stores': 0, 'deleted_orphan_products': 0, 'deleted_orphan_sales': 0}
    conn = connect()
    cur = conn.execute("DELETE FROM store_products WHERE LOWER(seller_username) = LOWER(?)", (seller_username,))
    deleted_store_links = cur.rowcount if cur.rowcount is not None else 0
    cur = conn.execute("DELETE FROM page_snapshots WHERE LOWER(COALESCE(seller_username, 'unknown')) = LOWER(?)", (seller_username,))
    deleted_snapshots = cur.rowcount if cur.rowcount is not None else 0
    cur = conn.execute("DELETE FROM stores WHERE LOWER(seller_username) = LOWER(?)", (seller_username,))
    deleted_stores = cur.rowcount if cur.rowcount is not None else 0
    orphan_result = prune_orphan_products(conn)
    conn.commit()
    conn.close()
    return {'deleted_store_links': deleted_store_links, 'deleted_snapshots': deleted_snapshots, 'deleted_stores': deleted_stores, **orphan_result}


if __name__ == '__main__':
    init_db()
    print(f'Database ready: {DB_PATH}')

# ---------------- Alibaba Search data layer ----------------
def _alibaba_badges_text(badges):
    if badges is None:
        return None
    if isinstance(badges, str):
        return badges
    try:
        return ', '.join([str(x) for x in badges if x])
    except Exception:
        return str(badges)


def _clean_manual_text(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {'unknown', 'n/a', 'na', 'none', 'null'}:
        return None
    return text


def upsert_alibaba_products(products, source_page_url=None):
    conn = connect()
    cur = conn.cursor()
    inserted = updated = skipped = 0
    for p in products or []:
        key = p.get('product_key') or p.get('product_id') or p.get('product_url')
        if not key:
            skipped += 1
            continue
        key = str(key)
        existing = cur.execute("SELECT id FROM alibaba_products WHERE product_key=?", (key,)).fetchone()
        vals = (
            p.get('product_url'), p.get('title'), p.get('price_text'), p.get('min_price'), p.get('image_url'),
            p.get('supplier_name'), p.get('country'), p.get('years_text'), p.get('min_order_text'),
            _clean_manual_text(p.get('shipping_text')), _clean_manual_text(p.get('delivery_text')), p.get('sold_text'),
            p.get('sold_count'), p.get('rating'), p.get('rating_text'), p.get('review_count'), _alibaba_badges_text(p.get('badges')),
            1 if p.get('has_add_to_cart') else 0,
            source_page_url or p.get('source_page_url'), p.get('metadata_source') or 'alibaba_search_public_dom'
        )
        if existing:
            cur.execute("""
                UPDATE alibaba_products SET
                    product_url=COALESCE(?, product_url), title=COALESCE(?, title),
                    price_text=COALESCE(?, price_text), min_price=COALESCE(?, min_price),
                    image_url=COALESCE(?, image_url), supplier_name=COALESCE(?, supplier_name),
                    country=COALESCE(?, country), years_text=COALESCE(?, years_text),
                    min_order_text=COALESCE(?, min_order_text), shipping_text=COALESCE(?, shipping_text),
                    delivery_text=COALESCE(?, delivery_text), sold_text=COALESCE(?, sold_text),
                    sold_count=COALESCE(?, sold_count), rating=COALESCE(?, rating),
                    rating_text=COALESCE(?, rating_text), review_count=COALESCE(?, review_count),
                    badges_text=COALESCE(?, badges_text), has_add_to_cart=CASE WHEN ?=1 THEN 1 ELSE has_add_to_cart END,
                    source_page_url=COALESCE(?, source_page_url),
                    metadata_source=COALESCE(?, metadata_source), last_seen_at=CURRENT_TIMESTAMP
                WHERE product_key=?
            """, vals + (key,))
            updated += 1
        else:
            cur.execute("""
                INSERT INTO alibaba_products (
                    product_key, product_url, title, price_text, min_price, image_url, supplier_name,
                    country, years_text, min_order_text, shipping_text, delivery_text, sold_text, sold_count, rating, rating_text,
                    review_count, badges_text, has_add_to_cart, source_page_url, metadata_source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (key,) + vals)
            inserted += 1
    conn.commit()
    conn.close()
    return {'inserted': inserted, 'updated': updated, 'skipped': skipped}


def upsert_alibaba_search_group(group_name, search_query=None, search_page_url=None, dominant_tokens=None):
    group_name = (group_name or 'Alibaba image search')[:90]
    tokens = ', '.join(dominant_tokens or []) if isinstance(dominant_tokens, list) else (dominant_tokens or '')
    conn = connect()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO alibaba_search_groups (group_name, search_query, search_page_url, dominant_tokens, last_seen_at)
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(group_name) DO UPDATE SET
            search_query=COALESCE(excluded.search_query, search_query),
            search_page_url=COALESCE(excluded.search_page_url, search_page_url),
            dominant_tokens=COALESCE(excluded.dominant_tokens, dominant_tokens),
            last_seen_at=CURRENT_TIMESTAMP
    """, (group_name, search_query, search_page_url, tokens))
    row = cur.execute("SELECT id FROM alibaba_search_groups WHERE group_name=?", (group_name,)).fetchone()
    conn.commit()
    conn.close()
    return row['id'] if row else None


def link_alibaba_search_results(group_id, products):
    conn = connect()
    cur = conn.cursor()
    inserted = updated = skipped = 0
    for idx, p in enumerate(products or [], start=1):
        key = p.get('product_key') or p.get('product_url')
        if not group_id or not key:
            skipped += 1
            continue
        key = str(key)
        before = cur.execute("SELECT id FROM alibaba_search_results WHERE group_id=? AND product_key=?", (group_id, key)).fetchone()
        cur.execute("""
            INSERT INTO alibaba_search_results (group_id, product_key, source_page_url, position, last_seen_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(group_id, product_key) DO UPDATE SET
                source_page_url=COALESCE(excluded.source_page_url, source_page_url),
                position=excluded.position,
                last_seen_at=CURRENT_TIMESTAMP
        """, (group_id, key, p.get('source_page_url'), idx))
        if before:
            updated += 1
        else:
            inserted += 1
    conn.commit()
    conn.close()
    return {'inserted': inserted, 'updated': updated, 'skipped': skipped}


def update_alibaba_product_fields(product_key, shipping_text=None, delivery_text=None):
    product_key = str(product_key or '').strip()
    if not product_key:
        return {'ok': False, 'error': 'Missing product_key'}
    shipping_text = None if shipping_text is None else str(shipping_text).strip()
    delivery_text = None if delivery_text is None else str(delivery_text).strip()
    conn = connect()
    row = conn.execute('SELECT product_key FROM alibaba_products WHERE product_key=?', (product_key,)).fetchone()
    if not row:
        conn.close()
        return {'ok': False, 'error': 'Alibaba product not found'}
    conn.execute('UPDATE alibaba_products SET shipping_text = ?, delivery_text = ?, last_seen_at = CURRENT_TIMESTAMP WHERE product_key = ?', (shipping_text or None, delivery_text or None, product_key))
    conn.commit()
    conn.close()
    return {'ok': True, 'product_key': product_key, 'shipping_text': shipping_text or None, 'delivery_text': delivery_text or None}

def alibaba_group_summary():
    conn = connect()
    rows = conn.execute("""
        SELECT g.group_name, g.search_query, g.search_page_url, g.dominant_tokens, g.last_seen_at,
               COUNT(r.product_key) AS product_count,
               COUNT(DISTINCT p.supplier_name) AS supplier_count
        FROM alibaba_search_groups g
        LEFT JOIN alibaba_search_results r ON r.group_id = g.id
        LEFT JOIN alibaba_products p ON p.product_key = r.product_key
        GROUP BY g.id
        ORDER BY g.last_seen_at DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def alibaba_product_cards(group_name=None):
    conn = connect()
    params = []
    where = ''
    if group_name and group_name != '__all__':
        where = 'WHERE g.group_name = ?'
        params.append(group_name)
    rows = conn.execute(f"""
        SELECT p.*, g.group_name AS search_group_name, r.position,
               CASE WHEN dp.item_id IS NULL THEN 0 ELSE 1 END AS dashboard_selected
        FROM alibaba_products p
        JOIN alibaba_search_results r ON r.product_key = p.product_key
        JOIN alibaba_search_groups g ON g.id = r.group_id
        LEFT JOIN dashboard_products dp ON dp.item_id = 'ali:' || p.product_key AND dp.source = 'alibaba_search'
        {where}
        ORDER BY COALESCE(p.sold_count, 0) DESC, COALESCE(p.min_price, 999999) ASC, r.position ASC
    """, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def merge_alibaba_groups(group_names, target_group_name=None, new_group_name=None):
    names = []
    for name in group_names or []:
        name = str(name or '').strip()
        if name and name != '__all__' and name not in names:
            names.append(name)
    if len(names) < 2:
        return {'ok': False, 'error': 'Select at least two Alibaba lists to merge'}
    target_group_name = str(target_group_name or names[0]).strip()
    new_group_name = str(new_group_name or target_group_name).strip()[:90] or target_group_name
    conn = connect()
    cur = conn.cursor()
    placeholders = ','.join(['?'] * len(names))
    rows = cur.execute('SELECT id, group_name FROM alibaba_search_groups WHERE group_name IN (' + placeholders + ')', names).fetchall()
    by_name = {r['group_name']: r for r in rows}
    missing = [n for n in names if n not in by_name]
    if missing:
        conn.close()
        return {'ok': False, 'error': 'Some lists were not found', 'missing': missing}
    target_row = by_name.get(target_group_name) or by_name[names[0]]
    target_id = target_row['id']
    existing_new = cur.execute('SELECT id, group_name FROM alibaba_search_groups WHERE group_name=?', (new_group_name,)).fetchone()
    if existing_new and existing_new['id'] != target_id:
        target_id = existing_new['id']
        target_group_name = existing_new['group_name']
    elif new_group_name != target_row['group_name']:
        cur.execute('UPDATE alibaba_search_groups SET group_name=?, last_seen_at=CURRENT_TIMESTAMP WHERE id=?', (new_group_name, target_id))
        target_group_name = new_group_name
    merged_products = 0
    removed_groups = 0
    for name in names:
        source_id = by_name[name]['id']
        if source_id == target_id:
            continue
        product_rows = cur.execute('SELECT product_key, source_page_url, position FROM alibaba_search_results WHERE group_id=?', (source_id,)).fetchall()
        for idx, r in enumerate(product_rows, start=1):
            cur.execute('INSERT INTO alibaba_search_results (group_id, product_key, source_page_url, position, last_seen_at) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP) ON CONFLICT(group_id, product_key) DO UPDATE SET source_page_url=COALESCE(excluded.source_page_url, source_page_url), last_seen_at=CURRENT_TIMESTAMP', (target_id, r['product_key'], r['source_page_url'], r['position'] or idx))
            merged_products += 1
        cur.execute('DELETE FROM alibaba_search_results WHERE group_id=?', (source_id,))
        cur.execute('DELETE FROM alibaba_search_groups WHERE id=?', (source_id,))
        removed_groups += 1
    cur.execute('UPDATE alibaba_search_groups SET last_seen_at=CURRENT_TIMESTAMP WHERE id=?', (target_id,))
    conn.commit()
    final_count = cur.execute('SELECT COUNT(*) AS c FROM alibaba_search_results WHERE group_id=?', (target_id,)).fetchone()['c']
    conn.close()
    return {'ok': True, 'target_group_name': target_group_name, 'merged_products': merged_products, 'removed_groups': removed_groups, 'final_count': final_count}

def delete_alibaba_group(group_name):
    if not group_name:
        return {'error': 'Missing group_name'}
    conn = connect()
    cur = conn.cursor()
    row = cur.execute("SELECT id FROM alibaba_search_groups WHERE group_name=?", (group_name,)).fetchone()
    if not row:
        conn.close()
        return {'deleted_groups': 0, 'deleted_links': 0}
    group_id = row['id']
    cur.execute("DELETE FROM alibaba_search_results WHERE group_id=?", (group_id,))
    links = cur.rowcount
    cur.execute("DELETE FROM alibaba_search_groups WHERE id=?", (group_id,))
    groups = cur.rowcount
    conn.commit()
    conn.close()
    return {'deleted_groups': groups, 'deleted_links': links}



def variation_stats(item_id):
    """Return per-variation sales statistics for a single product.

    Groups all sales rows for the given item_id by their variation column,
    summarising how many times each variation was purchased, total quantity,
    total revenue, and the first/last sale dates.
    Products without any variation text are grouped under 'No variation'.

    Date handling: earliest/latest sale is determined by the standardised
    sold_at column (YYYY-MM-DD HH:MM:SS), NOT by lexicographic MIN/MAX on the
    human-readable sold_at_text.  Rows whose sold_at is NULL (unparseable date)
    are excluded from the earliest/latest calculation so that the AI analysis
    receives chronologically correct dates.
    """
    if not item_id:
        return {'variations': [], 'total_sales': 0, 'total_quantity': 0, 'total_revenue': 0}
    conn = connect()
    # Fetch all raw sales rows ordered by sold_at so we can pick the correct
    # first/last sale text in Python instead of relying on MIN/MAX on text.
    all_rows = conn.execute('''
        SELECT variation, price, quantity, sold_at, sold_at_text
        FROM sales
        WHERE item_id = ?
        ORDER BY sold_at ASC
    ''', (item_id,)).fetchall()

    prod = conn.execute('SELECT title FROM products WHERE item_id=?', (item_id,)).fetchone()
    conn.close()

    # Group by variation name in Python
    from collections import defaultdict
    groups = defaultdict(list)
    for r in all_rows:
        var_name = (r['variation'] or '').strip() or 'No variation'
        groups[var_name].append(r)

    variations = []
    for var_name, rows in groups.items():
        qty = sum((r['quantity'] or 0) for r in rows)
        rev = round(sum((r['price'] or 0) * (r['quantity'] or 0) for r in rows), 2)

        # Rows with a valid parsed date — already in ASC order from the SQL.
        dated = [r for r in rows if r['sold_at']]
        if dated:
            earliest = dated[0]
            latest = dated[-1]
            earliest_text = earliest['sold_at_text'] or earliest['sold_at']
            latest_text = latest['sold_at_text'] or latest['sold_at']
            first_sale = earliest['sold_at']
            last_sale = latest['sold_at']
        else:
            earliest_text = None
            latest_text = None
            first_sale = None
            last_sale = None

        variations.append({
            'variation_name': var_name,
            'sales_count': len(rows),
            'total_quantity': qty,
            'total_revenue': rev,
            'first_sale': first_sale,
            'last_sale': last_sale,
            'earliest_sale_text': earliest_text,
            'latest_sale_text': latest_text,
        })

    # Sort by total_quantity DESC, then sales_count DESC (matches original SQL ORDER BY)
    variations.sort(key=lambda v: (v['total_quantity'], v['sales_count']), reverse=True)

    total_qty = sum(v['total_quantity'] for v in variations)
    total_rev = round(sum(v['total_revenue'] for v in variations), 2)
    total_sales = sum(v['sales_count'] for v in variations)

    return {
        'item_id': item_id,
        'product_title': prod['title'] if prod else None,
        'variations': variations,
        'total_variations': len(variations),
        'total_sales': total_sales,
        'total_quantity': total_qty,
        'total_revenue': total_rev
    }


# ===== Chat conversation CRUD =====
try:
    _conn = connect()
    _conn.executescript("""
        CREATE TABLE IF NOT EXISTS chat_conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL DEFAULT 'Untitled',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (conversation_id) REFERENCES chat_conversations(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_chat_msg_conv ON chat_messages(conversation_id);
    """)
    _conn.close()
except Exception:
    pass

def list_chat_conversations():
    conn = connect()
    rows = conn.execute('SELECT id, title, created_at, updated_at FROM chat_conversations ORDER BY updated_at DESC').fetchall()
    conn.close()
    return [dict(r) for r in rows]

def create_chat_conversation(title=None):
    conn = connect()
    cur = conn.cursor()
    if not title or not title.strip(): title = 'New Chat'
    cur.execute('INSERT INTO chat_conversations (title) VALUES (?)', (title.strip()[:200],))
    conn.commit()
    conv_id = cur.lastrowid
    conn.close()
    return {'id': conv_id, 'title': title.strip()[:200]}

def list_chat_messages(conversation_id):
    conn = connect()
    rows = conn.execute('SELECT id, role, content, created_at FROM chat_messages WHERE conversation_id=? ORDER BY id ASC', (conversation_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def add_chat_message(conversation_id, role, content):
    conn = connect()
    cur = conn.cursor()
    cur.execute('INSERT INTO chat_messages (conversation_id, role, content) VALUES (?, ?, ?)', (conversation_id, role, content))
    cur.execute('UPDATE chat_conversations SET updated_at=CURRENT_TIMESTAMP WHERE id=?', (conversation_id,))
    conn.commit()
    conn.close()

def delete_chat_conversation(conversation_id):
    conn = connect()
    cur = conn.cursor()
    cur.execute('DELETE FROM chat_messages WHERE conversation_id=?', (conversation_id,))
    cur.execute('DELETE FROM chat_conversations WHERE id=?', (conversation_id,))
    conn.commit()
    conn.close()

# ── Gemini conversation tables ─────────────────────────────────────────
def _init_gemini_tables():
    conn = connect()
    cur = conn.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS gemini_conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL DEFAULT 'Untitled',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS gemini_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (conversation_id) REFERENCES gemini_conversations(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_gemini_msg_conv ON gemini_messages(conversation_id);
    """)
    conn.commit()
    conn.close()

_init_gemini_tables()

def list_gemini_conversations():
    conn = connect()
    rows = conn.execute('SELECT id, title, created_at, updated_at FROM gemini_conversations ORDER BY updated_at DESC').fetchall()
    conn.close()
    return [dict(r) for r in rows]

def create_gemini_conversation(title=None):
    conn = connect()
    cur = conn.cursor()
    if not title or not title.strip(): title = 'New Chat'
    cur.execute('INSERT INTO gemini_conversations (title) VALUES (?)', (title.strip()[:200],))
    conn.commit()
    conv_id = cur.lastrowid
    conn.close()
    return {'id': conv_id, 'title': title.strip()[:200]}

def list_gemini_messages(conversation_id):
    conn = connect()
    rows = conn.execute('SELECT id, role, content, created_at FROM gemini_messages WHERE conversation_id=? ORDER BY id ASC', (conversation_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def add_gemini_message(conversation_id, role, content):
    conn = connect()
    cur = conn.cursor()
    cur.execute('INSERT INTO gemini_messages (conversation_id, role, content) VALUES (?, ?, ?)', (conversation_id, role, content))
    cur.execute('UPDATE gemini_conversations SET updated_at=CURRENT_TIMESTAMP WHERE id=?', (conversation_id,))
    conn.commit()
    conn.close()

def delete_gemini_conversation(conversation_id):
    conn = connect()
    cur = conn.cursor()
    cur.execute('DELETE FROM gemini_messages WHERE conversation_id=?', (conversation_id,))
    cur.execute('DELETE FROM gemini_conversations WHERE id=?', (conversation_id,))
    conn.commit()
    conn.close()

def search_ebay_products(query, limit=20):
    """Search eBay products in the database by keyword in title. Returns matching products."""
    conn = connect()
    rows = conn.execute(
        "SELECT item_id, title, price_text, seller_username, image_url, product_url "
        "FROM products WHERE title LIKE ? ORDER BY last_seen_at DESC LIMIT ?",
        (f"%{query}%", limit)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def search_alibaba_products(query, limit=20):
    """Search Alibaba products by keyword. Results sorted by cheapest price first."""
    conn = connect()
    rows = conn.execute(
        "SELECT product_key, title, price_text, min_price, supplier_name, "
        "country, sold_count, rating, min_order_text, image_url, product_url "
        "FROM alibaba_products WHERE title LIKE ? "
        "ORDER BY COALESCE(min_price, 999999) ASC LIMIT ?",
        (f"%{query}%", limit)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_dashboard_data_summary():
    """Build a plain-text summary of dashboard products for the AI chat context."""
    import datetime as _dt
    conn = connect()
    now = _dt.datetime.utcnow()
    d30 = (now - _dt.timedelta(days=30)).strftime('%Y-%m-%d')
    d7  = (now - _dt.timedelta(days=7)).strftime('%Y-%m-%d')
    d1  = (now - _dt.timedelta(days=1)).strftime('%Y-%m-%d')

    ebay_rows = conn.execute("""
        SELECT p.item_id, p.title, p.price_text, p.seller_username,
               COALESCE(s30.qty, 0)  AS sold_30_days,
               COALESCE(s7.qty,  0)  AS sold_7_days,
               COALESCE(s1.qty,  0)  AS sold_yesterday,
               COALESCE(s_all.qty, 0) AS total_qty,
               ROUND(COALESCE(s_all.rev, 0), 2) AS total_revenue
        FROM products p
        JOIN dashboard_products dp ON dp.item_id = p.item_id AND dp.source = 'ebay_search'
        LEFT JOIN (
            SELECT item_id, SUM(quantity) AS qty FROM sales
            WHERE date(sold_at) >= ? GROUP BY item_id
        ) s30 ON s30.item_id = p.item_id
        LEFT JOIN (
            SELECT item_id, SUM(quantity) AS qty FROM sales
            WHERE date(sold_at) >= ? GROUP BY item_id
        ) s7 ON s7.item_id = p.item_id
        LEFT JOIN (
            SELECT item_id, SUM(quantity) AS qty FROM sales
            WHERE date(sold_at) >= ? GROUP BY item_id
        ) s1 ON s1.item_id = p.item_id
        LEFT JOIN (
            SELECT item_id, SUM(quantity) AS qty,
                   SUM(COALESCE(price,0)*quantity) AS rev
            FROM sales GROUP BY item_id
        ) s_all ON s_all.item_id = p.item_id
        ORDER BY sold_30_days DESC
        LIMIT 50
    """, (d30, d7, d1)).fetchall()

    ali_rows = conn.execute("""
        SELECT ap.title, ap.price_text, ap.supplier_name, ap.country,
               ap.sold_count, ap.rating, ap.min_order_text
        FROM alibaba_products ap
        JOIN dashboard_products dp
          ON dp.item_id = 'ali:' || ap.product_key AND dp.source = 'alibaba_search'
        LIMIT 50
    """).fetchall()

    total_ebay  = conn.execute('SELECT COUNT(*) AS c FROM products').fetchone()['c']
    total_ali   = conn.execute('SELECT COUNT(*) AS c FROM alibaba_products').fetchone()['c']
    total_sales = conn.execute('SELECT COUNT(*) AS c FROM sales').fetchone()['c']
    conn.close()

    lines = ['=== DATABASE OVERVIEW ===',
             f'Total eBay products: {total_ebay}',
             f'Total Alibaba products: {total_ali}',
             f'Total sales records: {total_sales}']

    if ebay_rows:
        lines.append('')
        lines.append('=== DASHBOARD EBAY PRODUCTS (top 50 by 30-day sales) ===')
        for r in ebay_rows:
            d = dict(r)
            lines.append(
                f"- {d['title'] or 'Untitled'}"
                f" | Price: {d['price_text'] or 'N/A'}"
                f" | Seller: {d['seller_username'] or 'N/A'}"
                f" | 30d sold: {d['sold_30_days']}"
                f" | 7d sold: {d['sold_7_days']}"
                f" | Yesterday: {d['sold_yesterday']}"
                f" | Total qty: {d['total_qty']}"
                f" | Total revenue: {d['total_revenue']}"
            )

    if ali_rows:
        lines.append('')
        lines.append('=== DASHBOARD ALIBABA PRODUCTS (up to 50) ===')
        for r in ali_rows:
            d = dict(r)
            lines.append(
                f"- {d['title'] or 'Untitled'}"
                f" | Price: {d['price_text'] or 'N/A'}"
                f" | Supplier: {d['supplier_name'] or 'N/A'}"
                f" | Country: {d['country'] or 'N/A'}"
                f" | Sold: {d['sold_count'] or 0}"
                f" | Rating: {d['rating'] or 'N/A'}"
                f" | MOQ: {d['min_order_text'] or 'N/A'}"
            )

    return '\n'.join(lines)

# ============================================================================
# FITTINGS LIBRARY — Hose / Connector / Fitting parts catalogue
# ============================================================================
#
# PURPOSE:
#   This section adds a "Fittings Library" tab to the dashboard. It stores
#   technical hose fittings and connectors (brass barbed fittings, elbows,
#   tees, bulkheads, etc.) with their physical attributes — material, barb
#   size, thread spec, pressure rating, temperature range, grade.
#
# HOW IT CONNECTS TO THE REST OF THE APP:
#   The fittings tables are INDEPENDENT from the eBay/Alibaba product tables.
#   They do NOT use item_id as a key. A fitting has its own auto-increment id.
#
#   However, a fitting CAN be created FROM an eBay product by clicking the
#   "Add to Fittings Library" button on any product card. When that happens,
#   the source_item_id field stores the eBay item_id for traceability.
#
# POPULATION METHODS:
#   1. Manual entry via the Fittings Library tab (form with dropdowns)
#   2. "Add to Fittings" button on eBay/Alibaba product cards
#   3. CSV import (same columns as export)
#   4. Automatic extraction by the Chrome extension (content.js extracts
#      technical attributes from eBay page text and sends them alongside
#      normal product data; server.py can auto-create fittings from that)
#
# TABLE SCHEMA:
#   fittings: one row per part. Fields map to the user's data architecture:
#     - category: Hose, Straight Joiner, Elbow 90°, Tee/Y-Piece, Cross Joiner, Bulkhead
#     - material: Brass, Stainless Steel, PVC, Nylon, Aluminum, Copper
#     - barb_size: hose barb diameter (e.g. "8mm", "1/4\"")
#     - thread: thread specification (e.g. "NPT 1/8\"", "M10x1")
#     - pressure: max working pressure (e.g. "10 Bar", "150 PSI")
#     - temp: operating temperature range (e.g. "-20 to 120 °C")
#     - grade: Industrial, Food Grade, Pneumatic, General
#     - source_item_id: eBay item_id (only if created from an eBay product)
#     - source_url: original page URL
#
#   fitting_variations: child rows for parts that come in multiple sizes.
#     e.g. "Brass Y-Piece" might have variations for 6mm, 8mm, 10mm.
#     Each variation has its own size, sku, and pressure rating.
#
# FUTURE DEVELOPERS:
#   - To add new categories/materials/grades, update the constant lists in
#     the dashboard JS (FITTING_CATEGORIES, FITTING_MATERIALS, FITTING_GRADES)
#     in server.py's dashboard_html() function.
#   - The extraction logic for automatically detecting attributes from text
#     lives in content.js (extractFittingAttributes function).
# ============================================================================


def _init_fittings_tables():
    """
    Create the fittings and fitting_variations tables.
    """
    conn = connect()
    cur = conn.cursor()
    cur.executescript('''
        CREATE TABLE IF NOT EXISTS fittings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL DEFAULT '',
            sku TEXT DEFAULT '',
            category TEXT DEFAULT '',
            material TEXT DEFAULT '',
            barb_size TEXT DEFAULT '',
            thread TEXT DEFAULT '',
            pressure TEXT DEFAULT '',
            temp TEXT DEFAULT '',
            grade TEXT DEFAULT '',
            image_url TEXT DEFAULT '',
            source_url TEXT DEFAULT '',
            source_item_id TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS fitting_variations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fitting_id INTEGER NOT NULL,
            size TEXT DEFAULT '',
            sku TEXT DEFAULT '',
            pressure TEXT DEFAULT '',
            image_url TEXT DEFAULT '',
            FOREIGN KEY (fitting_id) REFERENCES fittings(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_fittings_category ON fittings(category);
        CREATE INDEX IF NOT EXISTS idx_fittings_material ON fittings(material);
        CREATE INDEX IF NOT EXISTS idx_fittings_grade ON fittings(grade);
        CREATE INDEX IF NOT EXISTS idx_fittings_sku ON fittings(sku);
        CREATE INDEX IF NOT EXISTS idx_fitvar_fid ON fitting_variations(fitting_id);
    ''')
    # Migration: add image_url column to fitting_variations if it doesn't exist
    try:
        cur.execute("SELECT image_url FROM fitting_variations LIMIT 1")
    except Exception:
        cur.execute("ALTER TABLE fitting_variations ADD COLUMN image_url TEXT DEFAULT ''")
        print("[DB] Added image_url column to fitting_variations")
    conn.commit()
    conn.close()

_init_fittings_tables()

# Gemini API key storage
def _init_gemini_settings_table():
    conn = connect()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS gemini_settings (
            id INTEGER PRIMARY KEY DEFAULT 1,
            gemini_key TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Ensure exactly one row exists
    row = conn.execute("SELECT COUNT(*) AS c FROM gemini_settings").fetchone()
    if row['c'] == 0:
        conn.execute("INSERT INTO gemini_settings (id, gemini_key) VALUES (1, '')")
    conn.commit()
    conn.close()

_init_gemini_settings_table()

# Gemini API key storage (so the server can use Gemini for fitting analysis)
def _init_gemini_settings_table():
    conn = connect()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS gemini_settings (
            id INTEGER PRIMARY KEY DEFAULT 1,
            gemini_key TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Ensure exactly one row exists
    row = conn.execute("SELECT COUNT(*) AS c FROM gemini_settings").fetchone()
    if row['c'] == 0:
        conn.execute("INSERT INTO gemini_settings (id, gemini_key) VALUES (1, '')")
    conn.commit()
    conn.close()

_init_gemini_settings_table()

def save_gemini_key(key):
    conn = connect()
    conn.execute("UPDATE gemini_settings SET gemini_key=?, updated_at=CURRENT_TIMESTAMP WHERE id=1", (key,))
    conn.commit()
    conn.close()
    return {'ok': True}

def get_gemini_key():
    conn = connect()
    row = conn.execute("SELECT gemini_key FROM gemini_settings WHERE id=1").fetchone()
    conn.close()
    return row['gemini_key'] if row else ''



def list_fittings(category=None, material=None, grade=None, size=None, search=None):
    """
    List all fittings, optionally filtered by category, material, grade, or
    a free-text search on name/sku. The 'size' filter also checks variation sizes.
    Returns a list of dicts; each dict includes a 'variations' list of child rows.
    """
    conn = connect()
    query = "SELECT * FROM fittings WHERE 1=1"
    params = []
    if category:
        query += " AND category = ?"
        params.append(category)
    if material:
        query += " AND material = ?"
        params.append(material)
    if grade:
        query += " AND grade = ?"
        params.append(grade)
    if search:
        query += " AND (name LIKE ? OR sku LIKE ?)"
        params.extend([f"%{search}%", f"%{search}%"])
    query += " ORDER BY created_at DESC"
    rows = conn.execute(query, params).fetchall()

    result = []
    for row in rows:
        fitting = dict(row)
        # Attach child variation rows for this fitting
        var_rows = conn.execute(
            "SELECT id, size, sku, pressure, image_url FROM fitting_variations "
            "WHERE fitting_id = ? ORDER BY id",
            (fitting['id'],)
        ).fetchall()
        fitting['variations'] = [dict(v) for v in var_rows]

        # If a size filter was requested, check main barb_size AND variation sizes
        if size:
            sizes = [fitting.get('barb_size', '').lower()] + \
                   [v['size'].lower() for v in fitting['variations'] if v.get('size')]
            if not any(size.lower() in s for s in sizes):
                continue

        result.append(fitting)

    conn.close()
    return result


def add_fitting(data):
    """
    Upsert a fitting record — if a fitting with the same source_url already
    exists, update it in-place instead of creating a duplicate.
    Returns {'ok': True, 'id': fitting_id, 'created': True/False}.
    """
    conn = connect()
    cur = conn.cursor()

    # ── Duplicate check by source_url (NOT eBay item_id) ─────────────
    source_url = data.get('source_url', '').strip()
    existing_id = None
    if source_url:
        row = cur.execute(
            "SELECT id FROM fittings WHERE source_url = ? LIMIT 1",
            (source_url,)
        ).fetchone()
        if row:
            existing_id = row['id']

    if existing_id:
        # UPDATE the existing fitting record
        cur.execute("""
            UPDATE fittings SET
                name=?, sku=?, category=?, material=?, barb_size=?, thread=?,
                pressure=?, temp=?, grade=?, source_url=?, notes=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
        """, (
            data.get('name', ''),
            data.get('sku', ''),
            data.get('category', ''),
            data.get('material', ''),
            data.get('barb_size', ''),
            data.get('thread', ''),
            data.get('pressure', ''),
            data.get('temp', ''),
            data.get('grade', ''),
            data.get('source_url', ''),
            data.get('notes', ''),
            existing_id,
        ))
        # Update image_url only if a new one is provided
        if data.get('image_url', ''):
            cur.execute("UPDATE fittings SET image_url=? WHERE id=?",
                        (data['image_url'], existing_id))
        # Replace variations (delete old, insert new)
        cur.execute("DELETE FROM fitting_variations WHERE fitting_id=?", (existing_id,))
        for var in data.get('variations', []):
            cur.execute("""
                INSERT INTO fitting_variations (fitting_id, size, sku, pressure)
                VALUES (?, ?, ?, ?)
            """, (existing_id, var.get('size', ''), var.get('sku', ''), var.get('pressure', '')))
        conn.commit()
        conn.close()
        return {'ok': True, 'id': existing_id, 'created': False}

    # ── INSERT new fitting ───────────────────────────────────────────
    cur.execute("""
        INSERT INTO fittings
            (name, sku, category, material, barb_size, thread, pressure,
             temp, grade, image_url, source_url, source_item_id, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data.get('name', ''),
        data.get('sku', ''),
        data.get('category', ''),
        data.get('material', ''),
        data.get('barb_size', ''),
        data.get('thread', ''),
        data.get('pressure', ''),
        data.get('temp', ''),
        data.get('grade', ''),
        data.get('image_url', ''),
        data.get('source_url', ''),
        data.get('source_item_id', ''),
        data.get('notes', ''),
    ))
    fitting_id = cur.lastrowid

    for var in data.get('variations', []):
        cur.execute("""
            INSERT INTO fitting_variations (fitting_id, size, sku, pressure)
            VALUES (?, ?, ?, ?)
        """, (fitting_id, var.get('size', ''), var.get('sku', ''), var.get('pressure', '')))

    conn.commit()
    conn.close()
    return {'ok': True, 'id': fitting_id, 'created': True}


def update_fitting(fitting_id, data):
    """
    Update an existing fitting by id. Also replaces all variation rows
    (delete + re-insert) so the caller can send the full new variation list.
    """
    conn = connect()
    cur = conn.cursor()
    cur.execute("""
        UPDATE fittings SET
            name=?, sku=?, category=?, material=?, barb_size=?, thread=?,
            pressure=?, temp=?, grade=?, image_url=?, notes=?,
            updated_at=CURRENT_TIMESTAMP
        WHERE id=?
    """, (
        data.get('name', ''), data.get('sku', ''), data.get('category', ''),
        data.get('material', ''), data.get('barb_size', ''), data.get('thread', ''),
        data.get('pressure', ''), data.get('temp', ''), data.get('grade', ''),
        data.get('image_url', ''), data.get('notes', ''), fitting_id
    ))
    # Replace all variations: delete old, insert new
    cur.execute("DELETE FROM fitting_variations WHERE fitting_id=?", (fitting_id,))
    for var in data.get('variations', []):
        cur.execute("""
            INSERT INTO fitting_variations (fitting_id, size, sku, pressure)
            VALUES (?, ?, ?, ?)
        """, (fitting_id, var.get('size', ''), var.get('sku', ''), var.get('pressure', '')))
    conn.commit()
    conn.close()
    return {'ok': True}


def delete_fitting(fitting_id):
    """Delete a fitting and all its variation rows (cascade)."""
    conn = connect()
    cur = conn.cursor()
    cur.execute("DELETE FROM fitting_variations WHERE fitting_id=?", (fitting_id,))
    cur.execute("DELETE FROM fittings WHERE id=?", (fitting_id,))
    conn.commit()
    conn.close()
    return {'ok': True}


def get_fitting_stats():
    """
    Return summary counts for the Fittings Library tab header stats bar.
    Returns: {total, by_category: {cat: count}, by_material: {mat: count}, by_grade: {grade: count}}
    """
    conn = connect()
    total = conn.execute("SELECT COUNT(*) AS c FROM fittings").fetchone()['c']
    by_category = {r['category']: r['c'] for r in conn.execute(
        "SELECT category, COUNT(*) AS c FROM fittings "
        "WHERE category IS NOT NULL AND category != '' GROUP BY category"
    ).fetchall()}
    by_material = {r['material']: r['c'] for r in conn.execute(
        "SELECT material, COUNT(*) AS c FROM fittings "
        "WHERE material IS NOT NULL AND material != '' GROUP BY material"
    ).fetchall()}
    by_grade = {r['grade']: r['c'] for r in conn.execute(
        "SELECT grade, COUNT(*) AS c FROM fittings "
        "WHERE grade IS NOT NULL AND grade != '' GROUP BY grade"
    ).fetchall()}
    conn.close()
    return {
        'total': total,
        'by_category': by_category,
        'by_material': by_material,
        'by_grade': by_grade
    }


def add_fitting_from_product(item_id, overrides=None):
    """
    Create a fitting record from an existing eBay product in the products table.
    This is called when the user clicks "Add to Fittings Library" on a product card.

    The function looks up the product by item_id (to find the product in the
    products table), but does NOT use item_id as the fitting's dedup key.
    Dedup is done by source_url instead.

    Returns {'ok': True, 'id': fitting_id} or {'ok': False, 'error': msg}.
    """
    conn = connect()
    product = conn.execute("SELECT * FROM products WHERE item_id=?", (item_id,)).fetchone()
    conn.close()
    if not product:
        return {'ok': False, 'error': f'Product {item_id} not found'}
    product = dict(product)
    title = product.get('title', '') or ''

    # Smart extraction from title text (server-side fallback).
    # The extension's content.js also does this and sends results via overrides.
    text = title.lower()

    # Material detection
    material_map = {
        'brass': 'Brass', 'stainless': 'Stainless Steel', 'steel': 'Stainless Steel',
        'pvc': 'PVC', 'nylon': 'Nylon', 'aluminum': 'Aluminum', 'copper': 'Copper',
        'plastic': 'PVC'
    }
    material = ''
    for kw, val in material_map.items():
        if kw in text:
            material = val
            break

    # Barb size detection (mm first, then inch fractions)
    barb_size = ''
    import re as _re
    m = _re.search(r'(\d+(?:\.\d+)?\s*mm\b)', text)
    if m:
        barb_size = m.group(1)
    else:
        m = _re.search(r'(\d+/\d+\s*(?:"|inch))', text)
        if m:
            barb_size = m.group(1)

    # Pressure detection
    pressure = ''
    m = _re.search(r'(\d+(?:\.\d+)?)\s*(bar|psi)', text)
    if m:
        pressure = m.group(1) + ' ' + m.group(2).upper()

    # Thread detection
    thread = ''
    m = _re.search(r'(npt\s*\d+/\d+"?)', text)
    if m:
        thread = m.group(1).upper()
    else:
        m = _re.search(r'(m\d+\s*(?:x\s*\d+\.?\d*)?)', text)
        if m:
            thread = m.group(1).upper()

    # Grade detection
    grade = ''
    if 'food' in text or 'fda' in text:
        grade = 'Food Grade'
    elif 'pneumatic' in text:
        grade = 'Pneumatic'
    elif 'industrial' in text:
        grade = 'Industrial'

    # Build the fitting data dict
    data = {
        'name': title,
        'sku': '',
        'source_item_id': '',
        'source_url': product.get('product_url', ''),
        'image_url': product.get('image_url', ''),
        'category': '',
        'material': material,
        'barb_size': barb_size,
        'thread': thread,
        'pressure': pressure,
        'temp': '',
        'grade': grade,
        'notes': '',
    }

    # Override with values extracted by the extension (more accurate)
    if overrides:
        for key in ['category', 'material', 'barb_size', 'thread', 'pressure',
                     'temp', 'grade', 'notes', 'sku', 'name']:
            if overrides.get(key):
                data[key] = overrides[key]

    return add_fitting(data)
