import json
import os
import re
import tempfile
import zipfile
from io import BytesIO
from google.oauth2.service_account import Credentials
from gradio_client import Client, handle_file
import gspread
import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="AI 背景音与动作拟音", page_icon="🎧", layout="wide")

DEFAULT_SHEET_URL = "https://docs.google.com/spreadsheets/d/1Y8YOEl5G2HV4C3WLZz9ttEEoByuWKGLqh_nmWvcb-_M/edit"
TAB_NAME_SCRIPT = "toonflow-story-Script"
TAB_NAME_PROMPT = "prompt"


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


def get_gspread_client():
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
    return gspread.authorize(creds)


def fetch_prompt_only() -> str:
    """单独从 prompt 标签页读取 A 列为「背景音」的 B 列内容"""
    sheet_url = get_config(
        ["sheet_url", "SHEET_URL", "SHEET_EDIT_URL"], DEFAULT_SHEET_URL
    )
    sh = get_gspread_client().open_by_url(sheet_url)
    rows = sh.worksheet(TAB_NAME_PROMPT).get_all_values()
    for row in rows:
        if len(row) >= 2 and row[0].strip() == "背景音":
            return row[1].strip()
    return ""


def fetch_script_and_prompt() -> tuple[pd.DataFrame, str]:
    """同时读取分镜表与 prompt 标签页中的「背景音」提示词"""
    sheet_url = get_config(
        ["sheet_url", "SHEET_URL", "SHEET_EDIT_URL"], DEFAULT_SHEET_URL
    )
    sh = get_gspread_client().open_by_url(sheet_url)

    records = sh.worksheet(TAB_NAME_SCRIPT).get_all_records()
    df = pd.DataFrame(records)

    sfx_prompt = ""
    rows = sh.worksheet(TAB_NAME_PROMPT).get_all_values()
    for row in rows:
        if len(row) >= 2 and row[0].strip() == "背景音":
            sfx_prompt = row[1].strip()
            break

    return df, sfx_prompt


def get_mmaudio_client(hf_token: str) -> Client:
    token = (hf_token or "").strip() or None
    try:
        return Client("hkchengrex/MMAudio", token=token)
    except TypeError:
        return Client("hkchengrex/MMAudio", hf_token=token)


def call_mmaudio_api(client: Client, candidate_apis: list, *args):
    last_err = None
    for ep in candidate_apis:
        try:
            if isinstance(ep, int):
                return client.predict(*args, fn_index=ep)
            return client.predict(*args, api_name=ep)
        except Exception as e:
            last_err = e
    raise RuntimeError(f"MMAudio 接口调用失败: {last_err}")


def translate_scene_sfx_with_gemini(
    api_key: str, tasks: list[dict], sheet_prompt: str
) -> dict[str, str]:
    """使用从 Google 表格 prompt 页读取的提示词调用 Gemini"""
    if not tasks or not api_key.strip():
        return {t["id"]: t["sfx"] for t in tasks}

    if not sheet_prompt.strip():
        st.warning("未在 `prompt` 标签页中找到「背景音」提示词，将直接使用原词生成。")
        return {t["id"]: t["sfx"] for t in tasks}

    full_prompt = (
        f"{sheet_prompt.strip()}\n\n"
        f"分镜列表：{json.dumps(tasks, ensure_ascii=False)}"
    )
    try:
        url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
        resp = requests.post(
            url,
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": api_key.strip(),
            },
            json={"contents": [{"role": "user", "parts": [{"text": full_prompt}]}]},
            timeout=45,
        )
        if resp.status_code != 200:
            return {t["id"]: t["sfx"] for t in tasks}
        raw_text = (
            resp.json()
            .get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "")
            .strip()
        )
        raw_text = re.sub(
            r"^```json\s*|\s*```$", "", raw_text, flags=re.MULTILINE
        ).strip()
        mapping = json.loads(raw_text)
        return {t["id"]: mapping.get(t["id"], t["sfx"]) for t in tasks}
    except Exception:
        return {t["id"]: t["sfx"] for t in tasks}


def safe_filename(text: str, max_len: int = 18) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "_", str(text).strip())
    return cleaned[:max_len].strip("_") or "背景音"


# ==============================================================================
# 🖥️ 【UI 界面渲染】
# ==============================================================================
st.title("🎧 短剧背景音与动作拟音（MMAudio）")

HF_TOKEN = get_config(["HF_TOKEN", "hf_token"])
GEMINI_API_KEY = get_config([
    "GEMINI_API_KEY",
    "gemini_api_key",
    "GOOGLE_API_KEY",
    "google_api_key",
    "api_key",
])

