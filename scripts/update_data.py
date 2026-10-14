#!/usr/bin/env python3
"""Ghost Grid: data.json 自動更新スクリプト（標準ライブラリのみ）

sougaku.com の「当選数字一覧」から、ロト7・ロト6の回号・本数字・ボーナス・
セット球を取得し、data.json を更新する。

- 通常は「最新50回」ページ（軽い）を読み、取れなければ「全回」ページに切り替える
- 取得結果が不自然なとき（行が少ない・回号が戻る等）は data.json を書き換えずに異常終了する
- キャリーオーバー当選（carryover）: 各回の「詳細表示」ページから1等賞金を読み、
  ロト7=6億円以上 / ロト6=4億円以上 の回だけを carryover に追加する（直近10回を保持）。
  すでに入っている手動分はそのまま残る。どこまで確認したかは data.json の coScan に記録する。
- predictionLog には一切触れない（手動管理のまま）
- 新しい回がなければ何も書かない（= GitHub Actions はコミットしない）

使い方:
  python scripts/update_data.py                  # 通常（ネットから取得）
  python scripts/update_data.py --full           # 全回ページから取り直す
  python scripts/update_data.py --html loto7=a.html --html loto6=b.html   # 保存済みHTMLから（テスト用）
"""
import argparse, datetime as dt, json, re, sys, time, urllib.request
from html.parser import HTMLParser

UA = 'ghost-grid-updater/1.0 (personal, twice a day)'
GAMES = {
    'loto7': dict(nmain=7, nbonus=2, maxn=37, label='全パターン',
                  base='http://sougaku.com/loto7/data/list1/',
                  detail='http://sougaku.com/loto7/data/detail/index{no}.html',
                  co_min=600_000_000, co_label='キャリーオーバー当選（過去10回・第{a}〜{b}回）'),
    'loto6': dict(nmain=6, nbonus=1, maxn=43, label='ロト6・全パターン',
                  base='http://sougaku.com/loto6/data/list1/',
                  detail='http://sougaku.com/loto6/data/detail/index{no}.html',
                  co_min=400_000_000, co_label='ロト6・キャリーオーバー当選（過去10回）'),
}
CO_KEEP = 10                # carryover に残す回数
CO_TICK_IDX = [0, 2, 4, 6, 8, 9]
CO_MAX_FETCH = 60           # 1回の実行で取得する詳細ページの上限（超えた分は次回に持ち越し）
CO_SLEEP = 1.5              # 詳細ページ取得の間隔（秒）
RECENT_PAGE = 'index_50.html'
WINDOW = 15                 # 「全パターン」タブに載せる直近回数（今のdata.jsonと同じ）
TICK_IDX = [0, 3, 6, 9, 12, 14]
DATED_RECENT = 60           # この回数分だけ日付を付ける（それより古い回は日付なし）
# ロト6の日付計算の起点（第2144回 = 2026/10/8 木曜）。月・木の交互で前後に計算する。
LOTO6_ANCHOR = (2144, dt.date(2026, 10, 8))
BALLS = set('ABCDEFGHIJ')


# ---------------------------------------------------------------- 取得・解析
def fetch(url, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA})
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.read().decode('utf-8', errors='replace')
        except Exception as e:  # noqa
            last = e
            print(f'  fetch failed ({i+1}/{tries}): {url}: {e}', file=sys.stderr)
            time.sleep(5 * (i + 1))
    raise RuntimeError(f'fetch failed: {url}: {last}')


class _RowParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell = [], None, None

    def handle_starttag(self, tag, attrs):
        if tag == 'tr':
            self.row = []
        elif tag in ('td', 'th') and self.row is not None:
            self.cell = []

    def handle_endtag(self, tag):
        if tag in ('td', 'th') and self.cell is not None and self.row is not None:
            self.row.append(' '.join(''.join(self.cell).split()))
            self.cell = None
        elif tag == 'tr' and self.row is not None:
            self.rows.append(self.row)
            self.row = None

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)


