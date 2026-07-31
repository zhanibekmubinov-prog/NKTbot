"""
google_sheets.py — движок Google Sheets для NKT-бота (меню-версия).

Что делает бот через этот модуль:
• пишет строки в журналы «Поступление» / «Передвижение» / «Списание»;
• добавляет локации в «Локации» и номенклатуру в «Справочник»;
• считает остатки на Python (Начальные остатки + журналы − списание) — для ответов в TG;
• даёт справочники (виды/размеры/резьбы/локации) для кнопок;
• локальный нечёткий поиск локаций (difflib) — подсказки «вы имели в виду…».

Лист «Учет» пересобирает Apps Script (см. инструкцию — там же добавлен вычет «Списания»).
"""

import re
from collections import defaultdict
from datetime import date, datetime
from difflib import SequenceMatcher

import gspread

SH_UCHET = 'Учет'
SH_INIT = 'Начальные остатки'
SH_IN = 'Поступление'
SH_MOVE = 'Передвижение'
SH_WOFF = 'Списание'
SH_LOC = 'Локации'
SH_REF = 'Справочник'

STATUSES = ['На устье', 'В скважине', 'Дефектоскопия', 'Брак', 'Ремонт', 'Хранение']
PARTS_DEFAULT = ['НКТ', 'АГРП', 'Пакер', 'ЯГ']

# заголовки журналов (используются и для автосоздания листов)
HEADER_IN = ['Дата', 'Поставщик', 'Вид', 'Размер', 'Резьба', 'Локация', 'Статус', 'Количество', 'Примечание']
HEADER_MOVE = ['Дата', 'Локация А (откуда)', 'Статус А', 'Локация Б (куда)', 'Статус Б',
               'Вид', 'Размер', 'Резьба', 'Количество', 'Примечание']
HEADER_WOFF = ['Дата', 'Локация', 'Статус', 'Вид', 'Размер', 'Резьба', 'Количество', 'Причина', 'Примечание']

THREAD_ALIASES = {'ТМК': 'TMK', 'ВАМ': 'VAM', 'ВАМ ТОП': 'VAM TOP', 'ВАМТОП': 'VAM TOP',
                  'ВЫСАДКА': 'EUE', 'ГЛАДКИЙ': 'NUE', 'ГЛАДКАЯ': 'NUE', 'HCM1': 'HCM-1'}


def _norm(s):
    return re.sub(r'\s+', ' ', str(s or '').strip()).lower()


def _s(x):
    return str(x or '').strip()


