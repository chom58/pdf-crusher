#!/usr/bin/env python3
"""
PDF Crusher - 超圧縮ツール
PDFを確実に目標サイズ以下に圧縮する。
ラスタライズ方式・Ghostscript方式・自動モード対応。
"""

import argparse
import glob as globmod
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image
from tqdm import tqdm

try:
    import img2pdf
except ImportError:
    print("エラー: img2pdf がインストールされていません。pip install img2pdf を実行してください。")
    sys.exit(1)


# 圧縮モード
MODE_RASTERIZE = "rasterize"
MODE_GHOSTSCRIPT = "ghostscript"
MODE_AUTO = "auto"

# DPIレベル: 高い方から試行し、目標未達なら下げていく
DPI_LEVELS = [200, 150, 120, 100, 85, 72]

# Ghostscript の品質プリセット（高品質→低品質の順）
GS_QUALITY_PRESETS = [
    ("prepress", "/prepress"),
    ("printer", "/printer"),
    ("ebook", "/ebook"),
    ("screen", "/screen"),
]

# JPEG品質の探索範囲
QUALITY_MIN = 5
QUALITY_MAX = 85

# サイズ推定時の安全マージン（10%上乗せ）
SAFETY_MARGIN = 1.10

# img2pdfのオーバーヘッド概算（ページあたり）
PDF_OVERHEAD_PER_PAGE = 500  # bytes


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------

def parse_size(size_str: str) -> int:
    """'5MB', '500KB' などの文字列をバイト数に変換"""
    size_str = size_str.strip().upper()
    match = re.match(r"^(\d+(?:\.\d+)?)\s*(KB|MB|GB|B)?$", size_str)
    if not match:
        raise argparse.ArgumentTypeError(
            f"サイズ形式が不正です: '{size_str}' (例: 5MB, 500KB, 1GB)"
        )
    value = float(match.group(1))
    unit = match.group(2) or "B"
    multipliers = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3}
    return int(value * multipliers[unit])


def format_size(size_bytes: int) -> str:
    """バイト数を人間が読みやすい形式に変換"""
    if size_bytes >= 1024**3:
        return f"{size_bytes / 1024**3:.1f} GB"
    if size_bytes >= 1024**2:
        return f"{size_bytes / 1024**2:.1f} MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes} B"


def parse_page_ranges(pages_str: str, total_pages: int) -> list[int]:
    """
    ページ範囲文字列をページ番号リスト（1始まり）に変換。
    例: "1-5,10,15-20" → [1,2,3,4,5,10,15,16,17,18,19,20]
    """
    pages = set()
    for part in pages_str.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_str, end_str = part.split("-", 1)
            start = int(start_str.strip())
            end = int(end_str.strip())
            if start < 1 or end > total_pages or start > end:
                raise ValueError(
                    f"ページ範囲が不正です: {part} (全{total_pages}ページ)"
                )
            pages.update(range(start, end + 1))
        else:
            p = int(part)
            if p < 1 or p > total_pages:
                raise ValueError(
                    f"ページ番号が不正です: {p} (全{total_pages}ページ)"
                )
            pages.add(p)
    return sorted(pages)


def _group_contiguous(pages: list[int]) -> list[tuple[int, int]]:
    """ソート済みページ番号リストを連続範囲にグループ化。"""
    if not pages:
        return []
    ranges = []
    start = pages[0]
    end = pages[0]
    for p in pages[1:]:
        if p == end + 1:
            end = p
        else:
            ranges.append((start, end))
            start = p
            end = p
    ranges.append((start, end))
    return ranges


# ---------------------------------------------------------------------------
# ツール存在チェック
# ---------------------------------------------------------------------------

def check_pdftoppm():
    if shutil.which("pdftoppm") is None:
        print("エラー: pdftoppm が見つかりません。")
        print("  macOS: brew install poppler")
        print("  Ubuntu: sudo apt install poppler-utils")
        sys.exit(1)


def check_ghostscript():
    if shutil.which("gs") is None:
        print("エラー: Ghostscript (gs) が見つかりません。")
        print("  macOS: brew install ghostscript")
        print("  Ubuntu: sudo apt install ghostscript")
        sys.exit(1)


def has_pdftoppm() -> bool:
    return shutil.which("pdftoppm") is not None


def has_ghostscript() -> bool:
    return shutil.which("gs") is not None


