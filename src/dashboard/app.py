"""
Kalshi Trading System Dashboard - Terminal Edition v3.0

Clean 5-tab layout: Dashboard, Positions, Trade History, Model Health, Config
Dark-mode terminal aesthetic with green/amber accents.
"""
import os
import sys

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Dict, Any

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from sqlalchemy import func

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.data.database import get_db_session
from src.data.models import (
    MarketDB, TradeDB, PositionDB, PortfolioSnapshotDB,
    ForecastDB, PriceDB,
)
from src.utils.logging import logger


# =============================================================================
# TERMINAL THEME CONFIGURATION
# =============================================================================

COLORS = {
    "bg_primary": "#0a0a0f",
    "bg_secondary": "#12121a",
    "bg_card": "#1a1a24",
    "bg_hover": "#252532",
    "border": "#2a2a3a",
    "text_primary": "#e0e0e0",
    "text_secondary": "#888899",
    "text_muted": "#555566",
    "accent_green": "#00ff88",
    "accent_cyan": "#00d4ff",
    "accent_amber": "#ffaa00",
    "accent_red": "#ff4455",
    "accent_purple": "#aa55ff",
    "grid": "#1a1a2e",
}


# =============================================================================
# PAGE CONFIGURATION & GLOBAL STYLES
# =============================================================================

st.set_page_config(
    page_title="KALSHI TERMINAL",
    page_icon="⌨️",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(f"""
<style>
    @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;600;700&family=Fira+Code:wght@300;400;500;600;700&display=swap');

    .stApp {{
        background: linear-gradient(180deg, {COLORS["bg_primary"]} 0%, {COLORS["bg_secondary"]} 100%);
        font-family: 'JetBrains Mono', 'Fira Code', monospace;
    }}

    .stApp::before {{
        content: "";
        position: fixed;
        top: 0;
        left: 0;
        width: 100%;
        height: 100%;
        background: repeating-linear-gradient(
            0deg,
            rgba(0, 0, 0, 0.03),
            rgba(0, 0, 0, 0.03) 1px,
            transparent 1px,
            transparent 2px
        );
        pointer-events: none;
        z-index: 1000;
    }}

    #MainMenu {{visibility: hidden;}}
    footer {{visibility: hidden;}}
    header [data-testid="stToolbar"] {{visibility: hidden;}}

    /* Keep sidebar toggle visible */
    [data-testid="collapsedControl"] {{
        display: block !important;
        visibility: visible !important;
        color: {COLORS["text_primary"]} !important;
    }}

    /* Top-level tab styling */
    .stTabs [data-baseweb="tab-list"] {{
        background: {COLORS["bg_secondary"]};
        border-bottom: 1px solid {COLORS["border"]};
        padding: 0 8px;
        gap: 0px;
    }}

    .stTabs [data-baseweb="tab"] {{
        font-family: 'JetBrains Mono', 'Fira Code', monospace !important;
        font-size: 13px !important;
        font-weight: 500;
        color: {COLORS["text_secondary"]} !important;
        padding: 12px 24px !important;
        border: none !important;
        background: transparent !important;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }}

    .stTabs [data-baseweb="tab"]:hover {{
        color: {COLORS["accent_cyan"]} !important;
        background: {COLORS["bg_hover"]} !important;
    }}

    .stTabs [aria-selected="true"] {{
        color: {COLORS["accent_green"]} !important;
        border-bottom: 2px solid {COLORS["accent_green"]} !important;
        background: {COLORS["bg_card"]} !important;
    }}

    .stTabs [data-baseweb="tab-highlight"] {{
        background-color: {COLORS["accent_green"]} !important;
    }}

    .stTabs [data-baseweb="tab-border"] {{
        display: none;
    }}

    [data-testid="stSidebar"] {{
        background: linear-gradient(180deg, {COLORS["bg_secondary"]} 0%, {COLORS["bg_primary"]} 100%);
        border-right: 1px solid {COLORS["border"]};
    }}

    [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] {{
        color: {COLORS["text_primary"]};
    }}

    [data-testid="stSidebar"] .stRadio > label {{
        color: {COLORS["text_secondary"]} !important;
        font-family: 'JetBrains Mono', monospace !important;
        font-size: 13px !important;
    }}

    [data-testid="stSidebar"] .stRadio > div {{
        gap: 0px;
    }}

    [data-testid="stSidebar"] .stRadio > div > label {{
        background: transparent;
        padding: 8px 12px;
        border-left: 2px solid transparent;
        transition: all 0.2s ease;
    }}

    [data-testid="stSidebar"] .stRadio > div > label:hover {{
        background: {COLORS["bg_hover"]};
        border-left-color: {COLORS["accent_cyan"]};
    }}

    [data-testid="stSidebar"] .stRadio > div > label[data-checked="true"] {{
        background: {COLORS["bg_card"]};
        border-left-color: {COLORS["accent_green"]};
        color: {COLORS["accent_green"]} !important;
    }}

    [data-testid="stMetric"] {{
        background: {COLORS["bg_card"]};
        border: 1px solid {COLORS["border"]};
        border-radius: 4px;
        padding: 16px;
        box-shadow: 0 0 20px rgba(0, 255, 136, 0.05);
    }}

    [data-testid="stMetric"] label {{
        color: {COLORS["text_secondary"]} !important;
        font-family: 'JetBrains Mono', monospace !important;
        font-size: 11px !important;
        text-transform: uppercase;
        letter-spacing: 1px;
    }}

    [data-testid="stMetric"] [data-testid="stMetricValue"] {{
        color: {COLORS["accent_green"]} !important;
        font-family: 'JetBrains Mono', monospace !important;
        font-size: 24px !important;
        font-weight: 600;
        text-shadow: 0 0 10px rgba(0, 255, 136, 0.3);
    }}

    [data-testid="stMetric"] [data-testid="stMetricDelta"] {{
        font-family: 'JetBrains Mono', monospace !important;
    }}

    h1, h2, h3 {{
        color: {COLORS["text_primary"]} !important;
        font-family: 'JetBrains Mono', monospace !important;
        font-weight: 600 !important;
    }}

    h1 {{
        font-size: 28px !important;
        letter-spacing: 2px;
        text-transform: uppercase;
        border-bottom: 1px solid {COLORS["border"]};
        padding-bottom: 10px;
    }}

    h2 {{
        font-size: 16px !important;
        color: {COLORS["accent_cyan"]} !important;
        letter-spacing: 1px;
    }}

    .stButton > button {{
        background: linear-gradient(135deg, {COLORS["bg_card"]} 0%, {COLORS["bg_hover"]} 100%);
        color: {COLORS["accent_green"]};
        border: 1px solid {COLORS["accent_green"]};
        border-radius: 2px;
        font-family: 'JetBrains Mono', monospace;
        font-size: 12px;
        text-transform: uppercase;
        letter-spacing: 1px;
        padding: 8px 20px;
        transition: all 0.3s ease;
        box-shadow: 0 0 10px rgba(0, 255, 136, 0.1);
    }}

    .stButton > button:hover {{
        background: {COLORS["accent_green"]};
        color: {COLORS["bg_primary"]};
        box-shadow: 0 0 20px rgba(0, 255, 136, 0.4);
    }}

    .stButton > button[kind="primary"] {{
        background: {COLORS["accent_green"]};
        color: {COLORS["bg_primary"]};
    }}

    .stButton > button[kind="primary"]:hover {{
        background: {COLORS["accent_cyan"]};
        border-color: {COLORS["accent_cyan"]};
        box-shadow: 0 0 25px rgba(0, 212, 255, 0.5);
    }}

    [data-testid="stDataFrame"] {{
        background: {COLORS["bg_card"]};
        border: 1px solid {COLORS["border"]};
        border-radius: 4px;
    }}

    [data-testid="stDataFrame"] table {{
        font-family: 'JetBrains Mono', monospace !important;
        font-size: 12px;
    }}

    [data-testid="stDataFrame"] th {{
        background: {COLORS["bg_secondary"]} !important;
        color: {COLORS["accent_cyan"]} !important;
        text-transform: uppercase;
        letter-spacing: 1px;
        font-size: 10px;
    }}

    [data-testid="stDataFrame"] td {{
        color: {COLORS["text_primary"]} !important;
        border-color: {COLORS["border"]} !important;
    }}

    .stSlider > div > div {{
        background: {COLORS["bg_card"]};
    }}

    .stSlider > div > div > div {{
        background: {COLORS["accent_green"]} !important;
    }}

    .stSelectbox > div > div {{
        background: {COLORS["bg_card"]};
        border-color: {COLORS["border"]};
        color: {COLORS["text_primary"]};
    }}

    hr {{
        border-color: {COLORS["border"]};
        margin: 20px 0;
    }}

    .status-online {{
        display: inline-block;
        width: 8px;
        height: 8px;
        background: {COLORS["accent_green"]};
        border-radius: 50%;
        margin-right: 8px;
        box-shadow: 0 0 10px {COLORS["accent_green"]};
        animation: pulse 2s infinite;
    }}

    @keyframes pulse {{
        0%, 100% {{ opacity: 1; }}
        50% {{ opacity: 0.5; }}
    }}

    @keyframes blink {{
        0%, 100% {{ opacity: 1; }}
        50% {{ opacity: 0; }}
    }}

    .cursor-blink {{
        animation: blink 1s infinite;
    }}

    .ascii-art {{
        font-family: 'JetBrains Mono', monospace;
        font-size: 10px;
        line-height: 1.2;
        color: {COLORS["accent_green"]};
        white-space: pre;
        text-shadow: 0 0 5px rgba(0, 255, 136, 0.3);
    }}
</style>
""", unsafe_allow_html=True)


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def create_terminal_chart(fig, height: int = 300) -> go.Figure:
    """Apply terminal theme to a plotly figure."""
    fig.update_layout(
        height=height,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="JetBrains Mono, monospace", color=COLORS["text_primary"], size=11),
        xaxis=dict(
            gridcolor=COLORS["grid"],
            linecolor=COLORS["border"],
            tickfont=dict(size=10, color=COLORS["text_secondary"]),
            showgrid=True,
            gridwidth=1,
            zeroline=False,
        ),
        yaxis=dict(
            gridcolor=COLORS["grid"],
            linecolor=COLORS["border"],
            tickfont=dict(size=10, color=COLORS["text_secondary"]),
            showgrid=True,
            gridwidth=1,
            zeroline=False,
        ),
        margin=dict(l=50, r=20, t=30, b=40),
        showlegend=False,
    )
    return fig


