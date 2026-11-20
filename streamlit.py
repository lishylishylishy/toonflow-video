import streamlit as st

st.set_page_config(page_title="Auto Video Studio", page_icon="🎬", layout="wide")

import requests, json, time, os, uuid, re
import urllib.parse
import gspread
from google.oauth2.service_account import Credentials
import zipfile
from io import BytesIO

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

def fetch_script_data():
    gcp_key_str = get_secret("GCP_SA_KEY", "")
    creds = Credentials.from_service_account_info(json.loads(gcp_key_str), scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"])
    gc = gspread.authorize(creds)
    sh = gc.open_by_url(SHEET_EDIT_URL)
    return sh.worksheet(TAB_NAME_SCRIPT).get_all_records()

# ==============================================================================
# 🎨 【二、 Streamlit 初始化】
# ==============================================================================

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

if 'node_config' not in st.session_state:
    st.session_state.node_config = {
        'text_model':'["modelscopeImageProvider", "Qwen/Qwen3.8-27B"]',
        'img_draft_model':'["modelscopeImageProvider", "Tongyi-MAI/Z-Image-Turbo"]',
        'img_final_model':'["modelscopeImageProvider", "Qwen/Qwen-Image-2512"]',
        'img_size': "1K",
        'img_ratio': "9:16",
        'img_count': 1
    }

# ==============================================================================
# 🤖 【三、 核心逻辑：AI 解析与写入 & Toonflow 转换】
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
    try: current_settings = sh.worksheet(TAB_NAME_SETTINGS).get_all_records()
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

def create_standalone_node(label, text_content, gen_type, x, y, config, ref_node_info=None):
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
        "id": gen_id, "type": "remote-imageGenerationNode", "position": {"x": x, "y": y},
        "data": {
            "label": label, "prompt": final_prompt, "promptModel": [prompt_model_items],
            "model": model_str, "size": config['img_size'], "ratio": config['img_ratio'], "other": {},
            "handles": [
                {"id": "in", "type": "target", "dataType": ["IMAGE", "STRING"], "label": "图片、文本输入"}, 
                {"id": "image", "type": "source", "dataType": "IMAGE", "label": "图片输出"}
            ]
        }
    }
    return node, edge_to_add