# ---------------------------------------------------------------------------
# PDF情報・ページ操作
# ---------------------------------------------------------------------------

def get_page_count(pdf_path: str) -> int:
    """PDFのページ数を取得"""
    try:
        result = subprocess.run(
            ["pdfinfo", pdf_path],
            capture_output=True, text=True, timeout=30,
        )
        for line in result.stdout.splitlines():
            if line.startswith("Pages:"):
                return int(line.split(":")[1].strip())
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # フォールバック
    with tempfile.TemporaryDirectory() as tmpdir:
        subprocess.run(
            ["pdftoppm", "-png", "-r", "10", pdf_path,
             os.path.join(tmpdir, "page")],
            capture_output=True, timeout=120,
        )
        return len(list(Path(tmpdir).glob("page-*.png")))


def extract_pages(pdf_path: str, pages: list[int], output_path: str):
    """Ghostscriptで指定ページのみ抽出したPDFを作成。"""
    ranges = _group_contiguous(pages)

    temp_files = []
    try:
        for s, e in ranges:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp_path = tmp.name
            cmd = [
                "gs", "-sDEVICE=pdfwrite", "-dNOPAUSE", "-dBATCH", "-dQUIET",
                f"-dFirstPage={s}", f"-dLastPage={e}",
                f"-sOutputFile={tmp_path}", pdf_path,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                raise RuntimeError(f"ページ抽出エラー: {result.stderr.strip()}")
            temp_files.append(tmp_path)

        if len(temp_files) == 1:
            shutil.move(temp_files[0], output_path)
            temp_files.clear()
        else:
            cmd = [
                "gs", "-sDEVICE=pdfwrite", "-dNOPAUSE", "-dBATCH", "-dQUIET",
                f"-sOutputFile={output_path}",
            ] + temp_files
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                raise RuntimeError(f"ページ結合エラー: {result.stderr.strip()}")
    finally:
        for f in temp_files:
            if os.path.exists(f):
                os.unlink(f)


def render_page_preview(pdf_path: str, page: int = 1, dpi: int = 72) -> bytes | None:
    """指定ページをPNG画像としてレンダリング。プレビュー用。"""
    if not has_pdftoppm():
        return None
    with tempfile.TemporaryDirectory() as tmpdir:
        prefix = os.path.join(tmpdir, "preview")
        cmd = [
            "pdftoppm", "-png", "-r", str(dpi),
            "-f", str(page), "-l", str(page),
            pdf_path, prefix,
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=30)
        if result.returncode != 0:
            return None
        images = sorted(Path(tmpdir).glob("preview-*.png"))
        if not images:
            return None
        return images[0].read_bytes()


# ---------------------------------------------------------------------------
# Ghostscript 圧縮
# ---------------------------------------------------------------------------

def gs_compress(pdf_path: str, output_path: str, preset: str,
                grayscale: bool = False, verbose: bool = False) -> int:
    cmd = [
        "gs", "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.4",
        f"-dPDFSETTINGS={preset}",
        "-dNOPAUSE", "-dBATCH", "-dQUIET",
        "-dColorImageResolution=150",
        "-dGrayImageResolution=150",
        "-dMonoImageResolution=300",
    ]
    if grayscale:
        cmd += ["-sColorConversionStrategy=Gray",
                "-dProcessColorModel=/DeviceGray"]
    cmd += [f"-sOutputFile={output_path}", pdf_path]

    if verbose:
        print(f"  Ghostscript 実行中 (プリセット={preset})...")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(f"Ghostscript エラー: {result.stderr.strip()}")
    return os.path.getsize(output_path)


def gs_compress_with_downsampling(
    pdf_path: str, output_path: str, preset: str,
    color_dpi: int, gray_dpi: int, mono_dpi: int,
    grayscale: bool = False, verbose: bool = False,
) -> int:
    cmd = [
        "gs", "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.4",
        f"-dPDFSETTINGS={preset}",
        "-dNOPAUSE", "-dBATCH", "-dQUIET",
        "-dDownsampleColorImages=true",
        "-dDownsampleGrayImages=true",
        "-dDownsampleMonoImages=true",
        f"-dColorImageResolution={color_dpi}",
        f"-dGrayImageResolution={gray_dpi}",
        f"-dMonoImageResolution={mono_dpi}",
    ]
    if grayscale:
        cmd += ["-sColorConversionStrategy=Gray",
                "-dProcessColorModel=/DeviceGray"]
    cmd += [f"-sOutputFile={output_path}", pdf_path]

    if verbose:
        print(f"  Ghostscript 実行中 (プリセット={preset}, DPI={color_dpi})...")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(f"Ghostscript エラー: {result.stderr.strip()}")
    return os.path.getsize(output_path)


def ghostscript_iterative_compress(
    pdf_path: str,
    output_path: str,
    target_size: int,
    grayscale: bool = False,
    verbose: bool = False,
    on_status: callable = None,
    on_search_step: callable = None,
    on_progress: callable = None,
) -> tuple[int, str, int]:
    """
    Ghostscriptによる反復圧縮。テキスト選択を保持したまま圧縮。
    Returns: (output_size, preset_name, color_dpi)
    """
    def _status(msg):
        print(msg)
        if on_status:
            on_status(msg)

    dpi_for_preset = {
        "/prepress": 300, "/printer": 300, "/ebook": 150, "/screen": 72,
    }

    for preset_name, preset in GS_QUALITY_PRESETS:
        _status(f"Ghostscript: プリセット={preset_name} で圧縮中...")
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            actual_size = gs_compress(pdf_path, tmp_path, preset,
                                      grayscale, verbose)
            default_dpi = dpi_for_preset[preset]
            is_ok = actual_size <= target_size
            mark = "✓" if is_ok else "✗"
            _status(f"  プリセット={preset_name}: {format_size(actual_size)} {mark}")
            if on_search_step:
                on_search_step(preset_name, actual_size, is_ok)
            if is_ok:
                shutil.copy2(tmp_path, output_path)
                if on_progress:
                    on_progress(1, 1)
                return actual_size, preset_name, default_dpi
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    extra_dpis = [50, 36, 24]
    for dpi in extra_dpis:
        _status(f"Ghostscript: screen + DPI={dpi} で圧縮中...")
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            actual_size = gs_compress_with_downsampling(
                pdf_path, tmp_path, "/screen",
                color_dpi=dpi, gray_dpi=dpi, mono_dpi=dpi * 2,
                grayscale=grayscale, verbose=verbose,
            )
            is_ok = actual_size <= target_size
            mark = "✓" if is_ok else "✗"
            _status(f"  screen+DPI={dpi}: {format_size(actual_size)} {mark}")
            if on_search_step:
                on_search_step(f"screen (DPI={dpi})", actual_size, is_ok)
            if is_ok:
                shutil.copy2(tmp_path, output_path)
                if on_progress:
                    on_progress(1, 1)
                return actual_size, f"screen (DPI={dpi})", dpi
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    # 最低設定でも未達
    _status("警告: Ghostscriptで目標未達。最低設定で出力します。")
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        actual_size = gs_compress_with_downsampling(
            pdf_path, tmp_path, "/screen",
            color_dpi=24, gray_dpi=24, mono_dpi=48,
            grayscale=grayscale, verbose=verbose,
        )
        shutil.copy2(tmp_path, output_path)
        if on_progress:
            on_progress(1, 1)
        return actual_size, "screen (DPI=24)", 24
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# ラスタライズ圧縮
# ---------------------------------------------------------------------------

def pdf_to_images(pdf_path: str, dpi: int, output_dir: str,
                  pages: list[int] | None = None,
                  verbose: bool = False) -> list[str]:
    """pdftoppmでPDFをPNG画像に変換。"""
    if pages is not None:
        all_images = []
        for start, end in _group_contiguous(pages):
            prefix = os.path.join(output_dir, f"page_r{start}")
            cmd = [
                "pdftoppm", "-png", "-r", str(dpi),
                "-f", str(start), "-l", str(end),
                pdf_path, prefix,
            ]
            if verbose:
                print(f"  pdftoppm (DPI={dpi}, ページ {start}-{end})...")
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    timeout=600)
            if result.returncode != 0:
                stderr = result.stderr.strip()
                if "password" in stderr.lower():
                    print("エラー: パスワード付きPDFは処理できません。")
                    sys.exit(1)
                raise RuntimeError(f"pdftoppm エラー: {stderr}")
            imgs = sorted(Path(output_dir).glob(f"page_r{start}-*.png"),
                          key=lambda p: p.name)
            all_images.extend(str(p) for p in imgs)
        return all_images

    prefix = os.path.join(output_dir, "page")
    cmd = ["pdftoppm", "-png", "-r", str(dpi), pdf_path, prefix]
    if verbose:
        print(f"  pdftoppm 実行中 (DPI={dpi})...")

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "password" in stderr.lower():
            print("エラー: パスワード付きPDFは処理できません。")
            sys.exit(1)
        raise RuntimeError(f"pdftoppm エラー: {stderr}")

    images = sorted(Path(output_dir).glob("page-*.png"), key=lambda p: p.name)
    return [str(p) for p in images]


