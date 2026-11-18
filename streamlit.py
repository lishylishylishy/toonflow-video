import streamlit as st
import requests, json, time, os, zipfile, io, re
import gspread
from google.oauth2.service_account import Credentials

# ==============================================================================
# ⚙️ 【一、 全局配置与密钥集中管理区】
# ==============================================================================

# 1. Google Sheets 编辑与预览链接
SHEET_EDIT_URL  = "https://docs.google.com/spreadsheets/d/1Y8YOEl5G2HV4C3WLZz9ttEEoByuWKGLqh_nmWvcb-_M/edit"
SHEET_EMBED_URL = "https://docs.google.com/spreadsheets/d/1Y8YOEl5G2HV4C3WLZz9ttEEoByuWKGLqh_nmWvcb-_M/htmlview"

# 2. 自动化视频脚本的标准表头 (Toonflow 框架)
SETTINGS_HEADERS = ["类型", "名称", "场景", "生图Prompt"]
SCRIPT_HEADERS = [
    "片段时长", "片段", "背景图", "人物", "人物动态", 
    "人物动画", "标题文字", "配音人物", "配音语气", "配音文字", "背景音"
]

# 3. 可用文本模型列表与备选配置
RECOMMENDED_MODELS = {
    "gemini-3.5-flash-lite": "⚡ gemini-3.5-flash-lite (推荐！RPD: 500次/天, RPM: 15)",
    "gemini-3.1-flash-lite": "⚡ gemini-3.1-flash-lite (额度大！RPD: 500次/天, RPM: 15)",
    "gemini-3.8-flash": "🧠 gemini-3.8-flash (最新性能王！RPD: 20次/天, RPM: 5)",
    "gemini-3.6-flash": "🧠 gemini-3.6-flash (高智能！RPD: 20次/天, RPM: 5)",
    "gemini-3-flash": "🛡️ gemini-3-flash (3代备用！RPD: 20次/天, RPM: 5)",
    "gemini-2.5-flash-lite": "🛡️ gemini-2.5-flash-lite (2.5轻量！RPD: 20次/天, RPM: 10)",
    "gemini-2.5-flash": "🛡️ gemini-2.5-flash (2.5经典！RPD: 20次/天, RPM: 5)"
}

# 4. 安全读取 Secrets
def get_secret(key, default=""):
    try:
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return os.getenv(key, default)


# ==============================================================================
# 🎨 【二、 Streamlit 页面初始化与 CSS 样式修复】
# ==============================================================================

st.set_page_config(
    page_title="Auto Video Studio",
    page_icon="🎬",
    layout="wide",
    initial_sidebar_state="expanded"
)

st.markdown("""
    <style>
        #MainMenu { visibility: hidden; }
        footer { visibility: hidden; }
        header[data-testid="stHeader"] {
            background-color: transparent !important;
            z-index: 100 !important;
        }
        [data-testid="stSidebarCollapsedControl"],
        [data-testid="stSidebarHeader"] button,
        button[aria-label="Expand sidebar"],
        button[aria-label="Close sidebar"] {
            display: flex !important;
            visibility: visible !important;
            z-index: 999999 !important;
            background-color: #ffffff !important;
            border: 1px solid #ddd !important;
            border-radius: 4px !important;
        }
        .block-container {
            padding-top: 2rem !important;
            padding-bottom: 1rem !important;
            padding-left: 2rem !important;
            padding-right: 2rem !important;
        }
        iframe {
            border-radius: 8px;
            border: 1px solid #e0e0e0;
            box-shadow: 0 2px 8px rgba(0,0,0,0.05);
        }
    </style>
""", unsafe_allow_html=True)


# ==============================================================================
# 🤖 【三、 AI 文本转固定脚本 & Google Sheets 动态读写】
# ==============================================================================

