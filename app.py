"""
PDF Crusher - Streamlit Web UI
ブラウザからPDFをアップロード → 目標サイズに圧縮 → ダウンロード
"""

import os
import tempfile

import streamlit as st

from pdf_crusher import (
    MODE_GHOSTSCRIPT,
    MODE_RASTERIZE,
    check_ghostscript,
    check_pdftoppm,
    format_size,
    get_page_count,
    ghostscript_iterative_compress,
    iterative_compress,
)

# ページ設定
st.set_page_config(
    page_title="PDF Crusher - 超圧縮ツール",
    page_icon="📄",
    layout="centered",
)

# カスタムCSS
st.markdown("""
<style>
    .main-title { text-align: center; padding: 0.5rem 0; }
    .result-box {
        background: #f0f7f0; border: 2px solid #28a745;
        border-radius: 12px; padding: 1.5rem;
        text-align: center; margin: 1rem 0;
    }
    .result-box-warn {
        background: #fff8e1; border: 2px solid #ffc107;
        border-radius: 12px; padding: 1.5rem;
        text-align: center; margin: 1rem 0;
    }
    .stat-number { font-size: 2rem; font-weight: bold; color: #1a73e8; }
</style>
""", unsafe_allow_html=True)

# ヘッダー
st.markdown('<h1 class="main-title">PDF Crusher</h1>', unsafe_allow_html=True)
st.markdown(
    '<p style="text-align:center;color:#666;margin-top:-10px;">'
    "PDFを確実に目標サイズ以下に圧縮するツール"
    "</p>",
    unsafe_allow_html=True,
)

# ツールの存在チェック
_pdftoppm_ok = True
_gs_ok = True
try:
    check_pdftoppm()
except SystemExit:
    _pdftoppm_ok = False
try:
    check_ghostscript()
except SystemExit:
    _gs_ok = False

if not _pdftoppm_ok and not _gs_ok:
    st.error("サーバーに poppler (pdftoppm) も Ghostscript (gs) もインストールされていません。")
    st.stop()

# セッション状態の初期化
if "result" not in st.session_state:
    st.session_state.result = None

# サイドバー設定
with st.sidebar:
    st.header("設定")

    # モード選択
    mode_options = {}
    if _pdftoppm_ok:
        mode_options["ラスタライズ（サイズ保証）"] = MODE_RASTERIZE
    if _gs_ok:
        mode_options["Ghostscript（テキスト保持）"] = MODE_GHOSTSCRIPT

    if len(mode_options) == 1:
        compress_mode = list(mode_options.values())[0]
        st.info(f"モード: {list(mode_options.keys())[0]}")
    else:
        mode_label = st.radio(
            "圧縮モード",
            options=list(mode_options.keys()),
            help="ラスタライズ: サイズ確実だがテキスト選択不可。Ghostscript: テキスト保持だがサイズはベストエフォート。",
        )
        compress_mode = mode_options[mode_label]

    st.divider()

    target_mb = st.select_slider(
        "目標サイズ",
        options=[0.5, 1, 2, 3, 5, 10, 15, 20],
        value=5,
        format_func=lambda x: f"{x} MB",
    )
    target_size = int(target_mb * 1024 * 1024)

    if compress_mode == MODE_RASTERIZE:
        min_dpi = st.select_slider(
            "最低DPI（品質下限）",
            options=[72, 85, 100, 120, 150],
            value=72,
            help="DPIが低いほど圧縮率が高いが、画質が落ちる",
        )
    else:
        min_dpi = 72  # Ghostscriptモードでは不使用

    st.divider()
    if compress_mode == MODE_RASTERIZE:
        st.markdown(
            "**仕組み:** PDF → 画像化 → JPEG圧縮 → PDF再構成。"
            "DPIとJPEG品質を自動調整し、確実に目標以下に。"
        )
        st.caption("テキスト選択はできなくなります（画像化のため）")
    else:
        st.markdown(
            "**仕組み:** GhostscriptでPDF内部の画像を再圧縮。"
            "テキスト・ベクター要素はそのまま保持。"
        )
        st.caption("テキスト選択・コピーが可能です")

# ファイルアップロード
uploaded_file = st.file_uploader(
    "PDFファイルをアップロード",
    type=["pdf"],
    help="最大200MBまで",
)

# ファイルが変わったら結果をクリア
if uploaded_file is not None:
    current_name = uploaded_file.name
    if st.session_state.get("last_file_name") != current_name:
        st.session_state.result = None
        st.session_state.last_file_name = current_name
elif st.session_state.get("last_file_name") is not None:
    st.session_state.result = None
    st.session_state.last_file_name = None


