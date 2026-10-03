"""덱 빌더용 데이터 생성 · 갱신

- data/cards.json   : 한국어 이름이 있는 전체 카드의 종류·스탯·한국어 효과 텍스트·패스코드·아키타입
- data/banlist.json : KONAMI 공식 리미트 레귤레이션 (한국 OCG / 일본 OCG)

사용법
  python tools/update_data.py                 # 새 카드만 추가 + 리미트 레귤레이션 갱신
  python tools/update_data.py --seed FILE     # 미리 받아 둔 카드 덤프(jsonl)로 시작
  python tools/update_data.py --refresh 300   # 최근 카드 300장의 텍스트도 다시 받기 (에라타 반영)

데이터 출처
  카드 텍스트 : db.ygoresources.com (KONAMI 공식 카드 DB 미러)
  패스코드·아키타입 : db.ygoprodeck.com
  리미트 레귤레이션 : www.db.yugioh-card.com (KONAMI 공식)
"""
import argparse, concurrent.futures as cf, html, json, os, re, sys, time, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, 'data')
YR = 'https://db.ygoresources.com'
YPD = 'https://db.ygoprodeck.com/api/v7/cardinfo.php?misc=yes'
OFFICIAL = 'https://www.db.yugioh-card.com/yugiohdb/forbidden_limited.action?request_locale='
UA = {'User-Agent': 'Mozilla/5.0 (ocg_rule deck data updater)'}


def get(url, tries=4, timeout=60):
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                return r.read()
        except Exception as e:
            if k == tries - 1:
                raise
            time.sleep(2 + 3 * k)


def card_row(kid, ko, prev=None):
    """[kid, name, cardType, property, properties, attribute, level, atk, def, linkArrows, passcode, archetype, text]"""
    text = (ko.get('effectText') or '').strip()
    pend = (ko.get('pendulumEffectText') or '').strip()
    if pend:
        text = '【펜듈럼 효과】' + pend + '\n【몬스터 효과】' + text
    return [kid, ko.get('name') or '', ko.get('cardType') or '', ko.get('property') or '', ko.get('properties') or [],
            ko.get('attribute') or '', ko.get('level'), ko.get('atk'), ko.get('def'), ko.get('linkArrows') or '',
            prev[10] if prev else None, prev[11] if prev else None, text]


def fetch_card(kid):
    try:
        j = json.loads(get(f'{YR}/data/card/{kid}'))
        ko = (j.get('cardData') or {}).get('ko')
        return kid, ko
    except Exception:
        return kid, None


