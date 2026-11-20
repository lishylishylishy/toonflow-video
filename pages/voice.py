import base64
from io import BytesIO
import json
import math
import os
import re
import struct
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
    "Kore（女·清冷坚定 / 独立大女主）": "Kore",
    "Aoede（女·温柔知性 / 情感叙事）": "Aoede",
    "Leda（女·甜美灵动 / 少女闺蜜）": "Leda",
    "Zephyr（女·明快自然 / 现代女主）": "Zephyr",
    "Gacrux（女·成熟御姐 / 霸气女总裁）": "Gacrux",
    "Vindemiatrix（女·柔美细腻 / 虐恋白月光）": "Vindemiatrix",
    "Achernar（女·轻柔温婉 / 乖巧甜妹）": "Achernar",
    "Despina（女·丝滑优雅 / 豪门千金）": "Despina",
    "Erinome（女·清晰干练 / 职场精英）": "Erinome",
    "Callirrhoe（女·随和亲切 / 邻家姐姐）": "Callirrhoe",
    "Autonoe（女·明亮活泼 / 元气少女）": "Autonoe",
    "Laomedeia（女·高亢娇俏 / 刁蛮千金）": "Laomedeia",
    "Sulafat（女·温暖包容 / 贤惠母性）": "Sulafat",
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
    return re.sub(r'[\\/:*?"<>|\s]+', "_", str(name).strip())


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


