"""Google スプレッドシートへの接続モジュール。

daikichi-mapper の scraper/sheets_writer.py から、認証・オープン部分だけを
抜き出して再利用したもの。このプロジェクトではフォーム連携シートを
「読み取る」だけなので、書き込み系の関数は持ち込んでいない。

サービスアカウントは daikichi-mapper のものを再利用する:
    daikichi-sheets-writer@daikichi-mapper.iam.gserviceaccount.com
対象スプレッドシートに、このサービスアカウントを閲覧者として共有しておくこと。
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Callable, TypeVar

import gspread
from google.oauth2.service_account import Credentials

try:
    from requests.exceptions import RequestException
except Exception:  # requests は gspread の依存に含まれるが念のため
    RequestException = ()  # type: ignore

logger = logging.getLogger(__name__)

# 一時的なサーバ側エラー（この場合だけリトライする）
_TRANSIENT_STATUS = {429, 500, 502, 503, 504}
_T = TypeVar("_T")


def call_with_retry(fn: Callable[..., _T], *args: Any,
                    attempts: int = 5, base_delay: float = 2.0, **kwargs: Any) -> _T:
    """Google API 呼び出しを一時エラー時に指数バックオフで再試行する。

    503(一時利用不可)・429(レート超過)・500/502/504 などの一過性エラーや
    ネットワーク例外のみ再試行し、それ以外（認証/権限エラー等）は即座に送出する。
    待機は 2,4,8,16 秒（計 ~30 秒）で、毎時実行のジョブが一時障害を自己回復できる。
    """
    last_err: Exception | None = None
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status not in _TRANSIENT_STATUS:
                raise
            last_err = e
        except RequestException as e:  # 接続断・タイムアウト等
            last_err = e
        if i < attempts - 1:
            delay = base_delay * (2 ** i)
            logger.warning("Google API 一時エラー、%.0f秒後に再試行 (%d/%d): %s",
                           delay, i + 1, attempts, last_err)
            time.sleep(delay)
    assert last_err is not None
    raise last_err

# 読み取り専用でも drive スコープが必要（open_by_key のため）
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]


def open_sheet(spreadsheet_id: str, sa_json_path: str | None = None) -> gspread.Spreadsheet:
    """サービスアカウント認証でスプレッドシートを開く。

    Args:
        spreadsheet_id: 対象スプレッドシートの ID（URL の /d/ の後ろ）
        sa_json_path: サービスアカウント JSON のパス。
            None の場合は環境変数から読み込む（GitHub Actions 運用）。
    """
    if sa_json_path:
        creds = Credentials.from_service_account_file(sa_json_path, scopes=SCOPES)
    else:
        creds = _default_creds()
    client = gspread.authorize(creds)
    return call_with_retry(client.open_by_key, spreadsheet_id.strip())


def _default_creds() -> Credentials:
    """環境変数のサービスアカウント JSON を読み込む。"""
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if sa_json:
        info = json.loads(sa_json.lstrip("﻿"))  # UTF-8 BOM を除去
        return Credentials.from_service_account_info(info, scopes=SCOPES)

    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if path:
        return Credentials.from_service_account_file(path, scopes=SCOPES)

    raise RuntimeError(
        "認証情報が見つかりません。GOOGLE_SERVICE_ACCOUNT_JSON または "
        "GOOGLE_APPLICATION_CREDENTIALS を設定してください。"
    )