def terminal_header(text: str, icon: str = "►") -> None:
    """Display a terminal-style header."""
    _c_cyan = COLORS["accent_cyan"]
    _c_green = COLORS["accent_green"]
    _c_border = COLORS["border"]
    st.markdown(f"""
    <div style="
        color: {_c_cyan};
        font-size: 14px;
        font-weight: 600;
        letter-spacing: 2px;
        text-transform: uppercase;
        margin: 20px 0 15px 0;
        padding-bottom: 8px;
        border-bottom: 1px solid {_c_border};
    ">
        <span style="color: {_c_green}; margin-right: 8px;">{icon}</span>{text}
    </div>
    """, unsafe_allow_html=True)


def _empty_state(message: str) -> None:
    """Display an empty state placeholder."""
    _c_card = COLORS["bg_card"]
    _c_border = COLORS["border"]
    _c_muted = COLORS["text_muted"]
    st.markdown(f"""
    <div style="background: {_c_card}; border: 1px solid {_c_border};
         padding: 60px; text-align: center; border-radius: 4px;">
        <span style="color: {_c_muted}; font-size: 13px;">[ {message} ]</span>
    </div>
    """, unsafe_allow_html=True)


def get_market_titles(session, tickers: List[str]) -> Dict[str, str]:
    """Get titles for multiple tickers at once."""
    if not tickers:
        return {}
    markets = session.query(MarketDB.ticker, MarketDB.title).filter(
        MarketDB.ticker.in_(tickers)
    ).all()
    return {m.ticker: m.title or m.ticker for m in markets}


# =============================================================================
# SIDEBAR
# =============================================================================

def render_sidebar():
    """Render terminal-style sidebar with 5-tab navigation."""
    with st.sidebar:
        st.markdown(f"""
        <div class="ascii-art" style="text-align: center; margin-bottom: 20px;">
╔═══════════════════╗
║   KALSHI TERMINAL ║
║   ▓▓▓▓▓▓▓▓▓▓▓▓   ║
║      v3.0         ║
╚═══════════════════╝
        </div>
        """, unsafe_allow_html=True)

        _c_card = COLORS["bg_card"]
        _c_border = COLORS["border"]
        _c_muted = COLORS["text_muted"]
        _c_green = COLORS["accent_green"]
        st.markdown(f"""
        <div style="
            background: {_c_card};
            border: 1px solid {_c_border};
            border-radius: 4px;
            padding: 12px;
            margin-bottom: 20px;
        ">
            <div style="font-size: 10px; color: {_c_muted}; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 8px;">
                System Status
            </div>
            <div style="display: flex; align-items: center; margin-bottom: 4px;">
                <span class="status-online"></span>
                <span style="color: {_c_green}; font-size: 12px;">ONLINE</span>
            </div>
            <div style="font-size: 10px; color: {_c_muted};">
                {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
            </div>
        </div>
        """, unsafe_allow_html=True)

        # Quick stats panel
        _c_cyan = COLORS["accent_cyan"]
        _c_text = COLORS["text_primary"]
        _c_sec = COLORS["text_secondary"]
        _c_amber = COLORS["accent_amber"]
        st.markdown(f"""
        <div style="
            background: {_c_card};
            border: 1px solid {_c_border};
            border-radius: 4px;
            padding: 12px;
        ">
            <div style="font-size: 10px; color: {_c_cyan}; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 12px;">
                ► Quick Stats
            </div>
        """, unsafe_allow_html=True)

        try:
            with next(get_db_session()) as session:
                position_count = session.query(PositionDB).filter(PositionDB.quantity > 0).count()
                today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
                trades_today = session.query(TradeDB).filter(
                    TradeDB.created_at >= today,
                    TradeDB.status == "filled"
                ).count()
                total_trades = session.query(func.count(TradeDB.id)).scalar() or 0
                resolved_count = session.query(func.count(TradeDB.id)).filter(TradeDB.resolved == True).scalar() or 0
                net_pnl = session.query(func.sum(TradeDB.pnl)).filter(TradeDB.resolved == True).scalar() or 0.0
                net_pnl = float(net_pnl)

                pnl_color = _c_green if net_pnl >= 0 else COLORS["accent_red"]

                st.markdown(f"""
                <div style="display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px dashed {_c_border};">
                    <span style="color: {_c_sec}; font-size: 11px;">Net P&L</span>
                    <span style="color: {pnl_color}; font-size: 12px; font-weight: bold;">${net_pnl:+.2f}</span>
                </div>
                <div style="display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px dashed {_c_border};">
                    <span style="color: {_c_sec}; font-size: 11px;">Positions</span>
                    <span style="color: {_c_green}; font-size: 12px;">{position_count}</span>
                </div>
                <div style="display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px dashed {_c_border};">
                    <span style="color: {_c_sec}; font-size: 11px;">Total Trades</span>
                    <span style="color: {_c_text}; font-size: 12px;">{total_trades}</span>
                </div>
                <div style="display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px dashed {_c_border};">
                    <span style="color: {_c_sec}; font-size: 11px;">Resolved</span>
                    <span style="color: {_c_cyan}; font-size: 12px;">{resolved_count}</span>
                </div>
                <div style="display: flex; justify-content: space-between; padding: 6px 0;">
                    <span style="color: {_c_sec}; font-size: 11px;">Trades Today</span>
                    <span style="color: {_c_amber}; font-size: 12px;">{trades_today}</span>
                </div>
                """, unsafe_allow_html=True)
        except Exception as e:
            logger.warning(f"Dashboard error in sidebar_stats: {e}")
            st.markdown(f"""
            <div style="color: {_c_muted}; font-size: 11px;">Loading...</div>
            """, unsafe_allow_html=True)

        st.markdown("</div>", unsafe_allow_html=True)

        # Mode indicator
        _is_paper = os.getenv("PAPER_TRADING", "true").lower() == "true"
        _mode_text = "Paper Trading Mode" if _is_paper else "LIVE TRADING"
        _mode_icon = "!!" if _is_paper else "**"
        _mode_c = _c_amber if _is_paper else COLORS["accent_red"]
        st.markdown(f"""
        <div style="
            margin-top: 20px;
            padding: 10px;
            background: linear-gradient(135deg, rgba(255, 170, 0, 0.1) 0%, rgba(255, 170, 0, 0.05) 100%);
            border: 1px solid {_mode_c}40;
            border-radius: 4px;
            text-align: center;
        ">
            <span style="color: {_mode_c}; font-size: 10px; text-transform: uppercase; letter-spacing: 1px;">
                {_mode_icon} {_mode_text}
            </span>
        </div>
        """, unsafe_allow_html=True)

        # Auto-refresh
        try:
            from streamlit_autorefresh import st_autorefresh
            st_autorefresh(interval=30000, key="auto_refresh")
        except ImportError:
            pass

        return


# =============================================================================
# TAB 1: DASHBOARD
# =============================================================================

