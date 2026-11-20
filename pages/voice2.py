import io
import json
import os
import re
import tempfile
import zipfile
import pandas as pd
import streamlit as st
from gradio_client import Client, handle_file


def _get_mmaudio_client(hf_token: str) -> Client:
    token = (hf_token or "").strip() or None
    try:
        return Client("hkchengrex/MMAudio", token=token)
    except TypeError:
        # 兼容旧版 gradio_client 参数名
        return Client("hkchengrex/MMAudio", hf_token=token)


def _call_with_endpoint_fallback(client: Client, candidate_apis: list, *args):
    """自动兼容 Gradio 不同版本的接口命名（/text_to_audio 或 /predict_1）"""
    last_err = None
    for ep in candidate_apis:
        try:
            if isinstance(ep, int):
                return client.predict(*args, fn_index=ep)
            return client.predict(*args, api_name=ep)
        except Exception as e:
            last_err = e
    raise RuntimeError(f"MMAudio 接口调用失败: {last_err}")


def translate_sfx_batch_with_gemini(gemini_client, descriptions: list[str]) -> dict[str, str]:
    """用你现有的 Gemini 一次性把整列中文背景音翻译成高质量英文声学提示词"""
    unique_items = [d.strip() for d in dict.fromkeys(descriptions) if d and str(d).strip()]
    if not unique_items or gemini_client is None:
        return {item: item for item in unique_items}

    prompt = (
        "你是一个好莱坞拟音师。请将以下中文音效描述翻译成适合 AI 音效模型（MMAudio）的简短英文声学提示词。\n"
        "要求：\n"
        "1. 聚焦具体物理声音细节（如材质、动作、环境空间感），不要出现抽象形容词；\n"
        "2. 严格只返回 JSON 对象，键为原中文，值为英文提示词，不要任何额外解释。\n\n"
        f"待翻译列表：{json.dumps(unique_items, ensure_ascii=False)}"
    )
    try:
        resp = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        raw = resp.text.strip()
        raw = re.sub(r"^```json\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
        mapping = json.loads(raw)
        return {item: mapping.get(item, item) for item in unique_items}
    except Exception:
        return {item: item for item in unique_items}


def _safe_filename(text: str, max_len: int = 18) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "_", str(text).strip())
    return cleaned[:max_len].strip("_") or "音效"