def compress_image(png_path: str, quality: int,
                   grayscale: bool = False) -> bytes:
    """PNG画像をJPEG圧縮してbytesで返却"""
    from io import BytesIO
    with Image.open(png_path) as img:
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        if grayscale:
            img = img.convert("L")
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()


def estimate_total_size(image_paths: list[str], quality: int,
                        grayscale: bool = False,
                        max_samples: int = 5) -> int:
    """サンプルページからJPEG圧縮後の合計サイズを推定"""
    total_pages = len(image_paths)
    if total_pages == 0:
        return 0

    sample_count = min(max_samples, total_pages)
    if sample_count >= total_pages:
        sample_indices = list(range(total_pages))
    else:
        step = total_pages / sample_count
        sample_indices = [int(i * step) for i in range(sample_count)]

    sample_sizes = []
    for idx in sample_indices:
        jpeg_data = compress_image(image_paths[idx], quality, grayscale)
        sample_sizes.append(len(jpeg_data))

    avg_size = sum(sample_sizes) / len(sample_sizes)
    estimated = int(avg_size * total_pages)
    estimated += PDF_OVERHEAD_PER_PAGE * total_pages
    return int(estimated * SAFETY_MARGIN)


def images_to_pdf(
    image_paths: list[str],
    quality: int,
    output_path: str,
    grayscale: bool = False,
    verbose: bool = False,
    on_progress: callable = None,
) -> int:
    """画像をJPEG圧縮してPDFに変換。出力ファイルサイズを返す。"""
    jpeg_data_list = []
    total = len(image_paths)

    iterator = (tqdm(image_paths, desc="圧縮中", unit="ページ")
                if not verbose else image_paths)
    for i, png_path in enumerate(iterator):
        if verbose:
            print(f"  ページ {i+1}/{total} を圧縮中...")
        jpeg_data = compress_image(png_path, quality, grayscale)
        jpeg_data_list.append(jpeg_data)
        if on_progress:
            on_progress(i + 1, total)

    pdf_bytes = img2pdf.convert(jpeg_data_list)
    with open(output_path, "wb") as f:
        f.write(pdf_bytes)

    return os.path.getsize(output_path)