def run_compression(file_data: bytes, file_name: str, target_size: int, min_dpi: int, mode: str):
    """圧縮を実行し、結果をsession_stateに格納"""
    status_text = st.empty()
    progress_bar = st.progress(0)
    search_log = st.empty()
    search_entries = []

    def on_status(msg):
        status_text.markdown(f"**{msg}**")

    def on_search_step(quality_or_preset, estimated_or_size, is_ok):
        mark = "✓" if is_ok else "✗"
        if mode == MODE_GHOSTSCRIPT:
            entry = f"{quality_or_preset}: {format_size(estimated_or_size)} {mark}"
        else:
            entry = f"品質={quality_or_preset}: {format_size(estimated_or_size)} {mark}"
        search_entries.append(entry)
        search_log.code("\n".join(search_entries[-8:]))

    def on_progress(current, total):
        progress_bar.progress(current / total)

    tmp_input_path = None
    tmp_output_path = None
    try:
        # 入力を一時ファイルに保存
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp_in:
            tmp_in.write(file_data)
            tmp_input_path = tmp_in.name

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp_out:
            tmp_output_path = tmp_out.name

        if mode == MODE_GHOSTSCRIPT:
            actual_size, used_preset, used_dpi = ghostscript_iterative_compress(
                pdf_path=tmp_input_path,
                output_path=tmp_output_path,
                target_size=target_size,
                on_status=on_status,
                on_search_step=on_search_step,
                on_progress=on_progress,
            )
            st.session_state.result = {
                "compressed_data": None,
                "input_size": len(file_data),
                "actual_size": actual_size,
                "used_preset": used_preset,
                "used_dpi": used_dpi,
                "file_name": file_name,
                "mode": MODE_GHOSTSCRIPT,
            }
        else:
            actual_size, used_dpi, used_quality = iterative_compress(
                pdf_path=tmp_input_path,
                output_path=tmp_output_path,
                target_size=target_size,
                min_dpi=min_dpi,
                on_status=on_status,
                on_search_step=on_search_step,
                on_progress=on_progress,
            )
            st.session_state.result = {
                "compressed_data": None,
                "input_size": len(file_data),
                "actual_size": actual_size,
                "used_dpi": used_dpi,
                "used_quality": used_quality,
                "file_name": file_name,
                "mode": MODE_RASTERIZE,
            }

        # 結果データを読み込んでセッションに保存
        with open(tmp_output_path, "rb") as f:
            st.session_state.result["compressed_data"] = f.read()

    except RuntimeError as e:
        st.error(f"圧縮エラー: {e}")
    finally:
        for p in [tmp_input_path, tmp_output_path]:
            if p and os.path.exists(p):
                os.unlink(p)

    # UIクリーンアップ
    progress_bar.empty()
    status_text.empty()
    search_log.empty()


if uploaded_file is not None:
    file_data = uploaded_file.getvalue()
    input_size = len(file_data)

    # ファイル情報
    col1, col2 = st.columns(2)

    # ページ数取得
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(file_data)
        tmp_path = tmp.name
    try:
        page_count = get_page_count(tmp_path)
    finally:
        os.unlink(tmp_path)

    with col1:
        st.metric("ファイルサイズ", format_size(input_size))
    with col2:
        st.metric("ページ数", f"{page_count} ページ")

    # 既に目標以下
    if input_size <= target_size:
        st.success(f"既に目標サイズ（{format_size(target_size)}）以下です。")
        st.download_button(
            label="ダウンロード（元ファイル）",
            data=file_data,
            file_name=uploaded_file.name,
            mime="application/pdf",
        )
    # 前回の圧縮結果がある場合
    elif st.session_state.result is not None:
        r = st.session_state.result
        reduction = (1 - r["actual_size"] / r["input_size"]) * 100
        achieved = r["actual_size"] <= target_size

        css_class = "result-box" if achieved else "result-box-warn"
        title = "圧縮完了" if achieved else "圧縮完了（目標未達）"
        extra = "" if achieved else f'<p>目標 {format_size(target_size)} に対し超過</p>'

        if r.get("mode") == MODE_GHOSTSCRIPT:
            setting_str = f'プリセット={r["used_preset"]} | DPI={r["used_dpi"]}'
            mode_str = "Ghostscript（テキスト保持）"
        else:
            setting_str = f'DPI={r["used_dpi"]} | 品質={r["used_quality"]}'
            mode_str = "ラスタライズ（サイズ保証）"

        st.markdown(
            f'<div class="{css_class}">'
            f"<p>{title}</p>"
            f'<p class="stat-number">{format_size(r["input_size"])} → {format_size(r["actual_size"])}</p>'
            f"<p>{reduction:.1f}% 削減 | {setting_str}</p>"
            f"<p><small>{mode_str}</small></p>"
            f"{extra}</div>",
            unsafe_allow_html=True,
        )

        stem = os.path.splitext(r["file_name"])[0]
        st.download_button(
            label=f"圧縮済みPDFをダウンロード（{format_size(r['actual_size'])}）",
            data=r["compressed_data"],
            file_name=f"{stem}_compressed.pdf",
            mime="application/pdf",
            type="primary",
            use_container_width=True,
        )

        if st.button("設定を変えて再圧縮"):
            st.session_state.result = None
            st.rerun()
    else:
        # 圧縮ボタン
        mode_hint = "テキスト保持" if compress_mode == MODE_GHOSTSCRIPT else "サイズ保証"
        st.info(f"目標: {format_size(target_size)} 以下に圧縮します（現在 {format_size(input_size)}）【{mode_hint}モード】")
        if st.button("圧縮開始", type="primary", use_container_width=True):
            run_compression(file_data, uploaded_file.name, target_size, min_dpi, compress_mode)
            st.rerun()