def render_dashboard():
    """Render main dashboard page."""
    # Alert banner
    _render_alert_banner()

    _c_text = COLORS["text_primary"]
    _c_green = COLORS["accent_green"]
    _c_muted = COLORS["text_muted"]
    st.markdown(f"""
    <div style="margin-bottom: 20px;">
        <h1 style="color: {_c_text}; font-size: 24px; font-weight: 600; letter-spacing: 3px; margin: 0;">
            <span style="color: {_c_green};">▶</span> TRADING DASHBOARD
        </h1>
        <div style="color: {_c_muted}; font-size: 11px; margin-top: 5px;">
            Real-time portfolio monitoring · Last updated: {datetime.now().strftime("%H:%M:%S")}
        </div>
    </div>
    """, unsafe_allow_html=True)

    # Hero P&L + Emergency Stop
    _render_hero_pnl()

    # Row 1 — Key Metrics
    col1, col2, col3, col4 = st.columns(4)

    try:
        with next(get_db_session()) as session:
            latest = session.query(PortfolioSnapshotDB).order_by(
                PortfolioSnapshotDB.timestamp.desc()
            ).first()

            yesterday = datetime.now(timezone.utc) - timedelta(days=1)
            prev_snapshot = session.query(PortfolioSnapshotDB).filter(
                PortfolioSnapshotDB.timestamp <= yesterday
            ).order_by(PortfolioSnapshotDB.timestamp.desc()).first()

            equity = float(latest.total_equity) if latest else 1000.0
            prev_equity = float(prev_snapshot.total_equity) if prev_snapshot else equity
            daily_change = equity - prev_equity
            daily_pct = (daily_change / prev_equity * 100) if prev_equity > 0 else 0

            today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            today_pnl = session.query(func.sum(TradeDB.pnl)).filter(
                TradeDB.resolved == True,
                TradeDB.timestamp >= today,
            ).scalar() or 0.0

            resolved_trades = session.query(TradeDB).filter(TradeDB.resolved == True).all()
            wins = sum(1 for t in resolved_trades if t.outcome == "win")
            win_rate = (wins / len(resolved_trades) * 100) if resolved_trades else 0

            position_count = session.query(PositionDB).filter(PositionDB.quantity > 0).count()

            with col1:
                st.metric("TOTAL EQUITY", f"${equity:,.2f}", f"{daily_pct:+.2f}%")
            with col2:
                st.metric("DAILY P&L", f"${float(today_pnl):+,.2f}")
            with col3:
                st.metric("WIN RATE", f"{win_rate:.1f}%", f"{len(resolved_trades)} resolved")
            with col4:
                st.metric("ACTIVE POSITIONS", position_count)
    except Exception as e:
        logger.warning(f"Dashboard error in metrics: {e}")
        with col1:
            st.metric("TOTAL EQUITY", "$1,000.00", "0%")
        with col2:
            st.metric("DAILY P&L", "$0.00")
        with col3:
            st.metric("WIN RATE", "0%")
        with col4:
            st.metric("ACTIVE POSITIONS", 0)

    st.markdown(f"<div style='height: 20px'></div>", unsafe_allow_html=True)

    # Row 2 — Equity Curve (full width)
    terminal_header("EQUITY CURVE", "◆")
    try:
        with next(get_db_session()) as session:
            thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
            snapshots = session.query(PortfolioSnapshotDB).filter(
                PortfolioSnapshotDB.timestamp >= thirty_days_ago
            ).order_by(PortfolioSnapshotDB.timestamp.asc()).all()

            if snapshots:
                df = pd.DataFrame([
                    {"timestamp": s.timestamp, "equity": float(s.total_equity or 0)}
                    for s in snapshots
                ])

                fig = go.Figure()
                fig.add_trace(go.Scatter(
                    x=df["timestamp"],
                    y=df["equity"],
                    fill="tozeroy",
                    fillcolor="rgba(0, 255, 136, 0.1)",
                    line=dict(color=COLORS["accent_green"], width=2),
                    mode="lines",
                ))
                fig = create_terminal_chart(fig, height=300)
                fig.update_layout(yaxis_title="Equity ($)")
                st.plotly_chart(fig, use_container_width=True)
            else:
                _empty_state("No equity data yet")
    except Exception as e:
        logger.warning(f"Dashboard error in equity_chart: {e}")
        _empty_state("Equity chart unavailable")

    st.markdown(f"<div style='height: 20px'></div>", unsafe_allow_html=True)

    # Row 3 — Strategy Leaderboard + P&L by Category
    col_left, col_right = st.columns(2)

    with col_left:
        terminal_header("STRATEGY LEADERBOARD", "►")
        try:
            with next(get_db_session()) as session:
                resolved = session.query(TradeDB).filter(TradeDB.resolved == True).all()

                if resolved:
                    strategy_stats: Dict[str, Dict[str, Any]] = {}
                    for t in resolved:
                        s = t.strategy or "unknown"
                        if s not in strategy_stats:
                            strategy_stats[s] = {"count": 0, "wins": 0, "pnl": 0.0}
                        strategy_stats[s]["count"] += 1
                        strategy_stats[s]["pnl"] += float(t.pnl or 0)
                        if t.outcome == "win":
                            strategy_stats[s]["wins"] += 1

                    rows = []
                    for name, stats in sorted(strategy_stats.items(), key=lambda x: x[1]["pnl"], reverse=True):
                        wr = (stats["wins"] / stats["count"] * 100) if stats["count"] > 0 else 0
                        rows.append({
                            "Strategy": name.upper(),
                            "Trades": stats["count"],
                            "Win Rate": f"{wr:.0f}%",
                            "Net P&L": f"${stats['pnl']:+.2f}",
                        })
                    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
                else:
                    _empty_state("No resolved trades")
        except Exception as e:
            logger.warning(f"Dashboard error in strategy_table: {e}")
            _empty_state("Strategy data unavailable")

    with col_right:
        terminal_header("P&L BY CATEGORY", "►")
        try:
            with next(get_db_session()) as session:
                resolved = session.query(TradeDB).filter(
                    TradeDB.resolved == True, TradeDB.pnl.isnot(None)
                ).all()

                if resolved:
                    ticker_pnl: Dict[str, float] = {}
                    for t in resolved:
                        ticker_pnl.setdefault(t.ticker, 0.0)
                        ticker_pnl[t.ticker] += float(t.pnl or 0)

                    tickers = list(ticker_pnl.keys())
                    markets = session.query(MarketDB.ticker, MarketDB.category).filter(
                        MarketDB.ticker.in_(tickers)
                    ).all()
                    ticker_cat = {m.ticker: (m.category or "unknown") for m in markets}

                    cat_pnl: Dict[str, float] = {}
                    for ticker, pnl in ticker_pnl.items():
                        cat = ticker_cat.get(ticker, "unknown")
                        cat_pnl.setdefault(cat, 0.0)
                        cat_pnl[cat] += pnl

                    if cat_pnl:
                        labels = list(cat_pnl.keys())
                        values = [abs(v) for v in cat_pnl.values()]
                        colors_list = [COLORS["accent_green"] if cat_pnl[l] >= 0 else COLORS["accent_red"] for l in labels]

                        fig = go.Figure(data=[go.Pie(
                            labels=labels,
                            values=values,
                            hole=0.5,
                            marker=dict(colors=colors_list),
                            textinfo="label+percent",
                            textfont=dict(size=10, color=COLORS["text_primary"]),
                        )])
                        fig = create_terminal_chart(fig, height=300)
                        fig.update_layout(showlegend=True, legend=dict(
                            font=dict(size=10, color=COLORS["text_secondary"])
                        ))
                        st.plotly_chart(fig, use_container_width=True)
                    else:
                        _empty_state("No category data")
                else:
                    _empty_state("No resolved trades")
        except Exception as e:
            logger.warning(f"Dashboard error in category_pnl: {e}")
            _empty_state("Category chart unavailable")

    st.markdown(f"<div style='height: 20px'></div>", unsafe_allow_html=True)

    # Row 4 — Recent Trades (last 10)
    terminal_header("RECENT TRADES", "►")
    try:
        with next(get_db_session()) as session:
            trades = session.query(TradeDB).order_by(
                TradeDB.created_at.desc()
            ).limit(10).all()

            if trades:
                tickers = [t.ticker for t in trades if t.ticker]
                titles = get_market_titles(session, tickers)

                rows = []
                for t in trades:
                    title = titles.get(t.ticker, t.ticker or "?")
                    display = (title[:40] + "...") if len(title) > 40 else title
                    pnl_str = f"${float(t.pnl):+.2f}" if t.pnl is not None and t.resolved else "OPEN"
                    outcome = (t.outcome or "pending").upper() if t.resolved else "OPEN"
                    rows.append({
                        "Time": t.created_at.strftime("%m/%d %H:%M") if t.created_at else "?",
                        "Market": display,
                        "Side": (t.side or "?").upper(),
                        "Price": f"{t.price}c" if t.price else "?",
                        "Qty": t.quantity or 0,
                        "Strategy": (t.strategy or "?").upper(),
                        "Outcome": outcome,
                        "P&L": pnl_str,
                    })
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
            else:
                _empty_state("No trades recorded yet")
    except Exception as e:
        logger.warning(f"Dashboard error in recent_trades: {e}")
        _empty_state("Trade data unavailable")

    st.markdown(f"<div style='height: 20px'></div>", unsafe_allow_html=True)

    # Row 5 — System Status (compact)
    terminal_header("SYSTEM STATUS", "►")
    try:
        with next(get_db_session()) as session:
            _is_paper = os.getenv("PAPER_TRADING", "true").lower() == "true"
            mode = "PAPER" if _is_paper else "LIVE"
            db_path = Path("data/kalshi_trading.db")
            db_size = f"{db_path.stat().st_size / (1024*1024):.1f}MB" if db_path.exists() else "N/A"
            latest_price = session.query(func.max(PriceDB.timestamp)).scalar()
            market_count = session.query(func.count(MarketDB.id)).filter(MarketDB.status == "active").scalar() or 0

            llm_status = "Enabled" if os.getenv("ANTHROPIC_API_KEY") else "Disabled"

        c1, c2, c3, c4, c5, c6 = st.columns(6)
        with c1:
            st.caption(f"Mode: **{mode}**")
        with c2:
            st.caption(f"DB: **{db_size}**")
        with c3:
            price_time = latest_price.strftime("%H:%M") if latest_price else "never"
            st.caption(f"Prices: **{price_time}**")
        with c4:
            st.caption(f"LLM: **{llm_status}**")
        with c5:
            st.caption(f"Markets: **{market_count:,}**")
        with c6:
            is_paused = st.session_state.get("trading_paused", False)
            status = "PAUSED" if is_paused else "ACTIVE"
            st.caption(f"Trading: **{status}**")
    except Exception as e:
        logger.warning(f"Dashboard error in system_status: {e}")


