import streamlit as st
import json
import time
import os
import zipfile
from io import BytesIO
import gspread
from google.oauth2.service_account import Credentials

# 关键：导入 gradio_client
from gradio_client import Client

st.set_page_config(page_title="配音控制台", page_icon="🎙️", layout="wide")

# ==============================================================================
# ⚙️ 【全局配置】
# ==============================================================================
SHEET_EDIT_URL  = "https://docs.google.com/spreadsheets/d/1Y8YOEl5G2HV4C3WLZz9ttEEoByuWKGLqh_nmWvcb-_M/edit"
TAB_NAME_SCRIPT = "toonflow-story-Script"

def get_secret(key, default=""):
    try: return st.secrets[key] if key in st.secrets else os.getenv(key, default)
    except Exception: return os.getenv(key, default)

def fetch_script_data():
    gcp_key_str = get_secret("GCP_SA_KEY", "")
    creds = Credentials.from_service_account_info(json.loads(gcp_key_str), scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"])
    gc = gspread.authorize(creds)
    sh = gc.open_by_url(SHEET_EDIT_URL)
    return sh.worksheet(TAB_NAME_SCRIPT).get_all_records()

# ==============================================================================
# 🤖 【核心逻辑：魔塔 Gradio API 调用】
# ==============================================================================
def generate_audio_modelscope(text, voice_id, instruct_text, api_key):
    """
    使用魔塔 Gradio API 生成音频 (自然语言控制模式)
    """
    try:
        # 连接到魔塔空间
        client = Client("https://modelscope.cn/api/v1/studio/iic/CosyVoice2-0.5B/gradio/")
        
        # 发送预测请求，参数严格对应 UI 结构
        result = client.predict(
            text,                 # 待合成文本
            "自然语言控制",         # 切换为自然语言控制模式
            voice_id,             # 发音人 (如 "中文女")
            "",                   # prompt_text (不需要)
            "",                   # prompt_wav (不需要)
            instruct_text,        # 导演指令 (如 "用愤怒的语气")
            api_name="/generate_audio"
        )
        
        temp_audio_path = result[0] if isinstance(result, tuple) else result
        with open(temp_audio_path, "rb") as f:
            audio_bytes = f.read()
        return audio_bytes, True
    except Exception as e:
        return str(e), False

# ==============================================================================
# 🖥️ 【UI 界面渲染】
# ==============================================================================
st.title("🎙️ 魔塔 CosyVoice 配音控制台")
st.markdown("通过魔塔 ModelScope Gradio 接口 (自然语言控制) 生成音频。")

# 替换为魔塔 API Key 验证
MODELSCOPE_API_KEY = st.text_input("🔑 请输入魔塔 API Token", type="password", value=get_secret("MODELSCOPE_API_KEY", ""))

if st.button("📥 从表格读取最新配音脚本", use_container_width=True):
    with st.spinner("正在读取表格数据..."):
        st.session_state.script_data = fetch_script_data()
        st.success("已读取！")

if "script_data" in st.session_state:
    script_records = st.session_state.script_data
    dubbing_lines = [row for row in script_records if str(row.get("配音文字", "")).strip()]
    
    if not dubbing_lines:
        st.info("表格中没有检测到配音文字。")
    else:
        st.markdown("### A. 角色分配音色 ID")
        st.caption("提示：魔塔基础发音人仅支持：中文女, 中文男, 日语男, 粤语女, 英文女, 英文男, 韩语女。")
        
        unique_chars = list(set([str(row.get("配音人物", "旁白")).strip() or "旁白" for row in dubbing_lines]))
        char_voice_map = {}
        map_cols = st.columns(min(len(unique_chars), 3) if len(unique_chars) > 0 else 1)
        
        for i, char in enumerate(unique_chars):
            with map_cols[i % 3]:
                # 默认值改为魔塔的格式
                char_voice_map[char] = st.text_input(f"【{char}】的音色 ID", value="中文女", key=f"voice_map_{char}")

        st.markdown("---")
        st.markdown("### B. 逐句调试与生成 (自然语言控制)")
        
        for idx, row in enumerate(dubbing_lines):
            seq = str(row.get("序号", idx + 1)).strip()
            char = str(row.get("配音人物", "旁白")).strip() or "旁白"
            text = str(row.get("配音文字", "")).strip()
            tone = str(row.get("配音语气", "")).strip() # 这里对应表格里的语气列，将作为导演指令
            
            with st.expander(f"片段 {seq} - {char} : {text[:15]}..."):
                uc1, uc2 = st.columns([3, 1])
                with uc1:
                    target_voice = char_voice_map.get(char, "中文女")
                    # 新增导演指令输入框
                    instruct_prompt = st.text_input("🎭 导演指令 (如：愤怒、开心、快速)", value=tone, key=f"instruct_{idx}")
                    final_prompt = st.text_area("将发送的文本", value=text, key=f"prompt_{idx}", height=68)
                
                with uc2:
                    st.write("") 
                    if st.button("▶️ 试听此句", key=f"play_{idx}", use_container_width=True):
                        if not MODELSCOPE_API_KEY:
                            st.warning("请先输入魔塔 API Token！")
                        else:
                            with st.spinner("队列排队生成中..."):
                                audio_data, success = generate_audio_modelscope(
                                    final_prompt, target_voice, instruct_prompt, MODELSCOPE_API_KEY
                                )
                                if success:
                                    st.session_state[f"audio_{idx}"] = audio_data
                                else:
                                    st.error(f"生成失败: {audio_data}")
                
                if f"audio_{idx}" in st.session_state:
                    st.audio(st.session_state[f"audio_{idx}"], format="audio/wav")

        st.markdown("---")
        st.markdown("### C. 确认无误，一键打包")
        if st.button("📦 按照上述设置批量生成并打包 (ZIP)", type="primary", use_container_width=True):
            if not MODELSCOPE_API_KEY:
                st.warning("请先输入魔塔 API Token！")
            else:
                with st.spinner("后台列队生成中，可能较慢，请勿刷新页面..."):
                    zip_buffer = BytesIO()
                    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
                        for idx, row in enumerate(dubbing_lines):
                            seq = str(row.get("序号", idx + 1)).strip()
                            char = str(row.get("配音人物", "旁白")).strip() or "旁白"
                            
                            target_voice = char_voice_map.get(char, "中文女")
                            final_prompt = st.session_state.get(f"prompt_{idx}", row.get("配音文字", ""))
                            instruct_prompt = st.session_state.get(f"instruct_{idx}", "")
                            
                            audio_data, success = generate_audio_modelscope(
                                final_prompt, target_voice, instruct_prompt, MODELSCOPE_API_KEY
                            )
                            if success:
                                filename = f"片段{seq}_{char}.wav"
                                zip_file.writestr(filename, audio_data)
                            
                            # 添加适当延迟，防止被认定为接口滥用
                            time.sleep(3) 
                    
                    zip_buffer.seek(0)
                    st.session_state["final_audio_zip"] = zip_buffer
                    st.success("打包完成！")
        
        if "final_audio_zip" in st.session_state:
            st.download_button("⬇️ 下载全剧配音包", data=st.session_state["final_audio_zip"], file_name=f"Dubbing_{int(time.time())}.zip", mime="application/zip", use_container_width=True)
