import base64
from io import BytesIO
import json
import os
import re
import time
import wave
import zipfile
from google.oauth2.service_account import Credentials
import gspread
import requests
import streamlit as st
st.set_page_config(page_title="Gemini AI 配音控制台", page_icon="🎙️", layout="wide")

import pandas as pd
def _cell_a1(row: int, col: int) -> str:
    letters = ""
    while col > 0:
        col, rem = divmod(col - 1, 26)
        letters = chr(65 + rem) + letters
    return f"{letters}{row}"

def update_segment_durations(group_items: list[dict], wav_bytes: bytes):
    """测量生成音频的真实秒数，只更新【片段时长】一列（同步内存 + 写回 Google 表格）"""
    real_sec = round(get_wav_duration(wav_bytes), 2)
    if real_sec <= 0 or "script_data" not in st.session_state:
        return

    # 1. 计算每一行应填的时长（单行填 100% 真实时长；多行合并按字数比例分摊总时长）
    if len(group_items) == 1:
        durations = [real_sec]
    else:
        weights = [max(1, len(str(it["text"]).strip())) for it in group_items]
        total_w = sum(weights)
        durations = [round(real_sec * (w / total_w), 2) for w in weights]

    # 2. 只修改内存数据里的「片段时长」字段，其他列原封不动
    for it, dur in zip(group_items, durations):
        st.session_state.script_data[it["raw_idx"]]["片段时长"] = dur

    # 同步给 pages/sfx.py
    st.session_state["shared_script_df"] = pd.DataFrame(st.session_state.script_data)

    # 3. 精准写回 Google 表格的「片段时长」这一列（不碰其他任何列）
    try:
        sheet_url = get_config(["sheet_url", "SHEET_URL", "SHEET_EDIT_URL"], DEFAULT_SHEET_URL)
        gcp_key = get_config(["GCP_SA_KEY", "gcp_sa_key"])
        creds_info = json.loads(gcp_key) if isinstance(gcp_key, str) else dict(gcp_key)
        creds = Credentials.from_service_account_info(
            creds_info,
            scopes=[
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive",
            ],
        )
        ws = gspread.authorize(creds).open_by_url(sheet_url).worksheet(TAB_NAME_SCRIPT)
        headers = ws.row_values(1)
        if "片段时长" in headers:
            dur_col = headers.index("片段时长") + 1
            updates = [
                {
                    "range": _cell_a1(it["raw_idx"] + 2, dur_col),  # 第1行是表头，数据从第2行开始
                    "values": [[dur]],
                }
                for it, dur in zip(group_items, durations)
            ]
            ws.batch_update(updates)
    except Exception as e:
        st.warning(f"音频已生成，但同步写入 Google 表格「片段时长」失败：{e}")

DEFAULT_SHEET_URL = "https://docs.google.com/spreadsheets/d/1Y8YOEl5G2HV4C3WLZz9ttEEoByuWKGLqh_nmWvcb-_M/edit"
TAB_NAME_SCRIPT = "toonflow-story-Script"

GEMINI_TTS_MODELS = {
    "Gemini 3.8 Flash TTS（旗舰演技｜10次/天）": "gemini-3.8-flash-tts",
    "Gemini 3.8 Flash-Lite TTS（高速轻量｜10次/天）": (
        "gemini-3.8-flash-lite-tts"
    ),
    "Gemini 3.1 Flash TTS（经典备用｜10次/天）": (
        "gemini-3.1-flash-tts-preview"
    ),
}

GEMINI_VOICES = {
    # 男声 (17款)
    "Fenrir（男·低沉霸总 / 质感男主）": "Fenrir",
    "Charon（男·沉稳厚重 / 悬疑旁白）": "Charon",
    "Puck（男·年轻爽朗 / 少年男主）": "Puck",
    "Orus（男·冷峻果断 / 反派精英）": "Orus",
    "Enceladus（男·低哑气声 / 隐忍深情）": "Enceladus",
    "Iapetus（男·清朗干净 / 阳光青年）": "Iapetus",
    "Umbriel（男·随和松弛 / 幽默损友）": "Umbriel",
    "Algieba（男·醇厚顺滑 / 温柔绅士）": "Algieba",
    "Algenib（男·粗犷沙哑 / 老年长辈·反派）": "Algenib",
    "Rasalgethi（男·专业播音 / 剧情解说）": "Rasalgethi",
    "Alnilam（男·自信刚毅 / 战神硬汉）": "Alnilam",
    "Schedar（男·平稳内敛 / 成熟男配）": "Schedar",
    "Pulcherrima（男·张扬高调 / 纨绔少爷）": "Pulcherrima",
    "Achird（男·亲切温和 / 暖男学长）": "Achird",
    "Zubenelgenubi（男·慵懒随性 / 痞帅男主）": "Zubenelgenubi",
    "Sadachbia（男·生动跳脱 / 搞笑担当）": "Sadachbia",
    "Sadaltager（男·博学老成 / 师尊长老）": "Sadaltager",
    # 女声 (13款)
    "Kore（女·坚定）": "Kore",
    "Aoede（女·知性）": "Aoede",
    "Leda（女·闺蜜）": "Leda",
    "Zephyr（女·自然）": "Zephyr",
    "Gacrux（女·成熟）": "Gacrux",
    "Vindemiatrix（女·白月光）": "Vindemiatrix",
    "Achernar（女·温婉）": "Achernar",
    "Despina（女·豪门）": "Despina",
    "Erinome（女·干练）": "Erinome",
    "Callirrhoe（女·邻家）": "Callirrhoe",
    "Autonoe（女·少女）": "Autonoe",
    "Laomedeia（女·刁蛮）": "Laomedeia",
    "Sulafat（女·贤惠）": "Sulafat",
}


