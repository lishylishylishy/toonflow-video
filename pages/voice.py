import base64
from io import BytesIO
import json
import os
import time
import wave
import zipfile
from google.oauth2.service_account import Credentials
import gspread
import requests
import streamlit as st

st.set_page_config(page_title="Gemini AI 配音控制台", page_icon="🎙️", layout="wide")

DEFAULT_SHEET_URL = "https://docs.google.com/spreadsheets/d/1Y8YOEl5G2HV4C3WLZz9ttEEoByuWKGLqh_nmWvcb-_M/edit"
TAB_NAME_SCRIPT = "toonflow-story-Script"

GEMINI_TTS_MODELS = {
    "Gemini 3.8 Flash TTS（10次/天）": "gemini-3.8-flash-tts",
    "Gemini 3.8 Flash-Lite TTS（10次/天）": "gemini-3.8-flash-lite-tts",
    "Gemini 3.1 Flash TTS（10次/天）": "gemini-3.1-flash-tts-preview",
}

GEMINI_VOICES = {
    "Fenrir（男声·低沉）": "Fenrir",
    "Charon（男声·稳重）": "Charon",
    "Puck（男声·爽朗）": "Puck",
    "Orus（男声·冷峻）": "Orus",
    "Kore（女声·清冷）": "Kore",
    "Aoede（女声·温柔）": "Aoede",
    "Leda（女声·甜美）": "Leda",
    "Zephyr（女声·明快）": "Zephyr",
}


def get_config(keys, default=""):
    """优先读取主页面存入 session_state 的配置，其次读取 st.secrets 和环境变量"""
    for k in keys:
        if st.session_state.get(k):
            return st.session_state[k]
        try:
            if k in st.secrets and st.secrets[k]:
                return st.secrets[k]
        except Exception:
            pass
        if os.getenv(k):
            return os.getenv(k)
    return default


def fetch_script_data():
    sheet_url = get_config(
        ["sheet_url", "SHEET_URL", "SHEET_EDIT_URL"], DEFAULT_SHEET_URL
    )
    gcp_key = get_config(["GCP_SA_KEY", "gcp_sa_key"])
    creds_info = (
        json.loads(gcp_key) if isinstance(gcp_key, str) else dict(gcp_key)
    )
    creds = Credentials.from_service_account_info(
        creds_info,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    sh = gspread.authorize(creds).open_by_url(sheet_url)
    return sh.worksheet(TAB_NAME_SCRIPT).get_all_records()


def ensure_wav_bytes(audio_bytes: bytes) -> bytes:
    if audio_bytes[:4] == b"RIFF":
        return audio_bytes
    wav_buffer = BytesIO()
    with wave.open(wav_buffer, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(audio_bytes)
    return wav_buffer.getvalue()


def generate_audio_gemini(text, voice_id, instruct_text, model_id, api_key):
    try:
        clean_instruct = str(instruct_text).strip()
        prompt_text = (
            f"STYLE: {clean_instruct}\n\n#### TRANSCRIPT\n{text}"
            if clean_instruct
            else text
        )

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent"
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        }
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt_text}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {
                        "prebuiltVoiceConfig": {"voiceName": voice_id}
                    }
                },
            },
        }

        resp = requests.post(url, headers=headers, json=payload, timeout=60)
        if resp.status_code != 200:
            return f"HTTP {resp.status_code}: {resp.text}", False

        data = resp.json()
        parts = (
            data.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [])
        )
        for part in parts:
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                raw_bytes = base64.b64decode(inline["data"])
                return ensure_wav_bytes(raw_bytes), True

        return "模型未返回有效音频流。", False
    except Exception as e:
        return str(e), False


st.title("🎙️ Gemini AI 配音控制台")

# 直接读取主页面已配置的 Gemini API Key
GEMINI_API_KEY = get_config([
    "GEMINI_API_KEY",
    "gemini_api_key",
    "GOOGLE_API_KEY",
    "google_api_key",
    "api_key",
])
if not GEMINI_API_KEY:
    st.warning("未检测到主页面或 Secrets 中的 Gemini API Key，请先在主页面配置。")

selected_model_label = st.selectbox(
    "🤖 选择配音模型", options=list(GEMINI_TTS_MODELS.keys())
)
SELECTED_MODEL_ID = GEMINI_TTS_MODELS[selected_model_label]

if st.button("📥 从表格读取最新配音脚本", use_container_width=True):
    with st.spinner("读取中..."):
        st.session_state.script_data = fetch_script_data()
        st.success("已读取！")