def parse_rows(html, spec):
    """表の行 → {回号: (本数字[昇順], ボーナス[昇順], セット球)}。不正行は警告して除外。"""
    p = _RowParser()
    p.feed(html)
    n, k = spec['nmain'], spec['nbonus']
    out, bad = {}, []
    for r in p.rows:
        if not r or not re.fullmatch(r'第\d+回', r[0]):
            continue
        try:
            no = int(re.sub(r'\D', '', r[0]))
            nums = [int(x) for x in r[1:1 + n + k]]
            ball = r[1 + n + k]
            if len(nums) != n + k or len(set(nums)) != n + k:
                raise ValueError('数字の個数/重複')
            if any(x < 1 or x > spec['maxn'] for x in nums):
                raise ValueError('範囲外')
            if ball not in BALLS:
                raise ValueError(f'セット球 {ball!r}')
            # サイト側で並びが乱れている回（例: ロト6 第2134回）があるため必ず昇順にそろえる
            out[no] = (sorted(nums[:n]), sorted(nums[n:]), ball)
        except Exception as e:  # noqa
            bad.append((r[:3], str(e)))
    return out, bad


def get_rows(game, spec, args):
    if args.html.get(game):
        html = open(args.html[game], encoding='utf-8', errors='replace').read()
        rows, bad = parse_rows(html, spec)
        return rows, bad, 'file'
    if not args.full:
        try:
            rows, bad = parse_rows(fetch(spec['base'] + RECENT_PAGE), spec)
            if len(rows) >= 20:
                return rows, bad, 'recent'
            print(f'  {game}: 最新ページの行が少ないため全回ページに切り替え ({len(rows)}行)', file=sys.stderr)
        except Exception as e:  # noqa
            print(f'  {game}: 最新ページ取得失敗 → 全回ページに切り替え: {e}', file=sys.stderr)
    rows, bad = parse_rows(fetch(spec['base']), spec)
    return rows, bad, 'full'


# ---------------------------------------------------------------- 詳細ページ（1等賞金）
def parse_detail(html, no):
    """詳細ページ → 1等賞金(円)。1等の該当なしは 0。読めなければ ValueError。"""
    if not re.search(rf'第\s*{no}\s*回抽選結果詳細データ', html):
        raise ValueError(f'ページの回号が {no} と一致しない')
    p = _RowParser()
    p.feed(html)
    for r in p.rows:
        if r and r[0] == '1等' and len(r) >= 3:
            m = re.search(r'([\d,]+)\s*円', r[2])
            if m:
                return int(m.group(1).replace(',', ''))
            if '該当' in r[1] or '該当' in r[2] or r[1].startswith('0'):
                return 0
    raise ValueError('1等の行が見つからない')


def scan_carryover(old, rows_by_game, args):
    """新しい回の詳細ページを順に見て、基準額以上の回を拾う。
    戻り値: {game: {'found': {no: 1等賞金}, 'scanned': 確認済みの最大回号}}"""
    out = {}
    for game, spec in GAMES.items():
        latest = max(rows_by_game[game])
        co = old.get(game, {}).get('carryover', {}).get('data', [])
        start = old.get('coScan', {}).get(game)
        if start is None:                      # 初回: 手動で入っている最後の回の次から
            start = max((d['no'] for d in co), default=latest - 1)
        found, scanned, fetched = {}, start, 0
        for no in range(start + 1, latest + 1):
            if fetched >= CO_MAX_FETCH:
                print(f'  {game}: 詳細ページの取得上限 ({CO_MAX_FETCH}) に達したため第{no}回以降は次回', file=sys.stderr)
                break
            try:
                if args.detail.get(game):
                    html = open(args.detail[game], encoding='utf-8', errors='replace').read()
                    html = html.replace('第698回抽選結果詳細データ', f'第{no}回抽選結果詳細データ')  # テスト用
                else:
                    if fetched: time.sleep(CO_SLEEP)
                    try:
                        html = fetch(spec['detail'].format(no=no), tries=2)
                    except Exception:
                        if no != latest:
                            raise
                        # 最新回だけは、番号つきページがまだなく「index.html」に載っていることがある
                        html = fetch(spec['detail'].format(no=''), tries=2)
                fetched += 1
                prize = parse_detail(html, no)
            except Exception as e:  # noqa
                print(f'  {game}: 第{no}回の詳細を読めないため、ここで中断（次回に再挑戦）: {e}', file=sys.stderr)
                break
            scanned = no
            if prize >= spec['co_min']:
                found[no] = prize
        out[game] = {'found': found, 'scanned': scanned}
        print(f"{game}: 詳細確認 第{start+1}〜{scanned}回 → 基準({spec['co_min']:,}円)以上: {found or 'なし'}", file=sys.stderr)
    return out


