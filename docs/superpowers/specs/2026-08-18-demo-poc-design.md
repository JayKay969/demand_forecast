# 店頭欠品補正ロジック P0デモ 設計書

**作成日:** 2026-08-18
**対象仕様:** [`design/欠品補正改.md`](../../../design/欠品補正改.md)(以下「仕様書」)
**目的:** 仕様書34章のP0項目(PoC開始前に必須の12項目)を実装し、社内向けにダミーデータへ対して
正しく補正が行われることを動作確認するためのCLIデモを作る。

---

## 1. スコープ

### 1.1 対象(P0として今回実装する)

- 欠品状態の分類(仕様7章): `NO_OOS` / `FULL_DAY_OOS` / `POSSIBLE_OOS` / `INVENTORY_DATA_ERROR` /
  `NOT_APPLICABLE`
- 在庫収支チェック(6章)による `INVENTORY_DATA_ERROR` 判定
- 通常日/欠品日の明確な分離(5章)、需要型判定における欠品日の除外(20.1)
- 需要型分類(8章 ADI/CV²)。フォールバック階層は使わずSKU×店舗単位のみで判定する
- 低量安定型の通常日ベースライン(10章)。**単純平均/中央値**のみ(時間減衰重み・Winsorizeは含めない)
- 間欠・数量安定型(11章)の発生確率 `p_raw` と発生時数量 `m_raw` の分離計算(**階層縮約は含めず**、
  生の推定値のみを使う)
- 決定表(16/21章)による補正計算、実売上下限制約(`AdjustedSales_t >= ActualSales_t`)
- 補正不能時に0を返さない処理(20.6)、参照件数・使用階層・信頼度・理由コードの保存(9, 17, 26章)
- 日付リーケージ防止(20.7)、補正済み値の再利用禁止(20.4, 2.2)
- Masking Test(22章)

### 1.2 非対象(P1/P2としてスコープ外。理由コード等で明示し、暗黙のNO_ADJUSTMENTにしない)

| 項目 | 仕様書の章 | 除外理由 |
| --- | --- | --- |
| `PARTIAL_DAY_OOS`判定 | 7.2 | 時間帯別データ(4.2推奨データ)がダミーデータに存在せず判定不能 |
| 補正上限 `U_t` | 15章 | P1項目。今回のデモでは上限を計算・適用しない |
| 時間帯需要構成比 `q_t` | 12章 | P1項目。`PARTIAL_DAY_OOS`を扱わないため不要 |
| 階層フォールバック | 13章 | P1項目。SKU×店舗単体のみで完結させる |
| 時間減衰重み・外れ値抑制 | 10.1, 10.2 | P1項目。単純平均/中央値に簡略化 |
| 階層縮約(κ, λ) | 11.1, 11.2 | P1項目。`p_raw`, `m_raw`をそのまま使う |
| 分類ヒステリシス | 20.5 | P1項目。分類は毎回の再計算値をそのまま使う |
| `sales_outlier_flag` / `price_anomaly_flag` / `lifecycle_exception_flag` | 5章 | 元となる入力データ(新商品フラグ等)がダミーデータに存在しないため常に`0`固定 |

### 1.3 販促日+欠品の扱い

仕様14.2の推奨に従い、`promotion_flag=1` の日に`FULL_DAY_OOS`/`POSSIBLE_OOS`が重なった場合は
`NO_ADJUSTMENT_PROMOTION` として補正対象外にする(倍率推定はP1)。

---

## 2. アーキテクチャ・モジュール構成

既存の `src/demand_forecast/` サブパッケージへ以下を追加する。各関数は「SKU×店舗ごとの通常日リス
ト」のような素の `list`/`dataclass` を受け取り、DuckDBとのやり取りは境界(読み込み・書き込み関数)に
限定する。pandasは導入しない(既存方針の踏襲)。

