"""
PDF Crusher - Streamlit Web UI
ブラウザからPDFをアップロード → 目標サイズに圧縮 → ダウンロード
"""

import io
import os
import tempfile
import zipfile

import streamlit as st

from pdf_crusher import (
    MODE_AUTO,
    MODE_GHOSTSCRIPT,
    MODE_RASTERIZE,
    auto_compress,
    check_ghostscript,
    check_pdftoppm,
    extract_pages,
    format_size,
    get_page_count,
    ghostscript_iterative_compress,
    has_ghostscript,
    has_pdftoppm,
    iterative_compress,
    parse_page_ranges,
    render_page_preview,
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
    .preview-label {
        text-align: center; font-weight: bold;
        margin-bottom: 0.5rem; color: #555;
    }
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
    st.error("サーバーに poppler (pdftoppm) も Ghostscript (gs) も"
             "インストールされていません。")
    st.stop()

# セッション状態の初期化
if "result" not in st.session_state:
    st.session_state.result = None
if "batch_results" not in st.session_state:
    st.session_state.batch_results = None

# ---------------------------------------------------------------------------
# サイドバー設定
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("設定")

    # モード選択
    mode_options = {}
    if _pdftoppm_ok and _gs_ok:
        mode_options["自動（テキスト保持優先）"] = MODE_AUTO
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
            help=("自動: GS→ラスタライズの順に試行。"
                  "ラスタライズ: サイズ確実だがテキスト選択不可。"
                  "Ghostscript: テキスト保持だがサイズはベストエフォート。"),
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

    if compress_mode in (MODE_RASTERIZE, MODE_AUTO):
        min_dpi = st.select_slider(
            "最低DPI（品質下限）",
            options=[72, 85, 100, 120, 150],
            value=72,
            help="DPIが低いほど圧縮率が高いが、画質が落ちる",
        )
    else:
        min_dpi = 72

    st.divider()

    # グレースケール
    grayscale = st.toggle(
        "グレースケール変換",
        value=False,
        help="カラー→白黒に変換。モノクロ書類で大幅にサイズ削減。",
    )

    # ページ範囲
    pages_str = st.text_input(
        "ページ範囲（空欄で全ページ）",
        placeholder="例: 1-5,10,15-20",
        help="特定ページだけ抽出して圧縮。",
    ).strip() or None

    st.divider()

    # モード説明
    if compress_mode == MODE_AUTO:
        st.markdown(
            "**自動モード:** まずGhostscriptでテキスト保持を試み、"
            "目標未達ならラスタライズに自動切替。"
        )
        st.caption("テキスト保持とサイズ保証の両立を目指します")
    elif compress_mode == MODE_RASTERIZE:
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

# ---------------------------------------------------------------------------
# ファイルアップロード（複数対応）
# ---------------------------------------------------------------------------
uploaded_files = st.file_uploader(
    "PDFファイルをアップロード",
    type=["pdf"],
    accept_multiple_files=True,
    help="最大200MBまで。複数ファイル対応。",
)

# ファイルが変わったら結果をクリア
if uploaded_files:
    current_names = tuple(sorted(f.name for f in uploaded_files))
    if st.session_state.get("last_file_names") != current_names:
        st.session_state.result = None
        st.session_state.batch_results = None
        st.session_state.last_file_names = current_names
elif st.session_state.get("last_file_names") is not None:
    st.session_state.result = None
    st.session_state.batch_results = None
    st.session_state.last_file_names = None


# ---------------------------------------------------------------------------
# 圧縮実行関数
# ---------------------------------------------------------------------------

def _run_single_compression(
    file_data: bytes, file_name: str, target_size: int,
    min_dpi: int, mode: str, grayscale: bool,
    pages_str: str | None,
    status_text, progress_bar, search_log,
) -> dict | None:
    """単一ファイルの圧縮。結果dictを返す。"""
    search_entries = []

    def on_status(msg):
        status_text.markdown(f"**{msg}**")

    def on_search_step(quality_or_preset, estimated_or_size, is_ok):
        mark = "✓" if is_ok else "✗"
        if mode in (MODE_GHOSTSCRIPT, MODE_AUTO):
            entry = (f"{quality_or_preset}: "
                     f"{format_size(estimated_or_size)} {mark}")
        else:
            entry = (f"品質={quality_or_preset}: "
                     f"{format_size(estimated_or_size)} {mark}")
        search_entries.append(entry)
        search_log.code("\n".join(search_entries[-8:]))

    def on_progress(current, total):
        progress_bar.progress(current / total)

    tmp_input_path = None
    tmp_output_path = None
    tmp_extracted = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf",
                                         delete=False) as tmp_in:
            tmp_in.write(file_data)
            tmp_input_path = tmp_in.name

        with tempfile.NamedTemporaryFile(suffix=".pdf",
                                         delete=False) as tmp_out:
            tmp_output_path = tmp_out.name

        work_pdf = tmp_input_path
        pages = None

        # ページ範囲
        if pages_str:
            page_count = get_page_count(tmp_input_path)
            pages = parse_page_ranges(pages_str, page_count)
            if mode in (MODE_GHOSTSCRIPT, MODE_AUTO) and has_ghostscript():
                with tempfile.NamedTemporaryFile(suffix=".pdf",
                                                 delete=False) as tmp_ex:
                    tmp_extracted = tmp_ex.name
                extract_pages(tmp_input_path, pages, tmp_extracted)
                work_pdf = tmp_extracted
                pages = None

        if mode == MODE_AUTO:
            result = auto_compress(
                pdf_path=work_pdf,
                output_path=tmp_output_path,
                target_size=target_size,
                min_dpi=min_dpi,
                grayscale=grayscale,
                pages=pages,
                on_status=on_status,
                on_search_step=on_search_step,
                on_progress=on_progress,
            )
            with open(tmp_output_path, "rb") as f:
                compressed_data = f.read()
            actual_size = result["actual_size"]
            res = {
                "compressed_data": compressed_data,
                "input_size": len(file_data),
                "actual_size": actual_size,
                "file_name": file_name,
                "mode": result["mode"],
                "auto": True,
            }
            if result["mode"] == MODE_GHOSTSCRIPT:
                res["used_preset"] = result["used_preset"]
                res["used_dpi"] = result["used_dpi"]
            else:
                res["used_dpi"] = result["used_dpi"]
                res["used_quality"] = result["used_quality"]
            return res

        elif mode == MODE_GHOSTSCRIPT:
            actual_size, used_preset, used_dpi = ghostscript_iterative_compress(
                pdf_path=work_pdf,
                output_path=tmp_output_path,
                target_size=target_size,
                grayscale=grayscale,
                on_status=on_status,
                on_search_step=on_search_step,
                on_progress=on_progress,
            )
            with open(tmp_output_path, "rb") as f:
                compressed_data = f.read()
            return {
                "compressed_data": compressed_data,
                "input_size": len(file_data),
                "actual_size": actual_size,
                "used_preset": used_preset,
                "used_dpi": used_dpi,
                "file_name": file_name,
                "mode": MODE_GHOSTSCRIPT,
            }
        else:
            actual_size, used_dpi, used_quality = iterative_compress(
                pdf_path=work_pdf,
                output_path=tmp_output_path,
                target_size=target_size,
                min_dpi=min_dpi,
                grayscale=grayscale,
                pages=pages,
                on_status=on_status,
                on_search_step=on_search_step,
                on_progress=on_progress,
            )
            with open(tmp_output_path, "rb") as f:
                compressed_data = f.read()
            return {
                "compressed_data": compressed_data,
                "input_size": len(file_data),
                "actual_size": actual_size,
                "used_dpi": used_dpi,
                "used_quality": used_quality,
                "file_name": file_name,
                "mode": MODE_RASTERIZE,
            }

    except RuntimeError as e:
        st.error(f"圧縮エラー ({file_name}): {e}")
        return None
    finally:
        for p in [tmp_input_path, tmp_output_path, tmp_extracted]:
            if p and os.path.exists(p):
                os.unlink(p)


