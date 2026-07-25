# AI Analyzer - connects the local product database to OpenAI for analysis
#
# This module gathers relevant data from the SQLite database, formats it as
# context for the AI, and sends it to the OpenAI API for analysis.
# The user can ask questions like:
#   - "What's the best product to sell?"
#   - "Compare the top Alibaba suppliers"
#   - "Which eBay products have the most sales?"
#   - "What's the profit margin if I source from Alibaba and sell on eBay?"
#
# Requirements: pip install openai

import sqlite3
import json
import os
from pathlib import Path
from database import DB_PATH, connect


# -------------------------------------------------------------------
# Data gathering functions
# -------------------------------------------------------------------

def gather_ebay_products(limit=50):
    """Return top eBay products sorted by total sold."""
    conn = connect()
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT item_id, title, price_text, seller_username,
               total_sold, available_quantity, watch_count,
               postage_text, product_url, image_url,
               first_seen_at, last_seen_at
        FROM products
        ORDER BY COALESCE(total_sold, 0) DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def gather_alibaba_products(limit=50):
    """Return top Alibaba products sorted by sold count."""
    conn = connect()
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT product_key, title, price_text, min_price,
               supplier_name, country, sold_count, rating,
               review_count, min_order_text, product_url, image_url,
               shipping_text, delivery_text
        FROM alibaba_products
        ORDER BY COALESCE(sold_count, 0) DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def gather_sales_summary():
    """Return sales summary statistics."""
    conn = connect()
    conn.row_factory = sqlite3.Row

    total_sales = conn.execute("SELECT COUNT(*) as c FROM sales").fetchone()["c"]
    total_revenue = conn.execute("SELECT COALESCE(SUM(price * quantity), 0) as r FROM sales").fetchone()["r"]

    top_by_sales = conn.execute("""
        SELECT p.title, p.price_text, s.item_id,
               COUNT(*) as sale_count,
               SUM(s.quantity) as total_qty,
               SUM(s.price * s.quantity) as revenue
        FROM sales s
        JOIN products p ON p.item_id = s.item_id
        GROUP BY s.item_id
        ORDER BY sale_count DESC
        LIMIT 10
    """).fetchall()

    recent = conn.execute("""
        SELECT s.item_id, p.title, s.variation, s.price_text,
               s.quantity, s.sold_at_text
        FROM sales s
        JOIN products p ON p.item_id = s.item_id
        ORDER BY s.collected_at DESC
        LIMIT 20
    """).fetchall()

    conn.close()
    return {
        "total_sales": total_sales,
        "total_revenue": round(total_revenue, 2),
        "top_products_by_sales": [dict(r) for r in top_by_sales],
        "recent_sales": [dict(r) for r in recent],
    }


def gather_sellers_summary():
    """Return eBay seller performance summary."""
    conn = connect()
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT seller_username,
               COUNT(*) as product_count,
               SUM(COALESCE(total_sold, 0)) as total_sold_sum,
               AVG(COALESCE(total_sold, 0)) as avg_sold,
               GROUP_CONCAT(DISTINCT price_text) as price_range
        FROM products
        WHERE seller_username IS NOT NULL
        GROUP BY seller_username
        ORDER BY total_sold_sum DESC
        LIMIT 20
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def gather_alibaba_suppliers():
    """Return Alibaba supplier comparison."""
    conn = connect()
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT supplier_name,
               COUNT(*) as product_count,
               AVG(min_price) as avg_min_price,
               MIN(min_price) as lowest_price,
               MAX(min_price) as highest_price,
               SUM(COALESCE(sold_count, 0)) as total_sold,
               AVG(rating) as avg_rating,
               AVG(review_count) as avg_reviews
        FROM alibaba_products
        WHERE supplier_name IS NOT NULL
        GROUP BY supplier_name
        ORDER BY total_sold DESC
        LIMIT 20
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def build_context():
    """Build a complete data context string for the AI."""
    ebay = gather_ebay_products(30)
    alibaba = gather_alibaba_products(30)
    sales = gather_sales_summary()
    sellers = gather_sellers_summary()
    suppliers = gather_alibaba_suppliers()

    context = {
        "ebay_products": ebay,
        "alibaba_products": alibaba,
        "sales_summary": sales,
        "ebay_sellers": sellers,
        "alibaba_suppliers": suppliers,
        "data_counts": {
            "ebay_products_total": len(gather_ebay_products(10000)),
            "alibaba_products_total": len(gather_alibaba_products(10000)),
            "sales_total": sales["total_sales"],
        }
    }
    return json.dumps(context, ensure_ascii=False, indent=2)