def find_best_quality(
    image_paths: list[str],
    target_size: int,
    dpi: int,
    grayscale: bool = False,
    verbose: bool = False,
    on_search_step: callable = None,
) -> int | None:
    """バイナリサーチで目標サイズ以下となる最高品質を探索。"""
    lo, hi = QUALITY_MIN, QUALITY_MAX
    best_quality = None

    while lo <= hi:
        mid = (lo + hi) // 2
        estimated = estimate_total_size(image_paths, mid, grayscale)
        is_ok = estimated <= target_size

        mark = "✓" if is_ok else "✗"
        if verbose:
            print(f"  品質={mid}: {format_size(estimated)} (推定) {mark}")
        if on_search_step:
            on_search_step(mid, estimated, is_ok)

        if is_ok:
            best_quality = mid
            lo = mid + 1
        else:
            hi = mid - 1

    return best_quality


def iterative_compress(
    pdf_path: str,
    output_path: str,
    target_size: int,
    min_dpi: int = 72,
    grayscale: bool = False,
    pages: list[int] | None = None,
    verbose: bool = False,
    on_status: callable = None,
    on_search_step: callable = None,
    on_progress: callable = None,
) -> tuple[int, int, int]:
    """
    反復圧縮: DPIを段階的に下げながら、各DPIで最適品質をバイナリサーチ。
    Returns: (output_size, used_dpi, used_quality)
    """
    dpi_levels = [d for d in DPI_LEVELS if d >= min_dpi]
    if not dpi_levels:
        dpi_levels = [min_dpi]

    def _status(msg):
        print(msg)
        if on_status:
            on_status(msg)

    for dpi in dpi_levels:
        _status(f"DPI={dpi}, 品質探索中...")

        with tempfile.TemporaryDirectory() as tmpdir:
            _status(f"DPI={dpi} で画像化中...")
            image_paths = pdf_to_images(pdf_path, dpi, tmpdir, pages, verbose)
            if not image_paths:
                _status("警告: 画像が生成されませんでした。次のDPIを試行...")
                continue

            _status(f"DPI={dpi}, 最適品質を探索中...")
            best_quality = find_best_quality(
                image_paths, target_size, dpi, grayscale, verbose,
                on_search_step=on_search_step,
            )

            if best_quality is not None:
                _status(f"最適設定: DPI={dpi}, 品質={best_quality}")
                _status("最終PDFを生成中...")
                actual_size = images_to_pdf(
                    image_paths, best_quality, output_path,
                    grayscale, verbose, on_progress=on_progress,
                )

                if actual_size > target_size and best_quality > QUALITY_MIN:
                    if verbose:
                        print(f"  実サイズ超過 ({format_size(actual_size)})、"
                              "品質を調整...")
                    best_quality = max(QUALITY_MIN, best_quality - 3)
                    actual_size = images_to_pdf(
                        image_paths, best_quality, output_path,
                        grayscale, verbose, on_progress=on_progress,
                    )

                return actual_size, dpi, best_quality

    # 全DPIで未達
    _status(f"警告: 全DPIで目標未達。最低設定で出力します。")
    with tempfile.TemporaryDirectory() as tmpdir:
        image_paths = pdf_to_images(pdf_path, dpi_levels[-1], tmpdir,
                                    pages, verbose)
        actual_size = images_to_pdf(
            image_paths, QUALITY_MIN, output_path,
            grayscale, verbose, on_progress=on_progress,
        )
        return actual_size, dpi_levels[-1], QUALITY_MIN