def generate_toonflow_json(config, export_type="all"):
    gcp_key_str = get_secret("GCP_SA_KEY", "")
    creds = Credentials.from_service_account_info(json.loads(gcp_key_str), scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"])
    gc = gspread.authorize(creds)
    sh = gc.open_by_url(SHEET_EDIT_URL)
    project = {"toonflowCanvas": True, "nodes": [], "edges": [], "viewport": {"x": 450, "y": 450, "zoom": 0.5}}
    current_y = -700
    
    if export_type in ["settings", "all"]:
        try: settings_records = sh.worksheet(TAB_NAME_SETTINGS).get_all_records()
        except: settings_records = []
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
                    node, edge = create_standalone_node(node_label, prompt, "image", node_x, current_y, config, None)
                    scene1_ref = {"id": node["id"], "label": node_label}
                else:
                    node, edge = create_standalone_node(node_label, prompt, "image", node_x, current_y, config, scene1_ref)
                project["nodes"].append(node)
                if edge: project["edges"].append(edge)
            if has_any_scene: current_y += 420

    if export_type in ["script", "all"]:
        try: script_records = sh.worksheet(TAB_NAME_SCRIPT).get_all_records()
        except: script_records = []
        for idx, row in enumerate(script_records, start=1):
            seq = str(row.get("序号", idx)).strip()
            clip_title = str(row.get("片段", "")).strip()
            clip_prefix = f"片段{seq}-{clip_title}" if clip_title else f"片段{seq}"
            node_x, has_clip_node = -280, False
            
            bg_prompt = str(row.get("背景图", "")).strip()
            if bg_prompt:
                node, _ = create_standalone_node(f"{clip_prefix}-背景图", bg_prompt, "image", node_x, current_y, config)
                project["nodes"].append(node)
                node_x += 420
                has_clip_node = True
                
            char_prompt = str(row.get("人物", "")).strip()
            if char_prompt:
                node, _ = create_standalone_node(f"{clip_prefix}-人物", char_prompt, "image", node_x, current_y, config)
                project["nodes"].append(node)
                node_x += 420
                has_clip_node = True
                
            if has_clip_node: current_y += 420
    return json.dumps(project, ensure_ascii=False, indent=2)

# ==============================================================================
# 🖥️ 【四、 UI 界面渲染区】
# ==============================================================================

st.title("🎬 Auto Video Studio")

col1, col2 = st.columns([1, 1.5], gap="large")

with col1:
    st.subheader("1. AI 解析与写入")
    selected_prompt_name = st.selectbox("选择 Prompt", list(available_prompts.keys()) if isinstance(available_prompts, dict) else [])
    current_prompt_text = st.text_area("当前 Prompt", value=available_prompts.get(selected_prompt_name, "") if isinstance(available_prompts, dict) else "")
    selected_model = st.selectbox("选择模型", list(RECOMMENDED_MODELS.keys()), format_func=lambda k: RECOMMENDED_MODELS[k])
    raw_story = st.text_area("输入草稿文案", height=150)
    
    if st.button("🚀 解析并写入", type="primary", use_container_width=True):
        if raw_story.strip():
            process_and_append_sheets(raw_story, current_prompt_text, selected_model, SHEET_EDIT_URL)
            st.success("✅ 写入成功！")
            time.sleep(1)
            st.rerun()

    st.divider()
    st.subheader("2. 导出视觉工程 JSON (Toonflow)")
    c1, c2 = st.columns(2)
    with c1: st.session_state.node_config['img_ratio'] = st.selectbox("比例", ["9:16", "16:9", "1:1", "3:4", "4:3"])
    with c2: st.session_state.node_config['img_size'] = st.selectbox("分辨率", ["1K", "2K"])

    if st.button("生成全剧 JSON", use_container_width=True):
        st.session_state["export_json"] = generate_toonflow_json(st.session_state.node_config, "all")
        st.session_state["export_name"] = f"Toonflow_Visual_{int(time.time())}.json"

    if "export_json" in st.session_state:
        st.download_button(label="⬇️ 下载 JSON 文件", data=st.session_state["export_json"], file_name=st.session_state["export_name"], mime="application/json", type="primary", use_container_width=True)

    st.divider()
    st.subheader("3. 🎙️ CosyVoice 配音控制台")
    
    # CosyVoice API 地址
    COSYVOICE_API_URL = get_secret("COSYVOICE_API_URL", "http://127.0.0.1:50000/tts") 
    
    if st.button("📥 从表格读取最新配音脚本", use_container_width=True):
        st.session_state.script_data = fetch_script_data()
        st.success("已读取！")

    if "script_data" in st.session_state:
        script_records = st.session_state.script_data
        # 过滤出有配音文字的行
        dubbing_lines = [row for row in script_records if str(row.get("配音文字", "")).strip()]
        
        if not dubbing_lines:
            st.info("表格中没有检测到配音文字。")
        else:
            # 步骤 A：角色映射
            st.markdown("##### A. 角色分配音色 ID")
            unique_chars = list(set([str(row.get("配音人物", "旁白")).strip() or "旁白" for row in dubbing_lines]))
            char_voice_map = {}
            map_cols = st.columns(min(len(unique_chars), 3))
            
            for i, char in enumerate(unique_chars):
                with map_cols[i % 3]:
                    # 默认给定一个基础值，用户可以根据本地 CosyVoice 注册的 speaker 修改
                    char_voice_map[char] = st.text_input(f"【{char}】对应的音色", value="中文女", key=f"voice_map_{char}")

            st.markdown("---")
            st.markdown("##### B. 逐句调试与生成")
            
            for idx, row in enumerate(dubbing_lines):
                seq = str(row.get("序号", idx + 1)).strip()
                char = str(row.get("配音人物", "旁白")).strip() or "旁白"
                text = str(row.get("配音文字", "")).strip()
                tone = str(row.get("配音语气", "")).strip()
                
                # 拼接语气词。如果你使用的是 Instruct 模型，可以改成 <|语气|>文本 格式
                # 这里默认展示拼接效果，用户肉眼可见，且可手动在下方编辑框修改
                default_prompt = f"【{tone}】{text}" if tone else text
                
                with st.expander(f"片段 {seq} - {char} : {text[:15]}..."):
                    uc1, uc2 = st.columns([3, 1])
                    with uc1:
                        target_voice = char_voice_map.get(char, "中文女")
                        st.caption(f"当前发音人识别为: **{target_voice}**")
                        # 将最终要发给模型的文本呈现出来，允许用户修改
                        final_prompt = st.text_area("将发送给 CosyVoice 的文本", value=default_prompt, key=f"prompt_{idx}", height=68)
                    
                    with uc2:
                        st.write("") # 占位对齐
                        if st.button("▶️ 试听此句", key=f"play_{idx}", use_container_width=True):
                            with st.spinner("生成中..."):
                                try:
                                    payload = {"text": final_prompt, "speaker": target_voice}
                                    res = requests.post(COSYVOICE_API_URL, json=payload, timeout=30)
                                    if res.status_code == 200:
                                        st.session_state[f"audio_{idx}"] = res.content
                                    else:
                                        st.error(f"报错: {res.status_code}")
                                except Exception as e:
                                    st.error(f"连接失败: {e}")
                    
                    # 如果生成过音频，就在展开面板里显示播放器
                    if f"audio_{idx}" in st.session_state:
                        st.audio(st.session_state[f"audio_{idx}"], format="audio/wav")

            st.markdown("---")
            st.markdown("##### C. 确认无误，一键打包")
            if st.button("📦 按照上述设置批量生成并打包 (ZIP)", type="primary", use_container_width=True):
                with st.spinner("正在后台批量生成所有台词，请耐心等待..."):
                    zip_buffer = BytesIO()
                    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
                        for idx, row in enumerate(dubbing_lines):
                            seq = str(row.get("序号", idx + 1)).strip()
                            char = str(row.get("配音人物", "旁白")).strip() or "旁白"
                            target_voice = char_voice_map.get(char, "中文女")
                            # 从上面对应输入框读取最新修改的文本
                            final_prompt = st.session_state.get(f"prompt_{idx}", row.get("配音文字", ""))
                            
                            payload = {"text": final_prompt, "speaker": target_voice}
                            try:
                                res = requests.post(COSYVOICE_API_URL, json=payload, timeout=60)
                                if res.status_code == 200:
                                    filename = f"片段{seq}_{char}.wav"
                                    zip_file.writestr(filename, res.content)
                            except:
                                pass # 批量打包时忽略单个错误继续执行
                    
                    zip_buffer.seek(0)
                    st.session_state["final_audio_zip"] = zip_buffer
                    st.success("打包完成！")
            
            if "final_audio_zip" in st.session_state:
                st.download_button("⬇️ 下载全剧配音包", data=st.session_state["final_audio_zip"], file_name=f"Dubbing_{int(time.time())}.zip", mime="application/zip", use_container_width=True)

with col2:
    st.subheader("📊 数据表预览")
    st.components.v1.iframe(SHEET_EMBED_URL, height=700, scrolling=True)
