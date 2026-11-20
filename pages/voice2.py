from io import BytesIO
import json
import os
import re
import time
import zipfile
from google.oauth2.service_account import Credentials
from gradio_client import Client
import gspread
import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="AI 背景音生成", page_icon="🎧", layout="wide")

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
    """直接调用 /text_to_audio，遇到额度耗尽直接提示，不覆盖真实报错"""
    last_err = None
    for attempt in range(3):
        try:
            res = client.predict(*args, api_name="/text_to_audio")
            if isinstance(res, (tuple, list)):
                for item in res:
                    if isinstance(item, str) and os.path.exists(item):
                        return item
                    if isinstance(item, dict):
                        p = (
                            item.get("path")
                            or item.get("video")
                            or item.get("audio")
                        )
                        if p:
                            return p
                return str(res[0])
            if isinstance(res, dict):
                return (
                    res.get("path")
                    or res.get("video")
                    or res.get("audio")
                    or str(res)
                )
            return res
        except Exception as e:
            last_err = e
            if "quota" in str(e).lower():
                raise RuntimeError(
                    f"ZeroGPU 算力受限（请检查 Token 是否为 Read 类型）：{e}"
                )
            if attempt < 2:
                time.sleep(3)
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
st.title("🎧 短剧背景音控制台（MMAudio）")

HF_TOKEN = get_config(["HF_TOKEN", "hf_token"])
GEMINI_API_KEY = get_config([
    "GEMINI_API_KEY",
    "gemini_api_key",
    "GOOGLE_API_KEY",
    "google_api_key",
    "api_key",
])

if "sfx_cache" not in st.session_state:
    st.session_state["sfx_cache"] = {}

if GEMINI_API_KEY:
    st.success("✅ 已加载 Gemini API Key")
else:
    st.warning("⚠️ 未检测到 Gemini API Key")

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
                st.session_state["sfx_cache"] = {}
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

