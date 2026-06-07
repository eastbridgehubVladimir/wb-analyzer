"""
Server-side chart rendering utilities for PDF reports.
Generates PNG images in-memory using matplotlib.
"""
from io import BytesIO
from typing import List
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt


def chart_price_distribution(products: List[dict]) -> BytesIO:
    """Render price distribution histogram for products.

    Args:
        products: list of product dicts with 'price' key
    Returns:
        BytesIO containing PNG image
    """
    prices = [p.get('price', 0) for p in products if p.get('price', 0) > 0]
    buf = BytesIO()
    if not prices:
        # create empty placeholder image
        fig, ax = plt.subplots(figsize=(6, 2.5))
        ax.text(0.5, 0.5, 'No price data', ha='center', va='center')
        ax.axis('off')
        fig.tight_layout()
        fig.savefig(buf, format='png', dpi=150)
        plt.close(fig)
        buf.seek(0)
        return buf

    fig, ax = plt.subplots(figsize=(6, 2.5))
    ax.hist(prices, bins=10, color='#4C78A8', edgecolor='white')
    ax.set_title('Price distribution')
    ax.set_xlabel('Price (₽)')
    ax.set_ylabel('Count')
    fig.tight_layout()
    fig.savefig(buf, format='png', dpi=150)
    plt.close(fig)
    buf.seek(0)
    return buf


def chart_top_revenue(products: List[dict], top_n: int = 10) -> BytesIO:
    """Render horizontal bar chart of top revenue contributors.

    revenue = price * max(1, reviews_count) * 0.1 (same heuristic as inline_metrics)
    """
    items = []
    for p in products:
        price = p.get('price', 0) or 0
        reviews = p.get('reviews_count', 0) or 0
        rev = price * max(1, reviews) * 0.1
        items.append((p.get('name', str(p.get('wb_sku', ''))), rev))

    items = sorted(items, key=lambda x: x[1], reverse=True)[:top_n]
    buf = BytesIO()
    if not items:
        fig, ax = plt.subplots(figsize=(6, 2.5))
        ax.text(0.5, 0.5, 'No revenue data', ha='center', va='center')
        ax.axis('off')
        fig.tight_layout()
        fig.savefig(buf, format='png', dpi=150)
        plt.close(fig)
        buf.seek(0)
        return buf

    names = [n if len(n) <= 30 else n[:27] + '...' for n, _ in items]
    values = [v for _, v in items]

    fig, ax = plt.subplots(figsize=(6, 2.5))
    y_pos = range(len(names))[::-1]
    ax.barh(y_pos, values, color='#59A14F')
    ax.set_yticks(range(len(names)))
    ax.set_yticklabels(names)
    ax.set_xlabel('Estimated monthly revenue (₽)')
    ax.set_title('Top revenue contributors')
    fig.tight_layout()
    fig.savefig(buf, format='png', dpi=150)
    plt.close(fig)
    buf.seek(0)
    return buf
