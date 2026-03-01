# PDF Crusher - 超圧縮ツール

PDFを**確実に目標サイズ以下**に圧縮するツール。CLI & Webアプリ対応。

3つの圧縮モード、グレースケール変換、ページ範囲指定、複数ファイル一括処理に対応。

## 圧縮モード比較

| モード | テキスト選択 | サイズ保証 | 用途 |
|--------|:----------:|:--------:|------|
| **ラスタライズ** (`rasterize`) | ✗ | ✓ 確実 | サイズ厳守が必要な提出物 |
| **Ghostscript** (`ghostscript`) | ✓ | △ ベストエフォート | テキストコピーが必要な文書 |
| **自動** (`auto`) | ✓→✗ | ✓ 確実 | 迷ったらこれ（GS優先→ラスタライズ） |

## インストール

### 前提条件

- Python 3.10+
- poppler（`pdftoppm`）… ラスタライズモード・自動モードに必要
- Ghostscript（`gs`）… Ghostscriptモード・自動モードに必要

```bash
# macOS
brew install poppler ghostscript

# Ubuntu/Debian
sudo apt install poppler-utils ghostscript
```

### セットアップ

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Web アプリ（ブラウザで使う）

```bash
streamlit run app.py
```

ブラウザが自動で開き、PDFをドラッグ&ドロップで圧縮できます。

**Web UI の機能:**
- 3モード切替（自動 / ラスタライズ / Ghostscript）
- 目標サイズスライダー（0.5MB〜20MB）
- グレースケール変換トグル
- ページ範囲指定
- 複数ファイルアップロード → ZIPダウンロード
- 圧縮前後の1ページ目プレビュー比較

### Streamlit Cloud にデプロイ（無料公開）

1. このリポジトリを GitHub に push
2. [Streamlit Cloud](https://share.streamlit.io/) にログイン
3. 「New app」→ リポジトリ・ブランチ・`app.py` を指定
4. デプロイ完了（`packages.txt` で poppler-utils と ghostscript が自動インストールされる）

## CLI（コマンドラインで使う）

```bash
# デフォルト: 5MB以下に圧縮
python pdf_crusher.py input.pdf

# 目標サイズ指定
python pdf_crusher.py input.pdf -s 3MB

# 出力先指定
python pdf_crusher.py input.pdf -o output.pdf -s 1MB

# テキスト保持モード（Ghostscript）
python pdf_crusher.py input.pdf --mode ghostscript

# 自動モード（GS優先→ラスタライズフォールバック）
python pdf_crusher.py input.pdf --mode auto

# グレースケール変換（さらにサイズ削減）
python pdf_crusher.py input.pdf --grayscale

# ページ範囲指定（抽出+圧縮）
python pdf_crusher.py input.pdf --pages 1-5,10

# 複数ファイル一括処理
python pdf_crusher.py *.pdf -s 3MB

# オプション組み合わせ
python pdf_crusher.py input.pdf --mode auto --grayscale --pages 1-10 -s 2MB
```

## オプション一覧

| オプション | デフォルト | 説明 |
|-----------|-----------|------|
| `-o, --output` | `<入力名>_compressed.pdf` | 出力ファイルパス（単一ファイル時のみ） |
| `-s, --size` | `5MB` | 目標ファイルサイズ（例: `3MB`, `500KB`） |
| `-m, --mode` | `rasterize` | 圧縮モード: `rasterize` / `ghostscript` / `auto` |
| `--min-dpi` | `72` | 最低DPI（下げすぎ防止） |
| `--grayscale` | off | グレースケールに変換 |
| `--pages` | 全ページ | ページ範囲（例: `1-5,10,15-20`） |
| `-v, --verbose` | off | 詳細出力 |

## 仕組み

### ラスタライズモード
1. PDFを`pdftoppm`でページごとにPNG画像化
2. DPIを200→72まで段階的に試行
3. 各DPIでJPEG品質をバイナリサーチ（目標以下の最高品質を探索）
4. `img2pdf`でJPEG→PDF再構成

**サイズ保証の原理:** ラスタライズにより予測困難な要素（フォント・ベクター等）を排除。
出力サイズ ≒ JPEG合計 + 数KB で完全に制御可能。

### Ghostscriptモード
1. Ghostscriptでプリセットを段階的に試行（prepress→printer→ebook→screen）
2. プリセットで未達の場合、画像のDPIをさらに下げて再試行
3. テキスト・ベクター要素はそのまま保持

### 自動モード
1. まずGhostscriptで圧縮を試行（テキスト保持）
2. 目標未達ならラスタライズにフォールバック（サイズ保証）
3. テキスト保持とサイズ保証の両立を目指す

## 出力例

```
PDF Crusher - 超圧縮ツール
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
入力: report.pdf (20.3 MB, 45ページ)
目標: 5.0 MB 以下
モード: 自動 (GS→ラスタライズ)

自動モード: Ghostscript（テキスト保持）で試行中...
  プリセット=ebook: 6.2 MB ✗
  プリセット=screen: 4.8 MB ✓
Ghostscript で目標達成: 4.8 MB

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
結果: report_compressed.pdf
20.3 MB → 4.8 MB (76.4% 削減) ✓
設定: プリセット=screen, DPI=72
モード: 自動 → Ghostscript (テキスト保持)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

### 一括処理

```
一括処理: 3 ファイル
==================================================

[1/3] report.pdf
...
[2/3] slides.pdf
...
[3/3] manual.pdf
...

==================================================
一括処理完了: 3 成功, 0 失敗 / 3 ファイル
==================================================
```

## 注意事項

- ラスタライズモードではテキスト選択・コピーができなくなる
- パスワード付きPDFは非対応
- 最低設定でも目標未達の場合は警告付きで出力
- 自動モードは poppler と Ghostscript の両方が必要