class SheetEngine:
    def __init__(self, creds_dict: dict, sheet_id: str):
        self.gc = gspread.service_account_from_dict(creds_dict)
        self.ss = self.gc.open_by_key(sheet_id)
        self._ws = {}

    def ws(self, title):
        if title not in self._ws:
            self._ws[title] = self.ss.worksheet(title)
        return self._ws[title]

    def ws_ensure(self, title, header):
        """Вернуть лист, создав его с заголовком, если его нет."""
        try:
            return self.ws(title)
        except gspread.WorksheetNotFound:
            w = self.ss.add_worksheet(title=title, rows=1000, cols=max(10, len(header)))
            w.append_row(header, value_input_option='USER_ENTERED')
            self._ws[title] = w
            return w

    # ── справочники для кнопок ──
    def locations(self):
        rows = self.ws(SH_LOC).get_all_values()[1:]
        return [{'name': r[0], 'type': r[1] if len(r) > 1 else ''} for r in rows if r and r[0]]

    def location_names(self):
        return [l['name'] for l in self.locations()]

    def ref(self):
        out = defaultdict(lambda: {'sizes': [], 'threads': []})
        for r in self.ws(SH_REF).get_all_values()[1:]:
            if not r or not r[0]:
                continue
            v = r[0].strip()
            if len(r) > 1 and r[1].strip() and r[1].strip() not in out[v]['sizes']:
                out[v]['sizes'].append(r[1].strip())
            if len(r) > 2 and r[2].strip() and r[2].strip() not in out[v]['threads']:
                out[v]['threads'].append(r[2].strip())
        return dict(out)

    def parts(self):
        r = self.ref()
        names = [p for p in PARTS_DEFAULT if p in r] + [p for p in r if p not in PARTS_DEFAULT]
        return names or PARTS_DEFAULT

    def sizes_for(self, part):
        return self.ref().get(part, {}).get('sizes', [])

    def threads_for(self, part):
        if part != 'НКТ':
            return []
        return self.ref().get(part, {}).get('threads', ['TMK', 'VAM TOP', 'HCM-1', 'EUE', 'NUE'])

    # ── нормализация ──
    def normalize_size(self, part, size):
        s = str(size or '').strip()
        if not s:
            return s
        if part in ('НКТ', 'Пакер', 'ЯГ') and re.fullmatch(r'\d{2,3}', s):
            s = 'ø' + s
        return s.replace('O', 'ø').replace('o', 'ø').replace('Ø', 'ø')

    def normalize_thread(self, part, thread):
        if part and part != 'НКТ':
            return ''
        t = str(thread or '').strip().upper()
        return THREAD_ALIASES.get(t, t)

    # ── поиск / сопоставление локаций (гибрид: локально) ──
    def find_location(self, name):
        """Точное совпадение (с учётом нормализации/вхождения). Вернёт каноническое имя или None."""
        want = _norm(name)
        if not want:
            return None
        locs = self.location_names()
        for n in locs:
            if _norm(n) == want:
                return n
        best, best_len = None, 0
        for n in locs:
            ln = _norm(n)
            if (want in ln or ln in want) and len(ln) > best_len:
                best, best_len = n, len(ln)
        return best

    def suggest_locations(self, name, limit=3, cutoff=0.55):
        """Нечёткие кандидаты по существующим локациям (difflib). Список имён по убыванию похожести."""
        want = _norm(name)
        scored = []
        for n in self.location_names():
            ratio = SequenceMatcher(None, want, _norm(n)).ratio()
            # бонус за общие слова (частичные совпадения по токенам)
            wt, nt = set(want.split()), set(_norm(n).split())
            if wt & nt:
                ratio = max(ratio, 0.6 + 0.1 * len(wt & nt))
            scored.append((ratio, n))
        scored.sort(reverse=True)
        return [n for r, n in scored if r >= cutoff][:limit]

    # ── запись справочников ──
    def add_location(self, name, loc_type='Прочее'):
        lw = self.ws(SH_LOC)
        existing = {_norm(v) for v in lw.col_values(1)[1:] if v}
        if _norm(name) in existing:
            return False
        lw.append_row([name, loc_type, 'добавлено ботом'],
                      value_input_option='USER_ENTERED', table_range='A1')
        return True

    def add_ref(self, part, size, thread=''):
        size = self.normalize_size(part, size)
        thread = self.normalize_thread(part, thread)
        rw = self.ws(SH_REF)
        existing = {(_norm(r[0]), _norm(r[1] if len(r) > 1 else ''), _norm(r[2] if len(r) > 2 else ''))
                    for r in rw.get_all_values()[1:] if r and r[0]}
        if (_norm(part), _norm(size), _norm(thread)) in existing:
            return False
        rw.append_row([part, size, thread], value_input_option='USER_ENTERED', table_range='A1')
        return True

    # ── запись журналов ──
    def record_incoming(self, d, supplier, part, size, thread, location, status, qty, note=''):
        size = self.normalize_size(part, size)
        thread = self.normalize_thread(part, thread)
        self.ws_ensure(SH_IN, HEADER_IN).append_row(
            [self._d(d), supplier, part, size, thread, location, status, qty, note or ''],
            value_input_option='USER_ENTERED', table_range='A1')

    def record_movement(self, d, loc_a, status_a, loc_b, status_b, part, size, thread, qty, note=''):
        size = self.normalize_size(part, size)
        thread = self.normalize_thread(part, thread)
        self.ws_ensure(SH_MOVE, HEADER_MOVE).append_row(
            [self._d(d), loc_a, status_a, loc_b, status_b, part, size, thread, qty, note or ''],
            value_input_option='USER_ENTERED', table_range='A1')

    def record_writeoff(self, d, location, status, part, size, thread, qty, reason='', note=''):
        size = self.normalize_size(part, size)
        thread = self.normalize_thread(part, thread)
        self.ws_ensure(SH_WOFF, HEADER_WOFF).append_row(
            [self._d(d), location, status, part, size, thread, qty, reason or '', note or ''],
            value_input_option='USER_ENTERED', table_range='A1')

    # ── остатки ──
    def _read_init_matrix(self):
        vals = self.ws(SH_INIT).get_all_values()
        if len(vals) < 4:
            return []
        vid, size, thr = vals[0], vals[1], vals[2]
        cols = []
        last_vid = ''
        for c in range(2, len(size)):
            vv = (vid[c] if c < len(vid) else '').strip()
            if vv:
                last_vid = vv
            sz = (size[c] if c < len(size) else '').strip()
            if not sz:
                continue
            cols.append((c, last_vid, sz, (thr[c] if c < len(thr) else '').strip()))
        out, cur = [], ''
        for r in vals[3:]:
            st = (r[0] if len(r) > 0 else '').strip()
            if st:
                cur = st
            loc = (r[1] if len(r) > 1 else '').strip()
            if not loc or loc.startswith('Итого') or loc.startswith('ИТОГО'):
                continue
            for c, v, sz, t in cols:
                q = self._num(r[c]) if c < len(r) else 0
                if q:
                    out.append((cur, loc, v, sz, t, q))
        return out

    def inventory(self, location=None, status=None, part=None):
        inv = defaultdict(int)

        def key(st, loc, p, s, t):
            return (_s(st), _s(loc), _s(p), _s(s), _s(t))

        for st, loc, v, s, t, q in self._read_init_matrix():
            inv[key(st, loc, v, s, t)] += q
        # поступления (+)
        for r in self.ws(SH_IN).get_all_values()[1:]:
            if len(r) >= 8 and r[2]:
                inv[key(r[6], r[5], r[2], r[3], r[4])] += self._num(r[7])
        # передвижения (−А / +Б)
        for r in self.ws(SH_MOVE).get_all_values()[1:]:
            if len(r) >= 9 and r[5]:
                q = self._num(r[8])
                inv[key(r[2], r[1], r[5], r[6], r[7])] -= q
                inv[key(r[4], r[3], r[5], r[6], r[7])] += q
        # списание (−)
        try:
            for r in self.ws(SH_WOFF).get_all_values()[1:]:
                if len(r) >= 7 and r[3]:
                    inv[key(r[2], r[1], r[3], r[4], r[5])] -= self._num(r[6])
        except gspread.WorksheetNotFound:
            pass

        loc_want = _norm(location) if location else None
        out = []
        for (st, loc, p, s, t), q in inv.items():
            if q <= 0:
                continue
            if status and _norm(st) != _norm(status):
                continue
            if part and _norm(p) != _norm(part):
                continue
            if loc_want and loc_want not in _norm(loc):
                continue
            out.append({'status': st, 'location': loc, 'part': p, 'size': s,
                        'thread': t or None, 'qty': q})
        return sorted(out, key=lambda x: (x['status'], x['location'], x['part'], x['size']))

    # ── utils ──
    @staticmethod
    def _d(d):
        if isinstance(d, (date, datetime)):
            return d.strftime('%Y-%m-%d')
        return str(d)

    @staticmethod
    def _num(v):
        try:
            return float(str(v).replace(',', '.')) if v not in ('', None) else 0
        except ValueError:
            return 0
