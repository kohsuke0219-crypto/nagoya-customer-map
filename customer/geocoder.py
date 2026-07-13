"""住所を緯度経度に変換するジオコーダ（フォールバック用）。

このプロジェクトの主な座標変換は customer/zip_to_coord.py（郵便番号CSVによる
オフライン変換・無料）が担当する。郵便番号CSVに無い住所が来た場合の
フォールバックとして、Google Geocoding API を使う。

daikichi-mapper の scraper/geocoder.py から再利用。GOOGLE_MAPS_API_KEY が
未設定なら single から呼ばれても None を返すだけで、致命的エラーにはしない。

Google Geocoding API: $5 / 1000 リクエスト（概算）
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

GEOCODE_ENDPOINT = "https://maps.googleapis.com/maps/api/geocode/json"


@dataclass
class GeocodeResult:
    latitude: float | None
    longitude: float | None
    formatted_address: str = ""
    status: str = ""  # API のステータス（OK / ZERO_RESULTS など）


class Geocoder:
    """Google Geocoding API のシンプルラッパー。

    Args:
        api_key: GOOGLE_MAPS_API_KEY。None の場合は環境変数から読む。
        cache: {address: GeocodeResult} の辞書。前回結果を渡せば API 呼び出しを節約できる。
        throttle_sec: 連続呼び出しの間隔（QPS 制限避け）。
    """

    def __init__(
        self,
        api_key: str | None = None,
        cache: dict[str, GeocodeResult] | None = None,
        throttle_sec: float = 0.05,
    ) -> None:
        self.api_key = api_key or os.environ.get("GOOGLE_MAPS_API_KEY", "")
        self.cache = cache or {}
        self.throttle_sec = throttle_sec
        self.api_call_count = 0

    @property
    def available(self) -> bool:
        """API キーが設定されていて呼び出し可能かどうか。"""
        return bool(self.api_key)

    def geocode(self, address: str) -> GeocodeResult:
        """住所をジオコードする。キャッシュにあればそれを返す。"""
        if not address:
            return GeocodeResult(None, None, status="EMPTY_ADDRESS")
        if not self.available:
            return GeocodeResult(None, None, status="NO_API_KEY")

        cached = self.cache.get(address)
        if cached and cached.latitude is not None:
            return cached

        result = self._call_api(address)
        self.cache[address] = result
        return result

    def _call_api(self, address: str) -> GeocodeResult:
        """実際に Geocoding API を呼ぶ。"""
        params = {
            "address": address,
            "key": self.api_key,
            "language": "ja",
            "region": "jp",
        }
        try:
            resp = requests.get(GEOCODE_ENDPOINT, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            logger.warning("Geocoding HTTP error for %r: %s", address, e)
            return GeocodeResult(None, None, status="HTTP_ERROR")

        self.api_call_count += 1
        if self.throttle_sec:
            time.sleep(self.throttle_sec)

        status = data.get("status", "")
        if status != "OK" or not data.get("results"):
            logger.info("Geocoding returned %s for %r", status, address)
            return GeocodeResult(None, None, status=status)

        top = data["results"][0]
        loc = top["geometry"]["location"]
        return GeocodeResult(
            latitude=loc["lat"],
            longitude=loc["lng"],
            formatted_address=top.get("formatted_address", ""),
            status="OK",
        )