```
src/demand_forecast/
├── config/
│   └── parameters.py         # 25.1のP0関連パラメータをdataclassで一元管理
├── ingestion/
│   ├── loader.py              # DuckDBからraw_daily_salesを読み込み、必須列を検証
│   └── inventory.py           # 6章: 理論在庫との差異計算 → inventory_data_error_flag
├── classification/
│   ├── oos_status.py          # 7章: 行単位でoos_statusを判定
│   ├── normal_days.py         # 5章: 通常日フラグの合成(全条件AND)
│   └── demand_type.py         # 8章: ADI/CV²計算 → demand_type
├── baseline/
│   ├── low_volume.py          # 10章: 通常日単純平均/中央値(P0範囲)
│   └── intermittent.py        # 11章: p_raw, m_rawの分離計算(P0範囲)
├── adjustment/
│   ├── decision_table.py      # 16/21章: (demand_type, oos_status) → adjusted_sales_qty等
│   └── writer.py              # fact_daily_sales_demand_adjustment相当のテーブルをDuckDBへ書き込み
└── evaluation/
    └── masking_test.py         # 22章: Masking Test実行・MAE/WAPE等の集計

scripts/
└── run_demo.py                  # 上記を順に呼び出すCLIエントリポイント
```

`evaluation/` は既存READMEの構成表にない新規サブパッケージだが、Masking Test(P0必須項目)を補正計算
本体から分離するために追加する。

パイプラインは `business_date, store_id, sku_id` をキーに、ステージを跨いで1つの `DailyRecord`
dataclass(または dict)に属性を積み増していく。処理は「SKU×店舗ごとに時系列でまとめて処理」→
「日付でソートして通常日集合を抽出し統計量を計算」という流れになる。

### 2.1 P0で使用する設定パラメータ(config/parameters.py)

仕様書25.1のうちP0範囲で実際に使うものだけをdataclassのフィールドとして持つ。

| パラメータ | 既定値 | 用途 |
| --- | --- | --- |
| `classification_lookback_days` | 180 | 需要型判定(ADI/CV²)に使う通常日の参照期間 |
| `baseline_lookback_days` | 56 | ベースライン(低量安定型平均・間欠型p/m)に使う通常日の参照期間 |
| `minimum_reference_days` | 28 | 需要型を`UNCLASSIFIED`にしない最低通常日数 |
| `minimum_normal_days` | 14 | 低量安定型で平均を採用する最低有効通常日数 |
| `minimum_median_days` | 8 | 低量安定型で中央値を採用する最低有効通常日数(これ未満は補正不能) |
| `minimum_positive_days` | 5 | 間欠型の`m_raw`計算に必要な最低正需要日数 |
| `adi_threshold` | 1.32 | 高頻度需要と間欠需要の境界 |
| `cv2_threshold` | 0.49 | 数量安定型と数量変動型の境界 |
| `inventory_gap_tolerance` | 0 | 在庫差異の許容範囲。整数量のためP0では差異ゼロを正常とし、非ゼロを`INVENTORY_DATA_ERROR`とする |

**重要:** 需要型判定(3.2)は `classification_lookback_days`(180日)の通常日集合を使い、ベースライン
計算(3.3)は `baseline_lookback_days`(56日)の通常日集合を使う。両者は対象日より過去の通常日のみを
含み、それぞれ独立に集計する(4.2のリーケージ防止と整合)。

---

## 3. データフロー・主要ロジック

### 3.1 Ingestion → 通常日判定までの流れ

```
raw_daily_sales (DuckDB)
  → loader.load_daily_sales(con) : list[DailyRecord]
  → inventory.check_inventory(records) : 各recordへ inventory_data_error_flag を付与
       ExpectedClosing = Opening + Receipt + TransferIn - TransferOut - Sales - Disposal + Return
       InventoryGap = Closing - ExpectedClosing
       |InventoryGap| > inventory_gap_tolerance → INVENTORY_DATA_ERROR
  → oos_status.classify(record) : 各recordへ oos_status を付与
       store_open_flag == 0                              → NOT_APPLICABLE
       inventory_data_error_flag == 1                    → INVENTORY_DATA_ERROR
       opening==0 and receipt==0 and transfer_in==0
         and actual_sales==0                              → FULL_DAY_OOS
       closing==0 and actual_sales>0                       → POSSIBLE_OOS (欠品時刻不明のまま売り切れ)
       それ以外                                            → NO_OOS
  → normal_days.is_normal_day(record) : bool
       store_open_flag==1 and inventory_data_error_flag==0
       and oos_status == NO_OOS and promotion_flag==0
```

`sales_outlier_flag`/`price_anomaly_flag`/`lifecycle_exception_flag` は1.2の理由により常に`0`固定
とし、コード上はフラグとして保持するのみ。

### 3.2 需要型分類(classification/demand_type.py)

SKU×店舗ごとに、対象日より過去 `classification_lookback_days`(既定180日)以内の通常日集合 `N_t` を
使う。

