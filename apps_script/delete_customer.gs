/**
 * 買取大吉 守山店 来店客マップ — 削除バックエンド Google Apps Script Webアプリ
 *
 * マップ上のピンから「🗑 このデータを削除」を押すと、このWebアプリが呼ばれ、
 * フォーム回答スプレッドシートの該当行を削除する。
 *
 * ● 仕組み
 *   マップの customers.json には、各来店客に不可逆な 12桁ID が入っている
 *   （郵便番号・丁目などの生値は公開していない）。このスクリプトは回答シートの
 *   各行から「同じ手順」でIDを再計算し、一致した行を削除する。
 *   ★ID計算は Python 側 build_customer_data._record_id() と厳密に一致させること。
 *
 * ● 貼り方（回答スプレッドシートにひも付けて動かす）
 *   1. スプレッドシート「買取大吉 来店客記録（名古屋）_回答」を開く
 *   2. 拡張機能 > Apps Script を開く
 *   3. このファイルの全文を貼り付けて保存
 *   4. 下の TOKEN に好きな合言葉を設定（例: 'moriyama2026'）
 *   5. デプロイ > 新しいデプロイ > 種類=ウェブアプリ
 *        実行するユーザー = 自分
 *        アクセスできるユーザー = 全員
 *      → 表示された URL（.../exec）を控える
 *   6. マップの「⚙ 削除の設定」に、その URL と 合言葉(TOKEN) を入力
 *
 *   ※コード更新時は「デプロイを管理 > 編集 > 新バージョン > デプロイ」でURL不変のまま反映。
 *
 * ● 注意
 *   「全員」公開なので URL を知れば誰でも削除可能。URL と合言葉はチーム内だけで共有すること。
 *   削除された回答は、Googleフォームの「回答」タブには残るため完全には失われない。
 */

// ★合言葉。マップの「トークン」欄と同じ値にする。空にすると無認証（非推奨）。
const TOKEN = '';

// 「今すぐ反映」用: GitHub Actions のビルドを起動する設定。
// GITHUB_TOKEN は「プロジェクトの設定 > スクリプト プロパティ」に登録する
// （コードには書かない）。未登録なら「今すぐ反映」は無効（通常の再読込にフォールバック）。
const GH_OWNER = 'kohsuke0219-crypto';
const GH_REPO = 'nagoya-customer-map';
const GH_WORKFLOW = 'update.yml';
const GH_REF = 'master';

// GitHub Actions の update.yml を起動（成功で 204）
function triggerRebuild_() {
  const token = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
  if (!token) return { ok: false, error: 'GITHUB_TOKEN 未設定' };
  const url = 'https://api.github.com/repos/' + GH_OWNER + '/' + GH_REPO +
    '/actions/workflows/' + GH_WORKFLOW + '/dispatches';
  const res = UrlFetchApp.fetch(url, {
    method: 'post',
    contentType: 'application/json',
    headers: {
      Authorization: 'Bearer ' + token,
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
      'User-Agent': 'nagoya-customer-map',
    },
    payload: JSON.stringify({ ref: GH_REF }),
    muteHttpExceptions: true,
  });
  const code = res.getResponseCode();
  if (code === 204) return { ok: true };
  return { ok: false, error: 'GitHub ' + code + ': ' + res.getContentText() };
}

// customers.json の id と一致させるフィールドの順番（変更しないこと）
// Python: _ID_FIELDS = (postal_code, chome, gender, age_group, newspaper, registered_at)
const FIELD_KEYWORDS = [
  ['registered_at', ['タイムスタンプ', 'timestamp', '登録日', '日時']],
  ['postal', ['郵便', 'postal', 'zip', '〒']],
  ['chome', ['丁目', 'chome']],
  ['gender', ['性別', 'gender', '男女']],
  ['age', ['年代', '年齢', 'age']],
  ['newspaper', ['新聞', 'newspaper', 'paper', '紙', 'きっかけ', '来店', '経路', '媒体', '知']],
];

