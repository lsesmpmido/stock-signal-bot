"""売買シグナル通知用のチャート画像を io.BytesIO に生成する（ディスクには保存しない）。

画像1: ローソク足＋移動平均 3 本 / RSI / MACD（ヒストグラム付き）
画像2: 5分足・日足・週足・月足の 2×2 マルチ時間軸チャート
"""

from __future__ import annotations

import io
import logging

import matplotlib

matplotlib.use("Agg")  # 画面のないサーバー環境用

import matplotlib.pyplot as plt  # noqa: E402
import mplfinance as mpf  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402

import market  # noqa: E402

log = logging.getLogger(__name__)

try:  # 日本語フォント（IPAex ゴシック）を同梱したパッケージ
    import matplotlib_fontja  # noqa: F401

    FONT_FAMILY = "IPAexGothic"
    NO_DATA = "データなし"
except ImportError:
    FONT_FAMILY = "DejaVu Sans"
    NO_DATA = "No data"

SURFACE = "#fcfcfb"
TEXT = "#0b0b0b"
MUTED = "#52514e"
GRID = "#e4e3df"
UP = "#e34948"  # 陽線
DOWN = "#1baf7a"  # 陰線
MA_COLORS = {"MA25": "#2a78d6", "MA75": "#eb6834", "MA200": "#4a3aa7"}
MACD_COLOR = "#2a78d6"
SIGNAL_COLOR = "#eb6834"
RSI_COLOR = "#4a3aa7"

STYLE = mpf.make_mpf_style(
    marketcolors=mpf.make_marketcolors(up=UP, down=DOWN, edge="inherit", wick="inherit", volume="inherit"),
    facecolor=SURFACE,
    figcolor=SURFACE,
    edgecolor=GRID,
    gridcolor=GRID,
    gridstyle="-",
    rc={
        "font.family": FONT_FAMILY,
        "axes.labelcolor": MUTED,
        "xtick.color": MUTED,
        "ytick.color": MUTED,
        "text.color": TEXT,
        "axes.titlesize": 11,
    },
)

DETAIL_BARS = 150
WEEKLY_BARS = 52
MONTHLY_BARS = 24
DAILY_BARS = 60


def _to_png(fig) -> io.BytesIO:
    buf = io.BytesIO()
    try:
        fig.savefig(buf, format="png", dpi=110, bbox_inches="tight", facecolor=SURFACE)
    finally:
        plt.close(fig)  # 図を残すとメモリが増え続けるので毎回解放する
    buf.seek(0)
    return buf


def detailed_chart(code: str, name: str, ind: pd.DataFrame) -> io.BytesIO:
    """画像1: 詳細テクニカルチャート。ind は signals.compute() の結果。"""
    df = ind.tail(DETAIL_BARS)
    n = len(df)
    hist = df["MACD_hist"]

    def addplot(data, panel=0, **kwargs):
        # 右側に別の目盛りが自動で付くと読み違えるので、各パネルの目盛りは 1 つに揃える
        return mpf.make_addplot(data, panel=panel, secondary_y=False, **kwargs)

    plots = []
    legend = []
    for col, color in MA_COLORS.items():
        if df[col].notna().any():
            plots.append(addplot(df[col], color=color, width=1.2))
            legend.append(Line2D([], [], color=color, linewidth=2, label=col.replace("MA", "") + "日線"))
    plots += [
        addplot(df["RSI"], panel=1, color=RSI_COLOR, width=1.2, ylabel="RSI", ylim=(0, 100)),
        addplot([70] * n, panel=1, color=MUTED, linestyle="--", width=0.7),
        addplot([30] * n, panel=1, color=MUTED, linestyle="--", width=0.7),
        addplot(hist.where(hist >= 0), panel=2, type="bar", color=UP, alpha=0.5),
        addplot(hist.where(hist < 0), panel=2, type="bar", color=DOWN, alpha=0.5),
        addplot(df["MACD"], panel=2, color=MACD_COLOR, width=1.2, ylabel="MACD"),
        addplot(df["MACD_signal"], panel=2, color=SIGNAL_COLOR, width=1.2),
    ]
    fig, axes = mpf.plot(
        df,
        type="candle",
        style=STYLE,
        addplot=plots,
        panel_ratios=(3, 1, 1.2),
        figsize=(11, 8),
        title=f"{name} ({code})  日足",
        ylabel="株価 (円)",
        datetime_format="%y/%m/%d",
        xrotation=0,
        returnfig=True,
    )
    axes[0].legend(handles=legend, loc="best", fontsize=9, frameon=False)
    macd_legend = [
        Line2D([], [], color=MACD_COLOR, linewidth=2, label="MACD"),
        Line2D([], [], color=SIGNAL_COLOR, linewidth=2, label="シグナル"),
    ]
    axes[4].legend(handles=macd_legend, loc="upper left", fontsize=8, frameon=False)
    return _to_png(fig)


def _draw_no_data(ax, title: str) -> None:
    ax.set_title(title)
    ax.text(0.5, 0.5, NO_DATA, ha="center", va="center", fontsize=14, color=MUTED, transform=ax.transAxes)
    ax.set_xticks([])
    ax.set_yticks([])


def multi_timeframe_chart(
    code: str, name: str, intraday: pd.DataFrame | None, daily: pd.DataFrame | None
) -> io.BytesIO | None:
    """画像2: 5分足・日足・週足・月足の 4 分割。データのないマスは「データなし」を表示し、
    4 マスとも描けなかった場合は None を返す。"""
    panels: list[tuple[str, pd.DataFrame | None, str]] = [("5分足（直近の取引日）", intraday, "%H:%M")]
    if daily is not None and not daily.empty:
        panels += [
            ("日足", daily.tail(DAILY_BARS), "%m/%d"),
            ("週足", market.resample(daily, "W-FRI").tail(WEEKLY_BARS), "%y/%m"),
            ("月足", market.resample(daily, "ME").tail(MONTHLY_BARS), "%y/%m"),
        ]
    else:
        panels += [("日足", None, ""), ("週足", None, ""), ("月足", None, "")]

    fig = mpf.figure(style=STYLE, figsize=(12, 8))
    drawn = 0
    for i, (title, df, fmt) in enumerate(panels, start=1):
        ax = fig.add_subplot(2, 2, i)
        if df is None or df.empty:
            _draw_no_data(ax, title)
            continue
        try:
            mpf.plot(df, type="candle", ax=ax, axtitle=title, ylabel="", datetime_format=fmt, xrotation=0)
            drawn += 1
        except Exception:
            log.warning("%s の %s を描画できませんでした", code, title, exc_info=True)
            ax.clear()
            _draw_no_data(ax, title)
    if drawn == 0:
        plt.close(fig)
        return None
    fig.suptitle(f"{name} ({code})  マルチ時間軸", fontsize=13)
    fig.tight_layout()
    return _to_png(fig)