```
N  = 通常日数
N+ = 通常日のうち actual_sales_qty > 0 の日数
ADI = N / N+                         (N+ == 0 の場合は分類不能)
CV² = (std(正需要日の数量) / mean(正需要日の数量))²

N < minimum_reference_days(28)       → UNCLASSIFIED
ADI < 1.32 and CV² < 0.49            → LOW_VOLUME_SMOOTH
ADI >= 1.32 and CV² < 0.49           → INTERMITTENT_STABLE_QTY
それ以外(ERRATIC/LUMPY相当)         → UNCLASSIFIED (Phase1対象外として扱う)
```

### 3.3 ベースライン計算(P0範囲・簡略化版)

対象日より過去 `baseline_lookback_days`(既定56日)以内の通常日集合を使う(3.2の需要型判定とは
異なる参照期間)。

```
LOW_VOLUME_SMOOTH:
    有効通常日数 >= minimum_normal_days(14) → baseline = mean(通常日の actual_sales_qty)
    8 <= 有効通常日数 < 14                  → baseline = median(同上)
    有効通常日数 < 8                        → baseline = None (補正不能)

INTERMITTENT_STABLE_QTY:
    p_raw = 正需要日数 / 通常日数
    正需要日数 >= minimum_positive_days(5)  → m_raw = mean(正需要日の actual_sales_qty)
    正需要日数 < 5                          → m_raw = None (補正不能)
    baseline = p_raw * m_raw (m_rawがNoneならNone)
```

### 3.4 補正計算(adjustment/decision_table.py, 16/21章準拠)

```
oos_status == NO_OOS or NOT_APPLICABLE
    → adjusted = actual, reason = NO_ADJUSTMENT_NO_OOS, confidence = HIGH(NO_OOS) / NONE(N/A)

oos_status == INVENTORY_DATA_ERROR
    → adjusted = actual, reason = NO_ADJUSTMENT_INVENTORY_ERROR, confidence = NONE

promotion_flag == 1 and oos_status in (FULL_DAY_OOS, POSSIBLE_OOS)
    → adjusted = actual, reason = NO_ADJUSTMENT_PROMOTION, confidence = NONE

demand_type == UNCLASSIFIED
    → adjusted = actual, reason = NO_ADJUSTMENT_UNCLASSIFIED, confidence = NONE

baseline is None (参照件数不足)
    → adjusted = actual, reason = NO_ADJUSTMENT_INSUFFICIENT_DATA, confidence = NONE

LOW_VOLUME_SMOOTH   & FULL_DAY_OOS/POSSIBLE_OOS  → adjusted = max(actual, baseline), reason = ADJ_FULL/POSSIBLE_OOS_SMOOTH
INTERMITTENT_STABLE & FULL_DAY_OOS/POSSIBLE_OOS  → adjusted = max(actual, baseline), reason = ADJ_FULL/POSSIBLE_OOS_INTERMITTENT

confidence:
    LOW_VOLUME_SMOOTH かつ FULL_DAY_OOS        → HIGH
    LOW_VOLUME_SMOOTH かつ POSSIBLE_OOS        → LOW  (欠品時刻不明のため)
    INTERMITTENT かつ FULL_DAY_OOS             → MEDIUM
    INTERMITTENT かつ POSSIBLE_OOS             → LOW

estimated_lost_sales_qty = adjusted - actual   (常に >= 0)
```

`baseline_hierarchy` / `probability_hierarchy` / `quantity_hierarchy` は固定値 `"SKU_STORE"` を保存
する(P0はフォールバック無しのため)。`reference_normal_days` / `reference_positive_days` はその都度
の集計件数を保存する。

---

## 4. エッジケース・不変条件・監査項目

### 4.1 自動検査する不変条件(31.3準拠、P0範囲)

出力テーブル全行に対し、パイプライン実行の最後に検査し、違反があれば**デモを失敗させる**
(exit code非0 + 違反行を標準出力へ列挙する)。

```
adjusted_sales_qty >= actual_sales_qty        (必須条件23.1)
estimated_lost_sales_qty >= 0
adjusted_sales_qty が None または NaN でない
adjustment_method が NO_ADJUSTMENT_* のとき adjusted_sales_qty == actual_sales_qty
```

上限 `U_t` は今回スコープ外のため `adjusted <= U_t` は検査しない。

### 4.2 日付リーケージ防止・補正値の再利用禁止の実装方法

- 通常日抽出・ベースライン計算は「対象日より過去の通常日」のみを参照させる関数シグネチャにする
  (`compute_baseline(records_before_target_date, target_date)` のように、対象日を含む将来データを
  引数に渡さない設計で強制する)。
