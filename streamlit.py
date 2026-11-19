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
    'text_model': '["Qwen/Qwen3.8-27B"]',
    'img_draft_model': '["Tongyi-MAI/Z-Image-Turbo"]',
    'img_final_model': '["Qwen/Qwen-Image-2512"]',
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

def create_standalone_node(label, text_content, gen_type, x, y, config):
    import uuid
    import json
    gen_id = str(uuid.uuid4())
    
    # 解析模型字符串
    try:
        img_model_parsed = json.loads(config['img_draft_model'])
    except Exception:
        img_model_parsed = config['img_draft_model']

    # 直接返回包含完整 Prompt 数据的单一生成节点，抛弃需要请求本地磁盘的 remote-textNode
    if gen_type == "image":
        node = {
            "id": gen_id, "type": "remote-imageGenerationNode", "position": {"x": x, "y": y},
            "data": {
                "label": label, 
                "prompt": text_content, 
                "promptModel": [[{"type": "Write", "text": text_content}]],
                "model": img_model_parsed,   
                "size": config['img_size'], "ratio": config['img_ratio'], "other": {"count": config['img_count']},
                "handles": [{"id": "in", "type": "target", "dataType": ["IMAGE", "STRING"], "label": "输入"}, {"id": "image", "type": "source", "dataType": "IMAGE", "label": "输出"}]
            }
        }
    else:
        node = {
            "id": gen_id, "type": "remote-audioGenerationNode", "position": {"x": x, "y": y},
            "data": {
                "label": label, 
                "prompt": text_content, 
                "promptModel": [[{"type": "Write", "text": text_content}]],
                "model": "", "language": "zh-CN", "sampleRate": 24000, "format": "wav", "other": {},
                "handles": [{"id": "in", "type": "target", "dataType": ["IMAGE", "AUDIO", "STRING"], "label": "输入"}, {"id": "audio", "type": "source", "dataType": "AUDIO", "label": "输出"}]
            }
        }
    return node


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
            for i in range(1, MAX_SCENE_COLUMNS + 1):
                prompt = str(row.get(f"场景{i}", "")).strip()
                if prompt:
                    # 仅生成并添加单一节点，不再维护 edges 连线
                    g = create_standalone_node(f"{prefix}-场景{i}", prompt, "image", x=-280, y=current_y, config=config)
                    project["nodes"].append(g)
                    current_y += 220
                    
    if export_type == "all": current_y = max(current_y, 30)
    elif export_type == "script": current_y = 30
        
    if export_type in ["script", "all"]:
        try: script_records = sh.worksheet(TAB_NAME_SCRIPT).get_all_records()
        except: script_records = []
        for index, row in enumerate(script_records):
            fragment_id = f"片段{row.get('序号', index + 1)}"
            
            bg_img = str(row.get("背景图", "")).strip()
            if bg_img:
                g = create_standalone_node(f"{fragment_id}-背景图", bg_img, "image", x=650, y=current_y, config=config)
                project["nodes"].append(g)
                current_y += 220
                
            voice_text = str(row.get("配音文字", "")).strip()
            if voice_text:
                voice_actor = str(row.get("配音人物", "")).strip()
                g = create_standalone_node(f"{fragment_id}-配音-{voice_actor}" if voice_actor else fragment_id, voice_text, "audio", x=650, y=current_y, config=config)
                project["nodes"].append(g)
                current_y += 220
                
            bgm_text = str(row.get("背景音", "")).strip()
            if bgm_text:
                g = create_standalone_node(f"{fragment_id}-背景音", bgm_text, "audio", x=650, y=current_y, config=config)
                project["nodes"].append(g)
                current_y += 220
            
    return json.dumps(project, ensure_ascii=False, indent=2)
# ==============================================================================
# 🖥️ 【五、 Streamlit UI 界面构建 (极简重构版)】
# ==============================================================================

