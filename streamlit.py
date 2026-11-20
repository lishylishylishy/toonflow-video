import streamlit as st

st.set_page_config(page_title="Auto Video Studio", page_icon="🎬", layout="wide", initial_sidebar_state="expanded")
import requests, json, time, os, uuid, re
import urllib.parse
import gspread
from google.oauth2.service_account import Credentials

# ==============================================================================
# ⚙️ 【全局配置区】
# ==============================================================================
SHEET_EDIT_URL  = "https://docs.google.com/spreadsheets/d/1Y8YOEl5G2HV4C3WLZz9ttEEoByuWKGLqh_nmWvcb-_M/edit"
SHEET_EMBED_URL = "https://docs.google.com/spreadsheets/d/1Y8YOEl5G2HV4C3WLZz9ttEEoByuWKGLqh_nmWvcb-_M/htmlview"

TAB_NAME_SETTINGS = "toonflow-story-Settings"
TAB_NAME_SCRIPT = "toonflow-story-Script"
TAB_NAME_PROMPT = "prompt"

MAX_SCENE_COLUMNS = 5 
SETTINGS_HEADERS = ["类型", "名称"] + [f"场景{i}" for i in range(1, MAX_SCENE_COLUMNS + 1)]
SCRIPT_HEADERS = ["序号", "片段", "片段时长", "背景图", "背景动画", "人物", "人物动态", "标题文字", "配音人物", "配音语气", "配音文字", "背景音"]

RECOMMENDED_MODELS = {
    "gemini-3.5-flash-lite": "⚡ gemini-3.5-flash-lite (推荐！)",
    "gemini-3.1-flash-lite": "⚡ gemini-3.1-flash-lite (额度大！)",
    "gemini-3.8-flash": "🧠 gemini-3.8-flash (最新性能王！)"
}

TOONFLOW_IMAGE_MODELS = {
    '["modelscopeImageProvider", "Tongyi-MAI/Z-Image-Turbo"]': "⚡ Z-Image-Turbo (魔搭 · 快速出图)",
    '["modelscopeImageProvider", "Qwen/Qwen-Image-2512"]': "🎨 Qwen-Image-2512 (魔搭 · 高清精绘)",
    "CUSTOM": "✏️ 自定义模型配置字符串..."
}

def get_secret(key, default=""):
    try: return st.secrets[key] if key in st.secrets else os.getenv(key, default)
    except Exception: return os.getenv(key, default)

def safe_get_sheet_records(ws):
    """安全读取工作表为字典列表，自动忽略空表头列与空行"""
    all_values = ws.get_all_values()
    if not all_values or len(all_values) < 2:
        return []
    headers = [h.strip() for h in all_values[0]]
    records = []
    for row in all_values[1:]:
        if not any(cell.strip() for cell in row):
            continue
        item = {}
        for idx, h in enumerate(headers):
            if h and h not in item:
                item[h] = row[idx] if idx < len(row) else ""
        records.append(item)
    return records

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