- ベースライン計算に使う実売上は常に生の `actual_sales_qty` のみを参照し、`adjusted_sales_qty` を
  入力に取る経路を型上作らない(`baseline` モジュールは `adjustment` モジュールに依存しない一方向の
  依存関係にする)。

### 4.3 主な境界ケースの扱い

| ケース | 扱い |
| --- | --- |
| 通常日数がちょうど14日/8日/28日 | 3.3の閾値どおり境界含む(`>=`) |
| 正需要日数がちょうど5日 | `>= 5` でmean計算対象に含める |
| 実売上がベースラインを上回る | `max(actual, baseline)` によりactualを維持 |
| ベースラインNone(参照不足) | `NO_ADJUSTMENT_INSUFFICIENT_DATA`、confidence=NONE |
| 在庫収支異常 | 通常日集合・需要型判定・ベースライン計算のいずれからも除外 |
| 休業日(store_open_flag=0) | `NOT_APPLICABLE`、通常日集合から除外 |
| 終日欠品なのに実売上>0(矛盾) | 在庫収支チェックで検出され`INVENTORY_DATA_ERROR`になる想定(7.1と6章の整合) |

---

## 5. Masking Test(evaluation/masking_test.py)

```
対象: LOW_VOLUME_SMOOTH / INTERMITTENT_STABLE_QTY の通常日(NO_OOS)のみ
手順:
  1. 通常日を1件選び、その日のactual_sales_qtyを「隠された正解」として保持する
  2. その日をFULL_DAY_OOSとして扱い、その日より前の通常日だけを使ってbaselineを再計算する
  3. 決定表を適用してadjusted_sales_qtyを算出する
  4. 比較対象3方式で誤差を算出する:
       A. 補正なし(常に0固定と仮定)
       B. 通常日単純平均(既存baseline計算をそのまま流用)
       C. 通常日中央値
  5. 全対象日について MAE / WAPE / Mean Error を需要型別に集計して標準出力へ表示する
```

既存 `baseline/low_volume.py` の平均/中央値ロジックをそのまま比較方式B/Cとして再利用する(提案方式
=B相当のため、実質「提案方式 vs 中央値 vs 補正なし」の3者比較になる)。

---

## 6. CLI設計(scripts/run_demo.py)

```bash
uv run python scripts/run_demo.py \
    --db data/sample/demand.duckdb \
    [--masking-test]
```

- 入力: 既存の `raw_daily_sales` テーブル(`generate_dummy_data.py` で事前生成)
- 出力: 同じDuckDBファイルへ `fact_daily_sales_demand_adjustment` テーブルを作成し、標準出力へ
  サマリー(件数: oos_status別/demand_type別/confidence_level別、不変条件チェック結果)を表示する
- `--masking-test` 指定時のみ5章を追加実行し、需要型別のMAE/WAPE/Mean Errorを表示する

---

## 7. テスト計画

`tests/` 配下に仕様書31.1の該当ケース(P0範囲分)をモジュール単位のpytestとして実装する。

- 欠品なし・通常日
- 終日欠品・低量安定型
- 終日欠品・間欠型
- 実売上が期待需要を上回る
- ベースラインがNULL(参照件数不足)
- 参照件数不足 → 上位階層フォールバックはせず補正しない(P0はフォールバック非対応のため)
- 在庫収支異常
- 販促日(Phase 1では原則補正しない)
- 正常なゼロ販売日(間欠型の確率分母へ含める)
- 欠品日のゼロ販売(通常日集合へ含めない)
- 将来データが存在(対象日以前だけを参照)
- 補正済み過去値が存在(ベースラインへ使用しない)

境界値(31.2)は `demand_type.py` と `baseline` の閾値テストとして追加する:
`ADI=1.32` 前後、`CV²=0.49` 前後、通常日数 `7/8/13/14`、正需要日数 `4/5`。

---

## 8. 用語対応(実装 ↔ 仕様書)

| 実装上の名称 | 仕様書の記号/用語 |
| --- | --- |
| `baseline`(低量安定型) | \(\hat D_t\)(10章) |
| `p_raw` | \(p_{raw}\)(11.1) |
| `m_raw` | \(m_{raw}\)(11.2) |
| `adjusted_sales_qty` | \(AdjustedSales_t\) |
| `estimated_lost_sales_qty` | \(EstimatedLostSales_t\) |