def get_config(keys, default=""):
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


def safe_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", str(name).strip())


def get_wav_duration(wav_bytes: bytes) -> float:
    try:
        with wave.open(BytesIO(wav_bytes), "rb") as wf:
            return wf.getnframes() / float(wf.getframerate())
    except Exception:
        return 0.0


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


def group_consecutive_by_speaker(segments: list[dict]) -> list[dict]:
    """
    核心分组逻辑：前后连续为同一配音人物的片段合并为一组；一旦换人立即切断开启新组。
    每组只发 1 次 API 请求，直接生成一个完整音频文件，不做任何音频裁剪。
    """
    groups = []
    for seg in segments:
        if groups and groups[-1]["char"] == seg["char"]:
            groups[-1]["items"].append(seg)
            groups[-1]["end_seq"] = seg["seq"]
        else:
            groups.append({
                "char": seg["char"],
                "start_seq": seg["seq"],
                "end_seq": seg["seq"],
                "items": [seg],
            })

    for g_idx, grp in enumerate(groups):
        if grp["start_seq"] == grp["end_seq"]:
            title = f"片段{grp['start_seq']}（{grp['char']}）"
        else:
            title = f"片段{grp['start_seq']}-{grp['end_seq']}（{grp['char']}）"
        grp["group_idx"] = g_idx
        grp["title"] = title
        grp["filename"] = f"{safe_filename(title)}.wav"
    return groups


def generate_single_speaker_group(
    group_items: list[dict], voice_id: str, model_id: str, api_key: str
):
    """为同一角色的连续片段发送 1 次单音色 API 请求，保留每句独立导演语气"""
    try:
        base_style = "中国大陆普通话，自然口语化，富有短剧戏剧张力"
        is_38 = "3.8" in model_id

        if is_38:
            parts = [
                {
                    "text": item["text"],
                    "speechMetadata": {
                        "style": (
                            f"{base_style}，{item['tone']}"
                            if item["tone"]
                            else base_style
                        )
                    },
                }
                for item in group_items
            ]
        else:
            merged_lines = [
                (
                    f"STYLE: {base_style}，{item['tone']}\n{item['text']}"
                    if item["tone"]
                    else item["text"]
                )
                for item in group_items
            ]
            parts = [{"text": "\n\n".join(merged_lines)}]

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent"
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        }
        payload = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {
                        "prebuiltVoiceConfig": {"voiceName": voice_id}
                    }
                },
            },
        }

        resp = requests.post(url, headers=headers, json=payload, timeout=90)
        if resp.status_code != 200:
            return f"HTTP {resp.status_code}: {resp.text}", False

        resp_parts = (
            resp.json()
            .get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [])
        )
        for part in resp_parts:
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                return ensure_wav_bytes(base64.b64decode(inline["data"])), True

        return "模型未返回有效音频流。", False
    except Exception as e:
        return str(e), False


def build_zip_from_cache(audio_cache: dict, groups: list[dict]) -> bytes:
    """纯本地打包当前所有已缓存的分组 WAV 文件（不发送任何 API 请求）"""
    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for grp in groups:
            cached = audio_cache.get(grp["group_idx"])
            if cached:
                zf.writestr(grp["filename"], cached["bytes"])
    zip_buffer.seek(0)
    return zip_buffer.getvalue()


# ==============================================================================
# 🖥️ 【UI 界面渲染】
# ==============================================================================
st.title("🎙️ Gemini AI 配音控制台")

