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
TAB_NAME_PROMPT = "prompt" # 新增：存放 Prompt 的表格名称

# 本地记忆配置文件路径（用于记住你的导出目录）
LOCAL_CONFIG_FILE = ".studio_config.json"

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

def load_saved_export_dir():
    """读取上次记住的本地导出路径"""
    default_dir = os.path.join(os.path.expanduser("~"), "Downloads")
    try:
        if os.path.exists(LOCAL_CONFIG_FILE):
            with open(LOCAL_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("export_dir", default_dir)
    except Exception:
        pass
    return default_dir

def save_export_dir(path):
    """永久记住本地导出路径"""
    try:
        with open(LOCAL_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"export_dir": path}, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
        
@st.cache_data(ttl=60) # 缓存60秒，避免频繁读取表格
def fetch_prompts_from_sheet():
    """从 Google Sheets 读取 prompt 配置"""
    gcp_key_str = get_secret("GCP_SA_KEY", "")
    if not gcp_key_str:
        return {"错误": "未配置 GCP_SA_KEY"}
    try:
        creds = Credentials.from_service_account_info(json.loads(gcp_key_str), scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"])
        gc = gspread.authorize(creds)
        sh = gc.open_by_url(SHEET_EDIT_URL)
        ws_prompt = sh.worksheet(TAB_NAME_PROMPT)
        records = ws_prompt.get_all_values()
        
        prompts = {}
        for row in records:
            if len(row) >= 2 and row[0].strip(): # 确保 A 列有标题
                prompts[row[0].strip()] = row[1].strip()
        
        if not prompts:
            return {"默认": "请在 prompt 表格中添加数据。"}
        return prompts
    except Exception as e:
        return {"读取错误": f"读取 prompt 表格失败: {e}"}

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

# 获取动态 Prompts
available_prompts = fetch_prompts_from_sheet()

# 初始化 session state 用于存储参数控制
if 'node_config' not in st.session_state:
    st.session_state.node_config = {
        'img_model': "", # 默认为空，使用内置
        'img_size': "1K",
        'img_ratio': "16:9",
        'img_count': 1
    }
if "export_dir" not in st.session_state:
    st.session_state.export_dir = load_saved_export_dir()


# ==============================================================================
# 🤖 【三、 核心逻辑：AI 解析与双表写入】
# ==============================================================================

def process_and_update_sheets(raw_text, selected_prompt_text, model_name, sheet_url):
    gcp_key_str = get_secret("GCP_SA_KEY", "")
    if not gcp_key_str:
        raise ValueError("未在 Streamlit Secrets 中找到 GCP_SA_KEY 密钥！")
    
    creds = Credentials.from_service_account_info(json.loads(gcp_key_str), scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"])
    gc = gspread.authorize(creds)
    sh = gc.open_by_url(sheet_url)
    
    final_prompt = f"{selected_prompt_text}\n\n【草稿文案内容】：\n{raw_text}"
    
    # 调用 Gemini API
    gemini_key = get_secret("GEMINI_API_KEY", "")
    if not gemini_key:
        raise ValueError("未在 Streamlit Secrets 中找到 GEMINI_API_KEY 密钥！")

    payload = {
        "contents": [{"parts": [{"text": final_prompt}]}],
        "generationConfig": {"response_mime_type": "application/json"}
    }

    candidate_models = [model_name] + [m for m in RECOMMENDED_MODELS.keys() if m != model_name]
    res = None

    for current_model in candidate_models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{current_model}:generateContent?key={gemini_key}"
        for attempt in range(3):
            res = requests.post(url, headers={"Content-Type": "application/json"}, json=payload, timeout=90)
            if res.status_code == 200:
                break
            if res.status_code in (429, 503):
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(f"模型 [{current_model}] 请求失败 ({res.status_code}): {res.text}")
        
        if res is not None and res.status_code == 200:
            break

    if res is None or res.status_code != 200:
        raise RuntimeError(f"所有备用模型均繁忙，请稍后再试。最后报错: {res.text if res else '无响应'}")
        
    text_content = res.json()['candidates'][0]['content']['parts'][0]['text']
    
    # 暴力提取 JSON
    start_idx = text_content.find('{')
    end_idx = text_content.rfind('}')
    clean_json = text_content[start_idx:end_idx+1] if start_idx != -1 and end_idx != -1 else text_content
    project_data = json.loads(clean_json)

    # 智能提取数据
    settings_data = (
        project_data.get("settings") or 
        project_data.get("Settings") or 
        project_data.get("toonflow-story-Settings") or 
        []
    )
    script_data = (
        project_data.get("script") or 
        project_data.get("Script") or 
        project_data.get("toonflow-story-Script") or 
        []
    )

    if not settings_data and not script_data:
        keys_found = list(project_data.keys()) if isinstance(project_data, dict) else type(project_data).__name__
        raise ValueError(f"未解析出有效数据，AI 实际返回的 JSON 键名为：{keys_found}，内容片段：\n{clean_json[:400]}")

    # 写入 Settings 表 (不再需要保护第一行)
    try:
        ws_settings = sh.worksheet(TAB_NAME_SETTINGS)
    except gspread.exceptions.WorksheetNotFound:
        ws_settings = sh.add_worksheet(title=TAB_NAME_SETTINGS, rows="100", cols="20")
        
    rows_settings = [SETTINGS_HEADERS]
    for item in settings_data:
        rows_settings.append([str(item.get(col, "")) for col in SETTINGS_HEADERS])
    ws_settings.clear()
    ws_settings.update(range_name='A1', values=rows_settings)

    # 写入 Script 表
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
# 🧩 【四、 Toonflow 节点工程转换器】
# ==============================================================================

def create_node_pair(label, text_content, gen_type, x, y, config):
    text_id = str(uuid.uuid4())
    gen_id = str(uuid.uuid4())
    
    text_node = {
        "id": text_id,
        "type": "remote-textNode",
        "initialized": False,
        "position": {"x": x, "y": y},
        "data": {
            "label": label,
            "handles": [
                {"id": "in", "type": "target", "dataType": ["VIDEO", "IMAGE", "STRING", "FILE"], "label": "输入"},
                {"id": "text", "type": "source", "dataType": "STRING", "label": "输出"}
            ],
            "outputs": {},
            "model": "[\"deepSeek\",\"deepseek-flash\"]",
            "textPath": f"assets/{text_id}/content.md",
            "promptModel": [[{"type": "Write", "text": text_content}]]
        }
    }

    if gen_type == "image":
        gen_node = {
            "id": gen_id,
            "type": "remote-imageGenerationNode",
            "initialized": False,
            "position": {"x": x + 360, "y": y},
            "data": {
                "label": label,
                "prompt": text_content, # 自动带入 prompt
                "promptModel": [[{"type": "Write", "text": text_content}]],
                "inpaintPrompt": "",
                "inpaintPromptModel": [],
                "model": config['img_model'],
                "size": config['img_size'],
                "ratio": config['img_ratio'],
                "other": {"count": config['img_count']},
                "handles": [
                    {"id": "in", "type": "target", "dataType": ["IMAGE", "STRING"], "label": "输入"},
                    {"id": "image", "type": "source", "dataType": "IMAGE", "label": "输出"}
                ],
                "outputs": {}
            }
        }
    else:
        gen_node = {
            "id": gen_id,
            "type": "remote-audioGenerationNode",
            "initialized": False,
            "position": {"x": x + 360, "y": y},
            "data": {
                "label": label,
                "prompt": text_content,
                "promptModel": [[{"type": "Write", "text": text_content}]],
                "model": "",
                "language": "zh-CN",
                "sampleRate": 24000,
                "format": "wav",
                "other": {},
                "handles": [
                    {"id": "in", "type": "target", "dataType": ["IMAGE", "AUDIO", "STRING"], "label": "输入"},
                    {"id": "audio", "type": "source", "dataType": "AUDIO", "label": "输出"}
                ],
                "outputs": {}
            }
        }

    edge = {
        "id": f"vueflow__edge-{text_id}text-{gen_id}in",
        "type": "simplebezier",
        "source": text_id,
        "target": gen_id,
        "sourceHandle": "text",
        "targetHandle": "in",
        "updatable": False,
        "selectable": False,
        "focusable": False,
        "data": {},
        "label": "",
        "interactionWidth": 40,
        "animated": False
    }
    return text_node, gen_node, edge


def generate_toonflow_json(config):
    gcp_key_str = get_secret("GCP_SA_KEY", "")
    creds = Credentials.from_service_account_info(
        json.loads(gcp_key_str),
        scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_url(SHEET_EDIT_URL)
    
    # 假设表头在第一行
    settings_records = sh.worksheet(TAB_NAME_SETTINGS).get_all_records()
    script_records = sh.worksheet(TAB_NAME_SCRIPT).get_all_records()
    
    project = {
        "toonflowCanvas": True,
        "nodes": [],
        "edges": [],
        "viewport": {
            "x": 450,
            "y": 450,
            "zoom": 0.5
        }
    }
    current_y = -700
    
    for row in settings_records:
        item_type = str(row.get("类型", "")).strip()
        name = str(row.get("名称", "")).strip()
        if not name:
            continue
        prefix = f"{item_type}-{name}" if item_type else name
        for i in range(1, MAX_SCENE_COLUMNS + 1):
            scene_key = f"场景{i}"
            prompt = str(row.get(scene_key, "")).strip()
            if prompt:
                t, g, e = create_node_pair(f"{prefix}-{scene_key}", prompt, "image", x=-280, y=current_y, config=config)
                project["nodes"].extend([t, g])
                project["edges"].append(e)
                current_y += 220
                
    current_y = max(current_y, 30) 
    
    for index, row in enumerate(script_records):
        fragment_id = f"片段{row.get('序号', index + 1)}"
        
        bg_img = str(row.get("背景图", "")).strip()
        if bg_img:
            t, g, e = create_node_pair(f"{fragment_id}-背景图", bg_img, "image", x=650, y=current_y, config=config)
            project["nodes"].extend([t, g])
            project["edges"].append(e)
            current_y += 220
            
        voice_text = str(row.get("配音文字", "")).strip()
        if voice_text:
            voice_actor = str(row.get("配音人物", "")).strip()
            voice_label = f"{fragment_id}-配音-{voice_actor}" if voice_actor else fragment_id
            t, g, e = create_node_pair(voice_label, voice_text, "audio", x=650, y=current_y, config=config)
            project["nodes"].extend([t, g])
            project["edges"].append(e)
            current_y += 220
            
        bgm_text = str(row.get("背景音", "")).strip()
        if bgm_text:
            t, g, e = create_node_pair(f"{fragment_id}-背景音", bgm_text, "audio", x=650, y=current_y, config=config)
            project["nodes"].extend([t, g])
            project["edges"].append(e)
            current_y += 220
            
    return json.dumps(project, ensure_ascii=False, indent=2)


# ==============================================================================
# 🚀 【五、 GitHub Actions 云端渲染触发器】
# ==============================================================================

def trigger_github_workflow(tab_name, resolution):
    token = get_secret("GITHUB_TOKEN", ""); repo = get_secret("GITHUB_REPOSITORY", "")
    if not token or not repo: return False, "缺少 GitHub Secrets 配置"
    headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}"}
    url = f"https://api.github.com/repos/{repo}/actions/workflows/render.yml/dispatches"
    try:
        res = requests.post(url, headers=headers, json={"ref": get_secret("GITHUB_REF", "main"), "inputs": {"sheet_tab": tab_name, "resolution": resolution}}, timeout=10)
        return (True, "成功") if res.status_code == 204 else (False, f"失败 [{res.status_code}]")
    except Exception as e:
        return False, str(e)


# ==============================================================================
# 🖥️ 【六、 Streamlit UI 界面构建】
# ==============================================================================

# --- 侧边栏：参数配置和导出 ---
with st.sidebar:
    st.title("🎬 Auto-Studio")
    
    st.markdown("### 🎛️ 节点参数配置")
    st.session_state.node_config['img_model'] = st.text_input("生图模型(留空用默认)", value=st.session_state.node_config['img_model'], help='如果要强制使用指定模型，请填入对应标识，如 ["modelscopeImageProvider","Tongyi-MAI/Z-Image-Turbo"]')
    
    col_size, col_ratio = st.columns(2)
    with col_size:
        st.session_state.node_config['img_size'] = st.selectbox("画面尺寸", ["1K", "2K", "4K"], index=["1K", "2K", "4K"].index(st.session_state.node_config['img_size']))
    with col_ratio:
        st.session_state.node_config['img_ratio'] = st.selectbox("画面比例", ["16:9", "9:16", "1:1", "4:3", "3:4"], index=["16:9", "9:16", "1:1", "4:3", "3:4"].index(st.session_state.node_config['img_ratio']))
    
    st.session_state.node_config['img_count'] = st.number_input("生图张数", min_value=1, max_value=4, value=st.session_state.node_config['img_count'])

    st.markdown("---")
    
    st.markdown("### 📥 导出 Toonflow 工程")
    custom_dir = st.text_input(
        "本地保存路径（自动记住）：",
        value=st.session_state.export_dir,
        placeholder=r"例如: D:\Toonflow\Projects"
    )
    if custom_dir.strip() and custom_dir != st.session_state.export_dir:
        st.session_state.export_dir = custom_dir.strip()
        save_export_dir(custom_dir.strip())

    if st.button("🔄 生成并保存到本地", use_container_width=True):
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
                    with open(full_path, "w", encoding="utf-8") as f:
                        f.write(tf_json)
                    st.success(f"✅ 已直接覆盖保存至：\n`{full_path}`")
                except Exception as io_err:
                    st.warning(f"⚠️ 无法直接写入本地路径，请点击下方按钮手动下载：{io_err}")

                st.download_button(
                    label=f"⬇️ 浏览器下载 {dynamic_filename}", 
                    data=tf_json, 
                    file_name=dynamic_filename, 
                    mime="application/json", 
                    type="primary", 
                    use_container_width=True
                )
            except Exception as e:
                st.error(f"生成失败: {e}")
                
    st.markdown("---")
    st.markdown("### 🚀 云端自动渲染")
    video_resolution = st.text_input("视频分辨率", value="1920x1080")
    if st.button("▶️ Start Rendering", use_container_width=True):
        ok, msg = trigger_github_workflow(TAB_NAME_SCRIPT, video_resolution)
        if ok: st.success("✅ 云端任务已启动！")
        else: st.error(f"❌ 启动失败：{msg}")

# --- 主界面 ---
with st.expander("🪄 **AI 故事拆解器 (文本草稿 ➔ 填充双表)**", expanded=True):
    raw_draft = st.text_area("粘贴故事草稿：", placeholder="2026年的最后一次日食...", height=150)
    
    col_1, col_2, col_3 = st.columns([2, 1, 1])
    with col_1:
        selected_prompt_title = st.selectbox("选择 Prompt 模板：", options=list(available_prompts.keys()))
        selected_prompt_text = available_prompts.get(selected_prompt_title, "")
    with col_2:
        selected_model = st.selectbox("选择 Gemini 模型：", options=list(RECOMMENDED_MODELS.keys()), format_func=lambda x: RECOMMENDED_MODELS[x])
    with col_3:
        st.write(""); st.write("")
        if st.button("✨ 一键解析并写入表格", type="primary", use_container_width=True):
            if not raw_draft.strip():
                st.warning("⚠️ 请先粘贴草稿！")
            elif "错误" in selected_prompt_title or "默认" in selected_prompt_title and not selected_prompt_text.strip():
                st.warning("⚠️ 请确保 prompt 工作表中有有效数据！")
            else:
                with st.spinner("🤖 正在调用AI并写入表格..."):
                    try:
                        process_and_update_sheets(raw_draft, selected_prompt_text, selected_model, SHEET_EDIT_URL)
                        st.success("🎉 解析成功！表格已更新。")
                        time.sleep(1.5)
                        st.rerun()
                    except Exception as e:
                        st.error(f"❌ 失败: {str(e)}")

st.divider()
st.link_button("📋前往 Google Sheets 编辑", SHEET_EDIT_URL, use_container_width=True)
st.components.v1.iframe(SHEET_EMBED_URL, height=520, scrolling=True)