with st.sidebar:
    st.title("🎬 Auto-Studio")
    
    st.markdown("### 🎛️ 节点参数配置")
    st.session_state.node_config['text_model'] = st.text_input("📝 文本模型", value=st.session_state.node_config['text_model'])
    st.session_state.node_config['img_draft_model'] = st.text_input("🎨 生图模型 (草稿用)", value=st.session_state.node_config['img_draft_model'])
    st.session_state.node_config['img_final_model'] = st.text_input("💎 生图模型 (精绘备用)", value=st.session_state.node_config['img_final_model'])
    
    col_size, col_ratio = st.columns(2)
    with col_size: st.session_state.node_config['img_size'] = st.selectbox("画面尺寸", ["1K", "2K", "4K"], index=["1K", "2K", "4K"].index(st.session_state.node_config['img_size']))
    with col_ratio: st.session_state.node_config['img_ratio'] = st.selectbox("画面比例", ["16:9", "9:16", "1:1", "4:3", "3:4"], index=["16:9", "9:16", "1:1", "4:3", "3:4"].index(st.session_state.node_config['img_ratio']))
    st.session_state.node_config['img_count'] = st.number_input("生图张数", min_value=1, max_value=4, value=st.session_state.node_config['img_count'])

    st.markdown("---")
    
    # 【修复】：摒弃乱七八糟的5个按钮，采用极简的单线表单流
    st.markdown("### 📥 导出画布节点 (追加用)")
    st.caption("提示：在下放选择需要的模块，获取后于新标签页打开粘贴。")
    
    with st.form("export_form"):
        export_mode = st.radio("选择导出类型:", ["独立【设定】模块", "独立【剧本】模块", "完整工程 (设定+剧本)"])
        submitted = st.form_submit_button("1. 抓取表格数据", use_container_width=True)
        
        if submitted:
            with st.spinner("正在从表格提取并组装节点..."):
                type_map = {"独立【设定】模块": "settings", "独立【剧本】模块": "script", "完整工程 (设定+剧本)": "all"}
                file_map = {"独立【设定】模块": "Settings", "独立【剧本】模块": "Script", "完整工程 (设定+剧本)": "FullProject"}
                
                st.session_state.dl_data = generate_toonflow_json(st.session_state.node_config, export_type=type_map[export_mode])
                st.session_state.dl_filename = f"Toonflow_{file_map[export_mode]}_{time.strftime('%Y%m%d%H%M')}.json"
                st.success("✅ 数据抓取成功！请点击下方按钮下载。")

    if 'dl_data' in st.session_state:
        st.download_button(
            label=f"2. ⬇️ 下载 {st.session_state.dl_filename}", 
            data=st.session_state.dl_data, 
            file_name=st.session_state.dl_filename, 
            mime="application/json", 
            use_container_width=True, 
            type="primary"
        )

# --- 主界面 ---
with st.expander("🪄 **AI 故事拆解器 (纯追加模式)**", expanded=True):
    raw_draft = st.text_area("粘贴故事草稿：", placeholder="在这里粘贴你的故事大纲或小说段落...", height=150)
    prompt_keys = list(available_prompts.keys())
    
    col_1, col_2, col_3 = st.columns([2, 1, 1])
    with col_1:
        selected_prompt_title = st.selectbox("选择解析指令：", options=prompt_keys, index=0 if prompt_keys else 0)
    with col_2:
        selected_model = st.selectbox("核心大脑模型：", options=list(RECOMMENDED_MODELS.keys()), format_func=lambda x: RECOMMENDED_MODELS[x])
    with col_3:
        st.write(""); st.write("")
        if st.button("✨ 仅在末尾追加填表", type="primary", use_container_width=True):
            if not raw_draft.strip(): st.warning("⚠️ 请先粘贴草稿！")
            elif "错误" in selected_prompt_title or not available_prompts.get(selected_prompt_title, "").strip(): st.warning("⚠️ 请确保 prompt 工作表中有有效数据！")
            else:
                with st.spinner("🤖 正在调用 AI 并在末尾追加行..."):
                    try:
                        p_target = available_prompts[selected_prompt_title]
                        process_and_append_sheets(raw_draft, p_target, selected_model, SHEET_EDIT_URL)
                        st.success("🎉 追加成功！原数据未受任何影响。")
                        time.sleep(1.5); st.rerun()
                    except Exception as e: st.error(f"❌ 失败: {str(e)}")

st.divider()
st.link_button("📋 前往 Google Sheets 手动调整", SHEET_EDIT_URL, use_container_width=True)
st.components.v1.iframe(SHEET_EMBED_URL, height=550, scrolling=True)