def _render_alert_banner():
    """Render alert banner at top of dashboard showing warnings/errors."""
    alerts = []
    try:
        from src.monitoring.health_check import run_health_check, HealthStatus
        health = run_health_check()
        for ind in health.indicators:
            if ind.status == HealthStatus.UNHEALTHY:
                alerts.append(("error", f"{ind.name}: {ind.message}"))
            elif ind.status == HealthStatus.DEGRADED:
                alerts.append(("warning", f"{ind.name}: {ind.message}"))
    except Exception as e:
        logger.warning(f"Dashboard error in alert_banner: {e}")

    if not alerts:
        return

    for alert_type, msg in alerts[:3]:
        if alert_type == "error":
            bg = "rgba(255, 68, 85, 0.15)"
            border = COLORS["accent_red"]
            icon = "!!"
        else:
            bg = "rgba(255, 170, 51, 0.15)"
            border = COLORS["accent_amber"]
            icon = "!?"

        st.markdown(f"""
        <div style="
            background: {bg};
            border-left: 3px solid {border};
            padding: 8px 15px;
            margin-bottom: 5px;
            border-radius: 0 4px 4px 0;
            font-size: 12px;
            color: {COLORS["text_primary"]};
        ">
            <span style="color: {border}; font-weight: bold;">[{icon}]</span> {msg}
        </div>
        """, unsafe_allow_html=True)


def _render_hero_pnl():
    """Render large hero P&L display with emergency stop button."""
    total_pnl = 0.0
    total_fees = 0.0
    total_trades = 0
    resolved = 0
    wins = 0
    win_rate = 0.0

    try:
        from src.utils.fees import KALSHI_WINNER_FEE_RATE
        with next(get_db_session()) as session:
            trades = session.query(TradeDB).all()
            total_trades = len(trades)
            for t in trades:
                if t.resolved == 1:
                    resolved += 1
                    total_pnl += float(t.pnl or 0)
                    if t.outcome == "win":
                        wins += 1
                    winner_fee = float(t.quantity or 0) * KALSHI_WINNER_FEE_RATE if t.outcome == "win" else 0.0
                    total_fees += winner_fee
            win_rate = (wins / resolved * 100) if resolved > 0 else 0
    except Exception as e:
        logger.warning(f"Dashboard error in hero_pnl: {e}")

    pnl_color = COLORS["accent_green"] if total_pnl >= 0 else COLORS["accent_red"]
    pnl_sign = "+" if total_pnl >= 0 else ""
    trend = "^" if total_pnl >= 0 else "v"

    _c_card = COLORS["bg_card"]
    _c_sec = COLORS["bg_secondary"]
    _c_muted = COLORS["text_muted"]
    _c_text_sec = COLORS["text_secondary"]
    _c_cyan = COLORS["accent_cyan"]
    _c_amber = COLORS["accent_amber"]

    hero_col, stop_col = st.columns([4, 1])

    with hero_col:
        st.markdown(f"""
        <div style="
            background: linear-gradient(135deg, {_c_card}, {_c_sec});
            border: 1px solid {pnl_color}40;
            border-radius: 8px;
            padding: 20px 30px;
            margin-bottom: 15px;
        ">
            <div style="color: {_c_muted}; font-size: 10px; text-transform: uppercase; letter-spacing: 2px;">
                NET P&L (AFTER 2% KALSHI FEE)
            </div>
            <div style="color: {pnl_color}; font-size: 36px; font-weight: 700; letter-spacing: 1px; margin: 5px 0;">
                {pnl_sign}${abs(total_pnl):,.2f} {trend}
            </div>
            <div style="display: flex; gap: 30px; margin-top: 8px;">
                <span style="color: {_c_text_sec}; font-size: 12px;">
                    Win Rate: <span style="color: {_c_cyan};">{win_rate:.1f}%</span>
                </span>
                <span style="color: {_c_text_sec}; font-size: 12px;">
                    Trades: <span style="color: {_c_cyan};">{total_trades}</span> ({resolved} resolved)
                </span>
                <span style="color: {_c_text_sec}; font-size: 12px;">
                    Fees Paid: <span style="color: {_c_amber};">${total_fees:,.2f}</span>
                </span>
            </div>
        </div>
        """, unsafe_allow_html=True)

    with stop_col:
        st.markdown(f"<div style='height: 10px'></div>", unsafe_allow_html=True)
        if st.button("STOP ALL TRADING", type="primary", use_container_width=True):
            try:
                from src.execution.risk_manager import get_risk_manager
                rm = get_risk_manager()
                rm.pause_trading("Emergency stop from dashboard")
                st.session_state["trading_paused"] = True
                st.warning("Trading has been STOPPED.")
            except Exception as e:
                st.error(f"Failed to stop: {e}")

        is_paused = st.session_state.get("trading_paused", False)
        if is_paused:
            if st.button("RESUME TRADING", use_container_width=True):
                try:
                    from src.execution.risk_manager import get_risk_manager
                    rm = get_risk_manager()
                    rm.resume_trading()
                    st.session_state["trading_paused"] = False
                    st.success("Trading resumed.")
                except Exception as e:
                    st.error(f"Failed to resume: {e}")

        status_text = "PAUSED" if is_paused else "ACTIVE"
        status_color = COLORS["accent_red"] if is_paused else COLORS["accent_green"]
        st.markdown(f"""
        <div style="text-align: center; margin-top: 10px;">
            <span style="
                color: {status_color};
                font-size: 11px;
                font-weight: bold;
                letter-spacing: 1px;
            ">{status_text}</span>
        </div>
        """, unsafe_allow_html=True)


# =============================================================================
# TAB 2: OPEN POSITIONS
# =============================================================================

