"""Google スプレッドシート（フォーム回答）から来店客データを読み取る。

Googleフォームが書き込むシートの列名は日本語の質問文なので、
キーワードで各項目にマッピングする（列の順番や細かな文言が変わっても動くように）。

出力スキーマ（1件 = dict）:
    postal_code   郵便番号（7桁数字、ハイフンは除去。未入力可）
    chome         丁目（"3丁目" / "丁目なし" / "分からない" など原文）
    address       住所（郵便番号が無いお客様用の自由記述。未入力可）
    gender        性別（男性 / 女性 / その他）
    age_group     年代（10代 / 20代 / ... / 70代以上）
    newspaper     新聞社（動的カテゴリ・原文のまま）
    registered_at 登録日時（フォームのタイムスタンプ）

郵便番号と住所は「どちらか一方」あれば地図化できる（両方空の行だけ除外）。

新聞社は「動的カテゴリ」: ここでは値をそのまま通すだけで、選択肢が増えても
コード修正は不要。色分けは地図側（index.html）が値から自動生成する。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .sheets_client import call_with_retry, open_sheet

logger = logging.getLogger(__name__)

# 各項目を、ヘッダ（質問文）に含まれるキーワードで見分ける。
# 上から順に評価し、最初にキーワードを含んだ列をその項目に割り当てる。
FIELD_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("registered_at", ("タイムスタンプ", "timestamp", "登録日", "日時")),
    ("postal_code", ("郵便", "postal", "zip", "〒")),
    ("chome", ("丁目", "chome")),
    ("gender", ("性別", "gender", "男女")),
    ("age_group", ("年代", "年齢", "age")),
    # 「新聞社」でも「来店のきっかけ」でも認識できるようにキーワードを広める
    ("newspaper", ("新聞", "newspaper", "paper", "紙", "きっかけ", "来店", "経路", "媒体", "知")),
    ("address", ("住所", "address", "所在", "町名", "番地")),
]

SCHEMA_FIELDS = ["postal_code", "chome", "address", "gender", "age_group", "newspaper", "registered_at"]


def _build_header_map(headers: list[str]) -> dict[str, str]:
    """シートのヘッダ行 → {項目名: 実際の列名} のマッピングを作る。"""
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for field, keywords in FIELD_KEYWORDS:
        for h in headers:
            if h in used:
                continue
            if any(kw.lower() in h.lower() for kw in keywords):
                mapping[field] = h
                used.add(h)
                break
    return mapping


def _clean_postal(value: Any) -> str:
    """郵便番号を7桁の数字文字列に正規化。"""
    digits = re.sub(r"\D", "", str(value or ""))
    return digits


def fetch_customers(
    spreadsheet_id: str,
    worksheet_name: str | None = None,
    sa_json_path: str | None = None,
) -> list[dict[str, Any]]:
    """スプレッドシートから来店客レコードのリストを返す。

    Args:
        spreadsheet_id: 対象スプレッドシートID。
        worksheet_name: 対象シート名。None なら先頭シート（フォーム回答）。
        sa_json_path: サービスアカウントJSONのパス（None なら環境変数）。
    """
    ss = open_sheet(spreadsheet_id, sa_json_path=sa_json_path)
    # worksheet 取得・値取得も Google API を叩くため一時エラー(503等)に備えて再試行
    ws = (call_with_retry(ss.worksheet, worksheet_name) if worksheet_name
          else call_with_retry(lambda: ss.sheet1))

    rows = call_with_retry(ws.get_all_values)
    if not rows:
        logger.warning("シートが空です")
        return []

    headers = [h.strip() for h in rows[0]]
    header_map = _build_header_map(headers)
    logger.info("ヘッダマッピング: %s", header_map)

    missing = [f for f in ("postal_code", "chome", "gender", "age_group", "newspaper")
               if f not in header_map]
    if missing:
        logger.warning(
            "未対応のヘッダがあります（見つからない項目: %s）。ヘッダ=%s",
            missing, headers,
        )

    idx = {h: i for i, h in enumerate(headers)}
    customers: list[dict[str, Any]] = []
    for raw in rows[1:]:
        # 空行スキップ
        if not any(cell.strip() for cell in raw):
            continue
        rec: dict[str, Any] = {}
        for field in SCHEMA_FIELDS:
            col = header_map.get(field)
            val = raw[idx[col]].strip() if col and idx.get(col, -1) < len(raw) else ""
            rec[field] = val
        rec["postal_code"] = _clean_postal(rec.get("postal_code"))
        rec["address"] = (rec.get("address") or "").strip()
        # 郵便番号(7桁)も住所も無い行だけ除外（どちらか一方あれば地図化できる）
        if len(rec["postal_code"]) != 7 and not rec["address"]:
            logger.info("郵便番号も住所も無いためスキップ")
            continue
        customers.append(rec)

    logger.info("来店客 %d 件を取得", len(customers))
    return customers


if __name__ == "__main__":
    import json
    import os

    logging.basicConfig(level=logging.INFO)
    sid = os.environ.get("SPREADSHEET_ID", "14RapBTPI4fXUi4JbZfZbK_aD8g_hgMBA6faPOmvVSPc")
    data = fetch_customers(sid)
    print(json.dumps(data, ensure_ascii=False, indent=2))
