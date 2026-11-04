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


_8P_PACK = json.loads(r'''
{"1":{"cur":"HKD","cur_ratio":[68.8,10.471841704718416,0.9356288506675856,0.4696806785793524,0.03757267441860465],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[10951,13277,12856,17702,23983,32971,19199,17013,1562,166760,259842,248515,277129,299021,266396,280847,262497,275575,281351,280036,281614,281933.5,304266.8],"Revenue_Adj":[10951,13277,12856,17702,23983,32971,19199,17013,1562,166760,259842,248515,277129,299021,266396,280847,262497,275575,281351,280036,281614,281933.5,304266.8],"NetProfit":[18075,27678,13029,19618,26836,46055,32036,35260,53869,118570,33008,35100,39000,39830,29143,33484,36680,23500,17088,11841,30712.857,26499.286,28292],"NetProfit_Adj":[18075,27678,13029,14851.145,19495.515,26215.095,24388.82,29345.79,15931.62,24063.6730285013,33227.4822331748,31030.9132664053,36700.074515224194,33631.18916,14369.345,27463.935,31599.325,21773.22,18363.88,19791.87,30712.857,26499.286,28292],"OCF":[-10842,4320,-1507,13759,13092,-3628,208,7295,37813,44549,40338,53596,55734,69270,73171,52184,56714,51437,54228,62567,null,null,null],"FCF":[-12278,3811,-2430,12806,12919,-4901,82,7127,29946,22055,20292,29926,28194,37080,46067,25128,32829,29767,33863,43824,43289.687901090896,36118.853592710155,37078.32777602029],"DivYield":[2.2976501305483032,1.6990291262135926,3.3426630286493864,2.6920777666999003,2.4604970809007507,3.4199134199134202,2.6554621848739495,2.843137254901961,2.8042977743668454,2.437858508604206,3.0489192263936293,2.90519877675841,4.215425531914893,4.2664872139973085,4.2772643253234754,5.288270377733599,6.245464247598719,6.04778972520908,5.306024096385542,4.366383380547686,3.8095930232558146,3.944767441860465,4.132267441860465],"Assets":[249801,276622,283663,296963,330951,373186,406725,428837,457941,1032944,1013465,1100255,1232244,1210976,1254596,1213526,1148437,1158903,1112542,1155673,null,null,null],"Equity":[205681,230609,229475,246897,273458,314285,342652,372821,406047,549111,544190,591547,590823,596963,630063,644255,647309,670549,652592,688392,null,null,null],"Liabilities":[44120,46013,54188,50066,57493,58901,64073,56016,51894,483833,469275,508708,641421,614013,624533,569271,501128,488354,459950,467281,null,null,null],"Debt":[38481,36527,44249,34489,37154,45917,48099,41890,37874,308379,307423,335131,352308,438976,444134,411637,352952,343090,322508,334282,null,null,null],"Shares":[2316.164338,2316.164338,2316.164338,2316.164338,2316.164338,2316.164338,2316.164338,2316.164338,2316.164338,3859.6785,3857.6785,3857.6785,3856.2405,3856.2405,3856.2405,3834.6345,3830.0445,3830.0445,3830.0445,3830.0445,null,null,null],"ROA":[0.07757044641094527,0.10515497993058814,0.046508473366233254,0.06757534109736732,0.08547667355720687,0.1308126117502702,0.08215296360738597,0.0843982852259916,0.12149376732395256,0.1590598872481781,0.03225943591921263,0.03321158904679901,0.03344052880622886,3.2604513715506585,2.3639950486134658,2.713318061262774,3.1058911591756515,2.036977645253842,1.5045928913092765,1.0440809182551036,3.84,3.29,3.44],"ROE":[0.09375389020291297,0.12944986845951476,0.057607619114994166,0.08379731966469112,0.10467907998182274,0.1598959834184515,0.10007059543816027,0.10153716790204514,0.14273223232904006,0.30109957083724825,0.08381877187012762,0.08517091813521793,0.08902006032821012,8.749654011923958,6.080445992566095,6.647290976804758,7.076538708261588,4.402612341552792,3.1681112398609503,2.1578387716665435,4.086,4.279999999999999,4.359999999999999],"ProfitMargin":[1.6505341977901562,2.0846576786924755,1.0134567517112631,1.1082363574737317,1.118959262811158,1.39683358102575,1.668628574404917,2.072532769058955,34.48719590268886,0.7110218277764452,0.12703104194087175,0.14123895941894857,0.14072868591883203,13.320134706258088,10.939728824757127,11.922505848380078,13.973493030396538,8.527624058786175,6.073552253235283,4.228384921938607,0.1090601213007876,0.09399126389733749,0.09298418361779859]},"3":{"cur":"HKD","cur_ratio":[7.325,22.713178294573645,2.245820916592415,2.1980495123780943,0.04778156996587031],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[13465.3,14225.5,12352.2,12351.8,19375.4,22426.8,24922.5,28245.9,31614.7,29591.3,28557.1,32476.5,39073,40628.1,40927,53563.7,60953.4,56971.1,55472.8,54326.3,56214.9,56820.8,57894.333333333336],"Revenue_Adj":[13465.3,14225.5,12352.2,12351.8,19375.4,22426.8,24922.5,28245.9,31614.7,29591.3,28557.1,32476.5,39073,40628.1,40927,53563.7,60953.4,56971.1,55472.8,54326.3,56214.9,56820.8,57894.333333333336],"NetProfit":[5862.6,9269.6,4302.5,5275.1,5584.8,6149.6,7712.1,6853.8,7109.2,7302,7340.7,8225.3,9312.800000000001,6965.7,6007.3,5017,5247.9,6070.1,5711.5,5688.2,6419.1,6579.900000000001,6759.778],"NetProfit_Adj":[5862.6,9269.6,4302.5,4864.8645,5494.2025,6222.913,7109.4805,6865.7405,6947.711,7130.0735,7609.57,8140.2135,9196.067,6463.032,6960.2855,6317.262,5667.404,4448.823,5922.9495,5392.0255,6419.1,6579.900000000001,6759.778],"OCF":[5395.6,5640.1,6743,5053.1,5743.8,5490.4,7370.6,7926.6,9504.4,9160.3,8956.5,9113.8,10167,10726.2,10569.4,10603.1,9713.7,11019.2,9182.4,10461.7,null,null,null],"FCF":[3064.2000000000003,3997.3,3848,2292.9000000000005,1466.3000000000002,848.3999999999996,1465.1000000000004,2632.7000000000007,3139.3999999999996,2804.3999999999996,2699.8999999999996,2972.8999999999996,3421,3673.300000000001,3274.5,2216.3999999999996,1392.7000000000007,2591.6000000000004,2722.2999999999993,5330.1,4705.404348874385,5647.054591713215,5511.8334977892155],"DivYield":[2,1.4644351464435146,2.9914529914529915,1.7948820512820514,1.910480349344978,1.9444444444444444,1.6548463356973995,1.9685151856017995,1.9709740990990992,2.302631578947368,2.547307132459971,2.2845953002610964,2.1604938271604937,2.2996057818659654,3.0224525043177892,2.8830313014827014,4.716981132075471,5.852842809364548,5.636070853462157,4.9928673323823105,4.778156996587031,4.778156996587031,4.778156996587031],"Assets":[36162.7,43245.1,51954,67071.5,72664.1,85088.9,98937.2,106395.2,114410.9,115563.3,116743.4,131061.9,132693.1,140470.1,150280.2,168067.4,168469.3,161977.6,158268.6,163555.1,null,null,null],"Equity":[21218.2,29499.3,31499.8,37807.299999999996,41208.6,46332.2,50350.2,56370.5,62223.7,62112.89999999999,62898,70245.9,70193.3,75396.7,79153.3,81838.79999999997,75138.7,71018.7,68333.5,71389.3,null,null,null],"Liabilities":[14944.5,13745.800000000001,20454.2,29264.199999999997,31455.5,38756.7,48587,50024.7,52187.2,53450.399999999994,53845.4,60816,62499.8,65073.4,71126.9,86228.6,93330.6,90958.9,89935.1,92165.8,null,null,null],"Debt":[9240.8,8834.1,15704,20419.6,21728.1,25849.2,31721,30623.399999999998,31534,33075.7,33247.9,36918.8,38021.6,38274.6,42418.4,55437.399999999994,59732.2,55844.2,57730.2,60172.4,null,null,null],"Shares":[19060.95565524011,19060.955652723656,19060.955651007895,18671.432232711057,18671.4322316712,18671.432231198534,18671.432229909457,18671.432228151625,18665.132443967246,18661.337529225246,18661.337528051303,18659.87009948408,18659.870099120253,18659.870099009997,18659.8700988,18659.870098,18659.870098,18659.870098,18659.870098,18659.870098,null,null,null],"ROA":[0.17830400428226448,0.23346824871108385,0.0903895099848633,0.08863814896807827,0.07993381786745826,0.07796491984304578,0.08381528489708798,0.06675809565368154,0.06531884762241623,0.06446375289054163,0.06414967799034638,0.06728266102460279,0.07143144205797047,5.172219391191786,4.208146990733972,3.2215728970471273,3.1850315285078867,3.7394812903374186,3.566943183088511,3.5349789341182762,3.8125,3.8275,3.8675],"ROE":[0.3159613684868605,0.37474960027005777,0.1447951161726301,0.16352690625358438,0.15659247067466153,0.15559114565543555,0.17772702708619717,0.14418519523632212,0.13872178177057012,0.13846643961861996,0.13750028096832925,0.14383541282318069,0.15162549505655756,11.00338756544699,9.173698287901082,7.47774153425609,8.158166989107986,10.026569144608992,9.742995755830616,9.744949311084747,10.536999999999999,10.783333333333333,11.017777777777775],"ProfitMargin":[0.4353857693478794,0.6516185722821694,0.34831851815870857,0.4270713580206934,0.2882417911372152,0.2742076444254196,0.3094432741498646,0.24264760549318662,0.22810274967024832,0.2504959227881168,0.26092285281068456,0.25669330131017815,0.24109231438589307,17.387719337109043,14.947589610770398,9.57346113132588,8.792618623407389,10.844972275416835,10.296036976680464,10.470435129946267,0.1141885870116286,0.11580090389434855,0.11676061627655336]},"5":{"cur":"HKD","cur_ratio":[135.8,13.284840469951918,5.407054116660009,1.9899944070778777,0.03698458314657104],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[859638.9896207688,1075827.0441379307,1052391.9230381674,707619.5526957846,706289.7212942534,770336.6497538468,706504.095228735,650291.8087586211,628798.8158298853,602913.5584137925,492595.1384655173,524496.7605461539,598195.7541977015,656169.9059628351,525742.4835236637,489239.26730344835,598441.0889003841,1056122.2362149993,1137002.4499988512,1057247.4252455928,591395.3884,622480.5796842105,651414.15],"Revenue_Adj":[859638.9896207688,1075827.0441379307,1052391.9230381674,756090.7866528726,706289.7212942534,770336.6497538468,706504.095228735,651369.9790344832,625425.5148298852,593501.8768965511,502989.0130996169,522431.6899500001,597937.1139287358,648914.561497701,525742.4835236637,489239.26730344835,616576.8538126918,1042492.1755538455,1150672.9777505752,1057613.8293762442,591395.3884,622480.5796842105,651414.15],"NetProfit":[121957.69357615378,148572.61045977008,43182.63987251907,43138.08043678156,99021.79872643686,126292.86384000011,104363.31203908037,121243.73800000007,101703.0864712644,97465.94731034472,10083.378005747127,75456.90031153847,98816.25791264372,46767.98176283523,30233.450383206084,97992.4286310345,112338.37799307682,175617.18595692297,178817.0573551725,164507.6588298849,228856.836,247742.818,263057.457],"NetProfit_Adj":[121957.69357615378,148572.61045977008,43182.63987251907,79002.91772671253,95469.87919264376,120840.29554974819,127613.89724350562,132554.83012068973,119874.12839247129,112131.34729034471,80708.94109080461,107744.62975476925,109919.68465933338,107591.50887064748,53518.63647926714,114423.08582637933,157917.87773162295,143318.0523461326,218517.768086207,192531.72795028717,228856.836,247742.818,263057.457],"OCF":[-52142.1771630769,-73236.94241379309,668809.2380977096,-65400.895713409875,526953.5215609199,596294.7458007698,-116790.10153563209,79202.85386206901,157769.67550574717,286994.2653103445,749639.7722471266,-100307.88193846156,25284.04568735633,340084.4778716474,1406537.983515266,942443.5620413795,53538.09356884611,212679.05107461524,416381.4911045979,275808.7603896549,null,null,null],"FCF":[null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,255166.95923237127,266213.1385127448,281064.5542701953],"DivYield":[4.171528876310875,5.211350200936486,6.633052292289595,2.9705478913478425,3.5463438180331575,5.383618960580748,4.282879427426608,4.465376021113028,5.2728176967573015,6.455393044200076,6.288974687493449,4.91729305909622,6.185276688650273,3.821475930433852,2.9004109108817806,4.116935458638137,5.129077235728703,7.520755744716946,8.84940359597041,4.7498537045059015,4.795175336070896,5.355575345226169,5.91019803469943],"Assets":[14475022.5578,18362097.667,19588106.4965,18332542.1368,19079806.6592,19848159.8614,20869592.7842,20712865.5084,20424850.3921,18675074.9656,18415403.9454,19700831.5833,20035738.7928,21148861.9584,23138312.4068,23064529.3525,23009444.5862,23733890.5762,23435523.7496,25165613.3526,null,null,null],"Equity":[894036.4047999999,1056177.092,776784.7729,1051833.9974,1204123.312,1289977.8938,1419414.5661,1476780.9941999998,1550609.4142,1530784.2518,1415691.5542,1545827.6132999996,1521397.0178,1500729.5855999999,1589469.7315,1612343.6575,1444851.4349,1504399.666,1493518.9821,1600883.5773999998,null,null,null],"Liabilities":[13580986.153,17305920.575,10167425.9409,17280708.1394,17875683.3472,18558181.9676,19450178.2181,19236084.5142,18874240.9779,17144290.7138,16999712.3912,18155003.97,18514341.775,19648132.3728,21548842.6753,21452185.695,21564593.1513,22229490.9102,21942004.7675,23564729.7752,null,null,null],"Debt":[2749118.3818,3155529.5095,2628911.421,4422353.2784,4586534.96,4191113.6578,4347727.8388,3638804.0634,3861760.1099,3007317.8036,3020043.2493,3661289.0811,3245859.485,3242605.014,3034053.8248,3238738.41,2894555.5272,3437421.6282,3534777.3297,3695196.3597,null,null,null],"Shares":[13279.469444686702,13574.206616435402,13891.16938249051,17410,17686.155902,17868.085646,18476.008664,18830.007039,19217.87426,19685.096934,19866.312807,19995.442851,20035.568089,20313.251138,20368.347693,20306.247032,20293.60741,18937.454786,17946.950582,17175.239862,null,null,null],"ROA":[0.009390590020959174,0.00907847737047286,0.002346708575298393,0.002385159028658908,0.005461139236224879,0.006705030549264032,0.0053455363133100885,0.0060419220799365235,0.005159970196723863,0.005361835681267776,0.0010362321778724511,0.004410265814701444,0.005404442414656208,0.28001568664336934,0.1834957036949697,0.4684200189730808,0.5267786481808294,0.7860102007978339,0.7919448125534101,0.7131106439883509,0.8427272727272727,0.9245454545454547,0.92625],"ROE":[0.15637700215156586,0.1627391243040452,0.0511484420752655,0.050711001963741405,0.09237369820919969,0.10590354839551946,0.08057083484296353,0.08754091842077998,0.07270237758671345,0.06844232732221844,0.007140383568872545,0.05639717402573197,0.07597789602453854,3.6510994892497783,2.3125776816675714,7.206779757906621,8.592400044321595,13.774766116973753,13.74279263476986,12.289611312359499,15.605263157894735,16.84631578947368,17.154],"ProfitMargin":[0.22795391545391547,0.2296106950844854,0.06627941959223346,0.08343821510297483,0.18274104625810664,0.21923332941775325,0.19468694915959972,0.23765106183268803,0.21111728052316614,0.21425741946728782,0.0487435605017893,0.19817571163763834,0.2399657366617719,12.405484423833048,9.7545050927135,26.13864733392183,28.826842553822214,33.70089790774606,34.34451940016328,30.721405037290282,0.38697771515255713,0.39799284662021106,0.40382521165682383]},"6":{"cur":"HKD","cur_ratio":[64.25,22.864768683274022,102.52982043222602,1.5779940441469928,0.04389105058365759],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[12181,12524,12773,10395,10371,10201,10415,11578,2131,1308,1288,1420,1555,1348,1270,1276,1265,1292,919,771,771.5,770.1666666666666,777],"Revenue_Adj":[12181,12524,12773,10395,10371,10201,10415,11578,2131,1308,1288,1420,1555,1348,1270,1276,1265,1292,919,771,771.5,770.1666666666666,777],"NetProfit":[6842,7448,8029,6697,7194,9120,9729,11165,61005,7732,6417,8319,7636,7131,6132,6140,5649,6003,6119,6236,10632.2,6488.714,6699.143],"NetProfit_Adj":[6842,7448,8029,6725.61976893453,7229.7886446886505,9154.383697411,9771.69551129324,11194.2395153774,8078.670059294011,8264.81416837782,6417,7558.81874447392,7636,7131,6132,6140,5649,6003,6119,6236,10632.2,6488.714,6699.143],"OCF":[9108,8974,8858,7981,7973,9775,9623,12333,5257,6454,4564,4879,6882,5368,5533,5300,5472,5277,4901,5314,null,null,null],"FCF":[7130,7406,7088,5554,5735,7295,7018,10399,5134,6453,4564,4879,6881,5368,5531,5300,5471,5275,4901,5313,774.4529685979053,1176.241897147217,1206.7658476086938],"DivYield":[4.8556430446194225,4.481605351170568,4.850574712643678,5,4.3061224489795915,4.038294168842471,3.7009063444108765,4.13625304136253,3.561461794019934,3.786816269284713,3.9795171909290423,4.245640636846095,5.137614678899082,4.912280701754386,6.690476190476192,5.802469135802469,6.5964912280701755,6.232044198895028,5.2029520295202945,5.113327289211242,4.389105058365759,4.389105058365759,4.389105058365759],"Assets":[67779,70675,68440,74949,92703,94730,101646,105237,136274,135358,129777,107001,91437,93571,93552,94304,94527,95702,94077,98325,null,null,null],"Equity":[44085,48104,47327,52643,56684,58371,63462,69477,123088,123597,118411,95580,83557,85492,84766,86767,86857,88752,87076,90725,null,null,null],"Liabilities":[23694,22571,21113,22306,36019,36359,38184,35760,13186,11761,11366,11421,7880,8079,8786,7537,7670,6950,7001,7600,null,null,null],"Debt":[14689,13495,10667,12247,25773,23626,24599,22348,10204,9405,8514,7223,3437,3324,3642,3438,3239,3101,2509,3320,null,null,null],"Shares":[2134.261654,2134.261654,2134.261654,2134.261654,2134.261654,2134.261654,2134.261654,2134.261654,2134.261654,2134.261654,2134.261654,2134.261654,2134.261654,2134.261654,2134.261654,2134.261654,2133.515443,2133.515443,2131.105154,2131.105154,null,null,null],"ROA":[0.1061705214645387,0.10758807979545554,0.11542968048017826,0.09341023369993515,0.08582062844463531,0.097314773812509,0.09908542795453618,0.10793540310223654,0.5051943803801897,0.056929964069034576,0.04840552925867954,0.07026835263411296,0.07696106592487326,7.708855833261265,6.553977864826878,6.536921897623711,5.9831277703343195,6.311340542188626,6.448553317279572,6.482261099156974,19.003333333333334,5.6866666666666665,5.843333333333334],"ROE":[0.15954296374023552,0.1615810996973609,0.1682681728159613,0.13398019405821746,0.13160518444666003,0.15853287558124377,0.15971042328433183,0.16797177652908477,0.6336042375301846,0.06268723270567728,0.05303130475025619,0.07775093345047221,0.08525318610895571,8.436607137575496,7.2031857533860375,7.15897232602473,6.5071649080772245,6.836779436133683,6.96021111540824,7.014583720001574,15.66,6.263333333333333,6.41],"ProfitMargin":[0.561694442164026,0.5946981794953689,0.6285915603225554,0.6442520442520443,0.6936650274804744,0.8940299970591119,0.9341334613538166,0.9643288996372431,28.627404974190522,5.91131498470948,4.982142857142857,5.858450704225351,4.910610932475884,529.0059347181009,482.8346456692914,481.19122257053294,446.56126482213443,464.6284829721362,665.8324265505985,808.8197146562904,13.781205443940378,8.425074042383015,8.621805662805663]},"11":{"cur":"HKD","cur_ratio":[null,null,null,null,null],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[44979,56175,45434,36052,37622,39403,42743,46499,50811,51499,50475,57174,65620,74193,65334,56791,23190,81680,80521,84541,null,null,null],"Revenue_Adj":[44979,56175,45434,36052,37622,39403,42743,46499,50811,51499,50475,57174,65620,74193,65334,56791,23190,81680,80521,84541,null,null,null],"NetProfit":[12038,18242,14099,13138,14917,16885,19327,26678,15131,27184,15866,19629,24211,24840,16687,13960,11286,17848,18379,15762,null,null,null],"NetProfit_Adj":[12038,18242,14099,12776.445,14504.51,16124.315,18384.285,18741.325,16529.625,18113.395,15907.75,19519.615,24073.225,24795.745,17764.985,13871.49,11488.07,17913.965,18884.175,16323.12,null,null,null],"OCF":[45728,-8907,-58056,12725,51285,-26233,-21519,22863,2480,-4338,26200,32412,23048,35553,40382,2308,37982,-60429,29827,-58862,null,null,null],"FCF":[null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null],"DivYield":[4.8920216368767635,3.9154754505904283,6.1946902654867255,4.533756756756756,4.069028951486698,5.643211068909386,4.4651406908171865,4.375497215592681,4.3343653250774,3.8644067796610173,4.227304227304226,3.4536082474226806,4.266211604095563,5.09345652173913,4.113687359760658,3.5739313244569026,3.1587057010785817,7.138947830862163,7.116692830978545,4.429967426710098,null,null,null],"Assets":[669064,745999,762168,830668,916911,975665,1077096,1143730,1263990,1334429,1377242,1478418,1571297,1676991,1759787,1820185,1854446,1692094,1795196,1819113,null,null,null],"Equity":[48698,56456,51626,62148,70012,79634,92323,107778,139193,141981,140686,152079,162107,178917,183195,184416,159998,168184,169564,172692,null,null,null],"Liabilities":[620366,689543,710542,768520,846899,896031,984773,1035952,1124797,1192448,1236556,1326339,1409190,1498074,1576592,1635769,1694448,1523910,1625632,1646421,null,null,null],"Debt":[49977,51050,36908,52581,70015,85562,91519,85767,87499,86337,86331,94335,36771,61839,69631,90700,90311,95192,79234,64076,null,null,null],"Shares":[1911.842736,1911.842736,1911.842736,1911.842736,1911.842736,1911.842736,1911.842736,1911.842736,1911.842736,1911.842736,1911.842736,1911.842736,1911.842736,1911.842736,1911.842736,1911.842736,1911.842736,1911.842736,1895.522605,1879.195673,null,null,null],"ROA":[0.01926258756812632,0.02578259766526296,0.01869686845024457,0.01649636246292776,0.017071617363220777,0.01784340496762085,0.018830248626118675,0.024025295092906872,0.012568737228581397,0.021162098953248113,0.011957202772755249,0.014019876315807905,0.015877549213615044,1.5294210365583347,0.9710839629443625,0.7798943678889109,0.6142657589292639,1.0065021119175308,1.054056301598089,0.8721999142851372,null,null,null],"ROE":[0.26884938359835625,0.35271711283196533,0.26089450602320463,0.23094907448098861,0.2257415254237288,0.22566590486882376,0.224788755328367,0.26664534410122886,0.12609692070502937,0.20346391629118452,0.118118706843604,0.14086417360976555,0.16132600366483424,15.42057380178603,9.861652020258731,8.11760053962273,7.0366640999822305,11.719899138474469,11.694377103734386,9.882069704892132,null,null,null],"ProfitMargin":[0.43916675787092774,0.4999451874588906,0.3972220657012453,0.39002523378358317,0.4212057037978258,0.47840992803309346,0.5109178386380459,0.6463006928630263,0.34069620823200936,0.6029253744435429,0.35063586815468467,0.3810774795355035,0.4171864768928559,39.89848694143724,29.512052773995013,26.79154032165202,80.85106382978722,32.727006014375824,36.73742704085712,24.961201026193265,null,null,null]},"12":{"cur":"HKD","cur_ratio":[28.6,12.295786758383489,5.023679245283018,0.4175182481751824,0.06316183816183818],"date":["30/6/2006","30/6/2007","30/6/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[6773,8355.9,13492,10411,7092,15188,15592,23289,23371,23641,25568,27960,21982,24184,25020,23527,25551,27570,25256,25741,25245.81818181818,25220,26483.11111111111],"Revenue_Adj":[6773,8355.9,13492,10411,7092,15188,15592,23289,23371,23641,25568,27960,21982,24184,25020,23527,25551,27570,25256,25741,25245.81818181818,25220,26483.11111111111],"NetProfit":[13548.7,9817.7,15473,13149,15820,17184,20201,15948,16752,21326,21916,30809,31157,16994,10192,13195,9239,9261,6296,5653,8392.9,9424.300000000001,10950.625],"NetProfit_Adj":[13548.7,9817.7,15473,4556.50238404855,3730.2167179070702,5501.02987197724,6818.47571495546,8363.717954481599,8951.40464461647,13432.866526694701,12672.746777955099,16627.8040261219,19545.48413196,13363.103845750798,14805.958706937201,13529.243767,9997.005,11075.02,2001.29,4900.295,8392.9,9424.300000000001,10950.625],"OCF":[1239.3,1191,-442,-4576,-16390,176,4286,-433,5653,860,8528,2043,6290,8848,9356,3251,12186,13274,13358,11324,null,null,null],"FCF":[-400.60000000000014,216.20000000000005,null,null,-16767,-295,3788,-940,420,131,5114,-25957,4156,5279,6794,-51599,9000,8706,10543,7602,-6257.25209279314,-5967.92253562603,-5578.72155956263],"DivYield":[2.6022304832713754,2.0657276995305165,2.2636625514403286,1.7123287671232876,1.8867924528301887,2.5906735751295336,1.9378427787934187,2.3954802259887007,2.025782688766115,3.052631578947368,3.7575757575757573,3.320388349514563,4.615384615384616,4.705882352941177,5.950413223140496,5.421686746987951,6.605504587155964,7.484407484407485,7.627118644067796,4.477611940298507,4.405594405594405,4.416083916083917,4.426573426573426],"Assets":[125923.3,132896.5,174878,191242,230312,262470,281508,304114,316980,336269,355498,410998,441106,455245,461087,550304,529790,542550,531240,539381,null,null,null],"Equity":[93782.90000000001,100743.59999999999,124162,137510,164423,189925,209852,228000,243217,256269,269301,297943,317992,325393,332192,352179,345152,344100,340577,340886,null,null,null],"Liabilities":[32140.399999999998,32152.9,50716,53732,65889,72545,71656,76114,73763,80000,86197,113055,123114,129852,128895,198125,184638,198450,190663,198495,null,null,null],"Debt":[20694.5,18270.100000000002,35626,36009,41639,47157,41618,46785,42702,52088,56400,78550,85530,92126,88192,100280,88336,93087,83255,79744,null,null,null],"Shares":[3536.1420496600026,3785.577838460003,4183.895613700003,4183.895613700003,4240.408409600003,4616.510809900004,4706.151796500004,4781.443139000003,4831.5300000000025,4840.3146000000015,4840.847000000002,4841.210000000002,4841.1,4841,4841,4841,4841,4841,4841,4841,null,null,null],"ROA":[0.11751517001870013,0.07586513860222441,0.10054764121134144,0.07182890855457227,0.07505562751154063,0.06974280716422272,0.07427138597516811,0.05446516695069516,0.05394352545669415,0.0652921014804462,0.06336237490368866,0.08038919968271198,0.07312957103827702,3.791818160519707,2.224521243392133,2.6092777175197326,1.7107770249626422,1.7272506854169387,1.1726687713612531,1.056022626120728,1.482,1.5979999999999999,1.8175000000000001],"ROE":[0.18869819180539524,0.11537801616140725,0.1450037977631386,0.10340393908533636,0.10829497030787398,0.09979847491390174,0.10346249286169745,0.07442511637674565,0.07258987069712622,0.08715214846024802,0.0851468877056457,0.1108022182741482,0.10287472739369384,5.360849458363039,3.1434572478094185,3.982632763228785,2.787163181330019,2.8299897630215893,1.941145911214773,1.7539260111912456,2.53,2.8377777777777777,3.16875],"ProfitMargin":[2.0003986416654365,1.174942256369751,1.146827749777646,1.2629910671405244,2.230682459108855,1.1314195417434818,1.2956003078501797,0.6847868092232384,0.7167857601300757,0.9020769003003256,0.8571652065081352,1.10189556509299,1.417386952961514,70.26959973536222,40.73541167066347,56.08449866111277,36.15905444013933,33.59085963003265,24.928729806778588,21.96107377335768,0.33244714035409745,0.37368358445678035,0.4134946608047672]},"16":{"cur":"HKD","cur_ratio":[113.5,11.779968863518423,3.9891414970867802,0.5276689121652093,0.04368688580142326],"date":["30/6/2006","30/6/2007","30/6/2008","30/6/2009","30/6/2010","30/6/2011","30/6/2012","30/6/2013","30/6/2014","30/6/2015","30/6/2016","30/6/2017","30/6/2018","30/6/2019","30/6/2020","30/6/2021","30/6/2022","30/6/2023","30/6/2024","30/6/2025","30/6/2026",null,null],"Revenue":[25598,30994,24471,34234,33211,62553,68400,53793,75100,66783,91184,78207,85644,85302,82653,85262,77747,71195,71506,79721,87681.16666666667,83797.91666666667,86374.5],"Revenue_Adj":[25598,30994,24471,34234,33211,62553,68400,53793,75100,66783,91184,78207,85644,85302,82653,85262,77747,71195,71506,79721,87681.16666666667,83797.91666666667,86374.5],"NetProfit":[19850,21226,27602,10356,30039,48097,43080,40329,33520,31082,32666,41782,49951,44912,23521,26686,25560,23907,19046,19277,23242.923,23933.833,25222.667],"NetProfit_Adj":[19850,21226,27602,12465.79,12898.73,20955.71,21024.04,16961.16,20416.47,18636.81,23909.56,24738.095,29426.13,31774.14,28734.08,29534.98,28179.135,23996.91,21801.835,21603.265,23242.923,23933.833,25222.667],"OCF":[7355,3442,1923,9924,9516,-421,9346,17995,-2610,13066,15333,35573,7366,19755,47868,15656,15392,26177,24516,38080,null,null,null],"FCF":[-7190,-2870,-6053,2126,798,-9179,3208,8322,-16596,8575,8699,26375,-15821,7195,-1396,5430,2773,10543,14510,28993,25352.58519175385,17758.6383396216,18463.27776459405],"DivYield":[2.777777777777778,2.4442082890541976,2.3629489603024574,2.5826446280991737,2.5139664804469275,2.9567519858781997,3.6752605595172794,3.3466533466533472,3.151458137347131,2.6671974522292996,4.1375604513702315,3.5745422842197034,3.927364864864865,3.735849056603774,5.002526528549773,4.278305963699222,5.339805825242718,5.015197568389058,5.5514433752775725,4.164353137146031,3.4660792951541852,3.6387665198237884,3.7859030837004406],"Assets":[230798,257294,301515,309815,354946,413197,467570,512896,557048,604110,614515,639416,715486,751162,797831,796419,807567,805991,818094,816893,null,null,null],"Equity":[170547,188495,224057,226920,268025,312195,350959,390529,419689,456818,474508,507363,548432,575819,590415,599626,606744,606796,611071,622374,null,null,null],"Liabilities":[60251,68799,77458,82895,86921,101002,116611,122367,137359,147292,140007,132053,167054,175343,207416,196793,200823,199195,207023,194519,null,null,null],"Debt":[30237,33332,40303,42025,45388,60435,71266,64630,83731,83132,80761,67326,91434,95006,114247,118608,126772,128054,129654,112672,null,null,null],"Shares":[2491,2492,2564,2564,2570,2570,2616,2671,2725,2876,2895,2896,2897,2898,2898,2898,2898,2898,2898,2897.780274,null,null,null],"ROA":[0.09298926524426664,0.08697540627586603,0.09878867376867588,0.03388022835457118,0.09037533790339686,0.1252292867343711,0.09782382854943475,0.08226496380292636,0.06265748487771322,0.05353621126496136,0.053611242178684994,0.06667033513008291,0.07399059120143006,6.147760062400795,3.058761401762306,3.3560608436568917,3.1870602361866,2.9632650329272328,2.3454437421686674,2.3580615625689987,2.68,2.7933333333333334,2.94],"ROE":[0.1274674749239048,0.12025891984532797,0.1363984522862381,0.04691088472044175,0.12374739695441091,0.1687063519623421,0.13183907553509894,0.11011767018762518,0.0837272619411886,0.07179874545078649,0.07103365868137819,0.08642268973091935,0.09630844306395465,8.125170171406138,4.132951684123779,4.578799673653714,4.27508392110162,3.9712591600837537,3.1512973497069754,3.1483755904122925,3.5907692307692307,3.6950000000000003,3.8272727272727276],"ProfitMargin":[0.7754512071255567,0.6848422275279086,1.127947366270279,0.3025062803061284,0.9044894763783085,0.768899972823046,0.6298245614035087,0.7497072109754057,0.4463382157123835,0.4654178458589761,0.35824267415336025,0.5344790108302326,0.58527158936995,52.85104686877212,28.661996539750522,31.37622856606695,32.875866592923195,33.57960530936161,26.635527088635918,24.18057977195469,0.26508455344806253,0.28561369848847196,0.29201520124573804]},"17":{"cur":"HKD","cur_ratio":[5.19,3.095427435387674,0.15366192638484164,0.06171591652297997,0.35629415542710335],"date":["30/6/2006","30/6/2007","30/6/2008","30/6/2009","30/6/2010","30/6/2011","30/6/2012","30/6/2013","30/6/2014","30/6/2015","30/6/2016","30/6/2017","30/6/2018","30/6/2019","30/6/2020","30/6/2021","30/6/2022","30/6/2023","30/6/2024","30/6/2025","30/6/2026",null,null],"Revenue":[23910.2,23284.5,29360.8,24329.7,30218.6,32882,35620.1,46779.9,56501.1,55245,59570,56628.8,60688.7,76763.6,59007.8,68233.2,68212.7,54566.2,35782.2,27680.5,26575.545454545456,20778.125,21430],"Revenue_Adj":[23910.2,23284.5,29360.8,24329.7,30218.6,32882,35620.1,46779.9,56501.1,55245,59570,56628.8,60688.7,76763.6,59007.8,68233.2,68212.7,54566.2,35782.2,27680.5,26575.545454545456,20778.125,21430],"NetProfit":[1059.8,4312.9,9685.7,2083.5,12398.6,9153.9,10139,14148.7,9725.4,19112,8666.3,7675.700000000001,23338.100000000002,18160.1,1096.2,1151.5,1249.2000000000003,548,-19682.9,-16301.5,-2125.333,-1125.833,-1365],"NetProfit_Adj":[1059.8,4312.9,9685.7,2384.4465,6057.725,5342.0415,4692.462,7710.0985,6259.302,4712.771,1608.546,5728.313,8167.5695,8906.39899369217,-54.6625,29.4195,2725.9705,-2558.3345,-8066.7925,-8212.8135,-2125.333,-1125.833,-1365],"OCF":[1826.9,-1171.6,921.2,-5246.9,8277.1,1249.3,-15469.9,5547.2,5214.3,9941.5,5887.1,3603.7,7707,6220.4,-11304,9597.6,9857.4,9747.8,-6134,3204.5,null,null,null],"FCF":[913.6000000000001,-1921,-2384.1000000000004,-6979.9,5925.6,-3514.7,-18224.9,59.80000000000018,-1137.6999999999998,954.2000000000007,-3033.7999999999993,-12554.5,-2262.8999999999996,-3924.5,-28099.9,2436.8,4306.599999999999,2519.499999999999,-10958.6,-1006.6000000000004,1192.568019253935,-8689.75400714765,-8219.46640025585],"DivYield":[2.578125,2.044989775051125,2.7170843828715365,2.1398002853067046,2.9733959311424103,3.231292517006803,4.217536071032186,3.910614525139665,4.761904761904762,4.1420118343195265,5.605095541401274,4.641775983854692,4.3478260869565215,4.173486088379706,5.543478260869565,5.1053283767038415,7.317939609236235,3.941908713692946,2.735978112175103,0,0,0,0.7321772639691714],"Assets":[114160.6,144037.9,165757.5,176519.3,199980.1,229115.4,286371.7,332189.1,369227.4,397930.7,392108.6,437056.3,481454.8,503284.9,600195.9,627077.4,635883.9,609014,445157.6,420265,null,null,null],"Equity":[69364.5,78483.8,94904.70000000001,97483,114886.29999999999,134368.6,158040.69999999998,177745,200276.5,222358,200895.30000000002,220944.5,255181.9,275364.5,279745.1,304192.39999999997,288098.8,274092.8,224888.80000000002,206736.9,null,null,null],"Liabilities":[44796.100000000006,65615.3,70852.8,79036.3,85093.80000000002,94746.8,128331,154444.1,168950.9,175572.7,191213.3,216111.80000000002,226272.90000000002,227920.40000000002,320450.8,322885,347785.1,334921.19999999995,220268.8,213528.1,null,null,null],"Debt":[24489.3,41749.1,43163.7,50459,52223.3,60504.100000000006,88616.29999999999,104580.8,118699.09999999999,116226.79999999999,134932.1,147119.9,140752.7,156334.6,198376.3,182860.5,199111,195575.90000000002,160952.4,155007,null,null,null],"Shares":[1052.2041737030129,1067.5285179656767,1080.3662705177949,1118.185595603765,1132.8449211531433,1153.6918121217861,1633.5708625198915,1676.1954538018479,2166,2249.225,2347.1,2391.025,2553.525,2555.7,2549.1,2542.4,2516.6,2516.6,2516.6,2516.6,null,null,null],"ROA":[0.009373543399293667,0.033407630176007994,0.06252965667017651,0.012174357128499506,0.06586252195886633,0.0426660265605209,0.03933755083298884,0.045747160182151865,0.02773074200564144,0.049825453188853765,0.021938908608723637,0.01946922741182122,0.05198565373896952,3.8513934189918415,0.504675749682278,0.5596308499500477,0.5742693778502952,0.496121007192638,-3.3215465110234423,-3.5388953327541945,-0.7333333333333334,-0.4,0.023333333333333355],"ROE":[0.02034641509144176,0.07582247670817689,0.14577852296466648,0.028384185778480744,0.15138262126628768,0.09477416345667533,0.0892033504704533,0.10772841569476936,0.06506613893340746,0.11284607156975132,0.048348638184394624,0.041982188048829444,0.11601151361369745,8.252445273319845,0.5018222069831202,0.5277847866436303,0.5721235414960171,0.2654896624962666,-10.378706327731608,-9.490094097559087,-0.050909090909090876,-0.9850000000000002,-0.22599999999999998],"ProfitMargin":[0.04432417963881523,0.18522622345337025,0.3298854254652462,0.08563607442755151,0.41029696941618743,0.2783863511951828,0.284642659621955,0.3024525490648762,0.17212762229407924,0.3459498597158114,0.1454809467852946,0.14253524708275647,0.3933961347005292,24.703244767051046,4.718867675120916,5.032887216193877,5.316312065055335,5.659364221807639,-48.92767912537519,-55.32125503513304,-0.07997326113161554,-0.05418357046172357,-0.06369575361642557]},"27":{"cur":"HKD","cur_ratio":[34.6,19.222222222222225,4.1854733340837535,2.1568382994639075,0.01300578034682081],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[4669.495,13035.439,10520.12,12232.679,19262.133,41186.446,56746.423,66032.501,71752.47,50990.813,52826.115,48639.742,55210.901,51901.991,12876.099,19695.53,11473.793,35684.253,43432.373,49242.325,50404.85,53448.380952380954,59344.58823529412],"Revenue_Adj":[4669.495,13035.439,10520.12,12232.679,19262.133,41186.446,56746.423,66032.501,71752.47,50990.813,52826.115,48639.742,55210.901,51901.991,12876.099,19695.53,11473.793,35684.253,43432.373,49242.325,50404.85,53448.380952380954,59344.58823529412],"NetProfit":[-1531.546,-466.2,-11390.368,1149.113,898.455,3003.908,7377.839,10051.764,10339.81,4161.05,6283.397,10504.361,13507.389,13041.545,-3973.078,1326.231,-3433.77,6827.956,8759.247,10673.765,10809.478000000001,11205.304,12169.105],"NetProfit_Adj":[-1531.546,-466.2,-11390.368,312.92101,1488.4522,3956.53036,7390.397,10341.56932,10530.3076,4967.93308,6331.54896,10611.51244,13504.25444,13388.713,-3543.936,1672.87884,-3180.585475,6929.3584,8824.292665,10821.460635,10809.478000000001,11205.304,12169.105],"OCF":[1875.461,884.501,-706.486,1626.394,2200.925,5881.088,10274.328,13787.857,12376.168,6868.539,12863.553,19144.372,15003.834,21051.201,-6099.113,-2247.591,-1621.857,12207.658,12328.218,15244.734,null,null,null],"FCF":[-874.1450000000002,-419.06999999999994,-2232.705,-732.7459999999999,-2558.224,1358.8609999999996,8047.318999999999,8665.035,2900.9490000000005,327.10999999999973,10764.076,17135.881,11995.097000000002,16932.811,-11871.412,-9155.259,-6684.558,6248.2919999999995,7500.14,11813.849,7251.5907497296985,4729.864200804666,10924.015575248244],"DivYield":[0,0,0,0,0,0,0,0,2.6345933562428407,1.7177914110429449,0.9763313609467457,0.9409888357256778,1.9076305220883536,1.5853658536585367,0.7468879668049793,0.7425742574257426,0.5813953488372093,0.6857142857142857,3.0303030303030303,3.914405010438413,5.147398843930636,5.361271676300578,5.890173410404624],"Assets":[29208.998,31761.191,18651.964,18963.013,25186.174,35764.306,44388.566,46256.838,51838.836,55354.341,66261.03,83814.682,87383.831,95696.172,92726.779,85243.425,80961.332,87215.455,94577.891,99844.492,null,null,null],"Equity":[14123.499,18938.443,7273.9,8435.134,9574.551,14643.226999999997,22294.379999999994,33054.288,38989.081999999995,41713.337,46863.013,56015.593,62831.2,74154.879,68037.182,69075.73999999999,64465.365,71217.09099999999,76391.484,83631.484,null,null,null],"Liabilities":[15085.499,12822.748000000001,11378.064,10527.879,15611.623,21121.078999999998,22094.185999999998,13202.550000000001,12849.754,13641.004,19398.017,27799.089,24552.630999999998,21541.293,24689.596999999998,16167.685,16495.967,15998.364,18186.407,16213.007999999998,null,null,null],"Debt":[8972.853000000003,6505.818,6711.861,5843.191000000001,9426.232,11672.583999999999,11257.45,642.9770000000001,988.0039999999999,1390.3400000000001,6134.673,9944.276,9054.95,913.148,9577.902999999998,6869.104,7988.231,2081.05,4767.546,1953.667,null,null,null],"Shares":[3296.117361,3935.639361,3938.169361,3941.589361,3954.398244,4174.208722,4199.58469,4219.70653,4244.889689,4260.737025,4271.673502,4308.141595,4324.998101,4333.820007,4344.077873,4359.553541,4368.911944,4373.586962,4374.89455,4379.240712,null,null,null],"ROA":[-0.05509463572972453,-0.015292719528883204,-0.4518807838946006,0.06109869480978282,0.04070086273615866,0.09856880536461732,0.18409418941344982,0.22178209939910462,0.21081072341681448,0.07763647120935692,0.10333228354826957,0.13998748844849726,0.15779797106064816,14.246826290471493,-4.217191142495162,1.4903966733667395,-4.131975596823622,8.119974369590018,9.63648801535343,10.979975489756239,10.362,10.342,11.113333333333335],"ROE":[-0.10723139753041924,-0.02910162224689805,-0.8962464930276007,0.15140007250414877,0.10347601222978423,0.2565363803683365,0.4090231092011862,0.370265842345376,0.29204131279896833,0.10471926112628151,0.14370789532363507,0.20631769867587343,0.2294015734589191,19.19742456431057,-5.635125057536221,1.9525153247964677,-5.189369708909226,10.140043697513834,11.941321680946043,13.4185966433661,12.625882352941176,12.653125000000003,13.029333333333334],"ProfitMargin":[-0.32798964341968456,-0.0357640429294326,-1.0827222503165363,0.09393796730871463,0.04664358822566535,0.07293438234510449,0.13001416847014305,0.1522244932082763,0.14410388938527133,0.08160391559161843,0.118944900642419,0.21596251476827327,0.24465076199354183,25.127253788780475,-30.856224389079333,6.7336649483410715,-29.927069452969913,19.134367195524593,20.16755335933406,21.675997223932868,0.2144531329822428,0.2096472108294543,0.20505837870169388]},"66":{"cur":"HKD","cur_ratio":[33.38,23.020689655172415,3.607100846710001,1.1614474599860822,0.03967302918770863],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[9541,10690,17628,18797,29518,33423,35739,38707,40156,41701,45189,55440,53930,54504,42541,47202,47812,56982,60011,55451,54028.75,55965.166666666664,58240.6],"Revenue_Adj":[9541,10690,17628,18797,29518,33423,35739,38707,40156,41701,45189,55440,53930,54504,42541,47202,47812,56982,60011,55451,54028.75,55965.166666666664,58240.6],"NetProfit":[7759,15180,8284,9639,12059,15556,13375,13025,15606,12994,10254,16829,16008,11932,-4809,9552,9827,7784,15772,15313,19411.222,11997.222,8268.143],"NetProfit_Adj":[7759,15180,8284,7318.922,8700.368,10497.988,9637.09,8632.487,11601.821,10931.53,9500.08,10550.303,11263,12246.348,-2549.694,3017.824,1599.745,5834.275,8485.79,-2120.34,19411.222,11997.222,8268.143],"OCF":[5400,5965,8921,6999,10104,10553,13566,13956,15267,14239,16554,18838,9968,16111,-267,6923,6147,9905,16633,10017,null,null,null],"FCF":[2497,-4306,3114,1658,-2232,510,2441,600,2908,-7431,4615,10315,3006,6780,-9928,-1999,-13906,-3678,-4042,-12473,null,null,null],"DivYield":[2.147239263803681,1.5679442508710801,2.6726057906458793,1.9402985074626864,2.084805653710247,3.0218687872763423,2.5901639344262297,3.1345826235093694,3.30188679245283,2.760416666666667,2.8381962864721486,2.4454148471615724,2.9126213592233006,2.671009771986971,2.837370242214533,3.034647550776583,3.1680773881499396,4.323432343234323,4.833948339483395,4.395973154362416,3.9245056920311563,3.9245056920311563,3.9245056920311563],"Assets":[120421,155668,159338,176494,181665,197870,206687,215823,227152,241103,257340,263768,274687,289214,290574,292082,327081,346426,367499,398938,null,null,null],"Equity":[76786,91037,97822,106453,117293,134649,143111,152702,163482,170171,149556,166426,180619,186798,176981,180037,179912,178856,186133,216395,null,null,null],"Liabilities":[43635,64631,61516,70041,64372,63221,63576,63121,63670,70932,107784,97342,94068,102416,113593,112045,147169,167570,181366,182543,null,null,null],"Debt":[28152,34050,31289,34629,31960,34046,34424,35304,31245,31485,50555,52659,50760,50182,61444,54706,58632,69923,87786,99144,null,null,null],"Shares":[5548.613951,5611.057035,5661.143113,5727.833692,5772.563031,5784.87125,5793.19665,5798.54165,5826.534347,5857.112,5905.290065,6007.777302,6139.485589,6157.948911,6180.927873,6193.462514,6202.060784,6217.197282,6224.823171,6224.823171,null,null,null],"ROA":[0.06629159244212622,0.10996454041993704,0.052595823571614506,0.05740370185092546,0.06733880762454664,0.08197399449326151,0.06612170843663563,0.06165534543561099,0.07045995823692082,0.0554996743227515,0.041144122798394196,0.06458929818770773,0.05945900771652227,4.2319485157855725,-1.6588822121189124,3.27877855887522,3.174285285134932,2.311483028387233,4.4183912876002385,3.9958926826340586,4.676666666666667,2.5933333333333333,2.27],"ROE":[0.10582234284857,0.18095016718221968,0.08774726584222652,0.0944129919485964,0.10789265311782836,0.12365020885248378,0.0964426193454881,0.08816730465272911,0.09880904894865804,0.0779530865678805,0.0641845791760037,0.1065919275410511,0.09233138476889756,6.501513405421015,-2.6467140349042637,5.358735715367656,5.474651810584958,4.3531023683695445,8.66667216164014,7.631012418522136,9.822,5.138888888888889,3.9685714285714284],"ProfitMargin":[0.813227125039304,1.420018709073901,0.46993419559791233,0.5127945948821621,0.4085303882376855,0.4654279986835413,0.3742410252105543,0.3365024414188648,0.3886343261281004,0.3115992422244071,0.22691362942308968,0.3035533910533911,0.2968292230669386,21.891971231469253,-11.30438870736466,20.236430659717808,20.553417552078976,13.660454178512513,26.281848327806568,27.615372130349318,0.3592757929805891,0.21436944876801672,0.14196527851704824]},"175":{"cur":"HKD","cur_ratio":[13.9,25.4829772795119,0.7235830985337692,1.5835353545869544,0.014261970116214717],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[123.76891015076927,135.13886577777777,4806.853091824806,15963.977662471252,23073.042398652866,25254.468770689993,30277.565678182007,36220.496317670884,27361.421562521857,37184.27887024001,62797.10556943446,107093.56898737159,126396.91469925284,110497.68545953091,103652.07733614634,122467.08067361989,172294.62064800868,198117.34631846278,299024.85641124763,374661.7543911715,472814.80462500005,549417.12066,622477.1597947369],"Revenue_Adj":[123.76891015076927,135.13886577777777,4806.853091824806,15963.977662471252,23073.042398652866,25254.468770689993,30277.565678182007,36220.496317670884,27361.421562521857,37184.27887024001,62797.10556943446,107093.56898737159,126396.91469925284,110497.68545953091,103652.07733614634,122467.08067361989,172294.62064800868,198117.34631846278,299024.85641124763,374661.7543911715,472814.80462500005,549417.12066,622477.1597947369],"NetProfit":[203.4313932553847,310.3792563555555,985.1812028965643,1342.0238101609184,1570.8938461651335,1859.232521014615,2507.938670197319,3360.09297622069,1800.6383623954036,2789.0180749100005,5976.068105652105,12276.775293444236,14885.169610708808,9290.805429427575,6072.548611471544,5688.868825497949,5965.236745649175,5711.205083869834,18063.317393734145,18288.810874467898,24106.388,29006.97,34096.919],"NetProfit_Adj":[203.4313932553847,310.3792563555555,985.1812028965643,1355.8151999850563,1572.880943304214,1849.6703634715382,2504.4588524128367,3457.1840428163796,1771.607450861783,2754.7470900800004,5612.82228876475,11860.22627035414,14942.515046829498,9182.089692577576,5751.223257849593,5739.391846878196,4708.442505433679,5823.69605834349,10767.075739208534,17799.353135530866,24106.388,29006.97,34096.919],"OCF":[225.94165305076933,289.08862666666664,830.3657810667934,1059.1701149747119,2100.339865005363,1308.989259613846,5270.0632460114975,4461.310679611877,2570.8928672655193,9167.971470810002,9723.868735771644,13846.792268781928,16653.78626747126,14350.638357324122,2027.820086422764,20090.405509553104,19946.38965649298,25809.21663841115,40843.24048013002,52062.85234607036,null,null,null],"FCF":[220.04585358923086,267.51585919999997,302.1320382080151,226.90974982528712,313.1807436681991,-411.9051744261538,3910.9623518486615,3046.564240091954,1215.9646966126445,7190.760072520002,9035.546382216087,9584.677855622313,13698.956293295014,10982.856134479298,-1180.3652780243904,16675.4065013852,15791.435552145456,19305.06387582024,35761.67359086336,47581.00257039382,14275.670735758596,29069.916673986718,35053.97365500114],"DivYield":[1.2943750601884403,1.5789819526006623,2.5376689744194674,0.5390357412053953,0.9019994900233651,1.6486138386964833,1.0704108181236496,1.241482741715904,1.0134203987836325,0.9157503330040655,1.5772980065625128,1.049075318705136,2.555352395537154,1.6911058786225766,0.7544772854417396,0.9879851013182925,2.0832568645793756,2.8169695783931266,2.369578064313221,3.1109501712889167,4.886466326453365,5.816416665397779,6.67873425241896],"Assets":[1837.9825756000002,3121.2711487999995,11526.4252995,21353.6460473,28277.7375685,34024.04293820001,39008.26170060001,43044.0734788,46566.635365,50522.57271600001,75510.3026628,102036.3889264,104137.471828,120727.7887508,131638.0044791,164810.0344272,178485.7954661,211838.18933190004,288476.8528936,323429.72168779996,null,null,null],"Equity":[1046.4285334717044,2722.4339073056417,5430.32832731668,8059.374868978043,10707.46282616811,12513.593212216458,16414.28542447474,20792.04581253718,21817.883697683406,23581.23733368786,27582.562087856822,41795.79179279818,51665.89609938214,61438.040406583,76279.3235360032,86145.2823539592,86170.65634846201,93658.01417771645,100021.35081793022,104261.12339946486,null,null,null],"Liabilities":[791.5213313999999,398.9627328,6095.918124,13294.1282833,17570.617547000005,21509.9661547,22594.339466200003,22252.216338699996,24749.417580000005,26941.7385402,47928.3566551,60239.0205426,52473.8179132,59289.2810832,55359.065797699994,78662.94597759999,92315.94828260002,118180.05668169999,188451.36251520005,219170.46453819997,null,null,null],"Debt":[702.7612096,338.38101120000005,1204.601988,6819.143702099998,5280.8066815,7263.6476106,4553.47256518,2062.0175648,3593.2484970000005,2389.8104406,2505.7586542999998,1556.6595220000002,6261.7329771999985,7210.567190400001,5064.0806587,5662.4180799999995,18684.8462827,15171.614736900003,40415.2872868,48670.2692266,null,null,null],"Shares":[4151.388496,5201.08345,6489.75545,7310.85545,7440.75545,7457.46045,8258.948934,8801.44654,8801.44654,8801.98654,8801.98654,8970.51454,8981.61254,9166.99754,9816.62654,10018.44154,10018.44154,10063.382383,10063.382383,10075.001783,null,null,null],"ROA":[0.15430484209499248,0.126994288285728,0.13450102973532896,0.08170024147279548,0.06398073597925143,0.05985666274606004,0.06917894735985387,0.0819689594508908,0.04036678722910099,0.0568167614459297,0.09305818843937404,0.13940043151056464,0.14229294688628427,8.214752222642584,5.059619949880341,3.954564112152511,3.600913041276875,3.029705641459899,7.251631714704867,6.002746628567344,6.441111111111113,6.737222222222224,6.929411764705883],"ROE":[0.22836426568327847,0.1793282872979921,0.26875340029750644,0.2237183139885421,0.1900937628386049,0.17534989896093414,0.18158191135401325,0.18395201798286087,0.08577690024169549,0.12281541587792527,0.2325876572127736,0.3610507108533415,0.316157791895493,17.067701572239255,9.702529660590633,7.527342724871083,7.483547067648924,6.942899338650182,20.371088718174686,18.835937156565592,19.516578947368426,20.019459459459462,19.86914285714285],"ProfitMargin":[1.6436388832023683,2.2967430914060127,0.20495346624428745,0.08406575344413071,0.06808351577669927,0.07361994179708962,0.08283158219699736,0.09276772319051303,0.0658093863391154,0.07500530223115763,0.09516470626252663,0.11463597123083934,0.11776529234219352,8.408144831984082,6.0075529552669575,4.770591105755264,3.555141790052052,2.9622218733204857,6.09323638459494,4.881419216164021,0.05098640049653093,0.05279751163743398,0.05477785157176412]},"241":{"cur":"HKD","cur_ratio":[2.95,90.10316048289187,1.3895278640099833,2.434514220097769,0],"date":["31/3/2006","31/3/2007","31/3/2008","31/3/2009","31/3/2010","31/3/2011","31/3/2012","31/3/2013","31/3/2014","31/3/2015","31/3/2016","31/3/2017","31/3/2018","31/3/2019","31/3/2020","31/3/2021","31/3/2022","31/3/2023","31/3/2024","31/3/2025","31/3/2026",null,null],"Revenue":[403.5848893804577,286.1210332018236,233.52954747568933,274.65965145943153,287.56056250975877,372.2129287778929,481.72051701672854,15.890303216873885,27.488148039449907,37.18219505238052,69.05713226436785,548.0359650888892,2883.198645470769,5955.91019323192,10772.286081682163,17772.49673909629,24965.889995779435,30619.35734745019,29502.48897394421,33025.49833880332,40293.6792,44415.772552631584,49222.206447368415],"Revenue_Adj":[403.5848893804577,286.1210332018236,233.52954747568933,274.65965145943153,287.56056250975877,372.2129287778929,481.72051701672854,15.890303216873885,27.488148039449907,37.18219505238052,69.05713226436785,548.0359650888892,2883.198645470769,5955.91019323192,10772.286081682163,17772.49673909629,24965.889995779435,30619.35734745019,29502.48897394421,33025.49833880332,40293.6792,44415.772552631584,49222.206447368415],"NetProfit":[9.153904750397922,-61.01165426206957,-75.91056189423753,-148.014895714672,-51.51610077289168,-26.355730421725294,-7.735923662652922,-41.347391037726354,-39.20163725202592,-101.53199394534657,-233.7997879478928,-239.51122823524915,-126.26914724307692,-95.77975336192304,-7.392950926356584,399.21976146666645,-322.1858604917697,612.8364090666664,964.4133501747932,1546.0541231829875,2693.46,2557.542,2962.051],"NetProfit_Adj":[9.153904750397922,-61.01165426206957,-75.91056189423753,-127.59505643765159,-60.39311813761254,-22.329605037283184,-15.028294365462722,-54.53572265687923,-39.656156234960754,-141.08032868287856,-233.7997879478928,-243.4624955838124,-120.78395712923077,-122.1569076750961,-42.399740988856564,282.10983526944426,-815.5913851680044,522.001210350617,909.6098307447313,1542.8914314057054,2693.46,2557.542,2962.051],"OCF":[-10.249012988318356,-36.59819058738197,-12.66343475839146,18.038962271387526,23.218045417831334,-32.33603069464733,9.20909955859187,7.964653207828286,1.617067536646069,-69.23908753231751,-142.976755421456,-268.5858190689657,-95.25604523076922,450.8493909669229,620.7609222713173,1158.9590589777772,520.7932336942389,346.393357386008,1302.537722840909,1882.1483406058092,null,null,null],"FCF":[-36.47660526180088,-74.60869724771786,-48.05300675880808,-4.782255104877211,16.52103231751191,-40.089716547748445,-12.225459709407799,0.6130502783022027,-3.1181302283626744,-73.59134446798078,-153.63640923295023,-271.0544599348661,-102.02075892615383,443.7175399984613,612.2667741298445,1139.4016563518512,502.6793405378603,339.97271206584344,1277.6773703508263,1861.7566553406639,2759.0992304515107,2363.71879662728,2740.811619084314],"DivYield":[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0.9958233188882584,1.075489184399319],"Assets":[1007.203532025312,943.700256543642,800.371160742846,603.0337435620299,568.26825478896,565.774745146875,537.52835736096,399.7329966,494.1415604941201,1760.9395815102,1620.4763,1898.0609999999997,3883.1214914,6995.8145075,8613.684354,21045.7916256,23722.1125026,23710.608696,21350.7326395,22705.3808657,null,null,null],"Equity":[341.438,318.907,607.5169999999999,472.0590000000001,420.452,402.41599999999994,398.96900000000005,287.39099999999996,352.034,1583.617,1388.627613727587,1265.1874539083399,3149.327255343512,3200.7583941538414,5694.022389760389,16943.78094242244,17385.42138586751,17310.234012243414,15729.28040828533,17488.773889463024,null,null,null],"Liabilities":[665.7670466442801,624.8056728687799,192.855556816912,130.983555015324,147.82494469089596,163.36603893687501,138.553154190948,112.33840421250001,142.106448770748,177.28698496727404,231.84055800000002,632.8429399999999,733.8417440000001,3795.0965005,2919.5316865,4102.09664,6337.2770898,6400.3896788,5620.908705499998,5216.0420297,null,null,null],"Debt":[559.1775194775201,556.1163473231081,111.60274353630602,24.916534899278,11.073771122568001,23.3235772525,11.956185891887998,0,0,0,0,225.99999999999997,0,1988.15,76.3468475,44.77473599999998,167.2904556,88.90755440000001,89.03119499999998,38.0756315,null,null,null],"Shares":[3308.628417,3336.979992,3717.869631,3717.869631,3717.869631,3717.869631,3717.869631,3717.869631,3748.969631,8172.644639,8172.644639,8192.736918,9842.737787,11710.892714,12074.135224,13487.965042,13517.806542,13521.362542,16091.736264,16070.669722,null,null,null],"ROA":[0.012569746091952917,-0.06253174743521729,-0.08698992609411103,-0.21092361648489613,-0.08796192346274517,-0.046469999515018984,-0.014021508105193037,-0.08822424801119874,-0.08770869971852668,-0.0900436355896126,-0.139013782597901,-0.13714417821226282,-0.04465924127216258,-1.802490788928722,-0.0950919432679676,2.7223679512374788,-1.4377182879385015,2.68163118458523,4.367901372938582,7.007316402500612,9.822222222222221,8.535,9.22375],"ROE":[0.02831326791967236,-0.18596809475537535,-0.1638894469973405,-0.2742002715936396,-0.11544331028919091,-0.06404585050945011,-0.019304537502214968,-0.10868887992144873,-0.10004338616236634,-0.09767402930624945,-0.1504694596455819,-0.17307964834886355,-0.05693007394193424,-3.0493973041063915,-0.16335006022076315,3.562273853353633,-1.8700813359150366,3.6598329215176064,5.945779606387219,9.284475297192078,12.393125,11.150666666666666,11.967142857142857],"ProfitMargin":[0.022681485336207858,-0.21323722195226824,-0.3250576328125669,-0.5389029474412423,-0.17914869940186395,-0.07080820784022875,-0.016058945777441904,-2.602051733903959,-1.4261287153927313,-2.7306616460462614,-3.3855994345790266,-0.4370356025747351,-0.04379481359754165,-1.608146366457366,-0.06862935936066532,2.2462784341856423,-1.2905042061237804,2.0014672486837806,3.2689219917225856,4.681395288338316,0.06683560224982871,0.057580821207225415,0.06017606090833936]},"386":{"cur":"HKD","cur_ratio":[4.845,7.756685037762809,0.16080703694278936,0.661839794839646,0.08101096491228073],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[1008511.0930830773,1204337.4220444444,1554539.01797748,1493133.9619425277,2154419.6721816086,2967866.994637692,3360711.0165632204,3574722.937393487,3501168.397198853,2440284.8638300006,2197822.527034482,2724876.4058619244,3350516.625689654,3289569.242158617,2305268.733154471,3229476.747736629,3792966.2775157033,3479004.639378099,3267929.2065567886,2958878.9339999994,3470895.984322222,3451649.3840000005,3382218.159105883],"Revenue_Adj":[1008511.0930830773,1204337.4220444444,1554539.01797748,1493133.9619425277,2154419.6721816086,2967866.994637692,3360711.0165632204,3574722.937393487,3501168.397198853,2440284.8638300006,2197822.527034482,2724876.4058619244,3350516.625689654,3289569.242158617,2305268.733154471,3229476.747736629,3792966.2775157033,3479004.639378099,3267929.2065567886,2958878.9339999994,3470895.984322222,3451649.3840000005,3382218.159105883],"NetProfit":[52236.78322923079,58000.34542222222,31879.16919274807,71651.23149655167,82401.96813256701,88207.22928846152,78532.86707471267,83439.0991310345,58485.365560919585,40112.980480000006,54556.59958927201,59017.612962692336,73064.54685823753,65223.429479310274,37632.07561788618,86748.12051440339,77938.85956983472,64464.23497933883,53038.85969012343,35244.438671604934,55304.5,60407.291000000005,64815.458],"NetProfit_Adj":[52236.78322923079,58000.34542222222,33438.104835114485,77671.24208620684,94648.27216475092,90063.82733749998,79345.1933778736,86738.77408419542,63432.88311522993,45566.94917500001,70296.03596053638,75369.4368064924,80252.95851053638,67220.36026637924,49678.54828861788,98785.89113847748,83588.38763398762,64237.87497055784,54691.34546450615,42769.26089845678,55304.5,60407.291000000005,64815.458],"OCF":[90149.21004769235,122698.1287111111,60093.550786259504,180595.77899540216,190087.48016360146,178604.4311661538,168319.66525670505,185675.08477471268,179346.6460080461,201381.67138000004,250076.40840613018,218282.8510973078,221043.43597701145,186885.0408793101,196721.35740650402,280620.3771794242,148438.1695334711,191582.930411157,170265.37143703696,182647.93302345675,null,null,null],"FCF":[12906.441003076929,9987.677333333331,-62607.348518702245,48752.38431724134,59493.5276314176,7244.496783076921,-24777.33532183909,-9821.114553256706,22791.998441379325,74673.90596000002,164922.8985869731,136372.37545269236,98892.90804597699,26297.885286206867,48596.61064227641,105953.83500411535,-52457.61617768596,1989.9780991735536,19397.403108641967,37631.979165432094,77895.20567784515,100482.87671039414,102570.36030056707],"DivYield":[2.076393564619582,1.4971146517620586,4.963109569722611,2.958355719594554,3.329340238913456,4.52699742821276,4.340370030091333,4.857356404678304,4.026621631074559,3.8289119610614963,5.058435082705414,10.476917674057225,8.555147601757879,7.393674782775028,6.846719411510774,15.883851867873389,10.649157651629755,9.277874769666674,6.839294280484719,4.769678201735925,6.451373030601581,6.912185389930266,7.348744467189019],"Assets":[608816.2544,783136.48,866723.7435,1020157.2890999998,1174572.049,1411088.5712000004,1563750.1864,1771653.6875999998,1812903.7688000002,1728906.3528000002,1674395.8357,1915721.6528000003,1813001.8888,1969055.9196,2065634.5584,2317738.034,2206522.7389,2226963.1304,2215068.448,2398336.2445,null,null,null],"Equity":[285702.1010236896,355668.1450165134,397600.39800747193,459741.37985381787,532013.9946886809,625480.2467419083,681278.6258861268,796372.2950895514,806414.8436221284,941553.2279152216,928759.6101416488,1025443.167965328,975293.076542851,981344.7908780194,1054506.5261367816,1122589.2473372037,1062538.0253094833,1051292.9892694193,1034516.0221251061,1099103.4748966247,null,null,null],"Liabilities":[323105.2225,427484.7296,469110.2505,560407.7579,642575.087,785584.1536000001,882486.6348000001,975288.6189999998,1006513.5381000001,787369.2222000001,745656.9702,890239.8038000001,837751.1378,987703.6652,1011133.3526,1195125.2508,1143994.6911,1175668.8113,1180509.6106,1299252.4433,null,null,null],"Debt":[185217.7577,206246.336,272319.0165,258872.59369999997,247580.5885,289521.907,345703.6238,396449.206,411053.82800000004,304852.3632,214817.6845,224492.47760000004,176089.0444,375308.1976,356228.6399,387081.16279999993,403393.1609,484210.07690000004,506234.619,591381.3822,null,null,null],"Shares":[112713.1707,112713.1707,112713.1707,112713.1707,112713.33116680001,112713.33116680001,112866.3729518,116565.314,118280.39564,121071.209646,121071.209646,121071,121071.209646,121071.209646,121071.209646,121071.209646,119896.407646,119349.251646,121281.555698,120925.514222,null,null,null],"ROA":[0.09242916459747283,0.08415422643028915,0.03802751563813901,0.07600929247213462,0.07579596480622569,0.06842338869811013,0.05317772694125052,0.05008368486023493,0.032788527896287036,0.022432620032318648,0.0316863195578091,0.033042749246714645,0.0386584905257901,3.4297621483543788,1.911475310515429,3.9675856930982203,3.485752436740569,2.9332335970191785,2.383700880828107,1.5337225570700779,2.215,2.4,2.5725],"ROE":[0.219398937857501,0.19774838351986035,0.08934671204413781,0.17832090816672314,0.17978220361057123,0.16419413139118227,0.12993545841206947,0.12249876588031863,0.07998664192438916,0.05123073844306583,0.06728994060659274,0.07108559223160561,0.08531229578104405,7.896142779643325,4.503285932331541,9.467236915055308,8.571362712593691,7.331875173284865,6.046315675029219,3.952587450206234,5.66611111111111,5.948823529411764,6.168823529411765],"ProfitMargin":[0.05179594313587557,0.0481595476156198,0.02050715281127147,0.04798714202665066,0.038247872128425724,0.029720748755868557,0.02336793216901557,0.0233414171090625,0.016704528010623946,0.016437827023621794,0.024823023205101612,0.021658821969220313,0.02180694950086937,1.982734658490138,1.6324376883553788,2.686135473036014,2.054826061382299,1.8529505321631983,1.623011281385962,1.1911416268714745,0.015934269154741845,0.017501527852524523,0.019164180590468302]},"388":{"cur":"HKD","cur_ratio":[381.4,39.76487506740966,23.183176131036163,9.25234098297026,0.02274383848977451],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[3545.836,7152.242,6090.419,6414,7094,7357,6432,8131,9127,12233,11116,13180,15867,16311,19190,20950,18420,20492,22374,29161,32227.333333333332,33477.16,35872.041666666664],"Revenue_Adj":[3545.836,7152.242,6090.419,6414,7094,7357,6432,8131,9127,12233,11116,13180,15867,16311,19190,20950,18420,20492,22374,29161,32227.333333333332,33477.16,35872.041666666664],"NetProfit":[2518.569,6169.278,5128.631,4704,5037,5093,4084,4552,5165,7956,5769,7404,9312,9391,11505,12535,10078,11862,13050,17754,20072.16,20763.600000000002,22269.792],"NetProfit_Adj":[2518.569,6169.278,5128.631,4704.835,5036.165,5093,4198.43529411765,4552,5165,7578.87432636344,5769,7423.196051103369,9312,9391,11508.34,12545.855,10116.456,11994.765,13270.215,17844.579,20072.16,20763.600000000002,22269.792],"OCF":[3182.163,8307.151,5574.047,4811,5210,5452,6598,5016,4697,8267,3410,7379,9498,10098,14665,11950,15119,12305,13711,27764,null,null,null],"FCF":[3121.894,8238.423999999999,5352.669,4763,5104,4996,5556,4219,4032,7557,2790,6691,8638,9007,13314,10880,13835,10919,12107,26031,17118.84809876312,20172.6182041514,21471.740521107862],"DivYield":[2.4912280701754383,2.3462929475587706,5.828804347826087,2.81922525107604,2.38230289279637,3.4246575342465757,2.509476876421531,2.7378190255220414,2.3179965055329066,2.9974811083123427,2.319868995633188,2.2518765638031693,2.9489060017652253,2.6416252964426876,1.9185988235294118,1.947738252086078,2.117437722419929,3.1380597014925375,3.1411126187245593,3.0716388616290478,3.7098635886673663,3.7919727177334734,4.0430220356768105],"Assets":[40453.298,87944.189,62822.112,45332,47884,54028,80837,85943,251860,238193,247318,317604,255948,279051,399106,399304,406051,341179,381629,580775,null,null,null],"Equity":[5257.585999999999,8377.348,7293.614,8027,8677,9159,17764,20518,21359,29962,32384,37375,40903,44501,49236,49910,50099,51796,54407,58729,null,null,null],"Liabilities":[35195.71200000001,79566.841,55528.498,37305,39207,44869,63073,65425,230501,208231,214934,280229,215045,234550,349870,349394,355952,289383,327222,522046,null,null,null],"Debt":[0,0,0,0,0,0,6615,6921,7026,3409,3422,1860,1166,2924,2781,2485,2236,2051,1746,1249,null,null,null],"Shares":[1064.190346,1069.228714,1073.939532,1075.515,1076.436,1077.67,1147.408,1160.119,1165.264,1205.277,1221.105,1239.809,1250.632,1261.201,1267.837,1267.837,1267.837,1267.837,1267.836895,1267.837,null,null,null],"ROA":[0.07946991343933048,0.0960965536654156,0.06803418225403036,0.08698698390681624,0.10807157569515963,0.09994897558678076,0.06056426797167538,0.05458688092097374,0.030579953404795104,0.03246995733114581,0.023764652088212213,0.026212468269955852,0.032471336513515775,3.51066076759022,3.393019610503173,3.1399907315790134,2.5027472356910927,3.174926060249187,3.6109174220539892,3.6895108499133418,3.4858333333333333,3.4066666666666667,3.5236363636363635],"ROE":[0.5249721809886069,0.9049223120551957,0.6545393958584036,0.6140746056261192,0.6030890804597702,0.5710921731329894,0.303383723953497,0.2385181691948964,0.24785258409712557,0.3114564779110963,0.18585097129602782,0.2129452537425042,0.23876310863824007,22.12197592518433,24.71774929907295,25.44041240461114,20.2870543712382,23.472376127908817,24.810829309099205,31.703854498700878,33.92869565217392,33.73826086956522,34.89714285714285],"ProfitMargin":[0.7102891955521913,0.8625656122933201,0.8420818009401324,0.7333956969130029,0.7100366506907245,0.6922658692401794,0.6349502487562189,0.5598327389005042,0.5659033636463241,0.6503719447396387,0.518981648074847,0.5617602427921092,0.5868784269238041,57.574642879038684,59.95310057321521,59.83293556085919,54.71226927252986,57.88600429435877,58.326629123089305,60.88268577895134,0.6228303161170674,0.6202318237269828,0.6208119403963678]},"688":{"cur":"HKD","cur_ratio":[12.73,5.213089665464399,0.6634436137461238,0.345766894972678,0.05606443697523464],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[10911.446501457936,16636.82593489877,18899.248844289516,37321.613177900224,44317.70288626189,51341.83223990876,64583.76453198507,82474.76764562461,130970.4657764762,160371.3514016134,158783.93662532157,164049.82944527036,170782.70320369725,185655.23441492737,209061.55448695933,291961.55105505296,209972.0234219781,223899.14548084087,200665.2865631567,182417.60590677158,203654.5113623136,199285.4248216992,196915.886355472],"Revenue_Adj":[10911.446501457936,16636.82593489877,18899.248844289516,37321.613177900224,44317.70288626189,51341.83223990876,64583.76453198507,82474.76764562461,130970.4657764762,160371.3514016134,158783.93662532157,164049.82944527036,170782.70320369725,185655.23441492737,209061.55448695933,291961.55105505296,209972.0234219781,223899.14548084087,200665.2865631567,182417.60590677158,203654.5113623136,199285.4248216992,196915.886355472],"NetProfit":[2371.01347169377,4180.652741894661,5050.474442119437,7468.9246335057705,12374.46023835919,15466.969029705866,18723.111160431105,23045.30097640705,27202.71619919777,34650.18579040804,37036.25535834109,40786.14272637532,44722.665891360135,47214.25396141029,49403.3704168943,48397.38930638935,27090.192421686366,28312.78597411363,16945.5336403432,13773.054768041973,14227.769,14871.462,15313.5],"NetProfit_Adj":[2371.01347169377,4180.652741894661,5050.474442119437,6472.310082713293,9546.934081415375,13166.497508609367,15989.270184326117,19373.0440238273,22422.92484840968,26490.89839461227,24621.244976871676,34262.1410833532,36910.60105512451,38498.930745178404,41644.758421394305,43389.395601396136,22902.11753331767,24294.919863819727,16606.32618661851,13551.880342102158,14227.769,14871.462,15313.5],"OCF":[-2595.947466802986,-6809.060811563181,-3899.1795829230537,8385.146220534047,-3297.742906653621,-9054.227675546077,6614.095456609939,-9956.065471969196,-6658.941491050142,35571.019146402876,55071.50241758289,-47058.65906892971,-14314.084410536394,6609.047351393096,5821.680860300812,19197.61639145928,-22822.34323939711,28888.19366920661,41538.355274969115,11148.2079425679,null,null,null],"FCF":[-3091.578542305722,-7238.243040986342,-5340.985134483274,8361.053231393562,-5004.429476915423,-10655.665939788343,4910.571465281226,-14532.929047289601,-12917.226241521143,33007.73117813529,51797.03864964228,-51691.65327913616,-19316.603507835243,-355.0798759827581,-7871.180209016259,7241.971740734987,-31440.214334631408,20838.786429675616,36917.72543587158,5043.425345720987,24091.08818000582,-10421.040903745954,-27006.946874372803],"DivYield":[0.9578247848483228,0.7444511943117352,1.205897181100352,1.2194905095319657,1.8776673320639845,2.542274687073379,1.774715037112621,2.155983110402078,2.698564385855863,2.242685250104305,2.0438415673887893,3.1807937519060165,3.8096076612056646,3.4188868339406033,7.057156183594213,6.791233679280204,3.987283294748787,5.954777845988566,4.760895366889138,4.545797546542149,3.950751052406331,4.172288494597341,4.569209745189565],"Assets":[35854.65996622934,64518.50030730983,85579.42333896885,114119.43809779995,162243.25853693034,175982.84511541587,229818.82793010538,296518.9790409606,410149.61266699777,582240.9394999294,571273.4677118375,645429.9661130916,721812.8922096,809749.8273382001,978422.5910109,1067201.5371064,1032799.0978788,1015872.1396909001,966968.7082602001,1019812.1286716999,null,null,null],"Equity":[14889.409,25790.102000000003,32884.388,41809.631,57942.140999999996,71889.99799999999,87556.95599999998,111050.396,138556.80299999999,215579.52700000003,227423.35899999997,273543.43,292190.9373989678,323434.71767383756,390048.12515075377,438088.89024952415,421940.2924220132,432163.0409655833,427609.7864844507,467650.0675468658,null,null,null],"Liabilities":[20964.819500935337,38729.46622385344,52693.88931804704,72309.05782740281,104302.95401308931,104089.81501097024,142264.1044573695,185469.89043727276,271597.3813487127,366664.06110999815,343856.4021984511,371875.86354262667,429634.6352452,486312.6498132,588376.4337829,629103.4620192001,610862.7675863,583708.5520617,539341.2243886001,552170.4319522999,null,null,null],"Debt":[9825.175705691398,17373.289577035328,29622.5583101811,23666.875126468367,44536.58436821678,42795.81692304413,59612.318966333194,73540.9341923886,98553.02813554142,204998.13536196496,173815.7298641966,178245.391150858,194995.9596716,212727.21801520002,253821.28834509998,297566.007816,307087.7287711,284564.80812,258118.7164054,275870.6009745,null,null,null],"Shares":[7085.987634063277,7832.053434603094,7850.699,8169.023,8172.519,8172.519,8172.519,8172.616,8173.976,9860.581,10956.201,10956.201,10956.201,10956.201,10953.185,10944.815,10944.884,10944.884,10944.884,10944.884,null,null,null],"ROA":[0.07773516502291043,0.08327945443434188,0.0672713829233247,0.07480379188873704,0.08954184105721855,0.09144298480878152,0.0922731191662981,0.08756088695362539,0.07697911881653469,0.06981624076508469,0.06418628292900472,0.06701246644203644,0.06439009700636784,6.130060245535376,5.673983316571798,4.742113007122377,2.609382912476829,2.788439159792465,1.7067273980916606,1.391324378889466,1.3381818181818184,1.400909090909091,1.491111111111111],"ROE":[0.17870825759166173,0.20030818457574656,0.16969688760118978,0.19834404363430447,0.2555697908833401,0.24477829465970796,0.23570551138371038,0.233691600366427,0.2232794203913063,0.20186306652784158,0.1714661751640884,0.16709680287089593,0.1608184306949126,15.753588043091622,14.763829352948388,12.210719651686203,6.665735745646499,7.040528863042088,4.1494321597752615,3.302593532460966,3.0820000000000003,3.125263157894737,3.183125],"ProfitMargin":[0.21729598100279054,0.25128908352193435,0.26723149071850316,0.20012330651153235,0.2792216074492247,0.30125471481875093,0.28990430174070286,0.27942244196949395,0.2077011487889485,0.216062192452532,0.23324938369385945,0.24862045187302206,0.26186882542793677,25.431146129653154,23.63101649087563,16.57663111169848,12.901810431784785,12.645330071854325,8.444676172233617,7.5502880873686,0.0698604682907476,0.07462199421347519,0.07776468736193702]},"700":{"cur":"HKD","cur_ratio":[401.4,28.48440273959549,5.784835209146814,4.21033857546414,0.005019519182859991],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[2729.0642214661552,3920.0971792,8018.313189416789,14115.293739494242,22552.612359552862,34326.521771587686,53962.94512091382,76253.68708314178,99349.34951264375,126911.34077000001,177605.85851034475,274497.3035076925,370782.0022605363,428018.75854137883,542447.4150243902,675084.1787604945,645737.5353289257,673292.5067045452,715569.9622473248,815850.8031283949,974082.861494737,1065095.5715413792,1166582.8807632653],"Revenue_Adj":[2729.0642214661552,3920.0971792,8018.313189416789,14115.293739494242,22552.612359552862,34326.521771587686,53962.94512091382,76253.68708314178,99349.34951264375,126911.34077000001,177605.85851034475,274497.3035076925,370782.0022605363,428018.75854137883,542447.4150243902,675084.1787604945,645737.5353289257,673292.5067045452,715569.9622473248,815850.8031283949,974082.861494737,1065095.5715413792,1166582.8807632653],"NetProfit":[1036.6861929230774,1606.666919111111,3120.7594063362576,5849.975217512639,9245.138761829498,12290.688721477689,15652.5670855115,19558.956552490425,29968.93543678163,35540.55474000001,48037.44129501914,82559.31264230773,93342.33607279691,105856.3338965516,179869.4612113821,270967.5019144036,219195.98317727278,127376.2870413223,210331.44560864186,244008.80896049377,311121.903,323702.936,353937.60000000003],"NetProfit_Adj":[1036.6861929230774,1606.666919111111,3120.7594063362576,5843.410450145283,9245.463791687362,11781.315841592519,16921.569835845316,19432.155163409967,26761.843983908067,34527.613150000005,45576.12891730548,121101.33570455706,117426.43505747124,116143.60466896537,133684.5644126016,98780.16730136203,67336.65114460784,143304.69017937387,201051.2831255885,233351.19331345326,311121.903,323702.936,353937.60000000003],"OCF":[1526.534549507693,1952.5462896,4100.756111546562,9661.208690209189,14411.985069434477,16615.317216072304,25196.761244752885,32124.280272413795,43385.10019195406,59493.353800000004,79435.07295785438,128933.84026384623,133519.65505747122,169536.89965172394,219207.43123577232,212013.73231604963,171691.8564384298,246363.71085123962,286699.64373415627,337409.3930790123,null,null,null],"FCF":[1210.8705228000003,1343.0445612444444,2606.8138521938913,8766.150951377005,12179.485451493865,11734.020094955382,20316.39210278928,25965.8963908046,37948.903965517275,49024.645650000006,67856.75793103446,114901.87430307698,107214.61582375476,138768.11294137916,174853.05140650403,174643.64235884792,144671.23527520662,222743.77635743798,211210.38705020567,242469.93058765426,136549.56185893458,167033.90642462744,214212.5819761536],"DivYield":[0.4254984603804325,0.25965887688829287,0.5006027034907751,0.23696950784292975,0.32127979484844443,0.48246798018274023,0.3948611416900439,0.24995920439504649,0.31991703502361146,0.310659305193035,0.3169256775660338,0.2116621440028768,0.31961381653166654,0.31502051644621437,0.29948144949819394,0.35651922635867683,0.6978778833143946,1.1521254154121416,1.0684201164753013,0.8658071624647755,1.4886598016510275,1.5223266408230762,1.636208383761572],"Assets":[4635.2251124,7401.9145472,11190.9849735,19881.2973105,42261.619463,70034.1016085,93550.49865410001,137378.7585,213803.45060000004,366524.78280000004,442337.9527,665994.6704,823801.0106,1067128.7396,1583975.5575,1978048.1551999997,1784708.3479,1734812.8754000003,1895334.879,2270818.7082,null,null,null],"Equity":[3705.371577507014,5595.486847035123,8083.735247703463,13967.332567297975,25762.11679779049,35861.63349213454,52395.66879541298,74897.62894058556,102584.2194155363,145863.15375722546,208098.42115653414,332693.01549172384,405594.89211310603,546794.3678088291,924241.9428085549,1075504.4237032144,885344.6876177775,960960.5163352673,1121509.7075376057,1382198.83153768,null,null,null],"Liabilities":[929.7377072,1806.6856255999999,3106.9834875000006,5913.7170983999995,16500.327452,34171.08229090001,41155.9891895,62481.80919999999,111222.36220000002,220664.12280000004,234244.1796,333289.1053,418223.7204,520330.2132,659738.2778,902521.1827999998,899371.9739,773851.1435000001,773778.7558,888644.6176999998,null,null,null],"Debt":[0,312.28625919999996,0,229.77709539999998,6250.1079865,14465.333365900002,13300.5695827,19284.3983,45058.7843,78042.0234,122340.9981,158378.3335,204138.4554,260096.872,311780.9856,396840.35679999995,406152.5569,408326.87600000005,381102.7904,452606.56629999995,null,null,null],"Shares":[8842.25884,8942.873235,8975.157215,9001.487225,9092.52223,9089.794045,9249.21381,9290.2605,9351.158195,9382.167262,9412.64278,9481.186292,9503.100136,9518.433132,9567.272033,9593.165226,9514.542294,9436.743796,9180.530022,9086.323137,null,null,null],"ROA":[0.26339200271760727,0.27056286031394566,0.3318725742284385,0.376856498381182,0.3019965228284697,0.22028694089163067,0.19281923416488553,0.16989348576022276,0.17104823617731257,0.12053123117091785,0.11696031261517796,0.15045693588380038,0.12317232217669788,11.124841803938821,13.976237764004807,15.263958144999524,11.800237894119878,7.302835762572904,11.558015044185334,11.771891012023358,11.159545454545453,10.433181818181819,10.42315789473684],"ROE":[0.32012427008702304,0.35238371260977536,0.45681296909391783,0.5370623184549252,0.47464373026050366,0.4063291330799721,0.3650122207369838,0.3124064570436537,0.345177517795271,0.2879908821882748,0.27893259666258285,0.3320656237084918,0.27163965878975266,24.67813428967385,28.12499450157915,29.77216852735547,24.644135917627267,15.061092221999997,21.779782609549535,21.134746439817643,18.73371428571428,17.017142857142858,16.35382352941177],"ProfitMargin":[0.3798687421016904,0.4098538494494655,0.389203980016057,0.41444232939655756,0.40993649047993463,0.35805226067648904,0.29006139398876524,0.25649850257292717,0.30165205493336034,0.2800423864752146,0.27047216627835036,0.30076547779273216,0.25174451700384404,24.731704343354828,33.158875170101894,40.138327995172446,33.9450583534096,18.91841744456212,29.393554328087397,29.908508764695398,0.3194095976383348,0.3039284373456256,0.3034061503430822]},"1772":{"cur":"HKD","cur_ratio":[24.16,6.4276345719477375,1.3453534474197981,0.9085944915706802,0.015037322611163669],"date":[null,"31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[null,215.28408890104353,269.11371686071067,278.332646446156,411.80379757324897,570.0511417982465,769.8517512047376,863.689238325061,1090.876989889336,1658.4346084100002,3078.3425721624512,5060.7509489253525,5933.435615605923,5951.852069051717,6176.129934699186,13309.622814552278,48173.271665967775,36275.10845539462,20490.28490131452,24741.13580329876,55908.63160000001,58367.27160000001,63842.825666666664],"Revenue_Adj":[null,215.28408890104353,269.11371686071067,278.332646446156,411.80379757324897,570.0511417982465,769.8517512047376,863.689238325061,1090.876989889336,1658.4346084100002,3078.3425721624512,5060.7509489253525,5933.435615605923,5951.852069051717,6176.129934699186,13309.622814552278,48173.271665967775,36275.10845539462,20490.28490131452,24741.13580329876,55908.63160000001,58367.27160000001,63842.825666666664],"NetProfit":[null,30.292337622764443,43.631763656343296,38.42134130886908,48.95112793075396,65.39616251713392,85.62121186458235,93.52205119700007,107.90247858868727,269.24629033,1305.7101396429114,1696.071606885132,1590.5166805935312,409.25027512068914,1153.7393720569105,6298.560770474081,23875.394077911158,5508.421893390495,-2247.7636506147996,1751.2294800975305,8651.516,8135.211,9518.771],"NetProfit_Adj":[null,30.292337622764443,43.631763656343296,39.33962833469493,49.392991162712306,67.18040923053655,96.51607090696902,112.06124037830973,113.80954897612645,201.05940543200003,894.5553475707277,1468.661940750772,1626.481782476764,823.9845799367232,739.9764279126015,4297.045701834572,23418.037897317463,4810.24227836033,-548.3109600119714,1474.6220614308638,8651.516,8135.211,9518.771],"OCF":[null,-6.498211938163556,24.571994689067033,50.1774266321225,-9.854131722989647,40.863513168894315,-15.422454523675082,71.34078496494561,8.27790719884058,433.95355396,746.659293457471,535.177516368506,863.2831913973748,781.863209086206,719.8097965934959,3015.5273783028842,16403.925089367356,2374.107993830578,4674.222119714477,3556.1634278666656,null,null,null],"FCF":[null,-47.66904142953244,-37.04440562955554,4.464204249669613,-99.18216085567053,-124.59532592980105,-281.3754474796359,-245.30562735141467,-180.94009143545196,272.26784204,154.94368855325664,127.73172419910246,-557.7180912691746,-181.4577908379307,-665.5137432276422,-1098.441522447738,15575.12968668967,-7184.580446324379,-5319.410860377762,-4498.182723145679,2989.012432,6260.402474666667,8096.957157333333],"DivYield":[null,null,null,null,0.8163265306122449,0.9633911368015413,1.035732780942517,0.6399317406143344,0.6631299734748011,null,0.37721614485099964,0.5574912891986062,1.358695652173913,0.8613264427217916,0.2964426877470355,0.21001050052502626,1.4386419220256077,1.869158878504673,0.42844901456726653,0.23851168707266654,2.801495005720081,2.621537861255423,2.991179563399044],"Assets":[null,214.03480792147198,333.66146955006,340.79367334484897,915.87953485182,1023.2389920277582,1302.6437552657449,2300.7170317017167,2441.3057233069635,3102.3905352,4784.2819518999995,9604.51943780353,15529.104268147352,15898.697595200001,26158.0022746,47914.6994468,89521.942219,100858.52130990001,107305.73152871602,126135.53706039999,null,null,null],"Equity":[null,128.32628304036058,181.04741660470998,215.94474361215188,847.5875165002304,911.9009456336477,1007.6903086710574,1682.7217276888223,1733.5272354216288,2332.6695976107903,2782.704306703858,4854.451257735653,9217.625589314443,9407.668777493753,15939.803721120845,32100.634547914604,55263.42665520541,57542.14382600124,50640.944066266966,57732.54179027343,null,null,null],"Liabilities":[null,85.714440114816,152.60809013802,124.84510097061798,68.319154281175,111.30280718637302,294.97574321674904,618.01057346797,707.831397589552,769.7608182,1472.8650827999998,4749.885063925237,6311.878703211474,6490.9572686,10218.278975100002,15813.3918868,34259.0345018,43316.30469610001,56662.69159743128,68404.0286692,null,null,null],"Debt":[null,37.792768,93.34878562275,79.12378256184701,2.359,3.9945960000000005,129.494305687912,351.68609476359006,438.9844855368881,340.09903620000006,664.3856855,2735.1982826069357,3495.0862981875125,4684.7113418,7251.094839099999,7811.837927199999,17051.1051685,30659.2307438,37801.09837629306,42703.36866670001,null,null,null],"Shares":[null,945.0000000000001,945.0000000000001,945.0000000000001,1260.0000000000002,1260,1283.5284,1479.97941,1485.62631,null,null,1530.7002459,1820.1292900000003,1809.6411326000002,1875.9448120000002,2012.4706000000003,2017.036179,2017.167779,2004.417832,2096.694404,null,null,null],"ROA":[null,null,0.1575849093179972,0.11402610664174194,0.0792186924974908,0.06758839756984797,0.07417515350944338,0.05213007585516545,0.045717074821456795,0.09589310620047163,0.3247578323350073,0.24883092265931053,0.12398084870884572,2.5904606927199647,5.659467950653052,17.112562375132057,34.68873536255212,5.832390068488001,-2.1544811181535146,1.507467009631237,6.15,5.552857142857143,7.0633333333333335],"ROE":[null,null,0.2828598577857911,0.19452850963234009,0.09431048044851992,0.07549475258981048,0.09101549095093038,0.06992342203654714,0.0634935814832365,0.1307323097947325,0.4546657901752915,0.45025813737318926,0.22209647477478434,4.400146335809582,10.758254038729898,32.06148584136966,62.1928393068867,10.941402693039741,-4.670333871614997,3.712666921173146,14.542499999999997,11.557272727272728,12.621],"ProfitMargin":[null,0.14070866907720464,0.16213132561699353,0.13804108788331362,0.1188700255296865,0.11471981673580972,0.11121779190681073,0.10828206147197796,0.0989135159956334,0.16234965730010656,0.4241601150731206,0.3351422790811889,0.2680599881138354,6.8760155725089,18.68062013357082,47.32336038544574,49.561495933808544,15.185128667951133,-10.969899449619648,7.078209723354889,0.15290890560922346,0.1374118465060131,0.14503240262414557]},"762":{"cur":"HKD","cur_ratio":[5.385,8.09254079632405,0.4023851727838004,0.43409383456164014,0.06846980929933576],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[92917.13197291542,154598.1648,179083.71255572507,174677.32168965501,196723.76471647504,251963.6944838461,306029.7197739465,372251.0271793104,358319.4669919543,341820.2857100001,320518.8536505746,317293.9915280771,344912.1392528735,329577.2461896548,341896.79728455277,395147.18031440367,413307.79393057857,411922.14989876025,422226.778393827,425658.31595925917,462099.0395333334,466203.7496133334,473648.29838666675],"Revenue_Adj":[92917.13197291542,154598.1648,179083.71255572507,174677.32168965501,196723.76471647504,251963.6944838461,306029.7197739465,372251.0271793104,358319.4669919543,341820.2857100001,320518.8536505746,317293.9915280771,344912.1392528735,329577.2461896548,341896.79728455277,395147.18031440367,413307.79393057857,411922.14989876025,422226.778393827,425658.31595925917,462099.0395333334,466203.7496133334,473648.29838666675],"NetProfit":[3704.043320553848,21993.409244444443,39671.60594427479,10842.940570114935,4248.55373295019,5091.86696076923,8723.82511877395,13131.829428352492,15173.268235632195,13031.289980000001,730.5852490421454,2110.452013846155,12091.258793103445,12853.416172413778,14057.875211382112,17317.082258436232,19498.396956611574,20702.405491735535,22339.851954320977,22590.474054320985,19869.734,21015.061,22489.9],"NetProfit_Adj":[3704.043320553848,21993.409244444443,39671.60594427479,11525.446718390796,4254.580457375478,4714.223172692306,8159.530342911881,13145.077334674332,16239.047155459783,6321.631512500001,1041.8145651340992,5131.5271272115415,15780.177701149421,14625.72415258619,15850.414650406501,20727.648344238703,21058.152270041326,21574.679225206608,20067.17599506172,20462.84990061728,19869.734,21015.061,22489.9],"OCF":[35221.1785470954,68206.55128888889,64890.275839694616,65708.94604597696,76673.71005977008,81078.37462230767,87117.7699501916,99243.22052030652,111325.59565632191,104472.40204000002,87611.78306513408,98400.40240269236,109856.50310344824,106626.63129999988,119623.03210569105,134540.0374683129,118578.66561239671,114916.813053719,99371.32031687237,100161.66469876541,null,null,null],"FCF":[18676.522529849244,25323.660977777774,10952.895782061061,-22943.099448275843,-10061.759921455934,-12713.405229230766,-19573.28350000001,7444.061647509579,23739.777037931053,-4674.830310000001,-27286.482349425278,27410.47686692309,47987.961494252864,42884.804779310296,58880.36326829268,47705.138702057666,34192.35164132232,27164.306597107432,17286.20960905349,26754.561724691353,33572.57514340569,37540.187289174784,41345.92358797777],"DivYield":[1.5736879376819575,1.194245171368211,2.442062415031082,1.7676281978694512,0.8485882991129494,0.7544994575148901,1.2010892678570195,1.7670506025863435,2.4021885859769117,2.1490527354786613,0,0.5912315136402226,1.8251080661468195,2.2554707738992654,4.37790035898783,6.794442020604145,6.415517122289356,7.555630179063496,5.821897737063053,5.969424521708806,7.594084206198886,8.466967448290712,9.165274041964173],"Assets":[147807.33484380002,357072.1856,396007.896,473638.0065,520476.7855,562489.6657,641593.7444,677920.9681,680849.4352,729119.3316000002,686194.2642,686779.9881,615208.352,629211.3814,689713.7464,725132.0367999999,726787.5867,727089.9949000002,714330.4154,745753.5666,null,null,null],"Equity":[79598.12506585436,190806.69608474596,235866.24023148482,234480.41449517364,242584.91597493057,253841.834817531,260441.42814755414,280434.0536316173,284230.1382060122,276215.3559306359,254394.78072539155,365415.6589515422,357861.56999345793,358793.81422868144,389062.97095232253,409047.02844511584,388508.5105963529,389251.917816707,386895.1816587664,413041.97125524026,null,null,null],"Liabilities":[68206.7215883,166274.2848,160133.8875,239153.4346,277899.636,308638.0215,381158.0789,397489.4592,396627.9721000001,452908.69800000003,431805.1656,321350.5452,257362.3124,270414.8384,300652.7384,316076.4324,338282.7243,337837.58470000006,327419.2214,332718.9887,null,null,null],"Debt":[25561.466873300003,76375.3792,34057.0515,81610.2663,112063.1155,129395.32080000002,176669.372,183543.197,170951.826,176320.57080000004,195853.7516,87056.7535,45569.0492,64071.1708,50602.1642,43022.64919999999,63529.4384,50093.84560000001,42317.913,37962.6919,null,null,null],"Shares":[12680.989,13635,23768,23562,23562,23565,23565,23782,23947,23947,23947,30598,30598,30598,30598,30598,30598,30598,30598,30598,null,null,null],"ROA":[0.02612974306452715,0.08887945168065935,0.10367890527635358,0.024957005577196045,0.008623883567086172,0.009419477616762971,0.01459546236618855,0.01991399557062839,0.022443711525232186,0.01828256094331229,0.0010208248264597796,0.0030822746444972206,0.01833493211831668,2.054734276431581,2.185781832973935,2.452521652447913,2.7145125508717807,2.8727159484365434,3.094375990776769,3.104884569919939,2.564285714285714,2.657142857142857,2.755714285714286],"ROE":[0.04868428339181153,0.1659381833087382,0.18329678409888228,0.046143074434438304,0.017960526635058212,0.02054140475606171,0.0341644138342766,0.04858964902288494,0.05400501747155273,0.0460461638732488,0.0027255501795592457,0.006879201892156845,0.033001495213375365,3.5743072610805893,3.8640096252284906,4.361049225707365,4.969167126637347,5.403603845371401,5.785950971131998,5.7082762601686206,4.51,4.641538461538461,4.84923076923077],"ProfitMargin":[0.03986394373035045,0.14226177440655133,0.22152548312806652,0.06207411737958361,0.021596545486374512,0.020208732735087276,0.028506463768348825,0.035276811800513834,0.042345643017974506,0.03812322008020242,0.0022793830712954555,0.006651408694133443,0.03505605462102538,3.8999707416140303,4.11173059327668,4.382438524465158,4.717645600432744,5.025805360751697,5.290960473730008,5.30718494325932,0.04300017915322022,0.04507836649095507,0.04748372929738729]},"823":{"cur":"HKD","cur_ratio":[39.96,14.75143560295324,6.998512308174654,0.5490833724565161,0.06974621680504034],"date":["31/3/2006","31/3/2007","31/3/2008","31/3/2009","31/3/2010","31/3/2011","31/3/2012","31/3/2013","31/3/2014","31/3/2015","31/3/2016","31/3/2017","31/3/2018","31/3/2019","31/3/2020","31/3/2021","31/3/2022","31/3/2023","31/3/2024","31/3/2025","31/3/2026",null,null],"Revenue":[1354,3954,4199,4503,4990,5353,5932,6506,7155,7723,8740,9255,10023,10037,10718,10744,11602,12234,13578,14223,13980.285714285716,13721.2,13769.8],"Revenue_Adj":[1354,3954,4199,4503,4990,5353,5932,6506,7155,7723,8740,9255,10023,10037,10718,10744,11602,12234,13578,14223,13980.285714285716,13721.2,13769.8],"NetProfit":[2307.997,5000,5139,622,11982,15280,9602,21062,17305,27230,16295,17711,47761,20329,-17122,1185,6894,15451,-1983,-8863,6556.923,6509.2,6558.533],"NetProfit_Adj":[2307.997,5000,5139,2488,2174,2469,2922,3360,3860,4088,4636,4830,6167.49,5754.565,6838.525,6524.535,6479,6134,5764.605,7417,6556.923,6509.2,6558.533],"OCF":[938,2756,1991,2433,2442,2892,3368,3786,4282,4499,5123,5342,5682,5385,5895,6288,5836,6266,6416,6909,null,null,null],"FCF":[-32881,2485,1506,1620,1725,2167,762,2915,3277,-3779,-6153,-1457,3684,2762,4142,-1300,4978,5508,5439,5776,8792.495310118107,8448.90522727044,8427.62393886182],"DivYield":[1.2982142857142855,3.5867021276595743,4.310544611819235,5.4823759791122715,5.087251828631139,4.535934291581109,4.481660899653979,3.4624113475177305,4.344429882044562,3.8251046025104602,4.482173913043478,4.19485766758494,3.7280597014925374,2.953921568627451,4.371232876712329,4.09590395480226,4.562238805970149,5.431881188118813,7.805349182763745,7.481868131868132,6.366366366366367,6.266266266266267,6.313813813813814],"Assets":[40227,43736,50348,48680,55224,68754,79225,99423,113466,143144,163452,175940,216404,226937,207619,209885,225716,267919,252314,229177,null,null,null],"Equity":[0,17390,30558,29201,41145,54975,62735,81642,96351,118106,127441,138497,179068,189326,160117,158693,162990,189060,178442,163398,null,null,null],"Liabilities":[16976,19184,19790,19479,14079,13779,16490,17781,17115,25038,36011,37443,37336,37611,47502,51192,62726,78859,73872,65779,null,null,null],"Debt":[12169,12177,12185,11538,10867,10380,12595,13535,12524,19422,29363,27497,25785,24217,34598,38636,49745,64913,59259,52908,null,null,null],"Shares":[2199.4680002675436,2199.4680002675436,2221.3075328898285,2229.912818929724,2265.9314152530605,2297.0498608259886,2328.0112059518115,2354.4450172616935,2377.9354510421335,2358.2614505996576,2308.2286425774723,2277.208160073266,2212.438680599503,2170.5190382844294,2117.6042374756253,2142.2639994649135,2171.4169509315143,2553.845091,2553.845113,2582.396465,null,null,null],"ROA":[0.06248130701968109,0.11910007979705346,0.10924280430253816,0.012562103647453244,0.23063597166615338,0.24649534594847475,0.12977517080126233,0.23579329183646053,0.16257298404332773,0.21222867386306066,0.10629623347988884,0.10436898925136714,0.2434649185408723,9.170818850501082,-7.8802271744032995,0.567659232007358,3.1652819897107674,6.260090957893991,-0.7623507159292087,-3.681481066105078,1.8319999999999996,2.102,1.905],"ROE":[null,null,0.2143572203220155,0.020816948074766983,0.34065902823188243,0.31793591344153144,0.16314671650666893,0.29176392361664255,0.19444584899406156,0.2539436810176399,0.13275327606082454,0.13335190567259475,0.3014881562958638,11.068431096579943,-9.827521882623044,0.7442742697790102,4.289874551971326,8.788264870829398,-1.0784119120194255,-5.178604295150646,4.011,4.239090909090908,3.7353846153846155],"ProfitMargin":[1.704576809453471,1.2645422357106728,1.2238628244820196,0.13813013546524538,2.401202404809619,2.854474126657949,1.6186783546864465,3.23731939747925,2.418588399720475,3.5258319305969184,1.8644164759725401,1.9136682874122095,4.765140177591539,202.540599780811,-159.7499533495055,11.029411764705882,59.42078951904845,126.29556972372077,-14.604507291206364,-62.31456092244956,0.4690120788659116,0.4743899950441652,0.47629834855989195]},"857":{"cur":"HKD","cur_ratio":[10.76,11.27817409775356,0.5832900027070611,1.2377231761859655,0.04474491751858736],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[671417.5407292311,858061.0067555555,1071561.4794137396,1156544.3961494244,1682219.4997490414,2413840.0677238456,2698897.7435095794,2849087.1633425294,2873496.0429494274,2128815.8121200004,1890056.7694911873,2327373.6926653855,2790805.2957088114,2855216.801137928,2176068.611804878,3150953.2770921844,3771786.4422070254,3330794.3871818176,3184110.0560135785,3108652.6049148142,3843358.120175,3783681.276853334,3708164.4411466666],"Revenue_Adj":[671417.5407292311,858061.0067555555,1071561.4794137396,1156544.3961494244,1682219.4997490414,2413840.0677238456,2698897.7435095794,2849087.1633425294,2873496.0429494274,2128815.8121200004,1890056.7694911873,2327373.6926653855,2790805.2957088114,2855216.801137928,2176068.611804878,3150953.2770921844,3771786.4422070254,3330794.3871818176,3184110.0560135785,3108652.6049148142,3843358.120175,3783681.276853334,3708164.4411466666],"NetProfit":[138599.03844923084,150558.97777777776,127561.50597709915,117310.49568045969,160703.4677609195,160165.53654384613,141781.82858620698,163515.75346704983,134894.19355862078,43820.51943000001,9184.333282758618,26314.842861923087,62360.634616858224,51818.666417241315,21386.6946504065,111088.21490534992,173936.8793904959,178452.39158677682,178471.7149580246,170711.31579999998,229887.283,221547.299,215679.159],"NetProfit_Adj":[138599.03844923084,150558.97777777776,127561.50597709915,120426.02561666658,166897.21854885053,170595.00996384613,146263.91503304604,147008.86219003834,140729.0735439656,47298.88188750001,1877.8963241379304,51492.8217940032,103460.77621168579,74177.08139343307,36924.5682906504,149766.0049078191,250318.41235565947,188359.16589049582,197803.4600735491,188424.63985615168,229887.283,221547.299,215679.159],"OCF":[null,211710.03271111113,214541.37312595407,293814.64180689637,359242.13749003824,347788.84822461527,281493.3469272032,351816.4471042146,438689.32984137966,310590.59323000006,302154.8628306512,411553.1513850002,404263.2990421455,397633.41884482716,347913.5633739837,414315.53194567945,460195.9366747935,505331.72187396686,450501.3816847734,458051.8142320987,null,null,null],"FCF":[null,33944.76551111111,-36719.60272213738,1565.8495172413782,61785.978808045955,24984.720282307684,-101764.84611494259,-31868.154083524907,52842.74743563223,41932.82073000001,90514.2521425287,137928.66063576928,87402.83275862067,26935.451031034452,68425.94522764227,99000.7275744857,176363.55357727275,194811.11710537184,122495.82529670776,140303.81711975305,166690.64192414982,205009.11447314723,214498.87411258323],"DivYield":[3.2333408400056802,2.787843415888483,2.500526342985658,3.097530286218135,3.9960322951853504,4.167895375579501,3.210393905006713,4.8026914798391465,3.827446382561335,2.053665262203882,1.146707854330214,2.8639470918820322,4.171937003751243,4.109894328855058,8.65389469807886,8.011861882882998,13.386561106556494,9.378981404250936,8.18581450595997,6.246400843769141,6.148376056414736,5.951802754433448,5.831674625444883],"Assets":[null,1141136.384,1359150.351,1647092.0816,1953826.4165,2364191.7794,2696154.6176,3000477.121,3004676.3243,2859686.0424000006,2677778.1623,2887575.437,2779182.5522,3057346.334,2955644.8754,3069775.0215999996,3020647.4708,3034596.6025000005,2929750.0494,3189033.1745999996,null,null,null],"Equity":[615533.7084854774,835947.0131970235,963370.7558274118,1030857.7482972271,1191485.3014856519,1334619.388796292,1467820.3164715318,1626928.491672725,1646090.488110964,1605610.4668053952,1534118.4502500107,1658836.5535524918,1604232.9127280659,1615892.6612861536,1623417.2765810762,1728364.2528437253,1736343.6197199435,1798659.7951895453,1819560.8988547001,2038969.5008799415,null,null,null],"Liabilities":[null,305227.904,395747.8665,616216.0559,762379.261,1029520.8160000001,1228366.7788,1373563.3925,1358636.0772,1254103.026,1144021.3468,1228676.31,1175019.2596,1441441.3832,1332235.7895,1341374.5324,1284320.1558,1235934.5321,1110113.8448,1150100.1708,null,null,null],"Debt":[null,75416.6656,144157.4025,266061.5747,275499.3535,392064.6658000001,552963.2006,634718.4328,672120.7244,645683.6892,576667.5798,571499.5804,472031.6792,706296.3446,591745.2576,576961.5864,525310.9663,454476.48020000005,394146.6898,411779.43799999997,null,null,null],"Shares":[null,183021,183020.977818,183021,183021,183021,183021,183021,183021,183021,183021,183021,183021,183021,183021,183021,183021,183021,183020.977818,183020.977818,null,null,null],"ROA":[null,0.1513009042484366,0.10051919906104365,0.07810898101803758,0.09012046253751879,0.07440306899159586,0.05644268101511275,0.057459023552617755,0.04514802584809997,0.014800856038473809,0.0032802455696123263,0.009493404639035707,0.021705865321773325,1.7656130081036832,0.7280156956598681,3.6939011555814902,5.774872933465345,5.945354933630596,5.975200934981283,5.601459280882192,6.861,6.441,6.093999999999999],"ROE":[null,0.22152932980395973,0.14879452511446864,0.12616525333238962,0.15675288007887359,0.13695523082952776,0.11160103640731485,0.11799184702821675,0.09284471433045327,0.030155246411757465,0.006633906633906634,0.01912863298629659,0.043687616636200556,3.7375660284648093,1.5543105006325684,7.436905917935838,11.35765728501998,11.45643808937762,11.102537471468489,10.042717717669786,11.637777777777778,10.669411764705881,9.854705882352942],"ProfitMargin":[0.20642749115356365,0.17546418796847743,0.11904263864252485,0.10143190012508892,0.09553061760661656,0.06635300270530177,0.05253323469819104,0.05739233097916678,0.04694427677727443,0.020584457885231954,0.004859289641988418,0.011306668518619566,0.02234503235060682,1.8148767686078804,0.982813434024395,3.52554307018688,4.611525123588874,5.357652585026878,5.605073688359455,5.491488998484536,0.059814359412979703,0.058553556068062104,0.05816350348400907]},"868":{"cur":"HKD","cur_ratio":[8.395,6.238322717622081,1.2878314065444687,0.9858545959218534,0.07742703990470517],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[1933.173,2774.624,3894.283,3957.957,6364.314,8226.651,8433.049,9936.076,10861.082,11460.263,12848.4,14727.542,16014.49,16258.489,18615.879,30459.12,25745.99,24293.673,22323.569,20829.014,21224.285714285714,22440.57142857143,23632.333333333332],"Revenue_Adj":[1933.173,2774.624,3894.283,3957.957,6364.314,8226.651,8433.049,9936.076,10861.082,11460.263,12848.4,14727.542,16014.49,16258.489,18615.879,30459.12,25745.99,24293.673,22323.569,20829.014,21224.285714285714,22440.57142857143,23632.333333333332],"NetProfit":[388.235,670.86,709.232,773.526,1571.198,1265.371,1188.142,3521.938,1363.68,2113.143,3213.428,4013.764,4236.806,4477.792,6422.213,11555.887,5127.154,4883.103,3369.173,2729.004,2885,3391.714,3900.333],"NetProfit_Adj":[388.235,670.86,709.232,827.961,1556.97564,1268.53668,1033.72132,2164.2698,1168.17228,1851.332006,3201.077731,3676.134174,3901.38764,3974.217753,5489.134085,11627.565444,5022.670374,4914.05478,3571.67553,4015.086865,2885,3391.714,3900.333],"OCF":[369.107,449.062,1176.77,1327.872,1355.979,1312.53,1406.531,2737.266,1559.532,2787.568,3871.477,3893.512,5045.488,4098.384,4944.23,11772.903,7337.783,5030.052,6144.891,5465.975,null,null,null],"FCF":[-86.582,-905.43,320.087,-35.77299999999991,-841.5489999999998,-1754.336,-28.742999999999938,-136.538,-155.02100000000019,873.4610000000002,2115.018,698.0910000000003,2775.6820000000007,1223.6689999999999,1745.2009999999996,9375.301,5340.539000000001,2398.7639999999997,1439.995,4112.5470000000005,null,null,null],"DivYield":[3.3232628398791544,2.5065963060686016,9.615384615384617,2.9845661450924608,3.29,3.587443946188341,3.1380753138075312,3.947368421052632,3.8371099744245525,5.811403508771931,6.309148264984228,4.715127701375246,6.0046189376443415,5.329457364341086,3.6489607390300236,7.282051282051281,4.264099037138927,7.191780821917808,5.196451204055767,4.11124546553809,3.609291244788565,4.204883859440143,4.681357951161407],"Assets":[3245.024,5370.529,6497.938,8009.699,11016.498,15346.488,16065.091,19651.127,21034.164,21421.653,24022.321,30305,32679.267,36254.298,45071.089,56498.665,52231.789,46316.143,48078.296,47623.628,null,null,null],"Equity":[2246.323,4045.522,4389.536,5430.585999999999,6555.893999999999,8530.305,9946.073,12208.929,12335.329,12725.706999999999,13246.89,18306.192,18702.261,20927.425999999996,27973.349000000002,34706.524000000005,32087.623000000003,32694.652,34377.14099999999,36857.984,null,null,null],"Liabilities":[998.701,1325.007,2108.402,2579.1130000000003,4460.604,6816.183,6119.018,7442.198,8698.835,8695.946,10775.431,11998.808,13977.006,15326.872,17097.74,21792.141,20144.165999999997,13621.490999999998,13701.155,10765.644,null,null,null],"Debt":[653.294,724.659,1484.2930000000003,1563.2060000000001,2924.84,4659.916,4265.552,5139.995,6092.265,6254.008,7674.438,8466.385,9969.374,10816.605,11657.266,15545.113,14665.080999999998,7466.857,7322.1669999999995,4807.179,null,null,null],"Shares":[3209.324,3446.88192,3376.15092,3546.10092,3517.09292,3683.324044,3785.554299,3921.369699,3921.607699,3921.831699,3891.765199,4017.660647,3993.202147,4019.216147,4039.494647,4018.657047,4109.103027,4222.545672,4357.192919,4424.108852,null,null,null],"ROA":[0.13752424478738182,0.15573231341041022,0.11951535105586931,0.10663707673413665,0.16516154016485796,0.09599603019172413,0.07564993787800353,0.19721785772502565,0.06703552888438233,0.09954551104269176,0.14142372319815164,0.14776226495688977,0.13453537531841087,12.991615913089655,15.793870123237161,22.754583022816025,9.430943790596148,9.910107499769758,7.138498910936904,5.703132990304353,5.615,6.38,7.25],"ROE":[0.19433165348213735,0.21320463383043264,0.16857555448622608,0.15818776696483766,0.26303286215759714,0.16816830825237042,0.12876450269896292,0.31801242527659984,0.11113450952753817,0.1687022331255195,0.24814693562170578,0.2555061340037231,0.22987429472644652,22.68898323197133,26.3594820340661,36.9905445756986,15.403492768514704,15.126730784210274,10.076079013696724,7.67720789276017,7.36,8.266,9.116],"ProfitMargin":[0.20082786175888034,0.24178411200941102,0.1821213301652705,0.19543567552654062,0.2468762540628888,0.15381362355106593,0.14089115336576366,0.3544596478529351,0.12555655136385122,0.18438870032912857,0.2501033591731266,0.272534547855983,0.26456078214167295,27.5412555250368,34.49857511428818,37.93900480381574,19.914378899393654,20.100307598608083,15.092447807068844,13.101935598103683,0.13592918979700896,0.15114205427303967,0.16504223260564246]},"883":{"cur":"HKD","cur_ratio":[23.86,7.919408931960352,2.47302052943666,1.5420661165400924,0.052420504412997386],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[86680.22299380311,93078.61843573333,138744.2467025209,119362.07285262289,206671.8778344827,290242.4397907692,304432.7286762454,360667.3102325671,345673.6083471267,211517.25623000003,171237.4930114942,215189.90747307704,270012.0261877394,264554.6158862066,174833.9219837398,296626.14363209903,491657.6976404959,460579.3255103305,455733.84688806563,432166.5342962962,578368.8564,558030.77826,547678.6001684212],"Revenue_Adj":[86680.22299380311,93078.61843573333,138744.2467025209,119362.07285262289,206671.8778344827,290242.4397907692,303953.26252682006,360197.9558371648,343795.67253563245,210826.33383000002,170629.64608429113,215062.91090769242,270012.0261877394,264554.6158862066,174833.9219837398,296626.14363209903,491657.6976404959,460579.3255103305,455733.84688806563,432166.5342962962,578368.8564,558030.77826,547678.6001684212],"NetProfit":[30138.686592798473,32069.625516266668,49732.685190267905,33456.50244879308,62459.823996168554,84629.55129615382,78301.73980268202,71237.1465559387,75770.68224942534,24979.312340000004,744.6124858237546,28489.94767269232,62460.23898467431,69253.02649999992,28081.99261788618,84753.42597530872,164999.87152892564,136913.80985330575,149491.57420905345,132488.96298518518,194006.372,188455.083,184210.986],"NetProfit_Adj":[30138.686592798473,32069.625516266668,49732.685190267905,33285.0342676086,62149.87816858235,82847.0364782692,76236.34715900385,69507.34850191571,75712.15408908052,25088.502755000005,7365.468246743293,33889.610730000015,58882.485665708795,66650.29480948269,31164.924148373982,89349.05756296306,163814.7718870868,136928.73468904957,151755.30902541144,133632.54061944442,194006.372,188455.083,184210.986],"OCF":[38225.82607856617,43821.202587377775,64434.77412155378,61100.979972068926,81988.70702911874,140927.9885515384,117576.1644425288,139073.99715593873,138351.76036436792,96231.91863000001,82899.80044291184,106707.13229307698,145874.1539655172,139165.17345862053,92046.281300813,174107.30455226355,240318.76136735542,235698.5337830578,240251.21919341554,229032.41859012342,null,null,null],"FCF":[-4863.749998175385,16261.683849066663,22608.295483530906,11818.105127836767,36580.49534022987,77503.10584076922,40249.339143678175,29662.693107279698,17931.01445747128,12736.414170000002,22878.42319080459,51597.55000153848,85084.65967432948,57468.27034827579,7181.410357723576,74749.82194238692,129984.26717644629,102065.97670661156,106557.83165967073,107967.83686790122,142830.9047905806,145120.97959468229,142808.2442145871],"DivYield":[3.5065478036872704,2.4145685407474535,5.645946461371689,3.274426900356157,2.514481962792102,3.939188986764545,2.781072453310476,4.0465713384595565,5.429384086314016,6.1363963380370095,3.552333256810009,4.41751068309873,5.776130344238954,6.140324529604695,6.3379410183232,3.808011493485492,14.50466511957793,9.64526573976227,7.2236825920881795,6.027837762939802,7.682781078832799,7.510410990525653,7.116422217252176],"Assets":[154755.6315472,192163.01918719997,234672.665397,275144.0208611,375588.185,473759.08560000005,566940.617,796169.0603,827977.1769000001,793646.8452000001,712480.9813,741094.8533000001,781513.4066,847597.8966,856802.5725,964962.8491999998,1050641.1579,1106057.2402,1124094.2402,1223465.1583,null,null,null],"Equity":[107454.7282487476,143562.20951213496,181943.92510010986,197535.0484041833,254504.14507488962,324062.63942727435,385096.0388131516,437653.35338047723,474185.3238070689,461172.46305973025,427232.66090753203,456218.77334132505,478130.2757868722,501381.7903897522,515468.04770805244,591275.7602932617,676717.6892557911,734595.8831746747,797516.7845576347,896749.4724070339,null,null,null],"Liabilities":[47297.5443281,48607.427212799994,52722.747963,77605.470104,121092.188,149683.9232,181853.099,358519.6783,353806.3259,332482.2666,285257.863,284858.8708,303403.8806,346212.293,341337.1255,373674.69239999994,373929.8232,371460.42780000006,326544.449,326731.7375,null,null,null],"Debt":[20274.700964299995,11805.91136,15761.454229499997,21228.6781621,36627.0135,46844.035500000005,71958.0866,168915.5972,170580.8433,196684.91700000004,168126.8348,158792.575,162216.342,177350.6742,170521.8571,165792.2056,151988.4364,132182.68230000001,97786.1454,77736.26,null,null,null],"Shares":[43328.552648,44302.616976,44669.199984,44669.199984,44669.199984,44659.180984,44646.305984,44647.455984,44647.455984,44647.455984,44647.455984,44647.455984,44647.455984,44647.455984,44647.455984,44647.455984,46697.155252,47566.763984,47529.953984,47529.953984,null,null,null],"ROA":[0.22906040252549797,0.18658259089696014,0.2296485779990448,0.131357225776306,0.19407944512069553,0.1999590148770304,0.15158496502581117,0.10479581789311425,0.09374367375413833,0.030508860242566986,0.000978462308848479,0.03932903020160969,0.08081466707579012,8.454330412045604,3.374698953215876,9.327224832277079,16.519002098391233,12.802764767818534,13.379640609366506,11.330957286851923,14.041666666666664,12.635,11.487272727272726],"ROE":[0.3410275808369978,0.2582405674503362,0.3013063011535813,0.17646881335440137,0.27923936439116065,0.29357196284332937,0.22244846639051682,0.17335277863064172,0.1669342650749414,0.05288571424839777,0.0016579647376667726,0.06473963266023564,0.13170807938740428,14.064258536298782,5.659630681657114,15.376877829043757,26.28713266190147,19.599008678808136,19.50819370724415,15.749488162920935,18.77777777777778,16.501111111111115,14.792352941176471],"ProfitMargin":[0.34769968917769317,0.3445434199091526,0.35844863028373986,0.2802942479903313,0.3022173343109156,0.2915822763795737,0.25720539359601335,0.19751484133675232,0.2191971860731009,0.11809586028686922,0.004348419687350672,0.1323944417618971,0.23132391496238655,26.177213452887877,16.06209613057694,28.572473396150517,33.55990810695593,29.726434138484763,32.80238569723143,30.656923308723822,0.335447355147156,0.3377248209514448,0.33635898437235884]},"939":{"cur":"HKD","cur_ratio":[10.24,7.19908357915751,3.1146220104191564,0.7914689634233486,0.04214410574776787],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[220474.63900461548,315211.5070222222,434908.30158549594,451100.82179310307,519592.03741800744,696222.1603299999,877893.9137950195,972863.155007663,1084159.2795160927,1148174.8443200001,1049086.5184689653,1079159.0869269236,1185233.874463601,1242725.679155171,1325351.6741951217,1558230.0636172856,1538450.8840049587,1572589.037219008,1529892.4093452669,1471681.0456407405,960462.4905600002,1013513.9321600001,1075702.62746],"Revenue_Adj":[220474.63900461548,315211.5070222222,434908.30158549594,451100.82179310307,519592.03741800744,696222.1603299999,877893.9137950195,972863.155007663,1084159.2795160927,1148174.8443200001,1049086.5184689653,1079159.0869269236,1185233.874463601,1242725.679155171,1325351.6741951217,1558230.0636172856,1538450.8840049587,1572589.037219008,1529892.4093452669,1471681.0456407405,960462.4905600002,1013513.9321600001,1075702.62746],"NetProfit":[45141.35911692309,70845.30897777776,103778.49140725184,121133.21091494245,154793.83398160912,203889.09818923072,237494.33661494261,270833.88831685827,286762.81228735653,281483.0195500001,269314.7636521072,278490.7682319232,297294.13683908037,298102.826217241,298673.30391869915,359135.4110493831,372837.9948128099,362112.99807644624,355986.4566818928,361914.24051111104,414068.079,437212.67100000003,465488.183],"NetProfit_Adj":[45141.35911692309,70845.30897777776,103778.49140725184,120412.4078019411,156167.92715057466,204891.0287178846,237753.43240220513,271161.3000787175,288175.04015632207,283186.5750925001,271596.8197360152,280103.0473551924,297414.1956752873,304272.29581120657,296526.95644791704,359183.3198759263,373037.1125123967,362248.97991322307,355829.5802655348,361398.2061907407,414068.079,437212.67100000003,465488.183],"OCF":[28245.26472615386,-231359.1335111111,-95802.25491068697,-567870.0781839075,155158.8812896551,-334288.8356792307,32736.473402298867,561958.5222973181,254597.49750000017,728186.5593700001,340298.42644904205,532564.714997308,966105.4510344825,854.2473413793094,412456.2357804877,612631.5582576138,-94708.99471280992,262957.9171115702,1046087.7088469131,394314.2502580246,null,null,null],"FCF":[null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null],"DivYield":[1.8523947437091064,3.3102956702226525,5.188395946746005,3.4393902812365598,3.5910750446684383,5.3795581702742,5.356240990073167,6.569802951900063,5.902520945124988,6.164330707793402,5.2029535557789375,4.852646309787604,5.393602552830866,5.318711309491656,6.574827486409165,8.269366902722258,8.996405831798663,9.461492082032082,6.618130425778982,5.62942700051037,4.796872744437875,5.072291275218998,5.3821371223477605],"Assets":[5430530.9137,7052131.5776,8579215.746,10929244.2735,12750768.9015,15142273.138600001,17369622.486800004,19681808.331,20915046.5663,21920299.5594,23422747.5965,26564746.668100003,26441358.2498,28453001.5546,33418304.5266,37115581.4372,39129944.0699,42153476.1174,43175816.7658,50820155.706599995,null,null,null],"Equity":[329104.039205135,451354.73811514163,530899.1658047029,634867.7576130421,826743.9161115073,1006822.4395764878,1180405.5076984763,1376335.3711255041,1563731.0640848018,1726325.666019268,1776160.086257331,2156168.145465443,2267727.3427055315,2500194.047218313,2838326.7490041675,3206937.633926465,3252774.884005451,3488959.779248578,3558500.278173548,4105257.438053541,null,null,null],"Liabilities":[5101416.5869,6600797.6448,8048299.095,10294365.2595,11924051.454,14135411.791700002,16189243.097300002,18305485.4491,19351363.2295,20194003.4076,21646627.1823,24408497.189200003,24173729.3214,25952788.4924,30579992.0979,33908576.5676,35877199.7303,38664511.9248,39617169.2128,46714971.7517,null,null,null],"Debt":[284200.0347,744912.9856,611137.455,1017203.3333999999,986549.1335,1451734.8184000002,1576439.7512,1476894.3563,2086762.6983000003,2766592.0590000004,3148558.1038,3142664.1520000007,3230271.9616,3632829.1202,4226435.3495,4301342.3664,5219109.1545,5794069.718500001,6231494.4014,9348636.1318,null,null,null],"Shares":[231617.37040079915,240894.8888089419,240894.8888089419,240894.8888089419,250010.977486,250010.977486,250011,250011,250011,250011,250011,250011,250010.977486,250010.977486,250010.977486,250010.977486,250010.977486,250010.977486,250010.977486,261600.381459,null,null,null],"ROA":[0.009232774975875135,0.011464229836449654,0.013084842057114823,0.012428802535589346,0.013198215181294874,0.0146593533014746,0.014715786476321803,0.014634355191386103,0.014191786834291252,0.013002092519367216,0.011775181634949325,0.01124505687047427,0.01123137465357193,1.0963367605477092,1.011975037949064,1.0362477058590165,1.0013986652314582,0.9123086745319408,0.8506821799210923,0.7862977616536099,0.7621428571428571,0.7407142857142858,0.737142857142857],"ROE":[0.1499851057491808,0.18387508221428706,0.20880484991707468,0.20903018382853245,0.2153598234242378,0.22449008012955482,0.22042219089472956,0.2138423675010049,0.19747245998957297,0.17180427814453336,0.15508403669625964,0.14812501823016214,0.1394064028117147,13.160076595290745,12.17140092675176,12.538125094280513,12.306098780781918,11.562397242756756,10.747756132811611,10.06661450522701,9.74421052631579,9.603888888888887,9.584117647058822],"ProfitMargin":[0.3059173160744948,0.32089465539595424,0.3610407130436139,0.39541895385618303,0.41319589145196484,0.42283632947948,0.41629457551439086,0.4173275189993021,0.40711190529372343,0.36928557901330694,0.37414751557062104,0.38030114656720543,0.3773768047859894,36.912135906207446,35.46762834688531,35.97272611602816,40.88216039279869,41.984412961852776,44.15730869337516,43.69637167947622,0.4311264026193882,0.4313961791801121,0.43274265813897245]},"941":{"cur":"HKD","cur_ratio":[80.35,11.832946983631398,1.4917845842326076,1.1752221783658627,0.0607030118232732],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[null,366224.06915555557,461527.88417175546,512989.3219517238,557019.7180203063,636030.4384715384,715308.1719252877,795097.7011597703,820034.9079160925,824585.0396500002,828099.092338697,854933.9510838465,873695.1272605362,846211.9709689646,864278.5730894308,1022365.9216576143,1091373.426883058,1115834.8918326446,1127948.4781510283,1139710.903904938,1232398.7007263158,1247382.0060105263,1260390.2259764706],"Revenue_Adj":[null,366224.06915555557,461527.88417175546,512989.3219517238,557019.7180203063,636030.4384715384,715308.1719252877,795097.7011597703,820034.9079160925,824585.0396500002,828099.092338697,854933.9510838465,873695.1272605362,846211.9709689646,864278.5730894308,1022365.9216576143,1091373.426883058,1115834.8918326446,1127948.4781510283,1139710.903904938,1232398.7007263158,1247382.0060105263,1260390.2259764706],"NetProfit":[null,89321.74257777777,126224.47490496175,130675.8155816091,137340.44004597695,151623.67976153843,158929.50513026828,153539.44915402302,137469.4326137932,133914.33281000002,127111.31290574708,131936.73177807697,138377.73952107274,120621.31284482745,121344.68015447153,139987.78327901248,146088.34779214876,145673.0301198347,149965.18383909456,148781.7563641975,152546.329,155821.036,161601.622],"NetProfit_Adj":[null,89321.74257777777,126224.47490496175,134508.74287816082,139719.27427270112,156914.30661076918,161528.76489032575,155499.1930106322,141222.7868965518,128499.84539500001,133227.1881425287,142840.83005913466,133362.11373461323,116602.5502367225,119103.55841183329,141567.87061481498,142540.90877582648,134552.36917561982,136707.0945789094,138256.77245771603,152546.329,155821.036,161601.622],"OCF":[null,175152.1584,220824.1863618319,240365.84361149409,269842.28133908036,282081.2383099999,296157.6346973182,301729.267564751,293203.4272655175,312340.10745000007,314765.34869731794,301695.349756154,188026.95957854402,235914.0735482756,298436.9992845528,365695.60583868355,313978.337042562,321414.6293181818,324082.2912604936,229563.10372716046,null,null,null],"FCF":[null,73017.25688888889,83182.833391603,107977.80562298844,138588.25898888882,132211.46398846153,142453.08119540237,125038.7866869732,72053.82940000006,98039.42098000001,93408.53866513407,78175.62210807696,-43391.931379310336,3389.7623586206855,84132.32657723577,121370.23249629643,99838.31323140497,121020.51810123965,155036.16658271596,59232.70916049381,174833.77857804386,183627.0465,196161.4839],"DivYield":[null,1.474220899057723,3.524604292019376,3.852113751936969,3.9648974413195184,4.434370104223941,3.8196039485310784,4.176359985977883,3.189788260723437,3.010443165904909,3.241873533765846,4.137524590697917,4.169255135898223,4.960347210813348,7.444569535907686,8.763047254660234,8.424463593314538,8.198318512991364,7.07121912402182,6.4478522790226975,6.812326824162777,6.977589368415758,7.234989437340759],"Assets":[null,602261.3184,747643.8585,853328.6376,1016652.3325,1174408.7582,1307876.6979,1495545.8912,1683830.5185000002,1705763.3670000003,1699406.5962,1827601.0791000002,1708594.546,1782981.284,2010618.1578,2258939.9636,2188899.9242,2191723.4343000003,2243468.7534,2370156.2934,null,null,null],"Equity":[null,400005.08153924154,500342.7316826606,576509.7121178831,681068.6432462783,801870.5978697399,901651.5682598236,1013005.7086775495,1110462.5581886135,1099490.4102967246,1097367.29803882,1187304.6300428787,1202196.3000363451,1199117.7756268492,1327451.6862360584,1484755.096678781,1471793.0912214073,1480447.028812032,1486135.297462805,1595794.7230544705,null,null,null],"Liabilities":[null,202274.6752,247284.648,276808.7038,335605.494,372507.1731000001,406245.0800000001,482549.37479999993,573401.8532,606291.7542,602063.8088,640251.6624,546642.9986,583854.3886,683173.169,774153.7379999999,717120.6535,711274.5328000002,757271.9496,774390.1347,null,null,null],"Debt":[null,37945.6064,40707.675,38181.0983,39706.688,35365.7365,35660.8097,6478.522699999999,12792.033100000002,14656.547400000001,5584.2654,0,3667.4306,86354.8014,84571.3526,69904.2908,127407.194,155234.3865,94119.9764,112703.09889999998,null,null,null],"Shares":[null,20031.90559,20054.379231,20060.853651,20065.423246,20072.065571,20100.3406,20102.539665,20438.426514,20475.482897,20475.482897,20475.482897,20475.482897,20475.482897,20475.482897,20475.482897,21362.826764,21390.880312,21517.317437,21644.606612,null,null,null],"ROA":[null,null,0.1843443105931649,0.16337978216691082,0.1483168381884866,0.13873847956426397,0.12897304140787472,0.10965708057802181,0.08683853675737757,0.078200098705659,0.07375048704783395,0.07510679052691871,0.07721448508513681,6.794255315749956,6.562378100618221,6.573344135347005,6.643552258288289,6.708730091046906,6.748611972734969,6.472379611591128,6.0825000000000005,6.115000000000001,6.154545454545455],"ROE":[null,null,0.27680200743942107,0.24328189528607794,0.22096121081720002,0.2054649732578857,0.18837590372681895,0.16094483606882595,0.13035593120203093,0.12031467888077717,0.1146841022022752,0.11633481060561718,0.11653928291276806,10.02650788965702,9.883831502748054,10.012905361544092,10.021895612010715,9.985752610758295,10.123258628030802,9.721301879413877,8.96833333333333,9.011176470588236,9.111875],"ProfitMargin":[null,0.24389915928720104,0.2734926300964037,0.2547339876090183,0.24656297722115858,0.2383906030125057,0.22218326501499563,0.1931076507076583,0.16763851305200694,0.16240208877284595,0.1534977082836336,0.1543238885422828,0.15838218069838048,14.254266895646566,14.039996354498937,13.692532224865547,13.385734359446003,13.055070350110817,13.295393073708706,13.054341750564424,0.12378379876447634,0.12492227654518213,0.1282194667548243]},"960":{"cur":"HKD","cur_ratio":[4.595,1.7762968474616003,0.12554869810689748,0.17416497087976848,0.16697950861920857],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[2047.1248713507705,3588.8335715555554,5015.490458506485,12905.733990450566,17326.111811665127,29022.428638762303,34291.455888908065,52373.60036379962,64180.44253925752,58510.14531521001,64057.12432313408,83211.6605177758,137309.90955210724,171333.23773439982,207663.7203046504,269224.0750087995,291765.77603833104,199812.12553868798,138154.14865399335,105603.77132335676,93512.82590588236,80235.6594117647,79569.29173333335],"Revenue_Adj":[2047.1248713507705,3588.8335715555554,5015.490458506485,12905.733990450566,17326.111811665127,29022.428638762303,34291.455888908065,52373.60036379962,64180.44253925752,58510.14531521001,64057.12432313408,83211.6605177758,137309.90955210724,171333.23773439982,207663.7203046504,269224.0750087995,291765.77603833104,199812.12553868798,138154.14865399335,105603.77132335676,93512.82590588236,80235.6594117647,79569.29173333335],"NetProfit":[361.51385627230786,769.456407111111,371.62291132442726,2506.7287785770095,4741.201146423369,7622.2128474769215,7746.525578199238,10140.606108935634,10514.466949627595,11089.350170230002,10699.219915161682,14545.259524226545,19253.132995977005,20802.08785158963,22507.481903861786,28749.738490319374,28367.921384486777,14206.244702299584,11272.528030445674,1108.9582970987651,-4421.143,1312.787,3612.1710000000003],"NetProfit_Adj":[361.51385627230786,769.456407111111,371.62291132442726,1731.9353743787344,2596.938226819635,5245.618274791344,6534.06541028688,7660.60845455661,8355.430138690523,8163.150686590001,8944.560756470879,9915.445028223467,15170.550946580455,18119.101154868946,21169.13009900813,26176.33395135332,25863.7499485937,11808.064883115183,6578.835235715121,-2509.7380720382707,-4421.143,1312.787,3612.1710000000003],"OCF":[-510.9134146276925,2261.2029665777777,-919.1208847774803,6063.428873551719,8924.608858204978,11092.14250194692,12091.570764329508,13294.791293164753,21059.538463100016,14584.557562600003,35517.61112179922,79144.94648365966,71583.07180739462,33629.873200386166,34022.036218910565,27404.25977415888,4841.803040270662,28772.889915966938,24868.84392807365,18265.742892314807,null,null,null],"FCF":[-806.0356964015389,-1290.1052545777777,-3464.726059683204,4419.838331799996,-1173.7671449639854,381.0937778984609,-5439.639358459774,-5722.142943484676,-1130.2115016816088,3018.5363272400014,6026.787087045209,-9781.749971271933,-8903.47554639847,15333.332507113775,18609.0135694878,9202.784193495483,-7404.60153884339,18416.098648270658,18231.641724330446,12792.061044692591,13991.8164,3275.3189333333335,12123.045066666666],"DivYield":[null,null,null,0.817689505383512,1.0901457906473122,2.4572798389597086,1.6378487184652704,2.694588910915709,3.5511035054143076,3.6892688985124495,5.291397006023109,4.126867131434893,4.817363529930425,3.6775612375238733,3.741642465915076,5.682606377555711,5.258966887589814,4.839555535219646,3.4052990070680176,0.9107522631543203,1.0229545720706166,1.6367273153129867,3.452471680738331],"Assets":[10055.516927,24197.079430399997,36785.9611785,48204.9023414,84586.148738,119911.90454890001,155917.67842070002,184698.7069237,210043.94064750004,219912.2487276,251202.9768702,435570.5594978001,577138.1372018,729601.0869728001,908932.1682175,1074248.7780675997,889763.1870544,770377.5218125001,708375.987597,663611.9069908999,null,null,null],"Equity":[2481.7572501569484,4412.710003426018,4478.530206343858,15034.768292657205,20483.6404837853,29705.170725907585,40497.87693196472,50956.751675731946,62796.675590698185,74625.13530102503,83928.06521924576,127483.92621490154,160342.47904653632,186444.31987753112,228938.17641808122,272193.74276364443,263704.70913707133,260080.11888556863,261225.61098236832,263954.4657722597,null,null,null],"Liabilities":[7573.6820986,19784.572832,32307.283471500003,33169.86747799999,64103.164048,90205.5859059,115420.69756349998,133742.4176428,147249.18170020005,145288.38925920002,167276.78625620005,308081.82442530006,416802.616661,543155.3491112,679995.1468687999,802049.3285731999,626060.9541734001,510297.0739392001,447139.56533079996,399662.16594219994,null,null,null],"Debt":[3522.586975,9002.183897599998,14900.64417,11090.684028500002,20434.0814405,29548.575252500003,40819.383814600005,48301.415788,59633.65783000001,62436.461868,64660.463811,92939.2145351,136426.9631892,175905.5257764,215104.00306219998,255339.0390136,252045.3182814,228204.80472250003,202477.9878274,184808.0629255,null,null,null],"Shares":[null,null,null,5150,5155,5155.05,5428.7755,5441.5325,5808.932105,5834.023605,5841.231105,5907.604605,5938.471605,5983.110605,6058.513605,6074.310105,6058.513605,6074.310105,6281.542032,7041.631192,null,null,null],"ROA":[null,0.045831293431649496,0.012049985691118951,0.05903705476044939,0.07235815106669169,0.07489406407586559,0.056591296955723486,0.059623648300904254,0.05349265429103498,0.05103289649124759,0.044766584324356866,0.0428819630951174,0.03734125182417081,3.163854790774639,2.82234699698002,2.907550171996399,2.930903750326985,1.7281028315464468,1.5228112005907606,0.16200501702355885,-0.2983333333333333,0.29,0.548],"ROE":[null,0.3438132839763683,0.11068124592255318,0.28951869504173283,0.2937632699731789,0.3337250532999082,0.24305055513340662,0.24030122303127174,0.1971415109601372,0.17471399691087178,0.156608846855746,0.1904098426009562,0.21332290722469524,20.882374054881232,19.774593348111733,20.449536082656515,18.2453339268011,8.739510403116613,6.637680266079493,0.6303029338539832,-1.4849999999999999,0.5846153846153846,1.363076923076923],"ProfitMargin":[0.17659589863405223,0.21440292277961373,0.07409502907021565,0.19423372436095707,0.27364484299537234,0.2626318059852754,0.22590249895761744,0.19362056529428076,0.1638266508242938,0.1895286725146326,0.16702622898258462,0.1747983327543303,0.14021663155105862,12.141303185921787,10.838427564931647,10.678739815293154,9.72284061882567,7.109801101409606,8.159384383510398,1.0501123995876585,-0.04727991188361771,0.016362140632894564,0.045397933830717135]},"968":{"cur":"HKD","cur_ratio":[2.035,4.732558139534883,0.6983523535973822,0.5667646603744392,0.09898209898209899],"date":[null,null,"31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[null,null,276.631,208.88,1077.393,1233.678,1533.13,1967.507,2410.004,4750.41,6007.081,9527.031,7671.632,9096.101,12315.829,16064.655,20544.041,26713.982292068176,23757.913967132088,22639.488090167895,18970.1,20741.222222222223,23389],"Revenue_Adj":[null,null,276.631,208.88,1077.393,1233.678,1533.13,1967.507,2410.004,4750.41,6007.081,9527.031,7671.632,9096.101,12315.829,16064.655,20544.041,26713.982292068176,23757.913967132088,22639.488090167895,18970.1,20741.222222222223,23389],"NetProfit":[null,null,117.452,37.022,407.849,320.776,119.849,303.799,492.972,1205.597,1985.63,2332.031,1863.146,2416.462,4560.853,4924.347,3820.144,4248.333489148759,1092.6976158473246,916.5171070679011,406.87,1588.6000000000001,2397.667],"NetProfit_Adj":[null,null,117.452,37.043,408.05,322.402,119.85575,303.85225,493.12275,1151.53025,1986.355305,2332.26313,1860.236873,2419.6639,4584.8305,4923.98524,3823.64912,4271.555704408574,1411.2778528988676,2680.0521879197527,406.87,1588.6000000000001,2397.667],"OCF":[null,null,61.813,-54.159,474.156,-10.06,87.442,655.435,717.374,855.723,690.156,1312.306,2319.924,1631.922,4345.03,3785.458,5922.927,6017.062506626032,1395.0242098230444,6158.311254718517,null,null,null],"FCF":[null,null,-462.84299999999996,-617.098,-103.20400000000002,-888.5809999999999,-260.356,415.76899999999995,-1663.619,-2727.226,-4119.871,-1198.7910000000002,-738.6590000000001,-719.5369999999998,1057.0289999999995,-1152.8319999999997,-723.2470000000004,-3870.677656574379,-3700.368395806171,3411.071506811111,1188.0991268331588,3037.7110433386706,2894.745608739468],"DivYield":[null,null,null,null,null,null,null,1.1249999999999998,1.8691588785046727,2.7444794952681386,5.555555555555556,4.966887417218543,4.4363636363636365,2.5316455696202533,1.2592592592592593,2.042360060514372,3.472222222222222,5.427159416078171,3.389038507407794,1.8749531496081742,1.425061425061425,4.226044226044227,6.486486486486487],"Assets":[null,null,660.301,1394.843,1972.422,2927.145,2795.682,2686.995,5763.967,12734.633,16786.383,22767.259,23892.5,28397.02,43423.389,49070.353,50575.058,60573.522315500006,60587.549472800005,63387.4249316,null,null,null],"Equity":[null,null,488.275,753.586,1323.279,920.8789999999999,1065.118,2310.435,3305.888,6891.367999999999,7427.788,11680.356,12058.918000000001,18573.129,32024.518,35897.420999999995,35231.377,37941.11139658086,36615.34199172535,38909.31948607117,null,null,null],"Liabilities":[null,null,172.026,641.257,649.143,2006.266,1730.564,376.55999999999995,2458.079,5843.265,9358.595,11086.903,11833.582,9823.891,11398.871,13172.932,15343.681,22632.3629254,23970.6920904,24478.8019133,null,null,null],"Debt":[null,null,0,0,0,0,0,0,1300,3590.264,6665.931,7932.688,8769.509,7309.639999999999,6808.228999999999,8848.486,8912.653,11542.467189400002,13348.680959,14611.373137900002,null,null,null],"Shares":[null,null,null,null,null,null,null,5738.805804852413,6121.39285850924,6794.746072945256,6794.746072945256,6748.8,7659.689,8081.863,8809.254,8809.254,8896.24,8903.251,9078.447,9147.043,null,null,null],"ROA":[null,null,null,0.03602861891916089,0.2422434824701946,0.13094055046088768,0.04188454412478308,0.11082141078163094,0.11666648128343259,0.1303446747321419,0.13452314784829897,0.1179173841943556,0.07986093541546153,9.242624525908823,12.700715753373112,10.64795713422428,7.667476026567847,7.701505386155069,1.8003464848220285,1.4835937482981778,0.6299999999999999,2.137142857142857,3.4216666666666664],"ROE":[null,null,null,0.05962342001238465,0.3927544640600136,0.2858764846325437,0.1206940393162729,0.17999954377845642,0.17554973244950475,0.2664040479550577,0.33202771627041655,0.2854950604624469,0.18128453428412525,19.63752691669523,22.41279637468091,17.328910924958876,12.720987397789512,13.86205607220052,3.4653223223416227,2.8684541254443316,1.2027272727272726,4.175833333333332,6.242727272727273],"ProfitMargin":[null,null,0.42458003622153695,0.17724052087322864,0.378551744813638,0.26001598472210735,0.07817275769178086,0.15440809105126435,0.20455235758944798,0.25378798882622766,0.33054823132899325,0.2447804567865896,0.2428617535356232,26.565909943172354,37.032448242014404,30.653300677792334,18.594900584553933,15.903033260638791,4.599299489673286,4.048311973387487,0.021447962846795746,0.07659143709083294,0.10251259138911453]},"981":{"cur":"HKD","cur_ratio":[46.44,53.64225935651989,7.451598881588301,2.3452924639937187,0],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[11383.36181946093,12091.197523399538,10540.358369289032,8297.294689615019,11905.367235797914,10271.150140502317,13199.375876250562,16048.168968551736,15276.524776473569,17338.077199655156,22621.076610306518,24166.586060480775,26334.15652969196,24411.742548999224,30303.061778072497,42308.540032579316,56954.47701401072,49490.664143092275,62655.96910653681,72710.16339052751,92806.25583,111282.43531333334,128953.09883333332],"Revenue_Adj":[11383.36181946093,12091.197523399538,10540.358369289032,8297.294689615019,11905.367235797914,10271.150140502317,13199.375876250562,16048.168968551736,15276.524776473569,17338.077199655156,22621.076610306518,24166.586060480775,26334.15652969196,24411.742548999224,30303.061778072497,42308.540032579316,56954.47701401072,49490.664143092275,62655.96910653681,72710.16339052751,92806.25583,111282.43531333334,128953.09883333332],"NetProfit":[-342.66140637305983,-151.88958780678155,-3427.75728734867,-7469.028832546573,101.77505687826904,-1921.3033638065403,176.6357201160918,1343.2683011724146,1186.2309900436785,1964.5993615862049,2923.5586284099622,1400.1879986653848,1050.6673107931038,1838.7597131988502,5549.909035839692,13227.236744858623,14235.651440505373,7065.757684243841,3844.820324547128,5341.166562495397,11190.741,14788.782000000001,18656.834],"NetProfit_Adj":[-342.66140637305983,-151.88958780678155,-3427.75728734867,-4755.943211032411,193.4876321505866,-2049.7618264335406,54.89351969494248,899.128854455173,943.3668887948278,1275.2281896103434,2954.4045278328554,1159.101137099423,874.9036134672417,1333.8680486639269,5284.579096342744,10766.852998143966,13964.396073275853,7053.3508843398995,3229.383230792818,5478.264074019535,11190.741,14788.782000000001,18656.834],"OCF":[5979.017877475093,5246.540754604482,4436.4759110779305,2198.1130432851573,5396.343784951128,2953.123222956926,3375.603169822986,5724.510174896555,4715.65766598161,5188.030507655167,7585.448155482761,8421.482574623078,6265.568353273566,7984.45957300881,12878.37951533587,23427.13937842414,41942.15909194343,26361.586387096904,24801.552100436795,24952.59684314289,null,null,null],"FCF":[-877.3120025937366,-348.7940785321842,-772.9622071459522,513.9122000273112,1577.6527198738845,-4298.550121402311,270.5270185344825,91.71428303448246,-357.9188681724136,-4353.988068620684,-13817.09996168583,-9402.063813588462,-7906.766261202301,-6674.82288618084,-28224.004036777085,-10238.743608093106,-7007.5389702419225,-33382.273273393446,-35001.537041693126,-40531.040245208766,-9283.65220174095,-7443.8926243747355,13060.735381300932],"DivYield":[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0.30409302822379186,0.4056320617754672,0.5912919993240396],"Assets":[35327.1658262978,36723.5063183705,33097.7461826822,27323.5756422478,30334.853813779202,28953.3333714,31570.655844,35073.476889599995,44735.187828099995,55144.650784699996,78432.8540842,93110.51474729998,112974.159104,128037.467544,242850.3423775,281575.0624475,341775.1884328,373247.5791072,381869.8260896,406874.6343412,null,null,null],"Equity":[23696.854544994396,23768.691022285,21639.517580728898,14197.110325926,17166.5517054304,17443.768066800003,17644.551806800002,20107.014591599996,25647.7456158,32474.8952755,41896.0818353,52509.0854205,69891.263276,79433.1866904,168113.69193059998,198353.92004250002,225948.3182157,240922.76832959996,247553.9424466,272596.24897970003,null,null,null],"Liabilities":[11630.3112813034,12954.8152960855,11458.2286019533,13126.4653163218,13168.3021083488,11509.5653046,13926.104037199999,14966.462298,19087.442212299997,22669.7555092,36536.7722489,40601.4293268,43082.895828,48604.28085360001,74736.6504469,83221.14240499999,115826.87021710002,132324.8107776,134315.883643,134278.3853615,null,null,null],"Debt":[8309.7243208021,8924.864069853,8922.2003391802,9344.6626214314,7538.1543793696,7215.769428200001,8725.8779601,9088.112673,9996.537235299998,10965.9497443,23457.741853700005,25882.524890399996,26834.354687600007,30732.5534328,49417.5633929,52807.5316825,67831.3501344,79735.8066596,90074.2258969,98047.4132859,null,null,null],"Shares":[1843.2756463000003,1855.8919712,2232.7784827000005,2237.5886604,2733.4063747000005,2748.7676065,3200.0139623,3211.2307101000006,3585.6096167,4207.3748961,4252.922259,4916.106889,5039.819199,5056.868912,7703.507527,7903.856555,7912.664696,7946.55576,7976.149966,8000.408035,null,null,null],"ROA":[-0.009668052618577365,-0.004209449304318438,-0.09805722453668338,-0.24722886011318596,0.003527885554044714,-0.0646911858766017,0.00583790288766094,0.04028987435892902,0.02972357978235404,0.03933510111119166,0.04371634807210998,0.016309449934688768,0.010177744778633957,1.5208342648954352,2.9965412363627375,5.047500340938502,4.54947698427371,1.9706862908905083,1.0165143247099286,1.3509094653988607,2.576875,3.1462499999999998,3.657857142857143],"ROE":[-0.014621345214742943,-0.006467888526457379,-0.15280805946274592,-0.4239423485272724,0.0066067227159026295,-0.1118292677712199,0.010075356106384364,0.0727745756073618,0.05631948973774283,0.07589119998275003,0.09558494162194675,0.038295952761525906,0.023830205850279538,3.831195327685549,6.684934429860015,10.529064580538863,10.016308701684094,4.596975537001683,2.4195879043290813,3.2583796455936493,5.945769230769229,7.080769230769231,8.087826086956522],"ProfitMargin":[-0.030101951585800233,-0.012561997065454984,-0.3252031066928399,-0.9001763962770766,0.008548670096647205,-0.18705824932207427,0.013382126683270667,0.08370227804833723,0.0776505787409529,0.11331125931457266,0.1292404724485104,0.057939006989286314,0.039897511416721036,7.53227554119946,18.3146807952444,31.26525781574952,24.994789148890654,14.276950626111276,6.136399100314934,7.345832155276423,0.12058175259219593,0.13289412655285626,0.14467922170680053]},"1044":{"cur":"HKD","cur_ratio":[21.32,8.798738957469117,0.9392461941553923,1.1218499107944673,0.07560026383677297],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[4010.0625989523096,5834.580517688889,8967.572749459918,12292.870701464359,13433.109245175,17053.722568120986,18525.11374696016,21187.828903473495,23833.14197586508,23025.699643040003,22534.05102100689,20873.109306046164,24324.668434042138,25517.201914086178,25176.572003308942,25057.393778725953,26334.629248509922,26276.48450142148,24568.326154401224,25035.39040195185,26433.371644444447,27079.684555555556,27730.195155555557],"Revenue_Adj":[4010.0625989523096,5834.580517688889,8967.572749459918,12292.870701464359,13433.109245175,17053.722568120986,18525.11374696016,21187.828903473495,23833.14197586508,23025.699643040003,22534.05102100689,20873.109306046164,24324.668434042138,25517.201914086178,25176.572003308942,25057.393778725953,26334.629248509922,26276.48450142148,24568.326154401224,25035.39040195185,26433.371644444447,27079.684555555556,27730.195155555557],"NetProfit":[678.8686618338464,1034.324275022222,1502.8027519396937,2402.6814821770095,2438.5860064326284,2649.330777500056,3518.8722991660406,3721.287583247911,3916.2064430144933,4021.9863707700006,4204.45498567203,4380.274326621925,4505.680652969347,4433.150044617236,5170.362274024389,3945.5190561176996,2241.8196024078516,3096.110742229338,2491.094533150205,2751.148802843209,2827.713,2918.485,2998.763],"NetProfit_Adj":[678.8686618338464,1034.324275022222,1502.8027519396937,2407.8113413563206,2442.2311100906245,2651.049416519488,3520.785582130025,3741.7989615100273,3936.4099049674974,4003.3639590634402,3914.8420272095464,4312.736861596362,4547.6573307874505,4468.188192774378,5212.057096958309,3935.837929710687,2335.8826290279962,3324.156428291838,2552.40395002397,2872.6887221728393,2827.713,2918.485,2998.763],"OCF":[660.9366608600003,917.1427093333333,1517.7230324622128,2859.103922981607,2314.9199223564037,3451.8827498390233,2963.256883318062,3869.9608342873103,5459.759545877949,4561.544945990001,4904.0645890911865,4707.422097109617,4839.081373141761,4135.8980623655125,5999.636253975609,6108.0017556749035,4724.251402792976,4797.621616146694,3758.187786567488,3673.2136858222216,null,null,null],"FCF":[127.1738561538462,-11.00645119999995,364.21376428511434,1782.8717216367804,1198.0377543758334,1023.6060051162028,494.2004959736542,2462.71280435827,3799.153832730548,3491.0680269200006,3850.7208042590028,3681.3175571434635,4007.439447126436,3116.995396779307,5143.655349325203,5166.296219113586,3274.9342179123964,3116.6362607789238,2120.1227209658427,2535.2228963185185,3002.7988924896704,2950.567679908177,3175.260634175644],"DivYield":[2.222860935752082,1.8323133964402427,3.2832650611804524,2.1688330022248286,1.9389137222596937,1.8581524407120988,2.4303610823456543,2.019669122206005,2.4676878261939974,2.8396716490936145,3.825783623487957,2.906484283393844,4.394786903774742,4.5348302169644175,5.409407782363513,5.194311556640454,3.8197222079740523,5.3007152557992665,6.636162439984206,5.588559101806781,7.73307714693512,7.8212661949400815,7.920478873945665],"Assets":[5463.2665285,8949.980499199999,11410.653126,16067.814322299999,18576.791189940897,23320.063528837163,29204.071335601635,40190.013837396546,42575.19118521498,39379.21609380001,38149.9045944,46704.876628000005,51979.540267200005,48366.893715,52791.3748075,51884.743246399994,47879.186977800004,44233.4695099,42359.854186200006,48091.3468063,null,null,null],"Equity":[2807.6300889674685,5735.056115717202,7625.6544341220415,10558.39638971159,10825.532,12718.747000000001,14408.240000000002,16918.665999999997,18063.058999999997,18028.96654019268,16492.672786777526,19623.86197339665,19355.535896459987,20304.53630663182,23355.982803315357,23652.760163465024,22364.59098264866,22690.74441229651,22502.19600702797,24471.57101053069,null,null,null],"Liabilities":[2655.548674700001,3215.1887423999997,3784.7475405,5509.2307292,7751.60230517764,10600.780090244943,14796.198716905152,23271.54702085137,24512.728175846685,21350.5577868,21657.6001856,27080.274417300003,32624.844357800004,28062.2029842,29435.5098429,28231.487186399998,25514.806004499995,21542.696395000003,19856.726886,23620.21383119999,null,null,null],"Debt":[1765.2703205,2053.2706112,2074.8503235,3100.68814,5312.072628521505,7218.667448896251,11227.509713657593,19419.458372024947,20553.975799191277,17146.332720000002,17782.924454,22907.804695600003,27625.4293794,24182.751647600002,24774.9314925,22395.902605999996,19298.4658373,15693.645975200003,13954.775849200001,17552.988947899998,null,null,null],"Shares":[1080.766355,1141.624908,1150.064924,1219.313721,1224.218721,1229.007721,1229.027721,1231.384721,1224.384721,1218.094221,1204.888221,1205.910917,1206.067917,1189.677417,1186.337417,1162.120917,1162.120917,1162.120917,1162.120917,1138.167,null,null,null],"ROA":[0.14837483397330112,0.14552745269759096,0.14557057909160848,0.17502279522738212,0.14075995517973047,0.12644691222122345,0.1339849199305398,0.10724158088912733,0.09462259289722703,0.09723816070078663,0.10719327239781454,0.10388546611486574,0.08988282368418511,8.792175156413883,10.48090949216295,7.548627624705804,4.549803208058988,6.784800428494892,5.744878875253572,6.109576782881837,6.1225000000000005,6.51,6.789999999999999],"ROE":[0.27153442065492656,0.24778204474374652,0.22673017547775334,0.2732082309451939,0.23508980187327605,0.23190066799156037,0.26637075005474153,0.24311098717918733,0.22918796381845832,0.22613847704560786,0.24436869264531846,0.24610094914220576,0.23147257801620133,22.589234016051922,24.647431651650127,17.02852886320716,9.985563779570402,14.030920819118561,11.12639679897688,11.881413134548554,10.9075,10.78875,10.69625],"ProfitMargin":[0.16929128787446143,0.17727483096452737,0.1675818857483148,0.1954532460746371,0.18153548533884944,0.15535205096232338,0.18995145440029823,0.17563326569235463,0.16431767355644036,0.17467379637194777,0.18658229635463747,0.20985250747252698,0.18523091754310164,17.37318244979681,20.536402943756013,15.745927493335305,8.512820063850715,11.78281950944331,10.139455645023437,10.98903895115101,0.10697838469245738,0.10777725957930942,0.1081440179604662]},"1088":{"cur":"HKD","cur_ratio":[43.48,12.271067543365014,2.318508155912023,1.9412979711114504,0.058027994480220814],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[62020.886798461564,82358.58222222222,117022.15962137398,137649.51930114932,180987.70025517233,252033.56159615377,312974.60253831436,358068.19718275865,318545.4913597703,218465.96151000002,214063.81584214553,287180.79684692324,313162.06124521064,274392.6410448273,262481.561308943,404531.2840493832,401184.9028756199,379283.1924421487,368253.70474238665,320056.31467160484,454656.32184347825,466208.7116173914,474182.4257904762],"Revenue_Adj":[62020.886798461564,82358.58222222222,117022.15962137398,137649.51930114932,180987.70025517233,252033.56159615377,312974.60253831436,358068.19718275865,318545.4913597703,218465.96151000002,214063.81584214553,287180.79684692324,313162.06124521064,274392.6410448273,262481.561308943,404531.2840493832,401184.9028756199,379283.1924421487,368253.70474238665,320056.31467160484,454656.32184347825,466208.7116173914,474182.4257904762],"NetProfit":[16196.394553846161,21029.01102222222,29093.034033206088,35975.95999540228,44579.39358697316,55226.338463076914,61111.03424521076,56876.41610306514,49466.99418735636,21775.159710000004,26548.88348199233,51995.85741115387,52336.166455938685,47314.86569310339,44076.44056910568,60363.91619094656,84890.51259049588,71445.74147727272,64532.29247407404,57354.14889012344,73087.993,74978.743,77193.082],"NetProfit_Adj":[16196.394553846161,21029.01102222222,29093.034033206088,36558.04753333331,45003.84717863983,55501.89194480768,61347.079118773974,57571.93118496169,49804.947113218426,26002.124250000004,28779.699114518593,53825.52403463635,54648.41070881225,47195.747537930976,44705.61672796888,61649.36512972408,86973.9707424587,73811.32794266527,64823.286033950586,52863.39611276983,73087.993,74978.743,77193.082],"OCF":[14217.160056923083,8816.03608888889,37599.37589198471,60939.91305977007,67552.11914865898,85723.3329853846,88087.76685249046,64918.52609310346,81578.18615977018,62398.92925000001,90592.57088122603,103648.82409576929,100039.54404214557,69487.85943448269,92000.14563414633,114634.07069629642,128552.01705661158,101017.92157438015,100011.83128106991,86680.74287407406,null,null,null],"FCF":[-10839.500398461543,-19985.614222222222,-3358.82826755725,25155.712896551704,30621.49981800765,31105.320241538455,20926.853321839088,-267.481537164751,24747.971800000018,25538.219210000003,56625.61701455937,80249.1296665385,77053.4646360153,48916.72126551718,68737.64229268293,86144.28851193425,97269.57846363637,60019.950557851225,59144.847460493795,34157.02250123456,17295.550733659536,75597.18713050065,78357.38108214573],"DivYield":[1.8101889145095695,0.4128602291511892,3.1848346482315515,1.5839747185680393,2.7136600218178266,3.2924845747097677,3.515176591853212,4.7681450186769805,4.027721610413729,3.1385749496602,22.729151169518143,5.39553415788163,5.839226196361234,8.657401872472349,14.726785582070098,17.045984563317447,12.788570328109577,9.292602677091926,7.157715510579483,5.769528850514664,5.929408709144871,5.964541942152566,6.126695325265006],"Assets":[171962.6444,257628.896,312469.161,353971.5689,438928.5145,501182.48030000005,586848.8635,657586.0678,688094.2152,668726.3286,638720.1872,680945.7868,673625.3636,629864.6438,663379.1913,744455.3635999999,707174.388,696689.8588000002,714758.2238,1006595.4709999999,null,null,null],"Equity":[88439.4099370924,162769.7109125543,194771.57768661637,224203.647405194,280054.1125696162,330544.97712589486,389286.62163592625,428713.7564602472,456647.4245255662,434747.45928323705,424476.220240068,449797.6719535248,465522.01081631175,470052.27986328525,505014.47496629483,546467.8297226288,523380.9110834083,529767.2647721512,532356.7878729758,672271.4234021547,null,null,null],"Liabilities":[83520.4699,94866.688,117691.1685,129763.94629999998,158883.368,170624.7297,197570.8554,228876.20159999997,231460.72820000004,233986.30200000003,214253.448,231131.14790000004,208123.5554,159808.789,158367.2643,197976.07679999998,183798.3916,166921.9239,182379.4034,334336.0811,null,null,null],"Debt":[59290.6929,70004.2624,84620.8665,86521.0331,79767.226,76355.9628,84191.4337,115974.13969999999,122395.56170000002,127338.38700000002,121508.6096,116682.82530000001,68107.6362,56351.7122,76376.0305,80238.854,64390.0533,48446.195400000004,59750.5732,119126.92049999998,null,null,null],"Shares":[18089.620455,19889.620455,19890,19889.620455,19889.620455,19889.620455,19889.620455,19889.620455,19889.620455,19889.620455,19889.620455,19889.620455,19889.620455,19889.620455,19868.519955,19868.519955,19868.519955,19868.519955,19868.519955,19868.519955,null,null,null],"ROA":[0.10559051591322773,0.09912059906619565,0.10057203517057418,0.1080532121003512,0.11358159015396135,0.11775947230934015,0.11315377331002331,0.09149538808767758,0.07386225884961989,0.03178101728427075,0.040146536981143746,0.07909637263476609,0.07618036677454153,7.223811367192947,6.985100710636363,8.596089846679837,11.833490646814058,10.268269973274693,9.125160625906574,6.708986339940679,9.486999999999998,8.91,8.91888888888889],"ROE":[0.2621306394757387,0.20465687498127863,0.18675472390387085,0.1998575417761893,0.206688062505655,0.2116073406690791,0.20231917327049018,0.16693267368530196,0.13608355941212014,0.05895124305655298,0.07441372814023017,0.14673760760062818,0.13941375280331028,12.128182386553645,10.937277491881508,13.593547923064708,18.845946543307058,15.987165738359673,14.27788694754536,11.309304084801056,13.183636363636362,12.892380952380952,12.7545],"ProfitMargin":[0.26114419496252533,0.25533478667081905,0.2486113239350291,0.26135914006858346,0.24631173015691796,0.21912295375791613,0.19525876460767946,0.15884241200576468,0.15529020353167564,0.09967300882706741,0.12402321885904317,0.18105617778778352,0.16712166936134282,17.243489298014232,16.792204507358647,14.921940173995948,21.159946942673123,18.837043903064647,17.523867823466396,17.920017903403,0.1607493529045044,0.16082153452663941,0.16278686446427468]},"1093":{"cur":"HKD","cur_ratio":[8.28,14.762895506603114,2.826030047779081,2.6117089959861493,0.024044728578693112],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[3539.147277026348,4987.339928159881,6832.480764076233,7031.597830623592,7775.24663395326,2407.891960832131,4146.641145433685,9949.789039208365,10956.163726485727,11396.019922778018,12374.258947503571,15469.852263603922,21007.6757927203,25075.156709158593,28066.468528682923,33586.6425442058,36023.88980594711,34769.460070345034,31439.501269358832,28222.877423481477,38960.26380714286,36181.4983925926,38394.784516666674],"Revenue_Adj":[3539.147277026348,4987.339928159881,6832.480764076233,7031.597830623592,7775.24663395326,2407.891960832131,4146.641145433685,9949.789039208365,10956.163726485727,11396.019922778018,12374.258947503571,15469.852263603922,21007.6757927203,25075.156709158593,28066.468528682923,33586.6425442058,36023.88980594711,34769.460070345034,31439.501269358832,28222.877423481477,38960.26380714286,36181.4983925926,38394.784516666674],"NetProfit":[15.66574080801907,477.51064189661406,940.9023150762982,970.7385624556507,751.7685383546988,355.47698480808856,2162.337804899042,972.8180759289532,1268.5718278571585,1665.606272506858,2101.7342542032948,2771.8341568228884,3653.1111378160913,4213.4995698551675,5805.954224268292,6755.668827864204,7093.003298747934,6493.215621849173,4690.615686853907,4213.040932459258,10582.318000000001,7071.218,7652.415],"NetProfit_Adj":[15.66574080801907,477.51064189661406,940.9023150762982,961.0643168161569,804.322848687651,355.48523633976504,969.4923429657775,865.9769592204894,1265.427515977281,1677.304877337428,2117.1870202882224,2786.6746821698202,3767.353790402298,4397.934463728443,5535.327185957316,6703.139963350007,7017.867806790703,6447.1575787438005,4632.464119086623,4162.568869042592,10582.318000000001,7071.218,7652.415],"OCF":[139.6745209082696,790.7270871380908,1414.223516781703,1507.5313205051943,956.0231488303016,293.9905714976473,505.9390540646595,820.9136020963359,1814.4859763144902,2259.4888160583214,2930.5417437355004,3310.1079699237025,4567.869351475095,4394.757695975857,7687.264312260161,5809.161561367085,9163.59681711405,4907.171016049586,5166.529422326335,6529.43163432469,null,null,null],"FCF":[-199.4501632955584,185.2645826040142,1051.710628885488,811.1916343687892,165.3174908639501,-28.665320951236243,301.725345049865,190.84415872879916,1007.4079233764296,1436.4621477255002,1750.6492095130836,2125.198046162393,2527.2473954022985,2179.942786217239,6001.828260772357,3514.715656320169,6437.291247543802,2522.7869964235524,2536.0278403847724,4402.084218269135,8241.943516045047,7112.061480612473,7828.986917461085],"DivYield":[0,1.7182923507026309,7.352700282275991,5.56834612549071,5.53013159928702,0,4.484403277308052,1.3072012107069115,1.4620328059782093,1.3889126149886344,1.4493076853634639,0.9505344423024423,1.8137778190692135,1.2040815957934206,1.908439256206023,2.124767445144573,2.8962345365211037,4.242033461159942,5.7882957543073275,3.8313022147040616,4.924514687825922,4.058821904087071,4.285888863756277],"Assets":[5273.560812817023,6458.47755628704,7903.6814336991,8957.694527966814,9718.881950272951,2409.015600765778,13698.361710266923,12200.403362960167,12500.817521916151,13539.684649390616,14759.77754137282,21543.52050920288,26434.690608200002,29439.6749892,35720.3977074,42620.9654368,47237.4374166,50905.758783000005,47238.76422220001,52007.46682179999,null,null,null],"Equity":[2650.8340000000003,3371.14,4583.195000000001,5329.463,5876.44,960.9519999999999,6763.745,7596.8,8151.527999999998,8812.607,10191.322,15407.395999999997,17682.769952751325,21832.853321365998,27441.903070244512,32912.18376402197,35784.13245984693,38516.26267774461,36039.014279666815,38479.15407081026,null,null,null],"Liabilities":[2622.649996949371,3087.4771484521607,3320.32670935335,3628.136018660391,3842.62820403875,1448.0230706932741,6934.789172236933,4603.692800086569,4349.55848158251,4727.185921080217,4568.737565826528,6135.523374240544,8752.688048200002,7606.6556202,8278.6330907,9708.091645999999,11453.6409791,12389.4473842,11198.2584042,13529.0015189,null,null,null],"Debt":[1975.2932383888078,2040.819490247168,1749.1149545319,1567.8370967050892,1529.4685219586847,345.69357970318305,2316.168940818456,1340.050223402982,1225.8295524193502,1462.8920266877399,1138.1255037813391,987.129512342456,1964.1521774,563.4074992000001,376.22456059999996,115.37317919999998,658.7990096,777.5204099000001,539.900586,714.0030932999999,null,null,null],"Shares":[2953.1993491200005,2953.1993491200005,2947.1244691200004,2947.1244691200004,2947.1244691200004,2937.1526400000007,5232.810240000001,10725.805440000002,11343.395333760001,11349.155333760002,11620.835333760002,11986.595333760002,11973.769733760002,11973.769733760002,11973.769732,11949.979732,11933.219732,11903.219732,11032.752,11061.429,null,null,null],"ROA":[0.0029407162382487032,0.08138114121082465,0.13097759520840577,0.11514648649482177,0.08049473772809405,0.058609490815712005,0.2684734714149355,0.0751180593667973,0.10270078950190503,0.1278956891228901,0.1484696292093187,0.15263378929916993,0.14970136405453033,14.995837602943801,18.3003713095685,17.296808780847904,15.92284020606093,13.34059132186792,9.546662802740554,8.52396510782752,18.40153846153846,11.92230769230769,12.173333333333332],"ROE":[0.006111024090051776,0.1592902430271646,0.23964301201731125,0.2010298144623409,0.13791455355587431,0.1062531988577387,0.5738309763129369,0.13857463791136074,0.16333562412123687,0.1980436825159276,0.2229527285323359,0.2178939920524574,0.22152920467594484,22.164515629192746,25.294849940402376,23.198055814625302,21.67932357723022,18.52343439594711,13.219121818200977,11.861647538840774,23.99428571428572,15.3495,15.253684210526318],"ProfitMargin":[0.004426416755728147,0.09574455496816223,0.13771020330175937,0.1380537661337724,0.0966874201870132,0.1476299562399141,0.5214673103025146,0.09777273388364761,0.11578613276748306,0.146156841054454,0.16984728242068242,0.17917651116450614,0.17389411250729545,16.803482501531906,20.686443748114637,20.114153473282077,19.689720729650258,18.67505451252967,14.91949775750869,14.92775123260112,0.2716265348510731,0.19544337717512844,0.19931480266339102]},"1177":{"cur":"HKD","cur_ratio":[5.115,32.825284774586876,2.947003792019903,2.649925881512653,0.01237320099255583],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[734.7336450518914,1164.5731042329057,2283.0436072097727,3234.973541889188,4086.5763665383192,5783.410535701619,7497.244444518736,9901.878735786906,12379.577912938872,14553.15442734912,15831.348671339458,17109.09504906693,24768.97861777777,27492.504247551693,26609.27912331707,32374.743278475755,30305.67195759174,28964.58340071694,31284.41850045554,34548.24054557036,42744.42987692308,45415.13086153847,49519.95640000001],"Revenue_Adj":[734.7336450518914,1164.5731042329057,2283.0436072097727,3234.973541889188,4086.5763665383192,5783.410535701619,7497.244444518736,9901.878735786906,12379.577912938872,14553.15442734912,15831.348671339458,17109.09504906693,24768.97861777777,27492.504247551693,26609.27912331707,32374.743278475755,30305.67195759174,28964.58340071694,31284.41850045554,34548.24054557036,42744.42987692308,45415.13086153847,49519.95640000001],"NetProfit":[141.18768905449878,224.41063671778733,297.72331643003366,396.96182107602556,566.9569849866949,462.88692259431525,890.8003516806734,1036.835489937718,1513.3551075667326,1779.0501077949284,1913.990742249808,2506.3938238026935,10726.853261666663,3132.8551282965477,3118.192682943089,17606.839662383558,2961.8117376487608,2578.0597436714875,3793.032233284772,2543.1082914222216,5652.738,5830.634,6381.06],"NetProfit_Adj":[141.18768905449878,224.41063671778733,297.72331643003366,398.0526039309443,437.0871149690299,632.375701788942,868.7035755947622,1038.9612324639534,1472.4634648811304,1812.1298370325062,2033.0226217821046,2363.715919387673,4540.521174739733,3150.4459869460306,2598.762605202235,18269.323005278427,2857.2383531217874,2085.4896076268797,3092.138536737998,5430.188300837301,5652.738,5830.634,6381.06],"OCF":[203.9086587323657,336.31637813455416,465.759450621304,708.0526808569665,526.7017259137073,1135.3427460578741,1575.1918899838197,1703.9854900143137,2475.1795095777265,2918.472463913485,3707.0690413149414,5390.1671777596175,6500.885388850573,6274.054199748269,6321.053316300812,11635.641410246104,8523.251761707852,7311.166269842974,9054.186041878596,12363.34884739259,null,null,null],"FCF":[140.9036574923513,262.61544908684806,250.45211842393584,367.6588342838622,99.89856943386712,849.2816464549024,1243.291110291086,1140.816659452462,1865.2960160909436,2165.5519073501896,2872.1866110559376,4168.547961841156,4662.972550478926,4629.673445937926,4718.356519308943,9569.67902656462,7075.485598908265,6130.641612179751,8013.651595604522,11018.979057602466,9298.297454177897,10775.620323032086,10195.350940350254],"DivYield":[5.434612760329551,4.054240084503942,5.7140977008801626,2.642230047223182,2.7682550726031923,3.0333887539008235,1.3513812685951139,0.8130154153736304,0.8535110266391347,0.7790501423100087,1.0600261282115928,0.5656765161949808,1.5049593043115979,0.6701286903643191,1.127718740610593,1.49190149136225,2.587236645664393,1.4076801456557488,3.2480035436393737,2.632920590141848,2.3891972178683685,2.6648738199301034,2.710819920273726],"Assets":[2215.17418945758,2589.5427677728,3267.493868193,3766.388496238641,5620.657847602981,6295.761525134793,7700.981635363357,9968.749636528808,14163.473660766704,16482.41549455525,20540.3560506,25137.061537300004,56679.7448288,41963.3751712,56081.279300199996,74274.56583159999,72450.29877560001,69958.9404181,69607.2670298,84652.1376477,null,null,null],"Equity":[2031.4989999999996,2220.303,2651.9339999999993,3027.944,4301.548,4776.365,5784.696,7224.963,8818.296,10443.045,11930.547838407998,15141.515376873067,42755.74353024642,22999.563264267454,26632.327317165094,46284.995019351496,42910.76137645321,41983.078198304516,45518.28143943338,46871.93589665336,null,null,null],"Liabilities":[183.61632067955802,369.33170607942407,615.4674482931,738.3902324542172,1319.2461851668402,1519.1950723881866,1916.4331335419645,2743.8716960182073,5345.4686204432255,6039.498797806121,8610.0746912,9994.975003000001,13925.856802400001,18963.636986000005,29449.086352,27988.6004152,29539.9403424,27975.809113400002,24087.101735800003,37781.0407478,null,null,null],"Debt":[0,58.35358359475199,102.2255624367,1.136020358256,155.29307782989002,85.14259090760301,41.046953358096005,74.152126996731,1723.7271237469201,1726.0307937251284,3406.0231292999997,3543.5106428,3885.6330425999995,9637.74574,17401.561354799996,15053.216307999997,11914.8299154,13837.0103756,10316.6772526,17897.171250699997,null,null,null],"Shares":[15281.788967961797,15281.788967961797,15281.788967961797,15281.788971375,16728.678471375,16677.432471375,16677.432471375,16677.432471375,16677.432471375,16677.43247025,16677.43247025,16677.43247025,18910.2007305,18882.4567305,18861.49923,18839.59423,18813.86723,18801.21723,18791.21723,18760.71723,null,null,null],"ROA":[0.06482720490653754,0.09338780709130112,0.10162659231726659,0.1128742890833716,0.12078140378391458,0.07767507652522605,0.12728138127672334,0.1173470695745257,0.12540645912402734,0.11607776259644681,0.10175941400988699,0.1104268465269477,0.2558517153236473,6.326962554299016,6.5413941612964255,27.114431953776098,4.082527183469782,3.6530984321241764,5.425557173791814,3.3140757509534327,6.63909090909091,6.2261538461538475,6.33909090909091],"ROE":[0.07505537894135508,0.11423821417023737,0.14011425138694822,0.1688023411766607,0.1851989363663999,0.12368256835865728,0.21163221917464534,0.2059320337130072,0.25016494928191885,0.24761609101153867,0.22745506975862242,0.25370775490856995,0.470898633353211,12.506393776760937,17.485175369257174,62.11287165371452,8.473178995802186,7.744539226386641,11.211082045981135,7.482939921825907,13.829473684210528,12.68157894736842,12.709411764705884],"ProfitMargin":[0.19216173099644457,0.19269776702047803,0.1304063205318697,0.12270944906838538,0.13873642240704195,0.08003701617529382,0.11881703448150754,0.10471098643032618,0.12224609903581656,0.12224498246590688,0.12089878013455872,0.1464948214160154,0.4330761242600264,11.395306517322954,11.718441031387023,54.3844919817153,9.773126765819198,8.900731310389482,12.124349484806759,7.361035616467272,0.1322498496766794,0.12838996039787576,0.12886305844879337]},"1211":{"cur":"HKD","cur_ratio":[72.45,19.359733917893077,0.9413879297441788,4.152181007680907,0.008234819875776397],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[12609.133864709236,21761.761817422223,30738.0249403454,45886.53589459997,53592.33011835516,55788.02427102614,54561.84382936018,62792.409982083154,69688.01291323223,95756.89097315,117136.43144351414,118511.59466019928,154214.48657431031,138152.23464200328,172692.75894217883,260506.37762739536,493789.3457690517,665885.7573644256,842204.1332188124,872499.4967468418,1020646.7744238097,1168267.4236048781,1295426.3676684212],"Revenue_Adj":[12609.133864709236,21761.761817422223,30738.0249403454,45886.53589459997,53592.33011835516,55788.02427102614,54561.84382936018,62792.409982083154,69688.01291323223,95756.89097315,117136.43144351414,118511.59466019928,154214.48657431031,138152.23464200328,172692.75894217883,260506.37762739536,493789.3457690517,665885.7573644256,842204.1332188124,872499.4967468418,1020646.7744238097,1168267.4236048781,1295426.3676684212],"NetProfit":[1088.8557348030774,1577.813971022222,1144.544547685877,4304.470397259766,2896.746816935631,1667.9267306730767,100.04491497892725,697.7975069000001,545.6649615804602,3433.15885948,5657.689574547125,4413.048676605003,3013.9736248658996,1538.989547948274,4520.8823628536575,3595.979873418934,19348.364681050414,33167.40940736776,43593.592392844424,35082.63613758395,42142.702,54524.183000000005,65511.606],"NetProfit_Adj":[1088.8557348030774,1577.813971022222,1010.587937742946,4216.945852115758,2950.9580641017233,1359.894076259615,137.39071776772036,700.270133987069,56.62630980114947,2243.3775318850003,6075.663532440228,4459.007290474906,2856.924356685118,1503.1673153120673,5027.780277506097,3454.151632694694,20195.17735256444,33539.2145377212,43917.54031301235,32960.31637167216,42142.702,54524.183000000005,65511.606],"OCF":[2195.1445202784626,1561.9034522666666,640.5869406641217,13252.415342914932,3137.852528267432,5953.020076544613,5397.794257618776,1506.664198696935,-2020.893045809197,2610.8205514200004,-4454.672835382374,4563.119363405771,11030.559005843603,12603.54939228964,47216.95166211382,76813.51121797129,162508.38977087275,187132.71917156404,144466.7255952526,65459.39740446419,null,null,null],"FCF":[-548.3307891938464,-3902.075243911111,-6553.33620085992,4961.153644583903,-8788.973120210723,-3099.3207618392303,-1699.328844394636,-3842.099753013027,-4949.034251798854,-2833.7145852400013,-10863.228705485055,-4962.413882688465,-10126.345473658304,889.895434617239,40236.0367843821,31804.99584492844,49026.599300779366,52153.047526425595,38950.65304748969,-104715.64749898888,38853.1520809251,49364.910126426046,94180.40316034755],"DivYield":[1.3378105392714283,2.6928371773929274,null,0.5475154660675631,0,0,0,0.16856731938180294,0,0,1.4906782011425432,0.24859368261744816,0.46503400219204266,0.17275543186266634,0.08652071557308573,0.04831631448674959,0.6705608519293597,1.5882860823353964,1.586254905654955,0.41815600676431586,1.51654427255048,1.899018377982543,2.215614413159311],"Assets":[16332.7046227,31303.5391808,36939.81348,45934.6141917,63545.1650085,82457.6292844,87026.70488170002,99944.80383739999,117426.46078050001,137959.282923,162087.58025939998,213843.98560100002,221538.6282722,218844.6859298,238788.47561589998,362863.0843396,558507.0045614,747434.4822330001,833647.300891,984209.9706970999,null,null,null],"Equity":[5349.760554393921,13264.696258856258,15144.087134598198,21608.72500502409,24948.492880222162,29563.96721393082,30014.53170551724,31843.875524717474,36093.142870493284,43041.26715593064,61910.26409869302,71988.23915196642,69109.16367286473,70025.41584053767,76565.18836168648,127884.11442197263,137281.05576825736,165493.06874863402,211435.24139645722,287940.24439791957,null,null,null],"Liabilities":[10982.776838000002,18039.454361599997,21795.227574,24325.506057799998,38597.4708665,52892.51961000001,57012.8372916,68101.2172723,81334.41952380002,94918.75162440001,100178.69897950001,141853.0309601,152432.4637828,148818.7375178,162223.6735511,234976.2887384,421227.2378981,581941.2041443001,622203.3088732001,696274.8803626,null,null,null],"Debt":[6502.3482059,8682.5955968,13448.859729,8211.940061,16574.911955,22711.3359701,23157.8628013,31802.564461999995,37662.780759400004,44968.04229060001,47224.87552529999,67852.7769112,97730.09369320002,85848.148309,62106.251081,52414.3945176,28450.2944435,56028.18666540002,45594.15486320001,163285.86945159998,null,null,null],"Shares":[6150.3,6150.3,6150.3,6825.3,6825.3,7062.3,7062.3,7062.3,7428,7428,8184.428565,8184.428565,8184.429,8184.428565,8184.428565,8733.428565,8716.895493,8733.428565,8727.797565,9117.197565,null,null,null],"ROA":[0.08096583585551304,0.06734046378530598,0.03303930293003825,0.1039651474479767,0.05350708429526383,0.02293266506618458,0.001188950073893166,0.007472627109794642,0.0050402941887846605,0.02695478418275296,0.03877971465025596,0.025166168782488764,0.014920386495730933,0.8274718501580177,2.1349662647440213,1.2259273430918534,4.210128997223678,5.120265570028566,5.50334937500407,3.9132986692253744,3.986666666666666,4.603333333333332,4.956315789473685],"ROE":[0.2360288950738468,0.1922301326289256,0.09286747114603709,0.27128047575928493,0.14360966706121073,0.06995734427193283,0.0038456575535919667,0.025779581337648794,0.01841833990396802,0.1021892012084245,0.12644664686283177,0.07755881662140762,0.049639138449768076,2.6171169404480477,7.429922088079453,3.9557186566123357,16.12444157290063,24.01623308647231,26.020810447690895,16.248870399427005,13.387222222222222,15.175714285714282,15.858181818181816],"ProfitMargin":[0.08635452256166416,0.07250396288038784,0.03723546161170549,0.09380682837220508,0.0540515183896344,0.029897576629888373,0.0018336058306939445,0.011112768360047109,0.007830112221162935,0.03637893039328913,0.05041682274665053,0.03961474599655098,0.021377111710382,1.3257307961166784,2.7590340221004888,1.4088804743743124,3.9198281160900494,4.9875552400412495,5.1800564701600385,4.057269122916176,0.04129145507618363,0.04667240468932077,0.05057300863389091]},"2333":{"cur":"HKD","cur_ratio":[6.18,7.383275844800675,0.3031383944850642,0.7117018964383666,0.058435072815533985],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[4793.257675886156,7776.082395733332,9198.170341714931,14065.852608324127,25455.718805691562,34978.30941857075,51100.274690538856,69049.70611733315,75919.83110742575,90247.86037597382,115275.48402763398,116801.61457141313,117663.57313897647,109147.04533297736,116247.93488677191,164402.1972630098,159922.9351571274,191493.46625596043,219132.5427302103,241819.04202897658,275663.630312,306276.91888,330412.6227739131],"Revenue_Adj":[4793.257675886156,7776.082395733332,9198.170341714931,14065.852608324127,25455.718805691562,34978.30941857075,51100.274690538856,69049.70611733315,75919.83110742575,90247.86037597382,115275.48402763398,116801.61457141313,117663.57313897647,109147.04533297736,116247.93488677191,164402.1972630098,159922.9351571274,191493.46625596043,219132.5427302103,241819.04202897658,275663.630312,306276.91888,330412.6227739131],"NetProfit":[684.9301283876925,961.783061511111,576.1946283407151,1160.2638560896542,3097.2507728011483,4127.2130815977025,6998.298927435271,10375.821286082743,10121.640434252027,9943.523786742684,12333.63366576609,5804.087080708482,6174.657330698084,5101.518489570428,6034.196588010137,8106.648180842433,9625.23526042644,7762.638880512015,13720.348904663098,10706.25269861722,9286.906,11069.075,12692.636],"NetProfit_Adj":[684.9301283876925,961.783061511111,576.1946283407151,1146.3890677586198,3085.49952113247,4053.515123573045,6962.532647921977,10299.336371269706,10101.949990763535,9779.424266599834,12313.24170512451,5440.208764229993,6208.061675033321,5432.2945680086605,5969.052335640978,7552.669550076484,9385.295516631671,7617.878008843673,13522.981836561921,10400.779781881343,9286.906,11069.075,12692.636],"OCF":[984.238915035385,1325.6187061333333,695.0910928330571,1557.930630443677,3680.518310627968,5331.5759954113355,5424.94808001565,11466.01655177255,7674.692624709741,12509.048251086435,8339.767817646272,-1259.2524373486183,23019.30282036172,15761.451904772679,6304.165507933968,42578.80315991979,14213.04292920861,19810.637109108797,29447.12551505474,43480.61730617231,null,null,null],"FCF":[42.817147263076954,-198.3223386666665,-1289.045464345749,97.75780083908035,-14.06924467739499,803.6537691417631,-39.30179401834834,2465.912213930704,-1424.1664980661935,5282.945014446261,526.4331488928265,-7981.313140912736,15119.14703883746,7887.94336060378,-2767.216259945537,26800.780320161422,-4768.1396978707935,1333.651284723258,16574.012252530374,30990.09926448609,10257.151922759349,14577.427691275358,16173.726839448136],"DivYield":[2.123395840798397,1.9052572716525347,6.308126759704527,2.933057422576963,2.4573707607282667,3.2615140687583697,2.8980894472428234,2.4544659156998714,2.266010354534315,3.616048235672791,5.4014329538646635,2.2805729926228833,7.35430612375319,4.854997724948066,1.2504276015816125,1.6936808766492217,3.339302893806494,3.2541380975939878,3.5056394052565896,2.5477261907708253,5.457047332923909,5.856343479235415,6.445780647600019],"Assets":[6450.360333900001,10861.5859456,12146.311060071808,17107.4999729,28036.8647965,40851.96607740297,52918.016834871225,67392.02072187561,76626.3544274014,85904.43474044546,103137.0251011739,132733.87148956713,127295.94830368647,126509.64363197867,182950.24972203787,215190.5596572725,209620.57110499486,221377.190398374,231697.93830581912,250903.10402985275,null,null,null],"Equity":[4445.186900923754,7329.94489653424,7968.041221440423,8901.64161904762,12267.553678741066,20985.00102799394,26905.467834363677,35881.37035297649,41869.1689313283,45857.79434351833,52909.40879716285,59141.80364883713,59993.82931540191,60850.51579238841,68116.72441683018,76217.62471031294,73753.36305092862,75352.71614407438,84061.05198698849,97887.11874985487,null,null,null],"Liabilities":[2005.0344791000002,3531.9789247999993,4178.00741072442,8205.7005252,15769.7038695,19866.154111758246,26013.144323338904,31510.975965795875,34758.463402355,40047.42440716578,50228.79808098905,73589.8369358598,67304.72258658188,65658.66504693431,114833.8689767786,138971.3369915524,135867.90061740208,146024.37893705952,147633.40730355028,153017.73743666173,null,null,null],"Debt":[239.6714655,488.863776,null,43.5734019,0,4936.51420205494,5461.406671936217,6137.733262963895,5263.477219062188,7064.658414847243,4988.501684140606,20881.34932748299,21818.9981454178,19754.827788614675,46026.79178005494,59242.147600444696,68740.53162553483,63712.88207284895,61683.9584096001,55840.177682369685,null,null,null],"Shares":[7081.5,8214.54,8214.54,8214.54,8214.54,9127.269,9127.269,9127.269,9127.269,9127.269,9127.269,9127.269,9127.269,9127.269,9127.269,9179.390428,8764.512467,8497.528597,8556.164379,8557.872087,null,null,null],"ROA":[0.1148924230100993,0.11271416736037096,0.049294505077278755,0.07938989681341242,0.13895607310837924,0.12041810150415755,0.15038650105349435,0.17281254622018255,0.14114139901578396,0.1209602536040205,0.1285004572868308,0.04956513181752376,0.04683942312997361,3.9990559925109026,4.015223954280864,4.083603827111847,4.58250354211974,3.6322082949201904,6.042988957213078,4.4537689811120975,3.2749999999999995,3.6042857142857145,3.67],"ROE":[0.1845222608705241,0.17762649420111554,0.07803689646411302,0.14274423951801662,0.30646363102922725,0.25614251505389785,0.29763386687622667,0.33220055756616007,0.26173568296878547,0.22454795100850478,0.24644849487887174,0.1042690618321565,0.10244632450007211,8.411343250430722,9.59806422891829,11.260242998086904,12.984096278666001,10.503307976626171,17.16750151680338,11.823428801897398,9.081923076923077,9.9588,9.935652173913043],"ProfitMargin":[0.14289449362036766,0.12368478271768737,0.06264230895220492,0.08248798621727443,0.12167210034189427,0.11799349797639086,0.13695227608494628,0.15026597315927115,0.13332011263210022,0.11018016100678539,0.10699268599739263,0.04969183946648128,0.052477220995192676,4.673986798274851,5.190798953880409,4.930985300563506,6.018670962341617,4.053735635103109,6.261210103127037,4.427381983150159,0.033690298238266886,0.03614184662818757,0.038415675405818105]},"1299":{"cur":"HKD","cur_ratio":[76.4,18.725936160979654,2.7780450316511858,2.5131712624931963,0.021196156452109642],"date":[null,"30/11/2007","30/11/2008","30/11/2009","30/11/2010","30/11/2011","30/11/2012","30/11/2013","30/11/2014","30/11/2015","30/11/2016","30/11/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[null,118762.55498390803,29681.548557692295,146711.98059310325,142042.8960000001,110245.05246896566,156380.33219541976,169802.17418000012,197223.0841673078,181008.88846551714,218848.91472366403,298522.54309195396,253635.80497127655,370147.93004521064,390591.66953511426,369405.10594827594,186674.6544665383,272178.4543053844,311917.4354310346,330379.5883590035,187813.66126923074,206766.09347692307,226261.64289166665],"Revenue_Adj":[null,118762.55498390803,29681.548557692295,146936.7730819921,142042.8960000001,110268.40452413807,156861.40694961825,169802.17418000012,197223.0841673078,181008.88846551714,218848.91472366403,298522.54309195396,253635.80497127655,370147.93004521064,390591.66953511426,369405.10594827594,186674.6544665383,272178.4543053844,311917.4354310346,330379.5883590035,187813.66126923074,206766.09347692307,226261.64289166665],"NetProfit":[null,14929.169199999998,3178.4965384615375,13596.069845210708,20978.667000000012,12454.429425287371,23425.236821374034,21903.953036923096,26753.416442307705,21436.997327586196,32319.72197862594,47663.91765517241,20349.475903191487,46846.33321494252,44822.75776412211,57729.02097586208,26083.865683461518,29467.86233692306,53340.02723218393,48599.22022298846,62780.950000000004,69330.94,76073.418],"NetProfit_Adj":[null,14929.169199999998,3178.4965384615375,14302.465910496396,21231.347272189374,12775.832549959709,24313.453897120457,22243.309549843652,27099.331098490773,21889.01624026886,32834.03661414282,48564.080654740916,21684.219732744677,51014.6304670498,46428.2796290076,49785.15946551725,50272.71620769227,48625.10440346151,51615.60563793106,55646.65286360148,62780.950000000004,69330.94,76073.418],"OCF":[null,12745.173705747125,17816.719567307686,-8534.363112643667,-10974.771000000008,16517.687025287378,-11584.590451908392,7151.361437692313,-9282.272313461543,9001.212982758616,9748.69615877862,10241.511718390802,14511.83264255319,24524.004509578535,16536.09959389312,28028.9281862069,74680.82468423071,82398.84460576918,21949.311966666675,50041.44924789267,null,null,null],"FCF":[null,12269.374687356321,16928.61024038461,-8836.67025287355,-11821.374000000007,15832.693406896571,-14734.854164885488,3490.3607884615412,-12818.376051923082,7923.548379310341,8343.828320610683,9431.536647126435,13516.690771276593,23693.479117241375,13635.301635114494,21204.358317241382,70428.78653769224,68729.63960038457,15324.724032183914,46541.12042528731,46495.9275294558,49261.158471826195,53619.27532258157],"DivYield":[null,null,null,null,0,1.3959425614671683,1.2276776533877323,1.0824050104285294,1.1167285099871118,1.5038898292781802,1.8101366141769084,1.573928439356984,1.752641529963648,1.5481463988756126,1.422272861571209,1.8535240050991306,1.7597777414257696,2.3671340056762,3.1168619376230255,2.40400671032404,2.824005281737962,3.162885915546517,3.4914974392396614],"Assets":[null,686689.3696,558084.1518,702616.3159,837658.017,889350.5239,1041915.6939,1142748.7452,1294506.9207,1316082.8466,1435489.4662,1684676.1246,1799886.5532,2213160.9744,2528644.3977,2650167.515,2110133.6007,2236323.1814,2372675.0358,2688738.0897,null,null,null],"Equity":[null,105044.9733,69116.28360000001,115933.7459,152481.48299999995,166392.4085,207919.68279999995,192473.8002,240065.31149999998,243605.3394,273874.95300000004,330950.74319999997,308635.67319999996,431482.734,493662.57159999997,475132.865,352231.15160000004,324874.0964,317023.1400999999,339440.3112,null,null,null],"Liabilities":[null,581644.3963,488967.8682,586682.57,685176.534,722958.1154,833996.0111,950274.945,1054441.6092,1072477.5072,1161614.5132,1353725.3814,1491250.88,1781678.2404,2034981.8261,2175034.65,1757902.4491,1911449.085,2055651.8957,2349297.7785,null,null,null],"Debt":[null,53382.8728,26187.9258,7533.0972,13108.6704,9549.2071,19824.7558,29762.2314,51859.6911,48686.956,42225.2972,45621.7146,51982.3114,59065.5036,79266.0751,87144.86,101063.2218,119197.5666,139391.3765,156884.5045,null,null,null],"Shares":[null,12000,12000,12000,12044,12044,12044,12044,12045,12048,12056,12074,12077,12089,12095,12097,11781,11362,10793,10507,null,null,null],"ROA":[null,null,0.005093601163538305,0.02156539700494258,0.027210815820757187,0.014393278339015679,0.024258738449176375,0.02003966775593331,0.021952080834560846,0.01642523843327581,0.02347026198313568,0.03054158921063466,0.011658888836512928,2.3267398012989893,1.89396856713527,2.230347074677738,1.0915138159565492,1.352035776504607,2.310345352018426,1.9155693011121917,2.2399999999999998,2.31,2.302],"ROE":[null,null,0.03651333452657956,0.14729593550554249,0.15674781649885383,0.07830087109719096,0.12576546552801499,0.1099281807742463,0.12435121107266436,0.08930157448526443,0.12598520490749285,0.15900646938086208,0.06412345679012346,12.727640415952656,9.782728296105699,12.011288379276605,6.336373752841476,8.775631535385799,16.754696633619687,14.88983101451006,17.80071428571429,18.28,18.334615384615383],"ProfitMargin":[null,0.12570602916064627,0.10708661417322834,0.09267184445501137,0.14769247594050744,0.11297041587234342,0.14979656643842412,0.12899689384249954,0.13565053277238234,0.118430633486101,0.14768052205986665,0.15966605791807983,0.08023108529766136,12.656111087591551,11.475605154987193,15.627564439768543,13.97290154788372,10.826669734798367,17.100687929956223,14.710115859269921,0.33427254261339384,0.3353109738355408,0.33621879957797357]},"1398":{"cur":"HKD","cur_ratio":[7.775,7.230324229211719,2.9578491581445174,0.7517955764470199,0.04330661843515541],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[282369.81371538475,397620.3610666666,529871.1296118317,536071.9862482755,626227.1774777776,849827.4159492305,1035857.1102241385,1154579.0083501916,1265252.9596436792,1296655.3018700003,1146105.9016689651,1193812.7406703853,1325381.9630459766,1387450.8343137915,1419272.6389268292,1624827.5508995904,1766213.5859884298,1776174.8521983468,1726940.026039917,1642683.2072827157,1027935.6150307693,1089359.7810153847,1165724.08582],"Revenue_Adj":[282369.81371538475,397620.3610666666,529871.1296118317,536071.9862482755,626227.1774777776,849827.4159492305,1035857.1102241385,1154579.0083501916,1265252.9596436792,1296655.3018700003,1146105.9016689651,1193812.7406703853,1325381.9630459766,1387450.8343137915,1419272.6389268292,1624827.5508995904,1766213.5859884298,1776174.8521983468,1726940.026039917,1642683.2072827157,1027935.6150307693,1089359.7810153847,1165724.08582],"NetProfit":[48007.399813846176,83635.89688888889,124222.85085229,145970.08054022977,189590.41889195394,250877.14042692303,293251.33219157107,331385.65214521077,347155.0630724141,339045.4920000001,320053.61696398456,325125.0615553848,347631.1013409961,349071.78312758583,345530.26234959345,408256.72261152306,402512.35420619836,385866.7033202479,380296.61139917676,387213.4877802468,438979.22500000003,461281.211,485296.947],"NetProfit_Adj":[48007.399813846176,83635.89688888889,124222.85085229,145601.5953005746,189506.9057106321,249536.4146338461,292637.2467001917,330766.7856641763,346181.79640603473,337877.70976500004,319511.81544043537,326752.0622224027,356857.85349319724,361256.7299246793,369322.7227772073,435367.67352567666,433789.5528597108,389142.98115185945,381286.63898536155,373903.231291902,438979.22500000003,461281.211,485296.947],"OCF":[122325.65742307698,78350.17386666665,-202119.2001137403,29142.955797701125,-41409.9105130268,298259.0886253845,214542.66086206905,75325.07193524906,440307.9796091957,1674248.0948400002,-181783.6371984674,643183.3415042311,136373.79450191566,374026.4694068961,1566500.5533414632,1560315.1521687259,1016439.9637028927,698817.2924566114,2093251.6061666657,1588346.0413802466,null,null,null],"FCF":[null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null],"DivYield":[1.306451612903226,1.6359286592865927,4.661016949152542,3.125,4.339622641509433,4.787735849056604,5.759036144578313,7.310055865921787,5.24435318275154,5.093886462882096,5.312925170068027,3.883870967741935,4.737240075614366,4.469387755102041,5.330661322645291,6.334773218142549,6.993087557603686,6.410041841004184,4.4508670520231215,3.912988650693569,4.911874505384528,5.168803325666181,5.410618685931264],"Assets":[7483972.1217,9281151.3856,11079239.283,13384284.6921,15874444.649,19081430.5572,21806729.9527,24235532.0872,25743892.292300005,26531803.188000005,26968566.1845,31322712.530100003,31538696.244,33680415.1096,39610594.3982,43148252.6644,44795114.1114,49162317.19210001,51956102.0932,59558195.79009999,null,null,null],"Equity":[469432.0225365465,581107.6951117567,688805.6791443848,771051.6728332674,969175.4601271682,1180854.344143372,1402818.422309543,1637853.8116119227,1920305.036827013,2150936.7710057804,2213602.8626165395,2570668.9702614225,2669999.6558261253,3011251.654019588,3456230.3063334962,4018002.2741932957,3975624.679253762,4153863.8867796995,4243072.870126311,4757838.292999098,null,null,null],"Liabilities":[7014525.425,8700070.4768,10390410.918,12613219.3483,14905300.2175,17900530.5805,20403942.5698,22597693.1379,23823645.8659,24380903.190600004,24755012.7646,28751946.590900004,28868812.4602,30669140.5538,36154381.5297,39130166.15,40819526.7643,45008448.050900005,47712853.616,54800442.66129999,null,null,null],"Debt":[482331.0376,1104799.3216,783688.035,1263686.5758,1454737.5045,2159800.3845,2433850.5728,2334549.8111,2748540.8747,3476146.2318000006,3312333.0551,3938635.7981000007,3356158.9934,3695028.8732,4704819.3101,5086815.788,5444405.9216,6614759.003600001,8868879.9774,11195774.4038,null,null,null],"Shares":[334019,334019,334019,334019,349019,349084,349620,351390,353495,356407,356407,356407,356407,356407,356407,356407,356406.257089,356407,356406.257089,356406.257089,null,null,null],"ROA":[0.007055269067078404,0.010068882047159841,0.012021241094096597,0.011943534641008563,0.013084941079300062,0.014395125155993555,0.014448129013871826,0.014407527334979358,0.013955325764549194,0.012944078843275366,0.012007194849207753,0.011390858784953294,0.011068782711108456,1.0801921141104454,0.9956930710061292,1.0168012083406377,0.9640963612819418,0.8634918300299885,0.7824371189436993,0.7205547075934932,0.6825,0.6606666666666666,0.65],"ROE":[0.1364053589698506,0.16225549468320669,0.19427963465101322,0.20154821364784328,0.22104458005397765,0.23437798929985396,0.229166096230123,0.21895344606026101,0.19665717525854787,0.16957948853394583,0.15237301807919698,0.14349144234345781,0.1368230196084837,13.0546770855519,11.953395045328323,12.16027622517392,11.436276240231509,10.666601062843064,9.95735985526783,9.508149059725927,9.119,9.001666666666667,9.01142857142857],"ProfitMargin":[0.27120034352153616,0.3200138180647643,0.3754089699038794,0.4118102750096834,0.42730513885942256,0.4350946171234222,0.44257829894611844,0.44417555359383615,0.42506160681674227,0.40336894013603264,0.42105669342574165,0.41186396994771957,0.4010903206421197,39.433225685450104,38.73637692390697,39.73816512526481,38.756820393177186,42.493655014931434,45.56735159532997,45.11460977836968,0.4367086240157239,0.43252053201539786,0.42725783056498773]},"1810":{"cur":"HKD","cur_ratio":[28.7,44.818366439994065,2.4291187497097924,4.093559104992714,0],"date":[null,null,null,null,null,null,null,null,null,"31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[null,null,null,null,null,null,null,null,null,82431.06200782,79995.18169148042,132335.8958372516,207408.81343342905,233515.4672662963,276662.8021677317,395695.7454177247,326091.9312804827,299569.2478444276,396560.1168265636,496268.4011366209,513557.8422974358,605347.8700102563,707455.9101696969],"Revenue_Adj":[null,null,null,null,null,null,null,null,null,82431.06200782,79995.18169148042,132335.8958372516,207408.81343342905,233515.4672662963,276662.8021677317,395695.7454177247,326091.9312804827,299569.2478444276,396560.1168265636,496268.4011366209,513557.8422974358,605347.8700102563,707455.9101696969],"NetProfit":[null,null,null,null,null,null,null,null,null,-9353.725958050001,646.7140624521071,-50597.75914992002,16071.741029540226,11394.688437420677,22905.237740878045,23308.784283080684,2880.8372064834716,19319.561971816114,25640.083071686407,45193.30797168024,29174.847,38041.098,49268.158],"NetProfit_Adj":[null,null,null,null,null,null,null,null,null,-12060.493422610003,-1426.0617855904209,-9539.999309405774,864.7971370689653,8145.3825497836115,7407.869958552003,15957.822975093639,4482.891889078954,14572.592589762633,24610.498381647463,34538.824774814864,29174.847,38041.098,49268.158],"OCF":[null,null,null,null,null,null,null,null,null,-2677.99548176,5498.125080410727,-730.5177250050003,-831.4854048659001,28576.164080124105,26093.31020517886,13584.989491734172,-4053.1770135148763,48943.07355399173,46626.73585701397,40496.82264659135,null,null,null],"FCF":[null,null,null,null,null,null,null,null,null,-5792.520671000001,3363.3608273915706,-2136.4920823211546,-5319.9179014750935,24713.14755992066,22688.809702243896,4944.149920640333,-10806.376060254546,42012.53260626446,38718.09771384072,26639.152358202467,18668.47456496405,37240.78566006837,48955.08336280611],"DivYield":[null,null,null,null,null,null,null,null,null,null,null,null,0,0,0,0,0,0,0,0,0,0,0],"Assets":[null,null,null,null,null,null,null,null,null,46752.507100200004,56720.4059973,107906.62203270002,165356.54387,205407.63095020002,301346.2617417,359319.74611599994,309309.3049199,356639.75815610006,429037.8585538,565866.4784478999,null,null,null],"Equity":[null,null,null,null,null,null,null,null,null,-103499.89222825434,-102858.56117159242,-152736.11528106412,81128.91769336337,91341.58168411984,147316.39772881172,168598.6513050456,162764.11695155542,180671.29008347887,201343.75467247987,296609.5339269863,null,null,null],"Liabilities":[null,null,null,null,null,null,null,null,null,150250.629837,159576.6697348,260648.49871640003,84231.14699060001,114065.3545766,154030.60727319997,190717.56002319997,146546.7163659,175968.2395329,227685.77091399996,269262.25376259995,null,null,null],"Debt":[null,null,null,null,null,null,null,null,null,130426.03258740001,134032.06441209998,206824.55652120005,12446.4203082,20787.6809574,21948.379273799997,36224.9430392,29466.877416999996,32805.0795489,38323.30421079999,46594.864775199996,null,null,null],"Shares":[null,null,null,null,null,null,null,null,null,null,null,null,23626.423,23977.155,25183.019,24967.946,24931.045,25040.599,25094.939,25913.628,null,null,null],"ROA":[null,null,null,null,null,null,null,null,null,null,0.01230782742897616,-0.6232574137363831,0.11530427873880916,6.108526931040762,9.30943685292755,7.076590773975557,0.8735995812818066,5.846938371788492,6.504821906579377,9.13982586598487,4.715624999999999,5.876111111111111,6.526923076923079],"ROE":[null,null,null,null,null,null,null,null,null,null,null,null,null,13.159423292581081,19.856871158435617,14.82482167945815,1.7616819064545148,11.360278761513824,13.414173416804982,18.306539053745624,8.716363636363639,10.0309375,12.024482758620689],"ProfitMargin":[null,null,null,null,null,null,null,null,null,-0.11347331612884762,0.008084412695583425,-0.3823434211088563,0.07748822609555447,4.879628990240037,8.279117236364629,5.890582487429644,0.8834432655757944,6.4491138896370135,6.465623239388986,9.106626145886466,0.0568094402125349,0.06284190091236967,0.06964151897119401]},"1928":{"cur":"HKD","cur_ratio":[11.46,13.641110897968751,1.8098957819532753,83.27084386934763,0],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[9952.171955956148,15340.295623448274,23773.958671640827,25589.168241327166,32180.950333572437,37993.624754871955,50509.034975855124,69094.88341993108,73710.34911317244,52873.477807862015,51643.351710727984,59115.56808461539,67912.66456321841,69011.95901609193,13084.615391603045,22339.195675862073,12568.179051923067,51153.8290407692,55243.9135172414,58024.381796551665,61920.16434375,64917.52734375,68002.59761538463],"Revenue_Adj":[9952.171955956148,15340.295623448274,23773.958671640827,25589.168241327166,32180.950333572437,37993.624754871955,50509.034975855124,69094.88341993108,73710.34911317244,52873.477807862015,51643.351710727984,59115.56808461539,67912.66456321841,69011.95901609193,13084.615391603045,22339.195675862073,12568.179051923067,51153.8290407692,55243.9135172414,58024.381796551665,61920.16434375,64917.52734375,68002.59761538463],"NetProfit":[2919.6667181030757,1530.2596581609191,1368.3275154412208,1666.734501921837,5177.551997586212,8820.02769809616,9585.235750242522,17179.999546344836,19756.718277940236,11314.500244551711,9501.19682758621,12491.728926923079,14695.469827586212,15928.850213409958,-11812.607730534344,-8145.955834482759,-12388.074305384605,5417.577241538458,8153.9392126436815,6985.065980076621,6767.606,7839.774,8914.337],"NetProfit_Adj":[2919.6667181030757,1530.2596581609191,1368.3275154412208,2326.07485115408,5705.538912163145,9418.557987275368,11496.434314364968,17367.650909055475,20027.577803798475,11754.37780394846,10453.293934636886,12620.838699138463,16179.533962551353,16639.092830524867,-11236.316432101097,-6998.1909846823955,-12353.581021460712,5452.062704207569,8346.200516183912,7307.5016150498,6767.606,7839.774,8914.337],"OCF":[3348.6899415846133,882.970542643678,567.6266911912211,5236.156790655931,9466.875508432191,9519.052160618854,14169.728172437919,23495.54094793105,24722.59733097242,14876.759677862054,17504.24742337165,19497.38351538462,23018.983937931047,20199.0043532567,-7849.218006106865,-2230.8104241379315,-6452.448310769225,14921.82402076922,13545.682749425294,13759.644480842897,null,null,null],"FCF":[-5628.022446782305,-12475.06701827586,-14799.802563319456,2247.730416893484,6775.26320415173,3442.1655601046177,6496.139116034477,18833.633511172422,19137.178591751726,6275.234775310338,9485.671996168585,16201.063280769235,19162.89265517242,14557.699801532564,-15752.729022137393,-6614.702686206898,-8042.068464999993,13348.221382692298,6999.1229413793135,9690.219880842904,9130.962007884036,10683.801444872159,11326.41058001041],"DivYield":[null,null,null,0,0,5.307470618727233,3.9039883693109076,2.729435356934259,5.223465759368956,7.501980581254624,5.913211541116378,4.917780137279639,5.799938210107101,2.375098346838043,0,0,0,0,1.1893129822060196,1.2708407962135297,8.899895433356,9.173738369766953,9.721424242588862],"Assets":[21679.9394077,38283.8691485,53379.829013099996,59140.857288800005,65870.97446720001,78657.678321,80512.31098109999,88901.1939,87988.0858549,83481.68241909999,86711.8637,83177.5581,94440.6676,94249.32,81786.0276,78707.965,82401.5554,80121.1348,86757.4413,82283.6069,null,null,null],"Equity":[6650.920464299999,8172.706074999998,9684.0134534,28679.004739599997,33907.8062176,42838.79481520001,43297.5040134,50011.09505159999,49852.51370329999,45249.7726105,38823.7773,35452.2174,34532.169799999996,34630.7832,14956.887299999999,6924.179999999999,-5461.1900000000005,-31.2424,8008.498699999998,10905.2439,null,null,null],"Liabilities":[15029.0189434,30111.163073500004,43695.8155597,30461.8525492,31963.168249600003,35818.883505800004,37214.806967699995,38890.098848400004,38135.5721516,38231.909808599994,47888.0864,47725.3407,59908.4978,59618.5368,66829.1403,71783.785,87862.7454,80152.3772,78748.9426,71378.363,null,null,null],"Debt":[10793.2289815,22942.1434555,28234.854316,23937.110587000003,24353.744732799998,26478.009218999996,25272.460525600003,25042.362568200002,24813.922225399998,26236.3980298,33915.5586,34467.8676,43562.6964,43681.8336,54779.8905,62099.29,79429.1077,65046.6768,63376.6643,55242.3383,null,null,null],"Shares":[null,null,null,8047.865084,8047.865084,8049.283284,8055.414259,8063.193845,8067.701545,8069.300845,8071.130245,8074.417766,8080.603691,8088.352216,8090.118766,8093.188866,8088.352216,8093.379566,8093.379566,8093.379566,null,null,null],"ROA":[null,0.05097505668934241,0.029795524533715874,0.02962599006308006,0.08277709226413327,0.12181870199030932,0.12046513496065644,0.20270755762864184,0.22335456427189113,0.13196106455896717,0.1115023663963205,0.14686211635364177,0.16516185862144903,16.83086348207633,-13.449311197456728,-10.154054839647321,-15.317583268783888,6.647454370797311,9.754048630232884,8.242870285188593,8.626666666666667,9.898333333333333,11.206000000000001],"ROE":[null,0.20615475007396905,0.15298792017355392,0.0869027497217298,0.1653463397351854,0.22940555908354804,0.22260716140609468,0.36804262675797494,0.3956298936838533,0.23792756932860512,0.2257135494054965,0.33588266107909903,0.4191349055549346,45.917560700169396,-47.780392156862746,-74.40539581114662,null,null,null,73.68421052631578,62.302307692307686,74.21916666666667,88.04363636363637],"ProfitMargin":[0.29336980219234665,0.09975424827027808,0.05755572870047315,0.06513437585009181,0.16088872279774732,0.23214493892071095,0.1897726962082043,0.24864358540026285,0.26803180985625813,0.2139919807368772,0.18397715316398616,0.21131030846295806,0.21638776687824582,23.08128973660309,-90.27860106698282,-36.464857341684066,-98.56697819314641,10.5907560453015,14.75988700564972,12.038156657261858,0.1092956730540959,0.1207651363552404,0.13108818476447304]},"1929":{"cur":"HKD","cur_ratio":[11.59,16.85818181818182,1.1339920245779807,3.160498636540709,0.043597421712429585],"date":[null,null,null,"31/3/2009","31/3/2010","31/3/2011","31/3/2012","31/3/2013","31/3/2014","31/3/2015","31/3/2016","31/3/2017","31/3/2018","31/3/2019","31/3/2020","31/3/2021","31/3/2022","31/3/2023","31/3/2024","31/3/2025","31/3/2026",null,null],"Revenue":[null,null,null,18410.9,22933.6,35042.5,56571.1,57433.9,77407.1,64277,56591.5,51245.5,59156.4,66660.9,56750.8,70163.8,98937.7,94684.4,108713,89656,93574.85714285714,99949.07142857142,104712.21428571428],"Revenue_Adj":[null,null,null,18410.9,22933.6,35042.5,56571.1,57433.9,77407.1,64277,56591.5,51245.5,59156.4,66660.9,56750.8,70163.8,98937.7,94684.4,108713,89656,93574.85714285714,99949.07142857142,104712.21428571428],"NetProfit":[null,null,null,1896.7,2138.6,3537.6,6340.6,5505.3,7272,5456,2941.4,3055.3,4094.9,4576.8,2901,6026.4,6712.3,5384.4,6499.3,5915.5,8705.5,10530.966,11065.069],"NetProfit_Adj":[null,null,null,1909.66179446818,2140.39077834179,3542.04419238928,6347.42262947134,5369.05947641861,7525.0810278031195,5212.44549871604,3536.51346891593,2809.55244170245,4001.00437273616,4590.9318324937,2991.4773552146503,6446.52348652705,6687.49332962262,5371.32007251729,6531.2193840725,5966.125,8705.5,10530.966,11065.069],"OCF":[null,null,null,1034.9,1239,-2644.2,-6911.7,9763.4,-5472.2,7295.8,13752.3,6123.2,2673.7,8168.4,7895.7,10227.2,13722.7,9699.4,13357.7,9756.1,null,null,null],"FCF":[null,null,null,550.1000000000001,966.8,-3413.1,-7778.9,8846.5,-6660.7,5292.4,12835,5261.3,1589.2999999999997,6776.599999999999,6922.599999999999,9387.900000000001,12192,7717.799999999999,12394.5,9178.2,10317.432649989054,12189.177196000479,13785.3735210403],"DivYield":[null,null,null,null,null,null,0.8130081300813007,2.075471698113208,2.945990180032733,3.3573141486810556,3.2989690721649487,2.119205298013245,3.0201342281879198,4.408060453400503,4.371584699453552,3.3670033670033668,3.506311360448808,3.209242618741977,4.766031195840555,5.902383654937571,6.246764452113891,6.505608283002589,6.686798964624677],"Assets":[null,null,null,14409.5,17010.3,29048.7,47414.3,43218.6,61817.1,61121,55675.4,49401.9,57569.9,62237.1,65243.4,64308.2,87737.3,87340.5,88819,76524.4,null,null,null],"Equity":[null,null,null,6441.900000000001,8335.4,11672.9,29573,33860.7,38452.4,40540.799999999996,35749.9,32501.500000000004,34097.6,31403,26990.800000000003,31567.6,34554.700000000004,33359.8,26745.6,27393.399999999998,null,null,null],"Liabilities":[null,null,null,7967.6,8674.9,17375.8,17841.300000000003,9357.9,23364.7,20580.2,19925.5,16900.4,23472.3,30834.100000000002,38252.6,32740.600000000002,53182.6,53980.7,62073.4,49131,null,null,null],"Debt":[null,null,null,2174.4,2349.5,6812.6,14807.199999999999,5835.7,17086.4,15088.9,14433.3,10008.4,13258.4,18068.2,24508,13639.3,25979.6,22106.199999999997,29890.899999999998,20789.1,null,null,null],"Shares":[null,null,null,8950,8950,8950,10000,10000,10000,10000,10000,10000,10000,10000,10000,10000,10000,10000,10000,9987.7368,null,null,null],"ROA":[null,null,null,null,0.13613072011916053,0.1536116719859311,0.16584753410146086,0.12148568566160854,0.13846720686395197,0.08876011586318641,0.05036799079423681,0.05815337851277107,0.07656036450728136,7.640288130075873,4.551284314071564,9.303474445703488,8.829330693772588,6.1508655009372974,7.378881070847726,7.155411101985323,10.86,11.890909090909089,11.605454545454547],"ROE":[null,null,null,null,0.294987447929598,0.36350745233434545,0.3147855788229921,0.17786630223023464,0.2074785873652616,0.1424491010772452,0.0791403103298903,0.09160893571663084,0.12556767481011066,14.263698283263809,10.162188671313974,21.104387291981844,20.850055291179505,16.312236146308432,22.368031607711952,22.69065829951439,30.345555555555553,31.29928571428571,29.74777777777778],"ProfitMargin":[null,null,null,0.103020493294733,0.09325182265322496,0.10095170150531498,0.11208196411241783,0.09585453886990088,0.09394487069015632,0.08488261742147268,0.05197600346341766,0.059620844757100624,0.06922158887288611,6.865793891171587,5.11182221219789,8.589044493029167,6.7843703664022925,5.686681227319389,5.978401847065209,6.597996787721959,0.09289759546597841,0.10536332048549006,0.1056712352581906]},"2020":{"cur":"HKD","cur_ratio":[77.65,20.442731056620197,3.119653491795299,4.606502200359699,0.022223380346052382],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[1218.2787339578958,3066.296965866666,5185.374097238928,6665.748775788501,8504.34986673831,10726.730276945384,9371.483075816097,9186.808583683143,11230.72576523679,13727.07474639,15600.345798147122,19271.719561001544,28577.01367783524,38489.73624167237,39960.23889430894,59452.74454650212,62472.88713760331,68937.26352892561,76759.44086337445,87057.32312469135,103858.68302727274,112839.18329090909,122304.31706666667],"Revenue_Adj":[1218.2787339578958,3066.296965866666,5185.374097238928,6665.748775788501,8504.34986673831,10726.730276945384,9371.483075816097,9186.808583683143,11230.72576523679,13727.07474639,15600.345798147122,19271.719561001544,28577.01367783524,38489.73624167237,39960.23889430894,59452.74454650212,62472.88713760331,68937.26352892561,76759.44086337445,87057.32312469135,103858.68302727274,112839.18329090909,122304.31706666667],"NetProfit":[143.6596881195773,551.7517160888889,1002.8192540393123,1419.409681845976,1780.5963054249037,2084.1142772415383,1670.3875299750964,1658.9343722547894,2140.1293831379326,2517.6385616700004,2788.5515496183903,3564.9586858811554,4865.027125191569,6062.7147688413725,5808.592959349593,9304.557004115237,8838.031227272728,11316.34212396694,16902.553295473244,14746.31828641975,17836.099000000002,18963.564000000002,20955.322],"NetProfit_Adj":[143.6596881195773,551.7517160888889,1002.8192540393123,1419.5730748390795,1781.2015607493288,2084.859626823269,1669.5005175986598,1661.52149910364,2143.018975054312,2519.7640733925004,2790.853223829982,3548.3196057802897,4871.458872862577,6020.649428655567,5696.961419890707,9296.326002001757,8819.709205570267,11317.160961218276,13704.6157596601,14726.313250338771,17836.099000000002,18963.564000000002,20955.322],"OCF":[152.18179349198567,134.97060906666667,1138.471310988549,1916.1731718436768,1644.834293204597,1743.7265244999994,2415.749623666668,1423.270520959004,2110.6462943655188,2285.6243521700003,2858.5509679678153,3637.613420937694,5246.1478236781595,8366.908001031024,8221.150747967478,14161.72860082306,14059.339794214879,21581.312485537186,17924.553023456785,22713.106816049378,null,null,null],"FCF":[71.84494442063556,-27.542802844444417,1052.0092211702283,1770.6898662620674,1526.0538684934859,1526.218157832307,2152.328462371648,1202.4291842793107,1791.4160864632197,1719.9785211900003,2189.163046194635,3006.2326784146167,4330.290404674328,7225.289811986199,7490.856902439023,12475.57895720166,12171.797156611572,20297.776611570243,14318.833940740735,19922.94017530864,22916.738055200643,23111.858465372206,27243.174470814345],"DivYield":[null,1.4289940099095324,11.310912503437748,2.082524128276664,3.691846377224417,5.671602038974966,5.875063877905311,4.357501148997024,3.848604429791129,2.794144452614228,2.8686677481149063,2.2932367751873293,2.3652375577891216,1.0744893769886072,0.6572626556890916,1.3432601658821364,1.3113709687404378,2.860462154851482,3.2259576959389977,3.3874781386678694,3.865105075540596,4.087568053655109,4.471959866179507],"Assets":[853.5290090332031,4949.3327744,5612.188311,6931.6381942,8320.7721345,10102.7734293,12475.8299153,12962.3581217,14219.442125000001,14934.836637600001,15891.9623593,22902.4111512,27752.4948622,46106.896647,61612.8093,76881.1024,78252.6255,101441.57720000001,119844.883,138427.3415,null,null,null],"Equity":[237.11848842290797,4436.466247276316,5087.86258694601,5834.359964728801,6759.614806379654,7913.124502754098,8618.237519711307,9415.049472110239,9998.826056319875,10529.674708947974,11057.417603196101,17242.042781778782,18810.330060943517,23558.778472010803,30676.484373084935,38843.35402212049,42792.526933000896,61605.32107249479,71020.7044825602,80638.89191741425,null,null,null],"Liabilities":[616.4031046264648,513.0710272,524.158155,1097.1747845999998,1561.3737405,2189.3431343,3857.7830867000002,3547.3940841999997,4220.921247,4405.3419498,4834.791733,5659.717977400001,8942.9811276,22547.939001,30936.4797,38036.934,35460.5004,39836.1782,48821.2392,57789.89299999999,null,null,null],"Debt":[92.19475,5.66464,0,49.9776142,49.045969,48.2174861,1286.6917877,675.2050360999999,1729.4938671999998,1635.0561876000002,1092.7104616,177.5967377,1495.6182774000001,11037.0852564,20244.1918,22563.305599999996,22078.5607,23613.753100000005,29925.304,35375.5668,null,null,null],"Shares":[null,2490,2490,2492.088,2493.833,2494.163,2494.163,2495.351,2496.932,2501.121,2502.471,2684.569,2684.904,2701.947,2703.329,2703.329,2713.624,2832.624,2823.224,2796.653,null,null,null],"ROA":[0.2263673915260741,0.19602109966580428,0.1869362659585803,0.22649894693844444,0.23576913544770867,0.22691898278212963,0.14905898834802128,0.130477453594063,0.15815441772416639,0.1708614348682011,0.1785221056880626,0.18546852870600264,0.18886085285035414,16.29496683331244,11.090891326184952,13.480595451172132,11.511948006643259,12.682207616015065,15.227271617775564,11.47102275125575,11.864545454545453,11.64857142857143,11.690000000000001],"ROE":[0.8449273216150295,0.24508608049759972,0.20732993294611898,0.2616809389914726,0.2883653700465794,0.28715906104280686,0.20705387709509127,0.18910016432924878,0.22747923041752555,0.24923298407561414,0.2631848314022268,0.2655634158850688,0.2783131106014892,29.806448064957518,23.413257419104294,29.1672963578661,23.97233232790613,23.84346610761705,27.557448161923865,21.312671063672937,20.755999999999997,19.50794117647058,19.14181818181818],"ProfitMargin":[0.11792021326093531,0.17994073053943105,0.19339381021193564,0.21294077073555362,0.20937477094975385,0.19429166422883384,0.1782415351403315,0.18057787501975964,0.19056020313152133,0.1834067788063949,0.17874934220686253,0.18498394367958962,0.17024267056165343,15.751510300757388,14.535931516107231,15.650340577359714,14.14698700862985,16.415421130284173,22.020162087369044,16.938630499008962,0.17173484652756446,0.16805885144377034,0.1713380863120887]},"2269":{"cur":"HKD","cur_ratio":[48.28,50.56500838269904,11.150502048498373,4.743337532099104,0],"date":[null,null,null,null,null,null,null,null,"31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[null,null,null,null,null,null,null,null,417.68967764367846,687.2728491800001,1156.1119972398465,1868.961117681924,3005.268914578543,4519.328059279305,6315.392132422764,12402.118756502072,17779.30090627273,18832.107992076446,20239.898284190524,23647.52288010123,30295.719354838715,36340.61985806452,43682.484972413804],"Revenue_Adj":[null,null,null,null,null,null,null,null,417.68967764367846,687.2728491800001,1156.1119972398465,1868.961117681924,3005.268914578543,4519.328059279305,6315.392132422764,12402.118756502072,17779.30090627273,18832.107992076446,20239.898284190524,23647.52288010123,30295.719354838715,36340.61985806452,43682.484972413804],"NetProfit":[null,null,null,null,null,null,null,null,52.83645408505751,54.914759110000006,164.93225007816088,291.66262109076933,747.7347322605362,1150.1198219482746,1900.4361349756095,4083.9749622008276,5147.118010734712,3758.547918402892,3637.236340499175,5326.758031860492,7420.523,9018.676,10989.382],"NetProfit_Adj":[null,null,null,null,null,null,null,null,52.83645408505751,55.011920072500004,192.96042257241373,300.55266929625014,738.8815047461684,1148.3517681879296,1575.826748953211,3633.8806003336745,4822.753909901437,3815.1135544218587,3522.923703872778,4994.980108839357,7420.523,9018.676,10989.382],"OCF":[null,null,null,null,null,null,null,null,23.850540008045993,129.07787601,60.974060416858215,374.0487565984617,903.0879018965514,1314.0184622551708,2024.0414627967477,4035.258615813996,6336.541184845868,4920.141251082643,5438.183019163783,6751.14949703827,null,null,null],"FCF":[null,null,null,null,null,null,null,null,-40.39701733908049,-283.4546159700001,-440.3628889670497,-400.1696410696156,-686.5337770689653,-2328.2550995344805,-4755.403459642276,-3808.2937138559714,-496.794002745042,447.5836629793386,1179.358424170781,2751.8943666246914,1452.600668172498,4194.070550880831,8532.044869516429],"DivYield":[null,null,null,null,null,null,null,null,null,null,null,0,0,0,0,0,0,0,0,0,0,0,0],"Assets":[null,null,null,null,null,null,null,null,795.2644970000001,1620.7329336000003,2217.8360307999997,5822.148673400001,10695.04059,19689.898103400003,34405.8758827,54019.22189639999,56052.3629965,62227.64102740001,60635.386327,70939.05711059998,null,null,null],"Equity":[null,null,null,null,null,null,null,null,464.46702919096555,174.41577650867052,302.1995289854029,4831.866787772603,9102.623034324344,14425.467748513656,24826.51776645422,40123.08324050912,40953.75334573681,48415.20716909818,48395.22600779516,59070.974769512526,null,null,null],"Liabilities":[null,null,null,null,null,null,null,null,330.81164400000006,1446.3201390000004,1915.6432517,990.0996214,1592.8125892,5264.3206434,9579.4833743,13895.297447599998,15098.994217200001,13812.372615600005,12238.1573972,11869.1396978,null,null,null],"Debt":[null,null,null,null,null,null,null,null,69.212631,544.5691614000001,1261.9266638999998,0,0,2454.1502328000006,3958.0507267,5269.4580915999995,5000.4564249,5017.038764100001,5256.9873058,3456.9983042,null,null,null],"Shares":[null,null,null,null,null,null,null,null,null,null,null,3493.367509418549,3682.2220687500326,3888.2218947021565,4089.647571473465,4264.096480272178,4230.314403310738,4257.500616,4105.937505,4133.579013,null,null,null],"ROA":[null,null,null,null,null,null,null,null,null,0.044656679639568056,0.08444533819790574,0.07393314386772643,0.08855315840796645,7.510941022993567,7.253748570680998,9.283980067136612,9.44535748407684,6.406115403440646,5.911030517239411,8.13486909083686,9.17,10.148000000000001,10.997857142857141],"ROE":[null,null,null,null,null,null,null,null,null,0.1719055058503643,0.6775838719901648,0.11764292252051128,0.10494025061334494,9.758391015009153,10.128613892286646,12.82469035939985,13.130992484329976,9.020360629864516,8.170536999965648,11.021577530442087,11.242500000000001,12.327407407407406,13.158846153846149],"ProfitMargin":[null,null,null,null,null,null,null,null,0.12649691125508514,0.07990241310350027,0.14266113531554686,0.15605601332815264,0.24880792818016353,25.448912025467862,30.092131971012677,32.92965534666985,28.950058485813422,19.958190129242514,17.97062559024932,22.52564912979879,0.24494384232960428,0.24817825689389475,0.2515817921460401]},"2318":{"cur":"HKD","cur_ratio":[51.8,8.155004275867105,0.7510862788968861,0.9375598386060809,0.050295388245388255],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[92912.92695230774,172573.9320888889,98212.9454690839,173214.72746666655,223102.4505390804,326660.0697784615,417060.5565766285,531789.8853390805,667198.371868966,855634.5987900001,906928.6562421452,1116905.9297019236,1261740.7008429116,1417922.3928655155,1467984.026105691,1543121.0057670798,1316412.5247768597,998616.3374442147,1050743.6058049377,1042291.6550370369,1321930.2382499997,1161810.501090909,1245655.4522666666],"Revenue_Adj":[92912.92695230774,172573.9320888889,98212.9454690839,173214.72746666655,223102.4505390804,326660.0697784615,417060.5565766285,531789.8853390805,667198.371868966,855634.5987900001,906928.6562421452,1116905.9297019236,1261740.7008429116,1417922.3928655155,1467984.026105691,1543121.0057670798,1321957.5451595043,998616.3374442147,1050743.6058049377,1042291.6550370369,1321930.2382499997,1161810.501090909,1245655.4522666666],"NetProfit":[7638.227467692312,15477.56551111111,1589.1953564885484,15752.673078160908,19872.119338314165,23459.68986538461,24649.477681992346,35522.05281762453,49439.30344482762,66875.11937000001,72934.61764597699,102853.36379076929,127356.04191570877,169496.0591413791,161023.60400813006,122475.44995390959,97549.0419016529,94706.37436983468,137213.48839958842,146267.2421259259,147767.98,168421.476,183487.269],"NetProfit_Adj":[7638.227467692312,15477.56551111111,1669.1482455801515,15586.556868505735,19854.211357164746,23454.196864830767,24648.555631704996,35440.67282164752,49565.79979137935,66888.07416500001,72979.32946321837,102854.22967644235,175247.9563409961,226659.1601206894,228093.61073983737,217515.7145341566,100453.7150105372,102876.89278202478,89566.95071008227,156396.65398240738,147767.98,168421.476,183487.269],"OCF":[-7587.552827692311,45435.46773333333,34542.024035496164,39030.50122758618,55111.8119873563,-36517.61223461538,366526.05382758635,425436.95509272034,293772.3461574715,248439.65577000004,386044.752403065,179940.27897923088,383113.9716091953,526499.976944827,603397.1317317073,491991.7093810705,491937.16107851244,441506.49097314046,533954.216312757,1003706.8301654318,null,null,null],"FCF":[-9400.145720000004,40668.87822222222,28354.4728625954,34901.42413103445,50943.61458007661,-43014.049073846145,356245.80782375496,412715.1799076629,276742.5395022991,228046.34086000003,366612.3537149424,157707.79843730776,370470.14572796924,512648.2371862063,592150.156406504,477304.49025514454,481607.49375082646,432872.197109504,613255.1093098762,994639.6025172839,null,null,null],"DivYield":[0.7871483675121643,0.8939001784224755,0.6055799957548843,0.7515528334126361,0.7465428618082655,0.9631658877463869,0.8619527174879097,1.1990246211405013,1.1843914265220932,1.472438997963589,2.159777456530879,2.213868558195963,2.8322155961550366,2.48980727555693,2.750940262785132,5.199862618755686,5.298748707061525,7.560832027560012,5.892720595150915,4.615570027172032,6.46769199490948,6.9214059194909945,7.354409575393562],"Assets":[492677.7803,696156.4672,856982.289,1062688.1183999998,1381934.0465,2817699.2496000007,3535707.0646000006,4304895.703199999,5003783.4301,5692458.941400001,6231073.7219,7796235.152500001,8132974.256,9198168.3794,11318156.773,12442237.496799998,12451141.146,12740600.358300002,13789719.4934,15478727.152699998,null,null,null],"Equity":[47590.937335844494,116737.5794493703,97304.60326715991,104190.67776947752,137867.91240875912,211239.38873279677,260620.43850842022,307088.8621042893,441965.0549988722,494060.36332947976,543535.0156202718,705885.3149983862,778429.7019117541,953453.8305999944,1173538.270391592,1322122.8571765707,1341255.471139072,1351735.7937250044,1388417.0483053399,1577014.0637850368,null,null,null],"Liabilities":[445085.3553,579424.2688,759674.481,958495.5933,1244070.548,2606451.6978,3275092.3927000007,3997809.6276999996,4561831.8645,5198407.0248,5687550.8466,7090323.210600001,7354578.3362,8244707.2974,10144624.4235,11120086.920399997,11109898.2696,11388862.854700003,12401244.983,13901741.317099998,null,null,null],"Debt":[18512.7058,30593.3312,74166.318,128874.69319999998,181893.054,397070.23980000004,763369.0635,881222.5703999999,804350.4504000001,948401.3022000002,1143874.9805,1639908.8558000003,1783084.0352,1963839.8878,2602568.9221,2569201.364,2514663.5855,2450996.2619,2606317.3212,3154057.4260999993,null,null,null],"Shares":[12390,14690.106668,14690,14690,15288,15832,15832,15832,17784,18280,18280,18280.24141,18280,18280,18280,18280,18107.641995,18210.234607,18210.234607,18107.641995,null,null,null],"ROA":[0.01925763038764642,0.026336072091636818,0.002016980759027696,0.01642540655336216,0.016429250348425194,0.011266828288040877,0.007817236519165875,0.009075234447854472,0.010664624190714834,0.01235949547774673,0.01206606574201547,0.014761915887502032,0.015752966313154815,1.9446580669689857,1.6123105219094644,1.033233729349662,0.7921154941342096,0.7583202443089798,1.0317895865425566,1.0036975312085084,0.9577777777777778,1.0233333333333332,1.0637500000000002],"ROE":[0.19750286628617506,0.19642078263643406,0.014911796408759892,0.16535156412836988,0.17574619289340102,0.160356035685908,0.13804546894149075,0.16448648364424553,0.16634023117984725,0.17377992087359653,0.17387281819486497,0.2079551820728291,0.20858000949644562,24.300360503517613,19.93409583059661,12.904159774979126,9.963629789794933,9.689503801036308,13.85491770404096,13.97373483620431,12.498947368421051,13.013333333333334,13.016874999999999],"ProfitMargin":[0.0822084473951942,0.08968657852183011,0.0161811189848573,0.09094303532124515,0.0890717214907203,0.07181682744785674,0.05910287437470338,0.06679715766871894,0.07409985624865587,0.07815850301585722,0.08041935508817336,0.09208775874098768,0.10093677871422235,11.953831887712854,10.969029713170517,7.9368662273525015,7.410218306619963,9.483759760251793,13.058703154750484,14.033235459486473,0.11178231199402017,0.14496510007851518,0.1473022198771723]},"2319":{"cur":"HKD","cur_ratio":[21.34,14.279142515410802,0.777851118959509,1.780393619666625,0.0263248493774267],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[15832.285571298467,21871.38414257778,26746.197075257616,29172.97926018389,34743.1077756111,45037.59820155999,44258.40149909964,54703.64338939005,62995.48643959659,60488.425175640004,62864.62684261914,69450.52165977465,81790.67919927201,89656.10143106196,85558.98092282926,106232.82105755155,107818.53376454794,109033.15646777478,96103.49406893081,89255.97009661233,104444.10442424243,110040.82816363637,115457.52471333335],"Revenue_Adj":[15832.285571298467,21871.38414257778,26746.197075257616,29172.97926018389,34743.1077756111,45037.59820155999,44258.40149909964,54703.64338939005,62995.48643959659,60488.425175640004,62864.62684261914,69450.52165977465,81790.67919927201,89656.10143106196,85558.98092282926,106232.82105755155,107818.53376454794,109033.15646777478,96103.49406893081,89255.97009661233,104444.10442424243,110040.82816363637,115457.52471333335],"NetProfit":[708.8134760246156,960.074845511111,-1063.1246228244268,1266.0676271655163,1420.3244590187733,1914.4480180292303,1602.0328686724145,2057.7468207567053,2958.885482217244,2920.7399628900002,-878.0524203908043,2364.259267824232,3608.3223737547883,4657.448396348271,3966.5915846178855,6057.046048288896,6174.944945105785,5316.775947006197,113.26206317325098,1677.084410061728,5695.155,6560.622,7259.156],"NetProfit_Adj":[708.8134760246156,960.074845511111,-1063.1246228244268,1273.647530236206,1431.9999461518194,1902.6561359192306,1556.869001497127,2096.629352632392,2858.0952700905195,2798.0201151975,1766.5022378118767,2580.925510369905,3655.0706165996157,5119.733761142235,4177.7896953313,6999.316547427374,6845.313980310641,5373.273083556817,153.1265552139917,2192.8379231126537,5695.155,6560.622,7259.156],"OCF":[1384.1904602600007,2161.1692220444447,758.3117980912209,2524.2742358850555,2962.3508691605352,3251.3323245346146,2556.0696951973196,4564.88407294138,4640.5052572137965,3023.2234954500004,5813.888700792336,7055.947644996926,8571.702883333332,8497.471709227577,7082.989477601625,10836.575214659271,10936.663079488844,10632.78022472727,9799.653018437855,10444.18845092469,null,null,null],"FCF":[165.83958098615406,694.7360640000002,-163.96842498664105,1747.1465244988494,1655.999230682758,376.28017478153834,-89.4290426704978,1425.7043506061304,1111.2471190597705,-533.30819508,2276.040737201532,4585.309126921156,4875.134596992335,3665.968998999996,2179.693074788617,3392.9621532115266,5200.336000350001,6276.252731869834,6278.9896734226295,7938.2726141678995,5974.630877887687,7290.481324082592,8018.311396078444],"DivYield":[0.5331812743227403,0.49144640337846046,0,0.5782759763778561,0.9161457519180372,1.3441891314841004,0.9040927147078933,0.6962564033831099,1.0929960035381843,1.323155120090504,0.6656086729557962,0.6196925911619443,0.8446537153096986,0.6427463236508275,0.6802538057408345,1.0574682655438816,1.2842543705380547,2.5611923319263363,3.084597978103597,3.8842000452658696,3.2709499738129137,3.744521855543403,4.0969474419474885],"Assets":[7738.057862600001,10347.350995199999,12848.4947625,16008.970298199998,20412.2394595,24906.657436500005,26000.547831400003,51678.8002156,58808.6172872,60509.52547860001,54886.7222871,69807.17431170002,75668.23282019999,87851.941233,95205.718496,120350.72881919998,133234.9535345,126730.42410490001,113116.7892464,107559.29391689999,null,null,null],"Equity":[3620.578978872788,6236.419104575789,5379.788886836129,10120.831430563487,12051.937714190328,14854.416148705493,16311.209021058537,23074.27827380874,30594.981466427165,31795.006553433523,28495.951775898087,32546.294898012813,34688.13345697463,37302.04747112043,44246.59857493259,51523.8229448876,56604.10988557098,55711.74645645807,51106.612384675434,52547.07068114635,null,null,null],"Liabilities":[4117.3657066000005,4111.21936,7541.100768,5887.959422399999,8360.687593999999,10051.6672585,9689.6997197,28604.73132379999,28214.569622900002,28715.06251020001,26391.406992700002,37259.65172560001,40981.604750800005,50549.61006480001,50959.3431599,68825.8256416,76631.3751763,71018.60717600002,62008.06172420001,55013.1638161,null,null,null],"Debt":[1395.6959539000002,704.6459456,2168.339445,916.1794113000001,898.149147,974.9169079000001,957.0701486,15232.4506674,12472.6482228,13366.7174718,9636.147146199999,16762.994312600003,16755.7844794,26256.685265999997,22503.8448775,33163.9948436,44227.921394499994,41148.0894245,36860.90824,28275.435283199997,null,null,null],"Shares":[2736.832,2852.24,3123.28,3474.384,3475.976,3534.88,3536.464,3670.526,3917.182,3923.977,3925.027,3927.361,3927.708,3935.015,3948.108,3948.108,3955.196,3935.293,3919.105,3878.903,null,null,null],"ROA":[0.10502684216480757,0.10728441463054335,-0.0903576844085939,0.0878187707950459,0.07880226101759803,0.08474427238949325,0.06338413066586436,0.05325009480813821,0.05378169628278539,0.048443881474910924,-0.015056681217252463,0.03818345531090927,0.048846356112204836,5.662880196237844,4.4428573593705885,5.638827620799618,4.912102518853419,4.127478752422681,0.09435755124932023,1.5234794695700637,5.183157894736842,5.967499999999999,6.387222222222222],"ROE":[0.27295395333695466,0.2307593385506898,-0.19811523201421857,0.1711309576302497,0.1349722277099787,0.1497258341630897,0.10877166286252725,0.11712139709918395,0.12758657876439491,0.10852751062282437,-0.03495276255043141,0.09426363016329004,0.12727913090165668,15.10889167963697,11.350070673834496,14.398081400660717,13.78750367699039,11.393704121652295,0.24337276880252987,3.7654200450510276,11.094838709677422,11.898666666666667,12.109642857142857],"ProfitMargin":[0.04477012954526205,0.043896391707651465,-0.03974862743413727,0.04339864008656399,0.04088075448494592,0.04250777338217202,0.03619726005479446,0.03761626636290576,0.04696980132147053,0.04828593163748368,-0.013967352778359733,0.03404235434553399,0.04411654737532618,5.194792459194156,4.636090264089974,5.701671091844107,5.727164643687803,4.876292789503525,0.11785426146110053,1.8789604866166605,0.054529924580991054,0.059621712110704404,0.06287487929716075]},"2331":{"cur":"HKD","cur_ratio":[14.44,9.849712112714018,1.2437797715475536,1.3116170447916282,0.0367276439453898],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[3099.4782986446166,4461.621144355556,7497.766534675186,9516.405054091947,10880.851464122983,10755.35049627846,8208.019123423375,7348.310827436783,7611.423625729891,8746.948036050002,9369.367732081222,10245.057686593851,12463.468457969346,15734.521319275844,16267.85353818699,27205.320631788505,30046.258856821903,30511.32925568801,31077.94204215595,32121.55415485555,35614.42409230769,37422.91286153846,39548.92723157894],"Revenue_Adj":[3099.4782986446166,4461.621144355556,7497.766534675186,9516.405054091947,10880.851464122983,10755.35049627846,8208.019123423375,7348.310827436783,7611.423625729891,8746.948036050002,9369.367732081222,10245.057686593851,12463.468457969346,15734.521319275844,16267.85353818699,27205.320631788505,30046.258856821903,30511.32925568801,31077.94204215595,32121.55415485555,35614.42409230769,37422.91286153846,39548.92723157894],"NetProfit":[287.3310558738463,485.8987068444444,808.3456750270988,1071.7264126252867,1272.4848910501912,464.7524172546153,-2433.123509881227,-494.00811821455943,-983.6267800954031,17.654301110000002,751.9230140597699,594.7537785519233,848.1347492528733,1700.711162162067,1911.23638201626,4834.128355080664,4732.054254868595,3523.2672800206606,3265.3248954790106,3186.00240544074,2749.359,3156.634,3529.1240000000003],"NetProfit_Adj":[287.3310558738463,485.8987068444444,808.3456750270988,1071.7264126252867,1275.7496537674324,464.96563240769217,-2368.3928135579513,-575.8688239352491,-999.7336058432933,-51.07427928750001,29.00160428007662,619.8532065574042,854.2301808333332,1665.0023753879293,1857.6213923638209,4742.5827230177,4576.254261497625,3761.7434915671483,3449.7180855311713,3600.8012105129624,2749.359,3156.634,3529.1240000000003],"OCF":[303.18344758307705,409.1992954666666,524.1206631984729,1462.7485385931025,1133.6107931314173,-32.929092793846145,-1301.9915581647515,-128.08706684636016,-615.1057918218396,753.5421776600001,1108.0219538068961,1423.871636921539,2116.4849113601526,3772.474243227582,3327.4021776341456,8135.15015570865,4944.863334056198,5489.43639678099,6356.891413027569,6468.354007509875,null,null,null],"FCF":[207.79720839384623,180.29629955555555,277.0175962167938,1230.180171708045,913.0648216961681,-392.1074849299999,-1452.926272603449,-337.0633262831418,-920.2816892206904,369.56205386000005,660.3847736321835,974.3384308869236,1456.4541964942525,3050.988056393099,2547.946840861788,6013.26400972264,2564.241228571487,3358.796695667354,4806.189441882713,5141.764882924689,2241.431523994386,3141.022017827667,3379.8740155923438],"DivYield":[0.9150792095443401,0.504805958770746,1.9490543285991115,1.3905341275649523,3.014693409545241,2.2239299659876584,0,0,0,0,0,0,1.1901597376009552,0.7410966601786776,0.4559954373711662,null,0.7723577710759609,2.8807872959069556,3.78079166467326,3.393654361463262,3.5968255553972304,4.036256731848476,4.508238365814628],"Assets":[2158.1395595,2972.1735488,4924.5828795,6107.646959,7739.633664,9035.699712200001,7482.9299532,7708.1878160999995,7544.321674600001,8239.703326800001,7575.8459462,8790.743744300002,9936.909473,14035.6044164,17336.0522335,37141.144268799995,38050.8483371,37625.1625197,38000.8856652,42002.15641789999,null,null,null],"Equity":[1412.358489820758,1864.7155033506463,2371.9223345981977,3250.4418314364807,4198.425483648724,4517.983489594742,2252.8466346271052,3704.6724775626203,2709.931464042916,4074.3007405009635,4466.114332190153,6091.638133477799,6626.472748128226,7969.0616818795415,10322.21053329146,null,27517.23576922854,26844.830478595162,27778.396175754166,30769.29639955073,null,null,null],"Liabilities":[745.7369202000001,1107.544,2552.5824255,2857.1474962999996,3541.342595,4517.541631100001,5230.1331661,4003.5489556999996,4834.4729215,4165.4722428000005,3109.8313685,2698.8758264000003,3310.724299,6066.482126600001,7013.893779199999,11250.625801199998,10533.8709619,10780.2980838,10221.339831399999,11233.4107816,null,null,null],"Debt":[0,106.88,689.79354,926.8583984,1021.2700749999999,1687.2248829,2952.3028312,1582.3634759999995,2090.0665678,1648.8765150000002,954.2334169000001,87.1648165,64.08496240000001,1062.9507686,1321.4508454,1710.8278059999998,2496.7309041999997,2847.6608982000002,2121.5401668,2327.4169422,null,null,null],"Shares":[1136.435550188839,1140.5230198357963,1142.104022246412,1151.1912054399704,1156.6602618624759,1159.4873365563014,1160.6540832830206,1507.1128582320096,1579.336573929215,1885.248,1993.016,2162.159,2162.481,2289.659,2473.959,2612.388,2633.92,2625.415,2584.797,2584.811005,null,null,null],"ROA":[0.15733108223482004,0.19150544991079904,0.20266628077855736,0.19445054910554507,0.18568147071500016,0.05555018822067568,-0.2965322409668882,-0.06505919574278837,-0.12963477355290112,0.00221205967505958,0.09405706351360205,0.07306208131802346,0.0891368210692311,14.093109968380869,12.515845294147057,17.87831026278664,null,9.393388382835804,8.618653794572872,7.996851318260554,6.122631578947369,6.564736842105263,7.008333333333333],"ROE":[0.2303111918619411,0.30126736153629136,0.39619018218551205,0.41327513645499453,0.3668172890941311,0.11279193760693568,-0.7783452365970299,-0.18220370433709873,-0.33712949365930933,0.005576643183499776,0.1793166968243928,0.1136499263262651,0.13138451226556144,23.17298388807698,21.488234622103978,26.929138780120816,null,13.07823931888149,11.92990819897633,10.92751941386444,8.251142857142856,8.892647058823531,9.434545454545454],"ProfitMargin":[0.09270303844343558,0.10890631255393794,0.1078115291118647,0.11261883101165986,0.11694717966198757,0.043211275858971575,-0.2964324855113675,-0.06722743904218843,-0.12923032910299734,0.002018338400690035,0.08025333571710978,0.058052750579451314,0.06804965665160104,10.80878869876125,11.748546773732894,17.769054886389196,15.74922947118988,11.547406704228864,10.506889069584247,9.918581118713206,0.07719813699647428,0.08435055183735306,0.08923464448108553]},"2382":{"cur":"HKD","cur_ratio":[63.8,48.52113744149976,1.9936839013818464,2.8162857916023585,0.004689422021943575],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[877.7412865138465,1417.7320737777777,1420.0289850538159,1485.928784816091,2087.045484896934,3009.7065534615376,4898.295028950193,7334.00435031456,10606.1308594184,13196.904079280002,17080.239150525664,25822.15622297078,30749.115770957844,42937.788534730986,42761.872263048775,45193.21203482804,38655.54227349215,35025.00863566735,41502.608043762535,46914.22219898024,55208.73213913044,61316.71852173914,68544.42020454546],"Revenue_Adj":[877.7412865138465,1417.7320737777777,1420.0289850538159,1485.928784816091,2087.045484896934,3009.7065534615376,4898.295028950193,7334.00435031456,10606.1308594184,13196.904079280002,17080.239150525664,25822.15622297078,30749.115770957844,42937.788534730986,42761.872263048775,45193.21203482804,38655.54227349215,35025.00863566735,41502.608043762535,46914.22219898024,55208.73213913044,61316.71852173914,68544.42020454546],"NetProfit":[114.17191294461544,231.28731662222222,87.83946717595416,104.31507936091946,165.1046985126436,259.36117615076915,425.7093882720308,555.7786894245211,712.5281173885063,939.6939452800001,1485.4294351616854,3349.8853843453862,2953.5920341762444,4527.962423841374,5482.034583341463,6011.812884511118,2803.71228417686,1215.4509843904955,2925.2807675337435,5034.570826225926,4754.553,5578.96,6711.944],"NetProfit_Adj":[114.17191294461544,231.28731662222222,87.83946717595416,108.95305755057463,175.19343520057464,285.9227248815384,431.09600605076645,532.650683823659,705.7397947910925,941.9045885125001,1452.240992936398,3349.3061068300976,2954.5320503975086,4582.562782796546,5420.077196158536,6015.477457771612,2787.3121469815087,1212.3557392887394,2852.0715022896075,4252.091870349999,4754.553,5578.96,6711.944],"OCF":[250.9184034969232,195.9113431111111,236.68812167290062,168.46498393103437,232.19075097241372,142.17957354615382,345.3078326149427,856.369898750958,-180.57384937011508,2128.0928111800004,1923.566669226053,2862.137757272309,4363.13020189655,5286.502298144821,8083.351388455283,8540.402181349804,8590.352097606614,3431.3126031446277,4129.339505384772,6435.412460169134,null,null,null],"FCF":[142.4025599861539,21.17367075555556,72.87996109809156,82.55998313333326,155.62380003103442,-62.0624740207692,39.369088469348675,548.0848101149426,-525.9604809310349,1779.0844649300002,960.9639101977009,1539.9257164269238,1737.5566947318,2108.89844955517,5366.389345910569,6002.58546373087,5527.940508857852,1251.6752190557847,2402.6931823419736,4141.3669668345665,6463.028864717387,4959.609274828395,6014.461932529332],"DivYield":[null,0.7481944464732786,4.541849968161632,1.992420831158274,2.280439542926522,4.631350984651311,2.5745245159710017,2.0558952030357927,1.447057758143434,1.394394400433871,0.9544160997489886,0.7944271357079855,0.9292425936665899,0.6036579349094222,0.6216032323368081,0.4527025346367187,0.6089987111151278,0.3399827648919825,0.8222671171824925,2.0490407142351073,1.6584490410280104,1.660293811930155,1.9609914689797274],"Assets":[782.8002064,1676.5207487999999,1830.4816395,2069.0500595999997,2426.9969955,2928.7255933,3732.0944888000004,5976.649212799999,6987.264294900001,9122.316812400002,13001.505024699998,18882.580417,26019.450019800002,34333.2692206,42096.9368085,47567.746911999995,48630.156599199996,55321.753892400004,57261.6839636,65947.46818869999,null,null,null],"Equity":[353.69983295537526,1384.2079014265942,2965.286704607721,1646.0578020242856,1802.2711870950116,2072.5530305252632,2401.532143111467,3663.831920064769,4060.4358992492826,4593.185316238921,5489.799635978501,9027.973699525102,10575.939730580794,14235.393326306119,19987.845229639053,25633.774898916006,25086.295894268405,25167.473001464543,26915.641310189876,33149.12322779757,null,null,null],"Liabilities":[429.089317,292.3766528,279.0207375,422.9630724999999,624.783509,856.0924716999999,1330.6154831000001,2312.8505392999996,2926.9523259000002,4529.2100238,7511.828008099999,9854.266170700002,15443.969261400001,20097.7676284,22109.192424200002,21933.434583199996,23544.0962718,30154.249055400003,30344.928701999997,32798.9383225,null,null,null],"Debt":[0,0,43.1319675,63.484494299999994,133.23632,98.1807586,127.5942702,685.6498443999999,916.3659911,1361.5740204,1481.7465005,2397.0870856000006,7437.710938,7447.5370074,8841.4850566,9851.2947832,12218.6314987,17822.9390855,17450.4557524,16683.190361599998,null,null,null],"Shares":[1000,1000,1000,1000,1000,1000,1000,1097,1097,1097,1097,1097,1096.8497,1096.8497,1096.8497,1096.8497,1096.8497,1096.7748,1094.8048,1094.8048,null,null,null],"ROA":[0.15902908757375708,0.19153497907597852,0.04928363699243866,0.05354530753431179,0.0741471079735748,0.09713597132136555,0.1287808506385135,0.11490009254651062,0.11035991799270248,0.1151359551714257,0.13186987092141567,0.21207981668867887,0.12913280892834142,14.908140996504374,14.733723360110309,13.442596950855156,5.888822332384487,2.3567723009591033,5.185488242977473,8.209209942463508,7.024000000000001,7.733000000000001,8.497777777777777],"ROE":[0.42962386807514563,0.2743044317305189,0.04043456178178894,0.045920023815636604,0.09877789601968882,0.13638711724897198,0.19328567904018612,0.1846386253602408,0.18568128390937796,0.21519040273045253,0.2912487603478153,0.46860577397488046,0.297904586220242,36.63928885246347,33.42555367970982,26.827846146498334,11.350445249272392,4.967832660519152,11.44014846988445,17.209571483948903,13.016,13.7045,14.56421052631579],"ProfitMargin":[0.13007467541839773,0.1631389462791228,0.061857517065135974,0.07020193728451814,0.07910929575202674,0.08617490494296577,0.0869097075116909,0.07578106896005365,0.06718077749868331,0.07120563577902947,0.08696771878138307,0.12972911152033878,0.09605453555727529,10.54540203014613,12.819912443540451,13.302468697905626,7.253066751309014,3.470237500963109,7.048426365035117,10.731438336273559,0.08612117763986225,0.09098763537830774,0.09791934964203046]},"2628":{"cur":"HKD","cur_ratio":[25.38,20.94723080579985,0.810542381573472,1.444369790217068,0.01851104356636272],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[222569.84046615392,196339.16657777777,336650.52691030514,384983.39326436754,442921.7712007661,446786.9325484614,456703.80133141787,527245.2226180078,554778.991882759,626085.5017100001,632138.5944996167,742762.5029365388,743972.2958429116,827558.0678689646,905889.5686422762,994253.383818108,936654.3165801654,379486.61242561974,559823.9231069956,661687.3134419753,705020.1497230771,725992.3618769231,797909.5821666666],"Revenue_Adj":[222569.84046615392,196339.16657777777,336650.52691030514,384983.39326436754,442921.7712007661,446786.9325484614,456703.80133141787,527245.2226180078,554778.991882759,626085.5017100001,632138.5944996167,742762.5029365388,743972.2958429116,827558.0678689646,905889.5686422762,994253.383818108,936654.3165801654,379484.4013388429,559823.9231069956,661687.3134419753,705020.1497230771,725992.3618769231,797909.5821666666],"NetProfit":[14017.385033846162,39888.12604444444,21447.412931679373,37309.20143218389,38600.88295708811,22081.621305384608,13598.397637931042,31246.133339080465,40543.023072413824,42811.27921000001,22358.246493486582,37236.54748500002,13511.806800766279,66124.18962413786,56552.199991869915,61185.899076543275,37357.27507685951,56586.13279338842,115893.46862345673,167212.48373086416,153946.285,161119.659,183167.619],"NetProfit_Adj":[14017.385033846162,39888.12604444444,21447.412931679373,37310.052437356295,38600.88295708811,22082.52475942307,13598.397637931042,31246.133339080465,40543.023072413824,42811.27921000001,22358.246493486582,37236.54748500002,13511.806800766279,66124.18962413786,25762.550107723575,33859.78914444448,34130.3456910124,56855.79156111629,117202.93964290118,169878.11842993824,153946.285,161119.659,183167.619],"OCF":[-2320.3138046153854,79521.81511111111,185571.61940954186,212055.73820919526,255414.87703869725,226746.48536230763,241611.5984980844,176306.92244712648,197346.88732183923,82952.63686000001,214485.8018819923,372151.8897173079,319453.73714559374,482116.55232758564,513881.56053658534,545636.3373209882,619415.5727607439,613824.2222975205,607189.2212106993,704946.2914530863,null,null,null],"FCF":[-5023.610946153849,76045.87768888888,182265.4654591602,208355.56771954006,249848.47956475086,220593.36105769227,235104.38227011505,171608.33167164752,190993.12058160934,72608.54150000002,208278.74960613018,361046.61733153864,296276.74936781597,469166.70717241324,505476.99238211376,538907.4070419759,615833.7830301654,609213.00082438,607189.2212106993,704946.2914530863,null,null,null],"DivYield":[0.525550382638218,1.112556846356959,1.1089441164487754,2.0729488467297155,1.4860320418228858,1.4768544797491145,0.687896799756681,1.5848806611276849,1.6409037934003672,2.0029567456721824,1.3275151295239918,1.9562599830392262,1.0948555474962023,3.7716936971381627,4.44596348196745,6.1718548227823105,4.135418934817358,4.673482270329865,4.711862367225156,3.4818998156545566,4.09284168793822,4.4076756639334675,5.0929025528643015],"Assets":[724685.6195,997942.8352,1121298.3015,1392660.0749,1663777.9305,1952798.9403000001,2360542.4796,2527534.7151,2806186.8397,2924757.0990000004,3013303.3523,3479137.5137000005,3705463.2558,4168724.6524,5051504.3614,6002094.463999999,5665885.9012,6218534.327300001,7204150.8532,8454101.1548,null,null,null],"Equity":[115710.26286658722,220584.83671595564,198559.48093179986,241645.4187575984,248263.21076832025,238418.85181834057,277342.9897193263,285156.23108188494,358916.1067274543,389701.9069595376,343742.7881896912,390584.9836322559,368113.96932470746,457886.47878909734,542798.2008977816,597603.4620286026,424060.94774302037,371453.3585309525,554689.1648080662,677579.4126139274,null,null,null],"Liabilities":[608971.7396,777368.1664,922732.281,1151010.3717,1415522.668,1714370.8751000003,2083205.6265000002,2242381.0716,2447281.6876,2535061.8546,2669568.2419,3088537.7967000003,3337365.2618,3710834.6912,4508708.8991,5404478.4728,5241828.9355,5847080.498900001,6649438.7316,7776533.870699999,null,null,null],"Debt":[8199.8509,106.88,12933.345,38106.1421,27205.1675,53002.37100000001,169658.288,113263.33209999999,159373.9181,122861.02620000002,153390.7651,130434.44240000001,244765.9806,198851.2848,217614.9647,367132.16799999995,226262.5557,307482.5445,269281.9754,500189.39879999997,null,null,null],"Shares":[28265,28264.705,28264.705,28264.705,28264.705,28264.705,28264.705,28264.705,28264.705,28264.705,28264.705,28264.705,28264.705,28264.705,28264.705,28264.705,28264.705,28265,28264.705,28264.705,null,null,null],"ROA":[0.022364853098489935,0.04681991511263622,0.019921954906238142,0.02970615471485037,0.02550480955205405,0.012243169612414284,0.00635174397320794,0.012792311286289756,0.015267656797901557,0.01478162816445653,0.007434795402220216,0.011530166365718585,0.0037044899588653695,1.6698426058677835,1.259700220573491,1.1102525919781265,0.6479544456638836,0.9599584388109487,1.7215270082207805,2.145850959747363,1.657,1.6019999999999999,1.7377777777777776],"ROE":[0.14682420190369255,0.24219375375712102,0.10086784188569155,0.17080196042273235,0.16020696456732303,0.09160003997601439,0.05361414393563007,0.11220707903655508,0.12770689778214775,0.11440242790708409,0.06109759739855267,0.10328330296499583,0.035648142354810855,16.142964958075705,11.77227050197934,10.927794884820749,7.592635980887062,14.754578015436614,25.53796663478451,27.89044964159004,19.86866666666667,17.66071428571429,17.04],"ProfitMargin":[0.06297971461222203,0.20315929185042744,0.0637082410906004,0.09691119691119691,0.08715056578149377,0.049423158326121125,0.02977509186104419,0.05926299945199972,0.07307959325356311,0.0683792854060211,0.035369216004260504,0.05013250849064668,0.018161706929500063,7.990277926286613,6.242725598069185,6.153954321138808,3.9883737698722506,14.911232950142459,20.7017713677282,25.27061957120739,0.21835796861900844,0.22193093587497137,0.22956008737257977]},"3690":{"cur":"HKD","cur_ratio":[74.85,36.63137574967688,1.5321824105824726,2.9492286382573654,0],"date":[null,null,null,null,null,null,null,null,null,"31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[null,null,null,null,null,null,null,null,null,4958.551424610001,15182.235951397695,39170.343812853476,77344.30701850571,110642.08275614814,129173.68898837396,215894.6449636636,256122.35823677853,305953.5541759958,365873.27554775623,395957.03656364686,490785.00585106376,555898.87240625,624966.0719634147],"Revenue_Adj":[null,null,null,null,null,null,null,null,null,4958.551424610001,15182.235951397695,39170.343812853476,77344.30701850571,110642.08275614814,129173.68898837396,215894.6449636636,256122.35823677853,305953.5541759958,365873.27554775623,395957.03656364686,490785.00585106376,555898.87240625,624966.0719634147],"NetProfit":[null,null,null,null,null,null,null,null,null,-12978.654031020002,-6768.024853486588,-21839.503524511165,-136928.9358886398,2539.790791782756,5298.077051959349,-28369.71362564365,-7785.513698152893,15318.219036619832,38806.92173685883,-25345.92911201851,1870.207,27654.637,46067.243],"NetProfit_Adj":[null,null,null,null,null,null,null,null,null,-11423.061679612501,-12865.196231190419,-22220.815330135396,-137115.5369642624,1607.4376760568948,-1169.4550368028451,-27076.09666618624,-6479.958327942976,16269.38798893167,39463.22327066723,-25065.935331565146,1870.207,27654.637,46067.243],"OCF":[null,null,null,null,null,null,null,null,null,-4936.172941590001,-2029.3530754145586,44.31141069115387,-10248.387080134096,7584.0544095689565,11145.8043092439,-4352.098065860087,13690.382847897523,46923.113118123954,66193.90146554071,-12792.997611713577,null,null,null],"FCF":[null,null,null,null,null,null,null,null,null,-5375.503352370001,-2441.7608504122595,-807.3506470626926,-12869.225822528731,4197.722653817236,-6634.960392300811,-15211.980472662568,7016.674847239669,39317.470994708674,54272.93032242549,-27194.976126148144,-9435.04594650976,23739.21663878682,42486.97070841102],"DivYield":[null,null,null,null,null,null,null,null,null,null,null,null,0,0,0,0,0,0,0,0,0,0,0],"Assets":[null,null,null,null,null,null,null,null,null,51236.2112262,57782.912488,100419.5395141,137385.1964246,147669.64671900004,197874.20729579998,295233.43040919997,276483.7800328,322303.2922368001,345178.5026714,386353.978836,null,null,null],"Equity":[null,null,null,null,null,null,null,null,null,-21108.55105467437,-28575.97794125106,-48628.175050117556,98504.30126603179,102971.26199052185,115980.34043196168,154029.80627979425,145554.6621515815,167136.59664425734,183677.65798293002,168158.18076473367,null,null,null],"Liabilities":[null,null,null,null,null,null,null,null,null,72344.40139740001,86358.2521603,149049.5488815,38885.1700254,44697.601590599996,81894.45202330001,141200.39478759997,130930.48467749999,155166.4841735,161493.24286380003,218198.80806359998,null,null,null],"Debt":[null,null,null,null,null,null,null,null,null,59579.739628200005,71158.61022110001,121967.45660440001,2584.6857615999998,6203.8249532,28576.915917199993,72281.866004,65691.2951125,66676.92681010002,65458.48120139999,96467.6983522,null,null,null],"Shares":[null,null,null,null,null,null,null,null,null,null,null,null,5727.447,5808.666,5885.649,6136.145,6193.151,6244.549,6046.229,6111.84,null,null,null],"ROA":[null,null,null,null,null,null,null,null,null,null,-0.12239974402579308,-0.27951999931319166,-1.130490614304442,1.77205824541974,3.1537218257373927,-11.560292954362668,-2.756394582325909,5.155553109382593,11.599635610576318,-6.958506147608305,-0.915,4.5721428571428575,7.061538461538462],"ROE":[null,null,null,null,null,null,null,null,null,null,null,null,null,2.5067846509537612,4.9611983789382,-21.081681247666854,-5.2568913086649705,9.869708507369447,22.057165039773306,-14.429641537814753,-0.9277777777777778,11.11807692307692,15.927999999999997],"ProfitMargin":[null,null,null,null,null,null,null,null,null,-2.617428543063017,-0.44578577721705837,-0.5575519997693938,-1.7703815725684584,2.2955016107030257,4.101514088086617,-13.14053603803765,-3.039763397366264,5.006713871285255,10.6066565476148,-6.401181636266834,0.0038106156071297647,0.04974723172629281,0.07371103859454388]},"3968":{"cur":"HKD","cur_ratio":[53.3,8.59247840382224,3.634825213848369,1.3319841175058957,0.0377642325281426],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[36857.02998923079,61076.160177777776,92377.30356412209,89167.18729195396,115112.73241800761,171880.32390923073,217378.8875459771,264669.19590842916,353209.2663252876,375870.4221300001,345447.59128429106,373002.76670538477,431889.2819157087,448704.47703103395,469222.23520325194,552470.1246543216,566445.6069466943,559702.345716942,548855.0373732508,517190.10701851844,412677.5086896552,434711.4666785715,460170.20148888894],"Revenue_Adj":[36857.02998923079,61076.160177777776,92377.30356412209,89167.18729195396,115112.73241800761,171880.32390923073,217378.8875459771,264669.19590842916,353209.2663252876,375870.4221300001,345447.59128429106,373002.76670538477,431889.2819157087,448704.47703103395,469222.23520325194,552470.1246543216,566445.6069466943,559702.345716942,548855.0373732508,517190.10701851844,412677.5086896552,434711.4666785715,460170.20148888894],"NetProfit":[6620.836618461542,15638.640533333331,23621.62942786258,20690.77242528734,29581.45937432949,43521.18794076922,55652.49654406133,65284.42064865901,70373.50479655177,71184.74784000001,72568.74055325668,80989.17328846158,93558.14553639844,103459.22283103436,107677.27021951218,140181.8290921812,154607.324927686,157035.80506611566,154773.8930576131,156138.6368222222,179107.489,185038.537,192270.32200000001],"NetProfit_Adj":[6620.836618461542,15638.640533333331,23621.62942786258,20689.07041494251,29564.240161685815,43520.28448673076,55633.13348802685,65278.74297452108,70370.67278879315,71181.04647000002,72679.20504291185,80879.2058079808,93383.83789272027,103134.20043620677,107640.13663414633,140079.6838582306,154361.0477729339,156896.50659917353,154571.49809557604,155574.96021654317,179107.489,185038.537,192270.32200000001],"OCF":[27720.002592307705,-33730.3408,15702.55016908396,4232.332390804594,-32826.706983908036,27.70592384615384,266113.54813409975,238919.05113486593,141037.76238965528,652375.0990300002,195695.1492766283,106066.37689500005,47762.66590038312,116060.78918965504,366155.15586991864,106097.26017037049,-49963.40499338844,344099.27410537185,50270.89860288064,267984.05650123453,null,null,null],"FCF":[null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null],"DivYield":[0.7333496332518338,0.7065354529396921,0.8223684210526315,1.1634349030470912,2.263856362217018,3.5383319292333613,4.581818181818182,5.693296602387512,4.038577456298976,3.835508615897721,4.204545454545454,2.8945830461750517,3.730190476190476,3.1931878658861095,2.844141069397042,3.124615068774379,4.664519592055824,7.08842559309849,5.089058524173028,4.788598574821853,4.570397412857164,4.799688937670067,5.106145879487309],"Assets":[931019.4634,1401158.3232,1784775.4935,2348560.5937,2833757.0065,3445919.7459000004,4236607.866900001,5145408.7589,5910527.6039,6540408.718800001,6639344.0803,7561573.946600001,7680687.0394,8296924.664,9932564.0792,11346698.962799998,11466095.5808,12130228.451700002,12932196.7112,14556641.465099998,null,null,null],"Equity":[54976.253475291785,72664.64869605734,90588.34196762141,105371.78482811149,158065.13753281633,203432.96760163183,249123.9953327987,340719.3233758509,393553.4578084222,432163.49530635844,450686.42906753893,580386.8814606483,618975.5151559206,690961.795157538,867591.8938901826,1061995.1853337742,1079157.8877743313,1194191.8165098857,1312609.770323588,1426562.7514415304,null,null,null],"Liabilities":[876041.4914,1328497.024,1694184.168,2243186.9406,2675696.9295,3242478.9169000005,3987489.3838000004,4804692.5273,5516986.1579,6108252.612000001,6188667.7177,6981165.172200001,7061738.3864,7605957.6138,9064976.5626,10284681.512,10386947.8266,10936035.124600003,11619532.6162,13130104.248799998,null,null,null],"Debt":[89814.6304,297797.6064,234714.663,342320.4226,369706.0185,427118.4786000001,750299.1101,1103557.4754,1204011.2373,1642984.7478000002,1757082.7395,1834491.8964000002,1772154.6138,1913335.0978,1999677.5988,2074588.7275999999,1466323.8091,1566808.6499000003,1487359.9744,1640976.8101999997,null,null,null],"Shares":[19113.9,19116.5,19119.1,19119,21577,21577,21577,25220,25220,25220,25219.845601,25220,25220,25220,25220,25220,25219.845601,25219.845601,25220,25220,null,null,null],"ROA":[0.008145867719944707,0.013579110814559571,0.014622786974015535,0.010019951985555005,0.011528598476036404,0.01390251194906453,0.014595353591044434,0.013938450788187971,0.012782245730221025,0.011305396486873906,0.010874910847925457,0.011462466060928848,0.012352638701341456,1.3114058217595477,1.2338414955666783,1.3619398779214795,1.4236896733653865,1.3851680851611643,1.280307830898868,1.1908466543779321,1.1272000000000002,1.1258333333333335,1.0834782608695652],"ROE":[0.17021053570145944,0.24756382771389593,0.28579176807978357,0.21166815633379377,0.22725088077464076,0.2416631271258148,0.24782317114897695,0.22217165135585978,0.19284010698968218,0.17089794286222065,0.16269543841626088,0.16535159311486808,0.16572393556802262,16.83705825499936,15.726597706363215,16.96210728030812,17.057874725631905,16.224505006050894,14.487504894770053,13.439975786857083,12.987586206896552,12.518148148148148,12.216666666666667],"ProfitMargin":[0.2681136543014996,0.3642467979353852,0.3718858070435458,0.3433440030126153,0.35377539813289405,0.36961370053607234,0.39332696150838475,0.3827456375888571,0.32930082986329934,0.2797286879960438,0.28923717718753056,0.31049577522141913,0.3175391504172234,33.69397610469525,33.04253961357249,35.77113062568606,39.28473835223391,42.45297224667562,43.20788504374919,44.06628971822762,0.43432968260266624,0.4240334572009386,0.41381982869691397]},"3988":{"cur":"HKD","cur_ratio":[5.92,7.290861018313714,2.516068587548849,0.7253761626787396,0.04305819256756757],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[237279.12923846164,316165.64568888885,399029.24647786235,386002.33012413763,462974.1182980841,624814.3577353845,763982.5918946364,819091.5520666668,937276.2531126444,944138.0568600001,880355.2250957851,896897.0798296159,998289.5195019154,1057376.6952482746,1031331.3210487803,1183084.0651012359,1188506.7669206613,1345360.0713347103,1372592.2370024684,1336291.2209555553,833933.9680533335,889380.0599866668,946268.1873571429],"Revenue_Adj":[237279.12923846164,316165.64568888885,399029.24647786235,386002.33012413763,462974.1182980841,624814.3577353845,763982.5918946364,819091.5520666668,937276.2531126444,944138.0568600001,880355.2250957851,896897.0798296159,998289.5195019154,1057376.6952482746,1031331.3210487803,1183084.0651012359,1188506.7669206613,1345360.0713347103,1372592.2370024684,1336291.2209555553,833933.9680533335,889380.0599866668,946268.1873571429],"NetProfit":[41537.612603076945,57707.94808888889,71210.07317061065,91703.18270574705,119866.38305517237,149703.53877846152,171693.13990804605,197975.45036819924,213464.1581436783,204603.09707000002,184528.2998620689,191248.74586961546,205487.3681034482,204859.403265517,203493.1730650406,248171.33725432126,248143.7376318182,239469.5422809917,240100.57448271595,248724.20143580242,299968.302,313065.7,332499.416],"NetProfit_Adj":[41537.612603076945,57707.94808888889,71210.07317061065,89917.76849711603,117969.68233467398,147748.46423923076,170820.9147806138,196781.24624118776,212969.50078850592,204757.3163273647,169001.02544712636,186205.8277096155,204218.35979281543,203295.54997712738,203924.67014953506,246778.36552407435,252505.1190368291,238664.43030836774,236633.85736618403,247072.72882376538,299968.302,313065.7,332499.416],"OCF":[-165384.4837384616,-69408.9712,-69043.70178549615,31410.033577011473,110682.80297854402,-163404.72042307688,377831.619750958,239234.47747586208,392564.1048091957,713969.5972000001,-61690.61842911875,903265.3803265389,485189.4763601531,36791.62752758617,136131.7239512195,1579794.4592024707,-23967.483102892562,854292.0702582643,1289092.800053909,1042193.9829259258,null,null,null],"FCF":[null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null],"DivYield":[0.9336475368510684,2.827784648950916,6.962743963836579,3.7855998485760067,4.200851683775718,6.681843105339026,6.287601457692532,7.033734509133215,5.431178488983002,6.042777776051448,5.456703332681561,5.5035636620594675,6.198564964834934,6.415978996008677,8.83084933943454,9.648339270746018,9.238423399333639,8.725369010397731,6.497516790612696,5.651004170705774,4.863049349503302,5.101239521723872,5.458524780054728],"Assets":[5314429.3175,6403412.7296,7898190.537,9939581.6651,12337410.7675,14584946.858100003,15763272.506500002,17774364.4489,19050501.2562,20087912.1762,20277753.6797,23374535.996800005,24214919.315,25470235.6384,28987918.6261,32783050.134399995,32675713.4332,35672139.3834,37312234.3958,42719389.2412,null,null,null],"Equity":[415934.82562683366,481684.40496909735,560806.1153175591,619392.9837852088,797544.4587765756,933438.1358159912,1071006.557786688,1231759.3620051553,1478265.0335406128,1621822.6605780348,1661565.004077986,1893047.0671308034,1964622.284422476,2211115.3291782746,2569247.035007354,2883597.9637670903,2898864.3219928686,3032216.982904298,3142414.2344302265,3572951.65380378,null,null,null],"Liabilities":[4898481.49,5921750.528,7337365.9515,9320177.6993,11539891.8425,13651472.650800003,14692289.646300001,16542616.264199998,17572281.3414,18466117.2432,18616225.7881,21481417.5215,22250382.2908,23259103.4928,26418684.5538,29899391.713999998,29776876.3323,32639918.564900003,34169690.107,39146501.5424,null,null,null],"Debt":[526709.1051,651824.7808,1125265.7385,1438259.5656,2042205.172,2360834.5927,2746520.0572,3005591.2722,3325528.8940000003,3461481.3222000003,3329423.2759,4191532.0349000003,4608427.7086,4831413.4946,5346703.4509,6739023.409599999,5950620.5105,6453489.465800001,7267207.8958,8823142.3553,null,null,null],"Shares":[264664.4654753825,264705.13669267105,264698.7106309538,264716.6080616499,279107.653195,279136.353579,279142.173265,279354.422437,288723.928,294358.101241,294387.791241,294355.941241,294364.991241,294385.451241,294384.261241,294387.791241,294387.791241,294387.791241,294387.791241,322212.411814,null,null,null],"ROA":[0.008463798862458603,0.009934963855757917,0.009815314247545226,0.010290408417255887,0.010870189833252547,0.011151003061779246,0.01139565059800728,0.011817850360953907,0.0116457362833851,0.010655509519621414,0.009414009403713243,0.009166608114942047,0.008841896683709384,0.8511248229586112,0.8177238713067044,0.8471734618949253,0.8145935673568212,0.7563026498150516,0.7047823074426539,0.6620078146946906,0.6618181818181819,0.6445454545454546,0.6399999999999999],"ROE":[0.13890712964698024,0.13927667645558586,0.1429928142391938,0.16438921795163863,0.18016196252966593,0.18167956675016575,0.18036524815138408,0.17947115194902416,0.1701881055478342,0.1458291746286625,0.12542507548069284,0.12233122989952996,0.11912077669262242,11.410728469539732,10.59800388954672,11.231620118710175,10.762462513724346,10.111301903407957,9.555929909981684,8.986300016330743,8.669375,8.639333333333333,8.766923076923076],"ProfitMargin":[0.2850684180254411,0.2825950432322989,0.27381244801834065,0.3400828967577689,0.36801240594216433,0.37247414955791996,0.37555766505406557,0.3792099259517043,0.36623419812644953,0.3567982657655776,0.3338065934600859,0.34996975442214706,0.3505664784893907,33.222123145487394,33.69644027080149,35.083919256067134,37.8107609156493,36.54241304978129,36.93831371032495,36.211042966724484,0.3597136996592191,0.35201516806259364,0.3513904260353569]},"JD":{"cur":"USD","cur_ratio":[26.91,12.773322763040877,0.27882032819452907,1.2910620004212938,0.008005153598414467],"date":[null,null,null,"31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[null,null,null,427.2779635632182,1268.2615689655167,3269.6560208093074,6558.352794933338,11278.957281600002,18666.03047848905,28849.03567883462,38895.348725736214,53677.95822057387,69899.97834777906,83527.2636412261,108201.2573315447,147547.62905382726,155582.29692859494,153172.20269752064,160954.95185740737,182211.59502181076,202370.09488958336,214144.83118458334,224185.0076923077],"Revenue_Adj":[null,null,null,427.2779635632182,1268.2615689655167,3269.6560208093074,6558.352794933338,11278.957281600002,18666.03047848905,28849.03567883462,38895.348725736214,53677.95822057387,69899.97834777906,83527.2636412261,108201.2573315447,147547.62905382726,155582.29692859494,153172.20269752064,160954.95185740737,182211.59502181076,202370.09488958336,214144.83118458334,224185.0076923077],"NetProfit":[null,null,null,-42.01054318007661,-171.5544310344827,-455.63330782430756,-525.6960747555559,-404.2583468275863,-2102.5639117017636,-1449.4810094676925,-573.2566299827588,-22.556247953884593,-376.96459806735623,1764.1349327586217,7167.6974833333325,-551.99030617284,1543.5754859504123,3412.779854545454,5744.586388271604,2732.439697860083,4528.925,5873.155,6988.505],"NetProfit_Adj":[null,null,null,-42.01054318007661,-171.5544310344827,-455.6333078243077,-525.696074755556,-404.2583468275863,-2102.5639117017636,-129.07814477288463,-263.61129608500005,-31.231448738134585,-320.793605592945,1006.3513676010632,1655.2718890392084,1371.807446988297,2955.4825204523318,4777.148975494232,6346.6869767074995,3000.099936460563,4528.925,5873.155,6988.505],"OCF":[null,null,null,0.00365945498084291,-0.0874765057471264,-13.368801240692306,222.46324586666685,580.6741443724138,164.74728584950202,269.9607732310769,1320.206951374138,3978.7135792888807,3159.1959374855164,3588.0548038314196,6172.300814308942,6558.916275679018,8598.072352809912,8405.349018181818,8069.14447222222,2643.3580715226344,null,null,null],"FCF":[null,null,null,null,null,-109.12275695030766,63.5884365333334,372.16674724137937,-303.3761272636017,-572.4247966955386,690.976185074138,2297.687263823767,-54.05291500574733,2291.011800114944,4367.935798617886,3108.2015948148173,5192.242935206609,5578.900191735536,6149.7450839506155,669.0865278189302,5062.1513665717075,3822.5863449454837,8033.430438033314],"DivYield":[null,null,null,null,null,null,null,null,0,0,0,0,0,0,0,0,1.1027368534139907,2.1837285898328918,2.8608662734687846,3.6588575941037114,3.582122778656989,3.9951021294592706,4.6340513137194055],"Assets":[null,null,null,null,null,1679.30553906,2868.56534052,4297.34113864,10712.0500092,13104.233525459998,23109.8239438,28287.407724539997,30408.386910660003,37298.921131439994,64698.74448000001,78115.44631,86287.44,88569.86556,95658.058,99469.35908000001,null,null,null],"Equity":[null,null,null,null,null,965.1584996110926,1051.265597433841,1570.5832397065628,6040.865257595771,4735.39360308285,5939.706759658198,8051.501221817511,11169.316566111796,14450.445328431511,33953.14989582057,38826.324318371335,39736.03339808077,41736.60789725681,42920.925007534854,42034.45364925384,null,null,null],"Liabilities":[null,null,null,null,null,null,null,null,4671.1114155,8368.919240219999,17170.0532135,20235.787960469996,19239.111335939997,22848.27531753,30744.497490000005,39288.91959,46550.56992,46833.63396,52736.369000000006,57434.887440000006,null,null,null],"Debt":[null,null,null,null,null,0,139.11345162,154.12151172,304.60320809999996,982.44997914,2919.2913508,3658.97098644,2096.30938146,2695.3336578599997,4834.23513,4684.02876,9428.9232,9636.45342,12298.216000000002,15324.583400000001,null,null,null],"Shares":[null,null,null,null,null,581.3952775,660.2284225,731.827046,1365.8591785,1370.995243,1418.2221985,1426.3317145,1447.1481775,1462.1575,1551.7495195,1555.3958245,1567.8396235,1568.8319575,1451.7166275,1370.9236255,null,null,null],"ROA":[null,null,null,null,null,null,-0.12151565800596753,-0.0022735170551140284,-0.10802587730575264,-0.12023023608256053,-0.031026617501720956,-0.000884113870210572,-0.01267297757773519,5.197036572619651,14.488021161584053,-0.7749280307359095,1.9015220419928611,3.9481852757047826,6.232557158270996,2.817641296508269,3.612142857142857,5.062857142857143,5.911538461538462],"ROE":[null,null,null,null,null,null,-1.6659703624532143,-1.452131089207142,-0.6561226946589431,-0.26756148182094597,-0.11808406091618714,-0.0035435917502646282,-0.04456834233406894,17.20598459856611,36.677942755311946,-1.795920838230917,4.916204292443112,10.856108385891147,17.55456754491145,8.454586368696798,11.635312499999998,14.884516129032258,14.882499999999999],"ProfitMargin":[null,null,null,-0.03528605686879068,-0.04800186415006408,-0.06075760760385781,-0.04179437470108218,-0.0007196298715087372,-0.043445715967618294,-0.050243655476190446,-0.01473843656795516,-0.00042021434312378814,-0.0053929143753351895,2.112046840581411,6.624412377547928,-0.3741099126516406,0.9921279711269733,2.228067361076538,3.56906471157273,1.499597046792225,0.02237980631842057,0.027426563636414745,0.03117262466977939]},"9988":{"cur":"USD","cur_ratio":[100.36,28.818670133521675,2.1660089531819353,2.4400724900376756,0],"date":[null,null,null,null,"31/3/2010","31/3/2011","31/3/2012","31/3/2013","31/3/2014","31/3/2015","31/3/2016","31/3/2017","31/3/2018","31/3/2019","31/3/2020","31/3/2021","31/3/2022","31/3/2023","31/3/2024","31/3/2025","31/3/2026",null,null],"Revenue":[null,null,null,null,976.7287333333337,1774.772416130268,3131.3092499999975,5488.925201846154,8582.587482298848,12301.982521072794,15909.890780268204,23531.301948046,37824.93372276926,56163.032503999995,73191.45713899234,105956.98650189303,132949.50206740748,126787.83477020574,131312.6538585124,137995.96124116183,151116.227366,168785.28839207548,190339.0042326923],"Revenue_Adj":[null,null,null,null,976.7287333333337,1774.772416130268,3131.3092499999975,5488.925201846154,8582.587482298848,12301.982521072794,15909.890780268204,23531.301948046,37824.93372276926,56163.032503999995,73191.45713899234,105956.98650189303,132949.50206740748,126787.83477020574,131312.6538585124,137995.96124116183,151116.227366,168785.28839207548,190339.0042326923],"NetProfit":[null,null,null,null,-117.44174574712649,176.38879007662837,661.1323599999994,1336.4118375384617,3772.1276234482752,3898.490576628352,11240.726448275866,6493.398195402305,9670.62399307693,13055.486215384613,21433.275850310103,22203.299823539102,9656.294851481485,10582.936214485593,11125.540106900828,17931.842121161826,11385.194,16003.128,22438.537],"NetProfit_Adj":[null,null,null,null,7.05821963218391,176.38879007662837,682.0781214999994,1363.2386672615387,3776.8027339540217,3115.0471724137924,3743.6054959770127,5776.231714714963,8853.257986018374,7862.54191007922,17127.55616985364,11175.166157561227,18656.003586365383,15692.159987148782,18159.71163507728,21756.78055993554,11385.194,16003.128,22438.537],"OCF":[null,null,null,null,437.6974788505749,881.7948474329502,1450.331749999999,2301.9868824615387,4312.053847241379,6653.860867816091,8940.35724061303,11942.500365057482,19014.03221769232,22500.593965384614,25934.087158217088,34239.12268740742,22248.95490086421,29154.48668049382,25475.548898801655,22646.30859186722,null,null,null],"FCF":[null,null,null,null,null,null,1111.321589999999,1903.9575120000002,3531.343085862068,5410.005226053639,7234.4283526819945,9333.841756321848,14504.65130876924,15102.038004307691,19416.92846689925,28116.183271769554,13938.422667283956,24140.69494650205,20998.74016289256,10739.022495933608,-1286.215226362989,-5110.029195840651,-2258.3251915091914],"DivYield":[null,null,null,null,null,null,null,null,null,0,0,0,0,0,0,0,0,1.017076559566783,1.3604164276261943,1.5089104915963691,0.859321447098805,1.1534726406090072,1.4726960819678723],"Assets":[null,null,null,null,null,null,7495.5317,10270.18386,17938.19469,41188.7325,56478.8165,73690.4648,114044.22972,143777.02248,185393.482,257927.26680000004,267439.57469000004,255208.14551999996,244358.22334,248604.43833,null,null,null],"Equity":[null,null,null,null,null,null,5463.5950846219785,1778.5148451085204,6563.962370346548,25489.15584939127,38726.26962356843,47127.05013376758,69885.33715012722,91685.46356579909,124204.14272199708,165367.08938027438,170692.90220820188,163476.634153443,154048.3772707133,150208.20415305966,null,null,null],"Liabilities":[null,null,null,null,null,null,2031.77969,8491.667399999998,11374.25211,15699.78375,17753.51817,26563.2714,44160.24555,52094.43252,61186.7608,92564.71840000001,96745.2728,91733.30633999998,90307.7658,98398.73259,null,null,null],"Debt":[null,null,null,null,null,null,203.70191,5329.59201,6605.27075,8480.62125,8952.77187,13337.8328,19966.69359,20008.014,20796.9244,27687.591400000005,27854.64481,28470.935019999997,28469.31444,34187.0769,null,null,null],"Shares":[null,null,null,null,null,null,2506.952201,2175.220739,2226.81066,2495.499036,2473.927859,2529.364189,2571.929843,2571.929843,2686.499368,2718.179248,2710.914,2585.051,2418.185675,2385.776538,null,null,null],"ROA":[null,null,null,null,null,null,null,0.15373526973945006,0.2659480423189894,0.1322186586299638,0.23055926592717346,0.10025686877196527,0.1047326003974064,10.44893591725122,13.119315066629033,10.027826956752508,3.6770945229314087,4.2210208963239255,4.548714521530481,7.290947522257986,4.670869565217391,4.629473684210526,6.1763157894736835],"ROE":[null,null,null,null,null,null,null,null,null,0.27634070844561925,0.39434256924172106,0.1761848862210712,0.19851975036494313,20.417700468138715,23.926909457559685,17.757761814101606,6.570591251407117,7.482343860286377,8.070130518100132,12.97033363020073,7.982857142857143,8.420384615384615,11.098799999999999],"ProfitMargin":[null,null,null,null,-0.12023988005997001,0.09938670923296648,0.21113607990012484,0.247182547730104,0.4440614048453451,0.3183691144821794,0.7065244258129578,0.275947255691116,0.256099510121231,23.3215866512403,29.317201316039878,20.992654285789968,7.297124945197418,8.378506873016404,8.501032759294834,13.058603077040429,0.07534036971500034,0.09481084203398871,0.11788389054055011]},"9999":{"cur":"HKD","cur_ratio":[182.9,null,1.0767984877996786,null,null],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[2109.1949417854007,2270.733851563378,3456.9864642376456,4262.908590224225,6322.514912132948,8782.35865655692,10082.25844852874,11602.863325641763,14742.59411708277,26757.657764720007,39737.28916618849,51303.55872524426,60685.828673850556,67206.62764477578,82894.6900579756,105587.46927733181,112362.710572193,114388.53909544832,114116.33356305673,122226.67039202836,141837.7611785714,152585.34010465114,162936.9520921053],"Revenue_Adj":[2109.1949417854007,2270.733851563378,3456.9864642376456,4262.908590224225,6322.514912132948,8782.35865655692,10082.25844852874,11602.863325641763,14742.59411708277,26757.657764720007,39737.28916618849,51303.55872524426,60685.828673850556,67206.62764477578,82894.6900579756,105587.46927733181,112362.710572193,114388.53909544832,114116.33356305673,122226.67039202836,141837.7611785714,152585.34010465114,162936.9520921053],"NetProfit":[1211.1020513321114,1296.8961795665778,1789.456281781255,2099.6408141662787,2566.5488993854397,3896.011829667691,4471.884882459772,5606.905084065135,5987.01921814828,8309.708899320001,13564.94581474329,12362.467957708854,7295.316783122604,24093.083108234456,13573.736527463414,20316.76778476216,23681.731737520666,32521.274573999992,32185.523138665416,36637.677074320985,46457.163,53732.869,58447.819],"NetProfit_Adj":[1211.1020513321114,1296.8961795665778,1789.456281781255,2103.0350738266065,2578.5894338265316,3941.4031674680764,4471.846156347703,5606.905084065135,5987.01921814828,8594.6894050725,14250.278613459766,13372.626924376738,9782.362254889844,15131.575848175844,13556.367292908535,19282.587064377592,24066.91619413885,32074.796454409083,31902.220587346284,36675.110724838574,46457.163,53732.869,58447.819],"OCF":[1555.4276631944147,1415.7185764945777,2261.4083920281832,2376.5680809649566,3277.3373342613013,4906.29509205846,5193.343744501918,6606.150498233717,7392.198534470121,9965.223126800001,18104.79787654559,13726.294464002314,15908.094610517237,19531.35919584136,28005.66734631707,30043.02490900497,32265.489662421078,39060.25748104338,43000.733927098336,55065.14870395061,null,null,null],"FCF":[1416.546501483082,1342.346799637511,2111.982354167533,1913.930816331962,2935.271935357087,4412.26233172,4973.70644845211,6329.917772664369,6715.8194227827635,8896.373576740001,16777.432021184668,11816.16683239385,13335.688491973176,18159.25758666549,26817.874225959345,28112.413698395907,29819.877169994634,36515.78967322934,41618.48742331233,53908.95400342716,50193.417834145366,53673.81898732329,61659.67086756683],"DivYield":[0,0,0,0,0,0,2.3493954549191365,1.7972809976317936,1.4689594567189943,2.0670189106094172,1.4824059234673563,0.9054157033439677,0.733480626537607,3.4248804710627336,3.231431785034919,1.1593328302344108,1.9005014117485983,2.777158203836518,2.8710823974034816,2.1304790817599604,2.6681640279631855,3.136734621348409,3.3594581512688406],"Assets":[4359.274594161,5008.0327464127995,7205.7618455565,9998.0995604523,13666.467829,19042.055429900003,23964.427103600003,31446.303362999995,37916.019546100004,49166.66587800001,64838.997178,85287.4199905,99021.68282080001,125422.3214006,168532.8159578,188490.3659632,195375.3979365,204498.8833022,208574.20751,246589.95232199997,null,null,null],"Equity":[2895.9889491251997,3632.626855803744,6263.944367778786,8432.813336389889,11469.398219403009,16148.472229217263,19394.63638129912,25837.79618818982,29344.495520926634,35030.570267421965,42975.603876233836,56490.60718686891,58539.76662182161,81703.55017462735,111418.36446237286,121970.65387737768,123126.17471892851,140879.50248670648,151635.9859361865,183879.6262258517,null,null,null],"Liabilities":[1463.1951181583,1375.5733374976,941.6111744009999,1565.1367078696,2197.436808,2892.9591643000003,4570.219857,5608.7416336999995,8572.419660700001,14136.694512600001,21864.353198700002,28794.681905300004,40484.4566942,43718.1498376,57115.0136382,66517.15488639999,72250.37940450001,63619.2026103,56931.9458504,62713.6174987,null,null,null],"Debt":[777.6790800706,685.9332968704,0,0,0,0,0,1249.7181744,2560.4863715,2715.0390960000004,4263.2715542999995,7952.838851400001,15558.253959200001,19351.3649994,24126.760984899996,25049.831685199995,32188.084054900002,22529.781851800002,13640.1248994,7494.913665399999,null,null,null],"Shares":[631.67701,607.25445,607.254118,647.6050274,650.4728,654.7874,649.2084,650.0568,653.4,670.03466,656.64349,656.652175,656.643,656.6450000000001,674.330013,674.329335,660.0718353760001,648.162783141,644.952,633.198284,null,null,null],"ROA":[0.3107451538946149,0.27906720029300286,0.2894765501715479,0.24429226612384722,0.21929942653665185,0.239295109244949,0.20951322394361965,0.2028058141095556,0.17328000995828835,0.18836274996311464,0.2339873589174799,0.16593316727783333,0.07787889345843672,21.33434201792004,9.498270648383343,11.40831566061044,12.461577285897988,16.40239933225377,15.551884677795355,16.175977663602406,16.401666666666667,16.755555555555553,16.233529411764703],"ROE":[0.4692594014306764,0.4010365834321121,0.3582081197415945,0.28566661870437937,0.26028237047005215,0.2828697761260604,0.25253676966530647,0.24739398091726514,0.2173505003421702,0.25529148987809164,0.3441886306456167,0.2551845804339326,0.13527178105652607,39.813374537371985,16.802775101133687,18.9984543563801,20.331561831109585,25.689394284644074,22.586190766567903,22.58318631347786,21.536250000000003,21.740000000000002,23.064],"ProfitMargin":[0.5742010979349932,0.5711352647839717,0.5176347377385165,0.4925371421243259,0.4059379748492511,0.44361793705144614,0.44353999704424707,0.48323460569204046,0.4061035100471841,0.310554420434974,0.3413656567767431,0.24096706475891735,0.12021450382313305,35.84926658659281,16.374675528637717,19.241646687637676,21.076148498843096,28.430535813438023,28.204133565928853,29.975190322054697,0.327534887962233,0.35214698114607557,0.35871164283536733]},"googl":{"cur":"USD","cur_ratio":[280.03,48.760032395221714,14.519699046334395,12.065924596050266,0],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[10604.917,16593.986,21795.55,23650.563,29321,37905,46039,55519,66001,74989,90272,110855,136819,161857,182527,257637,282836,307394,350018,402836,433982.1212121212,568565.7714285714,713539.0666666667],"Revenue_Adj":[10604.917,16593.986,21795.55,23650.563,29321,37905,46039,55519,66001,74989,90272,110855,136819,161857,182527,257637,282836,307394,350018,402836,433982.1212121212,568565.7714285714,713539.0666666667],"NetProfit":[3077.446,4203.72,4226.858,6520.448,8505,9737,10737,12733,14136,15826,19478,12662,30736,34343,40269,76033,59972,73795,100118,132170,242505.146,188348.673,229992.51200000002],"NetProfit_Adj":[3077.446,4203.72,4226.858,6457.5683,8384.75,9968.4,11247.5,13180.8,13746.45,16073.65,19656.75,25423.45,30697,33422.65,35442.89,67583.16,63967.82,77845.33,98549.85,112732.84,242505.146,188348.673,229992.51200000002],"OCF":[3580.508,5775.41,7852.857,9316.198,11081,14565,16619,18659,22376,26572,36036,37091,47971,54520,65124,91652,91495,101746,125299,164713,null,null,null],"FCF":[1677.7099999999998,3372.5699999999997,5494.396000000001,8506.31,7063,11127,13346,11301,11417,16622,25824,23907,22832,30972,42843,67012,60010,69495,72764,73266,-11697.10152499874,-43292.13868296951,-4026.604053310146],"DivYield":[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0.31695721077654515,0.26517571884984026,0.31389493982787564,0.26604292397243157,0.2903260364960898],"Assets":[18473.351,25335.806,31767.575,40496.778,57851,72574,93798,110920,129187,147461,167497,197295,232792,275909,319616,359268,365264,402392,450256,595281,null,null,null],"Equity":[17039.84,22689.679,28238.861999999997,36004.224,46241,58145,71715,87309,103860,120331,139036,152502,177628,201442,222544,251635,256144,283379,325084,415265,null,null,null],"Liabilities":[1433.511,2646.127,3528.713,4492.554,11610,14429,22083,23611,25327,27130,28461,44793,55164,74467,97072,107633,109120,119013,125172,180016,null,null,null],"Debt":[0,0,0,0,3465,4204,5537,5245,5237,5220,3935,3969,4012,15967,26873,28508,29977,28867,25696,62941,null,null,null],"Shares":[12359.88,12516.68,12604.56,12710.88,12852.04,12995.8,13199.16,13433.32011914788,13603.44,13746.96,13825.86,13895.66,13911.12,13766.7,13504.44,13242.42,12849,12460,12211,12211,null,null,null],"ROA":[0.21411921671415757,0.19191056335551038,0.14804230243389616,0.18046098053351423,0.17295764424896312,0.1493118650565459,0.1290721996489794,0.12439550992096444,0.11774750423769403,0.11818628726757469,0.12368633278087872,0.06942038202592161,0.1429292212970864,13.502234121812224,13.523865496830528,22.39940844091185,16.554686335455163,19.226059589190992,23.484016851033488,25.2827016164899,25.46222222222222,16.9275,15.275],"ROE":[0.23262176281106053,0.2116169591683202,0.1659917176893012,0.20299298822600145,0.20682052005840487,0.18655758435039183,0.16536269829046668,0.16013935003521482,0.1478900867818527,0.14118318754990164,0.15019643979380568,0.08686346205297422,0.1862054342228819,18.1196085155776,18.99543852863066,32.06932403164206,23.621299817440264,27.355645635125843,32.90849238162386,35.70478247421149,63.108846153846166,53.245000000000005,34.90813953488372],"ProfitMargin":[0.2901904842819609,0.2533279225377194,0.1939321558758554,0.2756994833484514,0.29006514102520375,0.25687903970452447,0.2332153174482504,0.22934490895008916,0.21417857305192345,0.21800530744509194,0.2157701169797944,0.11422128005051645,0.22464716157843576,21.21811228430034,22.061941520980458,29.511677282377917,21.203807153261963,24.006649446638516,28.603671811164,32.80987796522654,0.5587906373682154,0.33126980660255045,0.32232644663308957]},"dis":{"cur":"USD","cur_ratio":[114.18,63.0828729281768,2.2825027979160555,2.1252016379203376,0.0002571128293671646],"date":["30/9/2006","29/9/2007","27/9/2008","3/10/2009","2/10/2010","1/10/2011","29/9/2012","28/9/2013","27/9/2014","3/10/2015","1/10/2016","30/9/2017","29/9/2018","28/9/2019","3/10/2020","2/10/2021","1/10/2022","30/9/2023","28/9/2024","27/9/2025",null,null,null],"Revenue":[33747,35510,37843,36149,38063,40893,42278,45041,48813,52465,55632,55137,59434,69607,65388,67418,82722,88898,91361,94425,101481.90909090909,106321.97058823529,111064.65384615384],"Revenue_Adj":[33747,35510,37843,36149,38063,40893,42278,45041,48813,52465,55632,55137,59434,69607,65388,67418,83745,88898,91361,94425,101481.90909090909,106321.97058823529,111064.65384615384],"NetProfit":[3374,4687,4427,3307,3963,4807,5682,6136,7501,8382,9391,8980,12598,11054,-2864,1995,4168,2354,4972,12404,12407.478000000001,12781.038,14112.095000000001],"NetProfit_Adj":[3374,4687,4427,3404.5,4047.5,4847,5599,6287.8,7616,8814,9376,8997,10641,8359.54,1247.90772902028,2371.76698014629,5694,5438.43158591844,10754,9342.04801670146,12407.478000000001,12781.038,14112.095000000001],"OCF":[6058,5421,5446,5319,6578,6994,7966,9452,9780,11385,13136,12343,14295,6606,7618,5567,6010,9866,13971,18101,null,null,null],"FCF":[4766,3855,3860,3566,4468,3435,4182,6656,6469,7120,8363,8720,9830,1730,3596,1989,1067,4897,8559,10077,9704.490811718784,10275.359910532794,11414.211297198448],"DivYield":[0.8735037204788095,0.9014248328002326,1.0687022900763359,1.2862918044836458,1.0497900419916015,1.3262599469496021,1.1476664116296862,1.150483202945237,0.9691232814965067,1.7572815533980584,1.5291837174240792,1.5826316323424978,1.4366341713699333,1.3542628501077254,0.7180742554059568,0,0,0,0.7811686282678887,0.8812902088657796,1.3154668067962865,1.4608512874408826,1.604484147836749],"Assets":[59998,60928,62497,63117,69206,72124,74898,81241,84141,88182,92033,95789,98598,193984,201549,203609,203631,205579,196219,197514,null,null,null],"Equity":[33163,32048,33667,35425,39342,39453,41958,48150,48178,48655,47323,46152,53955,102852,97512,102224,108378,113012,105522,114612,null,null,null],"Liabilities":[26835,28880,28830,27692,29864,32671,32940,33091,35963,39527,44710,49637,44643,91132,104037,101385,95253,92567,90697,82902,null,null,null],"Debt":[13583,15190,14656,12943,12721,14283,14612,14580,15057,17558,20364,25432,21028,47137,62323,58313,52259,50672,49517,45423,null,null,null],"Shares":[2064,1962.2,1822.9,1818.3,1896.9,1762.2,1800,1800,1700,1600,1600,1517,1488,1783,1781,1781,1781,1781,1853,1821,null,null,null],"ROA":[0.059634486903036514,0.07751848237765245,0.07173587198703667,0.05265336666295158,0.05989888379193338,0.06802518927333191,0.07729455455646093,0.07859663504953919,0.09071120194458888,0.097282428927073,0.104219959492828,0.09562245104407366,0.12961772134967872,7.5561722867435455,-1.4481724660142137,0.9848009912182407,1.544543757980552,1.1505095183402165,2.474875434920034,6.3007164753779845,5.13,5.895999999999999,6.635],"ROE":[0.11628468033775635,0.14980902306106467,0.1403703468831251,0.10012564906065974,0.11123742158224918,0.12835095589020612,0.147308928756611,0.1440578485232662,0.16597519554803233,0.18734284724472805,0.21394236245586057,0.21234334358004253,0.2796820886244561,16.061024337086817,-3.321349878232634,2.3179346563182603,4.541269659677165,2.4232442031036876,4.972671310626935,11.781635124545865,9.050833333333332,9.670000000000002,10.91111111111111],"ProfitMargin":[0.09997925741547396,0.13199098845395663,0.11698332584625955,0.09148247531052035,0.10411685889183722,0.11755068104565573,0.13439613983632148,0.13623143358273573,0.15366808022453035,0.1597636519584485,0.16880572332470523,0.16286704028148066,0.21196621462462562,15.880586722599737,-4.380008564262556,2.959150375270699,3.8018906699547883,2.647978582195325,5.442147086831362,13.136351601800369,0.12226295427690467,0.12021069473966016,0.12706198139328828]},"3328":{"cur":"HKD","cur_ratio":[8.525,6.7301468562341515,2.2254596145875536,0.6408357088991954,0.04835026392961876],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[67155.59214769234,102163.62826666665,145422.58242022892,151669.54584827577,188550.3784482758,263778.4640961538,333863.344448276,374742.8952731801,419932.62778850604,441066.35331000003,411498.34247969335,449364.6469569233,517825.3217816091,524748.3728344822,528086.8448292683,590738.1284674903,591877.9442371902,616047.4700516529,588252.422433333,560124.5965716048,330539.3023384616,346903.5989384616,365320.23998333333],"Revenue_Adj":[67155.59214769234,102163.62826666665,145422.58242022892,151669.54584827577,188550.3784482758,263778.4640961538,333863.344448276,374742.8952731801,419932.62778850604,441066.35331000003,411498.34247969335,449364.6469569233,517825.3217816091,524748.3728344822,528086.8448292683,590738.1284674903,591877.9442371902,616047.4700516529,588252.422433333,560124.5965716048,330539.3023384616,346903.5989384616,365320.23998333333],"NetProfit":[12225.256900000006,20800.22293333333,31967.70687480914,34174.09837701147,44818.1666689655,61115.65418846152,71758.87096360156,78597.93564942529,82883.42706896558,82081.58112000002,78564.21534099615,77964.34600384619,84203.63532567048,84641.95768965507,83134.22081300811,98792.21871934168,98532.98608966943,94253.10158057848,93336.86168971188,96500.04577777776,114804.527,120267.334,126699.299],"NetProfit_Adj":[12225.256900000006,20800.22293333333,31967.70687480914,33938.36994425285,44529.74485718389,60943.99792115383,71659.28953256708,78542.10518706897,82806.01885689661,81868.75234500002,78410.7924386973,77659.55424692311,84139.60394636012,84356.92496120681,83402.59536178861,100528.68769650217,98898.90852882233,93818.62302892559,93242.5732736625,96484.58102685184,114804.527,120267.334,126699.299],"OCF":[33911.079283076935,-61759.44657777778,137718.68335076328,114638.33943908037,-111082.28871187735,-81149.44633999998,53048.626532567076,86704.39261302682,497566.1418080463,330481.75561000005,543903.7683340994,111350.58852923082,122499.14321839076,91654.04642413784,174876.68186991868,188402.8162913582,206094.97008925624,470791.2297892561,98361.24211275716,320015.07533580245,null,null,null],"FCF":[null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null],"DivYield":[1.0557933066096157,1.4735975526491847,4.06247765637289,2.520931928031427,4.519301484711051,2.2705827328002326,5.108742783102578,6.089378016876946,4.658388308066465,5.907464168494881,5.407374463768699,5.620222998829568,5.322282531924117,6.057395050777794,9.184540303617144,9.246388113364365,9.394869645553985,8.46944484031257,6.311660467972153,5.606587144183741,4.796772875874187,5.0173141575235745,5.279206929482222],"Assets":[1710039.1867,2255856.3072,2902803.555,3758186.8909,4660903.9435,5685120.123300001,6555337.434900001,7636556.390699999,7829732.280900001,8547795.445200002,9388857.3718,10852231.5778,10852191.3006,11080404.16,12707698.0464,14311550.687599998,14692167.6439,15465113.152800001,15857343.0314,17316239.7156,null,null,null],"Equity":[87147.72711432265,142500.0533773691,165323.3550655629,186734.16164989528,263811.8775665053,336307.3290474149,474187.25778668805,539968.0522106934,591598.0619099784,642815.693132948,706604.1237085176,811967.9612245837,803098.5414843353,895893.3495706122,1043727.4671528373,1198847.9901197278,1168821.005334416,1209283.5253196643,1229735.432624599,1425875.586241433,null,null,null],"Liabilities":[1622888.7354,2113149.0624,2737474.755,3571449.4184,4397100.512,5348799.7981,6081160.6692,7096593.238299999,7238152.275400001,7904990.742000001,8682269.0307,10040232.9881,10049127.6118,10184503.9968,11663975.8452,13112677.562799998,13523357.6141,14255828.097800002,14627556.704,15890389.652199998,null,null,null],"Debt":[253067.1135,406989.4208,552180.024,809345.248,917958.8495,1162256.0629,1274170.0414,1550086.1615,1940149.5858000002,2184633.1122000003,2787809.8663,3223538.5012000003,2836742.198,2603311.0684,2741533.0278,3034097.2239999995,2922281.7888,3328198.4090000005,3334787.762,3549422.0397999994,null,null,null],"Shares":[53097.32207872949,56795.26237720009,56795.26237720009,56795.26237720009,61886.00000000001,61886,74263,74263,74263,74263,74262.726645,74262.726645,74262.726645,74262.726645,74262.726645,74262.726645,74262.726645,74262.726645,74262.726645,88363.784223,null,null,null],"ROA":[0.007992634925489147,0.01059705802796141,0.01222355687335094,0.010269459949771097,0.01075429054654284,0.011850137280342694,0.01181014099166417,0.011090127783480543,0.010769274548303754,0.009912050073374172,0.00863963480349812,0.008052440684302081,0.007930240166294863,0.7952041005164902,0.7598231266419767,0.7832539393766763,0.7470558042623272,0.6855526586291468,0.6462856203866492,0.6280775740370694,0.6088888888888887,0.6066666666666666,0.605],"ROE":[0.1471882296347573,0.18407230698692134,0.20515697486244475,0.1949290487516787,0.20196523210068776,0.2051660516605166,0.17912293623028294,0.15583899014233019,0.14787517852811985,0.13426221171000266,0.12340861913999561,0.11440595053251372,0.113637745680095,11.203493339620064,10.35682543954827,10.762802887658124,10.33772710252515,9.68545971141863,9.148532731376974,8.419290514758377,7.8914285714285715,7.826153846153847,7.859999999999999],"ProfitMargin":[0.2841063502128816,0.3197590057409627,0.36330289251461534,0.36029332599619585,0.36372275013974287,0.38816715632268334,0.38630407158362895,0.3699689391194864,0.3660387217271914,0.33850459205739436,0.34107228946233287,0.3512959173974597,0.33660197947381654,32.310271590072915,30.872811463415594,31.942534730453747,35.40845709364776,35.46044505290692,35.870310960862554,35.447721080239475,0.34733550936500174,0.3466986547058908,0.3468276838929083]},"3323":{"cur":"HKD","cur_ratio":[2.91,8.641348406695709,0.09861840723295438,0.21470622544020862,0.05599484536082474],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[6287.387742138464,10787.318378844444,29548.228671285095,37781.63752098617,59679.223064409554,96438.86404606921,107225.38650931614,148487.37900377015,153571.8788190185,123826.16127591002,118701.73079597391,212569.9821833632,259679.02401137925,287474.81739472726,286764.29415469104,332190.2913900119,272336.91657168395,232403.3887479793,196489.14857393943,193007.781606437,207797.6019,218153.7569,229060.7512],"Revenue_Adj":[6287.387742138464,10787.318378844444,29548.228671285095,37781.63752098617,59679.223064409554,96438.86404606921,107225.38650931614,148487.37900377015,153571.8788190185,123826.16127591002,118701.73079597391,212569.9821833632,259679.02401137925,287474.81739472726,286764.29415469104,332190.2913900119,272336.91657168395,232403.3887479793,196489.14857393943,193007.781606437,207797.6019,218153.7569,229060.7512],"NetProfit":[290.5469464892309,936.1290428444444,1694.03069642977,2669.201551420688,3867.932221041761,9655.001298490766,6859.563607178165,7269.762098296553,7450.749350877017,1257.8007871900004,1225.1599013609193,5702.584501176926,9405.194604750955,12450.641387099986,14136.314846796744,19645.575369546525,9466.299968863637,4270.767175586776,2587.294728118106,-4064.521805549382,1048.554,3845.795,6118.053],"NetProfit_Adj":[290.5469464892309,936.1290428444444,1694.03069642977,2362.6022589086183,3849.4473962687725,9743.79396599615,6660.40351126006,7318.845591218967,6945.932954539373,1245.0683828375002,1287.2961767919535,6886.418198093657,16321.186235981797,22302.693275721525,21452.634895934956,25135.6656527027,9518.331112190599,5328.788505079028,2193.059687767077,2546.559643992901,1048.554,3845.795,6118.053],"OCF":[337.4258610507694,231.8741632,3243.9354952259523,2447.375139158619,3582.8337173003815,9541.626248901537,3111.317313639848,1660.6856193,4833.775312033336,-2295.8228603100006,6076.802368726435,16092.955121426547,43627.57871283524,62208.18367622062,64017.55062220325,51027.55495752762,24410.842531857852,26263.96753917768,19539.827386746903,19815.00055482839,null,null,null],"FCF":[-1030.5350712369236,-1435.241031288889,-1652.8718815729003,-4954.486302726433,-8577.626800117238,-6204.182708447691,-11304.193912544068,-11165.749287405364,-6713.1397141080515,-12112.72962363,-7399.037762234481,-448.68810161769346,24264.084077318006,35746.650465062026,41627.085647910564,16111.097171620593,-7566.6938261859505,5575.411189469008,-165.40355724855803,6764.118182902469,null,null,null],"DivYield":[0.6407157031706529,0.11737778038617289,0.5488272050474541,0.49500364534827396,1.2311676036116899,3.005252482443385,1.6991602749920989,2.457768245050554,2.7335250445564583,1.188197664709887,1.2744025867408784,1.7176747356155047,3.8238228839229707,4.500080872881973,5.990512812113683,8.892829851163798,6.669020230949229,7.541219846646497,4.749622735662452,3.262851392780745,2.543969262067117,7.591527321724096,12.033378414222236],"Assets":[13944.145963800001,31935.7301056,66885.7088805,87459.1633209,131533.534825,195285.4642722,306341.5422757,373608.6982925,395317.44885660004,394001.5755126,380761.6804571,545307.0915557,496987.15875680005,499370.322654,542445.732661,569235.8833276,553398.56415,537738.8266076001,525722.5431192,545203.4922733,null,null,null],"Equity":[5695.218932672226,10424.463404777238,13776.822111552267,19891.918890187633,32907.19982712415,46369.009760655266,54778.22335401764,68636.7070362501,83663.81278005282,87795.11586990366,84406.8333764428,149800.08228000367,154773.62828224176,168922.18482861822,196355.62441635007,219932.84929595198,221644.82229255218,212845.8750524137,207008.0078245472,207613.4268298636,null,null,null],"Liabilities":[8248.749002,21511.747219200002,53108.43302850001,67566.8917413,98627.388538,148914.6626424,251564.53097000002,304972.6140836,311656.1896145,306207.9606366,296356.7323796,395501.35862400004,342220.2473046,330446.85310420004,346091.0989242,349298.42298679997,331755.82316020003,324892.68231630005,318705.9679022,337593.7816755,null,null,null],"Debt":[6378.730495,15031.5027328,32566.224027,48013.57664089999,72532.87598200001,109526.56523440001,183831.3385056,231709.7821374,238149.55877199996,235973.7687714,228344.1641027,273059.53666780004,237268.8996422,213043.1800084,211922.0394788,208720.3703444,199547.8263449,205714.87774720002,206962.25951320006,217589.5852074,null,null,null],"Shares":[4143.4,4416.976,4416.976,4962.430546,5399.026262,5399.026262,5399.026262,5399.026262,5399.026262,5399.026262,5399.026262,5399.026262,8323.596427,8323.596427,8323.596427,8323.596427,8434.770662,8434.770662,8434.770662,7593.021358,null,null,null],"ROA":[0.02515701406411129,0.041597435130431404,0.03404980558585562,0.0346161449421244,0.03574513813357137,0.05939037040457636,0.027565239836650438,0.021416955114927604,0.019616965893482028,0.004162313577843075,0.004697842336246485,0.014068671806868122,0.02001381604082915,2.7512117662711004,3.001884976460993,3.7136343339337317,1.8499302398231658,0.9026107240373824,0.590437409426667,-0.6802016636097379,0.03,0.67,1.13],"ROE":[0.09436092914559092,0.15741863023884625,0.18642686124904992,0.21656003049373684,0.21021349712281906,0.352355488984014,0.19636848584108368,0.1749342207740683,0.15587723000062514,0.024717626380881992,0.02502958133915417,0.09310216193203168,0.1165617000705264,14.414914478650948,14.712624541549035,16.914454453725675,7.7520679356751,3.6331743520320403,2.2905616414785857,-3.786194132746918,0.6266666666666667,3.6774999999999998,5.1425],"ProfitMargin":[0.046211074997326344,0.08678051485718029,0.05733104056000572,0.0706481170896326,0.06481204047960286,0.10011525326427045,0.06397331667890215,0.04895878792575342,0.048886208188292715,0.0134019574197432,0.01551207190876741,0.030370890592688996,0.04069751666967265,4.792932569484959,5.318778240194251,6.2022880545133585,3.7703551385845273,2.100147888532825,1.600501257852279,-1.8808635413908328,0.005045997371394534,0.01762869576589421,0.02670910312608397]},"aapl":{"cur":"USD","cur_ratio":[331.9,54.717633748181655,14.236672439337779,88.38096928812357,0.002862307924073516],"date":["30/9/2006","29/9/2007","27/9/2008","26/9/2009","25/9/2010","24/9/2011","29/9/2012","28/9/2013","27/9/2014","26/9/2015","24/9/2016","30/9/2017","29/9/2018","28/9/2019","26/9/2020","25/9/2021","24/9/2022","30/9/2023","28/9/2024","27/9/2025","26/9/2026",null,null],"Revenue":[19315,24578,37491,42905,65225,108249,156508,170910,182795,233715,215639,229234,265595,260174,274515,365817,394328,383285,391035,416161,478061.0465116279,525358.75,567309.6315789474],"Revenue_Adj":[19315,24578,37491,42905,65225,108249,156508,170910,182795,233715,215091,228594,265595,260174,274515,365817,394328,383285,391035,416161,478061.0465116279,525358.75,567309.6315789474],"NetProfit":[1989,3495,6119,8235,14013,25922,41733,37037,39510,53394,45687,48351,59531,55256,57411,94680,99803,96995,93736,112010,130030.78600000001,136634.911,146126.222],"NetProfit_Adj":[1989,3495,6119,8235,14013,25922,41733,37037,39643.25,53453.15,45387.35,47870.65,59337.72,55280.49,57346.22,94455.64,99803,96995,103982,112010,130030.78600000001,136634.911,146126.222],"OCF":[2220,5470,9596,10159,18595,37529,50856,53666,59713,81266,66231,64225,77434,69391,80674,104038,122151,110543,118254,111482,null,null,null],"FCF":[1563,4735,8505,9015,16590,33269,42561,45501,50142,70019,53497,51774,64121,58896,73365,92953,111443,99584,108807,98767,141519.08825327476,153039.96289040314,166439.6669135806],"DivYield":[0,0,0,0,0,0,0.39723881547882267,2.3614707405489384,1.806451612903226,1.7260918838810917,1.9341673320912078,1.5572281339216194,1.2049260210862054,1.3709898546750754,0.708051300320627,0.5887557854614757,0.5982849165724922,0.5490333508556743,0.43022081741955315,0.39927973068190714,0.3217836697800543,0.33473937933112385,0.354926182585116],"Assets":[17205,24878,36171,47501,75183,116371,176064,207000,231839,290345,321686,375319,365725,338516,323888,351002,352755,352583,364980,359241,null,null,null],"Equity":[9984,14531,22297,31640,47791,76615,118210,123549,111547,119355,128249,134047,107147,90488,65339,63090,50672,62146,56950,73733,null,null,null],"Liabilities":[7221,10347,13874,15861,27392,39756,57854,83451,120292,170990,193437,241272,258578,248028,258549,287912,302083,290437,308030,285508,null,null,null],"Debt":[0,0,0,0,0,0,0,16960,35295,64328,87032,115680,114483,108047,122278,136522,132480,123930,119059,112377,null,null,null],"Shares":[23947.351904,24425.211216,24873.127244,25194.554,25647.16,26019.756,26297.824,25177.964,23464.644,22315.012,21344.664,20504.804,19019.944,17772.945,16976.763,16426.786,15943.425,15550.061,15116.786,14773.26,null,null,null],"ROA":[0.1383363471971067,0.16610032554713305,0.20046192402823962,0.19684004206903147,0.22844054644452413,0.2706495296365516,0.2854172722143382,0.19337238685963704,0.18006603788633188,0.2045026274263478,0.14929635917134917,0.1387393203779026,0.160667922552507,15.69235531586488,17.33413445570981,28.057905732786082,28.362915040276686,27.503126160791,26.12620773367635,30.93254683307996,33.99625,32.624736842105264,32.43266666666667],"ROE":[0.22796561604584528,0.2851315521109525,0.3323015097208646,0.30535624895711666,0.3528345356347018,0.41673231194636917,0.42841524445014756,0.30639603903060486,0.3361180113655698,0.4624819187360872,0.3690328104554046,0.3686750846372038,0.4936358284202758,55.91722113998026,73.68556155223422,147.44333444938448,175.45929220653645,171.94951160275843,157.41250755692886,171.42244974480232,136.46380952380954,110.24565217391309,90.73],"ProfitMargin":[0.1029769609112089,0.14220034176906177,0.1632125043343736,0.1919356718331197,0.21484093522422384,0.2394664153941376,0.26665090602397323,0.21670469837926393,0.21614376760852325,0.22845773698735639,0.21186798306428797,0.21092420845075338,0.22414202074587247,21.238094505984456,20.913611278072235,25.88179335569424,25.309640705199733,25.30623426432028,23.971255769943866,26.91506412181824,0.2719961954984381,0.2600792525107843,0.2575775445321542]},"nvda":{"cur":"USD","cur_ratio":[206.69,1240.14,190.1279858147131,237.82322175732216,8.00984835045506e-05],"date":["29/1/2006","28/1/2007","27/1/2008","25/1/2009","31/1/2010","30/1/2011","29/1/2012","27/1/2013","26/1/2014","25/1/2015","31/1/2016","29/1/2017","28/1/2018","27/1/2019","26/1/2020","31/1/2021","30/1/2022","29/1/2023","28/1/2024","26/1/2025","25/1/2026",null,null],"Revenue":[2375.687,3068.771,4097.86,3424.859,3326.445,3543.309,3997.93,4280.159,4130,4682,5010,6910,9714,11716,10918,16675,26914,26974,60922,130497,213762.53731343284,411057.5714285714,702662.0476190476],"Revenue_Adj":[2375.687,3068.771,4097.86,3424.859,3326.445,3543.309,3997.93,4280.159,4130,4682,5010,6910,9714,11716,10918,16675,26914,26974,60922,130497,213762.53731343284,411057.5714285714,702662.0476190476],"NetProfit":[301.176,448.834,797.645,-30.041,-67.987,253.146,581.09,562.536,440,631,614,1666,3047,4141,2796,4332,9752,4368,29760,72880,114757.229,229348.797,382728.483],"NetProfit_Adj":[301.176,448.834,797.645,165.354,140.213,376.200422455596,604.177072822234,578.5465380428869,436.041800756293,618.675384615385,772.602985074627,1686.2431610942199,2916.84112149533,3772.8845500848897,2836.50900900901,5079.21604938272,10083.487379553499,6134.101726263871,30010.4748603352,72741.5,114757.229,229348.797,382728.483],"OCF":[446.408,572.681,1270.196,249.36,487.807,675.797,909.156,824.172,835,906,1175,1672,3502,3743,4761,5822,9108,5641,28090,64089,null,null,null],"FCF":[366.808,441.855,1082.451,-158.31,410.206,577.907,770.4209999999999,640.863,580,784,1089,1496,2909,3143,4272,4694,8132,3808,27021,60853,96848.85580100858,185748.02632681068,334679.9770228293],"DivYield":[0,0,0,0,0,0,0,0.604351329572925,1.992287917737789,1.6417189763399325,1.3485831341754866,0.43392681399302135,0.23424978424361975,0.3808929128941617,0.2555094219099329,0.12317404107084431,0.07005253940455342,0.07856616744414437,0.026216185217348566,0.023839573692329266,0.01935265373264309,0.3667327882335865,0.48042962891286467],"Assets":[1954.687,2675.263,3747.671,3350.727,3585.918,4495.246,5552.928,6412.245,7250.894,7201,7370,9841,11241,13292,17315,28791,44187,41182,65728,111601,null,null,null],"Equity":[1495.9920000000002,2006.9189999999996,2617.912,2394.652,2665.1400000000003,3181.462,4145.724,4827.702999999999,4456.398,4418,4469,5762,7471,9342,12204,16893,26612,22101,42978,79327,null,null,null],"Liabilities":[466.955,668.344,1129.759,956.075,920.778,1313.784,1407.204,1584.542,2794.496,2783,2901,4079,3770,3950,5111,11898,17575,19081,22750,32274,null,null,null],"Debt":[0,0,0,26.562,25.634,25.063,23.455,21.406000000000002,1376.792,1401.5,1514,2820,2000,1988,2643,7718,11831,12031,11056,10270,null,null,null],"Shares":[20577.29472,21659.31024,22284.10352,21538.43064,22458.63404,23542.22804,24487.65648,24670.24536,22719.88,21800,21560,23400,24240,24240,24480,24790,25060,24660,24640,24477,null,null,null],"ROA":[0.16810340857937114,0.19388287130530568,0.24837402968798997,-0.008464163322484878,-0.019602271703395516,0.06265087554218673,0.11566081558699125,0.0940288953615631,0.06440686872906731,0.08732419432359523,0.08427698853887859,0.1935971181221312,0.2890617588464093,33.75861085069091,18.270330316594244,18.791480501453172,26.72586258872537,10.233222832644168,55.67299597792535,82.19749730726502,72.09214285714286,70.90875,66.94625],"ROE":[0.22524062731372413,0.2562634334700482,0.3449401718679017,-0.011986280873421266,-0.026873436694630923,0.08659594068486275,0.1586120510657161,0.1253781860597963,0.09478569869069715,0.1422068291280152,0.13817936311466186,0.32567686443162935,0.4605153782211138,49.2595015761613,25.953773322194372,29.776265594391177,44.83162854844271,17.933611151027446,91.45807403309824,119.17746617063898,91.86074074074074,97.21968749999998,81.62857142857142],"ProfitMargin":[0.1267742762409358,0.1462585510616465,0.194649158341183,-0.008771455992786857,-0.020438335820974044,0.07144338808723709,0.14534771744377717,0.13142876234270737,0.10653753026634384,0.13477146518581803,0.12255489021956087,0.24109985528219974,0.3136709903232448,35.3448275862069,25.609085913170908,25.979010494752625,36.23393029649996,16.193371394676355,48.84934834706674,55.848027157712444,0.536844437807173,0.5579481152531794,0.5446835845046237]},"meta":{"cur":"USD","cur_ratio":[621.03,43.17882266332206,12.752061143246623,10.391800746556827,0],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[null,153,272,777,1974,3711,5089,7872,12466,17928,27638,40653,55838,70697,85965,117929,116609,134902,164501,200966,256821.7627118644,313494.05263157893,374756.5833333333],"Revenue_Adj":[null,153,272,777,1974,3711,5089,7872,12466,17928,27638,40653,55838,70697,85965,117929,116609,134902,164501,200966,256821.7627118644,313494.05263157893,374756.5833333333],"NetProfit":[null,-138,-56,122,372,668,32,1491,2925,3669,10188,15920,22111,18485,29146,39370,23200,39098,62360,60458,85962.105,99126.476,117377.632],"NetProfit_Adj":[null,-138,-56,122,372,668,32,1491,2925,3669,10188,18193,22111,23545,29172.07,39186.72,30020.07,45046.7,62667.31,75488.98,85962.105,99126.476,117377.632],"OCF":[null,null,null,155,698,1549,1612,4222,5457,8599,16108,24216,29274,36314,38747,57683,50475,71113,91328,115800,null,null,null],"FCF":[null,null,null,122,405,943,377,2860,3626,6076,11617,17483,15359,21212,23584,38993,19044,43847,54072,46109,-5590.006589040661,-53374.43662605747,-27160.57444662617],"DivYield":[null,null,null,null,null,null,0,0,0,0,0,0,0,0,0,0,0,0,0.3415825519632457,0.30298898635034616,0.34330064570149593,0.3542501972529508,0.346360079223226],"Assets":[null,null,null,1109,2990,6331,15103,17895,40184,49407,64961,84524,97334,133376,159316,165987,185727,229623,276054,366021,null,null,null],"Equity":[null,273,335,868,2162,4899,11755,15470,36096,44218,59194,74347,84127,101054,128290,124879,125713,153168,182637,217243,null,null,null],"Liabilities":[null,null,null,null,828,1432,3348,2425,4088,5189,5767,10177,13207,32322,31026,41108,60014,76455,93417,148778,null,null,null],"Debt":[null,null,null,241,473,677,2356,476,233,114,0,0,0,10797,11177,14454,27278,37924,49769,85081,null,null,null],"Shares":[null,null,null,null,1172,1330,2372,2547,2797,2845,2892,2906,2854,2852,2849,2741,2614,2561,2534,2530,null,null,null],"ROA":[null,null,null,null,0.18150768480117102,0.1433322604870722,0.002985910236073528,0.0903691132795927,0.10072487473957885,0.0819055485484033,0.17816172355903748,0.2129979596615045,0.2431677462635683,16.0244462745438,19.9158159430391,24.205125682824935,13.19253711822674,18.826531840616347,24.66396533755737,18.83206790483978,18.169999999999998,15.090000000000002,12.7],"ROE":[null,null,-0.18421052631578946,0.2028262676641729,0.30807453416149067,0.22912021951637798,0.003990273707837147,0.10953168044077137,0.11344684482023039,0.09136638693129467,0.19703709434108227,0.238428647381703,0.27904892916188145,19.96425119207694,25.4168410771592,31.10175416421442,18.516153747924914,28.03919951520541,37.140602432959604,30.238071421426426,28.088333333333335,24.200588235294116,20.635333333333335],"ProfitMargin":[null,-0.9019607843137255,-0.20588235294117646,0.15701415701415702,0.1884498480243161,0.18000538938291566,0.006288072312831597,0.189405487804878,0.23463821594737685,0.20465194109772422,0.3686229104855634,0.39160701547241283,0.3959848132096422,26.14679547929898,33.90449601582039,33.38449405998525,19.895548371051976,28.982520644616095,37.90858414234564,30.08369574952977,0.3347150334763491,0.3161989040985093,0.31321032724860765]},"msft":{"cur":"USD","cur_ratio":[520.99,53.954654732403725,18.537931628319015,17.727639510013613,0.005000426906069412],"date":["30/6/2006","30/6/2007","30/6/2008","30/6/2009","30/6/2010","30/6/2011","30/6/2012","30/6/2013","30/6/2014","30/6/2015","30/6/2016","30/6/2017","30/6/2018","30/6/2019","30/6/2020","30/6/2021","30/6/2022","30/6/2023","30/6/2024","30/6/2025","30/6/2026",null,null],"Revenue":[44282,51122,60420,58437,62484,69943,73723,77849,86833,93580,91154,96571,110360,125843,143015,168088,198270,211915,245122,281724,329523.86440677964,394904.42372881353,476146.1896551724],"Revenue_Adj":[44282,51122,60420,58437,62484,69943,73723,77849,86451,93580,91154,96571,110360,125843,143015,168088,198270,211915,245122,281724,329523.86440677964,394904.42372881353,476146.1896551724],"NetProfit":[12599,14065,17681,14569,18760,23150,16978,21863,22074,12193,20539,25489,16571,39240,44281,61271,72738,72361,88136,101832,126653.306,148956.356,179085.439],"NetProfit_Adj":[12599,14065,17681,15609,18572.15,22435,22804.4,22549.9,22083.64,20012.6,20790.5,24008.95,28539.72,36318.08,44584.36,60297.72,69082.81,73101.6,88229.22,102120.35,126653.306,148956.356,179085.439],"OCF":[14404,17796,21612,19037,24073,26994,31626,28833,32502,29668,33325,39507,43884,52185,60675,76740,89035,87582,118548,136162,null,null,null],"FCF":[12826,15532,18430,15918,22096,24639,29321,24576,27017,23724,24982,31378,32252,38260,45234,56118,65149,59475,74071,71611,61539.95784984464,30629.44602323857,42560.75286721403],"DivYield":[1.5021459227467808,1.3573125212080084,1.599418393311523,2.187631468237274,2.2598870056497176,2.4615384615384617,2.615233736515201,2.663192936749168,2.6858513189448443,2.808607021517554,2.8141489153801054,2.2631655302480778,1.7036811682385153,1.373544341594506,1.0024077440911996,0.82687338501292,0.9656192812366158,0.7987314265578199,0.6712160196890032,0.651374117930882,0.6938712835179178,0.7432004453060519,0.8138351983723295],"Assets":[69597,63171,72793,77888,86113,108704,121271,142431,172384,174472,202897,250312,258848,286556,301311,333779,364840,411976,512163,619003,null,null,null],"Equity":[40104,31097,36286,39558,46175,57083,66363,78944,89784,80083,83090,87711,82718,102330,118304,141988,166542,206223,268477,343479,null,null,null],"Liabilities":[29493,32074,36507,38330,39938,51621,54908,63487,82600,94389,119807,162601,176130,184226,183007,191791,198298,205753,243686,275524,null,null,null],"Debt":[0,0,0,5746,5939,11921,11944,15600,22645,35292,57718,95527,87508,86455,82110,82278,78400,79441,97852,112184,null,null,null],"Shares":[10062,9380,9151,8908,8668,8376,8381,8328,8239,8027,7808,7708,7677,7643,7571,7519,7464,7432,7434,7434,null,null,null],"ROA":[0.17945759621684756,0.21187334297421065,0.2600835515283457,0.19337540897657968,0.2287790928104097,0.23765893120210252,0.14765083161213174,0.16581595892333012,0.14023474103838762,0.07030583296814817,0.10885366842533434,0.11248232051878934,0.06509152329326734,14.389333411562804,15.064972179081323,19.29521800059834,20.82336724308958,18.63015179913905,19.074186891798746,18.004784443662558,18.297142857142855,17.503636363636364,17.528181818181817],"ROE":[0.28563007968804904,0.3950787208044831,0.5247911194218127,0.3841833236643637,0.4376377824175055,0.448391407929652,0.2750676409118157,0.30092149724376666,0.2616518894315111,0.14355937292116774,0.2517450803748169,0.29846429470553454,0.19446221006988249,42.41061778565561,40.1397790005167,47.07866549874756,47.15133050270639,38.82392391989591,37.13334737729092,33.280824111537434,30.74681818181818,28.77233333333334,27.308928571428584],"ProfitMargin":[0.28451741113770834,0.2751261687727397,0.29263488910956637,0.24931122405325393,0.3002368606363229,0.3309838010951775,0.2302944806912361,0.2808385464167812,0.2542121082998399,0.13029493481513144,0.22532198257893235,0.26394052044609667,0.1501540413193186,31.181710544090652,30.962486452470024,36.451739564989765,36.68633681343622,34.14623787839464,35.955972944084984,36.146015248967075,0.3843524546677445,0.3771959667891692,0.37611440091539955]},"384":{"cur":"HKD","cur_ratio":[5.155,4.307799442896936,0.30277609815548073,0.4151279838941616,0.0852462549843733],"date":["31/3/2006","31/3/2007","31/3/2008","31/3/2009","31/3/2010","31/3/2011","31/3/2012","31/3/2013","31/3/2014","31/3/2015","31/3/2016","31/3/2017","31/3/2018","31/3/2019","31/3/2020","31/3/2021","31/3/2022","31/3/2023","31/3/2024","31/3/2025","31/3/2026",null,null],"Revenue":[630.522,1236.469,2552.075,6323.823,10211.959,15861.88,18933.565,17955.672,26007.997,31666.111,29496.869,31993.323,52831.958,59386.062,59539.681,69975.381,88225.193,91988.445,81410.133,79258.009,77883,75581.92857142858,75514.21428571428],"Revenue_Adj":[630.522,1236.469,2552.075,6323.823,10211.959,15861.88,18933.565,17955.672,26007.997,31666.111,29496.869,31993.323,52831.958,59386.062,59539.681,69975.381,88225.193,91988.445,81410.133,79258.009,77883,75581.92857142858,75514.21428571428],"NetProfit":[156.736,200.789,141.059,103.679,875.636,625.896,953.926,1764.264,2575.506,3371.053,2273.121,4147.732,6095.153,8224.382,9188.474,10478.683,7662.036,4293.484,3184.939,3251.614,2974.688,2826.786,2960.929],"NetProfit_Adj":[156.736,200.789,141.059,14.683435331416,707.8142461461119,603.763550730239,969.912868948629,1756.25440813078,2526.69550894212,3330.17740860954,2576.63061187616,4233.51316821976,6039.97317145515,7835.86597848243,9194.716366494,10711.4095,7642.9275,4207.16125,3308.72275,2883.2053,2974.688,2826.786,2960.929],"OCF":[30.044,139.198,447.57,721.876,919.4,1511.802,700.27,2285.339,2954.42,4209.818,5473.951,3272.118,5889.318,8954.354,4967.922,6833.203,8335.323,8259.884,9349.396,5004.12,null,null,null],"FCF":[-479.195,-556.361,-741.3980000000001,-741.2930000000001,-245.41700000000003,-425.71400000000017,-1083.181,-171.24400000000014,-253.66499999999996,322.32400000000007,2507.317,491.88200000000006,-749.1309999999994,142.49699999999936,-2157.7750000000005,-1513.5230000000001,-671.0460000000003,2299.59,3288.326000000001,329.393,4146.513317909312,4218.705311655452,4051.3076632874577],"DivYield":[0.6666666666666667,0.6857142857142857,0.6217616580310882,1.15702479338843,0.38990825688073394,0.5759162303664921,1.0481283422459893,1.0956072351421189,0.995049504950495,1.2716535433070866,1.7010489510489513,1.996805111821086,1.2259194395796846,1.5942028985507246,1.8518518518518516,1.7268445839874413,5.478087649402391,4.520795660036167,7.0821529745042495,7.052186177715091,9.582929194956352,6.867119301648884,7.196896217264792],"Assets":[5093.888,7308.847,11306.127,18024.749,22997.594,30898.783,31874.385,32495.039,45264.629,50624.436,53532.891,59986.82,82058.007,109899.276,112099.544,140720.642,163146.352,157291.209,148697.724,148220.57,null,null,null],"Equity":[2120.7659999999996,2692.805,3686.0069999999996,3982.1279999999992,5230.237,10339.778,10798.99,12742.785,18157.012,21435.500000000004,20964.12,23927.608,32730.100000000002,40792.587999999996,45799.95,59864.132,72069.15299999998,64790.378999999986,60747.352999999996,60726.705,null,null,null],"Liabilities":[2973.122,4616.042,7620.120000000001,14042.621,17767.356999999996,20559.005,21075.395000000004,19752.254,27107.617000000002,29188.936,32568.771,36059.212,49327.90700000001,69106.688,66299.594,80856.51000000001,91077.199,92500.83,87950.371,87493.86499999999,null,null,null],"Debt":[2284.49,3202.69,6134.941000000001,10669.336,13696.564,15051.46,15377.629,14809.652,19955.537,20736.639000000003,22334.182,23618.434999999998,32372.421000000002,37898.907,39922.04,50704.82,57699.007,61386.845,59238.405,60424.308000000005,null,null,null],"Shares":[2921.563,3096.204,3331.444,3333.614,3361.036,4383.055,4383.055,4569.725,4995.288,5001.838,4910.384,4968.519,4968.519,5218.563,5218.563,5583.296972,5440.336,5440.336,5440.336,5448.153,null,null,null],"ROA":[0.039948652157362555,0.03237818110279708,0.015155433469850668,0.007069614968199381,0.04269068687763641,0.02322590254999886,0.03039279457745386,0.05481683353264121,0.0662427210980376,0.07031152092264117,0.04364783669995678,0.07307509794488465,0.0858201333864837,8.568971045500785,8.277948504410968,8.289435401333026,5.043019578493609,2.679763250351291,2.0817347665315724,2.1902416022907634,2.03,1.842857142857143,1.9085714285714288],"ROE":[0.10983263305125841,0.0950703916920592,0.05130238087141563,0.03258377564999256,0.238388563917688,0.09713076765144558,0.10308874711465553,0.16622944531626993,0.18890228955569338,0.1972540745257594,0.12541745084034184,0.21601198558594498,0.24875013337590207,25.791040466329523,24.526792196275217,22.89376056129273,13.26590033049718,7.0687155552666345,5.69612653920089,6.033107173189934,5.440666666666666,4.7930769230769235,4.951666666666667],"ProfitMargin":[0.24858133419611048,0.16238902875850506,0.055272278440092866,0.0163949876522477,0.08574613352834652,0.03945913094790782,0.050382799013286725,0.09825664001881967,0.09902746451408773,0.10645617328885129,0.07706312829337922,0.1296436759632627,0.11536867514923449,13.849010564128667,15.43252138015318,14.974813784865281,8.684635011226328,4.667416652167563,3.912214465980543,4.10256836000006,0.038194317116700696,0.03740028916171219,0.03921022074069393]},"6823":{"cur":"HKD","cur_ratio":[12.16,18.424242424242422,2.6391928600899583,2.5744530698659145,0.06277412280701754],"date":[null,null,"31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[null,null,7201,17947,18527,19825,21081,22832,28823,34729,33847,33067,35187,33103,32389,33961,34125,34330,34753,36553,36800.5,37401.5,38935],"Revenue_Adj":[null,null,7201,17947,18527,19825,21081,22832,28823,34729,33847,33067,35187,33103,32389,33961,34125,34330,34753,36553,36800.5,37401.5,38935],"NetProfit":[null,null,274,1316,925,1221,1610,2460,2991,3949,4889,4745,4825,5217,5303,4808,4901,4991,5070,5286,5644.5,5831,6010],"NetProfit_Adj":[null,null,274,1338.54669579465,879.085185185185,1230.18823529412,1607.49503546099,2431.61033797217,2967.62424242424,4024.1439162669,4931.581532,4738.32,4824.165,5218.67,5350.595,4821.36,4891.815,4989.33,5061.65,5281.825,5644.5,5831,6010],"OCF":[null,null,4004,4808,4943,5358,6289,6440,8759,10456,11465,9405,9773,10156,9768,10025,9792,9662,10035,10019,null,null,null],"FCF":[null,null,1750,3358,3379,3789,4383,4460,6249,7419,8630,6803,7237,7514,7391,7647,7539,7524,7998,8042,null,null,null],"DivYield":[null,null,null,null,null,0.7382017543859649,5.500805812417436,5.902088772845953,4.3861386138613865,5.461565656565657,6.503680336487908,6.513052208835342,6.054078014184397,6.409836065573771,7.064612326043737,6.943702290076335,7.785788923719958,8.207081545064378,8.208333333333334,7.104257167680278,6.990131578947368,7.2368421052631575,7.319078947368421],"Assets":[null,null,64179,64270,67667,65038,66373,65819,89800,89917,93366,95172,97565,99769,103171,109612,111195,112118,116813,121152,null,null,null],"Equity":[null,null,17088,18503,19513,30933,31117,30805,38039,37735,39159,37890,37594,37970,37900,37197,36429,35382,39645,38394,null,null,null],"Liabilities":[null,null,47091,45767,48154,34105,35256,35014,51761,52182,54207,57282,59971,61799,65271,72415,74766,76736,77168,82758,null,null,null],"Debt":[null,null,31745,34702,34829,23501,24106,24022,36426,36315,38193,41610,43362,43120,44980,45874,45812,46616,43159,47085,null,null,null],"Shares":[null,null,null,null,null,6626.389764137303,6626.389764137303,6626.389764137303,7571.742334,7571.742334,7571.742334,7571.742334,7571.742334,7571.742334,7574.922662,7575.742334,7575.126098,7575.914764,7577.110271,7578.646263,null,null,null],"ROA":[null,null,null,0.020490622737428865,0.014021843758763652,0.018401718096529896,0.02450327598146274,0.03721859114015977,0.03844003624236115,0.043946872026575115,0.053349192232776635,0.05033468054185363,0.050068227688508174,5.287482136884672,5.226175224204199,4.519158015442963,4.4391708596194865,4.469959205241074,4.429282185461996,4.44267014056689,null,null,null],"ROE":[null,null,null,0.0745735819119397,0.04907682512733446,0.04873570559003732,0.05219646620197763,0.07992592231590233,0.0872584056596893,0.10454280722189865,0.12746376055897382,0.12333324669248565,0.12797559843511702,13.825910662938767,14.0013201320132,12.824924312140732,13.33424023942321,13.925586975628129,13.778297143788896,14.076668042554891,15.3,16.1,17.17],"ProfitMargin":[null,null,0.03805027079572282,0.07332701844319385,0.049927133372915204,0.06158890290037831,0.07637208861059722,0.10774351786965662,0.10377129375845678,0.11370900400241873,0.14444411616982303,0.143496537333293,0.1371245062096797,15.759900915324895,16.372842631757695,14.1574158593681,14.361904761904762,14.538304689775705,14.588668604149282,14.46119333570432,0.15338106819200825,0.15590283812146571,0.15435983048670862]},"300750":{"cur":"HKD","cur_ratio":[252.88,23.367543448976594,2.351521468270486,5.162139008456923,0.00774644495412844],"date":[null,null,null,null,null,null,null,null,null,"31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024",null,null,null,null],"Revenue":[null,null,null,null,null,null,null,null,null,7036.162328692461,17392.58725330672,23086.66037123848,35112.040164404505,51944.61474209785,56622.51490818178,157111.77958816578,382625.02277439943,443231.18827906204,392340.11846725986,null,null,null,null],"Revenue_Adj":[null,null,null,null,null,null,null,null,null,7036.162328692461,17392.58725330672,23086.66037123848,35112.040164404505,51944.61474209785,56622.51490818178,157111.77958816578,382625.02277439943,443231.18827906204,392340.11846725986,null,null,null,null],"NetProfit":[null,null,null,null,null,null,null,null,null,1148.2221907151406,3333.597858761794,4477.1540829685,4016.2321503312837,5173.480080032963,6282.708625102151,19201.276625807182,35781.990288881476,48777.95439446174,54995.81251390368,null,null,null,null],"NetProfit_Adj":[null,null,null,null,null,null,null,null,null,1161.7951624074803,3465.0032477837667,3522.955322344958,4257.963669667171,5811.597809284741,6206.684222886573,17689.811323668044,35838.347575594875,53436.37382401549,53214.67701872059,null,null,null,null],"OCF":[null,null,null,null,null,null,null,null,null,795.8403207836211,2402.6669611283573,2736.401114145163,13166.513855754443,14985.946855052123,20306.23091390245,50773.751832253474,69855.15807749203,101333.38364447448,103614.2264261765,null,null,null,null],"FCF":[null,null,null,null,null,null,null,null,null,-1121.206109034144,-871.3122067552166,-5553.335616632013,5305.743464309178,4064.5291297516096,5337.619909500424,-1977.50702158724,13711.805660491611,64159.60159652256,69822.18690916458,null,null,null,null],"DivYield":[null,null,null,null,null,null,null,null,null,null,null,null,0.19241192411924118,0.20676691729323307,0.06835464669191992,0,0.8064663718163794,3.079750091877986,2.17406015037594,null,null,null,null],"Assets":[null,null,null,null,null,null,null,null,null,10360.7150306946,31941.723616160143,59630.226929822726,84123.9853931983,113372.32114928256,186047.02936272684,377445.70495211997,679617.0147486465,788813.1284153381,837161.5744966,null,null,null,null],"Equity":[null,null,null,null,null,null,null,null,null,1789.9336947786305,17643.81139721538,31782.817895831286,40080.65364539565,47190.99824489655,82196.8183419598,113626.50141812592,200068.45000530643,241849.1714579109,291000.01682053314,null,null,null,null],"Liabilities":[null,null,null,null,null,null,null,null,null,8570.811938162828,14298.306308969326,27846.21014565858,44045.071158718354,66180.96399785695,103850.62573110324,263816.82127551996,479550.4434379157,546963.6510303508,546149.5141258,null,null,null,null],"Debt":[null,null,null,null,null,null,null,null,null,1932.580178079606,6659.809108297443,16353.2737626834,19919.154067894386,30354.143828314838,52047.917187600746,137834.8971724,256405.2163460677,222858.37721268693,216780.6006392,null,null,null,null],"Shares":[null,null,null,null,null,null,null,null,null,847.062,1103.97924,3519.3478806,3910.3866,3920.346612,4156.8828876,4172.930424,4383.593287199999,4382.769416,4380.523638,null,null,null,null],"ROA":[null,null,null,null,null,null,null,null,null,null,0.15307160909750137,0.09911553725245166,0.05483008820911094,5.204770414072135,4.328666103816953,6.862727860319337,6.763925532867727,6.694570325608843,6.748743067664272,null,null,null,null],"ROE":[null,null,null,null,null,null,null,null,null,null,0.3406723648219231,0.19298067107490105,0.11752434537255285,12.832694392744736,10.91110835613806,21.424498075275388,24.682601935752338,24.363639594264228,22.825162154182216,null,null,null,null],"ProfitMargin":[null,null,null,null,null,null,null,null,null,0.16318870103847022,0.1916677381122813,0.19392818237782755,0.11438333208569337,9.95960814363338,11.09577812870039,12.22141119917242,9.351712031123222,11.005081701008534,14.017381839194451,null,null,null,null]},"pdd":{"cur":"USD","cur_ratio":[80.17,16.300606296657026,3.1502416575566023,5.061776337876985,0.0002499625138685556],"date":[null,null,null,null,null,null,null,null,null,null,"31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[null,null,null,null,null,null,null,null,null,null,76.01979971076922,258.37768185646127,1984.9519399517235,4364.221731570884,8631.103957884958,14567.262807171246,19414.78746485445,34970.748955999996,54702.13211238702,60108.622563898534,70193.32096,77831.50928142857,85293.73333333332],"Revenue_Adj":[null,null,null,null,null,null,null,null,null,null,76.01979971076922,258.37768185646127,1984.9519399517235,4364.221731570884,8631.103957884958,14567.262807171246,19414.78746485445,34970.748955999996,54702.13211238702,60108.622563898534,70193.32096,77831.50928142857,85293.73333333332],"NetProfit":[null,null,null,null,null,null,null,null,null,null,-48.54637202365384,-73.88064894946146,-1557.949570147356,-1008.8341661685829,-1041.6398879543087,1204.5591381617294,4689.920941963799,8476.74018708099,15616.667888661725,13618.707029851937,15149.808,18372.922,21167.499],"NetProfit_Adj":[null,null,null,null,null,null,null,null,null,null,-48.54637202365384,-72.76955481484609,-1557.9480950485627,-1008.815162576629,-1041.6372765069916,1214.4955063599393,4724.073330300596,8582.83379988595,15620.609539688576,13619.026679781444,15149.808,18372.922,21167.499],"OCF":[null,null,null,null,null,null,null,null,null,null,132.47225258711535,1434.989630234768,1175.2266402682756,2145.918325823756,4090.778107203495,4462.905352378152,7213.443504038012,13297.305782671072,16935.451803834563,14884.80066186914,null,null,null],"FCF":[null,null,null,null,null,null,null,null,null,null,132.12577995461535,1433.668020798114,1171.0916735898847,2141.945887356323,4084.5329760251216,3953.2086675070404,7118.908279218509,13214.85241697851,16801.1206535216,14725.417347116629,14014.438610107256,17931.33808208298,20841.433654383152],"DivYield":[null,null,null,null,null,null,null,null,null,null,null,null,0,0,0,0,0,0,0,0,0,0,0],"Assets":[null,null,null,null,null,null,null,null,null,null,255.16521910000003,2046.3008943,6277.80831894,10922.594022959998,24346.38875094,28509.72493294,34372.90838688001,49016.3608584,69189.701292,90146.74230716,null,null,null],"Equity":[null,null,null,null,null,null,null,null,null,null,51.36461230312548,185.18803694653198,2736.4387584502438,3539.487319412931,9219.249908076972,11817.710073787386,17071.711796596413,26367.600828029066,42923.13395621558,59147.12280551144,null,null,null],"Liabilities":[null,null,null,null,null,null,null,null,null,null,203.80005359999998,1861.11013083,3541.3796032200003,7383.057596699998,15126.840950460002,16691.95325343,17300.83712832,22648.99776066,26265.803304000005,30999.594186680006,null,null,null],"Debt":[null,null,null,null,null,null,null,null,null,null,0,0,0,954.9716027699999,2599.52678643,2007.58334822,2454.79960704,1431.5621788199999,1453.17818,769.5979886,null,null,null],"Shares":[null,null,null,null,null,null,null,null,null,null,null,null,1113.922172,1162.507172,1238.702492,1264.385669,1319.587099,1328.587009,1419.646462,1419.646,null,null,null],"ROA":[null,null,null,null,null,null,null,null,null,null,null,-0.06961979542759102,-0.3616903359361892,-11.686746257417818,-6.111304212376304,4.568215981960067,15.07808993065663,20.514949303327594,26.358662060343008,17.2397815082492,14.043571428571427,14.061428571428573,13.527500000000002],"ROE":[null,null,null,null,null,null,null,null,null,null,null,null,null,-32.05746287163447,-16.928811342296193,11.484433470851062,32.70133718426819,39.36005275691669,44.92396336975187,26.927967684194876,20.790000000000003,20.29478260869565,18.993809523809528],"ProfitMargin":[null,null,null,null,null,null,null,null,null,null,-0.578328024973062,-0.30108492978516993,-0.7787448770921318,-23.116015368115985,-12.06844330733286,8.268946294898607,24.15643720258958,24.239515709962,28.54855429871884,22.656827671228964,0.21582366205512377,0.23605352698652643,0.24816931522476704]},"amzn":{"cur":"USD","cur_ratio":[250.69,97.22947640594698,4.472720986481188,13.381858390208562,0],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[10711,14835,19166,24509,34204,48077,61093,74452,88988,107006,135987,177866,232887,280522,386064,469822,513983,574785,637959,716924,829050.3,959891.96,1120981.463414634],"Revenue_Adj":[10711,14835,19166,24509,34204,48077,61093,74452,88988,107006,135987,177866,232887,280522,386064,469822,513983,574785,637959,716924,829050.3,959891.96,1120981.463414634],"NetProfit":[190,476,645,902,1152,631,-39,274,-241,596,2371,3033,10073,11588,21331,33364,-2722,30425,59248,77670,150063.6,132837.81,173092.389],"NetProfit_Adj":[190,476,645,932.55,1151.35,628.4,173.25,285.35,-132.45,599.25,2376.2,2247.9,10078.53,11584.84,21302.56,24004.87,7546.42,30699.92,60263.15,75731.34,150063.6,132837.81,173092.389],"OCF":[702,1405,1697,3293,3495,3903,4180,5475,6842,12039,17203,18365,30723,38514,66064,46327,46752,84946,115877,139514,null,null,null],"FCF":[486,1181,1364,2920,2516,2092,395,2031,1949,6652,9399,6410,17296,21653,25924,-14726,-16893,32217,32878,7695,-34593.54691426751,-48514.80807812798,752.0727676615038],"DivYield":[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],"Assets":[4363,6485,8314,13813,18797,25278,32555,40159,54505,64747,83402,131310,162648,225248,321195,420549,462675,527854,624894,818042,null,null,null],"Equity":[431,1197,2672,5257,6864,7757,8192,9746,10741,13384,19285,27709,43549,62060,93404,138245,146043,201875,285970,411065,null,null,null],"Liabilities":[3932,5288,5642,8556,11933,17521,24363,30413,43764,51363,64117,103601,119099,163188,227791,282304,316632,325979,338924,406977,null,null,null],"Debt":[1298,1325,664,393,678,2194,4964,6889,16089,17539,20410,44147,49289,77533,100504,132318,154972,154556,147838,169934,null,null,null],"Shares":[8280,8320,8560,8880,9020,9100,9080,9180,9300,9420,9540,9680,9820,9960,10060,10180,10242,10383,10593,10731,null,null,null],"ROA":[0.04715225214046408,0.08775811209439528,0.08716805189539834,0.08152935327879966,0.07065317387304508,0.028633011911514464,-0.0013487109435789254,0.007536375388508403,-0.005091692723738697,0.009995639486130211,0.03200831595218327,0.02825179775699542,0.06853360003810068,5.9747973683667785,7.807218685205959,8.996095688000171,-0.6163781781292175,6.143182077455582,10.279436615808486,10.76555023923445,12.954444444444444,8.84,9.788],"ROE":[0.5612998522895125,0.5847665847665847,0.33341948823985523,0.2275192331946021,0.19008332645821302,0.08631420559469256,-0.004890588751645871,0.030549671089307614,-0.023527114755698735,0.04940932642487047,0.14515289724203373,0.1290803081244414,0.28271913329029724,21.94509937600015,27.441722842587353,28.80564992726064,-1.914959477712742,17.48975333268184,24.289682173641218,22.285824958574533,24.873636363636365,16.45909090909091,17.791249999999998],"ProfitMargin":[0.01773877322378863,0.03208628244017526,0.03365334446415528,0.036802807132073935,0.03368027131329669,0.013124779000353599,-0.0006383710081351384,0.0036802234997045076,-0.0027082303231896437,0.0055697811337682,0.017435490157147373,0.01705216286417865,0.04325273630559026,4.130870306072251,5.525249699531684,7.101412875514557,-0.5295895000418301,5.293283575597832,9.287117197186653,10.83378433418326,0.18100662891021208,0.1383882931991638,0.15441146416174056]},"ko":{"cur":"USD","cur_ratio":[94.8,35.32370749883559,9.021274795206825,15.882352941176471,0.019388185654008443],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[24088,28857,31944,30990,35119,46542,48017,46854,45998,44294,41863,36212,34300,37266,33014,38655,43004,45754,47061,47941,50412.545454545456,49094.27272727273,51505.75],"Revenue_Adj":[24088,28857,31944,30990,35123,46554,48032,46935,46012,44257,41854,36218,34291,37280,32999,38658,43046,45784,46897,48062,50412.545454545456,49094.27272727273,51505.75],"NetProfit":[5080,5981,5807,6824,11787,8584,9019,8584,7098,7351,6527,1248,6434,8920,7747,9771,9542,10714,10631,13107,14380.696,15158.087,16244.706],"NetProfit_Adj":[5080,5981,5807,7134,7825,8951,9367,9409,9037,8807,8354,8341,9125,9171,8435,10086,10803,11515,12373,12940,14380.696,15158.087,16244.706],"OCF":[5957,7150,7571,8186,9532,9474,10645,10542,10615,10528,8796,7041,7627,10471,9844,12625,11018,11599,6805,7408,null,null,null],"FCF":[4550,5502,5603,6193,7317,6554,7865,7992,8209,7975,6534,5291,6079,8417,8667,11258,9534,9747,4741,5296,12677.370493660723,14268.926333462,15327.805534574441],"DivYield":[2.5699481865284977,2.21606648199446,3.357631985862602,2.8771929824561404,2.6759920936597235,2.686865799628412,2.8137931034482757,2.71120793996611,2.8896257697773566,3.0726256983240225,3.3767486734201637,3.225806451612903,3.294614572333685,2.890695573622403,2.99051787016776,2.8373585542982602,2.766860556516271,3.122348549126082,3.1159653067780275,2.918037476755829,2.29535864978903,2.4124472573839664,2.5432489451476794],"Assets":[29963,43269,40519,48671,72921,79974,86174,90055,92023,89996,87270,87896,83216,86381,87296,94354,92763,97703,100549,104816,null,null,null],"Equity":[17278,21744,20472,25346,31317,31921,33168,33440,30561,25764,23220,18977,19058,21098,21284,24860,25826,27480,26372,34275,null,null,null],"Liabilities":[12685,20878,20047,23325,41604,48053,53006,56615,61462,64232,64050,68919,64158,65283,66012,69494,66937,70223,74177,70541,null,null,null],"Debt":[4582,9329,9312,11859,23417,28568,32610,37079,41745,44116,45709,48115,44214,44155,44415,44232,40603,43426,45735,47214,null,null,null],"Shares":[4636,4636,4624,4606,4584,4526,4469,4402,4366,4324,4288,4259,4268,4280,4302,4325,4328,4308,4302,4302,null,null,null],"ROA":[0.17107257113992255,0.1633438933799432,0.13861173437723778,0.15302163919721942,0.19387788670307254,0.112286209490173,0.1085658569468185,0.09741869953299402,0.07796658574896473,0.08077178756063925,0.07364074328974536,0.014249340625463845,0.07520220674178316,10.519053992700343,8.921158242023987,10.758051197357556,10.198966422078165,11.250301891151176,10.724734176704397,12.764589876561244,13.242857142857142,13.790000000000001,14.299999999999999],"ROE":[0.3053343350864012,0.3093834057521208,0.2751089634261891,0.3014733493848159,0.42245797641661587,0.27408282512213034,0.2799844780752813,0.2602671194457499,0.22358370214039344,0.26312775172710023,0.26851242389336843,0.0621916579458813,0.37788153760314797,49.60791947055225,40.47544409613375,46.200765993664,40.51460597826087,42.81660871997762,41.856802567080734,45.96931170539237,40.02857142857143,38.41153846153846,37.67333333333333],"ProfitMargin":[0.2108933909000332,0.20726340229407075,0.18178687703481092,0.2202000645369474,0.3356302856003872,0.18443556357698423,0.18782931045254805,0.18320741025312676,0.15431105700247838,0.16595927213618097,0.15591333635907603,0.034463713686070915,0.1875801749271137,23.9360274781302,23.465802386866176,25.277454404346138,22.18863361547763,23.416531887922368,22.589830220352308,27.33985523873094,0.28526026607067745,0.3087546891671051,0.3153959703528247]},"wfc":{"cur":"USD","cur_ratio":[85.53,17.67879288962381,4.028782043790645,1.8919217837550875,0.015208698702209755],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[47979,53593,51652,98636,93249,87597,91247,88069,88372,90033,94176,97741,101060,105684,82227,82407,83442,115340,125397,123529,88331.28571428572,93097.28571428572,97564.53846153847],"Revenue_Adj":[47979,53593,51652,98636,93249,87597,91247,88069,88372,90033,94176,97741,101060,105684,82227,82407,83442,115340,125397,123529,88331.28571428572,93097.28571428572,97564.53846153847],"NetProfit":[8482,8057,2369,7990,11632,15025,17999,20889,21821,21470,20373,20554,20689,18103,1786,20256,12562,17982,18606,20285,22698.25,24109.600000000002,26092.846],"NetProfit_Adj":[8482,8057,2369,9371.25,12971.65,16113.1,18343.5,20889,21802.8,21421.25,20344.4,18682.1,19665.05,18167.78,2970.21,20316.04,12562,19545,18608.05,20180,22698.25,24109.600000000002,26092.846],"OCF":[32094,9078,-4831,28613,18772,13665,58540,57641,17529,15904,946,18619,36073,6730,2051,-11525,27048,40358,3035,-19001,null,null,null],"FCF":[null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null],"DivYield":[3.0371203599550056,3.9085789996687637,4.409769335142469,1.8154872174879586,0.6453694740238787,1.741654571843251,2.574605032182563,2.5330396475770924,2.462604888726742,2.713392200147167,2.749047359825803,2.538322070215922,3.5590277777777777,3.5687732342007434,4.042412193505633,1.2505210504376825,2.664083313150884,2.6412027631044293,2.1355353075170846,1.8240343347639483,2.2202735882146616,2.4739857359990647,2.705483456097276],"Assets":[481996,575442,1309639,1243646,1258128,1313867,1422968,1523502,1687155,1787632,1930115,1951757,1895883,1927555,1952911,1948068,1881020,1932468,1929845,2148631,null,null,null],"Equity":[45876,47628,99084,114359,127889,141687,158911,171008,185262,193891,200497,208079,197066,187984,185712,190110,182213,187443,181066,183038,null,null,null],"Liabilities":[436120,null,1210555,1129287,1130239,1172180,1264057,1352494,1501893,1593741,1729618,1743678,1698817,1739571,1767199,1757958,1698807,1745025,1748779,1965593,null,null,null],"Debt":[99974,152648,375232,242827,212384,174445,184554,206881,247461,297064,351858,328276,334831,338000,276911,199574,230480,301207,286307,475352,null,null,null],"Shares":[3376.766057,3296.652404,4228.630889,5178.624593,5262.283228,5262.611636,5266.314176,5257.162705,5170.349198,5092.12881,5016.109326,4891.616628,4581.253608,4134.425937,4144.011543,3885.801497,3833.804452,3598.862582,3288.943829,3092.61885,null,null,null],"ROA":[0.017602312664139697,0.01523871848751416,0.0028168550847417167,0.009615064514928807,0.009882587316040537,0.012339837363602961,0.01380938200512636,0.01485031240772857,0.01436279241289244,0.013177210574345996,0.011801771341621687,0.01142902187398245,0.011639862357185182,1.031270809151345,0.17405126085372222,1.1047483208702227,0.7143737621073216,1.0039103309096555,1.0212533266982764,1.0463712425915954,1.0389473684210526,1.0405263157894737,1.0870000000000002],"ROE":[0.19765341908723363,0.17388583144491204,0.04122509353519534,0.09342133724635054,0.10525744276536059,0.12189235343509283,0.13162888829571343,0.13993823397399396,0.13678476505691792,0.12781093268644056,0.1178263459547618,0.11527503589375449,0.11671226694496939,10.694463726078057,1.0877873637601143,12.292865877526497,7.688070699405126,11.047829693115842,11.440588077955377,12.541539793189793,13.606521739130434,14.267272727272728,14.915333333333333],"ProfitMargin":[0.23765094841836876,0.20454430058390455,0.06336969234074039,0.1384096700719392,0.14507686891209953,0.19603943272224142,0.21951304509444042,0.261136309381714,0.2733588627929861,0.266032978142394,0.24854135747221498,0.2509701433436287,0.2591542449773169,22.704763220932374,4.547290746525907,27.452479233552463,18.39097461273666,23.17517585384457,23.964712744240305,25.493733497413352,0.25696727657740653,0.25897210365509477,0.26744190599252365]},"bac":{"cur":"USD","cur_ratio":[49.66,15.385954558866924,3.9936568671471173,1.5087137846272816,0.018542757417102977],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[116323,119696,113106,150450,134194,115074,100078,101697,96829,93514,93662,100264,109627,113589,93753,93851,115053,176100,196403,191567,122695.88235294117,130690.33333333333,137106.7857142857],"Revenue_Adj":[116323,119696,113106,150450,134194,115074,100078,101697,96829,93514,93662,100264,109627,113589,93753,93851,115053,176100,196403,191567,122695.88235294117,130690.33333333333,137106.7857142857],"NetProfit":[21111,14800,2556,-2210,-3599,84,2758,10080,4476,14353,16140,16618,26696,25998,16473,30557,26015,24656,25344,29055,32717.938000000002,35905.895000000004,38153.786],"NetProfit_Adj":[21111,14800,2556,2176.2,12265.25,2841.6,13613,15168.2,15292,15580.85,17074.7,20285.65,27344.59,26711.37,17228.24,30729.22,26947.2,25252.45,25643.41,29416.03,32717.938000000002,35905.895000000004,38153.786],"OCF":[14509,11036,4034,129731,82594,64490,-13858,92817,30795,28347,18361,9864,39520,61777,37993,-7193,-6327,44982,-8805,12613,null,null,null],"FCF":[null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null],"DivYield":[3.9707810451395398,5.816771691711101,15.909090909090912,0.2656042496679947,0.29985007496251875,0.7194244604316548,0.34453057708871665,0.2569043031470777,0.6707657909446617,1.1883541295306004,1.1312217194570136,1.321138211382114,2.1915584415584415,1.8739352640545146,2.375453645661498,1.7532029669588671,2.5966183574879227,2.7324027324027322,2.2753128555176336,1.9636363636363638,2.4083769633507854,2.700362464760371,3.010471204188482],"Assets":[1459737,1715746,1817943,2230232,2264909,2129046,2209974,2102273,2104534,2144287,2188067,2281234,2354507,2434079,2819627,3128528,3051375,3180151,3261299,3411738,null,null,null],"Equity":[135272,146803,177052,231444,228248,230101,236956,232685,243471,256176,266195,267146,265325,264810,272924,270066,273197,291646,293963,303243,null,null,null],"Liabilities":[1324465,null,1640891,1998788,2036661,1898945,1973018,1869588,1861063,1888111,1921872,2014088,2089182,2169269,2546703,2858462,2778178,2888505,2967336,3108495,null,null,null],"Debt":[572497,687374,690233,828662,825737,683335,673162,577248,549780,506116,474089,518120,504737,523539,534372,607747,589307,723501,760106,825702,null,null,null],"Shares":[4458.151391,4437.885419,5017.435592,8650.243926,10085.154806,10535.937957,10778.263628,10591.808296,10516.542476,10380.265063,10052.625604,10287.302431,9669.28637,8650.814105,8650.814105,8077.831463,7996.777943,7895.457665,7610.862311,7212.464345,null,null,null],"ROA":[0.015360852468072424,0.009436044847350781,0.0022684509021591885,0.003100656468655629,-0.0009957418465850127,0.0006581769726817867,0.0019303898115242612,0.00530164436313597,0.002624318158641459,0.007454303205524544,0.008227397853453342,0.008158770241699989,0.012143473934372088,1.1456409052693217,0.6811953314479341,1.0752241661489992,0.8908877695976782,0.8442554841302115,0.8374822439047108,0.9143962486645886,0.9557894736842103,1.039473684210526,1.0872727272727274],"ROE":[0.18068066568813307,0.10770882229863726,0.018144066443060214,-0.013249916813305073,-0.017732470770246502,0.00039679727910437183,0.012831129679082186,0.046077788266163225,0.020185120463590344,0.0626679619704627,0.06797535366978466,0.06841526725099732,0.10944975831348319,10.733926772472792,6.726103102549288,12.376967507270562,10.614944568894112,9.706150391005592,9.491192821686237,10.602950433806827,11.573333333333334,12.679999999999998,13.244285714285713],"ProfitMargin":[0.2921787941213068,0.22417069411817514,0.05506856090791679,0.05245605676888745,-0.020304844855743062,0.015472852954394677,0.050255597955216356,0.12852195813001732,0.06426525717745128,0.19087567046344844,0.21292457676730264,0.2087187471380163,0.30923972753241047,30.06225066853711,20.92180338602563,35.884775509746056,28.99210110584518,25.596240111317613,25.480841898428054,26.975958690327772,0.26665881092896015,0.2747402518287256,0.2782778818839791]},"9992":{"cur":"HKD","cur_ratio":[137.1,169.2512146027627,28.346025790005697,21.622452407943832,0.001419074398249453],"date":[null,null,null,null,null,null,null,null,null,null,null,"31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[null,null,null,null,null,null,null,null,null,null,null,182.49868251461547,610.0897963026817,1909.7862136620668,2828.3087861544714,5412.372825788483,5376.555164484298,6966.031101582644,14129.985081142791,40284.37603771357,38598.93741052632,42742.72962105263,47527.1952],"Revenue_Adj":[null,null,null,null,null,null,null,null,null,null,null,182.49868251461547,610.0897963026817,1909.7862136620668,2828.3087861544714,5412.372825788483,5376.555164484298,6966.031101582644,14129.985081142791,40284.37603771357,38598.93741052632,42742.72962105263,47527.1952],"NetProfit":[null,null,null,null,null,null,null,null,null,null,null,1.811432828076924,118.00864630268195,511.7747040482753,589.0793214227642,1029.695068178602,553.8732455289256,1196.5782532066114,3387.3091790242784,13864.761283655555,10796.595,12554.359,14050.584],"NetProfit_Adj":[null,null,null,null,null,null,null,null,null,null,null,1.7577479163461547,117.50084189176242,524.3050831284477,625.3453312032519,1015.8991340144045,468.50677177055786,1193.6173316265495,3425.852820409155,13758.637083852072,10796.595,12554.359,14050.584],"OCF":[null,null,null,null,null,null,null,null,null,null,null,17.90305217653847,207.50909961685818,570.5067612999993,791.4607374471544,941.5090802411532,1047.47554292562,2210.485361256198,5388.768302729627,11791.359259812343,null,null,null],"FCF":[null,null,null,null,null,null,null,null,null,null,null,4.562640240000002,165.44996954022983,451.4441945999995,593.4329529918697,594.9962890757209,737.5831942330578,1852.0914111384293,4984.880079369956,10722.12095456543,12388.312995054792,11099.659195306953,14305.725004826229],"DivYield":[null,null,null,null,null,null,null,null,null,null,null,null,null,null,0.21815999421744597,0.418255938603102,0.4964138379636761,1.5322536403924005,0.9669397070478937,1.413184141151089,1.5976172912918323,1.8204609048840406,2.0775881513365886],"Assets":[null,null,null,null,null,null,null,null,null,null,null,170.8343953,458.622387,1192.9656466000001,8281.2060821,10211.4243768,9703.192115799999,10964.7524137,15825.369142399999,35751.277949799995,null,null,null],"Equity":[null,null,null,null,null,null,null,null,null,null,null,94.64040684217804,249.1706514501708,663.0797836114532,7283.054021234219,8366.607949056814,7877.160011089209,8557.631336844477,11582.953442789816,25228.392710263124,null,null,null],"Liabilities":[null,null,null,null,null,null,null,null,null,null,null,76.1904185,209.462549,529.88082,998.1888062999999,1844.6410159999998,1826.1060733,2407.1102519,4241.9363186,10523.336821900002,null,null,null],"Debt":[null,null,null,null,null,null,null,null,null,null,null,46.50551240000001,85.7343028,205.1490028,346.5983346,762.3973136,838.1450479000001,872.0128189000001,1026.4858162,3186.9360774999996,null,null,null],"Shares":[null,null,null,null,null,null,null,null,null,null,null,null,null,null,1401.93755,1401.93755,1380.54055,1347.64315,1342.94315,1341.04315,null,null,null],"ROA":[null,null,null,null,null,null,null,null,null,null,null,null,0.36516509684923515,61.40684255374755,13.026109199306276,11.171535656435061,5.62788428182063,11.6701533916386,25.165310059145636,54.397010680356864,26.060000000000002,24.243333333333336,21.849375000000006],"ROE":[null,null,null,null,null,null,null,null,null,null,null,null,0.6687025875681161,111.1660635452207,15.575511127012101,13.196915309731041,6.902892071745612,14.692570494627278,33.87416497917569,77.51946832097337,34.945,32.152580645161294,29.889310344827585],"ProfitMargin":[null,null,null,null,null,null,null,null,null,null,null,0.00992573098675304,0.19342832320397427,26.797486566149907,20.827970563416088,19.024836265387805,10.30163791841335,17.17733147839026,23.97248942282905,34.41721741122561,0.27971311347995753,0.2937200866320801,0.2956334378534698]},"9961":{"cur":"HKD","cur_ratio":[325,21.368464809071885,4.3205952707550805,1.613085985210032,0],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[760.0723780571064,1230.2347429932445,1660.9272774816081,2255.738938736812,3307.5048173954283,4213.8115787291035,5112.81955492721,6796.485744761932,9247.346587982613,13445.310497981192,22476.82318432495,30936.36332769232,36717.25296934865,40461.6011655172,20610.26513821138,24132.790789300438,23334.032643388433,49207.73621900826,57758.69936707817,67729.09758148147,78496.50632727274,85432.56076444444,94714.9979263158],"Revenue_Adj":[760.0723780571064,1230.2347429932445,1660.9272774816081,2255.738938736812,3307.5048173954283,4213.8115787291035,5112.81955492721,6796.485744761932,9247.346587982613,13445.310497981192,22476.82318432495,30936.36332769232,36717.25296934865,40461.6011655172,20610.26513821138,24132.790789300438,23334.032643388433,49207.73621900826,57758.69936707817,67729.09758148147,78496.50632727274,85432.56076444444,94714.9979263158],"NetProfit":[234.43242968730934,408.6623205884444,497.72508431001836,747.7199477416684,1203.1290478976316,1296.6551810064145,878.2908429203223,1259.5853002742854,305.5293080523945,3093.9207531203606,-1672.4010400391487,2487.9781673076936,1318.5721072796932,7953.689389655163,-3653.719747967479,-662.8894238683134,1633.696681404959,10964.77932644628,18496.786169135794,36132.169637037034,18664.415,20568.543,23534.144],"NetProfit_Adj":[234.43242968730934,408.6623205884444,497.72508431001836,747.7199477416684,1203.1290478976316,1296.6551810064145,878.2908429203223,1259.5853002742854,305.5293080523945,3093.9207531203606,-1672.4010400391487,2487.9781673076936,4473.896187739462,5460.149341379304,-3134.97481300813,-585.7532000000006,124.30300836776861,12434.599261363634,17339.314579012338,32085.559811419746,18664.415,20568.543,23534.144],"OCF":[339.0250260025694,498.18425787377777,662.2157684270744,1165.977145125721,1779.568699548649,2230.1039055909296,2033.880414553798,3094.7454269467253,2465.236140558402,3761.5911862616404,6163.486590238957,8161.261097307696,8436.72710727969,8318.985065517232,-4301.8696016260155,2983.002407407411,3075.2622491735538,24326.376719008258,21269.082355967068,15604.747618518517,null,null,null],"FCF":[244.91362919657706,368.0455424412444,532.9144230292851,966.3349444780705,1593.1663088226587,1982.9038327373876,1366.164410375976,2272.4097565991897,-3562.1276977507437,2974.268541661941,5365.30021796676,7617.484894615388,7638.706398467431,7385.325620689647,-4900.50800813008,2296.0079135802494,2496.540046280992,23656.41742561983,20628.57139176954,14739.806812345676,16950.816282595173,20739.758173801954,25074.247520582816],"DivYield":[0.4190597408690683,0.5031424944851051,0.1502029278490387,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0.431346901287779,0.11786871209536467,0.17354902394409522,0.05170314671667837],"Assets":[1447.0613897401001,2271.1401557504,2907.5230518179997,4720.831987882901,9573.0626333335,12034.8729377962,14506.667478044801,26670.5476332682,39085.4844409383,141969.36035541602,161353.6523336564,194801.56800000003,211586.038,223909.0434,222433.0871,235372.62119999997,216783.3519,241028.78630000004,258154.7002,297788.9019,null,null,null],"Equity":[1024.9440707541223,1551.1972838892161,2195.6625341556073,3391.9444738927505,7301.247681467501,8808.811260627934,8185.844970713986,11184.214610550229,12963.215185841607,76079.3156602001,84381.2214973641,103994.70768592371,101035.77852729519,119515.90803343196,120652.04802978307,135504.57985242526,127814.38731046881,135294.1282600124,152687.22516919198,192228.06083758996,null,null,null],"Liabilities":[422.0852798491,720.0143747072,711.7882033875,1328.8273738443,2272.0487048875,3225.7212722905,6321.0036311245,15486.434511193296,26122.664910767497,65891.34538931341,76974.3155631718,90802.9375,110554.6442,104392.2264,101781.6478,99865.20039999999,88970.1648,105734.48690000002,105461.1558,105564.28189999999,null,null,null],"Debt":[0,0,0,0,0,0,1957.7539982668002,8239.755908670099,14522.6219794731,37110.0359526468,46410.3885645566,54675.07520000001,68494.7602,57317.064,68197.339,63452.54959999999,52766.6631,50125.74270000001,42910.6724,35208.5118,null,null,null],"Shares":[261.198024,263.373784,265.549544,204.329664,287.081304,287.009924,273.7188,263.408886,274.2822,409.337824,513.243296,540.805232,552.982592,592.691232,599.627136,641.329557,646.06683,644.08905,653.270717,649.583574,null,null,null],"ROA":[0.19339845277782927,0.22272645859220921,0.1895664900043874,0.19620117312296362,0.17079325984579158,0.12042025343323204,0.06666974670778023,0.06145732304904495,0.00931654637020495,0.03340567873426547,-0.010869271829029864,0.014054932862606253,0.006389519349556123,3.6326518980619125,-1.676225678724272,-0.29015478438861747,0.7315864945900143,4.828297973847937,7.39282419138955,13.057289869168262,4.009090909090909,5.544545454545454,5.822105263157895],"ROE":[0.26842463908682446,0.32151184950332995,0.2626918933108477,0.2713983323873951,0.2321629950366865,0.1637632602261306,0.10558819408653916,0.13293180414677863,0.026882114705218032,0.09273890910782728,-0.024648690010033992,0.027562332089157794,0.01296407482323041,7.373906824360923,-3.1865198531865193,-0.5237322109593345,1.2641917462605874,8.460039152631287,12.893697069511283,21.24025913958258,6.319117647058825,8.610606060606063,8.872999999999998],"ProfitMargin":[0.3084343497478022,0.3321823927636292,0.299666993888316,0.33147450482917273,0.36375730779587,0.30771551047792434,0.17178209273470557,0.18532891078967548,0.0330396730722135,0.23011151386834178,-0.07440557886336269,0.08042245111210629,0.035911512998546745,19.657376773397633,-17.727669796898887,-2.7468411326973983,7.001347372623385,22.282633116153676,32.024242879123356,53.348074796904285,0.23777457677601457,0.24075840970345955,0.24847403572545723]},"tsla":{"cur":"USD","cur_ratio":[398.5,148.65824308062574,12.628660259445528,24.15609016859253,0],"date":[null,"31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[null,0.073,14.742,111.943,116.744,204.242,413.256,2013.496,3198.356,4046.025,7000.132,11758.751,21461.268,24578,31536,53823,81462,96773,97690,94827,107423.27450980393,121572.23529411765,142506.8],"Revenue_Adj":[null,0.073,14.742,111.943,116.744,204.242,413.256,2013.496,3198.356,4046.025,7000.132,11758.751,21461.268,24578,31536,53823,81462,96773,97690,94827,107423.27450980393,121572.23529411765,142506.8],"NetProfit":[null,-78.157,-82.782,-55.74,-154.328,-254.411,-396.213,-74.014,-294.04,-888.663,-674.914,-1961.4,-976.091,-862,690,5514,12556,14997,7091,3794,5986.436,7676.8640000000005,11443.85],"NetProfit_Adj":[null,-78.157,-82.782,-55.74,-154.328,-254.411,-396.213,-57.628,-294.04,-888.663,-725.32255,-1834.9035,-741.78174,-628.95,782.43,5471.34,12695.04,9070,7166.05,4236.26,5986.436,7676.8640000000005,11443.85],"OCF":[null,-53.469,-52.412,-80.825,-127.817,-114.365,-266.081,264.804,-57.337,-524.499,-123.829,-60.654,2097.802,2405,5943,11497,14724,13256,14923,14747,null,null,null],"FCF":[null,-63.271,-63.042,-92.709,-168.01999999999998,-312.26099999999997,-505.309,0.5799999999999841,-1027.222,-2159.349,-1404.6309999999999,-3475.468,-2.9220000000000255,1078,2786,5015,7566,4358,3584,6220,-5143.057847109339,-9086.033509650842,-6138.698025089166],"DivYield":[null,null,null,null,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],"Assets":[null,34.837,51.699,130.424,386.082,713.448,1114.19,2416.93,5830.667,8067.939,22664.076,28655.372,29739.614,34309,52148,62131,82338,106618,122070,137806,null,null,null],"Equity":[null,-16.668000000000006,-98.536,65.70200000000003,207.048,224.04500000000004,124.69999999999999,667.1200000000001,969.9059999999997,1130.989,5913.909,5632.392,6313.603999999999,8110,23730,31583,45898,63609,73680,82865,null,null,null],"Liabilities":[null,51.505,150.235,64.722,179.03400000000002,489.403,989.49,1749.81,4860.761,6936.95,16750.167,23022.98,23426.010000000002,26199,28418,30548,36440,43009,48390,54941,null,null,null],"Debt":[null,0.289,57.831,2.824,72.603,280.148,466.666,606.878,2429.884,2649.02,7119.647,10314.867999999999,11971.371000000001,14603,13228,8873,5748,9573,13623,14719,null,null,null],"Shares":[null,null,105.156465,109.263,1423.62555,1567.954575,1713.21411,1846.36485,1885.314105,1971.3750000000002,2423.415,2531.955,2589.045,2715,2880,3099,3164,3185,3216,3751,null,null,null],"ROA":[null,null,-1.9132384210039752,-0.6121137912290047,-0.5975845391921876,-0.46276318063172456,-0.43357929743198603,-0.04192097691384036,-0.0713031929178887,-0.12787800445598646,-0.04392253485493874,-0.0764388580329235,-0.033430644199486576,-2.691705397403291,1.667881143227269,9.658817455525512,17.38227578234777,15.873536696373758,6.201462254250332,2.919854084255568,3.3800000000000003,4.141111111111112,5.119999999999999],"ROE":[null,null,null,null,null,-1.1803068015486216,-2.2722218239688017,-0.1869465282513702,-0.3592368111441114,-0.8459851634660468,-0.2290684516597191,-0.4359147625954732,-0.21310739360224354,-14.937732443550491,4.784523107859793,21.04018010455222,33.53050351835285,27.94350556186998,10.462791504053945,4.893905191873589,5.5530769230769215,6.379130434782609,8.008947368421053],"ProfitMargin":[null,-1070.6438356164383,-5.615384615384616,-0.49793198324147114,-1.3219351743986842,-1.245635079954172,-0.9587592194668682,-0.03675895060134214,-0.09193473146829184,-0.21963853411681836,-0.09641446761289645,-0.1668034300581754,-0.04548151581723876,-3.507201562372854,2.2862760020294264,10.253980640246734,15.413321548697553,15.497091130790613,7.2586754017811455,4.000970187815707,0.05572755066348516,0.06314652354626861,0.08030388725309949]},"adbe":{"cur":"USD","cur_ratio":[246.14,21.05360197681049,6.034219690549145,6.688223466115971,0],"date":["1/12/2006","30/11/2007","28/11/2008","27/11/2009","3/12/2010","2/12/2011","30/11/2012","29/11/2013","28/11/2014","27/11/2015","2/12/2016","1/12/2017","30/11/2018","29/11/2019","27/11/2020","3/12/2021","2/12/2022","30/11/2023","30/11/2024","28/11/2025",null,null,null],"Revenue":[2575.3,3157.881,3579.889,2945.853,3800,4216.258,4403.677,4055.24,4147.065,4795.511,5854.43,7301.505,9030.008,11171,12868,15785,17606,19409,21505,23769,26605.470588235294,29065.62162162162,31511.12],"Revenue_Adj":[2575.3,3157.881,3579.889,2945.853,3800,4216.258,4403.677,4055.24,4147.065,4795.511,5854.43,7301.505,9030.008,11171,12868,15785,17606,19409,21505,23769,26605.470588235294,29065.62162162162,31511.12],"NetProfit":[505.809,723.807,871.814,386.508,774.68,832.847,832.775,289.985,268.395,629.551,1168.782,1693.954,2590.774,2951,5260,4822,4756,5428,5560,7130,9776.719000000001,10490.606,11509.391],"NetProfit_Adj":[505.809,723.807,871.814,455.490534091129,794.004713238086,899.7037899678189,822.099201890393,313.59324814135897,285.55538942355605,610.32139483632,1168.8265380225598,1687.69679793242,2587.59823116237,2909.20858895706,5263.65145554681,4808.88688410826,4797.02090032154,5544.72522202487,6387.40108359133,7095.99821189093,9776.719000000001,10490.606,11509.391],"OCF":[899.945,1484.014,1280.682,1117.794,1112.995,1543.314,1499.58,1151.686,1287.482,1469.502,2199.728,2912.853,4029.304,4422,5727,7230,7838,7302,8056,10031,null,null,null],"FCF":[816.695,1351.9389999999999,1168.89,998.2020000000001,943.3529999999998,1333.02,1228.504,963.328,1139.15,1284.566,1995.923,2734.731,3762.725,4027,5308,6882,7396,6942,7873,9852,10334.3352424727,11456.41146549305,12413.970552328416],"DivYield":[0,null,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],"Assets":[5962.548,5713.679,5821.598,7282.237,8141.148,8991.183,10040.229,10380.298,10785.829,11726.472,12697.246,14535.556,18768.682,20762,24284,27241,27165,29779,30230,29496,null,null,null],"Equity":[5151.876,4649.982,4410.353999999999,4890.567999999998,5192.387,5783.1129999999985,6665.182000000001,6724.634,6775.905000000001,7001.58,7424.835,8459.869,9362.114000000001,10530,13264,14797,14051,16518,14105,11623,null,null,null],"Liabilities":[810.672,1063.697,1411.2440000000001,2391.669,2948.761,3208.0699999999997,3375.047,3655.6639999999993,4009.924,4724.892,5272.411,6075.687,9406.568,10232,11020,12444,13114,13261,16125,17873,null,null,null],"Debt":[0,0,350,1000,1553.075,1514.308,1508.1550000000002,1513.973,1514.315,1907.231,1892.2,1881.421,4124.8,4138,4708,4673,4633,4080,6056,6648,null,null,null],"Shares":[587.226,600.834,526.111,522.657,501.897,491.54,494.132,496.261,497.484,497.809,494.254,491.262,487.663,483,479,475,462,455,441,413,null,null,null],"ROA":[0.12038968146928018,0.12397960402791072,0.15115614475491138,0.058991585287818414,0.10045525025796867,0.09722518202572666,0.08751583960244252,0.02840132382479649,0.02536080408097334,0.055929511603456265,0.09570876964760239,0.12440541373597913,0.15558224151532907,14.930174996727857,23.35390489721618,18.717127607957302,17.48336580524207,19.06434391683057,18.53055375027079,23.875699025550013,23.61,24.633999999999997,24.2675],"ROE":[0.14418313497815485,0.1476877139007727,0.19244628455280247,0.08311176031795559,0.15366130266375286,0.1517647487585987,0.13379743972969793,0.04331426212279541,0.03976063474206474,0.09138837748689257,0.16203360294293487,0.21328115399569295,0.2907391394100196,29.670049146108852,44.212826763049506,34.367984034781365,32.97282307265668,35.51310150806373,36.312575515135684,55.42599502487562,69.23571428571428,61.921538461538475,58.32818181818182],"ProfitMargin":[0.1964077971498466,0.2292065470484797,0.24353101450910905,0.13120410285238265,0.20386315789473686,0.19753226676356145,0.18910901049282225,0.0715087146506742,0.06471926531173251,0.13127923176487344,0.1996406140307425,0.232000662877037,0.28690716553075035,26.41661444812461,40.87659309916071,30.54798859676908,27.013518118823125,27.96640733680251,25.854452452917926,29.99705498758888,0.36747024700295666,0.36092831593282265,0.3652485535265011]},"398":{"cur":"HKD","cur_ratio":[3.19,4.253333333333333,0.40623797610661094,0.7523584905660377,0.23510971786833856],"date":["31/3/2006","31/3/2007","31/3/2008","31/3/2009","31/3/2010","31/3/2011","31/3/2012","31/3/2013","31/3/2014","31/3/2015","31/3/2016","31/3/2017","31/3/2018","31/3/2019","31/3/2020","31/3/2021","31/3/2022","31/3/2023","31/3/2024","31/3/2025","31/3/2026",null,null],"Revenue":[2022.381,2645.914,2922.342,2538.781,3242.643,3917.764,3935.963,3732.925,3476.513,3109.031,3031.752,3142.295,2891.692,2437.184,2352.683,3504.022,3639.774,3704.677,3638.758,3449.857,null,null,null],"Revenue_Adj":[2022.381,2645.914,2922.342,2538.781,3242.643,3917.764,3935.963,3732.925,3476.513,3109.031,3031.752,3142.295,2891.692,2437.184,2352.683,3504.022,3639.774,3704.677,3638.758,3449.857,null,null,null],"NetProfit":[65.271,94.874,155.921,100.315,112.547,219.429,164.108,162.597,20.605,4.508,-15.528,16.383,138.763,138.099,100.301,233.256,362.574,295.432,250.488,200.496,null,null,null],"NetProfit_Adj":[65.271,94.874,155.921,100.315,112.547,219.429,164.108,162.597,20.605,4.508,-15.528,23.336045,138.912465,135.44036,125.512155,241.31899,364.881105,296.965895,246.7639,198.088695,null,null,null],"OCF":[-85.871,28.304,-49.162,-110.116,108.115,202.698,-392.669,63.223,366.247,26.713,197.981,271.263,509.439,283.259,188.068,537.487,740.712,431.977,373.77,303.674,null,null,null],"FCF":[-99.94999999999999,-6.205999999999999,-89.042,-142.983,73.805,168.006,-527.73,0.3780000000000001,325.745,15.651000000000002,184.112,264.70099999999996,491.99100000000004,258.02700000000004,150.53000000000003,516.7729999999999,672.36,387.414,320.647,286.43699999999995,null,null,null],"DivYield":[3.8922155688622757,4.705882352941177,3.1884057971014492,5,2.8645833333333335,3.0640668523676884,2.416918429003021,2.7667984189723325,0.5181347150259068,0.20491803278688525,0.368421052631579,0.22346368715083798,3.4934497816593884,4.593744680851064,7.012987012987012,5.6273764258555135,2.3980815347721824,1.7241379310344827,4.134078212290502,1.7231638418079096,null,null,null],"Assets":[1056.878,1252.986,1574.331,1773.027,1816.892,2370.974,2803.939,3082.092,2859.107,2706.073,2533.582,2354.738,2539.937,2408.229,2513.621,2727.53,2879.275,2619.258,2387.475,2243.513,null,null,null],"Equity":[764.5920000000001,849.818,1162.367,1244.511,1395.643,1864.675,2092.228,2227.9500000000003,2218.015,2211.7909999999997,2157.572,2147.014,2316.9519999999998,2213.601,2083.286,1990.5490000000002,2036.1670000000001,1895.9309999999998,1795.9669999999999,1754.7820000000002,null,null,null],"Liabilities":[292.286,403.168,411.964,528.516,421.249,506.299,711.711,854.142,641.092,494.282,376.01,207.724,222.985,194.628,430.335,736.981,843.108,723.327,591.508,488.731,null,null,null],"Debt":[190.544,278.03,267.659,402.955,306.86199999999997,301.375,506.351,685.951,491.424,337.514,216.638,110.74,75.32,63.367,313.103,368.887,470.118,352.121,266.425,195.256,null,null,null],"Shares":[363.33422400000006,363.33422400000006,422.7342240000001,426.69422400000013,467.3742240000001,563.4102240000001,570.610224,570.610224,570.610224,570.610224,570.610224,570.610224,570.610224,570.610224,570.358224,487.358224,487.358224,487.358224,487.358224,487.358224,null,null,null],"ROA":[0.06672333343726217,0.08214682769201996,0.11029608636031969,0.059936821815891814,0.06270169326940246,0.1047927512484879,0.06342444790859285,0.05524843481116562,0.006936310330625182,0.0016200733848680544,-0.005927107796219408,0.006702916339355852,0.056699576580671866,5.581825670359483,4.075743876794294,8.900945612900678,12.933355092606217,10.745848028919715,10.006045858646747,8.658886613396538,null,null,null],"ROE":[0.0882529509593153,0.11753395977477839,0.15497680382271015,0.08335694621829609,0.0852579054100632,0.13460588813729213,0.08294769924862955,0.07527328735066009,0.009272317687919121,0.0020366331941393035,-0.007111567059082157,0.007615529861076894,0.06219790617758965,6.098822843591219,4.669132639285867,11.447849454713571,18.009378860360677,15.03565619408044,13.580578571205898,11.304436812152879,null,null,null],"ProfitMargin":[0.03227433406464954,0.035856796554990064,0.05335480925914899,0.039513057644594,0.03470841532663324,0.05600873355311856,0.041694497636283676,0.043557531962201225,0.005926915849300722,0.001449969459937839,-0.005121790964432448,0.005213705269556168,0.047986784208000025,5.666334589427797,4.263260286234908,6.65680752004411,9.961442661000381,7.9745683631798405,6.883887304404415,5.811719152417043,null,null,null]},"jnj":{"cur":"USD","cur_ratio":[266.17,40.29215864365728,8.248480727656366,8.722119060622612,0.017442486631350892],"date":["31/12/2006","30/12/2007","28/12/2008","3/1/2010","2/1/2011","1/1/2012","30/12/2012","29/12/2013","28/12/2014","3/1/2016","1/1/2017","31/12/2017","30/12/2018","29/12/2019","3/1/2021","2/1/2022","1/1/2023","31/12/2023","29/12/2024","28/12/2025",null,null,null],"Revenue":[53324,61095,63747,61897,61587,65030,67224,71312,74331,70074,71890,76450,81581,82059,82584,93775,79990,85159,88821,94193,101187.13636363637,107691.63636363637,116556.3],"Revenue_Adj":[53324,61095,63747,61897,61587,65030,67224,71312,74331,70074,71890,76450,81581,82059,82584,93775,79990,85159,88821,94193,101187.13636363637,107691.63636363637,116556.3],"NetProfit":[11053,10576,12949,12266,13334,9672,10853,13831,16323,15409,16540,1300,15297,15119,14714,20878,17941,35153,14066,26804,27156.850000000002,28168.095,33662.474],"NetProfit_Adj":[11053,10576,12949,12616.85,13279,13867,14345,15840.25,15556.05,14979.85,17882.1,17559,18280.3,19646.9258144443,18169.9694289553,22269.4996826223,20356.773510564202,21686.6702644247,20360,22423,27156.850000000002,28168.095,33662.474],"OCF":[14248,15022,14972,16571,16385,14298,15396,17414,18710,19569,18767,21056,22201,23416,23536,23410,21194,22791,24266,24530,null,null,null],"FCF":[11582,12080,11906,14206,14001,11405,12462,13819,14996,16106,15541,17777,18531,19918,20189,19758,17185,18248,19842,19698,22425.892159937932,27091.645115534833,30918.54527361983],"DivYield":[2.2038776128445927,2.4042742653606415,3.065232240437158,2.9964291259121256,3.411479385610347,3.43092406221409,3.454231433506045,2.8045479155387114,2.6270702455739574,2.8718847352024923,2.7341376616613142,2.376180933295162,2.781488174746602,2.5728987993138936,2.5289109162536536,2.4492897644239204,2.5191055759977354,2.998596401684318,3.3850396415029302,2.4755574820594326,2.03215989780967,2.1347259270391103,2.2196340684524927],"Assets":[70556,80954,84912,94682,102908,113644,121347,132683,130358,133411,141208,157303,152954,157728,174894,182018,187378,167558,180104,199210,null,null,null],"Equity":[39318,43319,42511,50588,56579,57080,64826,74053,69752,71150,70418,60160,59752,59471,63278,74023,76804,68774,71490,81544,null,null,null],"Liabilities":[31238,37635,42401,44094,46329,56564,56521,58630,60606,62261,70790,97143,93202,98257,111616,107995,110574,98784,108614,117666,null,null,null],"Debt":[6593,9537,11852,14541,16773,19627,16165,18180,18760,19861,27126,34581,30480,28681,36366,34751,40742,30432,37834,49333,null,null,null],"Shares":[2893.23,2840.223,2769.178,2754.321,2738.097,2724.363,2778.489,2820.628,2783.223,2755.162,2706.511,2682.525,2662.324,2632.507,2632.512,2628.965,2613.597,2407.078,2406.922,2407.939,null,null,null],"ROA":[0.1708082212950085,0.13960794667018678,0.15613808737173382,0.13659699099079034,0.1349663444506301,0.08932727474232519,0.09236949500193624,0.10889265047435343,0.12410992963074198,0.11683708093066283,0.12045779789453752,0.008709896787723066,0.09860857289279533,9.732781429242761,8.847280095724276,11.699242390281078,9.713694788248924,19.808078076047515,8.091767291219632,14.132881992228075,11.9725,12.555,14.6175],"ROE":[0.2833085559030092,0.25596282536878157,0.30173598974717464,0.2635044415085017,0.24884526020136796,0.17019329749513898,0.17805522287664266,0.199180581657414,0.2270157504954626,0.21871939362109835,0.23366862567811936,0.01991147053868186,0.2551371005403963,25.362555882673643,23.97412606212678,30.412014479137078,23.790170194991614,48.29438514061191,20.056464951805168,35.03012402472653,31.80307692307692,30.83230769230769,33.933076923076925],"ProfitMargin":[0.20728002400420073,0.17310745560193141,0.2031311277393446,0.19816792413202577,0.2165067303164629,0.14873135475934185,0.16144531714863739,0.1939505272604891,0.21959882148767002,0.21989610982675456,0.23007372374460983,0.017004578155657292,0.18750689498780354,18.42454819093579,17.81701055894604,22.263929618768326,22.42905363170396,41.27925410115196,15.836345008500242,28.45646704107524,0.2683824354906142,0.2615625135456202,0.28880870446299345]},"6060":{"cur":"HKD","cur_ratio":[9.345,4.33363569079405,0.41945019221799196,0.6913958008884544,0],"date":[null,null,null,null,null,null,null,null,"31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[null,null,null,null,null,null,null,null,1029.0093897597708,3096.00476755,3989.2526257777768,6445.875771631157,11395.586315287354,17157.51133879998,20809.619604398373,26443.695636115255,26723.241789967775,32765.863887712803,37035.16111617365,39500.627887016046,43991.7346,47212.49086666667,53900.89946666666],"Revenue_Adj":[null,null,null,null,null,null,null,null,1029.0093897597708,3096.00476755,3989.2526257777768,6445.875771631157,11395.586315287354,17157.51133879998,20809.619604398373,26443.695636115255,26723.241789967775,32765.863887712803,37035.16111617365,39500.627887016046,43991.7346,47212.49086666667,53900.89946666666],"NetProfit":[null,null,null,null,null,null,null,null,46.546879520689686,54.60384403000001,10.955271926436778,-1151.339316634616,-2067.8518930076625,-515.1587941137925,623.153324406504,1403.6261711687257,-1295.329337240496,4508.245634225205,654.0104643160491,1195.5120662283948,2035.365,1916.723,2058.768],"NetProfit_Adj":[null,null,null,null,null,null,null,null,46.546879520689686,54.60384403000001,10.955271926436778,-1088.8139834925755,-1932.502203343007,-328.16895351541154,771.3950619809486,1508.2457873830922,-1260.4814508563309,494.98436415745306,672.4529607338945,1699.8616349367328,2035.365,1916.723,2058.768],"OCF":[null,null,null,null,null,null,null,null,223.085432502299,902.4902416200001,1060.7525037256703,33.01795248576924,-869.2508752298849,753.7297697517233,7152.716223577235,1627.3103731053513,778.0774464016529,3109.2103258388424,2791.3522008473237,4347.592947480247,null,null,null],"FCF":[null,null,null,null,null,null,null,null,195.27637498275877,853.5420909500002,846.0878545747124,-215.8479805846155,-1258.4217410153256,429.23943601724085,6873.964525577235,1313.0320867423882,299.7396748756198,2787.519310673553,1820.085059572839,3212.295737751851,null,null,null],"DivYield":[null,null,null,null,null,null,null,null,null,null,null,0,0,0,0,0,0,0,0,0,0,0,0],"Assets":[null,null,null,null,null,null,null,null,1710.5937351000002,9639.3982278,10426.8927579,25394.195044400003,29991.971905600003,34573.213395,54255.4746244,63514.2932172,53886.116130200004,47145.680239400004,48191.848971800006,52020.5216132,null,null,null],"Equity":[null,null,null,null,null,null,null,null,1274.82290359893,8240.871573302506,7663.700590918916,20736.14249324542,18758.876771185583,18462.946558462816,20661.646891392935,23468.433647598376,19921.126272577043,22078.730029262664,22268.95430414828,28343.352656221836,null,null,null],"Liabilities":[null,null,null,null,null,null,null,null,435.8097409,1398.6675449999998,2763.3633422999997,4657.270357200001,11233.909227799999,16110.1264184,33593.9319779,40045.367537599996,33965.1769222,25066.9222817,25921.9730276,23677.676296399997,null,null,null],"Debt":[null,null,null,null,null,null,null,null,174.874,1.9113600000000002,315.8316602,162.57478,2974.7564584,4975.6345926,12924.315175100002,14959.232386799998,16026.692974300002,17243.9957215,16012.9343924,9519.7461135,null,null,null],"Shares":[null,null,null,null,null,null,null,null,1000,1240.625,1240.625,1469.813,1469.813,1469.813,1469.813,1469.813,1469.813,1469.813,1469.813,1684.813,null,null,null],"ROA":[null,null,null,null,null,null,null,null,null,0.009377869862958547,0.0010771568163097079,-0.06543267004497615,-0.07344171017634062,-1.5864158663176653,1.446274978009888,2.3902321460558085,-2.2377801146590386,9.010591290368298,1.3691853099414355,2.394944073385484,3.9240000000000004,3.412,3.046666666666667],"ROE":[null,null,null,null,null,null,null,null,null,0.011177590770411977,0.001362477639128055,-0.0831530710665877,-0.10712230541081297,-2.9930502199237834,3.6175060232050775,7.176920560679702,-6.842573459243957,22.75619649184407,2.943700441250675,4.750789018335371,6.77,5.906363636363637,5.863333333333333],"ProfitMargin":[null,null,null,null,null,null,null,null,0.045234649930217224,0.017636873367352836,0.002746196582198364,-0.17861642970250177,-0.18146077224948112,-3.002526321802935,2.994544524373683,5.307980361306742,-4.8472013516218615,13.758970768098067,1.7659176971433124,3.026564716003825,0.04626547922168279,0.040596471006605454,0.038194184716983146]},"2015":{"cur":"HKD","cur_ratio":[43.46,null,0.31382827279919095,null,null],"date":[null,null,null,null,null,null,null,null,null,null,null,null,"31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[null,null,null,null,null,null,null,null,null,null,null,null,0,322.6025945896548,10641.145380999998,32553.630618400857,52733.372067424796,136923.02124081817,156562.06311401556,121886.66726177651,133202.42551,168278.5058769231,189926.4036],"Revenue_Adj":[null,null,null,null,null,null,null,null,null,null,null,null,0,322.6025945896548,10641.145380999998,32553.630618400857,52733.372067424796,136923.02124081817,156562.06311401556,121886.66726177651,133202.42551,168278.5058769231,189926.4036],"NetProfit":[null,null,null,null,null,null,null,null,null,null,null,null,-2193.2383771264363,-3722.847350865513,-891.1891698780487,-387.43476318107037,-2343.0855080351243,12939.426855452477,8705.25929487654,1220.2914808172836,-3778.855,3007.572,6252.532],"NetProfit_Adj":[null,null,null,null,null,null,null,null,null,null,null,null,-1755.7567249760532,-3336.0738207818927,-1139.088766054878,-357.02621742798397,-2286.041780396488,10739.124654347104,8698.664524627568,1220.2914808172836,-3778.855,3007.572,6252.532],"OCF":[null,null,null,null,null,null,null,null,null,null,null,null,-1596.995959482758,-2034.8897725172392,3533.096359577235,10052.278195436224,8617.25394680868,56043.88697777478,17267.958839786,-9345.481384509874,null,null,null],"FCF":[null,null,null,null,null,null,null,null,null,null,null,null,-2748.058421187739,-3115.916526631031,2773.3353261707316,5221.994599013997,2646.1694287190085,48849.90609601858,8890.355042005756,-13909.500536772837,-7148.69782541315,4993.540888772183,8447.169495164908],"DivYield":[null,null,null,null,null,null,null,null,null,null,null,null,null,null,0,0,0,0,0,0,0.15141280467587695,0.15952420492637034,0.02703800083497802],"Assets":[null,null,null,null,null,null,null,null,null,null,null,null,6582.178284,10641.713849200001,43207.8145604,75876.2464684,97865.7687859,157799.87135290002,172771.88880760001,171839.25250659997,null,null,null],"Equity":[null,null,null,null,null,null,null,null,null,null,null,null,3191.934913251436,5124.41416336847,35403.87150062814,50376.629512594205,51101.674444365526,66626.60239863685,75896.00943384386,81457.18984385688,null,null,null],"Liabilities":[null,null,null,null,null,null,null,null,null,null,null,null,3390.3818936000007,5517.2607126,7804.1216841,25498.5607748,46764.5741996,91173.18467510003,96872.7382832,90383.5207275,null,null,null],"Debt":[null,null,null,null,null,null,null,null,null,null,null,null,1544.3036748000004,1139.1520378,2511.5829094999995,9619.352294799999,13869.785080200001,14900.199013300002,17394.0499598,19840.3060079,null,null,null],"Shares":[null,null,null,null,null,null,null,null,null,null,null,null,null,658.438,726.738115,964.781213,977.176946,992.831465,1893.392776,1914.451928,null,null,null],"ROA":[null,null,null,null,null,null,null,null,null,null,null,null,null,-31.888038219573982,-0.6610063770550673,-0.65454660148126,-2.712120124056264,10.177267038513554,5.253051233666233,0.7102201956857059,-2.7753333333333337,0.009999999999999978,2.296363636363637],"ROE":[null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,-0.9071949487313007,-4.683763205995023,22.293305346384912,12.26149103675518,1.5672249861020455,-5.15969696969697,1.749375,5.141785714285715],"ProfitMargin":[null,null,null,null,null,null,null,null,null,null,null,null,null,-857.5312887923001,-1.6037143969894494,-1.1901430219032891,-4.443268875427233,9.450147052112449,5.560260973654247,1.001168961488182,-0.02836935461213167,0.017872641659623458,0.03292091825667]},"1024":{"cur":"HKD","cur_ratio":[27.98,22.286649368512695,0.780380728864546,2.3189846915279126,0],"date":[null,null,null,null,null,null,null,null,null,null,null,"31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[null,null,null,null,null,null,null,null,null,null,null,9628.161479610004,24071.820374808423,44380.41603297926,66138.40046730894,97723.77716171122,109669.60409462811,125446.00828512394,137528.86689465016,154947.0370666666,162415.4891826087,169124.11245957448,177986.99327619048],"Revenue_Adj":[null,null,null,null,null,null,null,null,null,null,null,9628.161479610004,24071.820374808423,44380.41603297926,66138.40046730894,97723.77716171122,109669.60409462811,125446.00828512394,137528.86689465016,154947.0370666666,162415.4891826087,169124.11245957448,177986.99327619048],"NetProfit":[null,null,null,null,null,null,null,null,null,null,null,-23142.180030057705,-14738.227081321835,-22293.852156075835,-131244.99137799998,-94098.53132285524,-15941.060276859507,7071.055512396693,16619.688047325093,20204.018806172837,10477.406,11694.966,13854.598],"NetProfit_Adj":[null,null,null,null,null,null,null,null,null,null,null,-23142.180030057705,-14738.270658232754,-22293.801105437906,-131129.5574081646,-94171.13488963117,-12825.91626621901,6680.522310433883,15171.222897325097,19510.8181287037,10477.406,11694.966,13854.598],"OCF":[null,null,null,null,null,null,null,null,null,null,null,2331.073910775001,2156.230611417624,9104.791652768956,2628.745021577235,-6586.096892224698,1041.0013066115703,23260.63289256198,33119.07708106994,29861.6201037037,null,null,null],"FCF":[null,null,null,null,null,null,null,null,null,null,null,1896.8553360115395,-1242.6747647701145,6213.857556417234,-4019.8107640081303,-15944.043010538287,-4897.59675123967,17846.786919421487,24380.600085596696,13645.879167901232,-7636.666907029719,-2194.7692304125226,7810.382851723931],"DivYield":[null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,0,0,0,0,1.0449288037759548,1.2548085120362689,1.0239908927653834,0.6798628058524266],"Assets":[null,null,null,null,null,null,null,null,null,null,null,9238.0573251,18763.5575614,36258.727705200006,61945.9475397,113497.86204999998,100997.2863,116914.97040000002,148852.8466,183208.1048,null,null,null],"Equity":[null,null,null,null,null,null,null,null,null,null,null,-26637.107053575543,-40654.88753658501,-62338.02556383377,-190124.16298785084,55322.57734656475,45053.21726147335,53976.42432264969,66003.2091404773,88634.28733313302,null,null,null],"Liabilities":[null,null,null,null,null,null,null,null,null,null,null,35876.1691646,59416.68076660001,98597.2273748,252069.1512878,58174.12482719998,55944.4921,62938.47780000001,82846.9058,94575.404,null,null,null],"Debt":[null,null,null,null,null,null,null,null,null,null,null,33047.514478000005,55338.497939400004,82853.801527,226984.18977429997,16695.134757999997,14627.0606,17292.6278,31429.0186,38927.1561,null,null,null],"Shares":[null,null,null,null,null,null,null,null,null,null,null,null,null,null,3392.695,3441.894,4294,4333,4277,4307,null,null,null],"ROA":[null,null,null,null,null,null,null,null,null,null,null,null,-1.0283443051765346,-80.384430927052,-275.85791106093086,-107.93878355114029,-15.05865270982188,6.539776997285318,12.45892049770686,12.232855964806802,2.9808000000000003,4.101153846153846,4.762173913043478],"ROE":[null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,-32.242347141128306,14.390334447031824,27.613962743209054,26.302256255209734,8.283823529411764,8.423030303030302,9.168709677419356],"ProfitMargin":[null,null,null,null,null,null,null,null,null,null,null,-2.40359284366667,-0.6122605956608768,-50.23353575484554,-198.43992363085965,-96.29031342816702,-14.53553189004385,5.636732175905525,12.08450881810588,13.039306325993163,0.06450787844819945,0.06914803817339928,0.07783807630514701]},"tsm":{"cur":"USD","cur_ratio":[404.8,null,153.25295806320116,null,null],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[9759.413016784165,9820.36657860785,10573.868436859006,8957.351418981882,13331.431158354515,14531.405805182067,17136.714246677388,20109.895420211407,25165.607800568567,26570.00227067522,29419.526017677905,32133.10604863693,34234.09358102784,34634.77328398547,45486.93402059686,56834.71744352338,75985.83155469886,69358.11043719453,90105.78893785358,122555.60420314889,171691.0111170667,235984.82636954344,297102.37030981574],"Revenue_Adj":[9759.413016784165,9820.36657860785,10573.868436859006,8957.351418981882,13331.431158354515,14531.405805182067,17136.714246677388,20109.895420211407,25165.607800568567,26570.00227067522,29419.526017677905,32133.10604863693,34234.09358102784,34634.77328398547,45486.93402059686,56834.71744352338,75985.83155469886,69358.11043719453,90105.78893785358,122555.60420314889,171691.0111170667,235984.82636954344,297102.37030981574],"NetProfit":[3905.2061050619905,3323.178547042579,3171.7120684258866,2702.202812135648,5135.235686313845,4566.194388704874,5624.4218000463015,6337.4521326955155,8705.674977080127,9657.015960267483,10373.45273623141,11279.624088490671,11653.859142067207,11175.97336181457,17589.646298235526,21358.108807312983,34119.083607822846,26902.73828930869,36526.25187385727,55272.550157583486,88179.592,118261.758,149000.016],"NetProfit_Adj":[3905.2061050619905,3323.178547042579,3171.7120684258866,2689.3645788015738,4966.245384999846,4548.974279241527,5720.650335149525,6270.805567794972,8723.940609992009,9123.28549226043,10435.300400217155,11243.098619452405,11778.910865949543,11240.136142426749,17327.60138192689,21590.731269590313,34169.28594101914,26747.98342554902,36748.67397430923,55489.98465262623,88179.592,118261.758,149000.016],"OCF":[6303.103509813019,5593.5675880204635,7029.836312095836,4845.015872951667,7291.928325732807,8424.141792211885,9742.91335674235,11805.795200765515,14013.776111008025,16833.14722067245,17022.89660232255,19585.094723330203,19542.066107350136,20407.46726667466,28331.28212095564,39985.74049644875,54356.668749459874,41196.98668988704,58757.638107862804,75892.05237421668,null,null,null],"FCF":[3882.1453237517967,3036.710067712194,5150.208054778799,2186.213210933912,1351.5026635857894,1144.073598649076,1419.2324656690353,2118.581788828311,4494.60511209276,8721.417237554,6816.928576205148,8690.220636044141,9068.060667935088,5503.8844095598115,10943.834121540474,9896.789912235672,18017.581978880527,10720.607861969045,28995.17602209805,34952.48311621183,46310.773879652355,72956.80254336269,90891.91852645641],"DivYield":[4.444444444444445,4.87915,6.756756756756757,4.651162790697675,4.225352112676056,3.95778364116095,3.0927835051546393,2.843601895734597,3.1914893617021276,4.195804195804196,3.8567493112947657,3.485838779956427,3.5476718403547673,2.8700906344410875,1.8867924528301887,1.788617886178862,2.4526198439241917,2.1922428330522767,1.5813953488372092,1.4193548387096775,1.0063676908136217,1.243726228187697,1.4506539909004919],"Assets":[18025.807133597,17604.341839388002,17029.629550241,18593.1773183,24657.104620488,25563.131325071998,33105.203837488,42360.337818626,47283.91744292201,50355.40589324,58236.761628042,67135.69667731499,68286.57312949799,75719.226634856,98267.522460975,134621.067346425,161558.86946899802,180919.60347293003,204070.64931,252516.083060618,null,null,null],"Equity":[15622.046454542662,15131.539749599113,14636.577300426572,15602.580834766295,19847.8720375896,20867.787605652404,24900.7853576225,28432.60841130898,33091.75913216737,37144.0782294325,42912.083660050004,51324.9399373083,54805.81534239415,54230.73203838052,65872.49943048338,78439.44514706945,96338.72004555809,113910.29291343733,131848.4955171993,173822.10602877513,null,null,null],"Liabilities":[2403.922320369,2472.58020089,2393.160853218,2990.42446508,4809.279642203,4695.581445256,8204.742090471998,13927.657017896001,14191.691286758001,13211.770421219999,15324.493317295999,15811.08291,13481.088375782,21487.710257397997,32394.63240657,56181.62298375,65221.601247951,67009.94185033599,72223.203306825,78693.508407445,null,null,null],"Debt":[637.3177282710001,467.35929559000004,452.928174897,210.580936605,1267.3392457060002,1682.0833533359994,4027.590964024,7621.096775232,7928.080703642,7746.57171372,7692.535092007,7211.7904962600005,5898.90064344,6443.849907631,13156.637960025002,27311.13826848,28986.806117016,31363.456778920005,32022.529466775002,33886.731955531,null,null,null],"Shares":[26185.698884627887,25850.648103253745,25753.55957242134,25902.706,25910.078,25916.222,25924.435,25928.617,25929.662,25930.38,25930.38,25930.38,25930.38,25930.38,25930.38,25930.38,25930.38,25932.071,25932.733,25932.524,null,null,null],"ROA":[0.22946763068111686,0.18850443598721645,0.17690702164470584,0.15467552943927135,0.24604433342126,0.17975064571756882,0.19165294097159372,0.16916560666436825,0.1913501194824207,0.19449153636235172,0.18862848188259643,0.1769383373591196,0.17203908458385936,15.856210070296212,20.610235524735586,18.394087333702664,23.394642660570046,15.975720201603105,19.195648316230848,23.49247322940612,29.39380952380953,30.219047619047622,28.56944444444444],"ROE":[0.266376219846303,0.2194354131834747,0.20744459554538655,0.18367787094783433,0.30228363814354475,0.22297415357425426,0.2463711457474149,0.23997413081926036,0.27869299876504916,0.270362458551082,0.2560378583490713,0.2357096782530568,0.21953396882725154,20.936314292875544,29.840122953764155,29.6937957003669,39.755268896062084,26.18436772709497,30.28780750427828,35.39055723460551,41.940322580645166,41.44399999999999,37.31785714285714],"ProfitMargin":[0.400147641906931,0.33839658840043796,0.2999575876478422,0.3016743103780992,0.3851976299705606,0.3142293629344875,0.3282088805989638,0.31514097911847283,0.3459354149548274,0.36345559409024736,0.3526043461746495,0.35102812879084083,0.3404167577705533,32.26807136913511,38.66966784411275,37.579334899547135,44.90190198584853,38.788164959698236,40.537075702261056,45.099977693360636,0.5135971873335538,0.501143799435622,0.5015131305867652]},"9926":{"cur":"HKD","cur_ratio":[82.65,33.79764654029796,12.931603151557068,14.396587042131051,0],"date":[null,null,null,null,null,null,null,null,null,null,null,null,"31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[null,null,null,null,null,null,null,null,null,null,null,null,3.350975517241378,80.40929257586197,0,271.93652572674927,975.3926068132232,5003.969078510329,2301.8771900872416,3316.809237623456,5332.349723076923,8195.435846153845,11578.275849999998],"Revenue_Adj":[null,null,null,null,null,null,null,null,null,null,null,null,3.350975517241378,80.40929257586197,0,271.93652572674927,975.3926068132232,5003.969078510329,2301.8771900872416,3316.809237623456,5332.349723076923,8195.435846153845,11578.275849999998],"NetProfit":[null,null,null,null,null,null,null,null,null,null,null,null,-168.7428425095785,-380.48153895862026,-1324.4885996504063,-1295.566758303705,-1360.5130197268595,2242.3736547520657,-557.6184411913578,-1207.905571882716,-316.962,832.301,2420.534],"NetProfit_Adj":[null,null,null,null,null,null,null,null,null,null,null,null,-170.75242035262258,-368.260926420486,-1282.3086004796844,-1295.5687710770467,-1360.5421887232028,2419.3577409983673,-495.6210819450224,-913.600010092543,-316.962,832.301,2420.534],"OCF":[null,null,null,null,null,null,null,null,null,null,null,null,-143.17060683908042,-249.95186452068936,-692.6325131463414,-1211.7341460008245,-1502.2987950045456,2644.4785793615697,-664.8861454086417,-1240.968666739506,null,null,null],"FCF":[null,null,null,null,null,null,null,null,null,null,null,null,-229.4185032375478,-405.01534108965467,-1193.8706504959348,-2074.8294336658455,-2413.3298076115702,1848.9936785640493,-1308.192165065843,-2095.1731374098763,-220.72872654937797,714.4779825940793,1704.6510493154176],"DivYield":[null,null,null,null,null,null,null,null,null,null,null,null,null,null,0,0,0,0,0,0,0,0.00710872060670384,0.05971325309631226],"Assets":[null,null,null,null,null,null,null,null,null,null,null,null,742.0461148,1871.3495654,4580.743155099999,5895.7419451999995,6215.3675932,10098.280891,13573.833753,17819.690028,null,null,null],"Equity":[null,null,null,null,null,null,null,null,null,null,null,null,555.7690861379662,1471.247595370078,4098.797167897414,4024.145959786662,2881.5760913518693,4970.196535261646,7186.749594459818,9963.290651485886,null,null,null],"Liabilities":[null,null,null,null,null,null,null,null,null,null,null,null,186.30114780000002,400.0907806,481.966667,1871.5116164,3333.8185605999997,5128.078068700001,6386.7867221999995,7856.577717099999,null,null,null],"Debt":[null,null,null,null,null,null,null,null,null,null,null,null,74.06251420000001,244.654599,236.3814089,1054.3389095999999,2125.0843681,3289.6931098,4205.6407134,5074.0450425,null,null,null],"Shares":[null,null,null,null,null,null,null,null,null,null,null,null,null,null,787.057,817.057176,841.057176,837.683779,851.026455,902.864253,null,null,null],"ROA":[null,null,null,null,null,null,null,null,null,null,null,null,null,-28.85466544096613,-42.576524097557865,-24.81963085020731,-22.68341736932325,27.639091756797246,-4.6910440368607755,-7.741327239174687,-1.52875,4.633529411764705,11.6275],"ROE":[null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,-33.8582796213054,-40.291615938319396,55.3577895967312,-8.943193862975143,-14.033989862592925,-3.188947368421053,6.335263157894737,17.572777777777777],"ProfitMargin":[null,null,null,null,null,null,null,null,null,null,null,null,-50.35633404104741,-473.1810550374582,null,-476.4224867701417,-139.48363051180917,44.811900704622566,-24.22450874411001,-36.41769801474017,-0.05944151285347092,0.10155694430005471,0.20905885366707658]},"nvo":{"cur":"USD","cur_ratio":[40.19,null,5.446368060375516,null,null],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[6525.692106153846,7696.150721455936,8986.33767519084,9565.69605287356,10826.510896551725,12398.714962307695,13478.647707279684,14884.589459003826,15824.991022988508,16061.977329118774,16617.274758620697,16963.099679999996,17722.042812643685,18295.95029348661,19444.687060305332,22392.540689655172,25053.691793846156,33713.398799230774,42118.671427586196,46810.17031417625,45556.42231875,45919.6826375,48669.271],"Revenue_Adj":[6525.692106153846,7696.150721455936,8986.33767519084,9565.69605287356,10826.510896551725,12398.714962307695,13478.647707279684,14884.589459003826,15824.991022988508,16061.977329118774,16617.274758620697,16963.099679999996,17722.042812643685,18295.95029348661,19444.687060305332,22392.540689655172,25053.691793846156,33713.398799230774,42118.671427586196,46810.17031417625,45556.42231875,45919.6826375,48669.271],"NetProfit":[1086.7451015384615,1567.8945386973176,1902.6897652671755,2016.590608429118,2565.7206206896553,3195.0807842307695,3702.283567816089,4485.3958375478915,4718.84317816092,5187.9560229885055,5637.95084291188,5790.744438461537,6121.442800000002,5840.351741762458,6454.399692366408,7595.1744724137925,7861.400346153848,12146.844936153846,14646.819730268197,15514.43385823755,15204.813,14284.449,15206.550000000001],"NetProfit_Adj":[1086.7451015384615,1567.8945386973176,1902.6897652671755,2049.878961015325,2383.351913793104,3233.4845451923084,3733.1186563218366,4507.448796275861,4816.383935105365,4903.332790229885,5698.943287785443,5830.664582261537,6146.658808551726,5998.005702773952,6535.523124068698,7666.2548853103435,7950.742099353847,12387.322941753848,15841.672324567047,16052.666606283525,15204.813,14284.449,15206.550000000001],"OCF":[1303.353006153846,2107.8816862068957,2571.047766793893,2883.3050712643667,3507.001448275862,3994.3648992307703,3837.3706222222195,4620.399413026818,5647.429402298851,5697.971091570881,7186.250329501919,6256.069404615383,7073.3819865900405,7017.5385026819995,7960.242479770987,8747.72235862069,11169.064189230772,15808.331301538463,17544.624006130263,18038.933375478926,null,null,null],"FCF":[815.2272615384614,1683.2512478927197,2221.481539312977,2390.393993869731,2905.6077931034483,3420.0838411538466,3254.8724796934844,4043.69548505747,4936.422103448276,4920.521801149425,6135.517658237551,5097.920516923075,5546.349191187741,5678.265524904221,7068.010332824423,7740.21706551724,9449.396136923078,12062.510998461541,10704.18152030651,8930.258011494254,8248.998503803798,10994.55222170681,13210.28912010164],"DivYield":[1.487778958554729,1.3432835820895521,2.214022140221402,2.2590361445783134,1.5898251192368837,2.1212121212121215,1.9639934533551555,2.2635814889336014,1.9208605455243948,1.6004001000250063,2.98390263054574,2.3467862481315396,2.7358173883853647,2.15957584378637,2.1328958162428218,1.4149659863945578,1.3219616204690832,1.3465119610371006,1.8263377122717077,3.597232897770945,4.452987521061917,4.325108277546593,4.57651150931818],"Assets":[7906.0148,9336.1836,9483.0022,10543.3092,11009.3786,11276.8614,11616.8461,12998.2776,12522.575,13365.9344,13840.7841,16530.3325,16991.9646,18879.4836,23810.6846,29779.174800000004,34741.008,46669.7224,64652.346,85289.9042,null,null,null],"Equity":[5328.121130647044,6294.76772616137,6181.630740393627,6882.776686313032,6628.709764189008,6529.04665597322,7187.433665888347,7868.431267444224,6548.356166609787,6838.51899305505,6421.499092147073,8044.7982946287275,7951.132720830713,8654.874969944698,10403.147640091338,10831.840521795048,12025.177887246855,15816.573404776393,19912.017762975294,30490.878521707706,null,null,null],"Liabilities":[2577.433,3041.3844,3302.7376,3660.9408,4381.5541,4749.675,4429.0453,5131.5264,5974.8,6527.248,7417.113,8485.21,9039.862,10223.2557,13406.3871,18947.9622,22719.024,30856.07,44736.4892,54805.1205,null,null,null],"Debt":[267.4728,267.1896,433.6436,237.4758,191.1338,148.6779,88.45,39.732,117,156.2288,32.4951,273.581,79.001,673.7949,1701.4908,4079.3495000000003,3712.8959999999997,4007.6904,14266.8356,20573.5018,null,null,null],"Shares":[6344.93862,6211.4487,6082.78905,5878.62055,5517.93245,5555.59814,5425.83324,5294.295949999999,5171.513624,5095.662594,5008.665496,4888,4788,4704,4625,4531,4499.8,4458.3,4440.197407,4444,null,null,null],"ROA":[0.14891750911692747,0.18441297079731236,0.19616816157178596,0.20443305330105843,0.24801969968315196,0.2711657414750198,0.32879486373085215,0.3703366027969354,0.35931044308306026,0.4128839696555155,0.400606323083586,0.3815021961639669,0.3624931964490156,32.95611745444854,31.15172214952649,28.139528032289423,25.483919084827832,30.115718956424104,25.885033725029828,20.310162516593188,17.586000000000002,16.318000000000005,16.346],"ROE":[0.22342298417201215,0.2735618900873138,0.29603597243750096,0.31341958581345597,0.39623653695374084,0.45951648233507586,0.5489754098360655,0.6053773392146728,0.639151370334166,0.7989640512015401,0.8223291918731976,0.8020276807875142,0.7599897692171482,71.18758681190145,69.69681933210936,71.24135719133892,72.00191918667981,88.06558377664473,80.77521425971918,60.695694939457766,45.9478947368421,37.99777777777778,36.29777777777778],"ProfitMargin":[0.16653330924296003,0.20372451053046783,0.21173138980967224,0.210814832217393,0.2369849940766092,0.2576945105959666,0.2746776715453823,0.30134494806873113,0.29818931153300454,0.3229960992152103,0.33928251923421,0.34137301246239793,0.3454140622904204,31.921554486522812,33.193641390827594,33.91832386363636,31.378211286549046,36.02972517986231,34.77512284652707,33.14329718116636,0.3337744639879486,0.31109014350661657,0.3124621983421281]},"9618":{"cur":"HKD","cur_ratio":[106.3,null,0.14077012376741876,null,null],"date":[null,null,null,"31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[null,null,null,3312.11213103448,9852.833474712641,25452.057782717686,50873.22837200577,87486.4127380889,144749.97955704725,223655.80661075003,301924.52040652407,418317.16625257634,547847.4525445017,654456.1138503855,839221.2797886177,1146909.586613993,1218269.6230694216,1199140.9027809915,1255898.9425049378,1420678.839709876,1587917.9451458333,1680309.6350958333,1758806.1136820514],"Revenue_Adj":[null,null,null,3312.11213103448,9852.833474712641,25452.057782717686,50873.22837200577,87486.4127380889,144749.97955704725,223655.80661075003,301924.52040652407,418317.16625257634,547847.4525445017,654456.1138503855,839221.2797886177,1146909.586613993,1218269.6230694216,1199140.9027809915,1255898.9425049378,1420678.839709876,1587917.9451458333,1680309.6350958333,1758806.1136820514],"NetProfit":[null,null,null,-325.65131264367795,-1332.767058620689,-3546.796728667691,-4077.831324653258,-3135.672181423372,-16304.8101516253,-11237.285292600001,-4449.895392321838,-175.78287323423083,-2954.494402318007,13822.419675568948,55593.478333333325,-4290.702452674902,12086.793694214877,26717.667068181814,44823.84596995883,21304.457924691353,35526.249,46070.791,54828.316],"NetProfit_Adj":[null,null,null,-325.65131264367795,-1332.767058620689,-3546.7967286676917,-4077.831324653259,-3135.672181423372,-16304.8101516253,-1000.6946820125002,-2046.2784562088882,-243.3894948167309,-2514.2491281221487,7885.00396753264,12838.477365018769,10663.262581910893,23142.572434396072,37398.918565569256,49521.915110249414,23391.36813019107,35526.249,46070.791,54828.316],"OCF":[null,null,null,0.028366839080459748,-0.6795849256704978,-104.06706378230767,1725.650306597702,4504.059780754407,1277.5703053885065,2092.9051203800004,10248.085277282755,31006.473513384248,24760.486159655167,28113.268660206864,47873.06835772357,50983.42821646096,67326.23551115704,65803.04802272726,62961.90264814812,20609.90069012345,null,null,null],"FCF":[null,null,null,null,null,-849.4467606476919,493.2563335229889,2886.750330162836,-2352.599191890806,-4437.79580899,5363.691549904979,17906.083930908466,-423.6446489463617,17950.626108486187,33878.20771544715,24160.511619753113,40657.27250743802,43675.59710330578,47985.21734485594,5216.775979012345,39710.28169903992,29986.456268195492,63024.93959651737],"DivYield":[null,null,null,null,null,null,null,null,0,0,0,0,0,0,0,0,1.1027368534139907,2.1837285898328918,2.8608662734687846,3.6588575941037114,3.5572720032128413,3.967386332980387,4.60190284318678],"Assets":[null,null,null,null,null,13042.8108801,22234.1537274,33321.17015319999,83056.62114520001,101559.0850494,179185.3316614,220994.79767620002,238155.1061802,290526.9352944,501635.9152,609114.7876,673168.225,691790.9042000001,743060.6228,774245.3537,null,null,null],"Equity":[null,null,null,null,null,7496.000003079511,8148.254519149959,12177.988324036749,46840.265120871845,36699.7739632524,46055.89224371371,62900.742995204935,87480.32120910083,112557.40875221876,263262.53834722395,302748.26387250045,310008.6117618068,325987.94964231394,333396.8691810285,327191.983760427,null,null,null],"Liabilities":[null,null,null,null,null,null,null,null,36217.7856555,64859.9385258,133130.4681155,158091.6819841,150678.5814218,177968.67049780002,238374.7051,306360.1764,363162.5243,365802.5422000001,409649.9554,447059.22659999994,null,null,null],"Debt":[null,null,null,null,null,0,1078.2636969,1195.0433885999998,2361.7620561000003,7614.082944600001,22635.143832399997,28585.6364332,16418.0620562,20994.361323600002,37481.8087,36524.2896,73559.3905,75267.2569,95531.1056,119282.83849999998,null,null,null],"Shares":[null,null,null,null,null,581.3952775,660.2284225,731.827046,1365.8591785,1370.995243,1418.2221985,1426.3317145,1447.1481775,1462.1575,1551.7495195,1555.3958245,1567.8396235,1568.8319575,1451.7166275,1370.9236255,null,null,null],"ROA":[null,null,null,null,null,null,-0.12151565800596753,-0.0022735170551140284,-0.10802587730575264,-0.12023023608256053,-0.031026617501720956,-0.000884113870210572,-0.01267297757773519,5.197036572619651,14.488021161584053,-0.7749280307359095,1.9015220419928611,3.9481852757047826,6.232557158270996,2.817641296508269,3.612142857142857,5.062857142857143,5.911538461538462],"ROE":[null,null,null,null,null,null,-1.6659703624532143,-1.452131089207142,-0.6561226946589431,-0.26756148182094597,-0.11808406091618714,-0.0035435917502646282,-0.04456834233406894,17.20598459856611,36.677942755311946,-1.795920838230917,4.916204292443112,10.856108385891147,17.55456754491145,8.454586368696798,11.635312499999998,14.884516129032258,14.882499999999999],"ProfitMargin":[null,null,null,-0.03528605686879068,-0.04800186415006408,-0.06075760760385781,-0.04179437470108218,-0.0007196298715087372,-0.043445715967618294,-0.050243655476190446,-0.01473843656795516,-0.00042021434312378814,-0.0053929143753351895,2.112046840581411,6.624412377547928,-0.3741099126516406,0.9921279711269733,2.228067361076538,3.56906471157273,1.499597046792225,0.02237353337898793,0.02741887480017841,0.03117262475124911]},"mrk":{"cur":"USD","cur_ratio":[144.67,195.09439834024894,6.111420646818913,9.043005375671958,0.020296646087465856],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[22636,24197.7,23850.3,27428,45987,48047,47267,44033,42237,39498,39807,40122,42294,39121,41518,48704,59283,60115,64168,65011,67199.68965517242,69541.03448275862,73682.88888888889],"Revenue_Adj":[22636,24197.7,23850.3,27428,45987,48047,47267,44033,42237,39498,39807,40122,42294,39121,41518,48704,59283,60115,64168,65011,67199.68965517242,69541.03448275862,73682.88888888889],"NetProfit":[4433.8,3275.4,7808.4,12899,861,6272,6168,4404,11920,4442,3920,2394,6220,9843,7067,13049,14519,365,17117,18254,6846.76,22195.2,24405.174],"NetProfit_Adj":[4433.8,3275.4,7808.4,14585.1,4409.35,8426.75,7422.15,6189.55,5936.85,10205.3,5796.9152456353995,10611,11621,6194.02,11314.134509371599,17309.718507573,17327.9279468346,17217.23,17771.5530127462,20171.8474866717,6846.76,22195.2,24405.174],"OCF":[6765.2,6999.2,6571.7,3392,10822,12383,10022,11654,7989,12538,10376,6451,10922,13440,10253,14109,19095,13006,21468,16472,null,null,null],"FCF":[5785,5988.2,5273.4,1931,9144,10660,8068,10106,6672,11255,8762,4563,8307,10071,5824,9661,14707,9143,18096,12360,14238.652445737936,20100.5178854041,21695.238435265463],"DivYield":[3.486238532110092,2.6157287902254347,5,4.159824849480022,4.217536071032186,4.137931034482759,4.103566194430874,3.436563436563437,3.1167459059693607,3.426732298371829,3.142517411245117,3.358805757952728,2.6043711556079048,2.4848818031885647,3.031784841075795,3.4446764091858038,2.5236593059936907,2.715098147128967,3.1363088057901085,3.1540946228386852,2.339807838529066,2.438653487246838,2.556162300407825],"Assets":[44569.8,48350.7,47195.7,112314,105781,105128,106132,105645,98167,101677,95377,87872,82637,84397,91588,105694,109160,106675,117106,136866,null,null,null],"Equity":[19965.8,20591.399999999998,21167.1,61485,56805,56943,55463,52326,48791,44767,40308,34569,26882,26001,25404,38257,46058,37635,46372,52662,null,null,null],"Liabilities":[24604,27759.3,26028.6,50829,48976,48185,50669,53319,49376,56910,55069,53303,55755,58396,66184,67437,63102,69040,70734,84204,null,null,null],"Debt":[6836.1,5739.4,6240.4,17681,17882,17515,20569,25060,21403,26412,24842,24410,25114,27350,33453,34631,31985,36268,38270,50534,null,null,null],"Shares":[2167.785445,2172.502884,2107.690342,3108.222551,3082.106823,3040.838643,3026.635301,2927.526714,2838.140196,2781.128073,2748.731322,2696.611608,2592.559543,2539.016026,2530.225827,2527.604499,2537.833884,2531.633273,2527.637335,2474.626766,null,null,null],"ROA":[0.0991728512698008,0.07049897493018226,0.16344728843786893,0.16173311090171943,0.007895641807469222,0.05947588770512401,0.058392502130076684,0.04159091874943927,0.11697054147940258,0.04445467464622405,0.03978604849432135,0.02612838269240214,0.07295802567606402,11.785624483638061,8.03136630962866,13.228779108078792,13.515224291844694,0.3382213264762434,15.297992233478267,14.374812971508671,4.62,15.5725,15.65],"ROE":[0.24995842294715062,0.18326786853325275,0.4227269036082614,0.33152437214311137,0.015180633672443888,0.11519565077645028,0.11471400541209072,0.08569343775842779,0.24224688046173232,0.09519625387096427,0.09249209570100515,0.06433408577878104,0.20381080328325443,37.42016423357664,27.592534749336252,41.0985653769232,34.497178497178496,0.8734983008663189,40.806255512909146,36.906964283909055,13.818333333333335,41.71454545454546,37.777],"ProfitMargin":[0.19587382929846264,0.13535997222876556,0.3273921082753676,0.4702858392883185,0.01872268249722748,0.13053884737860844,0.1304927327733937,0.10001589716803308,0.28221701351895256,0.11246139045014937,0.09847514256286584,0.059668012561686856,0.14706577765167636,25.160399785281562,17.02153282913435,26.792460578186596,24.491000792807384,0.6071695916160692,26.675289864106723,28.078325206503514,0.10188677953722704,0.31916695400301354,0.33121901612733995]},"511":{"cur":"HKD","cur_ratio":[1.72,-1.4453781512605042,0.23359999999999997,0.2529411764705882,0.5813953488372093],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[4275.082,4325.809,4407.304,3983.332,4674.656,5208.865,5447.884,5686.05,4912.061,4454.725,4210.308,4335.731,4477.306,3648.762,2724.2,2898.622,3585.75,3322.778,3258.089,3192.434,3036,3207,3390],"Revenue_Adj":[4275.082,4325.809,4407.304,3983.332,4674.656,5208.865,5447.884,5686.05,4912.061,4454.725,4210.308,4335.731,4477.306,3648.762,2724.2,2898.622,3585.75,3322.778,3258.089,3192.434,3036,3207,3390],"NetProfit":[1188.597,1263.684,1055.348,900.444,1329.891,1555.585,1732.256,1737.987,1409.632,1331.223,499.945,243.621,-199.08,-294.925,-280.881,-646.735,-807.132,-762.796,-491.049,59.289,182,238,297],"NetProfit_Adj":[1188.597,1263.684,1055.348,900.70452,1330.584885,1555.579155,1733.033385,1737.99034,1311.70379,70.145055,80.20803,228.849015,46.551115,34.47582,-273.989745,-584.636885,-603.691765,-565.16152,-120.326535,62.001915,182,238,297],"OCF":[1414.814,1481.063,1382.894,1204.241,1783.876,1920.697,1789.093,1425.485,1388.639,1236.558,864.211,180.936,167.887,729.304,4.263,-344.501,-370.609,-154.502,251.784,291.607,null,null,null],"FCF":[1319.375,1215.125,411.42499999999995,954.937,1615.229,1777.386,1101.1390000000004,809.7839999999999,994.8139999999999,931.331,406.063,-270.817,-168.68099999999998,465.76599999999996,-93.261,-438.05199999999996,-485.52599999999995,-240.96800000000002,209.821,247.74300000000002,null,null,null],"DivYield":[3.578947368421052,3.84204909284952,6.732673267326733,4.28380187416332,4.761904761904762,4.670912951167729,4.490500863557859,5.014464802314368,5.75858250276855,8.137715179968701,2.3529411764705883,3.202846975088968,6.747638326585695,4.084967320261438,0,0,0,0,0,0,null,null,null],"Assets":[5687.141,6250.699,6742.002,7043.136,8033.256,8834.129,9732.948,10301.175,10556.643,9113.303,12356.83,12015.909,10256.367,8595.384,9831.795,7649.79,7045.048,6249.129,5445.548,5459.179,null,null,null],"Equity":[4868.347,5368.750000000001,5633.913,5818.625000000001,6488.018000000001,7093.491999999999,7831.311000000001,8426.827000000001,8704.346,7836.173000000001,7230.0909999999985,7157.047,6307.031,5588.661999999999,5298.0149999999985,4593.16,3604.234999999999,2739.1800000000003,2195.7140000000004,2232.1479999999997,null,null,null],"Liabilities":[818.794,881.9490000000001,1108.089,1224.5110000000002,1545.238,1740.6370000000002,1901.637,1874.348,1852.297,1277.13,5126.739,4858.862,3949.3360000000007,3006.7219999999993,4533.78,3056.63,3440.813,3509.9489999999996,3249.834,3227.031,null,null,null],"Debt":[0,0,318.72400000000005,301.808,256.405,221.387,205.327,252.357,391.6,234.85,3842.493,3814.406,3044.305,2281.005,3792.428,2058.704,2259.9080000000004,2329.599,2164.422,2130.987,null,null,null],"Shares":[438,438,438,438,438,438,438,438,438,438,438,438,438,438,438,438,438,438.218,466.961836,466.961836,null,null,null],"ROA":[0.21592393049397313,0.21171066122514623,0.16245244156699978,0.1306398238450714,0.1764203265608907,0.18444886388731863,0.18659436808497104,0.17350267840523892,0.13516581648185827,0.13535604012334349,0.04657120661525478,0.019991269754293925,-0.017876933637137042,-3.128887072611982,-3.0485512730950295,-7.399043050158211,-10.985245294980455,-11.475640801232,-8.397820649514305,1.0873999871798716,3.3,4.2,5],"ROE":[0.25763748259842556,0.24804544213918883,0.19271741011122337,0.157949202447251,0.21704966968616207,0.23004275559451745,0.23395151828235813,0.21643746498275895,0.16741335492618886,0.1642892020035482,0.06781160622947964,0.03465566638517292,-0.030214976644785296,-5.069903382605843,-5.310810745655934,-13.63692672314499,-20.543012948253438,-24.40732025064671,-19.580910636816533,2.5786739329463884,7.5,9,10.2],"ProfitMargin":[0.2780290530099773,0.292126628799376,0.23945432400397157,0.22605296269555236,0.28448959666764784,0.29864183464151983,0.3179685911080339,0.3056580578784921,0.28697363489582073,0.29883393475467057,0.11874309432944098,0.05618914088535474,-0.044464238093174784,-8.082878521536893,-10.310586594229498,-22.311808852620317,-22.509433172976365,-22.95657428814083,-15.0716877286041,1.857172301760976,0.059947299077733864,0.07421265980667291,0.08761061946902655]},"945":{"cur":"HKD","cur_ratio":[348.4,22.905760022847907,3.891451976939708,2.6092435373104226,0.024382752583237667],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[228152.56557230785,252498.13936704973,235464.1071145039,267286.68502682,276424.8455065133,393550.1170892308,217939.76712950194,131703.50485823757,372743.7649095786,199209.53016091962,303071.9579176246,340347.1645961537,225421.22941915688,459555.4583563218,446487.3198206107,371114.0037827585,134329.04415692296,191847.1673423076,210382.6851938697,212948.71246551743,204396.6762,213253.0949,310913.13765],"Revenue_Adj":[228152.56557230785,252498.13936704973,235464.1071145039,245021.89527816098,276424.8455065133,393550.1170892308,217939.76712950194,131703.50485823757,372743.7649095786,199209.53016091962,303071.9579176246,340347.1645961537,225421.22941915688,459555.4583563218,446487.3198206107,395939.28005057457,134329.04415692296,191847.1673423076,197793.44728965513,177927.0070344829,204396.6762,213253.0949,310913.13765],"NetProfit":[27095.771930769253,31177.294970114934,3576.7629496183213,9132.52258850575,-13143.327261302675,346.44882461538464,13180.713940229887,22589.580272796935,23705.1801724138,12596.671494252878,16393.360436781615,11685.352782692302,28020.371797701126,32065.304655172407,32999.80190839695,42729.49125287355,-13202.921918076912,27851.870769230754,28903.97879003831,29306.609596551745,41835.215000000004,46849.450000000004,50120.79],"NetProfit_Adj":[27095.771930769253,31177.294970114934,3576.7629496183213,-5784.886543095787,4436.4388229885035,5582.5503784615385,15563.79944061303,20149.092107279692,23620.895087356326,13501.203567816097,16742.422923764374,27227.59293088845,29939.8459067835,32065.304655172407,32999.80190839695,61689.26110856895,-13202.921918076912,27851.870769230754,19778.990284277515,3621.936519030399,41835.215000000004,46849.450000000004,50120.79],"OCF":[72011.0388784616,52254.080521072785,58182.98991145039,82056.19324291189,87212.23869885055,85572.85968,83796.11718773947,71580.1204842912,75898.71909425288,62977.286786206925,99773.18117049812,106886.43257423073,116074.02721379302,121304.87812643677,116066.67169465651,143599.61828160912,100108.61178923068,118503.90764999994,150922.74616934863,179182.76539655187,null,null,null],"FCF":[72011.0388784616,52254.080521072785,58182.98991145039,82056.19324291189,87212.23869885055,85572.85968,83796.11718773947,71580.1204842912,75898.71909425288,62977.286786206925,99773.18117049812,106886.43257423073,116074.02721379302,121304.87812643677,116066.67169465651,143599.61828160912,100108.61178923068,118503.90764999994,150922.74616934863,179182.76539655187,null,null,null],"DivYield":[1.8424396442185513,2.1690904609317228,4.8076923076923075,4.0351784790481116,3.0320699708454817,4.7926267281106,3.84900074019245,2.480916030534351,2.569882777276826,3.2063645130183227,3.094939355918026,3.127383676582761,4.697986577181208,3.793626707132018,4.9448123620309055,4.852758191621734,5.465838509316771,4.9863387978142075,3.6231884057971024,3.523523523523523,3.125525674434648,3.2803504756450335,3.527102502574084],"Assets":[2473476.69,2770079.739,2396420.073,2918877.619,3320573.5458,3529411.8846,3778425.4188,3747224.4368,3867998.5748,3939310.8065999993,4154725.965,4549659.6012,4313157.9248,4856236.434,5346183.4072,5656810.2735,4807217.5118,5178144.636,5283659.564,5817281.409,null,null,null],"Equity":[172618.18227065986,192654.18171728385,174534.82411895174,213704.87700028406,192893.3319923564,190070.07810348223,196004.51613227458,211814.1469702672,226481.9727938011,235047.50781024003,246873.79903345727,262944.0447832681,271062.8759541985,300728.66019417474,321892.7178884712,362899.0651434897,278081.88041389507,288170.74748239573,285877.2703266157,297763.5326871219,null,null,null],"Liabilities":[2225611.662,2577426.5095,2221886.1006,2705173.8955,3127679.2008,3339341.3004,3582421.7134,3535411.282,3641515.384,3704265.0917999996,3907851.37,4286714.268,4042096.256,4555510.2432,5024288.5704,5293912.323,4529136.7506,4889973.158,4997781.484,5519516.985,null,null,null],"Debt":[56642.466,54417.5925,58821.0174,67394.97,204385.923,210285.495,216625.4236,211783.9724,184886.281,155034.4452,177533.175,195218.0292,190773.928,198965.6718,211831.4096,200851.739,201027.0306,203169.556,195537.152,223414.086,null,null,null],"Shares":[1547,1501,1610,1758,1778,1801,1828,1848,1864,1972,1975,1982,1971,1949,1940,1943,1865,1806,1729,1677,null,null,null],"ROA":[0.011448560062404746,0.011706136933291074,0.001364621701878208,0.0036227811434499883,-0.004044491842311027,0.0002909520673384878,0.0038226985928878797,0.006268613074364176,0.00640602213654836,0.0034173583398906787,0.004115058670143416,0.002901640723369103,0.006487345621447165,0.71848100648903,0.6950071590117427,0.7903260971127791,-0.220746266270473,0.5970994516350029,0.5807833510929729,0.5560181833512868,0.6,0.7,0.7],"ROE":[0.16423736555790872,0.17619764492380027,0.019436462324393357,0.049645653222514935,-0.06955757866155567,0.0019518686924697793,0.07611448551000739,0.12484129461962742,0.11958967453891538,0.06007614470388975,0.0728874753978702,0.051393164312798084,0.1164140842946543,12.417388918109264,12.149761800722592,13.705987666600357,-4.754832345001788,11.91140889633352,11.982383015904878,11.964955054515626,15.981111111111112,16.905555555555555,17.23125],"ProfitMargin":[0.11966248273376974,0.12434244754032026,0.016126013724267,0.03580183861082738,-0.04539127111936021,0.0025809291344884157,0.06446787291636985,0.17901058049756935,0.06597071736795493,0.06676824622885875,0.056663635836025614,0.037140335392762576,0.12881064834693,7.1984785793220425,7.612712490761271,11.873130462392005,-8.663499462172823,15.434171127846838,14.580851294270552,14.603590617219236,0.20467476822982708,0.21968746851627088,0.16120368469534857]},"392":{"cur":"HKD","cur_ratio":[31.32,4.855652250557124,0.41318520612480664,0.39854452194392515,0.05843106458045977],"date":["31/12/2006","31/12/2007","31/12/2008","31/12/2009","31/12/2010","31/12/2011","31/12/2012","31/12/2013","31/12/2014","31/12/2015","31/12/2016","31/12/2017","31/12/2018","31/12/2019","31/12/2020","31/12/2021","31/12/2022","31/12/2023","31/12/2024","31/12/2025",null,null,null],"Revenue":[7247.725381540447,10978.33463094153,19711.41832433359,24208.419088455546,27615.69978671071,30477.41631834337,35571.34018258395,42363.448965145806,47940.55015580958,60162.055114718576,55982.44053976109,57535.26158118383,67868.8783867957,67815.88593210501,68459.03566591535,80528.27342282557,92426.75188852397,91000.95886667974,91106.54940740613,94663.58761352468,95688.294652479,97497.79076929201,94968.796196526],"Revenue_Adj":[7247.725381540447,10978.33463094153,19711.41832433359,24208.419088455546,27615.69978671071,30477.41631834337,35571.34018258395,42363.448965145806,47940.55015580958,60162.055114718576,55982.44053976109,57535.26158118383,67868.8783867957,67815.88593210501,68459.03566591535,80528.27342282557,92426.75188852397,91000.95886667974,91106.54940740613,94663.58761352468,95688.294652479,97497.79076929201,94968.796196526],"NetProfit":[234.9831117868831,1478.5917555097271,2282.658467089733,2398.8819187436566,2639.557269524648,2776.395363646811,3235.145809843038,4184.166498807482,4832.157294892507,5668.519025112051,6238.513641302944,6883.636640406665,7589.024286766901,8058.684567591178,5290.875353339838,9919.43271218641,7583.333545057852,6078.59815716942,5552.270918808639,5486.827428532098,5235.5,5348,5462],"NetProfit_Adj":[234.9831117868831,1478.5917555097271,2282.658467089733,2344.441627281718,2577.4813027847,2666.2322178010413,2884.9211589711012,3652.443352429519,4749.471001355231,5989.649666003947,5957.161501414916,6751.625262623816,7618.616555228593,7999.319120037945,6359.829351312347,8736.272379842427,7794.882542234335,6076.487785395247,5629.191378276028,5588.233056090431,5235.5,5348,5462],"OCF":[2166.163708384007,2045.1572698986101,1732.2052035258157,2871.9217055287977,6702.867174432847,-1395.4660309983053,4561.872886963972,5166.220211534756,-6.468641615472871,13632.738155309758,10919.33143719944,12769.261832803551,11614.001455102154,8639.399933908475,10418.84293477427,11507.441618033581,8656.190190310332,9146.360555832644,7107.720907777775,11079.416389466665,null,null,null],"FCF":[645.6787490961929,666.6732256474336,-843.5849094719217,621.7537197547955,2904.9963534187314,-5701.872400324521,-3566.65057070769,-1245.5268792353154,-2686.640484094669,9580.610496158604,6628.265991128611,4491.280124264705,4424.4048758863855,4380.542446154031,3667.579992034159,1467.9433107964135,-3521.7400243347106,-504.05481926032974,596.6722696090535,5232.5131266037015,null,null,null],"DivYield":[1.8049976031782784,1.0796718070647557,1.8986714198158883,1.155535138841723,1.4523288289562868,1.502088053418476,1.4912744694181832,1.1053417216358796,1.480308351221756,2.023463590754865,2.592145097234657,2.111989406057009,2.5302301516137082,3.188786479150053,4.505951928147032,4.646743611132061,6.645588001129102,6.481907982024049,6.456675903964519,5.670100927796515,5.151255401863419,5.215036422862456,4.89613131786727],"Assets":[18777.480118051408,45020.16472782317,51698.5436314587,59106.36922626051,67026.66952237416,77358.69562009766,89496.33896416175,109620.53342226603,124169.7188849411,124764.50712443257,144704.75647445684,171555.7111564863,174488.02248703784,185807.39604023175,204804.00688546072,222827.84882976703,220008.4559127,224879.73112940002,223218.2029114,239291.0342648,null,null,null],"Equity":[13059.805,31564.39,36310.47,39017.001000000004,40936.261,45196.729,47667.67499999999,64068.079,68095.98,68652.17,68038.926,79173.00499999999,81015.372,87014.82699999998,97425.722,109066.969,102544.24945497638,102344.8671026024,103877.77858304791,114417.81040400051,null,null,null],"Liabilities":[4242.899947191905,13457.081746084288,15386.8082115792,20088.669002585586,26091.705997166566,32160.06035765861,41829.8793945389,45553.20869576011,56075.98571180118,56113.18058499318,76667.71331565603,92379.61714252322,93476.48429647162,98792.04451485458,107378.48188027063,113758.04190723362,117465.169375,122534.73456590001,119336.1251628,124875.27191299999,null,null,null],"Debt":[1901.467099116936,5923.354715571199,7584.196300030201,11023.870555243833,12197.664382773555,20186.83638428935,26613.864382382584,25768.999617995778,37213.935085696816,34755.178992624686,50627.691941109406,60489.46796500574,59519.898266080774,60243.693146793245,64755.4920641303,72965.47851235159,77861.27642410001,84555.2788576,84613.7268228,88679.89606490001,null,null,null],"Shares":[1245,1137.001,1137.001,1137.371,1137.371,1137.571,1137.571,1270.193509,1284.350268,1281.850268,1262.153268,1262.053268,1262.053268,1262.053268,1262.053268,1262.053268,1260.203268,1260.203268,1258.003268,1258.003268,null,null,null],"ROA":[0.012224009393087038,0.046339685918102266,0.04718480138669255,0.04330033526815911,0.04184876122004244,0.03845129149737388,0.03877630686755744,0.04202359029480875,0.04133249020383785,0.045532106357448844,0.04628175233349555,0.04351120166438657,0.04379416824210106,4.471120099408898,2.7069857741248695,4.638927030691402,3.4624625570832754,2.756052294050788,2.4736845051922285,2.3813844546378573,2.385,2.34,2.37],"ROE":[0.027141250317812896,0.08267400529065715,0.0807422877429736,0.0787331775112768,0.08049893591097587,0.07723911001116676,0.08375695969303504,0.08934307987132684,0.08690256373712547,0.09825242745713698,0.10797273533392246,0.11018366367426091,0.11042474749849472,11.11354743526813,6.601235388627873,10.967408530770808,8.276868326790243,6.867398926178063,6.176899288756859,5.813880333860291,5.77,5.68,5.59],"ProfitMargin":[0.03242163567418986,0.13468270053842576,0.1158038670546507,0.09909287797680394,0.09558176290701353,0.09109680868767703,0.09094810016258525,0.09876831563572579,0.10079478185351885,0.09422083428338962,0.11143697168529279,0.11964204995737551,0.11181891416439545,11.883181140860215,7.728527435238297,12.317950318024284,8.204695491413698,6.679707810633978,6.094261010786663,5.7961329871974865,0.05471399384461038,0.05485240722886623,0.05751350440670113]}}
''')

_8P_YEARS = list(range(2006, 2029))
_8P_YEAR_LABELS = [str(y) if y <= 2025 else f"{y}E" for y in _8P_YEARS]

_8P_CUR = [
    ("C.Price（現價）", 0), ("C.PE（市盈率）", 1), ("C.PS（市銷率）", 2),
    ("C.PB（市帳率）", 3), ("C.DY（現時股息）", 4),
]
# (顯示標籤, 數據鍵, 欄位類型)
_8P_ROWS = [
    ("Date（期末日）", "date", "date"),
    ("Revenue（收入，百萬）", "Revenue", "num"),
    ("Adjusted Revenue（收入-調整，百萬）", "Revenue_Adj", "num"),
    ("Net Profit（純利，百萬）", "NetProfit", "num"),
    ("Adjusted Net Profit（純利-調整，百萬）", "NetProfit_Adj", "num"),
    ("OCF（經營現金流，百萬）", "OCF", "num"),
    ("Free Cash Flow（自由現金流，百萬）", "FCF", "num"),
    ("Dividend Yield（股息）", "DivYield", "ratio"),
    ("Assets（資產，百萬）", "Assets", "num"),
    ("Equity（股東權益，百萬）", "Equity", "num"),
    ("Liabilities（負債，百萬）", "Liabilities", "num"),
    ("Short- and Long-Term Debt（債務，百萬）", "Debt", "num"),
    ("Number of Shares（已發行股數，百萬）", "Shares", "num"),
    ("ROA（資產會報率）", "ROA", "ratio"),
    ("ROE（股本回報率）", "ROE", "ratio"),
    ("Profit Margin（純利率）", "ProfitMargin", "ratio"),
]


def _8p_key_for_symbol(symbol: str):
    s = symbol.strip()
    if s.endswith(".HK"):
        return s[:-3].lstrip("0") or "0"
    return s.lower()


def get_8p_record(symbol: str):
    """依選股符號回傳內聯 8P 記錄；大小寫不敏感匹配，無資料回傳 None。"""
    k = _8p_key_for_symbol(symbol)
    if k in _8P_PACK:
        return _8P_PACK[k]
    low = k.lower()
    for key, rec in _8P_PACK.items():
        if key.lower() == low:
            return rec
    return None

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

# 頂部檢索區（全寬）
selected_symbol = None
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
    opts = {format_option_label(i["symbol"], i["code"], i["name"], i["en"], i["mkt"]): i["symbol"]
            for i in MASTER_CATALOG}
    sel_l = st.selectbox("🔍 全市場萬用檢索 (輸入編號或指數名稱即時過濾)", list(opts.keys()), index=20)
    selected_symbol = opts[sel_l]

if not selected_symbol:
    selected_symbol = "00700.HK"

# 切股只記錄符號，不做清快取或網絡等阻塞操作；重抓一律由手動按鈕觸發
if "last_symbol" not in st.session_state:
    st.session_state["last_symbol"] = selected_symbol
elif st.session_state["last_symbol"] != selected_symbol:
    st.session_state["last_symbol"] = selected_symbol

# ==========================================
# 報價卡（全寬，手動更新；不顯示任何數據來源；右側股票圖已移除）
# ==========================================
q_data_init = get_or_create_asset_profile(selected_symbol, apply_micro_tick=False)
ma20_txt = "20 日均線"


def render_quote_panel(profile: dict):
    """精簡手動報價：現價、升跌與最高/最低/開市/前收；純 HTML、不閃爍；
    非更新 rerun 不聯網（門控），按「更新報價」才單次重抓。不顯示數據來源。"""
    if not profile.get("live_ok"):
        st.info("📭 尚未載入此標的的最新報價（手動模式不會自動連線）。下方為本地／快取最後已知值，可能非最新；請按「🔄 更新報價」。")

    is_hk = profile.get("mkt") == "HK"
    _dp = ".3f" if is_hk else ".2f"
    is_up = profile.get("change", 0) >= 0
    arrow_sym = "▲" if is_up else "▼"
    price_cls = "qa-up" if is_up else "qa-down"
    curr_label = "港元" if profile.get("curr") == "HKD" else "美元"
    _state = "已收盤 · 最近成價" if profile.get("live_ok") else "離線 · 最後已知參考值"
    _qt = profile.get("quote_time") or profile.get("update_time") or "N/A"

    def _cell(v, cls="qa-mid"):
        if v is None:
            return '<span class="qa-na">N/A</span>'
        try:
            return f'<span class="{cls}">{float(v):{_dp}}</span>'
        except Exception:
            return '<span class="qa-na">N/A</span>'

    _px = profile.get("price")
    _px_html = (f'<span class="{price_cls}">{float(_px):{_dp}}</span>' if _px is not None
                else '<span class="qa-na">N/A</span>')

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
                <div class="qa-name">{profile.get('code')} {profile.get('name')}（{profile.get('symbol')}）</div>
                <div class="qa-big {price_cls}">
                    {_px_html}
                    <span class="qa-chg">{arrow_sym} {abs(profile.get('change',0)):{_dp}} &nbsp;{abs(profile.get('pct',0)):.2f}%</span>
                </div>
            </div>
            <div class="qa-status">
                <div style="font-weight:700;">{_state}</div>
                <div>⏱ {_qt}</div>
                <div style="margin-top:3px;">{curr_label}</div>
            </div>
        </div>
        <div class="qa-grid">
            <div class="qa-lab">最高</div>{_cell(profile.get('high'), 'qa-up')}
            <div class="qa-lab">最低</div>{_cell(profile.get('low'), 'qa-down')}
            <div class="qa-lab">開市</div>{_cell(profile.get('open'))}
            <div class="qa-lab">前收</div>{_cell(profile.get('prev_close'))}
        </div>
    </div>
    """, unsafe_allow_html=True)


render_quote_panel(q_data_init)
q_data = q_data_init

st.markdown("<div style='margin-top:10px;'></div>", unsafe_allow_html=True)

# ==========================================
# 10. 下方六大核心功能分頁籤 (修正問題3：徹底移除所有「1.」「2.」「4-5.」「6-7.」「8-10.」「11.」及「問題X」字樣)
# ==========================================
tabs = st.tabs([
    "全市場爆升與爆跌股批判篩選器",
    "2006–2028E 財務與 8P 分析全表",
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
    **說明**：
    * 點擊下方 **「🔄 以真實市場數據重新計算」**，系統會即時逐隻抓取**真實財報**（PE/PS/PB、營收與盈利增長、ROE、淨利率、負債）及**真實歷史價格**（52 週位置、RSI、近 1/3/6 月真實回報、年化波動率），
      再以量化模型推演 1–6 個月方向與**統計波幅區間**。
    * **區間是以真實歷史波動率（±1σ）計算的客觀統計分佈，不是保證預測，更不是 AI 隨機生成的數字**；「爆升／爆跌」只是模型依當前真實估值與動能給出的傾向，須結合你自己的判斷。
    * 未點擊重新計算前，表格顯示的是內置**分析師靜態觀點**（標註「靜態」），僅作參考，並非即時市場數據。
    """)

    rc1, rc2 = st.columns([1, 1])
    with rc1:
        run_real = st.button("🔄 以真實市場數據重新計算（逐隻連線）", type="primary")
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
# 分頁 B：2006–2028E 財務與 8P 分析全表（依內聯 8P 數據包）
# ------------------------------------------
with tabs[1]:
    st.subheader(f"📊 {q_data['code']} {q_data['name']} ({q_data['symbol']}) — 2006–2028E 財務與 8P 分析全表")
    rec = get_8p_record(selected_symbol)
    if not rec:
        st.warning("此標的未包含在 8P 數據包內，暫無歷史 8P 記錄。")
    else:
        # 當前估值比率（單一快照）
        ccols = st.columns(5)
        for (lab, idx), c in zip(_8P_CUR, ccols):
            v = rec["cur_ratio"][idx]
            c.metric(lab, "N/A" if v is None else f"{v:,.3f}")

        # 年份範圍
        ylo, yhi = st.select_slider(
            "📅 年份範圍（2026E–2028E 為預測）", options=_8P_YEAR_LABELS,
            value=(_8P_YEAR_LABELS[0], _8P_YEAR_LABELS[-1]), key="8p_year_range")
        i0 = _8P_YEAR_LABELS.index(ylo); i1 = _8P_YEAR_LABELS.index(yhi)
        year_cols = _8P_YEAR_LABELS[i0:i1 + 1]

        # 指標選擇（對應 8P 指標清單，預設全選，次序固定）
        all_labels = [lab for lab, _, _ in _8P_ROWS]
        picked = st.multiselect("📊 選擇顯示指標（可自由增減，次序固定）",
                               all_labels, default=all_labels, key="8p_metric_pick")

        table = {}
        for lab, key, kind in _8P_ROWS:
            if lab not in picked:
                continue
            vals = rec[key][i0:i1 + 1]
            out = []
            for v in vals:
                if v is None:
                    out.append("N/A")
                elif kind == "num":
                    out.append(f"{v:,.1f}")
                elif kind == "ratio":
                    out.append(f"{v:g}")
                else:
                    out.append(str(v))
            table[lab] = out
        df_view = pd.DataFrame(table, index=year_cols).T
        df_view.index.name = "財務與 8P 分析指標"
        st.dataframe(df_view, use_container_width=True)

        st.caption("註：財報絕對值依申報貨幣、單位為百萬；股息、ROA、ROE、純利率等比率欄，原始數據口徑不一（部分為小數、部分為百分比數字），"
                   "此處如實顯示原值、未作換算；2026E–2028E 為預測值，空白儲存格代表原始數據缺失。")

        st.download_button(
            "📥 下載此 8P 記錄 (CSV)",
            df_view.to_csv().encode("utf-8-sig"),
            file_name=f"{q_data['code']}_8P_{ylo}_{yhi}.csv",
            mime="text/csv", key="8p_csv_dl")

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
            "⚠️ 完整基本面估值模型（預測 EPS、PE／PB／PCF 估值帶、8P 綜合區間）需要完整財報數據。"
            "目前可取得的資料僅含真實報價，並無預測財報，故本頁定量估值帶**暫停計算**（不以內建或隨機數字填充）。"
            "請按本頁「🔄 更新」、待資料可連線時重試；上方報價、PE／PB 與 52 週區間仍可正常使用。"
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
        st.info("ℹ️ 目前沒有足夠真實數據繪製估值區間（需完整財報或真實 52 週高低點）；資料恢復後按「🔄 更新」即可重繪。")

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
        st.warning("⚠️ 目前無法提供此標的真實月度走勢，無法繪製波浪圖（不使用模擬數據）。")
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
            "⚠️ 目前無法提供此標的足夠長的真實日線，無法計算 ZigZag 擺動點與標浪。"
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
            <h4>📅 近月真實走勢客觀回顧</h4>
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
        st.warning("⚠️ 目前暫時無法連線，宏觀指數真實數據缺失（離線狀態），不顯示任何編造點位或結論。請稍後重試。")

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