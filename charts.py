"""売買シグナル通知用のチャート画像を io.BytesIO に生成する（ディスクには保存しない）。

画像1: ローソク足＋移動平均 3 本 / RSI / MACD（ヒストグラム付き）
画像2: 5分足・日足・週足・月足の 2×2 マルチ時間軸チャート
ほかに、2 銘柄の比較チャートと、監視銘柄と関連企業の関係図（/compare・/map）
"""

from __future__ import annotations

import io
import logging
import math

import matplotlib

matplotlib.use("Agg")  # 画面のないサーバー環境用

import matplotlib.pyplot as plt  # noqa: E402
import mplfinance as mpf  # noqa: E402
import networkx as nx  # noqa: E402
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


def compare_chart(
    a: tuple[str, str, pd.DataFrame], b: tuple[str, str, pd.DataFrame], period_label: str
) -> io.BytesIO | None:
    """2 銘柄の終値を、期間の初日を 100 にそろえて重ねる。a, b は (証券コード, 銘柄名, 日足)。
    共通の日付が 2 日未満なら None。"""
    (a_code, a_name, a_df), (b_code, b_name, b_df) = a, b
    joined = pd.concat({"a": a_df["Close"], "b": b_df["Close"]}, axis=1).dropna()
    if len(joined) < 2:
        return None
    indexed = joined / joined.iloc[0] * 100

    with plt.rc_context({"font.family": FONT_FAMILY}):
        fig, ax = plt.subplots(figsize=(11, 5.5))
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    lines = (("a", a_code, a_name, MA_COLORS["MA25"]), ("b", b_code, b_name, MA_COLORS["MA75"]))
    for col, code, name, color in lines:
        series = indexed[col]
        ax.plot(series.index, series.values, color=color, linewidth=2, label=f"{name} ({code})")
        # 終点に最終値を直接書き込む（凡例と行き来しなくても読めるように）
        ax.annotate(
            f"{series.iloc[-1]:.1f}",
            (series.index[-1], series.iloc[-1]),
            xytext=(6, 0),
            textcoords="offset points",
            va="center",
            color=TEXT,
            fontsize=10,
        )
    ax.axhline(100, color=MUTED, linewidth=0.8, linestyle="--")
    title = f"{a_name} ({a_code}) と {b_name} ({b_code}) の比較（{period_label}、初日 = 100）"
    ax.set_title(title, color=TEXT, fontsize=12, fontfamily=FONT_FAMILY)
    for label in ax.get_xticklabels() + ax.get_yticklabels():
        label.set_fontfamily(FONT_FAMILY)
    ax.grid(color=GRID, linewidth=0.8)
    for spine in ax.spines.values():
        spine.set_color(GRID)
    ax.tick_params(colors=MUTED)
    ax.legend(loc="upper left", frameon=False, prop={"family": FONT_FAMILY, "size": 10})
    ax.margins(x=0.06)
    fig.autofmt_xdate()
    return _to_png(fig)


EDGE_STYLES = {1: (2.6, TEXT), 2: (1.8, MUTED), 3: (1.1, MUTED), 4: (0.8, GRID)}  # 関係の優先度ごとの (線の太さ, 色)