def _generate_preview(file_data: bytes, label: str) -> bytes | None:
    """PDFデータから1ページ目のプレビューPNG bytesを生成。"""
    if not _pdftoppm_ok:
        return None
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(file_data)
        tmp_path = tmp.name
    try:
        return render_page_preview(tmp_path, page=1, dpi=100)
    finally:
        os.unlink(tmp_path)


def _display_result(r: dict, target_size: int, show_preview: bool = True):
    """結果表示（単一ファイル用）"""
    reduction = (1 - r["actual_size"] / r["input_size"]) * 100
    achieved = r["actual_size"] <= target_size

    css_class = "result-box" if achieved else "result-box-warn"
    title = "圧縮完了" if achieved else "圧縮完了（目標未達）"
    extra = ("" if achieved
             else f'<p>目標 {format_size(target_size)} に対し超過</p>')

    actual_mode = r.get("mode", MODE_RASTERIZE)
    is_auto = r.get("auto", False)

    if actual_mode == MODE_GHOSTSCRIPT:
        setting_str = (f'プリセット={r["used_preset"]} | '
                       f'DPI={r["used_dpi"]}')
        mode_str = ("自動 → Ghostscript（テキスト保持）" if is_auto
                    else "Ghostscript（テキスト保持）")
    else:
        setting_str = f'DPI={r["used_dpi"]} | 品質={r["used_quality"]}'
        mode_str = ("自動 → ラスタライズ（サイズ保証）" if is_auto
                    else "ラスタライズ（サイズ保証）")

    st.markdown(
        f'<div class="{css_class}">'
        f"<p>{title}</p>"
        f'<p class="stat-number">'
        f'{format_size(r["input_size"])} → {format_size(r["actual_size"])}'
        f"</p>"
        f"<p>{reduction:.1f}% 削減 | {setting_str}</p>"
        f"<p><small>{mode_str}</small></p>"
        f"{extra}</div>",
        unsafe_allow_html=True,
    )

    # プレビュー（before / after）
    if show_preview and _pdftoppm_ok:
        preview_before = st.session_state.get("preview_before")
        preview_after = _generate_preview(r["compressed_data"], "after")
        if preview_before or preview_after:
            st.markdown("#### 1ページ目プレビュー")
            col_b, col_a = st.columns(2)
            with col_b:
                st.markdown('<p class="preview-label">圧縮前</p>',
                            unsafe_allow_html=True)
                if preview_before:
                    st.image(preview_before, use_container_width=True)
                else:
                    st.caption("プレビューなし")
            with col_a:
                st.markdown('<p class="preview-label">圧縮後</p>',
                            unsafe_allow_html=True)
                if preview_after:
                    st.image(preview_after, use_container_width=True)
                else:
                    st.caption("プレビューなし")


