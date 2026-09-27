"""株価の折れ線グラフを画像 (PNG) として描画する。

日本語ラベル用にリポジトリ同梱の IPAex ゴシック
(assets/fonts/ipaexg.ttf) を登録する。本番環境のシステムフォントに
依存しないため、Linux/Windows どちらでも tofu にならない。
"""

from __future__ import annotations

import io
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib import font_manager

_REPO_ROOT = Path(__file__).resolve().parents[1]
_BUNDLED_FONT = _REPO_ROOT / "assets" / "fonts" / "ipaexg.ttf"

if _BUNDLED_FONT.exists():
    font_manager.fontManager.addfont(str(_BUNDLED_FONT))
    plt.rcParams["font.family"] = ["IPAexGothic", "DejaVu Sans"]


def render_stock_chart(
    history: list[tuple[str, int]],
    ticker: str,
    currency: str = "",
    *,
    width: int = 10,
    height: int = 5,
) -> io.BytesIO:
    """価格履歴から折れ線グラフの PNG を返す。history が空でも空グラフを返す。"""
    prices = [p for _, p in history]
    unit = currency or "通貨"
    fig, ax = plt.subplots(figsize=(width, height), dpi=100)
    try:
        if prices:
            xs = list(range(len(prices)))
            up = prices[-1] >= prices[0]
            color = "#26a69a" if up else "#ef5350"
            ax.plot(xs, prices, color=color, linewidth=2)
            ax.fill_between(xs, prices, alpha=0.15, color=color)
            ax.scatter([xs[-1]], [prices[-1]], color=color, s=40, zorder=5)
            ax.set_title(
                f"{ticker} 株価チャート（現在 {prices[-1]}{unit}・{len(prices)}件）"
            )
            ymin, ymax = min(prices), max(prices)
            pad = max(1, int((ymax - ymin) * 0.1))
            ax.set_ylim(ymin - pad, ymax + pad)
        else:
            ax.set_title(f"{ticker} 株価チャート（データなし）")
        ax.set_xlabel("更新回数")
        ax.set_ylabel(f"価格（{unit}）")
        ax.grid(True, alpha=0.3)
        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png")
        buf.seek(0)
        return buf
    finally:
        plt.close(fig)