# ---------------------------------------------------------------- 日付
def fmt_date(d):
    return f'{d.year}/{d.month}/{d.day}'


def parse_date(s):
    y, m, d = (int(x) for x in s.split('/'))
    return dt.date(y, m, d)


def known_dates(old, game):
    out = {}
    g = old.get(game, {})
    for sec in ('all', 'carryover'):
        for d in g.get(sec, {}).get('data', []):
            if d.get('date'):
                out[d['no']] = parse_date(d['date'])
    return out


def make_date_fn(game, known):
    """回号→日付。ロト7=毎週金曜、ロト6=月・木の交互（年末年始などの例外は反映されない近似）。"""
    if game == 'loto7':
        if not known:
            return lambda no: None
        a_no = max(known)  # 一番新しい既知の日付を起点にする
        a_date = known[a_no]
        return lambda no: a_date + dt.timedelta(days=7 * (no - a_no))
    a_no, a_date = LOTO6_ANCHOR

    def f(no):
        weeks, r = divmod(no - a_no, 2)
        return a_date + dt.timedelta(days=7 * weeks + (0 if r == 0 else 4))
    return f


# ---------------------------------------------------------------- 本体
def build(old, rows_by_game, co=None):
    data = json.loads(json.dumps(old))  # deep copy
    warnings = []
    for game, spec in GAMES.items():
        # 既存の履歴（draws があればそれ、なければ baseline）に取得結果を上書きマージ
        hist = {}
        for item in old.get('draws', {}).get(game, []):
            hist[item[0]] = (item[2], item[3], item[1])
        if not hist:
            for b in old.get(game, {}).get('baseline', []):
                hist[b['no']] = (b['main'], [], '')
        old_max = max(hist) if hist else 0
        fetched = rows_by_game[game]
        new_max = max(fetched)
        if new_max < old_max:
            raise RuntimeError(f'{game}: 取得した最新回 {new_max} が保存済みの {old_max} より古い')
        changed = [no for no, v in fetched.items() if no in hist and hist[no][0] != v[0]]
        if changed:
            warnings.append(f'{game}: 既存の本数字と食い違う回があり、取得値を採用: {sorted(changed)[:10]}')
        hist.update(fetched)

        nos = sorted(hist)
        known = known_dates(old, game)
        date_fn = make_date_fn(game, known)
        bad_dates = [no for no, d in known.items() if date_fn(no) and date_fn(no) != d]
        if bad_dates:
            warnings.append(f'{game}: 日付の計算が既知の日付と合わない回: {sorted(bad_dates)[:10]}')

        def date_of(no):
            if no in known:
                return fmt_date(known[no])
            if no > nos[-1] - DATED_RECENT:
                d = date_fn(no)
                return fmt_date(d) if d else ''
            return ''

        # 「全パターン」: 直近 WINDOW 回
        win = nos[-WINDOW:]
        a, b = win[0], win[-1]
        ticks = [f'第{win[i]}回' if j in (0, len(TICK_IDX) - 1) else str(win[i])
                 for j, i in enumerate(TICK_IDX)]
        sec = data[game]
        sec['all'] = {
            'label': f"{spec['label']}（第{a}〜{b}回）",
            'tickLabels': ticks,
            'maxNumber': spec['maxn'],
            'data': [{'no': no, 'date': date_of(no), 'main': hist[no][0], 'bonus': hist[no][1]} for no in win],
        }
        # baseline: 並び順は今のdata.jsonに合わせる（ロト7=新しい順 / ロト6=古い順）
        order = sorted(nos, reverse=True) if game == 'loto7' else nos
        sec['baseline'] = [{'no': no, 'main': hist[no][0]} for no in order]
        # セット球つきの全履歴（セット球コーナー用）: [回号, セット球, 本数字, ボーナス, 日付]
        data.setdefault('draws', {})[game] = [
            [no, hist[no][2], hist[no][0], hist[no][1], date_of(no)] for no in nos]
        # キャリーオーバー当選: 既存（手動分を含む）に、基準額以上の回を追加して直近 CO_KEEP 回を保持
        if co and co.get(game):
            cd = {d['no']: d for d in sec['carryover']['data']}
            for no in co[game]['found']:
                cd[no] = {'no': no, 'date': date_of(no), 'main': hist[no][0], 'bonus': hist[no][1]}
            keep = [cd[n] for n in sorted(cd)][-CO_KEEP:]
            for d in keep:                      # 日付が空なら、わかる範囲で補う
                if not d.get('date'):
                    d['date'] = date_of(d['no'])
            ca, cb = keep[0]['no'], keep[-1]['no']
            ticks = [f"第{keep[i]['no']}回" if j in (0, len(CO_TICK_IDX) - 1) else str(keep[i]['no'])
                     for j, i in enumerate(CO_TICK_IDX) if i < len(keep)]
            sec['carryover'].update({'label': spec['co_label'].format(a=ca, b=cb), 'tickLabels': ticks, 'data': keep})
            data.setdefault('coScan', {})[game] = co[game]['scanned']
    return data, warnings