# ---------------------------------------------------------------------------
# 自動モード (Ghostscript → ラスタライズ フォールバック)
# ---------------------------------------------------------------------------

def auto_compress(
    pdf_path: str,
    output_path: str,
    target_size: int,
    min_dpi: int = 72,
    grayscale: bool = False,
    pages: list[int] | None = None,
    verbose: bool = False,
    on_status: callable = None,
    on_search_step: callable = None,
    on_progress: callable = None,
) -> dict:
    """
    自動モード: Ghostscriptでまず試行し、未達ならラスタライズにフォールバック。
    Returns: dict with keys: actual_size, mode, and mode-specific fields
    """
    def _status(msg):
        print(msg)
        if on_status:
            on_status(msg)

    # Phase 1: Ghostscript
    if has_ghostscript():
        _status("自動モード: Ghostscript（テキスト保持）で試行中...")
        try:
            actual_size, used_preset, used_dpi = ghostscript_iterative_compress(
                pdf_path, output_path, target_size,
                grayscale=grayscale, verbose=verbose,
                on_status=on_status, on_search_step=on_search_step,
                on_progress=on_progress,
            )
            if actual_size <= target_size:
                _status(f"Ghostscript で目標達成: {format_size(actual_size)}")
                return {
                    "actual_size": actual_size,
                    "mode": MODE_GHOSTSCRIPT,
                    "used_preset": used_preset,
                    "used_dpi": used_dpi,
                }
            _status("Ghostscript では目標未達。ラスタライズにフォールバック...")
        except RuntimeError as e:
            _status(f"Ghostscript エラー: {e}。ラスタライズにフォールバック...")

    # Phase 2: ラスタライズ
    if has_pdftoppm():
        _status("自動モード: ラスタライズ（サイズ保証）で圧縮中...")
        actual_size, used_dpi, used_quality = iterative_compress(
            pdf_path, output_path, target_size,
            min_dpi=min_dpi, grayscale=grayscale, pages=pages,
            verbose=verbose,
            on_status=on_status, on_search_step=on_search_step,
            on_progress=on_progress,
        )
        return {
            "actual_size": actual_size,
            "mode": MODE_RASTERIZE,
            "used_dpi": used_dpi,
            "used_quality": used_quality,
        }

    raise RuntimeError("pdftoppm も Ghostscript も利用できません。")


# ---------------------------------------------------------------------------
# 単一ファイル圧縮（CLI用）
# ---------------------------------------------------------------------------

