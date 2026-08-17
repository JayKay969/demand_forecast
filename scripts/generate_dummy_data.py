"""ダミー売上・在庫データ生成スクリプト。

design/欠品補正改.md 4.1章「必須データ」のスキーマに沿った
SKU × 店舗 × 営業日 の日次データを生成し、DuckDBファイルへ
`raw_daily_sales` テーブルとして書き出す。

需要型は SKU × 店舗ごとに以下のいずれかをランダムに割り当てる。

- LOW_VOLUME_SMOOTH  : 低量安定型 (毎日ほぼ一定量が売れる)
- INTERMITTENT_STABLE_QTY : 間欠・数量安定型 (発生確率 p, 発生時数量 m)

在庫はシミュレーション上の在庫収支(6章)に整合するよう積み上げるが、
一定確率で意図的に在庫差異を発生させ、INVENTORY_DATA_ERROR 判定の
テスト材料とする。欠品(在庫切れ)も一定確率で発生させ、終日欠品・
実売上0のケースを作る。

使い方:
    uv run python scripts/generate_dummy_data.py \\
        --stores 5 --skus 20 --days 180 \\
        --out data/sample/demand.duckdb
"""

from __future__ import annotations

import argparse
import datetime as dt
import random

import duckdb

CREATE_TABLE_SQL = """
CREATE OR REPLACE TABLE raw_daily_sales (
    business_date          DATE    NOT NULL,
    store_id               VARCHAR NOT NULL,
    sku_id                 VARCHAR NOT NULL,
    actual_sales_qty       INTEGER NOT NULL,
    opening_inventory_qty  INTEGER NOT NULL,
    closing_inventory_qty  INTEGER NOT NULL,
    receipt_qty            INTEGER NOT NULL,
    transfer_in_qty        INTEGER NOT NULL,
    transfer_out_qty       INTEGER NOT NULL,
    disposal_qty           INTEGER NOT NULL,
    return_qty             INTEGER NOT NULL,
    store_open_flag        INTEGER NOT NULL,
    promotion_flag         INTEGER NOT NULL,
    regular_price          DECIMAL(10, 2) NOT NULL,
    actual_price           DECIMAL(10, 2) NOT NULL,
    PRIMARY KEY (business_date, store_id, sku_id)
);
"""