def render_positions():
    """Render open positions page."""
    st.markdown(f"""
    <div style="margin-bottom: 20px;">
        <h1 style="color: {COLORS["text_primary"]}; font-size: 24px; font-weight: 600; letter-spacing: 3px; margin: 0;">
            <span style="color: {COLORS["accent_green"]};">◉</span> OPEN POSITIONS
        </h1>
    </div>
    """, unsafe_allow_html=True)

    try:
        with next(get_db_session()) as session:
            positions = session.query(PositionDB).filter(PositionDB.quantity > 0).all()

            if not positions:
                _empty_state("No open positions")
                _render_position_management_stats()
                return

            # Get market titles and latest prices
            tickers = [str(p.ticker) for p in positions]
            titles = get_market_titles(session, tickers)

            latest_prices: Dict[str, int] = {}
            for t in tickers:
                price_row = (
                    session.query(PriceDB)
                    .filter(PriceDB.ticker == t)
                    .order_by(PriceDB.timestamp.desc())
                    .first()
                )
                if price_row:
                    yes_bid = int(price_row.yes_bid) if price_row.yes_bid is not None else None
                    yes_ask = int(price_row.yes_ask) if price_row.yes_ask is not None else None
                    if yes_bid is not None and yes_ask is not None:
                        latest_prices[t] = (yes_bid + yes_ask) // 2
                    elif yes_bid is not None:
                        latest_prices[t] = yes_bid
                    elif yes_ask is not None:
                        latest_prices[t] = yes_ask

            # Build data
            data = []
            total_unrealized = 0.0
            total_exposure = 0.0
            largest_ticker = ""
            largest_value = 0.0

            for p in positions:
                ticker = str(p.ticker)
                title = titles.get(ticker, ticker)
                entry_price = int(p.average_price or 50)
                qty = int(p.quantity or 0)
                side = str(p.side or "unknown")
                current = latest_prices.get(ticker, entry_price)

                if side == "yes":
                    pnl = qty * (current - entry_price) / 100.0
                else:
                    pnl = qty * (entry_price - current) / 100.0
                total_unrealized += pnl

                value = qty * entry_price / 100.0
                total_exposure += value
                if value > largest_value:
                    largest_value = value
                    largest_ticker = ticker

                age_hours = 0.0
                if p.created_at:
                    age_hours = (datetime.now(timezone.utc) - p.created_at).total_seconds() / 3600

                strategy = ""
                trade = session.query(TradeDB).filter(TradeDB.ticker == ticker).order_by(TradeDB.created_at.desc()).first()
                if trade:
                    strategy = trade.strategy or ""

                data.append({
                    "Market": (title[:45] + "...") if len(title) > 45 else title,
                    "Ticker": ticker[:25],
                    "Side": side.upper(),
                    "Qty": qty,
                    "Entry": f"{entry_price}c",
                    "Current": f"{current}c",
                    "Unrealized P&L": f"${pnl:+.2f}",
                    "Strategy": strategy.upper(),
                    "Age": f"{age_hours:.1f}h",
                })

        # Summary metrics
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.metric("OPEN POSITIONS", len(data))
        with col2:
            st.metric("TOTAL EXPOSURE", f"${total_exposure:,.2f}")
        with col3:
            st.metric("UNREALIZED P&L", f"${total_unrealized:+,.2f}")
        with col4:
            st.metric("LARGEST POSITION", f"{largest_ticker[:15]}")

        st.markdown(f"<div style='height: 15px'></div>", unsafe_allow_html=True)

        # Positions table
        terminal_header("POSITION DETAILS", "►")
        st.dataframe(pd.DataFrame(data), use_container_width=True, hide_index=True)

        _c = COLORS["accent_green"] if total_unrealized >= 0 else COLORS["accent_red"]
        st.markdown(f"<div style='text-align:right; color:{_c}; font-size:16px; font-weight:bold; margin-top:5px;'>Unrealized P&L: ${total_unrealized:+.2f}</div>", unsafe_allow_html=True)

        st.markdown(f"<div style='height: 20px'></div>", unsafe_allow_html=True)

        # Exposure breakdown charts
        if data:
            col_left, col_right = st.columns(2)

            with col_left:
                terminal_header("EXPOSURE BY CATEGORY", "►")
                try:
                    with next(get_db_session()) as session:
                        cat_exposure: Dict[str, float] = {}
                        for row in data:
                            ticker = row["Ticker"]
                            market = session.query(MarketDB.category).filter(MarketDB.ticker == ticker).first()
                            cat = market.category if market and market.category else "unknown"
                            value = int(row["Qty"]) * int(row["Entry"].replace("c", "")) / 100.0
                            cat_exposure.setdefault(cat, 0.0)
                            cat_exposure[cat] += value

                        if cat_exposure:
                            fig = go.Figure(data=[go.Bar(
                                x=list(cat_exposure.keys()),
                                y=list(cat_exposure.values()),
                                marker_color=COLORS["accent_cyan"],
                            )])
                            fig = create_terminal_chart(fig, height=250)
                            fig.update_layout(yaxis_title="Exposure ($)")
                            st.plotly_chart(fig, use_container_width=True)
                except Exception as e:
                    logger.warning(f"Dashboard error in category_exposure: {e}")

            with col_right:
                terminal_header("EXPOSURE BY SIDE", "►")
                yes_exposure = sum(
                    int(r["Qty"]) * int(r["Entry"].replace("c", "")) / 100.0
                    for r in data if r["Side"] == "YES"
                )
                no_exposure = sum(
                    int(r["Qty"]) * int(r["Entry"].replace("c", "")) / 100.0
                    for r in data if r["Side"] == "NO"
                )
                other_exposure = total_exposure - yes_exposure - no_exposure

                labels = []
                values = []
                colors = []
                if yes_exposure > 0:
                    labels.append("YES")
                    values.append(yes_exposure)
                    colors.append(COLORS["accent_green"])
                if no_exposure > 0:
                    labels.append("NO")
                    values.append(no_exposure)
                    colors.append(COLORS["accent_red"])
                if other_exposure > 0:
                    labels.append("OTHER")
                    values.append(other_exposure)
                    colors.append(COLORS["accent_amber"])

                if values:
                    fig = go.Figure(data=[go.Pie(
                        labels=labels,
                        values=values,
                        hole=0.5,
                        marker=dict(colors=colors),
                        textinfo="label+percent",
                        textfont=dict(size=11, color=COLORS["text_primary"]),
                    )])
                    fig = create_terminal_chart(fig, height=250)
                    st.plotly_chart(fig, use_container_width=True)

        # Position management stats
        _render_position_management_stats()

    except Exception as e:
        st.error(f"Error loading positions: {e}")


def _render_position_management_stats():
    """Render position management stats and exit history."""
    try:
        from src.execution.position_manager import get_position_manager

        pm = get_position_manager()
        stats = pm.get_stats()
        history = pm.get_exit_history(limit=20)

        terminal_header("POSITION MANAGEMENT", "⚡")

        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.metric("TOTAL EXITS", stats.get("total_exits", 0))
        with col2:
            pnl = stats.get("total_pnl", 0)
            st.metric("EXIT P&L", f"${pnl:+.2f}")
        with col3:
            st.metric("AVG HOLD", f"{stats.get('avg_hold_hours', 0):.1f}h")
        with col4:
            st.metric("TRACKED", stats.get("tracked_positions", 0))

        exits_by_reason = stats.get("exits_by_reason", {})
        if exits_by_reason:
            reason_parts = [f"{reason}: {count}" for reason, count in exits_by_reason.items()]
            _c = COLORS["text_muted"]
            st.markdown(f"<div style='color:{_c}; font-size:11px; margin-top:5px;'>Exit reasons: {' | '.join(reason_parts)}</div>", unsafe_allow_html=True)

        if history:
            terminal_header("RECENT EXITS", "▸")
            exit_data = []
            for h in reversed(history[-10:]):
                _pnl = h.get("net_profit", 0)
                exit_data.append({
                    "Ticker": h.get("ticker", "")[:25],
                    "Reason": h.get("reason", ""),
                    "Qty": h.get("quantity", 0),
                    "Entry": f"{h.get('entry_price', 0)}c",
                    "Exit": f"{h.get('exit_price', 0)}c",
                    "Net P&L": f"${_pnl:+.2f}",
                    "Hold": f"{h.get('hold_hours', 0):.1f}h",
                    "Tier": h.get("tier", ""),
                })
            st.dataframe(pd.DataFrame(exit_data), use_container_width=True, hide_index=True)

    except Exception as e:
        _c = COLORS["text_muted"]
        st.markdown(f"<div style='color:{_c}; font-size:11px;'>Position management: {e}</div>", unsafe_allow_html=True)


# =============================================================================
# TAB 3: TRADE HISTORY
# =============================================================================

