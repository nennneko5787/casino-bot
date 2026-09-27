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
from matplotlib import cm, font_manager

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


def render_multi_stock_chart(
    histories: dict[str, list[tuple[str, int]]],
    currency: str = "",
    *,
    width: int = 12,
    height: int = 6,
) -> io.BytesIO:
    """複数銘柄の変化率チャート (PNG) を返す。

    価格帯が違っても比較できるよう、各銘柄は表示区間の
    先頭価格を0%とした変化率に正規化する。データ不足の銘柄は除外し、
    全滅の場合は空グラフを返す。
    """
    unit = currency or "通貨"
    fig, ax = plt.subplots(figsize=(width, height), dpi=100)
    try:
        names = sorted(histories.keys())
        colors = [cm.tab10(i % 10) for i in range(len(names))]
        plotted = 0
        for ticker, color in zip(names, colors):
            history = histories[ticker]
            prices = [p for _, p in history]
            if len(prices) < 2 or prices[0] <= 0:
                continue
            base = prices[0]
            pcts = [(p - base) / base * 100 for p in prices]
            xs = list(range(len(pcts)))
            ax.plot(xs, pcts, color=color, linewidth=2,
                    label=f"{ticker} ({prices[-1]}{unit}, {pcts[-1]:+.1f}%)")
            ax.scatter([xs[-1]], [pcts[-1]], color=color, s=40, zorder=5)
            plotted += 1
        count = max((len(v) for v in histories.values()), default=0)
        if plotted:
            ax.set_title(f"銘柄比較チャート（{plotted}銘柄・直近{count}件・変化率）")
            ax.legend(loc="best")
        else:
            ax.set_title("銘柄比較チャート（データなし）")
        ax.set_xlabel("更新回数")
        ax.set_ylabel("変化率（%）")
        ax.axhline(0, color="gray", linewidth=1, alpha=0.6)
        ax.grid(True, alpha=0.3)
        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png")
        buf.seek(0)
        return buf
    finally:
        plt.close(fig)


def render_currency_chart(
    points: list[tuple[str, float]],
    *,
    width: int = 10,
    height: int = 5,
) -> io.BytesIO:
    """通貨価値指数のチャート (PNG) を返す。points は (時刻, 指数・起点100)。

    上がる=通貨高 (株安)、下がる=通貨安 (株高)。
    """
    values = [v for _, v in points]
    fig, ax = plt.subplots(figsize=(width, height), dpi=100)
    try:
        if values:
            xs = list(range(len(values)))
            up = values[-1] >= values[0]
            color = "#26a69a" if up else "#ef5350"
            ax.plot(xs, values, color=color, linewidth=2)
            ax.fill_between(xs, values, 100, alpha=0.15, color=color)
            ax.scatter([xs[-1]], [values[-1]], color=color, s=40, zorder=5)
            ax.set_title(f"通貨価値指数チャート（現在 {values[-1]:.1f}・起点100）")
            ax.axhline(100, color="gray", linewidth=1, alpha=0.6)
            lo, hi = min(values), max(values)
            pad = max(0.5, (hi - lo) * 0.1)
            ax.set_ylim(min(lo, 100) - pad, max(hi, 100) + pad)
        else:
            ax.set_title("通貨価値指数チャート（データなし）")
        ax.set_xlabel("更新回数")
        ax.set_ylabel("指数（起点=100）")
        ax.grid(True, alpha=0.3)
        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png")
        buf.seek(0)
        return buf
    finally:
        plt.close(fig)