function json_(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

// シートの全セルを表示文字列で取得（getDisplayValues は Range のメソッド）
function sheetValues_(sh) {
  return sh.getDataRange().getDisplayValues();
}

// フォーム回答シートを探す（郵便番号の列を持つ最初のシート）
function responsesSheet_() {
  const sheets = SpreadsheetApp.getActiveSpreadsheet().getSheets();
  for (let i = 0; i < sheets.length; i++) {
    const values = sheetValues_(sheets[i]);
    if (values.length >= 1 && headerMap_(values[0]).postal != null) return sheets[i];
  }
  return sheets[0];
}

// ヘッダ行 → {field: 列index}。Python fetch_customers._build_header_map と同じ規則。
function headerMap_(headers) {
  const map = {};
  const used = {};
  FIELD_KEYWORDS.forEach(function (pair) {
    const field = pair[0], kws = pair[1];
    for (let i = 0; i < headers.length; i++) {
      if (used[i]) continue;
      const h = String(headers[i]).toLowerCase();
      const hit = kws.some(function (kw) { return h.indexOf(kw.toLowerCase()) >= 0; });
      if (hit) { map[field] = i; used[i] = true; break; }
    }
  });
  return map;
}

function cell_(row, idx) {
  return idx == null ? '' : String(row[idx] == null ? '' : row[idx]).trim();
}

// 1行分 → 12桁ID。Python build_customer_data._record_id() と同一アルゴリズム。
function recordId_(row, m) {
  const postal = String(row[m.postal] == null ? '' : row[m.postal]).replace(/[^0-9]/g, '');
  const key = [
    postal,
    cell_(row, m.chome),
    cell_(row, m.gender),
    cell_(row, m.age),
    cell_(row, m.newspaper),
    cell_(row, m.registered_at),
  ].join('|');
  const bytes = Utilities.computeDigest(
    Utilities.DigestAlgorithm.SHA_256, key, Utilities.Charset.UTF_8);
  let hex = '';
  for (let i = 0; i < bytes.length; i++) {
    hex += ('0' + (bytes[i] & 0xFF).toString(16)).slice(-2);
  }
  return hex.slice(0, 12);
}

// 動作確認用: ブラウザで exec URL を開くと件数などが見える
function doGet(e) {
  const sh = responsesSheet_();
  const values = sheetValues_(sh);
  const m = values.length ? headerMap_(values[0]) : {};
  const out = { ok: true, sheet: sh.getName(), dataRows: Math.max(0, values.length - 1) };
  if (e && e.parameter && e.parameter.debug) {
    out.ids = [];
    for (let r = 1; r < values.length && r <= 20; r++) out.ids.push(recordId_(values[r], m));
  }
  return json_(out);
}

function doPost(e) {
  const lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    const body = JSON.parse((e && e.postData && e.postData.contents) || '{}');
    if (TOKEN && body.token !== TOKEN) return json_({ ok: false, error: 'unauthorized' });
    const kind = body.kind || '';
    if (kind === 'rebuild') return json_(triggerRebuild_());  // 「今すぐ反映」
    if (kind !== 'delete') return json_({ ok: false, error: 'unknown kind' });
    if (!body.id) return json_({ ok: false, error: 'no id' });

    const sh = responsesSheet_();
    const values = sheetValues_(sh);
    if (values.length < 2) return json_({ ok: true, deleted: 0 });

    const m = headerMap_(values[0]);
    if (m.postal == null) return json_({ ok: false, error: 'header not found' });

    // 下の行から探して削除（行番号のズレを避ける）
    let deleted = 0;
    for (let r = values.length - 1; r >= 1; r--) {
      if (recordId_(values[r], m) === body.id) {
        sh.deleteRow(r + 1);  // values は0始まり、シート行は1始まり
        deleted++;
      }
    }
    return json_({ ok: deleted > 0, deleted: deleted });
  } catch (err) {
    return json_({ ok: false, error: String(err) });
  } finally {
    lock.releaseLock();
  }
}