current_prompt = st.session_state.get("sfx_sheet_prompt", "")
if current_prompt:
    with st.expander(
        "📝 当前已加载的【背景音】Prompt（来自 Google 表格 prompt 页）",
        expanded=False,
    ):
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

        # 1. 一键批量生成全部背景音
        if st.button(
            f"🚀 一键批量生成全部 {len(valid_rows)} 条背景音",
            type="primary",
            use_container_width=True,
            disabled=len(valid_rows) == 0,
        ):
            if not HF_TOKEN.strip():
                st.error("请先在 Secrets 中配置 Hugging Face Token（HF_TOKEN）。")
            else:
                tasks = []
                for idx, (_, row) in enumerate(valid_rows.iterrows()):
                    seq = str(row.get("序号", idx + 1)).strip() or str(idx + 1)
                    row_key = f"{idx}_{seq}"
                    tasks.append({
                        "id": row_key,
                        "scene": str(row.get("片段", "")).strip(),
                        "sfx": str(row["背景音"]).strip(),
                    })

                with st.spinner("正在使用表格 Prompt 生成声学提示词并连接 MMAudio..."):
                    mm_client = get_mmaudio_client(HF_TOKEN)
                    en_map = translate_scene_sfx_with_gemini(
                        GEMINI_API_KEY, tasks, current_prompt
                    )

                progress = st.progress(0)
                status = st.empty()

                for idx, (_, row) in enumerate(valid_rows.iterrows()):
                    seq = str(row.get("序号", idx + 1)).strip() or str(idx + 1)
                    row_key = f"{idx}_{seq}"
                    cn_desc = str(row["背景音"]).strip()
                    en_prompt = en_map.get(row_key, cn_desc)

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
                            ["/text_to_audio", "/predict_1"],
                            en_prompt,
                            "music, speech, talking",
                            -1,
                            25,
                            4.5,
                            dur,
                        )
                        ext = os.path.splitext(audio_path)[1] or ".flac"
                        fname = f"片段{seq}_背景_{safe_filename(cn_desc)}{ext}"
                        old_ver = (
                            st.session_state["sfx_cache"]
                            .get(row_key, {})
                            .get("ver", 0)
                        )
                        with open(audio_path, "rb") as f:
                            st.session_state["sfx_cache"][row_key] = {
                                "filename": fname,
                                "bytes": f.read(),
                                "cn_desc": cn_desc,
                                "en_prompt": en_prompt,
                                "dur": dur,
                                "ver": old_ver + 1,
                            }
                    except Exception as e:
                        st.error(f"片段 {seq} 生成失败：{e}")

                    progress.progress((idx + 1) / len(valid_rows))

                status.success("批量生成完成！")
                st.rerun()

        # 2. 一键打包下载所有已缓存的背景音（按表格顺序打包）
        sfx_cache = st.session_state["sfx_cache"]
        ordered_items = []
        for idx, (_, row) in enumerate(valid_rows.iterrows()):
            seq = str(row.get("序号", idx + 1)).strip() or str(idx + 1)
            row_key = f"{idx}_{seq}"
            if row_key in sfx_cache:
                ordered_items.append(sfx_cache[row_key])

        if ordered_items:
            zip_buf = BytesIO()
            with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for item in ordered_items:
                    zf.writestr(item["filename"], item["bytes"])

            st.download_button(
                label=f"⬇️ 一键打包下载已生成的 {len(ordered_items)} 条背景音 (.zip)",
                data=zip_buf.getvalue(),
                file_name="短剧背景音包.zip",
                mime="application/zip",
                type="primary",
                use_container_width=True,
            )

        # 3. 分段明细（支持单条试听、微调提示词后独立覆盖重做）
        st.markdown("---")
        st.markdown("### 分段背景音明细（支持单条试听与独立覆盖重做）")

        for idx, (_, row) in enumerate(valid_rows.iterrows()):
            seq = str(row.get("序号", idx + 1)).strip() or str(idx + 1)
            row_key = f"{idx}_{seq}"
            cn_desc = str(row["背景音"]).strip()
            scene_desc = str(row.get("片段", "")).strip()

            try:
                dur = float(row.get("片段时长", 6.0))
            except (ValueError, TypeError):
                dur = 6.0
            dur = max(1.0, min(20.0, dur))

            cached = sfx_cache.get(row_key)
            badge = f"✅ 已缓存 `{cached['filename']}`" if cached else "⚪ 待生成"

            with st.expander(
                f"🔊 片段 {seq}（{dur:.2f}s ｜ {cn_desc}） ｜ {badge}",
                expanded=True,
            ):
                ac1, ac2 = st.columns([3.5, 1])
                with ac1:
                    if cached:
                        en_key = f"bgm_en_{row_key}_{cached.get('ver', 0)}"
                        st.text_input(
                            "英文声学提示词（可直接微调后点右侧重做）",
                            value=cached["en_prompt"],
                            key=en_key,
                        )
                        st.audio(cached["bytes"])
                    else:
                        st.caption("尚未生成此条背景音，可点击右侧按钮单独生成。")

                with ac2:
                    st.write("")
                    btn_text = "🔄 重新生成并覆盖" if cached else "▶️ 单独生成此条"
                    if st.button(
                        btn_text,
                        key=f"regen_bgm_{row_key}",
                        use_container_width=True,
                    ):
                        if not HF_TOKEN.strip():
                            st.error("请先配置 Hugging Face Token（HF_TOKEN）。")
                        else:
                            with st.spinner(f"正在生成片段 {seq}..."):
                                try:
                                    edited_en = (
                                        st.session_state.get(
                                            f"bgm_en_{row_key}_{cached.get('ver', 0)}",
                                            "",
                                        ).strip()
                                        if cached
                                        else ""
                                    )
                                    # 若表格中文描述未改且英文框有内容，则直接用英文框（支持微调或换随机效果）；若改了中文则重新翻译
                                    if (
                                        cached
                                        and cached["cn_desc"] == cn_desc
                                        and edited_en
                                    ):
                                        final_en = edited_en
                                    else:
                                        one_task = [
                                            {
                                                "id": row_key,
                                                "scene": scene_desc,
                                                "sfx": cn_desc,
                                            }
                                        ]
                                        en_map = translate_scene_sfx_with_gemini(
                                            GEMINI_API_KEY,
                                            one_task,
                                            current_prompt,
                                        )
                                        final_en = en_map.get(row_key, cn_desc)

                                    mm_client = get_mmaudio_client(HF_TOKEN)
                                    audio_path = call_mmaudio_api(
                                        mm_client,
                                        ["/text_to_audio", "/predict_1"],
                                        final_en,
                                        "music, speech, talking",
                                        -1,
                                        25,
                                        4.5,
                                        dur,
                                    )
                                    ext = (
                                        os.path.splitext(audio_path)[1]
                                        or ".flac"
                                    )
                                    fname = f"片段{seq}_背景_{safe_filename(cn_desc)}{ext}"
                                    old_ver = (
                                        cached.get("ver", 0) if cached else 0
                                    )
                                    with open(audio_path, "rb") as f:
                                        st.session_state["sfx_cache"][
                                            row_key
                                        ] = {
                                            "filename": fname,
                                            "bytes": f.read(),
                                            "cn_desc": cn_desc,
                                            "en_prompt": final_en,
                                            "dur": dur,
                                            "ver": old_ver + 1,
                                        }
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"片段 {seq} 生成失败：{e}")
else:
    st.info("点击右上角「📥 读取最新表格与 Prompt」加载数据。")
