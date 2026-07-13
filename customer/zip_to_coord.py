"""郵便番号（＋丁目）を緯度経度に変換するオフライン・ジオコーダ。

無料・高速・オフラインを実現するため、2つの公開データを組み合わせる:

1. 日本郵便の郵便番号CSV（愛知県）
   郵便番号(7桁) → (都道府県, 市区町村, 町域) を得る。
   例: 4600008 → (愛知県, 名古屋市中区, 栄)

2. geolonia/japanese-addresses（緯度経度付き住所データ）
   (都道府県, 市区町村) 内の「町域＋丁目」ごとの緯度経度を得る。
   例: 名古屋市中区「栄三丁目」→ (35.165759, 136.905971)

郵便番号だけだと町域どまりだが、フォームで入力された「丁目」を組み合わせて
丁目単位まで位置を絞り込む。CSVに無い県外の郵便番号は、任意で Google
Geocoding（geocoder.py）にフォールバックする。

いずれのデータもローカルにキャッシュするので、2回目以降は完全オフライン。

参考:
- 日本郵便 郵便番号データ: https://www.post.japanpost.jp/zipcode/dl/kogaki-zip.html
- geolonia/japanese-addresses: https://github.com/geolonia/japanese-addresses
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import zipfile
from dataclasses import dataclass
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

# このファイルからの相対でデータ置き場を決める
_HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(_HERE, "zipdata")
GEOLONIA_DIR = os.path.join(DATA_DIR, "geolonia")

# 日本郵便 都道府県別 小書きCSV（愛知＝23aichi）。UA を付けないと 404 ページが返る。
JAPANPOST_BASE = (
    "https://www.post.japanpost.jp/service/search/zipcode/download/kogaki/zip"
)
JAPANPOST_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

# geolonia の静的 JSON API（町丁目レベルの緯度経度）
GEOLONIA_BASE = "https://geolonia.github.io/japanese-addresses/api/ja"

# 都道府県コード(2桁) → 日本郵便CSVのファイル名（守山店の商圏＝愛知が主）
PREF_ZIP_FILES = {
    "23": "23aichi.zip",
}

_KANJI_DIGITS = "〇一二三四五六七八九"


@dataclass
class ZipCoordResult:
    latitude: float | None
    longitude: float | None
    prefecture: str = ""
    city: str = ""
    town: str = ""  # マッチした町域（内部用・表示には使わない）
    # 精度: "chome"(丁目一致) / "town"(町域中心) / "city"(市区町村中心) / "none"
    precision: str = "none"

    @property
    def found(self) -> bool:
        return self.latitude is not None and self.longitude is not None


def _digit_to_kanji(n: int) -> str:
    """1..9 を漢数字（一..九）に変換。丁目名の照合用。"""
    if 1 <= n <= 9:
        return _KANJI_DIGITS[n]
    return str(n)


def normalize_chome(chome: str | None) -> int | None:
    """フォームの丁目入力から丁目番号(1..9)を取り出す。

    「3丁目」「3」「三丁目」→ 3。「丁目なし」「分からない」「空」→ None。
    """
    if not chome:
        return None
    s = str(chome).strip()
    if not s or s in ("丁目なし", "分からない", "不明", "わからない"):
        return None
    # アラビア数字
    m = re.search(r"([1-9]\d?)", s)
    if m:
        return int(m.group(1))
    # 漢数字（一〜九）
    for i, k in enumerate(_KANJI_DIGITS):
        if i >= 1 and k in s:
            return i
    return None


class ZipToCoord:
    """郵便番号＋丁目 → 緯度経度 変換器。

    Args:
        pref_codes: 事前ロードする都道府県コード（既定=愛知のみ）。
        geocoder: CSVに無い郵便番号のフォールバック（任意）。
    """

    def __init__(
        self,
        pref_codes: list[str] | None = None,
        geocoder=None,
    ) -> None:
        self.geocoder = geocoder
        # {郵便番号7桁: (都道府県, 市区町村, 町域)}
        self.zip_index: dict[str, tuple[str, str, str]] = {}
        # {(都道府県, 市区町村): [{"town","lat","lng"}, ...]}
        self._city_cache: dict[tuple[str, str], list[dict]] = {}

        os.makedirs(GEOLONIA_DIR, exist_ok=True)
        for code in pref_codes or ["23"]:
            self._load_pref_zip(code)

    # ---- 日本郵便CSV -------------------------------------------------

    def _load_pref_zip(self, pref_code: str) -> None:
        """都道府県の郵便番号CSVを読み込む（無ければダウンロード）。"""
        fname = PREF_ZIP_FILES.get(pref_code)
        if not fname:
            logger.warning("郵便番号ファイル未定義の都道府県コード: %s", pref_code)
            return
        zip_path = os.path.join(DATA_DIR, fname)
        if not os.path.exists(zip_path):
            self._download_japanpost(fname, zip_path)

        with zipfile.ZipFile(zip_path) as zf:
            csv_name = next(n for n in zf.namelist() if n.upper().endswith(".CSV"))
            raw = zf.read(csv_name)
        text = raw.decode("cp932")
        reader = csv.reader(io.StringIO(text))
        count = 0
        for row in reader:
            if len(row) < 9:
                continue
            zip7 = row[2].strip()
            pref, city, town = row[6].strip(), row[7].strip(), row[8].strip()
            town = self._clean_town(town)
            # 既出の郵便番号（複数町域）は最初のものを優先（代表町域）
            self.zip_index.setdefault(zip7, (pref, city, town))
            count += 1
        logger.info("郵便番号 %d 件を読み込み（%s）", count, fname)

    def _download_japanpost(self, fname: str, dest: str) -> None:
        url = f"{JAPANPOST_BASE}/{fname}"
        logger.info("郵便番号CSVをダウンロード: %s", url)
        resp = requests.get(url, headers={"User-Agent": JAPANPOST_UA}, timeout=60)
        resp.raise_for_status()
        # HTML（404ページ）が返っていないか簡易チェック
        if resp.content[:2] != b"PK":
            raise RuntimeError(
                f"郵便番号CSVのダウンロードに失敗しました（ZIPではない）: {url}"
            )
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(resp.content)

    @staticmethod
    def _clean_town(town: str) -> str:
        """町域名を照合用に整える。

        「以下に掲載がない場合」など町域が無い表記は空にする。
        「〜（△△を除く）」の括弧書きは落とす。
        """
        if not town or town == "以下に掲載がない場合":
            return ""
        town = re.sub(r"（.*?）", "", town)
        town = re.sub(r"\(.*?\)", "", town)
        # 「〜一円」「〜地内」などのゆらぎはそのまま返す（照合は前方一致で吸収）
        return town.strip()

    # ---- geolonia（座標付き住所） -----------------------------------

    def _load_city_towns(self, pref: str, city: str) -> list[dict]:
        """(都道府県, 市区町村) の町丁目＋緯度経度リストを得る（ディスクキャッシュ）。"""
        key = (pref, city)
        if key in self._city_cache:
            return self._city_cache[key]

        cache_file = os.path.join(
            GEOLONIA_DIR, f"{pref}_{city}.json".replace("/", "_")
        )
        data: list[dict] | None = None
        if os.path.exists(cache_file):
            try:
                with open(cache_file, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                data = None

        if data is None:
            data = self._fetch_geolonia(pref, city)
            if data is not None:
                with open(cache_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)

        data = data or []
        self._city_cache[key] = data
        return data

    def _fetch_geolonia(self, pref: str, city: str) -> list[dict] | None:
        url = f"{GEOLONIA_BASE}/{quote(pref)}/{quote(city)}.json"
        try:
            resp = requests.get(url, timeout=30)
            if resp.status_code == 404:
                logger.info("geolonia に該当なし: %s %s", pref, city)
                return []
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            logger.warning("geolonia 取得失敗 %s %s: %s", pref, city, e)
            return None

    # ---- 変換本体 ----------------------------------------------------

    def resolve(self, postal_code: str, chome: str | None = None) -> ZipCoordResult:
        """郵便番号（＋丁目）を座標に変換する。"""
        zip7 = re.sub(r"\D", "", str(postal_code or ""))
        if len(zip7) != 7:
            return ZipCoordResult(None, None, precision="none")

        entry = self.zip_index.get(zip7)
        if not entry:
            return self._fallback_geocode(zip7, chome)

        pref, city, town_base = entry
        towns = self._load_city_towns(pref, city)
        chome_no = normalize_chome(chome)

        result = ZipCoordResult(
            None, None, prefecture=pref, city=city, town=town_base, precision="none"
        )

        if towns:
            best = self._match_town(towns, town_base, chome_no)
            if best is not None:
                result.latitude = best["lat"]
                result.longitude = best["lng"]
                result.town = best["town"]
                result.precision = best["_precision"]
                return result
            # 町域では当たらなかった → 市区町村の中心にフォールバック
            lat, lng = self._city_centroid(towns)
            if lat is not None:
                result.latitude, result.longitude = lat, lng
                result.precision = "city"
                return result

        # geolonia が空 → Google フォールバック
        return self._fallback_geocode(zip7, chome, base=result)

    def _match_town(
        self, towns: list[dict], town_base: str, chome_no: int | None
    ) -> dict | None:
        """町域＋丁目に最も一致するエントリを返す。

        優先順:
          1. town_base + 丁目（漢数字/アラビア）に完全一致 → precision=chome
          2. town_base に前方一致するものの平均 → precision=town
        """
        if not town_base:
            return None

        # 1. 丁目まで一致
        if chome_no is not None:
            targets = {
                town_base + _digit_to_kanji(chome_no) + "丁目",
                town_base + str(chome_no) + "丁目",
                f"{town_base}{chome_no}",
            }
            for t in towns:
                if t.get("town") in targets:
                    return {**t, "_precision": "chome"}

        # 2. 町域で前方一致（丁目違いも含めて平均座標＝町域中心）
        matches = [t for t in towns if t.get("town", "").startswith(town_base)]
        # 完全一致（丁目のない町域）を最優先
        for t in matches:
            if t.get("town") == town_base:
                return {**t, "_precision": "town"}
        if matches:
            lat = sum(t["lat"] for t in matches) / len(matches)
            lng = sum(t["lng"] for t in matches) / len(matches)
            return {
                "town": town_base,
                "lat": round(lat, 6),
                "lng": round(lng, 6),
                "_precision": "town",
            }
        return None

    @staticmethod
    def _city_centroid(towns: list[dict]) -> tuple[float | None, float | None]:
        pts = [(t["lat"], t["lng"]) for t in towns if "lat" in t and "lng" in t]
        if not pts:
            return None, None
        lat = round(sum(p[0] for p in pts) / len(pts), 6)
        lng = round(sum(p[1] for p in pts) / len(pts), 6)
        return lat, lng

    def _fallback_geocode(
        self, zip7: str, chome: str | None, base: ZipCoordResult | None = None
    ) -> ZipCoordResult:
        """CSV/geolonia で解決できない場合の Google フォールバック。"""
        result = base or ZipCoordResult(None, None, precision="none")
        if self.geocoder is None or not getattr(self.geocoder, "available", False):
            return result
        formatted = f"{zip7[:3]}-{zip7[3:]}"
        address = f"日本、〒{formatted}"
        geo = self.geocoder.geocode(address)
        if geo.latitude is not None:
            result.latitude = geo.latitude
            result.longitude = geo.longitude
            if result.precision == "none":
                result.precision = "city"
        return result


if __name__ == "__main__":
    # 動作確認: 460-0008 + 3丁目 → 名古屋市中区栄三丁目 付近
    logging.basicConfig(level=logging.INFO)
    z = ZipToCoord()
    for code, ch in [("460-0008", "3丁目"), ("463-0074", "丁目なし"), ("4600008", None)]:
        r = z.resolve(code, ch)
        print(
            f"{code} / {ch!r:>8} -> "
            f"({r.latitude}, {r.longitude}) "
            f"{r.prefecture}{r.city} [{r.town}] precision={r.precision}"
        )
