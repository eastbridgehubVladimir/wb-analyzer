"""
Server-side chart rendering utilities for PDF reports.
Generates PNG images in-memory using matplotlib.
"""
from io import BytesIO
from typing import List
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt


def chart_price_distribution(
    products: List[dict],
    median_price: float = 0,
    price_iqr: float = 0,
) -> BytesIO:
    """Render price distribution histogram.

    Uses real product prices if available; otherwise builds a synthetic
    bell-curve approximation from median_price + price_iqr.
    """
    prices = [p.get('price', 0) for p in products if p.get('price', 0) > 0]

    buf = BytesIO()

    if not prices and median_price > 0:
        # Synthetic distribution: 7 price buckets with normal-shaped weights
        iqr = price_iqr if price_iqr > 0 else median_price * 0.3
        p25 = max(median_price - iqr / 2, median_price * 0.3)
        p75 = median_price + iqr / 2
        # Evenly-spaced buckets: p25/2 … p75*1.5
        lo, hi = p25 * 0.6, p75 * 1.5
        step = (hi - lo) / 6
        centers = [lo + step * i + step / 2 for i in range(6)]
        # Gaussian weights centered on median
        import math
        sigma = iqr / 2
        raw = [math.exp(-0.5 * ((c - median_price) / sigma) ** 2) for c in centers]
        total = sum(raw)
        weights = [r / total * 100 for r in raw]  # scale to %

        fig, ax = plt.subplots(figsize=(6, 2.5))
        bar_w = step * 0.8
        bars = ax.bar(centers, weights, width=bar_w, color='#4C78A8', edgecolor='white')
        # Highlight the median bar
        for bar, c in zip(bars, centers):
            if abs(c - median_price) == min(abs(x - median_price) for x in centers):
                bar.set_color('#E45756')
        ax.axvline(median_price, color='#E45756', linestyle='--', linewidth=1.2,
                   label=f'Медиана {median_price:,.0f} ₽')
        ax.set_xlabel('Цена (₽)')
        ax.set_ylabel('Доля продавцов (%)')
        ax.set_title('Распределение цен в нише (оценка)')
        ax.legend(fontsize=8)
        ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f'{x:,.0f}'))
        fig.tight_layout()
        fig.savefig(buf, format='png', dpi=150)
        plt.close(fig)
        buf.seek(0)
        return buf

    if not prices:
        fig, ax = plt.subplots(figsize=(6, 2.5))
        ax.text(0.5, 0.5, 'Нет данных о ценах', ha='center', va='center', fontsize=12)
        ax.axis('off')
        fig.tight_layout()
        fig.savefig(buf, format='png', dpi=150)
        plt.close(fig)
        buf.seek(0)
        return buf

    fig, ax = plt.subplots(figsize=(6, 2.5))
    ax.hist(prices, bins=10, color='#4C78A8', edgecolor='white')
    if median_price > 0:
        ax.axvline(median_price, color='#E45756', linestyle='--', linewidth=1.2,
                   label=f'Медиана {median_price:,.0f} ₽')
        ax.legend(fontsize=8)
    ax.set_title('Распределение цен в нише')
    ax.set_xlabel('Цена (₽)')
    ax.set_ylabel('Кол-во товаров')
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f'{x:,.0f}'))
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
