#!/usr/bin/env python3
"""
PDF Crusher - 超圧縮ツール
PDFを確実に目標サイズ以下に圧縮する。
ラスタライズ方式により、DPI×JPEG品質の2軸で精密なサイズ制御を実現。
"""

import argparse
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


# DPIレベル: 高い方から試行し、目標未達なら下げていく
DPI_LEVELS = [200, 150, 120, 100, 85, 72]

# JPEG品質の探索範囲
QUALITY_MIN = 5
QUALITY_MAX = 85

# サイズ推定時の安全マージン（10%上乗せ）
SAFETY_MARGIN = 1.10

# img2pdfのオーバーヘッド概算（ページあたり）
PDF_OVERHEAD_PER_PAGE = 500  # bytes


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


def check_pdftoppm():
    """pdftoppmの存在確認"""
    if shutil.which("pdftoppm") is None:
        print("エラー: pdftoppm が見つかりません。")
        print("  macOS: brew install poppler")
        print("  Ubuntu: sudo apt install poppler-utils")
        sys.exit(1)


def get_page_count(pdf_path: str) -> int:
    """PDFのページ数を取得"""
    try:
        result = subprocess.run(
            ["pdfinfo", pdf_path],
            capture_output=True, text=True, timeout=30
        )
        for line in result.stdout.splitlines():
            if line.startswith("Pages:"):
                return int(line.split(":")[1].strip())
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # pdfinfo が使えない場合、pdftoppmで1ページだけ変換して確認
    # フォールバック: 全ページ変換して数える（非効率だが確実）
    with tempfile.TemporaryDirectory() as tmpdir:
        subprocess.run(
            ["pdftoppm", "-png", "-r", "10", pdf_path, os.path.join(tmpdir, "page")],
            capture_output=True, timeout=120
        )
        return len(list(Path(tmpdir).glob("page-*.png")))


def pdf_to_images(pdf_path: str, dpi: int, output_dir: str, verbose: bool = False) -> list[str]:
    """pdftoppmでPDFをPNG画像に変換"""
    prefix = os.path.join(output_dir, "page")
    cmd = ["pdftoppm", "-png", "-r", str(dpi), pdf_path, prefix]
    if verbose:
        print(f"  pdftoppm 実行中 (DPI={dpi})...")

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "Incorrect password" in stderr or "password" in stderr.lower():
            print("エラー: パスワード付きPDFは処理できません。")
            sys.exit(1)
        raise RuntimeError(f"pdftoppm エラー: {stderr}")

    # 出力ファイルをソートして返却
    images = sorted(Path(output_dir).glob("page-*.png"), key=lambda p: p.name)
    return [str(p) for p in images]


def compress_image(png_path: str, quality: int) -> bytes:
    """PNG画像をJPEG圧縮してbytesで返却"""
    with Image.open(png_path) as img:
        # RGBAの場合はRGBに変換
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        from io import BytesIO
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()


def estimate_total_size(image_paths: list[str], quality: int, max_samples: int = 5) -> int:
    """サンプルページからJPEG圧縮後の合計サイズを推定"""
    total_pages = len(image_paths)
    if total_pages == 0:
        return 0

    # サンプル数を決定（全ページ数が少ない場合は全部）
    sample_count = min(max_samples, total_pages)

    # 均等にサンプルを選択
    if sample_count >= total_pages:
        sample_indices = list(range(total_pages))
    else:
        step = total_pages / sample_count
        sample_indices = [int(i * step) for i in range(sample_count)]

    # サンプルページのJPEGサイズを計測
    sample_sizes = []
    for idx in sample_indices:
        jpeg_data = compress_image(image_paths[idx], quality)
        sample_sizes.append(len(jpeg_data))

    # 平均サイズ × 全ページ数 で推定
    avg_size = sum(sample_sizes) / len(sample_sizes)
    estimated = int(avg_size * total_pages)

    # PDFオーバーヘッドを加算
    estimated += PDF_OVERHEAD_PER_PAGE * total_pages

    # 安全マージンを適用
    return int(estimated * SAFETY_MARGIN)


def images_to_pdf(
    image_paths: list[str],
    quality: int,
    output_path: str,
    verbose: bool = False,
    on_progress: callable = None,
) -> int:
    """画像をJPEG圧縮してPDFに変換。出力ファイルサイズを返す。"""
    jpeg_data_list = []
    total = len(image_paths)

    iterator = tqdm(image_paths, desc="圧縮中", unit="ページ") if not verbose else image_paths
    for i, png_path in enumerate(iterator):
        if verbose:
            print(f"  ページ {i+1}/{total} を圧縮中...")
        jpeg_data = compress_image(png_path, quality)
        jpeg_data_list.append(jpeg_data)
        if on_progress:
            on_progress(i + 1, total)

    # img2pdfでPDFに変換
    pdf_bytes = img2pdf.convert(jpeg_data_list)
    with open(output_path, "wb") as f:
        f.write(pdf_bytes)

    return os.path.getsize(output_path)


