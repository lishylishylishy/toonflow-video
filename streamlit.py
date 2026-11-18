import streamlit as st
import requests, json, time, os, zipfile, io, re
import gspread
from google.oauth2.service_account import Credentials

# ==============================================================================
# ⚙️ 【一、 全局集中配置区（请在这里修改你的表格链接和配置）】
# ==============================================================================

# 1. Google Sheets 编辑与预览链接
SHEET_EDIT_URL  = "https://docs.google.com/spreadsheets/d/1Y8YOEl5G2HV4C3WLZz9ttEEoByuWKGLqh_nmWvcb-_M/edit"
SHEET_EMBED_URL = "https://docs.google.com/spreadsheets/d/1Y8YOEl5G2HV4C3WLZz9ttEEoByuWKGLqh_nmWvcb-_M/htmlview"

# 2. 目标写入的两个 Tab 名称（AI 会将设定和剧本分别写入这两个 Tab）
TAB_NAME_SETTINGS = "Settings"
TAB_NAME_SCRIPT = "Script"

# 3. 自动化视频脚本的标准表头（请勿随意修改，需与 Prompt 对应）
SETTINGS_HEADERS = ["类型", "名称", "场景", "生图Prompt"]
SCRIPT_HEADERS = [
    "片段时长", "片段", "背景图", "人物", "人物动态", 
    "人物动画", "标题文字", "配音人物", "配音语气", "配音文字", "背景音"
]

# 4. 可用文本模型列表与备选配置（保持你原有的配置）
RECOMMENDED_MODELS = {
    "gemini-3.5-flash-lite": "⚡ gemini-3.5-flash-lite (推荐！RPD: 500次/天, RPM: 15)",
    "gemini-3.1-flash-lite": "⚡ gemini-3.1-flash-lite (额度大！RPD: 500次/天, RPM: 15)",
    "gemini-3.8-flash": "🧠 gemini-3.8-flash (最新性能王！RPD: 20次/天, RPM: 5)",
    "gemini-3.6-flash": "🧠 gemini-3.6-flash (高智能！RPD: 20次/天, RPM: 5)",
    "gemini-3-flash": "🛡️ gemini-3-flash (3代备用！RPD: 20次/天, RPM: 5)",
    "gemini-2.5-flash-lite": "🛡️ gemini-2.5-flash-lite (2.5轻量！RPD: 20次/天, RPM: 10)",
    "gemini-2.5-flash": "🛡️ gemini-2.5-flash (2.5经典！RPD: 20次/天, RPM: 5)"
}

# 5. 安全读取 Streamlit Secrets (兼容本地环境变量)
def get_secret(key, default=""):
    try:
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return os.getenv(key, default)


# ==============================================================================
# 🎨 【二、 Streamlit 页面初始化与 CSS 样式】
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
# 🤖 【三、 AI 文本转双轨脚本 & Google Sheets 动态读写】
# ==============================================================================