GEMINI_API_KEY = get_config([
    "GEMINI_API_KEY",
    "gemini_api_key",
    "GOOGLE_API_KEY",
    "google_api_key",
    "api_key",
])
if not GEMINI_API_KEY:
    st.warning("未检测到主页面或 Secrets 中的 Gemini API Key，请先在主页面配置。")

if "audio_cache" not in st.session_state:
    st.session_state["audio_cache"] = {}
if "char_voice_binding" not in st.session_state:
    st.session_state["char_voice_binding"] = {}

col_m, col_b = st.columns([2, 1])
with col_m:
    selected_model_label = st.selectbox(
        "🤖 选择配音模型", options=list(GEMINI_TTS_MODELS.keys())
    )
    SELECTED_MODEL_ID = GEMINI_TTS_MODELS[selected_model_label]
with col_b:
    st.write("")
    st.write("")
    if st.button("📥 从表格读取最新配音脚本", use_container_width=True):
        with st.spinner("读取中..."):
            st.session_state.script_data = fetch_script_data()
            st.session_state["audio_cache"] = {}
            st.success("已读取最新脚本！")

if "script_data" in st.session_state:
    dubbing_lines = [
        (raw_idx, row)
        for raw_idx, row in enumerate(st.session_state.script_data)
        if str(row.get("配音文字", "")).strip()
    ]

    if not dubbing_lines:
        st.info("表格中没有检测到配音文字。")
    else:
        # 1. 按表格「配音人物」去重并严格绑定音色
        st.markdown("### 1. 角色音色绑定（已按表格「配音人物」自动去重）")
        unique_chars = list(
            dict.fromkeys([
                str(row.get("配音人物", "")).strip() or "旁白"
                for _, row in dubbing_lines
            ])
        )
        voice_labels = list(GEMINI_VOICES.keys())
        map_cols = st.columns(min(len(unique_chars), 3) or 1)

        STRICT_CHAR_VOICE_MAP = {}
        for i, char in enumerate(unique_chars):
            with map_cols[i % 3]:
                saved_label = st.session_state["char_voice_binding"].get(
                    char, voice_labels[0]
                )
                default_idx = (
                    voice_labels.index(saved_label)
                    if saved_label in voice_labels
                    else 0
                )
                chosen_label = st.selectbox(
                    f"👤 【{char}】的声音",
                    options=voice_labels,
                    index=default_idx,
                    key=f"voice_bind_{char}",
                )
                st.session_state["char_voice_binding"][char] = chosen_label
                STRICT_CHAR_VOICE_MAP[char] = GEMINI_VOICES[chosen_label]

        # 读取界面上可能微调过的台词和语气，并按连续同角色自动分组
        all_segments = []
        for idx, (raw_idx, row) in enumerate(dubbing_lines):
            seq = str(row.get("序号", idx + 1)).strip()
            char = str(row.get("配音人物", "")).strip() or "旁白"
            all_segments.append({
                "idx": idx,
                "raw_idx": raw_idx,
                "seq": seq,
                "char": char,
                "text": st.session_state.get(
                    f"prompt_{idx}", str(row.get("配音文字", "")).strip()
                ),
                "tone": st.session_state.get(
                    f"instruct_{idx}", str(row.get("配音语气", "")).strip()
                ),
            })

        groups = group_consecutive_by_speaker(all_segments)

        # 2. 一键批量生成全部分组
        st.markdown("---")
        st.markdown("### 2. 生成配音（连续同角色合并一次生成，换人自动切断）")
        st.caption(
            f"共 {len(all_segments)} 个片段，已按连续同角色自动合并为 **{len(groups)} 组**（共消耗 **{len(groups)} 次 API 请求**，不裁剪音频、零错位）。"
        )

        if st.button(
            f"🚀 一键生成全部 {len(groups)} 组配音文件",
            type="primary",
            use_container_width=True,
        ):
            if not GEMINI_API_KEY:
                st.error("缺少 Gemini API Key！")
            else:
                progress = st.progress(0)
                status = st.empty()
                for g_idx, grp in enumerate(groups):
                    status.text(
                        f"正在发送第 {g_idx + 1}/{len(groups)} 次 API 请求：{grp['title']}..."
                    )
                    voice_id = STRICT_CHAR_VOICE_MAP[grp["char"]]

                    wav_data, success = generate_single_speaker_group(
                        grp["items"], voice_id, SELECTED_MODEL_ID, GEMINI_API_KEY
                    )
                    if success:
                        st.session_state["audio_cache"][g_idx] = {
                            "filename": grp["filename"],
                            "bytes": wav_data,
                        }
                        update_segment_durations(grp["items"], wav_data)
                    else:
                        st.error(f"{grp['title']} 生成失败：{wav_data}")

                    progress.progress((g_idx + 1) / len(groups))
                    if len(groups) > 3 and g_idx < len(groups) - 1:
                        status.text(
                            "为避免触发免费版 3次/分钟 频率限制，等待 21 秒后继续..."
                        )
                        time.sleep(21)

                status.text("全部生成并缓存在网页中！")
                st.success(
                    f"已成功生成 {len(st.session_state['audio_cache'])}/{len(groups)} 组配音文件！"
                )

        # 3. 按分组展示明细，支持修改台词后单独覆盖重做
        st.markdown("---")
        st.markdown("### 3. 配音分组明细（支持修改任意句子后单独覆盖重做）")

        for grp in groups:
            g_idx = grp["group_idx"]
            char = grp["char"]
            bound_voice = STRICT_CHAR_VOICE_MAP[char]
            cached_item = st.session_state["audio_cache"].get(g_idx)

            if cached_item:
                dur = get_wav_duration(cached_item["bytes"])
                badge = f"✅ 已缓存 `{grp['filename']}` (⏱️ {dur:.1f}s)"
            else:
                badge = "⚪ 待生成"

            with st.expander(
                f"📁 {grp['title']} ｜ 绑定音色: {bound_voice} ｜ 共 {len(grp['items'])} 句 ｜ {badge}",
                expanded=True,
            ):
                # 展示该组内包含的每一句（可独立修改语气和台词）
                for item in grp["items"]:
                    s_idx = item["idx"]
                    c_seq, c_tone, c_txt = st.columns([0.6, 1.4, 3.0])
                    with c_seq:
                        st.markdown(f"**片段 {item['seq']}**")
                    with c_tone:
                        st.text_input(
                            "配音语气",
                            value=item["tone"],
                            key=f"instruct_{s_idx}",
                            label_visibility="collapsed",
                            placeholder="配音语气",
                        )
                    with c_txt:
                        st.text_area(
                            "配音文字",
                            value=item["text"],
                            key=f"prompt_{s_idx}",
                            height=52,
                            label_visibility="collapsed",
                        )

                # 组操作栏：左边试听已缓存音频，右边单独重新生成并覆盖
                ac1, ac2 = st.columns([3.5, 1])
                with ac1:
                    if cached_item:
                        st.audio(cached_item["bytes"], format="audio/wav")
                    else:
                        st.caption("尚未生成此组音频")
                with ac2:
                    btn_text = (
                        "🔄 重新生成并覆盖"
                        if cached_item
                        else "▶️ 单独生成此组"
                    )
                    if st.button(
                        btn_text,
                        key=f"regen_grp_{g_idx}",
                        use_container_width=True,
                    ):
                        if not GEMINI_API_KEY:
                            st.error("缺少 Gemini API Key！")
                        else:
                            # 收集最新编辑后的组内台词
                            latest_items = [
                                {
                                    "idx": it["idx"],
                                    "raw_idx": it["raw_idx"],
                                    "seq": it["seq"],
                                    "char": it["char"],
                                    "tone": st.session_state.get(
                                        f"instruct_{it['idx']}", it["tone"]
                                    ),
                                    "text": st.session_state.get(
                                        f"prompt_{it['idx']}", it["text"]
                                    ),
                                }
                                for it in grp["items"]
                            ]
                            with st.spinner(f"正在生成 {grp['title']}..."):
                                wav_data, success = (
                                    generate_single_speaker_group(
                                        latest_items,
                                        bound_voice,
                                        SELECTED_MODEL_ID,
                                        GEMINI_API_KEY,
                                    )
                                )
                                if success:
                                    st.session_state["audio_cache"][g_idx] = {
                                        "filename": grp["filename"],
                                        "bytes": wav_data,
                                    }
                                    update_segment_durations(latest_items, wav_data)
                                    st.rerun()
                                else:
                                    st.error(f"生成失败: {wav_data}")

        # 4. 纯本地一键打包下载（0 次 API 请求）
        st.markdown("---")
        cached_count = len(st.session_state["audio_cache"])
        if cached_count > 0:
            zip_bytes = build_zip_from_cache(
                st.session_state["audio_cache"], groups
            )
            st.download_button(
                label=f"⬇️ 一键打包下载已生成的 {cached_count} 个配音文件 (ZIP，不消耗 API)",
                data=zip_bytes,
                file_name=f"Dubbing_{int(time.time())}.zip",
                mime="application/zip",
                type="primary",
                use_container_width=True,
            )
        else:
            st.info(
                "尚未生成任何音频，点击上方「一键生成全部」或「单独生成此组」后，此处将出现打包下载按钮。"
            )