def render_mmaudio_panel(df: pd.DataFrame | None = None, gemini_client=None):
    """在 pages/voice.py 底部调用此函数即可渲染完整的背景音与视频拟音工作台"""
    st.divider()
    st.subheader("🎧 智能音效与动作拟音（MMAudio）")

    default_hf_token = st.secrets.get("HF_TOKEN", "") if hasattr(st, "secrets") else ""
    hf_token = st.text_input(
        "Hugging Face Token（用于激活每日免费 ZeroGPU 额度）",
        value=default_hf_token,
        type="password",
        placeholder="hf_...",
    )

    tab_bgm, tab_foley = st.tabs(["📄 模式一：按表格批量生成背景音", "🎬 模式二：视频自动动作拟音"])

    # ================= 模式一：按表格批量生成环境背景音 =================
    with tab_bgm:
        if df is None or df.empty:
            st.info("请先在上方加载分镜表格（需包含 `序号`、`片段时长`、`背景音` 列）。")
        elif "背景音" not in df.columns:
            st.warning("当前表格中未检测到 `背景音` 列。")
        else:
            valid_rows = df[df["背景音"].notna() & (df["背景音"].astype(str).str.strip() != "")].copy()
            st.caption(f"共检测到 **{len(valid_rows)}** 条有效背景音需求（自动过滤空行）。")

            if st.button("🚀 批量生成整集背景音", type="primary", disabled=len(valid_rows) == 0):
                if not hf_token.strip():
                    st.error("请先填写 Hugging Face Token（hf_...）。")
                else:
                    with st.spinner("正在连接 Hugging Face MMAudio 服务并翻译提示词..."):
                        client = _get_mmaudio_client(hf_token)
                        cn_list = valid_rows["背景音"].astype(str).tolist()
                        en_map = translate_sfx_batch_with_gemini(gemini_client, cn_list)

                    results = []
                    progress = st.progress(0)
                    status_box = st.empty()

                    for idx, (_, row) in enumerate(valid_rows.iterrows()):
                        seq = row.get("序号", idx + 1)
                        cn_desc = str(row["背景音"]).strip()
                        en_prompt = en_map.get(cn_desc, cn_desc)

                        # 读取精确片段时长，限制在 MMAudio 推荐的 1~20 秒区间
                        try:
                            dur = float(row.get("片段时长", 6.0))
                        except (ValueError, TypeError):
                            dur = 6.0
                        dur = max(1.0, min(20.0, dur))

                        status_box.info(f"正在生成片段 {seq}（{dur:.1f}s）：{cn_desc} → `{en_prompt}`")
                        try:
                            # 参数顺序严格对应 hkchengrex/MMAudio 的 text_to_audio 接口
                            audio_path = _call_with_endpoint_fallback(
                                client,
                                ["/text_to_audio", "/predict_1", 1],
                                en_prompt,                 # Prompt
                                "music, speech, talking",  # Negative prompt（抑制不需要的人声和背景音乐）
                                -1,                        # Seed (-1: random)
                                25,                        # Num steps
                                4.5,                       # Guidance Strength
                                dur,                       # Duration (sec)
                            )
                            ext = os.path.splitext(audio_path)[1] or ".flac"
                            fname = f"片段{seq}_音效_{_safe_filename(cn_desc)}{ext}"
                            with open(audio_path, "rb") as f:
                                results.append((fname, f.read(), cn_desc, en_prompt))
                        except Exception as e:
                            st.error(f"片段 {seq} 生成失败：{e}")

                        progress.progress((idx + 1) / len(valid_rows))

                    status_box.success(f"生成完成！成功 {len(results)} / {len(valid_rows)} 条。")
                    st.session_state["mmaudio_bgm_results"] = results

            if st.session_state.get("mmaudio_bgm_results"):
                saved = st.session_state["mmaudio_bgm_results"]
                zip_buf = io.BytesIO()
                with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
                    for fname, data, _, _ in saved:
                        zf.writestr(fname, data)

                st.download_button(
                    label=f"📦 一键打包下载全部背景音 ({len(saved)} 条 .zip)",
                    data=zip_buf.getvalue(),
                    file_name="短剧背景音包.zip",
                    mime="application/zip",
                )
                for fname, data, cn_desc, en_prompt in saved:
                    with st.expander(f"🔊 {fname}（{cn_desc}）", expanded=False):
                        st.caption(f"英文提示词：`{en_prompt}`")
                        st.audio(data)

    # ================= 模式二：上传分镜视频自动匹配动作拟音 =================
    with tab_foley:
        st.caption("上传无声分镜视频（建议 5～12 秒），AI 会自动识别画面里的脚步、拍手、玻璃碎裂、爆炸并帧级对齐生成音效。")
        uploaded_videos = st.file_uploader(
            "上传分镜视频（支持多选）",
            type=["mp4", "mov", "webm"],
            accept_multiple_files=True,
        )
        foley_hint = st.text_input(
            "可选动作补充描述（英文效果更佳，留空则完全由 AI 看画面自动判断）",
            placeholder="例如：footsteps on concrete, glass shattering, heavy explosion",
        )

        if st.button("🎬 开始自动视频拟音", disabled=not uploaded_videos):
            if not hf_token.strip():
                st.error("请先填写 Hugging Face Token（hf_...）。")
            else:
                client = _get_mmaudio_client(hf_token)
                foley_outputs = []
                for vid in uploaded_videos:
                    with st.spinner(f"正在分析画面并合成同步音效：{vid.name} ..."):
                        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp_v:
                            tmp_v.write(vid.read())
                            tmp_v_path = tmp_v.name
                        try:
                            # 参数顺序严格对应 hkchengrex/MMAudio 的 video_to_audio 接口
                            out_video = _call_with_endpoint_fallback(
                                client,
                                ["/video_to_audio", "/predict", 0],
                                handle_file(tmp_v_path),  # Video
                                foley_hint.strip(),       # Prompt
                                "music, speech",          # Negative prompt
                                -1,                       # Seed
                                25,                       # Num steps
                                4.5,                      # Guidance Strength
                                8.0,                      # Duration (sec)
                            )
                            out_path = out_video["video"] if isinstance(out_video, dict) else out_video
                            with open(out_path, "rb") as f:
                                foley_outputs.append((f"拟音_{vid.name}", f.read()))
                        except Exception as e:
                            st.error(f"{vid.name} 拟音失败：{e}")
                        finally:
                            if os.path.exists(tmp_v_path):
                                os.remove(tmp_v_path)

                st.session_state["mmaudio_foley_results"] = foley_outputs

        if st.session_state.get("mmaudio_foley_results"):
            for out_name, v_bytes in st.session_state["mmaudio_foley_results"]:
                st.markdown(f"**{out_name}**")
                st.video(v_bytes)
                st.download_button(
                    label=f"⬇️ 下载 {out_name}",
                    data=v_bytes,
                    file_name=out_name,
                    mime="video/mp4",
                    key=f"dl_{out_name}",
                )
