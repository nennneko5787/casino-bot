"""株価の折れ線グラフを画像 (PNG) として描画する。"""

from __future__ import annotations

import io

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt


def render_stock_chart(
    history: list[tuple[str, int]],
    ticker: str,
    *,
    width: int = 10,
    height: int = 5,
) -> io.BytesIO:
    """価格履歴から折れ線グラフの PNG を返す。history が空でも空グラフを返す。"""
    prices = [p for _, p in history]
    fig, ax = plt.subplots(figsize=(width, height), dpi=100)
    try:
        if prices:
            xs = list(range(len(prices)))
            up = prices[-1] >= prices[0]
            color = "#26a69a" if up else "#ef5350"
            ax.plot(xs, prices, color=color, linewidth=2)
            ax.fill_between(xs, prices, alpha=0.15, color=color)
            ax.scatter([xs[-1]], [prices[-1]], color=color, s=40, zorder=5)
            ax.set_title(f"{ticker}  price: {prices[-1]} (n={len(prices)})")
            ymin, ymax = min(prices), max(prices)
            pad = max(1, int((ymax - ymin) * 0.1))
            ax.set_ylim(ymin - pad, ymax + pad)
        else:
            ax.set_title(f"{ticker}  no data")
        ax.set_xlabel("tick")
        ax.set_ylabel("price")
        ax.grid(True, alpha=0.3)
        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png")
        buf.seek(0)
        return buf
    finally:
        plt.close(fig)
