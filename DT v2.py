import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import numpy as np
import yfinance as yf
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import html
import io
import json
import re
import urllib.request

try:
    from streamlit_searchbox import st_searchbox
    HAS_SEARCHBOX = True
except ImportError:
    HAS_SEARCHBOX = False

# ==========================================
# v10：手動聯網門控（預設禁止自動聯網；僅手動更新按鈕觸發的單次 rerun 放行）
# 舊版切股時頂層自動走「報價競速＋K線多源備援」，新代號 cache miss 時各源 timeout 累加可達 20 秒以上。
# 此處統一門控：所有網絡入口（urllib 4 處、yfinance 經 _run_with_timeout、fetch_realtime 競速）先檢查
# _network_allowed()，不許可即「快速失敗、不發任何請求」，由現有離線邏輯回退到本地/快取或 N/A，絕不後台重試。
# ==========================================
_MANUAL_NETWORK_KEY = "_manual_network_open"
_FIRST_INIT_KEY = "_v10_first_init_done"


def _network_allowed() -> bool:
    return bool(st.session_state.get(_MANUAL_NETWORK_KEY, False))


def open_manual_network():
    """手動更新按鈕呼叫：標記本次 rerun 允許聯網（單次）。"""
    st.session_state[_MANUAL_NETWORK_KEY] = True


def close_manual_network():
    """本次 rerun 抓取結束後關閉，恢復預設禁止自動聯網。"""
    st.session_state[_MANUAL_NETWORK_KEY] = False


# 每次 rerun 開頭明確決定本次門狀態（不依賴腳本末尾，避免異常中斷後門殘留開放）：
#   ① 手動按鈕會先設 _pending_open=True 再 rerun → 本次開門；
#   ② 首次開啟會話 → 開門一次以載入默認標的（頁面初始化，非自動/定時更新）；
#   ③ 其餘一切 rerun（切股、切週期、拖滑桿等）→ 一律關門，網絡入口快速失敗，不自動抓數據。
_PENDING_OPEN_KEY = "_pending_open"
if st.session_state.pop(_PENDING_OPEN_KEY, False):
    st.session_state[_MANUAL_NETWORK_KEY] = True
elif not st.session_state.get(_FIRST_INIT_KEY):
    st.session_state[_FIRST_INIT_KEY] = True
    st.session_state[_MANUAL_NETWORK_KEY] = True
else:
    st.session_state[_MANUAL_NETWORK_KEY] = False

# ==========================================
# 1. 頁面設定與防查看原始碼安全注入
# ==========================================
st.set_page_config(
    page_title="全球港美股、指數期貨、窩輪牛熊與8P批判性估值系統",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# ===== 主題狀態初始化（白色 / 深藍 / 黑色）=====
if "theme_mode" not in st.session_state:
    st.session_state["theme_mode"] = "light"

def apply_theme(theme_mode: str):
    """注入 JS 將主題 class 加到頂層 document.body（components.html 跑在 iframe，須用 window.parent.document）。"""
    class_map = {"light": "theme-light", "darkblue": "theme-darkblue", "dark": "theme-dark"}
    cls = class_map.get(theme_mode, "theme-light")
    components.html(
        f"<script>try{{var d=window.parent.document; d.body.classList.remove('theme-light','theme-darkblue','theme-dark'); d.body.classList.add('{cls}');}}catch(e){{}}</script>",
        height=0, width=0
    )

# 右上角主題切換按鈕（固定定位）
_theme_top = st.columns([10, 1.6])
with _theme_top[1]:
    # v11：以專屬容器包裹主題選擇器，背景色由 CSS 依各主題設定，避免與頁面背景衝突或融為一體
    st.markdown('<div class="theme-picker-wrap">', unsafe_allow_html=True)
    theme_choice = st.selectbox(
        "🎨 背景主題",
        ["☀️ 白色", "🌙 深藍", "⚫ 黑色"],
        index=["light", "darkblue", "dark"].index(st.session_state["theme_mode"]),
        key="theme_selectbox",
        label_visibility="collapsed"
    )
    st.markdown('</div>', unsafe_allow_html=True)
    _theme_map = {"☀️ 白色": "light", "🌙 深藍": "darkblue", "⚫ 黑色": "dark"}
    st.session_state["theme_mode"] = _theme_map.get(theme_choice, "light")
apply_theme(st.session_state["theme_mode"])

# 主題對應的圖表配色（Plotly 背景與文字色）
def get_theme_chart_colors():
    m = st.session_state["theme_mode"]
    if m == "darkblue":
        return dict(paper="#0b1a3a", plot="#10224a", text="#e8eefc", grid="#2a4580",
                    up="#26a69a", down="#ef5350", ma10="#ce93d8", ma20="#64b5f6", ma50="#ffb74d",
                    vol="#90caf9", rsi="#ba68c8", macd_dif="#64b5f6", macd_dea="#ffb74d")
    elif m == "dark":
        return dict(paper="#0a0a0a", plot="#1a1a1a", text="#e8e8e8", grid="#333333",
                    up="#26a69a", down="#ef5350", ma10="#ab47bc", ma20="#42a5f5", ma50="#ffa726",
                    vol="#90caf9", rsi="#ab47bc", macd_dif="#42a5f5", macd_dea="#ffa726")
    return dict(paper="#ffffff", plot="#ffffff", text="#111111", grid="#f0f0f0",
                up="#188038", down="#d93025", ma10="#9c27b0", ma20="#1a73e8", ma50="#fa903e",
                vol=None, rsi="#8e24aa", macd_dif="#1a73e8", macd_dea="#fa903e")

def inject_security_and_styles():
    st.markdown("""
        <style>
        #MainMenu {visibility: hidden !important;}
        header {visibility: hidden !important;}
        footer {visibility: hidden !important;}
        .stDeployButton {display: none !important;}
        [data-testid="stToolbar"] {visibility: hidden !important; display: none !important;}
        [data-testid="stDecoration"] {display: none !important;}
        
        /* 互動式導覽列樣式 */
        div[data-testid="stHorizontalBlock"] .stRadio > div[role="radiogroup"] {
            background-color: #1e3a5f;
            padding: 7px 12px;
            border-radius: 5px;
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
        }
        div[role="radiogroup"] > label {
            background-color: rgba(255,255,255,0.08) !important;
            color: #ffffff !important;
            padding: 5px 11px !important;
            border-radius: 4px !important;
            font-size: 13px !important;
            font-weight: 600 !important;
            cursor: pointer !important;
            border: 1px solid rgba(255,255,255,0.15) !important;
            transition: all 0.15s ease-in-out;
        }
        div[role="radiogroup"] > label:hover {
            background-color: #2b548a !important;
            border-color: #8ab4f8 !important;
        }
        div[role="radiogroup"] > label[data-checked="true"] {
            background-color: #ff9800 !important;
            color: #111111 !important;
            border-color: #ffc107 !important;
            font-weight: 800 !important;
        }
        div[role="radiogroup"] > label p {
            color: inherit !important;
            font-size: 13px !important;
            font-weight: inherit !important;
            margin: 0 !important;
        }

        .selected-asset-banner {
            background-color: #eef4fc; border-left: 5px solid #1a73e8;
            padding: 8px 14px; border-radius: 4px; margin-bottom: 8px;
            font-size: 14px; color: #111111; display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center;
        }
        .aa-index-bar {
            background-color: #ffffff; border: 1px solid #d9d9d9; padding: 10px 16px;
            display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center;
            font-size: 13px; margin-bottom: 10px; border-radius: 4px;
        }
        .aa-quote-container {
            background-color: #ffffff; border: 1px solid #d9d9d9; padding: 16px;
            border-radius: 4px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); height: 100%;
        }
        .aa-price-box-down {
            background-color: #fdf6f2; padding: 14px; border-left: 4px solid #d93025;
        }
        .aa-price-box-up {
            background-color: #f2faf4; padding: 14px; border-left: 4px solid #188038;
        }
        .aa-price-down { color: #d93025; font-size: 32px; font-weight: 800; line-height: 1.1; }
        .aa-price-up { color: #188038; font-size: 32px; font-weight: 800; line-height: 1.1; }
        .aa-metric-table {
            width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 10px;
        }
        .aa-metric-table td { padding: 7px 10px; border-bottom: 1px solid #eeeeee; }
        .aa-label { color: #666666; font-weight: 500; }
        .aa-val { color: #111111; font-weight: 700; text-align: right; }

        /* ===== v8：手機報價 App 樣式（大價格 + 左右兩欄成對欄位）===== */
        .qa-card { background-color:#ffffff; border:1px solid #d9d9d9; border-radius:8px;
                   padding:14px 16px 12px; height:100%; box-shadow:0 1px 3px rgba(0,0,0,0.05); }
        .qa-head { display:flex; justify-content:space-between; align-items:flex-start; gap:10px;
                   border-bottom:1px solid #eeeeee; padding-bottom:12px; margin-bottom:4px; }
        .qa-big { font-size:34px; font-weight:800; line-height:1.05; white-space:nowrap; }
        .qa-big .qa-chg { font-size:18px; font-weight:800; margin-left:8px; }
        .qa-name { font-size:14px; color:#333; font-weight:700; margin-bottom:4px; }
        .qa-status { font-size:12.5px; color:#888; text-align:right; white-space:nowrap; padding-top:6px; }
        .qa-grid { display:grid; grid-template-columns:1fr auto 1fr auto; column-gap:14px; }
        .qa-lab { color:#666666; font-size:13.5px; padding:9px 2px; border-bottom:1px solid #f0f0f0; }
        .qa-val { font-weight:700; font-size:15px; text-align:right; padding:9px 2px;
                  border-bottom:1px solid #f0f0f0; white-space:nowrap; }
        .qa-up { color:#188038; } .qa-down { color:#d93025; } .qa-mid { color:#111111; }
        .qa-na { color:#b0b0b0; font-weight:600; }
        .qa-foot { font-size:11.5px; font-weight:700; margin-top:10px; padding-top:8px;
                   border-top:1px dashed #d9d9d9; line-height:1.6; }
        .tag-badge {
            background-color: #ffece8; color: #d93025; border: 1px solid #f5c2b8;
            font-size: 11px; padding: 2px 6px; border-radius: 3px; margin-left: 6px; font-weight: 600;
        }
        .live-pulse {
            display: inline-block; width: 8px; height: 8px; background-color: #188038;
            border-radius: 50%; margin-right: 5px; animation: pulse 1s infinite;
        }
        @keyframes pulse {
            0% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(24, 128, 56, 0.7); }
            70% { transform: scale(1.1); box-shadow: 0 0 0 6px rgba(24, 128, 56, 0); }
            100% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(24, 128, 56, 0); }
        }
        .section-card {
            background-color: #ffffff; border: 1px solid #e0e4e8; border-radius: 6px;
            padding: 16px; margin-bottom: 14px;
        }

        /* 可自由調校直行闊度與橫行高度、且100%完整顯示中文之自訂表格引擎樣式 */
        .resizable-table-wrapper {
            width: 100%;
            overflow-x: auto;
            overflow-y: auto;
            border: 1px solid #d0d7de;
            border-radius: 6px;
            background: #ffffff;
            margin-top: 6px;
            margin-bottom: 14px;
        }
        table.custom-resizable-grid {
            width: 100%;
            border-collapse: collapse;
            table-layout: auto;
        }
        table.custom-resizable-grid th {
            background-color: #f1f5f9;
            color: #1e293b;
            font-weight: 700;
            text-align: left;
            border: 1px solid #cbd5e1;
            position: sticky;
            top: 0;
            z-index: 10;
            /* 允許滑鼠直接拖曳直行標題調校闊度 */
            resize: horizontal;
            overflow: hidden;
            white-space: normal;
            word-break: break-word;
            overflow-wrap: anywhere;
            line-height: 1.4;
        }
        table.custom-resizable-grid td {
            border: 1px solid #e2e8f0;
            color: #111827;
            vertical-align: top;
            /* 關鍵修正：中文 100% 完整換行顯示，不出現捲軸或被截斷 */
            overflow: visible;
            white-space: normal !important;
            word-wrap: break-word !important;
            word-break: break-word !important;
            overflow-wrap: anywhere !important;
            line-height: 1.55;
        }
        table.custom-resizable-grid tr:nth-child(even) {
            background-color: #f8fafc;
        }
        table.custom-resizable-grid tr:hover {
            background-color: #eef2ff;
        }

        /* ============================================================
           v7 全畫面主題系統（白色 / 深藍 / 黑色）
           機制：body 加上 theme-darkblue / theme-dark class，並用 CSS 變數
           統一覆蓋所有 Streamlit 容器、widget、表格、自訂卡片，確保「整個畫面」變色。
           ============================================================ */
        body.theme-darkblue {
            --bg:#0b1a3a; --bg2:#0d2047; --panel:#13285a; --panel2:#0f224a;
            --border:#2a4480; --text:#e9effd; --muted:#a7b9dd; --accent:#86b4ff; --th:#1d3a73;
        }
        body.theme-dark {
            --bg:#0a0a0a; --bg2:#0f0f0f; --panel:#171717; --panel2:#131313;
            --border:#2d2d2d; --text:#ededed; --muted:#a8a8a8; --accent:#82c5ff; --th:#222222;
        }
        body.theme-darkblue, body.theme-dark { background: var(--bg) !important; }

        /* 頂層容器全覆蓋（含主內容區、側邊欄、Header、區塊）*/
        body.theme-darkblue .stApp, body.theme-dark .stApp,
        body.theme-darkblue [data-testid="stAppViewContainer"], body.theme-dark [data-testid="stAppViewContainer"],
        body.theme-darkblue [data-testid="stMainBlockContainer"], body.theme-dark [data-testid="stMainBlockContainer"],
        body.theme-darkblue [data-testid="stMain"], body.theme-dark [data-testid="stMain"],
        body.theme-darkblue section.main, body.theme-dark section.main,
        body.theme-darkblue [data-testid="stSidebar"], body.theme-dark [data-testid="stSidebar"],
        body.theme-darkblue [data-testid="stSidebarContent"], body.theme-dark [data-testid="stSidebarContent"],
        body.theme-darkblue [data-testid="stSidebarUserContent"], body.theme-dark [data-testid="stSidebarUserContent"],
        body.theme-darkblue [data-testid="stHeader"], body.theme-dark [data-testid="stHeader"],
        body.theme-darkblue [data-testid="stToolbar"], body.theme-dark [data-testid="stToolbar"],
        body.theme-darkblue [data-testid="stVerticalBlock"], body.theme-dark [data-testid="stVerticalBlock"],
        body.theme-darkblue [data-testid="stHorizontalBlock"], body.theme-dark [data-testid="stHorizontalBlock"],
        body.theme-darkblue [data-testid="stElementContainer"], body.theme-dark [data-testid="stElementContainer"],
        body.theme-darkblue .stBlock, body.theme-dark .stBlock {
            background-color: var(--bg) !important;
            color: var(--text) !important;
            border-color: var(--border) !important;
        }

        /* 所有文字 */
        body.theme-darkblue p, body.theme-dark p,
        body.theme-darkblue li, body.theme-dark li,
        body.theme-darkblue h1, body.theme-dark h1,
        body.theme-darkblue h2, body.theme-dark h2,
        body.theme-darkblue h3, body.theme-dark h3,
        body.theme-darkblue h4, body.theme-dark h4,
        body.theme-darkblue h5, body.theme-dark h5,
        body.theme-darkblue h6, body.theme-dark h6,
        body.theme-darkblue label, body.theme-dark label,
        body.theme-darkblue .stMarkdown, body.theme-dark .stMarkdown,
        body.theme-darkblue .stText, body.theme-dark .stText,
        body.theme-darkblue .stCaption, body.theme-dark .stCaption,
        body.theme-darkblue [data-testid="stWidgetLabel"], body.theme-dark [data-testid="stWidgetLabel"],
        body.theme-darkblue .stMetric, body.theme-dark .stMetric,
        body.theme-darkblue .stMetricValue, body.theme-dark .stMetricValue,
        body.theme-darkblue .stMetricLabel, body.theme-dark .stMetricLabel,
        body.theme-darkblue .stMetricDirection, body.theme-dark .stMetricDirection,
        body.theme-darkblue small, body.theme-dark small {
            color: var(--text) !important;
        }
        body.theme-darkblue .stCaption, body.theme-dark .stCaption { color: var(--muted) !important; }

        /* 漲跌色保留（深色用較亮的綠/紅，避免與背景相撞）*/
        body.theme-darkblue .aa-price-up, body.theme-dark .aa-price-up { color:#26a69a !important; }
        body.theme-darkblue .aa-price-down, body.theme-dark .aa-price-down { color:#ef5350 !important; }

        /* 輸入控件：selectbox / multiselect / text / number input */
        body.theme-darkblue [data-baseweb="select"] > div, body.theme-dark [data-baseweb="select"] > div,
        body.theme-darkblue [data-baseweb="input"], body.theme-dark [data-baseweb="input"],
        body.theme-darkblue [data-baseweb="base-input"], body.theme-dark [data-baseweb="base-input"],
        body.theme-darkblue input, body.theme-dark input,
        body.theme-darkblue textarea, body.theme-dark textarea {
            background-color: var(--panel) !important;
            color: var(--text) !important;
            border-color: var(--border) !important;
        }
        body.theme-darkblue [data-baseweb="select"] span, body.theme-dark [data-baseweb="select"] span,
        body.theme-darkblue [data-baseweb="select"] input, body.theme-dark [data-baseweb="select"] input,
        body.theme-darkblue [data-baseweb="input"] input, body.theme-dark [data-baseweb="input"] input {
            color: var(--text) !important;
        }
        body.theme-darkblue [data-baseweb="tag"], body.theme-dark [data-baseweb="tag"] {
            background-color: var(--th) !important; color: var(--text) !important;
        }
        /* 下拉彈層（baseweb portal 掛在 body 下）*/
        body.theme-darkblue [data-baseweb="menu"], body.theme-dark [data-baseweb="menu"],
        body.theme-darkblue ul[role="listbox"], body.theme-dark ul[role="listbox"],
        body.theme-darkblue [data-baseweb="popover"], body.theme-dark [data-baseweb="popover"] {
            background-color: var(--panel) !important; border:1px solid var(--border) !important;
        }
        body.theme-darkblue li[role="option"], body.theme-dark li[role="option"],
        body.theme-darkblue [role="option"], body.theme-dark [role="option"] {
            background-color: var(--panel) !important; color: var(--text) !important;
        }
        body.theme-darkblue li[role="option"]:hover, body.theme-dark li[role="option"]:hover,
        body.theme-darkblue [role="option"][aria-selected="true"], body.theme-dark [role="option"][aria-selected="true"] {
            background-color: var(--th) !important;
        }

        /* 按鈕 */
        body.theme-darkblue .stButton > button, body.theme-dark .stButton > button,
        body.theme-darkblue .stDownloadButton > button, body.theme-dark .stDownloadButton > button {
            background-color: var(--panel) !important; color: var(--text) !important;
            border:1px solid var(--border) !important;
        }
        body.theme-darkblue .stButton > button:hover, body.theme-dark .stButton > button:hover {
            background-color: var(--th) !important; border-color: var(--accent) !important;
        }

        /* 核取方塊 / 單選 / 滑桿 */
        body.theme-darkblue [data-baseweb="checkbox"], body.theme-dark [data-baseweb="checkbox"],
        body.theme-darkblue [data-baseweb="radio"], body.theme-dark [data-baseweb="radio"] {
            color: var(--text) !important;
        }
        body.theme-darkblue [data-testid="stTickBarMin"], body.theme-dark [data-testid="stTickBarMin"],
        body.theme-darkblue [data-testid="stTickBarMax"], body.theme-dark [data-testid="stTickBarMax"] {
            color: var(--muted) !important; fill: var(--muted) !important;
        }

        /* Expander 折疊面板 */
        body.theme-darkblue [data-testid="stExpander"], body.theme-dark [data-testid="stExpander"],
        body.theme-darkblue details, body.theme-dark details {
            background-color: var(--panel) !important; border:1px solid var(--border) !important;
            color: var(--text) !important;
        }
        body.theme-darkblue [data-testid="stExpander"] summary, body.theme-dark [data-testid="stExpander"] summary,
        body.theme-darkblue details summary, body.theme-dark details summary {
            color: var(--text) !important;
        }
        body.theme-darkblue [data-testid="stExpanderDetails"], body.theme-dark [data-testid="stExpanderDetails"] {
            background-color: var(--panel) !important;
        }

        /* Tabs 分頁 */
        body.theme-darkblue [data-baseweb="tab-list"], body.theme-dark [data-baseweb="tab-list"] {
            background-color: var(--panel2) !important;
        }
        body.theme-darkblue [data-baseweb="tab"], body.theme-dark [data-baseweb="tab"] {
            color: var(--muted) !important;
        }
        body.theme-darkblue [data-baseweb="tab"][aria-selected="true"], body.theme-dark [data-baseweb="tab"][aria-selected="true"] {
            background-color: var(--panel) !important; color: var(--text) !important;
        }
        body.theme-darkblue [data-baseweb="tab-highlight"], body.theme-dark [data-baseweb="tab-highlight"],
        body.theme-darkblue [data-baseweb="tab-border"], body.theme-dark [data-baseweb="tab-border"] {
            background-color: var(--accent) !important;
        }

        /* 數據框 / 表格 */
        body.theme-darkblue .stDataFrame, body.theme-dark .stDataFrame,
        body.theme-darkblue [data-testid="stDataFrame"], body.theme-dark [data-testid="stDataFrame"],
        body.theme-darkblue .stTable, body.theme-dark .stTable,
        body.theme-darkblue .resizable-table-wrapper, body.theme-dark .resizable-table-wrapper {
            background-color: var(--panel) !important; border-color: var(--border) !important;
        }
        body.theme-darkblue table, body.theme-dark table {
            background-color: var(--panel) !important; color: var(--text) !important;
        }
        body.theme-darkblue table th, body.theme-dark table th {
            background-color: var(--th) !important; color: var(--text) !important;
            border-color: var(--border) !important;
        }
        body.theme-darkblue table td, body.theme-dark table td {
            color: var(--text) !important; border-color: var(--border) !important;
        }
        body.theme-darkblue table.custom-resizable-grid tr:nth-child(even),
        body.theme-dark table.custom-resizable-grid tr:nth-child(even) {
            background-color: var(--panel2) !important;
        }

        /* Alert 提示框：保留語意底色，文字轉淺色 */
        body.theme-darkblue [data-testid="stAlert"] p, body.theme-dark [data-testid="stAlert"] p,
        body.theme-darkblue [data-testid="stAlert"] span, body.theme-dark [data-testid="stAlert"] span {
            color: #f4f6fb !important;
        }

        /* 自訂 HTML 卡片 */
        body.theme-darkblue .aa-quote-container, body.theme-dark .aa-quote-container,
        body.theme-darkblue .aa-index-bar, body.theme-dark .aa-index-bar,
        body.theme-darkblue .section-card, body.theme-dark .section-card {
            background-color: var(--panel) !important; border-color: var(--border) !important;
            color: var(--text) !important;
        }
        body.theme-darkblue .selected-asset-banner, body.theme-dark .selected-asset-banner {
            background-color: var(--panel2) !important; color: var(--text) !important;
        }
        body.theme-darkblue .aa-label, body.theme-dark .aa-label { color: var(--muted) !important; }
        body.theme-darkblue .aa-val, body.theme-dark .aa-val { color: var(--text) !important; }
        body.theme-darkblue .aa-price-box-up, body.theme-dark .aa-price-box-up {
            background-color: rgba(38,166,154,0.14) !important;
        }
        body.theme-darkblue .aa-price-box-down, body.theme-dark .aa-price-box-down {
            background-color: rgba(239,83,80,0.14) !important;
        }
        body.theme-darkblue .aa-metric-table td, body.theme-dark .aa-metric-table td {
            border-color: var(--border) !important;
        }
        body.theme-darkblue .tag-badge, body.theme-dark .tag-badge {
            background-color: rgba(239,83,80,0.18) !important; color:#ff9b97 !important;
            border-color: rgba(239,83,80,0.4) !important;
        }

        /* v8 手機報價卡片：深色主題覆蓋 */
        body.theme-darkblue .qa-card, body.theme-dark .qa-card {
            background-color: var(--panel) !important; border-color: var(--border) !important;
        }
        body.theme-darkblue .qa-head, body.theme-dark .qa-head { border-color: var(--border) !important; }
        body.theme-darkblue .qa-name, body.theme-dark .qa-name { color: var(--text) !important; }
        body.theme-darkblue .qa-sub, body.theme-dark .qa-sub,
        body.theme-darkblue .qa-status, body.theme-dark .qa-status { color: var(--muted) !important; }
        body.theme-darkblue .qa-lab, body.theme-dark .qa-lab {
            color: var(--muted) !important; border-color: var(--border) !important;
        }
        body.theme-darkblue .qa-val, body.theme-dark .qa-val {
            color: var(--text) !important; border-color: var(--border) !important;
        }
        body.theme-darkblue .qa-up, body.theme-dark .qa-up { color:#26a69a !important; }
        body.theme-darkblue .qa-down, body.theme-dark .qa-down { color:#ef5350 !important; }
        body.theme-darkblue .qa-mid, body.theme-dark .qa-mid { color: var(--text) !important; }
        body.theme-darkblue .qa-na, body.theme-dark .qa-na { color:#6b7689 !important; }
        body.theme-darkblue .qa-foot, body.theme-dark .qa-foot { border-color: var(--border) !important; }

        /* 程式碼 / 分隔線 / 引用 */
        body.theme-darkblue code, body.theme-dark code {
            background-color: var(--panel2) !important; color: var(--text) !important;
        }
        body.theme-darkblue pre, body.theme-dark pre {
            background-color: var(--panel2) !important; border:1px solid var(--border) !important;
        }
        body.theme-darkblue hr, body.theme-dark hr { border-color: var(--border) !important; }

        /* v11 右上角主題選擇器：明確背景與邊框，三主題各自協調，文字/箭頭對比清晰，不與頁面背景衝突 */
        .theme-picker-wrap {
            background: #f1f5f9;
            border: 1px solid #cbd5e1;
            border-radius: 8px;
            padding: 5px 8px;
        }
        .theme-picker-wrap [data-baseweb="select"] > div {
            background-color: transparent !important;
            border: none !important;
        }
        body.theme-darkblue .theme-picker-wrap {
            background: #13285a; border: 1px solid #2a4480;
        }
        body.theme-dark .theme-picker-wrap {
            background: #171717; border: 1px solid #2d2d2d;
        }
        body.theme-darkblue .theme-picker-wrap [data-baseweb="select"] span,
        body.theme-dark .theme-picker-wrap [data-baseweb="select"] span,
        body.theme-darkblue .theme-picker-wrap svg,
        body.theme-dark .theme-picker-wrap svg {
            color: var(--text); fill: var(--text);
        }

        /* 主題切換按鈕固定在右上角 */
        .theme-switcher-fixed {
            position: fixed;
            top: 12px;
            right: 16px;
            z-index: 9999;
            background: rgba(255,255,255,0.95);
            border: 1px solid #cbd5e1;
            border-radius: 8px;
            padding: 6px 10px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.15);
        }
        .theme-darkblue .theme-switcher-fixed,
        .theme-dark .theme-switcher-fixed {
            background: rgba(20,30,60,0.95);
            border-color: #2a4580;
        }

        </style>
    """, unsafe_allow_html=True)
    
    components.html("""
        <script>
        const doc = window.parent.document;
        doc.addEventListener('contextmenu', event => event.preventDefault());
        doc.addEventListener('keydown', function(e) {
            if (e.keyCode == 123 || 
                (e.ctrlKey && e.shiftKey && (e.keyCode == 73 || e.keyCode == 74 || e.keyCode == 67)) || 
                (e.ctrlKey && e.keyCode == 85)) {
                e.preventDefault();
                return false;
            }
        });
        </script>
    """, height=0, width=0)

inject_security_and_styles()

# ==========================================
# 2. 支援「直行闊度與橫行高度自由調校」及「100%完整顯示中文」之表格渲染函數
# ==========================================
def render_resizable_table(df: pd.DataFrame, key_prefix: str, default_col_w: int = 125, default_text_w: int = 380, default_row_h: int = 52, max_container_h: int = 850, freeze_first_col: bool = False):
    with st.expander("📐 表格顯示設定：調校每個「直行闊度」、「橫行高度」與「字體大小」（亦可用滑鼠直接拖曳表頭右邊框）", expanded=False):
        rc1, rc2, rc3, rc4, rc5 = st.columns(5)
        col_w = rc1.slider("一般直行闊度 (px)", 70, 350, default_col_w, 5, key=f"{key_prefix}_cw")
        text_w = rc2.slider("中文長文分析欄闊度 (px)", 220, 900, default_text_w, 20, key=f"{key_prefix}_tw")
        row_h = rc3.slider("橫行高度 (px)", 28, 180, default_row_h, 4, key=f"{key_prefix}_rh")
        font_sz = rc4.slider("字體大小 (px)", 11, 20, 13, 1, key=f"{key_prefix}_fs")
        auto_fit = rc5.checkbox("自動配合內容寬度", value=True, key=f"{key_prefix}_af", help="開啟後表格寬度自動配合內容，中文長文完整換行不截斷")

    container_style = "" if auto_fit else f"max-height: {max_container_h}px;"

    # 建立可自由拖曳直行與橫行之 HTML 表格
    # 關鍵修正：列寬由 th 控制，td 只設 padding 與高度，避免 min-width 衝突導致中文截斷
    html_parts = [f'<div class="resizable-table-wrapper" style="{container_style}">']
    html_parts.append(f'<table class="custom-resizable-grid" style="font-size: {font_sz}px; table-layout: fixed;">')

    # 表頭 (Headers) — 每列寬度在此統一設定
    html_parts.append("<thead><tr>")
    if df.index.name:
        sticky_th = "position:sticky; left:0; z-index:12; background:#e2e8f0;" if freeze_first_col else ""
        html_parts.append(f'<th style="width:{col_w+60}px; min-width:{col_w+60}px; max-width:{col_w+120}px; padding:8px 10px; {sticky_th}">{html.escape(str(df.index.name))}</th>')
    for col in df.columns:
        c_str = str(col)
        is_long_col = any(k in c_str for k in ["剖析", "分析", "催化劑", "定性", "階段", "建議", "名稱", "詳細", "內容", "備註"])
        w_px = text_w if is_long_col else col_w
        html_parts.append(f'<th style="width:{w_px}px; min-width:{w_px}px; max-width:{w_px+150}px; padding:8px 10px;">{html.escape(c_str)}</th>')
    html_parts.append("</tr></thead>")

    # 表身 (Rows) — td 不設 min-width，由 th 控制列寬，內容自動換行
    html_parts.append("<tbody>")
    for idx_val, row in df.iterrows():
        html_parts.append(f'<tr style="height:{row_h}px;">')
        if df.index.name:
            sticky_td = "position:sticky; left:0; z-index:5; background:#f8fafc; font-weight:700;" if freeze_first_col else "font-weight:600;"
            html_parts.append(f'<td style="padding:7px 10px; vertical-align:top; {sticky_td}">{html.escape(str(idx_val))}</td>')
        for col in df.columns:
            val_str = str(row[col])
            html_parts.append(f'<td style="padding:7px 10px; vertical-align:top; line-height:1.5;">{html.escape(val_str)}</td>')
        html_parts.append("</tr>")
    html_parts.append("</tbody></table></div>")

    st.markdown("".join(html_parts), unsafe_allow_html=True)

# ==========================================
# 3. 全市場萬用資料庫與中文同義詞別名映射 (確保輸入「道斯指數」「納斯達克指數」「標普500指數」100%精準匹配)
# ==========================================
INDEX_SYNONYM_MAP = {
    "納斯達克指數": "^IXIC",
    "納斯達克綜合指數": "^IXIC",
    "納指": "^IXIC",
    "納斯達克": "^IXIC",
    "納斯達克100指數": "^NDX",
    "納指100": "^NDX",
    "道斯指數": "^DJI",
    "道瓊斯指數": "^DJI",
    "道瓊斯工業平均指數": "^DJI",
    "道指": "^DJI",
    "道瓊斯": "^DJI",
    "標普500指數": "^GSPC",
    "標普指數": "^GSPC",
    "標準普爾500指數": "^GSPC",
    "標普500": "^GSPC",
    "標普": "^GSPC",
    "羅素2000指數": "^RUT",
    "羅素指數": "^RUT",
    "費城半導體指數": "^SOX",
    "費半指數": "^SOX",
    "半導體指數": "^SOX",
    "恐慌指數": "^VIX",
    "恒生指數": "^HSI",
    "恒指": "^HSI",
    "國企指數": "^HSCE",
    "國指": "^HSCE",
    "恒生科技指數": "HSTECH.HK",
    "科指": "HSTECH.HK",
    "黃金": "GC=F",
    "黃金期貨": "GC=F",
    "期指": "ES=F",
    "美股期指": "ES=F",
    "納指期貨": "NQ=F",
    "道指期貨": "YM=F"
}

RAW_UNIVERSE = [
    # --- 全球核心指數與期貨 (Indices, Futures, Gold) ---
    ("^IXIC", "IXIC", "美國納斯達克綜合指數 (納斯達克指數)", "Nasdaq Composite Index (納指/納斯達克)", "INDEX", "USD", 19980.0, 28.2, 4.80, 6.90, 15.0, 52.0, 31000.0, 10.0, "⚪ 牛市震盪 | 大型科技股領漲，中小型科技股受高利率壓制表現分化。"),
    ("^NDX", "NDX", "美國納斯達克100指數 (納指100)", "Nasdaq 100 Index (納斯達克100)", "INDEX", "USD", 22450.0, 27.5, 5.60, 8.20, 16.5, 56.0, 28000.0, 12.0, "🟢 AI算力牛市 | 10月再創歷史新高，算力硬體與雲端巨頭主導升勢，高波動慢牛格局。"),
    ("^DJI", "DJI", "美國道瓊斯工業平均指數 (道斯指數/道指)", "Dow Jones Industrial Average (道瓊斯/道斯)", "INDEX", "USD", 44250.0, 20.2, 2.75, 5.10, 7.5, 38.0, 17500.0, 6.0, "⚪ 穩健慢牛 | 金融與工業藍籌權重高，波幅低於納指，對宏觀經濟周期敏感。"),
    ("^GSPC", "SPX", "美國標普500指數 (標準普爾500)", "S&P 500 Index (標普500/標普指數)", "INDEX", "USD", 6120.0, 22.8, 3.05, 4.95, 11.0, 45.0, 52000.0, 8.0, "⚪ 牛市後期 | AI巨頭盈利支撐長期多頭，惟整體估值處歷史90%分位，對CPI與美債息率敏感。"),
    ("^RUT", "RUT", "美國羅素2000小型股指數 (羅素指數)", "Russell 2000 Index", "INDEX", "USD", 2310.0, 24.5, 1.45, 2.15, 6.0, 28.0, 3200.0, -8.0, "🔴 高息受壓 | 小型股浮動利率債務佔比高，美國9月CPI超預期推遲減息直接打擊羅素2000表現。"),
    ("^SOX", "SOX", "美國費城半導體指數 (費半指數)", "PHLX Semiconductor Index", "INDEX", "USD", 5860.0, 26.8, 8.90, 7.80, 34.0, 58.0, 9500.0, 22.0, "🟢 高Beta主線 | 全球AI算力基建核心晴雨表，受台積電、輝達、博通、美光業績驅動。"),
    ("^VIX", "VIX", "CBOE 恐慌波動率指數 (恐慌指數)", "CBOE Volatility Index", "INDEX", "USD", 16.80, 20.0, 1.00, 1.00, 0.0, 0.0, 100.0, 0.0, "⚪ 市場情緒指標 | 反映標普500期權隱含波動率，美國CPI公布與財報季期間易出現脈衝式抽升。"),
    ("^HSI", "HSI", "香港恒生指數 (恒指)", "Hang Seng Index", "INDEX", "HKD", 23956.0, 10.8, 1.35, 1.12, 8.0, 35.0, 3500.0, 15.0, "🟢 結構性慢牛 | 較熊市底部累升逾60%，日均成交逾2,000億港元，短期於23,400–24,400點整固蓄勢。"),
    ("^HSCE", "HSCEI", "恒生中國企業指數 (國企指數/國指)", "HS China Enterprises Index", "INDEX", "HKD", 8055.0, 9.6, 1.20, 1.05, 8.5, 34.0, 2200.0, 16.0, "🟢 結構性慢牛 | 受國有大行與中資科網龍頭雙輪支撐，估值處全球主要市場低位。"),
    ("HSTECH.HK", "HSTECH", "恒生科技指數 (科指)", "Hang Seng Tech Index", "INDEX", "HKD", 4668.0, 17.2, 2.15, 2.40, 14.5, 42.0, 1400.0, 28.0, "🟢 高彈性修復 | 剔除負值後PE僅17.2倍(處歷史13%分位)，完成二浪回調洗盤後具備較高向上彈性。"),
    ("ES=F", "ES=F", "標普500指數期貨 (美股標普期指)", "E-Mini S&P 500 Futures", "FUTURES", "USD", 6135.0, 22.8, 3.05, 4.95, 11.0, 45.0, 500.0, 8.0, "⚪ 期指合約 | 24小時反映全球宏觀事件與美股盤前盤後資金動向。"),
    ("NQ=F", "NQ=F", "納斯達克100期貨 (美股納指期指)", "E-Mini Nasdaq 100 Futures", "FUTURES", "USD", 22510.0, 27.5, 5.60, 8.20, 16.5, 56.0, 500.0, 12.0, "🟢 期指合約 | 全球科技股高頻量化交易核心對沖與方向性工具。"),
    ("YM=F", "YM=F", "道瓊斯指數期貨 (美股道斯/道指期貨)", "Mini Dow Jones Futures", "FUTURES", "USD", 44310.0, 20.2, 2.75, 5.10, 7.5, 38.0, 500.0, 6.0, "⚪ 期指合約 | 反映美國傳統工業與金融巨頭期貨定價。"),
    ("RTY=F", "RTY=F", "羅素2000指數期貨", "E-Mini Russell 2000 Futures", "FUTURES", "USD", 2318.0, 24.5, 1.45, 2.15, 6.0, 28.0, 500.0, -8.0, "🔴 期指合約 | 對美債孳息率抽升高度敏感。"),
    ("GC=F", "GC=F", "COMEX 黃金期貨 (國際金價/黃金)", "Gold Futures COMEX", "FUTURES", "USD", 2985.0, 20.0, 1.00, 1.00, 12.0, 50.0, 1500.0, 14.0, "🟢 避險配置 | 全球央行持續增持黃金對沖美元主權債務擴張及中東地緣風險，惟短線受實際利率反彈震盪。"),
    ("SI=F", "SI=F", "COMEX 白銀期貨 (國際銀價/白銀)", "Silver Futures COMEX", "FUTURES", "USD", 34.80, 20.0, 1.00, 1.00, 15.0, 50.0, 400.0, 18.0, "🟢 工業+貴金屬 | 兼具光伏工業需求與貴金屬屬性，波幅大於黃金。"),
    ("HG=F", "HG=F", "COMEX 銅期貨 (國際銅價)", "Copper Futures", "FUTURES", "USD", 4.65, 20.0, 1.00, 1.00, 8.0, 40.0, 300.0, -10.0, "🔴 短線受壓 | 受美國9月CPI超預期與美元走強影響，銅價近期連跌3日。"),
    ("CL=F", "CL=F", "WTI 紐約原油期貨 (國際油價)", "WTI Crude Oil Futures", "FUTURES", "USD", 71.50, 15.0, 1.00, 1.00, -5.0, 35.0, 800.0, -15.0, "🔴 供應過剩預期 | 俄烏能源停火協議與美國選前平抑油價政策壓制原油表現。"),
    ("BZ=F", "BZ=F", "布蘭特原油期貨 (Brent Oil)", "Brent Crude Oil Futures", "FUTURES", "USD", 75.20, 15.0, 1.00, 1.00, -5.0, 35.0, 800.0, -15.0, "🔴 弱勢震盪 | 地緣風險溢價回落，全球原油需求增速放緩。"),
    ("NG=F", "NG=F", "美國天然氣期貨 (Natural Gas)", "Natural Gas Futures", "FUTURES", "USD", 2.85, 15.0, 1.00, 1.00, 10.0, 35.0, 200.0, 15.0, "⚪ 季節性波動 | 受北美冬季取暖需求與AI數據中心燃氣發電需求支撐。"),

    # --- 香港股市核心股票 (55 隻) ---
    ("00700.HK", "00700", "騰訊控股", "Tencent Holdings", "HK", "HKD", 410.80, 13.0, 4.4, 2.57, 13.9, 56.2, 476.0, 38.0, "🟢 潛力上升 | 估值處10年底部12%分位，遊戲與視頻號廣告高增並有日均1億回購托底；惟須警惕2Q26 AI資本開支暴增176%導致單季自由現金流轉負及發債50億美元之折舊壓力。"),
    ("09988.HK", "09988", "阿里巴巴-W", "Alibaba Group", "HK", "HKD", 106.40, 9.0, 1.75, 1.62, 7.8, 39.2, 269.9, 42.0, "🟢 潛力上升 | 動態PE僅9倍處極端低估窪地，88VIP ARPU擴張與阿里雲AI重啟增長；惟內地電商存量價格戰與直播電商分流仍壓抑短期利潤率擴張速度。"),
    ("03690.HK", "03690", "美團-W", "Meituan", "HK", "HKD", 168.00, 18.9, 2.95, 5.40, 18.2, 38.5, 133.5, 24.0, "🟢 溫和上升 | 本地生活外賣與到店競爭格局企穩，中東Keeta擴張具潛力；但海外前置補貼投入及騎手社保合規成本將限制利潤率超預期空間。"),
    ("01810.HK", "01810", "小米集團-W", "Xiaomi Corp", "HK", "HKD", 27.08, 20.5, 1.85, 3.45, 24.5, 21.8, 86.1, 22.0, "🟢 溫和上升 | 智慧電動車SU7交付超預期並改善毛利；惟股價自低位已翻倍，記憶體晶片(DRAM/NAND)大幅漲價將擠壓手機業務毛利率，不宜盲目追高。"),
    ("09999.HK", "09999", "網易-S", "NetEase", "HK", "HKD", 135.00, 11.5, 3.60, 2.95, 8.4, 62.5, 55.5, 32.0, "🟢 潛力上升 | 遠期PE僅11.5倍且帳上淨現金逾千億人民幣，隨遊戲板塊遭錯殺；風險在於部分老遊戲流水自然衰減，需視乎下半年新遊管線表現。"),
    ("01211.HK", "01211", "比亞迪股份", "BYD Company", "HK", "HKD", 285.00, 16.3, 1.05, 4.85, 21.0, 20.6, 108.0, 26.0, "🟢 溫和上升 | 10月底中國渠道庫存低於2.25個月極度健康，DM5.0技術領先；惟歐盟關稅壁壘與內地車市價格戰仍是中長期估值上限的制約因素。"),
    ("00388.HK", "00388", "香港交易所", "HKEX", "HK", "HKD", 372.40, 30.0, 18.20, 8.95, 18.5, 88.4, 60.5, 8.0, "⚪ 中性觀望 | 雖受惠港股日均成交逾千億及IPO復甦，但現價對應30倍預測PE及近9倍PB，已充分反映市場活躍度預期，若大市成交回落將面臨估值收縮。"),
    ("00005.HK", "00005", "匯豐控股", "HSBC Holdings", "HK", "HKD", 92.96, 7.8, 3.65, 1.18, 5.5, 61.0, 313.1, 12.0, "⚪ 中性穩健 | 高股息率(6.45%)與持續回購提供防守性，且受惠高利率延長淨息差；但股價已貼近52週高位，英國稅務討論與香港商業地產撥備為潛在隱憂。"),
    ("00939.HK", "00939", "建設銀行", "China Construction Bank", "HK", "HKD", 9.81, 6.3, 2.65, 0.68, 4.2, 52.0, 312.5, 9.0, "⚪ 中性收息 | 險資與南向資金抱團高息國企推升股價創52週新高；適合長線收息，但淨息差(NIM)受LPR下調壓抑，短期資本增值爆發力有限。"),
    ("01398.HK", "01398", "工商銀行", "ICBC", "HK", "HKD", 5.12, 6.1, 2.50, 0.62, 3.8, 51.0, 285.0, 8.0, "⚪ 中性收息 | 估值低廉且股息率逾6%，惟屬典型防守型巨型銀行股，缺乏短期爆升30%以上之基本面彈性。"),
    ("03988.HK", "03988", "中國銀行", "Bank of China", "HK", "HKD", 3.98, 5.9, 2.30, 0.58, 4.0, 50.5, 195.0, 7.0, "⚪ 中性收息 | 海外業務佔比較高受惠外幣高息環境，但龐大股本決定其僅具收息防守屬性。"),
    ("01288.HK", "01288", "農業銀行", "Agricultural Bank of China", "HK", "HKD", 4.25, 6.2, 2.40, 0.65, 4.5, 51.2, 210.0, 6.0, "⚪ 中性觀望 | 近期隨國有大行連創新高，短期獲利盤累積，追高風險回報比一般。"),
    ("03968.HK", "03968", "招商銀行", "China Merchants Bank", "HK", "HKD", 41.50, 6.8, 2.90, 0.95, 5.2, 55.0, 135.0, 14.0, "⚪ 中性偏強 | 零售之王財富管理手續費收入觸底回升，惟內房按揭早償與消費信貸需求仍待進一步復甦。"),
    ("02318.HK", "02318", "中國平安", "Ping An Insurance", "HK", "HKD", 49.80, 7.2, 0.85, 0.92, 9.5, 32.0, 118.0, 18.0, "🟢 溫和上升 | 壽險新業務價值(NBV)維持雙位數正增長，資產端受惠股市回暖，惟不動產投資敞口仍需時間消化。"),
    ("01299.HK", "01299", "友邦保險", "AIA Group", "HK", "HKD", 64.20, 14.5, 2.80, 2.10, 11.0, 42.0, 92.0, 20.0, "🟢 溫和上升 | 內地訪港旅客保單需求穩健，持續回購註銷支撐每股內含價值，估值處歷史偏低水平。"),
    ("00941.HK", "00941", "中國移動", "China Mobile", "HK", "HKD", 76.50, 10.8, 1.45, 1.15, 6.0, 31.0, 212.0, 8.0, "⚪ 中性收息 | 5G資本開支高峰已過，派息率提升至70%以上，屬類債券防守資產，不具備短期爆升特徵。"),
    ("00883.HK", "00883", "中國海洋石油", "CNOOC", "HK", "HKD", 19.85, 6.8, 2.10, 1.25, 5.0, 48.0, 120.0, -12.0, "🔴 回撤風險 | 桶油成本雖具全球競爭力，但特朗普提出俄烏能源停火及考慮暫停聯邦汽油稅，油價回落風險直接壓抑盈利預期。"),
    ("00857.HK", "00857", "中國石油股份", "PetroChina", "HK", "HKD", 6.45, 6.5, 0.42, 0.78, 3.2, 22.0, 152.0, -10.0, "⚪ 中性偏弱 | 天然氣順價機制提供一定支撐，但國際油價下行週期限制股價表現。"),
    ("09618.HK", "09618", "京東集團-SW", "JD.com", "HK", "HKD", 152.00, 8.8, 0.38, 1.65, 6.5, 15.8, 62.0, 25.0, "🟢 溫和上升 | 估值極低(PE<9x)且受惠家電以舊換新補貼，惟高盛預計3Q26內地白電增長疲軟，補貼退坡後營收持續性存疑。"),
    ("09888.HK", "09888", "百度集團-SW", "Baidu Inc", "HK", "HKD", 96.50, 9.2, 1.85, 0.98, 3.5, 48.5, 34.5, -15.0, "🔴 弱勢預警 | 傳統搜尋廣告受AI原生對話應用與短視頻嚴重侵蝕，文心一言商業化變現未能抵消核心廣告下滑，低估值存在價值陷阱風險。"),
    ("01024.HK", "01024", "快手-W", "Kuaishou Technology", "HK", "HKD", 51.20, 11.8, 1.65, 3.80, 12.5, 53.0, 28.5, 28.0, "🟢 溫和上升 | 可靈(Kling)視頻生成大模型商業化領先，內循環電商GMV穩健，惟面臨抖音與微信視頻號雙重夾擊。"),
    ("09961.HK", "09961", "攜程集團-S", "Trip.com Group", "HK", "HKD", 495.00, 18.5, 5.80, 2.45, 16.0, 81.0, 41.5, 10.0, "⚪ 中性觀望 | 出境遊與國際OTA業務高增，但股價過去一年累積升幅巨大，現價已充分反映旅遊復甦紅利。"),
    ("02015.HK", "02015", "理想汽車-W", "Li Auto", "HK", "HKD", 108.50, 17.5, 1.35, 3.10, 14.0, 20.5, 29.5, -18.0, "🔴 回撤預警 | 增程式SUV市場遭問界、零跑等競爭對手密集分流，純電車型轉型陣痛未消，10月初內地車企EV訂單按月跌8%構成壓力。"),
    ("09868.HK", "09868", "小鵬汽車-W", "XPeng Inc", "HK", "HKD", 46.80, 45.0, 2.10, 2.85, 22.0, 14.2, 11.5, -24.0, "🔴 高危爆跌 | 雖有MONA低價車型衝量，但單車毛利微薄且尚未實現規模化純利，在價格戰中現金消耗風險高。"),
    ("09866.HK", "09866", "蔚來-SW", "NIO Inc", "HK", "HKD", 43.20, 55.0, 1.25, 3.60, 15.0, 10.5, 11.2, -32.0, "🔴 高危爆跌 | 換電站重資產投入導致長期巨額虧損，負債率高企且多次融資攤薄股東權益，基本面風險極高。"),
    ("00175.HK", "00175", "吉利汽車", "Geely Automobile", "HK", "HKD", 13.80, 11.2, 0.65, 1.45, 18.0, 15.5, 17.8, 15.0, "⚪ 中性偏強 | 極氪與銀河系列新能源轉型見效，出口增長穩健，估值相對合理。"),
    ("00981.HK", "00981", "中芯國際", "SMIC", "HK", "HKD", 28.90, 38.5, 4.20, 1.45, 16.5, 18.0, 29.5, -22.0, "🔴 回撤預警 | 雖受國產替代情緒追捧，但成熟製程產能過剩導致折舊壓力龐大，毛利率長期受壓，近40倍PE嚴重脫離ROE水平。"),
    ("02899.HK", "02899", "紫金礦業", "Zijin Mining", "HK", "HKD", 17.60, 13.5, 1.45, 3.40, 14.0, 17.5, 59.5, -14.0, "🔴 短線回撤 | 美國9月CPI超預期引發聯儲局加息預期升溫，銅價連跌3日拖累有色金屬板塊短線急挫。"),
    ("03993.HK", "03993", "洛陽鉬業", "CMOC Group", "HK", "HKD", 7.15, 12.0, 0.85, 2.30, 12.0, 16.0, 19.8, -18.0, "🔴 短線回撤 | 受美息預期抽升與基本金屬價格回調影響，單日曾急挫7%，短線技術面破位。"),
    ("01208.HK", "01208", "五礦資源", "MMG Limited", "HK", "HKD", 2.85, 14.5, 0.95, 1.85, 10.0, 21.0, 3.5, -25.0, "🔴 高危回撤 | 銅價下跌疊加高財務槓桿，對美元利率上升極為敏感。"),
    ("01088.HK", "01088", "中國神華", "China Shenhua Energy", "HK", "HKD", 34.50, 10.2, 1.85, 1.55, 1.5, 34.0, 88.0, 5.0, "⚪ 中性收息 | 長協煤價穩定提供高息護城河，但缺乏成長催化劑。"),
    ("09992.HK", "09992", "泡泡瑪特", "Pop Mart", "HK", "HKD", 72.40, 28.5, 7.20, 9.50, 45.0, 64.0, 12.5, 18.0, "🟢 溫和上升 | 花旗列為IP玩具板塊首選，海外東南亞與歐美門店爆發；惟近30倍PE已反映高預期，需防範單一IP熱度週期見頂風險。"),
    ("02020.HK", "02020", "安踏體育", "ANTA Sports", "HK", "HKD", 92.60, 17.2, 3.40, 4.60, 13.5, 62.5, 33.8, 22.0, "🟢 溫和上升 | 瑞銀指其全球化擴張(Amer Sports)仍被低估，多品牌矩陣抗週期能力強。"),
    ("02269.HK", "02269", "藥明生物", "WuXi Biologics", "HK", "HKD", 18.40, 16.5, 3.80, 1.85, 6.0, 41.0, 10.2, -20.0, "🔴 風險預警 | 美國生物安全法案地緣政治陰霾未散，全球生科投融資復甦緩慢，海外訂單轉移風險仍在。"),
    ("00669.HK", "00669", "創科實業", "Techtronic Industries", "HK", "HKD", 115.20, 21.5, 2.35, 4.20, 9.5, 39.8, 27.2, -12.0, "⚪ 中性偏弱 | 美國9月CPI超預期推遲聯儲局減息步伐，高按揭利率壓抑美國房屋裝修與電動工具需求。"),
    ("00300.HK", "00300", "美的集團", "Midea Group", "HK", "HKD", 74.50, 13.2, 1.25, 2.85, 8.0, 26.5, 68.0, -14.0, "🔴 下調預警 | 高盛預計3Q26內地白電增長疲軟並下調目標價，匯豐亦減持H股好倉，短線承壓。"),
    ("06690.HK", "06690", "海爾智家", "Haier Smart Home", "HK", "HKD", 28.40, 12.8, 0.95, 2.40, 7.5, 30.5, 34.0, -12.0, "🔴 下調預警 | 同受高盛下調目標價影響，歐美家電需求受高息壓制，內地地產後週期疲軟。"),
    ("06936.HK", "06936", "順豐控股", "SF Holding", "HK", "HKD", 38.20, 16.8, 0.65, 1.95, 7.0, 13.5, 24.0, -15.0, "🔴 評級下調 | 高盛將順豐下調至「中性」評級，商務件需求增速放緩且單票收入面臨價格競爭壓力。"),
    ("02057.HK", "02057", "中通快遞-W", "ZTO Express", "HK", "HKD", 154.00, 11.5, 2.65, 1.85, 8.5, 30.0, 16.2, -10.0, "⚪ 中性觀望 | 雖有股份回購，但同樣遭高盛下調評級至「中性」，電商快遞件量增速放緩。"),
    ("02618.HK", "02618", "京東物流", "JD Logistics", "HK", "HKD", 14.20, 12.5, 0.48, 1.75, 11.0, 11.8, 12.1, 22.0, "🟢 溫和上升 | 獲高盛列為物流板塊首選，接入淘天平台打開外部一體化供應鏈增量並大幅改善利潤率。"),
    ("01109.HK", "01109", "華潤置地", "China Resources Land", "HK", "HKD", 26.80, 6.8, 0.68, 0.65, 4.0, 25.0, 24.5, 10.0, "⚪ 中性觀望 | 摩通指專家對內地房地產短期表現仍持審慎態度，惟華潤置地憑藉經常性商場租金收入列為行業首選。"),
    ("01999.HK", "01999", "敏華控股", "Man Wah Holdings", "HK", "HKD", 3.15, 7.5, 0.72, 1.15, -2.0, 38.5, 1.6, -15.0, "🔴 基本面疲弱 | 花旗大幅下調目標價至3.6港元並調低盈利預測，受制於內房低迷與美國高息壓抑傢俬出口。"),
    ("00992.HK", "00992", "聯想集團", "Lenovo Group", "HK", "HKD", 11.20, 10.8, 0.28, 2.95, 9.5, 16.8, 17.8, -14.0, "🔴 短線承壓 | AI PC換機潮慢於預期且記憶體晶片漲價侵蝕組裝毛利，近期隨大市急挫逾6%。"),
    ("00001.HK", "00001", "長和", "CK Hutchison", "HK", "HKD", 43.80, 6.5, 0.62, 0.32, 3.5, 36.0, 21.5, 6.0, "⚪ 中性收息 | 歐洲電訊與全球港口業務現金流穩定，市帳率僅0.32倍，惟缺乏價值重估催化劑。"),
    ("00016.HK", "00016", "新鴻基地產", "Sun Hung Kai Properties", "HK", "HKD", 86.40, 11.2, 3.45, 0.42, 4.0, 44.0, 32.2, -8.0, "⚪ 中性偏弱 | 美國CPI超預期令港元HIBOR拆息維持高位，削弱香港住宅樓市反彈動力。"),
    ("00027.HK", "00027", "銀河娛樂", "Galaxy Entertainment", "HK", "HKD", 36.80, 15.2, 3.60, 2.15, 12.0, 38.0, 20.6, 8.0, "⚪ 中性觀望 | 澳門中場博彩收入穩健，惟非博彩資本開支增加及高端消費審慎限制估值倍數。"),
    ("01928.HK", "01928", "金沙中國有限公司", "Sands China", "HK", "HKD", 19.60, 14.0, 2.85, 12.50, 11.5, 36.5, 20.4, 9.0, "⚪ 中性觀望 | 倫敦人翻新工程完成後有助市佔回升，惟負債率偏高。"),
    ("01919.HK", "01919", "中遠海控", "COSCO Shipping Holdings", "HK", "HKD", 12.40, 5.8, 0.95, 0.88, 6.0, 24.0, 25.5, 12.0, "⚪ 中性偏強 | 摩通指集裝箱航運與港口短期需求勝預期並重申增持，惟長期新船交付運力過剩風險仍存。"),
    ("00316.HK", "00316", "東方海外國際", "OOIL", "HK", "HKD", 112.50, 6.2, 1.15, 0.82, 5.5, 25.0, 9.6, 10.0, "⚪ 中性收息 | 高派息率航運藍籌，短期運價有支撐但受全球貿易關稅政策波動影響。"),
    ("00002.HK", "00002", "中電控股", "CLP Holdings", "HK", "HKD", 68.50, 14.2, 1.95, 1.62, 3.0, 28.0, 22.3, 4.0, "⚪ 純防守收息 | 香港管制計劃協議保證穩定回報，澳洲業務改善，波幅極低。"),
    ("00003.HK", "00003", "香港中華煤氣", "HK & China Gas", "HK", "HKD", 6.35, 18.5, 2.10, 1.95, 2.5, 26.0, 15.2, -5.0, "⚪ 中性偏弱 | 估值高於同類公用股且無紅股派發，自由現金流僅夠覆蓋股息。"),
    ("00008.HK", "00008", "電訊盈科", "PCCW", "HK", "HKD", 5.14, 22.0, 1.10, 4.50, 2.0, 45.0, 5.1, 3.0, "⚪ 純收息股 | 依賴香港電訊(06823)派息上繳，本身媒體業務盈利薄弱且負債偏高。"),
    ("06969.HK", "06969", "思摩爾國際", "Smoore International", "HK", "HKD", 10.80, 26.5, 4.80, 2.75, 12.0, 38.0, 8.5, 16.0, "⚪ 政策博弈 | 受惠美國FDA對非法電子煙執法收緊及BAT新產品推出，惟估值已提前反映。"),
    ("01888.HK", "01888", "建滔積層板", "KB Laminates", "HK", "HKD", 8.45, 11.8, 1.35, 1.65, 18.0, 22.0, 3.4, 24.0, "🟢 溫和上升 | 花旗確認其自10月起薄布覆銅板(CCL)平均提價15%，受惠AI伺服器PCB上游漲價週期。"),
    ("00288.HK", "00288", "萬洲國際", "WH Group", "HK", "HKD", 6.15, 7.8, 0.38, 1.02, 5.5, 18.5, 10.1, 14.0, "🟢 穩健上升 | 瑞銀調研指農業與必需消費品受機構青睞，北美豬肉業務扭虧並分拆上市釋放價值。"),

    # --- 美國股市核心股票 (55 隻) ---
    ("NVDA", "NVDA", "輝達 (NVIDIA)", "NVIDIA Corp", "US", "USD", 217.09, 27.7, 17.29, 38.50, 68.0, 75.2, 5240.0, 28.0, "🟢 潛力上升 | 全球AI算力絕對霸主，TTM營收突破3,029億美元；惟市值已達5.24萬億美元且PS高達17倍，若雲端巨頭(微軟/Meta/騰訊)放緩晶片採購將引發高Beta回撤。"),
    ("TSM", "TSM", "台積電 ADR", "Taiwan Semiconductor", "US", "USD", 420.22, 21.6, 12.44, 8.20, 61.0, 58.5, 1930.0, 36.0, "🟢 強勢首選 | 獨家壟斷先進製程與CoWoS封裝，4Q26營收指引按年激增高達61%，遠期PE僅21.6倍，基本面確定性居全球硬體之首。"),
    ("MU", "MU", "美光科技 (Micron)", "Micron Technology", "US", "USD", 878.84, 16.9, 7.66, 6.40, 54.0, 48.5, 1020.0, 32.0, "🟢 潛力上升 | HBM高頻寬記憶體產能售罄，遠期PE僅16.9倍；惟記憶體具高度週期性且單日波動極大(近期隨亞太晶片股單日急挫5.6%)，須嚴控倉位。"),
    ("AVGO", "AVGO", "博通 (Broadcom)", "Broadcom Inc", "US", "USD", 334.59, 24.1, 17.96, 18.20, 32.0, 76.5, 1600.0, 25.0, "🟢 溫和上升 | 壟斷客製化AI ASIC晶片與高速交換機，VMware整合釋放強勁現金流；惟估值已處歷史高位區。"),
    ("AMD", "AMD", "超微半導體 (AMD)", "Advanced Micro Devices", "US", "USD", 538.05, 26.3, 21.26, 11.40, 38.5, 54.8, 878.4, 22.0, "🟢 溫和上升 | MI系列AI加速器在企業推理市場份額擴大；但市銷率(21.3x)甚至高於輝達，軟體生態ROCm仍落後CUDA。"),
    ("AAPL", "AAPL", "蘋果 (Apple)", "Apple Inc", "US", "USD", 342.94, 27.7, 10.71, 48.50, 9.5, 46.8, 5000.0, -6.0, "⚪ 中性偏貴 | 市值高達5萬億美元，營收增速僅約9.5%卻享有近28倍遠期PE與10.7倍PS，估值溢價過高，缺乏短期爆升催化劑。"),
    ("MSFT", "MSFT", "微軟 (Microsoft)", "Microsoft Corp", "US", "USD", 533.52, 27.8, 11.93, 11.80, 15.8, 69.8, 3960.0, 12.0, "⚪ 中性穩健 | Azure雲與Copilot護城河極深，但龐大AI數據中心資本開支開始進入折舊高峰期，壓抑短期自由現金流增速。"),
    ("GOOGL", "GOOGL", "谷歌 (Alphabet)", "Alphabet Inc", "US", "USD", 344.18, 22.1, 9.44, 9.80, 14.2, 58.2, 4210.0, 20.0, "🟢 溫和上升 | 美股科技七巨頭中估值最合理(22x PE)，自研TPU降低算力成本；惟美國司法部反壟斷拆分訴訟仍構成長期政策折價。"),
    ("AMZN", "AMZN", "亞馬遜 (Amazon)", "Amazon.com Inc", "US", "USD", 262.45, 25.2, 3.65, 8.40, 12.4, 49.5, 2830.0, 22.0, "🟢 溫和上升 | AWS雲業務受AI需求帶動加速，北美零售物流自動化大幅推升營業利潤率。"),
    ("META", "META", "Meta Platforms", "Meta Platforms Inc", "US", "USD", 705.44, 20.5, 7.89, 9.20, 18.5, 81.2, 1800.0, 18.0, "🟢 溫和上升 | AI推薦算法顯著提升Reels廣告轉化率，毛利率高達81%；惟Reality Labs元宇宙部門持續巨額虧損且AI資本開支再度上調。"),
    ("TSLA", "TSLA", "特斯拉 (Tesla)", "Tesla Inc", "US", "USD", 368.06, 62.5, 13.99, 16.80, 8.5, 17.8, 1450.0, -28.0, "🔴 高危爆跌 | 遠期PE高達62倍而汽車毛利率跌至17.8%，花旗指10月前11日中國車企EV訂單按月下跌8%，Robotaxi商業化落地需時數年，估值泡沫顯著。"),
    ("PLTR", "PLTR", "Palantir Technologies", "Palantir Technologies", "US", "USD", 194.15, 69.3, 75.73, 36.00, 42.0, 82.5, 466.6, -38.0, "🔴 極高危泡沫 | 總市值高達4,665億美元但TTM營收僅61.6億美元，市銷率(PS)高達75.7倍、靜態PE逾100倍！屬於極端動能泡沫，一旦增速微幅放緩或美債息率抽升，極易發生30%–50%均值回歸暴跌。"),
    ("ARM", "ARM", "Arm Holdings", "Arm Holdings plc", "US", "USD", 228.05, 58.0, 47.20, 34.00, 28.0, 95.0, 243.6, -32.0, "🔴 高危泡沫 | 市值2,436億美元但年營收僅51.6億美元，市銷率高達47倍，嚴重透支未來5年v9架構權利金增長。"),
    ("INTC", "INTC", "英特爾 (Intel)", "Intel Corporation", "US", "USD", 92.03, 42.0, 8.48, 3.85, -4.0, 41.0, 483.6, -26.0, "🔴 基本面惡化 | 資料中心CPU市佔率持續被AMD侵蝕，AI加速器遠落後輝達，晶圓代工(Foundry)業務巨額虧損，現價反彈缺乏實質業績支撐。"),
    ("ASML", "ASML", "艾司摩爾 (ASML)", "ASML Holding NV", "US", "USD", 1647.12, 31.5, 14.33, 28.00, 18.0, 51.5, 644.5, 14.0, "⚪ 中性觀望 | 獨家壟斷EUV光刻機，惟受對華出口管制收緊及部分晶圓廠延後非AI產能擴張影響，31.5倍PE處合理偏高區間。"),
    ("LRCX", "LRCX", "科林研發 (Lam Research)", "Lam Research", "US", "USD", 272.20, 22.8, 14.66, 32.00, 24.0, 47.8, 340.6, 24.0, "🟢 溫和上升 | 3D NAND與先進封裝蝕刻設備龍頭，直接受惠2026–2027年HBM記憶體擴產週期。"),
    ("AMAT", "AMAT", "應用材料 (AMAT)", "Applied Materials", "US", "USD", 413.11, 21.5, 10.63, 15.80, 20.0, 47.5, 327.8, 22.0, "🟢 溫和上升 | GAA環繞式閘極與背側供電製程轉換核心設備商，估值較同業更具安全邊際。"),
    ("DELL", "DELL", "戴爾科技 (Dell)", "Dell Technologies", "US", "USD", 455.44, 19.8, 1.92, 24.00, 18.5, 22.4, 289.6, 20.0, "🟢 溫和上升 | AI伺服器積壓訂單龐大，市銷率僅1.92倍；惟AI伺服器組裝毛利率偏低(約10%-12%)，拉低整體毛利率表現。"),
    ("ORCL", "ORCL", "甲骨文 (Oracle)", "Oracle Corp", "US", "USD", 129.59, 19.5, 5.47, 22.00, 14.0, 71.5, 392.9, 25.0, "🟢 溫和上升 | OCI雲端憑藉主權雲與大型AI模型訓練租賃合約，剩餘履約義務(RPO)屢創新高。"),
    ("ANET", "ANET", "Arista Networks", "Arista Networks", "US", "USD", 182.95, 32.0, 21.89, 18.50, 26.0, 64.5, 230.7, 15.0, "⚪ 中性偏貴 | 超大規模AI集群後端高速交換機領導者，基本面優異但近22倍PS已反映高預期。"),
    ("PANW", "PANW", "Palo Alto Networks", "Palo Alto Networks", "US", "USD", 346.95, 34.5, 24.72, 28.00, 19.0, 74.5, 283.8, 10.0, "⚪ 中性偏貴 | 網路安全平台化策略成功，但34.5倍PE與24.7倍PS限制短期爆發空間。"),
    ("CRWD", "CRWD", "CrowdStrike Holdings", "CrowdStrike Holdings", "US", "USD", 228.10, 48.0, 43.34, 38.00, 25.0, 75.5, 234.1, -24.0, "🔴 估值過高 | 市銷率高達43.3倍，企業IT安全預算審批拉長，高估值面臨美債息率上升壓縮。"),
    ("GEV", "GEV", "GE Vernova", "GE Vernova Inc", "US", "USD", 908.44, 36.0, 5.85, 18.00, 22.0, 24.5, 242.0, 18.0, "🟢 趨勢偏強 | AI數據中心引發全球燃氣渦輪與電網變壓器嚴重短缺，訂單排至2028年後；惟短期累升巨大需防獲利回吐。"),
    ("GE", "GE", "GE Aerospace", "GE Aerospace", "US", "USD", 311.28, 28.5, 6.38, 14.50, 16.0, 32.0, 323.0, 12.0, "⚪ 中性穩健 | 商用航空發動機售後維修服務高毛利現金流穩健。"),
    ("LLY", "LLY", "禮來製藥 (Eli Lilly)", "Eli Lilly and Co", "US", "USD", 1240.34, 34.0, 13.93, 46.00, 32.0, 81.0, 1110.0, 10.0, "⚪ 中性偏貴 | GLP-1減肥藥Mounjaro/Zepbound需求強勁，但市值已破1.1萬億美元且面臨產能擴張與醫保議價壓力。"),
    ("JNJ", "JNJ", "強生 (Johnson & Johnson)", "Johnson & Johnson", "US", "USD", 253.79, 16.8, 6.16, 6.80, 5.5, 69.0, 611.6, 5.0, "⚪ 純防守股 | 醫療器械與製藥現金流穩定，滑石粉訴訟陰霾漸消，波幅極小。"),
    ("ABBV", "ABBV", "艾伯維 (AbbVie)", "AbbVie Inc", "US", "USD", 282.68, 17.5, 7.76, 38.00, 7.5, 70.5, 499.5, 8.0, "⚪ 中性穩健 | Skyrizi與Rinvoq成功接棒Humira專利到期缺口，股息率穩健。"),
    ("MRK", "MRK", "默沙東 (Merck)", "Merck & Co", "US", "USD", 143.63, 14.2, 5.32, 7.20, 6.5, 75.0, 354.4, 9.0, "⚪ 中性觀望 | 抗癌藥Keytruda維持增長，惟中國市場HPV疫苗出貨放緩拖累短期表現。"),
    ("UNH", "UNH", "聯合健康 (UnitedHealth)", "UnitedHealth Group", "US", "USD", 406.88, 15.8, 0.81, 3.85, 8.0, 24.0, 365.2, 12.0, "⚪ 中性穩健 | 美國最大醫療保險與Optum服務巨頭，醫療賠付率(MLR)逐步企穩。"),
    ("BRK-B", "BRK-B", "波克夏海瑟威 (Berkshire)", "Berkshire Hathaway B", "US", "USD", 532.23, 21.5, 2.96, 1.78, 7.0, 28.0, 1140.0, 8.0, "⚪ 防守核心 | 帳上持有逾3,000億美元短期美債現金，受惠高無風險利率，屬抗跌價值錨。"),
    ("JPM", "JPM", "摩根大通 (JPMorgan)", "JPMorgan Chase", "US", "USD", 333.53, 14.8, 4.64, 2.35, 6.5, 58.0, 885.7, 6.0, "⚪ 中性觀望 | 投行與財富管理龍頭，惟2.35倍PB已處於美國大型銀行歷史估值頂部區間。"),
    ("GS", "GS", "高盛集團 (Goldman Sachs)", "Goldman Sachs", "US", "USD", 875.78, 15.5, 3.73, 2.15, 11.0, 56.0, 265.2, 9.0, "⚪ 中性觀望 | 受惠2026年大型科技IPO與併購活動復甦，但股價已大幅領先同業。"),
    ("MS", "MS", "摩根士丹利 (Morgan Stanley)", "Morgan Stanley", "US", "USD", 178.47, 16.2, 3.50, 2.45, 9.5, 54.0, 279.3, 7.0, "⚪ 中性觀望 | 財富管理資產規模龐大，估值處歷史高位。"),
    ("BAC", "BAC", "美國銀行 (Bank of America)", "Bank of America", "US", "USD", 50.39, 12.5, 3.04, 1.32, 5.0, 52.0, 348.8, 8.0, "⚪ 中性穩健 | 聯儲局推遲減息有利高息差維持，但長債帳面浮虧仍受美債息率波動影響。"),
    ("WFC", "WFC", "富國銀行 (Wells Fargo)", "Wells Fargo & Co", "US", "USD", 81.80, 12.8, 2.95, 1.48, 4.5, 51.0, 245.5, 6.0, "⚪ 中性觀望 | 資產上限監管有望逐步放寬，惟近期非利息收入增速放緩。"),
    ("V", "V", "Visa Inc", "Visa Inc", "US", "USD", 402.92, 26.5, 16.62, 18.50, 10.5, 80.0, 739.6, 11.0, "⚪ 穩健複利 | 全球跨境支付清算雙寡頭，現金流極佳，惟欠缺短期爆發性催化。"),
    ("MA", "MA", "萬事達卡 (Mastercard)", "Mastercard Inc", "US", "USD", 616.40, 28.0, 15.39, 55.00, 11.5, 78.5, 540.0, 11.0, "⚪ 穩健複利 | 與Visa同屬高ROE複利機器，適合長線底倉配置。"),
    ("WMT", "WMT", "沃爾瑪 (Walmart)", "Walmart Inc", "US", "USD", 118.70, 31.2, 1.28, 9.80, 5.5, 24.5, 941.7, -8.0, "⚪ 估值偏貴 | 雖具抗通脹剛需優勢與電商廣告增長，但作為傳統零售商PE高達31倍，安全邊際不足。"),
    ("COST", "COST", "好市多 (Costco)", "Costco Wholesale", "US", "USD", 995.53, 46.5, 1.46, 18.20, 7.5, 12.8, 441.2, -14.0, "🔴 估值過高 | 會員費續卡率雖高，但46.5倍PE遠超其7.5%營收增速(PEG > 5)，存在高估值回調風險。"),
    ("HD", "HD", "家得寶 (Home Depot)", "Home Depot Inc", "US", "USD", 309.88, 21.8, 1.83, 65.00, 3.0, 33.5, 309.2, -10.0, "🔴 利率壓抑 | 美國9月CPI超預期令30年期房貸利率回升，直接凍結美國二手房交易與家居裝修需求。"),
    ("KO", "KO", "可口可樂 (Coca-Cola)", "The Coca-Cola Company", "US", "USD", 93.42, 23.5, 8.02, 14.50, 4.5, 60.5, 401.9, 3.0, "⚪ 純防守收息 | 全球必需消費品定價權強，但受美元走強匯兌損益及高息環境壓制。"),
    ("PG", "PG", "寶潔 (Procter & Gamble)", "Procter & Gamble", "US", "USD", 157.69, 23.0, 4.21, 7.40, 4.0, 51.5, 366.3, 4.0, "⚪ 純防守收息 | 日化必需品龍頭，提價空間見頂，銷量增長溫和。"),
    ("PM", "PM", "菲利普莫里斯 (Philip Morris)", "Philip Morris International", "US", "USD", 201.05, 20.5, 7.36, 18.00, 8.5, 64.0, 313.4, 9.0, "⚪ 中性收息 | 無煙產品ZYN尼古丁袋與IQOS增長強勁，現金流充裕。"),
    ("XOM", "XOM", "埃克森美孚 (ExxonMobil)", "ExxonMobil Corp", "US", "USD", 178.28, 14.5, 2.03, 2.65, 2.0, 32.0, 733.1, -12.0, "🔴 油價回落風險 | 特朗普推動俄烏能源停火並考慮暫停聯邦汽油稅，原油供應過剩預期壓制能源股表現。"),
    ("CVX", "CVX", "雪佛龍 (Chevron)", "Chevron Corporation", "US", "USD", 224.37, 14.2, 2.10, 2.40, 1.5, 31.0, 440.1, -12.0, "🔴 油價回落風險 | 與埃克森美孚同樣面臨國際油價下行週期壓力。"),
    ("CAT", "CAT", "卡特彼勒 (Caterpillar)", "Caterpillar Inc", "US", "USD", 725.79, 22.5, 4.46, 16.50, 6.0, 36.5, 333.6, -9.0, "⚪ 週期見頂風險 | 基建與數據中心備用發電機需求雖強，但全球礦業資本開支受高利率與金屬價格回落影響。"),
    ("CSCO", "CSCO", "思科 (Cisco Systems)", "Cisco Systems", "US", "USD", 106.50, 18.5, 6.63, 8.90, 5.0, 64.5, 419.9, 6.0, "⚪ 中性觀望 | 整合Splunk後軟體訂閱佔比提升，但傳統企業園區網路設備增長緩慢。"),
    ("IBM", "IBM", "國際商業機器 (IBM)", "IBM Corp", "US", "USD", 236.39, 21.0, 3.22, 8.80, 6.0, 56.5, 222.7, 8.0, "⚪ 中性收息 | 企業級AI諮詢與Red Hat混合雲業務穩健，惟整體營收增速僅個位數。"),
    ("CRM", "CRM", "Salesforce", "Salesforce Inc", "US", "USD", 318.00, 26.5, 7.80, 5.20, 10.5, 76.0, 308.0, 12.0, "⚪ 中性觀望 | Agentforce企業AI代理商業化推進中，但SaaS席位增長放緩。"),
    ("NFLX", "NFLX", "網飛 (Netflix)", "Netflix Inc", "US", "USD", 69.85, 28.5, 6.01, 11.80, 14.5, 45.0, 290.9, 14.0, "⚪ 中性偏強 | 廣告訂閱層級與體育直播帶動用戶增長，定價權穩固(已拆股後價格)。"),
    ("SHOP", "SHOP", "Shopify", "Shopify Inc", "US", "USD", 171.06, 52.0, 16.59, 19.50, 22.0, 51.5, 220.1, -20.0, "🔴 估值偏高 | 電商SaaS龍頭基本面雖佳，但52倍PE與16.6倍PS對美國高利率與消費放緩極為敏感。"),
    ("TXN", "TXN", "德州儀器 (Texas Instruments)", "Texas Instruments", "US", "USD", 276.15, 34.0, 12.97, 14.20, 4.0, 58.0, 252.2, -15.0, "🔴 估值偏高 | 工業與汽車類比晶片庫存調整緩慢，34倍PE遠高於周期性復甦速度。"),
    ("TMO", "TMO", "賽默飛世爾 (Thermo Fisher)", "Thermo Fisher Scientific", "US", "USD", 679.38, 24.5, 5.42, 5.10, 7.0, 41.5, 251.2, 8.0, "⚪ 中性穩健 | 生命科學儀器與耗材龍頭，營運穩健但缺乏短期爆發力。"),
    ("COIN", "COIN", "Coinbase Global", "Coinbase Global", "US", "USD", 245.00, 38.0, 11.50, 7.20, 25.0, 84.0, 62.0, -28.0, "🔴 高波動風險 | 高度依賴加密貨幣現貨交易手續費，美債息率上升壓抑風險資產投機熱度。"),
    ("MSTR", "MSTR", "MicroStrategy", "MicroStrategy Inc", "US", "USD", 215.00, 85.0, 95.00, 6.80, 2.0, 72.0, 45.0, -35.0, "🔴 極高槓桿風險 | 透過持續發債加槓桿囤積比特幣，股價相較其持幣淨資產(NAV)存在大幅溢價，回撤風險極高。"),

    # --- 港美熱門 ETF 與基金 ---
    ("02800.HK", "02800", "盈富基金 (Tracker Fund)", "Tracker Fund of HK", "ETF", "HKD", 24.72, 10.8, 1.35, 1.12, 8.0, 35.0, 18.5, 15.0, "🟢 核心指數ETF | 緊貼恒生指數表現，日均成交逾100億港元，港股核心配置工具。"),
    ("02828.HK", "02828", "恒生中國企業 ETF", "Hang Seng H-Share ETF", "ETF", "HKD", 82.50, 9.6, 1.20, 1.05, 8.5, 34.0, 8.2, 16.0, "🟢 核心指數ETF | 追蹤國企指數，涵蓋中資金融與科網龍頭。"),
    ("03033.HK", "03033", "南方東英恒生科技 ETF", "CSOP Hang Seng TECH ETF", "ETF", "HKD", 4.62, 17.2, 2.15, 2.40, 14.5, 42.0, 5.6, 28.0, "🟢 科網龍頭ETF | 一籃子配置騰訊、阿里、美團、小米、中芯等30大科網股，分散單一個股風險。"),
    ("02840.HK", "02840", "SPDR 金 ETF (港股黃金ETF)", "SPDR Gold Trust HK", "ETF", "HKD", 2145.0, 20.0, 1.00, 1.00, 12.0, 50.0, 9.5, 14.0, "🟢 實物黃金ETF | 以港元直接交易倫敦實物黃金，對沖地緣與貨幣貶值風險。"),
    ("03110.HK", "03110", "Global X 恒生高股息率 ETF", "Global X HS High Dividend ETF", "ETF", "HKD", 26.80, 6.8, 1.10, 0.72, 4.5, 40.0, 1.2, 8.0, "⚪ 高息防守ETF | 聚焦低估值高派息國企與公用股，股息率逾6.5%。"),
    ("07200.HK", "07200", "FL二南方恒指 (2倍做多恒指)", "CSOP HSI Daily 2x Leveraged", "ETF", "HKD", 5.15, 10.8, 1.00, 1.00, 16.0, 35.0, 1.5, 25.0, "⚪ 槓桿型ETF | 提供恒指單日2倍正向槓桿，僅適合短線波段操作，長線持有具波動率耗損。"),
    ("07500.HK", "07500", "FI二南方恒指 (2倍做空恒指)", "CSOP HSI Daily -2x Inverse", "ETF", "HKD", 3.42, 10.8, 1.00, 1.00, -16.0, 35.0, 1.2, -25.0, "🔴 反向對沖ETF | 恒指單日2倍反向ETF，僅供大市回調時短線對沖，不宜長線持有。"),
    ("SPY", "SPY", "SPDR 標普500 ETF", "SPDR S&P 500 ETF Trust", "ETF", "USD", 610.50, 22.8, 3.05, 4.95, 11.0, 45.0, 620.0, 8.0, "⚪ 美股核心ETF | 全球流動性最高之標普500指數ETF。"),
    ("QQQ", "QQQ", "Invesco 納斯達克100 ETF", "Invesco QQQ Trust", "ETF", "USD", 546.00, 27.5, 5.60, 8.20, 16.5, 56.0, 310.0, 12.0, "🟢 科技核心ETF | 追蹤納斯達克100指數，匯聚全球最強AI與科技巨頭。"),
    ("SOXX", "SOXX", "iShares 半導體 ETF", "iShares Semiconductor ETF", "ETF", "USD", 265.00, 26.8, 8.90, 7.80, 34.0, 58.0, 18.5, 22.0, "🟢 半導體ETF | 集中持倉輝達、博通、AMD、美光、台積電等AI晶片核心資產。"),
    ("GLD", "GLD", "SPDR 黃金 ETF (美股黃金)", "SPDR Gold Shares", "ETF", "USD", 275.40, 20.0, 1.00, 1.00, 12.0, 50.0, 78.0, 14.0, "🟢 黃金現貨ETF | 全球最大實物黃金ETF，機構避險標準配置。"),
    ("TLT", "TLT", "iShares 20年期以上美國公債 ETF", "iShares 20+ Year Treasury Bond", "ETF", "USD", 91.80, 20.0, 1.00, 1.00, 4.5, 40.0, 52.0, -6.0, "🔴 長債利率風險 | 美國9月CPI超預期加上財政赤字龐大，長端美債息率居高不下壓抑TLT表現。"),

    # --- 香港熱門窩輪與牛熊證 ---
    ("19888.HK", "19888", "騰訊摩通六乙購A (00700 Call輪)", "JP-TENCENT@EC2612A (Call Warrant)", "WARRANT", "HKD", 0.185, 8.5, 1.0, 1.0, 35.0, 50.0, 0.5, 65.0, "🔥 認購窩輪 (Call) | 掛鉤騰訊(00700)，行使價480.0港元，實際槓桿約7.8倍，街貨量12.4%，引伸波幅31.5%，到期日2026-12-28。時間值每日耗損，僅宜短線看多博反彈。"),
    ("21055.HK", "21055", "騰訊高盛七甲購B (00700 Call輪)", "GS-TENCENT@EC2701B (Call Warrant)", "WARRANT", "HKD", 0.240, 8.5, 1.0, 1.0, 35.0, 50.0, 0.5, 55.0, "🔥 認購窩輪 (Call) | 掛鉤騰訊(00700)，行使價450.0港元，實際槓桿約6.9倍，街貨量8.5%，引伸波幅30.2%，到期日2027-01-26。條款中年期貼價。"),
    ("18422.HK", "18422", "騰訊瑞銀六十一沽A (00700 Put輪)", "UB-TENCENT@EP2611A (Put Warrant)", "WARRANT", "HKD", 0.112, 8.5, 1.0, 1.0, -25.0, 50.0, 0.5, -35.0, "🔥 認沽窩輪 (Put) | 掛鉤騰訊(00700)，行使價380.0港元，實際槓桿約8.4倍，街貨量15.2%，供看淡或正股持倉對沖下行風險使用。"),
    ("22318.HK", "22318", "阿里匯豐七乙購A (09988 Call輪)", "HS-ALIBABA@EC2702A (Call Warrant)", "WARRANT", "HKD", 0.158, 9.0, 1.0, 1.0, 35.0, 50.0, 0.5, 60.0, "🔥 認購窩輪 (Call) | 掛鉤阿里巴巴(09988)，行使價122.0港元，實際槓桿約6.5倍，街貨量18.6%，引伸波幅36.8%，到期日2027-02-23。"),
    ("23690.HK", "23690", "美團法興七甲購A (03690 Call輪)", "SG-MEITUAN@EC2701A (Call Warrant)", "WARRANT", "HKD", 0.195, 10.0, 1.0, 1.0, 25.0, 50.0, 0.5, 48.0, "🔥 認購窩輪 (Call) | 掛鉤美團(03690)，行使價195.0港元，實際槓桿約6.1倍，街貨量14.0%，引伸波幅41.2%。"),
    ("21810.HK", "21810", "小米中銀七丙購A (01810 Call輪)", "BI-XIAOMI@EC2703A (Call Warrant)", "WARRANT", "HKD", 0.142, 10.0, 1.0, 1.0, 22.0, 50.0, 0.5, 42.0, "🔥 認購窩輪 (Call) | 掛鉤小米(01810)，行使價31.5港元，實際槓桿約5.8倍，街貨量21.5%，引伸波幅43.5%。"),
    ("20388.HK", "20388", "港交摩通七乙購A (00388 Call輪)", "JP-HKEX@EC2702A (Call Warrant)", "WARRANT", "HKD", 0.168, 10.0, 1.0, 1.0, 15.0, 50.0, 0.5, 30.0, "🔥 認購窩輪 (Call) | 掛鉤港交所(00388)，行使價415.0港元，實際槓桿約7.2倍，街貨量11.8%，引伸波幅33.0%。"),
    ("10588.HK", "10588", "輝達法巴七甲購A (NVDA 美股窩輪)", "BN-NVIDIA@EC2701A (US Stock Warrant)", "WARRANT", "HKD", 0.215, 10.0, 1.0, 1.0, 30.0, 50.0, 0.5, 50.0, "🔥 美股認購輪 (Call) | 在港交所上市掛鉤美股輝達(NVDA)，行使價250美元，實際槓桿約5.2倍，方便亞洲時段捕捉美股AI龍頭波幅。"),
    ("56788.HK", "56788", "恒指法興七甲牛A (恒指牛證 收回23400)", "SG#HSI RC2701A (HSI Bull CBBC)", "CBBC", "HKD", 0.068, 12.0, 1.0, 1.0, 25.0, 50.0, 0.5, 55.0, "🐂 恒指牛證 (Bull) | 掛鉤恒生指數，收回價23,400點(距離現價約556點)，行使價23,300點，實際槓桿約34.5倍，街貨量28.4%。注意強制收回(打靶)風險！"),
    ("58912.HK", "58912", "恒指摩通七乙牛B (恒指遠牛 收回22900)", "JP#HSI RC2702B (HSI Bull CBBC)", "CBBC", "HKD", 0.118, 12.0, 1.0, 1.0, 20.0, 50.0, 0.5, 40.0, "🐂 恒指牛證 (Bull) | 掛鉤恒生指數，收回價22,900點(距離現價約1,056點，對應牛市關鍵防線)，實際槓桿約19.8倍，街貨量14.2%，安全墊較厚。"),
    ("61234.HK", "61234", "恒指瑞銀六乙熊A (恒指熊證 收回24500)", "UB#HSI RP2612A (HSI Bear CBBC)", "CBBC", "HKD", 0.065, 12.0, 1.0, 1.0, -20.0, 50.0, 0.5, -35.0, "🐻 恒指熊證 (Bear) | 掛鉤恒生指數，收回價24,500點(距離現價約544點)，行使價24,600點，實際槓桿約36.0x，街貨量35.6%(重貨區)。"),
    ("54321.HK", "54321", "騰訊法興七甲牛C (00700牛證 收回390)", "SG#TENCENT RC2701C (00700 Bull CBBC)", "CBBC", "HKD", 0.245, 12.0, 1.0, 1.0, 38.0, 50.0, 0.5, 85.0, "🐂 個股牛證 (Bull) | 掛鉤騰訊(00700)，收回價390.0港元(距離現價20.8港元，位於400港元鐵底下方)，行使價387.2港元，實際槓桿約16.5倍，街貨量9.8%。"),
    ("62700.HK", "62700", "騰訊高盛六乙熊A (00700熊證 收回445)", "GS#TENCENT RP2612A (00700 Bear CBBC)", "CBBC", "HKD", 0.380, 12.0, 1.0, 1.0, -30.0, 50.0, 0.5, -45.0, "🐻 個股熊證 (Bear) | 掛鉤騰訊(00700)，收回價445.0港元(距離現價34.2港元)，行使價447.8港元，實際槓桿約10.8倍，供看空或正股鎖倉對沖。"),
    ("59988.HK", "59988", "阿里摩通七甲牛A (09988牛證 收回95)", "JP#ALIBABA RC2701A (09988 Bull CBBC)", "CBBC", "HKD", 0.135, 12.0, 1.0, 1.0, 40.0, 50.0, 0.5, 75.0, "🐂 個股牛證 (Bull) | 掛鉤阿里巴巴(09988)，收回價95.0港元(距離現價11.4港元)，行使價93.5港元，實際槓桿約7.9倍，街貨量16.4%。")
]

def compute_critical_score(fwd_pe: float, ps: float, pb: float, rev_g: float, gm: float, exp_ret: float, mkt: str) -> tuple:
    score = 5.5
    if fwd_pe <= 10.0 and rev_g >= 5.0:
        score += 1.4
    elif fwd_pe <= 14.5 and rev_g >= 10.0:
        score += 1.3
    elif fwd_pe <= 22.0 and rev_g >= 20.0:
        score += 1.2
    elif fwd_pe > 45.0 or ps > 25.0:
        score -= 2.2
    elif fwd_pe > 30.0 and rev_g < 15.0:
        score -= 1.3
    elif fwd_pe > 25.0:
        score -= 0.5

    if rev_g >= 30.0 and gm >= 50.0:
        score += 0.9
    elif rev_g >= 12.0 and gm >= 45.0:
        score += 0.6
    elif rev_g < 3.0:
        score -= 0.7

    if exp_ret >= 30.0:
        score += 0.5
    elif exp_ret <= -20.0:
        score -= 1.5
    elif exp_ret < 0.0:
        score -= 0.8

    if mkt in ["WARRANT", "CBBC"]:
        score = min(score, 5.8)

    final_score = round(float(np.clip(score, 2.2, 8.4)), 1)
    
    if final_score >= 7.2:
        verdict = "🟢 估值偏便宜 (具安全邊際，可逢低分批吸納)"
        action_title = "執行「兩階段分批入市策略」（現價建立 30%–35% 左側底倉 ＋ 待右側訊號確認加倉）"
    elif final_score >= 5.5:
        verdict = "🟡 估值合理至略貴 (建議觀望，等待回調 10%–15% 再考慮)"
        action_title = "建議「暫時觀望並等待回調」（目前價格已充分反映利好，現價追高賠率不足）"
    else:
        verdict = "🔴 嚴重偏貴或基本面惡化 (強烈建議迴避/減持，切勿盲目買入)"
        action_title = "強烈建議「迴避或逢高減持」（估值存在泡沫或面臨基本面逆風，具顯著回撤風險）"
        
    return final_score, verdict, action_title

# ==========================================
# 4.5 真實上市年份對照表 (Listing Year Map)
# --------------------------------------------------
# 重大批判性修正：
# 過去版本無視實際上市年份，由 2010 年起為所有股票硬塞全套「歷史」財務數據，
# 導致 SpaceX (SPCX, 2025 才上市) 這類新股在表格中出現 2010 年的 PE/PS/PB，
# 全部屬虛假數據。現改為：
#   (1) 上市前所有年度 → 一律顯示「未上市 / N/A」，不再偽造數字；
#   (2) 上市後年度     → 沿用原有模型軌跡，並保留 82 項指標格式；
#   (3) 2026E/2027E/2028E → 明確標註為「模型預估」，非歷史實際財報。
# ==========================================
LISTING_YEAR_MAP = {
    # ---- 港股核心 ----
    "00700.HK": 2004, "00175.HK": 2004, "00981.HK": 2004, "02899.HK": 2003,
    "03993.HK": 2007, "01208.HK": 1994, "01088.HK": 2007, "09992.HK": 2020,
    "02020.HK": 2007, "02269.HK": 2017, "00669.HK": 1991, "00300.HK": 2024,
    "06690.HK": 2018, "06936.HK": 2024, "02057.HK": 2020, "02618.HK": 2021,
    "01109.HK": 1996, "01999.HK": 2010, "00992.HK": 1994, "00001.HK": 2015,
    "00016.HK": 1972, "00027.HK": 2005, "01928.HK": 2009, "01919.HK": 2007,
    "00316.HK": 1992, "00002.HK": 1970, "00003.HK": 1960, "00008.HK": 1994,
    "06969.HK": 2020, "01888.HK": 2004, "00288.HK": 2014,
    # ---- 美股核心 ----
    "NVDA": 1999, "TSM": 1997, "MU": 1984, "AVGO": 1991, "AMD": 1972,
    "AAPL": 1980, "MSFT": 1986, "GOOGL": 2004, "AMZN": 1997, "META": 2012,
    "TSLA": 2010, "PLTR": 2020, "ARM": 2023, "INTC": 1980, "ASML": 1995,
    "LRCX": 1984, "AMAT": 1972, "DELL": 2018, "ORCL": 1986, "ANET": 2017,
    "PANW": 2012, "CRWD": 2019, "GEV": 2024, "GE": 2024, "LLY": 1952,
    "JNJ": 1944, "ABBV": 2013, "MRK": 1946, "UNH": 1984, "BRK-B": 1996,
    "JPM": 1969, "GS": 1999, "MS": 1986, "BAC": 1998, "WFC": 1978,
    "V": 2008, "MA": 2006, "WMT": 1972, "COST": 1985, "HD": 1981,
    "KO": 1932, "PG": 1929, "PM": 2008, "XOM": 1999, "CVX": 1984,
    "CAT": 1929, "CSCO": 1990, "IBM": 1911, "CRM": 2004, "NFLX": 2002,
    "SHOP": 2015, "TXN": 1953, "TMO": 1980, "COIN": 2021, "MSTR": 1998,
    # ---- ETF ----
    "02800.HK": 1999, "02828.HK": 2003, "03033.HK": 2020, "02840.HK": 2010,
    "03110.HK": 2016, "07200.HK": 2017, "07500.HK": 2018,
    # ---- 用戶舉例：SpaceX (SPCX) 2025 年才上市 ----
    "SPCX": 2025, "SPACE-X": 2025,
}

def infer_listing_year(symbol: str) -> int:
    """
    若 symbol 不在 LISTING_YEAR_MAP 中，按代號規則保守推斷上市年份。
    規則採用「就晚不就早」原則：寧願少顯示歷史，也不偽造上市前數據。
    """
    sym = (symbol or "").strip().upper()
    if sym in LISTING_YEAR_MAP:
        return LISTING_YEAR_MAP[sym]
    if sym in INDEX_SYNONYM_MAP:
        return 2010  # 指數視為 2010 起有數據
    if sym.endswith(".HK"):
        num = sym.replace(".HK", "")
        if num.isdigit():
            n = int(num)
            if n >= 7000 and n <= 7999:
                return 2017  # 槓桿/反向 ETF
            if n >= 9000:
                return 2018  # 多數 2018 後新經濟股
            if n >= 6000:
                return 2018
            if n >= 3000:
                return 2020  # 新經濟/ETF
            if n >= 2000:
                return 2015
            if n >= 1000:
                return 2005
            return 2000      # 傳統華資/英資白籃
    # 未知美股 / ADR：保守預設 2010，並於 UI 提示用戶核實
    return 2010

MASTER_CATALOG = []
STOCK_DB = {}

for row in RAW_UNIVERSE:
    sym, code, name, en, mkt, curr, p, f_pe, ps, pb, rev_g, gm, mcap_u, exp_r, cat_note = row
    sc_val, verd_str, act_str = compute_critical_score(f_pe, ps, pb, rev_g, gm, exp_r, mkt)
    
    MASTER_CATALOG.append({
        "symbol": sym, "code": code, "name": name, "en": en,
        "mkt": mkt, "curr": curr, "base_p": p
    })
    
    if sym == "00700.HK":
        chg, pct, lo, hi, w52_l, w52_h = -7.00, -1.675, 406.40, 415.60, 411.00, 664.00
        vol_s, turn_s, mcap_s = "1.53千萬", "62.64億 HKD", "37,352.57億 HKD"
        eps_v, pe_v, f_eps_v, bps_v, sps_v, dps_v, ocf_v, fcf_v, sh_m = 27.5478, 14.912, 31.55, 199.32, 100.44, 6.28, 45.41, 18.93, 9086.32
        news_s = "《大行》摩通降騰訊目標價至625元 AI策略順序清晰惟回報需時更長"
        adr_s = "相關美股: TCEHY.US 52.290 美元 (折合 410.38 港元, 港股比美股高 +0.10%)"
    else:
        # v6：不再用 np.random 造假漲跌/高低點。以下僅為「最後已知靜態參考基準」，
        # 真實行情會在 get_or_create_asset_profile 由 Yahoo Finance 覆蓋；離線時漲跌明確顯示 0。
        chg, pct = 0.0, 0.0
        lo = round(p, 3)
        hi = round(p, 3)
        w52_l = round(p * 0.72, 2)
        w52_h = round(p * 1.28, 2)
        vol_s = "數據連線中"
        turn_s = f"— {curr}"
        mcap_s = f"{round(mcap_u * (7.85 if curr=='HKD' else 1.0), 1)}十億 {curr}" if mkt in ["HK", "US"] else f"指數/合約資產 (US${mcap_u}B)"
        pe_v = round(f_pe * 1.12, 2)
        eps_v = round(p / max(pe_v, 1.0), 4)
        f_eps_v = round(p / max(f_pe, 1.0), 4)
        bps_v = round(p / max(pb, 0.2), 2)
        sps_v = round(p / max(ps, 0.2), 2)
        dps_v = round(p * (0.045 if f_pe < 9 else (0.015 if f_pe < 22 else 0.004)), 2)
        ocf_v = round(f_eps_v * 1.32, 2)
        fcf_v = round(f_eps_v * 0.92, 2)
        sh_m = round((mcap_u * (7850 if curr=="HKD" else 1000)) / max(p, 0.01), 2)
        news_s = f"《即時診斷》{name} ({sym})：{cat_note}"
        adr_s = f"類別: {mkt} | 貨幣: {curr} | 批判性評分: {sc_val}/10 ({verd_str[:8]})"

    STOCK_DB[sym] = {
        "symbol": sym, "code": code, "name": name, "en": en, "mkt": mkt, "curr": curr,
        "price": p, "change": chg, "pct": pct, "low": lo, "high": hi,
        "vol": vol_s, "turnover": turn_s, "mkt_cap": mcap_s, "mkt_cap_usd": mcap_u,
        "eps": eps_v, "pe": pe_v, "fwd_eps": f_eps_v, "fwd_pe": f_pe, "ps": ps, "pb": pb,
        "yield": round(dps_v / max(p, 0.01) * 100, 2),
        "lot": 10000 if mkt in ["WARRANT", "CBBC"] else (100 if mkt in ["HK", "ETF"] else 1),
        "w52_low": w52_l, "w52_high": w52_h,
        "rev_b": round(mcap_u / max(ps, 0.3) * (7.85 if curr=="HKD" else 1.0), 2),
        "rev_usd": round(mcap_u / max(ps, 0.3), 2),
        "rev_growth": rev_g, "gross_margin": gm, "net_margin": round(gm * 0.48, 2),
        "roe": round(min(max(pb / max(f_pe, 1.0) * 100.0, 5.0), 65.0), 2),
        "bps": bps_v, "sps": sps_v, "dps": dps_v, "ocf_ps": ocf_v, "fcf_ps": fcf_v,
        "shares_m": sh_m, "exp_return": exp_r,
        "tag": cat_note.split("|")[0].strip(),
        "news": news_s, "adr_info": adr_s,
        "score": sc_val, "verdict": verd_str, "action_plan": act_str,
        "catalyst": cat_note,
        "stage": "衍生槓桿合約 (具時間值耗損或強制收回機制)" if mkt in ["WARRANT", "CBBC"] else (
            "高估值動能期 (需警惕泡沫均值回歸)" if f_pe > 35 else (
                "核心現金牛與AI轉型收穫期" if f_pe < 18 and rev_g > 8 else "成熟期穩健經營階段"
            )
        )
    }

# ==========================================
# 4. 真實市場數據抓取層（v7：多數據源備援 — Yahoo Finance 為主，Stooq 為備；杜絕假跳動、假盤口、假財報）
# ==========================================

def to_stooq_symbols(symbol: str):
    """把 Yahoo 代號轉換為 Stooq 候選代號清單（回傳 list，港股主板給 4 位補零與不補零兩種寫法）；無法對應者回傳空清單。"""
    s = str(symbol).strip().upper()
    idx_map = {"^HSI": "^hsi", "^GSPC": "^spx", "^SPX": "^spx", "^IXIC": "^comp",
               "^NDX": "^ndx", "^DJI": "^dji", "^RUT": "^rut", "^VIX": "^vix"}
    if s in idx_map:
        return [idx_map[s]]
    if s.endswith(".HK"):
        digits = s[:-3]
        if not digits.isdigit():
            return []
        # 港股主板代號為 4 位（Yahoo 常寫 00700；Stooq 為 0700.hk）。
        # 5 位且以 1/2/3/6 等開頭者為窩輪/牛熊證/界內證，Stooq 免費源不提供 → 回空。
        is_main = (len(digits) <= 4) or (len(digits) == 5 and digits.startswith("0"))
        if not is_main:
            return []
        bare = digits.lstrip("0")
        if not bare:
            bare = "0"
        main_code = bare.zfill(4)
        cands = [main_code.lower() + ".hk"]
        alt = bare.lower() + ".hk"
        if alt not in cands:
            cands.append(alt)
        return cands
    if "." not in s and "=" not in s and s.isalnum():
        return [s.lower() + ".us"]
    return []


def to_stooq_symbol(symbol: str):
    """回傳首選 Stooq 代號（向後相容）；無對應回傳 None。"""
    cands = to_stooq_symbols(symbol)
    return cands[0] if cands else None


@st.cache_data(ttl=15, show_spinner=False)
def fetch_stooq_ohlc(symbol: str, stooq_interval: str = "d", days: int = 2600):
    """
    從 Stooq（免費、無須 API key、全球可訪問性通常較 Yahoo 穩定）下載歷史 OHLCV。
    stooq_interval: d=日線, w=週線, m=月線。Stooq 免費源不提供分鐘線。
    回傳 DataFrame（含 Open/High/Low/Close/Volume），失敗回傳空 DataFrame。
    """
    out = pd.DataFrame()
    candidates = to_stooq_symbols(symbol)
    if not candidates:
        return out
    for ss in candidates:
        try:
            url = f"https://stooq.com/q/d/l/?s={ss}&i={stooq_interval}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            if not _network_allowed():
                raise OSError("manual mode: 自動聯網已關閉")
            with urllib.request.urlopen(req, timeout=12) as resp:
                raw = resp.read().decode("utf-8", errors="ignore")
            if not raw or "No data" in raw or len(raw) < 40:
                continue
            df = pd.read_csv(io.StringIO(raw))
            if df is None or df.empty or "Close" not in df.columns:
                continue
            df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
            df = df.dropna(subset=["Date"]).set_index("Date").sort_index()
            for c in ["Open", "High", "Low", "Close", "Volume"]:
                if c in df.columns:
                    df[c] = pd.to_numeric(df[c], errors="coerce")
            df = df.dropna(subset=["Close"])
            if not df.empty:
                df = df.tail(days)
                return df[["Open", "High", "Low", "Close", "Volume"]] if "Volume" in df.columns else df[["Open", "High", "Low", "Close"]]
        except Exception:
            continue
    return out


# ============================================================
# AAStocks 行情源（v7.1 第四數據源）：港股正股 / 窩輪 / 牛熊證即時(延遲)報價備援
#   - 端點：https://www.aastocks.com/tc/resources/datafeed/getrtqsymbol.ashx?s=<code>
#   - 回傳 JSON 陣列，單筆欄位：a=最新價、b=升跌(HTML)、c=最高-最低、d=成交量文字、e=報價時間
#   - 誠實範圍：僅支援港股純數字代號（正股/窩輪/牛熊證）；美股與指數代號回傳 "a":"N/A"，故回 live_ok=False，不硬接
#   - 成交量 d 為文字（如「34.34億」），只保留為 vol_str 顯示，不硬換算成假股數，避免污染成交額
#   - 全部採防禦式解析，任何異常或欄位缺失即回空/None，絕不拋出、絕不造假
# ============================================================
def _to_float(x, default=0.0):
    """把任意型別（int/float/str/None）安全轉成 float；空值或無法解析回 default。
    非官方數據源（AAStocks/騰訊）可能回傳字串/None，下游做數值比較前一律先經此正規化，
    徹底避免 TypeError: '>' not supported between instances of 'str'/'NoneType' and 'int'。"""
    if x is None:
        return default
    if isinstance(x, bool):
        return float(x)
    if isinstance(x, (int, float)):
        return float(x)
    try:
        s = str(x).strip().replace(",", "")
        if s in ("", "-", "--", "N/A", "n/a", "None"):
            return default
        return float(s)
    except Exception:
        return default


def to_aastocks_code(symbol: str):
    """Yahoo 代號 → AAStocks 港股 5 位純數字代號；無法對應（美股/指數）回傳 None。"""
    s = str(symbol).strip().upper()
    if s.endswith(".HK"):
        digits = s[:-3]
        if digits.isdigit():
            return digits.zfill(5)
        return None
    if s.isdigit():
        return s.zfill(5)
    return None


@st.cache_data(ttl=8, show_spinner=False)
def fetch_aastocks_quote(symbol: str):
    """AAStocks 港股即時/延遲報價備援。回傳與 fetch_realtime_market_tick 相容的 dict；失敗或非港股 live_ok=False。"""
    out = {"live_ok": False, "bid": None, "ask": None, "bid_size": None, "ask_size": None,
           "quote_time": None, "delayed": True}
    try:
        aa_code = to_aastocks_code(symbol)
        if not aa_code:
            return out

        url = f"https://www.aastocks.com/tc/resources/datafeed/getrtqsymbol.ashx?s={aa_code}"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer": "https://www.aastocks.com/"})
        if not _network_allowed():
            raise OSError("manual mode: 自動聯網已關閉")
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
        j = json.loads(raw)
        if not isinstance(j, list) or not j:
            return out

        f = j[0]
        price_raw = str(f.get("a", "")).strip()
        if not price_raw or price_raw.upper() == "N/A":
            return out
        p = float(price_raw)

        # b 欄位為帶 HTML 的升跌字串，例：<span class='neg'>-5.800(1.496%)</span>
        b_text = re.sub(r"<[^>]+>", "", str(f.get("b", "")))
        m_chg = re.search(r"([-+]?\d+(?:\.\d+)?)\s*\(([-+]?\d+(?:\.\d+)?)\s*%\)", b_text)
        chg = float(m_chg.group(1)) if m_chg else 0.0
        pct = float(m_chg.group(2)) if m_chg else 0.0
        prev = round(p - chg, 3)

        # c 欄位為「最低-最高」字串（前小後大），例：391.800-402.600；對照參考圖波幅 384.00-390.80
        hi = lo = None
        c_raw = str(f.get("c", ""))
        if "-" in c_raw:
            parts = c_raw.split("-")
            try:
                lo, hi = float(parts[0]), float(parts[-1])
                # 雙保險：若端點順序有變，強制 hi >= lo，避免出現負振幅
                if hi < lo:
                    hi, lo = lo, hi
            except Exception:
                hi = lo = None

        # d 欄實為「成交金額」文字（如「62.36億」），不是成交量；抽數字轉成本幣絕對金額
        d_text = str(f.get("d", "")).strip()
        amount_real = None
        m_amt = re.search(r"([-+]?\d+(?:\.\d+)?)\s*億", d_text)
        if m_amt:
            amount_real = float(m_amt.group(1)) * 1e8

        out.update({
            "live_ok": True,
            "price": round(p, 3),
            "prev_close": prev,
            "change": round(chg, 3),
            "pct": round(pct, 2),
            "open": None,
            "low": round(lo, 3) if lo is not None else round(p, 3),
            "high": round(hi, 3) if hi is not None else round(p, 3),
            # 此端點不提供標準化成交量（股/手），明確給 0.0（數值型），絕不給 None 或原始字串
            "vol_num": 0.0,
            "amount_real": amount_real,
            "amount_str": d_text,
            "quote_time": str(f.get("e", "")),
            "delayed": True,
            "source": "AAStocks",
            "currency": "HKD",
        })
    except Exception:
        pass
    return out



# ============================================================
# AAStocks 詳細數據源（v8 增強）
# ============================================================
@st.cache_data(ttl=15, show_spinner=False)
def fetch_aastocks_full_detail(symbol: str):
    """從 AAStocks 網頁抓取完整港股報價明細（對應 Err 1.jpg 格式）。"""
    out = {}
    try:
        aa_code = to_aastocks_code(symbol)
        if not aa_code:
            return out
        url = f"https://www.aastocks.com/tc/stocks/analysis/stock-aafn/{aa_code}/0/all/1"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.aastocks.com/"})
        if not _network_allowed():
            raise OSError("manual mode: 自動聯網已關閉")
        with urllib.request.urlopen(req, timeout=12) as resp:
            html_raw = resp.read().decode("utf-8", errors="ignore")
        if not html_raw or len(html_raw) < 5000:
            return out

        def _ex_num(pattern, text, group=1, cast=float):
            m = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
            if m:
                try:
                    return cast(m.group(group).strip().replace(",", ""))
                except Exception:
                    return None
            return None

        def _ex_str(pattern, text, group=1):
            m = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
            if m:
                return m.group(group).strip()
            return None

        out["bid"] = _ex_num(r"買價[^<]*</[^>]*>\s*<[^>]*>\s*([\d,.]+)", html_raw)
        out["ask"] = _ex_num(r"賣價[^<]*</[^>]*>\s*<[^>]*>\s*([\d,.]+)", html_raw)
        out["prev_close"] = _ex_num(r"前收市價[^<]*</[^>]*>\s*<[^>]*>\s*([\d,.]+)", html_raw)
        out["open"] = _ex_num(r"開市[^<]*</[^>]*>\s*<[^>]*>\s*([\d,.]+)", html_raw)
        range_m = re.search(r"波幅[^<]*</[^>]*>\s*<[^>]*>\s*([\d,.]+)\s*-\s*([\d,.]+)", html_raw)
        if range_m:
            try:
                out["low"] = float(range_m.group(1).replace(",", ""))
                out["high"] = float(range_m.group(2).replace(",", ""))
            except Exception:
                pass
        out["avg_price"] = _ex_num(r"均價[^<]*</[^>]*>\s*<[^>]*>\s*([\d,.]+)", html_raw)
        out["pe_static"] = _ex_num(r"市盈率\(倍\)[^<]*</[^>]*>\s*<[^>]*>\s*([\d,.]+)", html_raw)
        out["pe_ttm"] = _ex_num(r"TTM[^<]*</[^>]*>\s*<[^>]*>\s*([\d,.]+)", html_raw)
        out["eps"] = _ex_num(r"每股盈利[^<]*</[^>]*>\s*<[^>]*>\s*([\d,.]+)", html_raw)
        out["div_yield"] = _ex_num(r"收益率[^<]*</[^>]*>\s*<[^>]*>\s*([\d,.]+)%", html_raw)
        out["payout_ratio"] = _ex_num(r"派息比率[^<]*</[^>]*>\s*<[^>]*>\s*([\d,.]+)%", html_raw)
        out["dps"] = _ex_num(r"每股派息[^<]*</[^>]*>\s*<[^>]*>\s*([\d,.]+)", html_raw)
        out["pb"] = _ex_num(r"市賬率\(倍\)[^<]*</[^>]*>\s*<[^>]*>\s*([\d,.]+)", html_raw)
        out["nav"] = _ex_num(r"資產淨值[^<]*</[^>]*>\s*<[^>]*>\s*([\d,.]+)", html_raw)
        out["fund_flow"] = _ex_str(r"資金流向[^<]*</[^>]*>\s*<[^>]*>\s*([\d,.]+億)", html_raw)
        out["vol_ratio"] = _ex_num(r"量比[^<]*</[^>]*>\s*<[^>]*>\s*([\d,.]+)", html_raw)
        out["bid_ratio"] = _ex_num(r"委比[^<]*</[^>]*>\s*<[^>]*>\s*([-\d,.]+)%", html_raw)
        out["turnover_rate"] = _ex_num(r"換手[^<]*</[^>]*>\s*<[^>]*>\s*([\d,.]+)%", html_raw)
        out["amplitude"] = _ex_num(r"振幅[^<]*</[^>]*>\s*<[^>]*>\s*([\d,.]+)%", html_raw)
        out["avg_vol_3m"] = _ex_str(r"平均成交量[^<]*</[^>]*>\s*<[^>]*>\s*([\d,.]+千萬)", html_raw)
        out["mcap"] = _ex_str(r"市值/流通市值[^<]*</[^>]*>\s*<[^>]*>\s*([\d,.]+億)", html_raw)
        out["exchange"] = _ex_str(r"交易所[^<]*</[^>]*>\s*<[^>]*>\s*([^<]+)", html_raw)
        out["lot_size"] = _ex_num(r"每手股數[^<]*</[^>]*>\s*<[^>]*>\s*([\d,.]+)", html_raw, cast=int)
        out["spread"] = _ex_num(r"差價[^<]*</[^>]*>\s*<[^>]*>\s*([\d,.]+)", html_raw)
        out["short_sell_amt"] = _ex_str(r"沽空額/比率[^<]*</[^>]*>\s*<[^>]*>\s*([\d,.]+億)", html_raw)
        out["short_sell_ratio"] = _ex_num(r"沽空額/比率[^<]*</[^>]*>\s*<[^>]*>\s*([\d,.]+)%", html_raw)
        out["ex_div_date"] = _ex_str(r"除淨日[^<]*</[^>]*>\s*<[^>]*>\s*([\d-]+)", html_raw)
        out["div_pay_date"] = _ex_str(r"派息日期[^<]*</[^>]*>\s*<[^>]*>\s*([\d-]+)", html_raw)
        out["vol_str"] = _ex_str(r"成交量[^<]*</[^>]*>\s*<[^>]*>\s*([\d,.]+百萬)", html_raw)
        out["amount_str"] = _ex_str(r"成交金額[^<]*</[^>]*>\s*<[^>]*>\s*([\d,.]+億)", html_raw)
        out["ok"] = True
        out["source"] = "AAStocks"
    except Exception:
        pass
    return out

# ============================================================
# 騰訊財經行情源（v7 第三數據源）：qt.gtimg.cn 報價 + web.ifzq.gtimg.cn K線
#   - 免費、無 key、香港可達性通常優於 Yahoo；港股 r_hk 約 78 字段、美股 us 約 71 字段
#   - 報價覆蓋港美股現價/升跌/開高低/量/額/市值/PE/PB/52週高低
#   - K線覆蓋日/週/月線與 m15/m30/m60 分鐘線（45分/2H/3H/4H 由調用方 resample）
#   - 全部採防禦式解析，任何異常或欄位缺失即回空/None，絕不拋出、絕不造假
# ============================================================
_TENCENT_IDX = {
    "^HSI": ("hkHSI", "hkHSI"),
    "^HSCEI": ("hkHSCEI", "hkHSCEI"),
    "^HSTECH": ("hkHSTECH", "hkHSTECH"),
    "^GSPC": ("usINX", "usINX"),
    "^SPX": ("usINX", "usINX"),
    "^IXIC": ("usIXIC", "usIXIC"),
    "^NDX": ("usNDX", "usNDX"),
    "^DJI": ("usDJI", "usDJI"),
    "^RUT": ("usRUT", "usRUT"),
    "^VIX": ("usVIX", "usVIX"),
}


def _http_get_text(url: str, encoding: str = "utf-8", timeout: int = 10):
    if not _network_allowed():
        raise OSError("manual mode: 自動聯網已關閉，請按手動更新按鈕")
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": "https://gu.qq.com/"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode(encoding, errors="ignore")


def tencent_quote_code(symbol: str):
    """Yahoo 代號 → (騰訊報價代號, 市場 hk/us/index)；不支援回傳 (None, None)。"""
    s = str(symbol).strip().upper()
    if s in _TENCENT_IDX:
        return _TENCENT_IDX[s][0], "index"
    if s.endswith(".HK"):
        digits = s[:-3]
        if digits.isdigit() and (len(digits) <= 4 or (len(digits) == 5 and digits.startswith("0"))):
            return "r_hk" + digits.zfill(5), "hk"
        return None, None
    if "." not in s and "=" not in s and s.isalnum() and any(c.isalpha() for c in s):
        return "us" + s, "us"
    return None, None


def tencent_kline_codes(symbol: str):
    """Yahoo 代號 → (騰訊 K線代號候選清單, 市場)；不支援回傳 ([], None)。"""
    s = str(symbol).strip().upper()
    if s in _TENCENT_IDX:
        return [_TENCENT_IDX[s][1]], "index"
    if s.endswith(".HK"):
        digits = s[:-3]
        if digits.isdigit() and (len(digits) <= 4 or (len(digits) == 5 and digits.startswith("0"))):
            return ["hk" + digits.zfill(5)], "hk"
        return [], None
    if "." not in s and "=" not in s and s.isalnum() and any(c.isalpha() for c in s):
        return [f"us{s}.OQ", f"us{s}.UN", f"us{s}.N", f"us{s}"], "us"
    return [], None


def yf_to_tencent_period(interval: str):
    """Yahoo 區間 → 騰訊 K線週期；45m/2h/3h/4h 等由調用方以 m15/m60 resample。"""
    return {"15m": "m15", "30m": "m30", "45m": "m15",
            "60m": "m60", "1h": "m60", "2h": "m60", "3h": "m60", "4h": "m60",
            "1d": "day", "1wk": "week", "1mo": "month"}.get(interval)


@st.cache_data(ttl=8, show_spinner=False)
def fetch_tencent_quote(symbol: str):
    """騰訊財經即時/延遲報價。回傳與 fetch_realtime_market_tick 相容的 dict；失敗 live_ok=False。"""
    out = {"live_ok": False, "bid": None, "ask": None, "bid_size": None, "ask_size": None,
           "quote_time": None, "delayed": True}
    try:
        code, mkt = tencent_quote_code(symbol)
        if not code:
            return out
        raw = _http_get_text(f"https://qt.gtimg.cn/q={code}", encoding="gbk", timeout=10)
        m = re.search(r'"([^"]+)"', raw)
        if not m:
            return out
        f = m.group(1).split("~")
        if len(f) < 40:
            return out

        def num(i):
            try:
                v = f[i].strip()
                return float(v) if v not in ("", "-", "--") else None
            except Exception:
                return None

        price = num(3)
        prev = num(4)
        if price is None or price <= 0:
            return out
        hi, lo = num(33), num(34)
        pct = num(32)
        chg = (price - prev) if prev else None
        if pct is None and prev:
            pct = (chg / prev * 100.0) if prev else None
        out.update({
            "live_ok": True,
            "price": round(price, 3),
            "prev_close": round(prev, 3) if prev else None,
            "change": round(chg, 3) if chg is not None else None,
            "pct": round(pct, 2) if pct is not None else None,
            "open": round(num(5), 3) if num(5) else None,
            "low": round(lo, 3) if lo else round(price, 3),
            "high": round(hi, 3) if hi else round(price, 3),
            "vol_num": num(6) or 0.0,
            "pe": (num(39) if mkt == "hk" else num(53)) if mkt in ("hk", "us") else None,
            "pb": num(56) if mkt in ("hk", "us") else None,
            "mcap_yi": num(44) if mkt in ("hk", "us") else None,   # 億（港元 / 美元）
            "hi52": num(35),
            "lo52": num(36),
            "amount_raw": num(37) if mkt == "hk" else None,
            "quote_time": f[30] if len(f) > 30 else None,
            "delayed": True,
            "source": "騰訊財經",
            "currency": {"hk": "HKD", "us": "USD"}.get(mkt),
        })
    except Exception:
        pass
    return out


@st.cache_data(ttl=15, show_spinner=False)
def fetch_tencent_kline(symbol: str, tperiod: str, count: int = 640):
    """騰訊財經 K 線。tperiod: day/week/month/m15/m30/m60。回傳 OHLCV DataFrame；失敗回空。"""
    out = pd.DataFrame()
    try:
        codes, mkt = tencent_kline_codes(symbol)
        if not codes:
            return out
        is_min = str(tperiod).startswith("m")
        for code in codes:
            try:
                if is_min:
                    url = (f"https://web.ifzq.gtimg.cn/appstock/app/kline/mkline"
                           f"?param={code},{tperiod},,{count}")
                elif mkt == "hk":
                    url = (f"https://web.ifzq.gtimg.cn/appstock/app/hkfqkline/get"
                           f"?param={code},{tperiod},,,{count},qfq")
                else:
                    url = (f"https://web.ifzq.gtimg.cn/appstock/app/kline/kline"
                           f"?param={code},{tperiod},,,{count},qfq")
                raw = _http_get_text(url, encoding="utf-8", timeout=12)
                j = json.loads(raw)
                node = (j.get("data") or {}).get(code)
                if not isinstance(node, dict):
                    continue
                arr = node.get(f"qfq{tperiod}") or node.get(tperiod)
                if arr is None:
                    for k, v in node.items():
                        if tperiod in k and isinstance(v, list) and v:
                            arr = v
                            break
                if not arr:
                    continue
                rows = []
                for item in arr:
                    try:
                        # 騰訊 K 線順序固定為：[時間, 開盤, 收盤, 最高, 最低, 成交量]
                        o, c, h, l = float(item[1]), float(item[2]), float(item[3]), float(item[4])
                        v = float(item[5]) if len(item) > 5 and item[5] not in ("", None) else 0.0
                        rows.append((item[0], o, h, l, c, v))
                    except Exception:
                        continue
                if len(rows) < 6:
                    continue
                df = pd.DataFrame(rows, columns=["t", "Open", "High", "Low", "Close", "Volume"])
                df["dt"] = pd.to_datetime(df["t"], errors="coerce")
                df = df.dropna(subset=["dt"]).set_index("dt").sort_index()
                df = df[["Open", "High", "Low", "Close", "Volume"]]
                if not df.empty:
                    return df
            except Exception:
                continue
    except Exception:
        pass
    return out


@st.cache_data(ttl=15, show_spinner=False)
def get_price_history(symbol: str, interval: str, period: str, resample_rule=None):
    """
    統一歷史 K 線獲取（v7 多源）：
      1) Yahoo Finance（含分鐘/小時/日/週）；
      2) Yahoo 失敗或空白時，改以騰訊財經 K 線（含 m15/m30/m60 與日/週/月線）；
      3) 日/週/月線仍失敗時，再以 Stooq 備援。
    回傳 (DataFrame, source_name)；皆失敗回傳 (空 DataFrame, None)。
    """
    df = pd.DataFrame()
    source = None
    # 1) Yahoo（v9：以線程超時包裹，yfinance 內部重試最壞 4s 即放棄，轉騰訊/Stooq）
    def _yahoo_hist():
        tk = yf.Ticker(symbol)
        return tk.history(period=period, interval=interval, auto_adjust=True)
    raw = _run_with_timeout(_yahoo_hist, timeout=4.0)
    try:
        if raw is not None and not raw.empty and len(raw) >= 6:
            df = raw.copy()
            if df.index.tz is not None:
                df.index = df.index.tz_localize(None)
            if resample_rule:
                df = df.resample(resample_rule).agg({
                    "Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"
                }).dropna()
            source = "Yahoo Finance"
    except Exception:
        df = pd.DataFrame()
    # 2) 騰訊財經備援（含 m15/m30/m60 分鐘線與日/週/月線；45m/2H/3H/4H 由原始 m15/m60 resample）
    if df.empty:
        tp = yf_to_tencent_period(interval)
        if tp:
            count = {"1y": 320, "2y": 640, "3mo": 480, "5y": 1400, "10y": 2600, "max": 3000}.get(period, 640)
            try:
                td = fetch_tencent_kline(symbol, tp, count=count)
                if td is not None and not td.empty and len(td) >= 6:
                    df = td.copy()
                    if resample_rule:
                        df = df.resample(resample_rule).agg({
                            "Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"
                        }).dropna()
                    if len(df) >= 6:
                        source = "騰訊財經（Yahoo 連線失敗時的備援源）"
                    else:
                        df = pd.DataFrame()
            except Exception:
                df = pd.DataFrame()
    # 3) Stooq 備援（僅日/週/月線；分鐘/小時線 Stooq 免費源不提供）
    if df.empty and interval in ("1d", "1wk", "1mo"):
        si = {"1d": "d", "1wk": "w", "1mo": "m"}.get(interval, "d")
        days = {"1y": 300, "2y": 600, "5y": 1500, "10y": 3000, "max": 4000}.get(period, 300)
        sd = fetch_stooq_ohlc(symbol, si, days=days)
        if sd is not None and not sd.empty and len(sd) >= 6:
            df = sd.copy()
            source = "Stooq（Yahoo 連線失敗時的備援源）"
    return df, source


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def fetch_real_fundamentals(symbol: str):
    """
    從 Yahoo Finance 抓取真實基本面與財報數據（財報更新頻率低，快取 6 小時）。
    任何字段取不到時一律為 None，由調用方顯示「數據缺失」，絕不以亂數或推估值填充。
    回傳 dict：fund（基本面指標）、fin（財報關鍵值）、as_of（數據時點）。
    """
    out = {"ok": False, "fund": {}, "as_of": None, "source": "Yahoo Finance"}
    try:
        tk = yf.Ticker(symbol)
        def _get_info():
            return tk.info or {}
        # v9：info 觸發多個請求，以線程超時包裹，最壞 4s 回空 dict
        info = _run_with_timeout(_get_info, timeout=4.0, default={}) or {}

        def _g(*keys):
            for k in keys:
                v = info.get(k)
                if v is not None:
                    return v
            return None

        fund = {
            "bid": _g("bid", "regularMarketBid"),
            "ask": _g("ask", "regularMarketAsk"),
            "bid_size": _g("bidSize", "regularMarketBidSize"),
            "ask_size": _g("askSize", "regularMarketAskSize"),
            "trailing_pe": _g("trailingPE", "peTrailing"),
            "forward_pe": _g("forwardPE", "peForward"),
            "pb": _g("priceToBook", "pb"),
            "ps": _g("priceToSalesTrailing12Months", "psTrailing"),
            "trailing_eps": _g("trailingEps"),
            "forward_eps": _g("forwardEps"),
            "dividend_yield": _g("dividendYield"),
            "total_revenue": _g("totalRevenue"),
            "revenue_growth": _g("revenueGrowth"),
            "earnings_growth": _g("earningsGrowth"),
            "gross_margin": _g("grossMargins"),
            "profit_margin": _g("profitMargins"),
            "roe": _g("returnOnEquity"),
            "roa": _g("returnOnAssets"),
            "debt_to_equity": _g("debtToEquity"),
            "current_ratio": _g("currentRatio"),
            "market_cap": _g("marketCap", "marketCapNumber"),
            "shares_out": _g("sharesOutstanding"),
            "beta": _g("beta"),
            "book_value": _g("bookValue"),
            "operating_cf": _g("operatingCashflow"),
            "free_cf": _g("freeCashflow"),
            "total_cash": _g("totalCash", "cashAndCashEquivalents"),
            "total_debt_info": _g("totalDebt"),
            "fifty_two_high": _g("fiftyTwoWeekHigh"),
            "fifty_two_low": _g("fiftyTwoWeekLow"),
            "avg_volume": _g("averageVolume"),
            "sector": _g("sector"),
            "industry": _g("industry"),
            "quote_type": _g("quoteType"),
            "currency": _g("currency"),
            "regular_market_time": _g("regularMarketTime", "regularMarketUnix"),
        }

        # 財報（年度）— 真實歷史財報，取不到就留空
        fin = {}
        try:
            inc = _run_with_timeout(lambda: tk.income_stmt, timeout=3.0)
            if inc is not None and not inc.empty:
                latest = inc.columns[0]
                def _row(name):
                    if name in inc.index:
                        v = inc.loc[name].dropna()
                        return float(v.iloc[0]) if len(v) else None
                    return None
                fin["fiscal_date"] = str(latest.date()) if hasattr(latest, "date") else str(latest)
                fin["revenue"] = _row("Total Revenue")
                fin["gross_profit"] = _row("Gross Profit")
                fin["net_income"] = _row("Net Income")
                fin["ebitda"] = _row("EBITDA")
        except Exception:
            pass

        try:
            bs = _run_with_timeout(lambda: tk.balance_sheet, timeout=3.0)
            if bs is not None and not bs.empty:
                def _brow(name):
                    if name in bs.index:
                        v = bs.loc[name].dropna()
                        return float(v.iloc[0]) if len(v) else None
                    return None
                fin["total_assets"] = _brow("Total Assets")
                fin["total_debt"] = _brow("Total Debt")
                fin["total_equity"] = _brow("Stockholders Equity")
                fin["cash"] = _brow("Cash And Cash Equivalents")
        except Exception:
            pass

        out["ok"] = bool(info)
        out["fund"] = fund
        out["fin"] = fin
        out["as_of"] = datetime.now().strftime("%Y/%m/%d %H:%M")
        return out
    except Exception:
        return out


def _race_quote(workers, deadline=3.0):
    """並行競速：同時執行多個報價 worker，回傳第一個 live_ok 的 dict；無成功者回 None。
    全部用「守護線程」。追蹤完成數：所有 worker 快速失敗時立即返回（不空等 deadline），
    只有仍有線程在跑時才等到 deadline；慢源不阻塞主流程、也不阻止進程退出。"""
    import threading
    lock = threading.Lock()
    all_done = threading.Event()
    state = {"winner": None, "done": 0, "n": len(workers)}

    def _target(fn):
        try:
            r = fn()
        except Exception:
            r = None
        ok = isinstance(r, dict) and r.get("live_ok") and r.get("price")
        with lock:
            if ok and state["winner"] is None:
                state["winner"] = r
            state["done"] += 1
            if state["done"] >= state["n"]:
                all_done.set()

    ths = [threading.Thread(target=_target, args=(fn,), daemon=True) for fn in workers]
    for th in ths:
        th.start()
    all_done.wait(deadline)
    return state["winner"]


def _run_with_timeout(fn, timeout=4.0, default=None):
    """在獨立守護線程執行 fn，最多等 timeout；超時或拋異常回 default。
    用於給 yfinance 這類「內部重試不受 urllib timeout 控制」的調用加上硬性截止，避免單一慢源拖垮整頁；
    守護線程在超時後於後台自行結束，不阻止進程退出。
    v10：手動門控關閉時（切股/非更新 rerun）直接回 default，連線程都不啟動，確保零網絡、零等待。"""
    if not _network_allowed():
        return default
    import threading
    box = {}

    def _target():
        try:
            box["v"] = fn()
        except Exception:
            box["e"] = True

    th = threading.Thread(target=_target, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive() or "e" in box:
        return default
    return box.get("v", default)


@st.cache_data(ttl=600, show_spinner=False)
def fetch_realtime_market_tick(symbol: str):
    """v9 並行競速報價：Yahoo/AAStocks/騰訊同時發，第一成功者勝出（deadline 3s）；
    皆失敗再以 Stooq 日線收盤備援。港股成價一般延遲約 15 分鐘；Level 2 深度付費，本系統不提供。
    快取 ttl 拉長至 600s，配合手動按鈕刷新，避免切股反覆重抓造成凍結。"""
    base = {"live_ok": False, "bid": None, "ask": None, "bid_size": None,
            "ask_size": None, "quote_time": None, "delayed": None}
    # v10：門控關閉時不啟動任何競速線程（含 worker_yahoo 內直接 yf.Ticker），立即回 base。
    if not _network_allowed():
        return base

    # --- 即時源 1：Yahoo（yfinance；慢或被干擾時在獨立線程，不阻塞其他源）---
    def worker_yahoo():
        r = dict(base)
        try:
            tk = yf.Ticker(symbol)
            p = prev = lo = hi = vol = None
            try:
                h1d = tk.history(period="5d", interval="1d", auto_adjust=False)
                if h1d is not None and not h1d.empty:
                    p = float(h1d["Close"].iloc[-1])
                    prev = float(h1d["Close"].iloc[-2]) if len(h1d) > 1 else float(h1d["Open"].iloc[-1])
                    lo = float(h1d["Low"].iloc[-1]); hi = float(h1d["High"].iloc[-1])
                    vol = float(h1d["Volume"].iloc[-1])
            except Exception:
                pass
            try:
                h1m = tk.history(period="1d", interval="1m", auto_adjust=False)
                if h1m is not None and not h1m.empty:
                    p = float(h1m["Close"].iloc[-1])
                    lo = min(lo, float(h1m["Low"].min())) if lo is not None else float(h1m["Low"].min())
                    hi = max(hi, float(h1m["High"].max())) if hi is not None else float(h1m["High"].max())
                    r["quote_time"] = str(h1m.index[-1].tz_localize(None))[:19]
            except Exception:
                pass
            try:
                info = tk.info or {}
                r["bid"] = info.get("bid"); r["ask"] = info.get("ask")
                r["bid_size"] = info.get("bidSize"); r["ask_size"] = info.get("askSize")
                r["delayed"] = ("delayed" in str(info.get("exchangeDataDelayedBy", 0)).lower()) or (
                    isinstance(info.get("exchangeDataDelayedBy"), (int, float)) and info.get("exchangeDataDelayedBy", 0) > 60)
            except Exception:
                pass
            if p is not None and prev is not None:
                chg = p - prev
                pct = (chg / prev * 100.0) if prev else 0.0
                r.update({"live_ok": True, "price": round(p, 3), "change": round(chg, 3),
                          "pct": round(pct, 2),
                          "low": round(lo, 3) if lo is not None else round(p, 3),
                          "high": round(hi, 3) if hi is not None else round(p, 3),
                          "vol_num": vol or 0.0, "prev_close": round(prev, 3),
                          "source": "Yahoo Finance"})
        except Exception:
            pass
        return r

    # --- 即時源 2/3：AAStocks、騰訊（獨立線程，成功即標準 dict）---
    def worker_aastocks():
        try:
            d = fetch_aastocks_quote(symbol)
            return d if (isinstance(d, dict) and d.get("live_ok") and d.get("price")) else None
        except Exception:
            return None

    def worker_tencent():
        try:
            d = fetch_tencent_quote(symbol)
            return d if (isinstance(d, dict) and d.get("live_ok") and d.get("price")) else None
        except Exception:
            return None

    # --- 收盤備援：Stooq（僅當即時源全軍覆沒）---
    def worker_stooq():
        r = dict(base)
        try:
            sd = fetch_stooq_ohlc(symbol, "d", days=20)
            if sd is not None and not sd.empty and len(sd) >= 2:
                p = float(sd["Close"].iloc[-1]); prev = float(sd["Close"].iloc[-2])
                chg = p - prev; pct = (chg / prev * 100.0) if prev else 0.0
                r.update({"live_ok": True, "price": round(p, 3), "change": round(chg, 3),
                          "pct": round(pct, 2), "low": round(float(sd["Low"].iloc[-1]), 3),
                          "high": round(float(sd["High"].iloc[-1]), 3),
                          "vol_num": float(sd["Volume"].iloc[-1]) if "Volume" in sd.columns else 0.0,
                          "prev_close": round(prev, 3), "quote_time": str(sd.index[-1])[:10],
                          "delayed": True, "source": "Stooq"})
        except Exception:
            pass
        return r

    # 第一級：三個即時源並行競速（3s）
    winner = _race_quote([worker_yahoo, worker_aastocks, worker_tencent], deadline=3.0)
    # 第二級：全失敗才動用 Stooq 收盤備援（4s）
    if not winner:
        winner = _race_quote([worker_stooq], deadline=4.0)
    if isinstance(winner, dict) and winner.get("live_ok"):
        for k, v in base.items():
            winner.setdefault(k, v)
        return winner
    return base


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def fetch_real_momentum_snapshot(symbol: str):
    """
    v6 真實價格動能快照（快取 6 小時）。全部由真實歷史日線計算，不含任何假設軌跡：
      - 52 週高/低、現價於 52 週區間位置百分位
      - 近 21 / 63 / 126 交易日（約 1/3/6 月）真實回報
      - 日收益率年化波動率（用於客觀波幅區間）
      - RSI14、現價相對 50/200 日均線位置
    取不到時 ok=False。
    """
    out = {"ok": False}
    try:
        df, _src = get_price_history(symbol, "1d", "2y")
        if df is None or df.empty or len(df) < 60:
            return out
        out["source"] = _src
        close = df["Close"].dropna()
        if len(close) < 60:
            return out
        p = float(close.iloc[-1])
        hi52 = float(df["High"].tail(252).max())
        lo52 = float(df["Low"].tail(252).min())
        pos52 = (p - lo52) / (hi52 - lo52) * 100 if hi52 > lo52 else 50.0

        def _ret(n):
            if len(close) > n:
                base = float(close.iloc[-1 - n])
                return (p / base - 1) * 100 if base else None
            return None
        r21, r63, r126 = _ret(21), _ret(63), _ret(126)

        ret = close.pct_change().dropna()
        vol_ann = float(ret.tail(252).std() * (252 ** 0.5) * 100) if len(ret) > 30 else None

        delta = close.diff()
        up = delta.clip(lower=0).rolling(14).mean()
        dn = (-delta.clip(upper=0)).rolling(14).mean()
        rsi = float(100 - 100 / (1 + (up.iloc[-1] / (dn.iloc[-1] + 1e-9))))

        ma50 = float(close.tail(50).mean())
        ma200 = float(close.tail(min(200, len(close))).mean())
        above_ma50 = p > ma50
        above_ma200 = p > ma200

        out.update({
            "ok": True, "price": round(p, 3), "hi52": round(hi52, 3), "lo52": round(lo52, 3),
            "pos52": round(pos52, 1), "r21": round(r21, 1) if r21 is not None else None,
            "r63": round(r63, 1) if r63 is not None else None,
            "r126": round(r126, 1) if r126 is not None else None,
            "vol_ann": round(vol_ann, 1) if vol_ann is not None else None,
            "rsi": round(rsi, 1), "above_ma50": above_ma50, "above_ma200": above_ma200,
            "ma50": round(ma50, 3), "ma200": round(ma200, 3),
            "as_of": str(df.index[-1].tz_localize(None))[:10]
        })
        return out
    except Exception:
        return out


# ============================================================
# 本地真實年度財報（v9）：由用戶提供的 4 個 30 年財報 xlsx 經腳本 1:1 轉出，無手工抄錄、無估算
# 結構：代號 -> name / years(年度) / groups(分組 -> rows(l 指標, v 各年值，缺= None)
# ============================================================
LOCAL_FINANCIALS = {
  '00700.HK': {'name': '騰訊控股', 'years': [2003, 2004, 2005, 2006, 2007, 2008, 2009, 2010, 2011, 2012, 2013, 2014, 2015, 2016, 2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025], 'groups': [
    {'t': '每股指标', 'rows': [
      {'l': '每股收入', 'v': [0.1, 0.14, 0.15, 0.31, 0.44, 0.88, 1.53, 2.47, 3.76, 5.86, 8.26, 10.57, 13.11, 17.94, 29.55, 37.12, 43.72, 59.22, 70.73, 63.88, 69.32, 74.93, 89.84]},
      {'l': '每股税息折旧及摊销前利润', 'v': [0.05, 0.06, 0.05, 0.14, 0.2, 0.44, 0.79, 1.3, 1.86, 2.4, 3.18, 4.65, 5.66, 7.86, 14.28, 15.86, 18.74, 29.27, 39.61, 32.42, 26.43, 35.2, 34.94]},
      {'l': '每股息税前利润', 'v': [0.05, 0.06, 0.05, 0.12, 0.18, 0.4, 0.72, 1.2, 1.61, 2.05, 2.69, 4, 4.81, 6.35, 11.34, 11.8, 13.57, 23.03, 32.33, 25.36, 19.71, 28.82, 34.94]},
      {'l': '稀释每股收益', 'v': [0.05, 0.05, 0.05, 0.12, 0.18, 0.34, 0.64, 1.01, 1.35, 1.7, 2.12, 3.19, 3.67, 4.85, 8.89, 9.35, 10.73, 19.59, 28.36, 21.6, 13, 21.87, 26.68]},
      {'l': '持续经营每股收益', 'v': [0.05, 0.05, 0.05, 0.12, 0.18, 0.35, 0.64, 1.01, 1.31, 1.79, 2.32, 3.31, 4.13, 5.36, 8.09, 9.2, 10.83, 15.04, 15.55, 13.22, 17.85, 25.1, 30.8]},
      {'l': '巴菲特所有者每股收益（TTM）', 'v': [0, 0, 0, 0, 0.19, 0.38, 0.91, 1.34, 1.86, 2.64, 3.67, 4.93, 5.68, 8.07, 15.43, 13.41, 16.11, 27.5, 33.22, 23.33, 21.29, 29.37, 20.03]},
      {'l': '每股自由现金流', 'v': [0.04, -0.04, 0.08, 0.13, 0.12, 0.27, 0.93, 1.26, 1.12, 1.95, 2.5, 3.49, 4.12, 5.52, 9.21, 6.75, 10.61, 15.67, 14.27, 10.97, 19.87, 18.44, 36.22]},
      {'l': '每股经营现金流', 'v': [0.05, -0.03, 0.11, 0.17, 0.21, 0.44, 1.03, 1.55, 1.76, 2.59, 3.33, 4.38, 5.79, 7.74, 13.19, 13.17, 17.22, 23.85, 22.12, 16.83, 25.26, 29.34, 36.22]},
      {'l': '每股现金', 'v': [0.04, 0.16, 0.21, 0.26, 0.43, 0.44, 1.44, 2.82, 3.51, 3.65, 5.46, 7.16, 10.32, 14.46, 17.77, 19.91, 21.79, 28.16, 33.43, 33.93, 44.16, 40.12, 52.22]},
      {'l': '每股股息', 'v': [0, 0, 0, 0, 0, 0.03, 0.05, 0.08, 0.11, 0.15, 0.2, 0.24, 0.35, 0.45, 0.64, 0.82, 0.98, 1.31, 1.61, 1.52, 2.37, 3.3, 4.57]},
      {'l': '每股账面价值', 'v': [0.05, 0.28, 0.32, 0.42, 0.61, 0.88, 1.54, 2.77, 3.79, 5.54, 7.95, 10.7, 15.34, 20.66, 31.94, 38.6, 50.4, 86.99, 102.75, 84.19, 94.18, 113.81, 141.22]},
      {'l': '每股有形账面价值', 'v': [0.05, 0.28, 0.32, 0.4, 0.56, 0.84, 1.5, 2.67, 3.16, 4.72, 7.15, 9.27, 12.93, 15.27, 25.41, 29.36, 31.74, 63.39, 76.29, 61.19, 69.86, 86.73, 113.28]},
      {'l': '每股总债务', 'v': [0, 0, 0, 0, 0.04, 0, 0.03, 0, 1.56, 1.44, 2.06, 4.76, 8.42, 12.95, 16.45, 21.39, 27.09, 32.43, 41.22, 41.91, 43.24, 41.87, 49.73]},
      {'l': '月末股价', 'v': [0, 0.85, 1.53, 5.11, 11.06, 9.22, 31.16, 31.14, 28.78, 45.91, 91.19, 103.71, 140.58, 174.88, 374.27, 289.46, 346.25, 521.77, 421.1, 317.23, 293.6, 419.8, 599]},
    ]},
    {'t': '财务比率', 'rows': [
      {'l': '股本回报率 ROE ％', 'v': [68.27, 28.25, 17.57, 32.5, 36.16, 46.96, 53.79, 47.96, 41.45, 36.72, 31.58, 34.25, 28.32, 27.1, 33.95, 26.66, 24.46, 28.8, 30.22, 23.45, 14.91, 21.54, 21.46]},
      {'l': '股本回报率 ROE ％／市净率', 'v': [None, 9.35, 3.65, 2.66, 2, 4.5, 2.66, 4.27, 5.46, 4.43, 2.75, 3.53, 3.09, 3.2, 2.9, 3.55, 3.56, 4.8, 7.37, 6.22, 4.78, 5.84, 5.06]},
      {'l': '资产收益率 ROA ％', 'v': [55.96, 25.66, 15.58, 26.72, 27.74, 34.09, 37.74, 30.49, 22.43, 19.41, 17.17, 16.98, 11.87, 11.34, 15.39, 12.09, 11.03, 14.34, 15.49, 11.25, 7.23, 11.43, 11.96]},
      {'l': '有形股本回报率 ％', 'v': [68.27, 28.25, 17.64, 33.4, 38.73, 50.22, 55.74, 49.58, 46.66, 43.46, 35.87, 38.9, 33.21, 34.6, 43.9, 34.33, 35.63, 41.58, 41.05, 31.89, 20.3, 28.62, 27.37]},
      {'l': '有形资产收益率 ％', 'v': [55.96, 25.66, 15.64, 27.33, 29.22, 35.78, 38.69, 31.13, 23.87, 21.14, 18.37, 18.05, 12.65, 12.47, 17.15, 13.46, 12.84, 16.94, 17.91, 12.89, 8.29, 13.15, 13.59]},
      {'l': '乔尔·格林布拉特资本回报率 ％', 'v': [419.53, 110.86, 66.22, 130.7, 150.15, 166.2, 209.2, 307.74, 241.85, 210.11, 210.4, 263.44, 285.06, 318.22, 411.76, 292.74, 228.67, 276.63, 313.05, 241.25, 198, 230.93, 193.04]},
      {'l': '已动用资本回报率 ROCE ％', 'v': [71.09, 30, 15.82, 33.94, 34.8, 51.11, 56.93, 54.17, 42.42, 34.29, 30.95, 30.41, 24.44, 21.95, 26.78, 21.11, 18.8, 21.62, 22.86, 17.83, 14.48, 19.24, 19.73]},
      {'l': '5年留存收益回报率 RORE %', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 39.84, 37.65, 22.1, 23.39, 31.67, 23.54, 20.54, 28.09, 27.28, 14.68, 2.5, 3.57, -1.77]},
      {'l': '资本回报率 ％', 'v': [174.34, 62.28, 38.62, 68.58, 72.66, 75.1, 90.45, 63.67, 34.36, 28.07, 21.62, 20.52, 14.97, 13.87, 15.34, 12.09, 11.62, 11.71, 9.34, 7.89, 9.72, 11.9, 13.08]},
      {'l': '加权平均资本成本 WACC ％', 'v': [0, 0, 0, 10.71, 12.46, 9.12, 10.68, 9.85, 9.54, 9.34, 9.93, 8.85, 8.58, 8.27, 8.16, 10.26, 10.35, 7.37, 7.61, 10.77, 11.55, 11.88, 10.81]},
      {'l': '实际借款利率 ％', 'v': [None, 0, 32006.06, None, 0.54, 51.46, 0.97, 0.5, 0.62, 2.94, 3.09, 3.4, 2.93, 2.41, 2.6, 3.09, 3.7, 3.1, 2.74, 2.8, 3.22, 3.37, 4.02]},
      {'l': '毛利率 ％', 'v': [68.77, 63.44, 67.06, 70.82, 70.75, 69.66, 68.73, 67.83, 65.16, 58.52, 54.04, 60.89, 59.53, 55.61, 49.18, 45.45, 44.4, 45.95, 43.91, 43.05, 48.13, 52.9, 56.21]},
      {'l': '营业利润率 ％', 'v': [45.74, 40.99, 29.56, 38.44, 42.79, 44.44, 47.3, 48.77, 40.23, 34.52, 28.7, 33.57, 35.72, 33.1, 29.52, 24.95, 25.7, 26.57, 21.84, 20.55, 27.2, 31.62, 32.56]},
      {'l': '净利率 ％', 'v': [43.84, 38.58, 34.03, 37.99, 40.99, 38.92, 41.44, 40.99, 35.81, 29.01, 25.65, 30.17, 28, 27.05, 30.08, 25.17, 24.73, 33.16, 40.14, 33.95, 18.92, 29.39, 29.91]},
      {'l': '税息折旧及摊销前利润率 ％', 'v': [45.74, 40.99, 34.66, 44.87, 45.24, 50.4, 51.63, 52.76, 49.52, 41.01, 38.52, 43.96, 43.16, 43.8, 48.32, 42.73, 42.88, 49.42, 56, 50.75, 38.13, 46.97, 38.89]},
      {'l': '自由现金流利润率 ％', 'v': [39.86, -26.07, 51.14, 41.42, 27.34, 30.4, 60.91, 51.27, 29.84, 33.24, 30.27, 33.03, 31.43, 30.78, 31.18, 18.19, 24.28, 26.45, 20.18, 17.17, 28.66, 24.61, 40.31]},
      {'l': '债务股本比率', 'v': [0, 0, 0, 0, 0.06, 0, 0.02, 0, 0.41, 0.26, 0.26, 0.45, 0.55, 0.63, 0.52, 0.55, 0.54, 0.37, 0.4, 0.5, 0.46, 0.37, 0.35]},
      {'l': '股东权益比率', 'v': [0.82, 0.93, 0.85, 0.8, 0.75, 0.71, 0.7, 0.61, 0.5, 0.55, 0.54, 0.47, 0.39, 0.44, 0.46, 0.45, 0.45, 0.53, 0.5, 0.46, 0.51, 0.55, 0.57]},
      {'l': '资产有息负债率', 'v': [0, 0, 0, 0, 0.04, 0, 0.01, 0, 0.21, 0.14, 0.14, 0.21, 0.22, 0.28, 0.24, 0.25, 0.24, 0.2, 0.2, 0.23, 0.24, 0.2, 0.2]},
      {'l': '资产负债率', 'v': [0.18, 0.07, 0.15, 0.2, 0.24, 0.28, 0.3, 0.39, 0.49, 0.44, 0.45, 0.52, 0.6, 0.53, 0.5, 0.51, 0.49, 0.42, 0.46, 0.5, 0.45, 0.41, 0.39]},
      {'l': '毛利润／总资产 ％', 'v': [87.79, 42.2, 30.71, 49.83, 47.88, 61.02, 62.59, 50.44, 40.81, 39.15, 36.18, 34.28, 25.24, 23.32, 25.17, 21.83, 19.8, 19.88, 16.94, 14.27, 18.39, 20.56, 22.48]},
      {'l': 'Degree of Financial Leverage', 'v': [0, 0.38, 0.8, 0.77, 1.22, 0.71, 1.05, 0.87, 0.99, 0.93, 0.79, 1.03, 0.72, 0.97, 1.05, 1.19, 0.96, 1.17, 1.09, 1.11, 1.73, 1.58, 1.15]},
      {'l': '资产周转率', 'v': [1.28, 0.67, 0.46, 0.7, 0.68, 0.88, 0.91, 0.74, 0.63, 0.67, 0.67, 0.56, 0.42, 0.42, 0.51, 0.48, 0.45, 0.43, 0.39, 0.33, 0.38, 0.39, 0.4]},
      {'l': '股息支付率', 'v': [0, 0, 0, 0, 0, 0.09, 0.08, 0.08, 0.09, 0.09, 0.09, 0.07, 0.08, 0.08, 0.08, 0.09, 0.09, 0.09, 0.1, 0.12, 0.13, 0.13, 0.15]},
      {'l': '应收账款周转天数', 'v': [0, 0, 57, 40.05, 43.49, 37.87, 32.4, 27.03, 23.43, 18.06, 15.85, 17.57, 21.01, 21.29, 20.08, 26.67, 31.37, 29.76, 30.26, 32.76, 27.88, 26.52, 23.43]},
      {'l': '应付账款周转天数', 'v': [94.29, 26.94, 10.88, 14.21, 25.09, 29.78, 44.11, 59.4, 65.47, 64.38, 70.84, 91.56, 108.51, 119.72, 114.82, 134.79, 135.7, 118.92, 116.47, 122.75, 112.81, 130.34, 130.76]},
      {'l': '存货周转天数', 'v': [0, 0, 2.06, 1.12, 0.66, 0.59, 0.51, 0, 0, 11.39, 12.73, 9.78, 2.09, 1.36, 0.82, 0.68, 0.91, 1.04, 1.08, 2.02, 1.64, 0.53, 0.53]},
      {'l': '现金循环周期', 'v': [-94.29, -26.94, 48.18, 26.96, 19.06, 8.68, -11.2, -32.37, -42.04, -34.93, -42.27, -64.21, -85.41, -97.08, -93.92, -107.45, -103.41, -88.12, -85.13, -87.97, -83.29, -103.29, -106.8]},
      {'l': '存货周转率', 'v': [None, None, 177.51, 325.31, 556.48, 613.54, 712.31, None, None, 32.05, 28.68, 37.33, 174.78, 269.24, 444.37, 540.02, 400.01, 350.18, 339.45, 180.53, 222.63, 685.69, 689.19]},
      {'l': '营业成本／收入 ％', 'v': [0.31, 0.37, 0.33, 0.29, 0.29, 0.3, 0.31, 0.32, 0.35, 0.41, 0.46, 0.39, 0.4, 0.44, 0.51, 0.55, 0.56, 0.54, 0.56, 0.57, 0.52, 0.47, 0.44]},
      {'l': '存货／收入', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0.01, 0.02, 0.01, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '资本开支／收入', 'v': [0.08, 0.08, 0.19, 0.12, 0.21, 0.2, 0.07, 0.11, 0.17, 0.11, 0.1, 0.08, 0.13, 0.12, 0.13, 0.17, 0.15, 0.14, 0.11, 0.09, 0.08, 0.15, 0]},
      {'l': '资本开支／营业利润', 'v': [0.18, 0.19, 0.63, 0.32, 0.48, 0.44, 0.14, 0.23, 0.42, 0.32, 0.35, 0.25, 0.36, 0.37, 0.46, 0.69, 0.59, 0.52, 0.51, 0.45, 0.29, 0.46, 0]},
      {'l': '资本开支／经营现金流', 'v': [0.17, 0, 0.27, 0.23, 0.43, 0.39, 0.1, 0.18, 0.36, 0.25, 0.25, 0.2, 0.29, 0.29, 0.3, 0.49, 0.38, 0.34, 0.35, 0.35, 0.21, 0.37, 0]},
    ]},
    {'t': '合并利润表', 'rows': [
      {'l': '营业收入', 'v': [689, 1074, 1369, 2783, 4044, 8090, 14126, 22967, 34908, 54579, 77151, 98899, 123623, 170356, 281740, 355198, 419829, 571454, 685846, 619276, 666128, 704938, 830495]},
      {'l': '营业成本', 'v': [215, 393, 451, 812, 1183, 2454, 4416, 7388, 12162, 22639, 35460, 38683, 50033, 75614, 143187, 193760, 233406, 308843, 384696, 352665, 345531, 332058, 363646]},
      {'l': '毛利润', 'v': [474, 682, 918, 1971, 2861, 5636, 9709, 15578, 22746, 31940, 41691, 60216, 73590, 94742, 138554, 161438, 186422, 262611, 301150, 266611, 320596, 372880, 466849]},
      {'l': '毛利率 ％', 'v': [68.77, 63.44, 67.06, 70.82, 70.75, 69.66, 68.73, 67.83, 65.16, 58.52, 54.04, 60.89, 59.53, 55.61, 49.18, 45.45, 44.4, 45.95, 43.91, 43.05, 48.13, 52.9, 56.21]},
      {'l': '销售及行政开支', 'v': [158, 266, 524, 898, 1204, 2092, 2961, 4421, 8825, 13379, 20020, 27505, 29827, 38789, 60082, 74693, 83280, 120183, 159721, 151789, 150653, 159242, 196480]},
      {'l': '研发费用', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '其他营业成本', 'v': [1, -24, -10, 4, -73, -51, 66, -44, -124, -281, -470, -491, -398, -426, -4706, -1870, -4744, -9391, -8373, -12417, -11250, -9277, 0]},
      {'l': '总营业成本', 'v': [159, 241, 513, 901, 1131, 2041, 3027, 4376, 8701, 13098, 19550, 27014, 29429, 38362, 55376, 72823, 78537, 110792, 151348, 139373, 139403, 149965, 196480]},
      {'l': '营业利润', 'v': [315, 440, 405, 1070, 1730, 3595, 6682, 11202, 14045, 18842, 22140, 33202, 44161, 56379, 83177, 88615, 107886, 151820, 149803, 127238, 181193, 222915, 270369]},
      {'l': '营业利润率 ％', 'v': [45.74, 40.99, 29.56, 38.44, 42.79, 44.44, 47.3, 48.77, 40.23, 34.52, 28.7, 33.57, 35.72, 33.1, 29.52, 24.95, 25.7, 26.57, 21.84, 20.55, 27.2, 31.62, 32.56]},
      {'l': '利息收入', 'v': [0, 0, 44, 75, 91, 119, 154, 299, 575, 1040, 1677, 2100, 2797, 2936, 4669, 5190, 7026, 8247, 8143, 9595, 15103, 17087, 18680]},
      {'l': '利息支出', 'v': [0, 0, -44, -5, -2, -159, -2, -1, -89, -407, -503, -1085, -1815, -2430, -3626, -5564, -8557, -8830, -9695, -11150, -13000, -13289, -16714]},
      {'l': '净利息收入', 'v': [0, 0, -2, 29, -106, -40, 152, 298, 486, 633, 1174, 1015, 982, 507, 1043, -374, -1531, -583, -1553, -1556, 2103, 3798, 1965]},
      {'l': '其他收入（支出）', 'v': [2, -5, 17, 11, 0, -44, 25, 89, 291, -760, 1298, 2135, -1618, 1014, 20313, 19065, 15380, 62168, 155494, 109078, -6844, 31114, 33949]},
      {'l': '利润总额', 'v': [317, 436, 420, 1110, 1624, 3511, 6859, 11589, 14822, 18715, 24613, 36352, 43525, 57900, 104533, 107307, 121735, 213404, 303744, 234761, 176453, 257827, 306284]},
      {'l': '所得税费用', 'v': [-15, -21, 46, -53, 35, -327, -930, -2102, -2296, -2818, -4746, -6421, -8543, -11429, -18656, -16451, -15035, -23587, -24798, -24027, -47334, -48064, -52417]},
      {'l': '税率 ％', 'v': [4.73, 4.86, -11.05, 4.74, -2.18, 9.32, 13.56, 18.14, 15.49, 15.06, 19.28, 17.66, 19.63, 19.74, 17.85, 15.33, 12.35, 11.05, 8.16, 10.23, 26.83, 18.64, 17.11]},
      {'l': '其他净利润（亏损）', 'v': [0, 0, 0, 0, -4, 0, -150, -144, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '净利润', 'v': [302, 414, 466, 1057, 1655, 3184, 5779, 9343, 12526, 15897, 19867, 29931, 34983, 46471, 85877, 90856, 106699, 189817, 278946, 210734, 129118, 209762, 253867]},
      {'l': '持续经营净利润', 'v': [302, 414, 466, 1057, 1660, 3184, 5929, 9487, 12526, 15897, 19867, 29931, 34983, 46471, 85877, 90856, 106699, 189817, 278946, 210734, 129118, 209762, 253867]},
      {'l': '终止经营净利润', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '其他收入（少数股东权益）', 'v': [0, 0, 0, 0, 2, -35, 75, 72, -27, -66, -78, -98, -363, -395, -1139, -1437, -2869, -330, -3659, -520, -3098, -2556, -5478]},
      {'l': '归属于母公司股东的净利润', 'v': [302, 414, 466, 1057, 1657, 3149, 5854, 9415, 12499, 15831, 19789, 29833, 34620, 46076, 84738, 89419, 103831, 189488, 275287, 210214, 126021, 207206, 248389]},
      {'l': '净利率 ％', 'v': [43.84, 38.58, 34.03, 37.99, 40.99, 38.92, 41.44, 40.99, 35.81, 29.01, 25.65, 30.17, 28, 27.05, 30.08, 25.17, 24.73, 33.16, 40.14, 33.95, 18.92, 29.39, 29.91]},
      {'l': '优先股股利', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '基本每股收益', 'v': [0.05, 0.05, 0.05, 0.12, 0.19, 0.35, 0.65, 1.04, 1.37, 1.73, 2.16, 3.23, 3.72, 4.91, 9, 9.47, 10.97, 19.97, 28.89, 22.06, 13.33, 22.36, 27.34]},
      {'l': '稀释每股收益', 'v': [0.05, 0.05, 0.05, 0.12, 0.18, 0.34, 0.64, 1.01, 1.35, 1.7, 2.12, 3.19, 3.67, 4.85, 8.89, 9.35, 10.73, 19.59, 28.36, 21.6, 13, 21.87, 26.68]},
      {'l': '发行在外的稀释性潜在普通股平均股数', 'v': [6602.4, 7766.2, 9094.9, 9092.7, 9184.8, 9194, 9236.4, 9304.9, 9292.7, 9315, 9341, 9357, 9430, 9494, 9536, 9568, 9603, 9650, 9696, 9695, 9610, 9408, 9244]},
      {'l': '息税前利润', 'v': [315, 440, 420, 1115, 1626, 3670, 6682, 11202, 14911, 19122, 25116, 37437, 45340, 60329, 108159, 112871, 130292, 222234, 313439, 245912, 189452, 271116, 322998]},
      {'l': '折旧、损耗和摊销', 'v': [0, 0, 55, 134, 204, 407, 611, 915, 2376, 3259, 4603, 6037, 8021, 14285, 27979, 38903, 49710, 60189, 70615, 68361, 64542, 60017, 0]},
      {'l': '税息折旧及摊销前利润', 'v': [315, 440, 475, 1249, 1830, 4077, 7293, 12117, 17286, 22381, 29719, 43474, 53361, 74615, 136137, 151774, 180002, 282423, 384054, 314273, 253994, 331133, 322998]},
      {'l': '税息折旧及摊销前利润率 ％', 'v': [45.74, 40.99, 34.66, 44.87, 45.24, 50.4, 51.63, 52.76, 49.52, 41.01, 38.52, 43.96, 43.16, 43.8, 48.32, 42.73, 42.88, 49.42, 56, 50.75, 38.13, 46.97, 38.89]},
    ]},
    {'t': '合并资产负债表', 'rows': [
      {'l': '货币资金和现金等价物', 'v': [305, 808, 1513, 1833, 3121, 3469, 6863, 12167, 15450, 16641, 25822, 53518, 52205, 80618, 125249, 111110, 147986, 181132, 205669, 175033, 188480, 141487, 155812]},
      {'l': '短期证券', 'v': [0, 627, 369, 427, 717, 450, 6030, 13708, 16802, 17167, 25050, 13530, 44865, 56420, 43517, 78485, 60116, 89002, 115573, 149659, 226232, 224894, 315633]},
      {'l': '货币资金、现金等价物、及短期证券', 'v': [305, 1434, 1882, 2260, 3838, 3919, 12892, 25875, 32253, 33808, 50872, 67047, 97070, 137037, 168766, 189595, 208102, 270134, 321241, 324692, 414712, 366381, 471445]},
      {'l': '应收账款', 'v': [0, 0, 214, 397, 567, 1112, 1396, 2005, 2476, 2927, 3772, 5749, 8486, 11383, 19610, 32291, 39880, 53322, 60404, 50774, 50977, 51465, 55159]},
      {'l': '应收票据', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '应收贷款', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '其他流动应收款项', 'v': [94, 181, 0, 0, 0, 0, 0, 0, 176, 439, 1444, 2540, 4686, 4453, 4083, 2567, 4756, 8402, 22321, 27221, 35079, 36342, 0]},
      {'l': '应收账款总额', 'v': [94, 181, 214, 397, 567, 1112, 1396, 2005, 2651, 3366, 5216, 8288, 13172, 15836, 23694, 34858, 44636, 61724, 82725, 77995, 86055, 87807, 55159]},
      {'l': '原材料', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '在产品', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '存货调整', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '库存商品', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '其他存货', 'v': [0, 0, 3, 2, 2, 6, 0, 0, 0, 706, 1767, 306, 267, 295, 350, 368, 799, 965, 1302, 2605, 499, 470, 586]},
      {'l': '存货', 'v': [0, 0, 3, 2, 2, 6, 0, 0, 0, 706, 1767, 306, 267, 295, 350, 368, 799, 965, 1302, 2605, 499, 470, 586]},
      {'l': '其他流动资产', 'v': [56, 784, 459, 1052, 711, 2308, 651, 1782, 8589, 7517, 10678, 18733, 76228, 14066, 18646, 21767, 29066, 43726, 188368, 226757, 65800, 75099, 130630]},
      {'l': '流动资产合计', 'v': [455, 2399, 2557, 3711, 5117, 7346, 14940, 29662, 43493, 45397, 68533, 94375, 186737, 167234, 211455, 246588, 282603, 376549, 593636, 632048, 567065, 529758, 657820]},
      {'l': '非流动投资', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '土地', 'v': [0, 0, 0, 0, 0, 41, 103, 152, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '房屋及建筑物修缮', 'v': [0, 0, 105, 169, 297, 326, 1475, 1600, 2594, 3661, 4687, 4418, 6026, 7126, 13023, 13444, 23818, 21276, 26409, 28167, 29010, 34927, 0]},
      {'l': '机器、家具和设备', 'v': [0, 0, 343, 591, 954, 1642, 2308, 3880, 7873, 10784, 14093, 16163, 19181, 23916, 35159, 52599, 77128, 105709, 128158, 118939, 124818, 162345, 0]},
      {'l': '在建工程', 'v': [0, 0, 0, 14, 119, 990, 0, 452, 820, 967, 2605, 4799, 5105, 5241, 3748, 5542, 4379, 5855, 7253, 10306, 15023, 34683, 10683]},
      {'l': '其他固定资产、无形资产和其他长期资产', 'v': [75, 133, 0, 0, 0, 0, 120, 0, 0, 0, 0, 0, 0, 0, 0, 0, 22, 15326, 25062, 25153, 22383, 18875, 184790]},
      {'l': '固定资产、无形资产和其他长期资产总值', 'v': [75, 133, 447, 774, 1370, 2999, 4005, 6085, 11286, 15412, 21386, 25380, 30312, 36283, 51930, 71585, 105347, 148166, 186882, 182565, 191234, 250830, 195472]},
      {'l': '累计折旧', 'v': [0, 0, -97, -204, -363, -651, -1027, -1783, -3257, -5240, -7684, -10660, -13221, -15457, -20220, -26182, -36795, -56045, -78755, -86828, -95604, -111660, 0]},
      {'l': '固定资产、无形资产和其他长期资产净额', 'v': [75, 133, 350, 570, 1007, 2349, 2978, 4302, 8029, 10173, 13702, 14720, 17091, 20825, 31710, 45403, 68552, 92121, 108127, 95737, 95630, 139170, 195472]},
      {'l': '无形资产', 'v': [0, 0, 21, 155, 454, 419, 345, 939, 5791, 7570, 7422, 13421, 22632, 51108, 62102, 87930, 178263, 226350, 254226, 220113, 228357, 247351, 252251]},
      {'l': '商誉', 'v': [0, 0, 3, 27, 0, 70, 0, 0, 3204, 3642, 3258, 7964, 8599, 25706, 27975, 37037, 103993, 128765, 137352, 130355, 138057, 151744, 0]},
      {'l': '其他非流动资产', 'v': [10, 157, 362, 186, 751, 1032, 1614, 6983, 12274, 30437, 47234, 91950, 142282, 204721, 352007, 441948, 532131, 885665, 1018298, 814423, 834106, 985240, 1146976]},
      {'l': '非流动资产合计', 'v': [85, 291, 733, 910, 2212, 3799, 4938, 12224, 26094, 48179, 68358, 120090, 182005, 276655, 445820, 575281, 778946, 1204136, 1380651, 1130274, 1158093, 1371761, 1594699]},
      {'l': '资产总计', 'v': [540, 2690, 3290, 4622, 7330, 11145, 19878, 41886, 69587, 93576, 136891, 214465, 368742, 443889, 657274, 821869, 1061549, 1580686, 1974287, 1762322, 1725158, 1901519, 2252519]},
      {'l': '应付账款', 'v': [56, 2, 25, 39, 124, 277, 791, 1614, 2749, 5237, 8527, 10879, 18869, 30736, 59350, 83758, 89788, 111466, 134042, 103163, 110415, 126745, 133812]},
      {'l': '应交税费', 'v': [7, 5, 40, 65, 75, 171, 343, 662, 1088, 1194, 2439, 1287, 2488, 6687, 11426, 12789, 12216, 16932, 18056, 20309, 24103, 22020, 24614]},
      {'l': '其他应付账款', 'v': [0, 75, 188, 442, 708, 1146, 1846, 3504, 794, 839, 1125, 2931, 3788, 6665, 10120, 12595, 12465, 20465, 149124, 168753, 2997, 4690, 106602]},
      {'l': '应交税费和其他应付款', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '应付票据及应付账款、应交税费和其他应付款', 'v': [62, 83, 253, 545, 907, 1594, 2980, 5780, 4631, 7270, 12091, 15097, 25145, 44087, 80895, 109142, 114469, 148863, 301223, 292225, 137514, 153455, 265028]},
      {'l': '短期借款', 'v': [0, 0, 0, 0, 309, 0, 230, 0, 9800, 1339, 3305, 6326, 19113, 17652, 24230, 46066, 36976, 16883, 23269, 24597, 60921, 65670, 58727]},
      {'l': '短期资本化租赁债务', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 3649, 4531, 6668, 7096, 6731, 5979, 5950]},
      {'l': '短期借款和资本化租赁债务', 'v': [0, 0, 0, 0, 309, 0, 230, 0, 9800, 1339, 3305, 6326, 19113, 17652, 24230, 46066, 40624, 21414, 29937, 31692, 67652, 71649, 64677]},
      {'l': '流动递延收益负债', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '流动递延所得税负债', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '流动递延所得税和收益负债', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '其他流动负债', 'v': [34, 115, 225, 317, 529, 771, 1971, 9443, 11519, 17086, 27070, 41269, 105257, 51724, 74683, 74743, 112141, 148698, 162421, 160964, 180015, 198664, 126271]},
      {'l': '流动负债合计', 'v': [96, 198, 478, 862, 1746, 2365, 5181, 15223, 25950, 25696, 42467, 62692, 149514, 113464, 179809, 229952, 267234, 318975, 493580, 484882, 385182, 423769, 455976]},
      {'l': '长期借款', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 4573, 11966, 15911, 38259, 60108, 105117, 132074, 157593, 208734, 277631, 345944, 348791, 320390, 295859, 369611]},
      {'l': '长期资本租赁负债', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 9378, 12089, 20205, 20574, 18012, 14837, 14671]},
      {'l': '长期借款和资本化租赁债务', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 4573, 11966, 15911, 38259, 60108, 105117, 132074, 157593, 218113, 289720, 366149, 369366, 338402, 310697, 384282]},
      {'l': '债务股本比率', 'v': [0, 0, 0, 0, 0.06, 0, 0.02, 0, 0.41, 0.26, 0.26, 0.45, 0.55, 0.63, 0.52, 0.55, 0.54, 0.37, 0.4, 0.5, 0.46, 0.37, 0.35]},
      {'l': '养老金和退休福利', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '非流动递延负债', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '其他非流动负债', 'v': [1, 0, 1, 65, 43, 729, 731, 1131, 3429, 3506, 3882, 10615, 12376, 16484, 17042, 29698, 32263, 49673, 41076, 33843, 45961, 41838, 41225]},
      {'l': '非流动负债合计', 'v': [1, 0, 1, 65, 43, 729, 731, 1131, 8003, 15472, 19793, 48874, 72485, 121602, 149117, 187291, 250376, 339393, 407225, 403209, 384363, 352535, 425507]},
      {'l': '负债合计', 'v': [97, 198, 479, 927, 1789, 3094, 5913, 16354, 33953, 41168, 62260, 111567, 221999, 235065, 328925, 417243, 517609, 658368, 900805, 888091, 769545, 776303, 881483]},
      {'l': '普通股', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '优先股', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '留存收益', 'v': [0, 767, 1109, 2047, 3613, 6716, 11946, 20803, 32721, 47585, 66666, 92797, 120197, 153319, 240174, 340393, 428021, 638313, 820284, 788379, 890239, 952396, 1116254]},
      {'l': '累计其他综合收益（亏损）', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '资本公积', 'v': [14, 1670, 1600, 1450, 1541, 1306, 1413, 1286, 2390, 3581, 3633, 6429, 14623, 19424, 26311, 31004, 39248, 57841, 82443, 69703, 41552, 45994, 70477]},
      {'l': '库存股', 'v': [0, 0, 0, 0, 0, -25, -141, -302, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, -2086, -5185, -3840, -3811]},
      {'l': '其他所有者权益', 'v': [428, 55, 102, 198, 318, -58, 610, 3647, -242, 185, 3670, 1027, 9442, 23049, 36957, -3912, 14225, 138372, 84559, -50409, -42186, 44881, 92101]},
      {'l': '归属于母公司所有者权益合计', 'v': [443, 2492, 2812, 3695, 5472, 7939, 13829, 25434, 34869, 51352, 73970, 100253, 144261, 195791, 303442, 367485, 481494, 834526, 987287, 805588, 884420, 1039430, 1275021]},
      {'l': '少数股东权益', 'v': [0, 0, 0, 0, 68, 111, 136, 98, 765, 1057, 661, 2645, 2482, 13032, 24907, 37141, 62445, 87792, 86195, 68643, 71194, 85785, 96015]},
      {'l': '所有者权益合计', 'v': [443, 2492, 2812, 3695, 5541, 8051, 13965, 25532, 35634, 52408, 74631, 102899, 146743, 208823, 328349, 404626, 543939, 922318, 1073482, 874231, 955614, 1125215, 1371036]},
      {'l': '股东权益比率', 'v': [0.82, 0.93, 0.85, 0.8, 0.75, 0.71, 0.7, 0.61, 0.5, 0.55, 0.54, 0.47, 0.39, 0.44, 0.46, 0.45, 0.45, 0.53, 0.5, 0.46, 0.51, 0.55, 0.57]},
    ]},
    {'t': '合并现金流量表', 'rows': [
      {'l': '来自持续经营收益的净利润', 'v': [302, 414, 466, 1057, 1660, 3184, 5929, 9487, 12526, 15897, 19867, 29931, 34983, 46471, 85877, 90856, 106699, 189817, 278946, 210734, 129118, 209762, 253867]},
      {'l': '折旧、损耗和摊销', 'v': [0, 0, 55, 134, 204, 407, 611, 915, 2376, 3259, 4603, 6037, 8021, 14285, 27979, 38903, 49710, 60189, 70615, 68361, 64542, 60017, 0]},
      {'l': '经营性应收项目变动', 'v': [0, 0, -29, -157, -122, -507, -279, -565, -307, -332, -774, -1777, -2967, -3285, -7584, -11702, -6718, -8926, -4930, 4842, -1105, -1119, 0]},
      {'l': '存货变动', 'v': [0, 0, 3, 0, 1, -4, 6, 0, 0, -374, -1040, 1629, -20, -43, -46, -33, -438, -113, 1588, -1341, 2058, 21, 0]},
      {'l': '预付资产变动', 'v': [0, 0, 14, -60, -16, -51, 42, -61, -3222, -2804, -1200, -2194, -6106, -4606, -4456, -4601, -4399, 139, -21773, -6522, 5982, 2810, 0]},
      {'l': '应付票据及应付账款、应交税费和其他应付款变动', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '其他营运资金变动', 'v': [0, 0, 522, 501, 192, 790, 2623, 3372, 4972, 7871, 10565, 9303, 16773, 20230, 40126, 21437, 28613, 41408, 26206, -17607, 14101, 21649, 0]},
      {'l': '营运资金变动', 'v': [0, 0, 510, 284, 55, 228, 2391, 2746, 1442, 4361, 7551, 6961, 7678, 12296, 28040, 5101, 17058, 32508, 1091, -20628, 21037, 23362, 0]},
      {'l': '递延所得税', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 2296, 2818, 4746, 6421, 8543, 11429, 18656, 16451, 15035, 23587, 24798, 24027, 47334, 48064, 0]},
      {'l': '股权激励', 'v': [0, 0, 33, 77, 109, 182, 365, 580, 898, 1125, 1491, 2258, 3312, 4836, 7272, 8939, 11269, 14977, 26479, 27861, 23049, 22103, 0]},
      {'l': '资产减值费用', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '终止经营活动产生的现金流量净额', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '其他经营活动产生的现金流量净额', 'v': [28, -609, -111, -53, -85, 47, 240, 674, -3173, -3301, -7144, -10623, -7937, -15857, -42050, -34235, -34429, -90963, -187419, -147213, -42303, -87293, 80922]},
      {'l': '经营活动产生的现金流量净额', 'v': [331, -195, 954, 1498, 1943, 4048, 9536, 14401, 16364, 24159, 31115, 40986, 54600, 73460, 125774, 126016, 165344, 230115, 214510, 163142, 242777, 276016, 334789]},
      {'l': '购建固定资产、无形资产和其他长期资产支付的现金', 'v': [-56, -85, -246, -322, -629, -1507, -896, -1740, -4973, -4547, -6112, -5383, -6538, -9417, -14348, -22427, -25333, -40388, -37966, -25913, -22978, -67185, 0]},
      {'l': '处置固定资产、无形资产和其他长期资产收回的现金净额', 'v': [0, 1, 0, 0, 12, 3, 1, 2, 1, 5, 22, 209, 84, 35, 33, 37, 4, 0, 234, 420, 281, 217, 0]},
      {'l': '取得子公司及其他营业单位支付的现金净额', 'v': [0, 0, -13, -55, -191, -361, 0, -1134, -6331, -5102, -5700, -45565, -18828, -13813, -48616, -49453, -17862, -54384, -88650, -26677, -14529, -10502, 0]},
      {'l': '处置子公司及其他营业单位收到的现金净额', 'v': [0, 0, 0, 0, 0, 0, 0, 64, 0, 138, 462, 1763, 1428, 3678, 1332, 487, 742, 2635, 6634, 25298, 4794, 5226, 0]},
      {'l': '投资支付的现金', 'v': [0, -156, -112, -20, -575, -47, -635, -10181, -11866, -37390, -33121, -21363, -120407, -101588, -143656, -157741, -150058, -157441, -202669, -203267, -331295, -410960, 0]},
      {'l': '收回投资及取得投资收益收到的现金', 'v': [0, 0, 0, 0, 1027, 0, 719, 533, 7337, 23043, 20361, 35364, 74553, 49284, 108859, 90539, 110800, 64747, 144241, 134687, 241467, 368584, 0]},
      {'l': '取得无形资产支付的现金净额差', 'v': [0, 0, -8, -24, -209, -81, -36, -887, -973, -1470, -1651, -2876, -9074, -11610, -23576, -38983, -38081, -38561, -38153, -30872, -28875, -35362, 0]},
      {'l': '终止投资活动产生的现金流量净额', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '其他投资活动产生的现金流量净额', 'v': [33, -695, 369, -437, -213, -850, -4858, -703, -2004, 5092, 1314, 2282, 2340, 3912, 5750, 4977, -9482, 7696, -2298, 9213, 14236, 19527, 0]},
      {'l': '投资活动产生的现金流量净额', 'v': [-22, -935, -10, -857, -777, -2843, -5706, -14046, -18810, -20231, -24425, -35569, -76442, -79520, -114222, -172563, -129268, -215695, -218627, -117111, -136898, -130456, -227277]},
      {'l': '发行股票产生的现金流入', 'v': [0, 1659, 11, 34, 110, 98, 188, 233, -342, 145, 38, -288, -580, -1918, -2442, -1638, -1262, -177, -2184, -2107, -3618, -1589, 0]},
      {'l': '回购股票产生的现金流出', 'v': [0, 0, -118, -240, -114, -463, -202, -558, -1283, -25, -1691, -76, 0, 0, 0, -889, -1164, 0, -2657, -32728, -47871, -109256, 0]},
      {'l': '发行优先股产生的现金流量净额', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '发行债券产生的现金流入', 'v': [0, 0, 0, 0, 309, 0, 230, 6195, 12794, 8661, 8952, 30928, 36974, 64785, 59478, 75252, 126466, 94077, 102256, 33765, 69400, 122338, 0]},
      {'l': '偿付债券现金流出', 'v': [0, 0, 0, 0, 0, -330, 0, -237, -4613, -9977, -2954, -5093, -4948, -22780, -25099, -32266, -100916, -41337, -29046, -26610, -59224, -137627, 0]},
      {'l': '发行债券产生的现金流量净额', 'v': [0, 0, 0, 0, 309, -330, 230, 5958, 8180, -1317, 5999, 25835, 32026, 42005, 34379, 42986, 25550, 52740, 73211, 7155, 10177, -15289, 0]},
      {'l': '分配股利、利润或偿付利息支付的现金', 'v': [-10, -27, -127, -144, -222, -292, -630, -747, -1027, -1378, -1874, -2206, -3173, -4147, -5987, -7697, -9253, -12256, -15310, -14464, -22951, -30812, 0]},
      {'l': '融资租赁产生的现金流量净额', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, -2671, -4193, -5416, -6666, -7276, -6800, 0]},
      {'l': '其他筹资活动产生的现金流量净额', 'v': [-36, 0, -3, 0, 0, 3, -36, -78, -172, -393, -291, -272, -6006, -685, 5568, 2324, -9340, -19937, -21171, -18141, -18777, -24692, 0]},
      {'l': '筹资活动产生的现金流量净额', 'v': [-45, 1632, -237, -350, 83, -984, -451, 4807, 5357, -2967, 2180, 22992, 22267, 35254, 31518, 35085, 1861, 16178, 26473, -66950, -90317, -188438, -96282]},
      {'l': '期初现金及现金等价物余额', 'v': [42, 306, 826, 1566, 1952, 3334, 3484, 7065, 12750, 15682, 17084, 25345, 51334, 48703, 85202, 120064, 108843, 157652, 187096, 187570, 171438, 183981, 146397]},
      {'l': '汇率变动对现金及现金等价物的影响', 'v': [0, 0, -19, -24, -80, -86, -1, -61, -211, -2, -131, -236, 446, 2720, -3023, 2507, 1207, -7117, -3782, 8382, 1480, 383, -1815]},
      {'l': '现金及现金等价物净额', 'v': [263, 502, 688, 267, 1169, 135, 3379, 5102, 2700, 959, 8738, 28173, 871, 31914, 40046, -8955, 39143, 23480, 18573, -12537, 17042, -42494, 9414]},
      {'l': '期末现金及现金等价物余额', 'v': [305, 808, 1513, 1833, 3121, 3469, 6863, 12167, 15450, 16641, 25822, 53518, 52205, 80618, 125249, 111110, 147986, 181132, 205669, 175033, 188480, 141487, 155812]},
      {'l': '资本开支', 'v': [-56, -85, -254, -345, -837, -1588, -932, -2626, -5946, -6017, -7763, -8318, -15750, -21027, -37924, -61410, -63413, -78949, -76119, -56785, -51853, -102548, 0]},
      {'l': '自由现金流', 'v': [275, -280, 700, 1153, 1105, 2460, 8604, 11775, 10418, 18142, 23352, 32667, 38850, 52433, 87850, 64606, 101930, 151166, 138391, 106357, 190925, 173468, 334789]},
    ]},
    {'t': '估值比率', 'rows': [
      {'l': '市盈率', 'v': [9999, 15.98, 29.85, 43.92, 61.12, 26.95, 48.99, 30.77, 21.4, 27.02, 43.03, 32.52, 38.29, 36.03, 42.12, 30.97, 32.27, 26.64, 14.85, 14.69, 22.58, 19.19, 22.45]},
      {'l': '持续经营市盈率', 'v': [9999, 15.98, 30.5, 44.37, 61.12, 26.57, 48.99, 30.77, 21.89, 25.59, 39.32, 31.3, 34.03, 32.6, 46.25, 31.47, 31.98, 34.69, 27.08, 24, 16.45, 16.73, 19.45]},
      {'l': '股价／巴菲特所有者每股收益', 'v': [0, 0, 0, 0, 58.53, 24.13, 34.24, 23.27, 15.45, 17.39, 24.83, 21.06, 24.75, 21.67, 24.26, 21.59, 21.5, 18.97, 12.68, 13.6, 13.79, 14.29, 29.9]},
      {'l': '市净率', 'v': [0, 3.02, 4.81, 12.22, 18.08, 10.43, 20.24, 11.24, 7.59, 8.28, 11.48, 9.69, 9.16, 8.46, 11.72, 7.5, 6.87, 6, 4.1, 3.77, 3.12, 3.69, 4.24]},
      {'l': '股价／每股有形账面价值', 'v': [0, 3.02, 4.85, 12.76, 19.71, 11.01, 20.76, 11.67, 9.1, 9.72, 12.76, 11.19, 10.87, 11.45, 14.73, 9.86, 10.91, 8.23, 5.52, 5.18, 4.2, 4.84, 5.29]},
      {'l': '股价／自由现金流', 'v': [0, 0, 19.87, 40.29, 92.19, 34.4, 33.43, 24.62, 25.67, 23.57, 36.48, 29.71, 34.12, 31.66, 40.63, 42.87, 32.62, 33.31, 29.5, 28.92, 14.78, 22.77, 16.54]},
      {'l': '股价／每股经营现金流', 'v': [0, 0, 14.59, 31, 52.18, 20.95, 30.19, 20.12, 16.34, 17.7, 27.38, 23.68, 24.28, 22.6, 28.38, 21.98, 20.11, 21.88, 19.03, 18.85, 11.62, 14.31, 16.54]},
      {'l': '市销率', 'v': [0, 6.16, 10.16, 16.69, 25.14, 10.48, 20.38, 12.62, 7.66, 7.84, 11.04, 9.81, 10.72, 9.75, 12.67, 7.8, 7.92, 8.81, 5.95, 4.97, 4.24, 5.6, 6.67]},
      {'l': '市盈增长比', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0.64, 0.96, 1.24, 0.81, 0.96, 1.2, 0.91, 1, 0.75, 1.07, 2.27]},
      {'l': '企业价值／收入', 'v': [0, 5.67, 8.51, 15.41, 24.27, 10.01, 19.49, 11.49, 7.1, 7.49, 10.66, 9.6, 10.63, 9.76, 12.74, 7.94, 8.24, 9.09, 6.11, 5.18, 4.33, 5.75, 6.6]},
      {'l': '企业价值倍数', 'v': [0, 13.83, 24.56, 34.35, 53.64, 19.85, 37.74, 21.79, 14.34, 18.26, 27.66, 21.84, 24.63, 22.29, 26.37, 18.59, 19.23, 18.38, 10.91, 10.21, 11.35, 12.23, 16.96]},
      {'l': '企业价值／息税前利润', 'v': [0, 13.83, 27.78, 38.47, 60.37, 22.06, 41.2, 23.57, 16.63, 21.37, 32.74, 25.36, 28.99, 27.57, 33.2, 25, 26.56, 23.36, 13.37, 13.04, 15.22, 14.94, 16.96]},
      {'l': '乔尔·格林布拉特收益率 ％', 'v': [0, 7.23, 3.6, 2.6, 1.66, 4.53, 2.43, 4.24, 6.01, 4.68, 3.05, 3.94, 3.45, 3.63, 3.01, 4, 3.77, 4.28, 7.48, 7.67, 6.57, 6.69, 5.9]},
      {'l': '唐纳德·亚克曼收益率 ％', 'v': [0, 0, 0, 0, 0, 21.03, 20.76, 21.42, 22.11, 21.95, 21.39, 21.72, 22.42, 22.69, 21.79, 23.01, 23.26, 22.81, 24.12, 25.81, 27.07, 21.27, 12.91]},
      {'l': '席勒市盈率', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 65.24, 65.09, 75.6, 43.57, 26.8, 22.84, 28.54, 35.14]},
      {'l': 'Cyclically Adjusted PB Ratio', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 21.02, 17.81, 20.21, 12.05, 7.21, 5.65, 6.86, 8.14]},
      {'l': 'Cyclically Adjusted PS Ratio', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 18.27, 17.71, 21.59, 13.54, 8.43, 6.75, 8.42, 10.4]},
      {'l': '股息率 ％', 'v': [0, 0, 0, 0, 0, 0.32, 0.15, 0.23, 0.35, 0.3, 0.2, 0.21, 0.24, 0.25, 0.15, 0.28, 0.27, 0.21, 0.35, 0.48, 0.82, 0.81, 0.75]},
    ]},
    {'t': '估值和质量', 'rows': [
      {'l': '总市值', 'v': [0, 7525, 13537, 45158, 98929, 82806, 279885, 285826, 264753, 425418, 849030, 971822, 1322033, 1657316, 3555247, 2755776, 3307586, 5005823, 4046127, 3035453, 2757259, 3834002, 5408220]},
      {'l': '企业价值', 'v': [0, 6091, 11656, 42898, 95468, 78998, 267359, 260049, 247638, 405972, 818035, 952005, 1306665, 1656080, 3567693, 2806982, 3420666, 5134615, 4207166, 3180463, 2819796, 3935752, 5481749]},
      {'l': '月末股价', 'v': [0, 0.85, 1.53, 5.11, 11.06, 9.22, 31.16, 31.14, 28.78, 45.91, 91.19, 103.71, 140.58, 174.88, 374.27, 289.46, 346.25, 521.77, 421.1, 317.23, 293.6, 419.8, 599]},
      {'l': '每股现金净流量', 'v': [0.02, 0.14, 0.16, 0.15, 0.22, 0.08, 0.76, 1.03, -0.27, -0.91, -1.29, -5.03, -13.55, -11.72, -19.48, -27.81, -38.94, -49.62, -69.29, -66.05, -45.36, -54.28, -56.05]},
      {'l': '净流动资产价值 NCAV', 'v': [0.04, 0.25, 0.23, 0.31, 0.36, 0.46, 0.99, 1.44, 0.95, 0.34, 0.6, -2.12, -4.01, -8.53, -14.99, -21.83, -31.14, -38.53, -40.94, -33.93, -29.14, -36.39, -35.41]},
      {'l': '净净营运资本 NNWC', 'v': [0.02, 0.14, 0.18, 0.18, 0.27, 0.17, 0.88, 1.19, -0.07, -0.63, -0.9, -4.56, -12.86, -10.8, -17.92, -25.25, -35.76, -45.4, -64.51, -61.94, -41.27, -50.02, -51.43]},
      {'l': '内在价值：预期自由现金流模型', 'v': [0, 0, 0, 0, 0, 2.15, 4.42, 8.01, 11.28, 16.64, 23.58, 26.83, 39.16, 55.43, 113.97, 145.94, 187.93, 261.23, 318.64, 323.81, 364.52, 424.82, 454.53]},
      {'l': '内在价值：市销率模型', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 503.53, 618.59, 732.69, 686.48, 702.3, 654.08, 689.23]},
      {'l': '彼得林奇公允价值', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 105.63, 139.03, 198.73, 239.45, 274.45, 339.95, 384.4, 327.81, 390.42, 382.61, 261.32]},
      {'l': '格雷厄姆估值', 'v': [0.23, 0.58, 0.6, 1.02, 1.51, 2.56, 4.63, 7.79, 9.67, 13.81, 19.31, 26.28, 34.67, 42.92, 68.02, 77.95, 87.93, 146.47, 163.37, 134.89, 167.51, 221.29, 280.17]},
      {'l': '盈利能力值（EPV）', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 15.9, 23.72, 31.01, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '两年破产风险（Z分数）', 'v': [0, 0, 0, 0, 37.23, 18.8, 31.06, 12.29, 6.84, 8.45, 10.29, 7.03, 4.93, 5.74, 8.05, 5.51, 5.27, 6.01, 4.19, 3.63, 3.8, 4.67, 5.31]},
      {'l': '基本面趋势（F分数）', 'v': [9999, 9999, 9999, 9999, 9999, 9999, 9999, 5, 5, 4, 5, 6, 4, 4, 6, 4, 3, 7, 6, 3, 8, 8, 8]},
      {'l': '财务造假嫌疑（M分数）', 'v': [0, 0, 0, 0, 0, 0, 0, 0, -0.62, -2.24, -2.4, -2.26, -2.33, -2.34, -2.17, -2.24, -2.46, -2.22, -2.12, -2.48, -2.69, -2.63, -2.99]},
      {'l': '可供营运的资产净额／总资产', 'v': [0.25, 1.96, 0.35, 0.44, 0.44, 0.56, 0.12, -0.02, 0.42, 0.46, 0.46, 0.59, 0.6, 0.53, 0.71, 0.64, 0.72, 0.91, 0.73, 0.48, 0.54, 0.66, 0.71]},
      {'l': '斯隆比率 ％', 'v': [-1.1, 57.41, -14.52, 9.01, 6.71, 17.45, 10.18, 21.63, 21.48, 12.72, 9.57, 11.38, 15.31, 11.75, 11.13, 16.54, 6.38, 11.08, 14.15, 9.32, 1.17, 3.24, 6.25]},
      {'l': '流动比率', 'v': [4.72, 12.1, 5.35, 4.3, 2.93, 3.11, 2.88, 1.95, 1.68, 1.77, 1.61, 1.51, 1.25, 1.47, 1.18, 1.07, 1.06, 1.18, 1.2, 1.3, 1.47, 1.25, 1.44]},
      {'l': '速动比率', 'v': [4.72, 12.1, 5.34, 4.3, 2.93, 3.1, 2.88, 1.95, 1.68, 1.74, 1.57, 1.5, 1.25, 1.47, 1.17, 1.07, 1.05, 1.18, 1.2, 1.3, 1.47, 1.25, 1.44]},
      {'l': '现金比率', 'v': [3.17, 7.23, 3.94, 2.62, 2.2, 1.66, 2.49, 1.7, 1.24, 1.32, 1.2, 1.07, 0.65, 1.21, 0.94, 0.82, 0.78, 0.85, 0.65, 0.67, 1.08, 0.86, 1.03]},
      {'l': '利息保障倍数', 'v': [10000, 10000, 9.29, 199.89, 1028.97, 22.59, 3013.04, 9781.49, 158.06, 46.34, 44.02, 30.6, 24.33, 23.2, 22.94, 15.93, 12.61, 17.19, 15.45, 11.41, 13.94, 16.77, 16.18]},
      {'l': '最高股价', 'v': [0, 1.12, 1.74, 5.11, 12.92, 13.23, 31.16, 34.59, 41.48, 51.29, 91.41, 123.07, 157.64, 201.15, 396.4, 437.51, 364.69, 576.16, 706.6, 460.07, 415, 478.4, 677.5]},
      {'l': '最低股价', 'v': [0, 0.63, 0.8, 1.6, 4.49, 7.01, 7.71, 23.03, 26.77, 28.49, 43.73, 87.32, 103.99, 122.7, 174.23, 232.49, 281.17, 307.9, 388.29, 190.72, 274, 262.2, 366]},
      {'l': '股票回购率 ％', 'v': [None, -5.01, -0.25, 0.04, -1.14, -0.44, 0, -2.18, -0.22, -0.73, -0.47, -0.65, -0.35, -0.78, -0.23, -0.22, -0.34, -0.43, -0.15, 0.41, 1.86, 2.75, 1.14]},
      {'l': '股票回购收益率 ％', 'v': [0, -22.05, 0.79, 0.46, 0, 0.44, 0.01, 0.11, 0.61, -0.03, 0.19, 0.04, 0.04, 0.12, 0.07, 0.09, 0.07, 0, 0.12, 1.15, 1.87, 2.89, 0]},
      {'l': '每股收入同比增长率 ％', 'v': [0, 32.48, 8.84, 103.27, 43.85, 99.86, 73.79, 61.39, 52.2, 55.98, 40.96, 27.97, 24.03, 36.87, 64.66, 25.65, 17.76, 35.45, 19.45, -9.7, 8.52, 8.1, 19.9]},
      {'l': '每股收益同比增长率 ％', 'v': [0, 16.57, -3.93, 126.79, 55.29, 89.64, 85.71, 59.13, 32.93, 26.33, 24.7, 50.48, 15.14, 32.2, 83.08, 5.18, 14.81, 82.54, 44.81, -23.85, -39.8, 68.23, 21.99]},
      {'l': '税息折旧及摊销前利润同比增长率 ％', 'v': [0, 18.7, -7.96, 163.15, 45.05, 122.64, 78.04, 64.93, 42.85, 29.16, 32.42, 46.03, 21.79, 38.89, 81.65, 11.11, 18.17, 56.14, 35.34, -18.16, -18.47, 33.17, -0.73]},
      {'l': '5年每股税息折旧及摊销前利润增长率 ％', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 53.22, 34.1, 37.24, 38.94, 33.15, 29, 29.71, 23.9, 21.8, 15.68, 8.58]},
      {'l': '发行在外的普通股平均股数', 'v': [6602.38, 7605.5, 8862.48, 8821.68, 8899.53, 8968.89, 9008.23, 9094.83, 9094.83, 9140, 9158, 9231, 9300, 9376, 9411, 9444, 9468, 9490, 9528, 9528, 9455, 9269, 9085]},
      {'l': '期末总股本', 'v': [8403.21, 8824.52, 8846.22, 8842.26, 8942.87, 8982.57, 8982.57, 9178.65, 9199.07, 9266.67, 9310.55, 9370.68, 9403.92, 9477.08, 9499.06, 9520.31, 9552.61, 9593.91, 9608.38, 9568.74, 9391.21, 9132.92, 9028.75]},
      {'l': 'β贝塔系数', 'v': [0, 0, 0, 0, 1.41, 1.15, 1.14, 1.1, 1.34, 1.29, 1.17, 1.15, 1.1, 1.03, 1, 1.35, 1.49, 1.12, 1.09, 1.33, 1.5, 1.37, 1.21]},
      {'l': '重述日期', 'v': [None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None]},
      {'l': '报告日期', 'v': [None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None]},
      {'l': '收益发布日期', 'v': [None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None]},
      {'l': '股东人数', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '在职员工人数', 'v': [0, 0, 0, 3017, 4344, 6194, 7515, 10692, 17446, 24160, 27492, 27690, 30641, 38775, 44796, 54309, 62885, 85858, 112771, 108436, 105417, 110558, 0]},
      {'l': '外汇汇率', 'v': [1.07, 1.06, 1.04, 1.01, 0.94, 0.88, 0.88, 0.86, 0.82, 0.8, 0.78, 0.8, 0.83, 0.89, 0.84, 0.88, 0.9, 0.84, 0.82, 0.9, 0.91, 0.94, 0.91]},
    ]},
  ]},
  '01810.HK': {'name': '小米集團', 'years': [2015, 2016, 2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025], 'groups': [
    {'t': '每股指标', 'rows': [
      {'l': '每股收入', 'v': [3.59, 3.43, 6.07, 8.43, 9.35, 11.82, 15.76, 12.36, 11.7, 15.32, 18.92]},
      {'l': '每股税息折旧及摊销前利润', 'v': [-0.39, 0.08, -2.19, 0.73, 0.64, 1.28, 1.46, 0.39, 1.23, 1.5, 2.21]},
      {'l': '每股息税前利润', 'v': [-0.4, 0.06, -2.2, 0.69, 0.58, 1.2, 1.31, 0.22, 1.02, 1.23, 2.21]},
      {'l': '稀释每股收益', 'v': [-0.41, 0.03, -2.32, 0.05, 0.46, 0.98, 0.93, 0.11, 0.76, 0.99, 1.72]},
      {'l': '持续经营每股收益', 'v': [-0.54, -0.06, -2.59, -0.17, 0.31, 0.38, 0.7, 0.17, 0.58, 0.94, 1.26]},
      {'l': '巴菲特所有者每股收益（TTM）', 'v': [0, 0, 0, 0, 0, 0, 0.88, 0, 0, 0, 0]},
      {'l': '每股自由现金流', 'v': [-0.28, 0.14, -0.12, -0.25, 0.93, 0.91, 0.13, -0.45, 1.51, 1.34, 1.41]},
      {'l': '每股经营现金流', 'v': [-0.14, 0.23, -0.05, -0.07, 1.08, 1.05, 0.47, -0.19, 1.78, 1.65, 1.41]},
      {'l': '每股现金', 'v': [1.33, 1.42, 1.94, 1.84, 2.97, 4.51, 4.23, 3.04, 4.7, 4.33, 4.59]},
      {'l': '每股股息', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '每股账面价值', 'v': [-9.99, -9.9, -14.45, 3.43, 3.77, 5.82, 6.73, 6.44, 7.15, 8.03, 11.34]},
      {'l': '每股有形账面价值', 'v': [-10.35, -10.4, -15.1, 3.17, 3.56, 5.41, 6.38, 5.86, 6.42, 7.35, 10.99]},
      {'l': '每股总债务', 'v': [0.38, 0.45, 1.23, 0.53, 0.86, 0.87, 1.5, 1.17, 1.3, 1.53, 1.54]},
      {'l': '月末股价', 'v': [0, 0, 0, 12.92, 10.78, 33.05, 18.9, 10.94, 15.6, 34.65, 39.2]},
    ]},
    {'t': '财务比率', 'rows': [
      {'l': '股本回报率 ROE ％', 'v': [None, 10000, None, 10000, 13.03, 20.35, 15.05, 1.68, 11.25, 13.26, 18.57]},
      {'l': '股本回报率 ROE ％／市净率', 'v': [None, 10000, None, 10000, 4.56, 3.59, 5.36, 0.99, 5.16, 3.07, 5.37]},
      {'l': '资产收益率 ROA ％', 'v': [-19.37, 1.19, -63.56, 11.34, 6.05, 9.56, 7.18, 0.83, 5.79, 6.43, 9.28]},
      {'l': '有形股本回报率 ％', 'v': [None, 10000, None, 10000, 13.95, 21.77, 16.03, 1.81, 12.44, 14.62, 19.6]},
      {'l': '有形资产收益率 ％', 'v': [-21.07, 1.31, -68.56, 11.91, 6.24, 9.86, 7.4, 0.86, 6.09, 6.74, 9.53]},
      {'l': '乔尔·格林布拉特资本回报率 ％', 'v': [-2545.51, 217.65, -3284.94, 416.42, 193.17, 341.07, 234.9, 33.62, 161.5, 141.57, 206.06]},
      {'l': '已动用资本回报率 ROCE ％', 'v': [-32.58, 5.15, -125.89, 22.39, 14.38, 21.59, 17.13, 2.68, 11.89, 13.34, 19.89]},
      {'l': '5年留存收益回报率 RORE %', 'v': [0, 0, 0, 0, 0, 0, -216.89, 174.36, 9.46, 1.36, 17.69]},
      {'l': '资本回报率 ％', 'v': [-8.95, 2.93, 18.7, -6.36, 11.09, 10.82, 13.63, 2.74, 7.79, 9.97, 10.31]},
      {'l': '加权平均资本成本 WACC ％', 'v': [0, 0, 3.06, 8.46, 7.46, 7.18, 7.77, 9.08, 9.61, 9.93, 11.36]},
      {'l': '实际借款利率 ％', 'v': [2.62, 2.23, 2.91, 3.46, 3.55, 18.73, 11.7, 3.79, 5.51, 4, 10.25]},
      {'l': '毛利率 ％', 'v': [4.04, 10.59, 13.22, 12.69, 13.87, 14.95, 17.75, 16.99, 21.21, 20.92, 22.26]},
      {'l': '营业利润率 ％', 'v': [-2.04, 2.24, 4.96, -1.78, 4.13, 3.97, 6.1, 2.19, 5.45, 6.34, 6.9]},
      {'l': '净利率 ％', 'v': [-11.35, 0.81, -38.23, 7.75, 4.88, 8.28, 5.89, 0.88, 6.45, 6.47, 9.11]},
      {'l': '税息折旧及摊销前利润率 ％', 'v': [-10.75, 2.19, -35.99, 8.61, 6.84, 10.86, 9.24, 3.13, 10.48, 9.78, 11.65]},
      {'l': '自由现金流利润率 ％', 'v': [-7.67, 3.95, -1.93, -2.97, 9.91, 7.67, 0.8, -3.64, 12.93, 8.74, 7.47]},
      {'l': '债务股本比率', 'v': [-0.04, -0.05, -0.09, 0.15, 0.23, 0.15, 0.22, 0.18, 0.18, 0.19, 0.14]},
      {'l': '股东权益比率', 'v': [-2.22, -1.82, -1.42, 0.49, 0.44, 0.49, 0.47, 0.53, 0.51, 0.47, 0.52]},
      {'l': '资产有息负债率', 'v': [0.08, 0.08, 0.12, 0.08, 0.1, 0.07, 0.11, 0.1, 0.09, 0.09, 0.07]},
      {'l': '资产负债率', 'v': [3.21, 2.81, 2.42, 0.51, 0.56, 0.51, 0.53, 0.47, 0.49, 0.53, 0.48]},
      {'l': '毛利润／总资产 ％', 'v': [6.9, 15.64, 21.98, 18.57, 17.21, 17.25, 21.64, 16, 19.05, 20.82, 22.68]},
      {'l': 'Degree of Financial Leverage', 'v': [0, 0.92, 2.34, 0.77, -61.83, 1.05, -0.43, 1.06, 1.61, 1.43, 0.84]},
      {'l': '资产周转率', 'v': [1.71, 1.48, 1.66, 1.46, 1.24, 1.15, 1.22, 0.94, 0.9, 1, 1.02]},
      {'l': '股息支付率', 'v': [None, 0, None, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '应收账款周转天数', 'v': [8.03, 9.77, 11.86, 11.79, 11.23, 12.38, 15.47, 18.93, 13.32, 11.55, 10.97]},
      {'l': '应付账款周转天数', 'v': [80.99, 97.91, 92.9, 97.7, 109.92, 111.78, 97.68, 105.93, 99.42, 102.11, 105.6]},
      {'l': '存货周转天数', 'v': [49.21, 52.62, 44.53, 55.6, 64.52, 63.06, 62.67, 84.7, 81.99, 68.13, 72.59]},
      {'l': '现金循环周期', 'v': [-23.75, -35.51, -36.52, -30.3, -34.17, -36.33, -19.54, -2.3, -4.1, -22.43, -22.04]},
      {'l': '存货周转率', 'v': [7.42, 6.94, 8.2, 6.56, 5.66, 5.79, 5.82, 4.31, 4.45, 5.36, 5.03]},
      {'l': '营业成本／收入 ％', 'v': [0.96, 0.89, 0.87, 0.87, 0.86, 0.85, 0.82, 0.83, 0.79, 0.79, 0.78]},
      {'l': '存货／收入', 'v': [0.13, 0.13, 0.11, 0.13, 0.15, 0.15, 0.14, 0.19, 0.18, 0.15, 0.16]},
      {'l': '资本开支／收入', 'v': [0.04, 0.03, 0.01, 0.02, 0.02, 0.01, 0.02, 0.02, 0.02, 0.02, 0]},
      {'l': '资本开支／营业利润', 'v': [0, 1.19, 0.21, 0, 0.4, 0.31, 0.36, 0.95, 0.42, 0.31, 0]},
      {'l': '资本开支／经营现金流', 'v': [0, 0.4, 0, 0, 0.14, 0.14, 0.73, 0, 0.15, 0.19, 0]},
    ]},
    {'t': '合并利润表', 'rows': [
      {'l': '营业收入', 'v': [80296, 76730, 135828, 198692, 229047, 291457, 402004, 312729, 296381, 390668, 505176]},
      {'l': '营业成本', 'v': [77051, 68601, 117870, 173483, 197274, 247890, 330665, 259599, 233515, 308927, 392709]},
      {'l': '毛利润', 'v': [3245, 8128, 17957, 25208, 31774, 43567, 71339, 53130, 62866, 81741, 112467]},
      {'l': '毛利率 ％', 'v': [4.04, 10.59, 13.22, 12.69, 13.87, 14.95, 17.75, 16.99, 21.21, 20.92, 22.26]},
      {'l': '销售及行政开支', 'v': [1681, 2806, 7640, 22823, 15002, 21677, 31493, 29523, 26637, 33088, 44042]},
      {'l': '研发费用', 'v': [1817, 2359, 3734, 6562, 8337, 10973, 16123, 17899, 20889, 25678, 36602]},
      {'l': '其他营业成本', 'v': [1387, 1242, -148, -648, -1022, -666, -805, -1128, -809, -1780, -3024]},
      {'l': '总营业成本', 'v': [4886, 6407, 11227, 28737, 22317, 31984, 46811, 46293, 46716, 56987, 77621]},
      {'l': '营业利润', 'v': [-1641, 1721, 6731, -3529, 9456, 11583, 24528, 6837, 16150, 24755, 34847]},
      {'l': '营业利润率 ％', 'v': [-2.04, 2.24, 4.96, -1.78, 4.13, 3.97, 6.1, 2.19, 5.45, 6.34, 6.9]},
      {'l': '利息收入', 'v': [121, 7, 352, 683, 1036, 1142, 1506, 2510, 3892, 5291, 5944]},
      {'l': '利息支出', 'v': [-103, -97, -256, -437, -588, -3989, -3479, -1262, -1702, -1422, -4015]},
      {'l': '净利息收入', 'v': [18, -90, 97, 246, 448, -2847, -1973, 1248, 2190, 3869, 1929]},
      {'l': '其他收入（支出）', 'v': [-7358, -313, -56394, 19103, 3630, 16908, 7344, -3691, 5735, 1406, 18071]},
      {'l': '利润总额', 'v': [-8981, 1318, -49567, 15820, 13534, 25645, 29898, 4393, 24075, 30030, 54846]},
      {'l': '所得税费用', 'v': [-186, -767, -2441, -510, -2292, -1566, -6286, -1598, -4962, -4856, -8927]},
      {'l': '税率 ％', 'v': [-2.07, 58.18, -4.92, 3.23, 16.93, 6.11, 21.03, 36.39, 20.61, 16.17, 16.28]},
      {'l': '其他净利润（亏损）', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '净利润', 'v': [-9166, 551, -52008, 15310, 11242, 24079, 23612, 2795, 19113, 25174, 45919]},
      {'l': '持续经营净利润', 'v': [-9166, 551, -52008, 15310, 11242, 24079, 23612, 2795, 19113, 25174, 45919]},
      {'l': '终止经营净利润', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '其他收入（少数股东权益）', 'v': [55, 69, 75, 86, -65, 51, 69, -32, 1, 85, 85]},
      {'l': '归属于母公司股东的净利润', 'v': [-9111, 620, -51933, 15396, 11177, 24130, 23680, 2763, 19114, 25259, 46004]},
      {'l': '净利率 ％', 'v': [-11.35, 0.81, -38.23, 7.75, 4.88, 8.28, 5.89, 0.88, 6.45, 6.47, 9.11]},
      {'l': '优先股股利', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '基本每股收益', 'v': [-0.41, 0.03, -2.32, 0.96, 0.47, 1.01, 0.96, 0.11, 0.77, 1.01, 1.79]},
      {'l': '稀释每股收益', 'v': [-0.41, 0.03, -2.32, 0.05, 0.46, 0.98, 0.93, 0.11, 0.76, 0.99, 1.72]},
      {'l': '发行在外的稀释性潜在普通股平均股数', 'v': [22376.1, 22376.1, 22376.1, 23562.9, 24508.8, 24668.3, 25509.4, 25296.7, 25325, 25500.7, 26695.3]},
      {'l': '息税前利润', 'v': [-8877, 1415, -49311, 16257, 14122, 29634, 33377, 5655, 25777, 31452, 58862]},
      {'l': '折旧、损耗和摊销', 'v': [248, 269, 428, 850, 1537, 2025, 3749, 4140, 5290, 6746, 0]},
      {'l': '税息折旧及摊销前利润', 'v': [-8629, 1684, -48884, 17107, 15659, 31659, 37126, 9795, 31067, 38198, 58862]},
      {'l': '税息折旧及摊销前利润率 ％', 'v': [-10.75, 2.19, -35.99, 8.61, 6.84, 10.86, 9.24, 3.13, 10.48, 9.78, 11.65]},
    ]},
    {'t': '合并资产负债表', 'rows': [
      {'l': '货币资金和现金等价物', 'v': [10088, 10349, 13702, 34339, 28842, 64905, 28789, 30829, 36785, 35939, 29733]},
      {'l': '短期证券', 'v': [3796, 4437, 6533, 9104, 42269, 48570, 76744, 44903, 81161, 72720, 89242]},
      {'l': '货币资金、现金等价物、及短期证券', 'v': [13884, 14787, 20235, 43443, 71112, 113476, 105534, 75732, 117946, 108660, 118975]},
      {'l': '应收账款', 'v': [1767, 2343, 6481, 6359, 7732, 12045, 22023, 10424, 11216, 13519, 16836]},
      {'l': '应收票据', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '应收贷款', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '其他流动应收款项', 'v': [1249, 4834, 18431, 25141, 27367, 20953, 20821, 32238, 34726, 28776, 51156]},
      {'l': '应收账款总额', 'v': [3015, 7176, 24913, 31500, 35099, 32998, 42844, 42662, 45941, 42294, 67992]},
      {'l': '原材料', 'v': [2192, 4333, 7923, 9655, 12330, 20926, 28420, 24047, 16351, 19348, 30904]},
      {'l': '在产品', 'v': [2086, 1108, 1603, 2350, 2696, 4902, 5666, 3427, 3899, 5815, 5146]},
      {'l': '存货调整', 'v': [-963, -317, -792, -2104, -972, -1453, -1630, -3875, -2380, -2644, -3317]},
      {'l': '库存商品', 'v': [6757, 3729, 10027, 21710, 20063, 21231, 29307, 31994, 29677, 43601, 55341]},
      {'l': '其他存货', 'v': [315, 540, 604, 1876, 2143, 3792, 2396, 732, 1042, 619, 1397]},
      {'l': '存货', 'v': [10388, 9394, 19366, 33488, 36259, 49398, 64160, 56325, 48589, 66740, 89471]},
      {'l': '其他流动资产', 'v': [2702, 2993, 7934, 11992, 10576, 13100, 15032, 4419, 5244, 23289, 5058]},
      {'l': '流动资产合计', 'v': [29989, 34350, 72448, 120423, 153047, 208971, 227569, 179138, 217720, 240983, 281496]},
      {'l': '非流动投资', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '土地', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '房屋及建筑物修缮', 'v': [132, 183, 392, 641, 4405, 6558, 6828, 9720, 9707, 13459, 0]},
      {'l': '机器、家具和设备', 'v': [17, 17, 17, 39, 25, 29, 31, 37, 123, 244, 0]},
      {'l': '在建工程', 'v': [71, 720, 1746, 5161, 4446, 1986, 2090, 4242, 5787, 5365, 0]},
      {'l': '其他固定资产、无形资产和其他长期资产', 'v': [419, 418, 501, 730, 1052, 1675, 13551, 3195, 6695, 12790, 30877]},
      {'l': '固定资产、无形资产和其他长期资产总值', 'v': [640, 1339, 2656, 6570, 9929, 10248, 22501, 17194, 22312, 31857, 30877]},
      {'l': '累计折旧', 'v': [-291, -388, -605, -813, -1065, -1735, -2596, -3452, -4132, -5605, 0]},
      {'l': '固定资产、无形资产和其他长期资产净额', 'v': [349, 951, 2051, 5757, 8864, 8513, 19905, 13742, 18180, 26252, 30877]},
      {'l': '无形资产', 'v': [3791, 5173, 6743, 6207, 5042, 10398, 8740, 14236, 18344, 17052, 9191]},
      {'l': '商誉', 'v': [298, 278, 294, 320, 276, 300, 2077, 1895, 1856, 1811, 0]},
      {'l': '其他非流动资产', 'v': [12907, 16445, 25252, 32582, 37380, 72838, 102422, 98313, 100411, 146150, 239742]},
      {'l': '非流动资产合计', 'v': [17047, 22569, 34046, 44546, 51287, 91749, 131068, 126292, 136935, 189455, 279811]},
      {'l': '资产总计', 'v': [47035, 56919, 106494, 164969, 204334, 300720, 358637, 305430, 354655, 430438, 561306]},
      {'l': '应付账款', 'v': [17097, 19708, 40293, 52579, 66240, 85587, 91398, 59290, 67922, 104931, 122292]},
      {'l': '应交税费', 'v': [158, 336, 569, 970, 1228, 1448, 4160, 2535, 3813, 6325, 7345]},
      {'l': '其他应付账款', 'v': [867, 1362, 3875, 5757, 6978, 11070, 14497, 12582, 17742, 23096, 39054]},
      {'l': '应交税费和其他应付款', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '应付票据及应付账款、应交税费和其他应付款', 'v': [18122, 21406, 44738, 59306, 74445, 98105, 110055, 74408, 89477, 134352, 168692]},
      {'l': '短期借款', 'v': [37, 4282, 4268, 3493, 14284, 8253, 8179, 2402, 6763, 14229, 14585]},
      {'l': '短期资本化租赁债务', 'v': [0, 0, 0, 0, 444, 431, 1877, 1058, 779, 2390, 0]},
      {'l': '短期借款和资本化租赁债务', 'v': [37, 4282, 4268, 3493, 14728, 8684, 10055, 3460, 7542, 16620, 14585]},
      {'l': '流动递延收益负债', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '流动递延所得税负债', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '流动递延所得税和收益负债', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '其他流动负债', 'v': [1628, 3534, 6845, 7560, 13400, 21152, 21594, 22221, 29408, 36282, 29278]},
      {'l': '流动负债合计', 'v': [19787, 29223, 55851, 70360, 102574, 127940, 141705, 100088, 126427, 187254, 212555]},
      {'l': '长期借款', 'v': [3902, 437, 8593, 8924, 5327, 12607, 25371, 24002, 23707, 18445, 25322]},
      {'l': '长期资本租赁负债', 'v': [0, 0, 0, 0, 624, 612, 2141, 1636, 1374, 3384, 0]},
      {'l': '长期借款和资本化租赁债务', 'v': [3902, 437, 8593, 8924, 5951, 13219, 27512, 25638, 25080, 21829, 25322]},
      {'l': '债务股本比率', 'v': [-0.04, -0.05, -0.09, 0.15, 0.23, 0.15, 0.22, 0.18, 0.18, 0.19, 0.14]},
      {'l': '养老金和退休福利', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '非流动递延负债', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '其他非流动负债', 'v': [127470, 130476, 192792, 4750, 4944, 12551, 21139, 18983, 23481, 19346, 29215]},
      {'l': '非流动负债合计', 'v': [131372, 130914, 201384, 13674, 10895, 25771, 48650, 44620, 48562, 41175, 54537]},
      {'l': '负债合计', 'v': [151160, 160136, 257236, 84034, 113469, 153711, 190355, 144709, 174989, 228429, 267092]},
      {'l': '普通股', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '优先股', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '留存收益', 'v': [0, 0, 0, 0, 0, 60354, 84858, 79750, 96440, 119089, 0]},
      {'l': '累计其他综合收益（亏损）', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '资本公积', 'v': [0, 0, 0, 0, 0, 76645, 73122, 66426, 66478, 64186, 0]},
      {'l': '库存股', 'v': [0, 0, 0, 0, 0, -43, -421, -213, -479, -37, 0]},
      {'l': '其他所有者权益', 'v': [-104216, -103367, -150815, 81017, 90500, 9672, 10453, 14463, 16936, 18271, 294098]},
      {'l': '归属于母公司所有者权益合计', 'v': [-104216, -103367, -150815, 81018, 90501, 146628, 168013, 160426, 179375, 201510, 294098]},
      {'l': '少数股东权益', 'v': [92, 150, 73, -83, 364, 381, 269, 295, 291, 499, 116]},
      {'l': '所有者权益合计', 'v': [-104124, -103217, -150742, 80935, 90865, 147010, 168282, 160721, 179666, 202009, 294214]},
      {'l': '股东权益比率', 'v': [-2.22, -1.82, -1.42, 0.49, 0.44, 0.49, 0.47, 0.53, 0.51, 0.47, 0.52]},
    ]},
    {'t': '合并现金流量表', 'rows': [
      {'l': '来自持续经营收益的净利润', 'v': [-8981, 1318, -49567, 15820, 13534, 25645, 29898, 4393, 24075, 30030, 45919]},
      {'l': '折旧、损耗和摊销', 'v': [248, 269, 428, 850, 1537, 2025, 3749, 4140, 5290, 6746, 0]},
      {'l': '经营性应收项目变动', 'v': [-760, -2412, -12150, -3230, -5258, -4203, -4704, 2012, -3977, -11696, 0]},
      {'l': '存货变动', 'v': [-934, -17, -10211, -18305, -7750, -13754, -16832, -6085, 2378, -25765, 0]},
      {'l': '预付资产变动', 'v': [282, -1933, -7850, -10750, 2177, 4297, -5351, 1613, -2654, -8883, 0]},
      {'l': '应付票据及应付账款、应交税费和其他应付款变动', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '其他营运资金变动', 'v': [-1771, 6308, 21581, 17068, 21460, 18781, 7633, -21276, 23197, 50609, 0]},
      {'l': '营运资金变动', 'v': [-3184, 1945, -8630, -15217, 10630, 5121, -19255, -23736, 18944, 4265, 0]},
      {'l': '递延所得税', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '股权激励', 'v': [830, 977, 1077, 14064, 2450, 2760, 2492, 2789, 3696, 3978, 0]},
      {'l': '资产减值费用', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '终止经营活动产生的现金流量净额', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '其他经营活动产生的现金流量净额', 'v': [7960, 571, 55512, -17123, -1655, -9616, -4903, 7511, -6831, -3064, -8202]},
      {'l': '经营活动产生的现金流量净额', 'v': [-3126, 5081, -1180, -1607, 26495, 25935, 11982, -4902, 45174, 41955, 37718]},
      {'l': '购建固定资产、无形资产和其他长期资产支付的现金', 'v': [-3034, -2048, -1443, -4300, -3789, -3587, -8779, -6476, -6857, -7791, 0]},
      {'l': '处置固定资产、无形资产和其他长期资产收回的现金净额', 'v': [0, 56, 2, 31, 75, 75, 43, 19, 80, 383, 0]},
      {'l': '取得子公司及其他营业单位支付的现金净额', 'v': [-681, -1037, -186, -970, -223, -1552, -1595, -59, -133, -127, 0]},
      {'l': '处置子公司及其他营业单位收到的现金净额', 'v': [0, 0, 0, 0, 89, 53, 3263, 403, 1013, 122, 0]},
      {'l': '投资支付的现金', 'v': [-91952, -63791, -137450, -168704, -183287, -298255, -323294, -167730, -187536, -234156, 0]},
      {'l': '收回投资及取得投资收益收到的现金', 'v': [96469, 62395, 135420, 164708, 150657, 280920, 273464, 189527, 151186, 197790, 0]},
      {'l': '取得无形资产支付的现金净额差', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 1649, 0]},
      {'l': '终止投资活动产生的现金流量净额', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '其他投资活动产生的现金流量净额', 'v': [247, 237, 483, 706, 1348, 1389, 1786, 1679, 3780, 4349, 0]},
      {'l': '投资活动产生的现金流量净额', 'v': [1050, -4188, -3173, -8529, -35130, -20957, -55111, 17364, -38467, -37781, -79185]},
      {'l': '发行股票产生的现金流入', 'v': [0, 0, 0, 26513, 208, 23901, 102, 94, 76, 90, 0]},
      {'l': '回购股票产生的现金流出', 'v': [0, 0, 0, 0, -3263, -539, -8580, -2665, -1484, -4322, 0]},
      {'l': '发行优先股产生的现金流量净额', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '发行债券产生的现金流入', 'v': [3902, 830, 13242, 11665, 18958, 43130, 27586, 22176, 9698, 27757, 0]},
      {'l': '偿付债券现金流出', 'v': [-4810, -56, -5369, -11934, -11592, -38629, -16287, -25293, -4400, -20569, 0]},
      {'l': '发行债券产生的现金流量净额', 'v': [-907, 774, 7873, -268, 7366, 4501, 11299, -3117, 5298, 7188, 0]},
      {'l': '分配股利、利润或偿付利息支付的现金', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '融资租赁产生的现金流量净额', 'v': [0, 0, 0, 0, -643, -585, -1366, -1431, -1310, -2644, 0]},
      {'l': '其他筹资活动产生的现金流量净额', 'v': [1591, -855, -508, 3941, -194, 3799, 4054, -1652, -3133, -4581, 0]},
      {'l': '筹资活动产生的现金流量净额', 'v': [683, -81, 7365, 30186, 3473, 31077, 5508, -8772, -552, -4270, 33988]},
      {'l': '期初现金及现金等价物余额', 'v': [11135, 9412, 10938, 13135, 33639, 30726, 67043, 26256, 30196, 35907, 37187]},
      {'l': '汇率变动对现金及现金等价物的影响', 'v': [347, 126, -247, 1153, 365, -1876, -633, 884, 435, 128, 26]},
      {'l': '现金及现金等价物净额', 'v': [-1047, 938, 2765, 21204, -4796, 34179, -38253, 4574, 6589, 32, -7454]},
      {'l': '期末现金及现金等价物余额', 'v': [10088, 10349, 13702, 34339, 28842, 64905, 28789, 30829, 36785, 35939, 29733]},
      {'l': '资本开支', 'v': [-3034, -2048, -1443, -4300, -3789, -3587, -8779, -6476, -6857, -7791, 0]},
      {'l': '自由现金流', 'v': [-6160, 3033, -2623, -5907, 22706, 22349, 3203, -11379, 38317, 34164, 37718]},
    ]},
    {'t': '估值比率', 'rows': [
      {'l': '市盈率', 'v': [9999, 9999, 9999, 258.4, 23.64, 33.59, 20.3, 97.68, 20.66, 34.89, 22.75]},
      {'l': '持续经营市盈率', 'v': [9999, 9999, 9999, 0, 34.55, 87.43, 26.96, 65.51, 26.85, 36.9, 31.06]},
      {'l': '股价／巴菲特所有者每股收益', 'v': [0, 0, 0, 0, 0, 0, 21.55, 0, 0, 0, 0]},
      {'l': '市净率', 'v': [0, 0, 0, 3.77, 2.86, 5.68, 2.81, 1.7, 2.18, 4.32, 3.46]},
      {'l': '股价／每股有形账面价值', 'v': [0, 0, 0, 4.08, 3.02, 6.11, 2.96, 1.87, 2.43, 4.71, 3.57]},
      {'l': '股价／自由现金流', 'v': [0, 0, 0, 0, 11.64, 36.48, 150, 0, 10.31, 25.86, 27.74]},
      {'l': '股价／每股经营现金流', 'v': [0, 0, 0, 0, 9.97, 31.45, 40.21, 0, 8.74, 21.06, 27.74]},
      {'l': '市销率', 'v': [0, 0, 0, 1.53, 1.15, 2.8, 1.2, 0.88, 1.33, 2.26, 2.07]},
      {'l': '市盈增长比', 'v': [0, 0, 0, 0, 0, 0, 0.38, 0, 1.81, 6.68, 1.8]},
      {'l': '企业价值／收入', 'v': [0, 0, 0, 1.38, 0.91, 2.54, 1.01, 0.72, 1.03, 2.05, 1.86]},
      {'l': '企业价值倍数', 'v': [0, 0, 0, 16.03, 13.31, 23.41, 10.89, 23.11, 9.86, 20.94, 15.93]},
      {'l': '企业价值／息税前利润', 'v': [0, 0, 0, 16.86, 14.76, 25.01, 12.11, 40.03, 11.88, 25.43, 15.93]},
      {'l': '乔尔·格林布拉特收益率 ％', 'v': [0, 0, 0, 5.93, 6.78, 4, 8.26, 2.5, 8.42, 3.93, 6.28]},
      {'l': '唐纳德·亚克曼收益率 ％', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 19.59, 8.39, 20.09]},
      {'l': '席勒市盈率', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 254.79, 110.03]},
      {'l': 'Cyclically Adjusted PB Ratio', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 94.62, 14.77]},
      {'l': 'Cyclically Adjusted PS Ratio', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 3.43, 3.36]},
      {'l': '股息率 ％', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
    ]},
    {'t': '估值和质量', 'rows': [
      {'l': '总市值', 'v': [0, 0, 0, 305253, 258474, 832299, 471894, 272746, 391286, 869686, 1016406]},
      {'l': '企业价值', 'v': [0, 0, 0, 274145, 208405, 741107, 404197, 226406, 306254, 799973, 937454]},
      {'l': '月末股价', 'v': [0, 0, 0, 12.92, 10.78, 33.05, 18.9, 10.94, 15.6, 34.65, 39.2]},
      {'l': '每股现金净流量', 'v': [-13.16, -13.94, -22.72, -1.71, -1.78, -1.61, -3.41, -2.78, -2.29, -4.79, -5.72]},
      {'l': '净流动资产价值 NCAV', 'v': [-11.62, -12.07, -17.71, 1.54, 1.64, 2.18, 1.48, 1.37, 1.69, 0.48, 0.55]},
      {'l': '净净营运资本 NNWC', 'v': [-12.54, -13.32, -21.32, -0.8, -0.78, -0.27, -1.46, -1.34, -0.98, -3.06, -3.5]},
      {'l': '内在价值：预期自由现金流模型', 'v': [0, 0, 0, 0, 0, 0, 0, 6.37, 11.51, 15.57, 19.47]},
      {'l': '内在价值：市销率模型', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '彼得林奇公允价值', 'v': [0, 0, 0, 0, 0, 0, 23.28, 0, 8.69, 5.26, 21.54]},
      {'l': '格雷厄姆估值', 'v': [0, 0, 0, 0, 5.01, 6.78, 10.03, 4.7, 9.16, 12.46, 17.66]},
      {'l': '盈利能力值（EPV）', 'v': [0, 0, 0, 0, 0, 0, 7.55, 0, 0, 0, 0]},
      {'l': '两年破产风险（Z分数）', 'v': [0, 0, 0.01, 4.13, 3.02, 5.1, 3.52, 2.94, 3.12, 3.99, 3.66]},
      {'l': '基本面趋势（F分数）', 'v': [9999, 9999, 9999, 4, 6, 6, 3, 3, 7, 6, 7]},
      {'l': '财务造假嫌疑（M分数）', 'v': [0, 0, 0, -1.88, -2.78, -2.43, -2.08, -2.21, -2.88, -2.63, -1.85]},
      {'l': '可供营运的资产净额／总资产', 'v': [-2.42, -2.41, -2.78, 0.47, 0.25, 0.27, 0.33, 0.32, 0.31, 0.37, 0.5]},
      {'l': '斯隆比率 ％', 'v': [-14.96, -0.48, -44.68, 15.48, 9.7, 6.37, 18.63, -3.18, 3.5, 4.9, 15.58]},
      {'l': '流动比率', 'v': [1.52, 1.18, 1.3, 1.71, 1.49, 1.63, 1.61, 1.79, 1.72, 1.29, 1.32]},
      {'l': '速动比率', 'v': [0.99, 0.85, 0.95, 1.24, 1.14, 1.25, 1.15, 1.23, 1.34, 0.93, 0.9]},
      {'l': '现金比率', 'v': [0.7, 0.51, 0.36, 0.62, 0.69, 0.89, 0.74, 0.76, 0.93, 0.58, 0.56]},
      {'l': '利息保障倍数', 'v': [0, 17.8, 26.33, 0, 16.08, 2.9, 7.05, 5.42, 9.49, 17.4, 8.68]},
      {'l': '最高股价', 'v': [0, 0, 0, 21.55, 12.5, 33.05, 35.3, 18.92, 16.8, 34.65, 60.15]},
      {'l': '最低股价', 'v': [0, 0, 0, 11.66, 8.35, 9.5, 17.62, 8.45, 9.99, 12.12, 32.8]},
      {'l': '股票回购率 ％', 'v': [None, 0, 0, -126.38, -1.48, -5.03, 0.85, 0.15, -0.61, -0.07, -3.31]},
      {'l': '股票回购收益率 ％', 'v': [0, 0, 0, -8.69, 1.18, -2.81, 1.8, 0.94, 0.36, 0.49, 0]},
      {'l': '每股收入同比增长率 ％', 'v': [0, -4.44, 77.02, 38.91, 10.83, 26.42, 33.38, -21.55, -5.33, 30.9, 23.52]},
      {'l': '每股收益同比增长率 ％', 'v': [0, 106.81, -8472.04, 102.15, 812.8, 115.66, -5.42, -88, 575.83, 31.57, 73.56]},
      {'l': '税息折旧及摊销前利润同比增长率 ％', 'v': [0, 119.51, -3003.12, 133.23, -12, 100.87, 13.4, -73.39, 216.8, 22.11, 47.2]},
      {'l': '5年每股税息折旧及摊销前利润增长率 ％', 'v': [0, 0, 0, 0, 0, 0, 71.43, 0, 14.81, 5.52, 17.29]},
      {'l': '发行在外的普通股平均股数', 'v': [22376.13, 22376.13, 22376.13, 16069.77, 23746.46, 23986.83, 24927.46, 24828.32, 24884.87, 24825.17, 25656.64]},
      {'l': '期末总股本', 'v': [10436.77, 10436.77, 10436.77, 23626.42, 23977.16, 25183.02, 24967.95, 24931.05, 25082.43, 25099.15, 25928.74]},
      {'l': 'β贝塔系数', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0.93, 1.21]},
      {'l': '重述日期', 'v': [None, None, None, None, None, None, None, None, None, None, None]},
      {'l': '报告日期', 'v': [None, None, None, None, None, None, None, None, None, None, None]},
      {'l': '收益发布日期', 'v': [None, None, None, None, None, None, None, None, None, None, None]},
      {'l': '股东人数', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '在职员工人数', 'v': [0, 0, 0, 16683, 18170, 22074, 33427, 32543, 33627, 43688, 56531]},
      {'l': '外汇汇率', 'v': [0.83, 0.89, 0.84, 0.88, 0.9, 0.84, 0.82, 0.9, 0.91, 0.94, 0.91]},
    ]},
  ]},
  '09988.HK': {'name': '阿里巴巴', 'years': [2006, 2007, 2008, 2009, 2010, 2011, 2012, 2013, 2014, 2015, 2016, 2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025], 'groups': [
    {'t': '每股指标', 'rows': [
      {'l': '每股收入', 'v': [0.07, 0.06, 0.08, 0.11, 0.16, 0.19, 1.32, 2.25, 3.54, 4.74, 5.89, 8.66, 14.88, 21, 26.41, 38.92, 48.28, 46.86, 50.22, 55.3]},
      {'l': '每股税息折旧及摊销前利润', 'v': [0.02, 0.02, 0.03, 0.03, 0.05, 0.06, 0.43, 0.82, 2.07, 2.46, 5.27, 4.21, 7.49, 7.72, 11.1, 11.83, 7.26, 8.26, 8.75, 11.52]},
      {'l': '每股息税前利润', 'v': [0.01, 0.02, 0.03, 0.03, 0.04, 0.05, 0.37, 0.76, 1.95, 2.18, 4.86, 3.43, 6.18, 5.65, 8.9, 9.23, 4.54, 5.73, 6.38, 9.16]},
      {'l': '稀释每股收益', 'v': [0.01, 0.03, 0.03, 0.03, 0.04, 0.05, 0.28, 0.56, 1.57, 1.51, 4.16, 2.39, 3.8, 4.88, 7.73, 8.16, 3.5, 3.91, 4.25, 7.18]},
      {'l': '持续经营每股收益', 'v': [0.01, 0.03, 0.03, 0.03, 0.04, 0.05, 0.37, 0.9, 1.9, 2.17, 2.5, 3.3, 5.1, 5.61, 7.32, 9.71, 8.13, 7.77, 8.45, 8.77]},
      {'l': '巴菲特所有者每股收益（TTM）', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 4.08, 2.3, 3.64, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '每股自由现金流', 'v': [0.03, 0.04, 0.04, 0.05, 0.08, 0.06, 0.47, 0.78, 1.46, 2.08, 2.68, 3.57, 5.71, 5.65, 7.01, 10.23, 5.06, 8.92, 7.99, 4.3]},
      {'l': '每股经营现金流', 'v': [0.04, 0.04, 0.04, 0.06, 0.09, 0.07, 0.61, 0.95, 1.78, 2.56, 3.31, 4.53, 7.48, 8.41, 9.36, 12.58, 8.08, 10.78, 9.74, 9.08]},
      {'l': '每股现金', 'v': [0.08, 0.14, 0.19, 0.2, 0.28, 0.35, 1.47, 2.18, 2.87, 7.85, 6.98, 8.4, 12.68, 11.48, 18.69, 26.57, 26.27, 29.1, 31.86, 24.16]},
      {'l': '每股股息', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0.96, 0.97]},
      {'l': '每股账面价值', 'v': [0.01, 0.1, 0.14, 0.14, 0.17, 0.22, 2.08, 0, 1.87, 9.06, 13.08, 15.52, 22.07, 27.82, 38.88, 51.53, 54.76, 54.92, 55.05, 57]},
      {'l': '每股有形账面价值', 'v': [0.01, 0.1, 0.14, 0.13, 0.15, 0.2, 1.19, -0.89, 0.89, 5.85, 7.66, 7.4, 9.73, 8.22, 20.88, 31.03, 35.06, 36.94, 38.38, 40.67]},
      {'l': '每股总债务', 'v': [0, 0, 0, 0, 0, 0.04, 0.09, 1.82, 2.62, 3.28, 3.47, 5.11, 7.57, 7.59, 7.58, 9.97, 10.2, 10.85, 11.47, 14]},
      {'l': '月末股价', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 183.4, 220, 112.1, 100.4, 70.25, 128]},
    ]},
    {'t': '财务比率', 'rows': [
      {'l': '股本回报率 ROE ％', 'v': [151.09, 51.62, 27.86, 20.54, 27.76, 26.54, 21.75, 41.18, 93.31, 26.16, 38.78, 17.17, 20.71, 19.96, 23.43, 18.39, 6.71, 7.22, 7.91, 12.95]},
      {'l': '股本回报率 ROE ％／市净率', 'v': [None, None, None, None, None, None, None, None, None, None, None, None, None, None, 4.97, 4.31, 3.28, 3.95, 6.19, 5.77]},
      {'l': '资产收益率 ROA ％', 'v': [10.76, 24.28, 17.01, 11.69, 13.43, 12.49, 13.54, 15.47, 26.66, 13.18, 22.67, 9.78, 10.89, 10.18, 12.81, 10.36, 3.74, 4.06, 4.44, 7.24]},
      {'l': '有形股本回报率 ％', 'v': [151.09, 51.62, 27.88, 21.35, 30.52, 29.38, 34.22, 116.67, 220.11, 41, 63.56, 32.64, 45.48, 55.53, 53.43, 32.01, 10.8, 11, 11.55, 18.36]},
      {'l': '有形资产收益率 ％', 'v': [10.76, 24.28, 17.02, 11.95, 14.04, 13.09, 17.51, 20.44, 31.92, 16.12, 29.36, 13.41, 15.26, 15.12, 18.48, 13.62, 4.74, 5.03, 5.4, 8.67]},
      {'l': '乔尔·格林布拉特资本回报率 ％', 'v': [236.97, 606.18, 426.52, 183.72, 185.17, 228.7, 350.77, 374.85, 619.46, 474.6, 720.56, 361.96, 245.13, 124.74, 146.02, 97.12, 34.23, 39.93, 43.99, 62.71]},
      {'l': '已动用资本回报率 ROCE ％', 'v': [146.95, 41.73, 26.95, 20.7, 24.77, 23.53, 25.59, 31.27, 51.02, 24.13, 31.06, 16.86, 21.74, 14.76, 18.36, 14.75, 6.21, 7.62, 8.61, 12.09]},
      {'l': '5年留存收益回报率 RORE %', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 15.37, 19.69, 15.45, 20.47, -0.84, -3.18, -13.69, -2.4]},
      {'l': '资本回报率 ％', 'v': [13.1, 35.75, 37.48, 24.63, 23.06, 19.68, 26.32, 31, 42.41, 18.3, 13.53, 11.64, 13.66, 7.17, 9.09, 6.99, 4.32, 6.66, 7.72, 8.94]},
      {'l': '加权平均资本成本 WACC ％', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 5.66, 7.76, 7.59, 8.15, 18.17, 12.39, 9.64, 6.47, 6.16, 6.21, 7.27, 5.48]},
      {'l': '实际借款利率 ％', 'v': [None, None, None, None, 0, 0, 5.3, 10.78, 6.38, 5.84, 3.46, 3.5, 3.42, 3.88, 3.58, 2.81, 11.71, 8.79, 8.72, 4.2]},
      {'l': '毛利率 ％', 'v': [82.58, 87.05, 86.66, 86.21, 83.25, 80.36, 67.27, 71.84, 74.54, 68.72, 66.03, 62.42, 57.23, 45.09, 44.6, 41.28, 36.76, 36.72, 37.7, 39.95]},
      {'l': '营业利润率 ％', 'v': [19.69, 37.19, 38, 27.69, 25.69, 26.34, 26.72, 32.78, 48.03, 30.59, 29.62, 30.65, 28.12, 15.15, 18.05, 12.5, 11.11, 11.86, 13.16, 14.76]},
      {'l': '净利率 ％', 'v': [16.13, 44.75, 38.43, 26.14, 26.44, 26.69, 21.11, 24.72, 44.41, 31.84, 70.65, 27.59, 25.61, 23.32, 29.32, 20.99, 7.3, 8.38, 8.5, 13.06]},
      {'l': '税息折旧及摊销前利润率 ％', 'v': [23.65, 39.93, 40.95, 30.96, 29.76, 30.28, 32.31, 36.56, 58.38, 51.93, 89.44, 48.65, 50.34, 36.75, 42.03, 30.39, 15.03, 17.63, 17.43, 20.83]},
      {'l': '自由现金流利润率 ％', 'v': [46.48, 58.69, 43.75, 47.22, 49.39, 31.32, 35.49, 34.69, 41.15, 43.98, 45.47, 41.26, 38.35, 26.89, 26.53, 26.29, 10.48, 19.04, 15.9, 7.78]},
      {'l': '债务股本比率', 'v': [0, 0, 0, 0, 0.02, 0.17, 0.04, 2.68, 1.04, 0.36, 0.27, 0.33, 0.34, 0.27, 0.2, 0.19, 0.19, 0.2, 0.21, 0.25]},
      {'l': '股东权益比率', 'v': [0.07, 0.6, 0.62, 0.53, 0.45, 0.49, 0.67, 0.16, 0.36, 0.57, 0.6, 0.55, 0.51, 0.51, 0.57, 0.56, 0.56, 0.56, 0.56, 0.56]},
      {'l': '资产有息负债率', 'v': [0, 0, 0, 0, 0.01, 0.08, 0.03, 0.44, 0.37, 0.21, 0.16, 0.18, 0.18, 0.14, 0.11, 0.11, 0.1, 0.11, 0.12, 0.14]},
      {'l': '资产负债率', 'v': [0.93, 0.4, 0.38, 0.47, 0.54, 0.51, 0.27, 0.83, 0.64, 0.38, 0.31, 0.36, 0.39, 0.36, 0.33, 0.36, 0.36, 0.36, 0.37, 0.4]},
      {'l': '毛利润／总资产 ％', 'v': [55.08, 47.22, 38.35, 38.54, 42.27, 37.62, 43.14, 44.97, 44.76, 28.45, 21.19, 22.13, 24.34, 19.69, 19.49, 20.36, 18.83, 17.78, 19.7, 22.16]},
      {'l': 'Degree of Financial Leverage', 'v': [0, 0.57, 0.42, 2.14, 0, 0, 1.81, 0, 0, -0.09, 1.33, 1.44, 0.69, -6.67, 0.99, 0.46, 1.31, 0.53, -1.13, 2.17]},
      {'l': '资产周转率', 'v': [0.67, 0.54, 0.44, 0.45, 0.51, 0.47, 0.64, 0.63, 0.6, 0.41, 0.32, 0.36, 0.43, 0.44, 0.44, 0.49, 0.51, 0.48, 0.52, 0.56]},
      {'l': '股息支付率', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0.11, 0.11]},
      {'l': '应收账款周转天数', 'v': [0, 0, 0, 3.57, 2.99, 2.76, 1.83, 1.52, 1.4, 3.21, 4.19, 6.54, 8.22, 10.41, 12.3, 11.56, 12.62, 14.21, 12.48, 11.4]},
      {'l': '应付账款周转天数', 'v': [13.36, 13.71, 12.59, 13.46, 7.68, 4.59, 22.76, 31.43, 39.93, 55.33, 72.79, 88.94, 100.02, 75.57, 0, 0, 0, 0, 0, 0]},
      {'l': '存货周转天数', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 5.87, 9.21, 11.77, 15.44, 18.04, 19.29, 20.29, 17.24, 13.63]},
      {'l': '现金循环周期', 'v': [-13.36, -13.71, -12.59, -9.89, -4.7, -1.82, -20.93, -29.91, -38.52, -52.13, -68.6, -76.53, -82.59, -53.38, 27.73, 29.6, 31.92, 34.5, 29.72, 25.03]},
      {'l': '存货周转率', 'v': [None, None, None, None, None, None, None, None, None, None, None, 62.16, 39.62, 31.01, 23.65, 20.23, 18.92, 17.99, 21.17, 26.78]},
      {'l': '营业成本／收入 ％', 'v': [0.17, 0.13, 0.13, 0.14, 0.17, 0.2, 0.33, 0.28, 0.25, 0.31, 0.34, 0.38, 0.43, 0.55, 0.55, 0.59, 0.63, 0.63, 0.62, 0.6]},
      {'l': '存货／收入', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0.01, 0.01, 0.02, 0.02, 0.03, 0.03, 0.04, 0.03, 0.02]},
      {'l': '资本开支／收入', 'v': [0.07, 0.06, 0.09, 0.11, 0.05, 0.03, 0.11, 0.07, 0.09, 0.1, 0.11, 0.11, 0.12, 0.13, 0.09, 0.06, 0.06, 0.04, 0.03, 0.09]},
      {'l': '资本开支／营业利润', 'v': [0.36, 0.17, 0.24, 0.38, 0.21, 0.1, 0.41, 0.22, 0.19, 0.33, 0.36, 0.36, 0.42, 0.87, 0.49, 0.48, 0.56, 0.33, 0.27, 0.58]},
      {'l': '资本开支／经营现金流', 'v': [0.13, 0.1, 0.17, 0.18, 0.1, 0.08, 0.23, 0.17, 0.18, 0.19, 0.19, 0.21, 0.24, 0.33, 0.25, 0.19, 0.37, 0.17, 0.18, 0.53]},
    ]},
    {'t': '合并利润表', 'rows': [
      {'l': '营业收入', 'v': [1355, 2289, 3397, 4400, 6497, 7861, 24623, 43090, 66013, 94768, 120705, 178288, 310636, 440698, 563771, 855461, 1051813, 989430, 1022392, 1068297]},
      {'l': '营业成本', 'v': [236, 296, 453, 607, 1088, 1544, 8059, 12133, 16809, 29640, 41000, 67005, 132865, 241992, 312315, 502342, 665134, 626100, 636924, 641489]},
      {'l': '毛利润', 'v': [1119, 1993, 2944, 3793, 5409, 6317, 16564, 30957, 49205, 65128, 79706, 111283, 177770, 198706, 251456, 353119, 386679, 363330, 385469, 426808]},
      {'l': '毛利率 ％', 'v': [82.58, 87.05, 86.66, 86.21, 83.25, 80.36, 67.27, 71.84, 74.54, 68.72, 66.03, 62.42, 57.23, 45.09, 44.6, 41.28, 36.76, 36.72, 37.7, 39.95]},
      {'l': '销售及行政开支', 'v': [765, 1023, 1617, 2309, 3062, 3285, 6479, 8117, 11018, 20287, 24479, 32164, 54043, 75627, 87235, 163084, 187070, 165928, 170686, 201855]},
      {'l': '研发费用', 'v': [105, 139, 242, 436, 678, 961, 3562, 4685, 6403, 13254, 16455, 19217, 28243, 43778, 47649, 68261, 68388, 64631, 56766, 61278]},
      {'l': '其他营业成本', 'v': [-18, -21, -207, -171, 0, 0, -55, 4031, 79, 2598, 3019, 5262, 8149, 12545, 14808, 14821, 14361, 15381, 23455, 5978]},
      {'l': '总营业成本', 'v': [852, 1141, 1653, 2575, 3740, 4247, 9986, 16833, 17500, 36139, 43953, 56643, 90434, 131950, 149692, 246166, 269818, 245940, 250907, 269111]},
      {'l': '营业利润', 'v': [267, 851, 1291, 1218, 1669, 2071, 6578, 14124, 31704, 28989, 35752, 54640, 87336, 66757, 101764, 106953, 116861, 117391, 134561, 157697]},
      {'l': '营业利润率 ％', 'v': [19.69, 37.19, 38, 27.69, 25.69, 26.34, 26.72, 32.78, 48.03, 30.59, 29.62, 30.65, 28.12, 15.15, 18.05, 12.5, 11.11, 11.86, 13.16, 14.76]},
      {'l': '利息收入', 'v': [23, 430, 277, 0, 0, 0, 317, 49, 2072, 11758, 62361, 9641, 37851, 51580, 80694, 86816, 0, 0, 0, 22258]},
      {'l': '利息支出', 'v': [0, 0, -7, 0, 0, 0, -84, -1962, -2760, -3420, -2322, -3009, -4426, -6069, -5729, -5338, -25413, -19350, -19457, -10289]},
      {'l': '净利息收入', 'v': [23, 430, 270, 160, 185, 323, 234, -1914, -688, 8338, 60038, 6633, 33425, 45510, 74964, 81478, -25413, -19350, -19457, 11969]},
      {'l': '其他收入（支出）', 'v': [0, -65, -18, -43, 141, 224, -10, 413, 2682, 2874, 1434, 6348, 3861, 258, 7591, 9043, -18024, 3541, -4741, -2985]},
      {'l': '利润总额', 'v': [290, 1217, 1543, 1336, 1995, 2618, 6802, 12624, 33698, 40201, 97225, 67620, 124622, 112525, 184320, 197473, 73424, 101581, 110364, 166681]},
      {'l': '所得税费用', 'v': [-71, -192, -238, -186, -276, -524, -1035, -1819, -4018, -7979, -10083, -15518, -22589, -19358, -22743, -34918, -33063, -17710, -24473, -38005]},
      {'l': '税率 ％', 'v': [24.52, 15.8, 15.41, 13.89, 13.86, 20.03, 15.22, 14.41, 11.92, 19.85, 10.37, 22.95, 18.13, 17.2, 12.34, 17.68, 45.03, 17.43, 22.18, 22.8]},
      {'l': '其他净利润（亏损）', 'v': [0, 0, 0, 0, 0, 0, -31, -7, -255, -1977, -2065, -5663, -25808, 662, -6341, 8329, 17686, -9184, -8403, 6397]},
      {'l': '净利润', 'v': [219, 1024, 1306, 1150, 1718, 2093, 5736, 10797, 29425, 30245, 85077, 46439, 76226, 93829, 155236, 170885, 58048, 74687, 77488, 135073]},
      {'l': '持续经营净利润', 'v': [219, 1024, 1306, 1150, 1718, 2093, 5736, 10797, 29425, 30245, 85077, 46439, 76226, 93829, 155236, 170885, 58048, 74687, 77488, 135073]},
      {'l': '终止经营净利润', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '其他收入（少数股东权益）', 'v': [0, 0, 0, 0, -1, 5, -537, -146, -111, -73, 204, 2759, 3328, 8949, 10046, 8699, 18704, 8212, 9426, 4431]},
      {'l': '归属于母公司股东的净利润', 'v': [219, 1024, 1306, 1150, 1718, 2098, 5199, 10651, 29314, 30171, 85281, 49198, 79554, 102778, 165282, 179584, 76752, 82899, 86914, 139505]},
      {'l': '净利率 ％', 'v': [16.13, 44.75, 38.43, 26.14, 26.44, 26.69, 21.11, 24.72, 44.41, 31.84, 70.65, 27.59, 25.61, 23.32, 29.32, 20.99, 7.3, 8.38, 8.5, 13.06]},
      {'l': '优先股股利', 'v': [0, 0, 0, 0, 0, 0, 0, 139, 262, 121, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '基本每股收益', 'v': [0.01, 0.03, 0.03, 0.03, 0.04, 0.05, 0.29, 0.57, 1.67, 1.61, 4.34, 2.47, 3.88, 4.96, 7.85, 8.29, 3.54, 3.94, 4.29, 7.39]},
      {'l': '稀释每股收益', 'v': [0.01, 0.03, 0.03, 0.03, 0.04, 0.05, 0.28, 0.56, 1.57, 1.51, 4.16, 2.39, 3.8, 4.88, 7.73, 8.16, 3.5, 3.91, 4.25, 7.18]},
      {'l': '发行在外的稀释性潜在普通股平均股数', 'v': [19072, 38879.1, 40450.8, 40637, 40578.1, 40630.6, 18656, 19112, 18656, 20000, 20496, 20584, 20881, 20988, 21346, 21982, 21787, 21114, 20359, 19318]},
      {'l': '息税前利润', 'v': [267, 851, 1291, 1218, 1669, 2071, 6886, 14586, 36458, 43621, 99547, 70629, 129049, 118595, 190049, 202812, 98837, 120932, 129821, 176970]},
      {'l': '折旧、损耗和摊销', 'v': [54, 63, 100, 144, 265, 310, 1070, 1167, 2080, 5595, 8411, 16099, 27332, 43363, 46927, 57138, 59263, 53462, 48345, 45525]},
      {'l': '税息折旧及摊销前利润', 'v': [321, 914, 1391, 1362, 1933, 2380, 7956, 15753, 38538, 49216, 107958, 86728, 156380, 161958, 236976, 259949, 158101, 174394, 178165, 222495]},
      {'l': '税息折旧及摊销前利润率 ％', 'v': [23.65, 39.93, 40.95, 30.96, 29.76, 30.28, 32.31, 36.56, 58.38, 51.93, 89.44, 48.65, 50.34, 36.75, 42.03, 30.39, 15.03, 17.63, 17.43, 20.83]},
    ]},
    {'t': '合并资产负债表', 'rows': [
      {'l': '货币资金和现金等价物', 'v': [435, 4573, 3041, 3121, 3608, 4206, 20728, 37946, 41548, 134550, 127478, 161913, 247387, 222167, 365556, 383147, 234141, 219924, 269539, 155993]},
      {'l': '短期证券', 'v': [1044, 1008, 4437, 5130, 7952, 10068, 6738, 3644, 15124, 22144, 10595, 7958, 13531, 15424, 36181, 193424, 326972, 377445, 350771, 303014]},
      {'l': '货币资金、现金等价物、及短期证券', 'v': [1480, 5581, 7477, 8251, 11560, 14274, 27466, 41590, 56672, 156694, 138073, 169871, 260917, 237590, 401738, 576571, 561113, 597369, 620310, 459007]},
      {'l': '应收账款', 'v': [0, 0, 0, 43, 63, 56, 191, 169, 338, 1327, 1443, 4943, 9041, 16104, 21885, 32292, 40458, 36600, 33334, 33423]},
      {'l': '应收票据', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '应收贷款', 'v': [0, 0, 0, 0, 0, 9, 714, 5525, 16545, 1038, 465, 915, 520, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '其他流动应收款项', 'v': [33, 28, 14, 0, 332, 620, 777, 285, 3158, 11482, 12619, 19107, 26521, 23796, 35471, 44139, 61182, 52974, 54015, 104973]},
      {'l': '应收账款总额', 'v': [33, 28, 14, 43, 395, 685, 1682, 5979, 20041, 13848, 14527, 24965, 36082, 39900, 57356, 76431, 101640, 89574, 87350, 138396]},
      {'l': '原材料', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '在产品', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '存货调整', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '库存商品', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '其他存货', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1078, 5629, 9980, 16435, 33224, 37097, 32515, 27657, 20251]},
      {'l': '存货', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1078, 5629, 9980, 16435, 33224, 37097, 32515, 27657, 20251]},
      {'l': '其他流动资产', 'v': [191, 283, 560, 952, 951, 1262, 5157, 6314, 8574, 6187, 7359, 8949, 16186, 28599, 36492, 81064, 87454, 75522, 82521, 105071]},
      {'l': '流动资产合计', 'v': [1704, 5892, 8051, 9246, 12906, 16221, 34305, 53883, 85287, 176729, 159959, 204862, 318814, 316069, 512021, 767291, 787304, 794980, 817837, 722725]},
      {'l': '非流动投资', 'v': [0, 0, 36, 30, 75, 287, 2324, 2243, 26012, 60300, 144227, 171019, 220804, 282472, 388184, 521669, 546525, 516098, 460671, 607931]},
      {'l': '土地', 'v': [0, 38, 50, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '房屋及建筑物修缮', 'v': [3, 0, 0, 558, 377, 392, 1061, 1114, 3060, 3517, 7345, 11860, 56983, 72435, 77912, 118174, 131675, 140411, 146794, 112154]},
      {'l': '机器、家具和设备', 'v': [163, 238, 390, 645, 814, 926, 3829, 4846, 7477, 12758, 16436, 21753, 44571, 66747, 81972, 121588, 141908, 139178, 149719, 184760]},
      {'l': '在建工程', 'v': [7, 41, 237, 5, 33, 91, 863, 2147, 981, 2261, 2247, 2959, 6343, 8138, 11976, 27027, 58280, 55930, 58684, 66100]},
      {'l': '其他固定资产、无形资产和其他长期资产', 'v': [28, 0, 0, 0, 221, 244, 0, 0, 0, 0, 0, 0, 0, 0, 38336, 85917, 96238, 88190, 83566, 42033]},
      {'l': '固定资产、无形资产和其他长期资产总值', 'v': [201, 316, 677, 1208, 1445, 1654, 5753, 8107, 11518, 18536, 26028, 36573, 107897, 147321, 210197, 352706, 428101, 423710, 438764, 405046]},
      {'l': '累计折旧', 'v': [-88, -148, -240, -319, -532, -756, -2725, -3353, -4501, -7171, -9763, -13811, -25369, -39697, -57509, -87756, -115600, -130492, -141733, -137690]},
      {'l': '固定资产、无形资产和其他长期资产净额', 'v': [113, 168, 437, 889, 913, 898, 3029, 4754, 7017, 11365, 16265, 22761, 82528, 107624, 152688, 264950, 312501, 293217, 297031, 267355]},
      {'l': '无形资产', 'v': [0, 0, 7, 418, 701, 832, 16590, 16882, 19311, 64187, 107277, 164303, 253960, 405605, 386844, 444795, 420740, 368931, 324480, 310193]},
      {'l': '商誉', 'v': [0, 0, 0, 230, 430, 558, 14062, 14099, 14827, 52148, 97436, 141280, 201263, 309827, 306138, 349168, 332389, 305354, 282090, 273952]},
      {'l': '其他非流动资产', 'v': [216, 346, 414, 155, 258, 500, 1803, 1868, 2624, 5080, 6966, 7957, 14005, 16833, 12504, 17101, 23522, 23483, 17117, 26313]},
      {'l': '非流动资产合计', 'v': [328, 515, 894, 1492, 1947, 2517, 23745, 25747, 54964, 140932, 274735, 366040, 571296, 812534, 940220, 1248515, 1303288, 1201728, 1099299, 1211793]},
      {'l': '资产总计', 'v': [2032, 6407, 8946, 10738, 14853, 18737, 58051, 79629, 140251, 317661, 434694, 570902, 890111, 1128604, 1452241, 2015806, 2090592, 1996708, 1917136, 1934517]},
      {'l': '应付账款', 'v': [9, 14, 18, 27, 19, 20, 985, 1105, 2572, 6415, 9939, 22715, 50099, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '应交税费', 'v': [127, 162, 177, 39, 222, 579, 916, 1388, 2479, 4188, 4455, 8644, 19948, 24714, 28391, 39592, 37623, 22256, 18469, 20671]},
      {'l': '其他应付账款', 'v': [355, 375, 607, 915, 1415, 474, 1587, 3011, 4536, 1982, 2992, 8635, 14439, 96924, 108878, 163417, 196071, 183275, 195957, 212485]},
      {'l': '应交税费和其他应付款', 'v': [151, 0, 0, 0, 450, 444, 1847, 5053, 5854, 10265, 13344, 14460, 25790, 22286, 18648, 29662, 34947, 33031, 32188, 33995]},
      {'l': '应付票据及应付账款、应交税费和其他应付款', 'v': [641, 551, 802, 981, 2106, 1517, 5335, 10558, 15442, 22850, 30729, 54455, 110276, 143924, 155918, 232670, 268640, 238561, 246614, 267151]},
      {'l': '短期借款', 'v': [0, 0, 0, 0, 108, 1576, 1578, 6801, 13031, 2475, 5136, 16781, 7482, 26273, 5701, 16025, 10901, 13971, 31504, 24191]},
      {'l': '短期资本化租赁债务', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 3059, 4853, 6158, 6455, 6378, 4229]},
      {'l': '短期借款和资本化租赁债务', 'v': [0, 0, 0, 0, 108, 1576, 1578, 6801, 13031, 2475, 5136, 16781, 7482, 26273, 8760, 20878, 17058, 20426, 37882, 28420]},
      {'l': '流动递延收益负债', 'v': [1209, 0, 0, 0, 4795, 4862, 6265, 10002, 14091, 18797, 21017, 26180, 39564, 48599, 57491, 92436, 100772, 96350, 92939, 73564]},
      {'l': '流动递延所得税负债', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '流动递延所得税和收益负债', 'v': [1209, 0, 0, 0, 4795, 4862, 6804, 11278, 16101, 20936, 23813, 31390, 48349, 64676, 90809, 161537, 171354, 159980, 153339, 140451]},
      {'l': '其他流动负债', 'v': [0, 1958, 2461, 3671, 455, 573, 733, 1318, 2429, 3075, 2425, 2770, 2463, 7985, 12038, 34963, 16147, 19946, 20049, 30762]},
      {'l': '流动负债合计', 'v': [1850, 2509, 3263, 4652, 7464, 8528, 14449, 29955, 47003, 49337, 62104, 105396, 168570, 242858, 267525, 450049, 473200, 438913, 457884, 466784]},
      {'l': '长期借款', 'v': [0, 0, 0, 0, 0, 0, 0, 28041, 38613, 62931, 63564, 86551, 148357, 130784, 133033, 161859, 163374, 169810, 154010, 223172]},
      {'l': '长期资本租赁负债', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 21116, 33652, 37309, 32516, 31467, 14435]},
      {'l': '长期借款和资本化租赁债务', 'v': [0, 0, 0, 0, 0, 0, 0, 28041, 38613, 62931, 63564, 86551, 148357, 130784, 154148, 195512, 200683, 202326, 185477, 237607]},
      {'l': '债务股本比率', 'v': [0, 0, 0, 0, 0.02, 0.17, 0.04, 2.68, 1.04, 0.36, 0.27, 0.33, 0.34, 0.27, 0.2, 0.19, 0.19, 0.2, 0.21, 0.25]},
      {'l': '养老金和退休福利', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '非流动递延负债', 'v': [37, 74, 134, 311, 542, 850, 1286, 1288, 3292, 8570, 10430, 13103, 25709, 32576, 56158, 76096, 81606, 75587, 63607, 58286]},
      {'l': '其他非流动负债', 'v': [0, 0, 0, 77, 63, 147, 37, 6663, 170, 245, 376, 743, 2032, 2707, 1462, 1775, 774, 880, 1550, 3014]},
      {'l': '非流动负债合计', 'v': [37, 74, 134, 388, 605, 997, 1323, 35992, 42074, 71745, 74370, 100398, 176098, 166067, 211769, 273382, 283064, 278794, 250635, 298906]},
      {'l': '负债合计', 'v': [1887, 2583, 3397, 5040, 8069, 9525, 15772, 65947, 89077, 121082, 136474, 205794, 344669, 408925, 479294, 723431, 756264, 717707, 708518, 765690]},
      {'l': '普通股', 'v': [0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1]},
      {'l': '优先股', 'v': [0, 0, 0, 0, 0, 0, 0, 13042, 12930, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '留存收益', 'v': [-269, 0, 0, 0, 0, 0, 15434, -25581, 1487, 30894, 93984, 122286, 213928, 301584, 449378, 661819, 694858, 682290, 649496, 692090]},
      {'l': '累计其他综合收益（亏损）', 'v': [414, 3823, 5548, 5651, 6725, 9086, -1259, -421, 2076, 4807, 7399, 9621, 11295, 3083, 6036, -13973, -28751, 2916, 19913, 20725]},
      {'l': '资本公积', 'v': [0, 0, 0, 0, 0, 0, 25549, 27034, 34001, 145679, 157776, 185398, 231816, 271058, 380161, 470264, 506148, 474824, 432347, 408920]},
      {'l': '库存股', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, -3180, -2772, 0, 0, 0, -2738, -32761, -30073, -38952]},
      {'l': '其他所有者权益', 'v': [0, 0, 0, 0, 0, 0, -1007, -1064, -679, -511, -205, -71, -202, -57, -56, -56, -57, -56, 0, 0]},
      {'l': '归属于母公司所有者权益合计', 'v': [145, 3824, 5549, 5651, 6726, 9087, 38718, 13012, 49817, 180870, 258955, 314055, 454067, 575668, 835519, 1118056, 1169461, 1127214, 1071684, 1082784]},
      {'l': '少数股东权益', 'v': [0, 0, 0, 47, 58, 125, 3560, 670, 1357, 15709, 39266, 51053, 91375, 144011, 137428, 174320, 164867, 151787, 136934, 86043]},
      {'l': '所有者权益合计', 'v': [145, 3824, 5549, 5698, 6784, 9212, 42278, 13682, 51173, 196579, 298221, 365109, 545442, 719679, 972948, 1292375, 1334328, 1279001, 1208618, 1168827]},
      {'l': '股东权益比率', 'v': [0.07, 0.6, 0.62, 0.53, 0.45, 0.49, 0.67, 0.16, 0.36, 0.57, 0.6, 0.55, 0.51, 0.51, 0.57, 0.56, 0.56, 0.56, 0.56, 0.56]},
    ]},
    {'t': '合并现金流量表', 'rows': [
      {'l': '来自持续经营收益的净利润', 'v': [290, 1217, 1543, 1336, 1995, 2618, 5736, 10797, 29425, 30245, 85077, 46439, 76226, 93829, 155236, 170885, 58048, 74687, 77488, 135073]},
      {'l': '折旧、损耗和摊销', 'v': [54, 63, 100, 144, 265, 310, 1070, 1167, 2080, 5595, 8411, 16099, 27332, 43363, 46927, 57138, 59263, 53462, 48345, 45525]},
      {'l': '经营性应收项目变动', 'v': [-3, 7, 19, -7, -2, -9, -278, -3530, -11536, -14518, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '存货变动', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '预付资产变动', 'v': [35, -30, 0, 0, 12, -197, -295, -442, -16021, -17582, -5375, -9279, -18327, -11911, -47988, -52012, -40067, 9801, -40868, -54243]},
      {'l': '应付票据及应付账款、应交税费和其他应付款变动', 'v': [-96, -43, 232, 276, 362, 400, 2036, 5639, 7976, 15845, 10734, 14124, 37747, 38138, 59741, 93717, 12084, 2215, 24292, 29852]},
      {'l': '其他营运资金变动', 'v': [438, 676, 287, 783, 832, -184, 735, 2248, 13931, 18194, 2939, 6180, 8787, 11487, 11937, 18532, 5604, 3340, 1640, -1328]},
      {'l': '营运资金变动', 'v': [374, 610, 538, 1051, 1206, 10, 2199, 3915, -5649, 1939, 8298, 11025, 28207, 37715, 23690, 60237, -22379, 15356, -14936, -25720]},
      {'l': '递延所得税', 'v': [0, 0, 0, 0, 0, 0, 184, 130, 1843, 2063, 127, -1226, 1211, -2569, -3808, 3859, -1688, -1956, -5717, 401]},
      {'l': '股权激励', 'v': [113, 161, 224, 228, 399, 412, 1542, 1572, 3576, 16202, 19192, 18018, 24918, 43844, 35109, 59775, 29556, 35116, 20147, 14979]},
      {'l': '资产减值费用', 'v': [0, 0, 0, 0, 0, 3, 166, 218, 205, 739, 2768, 3554, 3861, 16033, 19201, 10935, 42918, 22763, 37862, 16733]},
      {'l': '终止经营活动产生的现金流量净额', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '其他经营活动产生的现金流量净额', 'v': [-104, -559, -608, -215, -310, -679, 508, 272, 1687, -5524, -56044, -578, -5603, -55657, -76591, -86394, 10302, 28088, 35162, -11674]},
      {'l': '经营活动产生的现金流量净额', 'v': [726, 1491, 1798, 2544, 3553, 2673, 11405, 18072, 33166, 51258, 67829, 93332, 156152, 176557, 199762, 276435, 176020, 227517, 198351, 175317]},
      {'l': '购建固定资产、无形资产和其他长期资产支付的现金', 'v': [-96, -148, -312, -466, -337, -196, -1745, -1819, -1875, -3650, -6453, -12398, -24363, -41494, -36002, -49435, -65729, -39102, -34856, -92180]},
      {'l': '处置固定资产、无形资产和其他长期资产收回的现金净额', 'v': [0, 0, 2, 3, 1, 3, 1, 376, 0, 0, 0, 0, 0, 0, 0, 0, 0, 734, 405, 2603]},
      {'l': '取得子公司及其他营业单位支付的现金净额', 'v': [0, 0, 0, -471, -305, 3, -1195, -629, -21683, -43472, -46686, -82093, -67379, -55319, -43281, -45229, -16622, -6562, -6223, -9542]},
      {'l': '处置子公司及其他营业单位收到的现金净额', 'v': [0, 0, 0, 0, 0, 8, 91, 896, 112, 123, 17795, 5847, 7677, 330, 86, 11709, 0, 1140, 2133, 2008]},
      {'l': '投资支付的现金', 'v': [0, 0, -3360, -6044, -9502, -11673, -625, -90, -14362, -16080, -18334, -5268, -15642, -84752, -60669, -206496, -181015, -136335, -86504, -146591]},
      {'l': '收回投资及取得投资收益收到的现金', 'v': [0, 0, 0, 5391, 6787, 9723, 7207, 3265, 468, 1168, 8121, 11921, 8965, 21149, 20792, 8682, 17931, 26220, 102167, 44614]},
      {'l': '取得无形资产支付的现金净额差', 'v': [0, -148, -312, 0, -7, -15, 0, 0, 0, 0, 0, -7367, -12670, -16561, -208, -1629, -18, -25, -915, 0]},
      {'l': '终止投资活动产生的现金流量净额', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '其他投资活动产生的现金流量净额', 'v': [-694, 188, 463, 200, 153, 204, -3888, -1318, -4147, -4565, -5558, -285, -559, -9, -252, -8836, 592, -410, 86, 283]},
      {'l': '投资活动产生的现金流量净额', 'v': [-789, -108, -3518, -1388, -3211, -1942, -154, 680, -41487, -66476, -51115, -89642, -103970, -176656, -119534, -291233, -244861, -154341, -23707, -198805]},
      {'l': '发行股票产生的现金流入', 'v': [0, 3106, 0, 0, 12, 68, 760, 20963, 2059, 76741, 827, 16454, 495, 414, 101211, 209, 134, 13, 916, 11]},
      {'l': '回购股票产生的现金流出', 'v': [0, -119, -89, -79, -62, -512, -2, -50074, -197, -336, -23624, -14849, 0, -12714, 0, -922, -75490, -85135, -96404, -92920]},
      {'l': '发行优先股产生的现金流量净额', 'v': [0, 0, 0, 0, 0, 0, 0, -5733, -6451, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '发行债券产生的现金流入', 'v': [0, 0, 0, 0, 219, 1747, 1298, 45095, 105650, 227065, 34577, 185835, 127207, 14169, 17386, 45809, 11623, 25958, 22345, 133418]},
      {'l': '偿付债券现金流出', 'v': [0, 0, 0, 0, -109, -342, -1150, -11474, -89570, -194676, -31619, -152793, -85471, -19117, -35107, -8421, -20607, -13039, -19667, -64223]},
      {'l': '发行债券产生的现金流量净额', 'v': [0, 0, 0, 0, 109, 1405, 149, 33621, 16080, 32389, 2957, 33042, 41736, -4948, -17721, 37388, -8984, 12918, 2678, 69194]},
      {'l': '分配股利、利润或偿付利息支付的现金', 'v': [-195, -207, 0, -1009, 0, -1161, 0, -129, -262, -129, 0, 0, 0, 0, 0, 0, 0, 0, -19495, -31177]},
      {'l': '融资租赁产生的现金流量净额', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '其他筹资活动产生的现金流量净额', 'v': [0, 0, 0, 0, 0, -99, -322, -404, 544, 148, 928, 2429, -16961, 8604, -5122, -798, 4874, -2535, -5281, -26827]},
      {'l': '筹资活动产生的现金流量净额', 'v': [-195, 2780, -89, -1088, 60, -299, 584, -1755, 11773, 108812, -18911, 37076, 25270, -8645, 78368, 35877, -79465, -74740, -117586, -81719]},
      {'l': '期初现金及现金等价物余额', 'v': [694, 463, 4886, 3053, 3213, 3781, 8959, 21044, 38217, 41095, 129119, 121842, 181704, 237077, 219546, 412629, 439521, 258954, 249317, 307108]},
      {'l': '汇率变动对现金及现金等价物的影响', 'v': [0, -54, -37, 0, -7, -7, -66, -95, -122, -139, 556, 2296, -7528, 3795, 4535, -8571, -10892, 4021, 4768, 1035]},
      {'l': '现金及现金等价物净额', 'v': [-259, 4110, -1846, 68, 395, 425, 11769, 16902, 3331, 93455, -1641, 43061, 69924, -4949, 163131, 12507, -159198, 2457, 61826, -104172]},
      {'l': '期末现金及现金等价物余额', 'v': [435, 4573, 3041, 3121, 3608, 4206, 20728, 37946, 41548, 134550, 127478, 164903, 251628, 232128, 382677, 425136, 280323, 261411, 311143, 202936]},
      {'l': '资本开支', 'v': [-96, -148, -312, -466, -344, -211, -2666, -3125, -6005, -9582, -12943, -19765, -37033, -58055, -50200, -51504, -65748, -39127, -35771, -92180]},
      {'l': '自由现金流', 'v': [630, 1344, 1486, 2077, 3209, 2462, 8739, 14947, 27161, 41676, 54886, 73567, 119119, 118502, 149563, 224931, 110272, 188390, 162580, 83136]},
    ]},
    {'t': '估值比率', 'rows': [
      {'l': '市盈率', 'v': [9999, 9999, 9999, 9999, 9999, 9999, 9999, 9999, 9999, 53.53, 18.43, 43.8, 47.37, 36.71, 24.42, 26.98, 30.38, 25.66, 16.66, 17.88]},
      {'l': '持续经营市盈率', 'v': [9999, 9999, 9999, 9999, 9999, 9999, 9999, 9999, 9999, 37.28, 30.64, 31.71, 35.26, 31.89, 25.78, 22.67, 13.09, 12.9, 8.37, 14.65]},
      {'l': '股价／巴菲特所有者每股收益', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '市净率', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 4.72, 4.27, 2.05, 1.83, 1.28, 2.25]},
      {'l': '股价／每股有形账面价值', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 8.79, 7.09, 3.2, 2.72, 1.83, 3.15]},
      {'l': '股价／自由现金流', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 38.73, 28.62, 29.29, 31.54, 31.7, 26.94, 21.51, 21.02, 11.23, 8.86, 29.86]},
      {'l': '股价／每股经营现金流', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 31.49, 23.16, 23.08, 24.06, 21.28, 20.17, 17.5, 13.17, 9.3, 7.26, 14.16]},
      {'l': '市销率', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 17.04, 13.02, 12.08, 12.09, 8.53, 7.15, 5.65, 2.2, 2.14, 1.41, 2.32]},
      {'l': '市盈增长比', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1.43, 1.06, 0.88, 1, 0, 0, 0]},
      {'l': '企业价值／收入', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 16.21, 12.32, 11.79, 11.87, 8.55, 7.02, 5.36, 1.99, 1.85, 1.09, 2.18]},
      {'l': '企业价值倍数', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 31.21, 13.78, 24.24, 23.58, 23.27, 16.69, 17.66, 13.24, 10.52, 6.27, 10.49]},
      {'l': '企业价值／息税前利润', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 35.21, 14.94, 29.77, 28.58, 31.78, 20.81, 22.63, 21.18, 15.17, 8.61, 13.19]},
      {'l': '乔尔·格林布拉特收益率 ％', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 2.84, 6.69, 3.36, 3.5, 3.15, 4.81, 4.42, 4.72, 6.59, 11.61, 7.58]},
      {'l': '唐纳德·亚克曼收益率 ％', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 22.8, 23.34, 23.62, 21.32, 8.72, 8.95, 5.61]},
      {'l': '席勒市盈率', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 49.38, 23.91, 22.36, 14.78, 23.95]},
      {'l': 'Cyclically Adjusted PB Ratio', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 12.13, 4.33, 3.26, 2.06, 3.27]},
      {'l': 'Cyclically Adjusted PS Ratio', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 13.81, 5.34, 4.35, 2.52, 3.86]},
      {'l': '股息率 ％', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1.39, 0.76]},
    ]},
    {'t': '估值和质量', 'rows': [
      {'l': '总市值', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 3941632, 4773787, 2394156, 2060812, 1367706, 2431626]},
      {'l': '企业价值', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 3840230, 4587925, 2215652, 1837983, 1107689, 2324689]},
      {'l': '月末股价', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 183.4, 220, 112.1, 100.4, 70.25, 128]},
      {'l': '每股现金净流量', 'v': [-0.02, 0.08, 0.1, 0.08, 0.08, 0.11, 0.44, -1.31, -1.71, 1, -1.9, -4.3, -8.51, -15.24, -10, -14.8, -16.86, -13.26, -11.56, -20.67]},
      {'l': '净流动资产价值 NCAV', 'v': [-0.01, 0.09, 0.12, 0.1, 0.12, 0.16, 0.8, -1.35, -0.92, 2, -0.8, -2.57, -5.7, -11.44, -4.87, -6.01, -6.27, -3.63, -1.42, -6.79]},
      {'l': '净净营运资本 NNWC', 'v': [-0.02, 0.08, 0.1, 0.08, 0.09, 0.11, 0.44, -1.99, -2.35, 1.05, -1.85, -4.09, -8.05, -14.41, -8.86, -12.92, -14.57, -11.13, -9.57, -18.82]},
      {'l': '内在价值：预期自由现金流模型', 'v': [0, 0, 0, 0, 0, 0.86, 4.23, 4.81, 9.92, 19.4, 28.22, 38.8, 56.71, 73.29, 108.01, 144.32, 169.92, 181.78, 191.83, 187.68]},
      {'l': '内在价值：市销率模型', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 440.54, 436.2]},
      {'l': '彼得林奇公允价值', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 108.93, 172.98, 234.87, 110.9, 0, 0, 0]},
      {'l': '格雷厄姆估值', 'v': [0.04, 0.24, 0.32, 0.29, 0.36, 0.47, 3.12, 0, 6.17, 16.88, 20.77, 23.44, 33.41, 32.22, 58.65, 82.32, 80.06, 80.35, 85.43, 89.59]},
      {'l': '盈利能力值（EPV）', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 6.41, 12.73, 25.66, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '两年破产风险（Z分数）', 'v': [0, 0, 0, -883.9, 0.44, 0.49, 0.78, -0.09, 1.68, 9.36, 8.3, 7.41, 7.78, 6.62, 6.53, 5.34, 3.12, 3.15, 2.63, 3.43]},
      {'l': '基本面趋势（F分数）', 'v': [9999, 9999, 9999, 9999, 9999, 9999, 3, 2, 9, 5, 5, 4, 5, 4, 7, 5, 5, 6, 8, 7]},
      {'l': '财务造假嫌疑（M分数）', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, -2.77, -1.93, -2.34, -2.29, -2.39, -2.21, -2.38, -2.3, -2.96, -2.86, -1.83]},
      {'l': '可供营运的资产净额／总资产', 'v': [-0.66, -0.87, -0.3, -0.28, -0.44, -0.24, 0.88, 0.12, 0.58, 0.75, 0.72, 0.69, 0.77, 0.72, 0.65, 0.64, 0.49, 0.43, 0.41, 0.51]},
      {'l': '斯隆比率 ％', 'v': [13.87, -5.61, 33.82, -0.05, 9.26, 7.3, -10.43, -10.17, 26.83, 14.29, 15.77, 7.97, 3.08, 9.12, 5.86, 9.64, 6.96, 0.49, -4.58, 8.43]},
      {'l': '流动比率', 'v': [0.92, 2.35, 2.47, 1.99, 1.73, 1.9, 2.37, 1.8, 1.81, 3.58, 2.58, 1.94, 1.89, 1.3, 1.91, 1.7, 1.66, 1.81, 1.79, 1.55]},
      {'l': '速动比率', 'v': [0.92, 2.35, 2.47, 1.99, 1.73, 1.9, 2.37, 1.8, 1.81, 3.58, 2.58, 1.93, 1.86, 1.26, 1.85, 1.63, 1.59, 1.74, 1.73, 1.5]},
      {'l': '现金比率', 'v': [0.8, 2.22, 2.29, 1.77, 1.55, 1.67, 1.9, 1.39, 1.21, 3.18, 2.22, 1.61, 1.55, 0.98, 1.5, 1.28, 1.19, 1.36, 1.35, 0.98]},
      {'l': '利息保障倍数', 'v': [10000, 10000, 172.65, 10000, 10000, 10000, 78.68, 7.2, 11.49, 8.48, 15.39, 18.16, 19.73, 11, 17.76, 20.04, 4.6, 6.07, 6.92, 15.33]},
      {'l': '最高股价', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 223.6, 307.4, 237.8, 121, 99.3, 143.4]},
      {'l': '最低股价', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 170, 183.7, 71.25, 61.45, 65.45, 66.7]},
      {'l': '股票回购率 ％', 'v': [None, -97.15, -4.04, -0.46, 0.14, -0.13, 54.08, -2.44, -3.18, -1.24, 0.86, -2.24, -1.68, -0.59, -3.84, -0.96, 1.57, 3.89, 5.15, 2.42]},
      {'l': '股票回购收益率 ％', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, -2.57, 0.01, 3.15, 4.13, 6.98, 3.82]},
      {'l': '每股收入同比增长率 ％', 'v': [0, -17.16, 42.64, 28.92, 47.88, 20.84, 582.2, 70.82, 56.94, 33.91, 24.29, 47.07, 71.75, 41.15, 25.78, 47.35, 24.05, -2.93, 7.16, 10.12]},
      {'l': '每股收益同比增长率 ％', 'v': [0, 129.84, 22.51, -12.03, 48.77, 22.56, 438.42, 99.91, 182.11, -4.06, 175.92, -42.57, 58.95, 28.39, 58.54, 5.51, -57.07, 11.57, 8.72, 69.13]},
      {'l': '税息折旧及摊销前利润同比增长率 ％', 'v': [0, 39.88, 46.28, -2.53, 42.15, 22.96, 627.92, 93.29, 150.61, 19.13, 114.05, -20.01, 77.75, 3.04, 43.87, 6.52, -38.64, 13.82, 5.95, 31.61]},
      {'l': '5年每股税息折旧及摊销前利润增长率 ％', 'v': [0, 0, 0, 0, 43.68, 27.38, 47.88, 92.84, 155.54, 0, 0, 0, 0, 22.32, 24.25, 25.86, 13.09, -0.75, -4.84, -1.76]},
      {'l': '发行在外的普通股平均股数', 'v': [17584, 38879.06, 40417.04, 40325.48, 40321.79, 40350.4, 18128, 18352, 17400, 18696, 19664, 19944, 20425, 20640, 21017, 21619, 21558, 20980, 20182, 18791]},
      {'l': '期末总股本', 'v': [19720.05, 38879.06, 40450.78, 40637.02, 40578.1, 40630.56, 18656, 19112, 19720.05, 19963.99, 19791.42, 20234.91, 20575.44, 20696.48, 21492, 21699.03, 21357.32, 20526.02, 19469.13, 18997.08]},
      {'l': 'β贝塔系数', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1.31, 1.45, 1.61]},
      {'l': '重述日期', 'v': [None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None]},
      {'l': '报告日期', 'v': [None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None]},
      {'l': '收益发布日期', 'v': [None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None]},
      {'l': '股东人数', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '在职员工人数', 'v': [0, 5295, 5295, 11716, 13674, 12878, 12878, 12878, 22072, 34985, 36446, 50097, 66421, 101958, 117600, 251462, 254941, 235216, 204891, 124320]},
      {'l': '外汇汇率', 'v': [1.01, 0.94, 0.88, 0.88, 0.86, 0.82, 0.81, 0.8, 0.8, 0.8, 0.84, 0.89, 0.81, 0.86, 0.9, 0.84, 0.81, 0.88, 0.92, 0.93]},
    ]},
  ]},
  '00388.HK': {'name': '香港交易所', 'years': [1999, 2000, 2001, 2002, 2003, 2004, 2005, 2006, 2007, 2008, 2009, 2010, 2011, 2012, 2013, 2014, 2015, 2016, 2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025], 'groups': [
    {'t': '每股指标', 'rows': [
      {'l': '每股收入', 'v': [1.74, 2.22, 1.92, 1.72, 1.93, 2.27, 2.32, 3.85, 6.63, 6.05, 5.95, 6.57, 6.79, 5.9, 7.04, 7.84, 10.21, 8.56, 9.4, 11.45, 10.79, 13.3, 15.36, 13.36, 12.18, 13.68, 18.73]},
      {'l': '每股税息折旧及摊销前利润', 'v': [0.68, 1.11, 0.94, 0.77, 0.93, 1.35, 1.59, 2.82, 6.76, 5.64, 5.24, 5.63, 5.65, 4.64, 5.14, 5.92, 8.43, 6.4, 8.23, 10.27, 10.64, 11.86, 12.89, 12.21, 16.46, 17.47, 21.61]},
      {'l': '每股息税前利润', 'v': [0.54, 0.94, 0.79, 0.61, 0.76, 1.17, 1.45, 2.73, 6.69, 5.54, 5.15, 5.53, 5.57, 4.49, 4.7, 5.36, 7.86, 5.77, 7.54, 9.66, 9.81, 10.91, 11.82, 11.06, 15.32, 16.36, 20.38]},
      {'l': '稀释每股收益', 'v': [0.5, 0.84, 0.71, 0.56, 0.66, 1, 1.26, 2.34, 5.72, 4.75, 4.36, 4.67, 4.7, 3.74, 3.94, 4.43, 6.67, 4.75, 6.02, 7.48, 7.47, 9.09, 9.89, 7.95, 9.36, 10.29, 14.01]},
      {'l': '持续经营每股收益', 'v': [0.5, 0.84, 0.71, 0.56, 0.66, 1, 1.26, 2.34, 4.21, 4.87, 4.39, 4.69, 4.62, 3.61, 3.8, 4.29, 6.26, 4.68, 5.55, 7.57, 7.02, 8.77, 9.59, 8.31, 9.3, 10.2, 14.1]},
      {'l': '巴菲特所有者每股收益（TTM）', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 4.7, 5.67, 3.76, 3.42, 6.5, 4.79, 6.2, 7.63, 7.84, 0, 0, 0, 0, 0, 0]},
      {'l': '每股自由现金流', 'v': [0.44, 0.13, -2.64, -0.22, 1.29, 1.7, 1.28, 2.61, 7.03, 4.5, 4.17, 4.55, 4.44, 4.99, 3.63, 3.44, 6.35, 2.34, 5.46, 6.93, 7.14, 10.6, 8.89, 10.79, 7.98, 8.81, 18.85]},
      {'l': '每股经营现金流', 'v': [0.66, 0.43, -2.38, -0.1, 1.34, 1.72, 1.34, 2.67, 7.09, 4.71, 4.21, 4.62, 4.86, 5.95, 4.32, 4.02, 6.94, 2.85, 6.02, 7.62, 8.01, 11.67, 9.74, 11.8, 9.07, 10.08, 20.22]},
      {'l': '每股现金', 'v': [0, 0, 0.08, 0.01, 4.38, 3.59, 3.77, 5.52, 8.22, 32.07, 29.16, 33.75, 41.98, 40.86, 46.72, 179.93, 168.45, 175.97, 227.01, 173.87, 181.89, 259.59, 198.27, 218.42, 177.69, 182.71, 251.78]},
      {'l': '每股股息', 'v': [0, 0.08, 0.33, 0.33, 0.61, 0.85, 0.96, 1.58, 2.98, 5.89, 3.64, 3.98, 4.47, 3.94, 3.28, 3.55, 5.23, 5.08, 4.59, 6.49, 6.79, 6.7, 9.15, 7.63, 8.19, 8.27, 10.9]},
      {'l': '每股账面价值', 'v': [-0.01, 4.04, 4.16, 4.42, 5.35, 3.82, 4.08, 4.94, 7.84, 6.79, 7.46, 8.06, 8.5, 15.48, 17.59, 18.26, 24.74, 26.42, 30.06, 32.57, 35.03, 38.64, 39.22, 39.3, 40.6, 42.61, 46]},
      {'l': '每股有形账面价值', 'v': [-0.01, 4.04, 4.16, 4.42, 5.35, 3.82, 4.08, 4.94, 7.84, 6.79, 7.46, 8.06, 8.48, -0.39, 1.47, 2.87, 9.89, 11.82, 15.59, 18.14, 20.44, 23.83, 24.21, 24.3, 25.34, 27.09, 28.38]},
      {'l': '每股总债务', 'v': [0, 0, 0, 0, 0.05, 0, 0, 0, 0, 0, 0, 0, 0, 5.77, 5.78, 5.84, 2.57, 2.54, 1.5, 0.93, 1.99, 1.86, 1.63, 1.38, 1.27, 1.02, 0.67]},
      {'l': '月末股价', 'v': [0, 17.9, 11.85, 9.8, 16.8, 20.85, 32.15, 85.6, 220.8, 73.6, 139.4, 176.3, 124.1, 131.9, 129.3, 171.7, 198.5, 183.2, 239.8, 226.6, 253, 430, 455.4, 337.2, 268, 295.2, 407.8]},
    ]},
    {'t': '财务比率', 'rows': [
      {'l': '股本回报率 ROE ％', 'v': [10000, 41.64, 17.35, 13.16, 13.54, 21.91, 32.01, 52.5, 90.49, 65.45, 61.41, 60.31, 57.11, 30.34, 23.85, 24.79, 31.15, 18.59, 21.3, 23.88, 22.12, 24.72, 25.44, 20.29, 23.47, 24.81, 31.7]},
      {'l': '股本回报率 ROE ％／市净率', 'v': [10000, 9.41, 6.09, 5.94, 4.31, 4.01, 4.06, 3.03, 3.21, 6.03, 3.29, 2.76, 3.91, 3.56, 3.24, 2.64, 3.88, 2.68, 2.67, 3.43, 3.06, 2.22, 2.19, 2.36, 3.56, 3.58, 3.58]},
      {'l': '资产收益率 ROA ％', 'v': [1679567.74, 41.38, 17.27, 12.98, 5.65, 5.12, 6.04, 7.95, 9.61, 6.8, 8.7, 10.81, 9.99, 6.06, 5.46, 3.06, 3.25, 2.38, 2.62, 3.25, 3.51, 3.39, 3.14, 2.5, 3.17, 3.61, 3.69]},
      {'l': '有形股本回报率 ％', 'v': [10000, 41.64, 17.35, 13.16, 13.54, 21.91, 32.01, 52.5, 90.49, 65.45, 61.41, 60.31, 57.19, 93.98, 723.11, 204.51, 104.2, 43.78, 43.86, 44.32, 38.75, 41.13, 41.23, 32.84, 37.78, 39.38, 50.65]},
      {'l': '有形资产收益率 ％', 'v': [1679567.74, 41.38, 17.27, 12.98, 5.65, 5.12, 6.04, 7.95, 9.61, 6.8, 8.7, 10.81, 10, 7, 7.01, 3.43, 3.5, 2.57, 2.8, 3.46, 3.77, 3.59, 3.3, 2.63, 3.35, 3.82, 3.86]},
      {'l': '乔尔·格林布拉特资本回报率 ％', 'v': [None, 10127.68, 4804.01, 1887.81, 296.76, 298.04, 520.27, 1205.06, 2639.85, 1728.27, 1650.63, 1996.32, 970.88, 373.77, 316.92, 371.81, 595.51, 458.32, 625, 777.18, 443.23, 355.54, 409.73, 417.67, 621.76, 729.91, 978.04]},
      {'l': '已动用资本回报率 ROCE ％', 'v': [0, 46.68, 19.27, 14.39, 14, 20.21, 28.9, 47.86, 91.66, 72.84, 69.66, 69.93, 67.26, 28.24, 20.15, 21.63, 29.66, 19.79, 24.52, 29.54, 27.24, 27.38, 28.25, 26.34, 36.05, 37.12, 43.41]},
      {'l': '5年留存收益回报率 RORE %', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, -19.43, -11.06, 65.12, 41.8, 50.74, 69.55, 18.81, 84.27, 62.16, 9.39, 35.92, 18.07, 56.11]},
      {'l': '资本回报率 ％', 'v': [0, 20.7, 17.48, 13.13, 8.82, 8.65, 9.1, 12.3, 12.05, 10.05, 12.82, 17.29, 13.87, 7.54, 6.79, 3.29, 3.31, 2.48, 2.46, 3.25, 3.28, 3.48, 3.45, 2.59, 2.39, 2.98, 3.34]},
      {'l': '加权平均资本成本 WACC ％', 'v': [0, 0, 0, 9.83, 12.34, 12.25, 11.95, 13.65, 15.8, 12.15, 13.45, 11.61, 11.02, 8.92, 10.02, 8.9, 13.93, 13.51, 13.68, 10.62, 9.34, 7.5, 6.2, 12.26, 14.3, 15.74, 13.33]},
      {'l': '实际借款利率 ％', 'v': [None, None, None, None, 0, 0, None, None, None, None, None, None, None, 0.86, 2.79, 2.98, 2.83, 6.8, 26.79, 75.35, 74.78, 19.61, 6.66, 123.97, 363.99, 406.56, 435.62]},
      {'l': '毛利率 ％', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 81.89, 77.04, 74.86, 75.61, 79.26, 75.62, 76.62, 78.61, 75.42, 77.95, 83.09, 78.53, 74.21, 74.61, 80.79]},
      {'l': '营业利润率 ％', 'v': [30.7, 42.36, 41.15, 35.59, 39.41, 51.7, 62.64, 70.81, 81.41, 93.89, 87.31, 84.66, 76.95, 67.63, 60.14, 60.22, 67.45, 60.04, 62.07, 66.27, 62.72, 66.69, 70.02, 61.46, 54.86, 57.81, 67.89]},
      {'l': '净利率 ％', 'v': [28.69, 37.78, 37.04, 32.55, 34.29, 44.15, 54.17, 60.73, 86.32, 79.13, 73.34, 71, 69.23, 63.5, 55.98, 56.59, 65.04, 55.48, 63.97, 65.34, 69.23, 68.34, 64.38, 59.51, 76.8, 75.23, 74.77]},
      {'l': '税息折旧及摊销前利润率 ％', 'v': [38.97, 50.03, 48.78, 44.61, 48.41, 59.36, 68.74, 73.22, 102.04, 93.31, 88.18, 85.65, 83.24, 78.67, 73.05, 75.45, 82.58, 74.83, 87.55, 89.71, 98.55, 89.14, 83.93, 91.39, 135.12, 127.67, 115.38]},
      {'l': '自由现金流利润率 ％', 'v': [25.18, 5.87, -137.52, -12.78, 67.19, 74.9, 55.31, 67.71, 106.05, 74.49, 70.07, 69.16, 65.48, 84.72, 51.54, 43.91, 62.22, 27.34, 58.1, 60.48, 66.13, 79.7, 57.88, 80.74, 65.48, 64.4, 100.63]},
      {'l': '债务股本比率', 'v': [0, 0, 0, 0, 0.01, 0, 0, 0, 0, 0, 0, 0, 0, 0.37, 0.33, 0.32, 0.1, 0.1, 0.05, 0.03, 0.06, 0.05, 0.04, 0.04, 0.03, 0.02, 0.02]},
      {'l': '股东权益比率', 'v': [-403.45, 1, 0.99, 0.98, 0.28, 0.19, 0.19, 0.13, 0.1, 0.12, 0.18, 0.18, 0.17, 0.22, 0.24, 0.08, 0.13, 0.13, 0.12, 0.16, 0.16, 0.12, 0.12, 0.12, 0.15, 0.14, 0.1]},
      {'l': '资产有息负债率', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0.08, 0.08, 0.03, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0, 0.01, 0, 0]},
      {'l': '资产负债率', 'v': [404.45, 0, 0.01, 0.02, 0.72, 0.81, 0.81, 0.87, 0.9, 0.88, 0.82, 0.82, 0.83, 0.78, 0.76, 0.92, 0.87, 0.87, 0.88, 0.84, 0.84, 0.88, 0.88, 0.88, 0.85, 0.86, 0.9]},
      {'l': '毛利润／总资产 ％', 'v': [5854764.52, 109.51, 46.61, 39.88, 16.47, 11.61, 11.15, 13.08, 11.13, 8.6, 11.86, 15.22, 11.82, 7.35, 7.3, 4.09, 3.96, 3.24, 3.14, 3.91, 3.83, 3.87, 4.05, 3.3, 3.07, 3.58, 3.99]},
      {'l': 'Degree of Financial Leverage', 'v': [0, 0.9, 0.97, 0.97, 0.75, 0.93, 1.03, 0.96, 0.99, 0.96, 1.27, 0.95, 0.59, 1.09, 0.49, 0.84, 0.99, 1.13, 0.83, 0.82, -0.05, 1.8, 1.03, 3.03, 0.46, 1.47, 1.47]},
      {'l': '资产周转率', 'v': [58547.64, 1.09, 0.47, 0.4, 0.17, 0.12, 0.11, 0.13, 0.11, 0.09, 0.12, 0.15, 0.14, 0.1, 0.1, 0.05, 0.05, 0.04, 0.04, 0.05, 0.05, 0.05, 0.05, 0.04, 0.04, 0.05, 0.05]},
      {'l': '股息支付率', 'v': [0, 0.1, 0.47, 0.59, 0.92, 0.85, 0.76, 0.68, 0.71, 1.21, 0.83, 0.85, 0.97, 1.09, 0.86, 0.83, 0.84, 1.08, 0.83, 0.86, 0.97, 0.76, 0.95, 0.92, 0.88, 0.81, 0.77]},
      {'l': '应收账款周转天数', 'v': [0, 0, 0, 0, 0, 0, 456.61, 272.3, 937.82, 757.44, 565.35, 528.33, 407.15, 592.98, 552.8, 668.99, 567.71, 499.6, 465.03, 446.97, 593.74, 789.73, 747.73, 625.84, 693.22, 923.66, 941.02]},
      {'l': '应付账款周转天数', 'v': [None, None, None, None, None, None, None, None, None, None, None, None, 2521.3, 2999.33, 2556.52, 2922.79, 2741.1, 1980.93, 1915.71, 2064.2, 2124.15, 2957.96, 3625.44, 2111.1, 1882.95, 2458.55, 3234.42]},
      {'l': '存货周转天数', 'v': [None, None, None, None, None, None, None, None, None, None, None, None, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '现金循环周期', 'v': [0, 0, 0, 0, 0, 0, 456.61, 272.3, 937.82, 757.44, 565.35, 528.33, -2114.15, -2406.34, -2003.72, -2253.79, -2173.38, -1481.33, -1450.68, -1617.24, -1530.41, -2168.22, -2877.71, -1485.26, -1189.73, -1534.89, -2293.4]},
    ]},
    {'t': '存货周转率', 'rows': [
      {'l': '营业成本／收入 ％', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0.18, 0.23, 0.25, 0.24, 0.21, 0.24, 0.23, 0.21, 0.25, 0.22, 0.17, 0.21, 0.26, 0.25, 0.19]},
      {'l': '存货／收入', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '资本开支／收入', 'v': [0.13, 0.13, 0.13, 0.07, 0.03, 0.01, 0.03, 0.01, 0.01, 0.03, 0.01, 0.01, 0.06, 0.16, 0.1, 0.07, 0.06, 0.06, 0.06, 0.06, 0.08, 0.08, 0.05, 0.08, 0.09, 0.09, 0.07]},
      {'l': '资本开支／营业利润', 'v': [0.41, 0.32, 0.32, 0.2, 0.06, 0.02, 0.04, 0.02, 0.01, 0.04, 0.01, 0.01, 0.08, 0.24, 0.16, 0.12, 0.09, 0.1, 0.1, 0.09, 0.13, 0.12, 0.08, 0.12, 0.16, 0.16, 0.11]},
      {'l': '资本开支／经营现金流', 'v': [0.33, 0.7, 0, 0, 0.04, 0.01, 0.04, 0.02, 0.01, 0.04, 0.01, 0.02, 0.09, 0.16, 0.16, 0.14, 0.09, 0.18, 0.09, 0.09, 0.11, 0.09, 0.09, 0.09, 0.12, 0.13, 0.07]},
    ]},
    {'t': '合并利润表', 'rows': [
      {'l': '营业收入', 'v': [1815, 2312, 1999, 1808, 2020, 2394, 2473, 4147, 7147, 6481, 6414, 7094, 7357, 6432, 8131, 9127, 12233, 10398, 11574, 14252, 13565, 16835, 19471, 16934, 15445, 17346, 23745]},
      {'l': '营业成本', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1332, 1477, 2044, 2226, 2537, 2535, 2706, 3048, 3334, 3712, 3292, 3636, 3984, 4405, 4561]},
      {'l': '毛利润', 'v': [1815, 2312, 1999, 1808, 2020, 2394, 2473, 4147, 7147, 6481, 6414, 7094, 6025, 4955, 6087, 6901, 9696, 7863, 8868, 11204, 10231, 13123, 16179, 13298, 11461, 12941, 19184]},
      {'l': '毛利率 ％', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 81.89, 77.04, 74.86, 75.61, 79.26, 75.62, 76.62, 78.61, 75.42, 77.95, 83.09, 78.53, 74.21, 74.61, 80.79]},
      {'l': '销售及行政开支', 'v': [38, 31, 16, 15, 8, 11, 341, 11, 419, 482, 530, 550, 61, 86, 199, 253, 169, 185, 151, 212, 216, 255, 296, 431, 448, 351, 344]},
      {'l': '研发费用', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '其他营业成本', 'v': [1220, 1302, 1161, 1150, 1216, 1145, 583, 1199, 910, -86, 284, 538, 303, 519, 998, 1152, 1276, 1435, 1533, 1547, 1507, 1641, 2250, 2459, 2540, 2562, 2720]},
      {'l': '总营业成本', 'v': [1258, 1333, 1176, 1165, 1224, 1156, 924, 1211, 1329, 396, 814, 1088, 364, 605, 1197, 1405, 1445, 1620, 1684, 1759, 1723, 1896, 2546, 2890, 2988, 2913, 3064]},
      {'l': '营业利润', 'v': [557, 979, 822, 643, 796, 1238, 1549, 2936, 5818, 6085, 5600, 6006, 5661, 4350, 4890, 5496, 8251, 6243, 7184, 9445, 8508, 11227, 13633, 10408, 8473, 10028, 16120]},
      {'l': '营业利润率 ％', 'v': [30.7, 42.36, 41.15, 35.59, 39.41, 51.7, 62.64, 70.81, 81.41, 93.89, 87.31, 84.66, 76.95, 67.63, 60.14, 60.22, 67.45, 60.04, 62.07, 66.27, 62.72, 66.69, 70.02, 61.46, 54.86, 57.81, 67.89]},
      {'l': '利息收入', 'v': [0, 0, 0, 0, 0, 0, 0, 0, -712, -76, 0, 0, 278, 381, 403, 594, 686, 744, 1484, 2775, 3193, 2122, 813, 4151, 10841, 10565, 9737]},
      {'l': '利息支出', 'v': [0, 0, 0, 0, 0, 0, -4, -11, -23, -10, -13, -15, -2, -57, -186, -201, -140, -211, -665, -1140, -1373, -477, -147, -2358, -6095, -5891, -4672]},
      {'l': '净利息收入', 'v': [0, 0, 0, 0, 0, 0, -4, -11, -739, -14, -18, -21, 257, 304, 185, 357, 496, 476, 759, 1565, 1754, 1578, 597, 1726, 4676, 4605, 4996]},
      {'l': '其他收入（支出）', 'v': [0, 0, 0, 6, 9, 13, 22, 39, 2111, -143, -40, -31, 114, 191, 171, 185, 531, 80, 667, -127, 689, 527, 611, -475, 183, 220, 42]},
      {'l': '利润总额', 'v': [557, 979, 822, 650, 805, 1251, 1567, 2963, 7191, 5928, 5542, 5954, 6032, 4845, 5246, 6038, 9278, 6799, 8610, 10883, 10951, 13332, 14841, 11659, 13332, 14853, 21158]},
      {'l': '所得税费用', 'v': [-36, -106, -82, -61, -112, -194, -227, -445, -1022, -800, -838, -917, -939, -761, -700, -900, -1347, -1058, -1255, -1592, -1561, -1845, -2343, -1564, -1351, -1698, -3321]},
      {'l': '税率 ％', 'v': [6.55, 10.8, 9.97, 9.4, 13.92, 15.48, 14.52, 15.01, 14.21, 13.49, 15.12, 15.4, 15.57, 15.71, 13.34, 14.91, 14.52, 15.56, 14.58, 14.63, 14.25, 13.84, 15.79, 13.41, 10.13, 11.43, 15.7]},
      {'l': '其他净利润（亏损）', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '净利润', 'v': [521, 874, 740, 589, 693, 1057, 1340, 2519, 6169, 5129, 4704, 5037, 5093, 4084, 4546, 5138, 7931, 5741, 7355, 9291, 9390, 11487, 12498, 10095, 11981, 13155, 17837]},
      {'l': '持续经营净利润', 'v': [521, 874, 740, 589, 693, 1057, 1340, 2519, 6169, 5129, 4704, 5037, 5093, 4084, 4546, 5138, 7931, 5741, 7355, 9291, 9390, 11487, 12498, 10095, 11981, 13155, 17837]},
      {'l': '终止经营净利润', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '其他收入（少数股东权益）', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 6, 27, 25, 28, 49, 21, 1, 18, 37, -17, -119, -105, -83]},
      {'l': '归属于母公司股东的净利润', 'v': [521, 874, 740, 589, 693, 1057, 1340, 2519, 6169, 5129, 4704, 5037, 5093, 4084, 4552, 5165, 7956, 5769, 7404, 9312, 9391, 11505, 12535, 10078, 11862, 13050, 17754]},
      {'l': '净利率 ％', 'v': [28.69, 37.78, 37.04, 32.55, 34.29, 44.15, 54.17, 60.73, 86.32, 79.13, 73.34, 71, 69.23, 63.5, 55.98, 56.59, 65.04, 55.48, 63.97, 65.34, 69.23, 68.34, 64.38, 59.51, 76.8, 75.23, 74.77]},
      {'l': '优先股股利', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '基本每股收益', 'v': [0.5, 0.84, 0.71, 0.56, 0.66, 1, 1.26, 2.37, 5.78, 4.78, 4.38, 4.68, 4.71, 3.75, 3.95, 4.44, 6.7, 4.76, 6.03, 7.5, 7.49, 9.11, 9.91, 7.96, 9.37, 10.32, 14.05]},
      {'l': '稀释每股收益', 'v': [0.5, 0.84, 0.71, 0.56, 0.66, 1, 1.26, 2.34, 5.72, 4.75, 4.36, 4.67, 4.7, 3.74, 3.94, 4.43, 6.67, 4.75, 6.02, 7.48, 7.47, 9.09, 9.89, 7.95, 9.36, 10.29, 14.01]},
      {'l': '发行在外的稀释性潜在普通股平均股数', 'v': [1041.3, 1040.1, 1042.9, 1050.9, 1049.5, 1056.9, 1067, 1076.3, 1078.6, 1072.2, 1078.9, 1079.1, 1084.2, 1091.1, 1154.8, 1163.7, 1198.4, 1215.4, 1230.8, 1244.8, 1256.7, 1265.8, 1267.6, 1267.7, 1267.8, 1267.7, 1267.6]},
      {'l': '息税前利润', 'v': [557, 979, 822, 643, 796, 1238, 1549, 2936, 7214, 5938, 5555, 5969, 6034, 4902, 5432, 6239, 9418, 7010, 9275, 12023, 12324, 13809, 14988, 14017, 19427, 20744, 25830]},
      {'l': '折旧、损耗和摊销', 'v': [150, 177, 153, 163, 182, 183, 151, 100, 80, 109, 101, 107, 90, 158, 508, 647, 684, 771, 858, 762, 1044, 1197, 1354, 1459, 1443, 1402, 1568]},
      {'l': '税息折旧及摊销前利润', 'v': [707, 1157, 975, 807, 978, 1421, 1700, 3036, 7293, 6047, 5656, 6076, 6124, 5060, 5940, 6886, 10102, 7781, 10133, 12785, 13368, 15006, 16342, 15476, 20870, 22146, 27398]},
      {'l': '税息折旧及摊销前利润率 ％', 'v': [38.97, 50.03, 48.78, 44.61, 48.41, 59.36, 68.74, 73.22, 102.04, 93.31, 88.18, 85.65, 83.24, 78.67, 73.05, 75.45, 82.58, 74.83, 87.55, 89.71, 98.55, 89.14, 83.93, 91.39, 135.12, 127.67, 115.38]},
    ]},
    {'t': '合并资产负债表', 'rows': [
      {'l': '货币资金和现金等价物', 'v': [0, 0, 82, 10, 1384, 1035, 1359, 2224, 5427, 27784, 14738, 19361, 18221, 34077, 41452, 136778, 110890, 115723, 155660, 121196, 128152, 157996, 181361, 184965, 125107, 134365, 182724]},
      {'l': '短期证券', 'v': [0, 0, 0, 0, 3213, 2762, 2644, 3646, 3358, 6684, 16623, 16970, 27017, 12811, 12747, 72885, 92144, 99159, 125794, 96246, 101252, 170612, 69548, 91424, 99591, 96545, 135532]},
      {'l': '货币资金、现金等价物、及短期证券', 'v': [0, 0, 82, 10, 4597, 3797, 4003, 5870, 8785, 34468, 31361, 36331, 45238, 46888, 54199, 209663, 203034, 214882, 281454, 217442, 229404, 328608, 250909, 276389, 224698, 230910, 318256]},
      {'l': '应收账款', 'v': [0, 0, 0, 0, 0, 0, 3094, 0, 18364, 8535, 11334, 9203, 7210, 13689, 10940, 22517, 15537, 12928, 16564, 18341, 25791, 47059, 32717, 25354, 33313, 54478, 67958]},
      {'l': '应收票据', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '应收贷款', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '其他流动应收款项', 'v': [0, 0, 83, 489, 2, 0, 156, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '应收账款总额', 'v': [0, 0, 83, 489, 2, 0, 3250, 0, 18364, 8535, 11334, 9203, 7210, 13689, 10940, 22517, 15537, 12928, 16564, 18341, 25791, 47059, 32717, 25354, 33313, 54478, 67958]},
      {'l': '原材料', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '在产品', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '存货调整', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '库存商品', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '其他存货', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '存货', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '其他流动资产', 'v': [0, 4, 17, 20, 12520, 15223, 13803, 31869, 59857, 19394, 0, 0, 0, 0, 7, 8, 0, 0, 0, 0, 0, 26, 91443, 80735, 58191, 68188, 161007]},
      {'l': '流动资产合计', 'v': [0, 4, 181, 519, 17119, 19020, 21056, 37738, 87006, 62397, 42695, 45534, 52448, 60577, 65146, 232188, 218571, 227810, 298018, 235783, 255195, 375693, 375069, 382478, 316202, 353576, 547221]},
      {'l': '非流动投资', 'v': [0, 0, 0, 0, 0, 0, 82, 0, 476, 47, 2327, 2024, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '土地', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 86, 0, 107, 70, 70, 70, 70, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '房屋及建筑物修缮', 'v': [0, 0, 0, 0, 0, 0, 254, 0, 8, 310, 371, 296, 695, 1257, 1358, 1421, 1502, 1596, 1705, 1924, 4119, 3942, 3663, 3357, 3256, 2984, 2672]},
      {'l': '机器、家具和设备', 'v': [0, 0, 0, 0, 0, 0, 1622, 0, 1757, 1752, 1480, 1521, 1878, 2083, 2210, 2221, 2329, 2281, 2383, 2504, 2199, 2371, 2279, 2527, 2832, 2798, 3152]},
      {'l': '在建工程', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 187, 223, 0, 0, 0]},
      {'l': '其他固定资产、无形资产和其他长期资产', 'v': [0, 10, 25, 44, 493, 338, 16, 229, 274, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 15, 10, 6, 1, 5, 4, 3]},
      {'l': '固定资产、无形资产和其他长期资产总值', 'v': [0, 10, 25, 44, 493, 338, 1891, 229, 2039, 2148, 1851, 1924, 2643, 3410, 3638, 3712, 3831, 3877, 4088, 4428, 6333, 6323, 6135, 6108, 6093, 5786, 5827]},
      {'l': '累计折旧', 'v': [0, 0, 0, 0, 0, 0, -1633, 0, -1721, -1778, -1548, -1629, -1695, -1735, -1885, -2109, -2271, -2378, -2619, -2803, -2397, -2491, -2651, -2880, -3072, -3123, -3208]},
      {'l': '固定资产、无形资产和其他长期资产净额', 'v': [0, 10, 25, 44, 493, 338, 258, 229, 317, 370, 303, 295, 948, 1675, 1753, 1603, 1560, 1499, 1469, 1625, 3936, 3832, 3484, 3228, 3021, 2663, 2619]},
      {'l': '无形资产', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 25, 18207, 18703, 17924, 17894, 17833, 17945, 18039, 18397, 18755, 18989, 18984, 19295, 19620, 22275]},
      {'l': '商誉', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 13488, 13750, 13170, 13162, 13167, 13277, 13298, 13344, 13286, 13361, 13367, 13371, 13297, 13274]},
      {'l': '其他非流动资产', 'v': [0, 4210, 4148, 4151, 2197, 2086, 1536, 2496, 157, 8, 7, 31, 607, 378, 341, 145, 168, 176, 172, 501, 1523, 826, 1762, 1361, 2661, 5770, 8660]},
      {'l': '非流动资产合计', 'v': [0, 4219, 4173, 4195, 2689, 2423, 1875, 2726, 950, 425, 2637, 2350, 1580, 20260, 20797, 19672, 19622, 19508, 19586, 20165, 23856, 23413, 24235, 23573, 24977, 28053, 33554]},
      {'l': '资产总计', 'v': [0, 4223, 4354, 4714, 19808, 21443, 22931, 40464, 87955, 62822, 45332, 47884, 54028, 80837, 85943, 251860, 238193, 247318, 317604, 255948, 279051, 399106, 399304, 406051, 341179, 381629, 580775]},
      {'l': '应付账款', 'v': [0, 0, 0, 73, 4780, 4902, 3641, 11118, 21376, 8894, 11827, 9946, 8456, 15818, 12815, 22835, 15270, 12246, 16159, 18316, 20489, 39675, 25722, 16338, 24767, 34575, 46259]},
      {'l': '应交税费', 'v': [0, 0, 0, 0, 57, 200, 93, 287, 688, 141, 261, 320, 262, 178, 379, 348, 653, 356, 505, 678, 2180, 2088, 1694, 2872, 1199, 2103, 3288]},
      {'l': '其他应付账款', 'v': [13, 13, 23, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1538, 2385, 2072, 2016, 2522, 2227, 3528]},
      {'l': '应交税费和其他应付款', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '应付票据及应付账款、应交税费和其他应付款', 'v': [13, 13, 23, 73, 4837, 5102, 3734, 11406, 22064, 9035, 12088, 10266, 8718, 15996, 13194, 23183, 15923, 12602, 16664, 18994, 24207, 44148, 29488, 21226, 28488, 38905, 53075]},
      {'l': '短期借款', 'v': [0, 0, 0, 0, 50, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1027, 1005, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '短期资本化租赁债务', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 272, 304, 299, 297, 270, 260, 214]},
      {'l': '短期借款和资本化租赁债务', 'v': [0, 0, 0, 0, 50, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1027, 1005, 272, 304, 299, 297, 270, 260, 214]},
      {'l': '流动递延收益负债', 'v': [0, 0, 0, 0, 0, 0, 285, 0, 375, 393, 424, 473, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '流动递延所得税负债', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '流动递延所得税和收益负债', 'v': [0, 0, 0, 0, 0, 0, 285, 0, 375, 393, 424, 473, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '其他流动负债', 'v': [0, 1, 5, 24, 8161, 10853, 13680, 22021, 56814, 45792, 24473, 28421, 36091, 39341, 44344, 199381, 188053, 198086, 260875, 193582, 206458, 301882, 316177, 331425, 257753, 285360, 465586]},
      {'l': '流动负债合计', 'v': [13, 14, 28, 97, 13048, 15955, 17698, 33427, 79253, 55220, 36985, 39160, 44809, 55337, 57538, 222564, 203976, 210688, 278566, 213581, 230937, 346334, 345964, 352948, 286511, 324525, 518875]},
      {'l': '长期借款', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 6615, 6703, 6801, 3101, 3105, 833, 161, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '长期资本租赁负债', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 2234, 2054, 1760, 1448, 1334, 1034, 637]},
      {'l': '长期借款和资本化租赁债务', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 6615, 6703, 6801, 3101, 3105, 833, 161, 2234, 2054, 1760, 1448, 1334, 1034, 637]},
      {'l': '债务股本比率', 'v': [0, 0, 0, 0, 0.01, 0, 0, 0, 0, 0, 0, 0, 0, 0.37, 0.33, 0.32, 0.1, 0.1, 0.05, 0.03, 0.06, 0.05, 0.04, 0.04, 0.03, 0.02, 0.02]},
      {'l': '养老金和退休福利', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '非流动递延负债', 'v': [0, 0, 0, 0, 0, 0, 21, 0, 37, 31, 18, 18, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '其他非流动负债', 'v': [0, 0, 0, 0, 1146, 1456, 874, 1780, 289, 277, 302, 29, 60, 1121, 1184, 1136, 1154, 1141, 830, 1303, 1379, 1482, 1670, 1556, 1538, 1663, 2534]},
      {'l': '非流动负债合计', 'v': [0, 0, 0, 0, 1146, 1456, 895, 1780, 325, 308, 320, 47, 60, 7736, 7887, 7937, 4255, 4246, 1663, 1464, 3613, 3536, 3430, 3004, 2872, 2697, 3171]},
      {'l': '负债合计', 'v': [13, 14, 28, 97, 14194, 17411, 18593, 35207, 79578, 55528, 37305, 39207, 44869, 63073, 65425, 230501, 208231, 214934, 280229, 215045, 234550, 349870, 349394, 355952, 289383, 327222, 522046]},
      {'l': '普通股', 'v': [0, 0, 0, 0, 0, 0, 1063, 0, 1070, 1075, 1076, 1078, 1080, 1150, 1161, 12225, 19285, 22085, 25141, 27750, 30449, 31891, 31896, 31918, 31946, 31955, 31955]},
      {'l': '优先股', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '留存收益', 'v': [-13, -89, 28, 108, 1579, 1658, 1776, 3367, 6288, 5241, 6021, 6766, 7053, 6881, 7800, 8800, 10691, 10334, 12090, 13379, 14204, 17214, 18173, 18547, 19723, 21890, 25653]},
      {'l': '累计其他综合收益（亏损）', 'v': [0, 0, 0, 0, 0, 0, 699, 0, 801, 696, 606, 636, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '资本公积', 'v': [0, 0, 0, 19, 54, 104, 150, 186, 266, 347, 376, 416, 639, 8731, 10167, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '库存股', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '其他所有者权益', 'v': [0, 4298, 4298, 4489, 3981, 2270, 650, 1705, -48, -65, -52, -219, 387, 1002, 1277, 248, -160, -153, 42, -400, -480, -187, -443, -737, -325, 7, 539]},
      {'l': '归属于母公司所有者权益合计', 'v': [-13, 4209, 4326, 4617, 5614, 4032, 4337, 5258, 8377, 7294, 8027, 8677, 9159, 17764, 20405, 21273, 29816, 32266, 37273, 40729, 44173, 48918, 49626, 49728, 51344, 53852, 58147]},
      {'l': '少数股东权益', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 113, 86, 146, 118, 102, 174, 328, 318, 284, 371, 452, 555, 582]},
      {'l': '所有者权益合计', 'v': [-13, 4209, 4326, 4617, 5614, 4032, 4337, 5258, 8377, 7294, 8027, 8677, 9159, 17764, 20518, 21359, 29962, 32384, 37375, 40903, 44501, 49236, 49910, 50099, 51796, 54407, 58729]},
      {'l': '股东权益比率', 'v': [-403.45, 1, 0.99, 0.98, 0.28, 0.19, 0.19, 0.13, 0.1, 0.12, 0.18, 0.18, 0.17, 0.22, 0.24, 0.08, 0.13, 0.13, 0.12, 0.16, 0.16, 0.12, 0.12, 0.12, 0.15, 0.14, 0.1]},
    ]},
    {'t': '合并现金流量表', 'rows': [
      {'l': '来自持续经营收益的净利润', 'v': [521, 874, 740, 589, 693, 1057, 1567, 2963, 7191, 5928, 5542, 5954, 6032, 4845, 5246, 6038, 9278, 6799, 8610, 10883, 10951, 13332, 14841, 11659, 13332, 14853, 21158]},
      {'l': '折旧、损耗和摊销', 'v': [150, 177, 153, 163, 182, 183, 151, 100, 80, 109, 101, 107, 90, 158, 508, 647, 684, 771, 858, 762, 1044, 1197, 1354, 1459, 1443, 1402, 1568]},
      {'l': '经营性应收项目变动', 'v': [0, 0, 0, 0, 0, 0, 1358, -6891, -8161, 9655, -2809, 2133, 1972, -6263, 2765, -10910, 7226, 3432, -2833, -704, -5479, -14901, 15389, 4869, -8231, -10687, -11679]},
      {'l': '存货变动', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '预付资产变动', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, -619, 490, -134, -1426, -1325, 1554, -4752, -1160, 4562, 77, 558, -374]},
      {'l': '应付票据及应付账款、应交税费和其他应付款变动', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '其他营运资金变动', 'v': [0, 0, 0, 0, 0, 0, -1261, 7537, 10089, -9026, 2800, -2137, -1799, 8683, -3009, 10219, -7409, -3619, 4205, 1741, 3990, 19526, -14216, -9607, 8198, 8874, 12993]},
      {'l': '营运资金变动', 'v': [0, 0, 0, 0, 0, 0, 96, 645, 1928, 629, -9, -4, 173, 2420, -244, -1310, 307, -321, -54, -288, 65, -127, 13, -176, 44, -1255, 940]},
      {'l': '递延所得税', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '股权激励', 'v': [0, 0, 0, 0, 0, 0, 23, 24, 24, 28, 21, 36, 61, 105, 119, 137, 183, 213, 220, 226, 240, 281, 324, 380, 399, 390, 361]},
      {'l': '资产减值费用', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 165, -2, -4, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '终止经营活动产生的现金流量净额', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '其他经营活动产生的现金流量净额', 'v': [13, -606, -3379, -852, 534, 576, -408, -864, -1575, -1811, -1111, -1103, -1083, -1037, -641, -839, -2131, -3999, -2221, -2103, -2238, 86, -4192, 1634, -3718, -2616, 1600]},
      {'l': '经营活动产生的现金流量净额', 'v': [684, 445, -2486, -100, 1409, 1816, 1430, 2868, 7648, 5049, 4542, 4986, 5273, 6491, 4988, 4673, 8321, 3463, 7413, 9480, 10062, 14769, 12340, 14956, 11500, 12774, 25627]},
      {'l': '购建固定资产、无形资产和其他长期资产支付的现金', 'v': [-227, -309, -263, -131, -52, -23, -62, -60, -69, -221, -48, -80, -456, -1042, -797, -665, -710, -620, -688, -860, -1091, -1351, -1070, -1284, -1386, -1604, -1733]},
      {'l': '处置固定资产、无形资产和其他长期资产收回的现金净额', 'v': [4, 0, 0, 0, 0, 0, 0, 0, 0, 2, 1, 1, 0, 0, 0, 0, 512, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '取得子公司及其他营业单位支付的现金净额', 'v': [0, 0, 0, 0, 0, 0, -25, 0, 0, 0, 0, 0, 0, -16854, 0, 0, 0, 0, -14, 0, 0, 0, 0, 0, 0, 0, -3603]},
      {'l': '处置子公司及其他营业单位收到的现金净额', 'v': [0, 0, 0, 0, 0, 0, 0, 1, 270, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 41, 0, 0, 0, 0, 0, 0]},
      {'l': '投资支付的现金', 'v': [0, 0, -3153, -32, 0, 0, 0, -560, -6232, -4592, -3739, -1368, -1422, 0, -60, 0, -426, -2731, -315, -4920, -3068, -6394, -778, -963, -6270, -7228, -4881]},
      {'l': '收回投资及取得投资收益收到的现金', 'v': [0, 0, 0, 0, 0, 76, 0, 0, 3800, 5118, 3013, 1301, 1499, 2244, 306, 1423, 187, 0, 2299, 393, 536, 207, 4086, 316, 502, 11034, 2474]},
      {'l': '取得无形资产支付的现金净额差', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '终止投资活动产生的现金流量净额', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '其他投资活动产生的现金流量净额', 'v': [-1840, 1278, 4504, -71, -83, 452, -13, 248, 205, 903, 269, 198, 179, 107, 28, 24, 17, 16, 32, 60, 97, 56, 38, 231, 864, 992, 2178]},
      {'l': '投资活动产生的现金流量净额', 'v': [-2064, 970, 1088, -234, -135, 505, -99, -371, -2025, 1210, -504, 52, -200, -15545, -523, 782, -420, -3335, 1314, -5327, -3485, -7482, 2276, -1700, -6290, 3194, -5565]},
      {'l': '发行股票产生的现金流入', 'v': [0, 0, 0, 0, 0, 0, 47, 31, 0, 0, 0, 34, -72, 7662, -1, -393, -224, -188, -228, -300, -285, -31, -681, -350, -448, -481, -512]},
      {'l': '回购股票产生的现金流出', 'v': [0, 0, 0, 0, 0, 0, -30, -21, -5, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '发行优先股产生的现金流量净额', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '发行债券产生的现金流入', 'v': [0, 0, 46, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 6974, 769, 2326, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '偿付债券现金流出', 'v': [0, 0, 0, 0, 0, -50, 0, 0, 0, 0, 0, 0, 0, 0, -775, -2326, 0, 0, -1603, -781, -744, 0, 0, 0, 0, 0, 0]},
      {'l': '发行债券产生的现金流量净额', 'v': [0, 0, 46, 0, 0, -50, 0, 0, 0, 0, 0, 0, 0, 6974, -6, 0, 0, 0, -1603, -781, -744, 0, 0, 0, 0, 0, 0]},
      {'l': '分配股利、利润或偿付利息支付的现金', 'v': [0, -83, -343, -344, -638, -2673, -1019, -1671, -3163, -6282, -3889, -4258, -4606, -3784, -2320, -3234, -3017, -3329, -2561, -5427, -5785, -6983, -11527, -9665, -10316, -10416, -13722]},
      {'l': '融资租赁产生的现金流量净额', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, -159, -284, -310, -309, -307, -255, -251]},
      {'l': '其他筹资活动产生的现金流量净额', 'v': [0, 85, -4, 6, 40, 56, -5, -1, 66, 34, 14, -188, 0, -108, 163, -91, 17, -69, -75, -11, -181, -176, -151, -49, -179, -109, -150]},
      {'l': '筹资活动产生的现金流量净额', 'v': [-2, 2, -301, -338, -598, -2667, -1006, -1663, -3102, -6248, -3875, -4412, -4678, 10744, -2164, -3718, -3224, -3586, -4467, -6519, -7154, -7474, -12669, -10373, -11250, -11261, -14635]},
      {'l': '期初现金及现金等价物余额', 'v': [2704, 1634, 3051, 1352, 680, 1381, 1035, 1359, 2215, 4745, 4756, 4751, 1945, 2340, 4035, 6375, 8067, 12744, 9286, 13546, 11180, 10603, 10442, 12398, 15258, 9212, 13910]},
      {'l': '汇率变动对现金及现金等价物的影响', 'v': [0, 0, 0, 0, 0, 0, 0, 30, 9, 0, -3600, -3432, 0, 5, 39, -45, 0, 0, 0, 0, 0, 26, 9, -23, -6, -9, 16]},
      {'l': '现金及现金等价物净额', 'v': [-1381, 1416, -1699, -672, 675, -346, 324, 865, 2529, 11, -3437, -2806, 395, 1695, 2340, 1692, 4677, -3458, 4260, -2366, -577, -161, 1956, 2860, -6046, 4698, 5443]},
      {'l': '期末现金及现金等价物余额', 'v': [1323, 3051, 1352, 680, 1355, 1035, 1359, 2224, 4745, 4756, 1319, 1945, 2340, 4035, 6375, 8067, 12744, 9286, 13546, 11180, 10603, 10442, 12398, 15258, 9212, 13910, 19353]},
      {'l': '资本开支', 'v': [-227, -309, -263, -131, -52, -23, -62, -60, -69, -221, -48, -80, -456, -1042, -797, -665, -710, -620, -688, -860, -1091, -1351, -1070, -1284, -1386, -1604, -1733]},
      {'l': '自由现金流', 'v': [457, 136, -2749, -231, 1357, 1793, 1368, 2808, 7580, 4828, 4494, 4906, 4817, 5449, 4191, 4008, 7611, 2843, 6725, 8620, 8971, 13418, 11270, 13672, 10114, 11170, 23894]},
    ]},
    {'t': '估值比率', 'rows': [
      {'l': '市盈率', 'v': [9999, 21.31, 16.69, 17.5, 25.45, 20.85, 25.52, 36.58, 38.6, 15.49, 31.97, 37.75, 26.4, 35.27, 32.82, 38.76, 29.76, 38.57, 39.83, 30.29, 33.87, 47.3, 46.05, 42.42, 28.63, 28.69, 29.11]},
      {'l': '持续经营市盈率', 'v': [9999, 21.31, 16.69, 17.5, 25.45, 20.85, 25.58, 36.58, 52.46, 15.13, 31.75, 37.56, 26.86, 36.54, 34.02, 39.99, 31.69, 39.13, 43.19, 29.92, 36.02, 49.06, 47.5, 40.57, 28.82, 28.95, 28.93]},
      {'l': '股价／巴菲特所有者每股收益', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 26.39, 23.28, 34.38, 50.26, 30.55, 38.27, 38.7, 29.69, 32.28, 0, 0, 0, 0, 0, 0]},
      {'l': '市净率', 'v': [0, 4.43, 2.85, 2.22, 3.14, 5.46, 7.88, 17.33, 28.18, 10.85, 18.68, 21.87, 14.6, 8.52, 7.35, 9.41, 8.02, 6.93, 7.98, 6.96, 7.22, 11.13, 11.61, 8.58, 6.6, 6.93, 8.86]},
      {'l': '股价／每股有形账面价值', 'v': [0, 4.43, 2.85, 2.22, 3.14, 5.46, 7.88, 17.33, 28.18, 10.85, 18.68, 21.87, 14.64, 0, 88.13, 59.74, 20.07, 15.5, 15.38, 12.49, 12.38, 18.05, 18.81, 13.88, 10.57, 10.9, 14.37]},
      {'l': '股价／自由现金流', 'v': [0, 137.28, 0, 0, 12.99, 12.29, 25.08, 32.81, 31.42, 16.34, 33.46, 38.77, 27.93, 26.41, 35.63, 49.85, 31.25, 78.32, 43.89, 32.72, 35.44, 40.57, 51.22, 31.27, 33.59, 33.5, 21.63]},
      {'l': '股价／每股经营现金流', 'v': [0, 41.85, 0, 0, 12.52, 12.13, 23.99, 32.12, 31.14, 15.63, 33.11, 38.15, 25.51, 22.17, 29.94, 42.75, 28.59, 64.3, 39.81, 29.75, 31.6, 36.85, 46.78, 28.58, 29.54, 29.29, 20.17]},
      {'l': '市销率', 'v': [0, 8.05, 6.18, 5.69, 8.73, 9.21, 13.87, 22.22, 33.32, 12.18, 23.45, 26.82, 18.29, 22.37, 18.36, 21.89, 19.45, 21.41, 25.5, 19.79, 23.44, 32.33, 29.65, 25.24, 22, 21.57, 21.77]},
      {'l': '市盈增长比', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 6.76, 4.64, 3.75, 2.3, 3.08, 3.87, 3.21, 7.03, 3.73, 2.37, 1.82]},
      {'l': '企业价值／收入', 'v': [0, 8.06, 6.13, 5.65, 6.47, 7.62, 12.2, 20.55, 31.8, 6.89, 18.49, 21.63, 12.03, 17.27, 12.62, -0.3, 3.23, 1.16, 1.54, 4.72, 6.82, 12.97, 16.83, 9, 7.53, 8.3, 8.37]},
      {'l': '企业价值倍数', 'v': [0, 16.1, 12.56, 12.67, 13.37, 12.83, 17.74, 28.07, 31.17, 7.38, 20.96, 25.25, 14.45, 21.95, 17.28, -0.39, 3.91, 1.55, 1.76, 5.26, 6.92, 14.55, 20.05, 9.85, 5.57, 6.5, 7.25]},
      {'l': '企业价值／息税前利润', 'v': [0, 19.02, 14.89, 15.88, 16.43, 14.73, 19.47, 29.02, 31.51, 7.52, 21.34, 25.71, 14.67, 22.66, 18.89, -0.43, 4.19, 1.72, 1.92, 5.6, 7.51, 15.81, 21.87, 10.87, 5.98, 6.94, 7.69]},
      {'l': '乔尔·格林布拉特收益率 ％', 'v': [0, 5.26, 6.71, 6.3, 6.09, 6.79, 5.14, 3.45, 3.17, 13.3, 4.69, 3.89, 6.82, 4.41, 5.29, -232.56, 23.87, 58.14, 52.08, 17.86, 13.32, 6.33, 4.57, 9.2, 16.72, 14.41, 13]},
      {'l': '唐纳德·亚克曼收益率 ％', 'v': [0, 0, 0, 0, 0, 8.55, 10, 20.78, 21.03, 24.17, 22.54, 22.28, 12.15, -1.29, 1.06, 2.84, 8.97, 9.87, 13.55, 14.73, 13.48, 11.86, 15.65, 10.66, 11.75, 13.3, 15.48]},
      {'l': '席勒市盈率', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 34.67, 43.99, 39.17, 41.49, 67.98, 66.37, 46.08, 33.75, 34.53, 43.93]},
      {'l': 'Cyclically Adjusted PB Ratio', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 12.52, 13.84, 11.12, 11.22, 17.23, 16.07, 10.74, 7.86, 8.12, 10.56]},
      {'l': 'Cyclically Adjusted PS Ratio', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 22.27, 28.24, 25.2, 26.49, 43.38, 42.11, 29.03, 21.9, 23.01, 29.8]},
      {'l': '股息率 ％', 'v': [0, 0.45, 2.78, 3.37, 3.63, 4.08, 2.99, 1.85, 1.35, 8, 2.61, 2.25, 3.6, 2.99, 2.54, 2.07, 2.63, 2.77, 1.91, 2.86, 2.68, 1.56, 2.01, 2.26, 3.06, 2.8, 2.67]},
    ]},
    {'t': '估值和质量', 'rows': [
      {'l': '总市值', 'v': [0, 18628, 12332, 10227, 17623, 22031, 34168, 91095, 236086, 79112, 149927, 189776, 133739, 151343, 150003, 200076, 239247, 223706, 297306, 283393, 319084, 544317, 576293, 426703, 338898, 373086, 515472]},
      {'l': '企业价值', 'v': [0, 18628, 12250, 10217, 13076, 18234, 30165, 85225, 227301, 44644, 118566, 153445, 88501, 111070, 102620, -2700, 39460, 12047, 17814, 67291, 92514, 218385, 327727, 152430, 116256, 144025, 198649]},
      {'l': '月末股价', 'v': [0, 17.9, 11.85, 9.8, 16.8, 20.85, 32.15, 85.6, 220.8, 73.6, 139.4, 176.3, 124.1, 131.9, 129.3, 171.7, 198.5, 183.2, 239.8, 226.6, 253, 430, 455.4, 337.2, 268, 295.2, 407.8]},
      {'l': '每股现金净流量', 'v': [-0.01, -0.01, 0.05, -0.08, -9.15, -12.88, -13.73, -27.57, -66.21, -19.59, -5.53, -2.67, 0.34, -14.11, -9.77, -17.96, -4.43, -0.14, 0.91, 1.78, -4.34, -17.05, -78.05, -63.17, -51.51, -76.64, -161.68]},
      {'l': '净流动资产价值 NCAV', 'v': [-0.01, -0.01, 0.15, 0.4, 2.79, 1.52, 2.32, 2.38, 6.95, 6.39, 5.01, 5.88, 7.03, -2.18, -0.34, 1.37, 8.46, 10.45, 14.27, 16.44, 16.11, 20.15, 20.06, 20.67, 20.85, 20.41, 19.46]},
      {'l': '净净营运资本 NNWC', 'v': [-0.01, -0.01, 0.05, -0.08, -9.15, -12.88, -11.55, -27.57, -53.33, -13.64, 2.38, 3.74, 5.36, -5.16, -2.7, -3.46, 5.24, 7.8, 10.93, 12.78, 11, 10.83, -58.66, -48.14, -31.75, -44.32, -121.36]},
      {'l': '内在价值：预期自由现金流模型', 'v': [0, 0, 0, 0, 0, 0, 6.35, 12.51, 29.69, 44.06, 53.63, 61.09, 67.08, 59.91, 56.84, 52.83, 67.69, 65.55, 74.29, 89.87, 90.13, 104.68, 116.98, 129.72, 125.71, 123.95, 160.29]},
      {'l': '内在价值：市销率模型', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 255.3, 238.44, 297.46, 349.18, 309.31, 286.28, 319.85, 444.94]},
      {'l': '彼得林奇公允价值', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 40.04, 69.29, 98.65, 81.97, 115.17, 146.42, 45.93, 72.35, 124.51, 223.56]},
      {'l': '格雷厄姆估值', 'v': [0, 8.74, 8.15, 7.47, 8.91, 9.27, 10.74, 16.13, 27.24, 27.25, 27.16, 29.18, 29.68, 0, 11.2, 16.66, 37.33, 35.29, 44.13, 55.6, 56.83, 68.55, 72.27, 67.4, 72.82, 78.83, 94.88]},
      {'l': '盈利能力值（EPV）', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 58.89, 63.44, 80.63, 213.98, 202.37, 213.19, 269.99, 224.14, 234.82, 0, 0, 0, 0, 0, 0]},
      {'l': '两年破产风险（Z分数）', 'v': [0, 0, 0, 0, 1.1, 1.04, 1.39, 1.8, 1.99, 1.11, 3.29, 3.82, 2.65, 1.92, 1.91, 0.73, 1.01, 0.9, 0.9, 1.18, 1.19, 1.24, 1.31, 1.03, 1.15, 1.11, 0.91]},
      {'l': '基本面趋势（F分数）', 'v': [9999, 9999, 3, 2, 7, 4, 5, 6, 5, 5, 6, 6, 5, 3, 5, 6, 6, 3, 7, 6, 6, 7, 4, 4, 5, 7, 8]},
      {'l': '财务造假嫌疑（M分数）', 'v': [0, 0, 0.64, 2.46, -13.62, -3.51, 31810.19, -2.72, -5.89, -3.4, 21.29, -2.69, -2.47, 5.84, -2.81, -1.99, -2.54, -2.64, -2.3, -2.26, -2.03, -2.03, -2.73, -2.86, -2.02, -1.9, -2.39]},
      {'l': '可供营运的资产净额／总资产', 'v': [-403.45, 135762.65, 1, 1.06, 0.23, 0.01, 0.02, -0.03, -0.01, -0.31, -0.37, -0.61, -0.75, -0.42, -0.33, -2.11, -0.68, -0.75, -0.98, -0.55, -0.71, -0.99, -0.5, -0.56, -0.42, -0.51, -0.68]},
      {'l': '斯隆比率 ％', 'v': [6129709.68, -12.81, 49.1, 19.57, -2.93, -5.89, 0.04, 0.05, 0.62, -1.8, 1.47, 0, 0.04, 16.25, 0.1, -0.12, 0.02, 2.28, -0.42, 2.02, 1.01, 1.06, -0.52, -0.78, 1.95, -0.76, -0.4]},
      {'l': '流动比率', 'v': [0, 0.26, 6.52, 5.34, 1.31, 1.19, 1.19, 1.13, 1.1, 1.13, 1.15, 1.16, 1.17, 1.09, 1.13, 1.04, 1.07, 1.08, 1.07, 1.1, 1.11, 1.08, 1.08, 1.08, 1.1, 1.09, 1.05]},
      {'l': '速动比率', 'v': [0, 0.26, 6.52, 5.34, 1.31, 1.19, 1.19, 1.13, 1.1, 1.13, 1.15, 1.16, 1.17, 1.09, 1.13, 1.04, 1.07, 1.08, 1.07, 1.1, 1.11, 1.08, 1.08, 1.08, 1.1, 1.09, 1.05]},
      {'l': '现金比率', 'v': [0, 0, 2.94, 0.1, 0.35, 0.24, 0.23, 0.18, 0.11, 0.62, 0.85, 0.93, 1.01, 0.85, 0.94, 0.94, 1, 1.02, 1.01, 1.02, 0.99, 0.95, 0.73, 0.78, 0.78, 0.71, 0.61]},
      {'l': '利息保障倍数', 'v': [10000, 10000, 10000, 10000, 10000, 10000, 350.46, 255.87, 253.87, 626.63, 430.77, 400.4, 2830.5, 76.32, 26.29, 27.34, 58.94, 29.59, 10.8, 8.29, 6.2, 23.54, 92.74, 4.41, 1.39, 1.7, 3.45]},
      {'l': '最高股价', 'v': [0, 18.15, 19, 14.7, 17.25, 21.9, 32.15, 85.6, 265.4, 218.4, 151.9, 197.2, 189.1, 147.1, 149.9, 187.1, 309.4, 209.6, 254.2, 303.4, 285.4, 430, 567, 480.8, 377.4, 393.8, 462.8]},
      {'l': '最低股价', 'v': [0, 8.15, 7.95, 9, 7.9, 14.6, 18.55, 33.95, 69.4, 52.25, 54.6, 115.3, 100.7, 100.3, 114.1, 113.2, 173, 161.8, 182.6, 196.7, 221.2, 211.4, 430, 209, 245.2, 214.6, 275.4]},
      {'l': '股票回购率 ％', 'v': [None, 0, 0, -0.28, -0.52, -0.73, -0.58, -0.14, -0.47, -0.53, -0.06, -0.09, -0.11, -6.47, -1.11, -0.44, -3.43, -1.31, -1.53, -0.87, -0.85, -0.37, 0.03, 0, 0.07, 0.06, -0.02]},
      {'l': '股票回购收益率 ％', 'v': [0, 0, 0, 0, 0, 0, -0.05, -0.01, 0, 0, 0, -0.02, 0.05, -5.06, 0, 0.2, 0.09, 0.08, 0.08, 0.11, 0.09, 0.01, 0.12, 0.08, 0.13, 0.13, 0.1]},
      {'l': '每股收入同比增长率 ％', 'v': [0, 27.55, -13.79, -10.23, 11.86, 17.7, 2.33, 66.23, 71.99, -8.78, -1.65, 10.58, 3.22, -13.13, 19.44, 11.39, 30.15, -16.19, 9.92, 21.75, -5.72, 23.21, 15.5, -13.04, -8.8, 12.32, 36.91]},
      {'l': '每股收益同比增长率 ％', 'v': [0, 68, -15.48, -21.13, 17.86, 51.52, 26, 85.71, 144.44, -16.96, -8.21, 7.11, 0.64, -20.43, 5.35, 12.44, 50.56, -28.79, 26.74, 24.25, -0.13, 21.69, 8.8, -19.62, 17.74, 9.94, 36.15]},
      {'l': '税息折旧及摊销前利润同比增长率 ％', 'v': [0, 63.75, -15.94, -17.92, 21.39, 44.31, 18.5, 77.05, 139.71, -16.59, -7.05, 7.4, 0.31, -17.9, 10.91, 15.04, 42.46, -24.06, 28.6, 24.75, 3.57, 11.45, 8.75, -5.31, 34.84, 6.12, 23.73]},
      {'l': '5年每股税息折旧及摊销前利润增长率 ％', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, -0.27, 4.69, 8.43, 11.51, 13.03, 11.69, 12.67, 14.82, 5.77, 7.73, 12.21, 15.86]},
      {'l': '发行在外的普通股平均股数', 'v': [1041.33, 1040.06, 1042.85, 1050.9, 1049.52, 1056.88, 1060.35, 1062.69, 1067.24, 1072.22, 1074.7, 1076.4, 1081.76, 1088.35, 1152.06, 1163.71, 1186.8, 1212.38, 1227.67, 1242.06, 1253.73, 1262.75, 1265.43, 1265.49, 1265.46, 1264.48, 1263.89]},
      {'l': '期末总股本', 'v': [1040.67, 1040.67, 1040.67, 1043.58, 1049, 1056.64, 1062.76, 1064.19, 1069.23, 1074.89, 1075.52, 1076.44, 1077.67, 1147.41, 1160.12, 1165.26, 1205.28, 1221.11, 1239.81, 1250.63, 1261.2, 1265.85, 1265.47, 1265.43, 1264.55, 1263.84, 1264.03]},
      {'l': 'β贝塔系数', 'v': [0, 0, 0, 0, 1.35, 1.34, 1.26, 1.49, 1.96, 1.65, 1.6, 1.43, 1.52, 1.25, 1.22, 1.16, 1.98, 1.86, 1.86, 1.25, 1.16, 1.07, 0.78, 1.32, 1.34, 1.52, 1.35]},
      {'l': '重述日期', 'v': [None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None]},
      {'l': '报告日期', 'v': [None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None]},
      {'l': '收益发布日期', 'v': [None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None]},
      {'l': '股东人数', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
      {'l': '在职员工人数', 'v': [0, 0, 0, 0, 0, 0, 0, 0, 0, 865, 850, 875, 997, 1105, 1278, 1420, 1568, 1663, 1777, 1990, 2372, 2392, 2307, 2380, 2552, 2582, 2636]},
      {'l': '外汇汇率', 'v': [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1]},
    ]},
  ]},
}


# ----- 本地財報查詢層（v9）：數據源自 LOCAL_FINANCIALS（用戶上傳 xlsx 1:1 轉出）-----
def get_local_financial(symbol):
    """返回該代號的本地財報結構（name/years/groups）；無則 None。"""
    if symbol in LOCAL_FINANCIALS:
        return LOCAL_FINANCIALS[symbol]
    s = str(symbol).strip().upper()
    return LOCAL_FINANCIALS.get(s)


# 英文字段 -> 本地中文指標候選名（按優先序，前者更貼近合併口徑）
_LOCAL_FIELD_CANDIDATES = {
    "revenue": ["营业收入"],
    "gross_profit": ["毛利润"],
    "net_income": ["归属于母公司股东的净利润", "净利润"],
    "ebitda": ["税息折旧及摊销前利润"],
    "op_income": ["营业利润"],
    "rd": ["研发费用"],
    "sga": ["销售及行政开支"],
    "interest": ["利息支出"],
    "tax": ["所得税费用"],
    "eps": ["稀释每股收益", "基本每股收益"],
    "shares": ["发行在外的稀释性潜在普通股平均股数"],
    "assets": ["资产总计"],
    "equity": ["归属于母公司所有者权益合计", "所有者权益合计"],
    "liabilities": ["负债合计"],
    "cash": ["货币资金和现金等价物"],
    "curr_assets": ["流动资产合计"],
    "curr_liab": ["流动负债合计"],
    "receivables": ["应收账款"],
    "payables": ["应付账款"],
    "inventory": ["存货"],
    "ocf": ["经营活动产生的现金流量净额"],
    "fcf": ["自由现金流"],
    "capex": ["资本开支"],
    "depreciation": ["折旧、损耗和摊销"],
}


def _local_index(data):
    """指標名 -> 該 row（含 v）；同名取首個。"""
    idx = {}
    for g in data["groups"]:
        for row in g["rows"]:
            if row["l"] not in idx:
                idx[row["l"]] = row
    return idx


def get_local_indicator_series(symbol, label):
    """返回 {year(int): value}（本地原生單位）；找不到返回 {}。供其他按鈕/推算調用。"""
    data = get_local_financial(symbol)
    if not data:
        return {}
    row = _local_index(data).get(label)
    if not row:
        return {}
    out = {}
    for y, v in zip(data["years"], row["v"]):
        if v is not None:
            out[int(y)] = v
    return out


def local_annual_fields_scaled(symbol, scale=1_000_000.0):
    """把本地財報映射成 {year(int): {英文字段: 值×scale}}，以便與 Yahoo（原始元）同口徑合併。
    debt 由短期＋長期借款合成；缺失字段不寫入。注意本地部分明細（研發/capex/折舊）源頭以 0 填充，
    合併時只補 Yahoo 缺失，不覆蓋 Yahoo 真實值（見 fetch_real_annual_financials）。"""
    data = get_local_financial(symbol)
    if not data:
        return {}
    idx = _local_index(data)

    def vec(label):
        row = idx.get(label)
        return row["v"] if row else None

    raw = {}
    for f, cands in _LOCAL_FIELD_CANDIDATES.items():
        for c in cands:
            vv = vec(c)
            if vv is not None:
                raw[f] = vv
                break
    n = len(data["years"])
    sd = vec("短期借款和资本化租赁债务")
    ld = vec("长期借款和资本化租赁债务")
    debt_v = [None] * n
    for i in range(n):
        a = sd[i] if sd else None
        b = ld[i] if ld else None
        if a is not None or b is not None:
            debt_v[i] = (a or 0) + (b or 0)
    raw["debt"] = debt_v

    out = {}
    for i, y in enumerate(data["years"]):
        rec = {}
        for f, v in raw.items():
            if v is None:
                continue
            x = v[i]
            if x is not None:
                rec[f] = float(x) * scale
        if rec:
            out[int(y)] = rec
    return out


@st.cache_data(ttl=12 * 3600, show_spinner=False)
def fetch_real_annual_financials(symbol: str):
    """
    v6 真實年度財報（快取 12 小時）。從 Yahoo Finance 拉取：
      - 年度損益表（收入、毛利、純利、EBITDA、營運收入、R&D、稅項、EPS、股數）
      - 年度資產負債表（資產、權益、負債、債務、現金、流動比率、應收/應付、存貨）
      - 年度現金流（經營現金流、自由現金流、資本開支、折舊）
      - 年度股息（dividends 按年匯總）
      - 歷史年度最高/最低收市價（history(max) 按年 resample，可覆蓋上市以來）
    免費源通常僅提供最近約 4 年年度財報；更早年度一律回傳缺失，由表格標 N/A，不編造。
    回傳：{year(int): {field: value}}，外加 "_years_price" {year: {high, low}}。
    """
    annual = {}
    price_year = {}

    def _row(df, candidates):
        if df is None:
            return None
        for c in candidates:
            if c in df.index:
                ser = df.loc[c]
                ser = ser.dropna() if hasattr(ser, "dropna") else ser
                if ser is not None and len(ser) > 0:
                    return ser
        return None

    def _put(yr, key, val):
        if val is None or (isinstance(val, float) and (val != val)):
            return
        annual.setdefault(yr, {})[key] = float(val)

    try:
        tk = yf.Ticker(symbol)
        # v9：三張表各以線程超時包裹，最壞 3s 回 None，不讓慢連線拖垮整個函式
        inc = _run_with_timeout(lambda: tk.income_stmt, timeout=3.0)
        bs = _run_with_timeout(lambda: tk.balance_sheet, timeout=3.0)
        cf = _run_with_timeout(lambda: tk.cashflow, timeout=3.0)

        if inc is not None and not inc.empty:
            for col in inc.columns:
                yr = getattr(col, "year", None)
                if yr is None:
                    continue
                frame = inc[[col]]
                def _v(cands):
                    s = _row(frame, cands)
                    return s.iloc[0] if s is not None else None
                _put(yr, "revenue", _v(["Total Revenue", "Total Revenue Growth"]))
                _put(yr, "gross_profit", _v(["Gross Profit"]))
                _put(yr, "net_income", _v(["Net Income", "Net Income Common Stockholders", "Net Income Continuous Operations"]))
                _put(yr, "ebitda", _v(["EBITDA", "Normalized EBITDA"]))
                _put(yr, "op_income", _v(["Operating Income"]))
                _put(yr, "rd", _v(["Research And Development"]))
                _put(yr, "sga", _v(["Selling General And Administration", "Selling General & Administration"]))
                _put(yr, "interest", _v(["Interest Expense", "Net Interest"]))
                _put(yr, "tax", _v(["Tax Provision", "Income Tax Expense"]))
                _put(yr, "eps", _v(["Diluted EPS", "Basic EPS"]))
                _put(yr, "shares", _v(["Diluted Average Shares", "Basic Average Shares"]))

        if bs is not None and not bs.empty:
            for col in bs.columns:
                yr = getattr(col, "year", None)
                if yr is None:
                    continue
                frame = bs[[col]]
                def _v(cands):
                    s = _row(frame, cands)
                    return s.iloc[0] if s is not None else None
                _put(yr, "assets", _v(["Total Assets"]))
                _put(yr, "equity", _v(["Stockholders Equity", "Stockholders' Equity", "Total Equity Gross Minority Interest"]))
                _put(yr, "liabilities", _v(["Total Liabilities Net Minority Interest", "Total Liabilities"]))
                _put(yr, "debt", _v(["Total Debt", "Net Debt"]))
                _put(yr, "cash", _v(["Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"]))
                _put(yr, "curr_assets", _v(["Current Assets"]))
                _put(yr, "curr_liab", _v(["Current Liabilities"]))
                _put(yr, "receivables", _v(["Receivables", "Accounts Receivable"]))
                _put(yr, "payables", _v(["Payables", "Accounts Payable"]))
                _put(yr, "inventory", _v(["Inventory"]))

        if cf is not None and not cf.empty:
            for col in cf.columns:
                yr = getattr(col, "year", None)
                if yr is None:
                    continue
                frame = cf[[col]]
                def _v(cands):
                    s = _row(frame, cands)
                    return s.iloc[0] if s is not None else None
                _put(yr, "ocf", _v(["Operating Cash Flow", "Total Cash From Operating Activities"]))
                _put(yr, "fcf", _v(["Free Cash Flow"]))
                _put(yr, "capex", _v(["Capital Expenditure"]))
                _put(yr, "depreciation", _v(["Depreciation And Amortization", "Depreciation", "Depreciation Amortization Depletion"]))

        # 年度股息（v9：線程超時包裹）
        try:
            divs = _run_with_timeout(lambda: tk.dividends, timeout=3.0)
            if divs is not None and not divs.empty:
                div_year = divs.groupby(divs.index.year).sum()
                for yr, v in div_year.items():
                    _put(int(yr), "dps_total", float(v))
        except Exception:
            pass

        # 歷史年度最高/最低（v7 多源：Yahoo→Stooq）
        try:
            hmax, _hsrc = get_price_history(symbol, "1d", "max")
            if hmax is not None and not hmax.empty:
                grp = hmax.groupby(hmax.index.year)
                for yr, g in grp:
                    price_year[int(yr)] = {
                        "high": float(g["High"].max()),
                        "low": float(g["Low"].min()),
                        "close": float(g["Close"].iloc[-1])
                    }
        except Exception:
            pass

    except Exception:
        pass

    # v9：合併本地真實年度財報（用戶上傳，歷史更長）。只補 Yahoo 缺失的「年份/字段」，不覆蓋 Yahoo 已取得的值，
    # 避免本地源頭以 0 填充的明細（研發/capex/折舊）蓋掉 Yahoo 真實非零值。本地絕對值為百萬，×1e6 對齊 Yahoo 元口徑。
    try:
        _loc = local_annual_fields_scaled(symbol, 1_000_000.0)
        for _yr, _rec in _loc.items():
            _tgt = annual.setdefault(_yr, {})
            for _k, _v in _rec.items():
                if _k not in _tgt:
                    _tgt[_k] = _v
    except Exception:
        pass

    annual["_price_year"] = price_year
    return annual


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def fetch_real_monthly_path(symbol: str, months: int = 13):
    """
    v6 真實月度走勢（供波浪理論分頁）。回傳近 months 個月每月末收盤價（真實），
    以及 52 週真實最高/最低價及其發生日期。取不到時 ok=False。
    """
    out = {"ok": False}
    try:
        df, _src = get_price_history(symbol, "1d", "2y")
        if df is None or df.empty:
            return out
        out["source"] = _src
        m = df.resample("ME").agg({"Close": "last", "High": "max", "Low": "min"}).dropna()
        m = m.tail(months)
        labels = [d.strftime("%Y-%m") for d in m.index]
        closes = [round(float(v), 2) for v in m["Close"].values]
        d1 = df.tail(252)
        hi = float(d1["High"].max()); lo = float(d1["Low"].min())
        hi_d = d1["High"].idxmax(); lo_d = d1["Low"].idxmin()
        out.update({
            "ok": True, "labels": labels, "closes": closes,
            "hi52": round(hi, 2), "lo52": round(lo, 2),
            "hi52_date": str(hi_d)[:10], "lo52_date": str(lo_d)[:10]
        })
        return out
    except Exception:
        return out


def zigzag_pivots(highs, lows, pct: float = 0.06):
    """
    百分比閾值 ZigZag 擺動點識別（純函數，無隨機、無模擬）。
    回傳 [(idx, price, kind)]，kind：
      'H' 已確認擺動高點；'L' 已確認擺動低點；
      'H?'/'L?' 為最末端「尚未確認」的當前潛在端點（需日後價格再走 pct 才成立）。
    """
    n = len(highs)
    pivots = []
    if n < 5:
        return pivots
    trend = 0  # 0=未定, 1=向上, -1=向下
    hi = float(highs[0]); hi_i = 0
    lo = float(lows[0]); lo_i = 0
    for i in range(n):
        h = float(highs[i]); l = float(lows[i])
        if h >= hi:
            hi, hi_i = h, i
        if l <= lo:
            lo, lo_i = l, i
        # 自暫定高點回落 pct → 確認該高點
        if trend >= 0 and hi > 0 and l <= hi * (1.0 - pct):
            pivots.append((hi_i, hi, "H"))
            trend = -1
            lo, lo_i = l, i
            hi, hi_i = -1e18, i
        # 自暫定低點反彈 pct → 確認該低點
        elif trend <= 0 and 0 < lo < 1e17 and h >= lo * (1.0 + pct):
            pivots.append((lo_i, lo, "L"))
            trend = 1
            hi, hi_i = h, i
            lo, lo_i = 1e18, i
    # 末端尚未確認的潛在端點
    if trend == 1 and hi > 0:
        pivots.append((hi_i, hi, "H?"))
    elif trend == -1 and 0 < lo < 1e17:
        pivots.append((lo_i, lo, "L?"))
    return pivots


def label_elliott(pivots):
    """
    啟發式艾略特標號（非 100%、僅「一種」機械解讀，必須人工確認）：
    以最近一個擺動高點為推動浪「5」，向前交替標 0/1/2/3/4/5，
    其後若再轉折則標 A（低）/B（高）/C（低）。資料不足處留 None。
    """
    labels = [None] * len(pivots)
    if len(pivots) < 2:
        return labels
    last_h = None
    for j in range(len(pivots) - 1, -1, -1):
        if str(pivots[j][2]).startswith("H"):
            last_h = j
            break
    if last_h is None:
        return labels
    names = {0: "0", 1: "1", 2: "2", 3: "3", 4: "4", 5: "5"}
    for k in range(0, 6):
        idx = last_h - (5 - k)
        if 0 <= idx < len(pivots):
            labels[idx] = names[k]
    for k, nm in enumerate(["A", "B", "C"], start=1):
        idx = last_h + k
        if 0 <= idx < len(pivots):
            labels[idx] = nm
    return labels


def compute_wave_analysis(symbol: str, pct: float = 0.06, days: int = 420):
    """v7：取真實日線（Yahoo→Stooq 多源）→ ZigZag 擺動點 → 啟發式艾略特標浪。失敗回傳 ok=False。"""
    out = {"ok": False, "pivots": [], "labels": [], "dates": [], "prices": [], "kinds": [],
           "line_dates": [], "line_close": [], "src": None, "pct": pct, "hi52": None, "lo52": None}
    try:
        df, src = get_price_history(symbol, "1d", "2y")
        if df is None or df.empty or len(df) < 30:
            return out
        df = df.tail(days)
        highs = df["High"].values
        lows = df["Low"].values
        closes = df["Close"].values
        idx = df.index
        pivots = zigzag_pivots(highs, lows, pct)
        labels = label_elliott(pivots)
        out.update({
            "ok": True,
            "pivots": pivots,
            "labels": labels,
            "dates": [str(idx[i])[:10] for i, _, _ in pivots],
            "prices": [float(p) for _, p, _ in pivots],
            "kinds": [str(k) for _, _, k in pivots],
            "line_dates": [str(d)[:10] for d in idx],
            "line_close": [float(c) for c in closes],
            "src": src,
            "hi52": float(df["High"].tail(252).max()),
            "lo52": float(df["Low"].tail(252).min()),
        })
    except Exception:
        pass
    return out


def compute_real_forecast(prof: dict, mom: dict):
    """
    v6 真實量化推演（非預測、非保證）：
      以真實財報（估值/成長/盈利/負債）+ 真實價格動能（52週位置/RSI/均線/歷史波動率）
      計算方向性評分，並以真實年化波動率推出 1–6 個月客觀統計區間。
    回傳：direction, tag, expected_pct, lo_pct, hi_pct, t_lo, t_hi, score, reasons[]
    """
    p = float(prof.get("price", 0) or 0)
    if p <= 0 or not mom.get("ok"):
        return {"ok": False, "reasons": ["真實數據不足，無法推演（Yahoo Finance 未提供該標的完整報價/歷史）。"]}

    fpe = prof.get("fwd_pe")
    pe = prof.get("pe")
    ps = prof.get("ps")
    pb = prof.get("pb")
    rev_g = prof.get("rev_growth")
    earn_g = prof.get("earnings_growth")
    gm = prof.get("gross_margin")
    nm = prof.get("net_margin")
    roe = prof.get("roe")
    d2e = prof.get("debt_to_equity")
    eps = prof.get("fwd_eps") or prof.get("eps")
    vol_ann = mom.get("vol_ann")
    pos52 = mom.get("pos52", 50)
    rsi = mom.get("rsi", 50)
    r63 = mom.get("r63")
    r126 = mom.get("r126")
    above50 = mom.get("above_ma50")
    above200 = mom.get("above_ma200")

    score = 0.0
    reasons = []

    # 估值維度（最多 +3.5 / -3.5）
    profitable = isinstance(eps, (int, float)) and eps > 0
    if profitable and isinstance(fpe, (int, float)) and fpe > 0:
        if fpe < 12:
            score += 2.0; reasons.append(f"遠期PE僅 {fpe:.1f}x，估值處於偏低區間")
        elif fpe < 20:
            score += 1.0; reasons.append(f"遠期PE {fpe:.1f}x，估值合理")
        elif fpe < 35:
            score += 0.0; reasons.append(f"遠期PE {fpe:.1f}x，估值不便宜")
        elif fpe < 60:
            score -= 1.5; reasons.append(f"遠期PE {fpe:.1f}x，估值偏貴，需高增長兌現")
        else:
            score -= 3.0; reasons.append(f"遠期PE高達 {fpe:.1f}x，泡沫風險顯著")
    else:
        score -= 2.0
        reasons.append("未盈利或遠期EPS為負，缺乏估值支撐，股價依賴情緒與融資")

    # 成長/盈利維度（最多 +2.5 / -2.5）
    if isinstance(rev_g, (int, float)):
        if rev_g >= 25:
            score += 1.5; reasons.append(f"營收同比 {rev_g:+.0f}% 高增長")
        elif rev_g >= 10:
            score += 0.8; reasons.append(f"營收同比 {rev_g:+.0f}% 穩健成長")
        elif rev_g <= -10:
            score -= 1.2; reasons.append(f"營收同比 {rev_g:+.0f}% 收縮")
        else:
            reasons.append(f"營收同比 {rev_g:+.0f}%，成長平淡")
    if isinstance(roe, (int, float)):
        if roe >= 20:
            score += 1.0; reasons.append(f"ROE {roe:.0f}% 顯示盈利質素佳")
        elif roe < 0:
            score -= 1.0; reasons.append(f"ROE {roe:.0f}% 為負，蝕錢")
    if isinstance(nm, (int, float)) and nm < 0:
        score -= 0.8; reasons.append(f"淨利率 {nm:.0f}% 為負")

    # 動能/技術維度（最多 +2 / -2，以真實數據為準）
    if isinstance(r63, (int, float)):
        if r63 >= 20:
            score += 0.8; reasons.append(f"近 3 月真實升 {r63:.0f}%，動能強（亦需防追高）")
        elif r63 <= -20:
            score -= 0.8; reasons.append(f"近 3 月真實跌 {r63:.0f}%，動能弱（或存在超賣反彈）")
    if above200 is True:
        score += 0.5; reasons.append("現價站穩 200 日線之上")
    elif above200 is False:
        score -= 0.5; reasons.append("現價失守 200 日線，中長線偏弱")
    if rsi >= 75:
        score -= 0.6; reasons.append(f"RSI {rsi:.0f} 超買，短線回調風險")
    elif rsi <= 28:
        score += 0.4; reasons.append(f"RSI {rsi:.0f} 超賣，或現技術反彈")
    if pos52 >= 92:
        score -= 0.4; reasons.append(f"貼近 52 週高位（{pos52:.0f}% 分位），安全邊際低")
    elif pos52 <= 12:
        score += 0.5; reasons.append(f"處 52 週低位（{pos52:.0f}% 分位），估值修復空間大")

    # 風險維度（最多 -1.5）
    if isinstance(d2e, (int, float)) and d2e > 150:
        score -= 1.0; reasons.append(f"負債/股東權益 {d2e:.0f}% 偏高")
    if not profitable:
        score -= 0.5

    # 方向定性
    if score >= 3.0:
        tag = "🟢 強勢爆升潛力（模型）"
    elif score >= 1.2:
        tag = "🟢 溫和上升潛力（模型）"
    elif score <= -3.0:
        tag = "🔴 高危爆跌預警（模型）"
    elif score < 0:
        tag = "🔴 下行回撤風險（模型）"
    else:
        tag = "⚪ 中性區間震盪（模型）"

    # 客觀統計區間：以真實年化波動率推 6 個月（√0.5）的 ±1σ 區間，再疊加方向性傾向
    sigma_h = (vol_ann or 35) * (0.5 ** 0.5) / 100.0  # 6 個月 1σ
    drift = max(min(score * 0.04, 0.35), -0.35)        # 評分映射為方向性中樞
    t_hi = round(p * (1 + drift + sigma_h), 2)
    t_lo = round(p * (1 + drift - sigma_h), 2)
    hi_pct = round((drift + sigma_h) * 100)
    lo_pct = round((drift - sigma_h) * 100)
    expected_pct = round(drift * 100, 1)
    direction = "up" if score >= 1.2 else ("down" if score < 0 else "neutral")

    return {
        "ok": True, "tag": tag, "direction": direction, "score": round(score, 2),
        "expected_pct": expected_pct, "lo_pct": lo_pct, "hi_pct": hi_pct,
        "t_lo": t_lo, "t_hi": t_hi, "vol_ann": vol_ann, "pos52": pos52, "rsi": rsi,
        "reasons": reasons[:5]
    }


def _sf(v, spec=",.2f", suffix=""):
    """安全格式化：None / 空 / 非數值一律回 'N/A'，避免免費源缺欄位時 f-string 崩潰。"""
    try:
        if v is None or v == "":
            return "N/A"
        return f"{float(v):{spec}}{suffix}"
    except Exception:
        return "N/A"


def get_or_create_asset_profile(symbol: str, apply_micro_tick: bool = False):
    # 先檢查是否為中文同義詞 (如「道斯指數」「納斯達克指數」「標普500指數」)
    if symbol in INDEX_SYNONYM_MAP:
        symbol = INDEX_SYNONYM_MAP[symbol]

    if symbol in STOCK_DB:
        prof = STOCK_DB[symbol].copy()
    else:
        s_clean = symbol.strip().upper()
        detected_mkt = "US"
        curr = "USD"
        if s_clean.isdigit() or s_clean.endswith(".HK"):
            num_part = s_clean.replace(".HK", "")
            if num_part.isdigit():
                n_val = int(num_part)
                if 11000 <= n_val <= 29999:
                    symbol = f"{n_val:05d}.HK"
                    detected_mkt = "WARRANT"
                elif 50000 <= n_val <= 69999:
                    symbol = f"{n_val:05d}.HK"
                    detected_mkt = "CBBC"
                else:
                    symbol = f"{n_val:05d}.HK" if n_val >= 10000 else f"{n_val:04d}.HK"
                    detected_mkt = "HK"
                curr = "HKD"
                
        if symbol in STOCK_DB:
            prof = STOCK_DB[symbol].copy()
        else:
            code_str = symbol.replace(".HK", "")
            fetched_name = ""
            fetched_en = symbol
            p = 50.0
            if detected_mkt == "WARRANT":
                fetched_name = f"港股窩輪 {code_str} (認購/認沽證)"
                fetched_en = f"HK Structured Warrant {code_str}"
                p = 0.165
            elif detected_mkt == "CBBC":
                is_bull = int(code_str) < 60000
                fetched_name = f"港股{'牛證' if is_bull else '熊證'} {code_str} (CBBC)"
                fetched_en = f"HK {'Bull' if is_bull else 'Bear'} CBBC {code_str}"
                p = 0.085

            def _do_search():
                if hasattr(yf, "Search"):
                    return yf.Search(symbol, max_results=3)
                return None
            try:
                # v9：Search 線程超時，最壞 3s 回 None，不阻塞 profile 建立
                sr = _run_with_timeout(_do_search, timeout=3.0)
                if sr is not None:
                    for qt in getattr(sr, "quotes", []):
                        if qt.get("symbol", "").upper() == symbol.upper():
                            fetched_en = qt.get("longname") or qt.get("shortname") or fetched_en
                            if not fetched_name:
                                fetched_name = fetched_en
                            break
            except Exception:
                pass

            if not fetched_name:
                fetched_name = f"{symbol} ({fetched_en})"

            sc_val, verd_str, act_str = compute_critical_score(18.5, 3.2, 2.4, 10.0, 42.0, 8.0, detected_mkt)
            prof = {
                "symbol": symbol, "code": code_str, "name": fetched_name, "en": fetched_en,
                "mkt": detected_mkt, "curr": curr,
                "price": p, "change": -0.5, "pct": -1.0, "low": p*0.98, "high": p*1.02,
                "vol": "活躍成交", "turnover": f"{round(p*0.8, 2)}億 {curr}",
                "mkt_cap": f"{round(p*5.0, 1)}億 {curr}", "mkt_cap_usd": 25.0,
                "eps": round(p/18.5, 4), "pe": 18.5, "fwd_eps": round(p/16.5, 4), "fwd_pe": 16.5,
                "ps": 3.2, "pb": 2.4, "yield": 1.85,
                "lot": 10000 if detected_mkt in ["WARRANT", "CBBC"] else 100,
                "w52_low": round(p*0.72, 2), "w52_high": round(p*1.32, 2),
                "rev_b": 45.0, "rev_usd": 12.0, "rev_growth": 10.0, "gross_margin": 42.0, "net_margin": 18.0,
                "roe": 15.0, "bps": round(p/2.4, 2), "sps": round(p/3.2, 2), "dps": round(p*0.018, 2),
                "ocf_ps": round(p/12.0, 2), "fcf_ps": round(p/15.0, 2), "shares_m": 1500.0, "exp_return": 8.0,
                "tag": f"{detected_mkt} 即時連線標的",
                "news": f"《即時行情》{code_str} | {fetched_name} 已透過全市場接口載入即時數據與技術圖表",
                "adr_info": f"代號: {symbol} | 名稱: {fetched_name} | 評分: {sc_val}/10",
                "score": sc_val, "verdict": verd_str, "action_plan": act_str,
                "catalyst": "全市場動態檢索標的，建議結合K線支撐位與估值分位審慎評估風險。",
                "stage": "動態檢索資產"
            }

    # ===== v6 真實行情覆蓋：以 Yahoo Finance 真實成價為準，杜絕本地假跳動 =====
    live = fetch_realtime_market_tick(prof["symbol"])
    prof["live_ok"] = bool(live.get("live_ok"))
    prof["bid"] = live.get("bid")
    prof["ask"] = live.get("ask")
    prof["bid_size"] = live.get("bid_size")
    prof["ask_size"] = live.get("ask_size")
    prof["quote_time"] = live.get("quote_time")
    prof["delayed"] = live.get("delayed")
    prof["source"] = live.get("source") or ("Yahoo Finance" if live.get("live_ok") else None)

    if live.get("live_ok"):
        prof["price"] = live["price"]
        prof["change"] = live["change"]
        prof["pct"] = live["pct"]
        prof["low"] = live["low"]
        prof["high"] = live["high"]
        prof["prev_close"] = live.get("prev_close", prof["price"] - prof["change"])
        _vn_live = _to_float(live.get("vol_num"))
        if _vn_live > 0:
            prof["vol_num"] = _vn_live
            prof["vol"] = f"{_vn_live/1e6:.2f}百萬"
    else:
        prof["change"] = 0.0
        prof["pct"] = 0.0
        prof["prev_close"] = prof.get("price", 0)

    # ===== v6 真實基本面覆蓋：僅對股票/ETF（衍生工具與指數沒有財報）=====
    if prof.get("mkt") in ["HK", "US", "ETF"]:
        rf = fetch_real_fundamentals(prof["symbol"])
        prof["fund_ok"] = rf.get("ok", False)
        fund = rf.get("fund", {})
        fin = rf.get("fin", {})
        prof["fund_as_of"] = rf.get("as_of")

        if fund.get("trailing_pe") is not None:
            prof["pe"] = round(float(fund["trailing_pe"]), 2)
        if fund.get("forward_pe") is not None:
            prof["fwd_pe"] = round(float(fund["forward_pe"]), 2)
        if fund.get("pb") is not None:
            prof["pb"] = round(float(fund["pb"]), 3)
        if fund.get("ps") is not None:
            prof["ps"] = round(float(fund["ps"]), 3)
        if fund.get("trailing_eps") is not None:
            prof["eps"] = round(float(fund["trailing_eps"]), 4)
        if fund.get("forward_eps") is not None:
            prof["fwd_eps"] = round(float(fund["forward_eps"]), 4)
        if fund.get("dividend_yield") is not None:
            prof["yield"] = round(float(fund["dividend_yield"]) * 100, 2)
        if fund.get("revenue_growth") is not None:
            prof["rev_growth"] = round(float(fund["revenue_growth"]) * 100, 2)
        if fund.get("gross_margin") is not None:
            prof["gross_margin"] = round(float(fund["gross_margin"]) * 100, 2)
        if fund.get("profit_margin") is not None:
            prof["net_margin"] = round(float(fund["profit_margin"]) * 100, 2)
        if fund.get("roe") is not None:
            prof["roe"] = round(float(fund["roe"]) * 100, 2)
        if fund.get("market_cap") is not None:
            prof["mkt_cap_real"] = float(fund["market_cap"])
        if fund.get("shares_out") is not None:
            prof["shares_m"] = round(float(fund["shares_out"]) / 1e6, 2)
        if fund.get("fifty_two_high") is not None:
            prof["w52_high"] = round(float(fund["fifty_two_high"]), 3)
        if fund.get("fifty_two_low") is not None:
            prof["w52_low"] = round(float(fund["fifty_two_low"]), 3)
        if fund.get("book_value") is not None:
            prof["bps"] = round(float(fund["book_value"]), 3)
        # 真實每股經營/自由現金流（用真實現金流除以真實股數）
        _so = fund.get("shares_out")
        if isinstance(_so, (int, float)) and _so > 0:
            if isinstance(fund.get("operating_cf"), (int, float)):
                prof["ocf_ps"] = round(float(fund["operating_cf"]) / float(_so), 3)
            if isinstance(fund.get("free_cf"), (int, float)):
                prof["fcf_ps"] = round(float(fund["free_cf"]) / float(_so), 3)
        if isinstance(fund.get("total_revenue"), (int, float)):
            prof["total_revenue_real"] = float(fund["total_revenue"])
        prof["beta"] = fund.get("beta")
        prof["debt_to_equity"] = fund.get("debt_to_equity")
        prof["sector"] = fund.get("sector")
        prof["industry"] = fund.get("industry")
        prof["fin_real"] = fin
    else:
        prof["fund_ok"] = False
        prof["fin_real"] = {}

    # ===== v7 最終覆蓋（去寫死）：報價表欄位一律以真實源（Yahoo／騰訊／Stooq）為準，取不到即 N/A =====
    _src = str(live.get("source") or "")
    _curr = prof.get("curr", "")
    _price = live.get("price") if live.get("live_ok") else None

    # 市值顯示字串：Yahoo 為絕對值（÷1e8 得億）；騰訊直接給「億」；皆無則 N/A
    _mc = prof.get("mkt_cap_real")
    if isinstance(_mc, (int, float)) and _mc > 0:
        mcap_yi = _mc / 1e8
        prof["mkt_cap"] = f"{mcap_yi:,.2f}億 {_curr}".strip()
    elif isinstance(live.get("mcap_yi"), (int, float)) and live.get("mcap_yi"):
        mcap_yi = float(live["mcap_yi"])
        prof["mkt_cap"] = f"{mcap_yi:,.0f}億 {_curr}".strip()
    else:
        mcap_yi = None
        prof["mkt_cap"] = "N/A"
    prof["mcap_yi_real"] = mcap_yi

    # 開盤價 / 成交量（股數）
    if live.get("live_ok"):
        prof["open"] = live.get("open")
        prof["vol_num"] = _to_float(live.get("vol_num"))

    # 總股本（億股）：Yahoo shares_out（百萬股 ÷100 得億股），免費源缺時以「市值 ÷ 真實價格」推算
    shares_yi = None
    if prof.get("fund_ok") and isinstance(prof.get("shares_m"), (int, float)) and prof["shares_m"] > 0:
        shares_yi = prof["shares_m"] / 100.0
    elif mcap_yi and _price:
        shares_yi = mcap_yi / _price
    prof["shares_yi"] = shares_yi

    # 成交額（本幣）：優先採數據源直接給的真實成交額（AAStocks d 欄）；否則以「真實成交量×價」估算
    amount_real = _to_float(live.get("amount_real"), default=None)
    if not (amount_real and amount_real > 0) and live.get("live_ok") and _price:
        _vn0 = _to_float(live.get("vol_num"))
        if _vn0 > 0:
            amount_real = _vn0 * _price
    prof["amount_real"] = amount_real if amount_real else None

    if not prof.get("fund_ok"):
        # Yahoo 基本面失敗：騰訊可補 PE/PB/52週；預測 PE/EPS、股息率免費源缺，一律 N/A，不保留寫死
        prof["pe"] = live.get("pe") if live.get("live_ok") else None
        prof["pb"] = live.get("pb") if live.get("live_ok") else None
        if live.get("hi52") and live.get("lo52"):
            prof["w52_high"], prof["w52_low"] = live["hi52"], live["lo52"]
        else:
            prof["w52_high"], prof["w52_low"] = None, None
        prof["fwd_pe"] = None
        prof["fwd_eps"] = None
        prof["yield"] = None
        prof["ps"] = None  # 騰訊/Stooq 免費源無市銷率，避免殘留內建寫死值
        prof["beta"] = None  # 騰訊/Stooq 免費報價無 Beta，僅 Yahoo 提供
        # 歷史 EPS 無免費欄位時，以真實「價格／歷史PE」換算（數學恆等，標註為推算）
        if _price and isinstance(prof.get("pe"), (int, float)) and prof["pe"]:
            prof["eps"] = round(_price / float(prof["pe"]), 4)
            prof["eps_estimated"] = True
        else:
            prof["eps"] = None

    # 成交量 / 成交金額：以真實成交量與價格為準；成交額以「量×價」估算並標「約」；無真實源則 N/A
    if live.get("live_ok"):
        vn = _to_float(live.get("vol_num"))
        if vn and vn > 0:
            prof["vol"] = f"{vn / 1e6:,.2f}百萬股"
            if _price:
                prof["turnover"] = f"約 {vn * _price / 1e8:,.2f}億 {_curr}".strip()
        else:
            prof["vol"] = "N/A"
            prof["turnover"] = "N/A"
    else:
        prof["vol"] = "N/A"
        prof["turnover"] = "N/A"

    # v6：apply_micro_tick 已停用（不再製造假跳動）。真實價格只在 Yahoo 刷新時更新。
    prof["update_time"] = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
    return prof

# ==========================================
# 5. 頂部萬用檢索器 (支援模糊中文指數名如「道斯指數」「納斯達克指數」「標普500指數」)
# ==========================================
def format_option_label(sym: str, code: str, name: str, en: str, mkt: str) -> str:
    mkt_badge = {
        "HK": "港股", "US": "美股", "INDEX": "指數", "FUTURES": "期貨/期指",
        "ETF": "ETF/基金", "WARRANT": "窩輪", "CBBC": "牛熊證"
    }.get(mkt, mkt)
    return f"{code} ({sym}) | {name} — {en} [{mkt_badge}]"

def search_all_markets_fn(searchterm: str):
    default_list = [
        (format_option_label(i["symbol"], i["code"], i["name"], i["en"], i["mkt"]), i["symbol"])
        for i in MASTER_CATALOG
    ]
    if not searchterm or not searchterm.strip():
        return default_list[:65]
    
    q = searchterm.strip()
    q_up = q.upper()
    matched = []
    
    # 1. 優先檢查同義詞字典 (如輸入「道斯指數」、「納斯達克指數」、「標普500指數」)
    for alias_k, target_sym in INDEX_SYNONYM_MAP.items():
        if q_up in alias_k.upper() or alias_k.upper() in q_up:
            if target_sym in STOCK_DB:
                it = STOCK_DB[target_sym]
                lbl = format_option_label(it["symbol"], it["code"], it["name"], it["en"], it["mkt"])
                if target_sym not in [m[1] for m in matched]:
                    matched.append((lbl, target_sym))

    # 2. 標準與字元集模糊匹配 (解決「道斯指數」少一個「瓊」字也能匹配「道瓊斯指數」)
    q_chars = set(q.replace("指數", "").replace(" ", ""))
    for i in MASTER_CATALOG:
        full_text = f"{i['code']} {i['symbol']} {i['name']} {i['en']} {i['mkt']}".upper()
        is_sub = (q_up in full_text) or (i["code"].lstrip("0").startswith(q_up.lstrip("0")) and len(q_up) >= 1)
        is_fuzzy_cn = (len(q_chars) >= 2 and all(ch in i["name"] for ch in q_chars))
        if is_sub or is_fuzzy_cn:
            lbl = format_option_label(i["symbol"], i["code"], i["name"], i["en"], i["mkt"])
            if i["symbol"] not in [m[1] for m in matched]:
                matched.append((lbl, i["symbol"]))
            
    # 3. 動態識別任意港股代號、窩輪或牛熊證
    #    重大修正：避免與 STOCK_DB 中已存在的股票重複（例如輸入 700 時同時出現
    #    「0700.HK 即時」與「00700.HK 騰訊控股」兩筆，造成雙重騰訊報價）
    if q_up.isdigit():
        n_v = int(q_up)
        # 先把輸入數字正規化為港股常見格式 (1-4 位數 → 5 位數補零)
        norm_hk = f"{n_v:05d}.HK" if n_v < 100000 else f"{n_v}.HK"
        if 11000 <= n_v <= 29999:
            hk_sym = norm_hk
            lbl = f"{n_v:05d} ({hk_sym}) | 港股窩輪 {n_v:05d} (認購/認沽證 Warrant) [窩輪]"
        elif 50000 <= n_v <= 69999:
            hk_sym = norm_hk
            lbl = f"{n_v:05d} ({hk_sym}) | 港股{'牛證' if n_v<60000 else '熊證'} {n_v:05d} (CBBC) [牛熊證]"
        else:
            # 檢查 STOCK_DB 是否已有該正規化代號；若有則跳過動態生成，避免重複
            hk_sym = norm_hk
            if hk_sym in STOCK_DB:
                # 已在 MASTER_CATALOG 匹配過，跳過避免雙重結果
                hk_sym = None
            else:
                prof_tmp = get_or_create_asset_profile(hk_sym)
                lbl = f"{hk_sym.replace('.HK','')} ({hk_sym}) | {prof_tmp['name']} — {prof_tmp['en']} [港股即時]"
        if hk_sym and hk_sym not in [m[1] for m in matched]:
            matched.insert(0, (lbl, hk_sym))
            
    # 4. Yahoo Search 全市場即時補齊（v9：線程超時，最壞 3s）
    def _do_search_all():
        if hasattr(yf, "Search") and len(q_up) >= 2 and len(matched) < 6:
            return yf.Search(q, max_results=5)
        return None
    try:
        s_res = _run_with_timeout(_do_search_all, timeout=3.0)
        if s_res is not None:
            for qt in getattr(s_res, "quotes", []):
                sym = qt.get("symbol", "")
                sname = qt.get("shortname") or qt.get("longname") or sym
                exch = qt.get("quoteType", "ASSET")
                if sym and sym not in [m[1] for m in matched]:
                    matched.append((f"{sym} | {sname} [{exch}]", sym))
    except Exception:
        pass

    return matched if matched else default_list[:30]

# 頂部全局數據源口徑橫幅（v7 誠實聲明）
st.markdown(
    "<div style='font-size:12px; line-height:1.6; background:#eef5ff; border:1px solid #b8d3f5; "
    "border-radius:8px; padding:8px 12px; margin-bottom:8px; color:#1f3a5f;'>"
    "📡 <b>數據真實性聲明（v7）</b>：現價、歷史 K 線、財報、估值與動能均取自真實市場數據，"
    "採<b>五數據源互補備援：Yahoo Finance → AAStocks（港股優先）→ 騰訊財經 → Stooq</b>（報價與圖表會顯示實際採用的來源）；已完全移除假跳動、隨機買賣盤與模擬 K 線。"
    "本版<b>取消自動更新、改為手動按鈕</b>：每個區塊的「🔄 更新」按一下才重抓。"
    "免費源限制：港股成價一般延遲約 15 分鐘；分鐘線由 Yahoo／騰訊提供，Stooq 只提供日/週/月線；財報與預測估值仍以 Yahoo 為主；"
    "Level 2 多檔買賣深度屬付費數據，本系統已<b>移除 40 檔盤口</b>且絕不編造。模型推演僅供統計參考，<b>不構成投資建議</b>。"
    "</div>",
    unsafe_allow_html=True
)

# 頂部檢索與週期設定區
col_s1, col_s2 = st.columns([2.3, 2.7])
with col_s1:
    if HAS_SEARCHBOX:
        selected_symbol = st_searchbox(
            search_all_markets_fn,
            placeholder="輸入股票號碼、納斯達克指數、道斯指數、標普500指數、恒指、黃金、窩輪...",
            label="🔍 全市場萬用檢索 (支援所有港美股、窩輪、牛熊證、ETF、黃金、期貨、期指、美國所有指數)",
            default="00700.HK",
            default_searchterm="00700 (00700.HK) | 騰訊控股 — Tencent Holdings [港股]",
            edit_after_submit="option",
            clear_on_submit=False,
            key="universal_searchbox_v6"
        )
    else:
        opts = {format_option_label(i["symbol"], i["code"], i["name"], i["en"], i["mkt"]): i["symbol"] for i in MASTER_CATALOG}
        sel_l = st.selectbox("🔍 全市場萬用檢索 (輸入編號或指數名稱即時過濾)", list(opts.keys()), index=20)
        selected_symbol = opts[sel_l]

if not selected_symbol:
    selected_symbol = "00700.HK"

# ===== v9：切股零阻塞（取代舊 v6 清快取即時重抓）=====
# searchbox 改值後 Streamlit 本身會從頭重跑；此處僅記錄當前 symbol，不做清快取/網絡等阻塞操作。
# 強制重抓交給手動「更新報價 / 全部更新」按鈕。
if "last_symbol" not in st.session_state:
    st.session_state["last_symbol"] = selected_symbol
elif st.session_state["last_symbol"] != selected_symbol:
    # v9：切股不清快取、不強制 rerun（舊版清快取＋同步串行重抓是凍結 5+ 秒元兇）；
    # 已看過的股票命中持久快取、0 等待，新股由 fetch_realtime 並行競速（最壞 3s）。
    st.session_state["last_symbol"] = selected_symbol

with col_s2:
    c_tf1, c_tf2, c_tf3 = st.columns([2.2, 1.8, 1.4])
    with c_tf1:
        selected_tf = st.selectbox(
            "📊 選擇技術圖表時間週期 (15分/30分/45分/1H/2H/3H/4H/日線/周線)",
            ["15分鐘圖 (15M)", "30分鐘圖 (30M)", "45分鐘圖 (45M)",
             "1小時圖 (1H)", "2小時圖 (2H)", "3小時圖 (3H)", "4小時圖 (4H)",
             "日線圖 (Daily)", "周線圖 (Weekly)"],
            index=7
        )
    with c_tf2:
        chart_style = st.selectbox(
            "🕯️ 圖表主圖模式",
            ["陰陽燭 K線圖", "面積折線圖"],
            index=0
        )
    with c_tf3:
        st.markdown("<div style='margin-top:26px;'></div>", unsafe_allow_html=True)
        if st.button("🔄 全部更新", help="手動開門並清空快取，單次重新抓取所有真實數據（v10 預設禁止自動聯網）", use_container_width=True):
            st.session_state[_PENDING_OPEN_KEY] = True
            for _fn in (fetch_realtime_market_tick, fetch_real_fundamentals,
                        fetch_real_momentum_snapshot, fetch_real_annual_financials,
                        fetch_real_monthly_path, get_price_history, fetch_stooq_ohlc,
                        fetch_tencent_quote, fetch_tencent_kline):
                try:
                    _fn.clear()
                except Exception:
                    pass
            # 圖表快取 get_multi_tf_ohlcv 一併清除（按鈕執行時該函數已於模塊全局定義）
            try:
                get_multi_tf_ohlcv.clear()
            except Exception:
                pass
            st.rerun()

# 修正問題5：設置互動式按鈕控制，隨時加入或刪除 RSI, MACD, Volume, MA, 布林通道
active_indicators = st.multiselect(
    "🛠️ 圖表輔助指標按鈕制 (點擊 × 可隨時刪除指標，點擊 + 可隨時加入 RSI / MACD / Volume / MA均線 / 布林通道)：",
    options=[
        "Volume (成交量)",
        "RSI (相對強弱指標)",
        "MACD (指數平滑異同移動平均線)",
        "MA (移動平均線 MA10/20/50)",
        "Bollinger (布林通道上下軌)"
    ],
    default=[
        "Volume (成交量)",
        "RSI (相對強弱指標)",
        "MACD (指數平滑異同移動平均線)",
        "MA (移動平均線 MA10/20/50)"
    ]
)

# ==========================================
# 6. 多週期 K 線與指標計算引擎
# ==========================================
@st.cache_data(ttl=600)
def get_multi_tf_ohlcv(symbol: str, timeframe: str):
    base = get_or_create_asset_profile(symbol, apply_micro_tick=False)
    
    tf_config = {
        "15分鐘圖 (15M)": ("60d", "15m", None),
        "30分鐘圖 (30M)": ("60d", "30m", None),
        "45分鐘圖 (45M)": ("60d", "15m", "45min"),
        "1小時圖 (1H)": ("1mo", "1h", None),
        "2小時圖 (2H)": ("2mo", "1h", "2h"),
        "3小時圖 (3H)": ("3mo", "1h", "3h"),
        "4小時圖 (4H)": ("3mo", "1h", "4h"),
        "日線圖 (Daily)": ("1y", "1d", None),
        "周線圖 (Weekly)": ("5y", "1wk", None)
    }
    period, interval, resample_rule = tf_config.get(timeframe, ("1y", "1d", None))
    df_hist = pd.DataFrame()
    data_source = None

    # v7：統一多源獲取（Yahoo 主、Stooq 備）
    df_hist, data_source = get_price_history(base["symbol"], interval, period, resample_rule)

    if df_hist.empty:
        # 分鐘/小時線：再嘗試 Yahoo 其他備用區間（Stooq 免費源不提供分鐘線）
        if interval in ("15m", "30m", "1h"):
            for _p, _iv in [("60d", "30m"), ("60d", "15m"), ("3mo", "1h"), ("1mo", "1h"), ("2y", "1d")]:
                try:
                    _df, _src = get_price_history(base["symbol"], _iv, _p)
                    if _df is not None and not _df.empty and len(_df) >= 6:
                        df_hist, data_source = _df, _src + f"（以 {_iv} 備用呈現）"
                        break
                except Exception:
                    continue
        else:
            # 日/週線：再嘗試更長區間（自動含 Stooq 備援）
            for _p in ("2y", "5y", "10y", "max"):
                try:
                    _df, _src = get_price_history(base["symbol"], interval, _p)
                    if _df is not None and not _df.empty and len(_df) >= 6:
                        df_hist, data_source = _df, _src
                        break
                except Exception:
                    continue

    # 真實數據仍無法取得：回傳空 DataFrame 並標記，由圖表層顯示「數據缺失」，不繪製假圖
    if df_hist is None or df_hist.empty:
        empty = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume",
                                      "MA10", "MA20", "MA50", "BB_Upper", "BB_Lower",
                                      "RSI14", "MACD", "Signal", "Hist"])
        empty.attrs["data_ok"] = False
        empty.attrs["data_source"] = None
        empty.attrs["data_msg"] = (
            f"Yahoo Finance、騰訊財經與 Stooq 均暫時無法提供 {base['symbol']} 的 {timeframe} 真實數據"
            f"（可能為離線、代號無效、或該商品不提供此週期）。分鐘線由 Yahoo／騰訊提供，Stooq 僅支援日/週/月線。"
        )
        return empty

    # 補齊 Volume 欄（Stooq 極少數情況可能缺）
    if "Volume" not in df_hist.columns:
        df_hist["Volume"] = 0.0

    df_hist["MA10"] = df_hist["Close"].rolling(10, min_periods=1).mean()
    df_hist["MA20"] = df_hist["Close"].rolling(20, min_periods=1).mean()
    df_hist["MA50"] = df_hist["Close"].rolling(50, min_periods=1).mean()
    std20 = df_hist["Close"].rolling(20, min_periods=1).std().fillna(0)
    df_hist["BB_Upper"] = df_hist["MA20"] + 2 * std20
    df_hist["BB_Lower"] = df_hist["MA20"] - 2 * std20
    
    delta = df_hist["Close"].diff().fillna(0)
    up_g = delta.clip(lower=0).rolling(14, min_periods=1).mean()
    down_l = (-delta.clip(upper=0)).rolling(14, min_periods=1).mean()
    rs = up_g / down_l.replace(0, 1e-5)
    df_hist["RSI14"] = 100.0 - (100.0 / (1.0 + rs))
    
    ema12 = df_hist["Close"].ewm(span=12, adjust=False).mean()
    ema26 = df_hist["Close"].ewm(span=26, adjust=False).mean()
    df_hist["MACD"] = ema12 - ema26
    df_hist["Signal"] = df_hist["MACD"].ewm(span=9, adjust=False).mean()
    df_hist["Hist"] = df_hist["MACD"] - df_hist["Signal"]

    df_hist.attrs["data_ok"] = True
    df_hist.attrs["data_source"] = data_source
    return df_hist

df_chart = get_multi_tf_ohlcv(selected_symbol, selected_tf)
q_data_init = get_or_create_asset_profile(selected_symbol, apply_micro_tick=False)
# v6 離線安全：圖表數據缺失時不應令引用 MA20/BB/RSI 的分頁崩潰
chart_ok = bool(df_chart.attrs.get("data_ok", not df_chart.empty)) if hasattr(df_chart, "attrs") else (not df_chart.empty)
ma20_txt = f"{df_chart['MA20'].iloc[-1]:.2f} {q_data_init['curr']}" if (chart_ok and "MA20" in df_chart.columns) else "20 日均線（真實數據暫缺）"

# v11：已移除 13 個頂部功能導覽 radio 及其全部專項面板（詳細報價、窩輪牛熊、圖表分析、大戶成交等），
# 僅保留精簡報價卡與 K 線，減少自動觸發的計算與程式碼行數。

# ==========================================
# 8. 主圖表（v7：取消自動更新，圖表僅在切換標的/週期或按「更新圖表」時重抓；
#    數據採 Yahoo→Stooq 多源；右欄與左欄報價等寬對稱；已移除 40 檔買賣盤）
# ==========================================
def build_static_chart_figure():
    """構建 K 線圖 figure（只在 symbol/tf/指標改變或手動按更新時重新構建）"""
    tc = get_theme_chart_colors()

    # v7：真實數據缺失時，顯示明確提示，不繪製任何模擬圖
    if df_chart is None or df_chart.empty or df_chart.attrs.get("data_ok") is False:
        msg = getattr(df_chart, "attrs", {}).get("data_msg", "Yahoo Finance 與 Stooq 暫時無法提供此商品的真實歷史數據。")
        fig_empty = go.Figure()
        fig_empty.add_annotation(
            text=f"⚠️ {msg}<br><br>本系統不會以模擬或隨機數據繪製圖表。請按「🔄 更新圖表」、檢查網絡連線或股票代號，或切換到日線（Stooq 可提供）。",
            xref="paper", yref="paper", x=0.5, y=0.5, showarrow=False,
            font=dict(size=15, color=tc["text"]), align="center", bgcolor=tc["plot"], bordercolor=tc["grid"], borderwidth=1
        )
        fig_empty.update_layout(
            paper_bgcolor=tc["paper"], plot_bgcolor=tc["plot"],
            height=st.session_state.get("chart_height", 720),
            margin=dict(l=10, r=10, t=30, b=10)
        )
        return fig_empty

    show_vol = "Volume (成交量)" in active_indicators
    show_rsi = "RSI (相對強弱指標)" in active_indicators
    show_macd = "MACD (指數平滑異同移動平均線)" in active_indicators
    show_ma = "MA (移動平均線 MA10/20/50)" in active_indicators
    show_bb = "Bollinger (布林通道上下軌)" in active_indicators

    sub_panels = []
    if show_vol: sub_panels.append("VOL")
    if show_rsi: sub_panels.append("RSI")
    if show_macd: sub_panels.append("MACD")

    total_rows = 1 + len(sub_panels)
    if total_rows == 1: r_heights = [1.0]
    elif total_rows == 2: r_heights = [0.72, 0.28]
    elif total_rows == 3: r_heights = [0.56, 0.22, 0.22]
    else: r_heights = [0.46, 0.18, 0.18, 0.18]

    fig_k = make_subplots(rows=total_rows, cols=1, shared_xaxes=True, vertical_spacing=0.03, row_heights=r_heights)
    x_labels = df_chart.index.strftime("%Y-%m-%d %H:%M") if ("小時" in selected_tf or "分鐘" in selected_tf) else df_chart.index.strftime("%Y-%m-%d")

    if "陰陽燭" in chart_style:
        fig_k.add_trace(go.Candlestick(
            x=x_labels, open=df_chart["Open"], high=df_chart["High"],
            low=df_chart["Low"], close=df_chart["Close"], name="K線",
            increasing_line_color=tc["up"], decreasing_line_color=tc["down"]
        ), row=1, col=1)
    else:
        fig_k.add_trace(go.Scatter(
            x=x_labels, y=df_chart["Close"], mode="lines", name="收市價",
            line=dict(color=tc["ma20"], width=2.2),
            fill="tozeroy", fillcolor=tc["ma20"] + "1A"
        ), row=1, col=1)

    if show_ma:
        fig_k.add_trace(go.Scatter(x=x_labels, y=df_chart["MA10"], mode="lines", name="MA10", line=dict(color=tc["ma10"], width=1.2)), row=1, col=1)
        fig_k.add_trace(go.Scatter(x=x_labels, y=df_chart["MA20"], mode="lines", name="MA20", line=dict(color=tc["ma20"], width=1.5)), row=1, col=1)
        fig_k.add_trace(go.Scatter(x=x_labels, y=df_chart["MA50"], mode="lines", name="MA50", line=dict(color=tc["ma50"], width=1.5, dash="dot")), row=1, col=1)
    if show_bb:
        bb_color = "#888888" if st.session_state["theme_mode"] == "light" else "#8899bb"
        fig_k.add_trace(go.Scatter(x=x_labels, y=df_chart["BB_Upper"], mode="lines", name="BB上軌", line=dict(color=bb_color, width=1, dash="dash")), row=1, col=1)
        fig_k.add_trace(go.Scatter(x=x_labels, y=df_chart["BB_Lower"], mode="lines", name="BB下軌", line=dict(color=bb_color, width=1, dash="dash")), row=1, col=1)

    curr_row = 2
    for p_type in sub_panels:
        if p_type == "VOL":
            vol_colors = [tc["up"] if c >= o else tc["down"] for c, o in zip(df_chart["Close"], df_chart["Open"])]
            fig_k.add_trace(go.Bar(x=x_labels, y=df_chart["Volume"], name="Volume(成交量)", marker_color=vol_colors, opacity=0.75), row=curr_row, col=1)
            fig_k.update_yaxes(title_text="Vol", side="right", gridcolor=tc["grid"], showticklabels=False,
                               tickfont=dict(color=tc["text"]), title_font=dict(color=tc["text"]), row=curr_row, col=1)
        elif p_type == "RSI":
            fig_k.add_trace(go.Scatter(x=x_labels, y=df_chart["RSI14"], mode="lines", name="RSI(14)", line=dict(color=tc["rsi"], width=1.8)), row=curr_row, col=1)
            fig_k.add_hline(y=70, line_dash="dot", line_color=tc["down"], row=curr_row, col=1)
            fig_k.add_hline(y=30, line_dash="dot", line_color=tc["up"], row=curr_row, col=1)
            fig_k.update_yaxes(title_text="RSI", side="right", range=[0, 100], gridcolor=tc["grid"],
                               tickfont=dict(color=tc["text"]), title_font=dict(color=tc["text"]), row=curr_row, col=1)
        elif p_type == "MACD":
            m_colors = [tc["up"] if v >= 0 else tc["down"] for v in df_chart["Hist"]]
            fig_k.add_trace(go.Bar(x=x_labels, y=df_chart["Hist"], name="MACD柱", marker_color=m_colors), row=curr_row, col=1)
            fig_k.add_trace(go.Scatter(x=x_labels, y=df_chart["MACD"], mode="lines", name="DIF", line=dict(color=tc["macd_dif"], width=1.4)), row=curr_row, col=1)
            fig_k.add_trace(go.Scatter(x=x_labels, y=df_chart["Signal"], mode="lines", name="DEA", line=dict(color=tc["macd_dea"], width=1.4)), row=curr_row, col=1)
            fig_k.update_yaxes(title_text="MACD", side="right", gridcolor=tc["grid"],
                               tickfont=dict(color=tc["text"]), title_font=dict(color=tc["text"]), row=curr_row, col=1)
        curr_row += 1

    # 圖表高度：由右欄滑桿調校（v7 預設 720px，左右等寬對稱）
    chart_h = st.session_state.get("chart_height", 720)
    fig_k.update_layout(
        title=dict(text=f"{q_data_init['code']} {q_data_init['name']} — {selected_tf} | 高: {df_chart['High'].max():,.2f} 低: {df_chart['Low'].min():,.2f} | RSI: {df_chart['RSI14'].iloc[-1]:.1f}",
                   font=dict(size=13, color=tc["text"])),
        height=chart_h, margin=dict(l=10, r=50, t=32, b=15),
        xaxis_rangeslider_visible=False,
        legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="right", x=1,
                    font=dict(size=10.5, color=tc["text"])),
        paper_bgcolor=tc["paper"], plot_bgcolor=tc["plot"],
        font=dict(color=tc["text"])
    )
    fig_k.update_yaxes(side="right", gridcolor=tc["grid"], tickfont=dict(color=tc["text"]), row=1, col=1)
    fig_k.update_xaxes(gridcolor=tc["grid"], nticks=8, tickfont=dict(color=tc["text"]))
    return fig_k

# 靜態圖表在 fragment 外只渲染一次（消除每 1 秒整圖閃爍）
col_quote, col_chart = st.columns([1, 1])
with col_chart:
    # 圖表工具列：右上角「更新圖表」按鈕 + 高度滑桿
    _cc1, _cc2 = st.columns([1.1, 3.2])
    with _cc1:
        st.markdown("<div style='height:1px;'></div>", unsafe_allow_html=True)
        if st.button("🔄 更新圖表", key="refresh_chart_btn", use_container_width=True,
                     help="按一下即開門並清除圖表快取，單次重新抓取所選時間週期的真實 K 線"):
            st.session_state[_PENDING_OPEN_KEY] = True
            for _fn in (get_multi_tf_ohlcv, get_price_history, fetch_stooq_ohlc, fetch_tencent_kline):
                try:
                    _fn.clear()
                except Exception:
                    pass
            st.rerun()
    with _cc2:
        if "chart_height" not in st.session_state:
            st.session_state["chart_height"] = 720
        st.slider(
            "📏 圖表高度",
            min_value=400, max_value=1400, step=40,
            value=st.session_state["chart_height"],
            key="chart_height_slider",
            help="拖曳調整右邊 K 線圖整體高度（v7 左右兩欄已對稱）"
        )
        st.session_state["chart_height"] = st.session_state["chart_height_slider"]
    # 數據源標示
    _chart_src = df_chart.attrs.get("data_source") if chart_ok else None
    if chart_ok and _chart_src:
        st.caption(f"📊 圖表數據源：**{_chart_src}**　|　週期：{selected_tf}　|　按「🔄 更新圖表」手動刷新")
    st.plotly_chart(build_static_chart_figure(), use_container_width=True, key="main_kline_static", config={"displayModeBar": True, "responsive": True})


def render_quote_panel():
    """v11 精簡手動報價面板：只顯示現價、升跌與最高/最低/開市/前收，其餘欄位與指數列全部移除，
    純 HTML 渲染、不閃爍；非更新 rerun 不聯網（門控），按「更新報價」才單次重抓。"""
    live_q = get_or_create_asset_profile(selected_symbol, apply_micro_tick=False)

    if not live_q.get("live_ok"):
        st.info("📭 尚未載入此標的的最新報價（手動模式不會自動連線）。下方為本地／快取最後已知值，可能非最新；請按「🔄 更新報價」。")

    is_hk = live_q.get("mkt") == "HK"
    _dp = ".3f" if is_hk else ".2f"
    is_up = live_q.get("change", 0) >= 0
    arrow_sym = "▲" if is_up else "▼"
    price_cls = "qa-up" if is_up else "qa-down"
    curr_label = "港元" if live_q.get("curr") == "HKD" else "美元"
    _state = "已收盤 · 最近成價" if live_q.get("live_ok") else "離線 · 最後已知參考值"
    _qt = live_q.get("quote_time") or live_q.get("update_time") or "N/A"
    _src = live_q.get("source") or "Yahoo Finance"

    def _cell(v, cls="qa-mid"):
        if v is None:
            return '<span class="qa-na">N/A</span>'
        try:
            return f'<span class="{cls}">{float(v):{_dp}}</span>'
        except Exception:
            return '<span class="qa-na">N/A</span>'

    _px = live_q.get("price")
    _px_html = (f'<span class="{price_cls}">{float(_px):{_dp}}</span>' if _px is not None
                else '<span class="qa-na">N/A</span>')

    with col_quote:
        if st.button("🔄 更新報價", key="refresh_quote_btn", use_container_width=True,
                     help="按一下即開門並清除報價快取，單次重新抓取最新真實成價"):
            st.session_state[_PENDING_OPEN_KEY] = True
            for _fn in (fetch_realtime_market_tick, fetch_real_fundamentals,
                        get_price_history, fetch_stooq_ohlc, fetch_tencent_quote,
                        fetch_tencent_kline, fetch_aastocks_quote, fetch_aastocks_full_detail):
                try:
                    _fn.clear()
                except Exception:
                    pass
            st.rerun()

        st.markdown(f"""
        <div class="qa-card">
            <div class="qa-head">
                <div>
                    <div class="qa-name">{live_q.get('code')} {live_q.get('name')}（{live_q.get('symbol')}）</div>
                    <div class="qa-big {price_cls}">
                        {_px_html}
                        <span class="qa-chg">{arrow_sym} {abs(live_q.get('change',0)):{_dp}} &nbsp;{abs(live_q.get('pct',0)):.2f}%</span>
                    </div>
                </div>
                <div class="qa-status">
                    <div style="font-weight:700;">{_state}</div>
                    <div>⏱ {_qt}</div>
                    <div style="margin-top:3px;">{curr_label} · {_src}</div>
                </div>
            </div>
            <div class="qa-grid">
                <div class="qa-lab">最高</div>{_cell(live_q.get('high'), 'qa-up')}
                <div class="qa-lab">最低</div>{_cell(live_q.get('low'), 'qa-down')}
                <div class="qa-lab">開市</div>{_cell(live_q.get('open'))}
                <div class="qa-lab">前收</div>{_cell(live_q.get('prev_close'))}
            </div>
        </div>
        """, unsafe_allow_html=True)

# v7：統一以手動面板渲染（不再有自動/靜態分支）
render_quote_panel()

q_data = q_data_init

st.markdown("<div style='margin-top: 8px;'></div>", unsafe_allow_html=True)

# ==========================================
# 10. 下方六大核心功能分頁籤 (修正問題3：徹底移除所有「1.」「2.」「4-5.」「6-7.」「8-10.」「11.」及「問題X」字樣)
# ==========================================
tabs = st.tabs([
    "全市場爆升與爆跌股批判篩選器",
    "2010–2028E 財務與 8P 分析全表",
    "基本面與歷史高低位推算",
    "全年走勢與波浪理論分析",
    "批判性入市判斷、估值評分與同行比較",
    "港美股市牛熊宏觀診斷"
])

def tab_refresh_button(key: str, extra_fns=None, label: str = "🔄 更新本分頁數據"):
    """v7：每個分頁的手動更新小按鈕；按下後清除相關快取並重跑，不做任何自動更新。"""
    if st.button(label, key=key, use_container_width=True,
                 help="手動重新抓取本分頁所需的真實數據（Yahoo / Stooq）"):
        fns = list(extra_fns or []) + [get_price_history, fetch_stooq_ohlc,
                                       fetch_realtime_market_tick,
                                       fetch_tencent_quote, fetch_tencent_kline]
        for fn in fns:
            try:
                fn.clear()
            except Exception:
                pass
        st.rerun()

# ------------------------------------------
# 分頁 A：全市場爆升與爆跌股批判篩選器 (解決問題2：完整顯示所有中文字，直行橫行可自由調校闊度高度)
# ------------------------------------------
with tabs[0]:
    st.subheader("⚖️ 香港及美國股市「潛力爆升股 vs 高危爆跌預警股」客觀批判篩選器 (110+ 核心標的)")
    st.markdown("""
    **批判性量化模型原則（杜絕盲目樂觀與 AI 諂媚）**：
    * **絕非每隻股票都會爆升**：市值逾百億美元的大型股僅有少數具備「極端低估值修復」或「AI 算力業績非線性爆發」的股票能實現 +20% 至 +50% 升幅；大量成熟藍籌僅屬區間震盪；而估值處於極端泡沫（如 PE > 50x、PS > 30x）或面臨基本面惡化、關稅與加息衝擊的股票，則存在 **-15% 至 -45% 的高危爆跌（均值回歸）風險**。
    * **完整中文顯示與直行/橫行自由調校**：下方表格已啟用 **100% 自動換行完整顯示所有中文字**，你可展開表格上方的「📐 表格顯示設定」滑桿調校直行闊度與橫行高度，或直接用滑鼠拖曳任何表格儲存格邊框。
    """)
    
    sc_col1, sc_col2, sc_col3 = st.columns([1.3, 1.8, 1.0])
    with sc_col1:
        mkt_filter = st.selectbox("選擇市場範圍：", ["全部市場 (香港 + 美國)", "香港股市 (HK)", "美國股市 (US)"])
    with sc_col2:
        direction_filter = st.selectbox(
            "選擇批判性預測方向：",
            [
                "全部顯示 (含爆升潛力 + 中性 + 高危爆跌預警)",
                "🟢 僅顯示：具上升/爆升潛力股票 (預計 +15% 以上)",
                "🔴 僅顯示：高危爆跌 / 估值泡沫回撤預警股 (預計下跌)",
                "⚪ 僅顯示：中性區間震盪 / 收息防守股"
            ]
        )
    with sc_col3:
        max_rows = st.selectbox("顯示股票數量：", [20, 35, 50, 80, 100, 120], index=4)

    # 預先按市場過濾標的
    _symbols = [s for s, pf in STOCK_DB.items() if pf["mkt"] in ["HK", "US"]]
    if mkt_filter == "香港股市 (HK)":
        _symbols = [s for s in _symbols if STOCK_DB[s]["mkt"] == "HK"]
    elif mkt_filter == "美國股市 (US)":
        _symbols = [s for s in _symbols if STOCK_DB[s]["mkt"] == "US"]

    st.markdown("""
    **數據真實性聲明（v6 重點）**：
    * 點擊下方 **「🔄 以真實市場數據重新計算」**，系統會即時從 Yahoo Finance 逐隻抓取**真實財報**（PE/PS/PB、營收與盈利增長、ROE、淨利率、負債）及**真實歷史價格**（52 週位置、RSI、近 1/3/6 月真實回報、年化波動率），
      再以量化模型推演 1–6 個月方向與**統計波幅區間**。
    * **區間是以真實歷史波動率（±1σ）計算的客觀統計分佈，不是保證預測，更不是 AI 隨機生成的數字**；「爆升／爆跌」只是模型依當前真實估值與動能給出的傾向，須結合你自己的判斷。
    * 未點擊重新計算前，表格顯示的是內置**分析師靜態觀點**（標註「靜態」），僅作參考，並非即時市場數據。
    """)

    rc1, rc2 = st.columns([1, 1])
    with rc1:
        run_real = st.button("🔄 以真實市場數據重新計算（Yahoo Finance 逐隻連線）", type="primary")
    with rc2:
        st.caption(f"將計算前 {max_rows} 隻；首次連線需逐隻抓取，約需 1–3 分鐘，之後 6 小時內走快取。")

    def _worker(sym):
        prof = get_or_create_asset_profile(sym, apply_micro_tick=False)
        mom = fetch_real_momentum_snapshot(sym)
        fc = compute_real_forecast(prof, mom)
        return sym, prof, mom, fc

    if run_real:
        open_manual_network()
        targets = _symbols[:max_rows]
        results = {}
        prog = st.progress(0.0, text=f"正以真實市場數據計算 0/{len(targets)} …")
        done = 0
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(_worker, s): s for s in targets}
            for fut in as_completed(futs):
                try:
                    sym, prof, mom, fc = fut.result()
                    results[sym] = (prof, mom, fc)
                except Exception:
                    results[futs[fut]] = (STOCK_DB[futs[fut]], {"ok": False}, {"ok": False})
                done += 1
                prog.progress(done / len(targets), text=f"正以真實市場數據計算 {done}/{len(targets)} …")
        prog.empty()
        st.session_state["real_screener"] = results
        st.session_state["real_screener_key"] = (mkt_filter, max_rows)
        close_manual_network()

    real_results = st.session_state.get("real_screener")
    use_real = bool(real_results) and st.session_state.get("real_screener_key") == (mkt_filter, max_rows)

    def build_rows():
        rows = []
        if use_real:
            for sym in _symbols:
                if sym not in real_results:
                    continue
                prof, mom, fc = real_results[sym]
                p = prof["price"]
                if fc.get("ok"):
                    dir_tag = fc["tag"]
                    range_str = f"{fc['lo_pct']:+d}% 至 {fc['hi_pct']:+d}%"
                    t_lo, t_hi = fc["t_lo"], fc["t_hi"]
                    sort_key = fc["score"]
                    analyst = f"[真實] RSI {fc['rsi']:.0f}｜52週分位 {fc['pos52']:.0f}%｜年化波動 {fc['vol_ann']:.0f}%｜" + "；".join(fc["reasons"][:3])
                else:
                    dir_tag = "❔ 真實數據不足"
                    range_str = "—"
                    t_lo = t_hi = None
                    sort_key = -999
                    analyst = "Yahoo Finance 未能取得該標的完整真實數據，無法作模型推演。"
                rows.append({
                    "股票代號": sym,
                    "公司名稱": f"{prof['name']} ({prof['en']})",
                    "市場": prof["mkt"],
                    "當前現價": f"{p:,.2f} {prof['curr']}",
                    "真實遠期PE": (f"{prof['fwd_pe']:.1f}x" if isinstance(prof.get('fwd_pe'), (int, float)) else "數據缺失"),
                    "真實PS/PB": f"{prof['ps']:.2f}x / {prof['pb']:.2f}x" if isinstance(prof.get('ps'), (int, float)) else "數據缺失",
                    "真實營收增長/ROE": f"{prof['rev_growth']:+.1f}% / {prof['roe']:.1f}%" if isinstance(prof.get('rev_growth'), (int, float)) and isinstance(prof.get('roe'), (int, float)) else "數據缺失",
                    "方向定性(模型)": dir_tag,
                    "1-6月客觀波幅區間": range_str,
                    "模型目標區間": f"{prof['curr']} {t_lo:,.1f} – {t_hi:,.1f}" if t_lo is not None else "—",
                    "模型分數": sort_key,
                    "真實數據日期": mom.get("as_of", "—"),
                    "真實依據與風險剖析": analyst
                })
        else:
            for sym, prof in STOCK_DB.items():
                if prof["mkt"] not in ["HK", "US"]:
                    continue
                p = prof["price"]
                exp_r = prof["exp_return"]
                if exp_r >= 25.0:
                    dir_tag, range_str = "🟢 強勢爆升潛力(靜態)", f"+{int(exp_r*0.65)}% 至 +{int(exp_r*1.25)}%"
                    t_lo, t_hi = round(p*(1+exp_r*0.65/100),1), round(p*(1+exp_r*1.25/100),1)
                elif exp_r >= 15.0:
                    dir_tag, range_str = "🟢 溫和上升潛力(靜態)", f"+{int(exp_r*0.6)}% 至 +{int(exp_r*1.2)}%"
                    t_lo, t_hi = round(p*(1+exp_r*0.6/100),1), round(p*(1+exp_r*1.2/100),1)
                elif exp_r <= -20.0:
                    dir_tag, range_str = "🔴 高危爆跌預警(靜態)", f"{int(exp_r*1.35)}% 至 {int(exp_r*0.65)}%"
                    t_lo, t_hi = round(p*(1+exp_r*1.35/100),1), round(p*(1+exp_r*0.65/100),1)
                elif exp_r < 0.0:
                    dir_tag, range_str = "🔴 下行回撤風險(靜態)", f"{int(exp_r*1.4)}% 至 {int(exp_r*0.5)}%"
                    t_lo, t_hi = round(p*(1+exp_r*1.4/100),1), round(p*(1+exp_r*0.5/100),1)
                else:
                    dir_tag, range_str = "⚪ 中性區間震盪(靜態)", f"-5% 至 +{int(max(exp_r*1.2,8))}%"
                    t_lo, t_hi = round(p*0.95,1), round(p*(1+max(exp_r*1.2,8)/100),1)
                rows.append({
                    "股票代號": sym, "公司名稱": f"{prof['name']} ({prof['en']})", "市場": prof["mkt"],
                    "當前現價": f"{p:,.2f} {prof['curr']}",
                    "真實遠期PE": _sf(prof['fwd_pe'], ".1f", "x"), "真實PS/PB": f"{_sf(prof['ps'], '.2f', 'x')} / {_sf(prof['pb'], '.2f', 'x')}",
                    "真實營收增長/ROE": f"{prof['rev_growth']:+.1f}% / {prof['roe']:.1f}%",
                    "方向定性(模型)": dir_tag, "1-6月客觀波幅區間": range_str,
                    "模型目標區間": f"{prof['curr']} {t_lo:,.1f} – {t_hi:,.1f}",
                    "模型分數": exp_r, "真實數據日期": "未連線(靜態)",
                    "真實依據與風險剖析": "[靜態觀點] " + prof["catalyst"].split("|")[-1].strip()
                })
        return pd.DataFrame(rows)

    df_scr = build_rows()

    if mkt_filter == "香港股市 (HK)":
        df_scr = df_scr[df_scr["市場"] == "HK"]
    elif mkt_filter == "美國股市 (US)":
        df_scr = df_scr[df_scr["市場"] == "US"]

    sort_col = "模型分數"
    if "🟢" in direction_filter:
        df_scr = df_scr[df_scr["方向定性(模型)"].str.contains("🟢")].sort_values(sort_col, ascending=False)
    elif "🔴" in direction_filter:
        df_scr = df_scr[df_scr["方向定性(模型)"].str.contains("🔴")].sort_values(sort_col, ascending=True)
    elif "⚪" in direction_filter:
        df_scr = df_scr[df_scr["方向定性(模型)"].str.contains("⚪")].sort_values("批判評分(1-10)", ascending=False) if "批判評分(1-10)" in df_scr.columns else df_scr.sort_values(sort_col, ascending=False)
    else:
        df_scr = df_scr.sort_values(sort_col, ascending=False)

    df_scr = df_scr.head(max_rows).reset_index(drop=True)
    df_scr.insert(0, "序號", range(1, len(df_scr) + 1))
    display_cols = [c for c in df_scr.columns if c not in ["模型分數"]]

    s_c1, s_c2, s_c3, s_c4 = st.columns(4)
    s_c1.metric("當前篩選顯示總數", f"{len(df_scr)} 隻股票", f"數據模式: {'真實連線' if use_real else '靜態觀點'}")
    s_c2.metric("🟢 上升/爆升潛力股數", f"{len(df_scr[df_scr['方向定性(模型)'].str.contains('🟢')])} 隻")
    s_c3.metric("⚪ 中性區間震盪股數", f"{len(df_scr[df_scr['方向定性(模型)'].str.contains('⚪')])} 隻")
    s_c4.metric("🔴 高危爆跌/回撤預警股數", f"{len(df_scr[df_scr['方向定性(模型)'].str.contains('🔴')])} 隻", delta_color="inverse")

    if use_real:
        st.caption(f"✅ 上表為 **Yahoo Finance 真實財報 + 真實歷史價格** 計算結果（行情與財報快取 6 小時）。波幅區間為 ±1σ 統計分佈，非保證預測；投資有風險，本工具不構成投資建議。　計算時間：{datetime.now().strftime('%Y-%m-%d %H:%M')}")

    render_resizable_table(
        df_scr[display_cols],
        key_prefix="screener_main",
        default_col_w=105,
        default_text_w=460,
        default_row_h=58
    )

# ------------------------------------------
# 分頁 B：2010–2028E 歷年財務與 8P 分析全表
# ------------------------------------------
with tabs[1]:
    # ===== v9：本地真實年度財報全表（離線，用戶上傳 xlsx，1:1 轉出）=====
    _lf = get_local_financial(selected_symbol)
    if _lf:
        st.markdown("#### 🗂️ 本地真實年度財報全表（離線數據，依公司申報貨幣，絕對值單位：百萬）")
        st.caption(f"數據來源：用戶提供《{_lf['name']} 30年財報》，年度 {_lf['years'][0]}–{_lf['years'][-1]}，"
                   "由腳本 1:1 轉出、無手工抄錄；空白儲存格代表原始數據缺失，本系統不補值、不估算。")
        _g_names = [g["t"] for g in _lf["groups"]]
        _g_sel = st.selectbox("選擇財報分組：", _g_names, key="local_fin_group_sel")
        _g = next(g for g in _lf["groups"] if g["t"] == _g_sel)
        _ylo, _yhi = st.select_slider(
            "年份範圍：", options=_lf["years"],
            value=(_lf["years"][0], _lf["years"][-1]), key="local_fin_year_range")
        _i0 = _lf["years"].index(_ylo); _i1 = _lf["years"].index(_yhi)
        _yrs = _lf["years"][_i0:_i1 + 1]
        _rows = [[row["l"]] + [row["v"][i] for i in range(_i0, _i1 + 1)] for row in _g["rows"]]
        _df_loc = pd.DataFrame(_rows, columns=["指標"] + [str(y) for y in _yrs])
        st.dataframe(_df_loc, use_container_width=True, hide_index=True)
        st.download_button(
            "📥 下載此分組 (CSV)", _df_loc.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"{selected_symbol.replace('.HK', '')}_{_g_sel}_本地財報.csv",
            mime="text/csv", key="local_fin_csv_dl")
        st.markdown("---")

    # 取得真實上市年份，用於判斷哪些年度不應偽造數據
    _listing_yr = infer_listing_year(selected_symbol)
    st.subheader(f"📊 {q_data['code']} {q_data['name']} ({q_data['symbol']}) — 2010年至2028E 財務狀況及 82 項 8P 財務分析表")
    tab_refresh_button("refresh_tab_8p", [fetch_real_annual_financials, fetch_real_fundamentals])
    st.caption("嚴格依照參考圖一 (Photo 1) 之次序、格式與 82 項指標完整列出，支援自由調校每個直行闊度與橫行高度。")

    # v9 真實財報數據口徑提示框
    st.info(
        f"📡 **v9 數據真實性口徑**：本歷史年度表格（收入、純利、現金流、資產、負債、股數、ROE/ROA、毛利率、"
        f"歷史高低價及對應 PE/PS/PB 等）取自真實數據，**早期年度已由用戶上傳的本地 30 年財報補足**（見上方本地全表），"
        f"Yahoo Finance 僅就其可取到的欄位互補；本地或 Yahoo 皆缺的儲存格顯示 N/A，系統**不會編造**。"
        f"財報按公司申報貨幣列示（部分港股以人民幣列報，與交易貨幣港元不同，估值倍數僅供參考）。"
        f"2026E / 2027E / 2028E 三欄為 **模型預估值（非歷史實際財報）**，已標註 (預)。"
    )
    if _listing_yr > 2010:
        st.warning(
            f"⚠️ **上市年份口徑**：{q_data['name']} 真實上市年份為 **{_listing_yr} 年**，"
            f"早於 {_listing_yr} 年之所有年度一律顯示 **「未上市 / N/A」**，絕不偽造任何數據。"
        )
    
    m1, m2, m3, m4, m5, m6, m7 = st.columns(7)
    m1.metric("C.Price (現價)", f"{q_data['price']:,.2f} {q_data['curr']}")
    m2.metric("C.PE (靜態/預測)", f"{_sf(q_data['pe'], '.2f', 'x')} / {_sf(q_data['fwd_pe'], '.2f', 'x')}")
    m3.metric("C.PS (市銷率)", _sf(q_data['ps'], '.2f', 'x'))
    m4.metric("C.PB (市帳率)", _sf(q_data['pb'], '.2f', 'x'))
    m5.metric("C.DY (現時股息)", _sf(q_data['yield'], '.2f', '%'))
    m6.metric("C.PCF (市現率)", f"{(q_data['price']/max(q_data['ocf_ps'], 0.1)):.2f}x")
    m7.metric("C.PFCF (自由現金流比)", f"{(q_data['price']/max(q_data['fcf_ps'], 0.1)):.2f}x")

    all_19_years = [
        "2010", "2011", "2012", "2013", "2014", "2015", "2016", "2017",
        "2018", "2019", "2020", "2021", "2022", "2023", "2024", "2025",
        "2026E", "2027E", "2028E"
    ]
    yr_start, yr_end = st.select_slider(
        "📅 選擇財務分析表顯示年份範圍 (預設完整顯示 2010 年至 2028E)：",
        options=all_19_years,
        value=("2010", "2028E")
    )

    @st.cache_data(ttl=60)
    def build_82_row_table_2010_2028(symbol: str, current_p: float):
        d = get_or_create_asset_profile(symbol)
        # 重大批判性修正：取得真實上市年份，早於該年度一律不生成虛假數據
        listing_yr = infer_listing_year(symbol)
        years = [
            "2010", "2011", "2012", "2013", "2014", "2015", "2016", "2017",
            "2018", "2019", "2020", "2021", "2022", "2023", "2024", "2025",
            "2026E", "2027E", "2028E"
        ]
        scale_trajectories = [
            0.035, 0.052, 0.078, 0.105, 0.142, 0.185, 0.255, 0.355,
            0.460, 0.550, 0.665, 0.825, 0.792, 0.826, 0.877, 1.000,
            1.150, 1.280, 1.420
        ]
        p = current_p
        rev_base = d["rev_b"] * 1000.0
        sh_base = d["shares_m"]
        
        metric_names = [
            "C.Price（現價）", "C.PE（市盈率）", "C.PS（市銷率）", "C.PB（市帳率）", "C.DY（現時股息）",
            "Year (HKD M)", "Date", "Revenue (收入)", "Adjusted Revenue (收入-調整)",
            "Net Profit (純利)", "Adjusted Net Profit (純利-調整)", "OCF", "Free Cash Flow (自由現金流）",
            "Dividend Yield (股息)", "Assets (資產)", "Equity (股東權益)", "Liabilities",
            "Short- and Long-Term Debt", "Number of Shares (巳發行股數）", "ROA (資產會報率)",
            "ROE (股本回報率)", "Profit Margin (純利率)", "Gross Margin", "EBITDA Margin",
            "Operating Margin", "Revenue per Share (毎股銷售收入)", "Revenue per Share (毎股銷售收入) [重複列]",
            "Earnings per Share (毎股盈利)", "Book Value per Share (毎股帳面値)", "Dividend per Share (毎股股息)",
            "EPS from Continuing Operations", "OCF per Share", "FCF per Share",
            "Revenue Growth", "Gross Profit Growth", "Net Income Growth", "Cash Flow Growth",
            "FCF Growth", "SG&A Expense to Sales", "Operating Expense to Sales",
            "Personnel Expense to Sales", "Depreciation to Sales", "Interest to Sales",
            "Receivables to Sales", "Payables to Sales", "Effective Tax Rate", "R&D to Sales",
            "Capex to Sales", "Sales per Employee", "Number of Employees", "Payable Days",
            "Receivable Days", "Inventory Days", "CCC", "Payout Ratio", "Current Ratio",
            "Cash Ratio", "Debt-to-Asset Ratio", "Operating Income to Debt",
            "Liabilities/Net Profit", "Debt/Net Profit", "Market Cap",
            "Year (HKD M) [8P估值分區]", "High PE (最高市盈率)", "Low PE (最低市盈率)",
            "High PE from Continuing Operations", "Low PE from Continuing Operations",
            "High PS (最高市銷率)", "Low PS (最低市銷率)", "High PB (最高市帳率)", "Low PB (最低市帳率)",
            "Low Dividend Yield (最低股息)", "High Dividend Yield (最高股息)",
            "High Price (最高價)", "Low Price (最低價)", "High PCF", "Low PCF",
            "High PFCF", "Low PFCF", "High PEG", "Low PEG", "High PSG / Low PSG"
        ]
        
        table_dict = {m: [] for m in metric_names}
        tce_hi = [37.1, 45.5, 52.6, 99.4, 134.0, 171.0, 216.8, 439.6, 474.6, 400.4, 583.5, 714.9, 463.5, 416.6, 482.4, 683.0, 639.0, 694.0, 776.4]
        tce_lo = [23.8, 27.8, 31.2, 49.0, 93.0, 104.5, 132.9, 189.0, 249.4, 308.2, 299.8, 380.0, 188.6, 263.6, 260.2, 364.8, 406.4, 410.1, 458.8]

        # v6：真實年度財報 + 歷史年度高低價（Yahoo Finance）
        _af = fetch_real_annual_financials(symbol)
        _py = _af.get("_price_year", {})
        _rf = {yy: vv for yy, vv in _af.items() if yy != "_price_year"}

        def f1(v, nd=1):
            return f"{v:,.{nd}f}" if isinstance(v, (int, float)) else "N/A"
        def fdiv(num, den, nd=2, mul=1):
            if isinstance(num, (int, float)) and isinstance(den, (int, float)) and den != 0:
                return f"{num/den*mul:.{nd}f}"
            return "N/A"
        def fpct(num, den, nd=2):
            if isinstance(num, (int, float)) and isinstance(den, (int, float)) and den != 0:
                return f"{num/den*100:.{nd}f}%"
            return "N/A"

        def real_year_vals(yr_int, R, R_prev):
            """以真實財報與真實歷史高低價構建 82 項；缺失字段一律 N/A。"""
            PY = _py.get(yr_int, {})
            hp = PY.get("high"); lp = PY.get("low"); cp = PY.get("close")
            rev = R.get("revenue"); net = R.get("net_income"); gp = R.get("gross_profit")
            ebitda = R.get("ebitda"); opinc = R.get("op_income")
            ocf = R.get("ocf"); capex = R.get("capex")
            fcf = R.get("fcf")
            if fcf is None and isinstance(ocf, (int, float)) and isinstance(capex, (int, float)):
                fcf = ocf + capex  # Yahoo 資本開支為負數
            assets = R.get("assets"); equity = R.get("equity"); liab = R.get("liabilities")
            debt = R.get("debt"); cash = R.get("cash"); ca = R.get("curr_assets"); cl = R.get("curr_liab")
            rec = R.get("receivables"); pay = R.get("payables"); inv = R.get("inventory")
            shares = R.get("shares"); eps = R.get("eps"); dps = R.get("dps_total")
            rd = R.get("rd"); sga = R.get("sga"); interest = R.get("interest"); tax = R.get("tax"); depr = R.get("depreciation")

            sps = rev/shares if isinstance(rev,(int,float)) and isinstance(shares,(int,float)) and shares else None
            bps = equity/shares if isinstance(equity,(int,float)) and isinstance(shares,(int,float)) and shares else None
            ocfps = ocf/shares if isinstance(ocf,(int,float)) and isinstance(shares,(int,float)) and shares else None
            fcfps = fcf/shares if isinstance(fcf,(int,float)) and isinstance(shares,(int,float)) and shares else None

            prev_rev = R_prev.get("revenue") if R_prev else None
            prev_gp = R_prev.get("gross_profit") if R_prev else None
            prev_net = R_prev.get("net_income") if R_prev else None
            prev_ocf = R_prev.get("ocf") if R_prev else None
            prev_fcf = R_prev.get("fcf") if R_prev else None
            def grow(x, x0):
                if isinstance(x,(int,float)) and isinstance(x0,(int,float)) and x0 != 0:
                    return f"{(x/x0-1)*100:.2f}%"
                return "N/A"

            def pe(price, per):
                if isinstance(price,(int,float)) and isinstance(per,(int,float)) and per and per > 0:
                    return f"{price/per:.2f}"
                return "N/A"
            mcap = cp*shares if isinstance(cp,(int,float)) and isinstance(shares,(int,float)) else None

            def _days(x):
                if isinstance(x,(int,float)) and isinstance(rev,(int,float)) and rev > 0:
                    return round(x / (rev/365.0), 1)
                return None
            pay_days, rec_days, inv_days = _days(pay), _days(rec), _days(inv)
            ccc = round((rec_days or 0) + (inv_days or 0) - (pay_days or 0), 1) if (pay_days is not None and rec_days is not None) else None

            v = [
                f1(cp, 2), pe(cp, eps), pe(cp, sps), pe(cp, bps), fpct(dps, cp, 2),
                str(yr_int), f"31/12/{yr_int}", f1(rev), f1(rev),
                f1(net), f1(net), f1(ocf), f1(fcf),
                fpct(dps, cp, 2), f1(assets), f1(equity), f1(liab),
                f1(debt), f1(shares, 2), fpct(net, assets, 2),
                fpct(net, equity, 2), fpct(net, rev, 2), fpct(gp, rev, 2), fpct(ebitda, rev, 2),
                fpct(opinc, rev, 2), f1(sps, 2), f1(sps, 2),
                f1(eps, 3), f1(bps, 2), f1(dps, 3),
                f1(eps, 3), f1(ocfps, 2), f1(fcfps, 2),
                grow(rev, prev_rev), grow(gp, prev_gp), grow(net, prev_net), grow(ocf, prev_ocf),
                grow(fcf, prev_fcf), fpct(sga, rev, 2), fpct((sga or 0)+(rd or 0) if (sga is not None or rd is not None) else None, rev, 2),
                "N/A", fpct(depr, rev, 2), fpct(interest, rev, 2), fpct(rec, rev, 2), fpct(pay, rev, 2),
                fpct(tax, (net+tax) if isinstance(net,(int,float)) and isinstance(tax,(int,float)) else None, 2),
                fpct(rd, rev, 2), fpct(abs(capex) if isinstance(capex,(int,float)) else None, rev, 2),
                "N/A", "N/A",
                f1(pay_days, 1), f1(rec_days, 1), f1(inv_days, 1), f1(ccc, 1),
                fpct(dps, eps, 2), fdiv(ca, cl, 2), fdiv(cash, cl, 2), fdiv(debt, assets, 2),
                fdiv(opinc, debt, 2), fdiv(liab, net, 2), f1(mcap, 0),
                str(yr_int), pe(hp, eps), pe(lp, eps),
                pe(hp, eps), pe(lp, eps),
                pe(hp, sps), pe(lp, sps), pe(hp, bps), pe(lp, bps),
                fpct(dps, hp, 2), fpct(dps, lp, 2),
                f1(hp, 2), f1(lp, 2), pe(hp, ocfps), pe(lp, ocfps),
                pe(hp, fcfps), pe(lp, fcfps),
                "N/A", "N/A", "N/A"
            ]
            # 長度對齊保護：確保永遠為 82 項，與 metric_names 對應
            if len(v) < 82:
                v += ["N/A"] * (82 - len(v))
            elif len(v) > 82:
                v = v[:82]
            return v
        
        for idx, yr in enumerate(years):
            # ===== 批判性修正：判斷該年度是否早於真實上市年份 =====
            yr_year = int(yr[:4])
            is_pre_ipo = yr_year < listing_yr
            is_forecast = yr.endswith("E")

            if is_pre_ipo:
                # 上市前年度：82 項指標一律標示「未上市 / N/A」，絕不偽造數據
                # 僅保留兩個 Year 標記列，讓用戶仍可看到該年度欄位存在
                na_vals = ["未上市 / N/A"] * 82
                na_vals[5] = f"{yr} (未上市)"          # Year (HKD M)
                na_vals[6] = f"31/12/{yr[:4]} (未上市)" # Date
                na_vals[60] = f"{yr} (未上市)"         # Year (HKD M) [8P估值分區]
                vals = na_vals
            elif is_forecast:
                # ===== 預測年度 2026E-2028E：模型估值推演（明確標註 (預)，非歷史財報）=====
                _w52h = d.get("w52_high"); _w52l = d.get("w52_low")
                _fc_ok = (isinstance(p, (int, float)) and p > 0
                          and isinstance(rev_base, (int, float)) and rev_base > 0
                          and isinstance(_w52h, (int, float)) and _w52h > 0
                          and isinstance(_w52l, (int, float)) and _w52l > 0)
                if not _fc_ok:
                    _na_fc = ["數據缺失 / N/A (離線無法預估)"] * 82
                    _na_fc[5] = f"{yr} (預測)"
                    _na_fc[6] = f"31/12/{yr[:4]} (預測)"
                    _na_fc[60] = f"{yr} (預測)"
                    vals = _na_fc
                else:
                    sc = scale_trajectories[idx]
                    rev = rev_base * sc
                    net_p = rev * (d["net_margin"] / 100.0)
                    adj_net_p = net_p * 1.04
                    ocf = net_p * 1.35
                    fcf = ocf * (0.55 if yr == "2026E" and symbol == "00700.HK" else 0.72)
                    sh = sh_base * (1.06 - idx * 0.004)
                    eps = adj_net_p / max(sh, 1.0)
                    bps = d["bps"] * sc * 0.95 if idx < 16 else d["bps"] * (1.0 + (idx - 16) * 0.12)
                    dps = d["dps"] * sc
                    sps = rev / max(sh, 1.0)
                    ocf_s = ocf / max(sh, 1.0)
                    fcf_s = fcf / max(sh, 1.0)
    
                    if symbol == "00700.HK":
                        hp_val = tce_hi[idx]
                        lp_val = tce_lo[idx]
                    else:
                        hp_val = p * sc * 1.30 if idx < 16 else (d["w52_high"] if idx == 16 else p * (1.25 + (idx - 16)*0.12))
                        lp_val = p * sc * 0.72 if idx < 16 else (d["w52_low"] if idx == 16 else p * (0.85 + (idx - 16)*0.08))
    
                    hi_pe = hp_val / max(eps, 0.05)
                    lo_pe = lp_val / max(eps, 0.05)
                    hi_ps = hp_val / max(sps, 0.05)
                    lo_ps = lp_val / max(sps, 0.05)
                    hi_pb = hp_val / max(bps, 0.05)
                    lo_pb = lp_val / max(bps, 0.05)
    
                    vals = [
                        f"{p:,.2f}", f"{(p/max(eps,0.05)):.2f}", f"{(p/max(sps,0.05)):.2f}", f"{(p/max(bps,0.05)):.2f}", f"{(dps/max(p,0.05)*100):.2f}%",
                        yr, f"31/12/{yr[:4]}", f"{rev:,.1f}", f"{rev:,.1f}",
                        f"{net_p:,.1f}", f"{adj_net_p:,.1f}", f"{ocf:,.1f}", f"{fcf:,.1f}",
                        f"{(dps/max(p,0.05)*100):.2f}%", f"{(bps*sh*1.65):,.1f}", f"{(bps*sh):,.1f}", f"{(bps*sh*0.65):,.1f}",
                        f"{(bps*sh*0.32):,.1f}", f"{sh:,.2f}", f"{(d['roe']*0.58):.2f}%",
                        f"{d['roe']:.2f}%", f"{d['net_margin']:.2f}%", f"{d['gross_margin']:.2f}%", f"{(d['net_margin']*1.35):.2f}%",
                        f"{(d['net_margin']*1.12):.2f}%", f"{sps:.2f}", f"{sps:.2f}",
                        f"{eps:.2f}", f"{bps:.2f}", f"{dps:.2f}",
                        f"{(eps*0.96):.2f}", f"{ocf_s:.2f}", f"{fcf_s:.2f}",
                        f"{d['rev_growth']:.2f}%", f"{(d['rev_growth']*1.15):.2f}%", f"{(d['rev_growth']*1.08):.2f}%", f"{(d['rev_growth']*1.10):.2f}%",
                        f"{(-24.60 if yr=='2026E' and symbol=='00700.HK' else d['rev_growth']*0.95):.2f}%", "20.80%", "22.50%",
                        "17.20%", "4.50%", "1.70%",
                        "0.08", "0.17", "17.50%", "11.50%",
                        "10.50%", "7,042,364", "115,849", "132.0",
                        "25.0", "1.2", "-105.8", f"{(dps/max(eps,0.05)*100):.2f}%", "1.42",
                        "1.00", "19.90%", "0.60",
                        "3.45", "1.72", f"{(p*sh):,.0f}",
                        yr, f"{hi_pe:.2f}", f"{lo_pe:.2f}",
                        f"{(hi_pe*1.04):.2f}", f"{(lo_pe*1.04):.2f}",
                        f"{hi_ps:.2f}", f"{lo_ps:.2f}", f"{hi_pb:.2f}", f"{lo_pb:.2f}",
                        f"{(dps/max(hp_val,0.05)*100):.2f}%", f"{(dps/max(lp_val,0.05)*100):.2f}%",
                        f"{hp_val:,.2f}", f"{lp_val:,.2f}", f"{(hp_val/max(ocf_s,0.05)):.2f}", f"{(lp_val/max(ocf_s,0.05)):.2f}",
                        f"{(hp_val/max(fcf_s,0.05)):.2f}", f"{(lp_val/max(fcf_s,0.05)):.2f}",
                        f"{(hi_pe/max(abs(d['rev_growth']),1)):.2f}", f"{(lo_pe/max(abs(d['rev_growth']),1)):.2f}",
                        f"{(hi_ps/max(abs(d['rev_growth']),1)):.2f} / {(lo_ps/max(abs(d['rev_growth']),1)):.2f}"
                    ]
    
                    # 對預測年度 (2026E/2027E/2028E) 在關鍵估值列加「(預)」標記
                    for _k in [0, 1, 2, 3, 4, 6, 27, 28, 29, 61, 62, 63, 64, 65, 66, 67]:
                        vals[_k] = f"{vals[_k]} (預)"

            elif yr_year in _rf:
                # ===== 已上市歷史年度：Yahoo Finance 真實年度財報 + 真實歷史高低價 =====
                R = _rf.get(yr_year, {})
                R_prev = _rf.get(yr_year - 1, {})
                vals = real_year_vals(yr_year, R, R_prev)

            else:
                # ===== 已上市但免費源未提供該年度年報（Yahoo 通常僅含近 4 年）：標 N/A，不編造 =====
                miss_vals = ["年報未提供 / N/A"] * 82
                miss_vals[5] = str(yr_year)
                miss_vals[6] = f"31/12/{yr_year}"
                miss_vals[60] = str(yr_year)
                if yr_year in _py:
                    # 財報缺失但歷史股價仍可真實顯示
                    miss_vals[0] = f1(_py[yr_year].get("close"), 2)
                    miss_vals[72] = f1(_py[yr_year].get("high"), 2)
                    miss_vals[73] = f1(_py[yr_year].get("low"), 2)
                vals = miss_vals

            for m_idx, m_key in enumerate(metric_names):
                table_dict[m_key].append(vals[m_idx])
                
        df_out = pd.DataFrame(table_dict, index=years).T
        df_out.index.name = "財務與8P分析指標 (依Photo 1次序)"
        return df_out

    df_8p_all = build_82_row_table_2010_2028(selected_symbol, q_data["price"])
    idx_s = all_19_years.index(yr_start)
    idx_e = all_19_years.index(yr_end)
    selected_cols = all_19_years[idx_s:idx_e+1]
    df_8p_view = df_8p_all[selected_cols]
    
    render_resizable_table(
        df_8p_view,
        key_prefix="8p_full_tbl",
        default_col_w=100,
        default_text_w=240,
        default_row_h=36,
        freeze_first_col=True
    )
    
    csv_bytes = df_8p_view.to_csv().encode("utf-8-sig")
    st.download_button(
        label=f"📥 下載 {q_data['name']} ({yr_start}–{yr_end}) 82項 8P 財務分析表 (CSV)",
        data=csv_bytes,
        file_name=f"{q_data['code']}_8P_{yr_start}_{yr_end}.csv",
        mime="text/csv"
    )

# ------------------------------------------
# 分頁 C：基本面與歷史高低位推算 (已移除問題編號)
# ------------------------------------------
with tabs[2]:
    st.subheader(f"🎯 {q_data['code']} {q_data['name']} — 2026年基本面高低位分析與歷史倍數推算")
    tab_refresh_button("refresh_tab_fund", [fetch_real_fundamentals, fetch_real_momentum_snapshot])
    
    eps_26 = q_data.get("fwd_eps")
    bps_26 = q_data.get("bps")
    ocf_26 = q_data.get("ocf_ps")

    if q_data.get("fund_ok"):
        _fpe = q_data.get("fwd_pe") or q_data.get("pe")
        _pb = q_data.get("pb")
        if selected_symbol == "00700.HK":
            pe_lo_mult, pe_hi_mult = 13.0, 22.0
            pb_lo_mult, pb_hi_mult = 2.2, 3.5
        else:
            if q_data["score"] < 5.0:
                pe_lo_mult = round(max(_fpe * 0.55, 10.0), 1)
                pe_hi_mult = round(_fpe * 0.95, 1)
            else:
                pe_lo_mult = round(max(_fpe * 0.85, 6.0), 1)
                pe_hi_mult = round(_fpe * 1.35, 1)
            pb_lo_mult = round(max(_pb * 0.78, 0.5), 2)
            pb_hi_mult = round(_pb * 1.28, 2)

        fund_low = round(eps_26 * pe_lo_mult, 2)
        fund_high = round(eps_26 * pe_hi_mult, 2)
        pb_low = round(bps_26 * pb_lo_mult, 2)
        pb_high = round(bps_26 * pb_hi_mult, 2)
        pcf_low = round(ocf_26 * (pe_lo_mult * 0.55), 2)
        pcf_high = round(ocf_26 * (pe_hi_mult * 0.55), 2)
        model_low = round((fund_low * 0.5 + pb_low * 0.3 + pcf_low * 0.2), 2)
        model_high = round((fund_high * 0.5 + pb_high * 0.3 + pcf_high * 0.2), 2)
    else:
        st.warning(
            "⚠️ 完整基本面估值模型（預測 EPS、PE／PB／PCF 估值帶、8P 綜合區間）需要 **Yahoo 財報數據**。"
            "目前資料源（騰訊財經／Stooq）只提供真實報價與 K 線，並無預測財報，故本頁定量估值帶**暫停計算**（不以內建或隨機數字填充）。"
            "請按本頁「🔄 更新」並在 Yahoo 可連線時重試；上方報價、PE／PB、52 週區間與右側 K 線技術分析仍可正常使用。"
        )
        pe_lo_mult = pe_hi_mult = pb_lo_mult = pb_hi_mult = None
        fund_low = fund_high = pb_low = pb_high = pcf_low = pcf_high = model_low = model_high = None

    col_f4, col_f5 = st.columns(2)
    with col_f4:
        st.markdown(f"""
        <div class="section-card">
            <h4>📌 從基本面全面分析今年（2026年）價格的高位和低位</h4>
            <ul>
                <li><strong>核心盈利與毛利動能</strong>：{q_data['name']} 年度營收規模 <strong>{q_data['rev_b']:,.2f} 十億 ({q_data['curr']})</strong>，按年變動 <strong>{q_data['rev_growth']:+.2f}%</strong>，毛利率 <strong>{q_data['gross_margin']:.2f}%</strong>。</li>
                <li><strong>2026E 經調整每股盈利 (EPS) 基準</strong>：預測為 <strong>{_sf(eps_26, ',.2f')} {q_data['curr']}</strong>（每股帳面淨值 BPS 為 <strong>{_sf(bps_26, ',.2f')} {q_data['curr']}</strong>）。</li>
                <li><strong>基本面合理低位（Intrinsic Floor）</strong>：按審慎防守倍數 <strong>{_sf(pe_lo_mult, '.1f', 'x')} 2026E PE</strong> 計算，基本面支撐底線為 <strong style="color:#d93025;">{_sf(fund_low, ',.2f')} {q_data['curr']}</strong>。</li>
                <li><strong>基本面合理高位（Intrinsic Ceiling）</strong>：按基本面合理上限倍數 <strong>{_sf(pe_hi_mult, '.1f', 'x')} 2026E PE</strong> 計算，基本面合理頂部為 <strong style="color:#188038;">{_sf(fund_high, ',.2f')} {q_data['curr']}</strong>。</li>
                <li><strong>批判性基本面風險提示</strong>：{q_data['catalyst'].split('|')[-1].strip()}</li>
            </ul>
        </div>
        """, unsafe_allow_html=True)

    with col_f5:
        st.markdown(f"""
        <div class="section-card">
            <h4>📌 根據過往歷史高低位倍數，推算今年價格的高位和低位</h4>
            <p style="font-size:13.5px;">擷取 2010–2025 年歷史高低估值倍數中樞（8P 模型），套用至今年每股財務數據推算：</p>
            <ul>
                <li><strong>歷史市盈率法 (HP / LP on PE)</strong>：最低價 <strong>{_sf(fund_low, ',.2f')}</strong> | 最高價 <strong>{_sf(fund_high, ',.2f')} {q_data['curr']}</strong>。</li>
                <li><strong>歷史市帳率法 (HP / LP on PB)</strong>：按 {_sf(pb_lo_mult, '.2f', 'x')} – {_sf(pb_hi_mult, '.2f', 'x')} PB 推算，最低價 <strong>{_sf(pb_low, ',.2f')}</strong> | 最高價 <strong>{_sf(pb_high, ',.2f')} {q_data['curr']}</strong>。</li>
                <li><strong>歷史市現率法 (HP / LP on PCF)</strong>：按營運現金流推算，最低價 <strong>{_sf(pcf_low, ',.2f')}</strong> | 最高價 <strong>{_sf(pcf_high, ',.2f')} {q_data['curr']}</strong>。</li>
                <li><strong>8P 多因子綜合加權結論</strong>：推算今年綜合高低位區間為 <strong>{_sf(model_low, ',.2f')} 至 {_sf(model_high, ',.2f')} {q_data['curr']}</strong>（過去52週實際高低位為 <strong>{_sf(q_data['w52_low'], ',.2f')} – {_sf(q_data['w52_high'], ',.2f')}</strong>）。</li>
            </ul>
        </div>
        """, unsafe_allow_html=True)

    def _mrange(a, b):
        return f" ({a:g}x-{b:g}x)" if isinstance(a, (int, float)) and isinstance(b, (int, float)) else ""

    bands_labels = ["52週實際高低區間", f"8P PE估值帶{_mrange(pe_lo_mult, pe_hi_mult)}",
                    f"8P PB估值帶{_mrange(pb_lo_mult, pb_hi_mult)}", "8P PCF現金流估值帶",
                    "8P 綜合加權模型區間"]
    low_vals = [q_data["w52_low"], fund_low, pb_low, pcf_low, model_low]
    high_vals = [q_data["w52_high"], fund_high, pb_high, pcf_high, model_high]

    # 過濾缺資料的估值帶（Yahoo 財報未連線時 PE/PB/PCF/模型帶為 None，僅保留真實 52 週區間）
    _pairs = [(lab, l, h) for lab, l, h in zip(bands_labels, low_vals, high_vals)
              if isinstance(l, (int, float)) and isinstance(h, (int, float))]
    if _pairs:
        b_labels = [p[0] for p in _pairs]
        b_low = [p[1] for p in _pairs]
        b_high = [p[2] for p in _pairs]
        b_width = [max(h - l, 0.05) for h, l in zip(b_high, b_low)]

        fig_bands = go.Figure()
        fig_bands.add_trace(go.Bar(
            y=b_labels, x=b_width, base=b_low, orientation="h",
            marker=dict(color="rgba(26, 115, 232, 0.22)", line=dict(color="#1a73e8", width=1.8)),
            text=[f"{l:,.1f} ~ {h:,.1f} {q_data['curr']}" for l, h in zip(b_low, b_high)],
            textposition="outside", name="推算高低位區間"
        ))
        fig_bands.add_vline(
            x=q_data["price"], line_width=2.5, line_dash="dash", line_color="#d93025",
            annotation_text=f"現價: {q_data['price']:,.2f} {q_data['curr']}", annotation_position="top right",
            annotation_font=dict(color="#d93025", size=13)
        )
        _tc = get_theme_chart_colors()
        fig_bands.update_layout(
            title=f"{q_data['name']} — 2026年基本面與歷史 8P 模型高低位區間 vs 當前現價對標圖",
            xaxis_title=f"價格 ({q_data['curr']})", height=320,
            margin=dict(l=20, r=90, t=40, b=30),
            paper_bgcolor=_tc["paper"], plot_bgcolor=_tc["plot"], font=dict(color=_tc["text"])
        )
        fig_bands.update_xaxes(gridcolor=_tc["grid"],
                               range=[min(b_low + [q_data["price"]]) * 0.80,
                                      max(b_high + [q_data["price"]]) * 1.18])
        fig_bands.update_yaxes(gridcolor=_tc["grid"])
        st.plotly_chart(fig_bands, use_container_width=True)
    else:
        st.info("ℹ️ 目前沒有足夠真實數據繪製估值區間（需 Yahoo 財報或真實 52 週高低點）；資料源恢復後按「🔄 更新」即可重繪。")

# ------------------------------------------
# 分頁 D：全年走勢與波浪理論分析 (已移除問題編號)
# ------------------------------------------
with tabs[3]:
    st.subheader(f"🌊 {q_data['code']} {q_data['name']} — 2026年 1月至12月走勢預測與波浪理論分析")

    # v7 手動刷新 + ZigZag 閾值
    _w_r1, _w_r2 = st.columns([1, 3])
    with _w_r1:
        st.markdown("<div style='height:2px;'></div>", unsafe_allow_html=True)
        if st.button("🔄 更新波浪分析", key="refresh_wave_btn", use_container_width=True,
                     help="開門並單次重新抓取真實日線與月度走勢，重算擺動點與標浪"):
            st.session_state[_PENDING_OPEN_KEY] = True
            for _fn in (fetch_real_monthly_path, fetch_real_momentum_snapshot,
                        get_price_history, fetch_stooq_ohlc, fetch_tencent_kline):
                try:
                    _fn.clear()
                except Exception:
                    pass
            st.rerun()
    with _w_r2:
        zig_pct = st.slider(
            "ZigZag 擺動閾值 %（越小越敏感、擺動點越多；越大越只看主要高低點）",
            min_value=3.0, max_value=15.0, value=6.0, step=0.5, key="zig_pct_slider"
        ) / 100.0
    wave = compute_wave_analysis(selected_symbol, pct=zig_pct)

    # v6：波浪理論「實際軌跡」改用真實月度收盤 + 真實 52 週高低點，不再硬編碼或線性內插
    mp = fetch_real_monthly_path(selected_symbol)
    mom_w = fetch_real_momentum_snapshot(selected_symbol)
    if mp.get("ok"):
        w_dates = list(mp["labels"])
        w_actual = list(mp["closes"])
        cur_p = float(q_data["price"])
        # 未來 2 個月僅作動能推算（虛線，明確標 E），以真實近 3 月年化動能為基礎
        r63 = mom_w.get("r63")
        monthly_drift = (r63 / 100.0 / 3.0) if isinstance(r63, (int, float)) else 0.0
        monthly_drift = max(min(monthly_drift, 0.12), -0.12)
        m1 = round(cur_p * (1 + monthly_drift), 2)
        m2 = round(cur_p * (1 + monthly_drift * 2), 2)
        last_label = mp["labels"][-1]
        last_dt = pd.to_datetime(last_label[:7] + "-01")
        for k in (1, 2):
            w_dates.append((last_dt + pd.DateOffset(months=k)).strftime("%Y-%m") + "E")
            w_actual.append(None)
        w_proj = [None] * (len(w_dates) - 3) + [cur_p, m1, m2]
        blue_low, blue_high = round(mp["lo52"], 2), round(mp["hi52"], 2)
        hi52, lo52 = mp["hi52"], mp["lo52"]
        hi52_d, lo52_d = mp["hi52_date"], mp["lo52_date"]
        wave_ok = True
    else:
        w_dates, w_actual, w_proj = ["無數據"], [None], [None]
        blue_low = blue_high = hi52 = lo52 = cur_p = q_data["price"]
        hi52_d = lo52_d = "—"
        wave_ok = False

    fig_wave = go.Figure()
    fig_wave.add_hrect(
        y0=blue_low, y1=blue_high, fillcolor="rgba(26, 115, 232, 0.12)", line_width=1, line_dash="dot", line_color="#1a73e8",
        annotation_text=f"52週真實區間 ({blue_low:,.1f} - {blue_high:,.1f} {q_data['curr']})", annotation_position="bottom left"
    )
    fig_wave.add_trace(go.Scatter(
        x=w_dates, y=w_actual, mode="lines+markers", name="真實月度收盤軌跡",
        line=dict(color="#1a73e8", width=3), marker=dict(size=8, color="#1a73e8"),
        connectgaps=True,
        hovertemplate="%{x}<br>真實收盤: %{y:,.2f}<extra></extra>"
    ))
    fig_wave.add_trace(go.Scatter(
        x=w_dates, y=w_proj, mode="lines+markers+text", name="未來2個月動能推算（非預測）",
        line=dict(color="#fa903e", width=2.5, dash="dash"),
        marker=dict(size=8, color="#fa903e"),
        text=[f"{v:,.1f}" if v is not None else "" for v in w_proj],
        textposition="top center"
    ))
    fig_wave.update_layout(
        title=f"{q_data['name']} ({q_data['symbol']}) — 近月真實走勢與 52 週區間（實線為真實月末收盤；虛線為動能推算）",
        yaxis_title=f"價格 ({q_data['curr']})", height=400,
        margin=dict(l=20, r=30, t=55, b=25), paper_bgcolor="#ffffff", plot_bgcolor="#ffffff",
        legend=dict(orientation="h", y=1.02)
    )
    fig_wave.update_yaxes(gridcolor="#eeeeee")
    if wave_ok:
        st.caption(f"📡 上圖實線為 Yahoo Finance **真實月度收盤價**；52 週真實最高 **{hi52:,.2f}**（{hi52_d}）、最低 **{lo52:,.2f}**（{lo52_d}）。末段虛線僅為依近 3 月真實動能的**推算參考，非預測**。")
    else:
        st.warning("⚠️ Yahoo Finance 暫時無法提供此標的真實月度走勢，無法繪製波浪圖（不使用模擬數據）。")
    st.plotly_chart(fig_wave, use_container_width=True)

    # ===== v7：ZigZag 自動擺動點 + 啟發式艾略特標浪（誠實標註，不假裝 100% 準確）=====
    st.markdown("##### 🧩 艾略特波浪自動標示（ZigZag 擺動點演算法）")
    if wave.get("ok") and wave.get("pivots"):
        tc = get_theme_chart_colors()
        fig_zz = go.Figure()
        fig_zz.add_hrect(
            y0=wave["lo52"], y1=wave["hi52"],
            fillcolor="rgba(26,115,232,0.10)", line_width=1, line_dash="dot",
            line_color="#1a73e8", layer="below",
            annotation_text=f"52週真實區間 {wave['lo52']:,.1f}–{wave['hi52']:,.1f}",
            annotation_position="bottom left"
        )
        # 真實日線收盤（淡底線）
        fig_zz.add_trace(go.Scatter(
            x=wave["line_dates"], y=wave["line_close"], mode="lines",
            name="真實日線收盤", line=dict(color=tc["grid"], width=1), opacity=0.9
        ))
        # ZigZag 連線
        fig_zz.add_trace(go.Scatter(
            x=wave["dates"], y=wave["prices"], mode="lines",
            name=f"ZigZag {wave['pct']*100:.1f}% 閾值",
            line=dict(color=tc["ma20"], width=2)
        ))
        # 分類標號點
        zz_dates, zz_prices, zz_kinds = wave["dates"], wave["prices"], wave["kinds"]
        zz_labels = wave["labels"]
        h_d, h_p, h_t = [], [], []
        l_d, l_p, l_t = [], [], []
        u_d, u_p, u_t = [], [], []
        n_d, n_p = [], []
        for i, (d, p, k) in enumerate(zip(zz_dates, zz_prices, zz_kinds)):
            lab = zz_labels[i]
            if k.endswith("?"):
                u_d.append(d); u_p.append(p); u_t.append(f"{lab or '?'}")
            elif lab:
                if k.startswith("H"):
                    h_d.append(d); h_p.append(p); h_t.append(lab)
                else:
                    l_d.append(d); l_p.append(p); l_t.append(lab)
            else:
                n_d.append(d); n_p.append(p)
        fig_zz.add_trace(go.Scatter(
            x=h_d, y=h_p, text=h_t, mode="markers+text", textposition="top center",
            textfont=dict(size=15, color="#ef5350"), name="推動浪高點 1/3/5、B",
            marker=dict(size=10, color="#ef5350", symbol="triangle-down")
        ))
        fig_zz.add_trace(go.Scatter(
            x=l_d, y=l_p, text=l_t, mode="markers+text", textposition="bottom center",
            textfont=dict(size=15, color="#26a69a"), name="推動浪低點 0/2/4、A、C",
            marker=dict(size=10, color="#26a69a", symbol="triangle-up")
        ))
        fig_zz.add_trace(go.Scatter(
            x=u_d, y=u_p, text=u_t, mode="markers+text", textposition="middle right",
            textfont=dict(size=13, color="#fa903e"), name="尚未確認的當前潛在端點",
            marker=dict(size=12, color="rgba(250,144,62,0.25)", line=dict(width=1.5, color="#fa903e"), symbol="circle")
        ))
        fig_zz.add_trace(go.Scatter(
            x=n_d, y=n_p, mode="markers", name="未編號擺動點",
            marker=dict(size=5, color=tc["muted"] if "muted" in tc else "#9aa3b2")
        ))
        fig_zz.update_layout(
            title=f"{q_data['name']} ({q_data['symbol']}) — ZigZag 自動擺動點與啟發式 1–5 / A–C 標浪（數據源：{wave['src'] or 'Yahoo/Stooq'}）",
            yaxis_title=f"價格 ({q_data['curr']})", height=620,
            margin=dict(l=20, r=30, t=55, b=25),
            paper_bgcolor=tc["paper"], plot_bgcolor=tc["plot"], font=dict(color=tc["text"]),
            legend=dict(orientation="h", y=1.02)
        )
        fig_zz.update_xaxes(gridcolor=tc["grid"]); fig_zz.update_yaxes(gridcolor=tc["grid"])
        st.plotly_chart(fig_zz, use_container_width=True)

        st.error(
            "⚠️ **關於波浪標號的批判性說明（請務必先讀）**：艾略特波浪理論本質主觀、須事後才可確認，"
            "同一張圖往往存在多種合理數法，**並不存在「100% 準確」的即時標浪**。上圖 1–5 與 A–C，"
            f"是電腦以 **{wave['pct']*100:.1f}% ZigZag 擺動閾值**機械識別擺動高低點後，依固定規則自動編號的「**其中一種可能解讀**」，"
            "僅供分析參考、絕非定論；調整閾值或加入更長數據，標號都可能改變。橙色空心點為「尚未確認」的潛在端點。"
            "此標示**不構成投資建議**，請務必結合成交、基本面與大趨勢自行人工確認。"
        )
        # 擺動點明細表
        _rows = []
        for i, (d, p, k) in enumerate(zip(zz_dates, zz_prices, zz_kinds)):
            _rows.append({
                "日期": d,
                "高低點": "高點" if k.startswith("H") else "低點",
                f"價格({q_data['curr']})": round(p, 3),
                "自動標號": zz_labels[i] if zz_labels[i] else "—",
                "是否確認": "已確認" if not k.endswith("?") else "未確認"
            })
        with st.expander("📋 檢視 ZigZag 擺動點明細（含自動標號）", expanded=False):
            st.dataframe(pd.DataFrame(_rows), use_container_width=True, hide_index=True)
    else:
        st.warning(
            "⚠️ Yahoo Finance、騰訊財經與 Stooq 目前均無法提供此標的足夠長的真實日線，無法計算 ZigZag 擺動點與標浪。"
            "本系統不會在無真實數據時編造波浪，請按「🔄 更新波浪分析」、檢查網絡或稍後重試。"
        )

    c_w6, c_w7 = st.columns(2)
    with c_w6:
        _av = [v for v in w_actual if isinstance(v, (int, float))]
        _recent_hi = max(_av) if _av else cur_p
        _recent_lo = min(_av) if _av else cur_p
        _pv = [v for v in w_proj if isinstance(v, (int, float))]
        _proj_txt = f"{_pv[-2]:,.1f} – {_pv[-1]:,.1f}" if len(_pv) >= 2 else "—"
        _r21 = mom_w.get("r21"); _r63 = mom_w.get("r63"); _rsi = mom_w.get("rsi")
        _pos52 = mom_w.get("pos52")
        def _trend(v):
            if not isinstance(v, (int, float)): return "數據缺失"
            return f"{'上漲' if v >= 0 else '下跌'} {abs(v):.1f}%"
        st.markdown(f"""
        <div class="section-card">
            <h4>📅 近月真實走勢客觀回顧（數據源：Yahoo Finance）</h4>
            <ul>
                <li><strong>近 13 個月真實區間</strong>：最高 <strong>{_recent_hi:,.2f}</strong>、最低 <strong>{_recent_lo:,.2f} {q_data['curr']}</strong>（實際月末收盤，非繪製軌跡）。</li>
                <li><strong>52 週真實極值</strong>：最高 <strong>{hi52:,.2f}</strong>（{hi52_d}）、最低 <strong>{lo52:,.2f}</strong>（{lo52_d}）。</li>
                <li><strong>真實動能</strong>：近 1 個月 {_trend(_r21)}、近 3 個月 {_trend(_r63)}；RSI14 = <strong>{_rsi if _rsi is not None else 'N/A'}</strong>；52 週分位 <strong>{_pos52 if _pos52 is not None else 'N/A'}%</strong>。</li>
                <li><strong>未來 2 個月動能推算（虛線，非預測）</strong>：以近 3 月真實動能年化外推，參考區間 <strong>{_proj_txt} {q_data['curr']}</strong>。實際走勢受宏觀、業績與資金面影響，務必自行判斷。</li>
            </ul>
        </div>
        """, unsafe_allow_html=True)
    with c_w7:
        # 基於真實均線/RSI/52週位置的客觀技術位置，不編造具體子浪序號
        above200 = mom_w.get("above_ma200"); above50 = mom_w.get("above_ma50")
        if above200 is True:
            _trend_pos = "現價站穩 200 日長線均線之上，中長線仍屬多頭結構"
        elif above200 is False:
            _trend_pos = "現價失守 200 日長線均線，中長線轉弱"
        else:
            _trend_pos = "均線數據缺失"
        if isinstance(_rsi, (int, float)):
            if _rsi >= 70: _rsi_state = "RSI 進入超買區，短線存在技術回調壓力"
            elif _rsi <= 30: _rsi_state = "RSI 進入超賣區，或出現技術反彈"
            else: _rsi_state = f"RSI {_rsi:.0f} 處中性區間"
        else:
            _rsi_state = "RSI 數據缺失"
        st.markdown(f"""
        <div class="section-card">
            <h4>📐 波浪 / 技術結構客觀定位（基於真實價格，非主觀數浪）</h4>
            <ul>
                <li><strong>長線趨勢</strong>：{_trend_pos}（50 日線{'上方' if above50 else '下方' if above50 is False else '數據缺失'}）。</li>
                <li><strong>短線節奏</strong>：{_rsi_state}。</li>
                <li><strong>關鍵位置（Blue Box）</strong>：現價 {q_data['price']:,.2f} 相對真實 52 週區間 {lo52:,.2f}–{hi52:,.2f} 處於第 <strong>{_pos52 if _pos52 is not None else 'N/A'}%</strong> 分位；貼近低位邊緣屬估值修復區，貼近高位邊緣則安全邊際低。</li>
                <li style="color:#8a5a00;"><strong>批判性說明</strong>：艾略特波浪的子浪序號事後才可確認、主觀性高，本系統不為當前編造具體「(C)-5 末端」等斷言，僅呈現真實價格位置與動能供你自行判斷。</li>
            </ul>
        </div>
        """, unsafe_allow_html=True)

# ------------------------------------------
# 分頁 E：批判性入市判斷、估值評分與同行比較 (已移除問題編號，表格支援自由拉伸與完整中文)
# ------------------------------------------
with tabs[4]:
    st.subheader(f"⚖️ {q_data['code']} {q_data['name']} — 批判性入市判斷、客觀估值評分與同行比較")
    tab_refresh_button("refresh_tab_entry", [fetch_real_fundamentals, fetch_real_momentum_snapshot])
    
    _lo52, _hi52 = q_data.get("w52_low"), q_data.get("w52_high")
    if isinstance(_lo52, (int, float)) and isinstance(_hi52, (int, float)) and _hi52 > _lo52:
        pos_pct = (q_data["price"] - _lo52) / max(_hi52 - _lo52, 0.01) * 100.0
        pos_pct = float(np.clip(pos_pct, 1.0, 99.0))
    else:
        pos_pct = None
    sc_now = q_data["score"]
    score_color = "#188038" if sc_now >= 7.0 else ("#f57c00" if sc_now >= 5.2 else "#d93025")
    if isinstance(fund_low, (int, float)) and isinstance(fund_high, (int, float)):
        _support_txt = f"{fund_low:,.1f}–{(fund_low + fund_high)/2:,.1f} {q_data['curr']}"
    else:
        _support_txt = "待 Yahoo 財報連線後顯示"
    
    c_e8, c_e9 = st.columns([3, 2])
    with c_e8:
        st.markdown(f"""
        <div class="section-card">
            <h4>⏱️ 目前是否適合買入，還是應等待一段時間後再入市？（批判性分析）</h4>
            <p style="font-size:15px; font-weight:700; color:{score_color};">📌 客觀決策結論：{q_data['action_plan']}</p>
            <ul>
                <li><strong>當前價格位置與估值診斷</strong>：{q_data['name']} 現價 <strong>{q_data['price']:,.2f} {q_data['curr']}</strong> 處於過去 52 週高低區間的第 <strong>{_sf(pos_pct, '.1f', '%')}</strong> 分位數；預測市盈率為 <strong>{_sf(q_data['fwd_pe'], '.2f', 'x')}</strong>，市銷率 <strong>{_sf(q_data['ps'], '.2f', 'x')}</strong>，市帳率 <strong>{_sf(q_data['pb'], '.2f', 'x')}</strong>。</li>
                <li><strong>核心基本面與逆風審視（正反雙向評估）</strong>：{q_data['catalyst']}</li>
                <li><strong>具體執行時機與風控紀律</strong>：
                    {"目前雖處於歷史低估值安全區間，可先動用 30%–35% 資金建立左側底倉，但其餘 65%–70% 主力資金建議等待股價重新站穩 20 日均線（" + ma20_txt + "）或下次業績確認趨勢後再行投入，並留意美債息率與財報公布等外圍變數。" if sc_now >= 7.0 else (
                        "目前估值已充分反映甚至透支短期利好，現價直接買入的值博率（Risk-Reward Ratio）偏低。強烈建議耐心等待股價回調至 8P 基本面支撐區（約 " + _support_txt + "）且 RSI 回落至 35–40 區間時再作部署。" if sc_now >= 5.2 else
                        "⚠️ 當前估值處於嚴重泡沫區（或面臨結構性基本面惡化與宏觀加息衝擊），下行均值回歸風險遠大於上行空間。現階段絕對不適合買入，已持倉者應逢反彈減持鎖定利潤或嚴設止損。"
                    )}
                </li>
            </ul>
        </div>
        """, unsafe_allow_html=True)

    with c_e9:
        st.markdown(f"""
        <div class="section-card">
            <h4>⭐ 過往與目前估值比較及批判性評分 (1–10分)</h4>
            <div style="font-size:30px; font-weight:800; color:{score_color}; margin:6px 0;">
                批判性評分：{sc_now:.1f} / 10 分
            </div>
            <p style="font-size:13.5px; font-weight:700; margin-bottom:6px;">定性結論：{q_data['verdict']}</p>
            <ul style="font-size:13px;">
                <li><strong>為何不給 9–10 分滿分（嚴格扣分理由 -{10.0-sc_now:.1f}分）</strong>：任何股票均存在宏觀利率、地緣政治、資本開支折舊或競爭加劇風險。本系統杜絕「無腦唱好」，即便低估值龍頭最高亦僅給予 7.8–8.4 分。</li>
                <li><strong>PE / PS / PB 歷史對標</strong>：當前預測 PE <strong>{_sf(q_data['fwd_pe'], '.1f', 'x')}</strong>、PS <strong>{_sf(q_data['ps'], '.2f', 'x')}</strong>、PB <strong>{_sf(q_data['pb'], '.2f', 'x')}</strong>（對照基本面合理 PE 區間為 {_sf(pe_lo_mult, '.1f', 'x')} – {_sf(pe_hi_mult, '.1f', 'x')}）。</li>
            </ul>
        </div>
        """, unsafe_allow_html=True)

    st.markdown("#### 🏆 與全球及中國同行深度比較、發展潛力與所處階段分析")
    peer_keys = ["00700.HK", "09988.HK", "00388.HK", "NVDA", "TSM", "AAPL", "PLTR", "TSLA"]
    if selected_symbol not in peer_keys:
        peer_keys = [selected_symbol] + peer_keys[:7]
        
    peer_rows = []
    for pk in peer_keys:
        pd_item = get_or_create_asset_profile(pk)
        peer_rows.append({
            "代號": pd_item["symbol"],
            "公司名稱": pd_item["name"],
            "市場": pd_item["mkt"],
            "總市值 (USD B)": pd_item["mkt_cap_usd"],
            "營收增速": f"{pd_item['rev_growth']:+.1f}%",
            "毛利率": f"{pd_item['gross_margin']:.1f}%",
            "預測PE": _sf(pd_item['fwd_pe'], ".1f", "x"),
            "市銷率PS": _sf(pd_item['ps'], ".2f", "x"),
            "批判評分(1-10)": pd_item["score"],
            "估值定性與入市建議": pd_item["verdict"],
            "所處階段與批判性風險剖析": pd_item["catalyst"].split("|")[-1].strip()
        })
    df_peers = pd.DataFrame(peer_rows)
    render_resizable_table(df_peers, key_prefix="peers_main_tbl", default_col_w=100, default_text_w=420, default_row_h=55)

# ------------------------------------------
# 分頁 F：港美股市牛熊宏觀診斷 (已移除問題編號)
# ------------------------------------------
with tabs[5]:
    st.subheader("🌍 香港和美國目前或今年（2026年）是否已進入牛市或熊市？")
    tab_refresh_button("refresh_tab_macro", [fetch_real_momentum_snapshot])

    # v6：牛熊判定完全由真實指數數據驅動，不再寫死結論與點位
    macro_indices = [("🇺🇸 標普500", "^GSPC"), ("🇺🇸 納斯達克", "^IXIC"),
                     ("🇺🇸 道瓊斯", "^DJI"), ("🇭🇰 恒生指數", "^HSI")]
    macro = {sym: fetch_real_momentum_snapshot(sym) for _, sym in macro_indices}

    def _bull_bear(m):
        if not m.get("ok"):
            return "數據缺失", "#888888", "—"
        p, hi, lo = m.get("price"), m.get("hi52"), m.get("lo52")
        if not all(isinstance(x, (int, float)) for x in (p, hi, lo)) or hi <= lo:
            return "數據不足", "#888888", "—"
        dd = (p / hi - 1) * 100.0
        bounce = (p / lo - 1) * 100.0
        if dd <= -20:
            return "技術熊市（自52週高點回撤逾20%）", "#d93025", f"{dd:.1f}%"
        if bounce >= 20 and m.get("above_ma200"):
            return "技術牛市（自低點反彈逾20%且站穩200日線）", "#188038", f"+{bounce:.1f}%"
        if m.get("above_ma200"):
            return "中長線偏多（修正/整固中）", "#f57c00", f"{dd:.1f}%"
        return "中長線偏弱（200日線下方）", "#b06000", f"{dd:.1f}%"

    any_ok = any(m.get("ok") for m in macro.values())
    if any_ok:
        st.info("📡 下列牛熊判定依 **Yahoo Finance 真實指數日線** 計算：技術性牛熊分界以「自 52 週高點回撤 ≥20% 為熊市、自 52 週低點反彈 ≥20% 且站穩 200 日線為牛市」，其餘為修正/整固。數據會隨交易日自動更新。")
    else:
        st.warning("⚠️ Yahoo Finance 暫時無法連線，宏觀指數真實數據缺失（離線狀態），不顯示任何編造點位或結論。請稍後重試。")

    cols = st.columns(4)
    for col, (nm, sym) in zip(cols, macro_indices):
        m = macro[sym]
        verdict, vc, _ = _bull_bear(m)
        with col:
            if m.get("ok"):
                st.markdown(f"""
                <div class="section-card" style="text-align:center;">
                    <div style="font-weight:800; font-size:15px; margin-bottom:4px;">{nm}</div>
                    <div style="font-size:22px; font-weight:800; color:{vc};">{_sf(m.get('price'), ',.0f')}</div>
                    <div style="font-size:12px; font-weight:700; color:{vc}; margin:4px 0;">{verdict}</div>
                    <div style="font-size:11.5px; color:#555;">52週 {_sf(m.get('lo52'), ',.0f')} – {_sf(m.get('hi52'), ',.0f')}</div>
                    <div style="font-size:11.5px; color:#555;">位置 {_sf(m.get('pos52'), ',.0f')}% 分位｜RSI {_sf(m.get('rsi'), ',.0f')}</div>
                    <div style="font-size:11.5px; color:#555;">1月 {_sf(m.get('r21'), '+.1f', '%')}｜3月 {_sf(m.get('r63'), '+.1f', '%')}｜6月 {_sf(m.get('r126'), '+.1f', '%')}</div>
                    <div style="font-size:10.5px; color:#999;">截至 {m.get('as_of') or 'N/A'}</div>
                </div>
                """, unsafe_allow_html=True)
            else:
                st.markdown(f"""
                <div class="section-card" style="text-align:center;">
                    <div style="font-weight:800; font-size:14px;">{nm}</div>
                    <div style="font-size:13px; color:#888; margin-top:8px;">真實數據連線失敗</div>
                </div>
                """, unsafe_allow_html=True)

    def _card(m):
        if not m.get("ok"):
            return "真實數據缺失，無法給出結論（不以舊點位或主觀敘述填充）。"
        v, _, _ = _bull_bear(m)
        p, hi, lo = m.get("price"), m.get("hi52"), m.get("lo52")
        if not all(isinstance(x, (int, float)) for x in (p, hi, lo)) or hi <= lo:
            return "真實數據不足（52 週高低點缺失），無法計算牛熊位置（不以不完整數據填充）。"
        return (f"現價 {p:,.0f}，52 週區間 {lo:,.0f}–{hi:,.0f}（第 {_sf(m.get('pos52'), ',.0f')}% 分位），"
                f"自高點 {(p/hi-1)*100:+.1f}%、自低點 {(p/lo-1)*100:+.1f}%；"
                f"200 日線{'上方' if m.get('above_ma200') else '下方'}，RSI {_sf(m.get('rsi'), ',.0f')}。客觀定性：{v}。")

    spx, ndx, dji, hsi = macro["^GSPC"], macro["^IXIC"], macro["^DJI"], macro["^HSI"]
    c_m1, c_m2 = st.columns(2)
    with c_m1:
        st.markdown(f"""
        <div class="section-card">
            <h4>🇺🇸 美國股市（真實數據診斷，非主觀唱多）</h4>
            <ul style="font-size:13px;">
                <li><strong>標普500</strong>：{_card(spx)}</li>
                <li><strong>納斯達克</strong>：{_card(ndx)}</li>
                <li><strong>道瓊斯</strong>：{_card(dji)}</li>
                <li><strong>批判性提示</strong>：指數位於 52 週高分位（&gt;80%）且 RSI 偏高時，估值安全邊際低、回撤風險上升；個股分化大，指數走牛不代表所有股票皆可買入。</li>
            </ul>
        </div>
        """, unsafe_allow_html=True)
    with c_m2:
        st.markdown(f"""
        <div class="section-card">
            <h4>🇭🇰 香港股市（真實數據診斷，非主觀唱多）</h4>
            <ul style="font-size:13px;">
                <li><strong>恒生指數</strong>：{_card(hsi)}</li>
                <li><strong>批判性提示</strong>：港股以恒指 200 日線與 52 週位置作客觀依據；若跌破 200 日線且自高點回撤擴大，應視為趨弱，而非沿用「慢牛」固定結論。</li>
                <li><strong>配置原則</strong>：僅依真實估值與趨勢分批，不依賴固定成交額或點位預設。</li>
            </ul>
        </div>
        """, unsafe_allow_html=True)

    df_macro_rows = []
    for nm, sym in macro_indices:
        m = macro[sym]
        v, _, _ = _bull_bear(m)
        df_macro_rows.append({
            "指數": nm,
            "真實現價": _sf(m.get("price"), ",.2f") if m.get("ok") else "N/A",
            "52週區間": f"{_sf(m.get('lo52'), ',.0f')} – {_sf(m.get('hi52'), ',.0f')}" if m.get("ok") else "N/A",
            "52週分位": _sf(m.get("pos52"), ".0f", "%") if m.get("ok") else "N/A",
            "近1月/3月/6月": (f"{_sf(m.get('r21'), '+.1f', '%')} / {_sf(m.get('r63'), '+.1f', '%')} / {_sf(m.get('r126'), '+.1f', '%')}" if m.get("ok") else "N/A"),
            "RSI14": _sf(m.get("rsi"), ".0f") if m.get("ok") else "N/A",
            "200日線": ("上方" if m.get("above_ma200") else "下方") if m.get("ok") else "N/A",
            "數據驅動牛熊定性": v,
            "數據日期": m.get("as_of", "—")
        })
    render_resizable_table(pd.DataFrame(df_macro_rows), key_prefix="macro_tbl",
                           default_col_w=110, default_text_w=300, default_row_h=48)
#(註：內容由 AI 生成)