def strip_meta(d):
    d = json.loads(json.dumps(d))
    d.pop('meta', None)
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--data', default='data.json')
    ap.add_argument('--full', action='store_true')
    ap.add_argument('--html', action='append', default=[], help='game=path (テスト用)')
    ap.add_argument('--detail', action='append', default=[], help='game=path 詳細ページ見本（テスト用）')
    ap.add_argument('--no-co', action='store_true', help='キャリーオーバーの確認をしない')
    args = ap.parse_args()
    args.html = dict(x.split('=', 1) for x in args.html)
    args.detail = dict(x.split('=', 1) for x in args.detail)

    old = json.load(open(args.data, encoding='utf-8'))
    rows_by_game = {}
    for game, spec in GAMES.items():
        rows, bad, mode = get_rows(game, spec, args)
        print(f'{game}: {len(rows)}行 ({mode}), 不正行 {len(bad)}', file=sys.stderr)
        if bad:
            print(f'  不正行の例: {bad[:3]}', file=sys.stderr)
        if len(rows) < 20 or len(bad) > max(3, len(rows) // 20):
            raise RuntimeError(f'{game}: 取得結果が不自然（行={len(rows)}, 不正={len(bad)}）。data.json は変更しません')
        if mode == 'full':
            have = len(old.get('draws', {}).get(game, []))
            if have and len(rows) < have * 0.9:
                raise RuntimeError(f'{game}: 全回ページの行数が保存済みより大幅に少ない ({len(rows)} < {have})')
        rows_by_game[game] = rows

    co = None
    if not args.no_co:
        try:
            co = scan_carryover(old, rows_by_game, args)
        except Exception as e:  # noqa  キャリーオーバーの確認に失敗しても、本体の更新は続ける
            print(f'WARN キャリーオーバーの確認に失敗（本体の更新は続行）: {e}', file=sys.stderr)
    new, warnings = build(old, rows_by_game, co)
    for w in warnings:
        print('WARN', w, file=sys.stderr)
    if strip_meta(new) == strip_meta(old):
        print('変更なし')
        return 0
    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=9)))
    new['meta'] = {
        'updatedAt': now.strftime('%Y-%m-%d %H:%M JST'),
        'source': 'sougaku.com',
        'latest': {g: max(r) for g, r in rows_by_game.items()},
        'datesNote': '日付は ロト7=毎週金曜 / ロト6=月・木の交互 で計算した近似（年末年始等の例外は未反映）。既存の手入力日付は優先。',
        'drawsFormat': '[回号, セット球, 本数字, ボーナス, 日付(直近のみ)]',
    }
    with open(args.data, 'w', encoding='utf-8') as f:
        json.dump(new, f, ensure_ascii=False, separators=(',', ':'))
    print('data.json を更新:', new['meta']['latest'])
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:  # noqa
        print('ERROR:', e, file=sys.stderr)
        sys.exit(1)