def generate_structured_script_via_gemini(raw_text, model_name="gemini-3.5-flash-lite"):
    """
    调用 Gemini API 将自由文本格式化为包含 Settings 和 Script 两个数组的 JSON
    """
    gemini_key = get_secret("GEMINI_API_KEY", "")
    if not gemini_key:
        raise ValueError("未在 Streamlit Secrets 中找到 GEMINI_API_KEY，请先添加！")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={gemini_key}"
    
    prompt = f"""
你是一位专业的动画漫剧导演与分镜师。我需要你将我提供的故事草稿，拆解为一个结构化的 Toonflow 自动化工程数据。
你需要输出一个严格的 JSON，包含两个核心数组："settings" (全局资产字典) 和 "script" (分镜执行序列)。

【第一部分：提取全局资产 (settings)】
从草稿中提取所有出现的人物、核心场景和关键物品，为它们建立全局定妆/定景。
字段要求：
1. "类型"：只能是 "人物"、"场景" 或 "物品"。
2. "名称"：如 "林汐"、"亚特兰蒂斯"。
3. "场景"：如 "现代便服"、"繁华的城市"、"破败的城市"。
4. "生图Prompt"：必须是高质量的纯英文 Prompt。若是人物，需描述特征+服装；若是场景，需描述环境+光影+风格 (masterpiece, highly detailed)。

【第二部分：拆解分镜序列 (script)】
将故事主线拆解为连续的短镜头（每个镜头通常3-5秒）。
字段要求：
1. "片段时长"：数字，通常 3 到 5 秒。
2. "片段"：导演视角的画面与故事描述，要求连续且有画面感。
3. "背景图"：必须**完全匹配** settings 中的“名称-场景”（如 "亚特兰蒂斯-繁华的城市"）。
4. "人物"：必须**完全匹配** settings 中的“名称-场景”（如 "林汐-现代便服"）。如果没有人物则留空。
5. "人物动态"：描述人物在画面中的静态特征（如 "背影"、"正面惊恐"、"埋头工作"）。
6. "人物动画"：后期视频运镜指令（如 "左平移入画"、"旋转缩小"、"轻微放大"、"向左平移出画"）。
7. "标题文字"：视频正中显示的压字大标题，无需每集都有。
8. "配音人物"：如 "旁白"、"林汐"、"凯尔"。
9. "配音语气"：如 "低沉回忆"、"惊恐尖叫"、"平静冷酷"。
10. "配音文字"：该角色的具体台词。旁白讲故事也属于此处。
11. "背景音"：如 "玻璃碎裂声"、"脚步声"、"刺耳警报声"。

【草稿文案内容】：
{raw_text}

【输出强制要求】：
必须只输出 JSON 数据，不要包含任何 Markdown 代码块（不要输出 ```json ），格式如下：
{{
  "settings": [
    {{ "类型": "...", "名称": "...", "场景": "...", "生图Prompt": "..." }}
  ],
  "script": [
    {{ "片段时长": 5, "片段": "...", "背景图": "...", "人物": "...", "人物动态": "...", "人物动画": "...", "标题文字": "...", "配音人物": "...", "配音语气": "...", "配音文字": "...", "背景音": "..." }}
  ]
}}
"""
    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "response_mime_type": "application/json"
        }
    }
    
    res = requests.post(url, headers=headers, json=payload, timeout=60)
    if res.status_code != 200:
        raise RuntimeError(f"模型 [{model_name}] 请求失败 [{res.status_code}]: {res.text}")
        
    result_json = res.json()
    text_content = result_json['candidates'][0]['content']['parts'][0]['text']
    
    # 移除可能存在的 Markdown JSON 标记
    text_content = re.sub(r'^```json\s*', '', text_content)
    text_content = re.sub(r'^```\s*', '', text_content)
    text_content = re.sub(r'\s*```$', '', text_content)
    
    parsed_data = json.loads(text_content.strip())
    return parsed_data


def write_project_to_google_sheets(sheet_url, project_data):
    """
    自动在 Google Sheets 中创建/覆写 Settings 和 Script 两个 Tab
    """
    gcp_key_str = get_secret("GCP_SA_KEY", "")
    if not gcp_key_str:
        raise ValueError("未在 Streamlit Secrets 中找到 GCP_SA_KEY 密钥！")

    service_account_info = json.loads(gcp_key_str)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = Credentials.from_service_account_info(service_account_info, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_url(sheet_url)
    
    settings_data = project_data.get("settings", [])
    script_data = project_data.get("script", [])
    
    # 写入 Settings
    try:
        ws_settings = sh.worksheet("Settings")
    except gspread.exceptions.WorksheetNotFound:
        ws_settings = sh.add_worksheet(title="Settings", rows="100", cols="20")
        
    rows_settings = [SETTINGS_HEADERS]
    for item in settings_data:
        rows_settings.append([str(item.get(col, "")) for col in SETTINGS_HEADERS])
    ws_settings.clear()
    ws_settings.update(range_name='A1', values=rows_settings)

    # 写入 Script
    try:
        ws_script = sh.worksheet("Script")
    except gspread.exceptions.WorksheetNotFound:
        ws_script = sh.add_worksheet(title="Script", rows="100", cols="20")
        
    rows_script = [SCRIPT_HEADERS]
    for item in script_data:
        rows_script.append([str(item.get(col, "")) for col in SCRIPT_HEADERS])
    ws_script.clear()
    ws_script.update(range_name='A1', values=rows_script)


# ==============================================================================
# 🚀 【四、 GitHub Actions 渲染触发与状态查询】
# ==============================================================================
# （保留你的原版执行逻辑，不变）

def trigger_github_workflow(tab_name, resolution):
    token = get_secret("GITHUB_TOKEN", "")
    repo = get_secret("GITHUB_REPOSITORY", get_secret("GITHUB_REPO", ""))
    ref = get_secret("GITHUB_REF", "main")

    if not token or not repo or repo == "your_username/auto-video-maker":
        return False, "请检查 Streamlit Secrets 中的 GITHUB_TOKEN 与 GITHUB_REPOSITORY。"

    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}"
    }
    url = f"https://api.github.com/repos/{repo}/actions/workflows/render.yml/dispatches"
    payload = {"ref": ref, "inputs": {"sheet_tab": "Script", "resolution": resolution}}
    
    try:
        res = requests.post(url, headers