# ---------------------------------------------------------------------------
# メイン表示
# ---------------------------------------------------------------------------

if uploaded_files and len(uploaded_files) == 1:
    # ===== 単一ファイルモード =====
    uploaded_file = uploaded_files[0]
    file_data = uploaded_file.getvalue()
    input_size = len(file_data)

    col1, col2 = st.columns(2)

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

    # ページ範囲バリデーション
    if pages_str:
        try:
            parsed_pages = parse_page_ranges(pages_str, page_count)
            st.caption(f"対象: {len(parsed_pages)}/{page_count} ページ")
        except ValueError as e:
            st.error(str(e))
            st.stop()

    if input_size <= target_size and not pages_str:
        st.success(f"既に目標サイズ（{format_size(target_size)}）以下です。")
        st.download_button(
            label="ダウンロード（元ファイル）",
            data=file_data,
            file_name=uploaded_file.name,
            mime="application/pdf",
        )
    elif st.session_state.result is not None:
        _display_result(st.session_state.result, target_size)

        r = st.session_state.result
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
            st.session_state.pop("preview_before", None)
            st.rerun()
    else:
        options_parts = []
        if compress_mode == MODE_AUTO:
            options_parts.append("自動モード")
        elif compress_mode == MODE_GHOSTSCRIPT:
            options_parts.append("テキスト保持")
        else:
            options_parts.append("サイズ保証")
        if grayscale:
            options_parts.append("グレースケール")
        if pages_str:
            options_parts.append(f"ページ: {pages_str}")
        opts_label = " / ".join(options_parts)

        st.info(f"目標: {format_size(target_size)} 以下に圧縮"
                f"（現在 {format_size(input_size)}）【{opts_label}】")
        if st.button("圧縮開始", type="primary", use_container_width=True):
            # 圧縮前プレビュー生成
            st.session_state["preview_before"] = _generate_preview(
                file_data, "before")

            status_text = st.empty()
            progress_bar = st.progress(0)
            search_log = st.empty()

            result = _run_single_compression(
                file_data, uploaded_file.name, target_size,
                min_dpi, compress_mode, grayscale, pages_str,
                status_text, progress_bar, search_log,
            )

            progress_bar.empty()
            status_text.empty()
            search_log.empty()

            if result is not None:
                st.session_state.result = result
            st.rerun()