with st.sidebar:
    st.header("🔑 密钥状态")
    hf_token_input = st.text_input(
        "Hugging Face Token",
        value=HF_TOKEN,
        type="password",
        placeholder="hf_...",
    )
    if hf_token_input:
        HF_TOKEN = hf_token_input
        st.session_state["HF_TOKEN"] = hf_token_input

    if GEMINI_API_KEY:
        st.success("✅ 已加载 Gemini API Key")
    else:
        st.warning("⚠️ 未检测到 Gemini API Key")

tab_bgm, tab_foley = st.tabs(
    ["📄 模式一：按表格批量生成背景音", "🎬 模式二：视频自动动作拟音"]
)

# -------------------- 模式一：表格批量背景音 --------------------
with tab_bgm:
    c_info, c_btn = st.columns([3, 1])
    with c_info:
        st.caption(
            f"数据源：`{TAB_NAME_SCRIPT}`（分镜数据） + `{TAB_NAME_PROMPT}`（背景音 Prompt）"
        )
    with c_btn:
        if st.button("📥 读取最新表格与 Prompt", use_container_width=True):
            with st.spinner("正在读取 Google 表格..."):
                try:
                    df_new, prompt_new = fetch_script_and_prompt()
                    st.session_state["sfx_df"] = df_new
                    st.session_state["sfx_sheet_prompt"] = prompt_new
                    st.session_state["sfx_bgm_results"] = []
                    st.success("已同步最新分镜表与 Prompt！")
                except Exception as e:
                    st.error(f"读取失败：{e}")

    # 若刚从 voice.py 生成完配音切过来，自动同步最新时长并拉取一次 prompt
    if "shared_script_df" in st.session_state and "sfx_df" not in st.session_state:
        st.session_state["sfx_df"] = st.session_state["shared_script_df"].copy()
        if "sfx_sheet_prompt" not in st.session_state:
            try:
                st.session_state["sfx_sheet_prompt"] = fetch_prompt_only()
            except Exception:
                st.session_state["sfx_sheet_prompt"] = ""

    # 展示从 Google 表格读取到的 Prompt 供核对
    current_prompt = st.session_state.get("sfx_sheet_prompt", "")
    if current_prompt:
        with st.expander("📝 当前已加载的【背景音】Prompt（来自 Google 表格 prompt 页）", expanded=False):
            st.text(current_prompt)

    df = st.session_state.get("sfx_df")
    if df is not None and not df.empty:
        show_cols = [
            c for c in ["序号", "片段", "片段时长", "背景音"] if c in df.columns
        ]
        edited_df = st.data_editor(
            df[show_cols] if show_cols else df,
            use_container_width=True,
            num_rows="dynamic",
        )

        if "背景音" not in edited_df.columns:
            st.warning("表格中未找到 `背景音` 列。")
        else:
            valid_rows = edited_df[
                edited_df["背景音"].notna()
                & (edited_df["背景音"].astype(str).str.strip() != "")
            ].copy()
            st.info(f"共检测到 **{len(valid_rows)}** 条有效背景音需求。")

            if st.button(
                f"🚀 一键批量生成全部 {len(valid_rows)} 条背景音",
                type="primary",
                use_container_width=True,
                disabled=len(valid_rows) == 0,
            ):
                if not HF_TOKEN.strip():
                    st.error("请先在 Secrets 或左侧边栏填写 Hugging Face Token（hf_...）。")
                else:
                    tasks = []
                    for idx, (_, row) in enumerate(valid_rows.iterrows()):
                        seq = str(row.get("序号", idx + 1)).strip()
                        tasks.append({
                            "id": seq,
                            "scene": str(row.get("片段", "")).strip(),
                            "sfx": str(row["背景音"]).strip(),
                        })

                    with st.spinner("正在使用表格 Prompt 生成声学提示词并连接 MMAudio..."):
                        mm_client = get_mmaudio_client(HF_TOKEN)
                        en_map = translate_scene_sfx_with_gemini(
                            GEMINI_API_KEY, tasks, current_prompt
                        )

                    results = []
                    progress = st.progress(0)
                    status = st.empty()

                    for idx, (_, row) in enumerate(valid_rows.iterrows()):
                        seq = str(row.get("序号", idx + 1)).strip()
                        cn_desc = str(row["背景音"]).strip()
                        en_prompt = en_map.get(seq, cn_desc)

                        try:
                            dur = float(row.get("片段时长", 6.0))
                        except (ValueError, TypeError):
                            dur = 6.0
                        dur = max(1.0, min(20.0, dur))

                        status.info(
                            f"正在生成片段 {seq}（{dur:.2f}s）：{cn_desc} → `{en_prompt}`"
                        )
                        try:
                            audio_path = call_mmaudio_api(
                                mm_client,
                                ["/text_to_audio", "/predict_1", 1],
                                en_prompt,
                                "music, speech, talking",
                                -1,
                                25,
                                4.5,
                                dur,
                            )
                            ext = os.path.splitext(audio_path)[1] or ".flac"
                            fname = f"片段{seq}_背景_{safe_filename(cn_desc)}{ext}"
                            with open(audio_path, "rb") as f:
                                results.append(
                                    (fname, f.read(), cn_desc, en_prompt, dur)
                                )
                        except Exception as e:
                            st.error(f"片段 {seq} 生成失败：{e}")

                        progress.progress((idx + 1) / len(valid_rows))

                    status.success(
                        f"全部生成完成！成功 {len(results)} / {len(valid_rows)} 条。"
                    )
                    st.session_state["sfx_bgm_results"] = results

            if st.session_state.get("sfx_bgm_results"):
                saved = st.session_state["sfx_bgm_results"]
                zip_buf = BytesIO()
                with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
                    for fname, data, _, _, _ in saved:
                        zf.writestr(fname, data)

                st.download_button(
                    label=f"⬇️ 一键打包下载全部背景音 ({len(saved)} 条 .zip)",
                    data=zip_buf.getvalue(),
                    file_name="短剧背景音包.zip",
                    mime="application/zip",
                    type="primary",
                    use_container_width=True,
                )
                for fname, data, cn_desc, en_prompt, dur in saved:
                    with st.expander(
                        f"🔊 {fname}（{dur:.2f}s | {cn_desc}）", expanded=False
                    ):
                        st.caption(f"英文声学提示词：`{en_prompt}`")
                        st.audio(data)
    else:
        st.info("点击右上角「📥 读取最新表格与 Prompt」加载数据。")

