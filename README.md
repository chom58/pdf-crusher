# PDF Crusher - 超圧縮ツール

PDFを**確実に目標サイズ以下**に圧縮するツール。CLI & Webアプリ対応。

ラスタライズ方式（PDF→画像→PDF）により、DPI×JPEG品質の2軸で精密なサイズ制御を実現。
20MBのPDFでも確実に5MB以下に圧縮できる。

## インストール

### 前提条件

- Python 3.10+
- poppler（`pdftoppm`コマンド）

```bash
# macOS
brew install poppler

# Ubuntu/Debian
sudo apt install poppler-utils
```

### セットアップ

```bash
cd 超圧縮
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Web アプリ（ブラウザで使う）

```bash
streamlit run app.py
```

ブラウザが自動で開き、PDFをドラッグ&ドロップで圧縮できます。

### Streamlit Cloud にデプロイ（無料公開）

1. このリポジトリを GitHub に push
2. [Streamlit Cloud](https://share.streamlit.io/) にログイン
3. 「New app」→ リポジトリ・ブランチ・`app.py` を指定
4. デプロイ完了（`packages.txt` で poppler-utils が自動インストールされる）

## CLI（コマンドラインで使う）

```bash
# デフォルト: 5MB以下に圧縮
python pdf_crusher.py input.pdf

# 目標サイズ指定
python pdf_crusher.py input.pdf -s 3MB

# 出力先指定
python pdf_crusher.py input.pdf -o output.pdf -s 1MB

# 最低DPIを制限（品質を確保したい場合）
python pdf_crusher.py input.pdf --min-dpi 100

# 詳細出力
python pdf_crusher.py input.pdf -s 5MB -v
```

## オプション

| オプション | デフォルト | 説明 |
|-----------|-----------|------|
| `-o, --output` | `<入力名>_compressed.pdf` | 出力ファイルパス |
| `-s, --size` | `5MB` | 目標ファイルサイズ（例: `3MB`, `500KB`） |
| `--min-dpi` | `72` | 最低DPI（下げすぎ防止） |
| `-v, --verbose` | off | 詳細出力 |

## 仕組み

1. PDFを`pdftoppm`でページごとにPNG画像化
2. DPIを200→72まで段階的に試行
3. 各DPIでJPEG品質をバイナリサーチ（目標以下の最高品質を探索）
4. `img2pdf`でJPEG→PDF再構成

**サイズ保証の原理:** ラスタライズにより予測困難な要素（フォント・ベクター等）を排除。
出力サイズ ≒ JPEG合計 + 数KB で完全に制御可能。

## 出力例

```
PDF Crusher - 超圧縮ツール
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
入力: report.pdf (20.3 MB, 45ページ)
目標: 5.0 MB 以下

DPI=200, 品質探索中...
  品質=45: 6.2 MB (推定) ✗
  品質=22: 3.8 MB (推定) ✓
  品質=33: 4.8 MB (推定) ✓
  品質=39: 5.1 MB (推定) ✗
  品質=36: 4.9 MB (推定) ✓
  → 最適設定: DPI=200, 品質=36

圧縮中... ████████████████████ 45/45ページ

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
結果: report_compressed.pdf
20.3 MB → 4.9 MB (75.9% 削減) ✓
設定: DPI=200, JPEG品質=36
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

## 注意事項

- テキスト選択・コピーはできなくなる（画像化のため）
- パスワード付きPDFは非対応
- 最低設定（72dpi, quality=5）でも目標未達の場合は警告付きで出力
