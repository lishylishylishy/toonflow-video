import streamlit as st
import requests, json, time, os, uuid
import gspread
from google.oauth2.service_account import Credentials

# ==============================================================================
# ⚙️ 【一、 全局集中配置区】
# ==============================================================================

SHEET_EDIT_URL  = "https://docs.google.com/spreadsheets/d/1Y8YOEl5G2HV4C3WLZz9ttEEoByuWKGLqh_nmWvcb-_M/edit"
SHEET_EMBED_URL = "https://docs.google.com/spreadsheets/d/1Y8YOEl5G2HV4C3WLZz9ttEEoByuWKGLqh_nmWvcb-_M/htmlview"

TAB_NAME_SETTINGS = "toonflow-story-Settings"
TAB_NAME_SCRIPT = "toonflow-story-Script"
TAB_NAME_PROMPT = "prompt"

LOCAL_CONFIG_FILE = ".studio_config.json"

MAX_SCENE_COLUMNS = 5 
SETTINGS_HEADERS = ["类型", "名称"] + [f"场景{i}" for i in range(1, MAX_SCENE_COLUMNS + 1)]
SCRIPT_HEADERS = ["序号", "片段", "片段时长","背景图", "人物", "人物动态", "人物动画", "标题文字", "配音人物", "配音语气", "配音文字", "背景音"]

RECOMMENDED_MODELS = {
    "gemini-3.5-flash-lite": "⚡ gemini-3.5-flash-lite (推荐！)",
    "gemini-3.1-flash-lite": "⚡ gemini-3.1-flash-lite (额度大！)",
    "gemini-3.8-flash": "🧠 gemini-3.8-flash (最新性能王！)"
}

def get_secret(key, default=""):
    try: return st.secrets[key] if key in st.secrets else os.getenv(key, default)
    except Exception: return os.getenv(key, default)

def load_saved_export_dir():
    default_dir = os.path.join(os.path.expanduser("~"), "Downloads")
    try:
        if os.path.exists(LOCAL_CONFIG_FILE):
            with open(LOCAL_CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f).get("export_dir", default_dir)
    except Exception: return default_dir

def save_export_dir(path):
    try:
        with open(LOCAL_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"export_dir": path}, f, ensure_ascii=False, indent=2)
    except Exception: pass

@st.cache_data(ttl=60)
def fetch_prompts_from_sheet():
    """从 Google Sheets 读取 prompt 配置"""
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
    </style>