# -------------------- 模式二：视频自动动作拟音 --------------------
with tab_foley:
    st.caption(
        "上传无声分镜视频（建议 5～12 秒），AI 会自动识别画面里的脚步、拍手、玻璃碎裂、爆炸动作并帧级对齐生成音效。"
    )
    videos = st.file_uploader(
        "上传分镜视频（支持批量多选）",
        type=["mp4", "mov", "webm"],
        accept_multiple_files=True,
    )
    foley_prompt = st.text_input(
        "补充英文动作提示（可选，留空则完全由 AI 看画面自动判断）",
        placeholder="例如：footsteps on concrete, glass shattering, heavy explosion",
    )

    if st.button("🎬 开始自动视频动作拟音", type="primary", disabled=not videos):
        if not HF_TOKEN.strip():
            st.error("请先填写 Hugging Face Token（hf_...）。")
        else:
            mm_client = get_mmaudio_client(HF_TOKEN)
            foley_results = []
            for vid in videos:
                with st.spinner(f"正在分析画面并合成同步背景音：{vid.name} ..."):
                    with tempfile.NamedTemporaryFile(
                        suffix=".mp4", delete=False
                    ) as tmp_v:
                        tmp_v.write(vid.read())
                        tmp_path = tmp_v.name
                    try:
                        out_res = call_mmaudio_api(
                            mm_client,
                            ["/video_to_audio", "/predict", 0],
                            handle_file(tmp_path),
                            foley_prompt.strip(),
                            "music, speech",
                            -1,
                            25,
                            4.5,
                            8.0,
                        )
                        out_video_path = (
                            out_res["video"]
                            if isinstance(out_res, dict)
                            else out_res
                        )
                        with open(out_video_path, "rb") as f:
                            foley_results.append((f"拟音_{vid.name}", f.read()))
                    except Exception as e:
                        st.error(f"{vid.name} 拟音失败：{e}")
                    finally:
                        if os.path.exists(tmp_path):
                            os.remove(tmp_path)

            st.session_state["sfx_foley_results"] = foley_results

    if st.session_state.get("sfx_foley_results"):
        for out_name, v_bytes in st.session_state["sfx_foley_results"]:
            st.markdown(f"**{out_name}**")
            st.video(v_bytes)
            st.download_button(
                label=f"⬇️ 下载 {out_name}",
                data=v_bytes,
                file_name=out_name,
                mime="video/mp4",
                key=f"dl_{out_name}",
            )