def find_best_quality(
    image_paths: list[str],
    target_size: int,
    dpi: int,
    verbose: bool = False,
    on_search_step: callable = None,
) -> int | None:
    """
    バイナリサーチで目標サイズ以下となる最高品質を探索。
    見つからなければNoneを返す。
    on_search_step(quality, estimated_size, is_ok) — UI向けコールバック
    """
    lo, hi = QUALITY_MIN, QUALITY_MAX
    best_quality = None

    while lo <= hi:
        mid = (lo + hi) // 2
        estimated = estimate_total_size(image_paths, mid)
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
    verbose: bool = False,
    on_status: callable = None,
    on_search_step: callable = None,
    on_progress: callable = None,
) -> tuple[int, int, int]:
    """
    反復圧縮: DPIを段階的に下げながら、各DPIで最適品質をバイナリサーチ。
    Returns: (output_size, used_dpi, used_quality)

    コールバック:
      on_status(message) — ステータスメッセージ
      on_search_step(quality, estimated_size, is_ok) — 品質探索の各ステップ
      on_progress(current, total) — ページ圧縮の進捗
    """
    # min_dpi以上のDPIレベルのみ使用
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
            # PDF → 画像変換
            _status(f"DPI={dpi} で画像化中...")
            image_paths = pdf_to_images(pdf_path, dpi, tmpdir, verbose)
            if not image_paths:
                _status("警告: 画像が生成されませんでした。次のDPIを試行...")
                continue

            # バイナリサーチで最適品質を探索
            _status(f"DPI={dpi}, 最適品質を探索中...")
            best_quality = find_best_quality(
                image_paths, target_size, dpi, verbose,
                on_search_step=on_search_step,
            )

            if best_quality is not None:
                _status(f"最適設定: DPI={dpi}, 品質={best_quality}")

                # 最終出力を生成
                _status("最終PDFを生成中...")
                actual_size = images_to_pdf(
                    image_paths, best_quality, output_path, verbose,
                    on_progress=on_progress,
                )

                # 実際のサイズが目標を超えた場合、品質を1段階下げてリトライ
                if actual_size > target_size and best_quality > QUALITY_MIN:
                    if verbose:
                        print(f"  実サイズ超過 ({format_size(actual_size)})、品質を調整...")
                    best_quality = max(QUALITY_MIN, best_quality - 3)
                    actual_size = images_to_pdf(
                        image_paths, best_quality, output_path, verbose,
                        on_progress=on_progress,
                    )

                return actual_size, dpi, best_quality

    # 全DPIレベルで未達: 最低設定で強制出力
    _status(f"警告: 全DPIで目標未達。最低設定 (DPI={dpi_levels[-1]}, 品質={QUALITY_MIN}) で出力します。")
    with tempfile.TemporaryDirectory() as tmpdir:
        image_paths = pdf_to_images(pdf_path, dpi_levels[-1], tmpdir, verbose)
        actual_size = images_to_pdf(
            image_paths, QUALITY_MIN, output_path, verbose,
            on_progress=on_progress,
        )
        return actual_size, dpi_levels[-1], QUALITY_MIN


def main():
    parser = argparse.ArgumentParser(
        description="PDF Crusher - 超圧縮ツール: PDFを確実に目標サイズ以下に圧縮",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用例:
  python pdf_crusher.py input.pdf                    # デフォルト: 5MB以下に圧縮
  python pdf_crusher.py input.pdf -s 3MB             # 3MB以下に圧縮
  python pdf_crusher.py input.pdf -o out.pdf -s 1MB  # 出力先と目標サイズ指定
  python pdf_crusher.py input.pdf --min-dpi 100      # 最低DPIを100に制限
        """,
    )
    parser.add_argument("input", help="入力PDFファイルのパス")
    parser.add_argument("-o", "--output", help="出力PDFファイルのパス (デフォルト: <入力名>_compressed.pdf)")
    parser.add_argument("-s", "--size", default="5MB", help="目標ファイルサイズ (デフォルト: 5MB)")
    parser.add_argument("--min-dpi", type=int, default=72, help="最低DPI (デフォルト: 72)")
    parser.add_argument("-v", "--verbose", action="store_true", help="詳細出力")

    args = parser.parse_args()

    # 入力ファイル確認
    input_path = os.path.abspath(args.input)
    if not os.path.isfile(input_path):
        print(f"エラー: ファイルが見つかりません: {input_path}")
        sys.exit(1)

    # 出力パス決定
    if args.output:
        output_path = os.path.abspath(args.output)
    else:
        stem = Path(input_path).stem
        parent = Path(input_path).parent
        output_path = str(parent / f"{stem}_compressed.pdf")

    # 目標サイズ解析
    target_size = parse_size(args.size)

    # pdftoppm確認
    check_pdftoppm()

    # 入力ファイル情報
    input_size = os.path.getsize(input_path)
    page_count = get_page_count(input_path)

    # ヘッダー表示
    print()
    print("PDF Crusher - 超圧縮ツール")
    print("━" * 40)
    print(f"入力: {Path(input_path).name} ({format_size(input_size)}, {page_count}ページ)")
    print(f"目標: {format_size(target_size)} 以下")

    # 既に目標以下の場合
    if input_size <= target_size:
        print(f"\n既に目標サイズ以下です。コピーして終了します。")
        shutil.copy2(input_path, output_path)
        print(f"出力: {output_path}")
        print("━" * 40)
        sys.exit(0)

    # 圧縮実行
    actual_size, used_dpi, used_quality = iterative_compress(
        input_path, output_path, target_size, args.min_dpi, args.verbose
    )

    # 結果表示
    reduction = (1 - actual_size / input_size) * 100
    achieved = "✓" if actual_size <= target_size else "✗ (目標未達)"

    print()
    print("━" * 40)
    print(f"結果: {Path(output_path).name}")
    print(f"{format_size(input_size)} → {format_size(actual_size)} ({reduction:.1f}% 削減) {achieved}")
    print(f"設定: DPI={used_dpi}, JPEG品質={used_quality}")
    print("━" * 40)

    if actual_size > target_size:
        sys.exit(1)


if __name__ == "__main__":
    main()