def parse_banlist(h):
    """공식 리미트 레귤레이션 페이지 → {cid: 0|1|2}, 갱신일"""
    date = (re.search(r'(20\d\d)/(\d\d)/(\d\d)\s*(?:更新|갱신)', h) or re.search(r'(20\d\d)-(\d\d)-(\d\d)', h))
    date = f'{date.group(1)}-{date.group(2)}-{date.group(3)}' if date else None
    out = {}
    secs = [('list_forbidden', 0), ('list_limited', 1), ('list_semi_limited', 2)]
    for sid, lim in secs:
        i = h.find(f'id="{sid}"')
        if i < 0:
            continue
        nxt = [h.find(f'id="{s}"', i + 1) for s, _ in secs] + [h.find('id="list_release_of_restricted"', i + 1)]
        nxt = [x for x in nxt if x > i]
        part = h[i:min(nxt) if nxt else len(h)]
        for cid in re.findall(r'cid=(\d+)', part):
            out[int(cid)] = lim
    upd = []
    i = h.find('id="list_update"')
    j = h.find('id="list_forbidden"')
    if i >= 0 and j > i:
        part = h[i:j]
        for m in re.finditer(r'<span class="name">(.*?)</span>.*?cid=(\d+).*?<p>(.*?)</p>', part, re.S):
            upd.append([int(m.group(2)), html.unescape(m.group(1)).strip(), html.unescape(m.group(3)).strip()])
    return date, out, upd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--seed', help='카드 덤프 jsonl (kid, ko 필드)')
    ap.add_argument('--refresh', type=int, default=0, help='최근 카드 N장의 텍스트를 다시 받기')
    ap.add_argument('--workers', type=int, default=6)
    a = ap.parse_args()
    os.makedirs(DATA, exist_ok=True)
    cpath, bpath = os.path.join(DATA, 'cards.json'), os.path.join(DATA, 'banlist.json')

    rows = {}
    if os.path.exists(cpath):
        for r in json.load(open(cpath, encoding='utf-8'))['cards']:
            rows[r[0]] = r
    if a.seed:
        for line in open(a.seed, encoding='utf-8'):
            c = json.loads(line)
            if c.get('error') or not c.get('ko'):
                continue
            rows[c['kid']] = card_row(c['kid'], c['ko'], rows.get(c['kid']))
    print('기존 카드', len(rows))

    # 1) 한국어 이름 인덱스 → 새 카드
    idx = json.loads(get(f'{YR}/data/idx/card/name/ko'))
    ids = sorted({i for v in idx.values() for i in v})
    todo = [i for i in ids if i not in rows]
    if a.refresh:
        todo += [i for i in sorted(rows)[-a.refresh:] if i not in todo]
    print('받을 카드', len(todo))
    with cf.ThreadPoolExecutor(a.workers) as ex:
        for kid, ko in ex.map(fetch_card, todo):
            if ko:
                rows[kid] = card_row(kid, ko, rows.get(kid))

    # 2) 패스코드 · 아키타입 (YGOPRODeck)
    try:
        ypd = json.loads(get(YPD, timeout=180))['data']
        by_k = {}
        for c in ypd:
            k = ((c.get('misc_info') or [{}])[0]).get('konami_id')
            if k and k not in by_k:
                by_k[k] = (c.get('id'), c.get('archetype'))
        for kid, r in rows.items():
            if kid in by_k:
                r[10], r[11] = by_k[kid]
        print('패스코드 매칭', sum(1 for r in rows.values() if r[10]))
    except Exception as e:
        print('YGOPRODeck 실패 (기존 값 유지):', e)

    cards = sorted(rows.values(), key=lambda r: r[0])
    old = json.load(open(cpath, encoding='utf-8')) if os.path.exists(cpath) else {}
    if old.get('cards') != json.loads(json.dumps(cards, ensure_ascii=False)):   # 내용이 바뀐 경우에만 날짜 갱신
        json.dump({'updated': time.strftime('%Y-%m-%d'), 'fields': ['kid', 'name', 'cardType', 'property', 'properties', 'attribute', 'level', 'atk', 'def', 'linkArrows', 'passcode', 'archetype', 'text'], 'cards': cards},
                  open(cpath, 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
    print('cards.json', len(cards), os.path.getsize(cpath) // 1024, 'KB')

    # 3) 리미트 레귤레이션 (공식)
    bl = {k: v for k, v in (json.load(open(bpath, encoding='utf-8')) if os.path.exists(bpath) else {}).items() if k not in ('fetched', 'checked')}
    for loc, label in (('ko', '한국 OCG'), ('ja', '일본 OCG')):
        try:
            h = get(OFFICIAL + loc).decode('utf-8', 'replace')
            date, lst, upd = parse_banlist(h)
            if len(lst) < 50:
                raise ValueError('목록이 너무 짧음')
            bl[loc] = {'label': label, 'date': date, 'source': OFFICIAL + loc, 'list': {str(k): v for k, v in sorted(lst.items())}, 'updates': upd}
            print(label, date, len(lst), '장 (변경', len(upd), ')')
        except Exception as e:
            print(label, '실패 (기존 값 유지):', e)
    prev = json.load(open(bpath, encoding='utf-8')) if os.path.exists(bpath) else {}
    if {k: v for k, v in prev.items() if k != 'checked'} != {k: v for k, v in bl.items() if k != 'checked'}:   # 목록이 바뀐 경우에만 저장
        bl['checked'] = time.strftime('%Y-%m-%d')
        json.dump(bl, open(bpath, 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
        print('banlist.json 갱신')


if __name__ == '__main__':
    main()
