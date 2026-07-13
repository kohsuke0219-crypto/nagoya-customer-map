"""来店客データの座標変換＋プライバシー処理 → docs/data/customers.json 生成。

処理の流れ:
  1. スプレッドシートから来店客レコードを取得（fetch_customers）
  2. 郵便番号＋丁目を座標に変換（ZipToCoord）
  3. 個人情報保護のため座標に ±100m のランダムオフセットを適用
  4. 表示用テキストは「都道府県＋市区町村」レベルまで（丁目・番地は含めない）
  5. docs/data/customers.json に書き出し（公開・匿名化済み）

── 個人情報保護の絶対ルール ──────────────────────────────
GitHub Pages は公開URLなので、出力JSONには次を「絶対に」含めない:
  - 郵便番号そのもの
  - 丁目・町域・番地などの詳細住所
  - 個人名（そもそもフォームで集めていない）
座標は ±100m 丸め。表示住所は市区町村まで。

★オフセットは「顧客ごとに固定」する（毎回ランダムに振り直さない）。
  再ビルドのたびに座標が動くと、複数スナップショットの平均から真の位置を
  逆算されうるため。顧客属性からシードを作り、決定論的に同じ点へ丸める。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import random
from datetime import datetime, timezone, timedelta
from typing import Any

from .fetch_customers import fetch_customers
from .zip_to_coord import ZipToCoord

logger = logging.getLogger(__name__)

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUTPUT = os.path.normpath(os.path.join(_HERE, "..", "docs", "data", "customers.json"))

# プライバシー保護オフセットの最大半径（メートル）
OFFSET_RADIUS_M = 100.0
# 緯度1度あたりの距離（メートル）。日本付近でほぼ一定。
METERS_PER_DEG_LAT = 111_000.0

JST = timezone(timedelta(hours=9))


# 照合に使うフィールド（この順・この区切りで Apps Script 側と厳密に一致させる）
_ID_FIELDS = ("postal_code", "chome", "gender", "age_group", "newspaper", "registered_at")


def _record_key(rec: dict[str, Any]) -> str:
    return "|".join(str(rec.get(k, "")) for k in _ID_FIELDS)


def _stable_seed(rec: dict[str, Any]) -> int:
    """顧客レコードから安定したシード値を作る（同じ人は常に同じオフセット）。"""
    digest = hashlib.sha256(_record_key(rec).encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


def _record_id(rec: dict[str, Any]) -> str:
    """マップ上での削除に使う不可逆な識別子。

    郵便番号・丁目などの生値そのものは公開しないが、この 12桁ハッシュを
    Apps Script 側で各行から同じ手順で再計算し、一致した行を削除する。
    ★Apps Script(delete_customer.gs)の recordId_() と同一アルゴリズムを保つこと。
    """
    return hashlib.sha256(_record_key(rec).encode("utf-8")).hexdigest()[:12]


def apply_privacy_offset(lat: float, lng: float, seed: int) -> tuple[float, float]:
    """座標に半径 OFFSET_RADIUS_M 以内のランダムオフセットを適用（決定論的）。

    真の位置を隠すため円内一様分布でずらす。seed が同じなら結果も同じ。
    """
    rng = random.Random(seed)
    # 円内一様分布: 角度と、sqrt でならした半径
    angle = rng.uniform(0, 2 * math.pi)
    radius = OFFSET_RADIUS_M * math.sqrt(rng.random())
    dnorth = radius * math.cos(angle)
    deast = radius * math.sin(angle)

    dlat = dnorth / METERS_PER_DEG_LAT
    meters_per_deg_lng = METERS_PER_DEG_LAT * math.cos(math.radians(lat))
    dlng = deast / meters_per_deg_lng if meters_per_deg_lng else 0.0

    return round(lat + dlat, 6), round(lng + dlng, 6)


def build(
    spreadsheet_id: str,
    output_path: str = DEFAULT_OUTPUT,
    sa_json_path: str | None = None,
) -> dict[str, Any]:
    """来店客データを構築して JSON に書き出す。集計サマリを返す。"""
    customers = fetch_customers(spreadsheet_id, sa_json_path=sa_json_path)
    converter = ZipToCoord()

    out_records: list[dict[str, Any]] = []
    skipped = 0
    for rec in customers:
        res = converter.resolve(rec["postal_code"], rec.get("chome"))
        if not res.found:
            skipped += 1
            logger.info("座標化できずスキップ: 郵便番号=%s", rec["postal_code"])
            continue

        seed = _stable_seed(rec)
        lat, lng = apply_privacy_offset(res.latitude, res.longitude, seed)

        # ★出力には市区町村レベルの表示テキストと丸め座標のみ。詳細住所は入れない。
        out_records.append(
            {
                "id": _record_id(rec),  # 削除照合用の不可逆ID（生の住所は含まない）
                "lat": lat,
                "lng": lng,
                "area": f"{res.prefecture}{res.city}",  # 例: 愛知県名古屋市守山区
                "gender": rec.get("gender", ""),
                "age_group": rec.get("age_group", ""),
                "newspaper": rec.get("newspaper", ""),
                "registered_at": rec.get("registered_at", ""),
                "precision": res.precision,  # chome/town/city（内部指標・住所は含まない）
            }
        )

    # 新聞社別集計（動的カテゴリ）
    by_newspaper: dict[str, int] = {}
    for r in out_records:
        key = r["newspaper"] or "未回答"
        by_newspaper[key] = by_newspaper.get(key, 0) + 1

    store = {"name": "買取大吉 守山大森店", "lat": 35.2017705, "lng": 136.9961965}
    # generated_at 以外の「実データ」部分。これが前回と同じなら書き換えない
    # （10分ごとの自動実行で無意味なコミットを量産しないため）。
    data_body = {
        "store": store,
        "total": len(out_records),
        "by_newspaper": by_newspaper,
        "customers": out_records,
    }

    if _same_as_existing(output_path, data_body):
        logger.info("データに変化なし。customers.json は更新しません（%d 件）", len(out_records))
        return {
            "total": len(out_records), "skipped": skipped,
            "by_newspaper": by_newspaper, "changed": False,
        }

    payload = {"generated_at": datetime.now(JST).isoformat(timespec="seconds"), **data_body}
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    logger.info(
        "customers.json を書き出し: %d 件（スキップ %d 件）-> %s",
        len(out_records), skipped, output_path,
    )
    return {
        "total": len(out_records), "skipped": skipped,
        "by_newspaper": by_newspaper, "changed": True,
    }


def _same_as_existing(output_path: str, data_body: dict[str, Any]) -> bool:
    """既存の customers.json が data_body（generated_at 除く）と同一かどうか。"""
    if not os.path.exists(output_path):
        return False
    try:
        with open(output_path, encoding="utf-8") as f:
            old = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False
    old_body = {k: old.get(k) for k in ("store", "total", "by_newspaper", "customers")}
    # dict の等価比較で内容一致を判定（キー順は影響しない）
    return old_body == data_body


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="来店客データを構築して customers.json を生成")
    parser.add_argument(
        "--spreadsheet-id",
        default=os.environ.get("SPREADSHEET_ID", "14RapBTPI4fXUi4JbZfZbK_aD8g_hgMBA6faPOmvVSPc"),
    )
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--sa-json", default=None, help="サービスアカウントJSONのパス")
    args = parser.parse_args()

    summary = build(args.spreadsheet_id, output_path=args.output, sa_json_path=args.sa_json)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