if "script_data" in st.session_state:
    dubbing_lines = [
        row
        for row in st.session_state.script_data
        if str(row.get("配音文字", "")).strip()
    ]

    if not dubbing_lines:
        st.info("表格中没有检测到配音文字。")
    else:
        st.markdown("### A. 角色分配音色")
        unique_chars = list(
            dict.fromkeys([
                str(row.get("配音人物", "旁白")).strip() or "旁白"
                for row in dubbing_lines
            ])
        )
        char_voice_map = {}
        voice_labels = list(GEMINI_VOICES.keys())
        map_cols = st.columns(min(len(unique_chars), 3) or 1)

        for i, char in enumerate(unique_chars):
            with map_cols[i % 3]:
                chosen_label = st.selectbox(
                    f"【{char}】的音色",
                    options=voice_labels,
                    key=f"voice_map_{char}",
                )
                char_voice_map[char] = GEMINI_VOICES[chosen_label]

        st.markdown("---")
        st.markdown("### B. 逐句调试与生成")

        for idx, row in enumerate(dubbing_lines):
            seq = str(row.get("序号", idx + 1)).strip()
            char = str(row.get("配音人物", "旁白")).strip() or "旁白"
            text = str(row.get("配音文字", "")).strip()
            tone = str(row.get("配音语气", "")).strip()

            with st.expander(f"片段 {seq} - {char} : {text[:18]}..."):
                uc1, uc2 = st.columns([3, 1])
                with uc1:
                    instruct_prompt = st.text_input(
                        "🎭 导演指令", value=tone, key=f"instruct_{idx}"
                    )
                    final_prompt = st.text_area(
                        "台词文本", value=text, key=f"prompt_{idx}", height=68
                    )
                with uc2:
                    st.write("")
                    if st.button(
                        "▶️ 试听此句", key=f"play_{idx}", use_container_width=True
                    ):
                        if not GEMINI_API_KEY:
                            st.error("缺少 Gemini API Key！")
                        else:
                            with st.spinner("生成中..."):
                                audio_data, success = generate_audio_gemini(
                                    final_prompt,
                                    char_voice_map[char],
                                    instruct_prompt,
                                    SELECTED_MODEL_ID,
                                    GEMINI_API_KEY,
                                )
                                if success:
                                    st.session_state[f"audio_{idx}"] = (
                                        audio_data
                                    )
                                else:
                                    st.error(f"生成失败: {audio_data}")

                if f"audio_{idx}" in st.session_state:
                    st.audio(
                        st.session_state[f"audio_{idx}"], format="audio/wav"
                    )

        st.markdown("---")
        st.markdown("### C. 一键打包")
        sleep_sec = st.number_input(
            "每句间隔（秒，免费版限制 3 次/分，建议 21 秒）",
            min_value=1,
            max_value=60,
            value=21,
        )

        if st.button(
            "📦 批量生成并打包 (ZIP)", type="primary", use_container_width=True
        ):
            if not GEMINI_API_KEY:
                st.error("缺少 Gemini API Key！")
            else:
                progress_bar = st.progress(0)
                status_text = st.empty()
                zip_buffer = BytesIO()

                with zipfile.ZipFile(
                    zip_buffer, "w", zipfile.ZIP_DEFLATED
                ) as zip_file:
                    total = len(dubbing_lines)
                    for idx, row in enumerate(dubbing_lines):
                        seq = str(row.get("序号", idx + 1)).strip()
                        char = str(row.get("配音人物", "旁白")).strip() or "旁白"
                        status_text.text(
                            f"正在生成 ({idx + 1}/{total})：片段 {seq} - {char}"
                        )

                        audio_data, success = generate_audio_gemini(
                            st.session_state.get(
                                f"prompt_{idx}", row.get("配音文字", "")
                            ),
                            char_voice_map[char],
                            st.session_state.get(
                                f"instruct_{idx}", row.get("配音语气", "")
                            ),
                            SELECTED_MODEL_ID,
                            GEMINI_API_KEY,
                        )
                        if success:
                            zip_file.writestr(
                                f"片段{seq}_{char}.wav", audio_data
                            )
                            st.session_state[f"audio_{idx}"] = audio_data
                        else:
                            st.error(f"片段 {seq} 失败：{audio_data}")

                        progress_bar.progress((idx + 1) / total)
                        if idx < total - 1:
                            time.sleep(sleep_sec)

                zip_buffer.seek(0)
                st.session_state["final_audio_zip"] = zip_buffer
                status_text.text("完成！")
                st.success("打包完成！")

        if "final_audio_zip" in st.session_state:
            st.download_button(
                "⬇️ 下载全剧配音包",
                data=st.session_state["final_audio_zip"],
                file_name=f"Gemini_Dubbing_{int(time.time())}.zip",
                mime="application/zip",
                use_container_width=True,
            )