def render_trade_history():
    """Render trade history page with filters and charts."""
    st.markdown(f"""
    <div style="margin-bottom: 20px;">
        <h1 style="color: {COLORS["text_primary"]}; font-size: 24px; font-weight: 600; letter-spacing: 3px; margin: 0;">
            <span style="color: {COLORS["accent_cyan"]};">▲</span> TRADE HISTORY
        </h1>
    </div>
    """, unsafe_allow_html=True)

    # Filters row
    filter_col1, filter_col2, filter_col3 = st.columns(3)

    with filter_col1:
        default_start = datetime.now(timezone.utc).date() - timedelta(days=30)
        date_range = st.date_input(
            "Date Range",
            value=(default_start, datetime.now(timezone.utc).date()),
            key="trade_date_range",
        )

    with filter_col2:
        try:
            with next(get_db_session()) as session:
                strategies = session.query(TradeDB.strategy).distinct().all()
                strategy_list = ["All"] + sorted(set(s[0] for s in strategies if s[0]))
        except Exception:
            strategy_list = ["All"]
        strategy_filter = st.selectbox("Strategy", strategy_list)

    with filter_col3:
        outcome_filter = st.selectbox("Outcome", ["All", "Won", "Lost", "Open"])

    st.markdown(f"<div style='height: 15px'></div>", unsafe_allow_html=True)

    try:
        with next(get_db_session()) as session:
            query = session.query(TradeDB)

            # Apply date filter
            if isinstance(date_range, tuple) and len(date_range) == 2:
                start_dt = datetime.combine(date_range[0], datetime.min.time())
                end_dt = datetime.combine(date_range[1], datetime.max.time())
                query = query.filter(TradeDB.created_at >= start_dt, TradeDB.created_at <= end_dt)

            # Apply strategy filter
            if strategy_filter != "All":
                query = query.filter(TradeDB.strategy == strategy_filter)

            # Apply outcome filter
            if outcome_filter == "Won":
                query = query.filter(TradeDB.resolved == True, TradeDB.outcome == "win")
            elif outcome_filter == "Lost":
                query = query.filter(TradeDB.resolved == True, TradeDB.outcome == "loss")
            elif outcome_filter == "Open":
                query = query.filter(TradeDB.resolved == False)

            trades = query.order_by(TradeDB.created_at.desc()).all()

            if not trades:
                _empty_state("No trades match your filters")
                return

            # Summary metrics
            net_pnl = sum(float(t.pnl or 0) for t in trades if t.resolved)
            resolved_count = sum(1 for t in trades if t.resolved)
            best_trade = max((float(t.pnl or 0) for t in trades if t.resolved), default=0)
            worst_trade = min((float(t.pnl or 0) for t in trades if t.resolved), default=0)

            col1, col2, col3, col4 = st.columns(4)
            with col1:
                st.metric("TOTAL TRADES", len(trades))
            with col2:
                st.metric("NET P&L", f"${net_pnl:+.2f}")
            with col3:
                st.metric("BEST TRADE", f"${best_trade:+.2f}")
            with col4:
                st.metric("WORST TRADE", f"${worst_trade:+.2f}")

            st.markdown(f"<div style='height: 15px'></div>", unsafe_allow_html=True)

            # Trade table
            terminal_header("TRADE LOG", "►")
            tickers = [t.ticker for t in trades if t.ticker]
            titles = get_market_titles(session, tickers)

            from src.utils.fees import KALSHI_WINNER_FEE_RATE

            rows = []
            for t in trades:
                title = titles.get(t.ticker, t.ticker or "?")
                display = (title[:35] + "...") if len(title) > 35 else title
                gross_pnl = float(t.pnl or 0) if t.resolved else 0
                fee = float(t.quantity or 0) * KALSHI_WINNER_FEE_RATE if t.resolved and t.outcome == "win" else 0
                net = gross_pnl
                outcome = (t.outcome or "pending").upper() if t.resolved else "OPEN"

                duration = ""
                if t.resolved and t.created_at and t.timestamp:
                    dur_hours = (t.timestamp - t.created_at).total_seconds() / 3600
                    if dur_hours >= 24:
                        duration = f"{dur_hours/24:.1f}d"
                    else:
                        duration = f"{dur_hours:.1f}h"

                rows.append({
                    "Date": t.created_at.strftime("%Y-%m-%d %H:%M") if t.created_at else "?",
                    "Market": display,
                    "Side": (t.side or "?").upper(),
                    "Qty": t.quantity or 0,
                    "Entry": f"{t.price}c" if t.price else "?",
                    "Outcome": outcome,
                    "Gross P&L": f"${gross_pnl:+.2f}" if t.resolved else "-",
                    "Fee": f"${fee:.2f}",
                    "Net P&L": f"${net:+.2f}" if t.resolved else "-",
                    "Strategy": (t.strategy or "?").upper(),
                    "Duration": duration,
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

            st.markdown(f"<div style='height: 20px'></div>", unsafe_allow_html=True)

            # Bottom charts
            resolved_trades = [t for t in trades if t.resolved and t.pnl is not None]

            if resolved_trades:
                chart_left, chart_right = st.columns(2)

                with chart_left:
                    terminal_header("CUMULATIVE P&L", "►")
                    sorted_resolved = sorted(resolved_trades, key=lambda t: t.timestamp or t.created_at or datetime.min)
                    running = 0.0
                    chart_data = []
                    for t in sorted_resolved:
                        running += float(t.pnl or 0)
                        chart_data.append({
                            "date": t.timestamp or t.created_at,
                            "pnl": running,
                        })

                    cpnl_df = pd.DataFrame(chart_data)
                    line_color = COLORS["accent_green"] if running >= 0 else COLORS["accent_red"]
                    fill_color = "rgba(0, 255, 136, 0.1)" if running >= 0 else "rgba(255, 68, 85, 0.1)"

                    fig = go.Figure()
                    fig.add_trace(go.Scatter(
                        x=cpnl_df["date"], y=cpnl_df["pnl"],
                        mode="lines", line=dict(color=line_color, width=2),
                        fill="tozeroy", fillcolor=fill_color,
                    ))
                    fig.add_hline(y=0, line_dash="dash", line_color=COLORS["text_muted"], line_width=1)
                    fig = create_terminal_chart(fig, height=300)
                    fig.update_layout(yaxis_title="Cumulative P&L ($)")
                    st.plotly_chart(fig, use_container_width=True)

                with chart_right:
                    terminal_header("P&L DISTRIBUTION", "►")
                    pnl_values = [float(t.pnl or 0) for t in resolved_trades]

                    fig = go.Figure()
                    fig.add_trace(go.Histogram(
                        x=pnl_values,
                        nbinsx=20,
                        marker_color=COLORS["accent_cyan"],
                        marker_line_color=COLORS["border"],
                        marker_line_width=1,
                    ))
                    fig = create_terminal_chart(fig, height=300)
                    fig.update_layout(
                        xaxis_title="P&L ($)",
                        yaxis_title="Count",
                    )
                    st.plotly_chart(fig, use_container_width=True)

    except Exception as e:
        st.error(f"Error loading trade history: {e}")


# =============================================================================
# TAB 4: MODEL HEALTH
# =============================================================================

def render_model_health():
    """Render model health monitoring page."""
    st.markdown(f"""
    <div style="margin-bottom: 20px;">
        <h1 style="color: {COLORS["text_primary"]}; font-size: 24px; font-weight: 600; letter-spacing: 3px; margin: 0;">
            <span style="color: {COLORS["accent_cyan"]};">🏥</span> MODEL HEALTH MONITOR
        </h1>
        <div style="color: {COLORS["text_muted"]}; font-size: 11px; margin-top: 5px;">
            Calibration monitoring · Drift detection · Auto-pause controls
        </div>
    </div>
    """, unsafe_allow_html=True)

    try:
        from src.analytics.model_health import get_health_report, HealthStatus

        report = get_health_report()

        status_colors = {
            HealthStatus.EXCELLENT: COLORS["accent_green"],
            HealthStatus.GOOD: COLORS["accent_cyan"],
            HealthStatus.WARNING: COLORS["accent_amber"],
            HealthStatus.CRITICAL: COLORS["accent_red"],
        }
        status_color = status_colors.get(report.status, COLORS["text_muted"])

        # Row 1 — Health Status
        col1, col2, col3, col4 = st.columns(4)
        _c_card = COLORS["bg_card"]
        _c_border = COLORS["border"]
        _c_muted = COLORS["text_muted"]

        with col1:
            st.markdown(f"""
            <div style="background: {_c_card}; border: 1px solid {_c_border}; border-radius: 4px; padding: 15px; text-align: center;">
                <div style="font-size: 10px; color: {_c_muted}; text-transform: uppercase; letter-spacing: 1px;">Model Status</div>
                <div style="font-size: 24px; color: {status_color}; font-weight: bold; margin-top: 8px;">{report.status.value.upper()}</div>
            </div>
            """, unsafe_allow_html=True)

        with col2:
            brier_color = COLORS["accent_green"] if report.brier_score and report.brier_score < 0.25 else COLORS["accent_amber"] if report.brier_score and report.brier_score < 0.30 else COLORS["accent_red"]
            brier_display = f"{report.brier_score:.3f}" if report.brier_score else "N/A"
            st.markdown(f"""
            <div style="background: {_c_card}; border: 1px solid {_c_border}; border-radius: 4px; padding: 15px; text-align: center;">
                <div style="font-size: 10px; color: {_c_muted}; text-transform: uppercase; letter-spacing: 1px;">Brier Score</div>
                <div style="font-size: 24px; color: {brier_color}; font-weight: bold; margin-top: 8px;">{brier_display}</div>
            </div>
            """, unsafe_allow_html=True)

        with col3:
            wr_color = COLORS["accent_green"] if report.win_rate and report.win_rate > 0.52 else COLORS["accent_amber"] if report.win_rate and report.win_rate > 0.45 else COLORS["accent_red"]
            wr_display = f"{report.win_rate:.1%}" if report.win_rate else "N/A"
            st.markdown(f"""
            <div style="background: {_c_card}; border: 1px solid {_c_border}; border-radius: 4px; padding: 15px; text-align: center;">
                <div style="font-size: 10px; color: {_c_muted}; text-transform: uppercase; letter-spacing: 1px;">Win Rate</div>
                <div style="font-size: 24px; color: {wr_color}; font-weight: bold; margin-top: 8px;">{wr_display}</div>
            </div>
            """, unsafe_allow_html=True)

        with col4:
            streak_type, streak_count = report.current_streak
            streak_color = COLORS["accent_red"] if streak_type == "loss" and streak_count >= 5 else COLORS["accent_green"] if streak_type == "win" else COLORS["text_secondary"]
            streak_display = f"{streak_type.upper()} x{streak_count}"
            st.markdown(f"""
            <div style="background: {_c_card}; border: 1px solid {_c_border}; border-radius: 4px; padding: 15px; text-align: center;">
                <div style="font-size: 10px; color: {_c_muted}; text-transform: uppercase; letter-spacing: 1px;">Current Streak</div>
                <div style="font-size: 24px; color: {streak_color}; font-weight: bold; margin-top: 8px;">{streak_display}</div>
            </div>
            """, unsafe_allow_html=True)

        st.markdown(f"<div style='height: 20px'></div>", unsafe_allow_html=True)

        # Row 2 — LLM Forecaster Stats
        terminal_header("LLM FORECASTER", "►")
        try:
            with next(get_db_session()) as session:
                today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
                llm_forecasts = session.query(ForecastDB).filter(
                    ForecastDB.method == "llm",
                    ForecastDB.timestamp >= today,
                ).all()

                total_forecasts = session.query(func.count(ForecastDB.id)).filter(
                    ForecastDB.timestamp >= today,
                ).scalar() or 0

                if llm_forecasts:
                    avg_conf = sum(float(f.confidence or 0) for f in llm_forecasts) / len(llm_forecasts)
                    fc1, fc2, fc3 = st.columns(3)
                    with fc1:
                        st.metric("LLM FORECASTS TODAY", len(llm_forecasts))
                    with fc2:
                        st.metric("AVG CONFIDENCE", f"{avg_conf:.1%}")
                    with fc3:
                        st.metric("TOTAL FORECASTS TODAY", total_forecasts)

                    # Top edges
                    edges = [(f.ticker, float(f.edge or 0)) for f in llm_forecasts if f.edge]
                    edges.sort(key=lambda x: abs(x[1]), reverse=True)
                    if edges:
                        terminal_header("TOP EDGES FOUND", "▸")
                        edge_rows = [{"Ticker": t[:25], "Edge": f"{e:+.1%}"} for t, e in edges[:5]]
                        st.dataframe(pd.DataFrame(edge_rows), use_container_width=True, hide_index=True)
                else:
                    fc1, fc2 = st.columns(2)
                    with fc1:
                        st.metric("TOTAL FORECASTS TODAY", total_forecasts)
                    with fc2:
                        llm_active = "Yes" if os.getenv("ANTHROPIC_API_KEY") else "No"
                        st.metric("LLM ENABLED", llm_active)
        except Exception as e:
            logger.warning(f"Dashboard error in llm_stats: {e}")

        st.markdown(f"<div style='height: 20px'></div>", unsafe_allow_html=True)

        # Row 3 — Calibration Charts
        cal_left, cal_right = st.columns(2)

        with cal_left:
            terminal_header("FORECAST VS ACTUAL", "►")
            try:
                with next(get_db_session()) as session:
                    forecasts_with_results = session.query(
                        ForecastDB.probability, MarketDB.result
                    ).join(
                        MarketDB, ForecastDB.ticker == MarketDB.ticker
                    ).filter(
                        ForecastDB.probability.isnot(None),
                        MarketDB.result.isnot(None),
                    ).limit(500).all()

                    if forecasts_with_results:
                        probs = [float(f[0]) for f in forecasts_with_results]
                        actuals = [float(f[1]) for f in forecasts_with_results]

                        fig = go.Figure()
                        fig.add_trace(go.Scatter(
                            x=probs, y=actuals,
                            mode="markers",
                            marker=dict(color=COLORS["accent_cyan"], size=5, opacity=0.5),
                        ))
                        fig.add_trace(go.Scatter(
                            x=[0, 1], y=[0, 1],
                            mode="lines",
                            line=dict(color=COLORS["accent_amber"], dash="dash"),
                        ))
                        fig = create_terminal_chart(fig, height=300)
                        fig.update_layout(
                            xaxis_title="Forecast Probability",
                            yaxis_title="Actual Outcome",
                        )
                        st.plotly_chart(fig, use_container_width=True)
                    else:
                        _empty_state("No resolved forecasts yet")
            except Exception as e:
                logger.warning(f"Dashboard error in calibration: {e}")
                _empty_state("Calibration chart unavailable")

        with cal_right:
            terminal_header("ROLLING BRIER SCORE", "►")
            try:
                with next(get_db_session()) as session:
                    forecasts_with_results = session.query(
                        ForecastDB.probability, MarketDB.result, ForecastDB.timestamp
                    ).join(
                        MarketDB, ForecastDB.ticker == MarketDB.ticker
                    ).filter(
                        ForecastDB.probability.isnot(None),
                        MarketDB.result.isnot(None),
                    ).order_by(ForecastDB.timestamp.asc()).limit(500).all()

                    if len(forecasts_with_results) >= 10:
                        window = 50
                        brier_data = []
                        for i in range(window, len(forecasts_with_results)):
                            chunk = forecasts_with_results[i - window:i]
                            brier = sum((float(f[0]) - float(f[1])) ** 2 for f in chunk) / window
                            brier_data.append({
                                "date": chunk[-1][2],
                                "brier": brier,
                            })

                        if brier_data:
                            bdf = pd.DataFrame(brier_data)
                            fig = go.Figure()
                            fig.add_trace(go.Scatter(
                                x=bdf["date"], y=bdf["brier"],
                                mode="lines",
                                line=dict(color=COLORS["accent_purple"], width=2),
                            ))
                            fig.add_hline(y=0.25, line_dash="dash", line_color=COLORS["accent_amber"], line_width=1)
                            fig = create_terminal_chart(fig, height=300)
                            fig.update_layout(yaxis_title="Brier Score")
                            st.plotly_chart(fig, use_container_width=True)
                        else:
                            _empty_state("Not enough data for rolling Brier")
                    else:
                        _empty_state("Need 10+ resolved forecasts")
            except Exception as e:
                logger.warning(f"Dashboard error in brier_chart: {e}")
                _empty_state("Brier chart unavailable")

        st.markdown(f"<div style='height: 20px'></div>", unsafe_allow_html=True)

        # Row 4 — Strategy Health Table
        terminal_header("STRATEGY HEALTH", "►")
        col1, col2 = st.columns(2)

        with col1:
            stats_data = {
                "Total Trades": report.total_trades,
                "Winning Trades": report.winning_trades,
                "Losing Trades": report.losing_trades,
                "Position Size Multiplier": f"{report.position_size_multiplier:.2f}x",
                "Statistically Significant": "Yes" if report.is_statistically_significant else "No",
            }
            for label, value in stats_data.items():
                st.markdown(f"""
                <div style="display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px dashed {_c_border};">
                    <span style="color: {COLORS["text_secondary"]}; font-size: 12px;">{label}</span>
                    <span style="color: {COLORS["text_primary"]}; font-size: 12px;">{value}</span>
                </div>
                """, unsafe_allow_html=True)

        with col2:
            terminal_header("ALERTS", "⚠")
            if report.alerts:
                for alert in report.alerts:
                    alert_color = COLORS["accent_red"] if "CRITICAL" in alert else COLORS["accent_amber"] if "WARNING" in alert else COLORS["text_secondary"]
                    st.markdown(f"""
                    <div style="padding: 8px; margin-bottom: 8px; background: {COLORS["bg_secondary"]}; border-left: 3px solid {alert_color}; font-size: 11px; color: {COLORS["text_primary"]};">
                        {alert}
                    </div>
                    """, unsafe_allow_html=True)
            else:
                st.markdown(f"""
                <div style="color: {COLORS["accent_green"]}; font-size: 12px; padding: 10px;">
                    No active alerts - model is healthy
                </div>
                """, unsafe_allow_html=True)

        st.markdown(f"<div style='height: 20px'></div>", unsafe_allow_html=True)

        # Row 5 — Risk Manager Status
        terminal_header("RISK MANAGER STATUS", "►")
        try:
            from src.execution.risk_manager import get_risk_manager
            rm = get_risk_manager()

            r1, r2, r3, r4 = st.columns(4)

            with r1:
                daily_loss = getattr(rm, "daily_loss", 0)
                daily_limit = getattr(rm, "daily_loss_limit", 50)
                pct = (daily_loss / daily_limit * 100) if daily_limit > 0 else 0
                st.metric("DAILY LOSS", f"${daily_loss:.2f} / ${daily_limit:.2f}")
                st.progress(min(pct / 100, 1.0))

            with r2:
                daily_trades = getattr(rm, "daily_trade_count", 0)
                max_trades = getattr(rm, "max_daily_trades", 500)
                st.metric("DAILY TRADES", f"{daily_trades} / {max_trades}")
                st.progress(min(daily_trades / max(max_trades, 1), 1.0))

            with r3:
                is_paused = getattr(rm, "is_paused", False)
                pause_reason = getattr(rm, "pause_reason", "")
                status = "PAUSED" if is_paused else "ACTIVE"
                status_color = COLORS["accent_red"] if is_paused else COLORS["accent_green"]
                st.markdown(f"""
                <div style="background: {_c_card}; border: 1px solid {status_color}; border-radius: 4px; padding: 15px; text-align: center;">
                    <div style="font-size: 18px; color: {status_color}; font-weight: bold;">{status}</div>
                    <div style="font-size: 10px; color: {_c_muted}; margin-top: 5px;">Trading Status</div>
                </div>
                """, unsafe_allow_html=True)
                if is_paused and pause_reason:
                    st.caption(f"Reason: {pause_reason}")

            with r4:
                if is_paused:
                    if st.button("RESUME", use_container_width=True):
                        rm.resume_trading()
                        st.success("Trading resumed")
                else:
                    if st.button("PAUSE", use_container_width=True):
                        rm.pause_trading("Manual pause from dashboard")
                        st.warning("Trading paused")

        except Exception as e:
            logger.warning(f"Dashboard error in risk_status: {e}")
            st.caption("Risk manager status unavailable")

    except Exception as e:
        st.error(f"Error loading model health data: {e}")
        st.info("Make sure the model health monitor is configured correctly.")


# =============================================================================
# TAB 5: CONFIG
# =============================================================================

def render_config():
    """Render configuration page (read-only display)."""
    st.markdown(f"""
    <div style="margin-bottom: 20px;">
        <h1 style="color: {COLORS["text_primary"]}; font-size: 24px; font-weight: 600; letter-spacing: 3px; margin: 0;">
            <span style="color: {COLORS["accent_amber"]};">⚙</span> SYSTEM CONFIGURATION
        </h1>
    </div>
    """, unsafe_allow_html=True)

    # Section 1 — Trading Mode Banner
    _is_paper = os.getenv("PAPER_TRADING", "true").lower() == "true"
    if _is_paper:
        _banner_bg = "rgba(255, 170, 0, 0.15)"
        _banner_border = COLORS["accent_amber"]
        _banner_text = "PAPER TRADING MODE"
    else:
        _banner_bg = "rgba(255, 68, 85, 0.15)"
        _banner_border = COLORS["accent_red"]
        _banner_text = "LIVE TRADING MODE"

    st.markdown(f"""
    <div style="
        background: {_banner_bg};
        border: 2px solid {_banner_border};
        border-radius: 8px;
        padding: 20px;
        text-align: center;
        margin-bottom: 20px;
    ">
        <div style="color: {_banner_border}; font-size: 24px; font-weight: bold; letter-spacing: 3px;">
            {_banner_text}
        </div>
    </div>
    """, unsafe_allow_html=True)

    # Section 2 — Current Settings
    terminal_header("CURRENT SETTINGS", "►")

    try:
        from config.settings import Settings
        settings = Settings()

        col1, col2 = st.columns(2)

        with col1:
            settings_left = {
                "Environment": settings.kalshi_environment,
                "Paper Trading": str(settings.paper_trading),
                "Max Position %": f"{settings.max_position_pct:.0%}",
                "Max Daily Loss %": f"{settings.max_daily_loss_pct:.0%}",
                "Max Exposure %": f"{settings.max_total_exposure_pct:.0%}",
                "Kelly Fraction": f"{settings.kelly_fraction:.2f}",
            }
            for label, value in settings_left.items():
                _c_sec = COLORS["text_secondary"]
                _c_text = COLORS["text_primary"]
                _c_border = COLORS["border"]
                st.markdown(f"""
                <div style="display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px dashed {_c_border};">
                    <span style="color: {_c_sec}; font-size: 12px;">{label}</span>
                    <span style="color: {_c_text}; font-size: 12px;">{value}</span>
                </div>
                """, unsafe_allow_html=True)

        with col2:
            settings_right = {
                "Min Edge": f"{settings.min_edge_threshold:.0%}",
                "Max Daily Trades": str(settings.max_daily_trades),
                "LLM Forecaster": "Enabled" if settings.use_llm_forecaster else "Disabled",
                "Position Check Interval": f"{settings.position_check_interval}s",
                "Trailing Stop": "On" if settings.trailing_stop_enabled else "Off",
                "Take Profit": "On" if settings.take_profit_enabled else "Off",
            }
            for label, value in settings_right.items():
                _c_sec = COLORS["text_secondary"]
                _c_text = COLORS["text_primary"]
                _c_border = COLORS["border"]
                st.markdown(f"""
                <div style="display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px dashed {_c_border};">
                    <span style="color: {_c_sec}; font-size: 12px;">{label}</span>
                    <span style="color: {_c_text}; font-size: 12px;">{value}</span>
                </div>
                """, unsafe_allow_html=True)

    except Exception as e:
        logger.warning(f"Dashboard error in settings_display: {e}")
        st.caption("Settings unavailable — config/settings.py may need setup")

    st.markdown(f"<div style='height: 20px'></div>", unsafe_allow_html=True)

    # Section 3 — API Status
    terminal_header("API STATUS", "►")
    api_col1, api_col2, api_col3 = st.columns(3)

    _c_green = COLORS["accent_green"]
    _c_red = COLORS["accent_red"]

    with api_col1:
        has_kalshi = bool(os.getenv("KALSHI_API_KEY"))
        status = "CONFIGURED" if has_kalshi else "NOT SET"
        color = _c_green if has_kalshi else _c_red
        st.markdown(f"""
        <div style="background: {COLORS["bg_card"]}; border: 1px solid {COLORS["border"]}; border-radius: 4px; padding: 15px; text-align: center;">
            <div style="font-size: 10px; color: {COLORS["text_muted"]}; text-transform: uppercase;">Kalshi API</div>
            <div style="font-size: 16px; color: {color}; font-weight: bold; margin-top: 8px;">{status}</div>
        </div>
        """, unsafe_allow_html=True)

    with api_col2:
        has_anthropic = bool(os.getenv("ANTHROPIC_API_KEY"))
        status = "CONFIGURED" if has_anthropic else "NOT SET"
        color = _c_green if has_anthropic else _c_red
        st.markdown(f"""
        <div style="background: {COLORS["bg_card"]}; border: 1px solid {COLORS["border"]}; border-radius: 4px; padding: 15px; text-align: center;">
            <div style="font-size: 10px; color: {COLORS["text_muted"]}; text-transform: uppercase;">Anthropic API</div>
            <div style="font-size: 16px; color: {color}; font-weight: bold; margin-top: 8px;">{status}</div>
        </div>
        """, unsafe_allow_html=True)

    with api_col3:
        has_odds = bool(os.getenv("ODDS_API_KEY"))
        has_finnhub = bool(os.getenv("FINNHUB_API_KEY"))
        count = sum([has_odds, has_finnhub])
        status = f"{count}/2 SET"
        color = _c_green if count == 2 else COLORS["accent_amber"] if count > 0 else _c_red
        st.markdown(f"""
        <div style="background: {COLORS["bg_card"]}; border: 1px solid {COLORS["border"]}; border-radius: 4px; padding: 15px; text-align: center;">
            <div style="font-size: 10px; color: {COLORS["text_muted"]}; text-transform: uppercase;">Data APIs</div>
            <div style="font-size: 16px; color: {color}; font-weight: bold; margin-top: 8px;">{status}</div>
        </div>
        """, unsafe_allow_html=True)

    st.markdown(f"<div style='height: 20px'></div>", unsafe_allow_html=True)

    # Section 4 — Database Info
    terminal_header("DATABASE INFO", "►")
    try:
        db_path = Path("data/kalshi_trading.db")
        db_size = f"{db_path.stat().st_size / (1024*1024):.1f} MB" if db_path.exists() else "N/A"

        with next(get_db_session()) as session:
            market_count = session.query(func.count(MarketDB.id)).scalar() or 0
            trade_count = session.query(func.count(TradeDB.id)).scalar() or 0
            price_count = session.query(func.count(PriceDB.id)).scalar() or 0
            forecast_count = session.query(func.count(ForecastDB.id)).scalar() or 0
            position_count = session.query(func.count(PositionDB.id)).scalar() or 0

            latest_price = session.query(func.max(PriceDB.timestamp)).scalar()
            latest_trade = session.query(func.max(TradeDB.created_at)).scalar()
            latest_forecast = session.query(func.max(ForecastDB.timestamp)).scalar()

        db_col1, db_col2 = st.columns(2)

        with db_col1:
            db_stats = {
                "DB Path": str(db_path),
                "DB Size": db_size,
                "Markets": f"{market_count:,}",
                "Trades": f"{trade_count:,}",
                "Prices": f"{price_count:,}",
            }
            for label, value in db_stats.items():
                _c_sec = COLORS["text_secondary"]
                _c_text = COLORS["text_primary"]
                _c_border = COLORS["border"]
                st.markdown(f"""
                <div style="display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px dashed {_c_border};">
                    <span style="color: {_c_sec}; font-size: 11px;">{label}</span>
                    <span style="color: {_c_text}; font-size: 11px;">{value}</span>
                </div>
                """, unsafe_allow_html=True)

        with db_col2:
            db_stats2 = {
                "Forecasts": f"{forecast_count:,}",
                "Positions": f"{position_count:,}",
                "Latest Price": latest_price.strftime("%Y-%m-%d %H:%M") if latest_price else "never",
                "Latest Trade": latest_trade.strftime("%Y-%m-%d %H:%M") if latest_trade else "never",
                "Latest Forecast": latest_forecast.strftime("%Y-%m-%d %H:%M") if latest_forecast else "never",
            }
            for label, value in db_stats2.items():
                _c_sec = COLORS["text_secondary"]
                _c_text = COLORS["text_primary"]
                _c_border = COLORS["border"]
                st.markdown(f"""
                <div style="display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px dashed {_c_border};">
                    <span style="color: {_c_sec}; font-size: 11px;">{label}</span>
                    <span style="color: {_c_text}; font-size: 11px;">{value}</span>
                </div>
                """, unsafe_allow_html=True)

    except Exception as e:
        logger.warning(f"Dashboard error in db_info: {e}")
        st.caption("Database info unavailable")

    st.markdown(f"<div style='height: 20px'></div>", unsafe_allow_html=True)

    # Section 5 — Cycle Timing
    terminal_header("CYCLE TIMING (FROM MAIN.PY)", "►")
    timing_data = {
        "Market Scan": "120s (2 min)",
        "Price Updates": "60s (1 min)",
        "Impossible Scanner": "120s (2 min)",
        "Straddle Scanner": "120s (2 min)",
        "Mid-Range Strategy": "600s (10 min)",
        "Longshot Hunter": "600s (10 min)",
        "Weather Strategy": "600s (10 min)",
        "Sports Odds": "1800s (30 min)",
        "LLM Forecasts": "900s (15 min)",
        "Position Checks": "30s",
        "Portfolio Snapshots": "1800s (30 min)",
    }
    for label, value in timing_data.items():
        _c_sec = COLORS["text_secondary"]
        _c_text = COLORS["text_primary"]
        _c_border = COLORS["border"]
        st.markdown(f"""
        <div style="display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px dashed {_c_border};">
            <span style="color: {_c_sec}; font-size: 11px;">{label}</span>
            <span style="color: {_c_text}; font-size: 11px;">{value}</span>
        </div>
        """, unsafe_allow_html=True)


# =============================================================================
# MAIN APPLICATION
# =============================================================================

def main():
    """Main application entry point."""
    # Render sidebar for quick stats / mode indicator (navigation moved to tabs)
    render_sidebar()

    # Top-level tab navigation — always visible in main content area
    tab_dashboard, tab_positions, tab_history, tab_health, tab_config = st.tabs([
        "Dashboard",
        "Open Positions",
        "Trade History",
        "Model Health",
        "Config",
    ])

    with tab_dashboard:
        render_dashboard()
    with tab_positions:
        render_positions()
    with tab_history:
        render_trade_history()
    with tab_health:
        render_model_health()
    with tab_config:
        render_config()


if __name__ == "__main__":
    main()
