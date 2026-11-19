import streamlit as st
import requests, json, time, os, uuid
import gspread
from google.oauth2.service_account import Credentials
import io, zipfile, shutil
from gradio_client import Client

# ==============================================================================
# ⚙️ 【一、 全局集中配置区】
# ==============================================================================

SHEET_EDIT_URL  = "https://docs.google.com/spreadsheets/d/1Y8YOEl5G2HV4C3WLZz9ttEEoByuWKGLqh_nmWvcb-_M/edit"
SHEET_EMBED_URL = "https://docs.google.com/spreadsheets/d/1Y8YOEl5G2HV4C3WLZz9ttEEoByuWKGLqh_nmWvcb-_M/htmlview"

TAB_NAME_SETTINGS = "toonflow-story-Settings"
TAB_NAME_SCRIPT = "toonflow-story-Script"
TAB_NAME_PROMPT = "prompt"

MAX_SCENE_COLUMNS = 5 
SETTINGS_HEADERS = ["类型", "名称"] + [f"场景{i}" for i in range(1, MAX_SCENE_COLUMNS + 1)]
SCRIPT_HEADERS = ["序号", "片段", "片段时长","背景图", "背景动画", "人物", "人物动态", "标题文字", "配音人物", "配音语气", "配音文字", "背景音"]

RECOMMENDED_MODELS = {
    "gemini-3.5-flash-lite": "⚡ gemini-3.5-flash-lite (推荐！)",
    "gemini-3.1-flash-lite": "⚡ gemini-3.1-flash-lite (额度大！)",
    "gemini-3.8-flash": "🧠 gemini-3.8-flash (最新性能王！)"
}

def get_secret(key, default=""):
    try: return st.secrets[key] if key in st.secrets else os.getenv(key, default)
    except Exception: return os.getenv(key, default)