elif uploaded_files and len(uploaded_files) > 1:
    # ===== 複数ファイルモード =====
    st.markdown(f"**{len(uploaded_files)} ファイルを選択中**")

    # 一覧
    for uf in uploaded_files:
        st.caption(f"  {uf.name} ({format_size(len(uf.getvalue()))})")

    options_parts = []
    if compress_mode == MODE_AUTO:
        options_parts.append("自動")
    elif compress_mode == MODE_GHOSTSCRIPT:
        options_parts.append("GS")
    else:
        options_parts.append("ラスタライズ")
    if grayscale:
        options_parts.append("グレースケール")
    opts_label = " / ".join(options_parts)

    st.info(f"目標: {format_size(target_size)} 以下 【{opts_label}】")

    if st.session_state.batch_results is not None:
        # 結果表示
        results = st.session_state.batch_results
        success_count = sum(1 for r in results if r and
                           r["actual_size"] <= target_size)
        st.success(
            f"一括圧縮完了: {success_count}/{len(results)} ファイル目標達成"
        )

        for r in results:
            if r is None:
                continue
            reduction = (1 - r["actual_size"] / r["input_size"]) * 100
            achieved = r["actual_size"] <= target_size
            mark = "✓" if achieved else "✗"
            st.caption(
                f"{r['file_name']}: "
                f"{format_size(r['input_size'])} → "
                f"{format_size(r['actual_size'])} "
                f"({reduction:.0f}%削減) {mark}"
            )

        # ZIPダウンロード
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for r in results:
                if r is None or r["compressed_data"] is None:
                    continue
                stem = os.path.splitext(r["file_name"])[0]
                zf.writestr(f"{stem}_compressed.pdf", r["compressed_data"])
        zip_buf.seek(0)

        st.download_button(
            label="全ファイルをZIPでダウンロード",
            data=zip_buf.getvalue(),
            file_name="pdf_crusher_results.zip",
            mime="application/zip",
            type="primary",
            use_container_width=True,
        )

        if st.button("設定を変えて再圧縮"):
            st.session_state.batch_results = None
            st.rerun()

    elif st.button("一括圧縮開始", type="primary", use_container_width=True):
        results = []
        overall_progress = st.progress(0)
        file_status = st.empty()
        status_text = st.empty()
        progress_bar = st.progress(0)
        search_log = st.empty()

        for idx, uf in enumerate(uploaded_files):
            file_status.markdown(
                f"**[{idx+1}/{len(uploaded_files)}] {uf.name}**"
            )
            overall_progress.progress((idx) / len(uploaded_files))

            r = _run_single_compression(
                uf.getvalue(), uf.name, target_size,
                min_dpi, compress_mode, grayscale, pages_str,
                status_text, progress_bar, search_log,
            )
            results.append(r)

            progress_bar.progress(0)
            search_log.empty()
            status_text.empty()

        overall_progress.progress(1.0)
        file_status.empty()
        overall_progress.empty()
        progress_bar.empty()
        search_log.empty()

        st.session_state.batch_results = results
        st.rerun()
