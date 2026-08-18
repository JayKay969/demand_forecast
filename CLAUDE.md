# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

店頭欠品(品切れ)を考慮した売上補正ロジックのPhase 1 PoC。仕様は
[`design/欠品補正改.md`](design/欠品補正改.md)(全38章)に定義されている。ロングテール商品の
SKU×店舗×営業日を処理単位とし、欠品によって実売上が需要を下回っている日を検出して、需要予測モデル
への入力用に補正売上数量を生成する。現時点ではロジック未実装で、開発環境の骨組みとダミーデータ生成
のみが揃っている段階。

## Commands

```bash
uv sync                        # 依存関係インストール
uv run pytest                  # テスト実行 (現状テストは未実装)
uv run pytest tests/test_x.py::test_name   # 単一テスト実行
uv run ruff check .            # Lint
uv run ruff format .           # フォーマット

# ダミーデータ生成 (DuckDBファイルへ raw_daily_sales テーブルを作成)
uv run python scripts/generate_dummy_data.py \
    --stores 5 --skus 20 --days 180 --out data/sample/demand.duckdb
```

ランタイムは Python 3.13 (`.python-version` / `pyproject.toml` の `requires-python`)。パッケージ管理は
uv、DB は DuckDB。pandas は意図的に未導入(DuckDB中心の設計)。

## Architecture

`src/demand_forecast/` は仕様書の処理フロー(19章)にそのまま対応するサブパッケージ構成。実装を追加する
際は対応する章を参照すること。

| サブパッケージ | 仕様書の章 | 役割 |
| --- | --- | --- |
| `ingestion` | 4, 5, 6章 | 入力データ検証、在庫収支整合性チェック、「通常日」抽出 |
| `classification` | 7, 8章 | 欠品状態分類(`oos_status`)、需要型分類(ADI/CV²) |
| `baseline` | 10, 11章 | 低量安定型の通常日ベースライン、間欠型の発生確率×発生時数量 |
| `adjustment` | 16〜18章 | 欠品状態×需要型別の補正計算、補正上限、信頼度判定、出力生成 |
| `config` | 25章 | パラメータ管理(閾値をコードへ直書きしない) |
| `sql` | — | DuckDB向けSQLクエリ置き場 |

全サブパッケージは現状 `__init__.py` のみで未実装。実装時は上記の対応関係を崩さないこと。

### 仕様上の中核ロジック(実装時に必ず踏まえる制約)

- **実売上を下回らない**: `AdjustedSales_t >= ActualSales_t` は全出力で不変条件(仕様2.1, 31.3)。
- **需要型分類には欠品日を混ぜない**: 通常日(仕様5章の全条件を満たす日)だけでADI/CV²や発生確率/発生
  時数量を計算する。欠品日のゼロ売上を分母に入れると低量安定型が間欠型に誤分類される(仕様20.1)。
- **間欠型のゼロ売上は"通常日かつ終日販売可能"なら含める**: 発生確率の分母には含め、発生時数量
  (`m_t`)の計算には正需要日のみを使う。確率と数量を分離して推定すること(仕様11章, 20.2, 20.3)。
- **補正済み値を参照系列へ戻さない**: ベースライン・分類の計算には常に「在庫制約を受けていない実売上
  のみ」を使う。補正値の自己参照は誤差の自己増殖を招く(仕様2.2, 20.4)。
- **補正不能時に0を返さない**: 参照件数不足やフォールバック失敗時は `adjusted_sales_qty =
  actual_sales_qty`、`confidence_level = 'NONE'`、理由コード(26章)を設定する。`COALESCE(..., 0)` で
  代替しない(仕様20.6, 26章)。
- **日付リーケージ禁止**: 対象日 `t` の計算は `reference_date < target_date` のデータのみ参照する
  (仕様20.7)。
- **欠品状態(`oos_status`)**: `NO_OOS` / `FULL_DAY_OOS` / `PARTIAL_DAY_OOS` / `POSSIBLE_OOS` /
  `INVENTORY_DATA_ERROR` / `NOT_APPLICABLE` の6区分(仕様7章)。補正式は需要型×欠品状態の組み合わせで
  決定表(仕様16章)・疑似コード(仕様21章)として定義されている。
- **信頼度**: High/Medium/Low/None の4段階(仕様17章)。低信頼補正は後続の需要予測で除外・低ウェイト
  化できる設計とする。

### ダミーデータ生成 (`scripts/generate_dummy_data.py`)

仕様書4.1の必須データスキーマに沿って DuckDB の `raw_daily_sales` テーブルへ SKU×店舗×営業日データを
生成する。SKU×店舗ごとに `LOW_VOLUME_SMOOTH`(低量安定型)か `INTERMITTENT_STABLE_QTY`(間欠・数量
安定型)の需要パターンを割り当て、在庫収支をシミュレーションで積み上げつつ、`--inventory-error-rate`
/ `--stockout-rate` / `--closed-day-rate` で在庫収支異常・終日欠品・休業日を意図的に一定割合混入させ、
分類・検証ロジックのテスト材料とする。実装ロジックのテストで新しいダミーデータパターンが必要になった
場合は、このスクリプトの `SkuStoreProfile` / `generate_rows` を拡張する。
