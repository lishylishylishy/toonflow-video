import streamlit as st
import requests
import json
import time
import os
import zipfile
from io import BytesIO
import gspread
from google.oauth2.service_account import Credentials

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
# 🖥️ 【UI 界面渲染】
# ==============================================================================
st.title("🎙️ CosyVoice 配音控制台")
st.markdown("通过阿里云 DashScope 接口批量生成音频。")

# 云端 API 设置
COSYVOICE_API_URL = "https://dashscope.aliyuncs.com/api/v1/services/audio/text-to-speech/text-to-speech"
COSYVOICE_API_KEY = st.text_input("🔑 请输入阿里云 API Key (Bearer Token)", type="password", value=get_secret("COSYVOICE_API_KEY", ""))

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
        st.caption("提示：阿里云可用免费 ID 包括 longxiaochun(亲切女), longwan(知性女), longcheng(沉稳男), loongji(苍老男) 等。")
        
        unique_chars = list(set([str(row.get("配音人物", "旁白")).strip() or "旁白" for row in dubbing_lines]))
        char_voice_map = {}
        map_cols = st.columns(min(len(unique_chars), 3) if len(unique_chars) > 0 else 1)
        
        for i, char in enumerate(unique_chars):
            with map_cols[i % 3]:
                char_voice_map[char] = st.text_input(f"【{char}】的音色 ID", value="longxiaochun", key=f"voice_map_{char}")

        st.markdown("---")
        st.markdown("### B. 逐句调试与生成")
        
        for idx, row in enumerate(dubbing_lines):
            seq = str(row.get("序号", idx + 1)).strip()
            char = str(row.get("配音人物", "旁白")).strip() or "旁白"
            text = str(row.get("配音文字", "")).strip()
            tone = str(row.get("配音语气", "")).strip()
            
            default_prompt = f"<{tone}>{text}" if tone else text
            
            with st.expander(f"片段 {seq} - {char} : {text[:15]}..."):
                uc1, uc2 = st.columns([3, 1])
                with uc1:
                    target_voice = char_voice_map.get(char, "longxiaochun")
                    st.caption(f"当前发音人识别为: **{target_voice}**")
                    final_prompt = st.text_area("将发送的文本", value=default_prompt, key=f"prompt_{idx}", height=68)
                
                with uc2:
                    st.write("") 
                    if st.button("▶️ 试听此句", key=f"play_{idx}", use_container_width=True):
                        if not COSYVOICE_API_KEY:
                            st.warning("请先在最上方输入 API Key！")
                        else:
                            with st.spinner("生成中..."):
                                headers = {
                                    "Authorization": f"Bearer {COSYVOICE_API_KEY}",
                                    "Content-Type": "application/json"
                                }
                                payload = {
                                    "model": "cosyvoice-v1",
                                    "input": {"text": final_prompt},
                                    "parameters": {"voice": target_voice}
                                }
                                try:
                                    res = requests.post(COSYVOICE_API_URL, headers=headers, json=payload, timeout=30)
                                    if res.status_code == 200:
                                        st.session_state[f"audio_{idx}"] = res.content
                                    else:
                                        st.error(f"报错 {res.status_code}: {res.text}")
                                except Exception as e:
                                    st.error(f"连接失败: {e}")
                
                if f"audio_{idx}" in st.session_state:
                    st.audio(st.session_state[f"audio_{idx}"], format="audio/wav")

        st.markdown("---")
        st.markdown("### C. 确认无误，一键打包")
        if st.button("📦 按照上述设置批量生成并打包 (ZIP)", type="primary", use_container_width=True):
            if not COSYVOICE_API_KEY:
                st.warning("请先在最上方输入 API Key！")
            else:
                with st.spinner("正在后台批量生成所有台词，请耐心等待..."):
                    zip_buffer = BytesIO()
                    headers = {
                        "Authorization": f"Bearer {COSYVOICE_API_KEY}",
                        "Content-Type": "application/json"
                    }
                    
                    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
                        for idx, row in enumerate(dubbing_lines):
                            seq = str(row.get("序号", idx + 1)).strip()
                            char = str(row.get("配音人物", "旁白")).strip() or "旁白"
                            target_voice = char_voice_map.get(char, "longxiaochun")
                            final_prompt = st.session_state.get(f"prompt_{idx}", row.get("配音文字", ""))
                            
                            payload = {
                                "model": "cosyvoice-v1",
                                "input": {"text": final_prompt},
                                "parameters": {"voice": target_voice}
                            }
                            try:
                                res = requests.post(COSYVOICE_API_URL, headers=headers, json=payload, timeout=60)
                                if res.status_code == 200:
                                    filename = f"片段{seq}_{char}.wav"
                                    zip_file.writestr(filename, res.content)
                            except:
                                pass
                    
                    zip_buffer.seek(0)
                    st.session_state["final_audio_zip"] = zip_buffer
                    st.success("打包完成！")
        
        if "final_audio_zip" in st.session_state:
            st.download_button("⬇️ 下载全剧配音包", data=st.session_state["final_audio_zip"], file_name=f"Dubbing_{int(time.time())}.zip", mime="application/zip", use_container_width=True)