def compress_single_file(
    input_path: str,
    output_path: str,
    target_size: int,
    mode: str,
    min_dpi: int = 72,
    grayscale: bool = False,
    pages_str: str | None = None,
    verbose: bool = False,
) -> bool:
    """単一ファイルの圧縮を実行。成功(目標達成)ならTrue。"""
    input_size = os.path.getsize(input_path)
    page_count = get_page_count(input_path)

    # ページ範囲
    pages = None
    if pages_str:
        pages = parse_page_ranges(pages_str, page_count)
        page_label = f"{len(pages)}/{page_count}ページ (指定: {pages_str})"
    else:
        page_label = f"{page_count}ページ"

    mode_labels = {
        MODE_RASTERIZE: "ラスタライズ (サイズ保証)",
        MODE_GHOSTSCRIPT: "Ghostscript (テキスト保持)",
        MODE_AUTO: "自動 (GS→ラスタライズ)",
    }
    mode_label = mode_labels.get(mode, mode)
    options = []
    if grayscale:
        options.append("グレースケール")
    option_str = f" [{', '.join(options)}]" if options else ""

    # ヘッダー
    print()
    print("PDF Crusher - 超圧縮ツール")
    print("━" * 40)
    print(f"入力: {Path(input_path).name} "
          f"({format_size(input_size)}, {page_label})")
    print(f"目標: {format_size(target_size)} 以下")
    print(f"モード: {mode_label}{option_str}")

    # 既に目標以下（ページ指定なし時のみ）
    if input_size <= target_size and not pages_str:
        print("\n既に目標サイズ以下です。コピーして終了します。")
        shutil.copy2(input_path, output_path)
        print(f"出力: {output_path}")
        print("━" * 40)
        return True

    # ページ抽出（GS/autoモードでページ指定あり）
    work_pdf = input_path
    tmp_extracted = None
    if pages and mode in (MODE_GHOSTSCRIPT, MODE_AUTO):
        if has_ghostscript():
            with tempfile.NamedTemporaryFile(suffix=".pdf",
                                             delete=False) as tmp:
                tmp_extracted = tmp.name
            extract_pages(input_path, pages, tmp_extracted)
            work_pdf = tmp_extracted
            pages = None  # 抽出済み

    try:
        if mode == MODE_AUTO:
            result = auto_compress(
                work_pdf, output_path, target_size,
                min_dpi=min_dpi, grayscale=grayscale,
                pages=pages, verbose=verbose,
            )
            actual_size = result["actual_size"]
            if result["mode"] == MODE_GHOSTSCRIPT:
                setting_str = (f"プリセット={result['used_preset']}, "
                               f"DPI={result['used_dpi']}")
                mode_label = "自動 → Ghostscript (テキスト保持)"
            else:
                setting_str = (f"DPI={result['used_dpi']}, "
                               f"JPEG品質={result['used_quality']}")
                mode_label = "自動 → ラスタライズ (サイズ保証)"
        elif mode == MODE_GHOSTSCRIPT:
            actual_size, used_preset, used_dpi = ghostscript_iterative_compress(
                work_pdf, output_path, target_size,
                grayscale=grayscale, verbose=verbose,
            )
            setting_str = f"プリセット={used_preset}, DPI={used_dpi}"
        else:
            actual_size, used_dpi, used_quality = iterative_compress(
                work_pdf, output_path, target_size,
                min_dpi=min_dpi, grayscale=grayscale,
                pages=pages, verbose=verbose,
            )
            setting_str = f"DPI={used_dpi}, JPEG品質={used_quality}"
    finally:
        if tmp_extracted and os.path.exists(tmp_extracted):
            os.unlink(tmp_extracted)

    # 結果
    reduction = (1 - actual_size / input_size) * 100
    achieved = actual_size <= target_size
    mark = "✓" if achieved else "✗ (目標未達)"

    print()
    print("━" * 40)
    print(f"結果: {Path(output_path).name}")
    print(f"{format_size(input_size)} → {format_size(actual_size)} "
          f"({reduction:.1f}% 削減) {mark}")
    print(f"設定: {setting_str}")
    print(f"モード: {mode_label}{option_str}")
    print("━" * 40)

    return achieved