INSERT_SQL = """
INSERT INTO raw_daily_sales VALUES
    (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stores", type=int, default=5, help="店舗数 (既定: 5)")
    parser.add_argument("--skus", type=int, default=20, help="SKU数 (既定: 20)")
    parser.add_argument("--days", type=int, default=180, help="生成日数 (既定: 180)")
    parser.add_argument(
        "--out",
        type=str,
        default="data/sample/demand.duckdb",
        help="出力先DuckDBファイル (既定: data/sample/demand.duckdb)",
    )
    parser.add_argument("--seed", type=int, default=42, help="乱数シード (既定: 42)")
    parser.add_argument(
        "--inventory-error-rate",
        type=float,
        default=0.02,
        help="在庫収支を意図的に崩す行の割合 (既定: 0.02)",
    )
    parser.add_argument(
        "--stockout-rate",
        type=float,
        default=0.03,
        help="終日欠品を発生させる日の割合 (既定: 0.03)",
    )
    parser.add_argument(
        "--closed-day-rate",
        type=float,
        default=0.01,
        help="店舗休業日の割合 (既定: 0.01)",
    )
    return parser.parse_args()


class SkuStoreProfile:
    """SKU×店舗ごとの需要パターンと価格を保持する。"""

    def __init__(self, rng: random.Random):
        self.demand_type = rng.choice(["LOW_VOLUME_SMOOTH", "INTERMITTENT_STABLE_QTY"])
        if self.demand_type == "LOW_VOLUME_SMOOTH":
            self.daily_mean = rng.uniform(0.5, 3.0)
        else:
            self.occurrence_prob = rng.uniform(0.1, 0.4)
            self.positive_qty_mean = rng.uniform(1.0, 3.0)
        self.regular_price = round(rng.uniform(150, 3000), 2)
        # 発注のたびに在庫を積み上げる目標水準
        base_rate = (
            self.daily_mean
            if self.demand_type == "LOW_VOLUME_SMOOTH"
            else self.occurrence_prob * self.positive_qty_mean
        )
        self.target_stock = max(3, round(base_rate * 10))
        self.replenishment_cycle_days = rng.choice([3, 7, 14])

    def true_demand(self, rng: random.Random) -> int:
        if self.demand_type == "LOW_VOLUME_SMOOTH":
            qty = round(rng.gauss(self.daily_mean, max(0.3, self.daily_mean * 0.3)))
            return max(0, qty)
        if rng.random() < self.occurrence_prob:
            qty = round(rng.gauss(self.positive_qty_mean, max(0.3, self.positive_qty_mean * 0.3)))
            return max(1, qty)
        return 0


def generate_rows(
    stores: list[str],
    skus: list[str],
    dates: list[dt.date],
    rng: random.Random,
    inventory_error_rate: float,
    stockout_rate: float,
    closed_day_rate: float,
) -> list[tuple]:
    rows: list[tuple] = []

    for store_id in stores:
        for sku_id in skus:
            profile = SkuStoreProfile(rng)
            closing_inventory = profile.target_stock

            for day_index, business_date in enumerate(dates):
                store_open = 0 if rng.random() < closed_day_rate else 1

                opening_inventory = closing_inventory
                receipt_qty = 0
                transfer_in_qty = 0
                transfer_out_qty = 0
                disposal_qty = 0
                return_qty = 0
                promotion_flag = 1 if rng.random() < 0.03 else 0
                actual_price = (
                    round(profile.regular_price * 0.85, 2)
                    if promotion_flag
                    else profile.regular_price
                )

                if not store_open:
                    # 休業日は在庫・販売とも動かない
                    closing_inventory = opening_inventory
                    rows.append(
                        (
                            business_date,
                            store_id,
                            sku_id,
                            0,
                            opening_inventory,
                            closing_inventory,
                            receipt_qty,
                            transfer_in_qty,
                            transfer_out_qty,
                            disposal_qty,
                            return_qty,
                            store_open,
                            promotion_flag,
                            profile.regular_price,
                            actual_price,
                        )
                    )
                    continue

                # 定期発注: サイクル日にちで目標在庫まで補充
                if day_index % profile.replenishment_cycle_days == 0:
                    receipt_qty = max(0, profile.target_stock - opening_inventory)

                is_forced_stockout = rng.random() < stockout_rate

                if is_forced_stockout:
                    # 終日欠品: 開店時在庫・当日入荷ともゼロ扱いにして実売上0にする
                    opening_inventory = 0
                    receipt_qty = 0
                    actual_sales_qty = 0
                else:
                    demand = profile.true_demand(rng)
                    available = opening_inventory + receipt_qty
                    actual_sales_qty = min(demand, available)

                expected_closing = (
                    opening_inventory
                    + receipt_qty
                    + transfer_in_qty
                    - transfer_out_qty
                    - actual_sales_qty
                    - disposal_qty
                    + return_qty
                )
                closing_inventory = max(0, expected_closing)

                if rng.random() < inventory_error_rate:
                    # 意図的に在庫収支を崩す(INVENTORY_DATA_ERRORの検証用)
                    closing_inventory += rng.choice([-3, -2, -1, 1, 2, 3])
                    closing_inventory = max(0, closing_inventory)

                rows.append(
                    (
                        business_date,
                        store_id,
                        sku_id,
                        actual_sales_qty,
                        opening_inventory,
                        closing_inventory,
                        receipt_qty,
                        transfer_in_qty,
                        transfer_out_qty,
                        disposal_qty,
                        return_qty,
                        store_open,
                        promotion_flag,
                        profile.regular_price,
                        actual_price,
                    )
                )

    return rows


def main() -> None:
    args = parse_args()
    rng = random.Random(args.seed)

    stores = [f"S{store_no:03d}" for store_no in range(1, args.stores + 1)]
    skus = [f"SKU{sku_no:05d}" for sku_no in range(1, args.skus + 1)]

    end_date = dt.date.today() - dt.timedelta(days=1)
    start_date = end_date - dt.timedelta(days=args.days - 1)
    dates = [start_date + dt.timedelta(days=offset) for offset in range(args.days)]

    rows = generate_rows(
        stores=stores,
        skus=skus,
        dates=dates,
        rng=rng,
        inventory_error_rate=args.inventory_error_rate,
        stockout_rate=args.stockout_rate,
        closed_day_rate=args.closed_day_rate,
    )

    con = duckdb.connect(args.out)
    try:
        con.execute(CREATE_TABLE_SQL)
        con.executemany(INSERT_SQL, rows)
        row_count = con.execute("SELECT COUNT(*) FROM raw_daily_sales").fetchone()[0]
    finally:
        con.close()

    print(f"generated {row_count} rows -> {args.out}")
    print(f"stores={len(stores)} skus={len(skus)} days={len(dates)}")


if __name__ == "__main__":
    main()