def generate_structured_script_via_gemini(raw_text, model_name="gemini-3.5-flash-lite"):
    """
    调用 Gemini API 将草稿格式化为包含 settings 和 script 两个数组的 JSON
    """
    gemini_key = get_secret("GEMINI_API_KEY", "")
    if not gemini_key:
        raise ValueError("未在 Streamlit Secrets 中找到 GEMINI_API_KEY，请先添加！")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={gemini_key}"
    
    prompt = f"""
你是一位专业的动画漫剧导演与分镜师。我需要你将我提供的故事草稿，拆解为自动化工程需要的数据。
你需要输出一个严格的 JSON，包含两个核心数组："settings" (全局资产字典) 和 "script" (分镜执行序列)。

【第一部分：提取全局资产 (settings)】
从草稿中提取所有出现的人物、核心场景和关键物品，为它们建立全局定妆/定景。
字段要求：
1. "类型"：只能是 "人物"、"场景" 或 "物品"。
2. "名称"：如 "林汐"、"亚特兰蒂斯"。
3. "场景"：如 "现代便服"、"繁华的城市"。
4. "生图Prompt"：必须是高质量的纯英文 Prompt。

【第二部分：拆解分镜序列 (script)】
将故事主线拆解为连续的短镜头。
字段要求：
1. "片段时长"：数字，通常 3 到 5 秒。
2. "片段"：导演视角的画面与故事描述。
3. "背景图"：必须完全匹配 settings 中的“名称-场景”（如 "亚特兰蒂斯-繁华的城市"）。
4. "人物"：必须完全匹配 settings 中的“名称-场景”（如 "林汐-现代便服"）。如果没有则留空。
5. "人物动态"：描述人物在画面中的静态特征。
6. "人物动画"：后期视频运镜指令（如 "左平移入画"、"旋转缩小"）。
7. "标题文字"：视频正中显示的压字大标题。
8. "配音人物"：如 "旁白"、"林汐"。
9. "配音语气"：如 "低沉回忆"、"惊恐尖叫"。
10. "配音文字"：该角色的具体台词。
11. "背景音"：如 "玻璃碎裂声"、"脚步声"。

【草稿文案内容】：
{raw_text}

【输出强制要求】：
只输出 JSON 数据，格式如下：
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
        "generationConfig": {"response_mime_type": "application/json"}
    }
    
    # 这里修复了你之前的括号未闭合问题
    res = requests.post(url, headers=headers, json=payload, timeout=60)
    
    if res.status_code != 200:
        raise RuntimeError(f"模型 [{model_name}] 请求失败 [{res.status_code}]: {res.text}")
        
    result_json = res.json()
    text_content = result_json['candidates'][0]['content']['parts'][0]['text']
    
    # 清理可能存在的 Markdown 代码块标记
    text_content = re.sub(r'^```json\s*', '', text_content)
    text_content = re.sub(r'^```\s*', '', text_content)
    text_content = re.sub(r'\s*```$', '', text_content)
    
    parsed_data = json.loads(text_content.strip())
    return parsed_data


def write_project_to_google_sheets(sheet_url, project_data):
    """
    自动在 Google Sheets 中创建/覆写两个 Tab
    """
    gcp_key_str = get_secret("GCP_SA_KEY", "")
    if not gcp_key_str:
        raise ValueError("未在 Streamlit Secrets 中找到 GCP_SA_KEY 密钥！")

    service_account_info = json.loads(gcp_key_str)
    scopes = [
        "[https://www.googleapis.com/auth/spreadsheets](https://www.googleapis.com/auth/spreadsheets)",
        "[https://www.googleapis.com/auth/drive](https://www.googleapis.com/auth/drive)"
    ]
    creds = Credentials.from_service_account_info(service_account_info, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_url(sheet_url)
    
    settings_data = project_data.get("settings", [])
    script_data = project_data.get("script", [])
    
    # --- 1. 写入 Settings 表 ---
    try:
        ws_settings = sh.worksheet(TAB_NAME_SETTINGS)
    except gspread.exceptions.WorksheetNotFound:
        ws_settings = sh.add_worksheet(title=TAB_NAME_SETTINGS, rows="100", cols="20")
        
    rows_settings = [SETTINGS_HEADERS]
    for item in settings_data:
        rows_settings.append([str(item.get(col, "")) for col in SETTINGS_HEADERS])
    ws_settings.clear()
    ws_settings.update(range_name='A1', values=rows_settings)

    # --- 2. 写入 Script 表 ---
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
# 🚀 【四、 GitHub Actions 渲染触发与状态查询】
# ==============================================================================

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
    url = f"[https://api.github.com/repos/](https://api.github.com/repos/){repo}/actions/workflows/render.yml/dispatches"
    payload = {"ref": ref, "inputs": {"sheet_tab": tab_name, "resolution": resolution}}
    
    try:
        # 这里也确保了语法绝对正确
        res = requests.post(url, headers=headers, json=payload, timeout=10)
        if res.status_code == 204:
            return True, "成功"
        else:
            return False, f"GitHub 返回 [{res.status_code}]: {res.text}"
    except Exception as e:
        return False, f"网络请求异常: {str(e)}"

def get_latest_workflow_run():
    token = get_secret("GITHUB_TOKEN", "")
    repo = get_secret("GITHUB_REPOSITORY", get_secret("GITHUB_REPO", ""))
    if not token or not repo or repo == "your_username/auto-video-maker":
        return None
    headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}"}
    url = f"[https://api.github.com/repos/](https://api.github.com/repos/){repo}/actions/runs?per_page=1"
    try:
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code == 200:
            data = res.json()
            if data.get("workflow_runs"):
                return data["workflow_runs"][0]
    except Exception:
        pass
    return None

@st.cache_data(ttl=600, show_spinner=False)
def fetch_rendered_video_bytes(run_id):
    token = get_secret("GITHUB_TOKEN", "")
    repo = get_secret("GITHUB_REPOSITORY", get_secret("GITHUB_REPO", ""))
    if not token or not repo: return None
    headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}"}
    artifacts_url = f"[https://api.github.com/repos/](https://api.github.com/repos/){repo}/actions/runs/{run_id}/artifacts"
    try:
        res = requests.get(artifacts_url, headers=headers, timeout=10)
        if res.status_code != 200: return None
        artifacts = res.json().get("artifacts", [])
        target_art = next((art for art in artifacts if art.get("name") == "Rendered-Video-Output"), None)
        if not target_art: return None
        zip_res = requests.get(target_art.get("archive_download_url"), headers=headers, timeout=30)
        if zip_res.status_code != 200: return None
        with zipfile.ZipFile(io.BytesIO(zip_res.content)) as z:
            for filename in z.namelist():
                if filename.endswith(".mp4"):
                    return z.read(filename)
    except Exception as e:
        print(f"提取视频失败: {e}")
    return None


# ==============================================================================
# 📌 【五、 侧边控制面板 UI 界面构建】
# ==============================================================================

with st.sidebar:
    st.title("🎬 Auto-Studio")
    st.markdown("---")
    
    st.info(f"🎯 当前工作流已绑定读取 【{TAB_NAME_SETTINGS}】 和 【{TAB_NAME_SCRIPT}】。")
    video_resolution = st.text_input("视频分辨率", value="1920x1080")
    
    if st.button("🚀 Start Rendering", type="primary", use_container_width=True):
        # 触发渲染时，默认传脚本表名过去
        ok, msg = trigger_github_workflow(TAB_NAME_SCRIPT, video_resolution)
        if ok:
            st.cache_data.clear()
            st.success("✅ 云端渲染任务已启动！")
            time.sleep(1)
            st.rerun()
        else:
            st.error(f"❌ 启动失败：{msg}")

    st.markdown("---")
    st.subheader("📡 Process Status")
    
    run_info = get_latest_workflow_run()
    if run_info:
        status = run_info.get("status")
        conclusion = run_info.get("conclusion")
        run_id = run_info.get("id")
        
        if status in ["in_progress", "queued"]:
            st.info(f"⏳ **云端渲染中...** ({status})")
            st.progress(50)
            if st.button("🔄 刷新进度", use_container_width=True):
                st.rerun()
        elif status == "completed":
            if conclusion == "success":
                st.success("🎉 **视频渲染成功！**")
                with st.spinner("📥 正在加载视频预览窗口..."):
                    video_bytes = fetch_rendered_video_bytes(run_id)
                if video_bytes:
                    st.video(video_bytes)
                    st.download_button("⬇️ 下载视频", data=video_bytes, file_name="final_output.mp4", mime="video/mp4", type="primary", use_container_width=True)
                else:
                    st.warning("⚠️ 视频文件提取中，请稍等片刻后刷新。")
                    if st.button("🔄 重新拉取", use_container_width=True):
                        st.cache_data.clear()
                        st.rerun()
            else:
                st.error(f"❌ 渲染失败，结论: {conclusion}")
    else:
        st.caption("暂无云端运行记录。")


# ==============================================================================
# 📊 【六、 主界面 UI：AI 智能剧本生成器 + 表格预览】
# ==============================================================================

with st.expander("🪄 **AI Toonflow 剧本生成器（草稿 ➔ 自动提取 Settings 并新建 Script）**", expanded=True):
    raw_draft = st.text_area("粘贴故事草稿：", placeholder="2026年的最后一次日食，林汐在实验室触碰了...", height=150)
    
    col_1, col_2 = st.columns([3, 1])
    with col_1:
        selected_model_key = st.selectbox(
            "选择 Gemini 文本模型（默认推荐高额度模型）：",
            options=list(RECOMMENDED_MODELS.keys()),
            format_func=lambda x: RECOMMENDED_MODELS[x]
        )
    with col_2:
        st.write("")
        st.write("")
        gen_btn = st.button("✨ 一键解析并写入双表", type="primary", use_container_width=True)
        
    if gen_btn:
        if not raw_draft.strip():
            st.warning("⚠️ 请先粘贴文案草稿！")
        else:
            with st.spinner(f"🤖 正在调用 [{selected_model_key}] 拆解资产和分镜..."):
                try:
                    # 1. 自动生成包含双表数据的 JSON
                    project_data = generate_structured_script_via_gemini(raw_draft, model_name=selected_model_key)
                    # 2. 自动写入到配置好的两个 Tab 中
                    write_project_to_google_sheets(SHEET_EDIT_URL, project_data)
                    
                    st.success(f"🎉 解析成功！已自动写入 【{TAB_NAME_SETTINGS}】和【{TAB_NAME_SCRIPT}】 工作表。")
                    time.sleep(1.5)
                    st.rerun()
                except Exception as e:
                    st.error(f"❌ 生成或写入失败: {str(e)}")

st.divider()

col_title, col_btn = st.columns([3, 1], vertical_alignment="center")
with col_btn:
    st.link_button("📋前往 Google Sheets 编辑", SHEET_EDIT_URL, use_container_width=True)

st.components.v1.iframe(SHEET_EMBED_URL, height=520, scrolling=True)