def _component_layout(graph: nx.Graph) -> dict[str, tuple[float, float]]:
    """つながっている会社のまとまりごとに配置し、まとまりを格子状に並べる（離れたまとまりが端に飛ばないように）。"""
    comps = sorted(nx.connected_components(graph), key=lambda c: (-len(c), min(c)))
    cols = max(1, math.ceil(math.sqrt(len(comps))))
    pos: dict[str, tuple[float, float]] = {}
    for i, comp in enumerate(comps):
        sub = graph.subgraph(comp)
        if len(comp) == 1:
            local = {next(iter(comp)): (0.0, 0.0)}
        else:
            # 見た目がいつも同じになるよう、配置の乱数を固定する
            local = nx.spring_layout(sub, seed=7, k=1.2 / math.sqrt(len(comp)), iterations=300)
        cx, cy = (i % cols) * 2.8, -(i // cols) * 2.8
        for node, (x, y) in local.items():
            pos[node] = (cx + float(x), cy + float(y))
    return pos


def relation_map(primary: dict[str, str], others: dict[str, str], edges: list[tuple[str, str, int]]) -> io.BytesIO:
    """監視・保有銘柄（primary）と関連企業（others）の関係図。edges は (証券コード, 証券コード, 優先度)。"""
    graph = nx.Graph()
    graph.add_nodes_from([*primary, *others])
    for a, b, priority in edges:
        if not graph.has_edge(a, b) or priority < graph[a][b]["priority"]:
            graph.add_edge(a, b, priority=priority)
    pos = _component_layout(graph)

    with plt.rc_context({"font.family": FONT_FAMILY}):
        fig, ax = plt.subplots(figsize=(12, 9))
        fig.patch.set_facecolor(SURFACE)
        ax.set_facecolor(SURFACE)
        ax.axis("off")
        for priority, (width, color) in EDGE_STYLES.items():
            chosen = [(a, b) for a, b, d in graph.edges(data=True) if d["priority"] == priority]
            nx.draw_networkx_edges(graph, pos, edgelist=chosen, width=width, edge_color=color, ax=ax)
        nx.draw_networkx_nodes(graph, pos, nodelist=list(others), node_size=220, node_color=GRID, edgecolors=MUTED, ax=ax)
        nx.draw_networkx_nodes(
            graph, pos, nodelist=list(primary), node_size=700, node_color=MA_COLORS["MA25"], edgecolors=TEXT, ax=ax
        )
        # 名前は丸の下に書く（丸や線と重ならないように、背景を付ける）
        for code, (x, y) in pos.items():
            is_primary = code in primary
            name = (primary if is_primary else others)[code]
            ax.text(
                x, y - (0.2 if is_primary else 0.14), f"{name[:12]}\n{code}",
                ha="center", va="top", fontsize=10 if is_primary else 8, color=TEXT if is_primary else MUTED,
                bbox={"boxstyle": "round,pad=0.15", "facecolor": SURFACE, "edgecolor": "none", "alpha": 0.8},
            )
        xs, ys = zip(*pos.values())
        ax.set_xlim(min(xs) - 0.6, max(xs) + 0.6)
        ax.set_ylim(min(ys) - 0.7, max(ys) + 0.4)
        handles = [
            Line2D([], [], color=MA_COLORS["MA25"], marker="o", linestyle="", markersize=12, label="監視・保有銘柄"),
            Line2D([], [], color=GRID, marker="o", markeredgecolor=MUTED, linestyle="", markersize=9, label="関連企業"),
            Line2D([], [], color=TEXT, linewidth=2.6, label="主要取引先・親子会社"),
            Line2D([], [], color=MUTED, linewidth=1.8, label="資本関係・資本業務提携"),
            Line2D([], [], color=MUTED, linewidth=1.1, label="業務提携・技術・共同研究"),
            Line2D([], [], color=GRID, linewidth=0.8, label="取引先・製品導入"),
        ]
        ax.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, 0.0), ncol=3, frameon=False, fontsize=9)
        ax.set_title("監視・保有銘柄と関連企業の関係図", fontsize=14, color=TEXT, loc="left")
    return _to_png(fig)


def sentiment_chart(code: str, name: str, judged: pd.DataFrame, daily: pd.DataFrame | None) -> io.BytesIO:
    """ニュースの感情スコア（Jev のプラス材料の確率）の推移と株価。judged は judged_at・is_positive の列を持つ。"""
    judged = judged.copy()
    judged["day"] = pd.to_datetime(judged["judged_at"]).dt.tz_convert(market.JST).dt.tz_localize(None).dt.normalize()
    per_day = judged.groupby("day")["is_positive"].agg(["mean", "count"])

    with plt.rc_context({"font.family": FONT_FAMILY}):
        fig, ax = plt.subplots(figsize=(11, 5.5))
        fig.patch.set_facecolor(SURFACE)
        ax.set_facecolor(SURFACE)
        ax.axhspan(0.5, 1.0, color=UP, alpha=0.05)
        ax.axhspan(0.0, 0.5, color=DOWN, alpha=0.05)
        ax.axhline(0.5, color=GRID, linewidth=1)
        ax.scatter(judged["day"], judged["is_positive"], s=18, color=MUTED, alpha=0.35, label="記事ごと")
        ax.plot(per_day.index, per_day["mean"], color=MA_COLORS["MA25"], linewidth=2, marker="o", markersize=4, label="日ごとの平均")
        ax.set_ylim(0, 1)
        ax.set_ylabel("プラス材料の確率（Jev）", color=TEXT)
        ax.grid(color=GRID, linewidth=0.6, axis="x")
        handles, labels = ax.get_legend_handles_labels()
        if daily is not None and not daily.empty:
            start = per_day.index.min() - pd.Timedelta(days=3)
            price = daily[daily.index >= start]["Close"]
            twin = ax.twinx()
            twin.plot(price.index, price.values, color=MA_COLORS["MA75"], linewidth=1.4, alpha=0.9, label="株価（終値）")
            twin.set_ylabel("株価（円）", color=TEXT)
            h2, l2 = twin.get_legend_handles_labels()
            handles, labels = handles + h2, labels + l2
        ax.legend(handles, labels, loc="upper left", frameon=False, fontsize=9)
        ax.set_title(f"{name} ({code}) ニュースの感情スコアの推移", fontsize=14, color=TEXT, loc="left")
        fig.autofmt_xdate()
    return _to_png(fig)