# -------------------------------------------------------------------
# AI query function
# -------------------------------------------------------------------

SYSTEM_PROMPT = """You are a product research and e-commerce analysis assistant.

You have access to a database of eBay products and Alibaba products that a seller has collected.

Your job:
1. Analyze the data to find the best products to sell on eBay.
2. Compare Alibaba suppliers to find the best sourcing opportunities.
3. Calculate potential profit margins (eBay selling price - Alibaba cost - eBay fees ~13%).
4. Identify trending products, high-demand items, and underserved niches.
5. Compare sellers and suppliers based on ratings, sales volume, and pricing.
6. Give actionable recommendations in clear, simple language.

When answering:
- Use the actual data from the context, not generic advice.
- Compare specific products by name and price.
- Calculate profit margins when relevant.
- Rank products/suppliers when asked.
- Be specific and data-driven.
- Respond in the same language as the user's question (Persian/Farsi or English).
"""

QUICK_PROMPTS = {
    "best_product": "Based on the eBay sales data and Alibaba sourcing data, what are the top 5 best products to sell right now? For each, calculate the estimated profit margin and explain why it's a good choice.",
    "compare_suppliers": "Compare the top Alibaba suppliers. Which ones have the best combination of price, rating, and sales volume? Rank them and explain.",
    "profit_analysis": "Calculate the profit margin for each eBay product if sourced from the cheapest Alibaba supplier. Which products have the highest profit potential?",
    "trending": "Which products are trending based on sales data, watch counts, and sold quantities? What should the seller stock up on?",
    "market_gaps": "Are there any product categories or variations that are in high demand on eBay but have low competition? Identify market gaps.",
}


def ask_ai(question, api_key=None, model="gpt-4o"):
    """Send a question to OpenAI with database context and return the answer."""
    try:
        from openai import OpenAI
    except ImportError:
        return {
            "ok": False,
            "error": "openai package not installed. Run: pip install openai",
        }

    api_key = api_key or os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_KEY")
    if not api_key:
        return {
            "ok": False,
            "error": "No OpenAI API key found. Set OPENAI_API_KEY environment variable or enter it in the dashboard.",
        }

    context = build_context()

    client = OpenAI(api_key=api_key)

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Here is my product database:\n\n{context}\n\nMy question: {question}"},
            ],
            temperature=0.7,
            max_tokens=2000,
        )
        answer = response.choices[0].message.content
        return {
            "ok": True,
            "answer": answer,
            "model": model,
            "question": question,
        }
    except Exception as e:
        return {
            "ok": False,
            "error": f"OpenAI API error: {str(e)}",
        }


def quick_analysis(prompt_key, api_key=None, model="gpt-4o"):
    """Run a pre-defined analysis prompt."""
    question = QUICK_PROMPTS.get(prompt_key)
    if not question:
        return {"ok": False, "error": f"Unknown prompt: {prompt_key}"}
    return ask_ai(question, api_key, model)


def get_data_stats():
    """Return database statistics for the AI tab dashboard."""
    return {
        "ebay_products": len(gather_ebay_products(10000)),
        "alibaba_products": len(gather_alibaba_products(10000)),
        "sales": gather_sales_summary()["total_sales"],
        "sellers": len(gather_sellers_summary()),
        "suppliers": len(gather_alibaba_suppliers()),
    }
