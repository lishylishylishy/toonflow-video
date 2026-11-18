import streamlit as st
import requests, json, time, os, zipfile, io, re, uuid
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
# 🤖 【三、 核心逻辑：AI 解析与双表写入】
# ==============================================================================

def process_and_update_sheets(raw_text, model_name, sheet_url):
    gcp_key_str = get_secret("GCP_SA_KEY", "")
    if not gcp_key_str:
        raise ValueError("未在 Streamlit Secrets 中找到 GCP_SA_KEY 密钥！")
    
    creds = Credentials.from_service_account_info(json.loads(gcp_key_str), scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"])
    gc = gspread.authorize(creds)
    sh = gc.open_by_url(sheet_url)
    
    # 1. 读取 Settings 表第一行的 Prompt
    try:
        ws_settings = sh.worksheet(TAB_NAME_SETTINGS)
        first_row = ws_settings.row_values(1)
        base_prompt = first_row[1] if len(first_row) > 1 else ""
    except gspread.exceptions.WorksheetNotFound:
        ws_settings = sh.add_worksheet(title=TAB_NAME_SETTINGS, rows="100", cols="20")
        first_row = ["Prompt", "请在B1填写你的系统提示词..."]
        base_prompt = first_row[1]

    if not base_prompt:
        raise ValueError("未在 Settings 表的 B1 单元格找到 Prompt，请先在表格中填写！")

    final_prompt = f"{base_prompt}\n\n【草稿文案内容】：\n{raw_text}"
    
    # 2. 调用 Gemini API
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
    
    # 【防崩溃补丁】暴力提取 JSON，无视 AI 附带的废话
    start_idx = text_content.find('{')
    end_idx = text_content.rfind('}')
    clean_json = text_content[start_idx:end_idx+1] if start_idx != -1 and end_idx != -1 else text_content
    project_data = json.loads(clean_json)

    # 3. 写入 Settings 表
    settings_data = project_data.get("settings", [])
    # 【防崩溃补丁】给第一行补齐空白，防止长度不一致导致 gspread 报错
    padded_first_row = first_row + [""] * (len(SETTINGS_HEADERS) - len(first_row))
    rows_settings = [padded_first_row, SETTINGS_HEADERS]
    for item in settings_data:
        rows_settings.append([str(item.get(col, "")) for col in SETTINGS_HEADERS])
    ws_settings.clear()
    ws_settings.update(range_name='A1', values=rows_settings)

    # 4. 写入 Script 表
    script_data = project_data.get("script", [])
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
# 🧩 【四、 Toonflow 节点工程转换器 (抓表格 -> 生成 JSON)】
# ==============================================================================

def create_node_pair(label, text_content, gen_type, x, y):
    text_id = str(uuid.uuid4())
    gen_id = str(uuid.uuid4())
    text_node = {
        "id": text_id, "type": "remote-textNode", "position": {"x": x, "y": y},
        "data": {
            "label": label, "promptModel": [[{"type": "Write", "text": text_content}]],
            "handles": [{"id": "text", "type": "source", "dataType": "STRING", "label": "文本输出"}]
        }
    }
    gen_node = {
        "id": gen_id, "type": f"remote-{gen_type}GenerationNode", "position": {"x": x + 300, "y": y},
        "data": {
            "label": label, "handles": [{"id": "in", "type": "target", "dataType": ["STRING"]}]
        }
    }
    edge = {
        "id": f"vueflow__edge-{text_id}text-{gen_id}in", "type": "simplebezier",
        "source": text_id, "target": gen_id, "sourceHandle": "text", "targetHandle": "in"
    }
    return text_node, gen_node, edge

def generate_toon
