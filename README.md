# demand_forcast

店頭欠品を考慮した売上補正ロジック(PoC)。仕様は [`design/欠品補正改.md`](design/欠品補正改.md) を参照。

現時点では、ロジック実装のための開発環境の骨組みのみを整備している段階。

## セットアップ

```bash
uv sync
```

## ディレクトリ構成

```
src/demand_forecast/
├── config/          # パラメータ管理 (仕様書25章) — 未実装
├── ingestion/        # 入力データ検証・通常日抽出 (仕様書4, 5, 6章) — 未実装
├── classification/    # 欠品状態分類・需要型分類 (仕様書7, 8章) — 未実装
├── baseline/           # 通常日ベースライン・間欠需要推定 (仕様書10, 11章) — 未実装
├── adjustment/          # 補正計算・信頼度判定・出力生成 (仕様書16〜18章) — 未実装
└── sql/                  # DuckDB向けSQLクエリ置き場

scripts/
└── generate_dummy_data.py  # ダミーデータ生成スクリプト

tests/                        # pytest用の土台 (テストは未実装)
```

## ダミーデータ生成

仕様書4.1の必須データスキーマに沿った SKU × 店舗 × 営業日 のダミーデータを
DuckDBファイルへ生成する。低量安定型・間欠型の需要パターン、終日欠品、
在庫収支異常のケースを一定割合で含む。

```bash
uv run python scripts/generate_dummy_data.py \
    --stores 5 --skus 20 --days 180 \
    --out data/sample/demand.duckdb
```

主なオプション:

| オプション | 説明 | 既定値 |
| --- | --- | --- |
| `--stores` | 店舗数 | 5 |
| `--skus` | SKU数 | 20 |
| `--days` | 生成日数 | 180 |
| `--out` | 出力先DuckDBファイル | `data/sample/demand.duckdb` |
| `--seed` | 乱数シード | 42 |
| `--inventory-error-rate` | 在庫収支を崩す行の割合 | 0.02 |
| `--stockout-rate` | 終日欠品を発生させる日の割合 | 0.03 |
| `--closed-day-rate` | 店舗休業日の割合 | 0.01 |

生成後は以下で中身を確認できる。

```bash
uv run python -c "
import duckdb
con = duckdb.connect('data/sample/demand.duckdb')
for row in con.execute('SELECT * FROM raw_daily_sales LIMIT 5').fetchall():
    print(row)
"
```

pandasを導入していれば `.fetchdf()` でDataFrameとしても取得できる(本プロジェクトではDuckDB
中心のため既定では未導入)。

## Lint / Format

```bash
uv run ruff check .
uv run ruff format .
```

## テスト

```bash
uv run pytest
```