@st.cache_data(ttl=60)
def fetch_prompts_from_sheet():
    gcp_key_str = get_secret("GCP_SA_KEY", "")
    if not gcp_key_str: return {"错误": "未配置 GCP_SA_KEY"}
    try:
        creds = Credentials.from_service_account_info(json.loads(gcp_key_str), scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"])
        gc = gspread.authorize(creds)
        sh = gc.open_by_url(SHEET_EDIT_URL)
        ws_prompt = sh.worksheet(TAB_NAME_PROMPT)
        records = ws_prompt.get_all_values()
        
        prompts = {}
        for row in records:
            if len(row) >= 2 and row[0].strip():
                prompts[row[0].strip()] = row[1].strip()
        return prompts if prompts else {"默认": "请在 prompt 表格中添加数据。"}
    except Exception as e: return {"读取错误": f"读取 prompt 表格失败: {e}"}

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
        div[data-testid="stForm"] { border: 1px solid #e0e0e0; border-radius: 8px; padding: 15px; }
    </style>
""", unsafe_allow_html=True)

available_prompts = fetch_prompts_from_sheet()

#if 'node_config' not in st.session_state:
st.session_state.node_config = {
    'text_model':'["modelscopeImageProvider", "Qwen/Qwen3.8-27B"]',
    'img_draft_model':'["modelscopeImageProvider", "Tongyi-MAI/Z-Image-Turbo"]',
    'img_final_model':'["modelscopeImageProvider", "Qwen/Qwen-Image-2512"]',
    'img_size': "1K",
    'img_ratio': "9:16",
    'img_count': 1
    }

# ==============================================================================
# 🤖 【三、 核心逻辑：AI 解析与纯追加写入】
# ==============================================================================

def call_gemini_json(prompt_text, model_name):
    gemini_key = get_secret("GEMINI_API_KEY", "")
    payload = {"contents": [{"parts": [{"text": prompt_text}]}], "generationConfig": {"response_mime_type": "application/json"}}
    candidate_models = [model_name] + [m for m in RECOMMENDED_MODELS.keys() if m != model_name]
    res = None
    for current_model in candidate_models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{current_model}:generateContent?key={gemini_key}"
        for attempt in range(3):
            res = requests.post(url, headers={"Content-Type": "application/json"}, json=payload, timeout=90)
            if res.status_code == 200: break
            if res.status_code in (429, 503):
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(f"模型 [{current_model}] 请求失败 ({res.status_code}): {res.text}")
        if res is not None and res.status_code == 200: break

    if res is None or res.status_code != 200:
        raise RuntimeError(f"所有备用模型均繁忙，请稍后再试。报错: {res.text if res else '无响应'}")
        
    text_content = res.json()['candidates'][0]['content']['parts'][0]['text']
    start_idx, end_idx = text_content.find('{'), text_content.rfind('}')
    if start_idx == -1: start_idx = text_content.find('[')
    if end_idx == -1 or text_content.rfind(']') > end_idx: end_idx = text_content.rfind(']')
    clean_json = text_content[start_idx:end_idx+1] if start_idx != -1 and end_idx != -1 else text_content
    return json.loads(clean_json)

def process_and_append_sheets(raw_text, selected_prompt, model_name, sheet_url):
    gcp_key_str = get_secret("GCP_SA_KEY", "")
    creds = Credentials.from_service_account_info(json.loads(gcp_key_str), scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"])
    gc = gspread.authorize(creds)
    sh = gc.open_by_url(sheet_url)
    
    st.toast("⏳ 正在请求 AI 解析...")
    
    # 仅读取上下文，不赋予覆盖权限
    try: current_settings = sh.worksheet(TAB_NAME_SETTINGS).get_all_records()
    except Exception: current_settings = []
    
    context_str = json.dumps(current_settings, ensure_ascii=False) if current_settings else "目前为空"
    final_prompt = f"{selected_prompt}\n\n【草稿文案内容】：\n{raw_text}\n\n【参考上下文（表格中已有的全局设定）】：\n{context_str}"
    
    result_data = call_gemini_json(final_prompt, model_name)
    settings_new = result_data.get("settings", result_data.get("Settings", []))
    script_new = result_data.get("script", result_data.get("Script", []))
    
    # 【修复】：使用 append_rows 纯追加模式，AI 绝无权限删除或覆盖已有内容
    if settings_new:
        try: ws_settings = sh.worksheet(TAB_NAME_SETTINGS)
        except: 
            ws_settings = sh.add_worksheet(title=TAB_NAME_SETTINGS, rows="100", cols="20")
            ws_settings.append_row(SETTINGS_HEADERS)
            
        rows_to_append = [[str(item.get(col, "")) for col in SETTINGS_HEADERS] for item in settings_new]
        if rows_to_append: ws_settings.append_rows(rows_to_append)

    if script_new:
        try: ws_script = sh.worksheet(TAB_NAME_SCRIPT)
        except: 
            ws_script = sh.add_worksheet(title=TAB_NAME_SCRIPT, rows="100", cols="20")
            ws_script.append_row(SCRIPT_HEADERS)
            
        rows_to_append = [[str(item.get(col, "")) for col in SCRIPT_HEADERS] for item in script_new]
        if rows_to_append: ws_script.append_rows(rows_to_append)

# ==============================================================================
# 🧩 【四、 Toonflow 节点工程转换器 (状态修复版)】
# ==============================================================================

def create_standalone_node(label, text_content, gen_type, x, y, config, ref_node_info=None):
    import uuid
    import json
    import urllib.parse
    import re
    
    gen_id = str(uuid.uuid4())
    
    if gen_type == "image":
        try:
            raw_list = json.loads(config['img_draft_model'])
            model_str = json.dumps(raw_list, separators=(',', ':'))
        except Exception:
            model_str = config['img_draft_model']

        # 处理自动 @场景1 引用逻辑
        final_prompt = text_content
        prompt_model_items = []
        edge_to_add = None

        if ref_node_info:
            ref_id = ref_node_info["id"]
            ref_label = ref_node_info["label"]
            
            # 1. 生成节点连线 (Edge)
            edge_to_add = {
                "id": f"vueflow__edge-{ref_id}image-{gen_id}in",
                "type": "simplebezier",
                "source": ref_id,
                "target": gen_id,
                "sourceHandle": "image",
                "targetHandle": "in"
            }

            # 2. 构造 Toonflow 专属富文本标签
            encoded_ref = urllib.parse.quote(json.dumps([ref_id, "image"], separators=(',', ':')))
            custom_tag = {
                "type": "Custom",
                "html": f'<span style="display: inline-block;"><span class="imageReference previewable" data-reference-id="{encoded_ref}" data-value="{{{{ref 1}}}}">{ref_label}</span></span>'
            }

            # 3. 识别文本中的 @引用 / @场景1 占位符；若用户没写，则自动加在句首
            pattern = r'(@引用|@场景1|@参考图)'
            if re.search(pattern, text_content):
                final_prompt = re.sub(pattern, '{{ref 1}}', text_content)
                parts = re.split(pattern, text_content)
                for part in parts:
                    if not part:
                        continue
                    if re.match(pattern, part):
                        prompt_model_items.append(custom_tag)
                    else:
                        prompt_model_items.append({"type": "Write", "text": part})
            else:
                final_prompt = f"参考{{{{ref 1}}}}，{text_content}"
                prompt_model_items = [
                    {"type": "Write", "text": "参考"},
                    custom_tag,
                    {"type": "Write", "text": f"，{text_content}"}
                ]
        else:
            prompt_model_items = [{"type": "Write", "text": text_content}]

        node = {
            "id": gen_id, "type": "remote-imageGenerationNode", "position": {"x": x, "y": y},
            "data": {
                "label": label, 
                "prompt": final_prompt, 
                "promptModel": [prompt_model_items],
                "model": model_str,
                "size": config['img_size'], "ratio": config['img_ratio'], 
                "other": {},
                "handles": [
                    {"id": "in", "type": "target", "dataType": ["IMAGE", "STRING"], "label": "图片、文本输入"}, 
                    {"id": "image", "type": "source", "dataType": "IMAGE", "label": "图片输出"}
                ]
            }
        }
        return node, edge_to_add

    else:
        node = {
            "id": gen_id, "type": "remote-audioGenerationNode", "position": {"x": x, "y": y},
            "data": {
                "label": label, 
                "prompt": text_content, 
                "promptModel": [[{"type": "Write", "text": text_content}]],
                "model": "", "language": "zh-CN", "sampleRate": 24000, "format": "wav", "other": {},
                "handles": [
                    {"id": "in", "type": "target", "dataType": ["IMAGE", "AUDIO", "STRING"], "label": "图片或音频限1个，文本不限"}, 
                    {"id": "audio", "type": "source", "dataType": "AUDIO", "label": "音频输出"}
                ]
            }
        }
        return node, None


def generate_toonflow_json(config, export_type="all"):
    gcp_key_str = get_secret("GCP_SA_KEY", "")
    creds = Credentials.from_service_account_info(json.loads(gcp_key_str), scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"])
    gc, sh = gspread.authorize(creds), gspread.authorize(creds).open_by_url(SHEET_EDIT_URL)
    
    project = {"toonflowCanvas": True, "nodes": [], "edges": [], "viewport": {"x": 450, "y": 450, "zoom": 0.5}}
    current_y = -700
    
    if export_type in ["settings", "all"]:
        try: settings_records = sh.worksheet(TAB_NAME_SETTINGS).get_all_records()
        except: settings_records = []
        for row in settings_records:
            item_type, name = str(row.get("类型", "")).strip(), str(row.get("名称", "")).strip()
            if not name: continue
            prefix = f"{item_type}-{name}" if item_type else name
            
            scene1_ref = None  # 用于记录当前角色/背景“场景1”的节点信息
            has_any_scene = False
            
            for i in range(1, MAX_SCENE_COLUMNS + 1):
                prompt = str(row.get(f"场景{i}", "")
