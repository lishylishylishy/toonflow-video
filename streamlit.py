import streamlit as st
import requests, json, time, os, zipfile, io, re
import gspread
from google.oauth2.service_account import Credentials

# ==============================================================================
# ⚙️ 【一、 全局集中配置区】
# ==============================================================================

SHEET_EDIT_URL  = "https://docs.google.com/spreadsheets/d/1Y8YOEl5G2HV4C3WLZz9ttEEoByuWKGLqh_nmWvcb-_M/edit"
SHEET_EMBED_URL = "https://docs.google.com/spreadsheets/d/1Y8YOEl5G2HV4C3WLZz9ttEEoByuWKGLqh_nmWvcb-_M/htmlview"

TAB_NAME_SETTINGS = "toonflow-story-Settings"
TAB_NAME_SCRIPT = "toonflow-story-Script"

# 根据你的表格，设定最大场景列数
MAX_SCENE_COLUMNS = 5 
SETTINGS_HEADERS = ["类型", "名称"] + [f"场景{i}" for i in range(1, MAX_SCENE_COLUMNS + 1)]

SCRIPT_HEADERS = [
     "序号", "片段", "片段时长","背景图", "人物", "人物动态", 
    "人物动画", "标题文字", "配音人物", "配音语气", "配音文字", "背景音"
]

RECOMMENDED_MODELS = {
    "gemini-3.5-flash-lite": "⚡ gemini-3.5-flash-lite (推荐！)",
    "gemini-3.1-flash-lite": "⚡ gemini-3.1-flash-lite (额度大！)",
    "gemini-3.8-flash": "🧠 gemini-3.8-flash (最新性能王！)"
}

def get_secret(key, default=""):
    try:
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return os.getenv(key, default)

# ==============================================================================
# 🎨 【二、 Streamlit 初始化】
# ==============================================================================
st.set_page_config(page_title="Auto Video Studio", page_icon="🎬", layout="wide")
st.markdown("""
    <style>
        #MainMenu, footer { visibility: hidden; }
        header[data-testid="stHeader"] { background-color: transparent !important; }
        .block-container { padding-top: 2rem !important; padding-bottom: 1rem !important; }
        iframe { border-radius: 8px; border: 1px solid #e0e0e0; }
    </style>
""", unsafe_allow_html=True)

# ==============================================================================
# 🤖 【三、 核心逻辑：读取表头Prompt -> 调AI -> 保留原Prompt并写入】
# ==============================================================================

def process_and_update_sheets(raw_text, model_name, sheet_url):
    # 1. 连接 Google Sheets
    gcp_key_str = get_secret("GCP_SA_KEY", "")
    if not gcp_key_str:
        raise ValueError("未在 Streamlit Secrets 中找到 GCP_SA_KEY 密钥！")
    
    creds = Credentials.from_service_account_info(json.loads(gcp_key_str), scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"])
    gc = gspread.authorize(creds)
    sh = gc.open_by_url(sheet_url)
    
    # 2. 读取 Settings 表第一行的 Prompt
    try:
        ws_settings = sh.worksheet(TAB_NAME_SETTINGS)
        first_row = ws_settings.row_values(1)
        # 假设 A1 是 "Prompt", B1 是内容
        base_prompt = first_row[1] if len(first_row) > 1 else ""
    except gspread.exceptions.WorksheetNotFound:
        ws_settings = sh.add_worksheet(title=TAB_NAME_SETTINGS, rows="100", cols="20")
        first_row = ["Prompt", "请在B1填写你的系统提示词..."]
        base_prompt = first_row[1]

    if not base_prompt:
        raise ValueError("未在 Settings 表的 B1 单元格找到 Prompt，请先在表格中填写！")

    # 3. 组合最终发给 Gemini 的 Prompt
    final_prompt = f"""
{base_prompt}

【注意：提取 Settings 资产时，请使用 "场景1", "场景2" 等作为键名直接填写 Prompt。】

【草稿文案内容】：
{raw_text}
"""
    
    # 4. 调用 Gemini API
    gemini_key = get_secret("GEMINI_API_KEY", "")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={gemini_key}"
    payload = {
        "contents": [{"parts": [{"text": final_prompt}]}],
        "generationConfig": {"response_mime_type": "application/json"}
    }
    res = requests.post(url, headers={"Content-Type": "application/json"}, json=payload, timeout=60)
    if res.status_code != 200:
        raise RuntimeError(f"模型请求失败: {res.text}")
        
    text_content = res.json()['candidates'][0]['content']['parts'][0]['text']
    text_content = re.sub(r'^```json\s*|^```\s*|\s*```$', '', text_content)
    project_data = json.loads(text_content.strip())

    # 5. 写回 Google Sheets (保留第一行 Prompt)
    settings_data = project_data.get("settings", [])
    script_data = project_data.get("script", [])

    # -- 写入 Settings (第一行原封不动 + 第二行表头 + 数据) --
    rows_settings = [first_row, SETTINGS_HEADERS]
    for item in settings_data:
        rows_settings.append([str(item.get(col, "")) for col in SETTINGS_HEADERS])
    ws_settings.clear()
    ws_settings.update(range_name='A1', values=rows_settings)

    # -- 写入 Script (覆盖式写入) --
    try:
        ws_script = sh.worksheet(TAB_NAME_SCRIPT)
    except gspread.exceptions.WorksheetNotFound:
        ws_script = sh.add_worksheet(title=TAB_NAME_SCRIPT, rows="100", cols="20")
        
    rows_script = [SCRIPT_HEADERS]
    for item in script_data:
        rows_script.append([str(item.get(col, "")) for col in SCRIPT_HEADERS])
    ws_script.clear()
    ws_script.update(range_name='A1', values=rows_script)

# ==============================================================================
# 📊 【四、 主界面 UI】
# ==============================================================================

with st.expander("🪄 **AI Toonflow 剧本生成器**", expanded=True):
    raw_draft = st.text_area("粘贴故事草稿：", placeholder="输入草稿...", height=150)
    col_1, col_2 = st.columns([3, 1])
    with col_1:
        selected_model_key = st.selectbox("选择 Gemini 文本模型：", options=list(RECOMMENDED_MODELS.keys()), format_func=lambda x: RECOMMENDED_MODELS[x])
    with col_2:
        st.write(""); st.write("")
        gen_btn = st.button("✨ 一键解析并写入双表", type="primary", use_container_width=True)
        
    if gen_btn:
        if not raw_draft.strip():
            st.warning("⚠️ 请先粘贴文案草稿！")
        else:
            with st.spinner(f"🤖 正在读取表格 Prompt 并生成分镜..."):
                try:
                    process_and_update_sheets(raw_draft, selected_model_key, SHEET_EDIT_URL)
                    st.success(f"🎉 解析成功！已自动写入并保留了你原本的 Prompt。")
                    time.sleep(1.5)
                    st.rerun()
                except Exception as e:
                    st.error(f"❌ 失败: {str(e)}")

st.divider()
st.link_button("📋前往 Google Sheets 编辑", SHEET_EDIT_URL, use_container_width=True)
st.components.v1.iframe(SHEET_EMBED_URL, height=520, scrolling=True)
