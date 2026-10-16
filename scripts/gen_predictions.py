#!/usr/bin/env python3
"""Ghost Grid: AI生成（V4・A・B）の予想を、結果の更新のあとに自動で作って ai-generated.json に保存する。

- index.html をヘッドレスブラウザで開き、アプリ内の GG_AI.generate をそのまま呼ぶ（アプリ内の生成と同じ結果になる）
- ロト7・ロト6それぞれ、次の回に向けて V4/A/B × 2本（k=1,2）＝計6本を作る
- すでに同じ id がある分は作り直さない（何度実行しても同じ）
- 直近 KEEP_TARGETS 回分の予想だけを残す（古い分は削除）
- 乱数の種は「ゲーム・回号・コーナー・番号」で決まるので、同じデータなら、アプリを開いたときと同じ予想になる

使い方: python scripts/gen_predictions.py   （リポジトリ直下で実行）
前提: pip install playwright && python -m playwright install --with-deps chromium
"""
import functools, http.server, json, os, sys, threading
from playwright.sync_api import sync_playwright

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
OUT = os.path.join(ROOT, 'ai-generated.json')
KEEP_TARGETS = 4
CORNERS = ['V4', 'A', 'B']
KS = [1, 2]

class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a): pass

def main():
    old = {'loto7': [], 'loto6': []}
    if os.path.exists(OUT):
        try:
            j = json.load(open(OUT, encoding='utf-8'))
            old = {g: [e for e in j.get(g, []) if isinstance(e, dict) and e.get('id')] for g in old}
        except Exception as e:
            print('既存の ai-generated.json が読めないので作り直します:', e)
    handler = functools.partial(Quiet, directory=ROOT)
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    new_count = 0
    with sync_playwright() as p:
        b = p.chromium.launch(args=['--no-sandbox'])
        ctx = b.new_context(service_workers='block')
        pg = ctx.new_page()
        errs = []
        pg.on('pageerror', lambda e: errs.append(str(e)))
        pg.goto(f'http://127.0.0.1:{port}/index.html?ai=1')
        pg.wait_for_function('window.GG_AI', timeout=20000)
        pg.wait_for_timeout(500)
        for game in ['loto7', 'loto6']:
            have = {e['id'] for e in old[game]}
            for c in CORNERS:
                for k in KS:
                    e = pg.evaluate('([g, c, k]) => window.GG_AI.generate(g, c, k)', [game, c, k])
                    if e['id'] in have:
                        continue
                    old[game].append(e); have.add(e['id']); new_count += 1
        b.close()
    srv.shutdown()
    if errs:
        print('ページのエラー:', errs[:3])
    for g in old:
        targets = sorted({e['targetNo'] for e in old[g]})[-KEEP_TARGETS:]
        old[g] = sorted([e for e in old[g] if e['targetNo'] in targets], key=lambda e: (e['targetNo'], e['corner'], e['k']))
    out = {'updatedAt': __import__('datetime').datetime.now(__import__('datetime').timezone(__import__('datetime').timedelta(hours=9))).strftime('%Y-%m-%d %H:%M JST'),
           'note': 'AI生成（V4・A・B）の自動生成の記録。当選を保証するものではなく、観察用。', **old}
    text = json.dumps(out, ensure_ascii=False, indent=1) + '\n'
    if os.path.exists(OUT):
        prev = json.load(open(OUT, encoding='utf-8'))
        if {k: v for k, v in prev.items() if k in ('loto7', 'loto6')} == {k: out[k] for k in ('loto7', 'loto6')}:
            print('変更なし'); return
    open(OUT, 'w', encoding='utf-8').write(text)
    print(f'ai-generated.json を更新しました（新規 {new_count} 本）')

if __name__ == '__main__':
    sys.exit(main())