@st.cache_data(ttl=10)
def fetch_sheet_tables():
    """读取 Settings 和 Script 两张表的最新数据"""
    gcp_key_str = get_secret("GCP_SA_KEY", "")
    if not gcp_key_str:
        raise RuntimeError("未配置 GCP_SA_KEY")
    creds = Credentials.from_service_account_info(json.loads(gcp_key_str), scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"])
    gc = gspread.authorize(creds)
    sh = gc.open_by_url(SHEET_EDIT_URL)
    try:
        settings_records = safe_get_sheet_records(sh.worksheet(TAB_NAME_SETTINGS))
    except Exception:
        settings_records = []
    try:
        script_records = safe_get_sheet_records(sh.worksheet(TAB_NAME_SCRIPT))
    except Exception:
        script_records = []
    return settings_records, script_records

# ==============================================================================
# 🎨 【Streamlit 样式初始化】
# ==============================================================================
st.markdown("""
    <style>
        footer { visibility: hidden; }
        header[data-testid="stHeader"] { background-color: transparent !important; }
        .block-container {
            max-width: 1200px !important;
            padding-top: 2rem !important;
            padding-bottom: 2.5rem !important;
        }
        iframe { border-radius: 8px; border: 1px solid #e0e0e0; }
    </style>
""", unsafe_allow_html=True)

available_prompts = fetch_prompts_from_sheet()

if 'node_config' not in st.session_state:
    st.session_state.node_config = {
        'text_model': '["modelscopeImageProvider", "Qwen/Qwen3.8-27B"]',
        'img_draft_model': '["modelscopeImageProvider", "Tongyi-MAI/Z-Image-Turbo"]',
        'img_final_model': '["modelscopeImageProvider", "Qwen/Qwen-Image-2512"]',
        'img_size': "1K",
        'img_ratio': "9:16",
        'img_count': 1
    }

# ==============================================================================
# 🤖 【核心逻辑：AI 解析与写入 & Toonflow 节点构建】
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
    if res is None or res.status_code != 200: raise RuntimeError("模型无响应")
        
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
    try: current_settings = safe_get_sheet_records(sh.worksheet(TAB_NAME_SETTINGS))
    except Exception: current_settings = []
    
    context_str = json.dumps(current_settings, ensure_ascii=False) if current_settings else "目前为空"
    final_prompt = f"{selected_prompt}\n\n【草稿文案内容】：\n{raw_text}\n\n【参考上下文（表格中已有的全局设定）】：\n{context_str}"
    
    result_data = call_gemini_json(final_prompt, model_name)
    settings_new = result_data.get("settings", result_data.get("Settings", []))
    script_new = result_data.get("script", result_data.get("Script", []))
    
    if settings_new:
        try: ws_settings = sh.worksheet(TAB_NAME_SETTINGS)
        except: 
            ws_settings = sh.add_worksheet(title=TAB_NAME_SETTINGS, rows="100", cols="20")
            ws_settings.append_row(SETTINGS_HEADERS)
        ws_settings.append_rows([[str(item.get(col, "")) for col in SETTINGS_HEADERS] for item in settings_new])

    if script_new:
        try: ws_script = sh.worksheet(TAB_NAME_SCRIPT)
        except: 
            ws_script = sh.add_worksheet(title=TAB_NAME_SCRIPT, rows="100", cols="20")
            ws_script.append_row(SCRIPT_HEADERS)
        ws_script.append_rows([[str(item.get(col, "")) for col in SCRIPT_HEADERS] for item in script_new])
    fetch_sheet_tables.clear()

def create_image_node(label, text_content, x, y, config, ref_node_info=None):
    """创建文生图节点 (remote-imageGenerationNode)"""
    gen_id = str(uuid.uuid4())
    try: model_str = json.dumps(json.loads(config['img_draft_model']), separators=(',', ':'))
    except: model_str = config['img_draft_model']

    final_prompt = text_content
    prompt_model_items = []
    edge_to_add = None

    if ref_node_info:
        ref_id = ref_node_info["id"]
        edge_to_add = {"id": f"vueflow__edge-{ref_id}image-{gen_id}in", "type": "simplebezier", "source": ref_id, "target": gen_id, "sourceHandle": "image", "targetHandle": "in"}
        encoded_ref = urllib.parse.quote(json.dumps([ref_id, "image"], separators=(',', ':')))
        custom_tag = {"type": "Custom", "html": f'<span style="display: inline-block;"><span class="imageReference previewable" data-reference-id="{encoded_ref}" data-value="{{{{ref 1}}}}">{ref_node_info["label"]}</span></span>'}
        
        pattern = r'(@引用|@场景1|@参考图)'
        if re.search(pattern, text_content):
            final_prompt = re.sub(pattern, '{{ref 1}}', text_content)
            for part in re.split(pattern, text_content):
                if not part: continue
                if re.match(pattern, part): prompt_model_items.append(custom_tag)
                else: prompt_model_items.append({"type": "Write", "text": part})
        else:
            final_prompt = f"参考{{{{ref 1}}}}，{text_content}"
            prompt_model_items = [{"type": "Write", "text": "参考"}, custom_tag, {"type": "Write", "text": f"，{text_content}"}]
    else:
        prompt_model_items = [{"type": "Write", "text": text_content}]

    node = {
        "id": gen_id,
        "type": "remote-imageGenerationNode",
        "position": {"x": x, "y": y},
        "data": {
            "label": label,
            "prompt": final_prompt,
            "promptModel": [prompt_model_items],
            "model": model_str,
            "size": config['img_size'],
            "ratio": config['img_ratio'],
            "other": {},
            "handles": [
                {"id": "in", "type": "target", "dataType": ["IMAGE", "STRING"], "label": "图片、文本输入"}, 
                {"id": "image", "type": "source", "dataType": "IMAGE", "label": "图片输出"}
            ]
        }
    }
    return node, edge_to_add

def create_text_node(label, text_content, x, y):
    """创建纯文本节点 (remote-textNode)，用于背景动画、人物动态、标题文字"""
    node_id = str(uuid.uuid4())
    return {
        "id": node_id,
        "type": "remote-textNode",
        "position": {"x": x, "y": y},
        "data": {
            "label": label,
            "text": text_content,
            "prompt": text_content,
            "promptModel": [[{"type": "Write", "text": text_content}]],
            "outputs": {
                "text": {"dataType": "STRING", "value": text_content}
            },
            "other": {},
            "handles": [
                {"id": "text", "type": "source", "dataType": "STRING", "label": "文本输出"}
            ]
        }
    }

def build_toonflow_json(config, settings_records, script_records, export_type="settings"):
    project = {"toonflowCanvas": True, "nodes": [], "edges": [], "viewport": {"x": 450, "y": 450, "zoom": 0.5}}
    current_y = -700
    
    if export_type == "settings":
        for row in settings_records:
            item_type, name = str(row.get("类型", "")).strip(), str(row.get("名称", "")).strip()
            if not name: continue
            prefix = f"{item_type}-{name}" if item_type else name
            scene1_ref = None
            has_any_scene = False
            for i in range(1, MAX_SCENE_COLUMNS + 1):
                prompt = str(row.get(f"场景{i}", "")).strip()
                if not prompt: continue
                has_any_scene = True
                node_label = f"{prefix}-场景{i}"
                node_x = -280 + (i - 1) * 420
                if i == 1:
                    node, edge = create_image_node(node_label, prompt, node_x, current_y, config, None)
                    scene1_ref = {"id": node["id"], "label": node_label}
                else:
                    node, edge = create_image_node(node_label, prompt, node_x, current_y, config, scene1_ref)
                project["nodes"].append(node)
                if edge: project["edges"].append(edge)
            if has_any_scene: current_y += 420

    elif export_type == "script":
        for idx, row in enumerate(script_records, start=1):
            raw_seq = str(row.get("序号", "")).strip() or str(idx)
            seq_prefix = raw_seq if raw_seq.startswith("片段") else f"片段{raw_seq}"
            node_x, has_clip_node = -280, False
            
            # 1. 文生图节点：片段序号 + 列名（如 片段1-背景图、片段1-人物）
            for img_col in ["背景图", "人物"]:
                val = str(row.get(img_col, "")).strip()
                if val:
                    node, _ = create_image_node(f"{seq_prefix}-{img_col}", val, node_x, current_y, config)
                    project["nodes"].append(node)
                    node_x += 420
                    has_clip_node = True
            
            # 2. 文本节点：片段序号 + 列名（如 片段1-背景动画、片段1-人物动态、片段1-标题文字）
            for txt_col in ["背景动画", "人物动态", "标题文字"]:
                val = str(row.get(txt_col, "")).strip()
                if val:
                    node = create_text_node(f"{seq_prefix}-{txt_col}", val, node_x, current_y)
                    project["nodes"].append(node)
                    node_x += 420
                    has_clip_node = True
                
            if has_clip_node: current_y += 420

    return json.dumps(project, ensure_ascii=False, indent=2), len(project["nodes"])

# ==============================================================================
# 📌 【左侧边栏：Google 表格直达与实时预览】
# ==============================================================================
with st.sidebar:
    st.header("📊 Google 表格数据")
    st.link_button("🔗 打开 Google 表格编辑", SHEET_EDIT_URL, use_container_width=True)
    with st.expander("📋 表格数据预览（展开查看）", expanded=True):
        st.components.v1.iframe(SHEET_EMBED_URL, height=580, scrolling=True)

# ==============================================================================
# 🖥️ 【主界面渲染区（宽屏自上而下流式布局）】
# ==============================================================================
st.title("🎬 Auto Video Studio")
st.divider()

# ------------------------------------------------------------------------------
# 第一部分：草稿解析
# ------------------------------------------------------------------------------
st.subheader("1. 草稿解析")

c_p1, c_p2 = st.columns([1.2, 1])
with c_p1:
    selected_prompt_name = st.selectbox(
        "选择 Prompt 模板",
        list(available_prompts.keys()) if isinstance(available_prompts, dict) else []
    )
with c_p2:
    selected_model = st.selectbox(
        "选择解析模型",
        list(RECOMMENDED_MODELS.keys()),
        format_func=lambda k: RECOMMENDED_MODELS[k]
    )

with st.expander("📝Prompt", expanded=False):
    current_prompt_text = st.text_area(
        "Prompt 内容",
        value=available_prompts.get(selected_prompt_name, "") if isinstance(available_prompts, dict) else "",
        height=180,
        label_visibility="collapsed"
    )

raw_story = st.text_area("输入草稿文案", height=160, placeholder="在此粘贴故事草稿或分镜文案...")

if st.button("🚀 解析并写入 Google 表格", type="primary", use_container_width=True):
    if raw_story.strip():
        process_and_append_sheets(raw_story, current_prompt_text, selected_model, SHEET_EDIT_URL)
        st.success("✅ 解析并写入成功！")
        time.sleep(1)
        st.rerun()
    else:
        st.warning("请先输入草稿文案。")

st.divider()

# ------------------------------------------------------------------------------
# 第二部分：导出 JSON (Toonflow) —— 单按钮一键生成并下载
# ------------------------------------------------------------------------------
h_c1, h_c2 = st.columns([4, 1])
with h_c1:
    st.subheader("2. 导出 JSON (Toonflow)")
with h_c2:
    if st.button("🔄 同步最新表格", use_container_width=True):
        fetch_sheet_tables.clear()
        st.rerun()

c_m, c_r, c_s = st.columns([2.2, 1, 1])
with c_m:
    chosen_tf_model = st.selectbox(
        "默认生图模型（写入文生图节点）",
        list(TOONFLOW_IMAGE_MODELS.keys()),
        format_func=lambda k: TOONFLOW_IMAGE_MODELS[k]
    )
with c_r:
    st.session_state.node_config['img_ratio'] = st.selectbox("画面比例", ["9:16", "16:9", "1:1", "3:4", "4:3"])
with c_s:
    st.session_state.node_config['img_size'] = st.selectbox("分辨率", ["1K", "2K"])

if chosen_tf_model == "CUSTOM":
    custom_model_str = st.text_input(
        "自定义 Toonflow 模型配置（JSON 数组格式）",
        value='["modelscopeImageProvider", "Qwen/Qwen-Image-2512"]'
    )
    st.session_state.node_config['img_draft_model'] = custom_model_str.strip()
else:
    st.session_state.node_config['img_draft_model'] = chosen_tf_model

st.write("")

# 预先生成最新 JSON（单按钮点击即可完成生成+下载）
settings_json_data, settings_node_cnt = "", 0
script_json_data, script_node_cnt = "", 0
load_error = ""

try:
    s_records, sc_records = fetch_sheet_tables()
    settings_json_data, settings_node_cnt = build_toonflow_json(
        st.session_state.node_config, s_records, sc_records, "settings"
    )
    script_json_data, script_node_cnt = build_toonflow_json(
        st.session_state.node_config, s_records, sc_records, "script"
    )
except Exception as e:
    load_error = str(e)

if load_error:
    st.error(f"表格读取异常：{load_error}")
else:
    ts_str = time.strftime("%Y%m%d_%H%M%S")
    btn_c1, btn_c2 = st.columns(2, gap="medium")
    with btn_c1:
        st.download_button(
            label=f"⬇️ 导出 Settings（定妆 / 定景 · {settings_node_cnt} 节点）",
            data=settings_json_data,
            file_name=f"Toonflow_Settings_{ts_str}.json",
            mime="application/json",
            type="primary",
            use_container_width=True,
            disabled=(settings_node_cnt == 0),
            key="dl_settings_json",
        )

    with btn_c2:
        st.download_button(
            label=f"⬇️ 导出 Script（分镜 · {script_node_cnt} 节点）",
            data=script_json_data,
            file_name=f"Toonflow_Script_{ts_str}.json",
            mime="application/json",
            type="primary",
            use_container_width=True,
            disabled=(script_node_cnt == 0),
            key="dl_script_json",
        )