# ---------------------------------------------------------------------------
# CLI エントリーポイント
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="PDF Crusher - 超圧縮ツール: PDFを確実に目標サイズ以下に圧縮",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用例:
  python pdf_crusher.py input.pdf                          # 5MB以下に圧縮
  python pdf_crusher.py input.pdf -s 3MB                   # 3MB以下に圧縮
  python pdf_crusher.py input.pdf -o out.pdf -s 1MB        # 出力先指定
  python pdf_crusher.py input.pdf --mode ghostscript       # テキスト保持
  python pdf_crusher.py input.pdf --mode auto              # 自動（GS→ラスタライズ）
  python pdf_crusher.py input.pdf --grayscale              # グレースケール変換
  python pdf_crusher.py input.pdf --pages 1-5,10           # ページ指定
  python pdf_crusher.py *.pdf -s 3MB                       # 複数ファイル一括
        """,
    )
    parser.add_argument("input", nargs="+",
                        help="入力PDFファイル（複数指定・glob対応）")
    parser.add_argument("-o", "--output",
                        help="出力ファイルパス（単一ファイル時のみ）")
    parser.add_argument("-s", "--size", default="5MB",
                        help="目標サイズ (デフォルト: 5MB)")
    parser.add_argument("--min-dpi", type=int, default=72,
                        help="最低DPI (デフォルト: 72)")
    parser.add_argument(
        "-m", "--mode",
        choices=[MODE_RASTERIZE, MODE_GHOSTSCRIPT, MODE_AUTO],
        default=MODE_RASTERIZE,
        help="圧縮モード (デフォルト: rasterize)",
    )
    parser.add_argument("--grayscale", action="store_true",
                        help="グレースケールに変換（さらにサイズ削減）")
    parser.add_argument("--pages",
                        help="ページ範囲 (例: 1-5,10,15-20)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="詳細出力")

    args = parser.parse_args()

    # 入力ファイル展開
    input_files = []
    for pattern in args.input:
        expanded = globmod.glob(pattern)
        if expanded:
            input_files.extend(sorted(expanded))
        elif os.path.isfile(pattern):
            input_files.append(pattern)
        else:
            print(f"警告: ファイルが見つかりません: {pattern}")

    if not input_files:
        print("エラー: 処理対象のPDFファイルがありません。")
        sys.exit(1)

    if args.output and len(input_files) > 1:
        print("エラー: 複数ファイル処理時は -o は使用できません。")
        sys.exit(1)

    target_size = parse_size(args.size)

    # ツール確認
    mode = args.mode
    if mode == MODE_GHOSTSCRIPT:
        check_ghostscript()
    elif mode == MODE_AUTO:
        if not has_ghostscript() and not has_pdftoppm():
            print("エラー: pdftoppm も Ghostscript も見つかりません。")
            sys.exit(1)
    else:
        check_pdftoppm()

    # 複数ファイル一括処理
    if len(input_files) > 1:
        print(f"\n一括処理: {len(input_files)} ファイル")
        print("=" * 50)
        success = 0
        fail = 0
        for i, fpath in enumerate(input_files, 1):
            fpath = os.path.abspath(fpath)
            if not fpath.lower().endswith(".pdf"):
                print(f"\n[{i}/{len(input_files)}] スキップ: {fpath}")
                continue
            stem = Path(fpath).stem
            parent = Path(fpath).parent
            out = str(parent / f"{stem}_compressed.pdf")
            print(f"\n[{i}/{len(input_files)}] {Path(fpath).name}")
            try:
                ok = compress_single_file(
                    fpath, out, target_size, mode,
                    min_dpi=args.min_dpi, grayscale=args.grayscale,
                    pages_str=args.pages, verbose=args.verbose,
                )
                if ok:
                    success += 1
                else:
                    fail += 1
            except Exception as e:
                print(f"  エラー: {e}")
                fail += 1

        print()
        print("=" * 50)
        print(f"一括処理完了: {success} 成功, {fail} 失敗 "
              f"/ {len(input_files)} ファイル")
        print("=" * 50)
        sys.exit(0 if fail == 0 else 1)

    # 単一ファイル処理
    input_path = os.path.abspath(input_files[0])
    if not os.path.isfile(input_path):
        print(f"エラー: ファイルが見つかりません: {input_path}")
        sys.exit(1)

    if args.output:
        output_path = os.path.abspath(args.output)
    else:
        stem = Path(input_path).stem
        parent = Path(input_path).parent
        output_path = str(parent / f"{stem}_compressed.pdf")

    ok = compress_single_file(
        input_path, output_path, target_size, mode,
        min_dpi=args.min_dpi, grayscale=args.grayscale,
        pages_str=args.pages, verbose=args.verbose,
    )
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