""", unsafe_allow_html=True)

available_prompts = fetch_prompts_from_sheet()

if 'node_config' not in st.session_state:
    st.session_state.node_config = {
        'text_model': '["deepSeek","deepseek-flash"]',
        'img_draft_model': '["modelscopeImageProvider","Tongyi-MAI/Z-Image-Turbo"]',
        'img_final_model': '["modelscopeImageProvider","FLUX.1-schnell"]', # 备用展示
        'img_size': "1K",
        'img_ratio': "16:9",
        'img_count': 1
    }
if "export_dir" not in st.session_state: st.session_state.export_dir = load_saved_export_dir()

# ==============================================================================
# 🤖 【三、 核心逻辑：两步 AI 解析与双表写入】
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

def process_and_update_sheets(raw_text, setting_prompt, script_prompt, model_name, sheet_url):
    gcp_key_str = get_secret("GCP_SA_KEY", "")
    creds = Credentials.from_service_account_info(json.loads(gcp_key_str), scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"])
    gc, sh = gspread.authorize(creds), gspread.authorize(creds).open_by_url(sheet_url)
    
    # 第一步：全局设定
    st.toast("⏳ 正在生成全局设定 (1/2)...")
    settings_data = call_gemini_json(f"{setting_prompt}\n\n【草稿文案内容】：\n{raw_text}", model_name)
    if isinstance(settings_data, dict): settings_data = settings_data.get("settings", settings_data.get("Settings", []))

    try: ws_settings = sh.worksheet(TAB_NAME_SETTINGS)
    except: ws_settings = sh.add_worksheet(title=TAB_NAME_SETTINGS, rows="100", cols="20")
    rows_settings = [SETTINGS_HEADERS] + [[str(item.get(col, "")) for col in SETTINGS_HEADERS] for item in settings_data]
    ws_settings.clear(); ws_settings.update(range_name='A1', values=rows_settings)

    # 第二步：故事剧本
    st.toast("⏳ 正在结合设定生成故事剧本 (2/2)...")
    settings_json_str = json.dumps(settings_data, ensure_ascii=False)
    prompt_2 = f"{script_prompt}\n\n【草稿文案内容】：\n{raw_text}\n\n【已确定的全局设定（请严格参考此设定）】：\n{settings_json_str}"
    script_data = call_gemini_json(prompt_2, model_name)
    if isinstance(script_data, dict): script_data = script_data.get("script", script_data.get("Script", []))

    try: ws_script = sh.worksheet(TAB_NAME_SCRIPT)
    except: ws_script = sh.add_worksheet(title=TAB_NAME_SCRIPT, rows="100", cols="20")
    rows_script = [SCRIPT_HEADERS] + [[str(item.get(col, "")) for col in SCRIPT_HEADERS] for item in script_data]
    ws_script.clear(); ws_script.update(range_name='A1', values=rows_script)

# ==============================================================================
# 🧩 【四、 Toonflow 节点工程转换器】
# ==============================================================================

def create_node_pair(label, text_content, gen_type, x, y, config):
    text_id, gen_id = str(uuid.uuid4()), str(uuid.uuid4())
    
    text_node = {
        "id": text_id, "type": "remote-textNode", "initialized": False, "position": {"x": x, "y": y},
        "data": {
            "label": label, 
            "handles": [{"id": "in", "type": "target", "dataType": ["VIDEO", "IMAGE", "STRING", "FILE"], "label": "输入"}, {"id": "text", "type": "source", "dataType": "STRING", "label": "输出"}],
            "outputs": {}, 
            "model": config['text_model'], # 应用文本模型
            "textPath": f"assets/{text_id}/content.md", 
            "promptModel": [[{"type": "Write", "text": text_content}]]
        }
    }

    if gen_type == "image":
        gen_node = {
            "id": gen_id, "type": "remote-imageGenerationNode", "initialized": False, "position": {"x": x + 360, "y": y},
            "data": {
                "label": label, "prompt": text_content, "promptModel": [[{"type": "Write", "text": text_content}]],
                "inpaintPrompt": "", "inpaintPromptModel": [],
                "model": config['img_draft_model'], # 生成阶段统一绑定草稿模型
                "size": config['img_size'], "ratio": config['img_ratio'], "other": {"count": config['img_count']},
                "handles": [{"id": "in", "type": "target", "dataType": ["IMAGE", "STRING"], "label": "输入"}, {"id": "image", "type": "source", "dataType": "IMAGE", "label": "输出"}],
                "outputs": {}
            }
        }
    else:
        gen_node = {
            "id": gen_id, "type": "remote-audioGenerationNode", "initialized": False, "position": {"x": x + 360, "y": y},
            "data": {
                "label": label, "prompt": text_content, "promptModel": [[{"type": "Write", "text": text_content}]],
                "model": "", "language": "zh-CN", "sampleRate": 24000, "format": "wav", "other": {},
                "handles": [{"id": "in", "type": "target", "dataType": ["IMAGE", "AUDIO", "STRING"], "label": "输入"}, {"id": "audio", "type": "source", "dataType": "AUDIO", "label": "输出"}],
                "outputs": {}
            }
        }

    edge = {"id": f"vueflow__edge-{text_id}text-{gen_id}in", "type": "simplebezier", "source": text_id, "target": gen_id, "sourceHandle": "text", "targetHandle": "in", "updatable": False, "selectable": False, "focusable": False, "data": {}, "label": "", "interactionWidth": 40, "animated": False}
    return text_node, gen_node, edge


def generate_toonflow_json(config):
    gcp_key_str = get_secret("GCP_SA_KEY", "")
    creds = Credentials.from_service_account_info(json.loads(gcp_key_str), scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"])
    gc, sh = gspread.authorize(creds), gspread.authorize(creds).open_by_url(SHEET_EDIT_URL)
    
    settings_records = sh.worksheet(TAB_NAME_SETTINGS).get_all_records()
    script_records = sh.worksheet(TAB_NAME_SCRIPT).get_all_records()
    project = {"toonflowCanvas": True, "nodes": [], "edges": [], "viewport": {"x": 450, "y": 450, "zoom": 0.5}}
    current_y = -700
    
    for row in settings_records:
        item_type, name = str(row.get("类型", "")).strip(), str(row.get("名称", "")).strip()
        if not name: continue
        prefix = f"{item_type}-{name}" if item_type else name
        for i in range(1, MAX_SCENE_COLUMNS + 1):
            prompt = str(row.get(f"场景{i}", "")).strip()
            if prompt:
                t, g, e = create_node_pair(f"{prefix}-场景{i}", prompt, "image", x=-280, y=current_y, config=config)
                project["nodes"].extend([t, g]); project["edges"].append(e); current_y += 220
                
    current_y = max(current_y, 30) 
    
    for index, row in enumerate(script_records):
        fragment_id = f"片段{row.get('序号', index + 1)}"
        bg_img = str(row.get("背景图", "")).strip()
        if bg_img:
            t, g, e = create_node_pair(f"{fragment_id}-背景图", bg_img, "image", x=650, y=current_y, config=config)
            project["nodes"].extend([t, g]); project["edges"].append(e); current_y += 220
            
        voice_text = str(row.get("配音文字", "")).strip()
        if voice_text:
            voice_actor = str(row.get("配音人物", "")).strip()
            t, g, e = create_node_pair(f"{fragment_id}-配音-{voice_actor}" if voice_actor else fragment_id, voice_text, "audio", x=650, y=current_y, config=config)
            project["nodes"].extend([t, g]); project["edges"].append(e); current_y += 220
            
        bgm_text = str(row.get("背景音", "")).strip()
        if bgm_text:
            t, g, e = create_node_pair(f"{fragment_id}-背景音", bgm_text, "audio", x=650, y=current_y, config=config)
            project["nodes"].extend([t, g]); project["edges"].append(e); current_y += 220
            
    return json.dumps(project, ensure_ascii=False, indent=2)

# ==============================================================================
# 🖥️ 【五、 Streamlit UI 界面构建】
# ==============================================================================

# --- 侧边栏：参数配置和导出 ---
with st.sidebar:
    st.title("🎬 Auto-Studio")
    
    # 1. 模型与参数分类配置
    st.markdown("### 🎛️ 节点参数配置")
    st.session_state.node_config['text_model'] = st.text_input("📝 文本模型", value=st.session_state.node_config['text_model'], help='生成文本节点用的语言模型')
    st.session_state.node_config['img_draft_model'] = st.text_input("🎨 生图模型 (草稿用)", value=st.session_state.node_config['img_draft_model'], help='画布生成时默认绑定的高速模型，用于初版打样')
    st.session_state.node_config['img_final_model'] = st.text_input("💎 生图模型 (精绘备用)", value=st.session_state.node_config['img_final_model'], help='仅做备忘记录：在 Toonflow 选中满意构图后，可手动替换为此高精度模型')
    
    col_size, col_ratio = st.columns(2)
    with col_size:
        st.session_state.node_config['img_size'] = st.selectbox("画面尺寸", ["1K", "2K", "4K"], index=["1K", "2K", "4K"].index(st.session_state.node_config['img_size']))
    with col_ratio:
        st.session_state.node_config['img_ratio'] = st.selectbox("画面比例", ["16:9", "9:16", "1:1", "4:3", "3:4"], index=["16:9", "9:16", "1:1", "4:3", "3:4"].index(st.session_state.node_config['img_ratio']))
    st.session_state.node_config['img_count'] = st.number_input("生图张数", min_value=1, max_value=4, value=st.session_state.node_config['img_count'])

    st.markdown("---")
    
    # 2. 导出功能
    st.markdown("### 📥 导出 Toonflow 工程")
    custom_dir = st.text_input("📁 本地工程保存路径：", value=st.session_state.export_dir, placeholder=r"例如: D:\Toonflow\Projects", help="设置一次自动记住，直接覆写到本地项目文件夹内")
    if custom_dir.strip() and custom_dir != st.session_state.export_dir:
        st.session_state.export_dir = custom_dir.strip(); save_export_dir(custom_dir.strip())

    if st.button("🔄 组装 JSON 并保存到本地", use_container_width=True, type="primary"):
        with st.spinner("正在抓取表格组装节点..."):
            try:
                tf_json = generate_toonflow_json(st.session_state.node_config)
                date_str = time.strftime("%Y%m%d")
                dynamic_filename = f"Toonflow_{date_str}.json"
                target_dir = st.session_state.export_dir.strip()
                save_export_dir(target_dir)
                
                try:
                    os.makedirs(target_dir, exist_ok=True)
                    full_path = os.path.join(target_dir, dynamic_filename)
                    with open(full_path, "w", encoding="utf-8") as f: f.write(tf_json)
                    st.success(f"✅ 已直接保存至本地：\n`{full_path}`")
                except Exception as io_err: st.warning(f"⚠️ 无法直接写入本地路径，请点击下方按钮下载：{io_err}")

                st.download_button(label=f"⬇️ 浏览器手动下载 JSON", data=tf_json, file_name=dynamic_filename, mime="application/json", use_container_width=True)
            except Exception as e: st.error(f"生成失败: {e}")

# --- 主界面 ---
with st.expander("🪄 **AI 故事拆解器 (分步生成：先设定 ➔ 后剧本)**", expanded=True):
    raw_draft = st.text_area("粘贴故事草稿：", placeholder="在这里粘贴你的故事大纲或小说段落...", height=150)
    prompt_keys = list(available_prompts.keys())
    
    col_1, col_2, col_3, col_4 = st.columns([1.5, 1.5, 1, 1])
    with col_1:
        setting_prompt_title = st.selectbox("1. 选择【全局设定】指令：", options=prompt_keys, index=0 if prompt_keys else 0)
    with col_2:
        script_prompt_title = st.selectbox("2. 选择【故事剧本】指令：", options=prompt_keys, index=min(1, len(prompt_keys)-1) if prompt_keys else 0)
    with col_3:
        selected_model = st.selectbox("核心大脑模型：", options=list(RECOMMENDED_MODELS.keys()), format_func=lambda x: RECOMMENDED_MODELS[x])
    with col_4:
        st.write(""); st.write("")
        if st.button("✨ 两步解析填表", type="primary", use_container_width=True):
            if not raw_draft.strip(): st.warning("⚠️ 请先粘贴草稿！")
            elif "错误" in setting_prompt_title or not available_prompts.get(setting_prompt_title, "").strip(): st.warning("⚠️ 请确保 prompt 工作表中有有效数据！")
            else:
                with st.spinner("🤖 正在按步骤调用 AI 并更新双表..."):
                    try:
                        p_setting, p_script = available_prompts[setting_prompt_title], available_prompts[script_prompt_title]
                        process_and_update_sheets(raw_draft, p_setting, p_script, selected_model, SHEET_EDIT_URL)
                        st.success("🎉 分步解析成功！双表已更新。")
                        time.sleep(1.5); st.rerun()
                    except Exception as e: st.error(f"❌ 失败: {str(e)}")

st.divider()
st.link_button("📋 前往 Google Sheets 手动调整", SHEET_EDIT_URL, use_container_width=True)
st.components.v1.iframe(SHEET_EMBED_URL, height=550, scrolling=True)
