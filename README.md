# 買取大吉 名古屋守山店 来店客マップ

新聞折込チラシの効果を分析するための、来店客可視化ツールです。
店長・スタッフが来店客の情報（郵便番号・丁目・性別・年代・新聞社）を
Googleフォームから入力すると、地図上に新聞社別に色分けして表示します。

## 概要

- **対象店舗**: 買取大吉 守山店（愛知県名古屋市守山区）
- **目的**: どの新聞社の折込チラシが、どのエリアの来店客に効いているかを可視化
- **公開URL**: https://kohsuke0219-crypto.github.io/nagoya-customer-map/

## 個人情報保護の方針

このツールは公開URLで運用するため、以下を厳守します。

1. 詳細住所（番地）は一切保存・表示しない
2. 座標は ±100m のランダムオフセットを適用して丸める
3. 個人名は収集しない
4. 表示は「市区町村レベル」まで

## データの流れ

```
Googleフォーム
   └─→ Googleスプレッドシート（郵便番号・丁目・性別・年代・新聞社）
          └─→ customer/build_customer_data.py
                 ├─ 郵便番号＋丁目 → 座標変換（zip_to_coord.py）
                 ├─ ±100m ランダムオフセット
                 └─→ docs/data/customers.json
                        └─→ docs/index.html（Leaflet 地図）
```

## セットアップ

```bash
python -m venv venv
venv\Scripts\activate      # Windows
pip install -r requirements.txt
```

## 構成

| パス | 役割 |
|------|------|
| `customer/zip_to_coord.py` | 郵便番号＋丁目 → 座標変換 |
| `customer/fetch_customers.py` | スプレッドシートから来店客データ取得 |
| `customer/build_customer_data.py` | 座標変換＋プライバシー処理 → JSON 生成 |
| `docs/index.html` | Leaflet 地図（新聞社別色分け） |
| `docs/data/customers.json` | 表示用データ（公開・匿名化済み） |

## 関連プロジェクト

商圏分析ツール「daikichi-mapper」とは完全に分離した独立プロジェクトです。
一部のコード（ジオコーダ・スプレッドシート連携）を再利用しています。