def _pack_pcm_to_wav(
    pcm_samples, framerate=24000, nchannels=1, sampwidth=2
) -> bytes:
    buf = BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(nchannels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(framerate)
        wf.writeframes(struct.pack(f"<{len(pcm_samples)}h", *pcm_samples))
    return buf.getvalue()


def split_wav_to_segments(wav_bytes: bytes, expected_count: int) -> list[bytes]:
    """根据句间 <long pause> 停顿，将合批生成的整条 WAV 切分为 expected_count 条独立单句音频"""
    if expected_count <= 1:
        return [wav_bytes]

    with wave.open(BytesIO(wav_bytes), "rb") as wf:
        nchannels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        framerate = wf.getframerate()
        frames = wf.readframes(wf.getnframes())

    if sampwidth != 2 or nchannels != 1:
        return [wav_bytes]

    total_samples = len(frames) // 2
    if total_samples == 0:
        return [wav_bytes]

    samples = struct.unpack(f"<{total_samples}h", frames)
    win_size = max(1, int(framerate * 0.02))  # 20ms 窗口
    num_wins = total_samples // win_size
    if num_wins < expected_count * 5:
        return [wav_bytes]

    rms_list = [
        math.sqrt(
            sum(
                s * s
                for s in samples[w * win_size : (w + 1) * win_size]
            )
            / win_size
        )
        for w in range(num_wins)
    ]
    peak_ref = sorted(rms_list)[int(num_wins * 0.90)]

    def find_gaps(thresh_ratio, min_win_len):
        thresh = max(150.0, peak_ref * thresh_ratio)
        gaps = []
        in_sil = False
        g_start = 0
        for w, r in enumerate(rms_list):
            if r <= thresh:
                if not in_sil:
                    in_sil = True
                    g_start = w
            else:
                if in_sil:
                    in_sil = False
                    if g_start > 2 and (w - g_start) >= min_win_len:
                        gaps.append((g_start, w))
        return gaps

    # 优先匹配 >=240ms 的长停顿；不足时自动放宽至 >=100ms 停顿
    silent_gaps = find_gaps(0.03, 12)
    if len(silent_gaps) < expected_count - 1:
        silent_gaps = find_gaps(0.05, 5)

    if len(silent_gaps) < expected_count - 1:
        return [wav_bytes]

    top_gaps = sorted(silent_gaps, key=lambda g: (g[1] - g[0]), reverse=True)[
        : expected_count - 1
    ]
    top_gaps.sort(key=lambda g: g[0])

    pad_samples = 8 * win_size  # 边界留白 0.16s 防吞字
    split_indices = (
        [0]
        + [((g[0] + g[1]) // 2) * win_size for g in top_gaps]
        + [total_samples]
    )

    return [
        _pack_pcm_to_wav(
            samples[
                max(0, split_indices[i] - (pad_samples if i > 0 else 0)) : min(
                    total_samples,
                    split_indices[i + 1]
                    + (pad_samples if i < len(split_indices) - 2 else 0),
                )
            ],
            framerate,
            nchannels,
            sampwidth,
        )
        for i in range(len(split_indices) - 1)
    ]


def group_segments_for_batch(segments, max_chars=2500):
    """按相邻最多 2 个不重复角色自动合批（≤2 角色时整剧合并为 1 次请求）"""
    batches = []
    current_batch = []
    current_speakers = set()
    current_chars = 0

    for seg in segments:
        spk = seg["char"]
        text_len = len(seg["text"])
        next_speakers = current_speakers | {spk}

        if current_batch and (
            len(next_speakers) > 2 or current_chars + text_len > max_chars
        ):
            batches.append(current_batch)
            current_batch = [seg]
            current_speakers = {spk}
            current_chars = text_len
        else:
            current_batch.append(seg)
            current_speakers = next_speakers
            current_chars += text_len

    if current_batch:
        batches.append(current_batch)
    return batches


def generate_and_split_segments(segments, char_voice_map, model_id, api_key):
    """发送 1 次 API 请求生成整批台词，并自动切分为独立 WAV 文件列表返回"""
    try:
        unique_speakers = list(dict.fromkeys([s["char"] for s in segments]))
        base_style = "中国大陆普通话，自然口语化，富有短剧戏剧张力"
        is_38 = "3.8" in model_id
        total_segs = len(segments)

        def with_pause(raw_txt, index):
            if total_segs <= 1 or index == total_segs - 1:
                return raw_txt
            return (
                f"{raw_txt} <long pause>" if is_38 else f"{raw_txt} ... ... "
            )

        if len(unique_speakers) == 1:
            spk = unique_speakers[0]
            speech_config = {
                "voiceConfig": {
                    "prebuiltVoiceConfig": {"voiceName": char_voice_map[spk]}
                }
            }
            if is_38:
                parts = [
                    {
                        "text": with_pause(s["text"], idx),
                        "speechMetadata": {
                            "style": (
                                f"{base_style}，{s['tone']}"
                                if s["tone"]
                                else base_style
                            )
                        },
                    }
                    for idx, s in enumerate(segments)
                ]
            else:
                parts = [
                    {
                        "text": "\n\n".join([
                            f"STYLE: {base_style}，{s['tone']}\n{with_pause(s['text'], idx)}"
                            for idx, s in enumerate(segments)
                        ])
                    }
                ]
        else:
            speech_config = {
                "multiSpeakerVoiceConfig": {
                    "speakerVoiceConfigs": [
                        {
                            "speaker": spk,
                            "voiceConfig": {
                                "prebuiltVoiceConfig": {
                                    "voiceName": char_voice_map[spk]
                                }
                            },
                        }
                        for spk in unique_speakers
                    ]
                }
            }
            if is_38:
                parts = [
                    {
                        "text": with_pause(s["text"], idx),
                        "speechMetadata": {
                            "speaker": s["char"],
                            "style": (
                                f"{base_style}，{s['tone']}"
                                if s["tone"]
                                else base_style
                            ),
                        },
                    }
                    for idx, s in enumerate(segments)
                ]
            else:
                parts = [
                    {
                        "text": "\n".join([
                            f"{s['char']}: {with_pause(s['text'], idx)}"
                            for idx, s in enumerate(segments)
                        ])
                    }
                ]

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent"
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        }
        payload = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": speech_config,
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
                full_wav = ensure_wav_bytes(base64.b64decode(inline["data"]))
                return split_wav_to_segments(full_wav, total_segs), True

        return "模型未返回有效音频流。", False
    except Exception as e:
        return str(e), False


def build_zip_from_cache(audio_cache: dict) -> bytes:
    """纯本地打包网页中已缓存的独立 WAV 文件（不发送任何 API 请求）"""
    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for idx in sorted(audio_cache.keys()):
            item = audio_cache[idx]
            zf.writestr(item["filename"], item["bytes"])
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
            st.session_state["audio_cache"] = {}  # 读取新脚本时清空旧缓存
            st.success("已读取最新脚本！")

if "script_data" in st.session_state:
    dubbing_lines = [
        row
        for row in st.session_state.script_data
        if str(row.get("配音文字", "")).strip()
    ]

    if not dubbing_lines:
        st.info("表格中没有检测到配音文字。")
    else:
        # ----------------------------------------------------------------------
        # 1. 按表格「配音人物」自动去重并严格绑定音色
        # ----------------------------------------------------------------------
        st.markdown("### 1. 角色音色绑定（已按表格「配音人物」自动去重）")
        unique_chars = list(
            dict.fromkeys([
                str(row.get("配音人物", "")).strip() or "旁白"
                for row in dubbing_lines
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

        # ----------------------------------------------------------------------
        # 2. 一键批量生成全部独立片段（缓存在网页中）
        # ----------------------------------------------------------------------
        st.markdown("---")
        all_segments = []
        for idx, row in enumerate(dubbing_lines):
            seq = str(row.get("序号", idx + 1)).strip()
            char = str(row.get("配音人物", "")).strip() or "旁白"
            all_segments.append({
                "idx": idx,
                "seq": seq,
                "char": char,
                "text": st.session_state.get(
                    f"prompt_{idx}", str(row.get("配音文字", "")).strip()
                ),
                "tone": st.session_state.get(
                    f"instruct_{idx}", str(row.get("配音语气", "")).strip()
                ),
            })

        batches = group_segments_for_batch(all_segments)
        st.markdown("### 2. 生成配音（生成后自动缓存在网页，支持单句覆盖重做）")
        st.caption(
            f"当前共 {len(all_segments)} 个片段、{len(unique_chars)} 个角色（{', '.join(unique_chars)}），"
            f"将自动合并为 **{len(batches)} 次 API 请求**生成并切分为 {len(all_segments)} 个独立音频文件。"
        )

        if st.button(
            f"🚀 一键生成全部 {len(all_segments)} 个独立配音文件",
            type="primary",
            use_container_width=True,
        ):
            if not GEMINI_API_KEY:
                st.error("缺少 Gemini API Key！")
            else:
                progress = st.progress(0)
                status = st.empty()
                for b_idx, batch in enumerate(batches):
                    spks = "&".join(
                        list(dict.fromkeys([x["char"] for x in batch]))
                    )
                    status.text(
                        f"正在发送第 {b_idx + 1}/{len(batches)} 次 API 请求（片段 {batch[0]['seq']}~{batch[-1]['seq']}：{spks}）..."
                    )

                    clips, success = generate_and_split_segments(
                        batch,
                        STRICT_CHAR_VOICE_MAP,
                        SELECTED_MODEL_ID,
                        GEMINI_API_KEY,
                    )
                    if success:
                        if len(clips) == len(batch):
                            for seg_item, wav_data in zip(batch, clips):
                                fname = f"片段{safe_filename(seg_item['seq'])}_{safe_filename(seg_item['char'])}.wav"
                                st.session_state["audio_cache"][
                                    seg_item["idx"]
                                ] = {
                                    "filename": fname,
                                    "bytes": wav_data,
                                }
                        else:
                            # 极端情况下未检测到足够停顿，兜底逐句补齐确保每个片段独立
                            for seg_item in batch:
                                single_clip, ok = generate_and_split_segments(
                                    [seg_item],
                                    STRICT_CHAR_VOICE_MAP,
                                    SELECTED_MODEL_ID,
                                    GEMINI_API_KEY,
                                )
                                if ok:
                                    fname = f"片段{safe_filename(seg_item['seq'])}_{safe_filename(seg_item['char'])}.wav"
                                    st.session_state["audio_cache"][
                                        seg_item["idx"]
                                    ] = {
                                        "filename": fname,
                                        "bytes": single_clip[0],
                                    }
                    else:
                        st.error(f"第 {b_idx + 1} 批请求失败：{clips}")

                    progress.progress((b_idx + 1) / len(batches))
                    # 仅当总请求次数超过免费版 3 次/分限制时才自动间隔
                    if len(batches) > 3 and b_idx < len(batches) - 1:
                        time.sleep(21)

                status.text("全部生成并缓存完成！可在下方逐条试听或单独覆盖重做。")
                st.success(
                    f"已成功缓存 {len(st.session_state['audio_cache'])}/{len(all_segments)} 个独立配音文件！"
                )

        # ----------------------------------------------------------------------
        # 3. 逐片段试听与单独覆盖重做
        # ----------------------------------------------------------------------
        st.markdown("---")
        st.markdown("### 3. 片段明细（支持单独发送请求覆盖重做）")

        for seg in all_segments:
            idx = seg["idx"]
            seq = seg["seq"]
            char = seg["char"]
            bound_voice = STRICT_CHAR_VOICE_MAP[char]
            cached_item = st.session_state["audio_cache"].get(idx)
            status_badge = (
                f"✅ 已缓存 `{cached_item['filename']}`"
                if cached_item
                else "⚪ 待生成"
            )

            with st.expander(
                f"片段 {seq} ｜【{char}】({bound_voice}) ｜ 语气：{seg['tone'] or '默认'} ｜ {status_badge}",
                expanded=bool(cached_item),
            ):
                c1, c2 = st.columns([3.5, 1])
                with c1:
                    new_tone = st.text_input(
                        "🎭 配音语气", value=seg["tone"], key=f"instruct_{idx}"
                    )
                    new_text = st.text_area(
                        "📝 配音文字",
                        value=seg["text"],
                        key=f"prompt_{idx}",
                        height=68,
                    )
                with c2:
                    st.write("")
                    btn_label = (
                        "🔄 重新生成并覆盖"
                        if cached_item
                        else "▶️ 单独生成此片段"
                    )
                    if st.button(
                        btn_label, key=f"regen_{idx}", use_container_width=True
                    ):
                        if not GEMINI_API_KEY:
                            st.error("缺少 Gemini API Key！")
                        else:
                            with st.spinner(f"正在生成片段 {seq}..."):
                                single_seg = [{
                                    "idx": idx,
                                    "seq": seq,
                                    "char": char,
                                    "text": new_text,
                                    "tone": new_tone,
                                }]
                                clips, success = generate_and_split_segments(
                                    single_seg,
                                    STRICT_CHAR_VOICE_MAP,
                                    SELECTED_MODEL_ID,
                                    GEMINI_API_KEY,
                                )
                                if success:
                                    fname = f"片段{safe_filename(seq)}_{safe_filename(char)}.wav"
                                    # 新音频直接覆盖旧缓存
                                    st.session_state["audio_cache"][idx] = {
                                        "filename": fname,
                                        "bytes": clips[0],
                                    }
                                    st.rerun()
                                else:
                                    st.error(f"生成失败: {clips}")

                if cached_item:
                    st.audio(cached_item["bytes"], format="audio/wav")

        # ----------------------------------------------------------------------
        # 4. 纯本地一键下载（不触发任何 API 请求）
        # ----------------------------------------------------------------------
        st.markdown("---")
        cached_count = len(st.session_state["audio_cache"])
        if cached_count > 0:
            zip_bytes = build_zip_from_cache(st.session_state["audio_cache"])
            st.download_button(
                label=f"⬇️ 一键打包下载已生成的 {cached_count} 个独立配音文件 (ZIP，不消耗 API)",
                data=zip_bytes,
                file_name=f"Dubbing_{int(time.time())}.zip",
                mime="application/zip",
                type="primary",
                use_container_width=True,
            )
        else:
            st.info(
                "尚未生成任何音频，点击上方「一键生成全部」或「单独生成」后，此处将出现打包下载按钮。"
            )
