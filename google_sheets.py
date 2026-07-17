"""
google_sheets.py — движок Google Sheets (версия под Apps Script).

Лист «Учет» полностью пересобирает Apps Script. Бот только:
  • пишет строки в журналы «Поступление» / «Передвижение»;
  • добавляет локации в «Локации»;
  • считает остатки на Python (из «Начальные остатки» + журналы) — для ответов в TG.

Нормализация локаций/номенклатуры/резьбы — здесь, чтобы не плодить дубли.
"""
import re
from collections import defaultdict
from datetime import date, datetime

import gspread

SH_UCHET = 'Учет'
SH_INIT  = 'Начальные остатки'
SH_IN    = 'Поступление'
SH_MOVE  = 'Передвижение'
SH_LOC   = 'Локации'
SH_REF   = 'Справочник'

STATUSES = ['На устье', 'В скважине', 'Дефектоскопия', 'Брак', 'Ремонт', 'Хранение']

# резьба есть только у НКТ
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

    # ── справочники ──
    def locations(self):
        rows = self.ws(SH_LOC).get_all_values()[1:]
        return [{'name': r[0], 'type': r[1] if len(r) > 1 else ''} for r in rows if r and r[0]]

    def ref(self):
        out = defaultdict(lambda: {'sizes': set(), 'threads': set()})
        for r in self.ws(SH_REF).get_all_values()[1:]:
            if not r or not r[0]:
                continue
            v = r[0].strip()
            if len(r) > 1 and r[1].strip():
                out[v]['sizes'].add(r[1].strip())
            if len(r) > 2 and r[2].strip():
                out[v]['threads'].add(r[2].strip())
        return {k: {'sizes': sorted(v['sizes']), 'threads': sorted(v['threads'])} for k, v in out.items()}

    # ── нормализация (защита от дублей) ──
    def resolve_location(self, name, add_if_new=True):
        want = _norm(name)
        locs = self.locations()
        for l in locs:
            if _norm(l['name']) == want:
                return l['name'], False
        best, best_len = None, 0
        for l in locs:
            ln = _norm(l['name'])
            if want and (want in ln or ln in want) and len(ln) > best_len:
                best, best_len = l['name'], len(ln)
        if best:
            return best, False
        if add_if_new and str(name).strip():
            self.add_location(str(name).strip())
            return str(name).strip(), True
        return str(name).strip(), True

    def normalize_size(self, part, size):
        s = str(size or '').strip()
        if not s:
            return s
        if part in ('НКТ', 'Пакер', 'ЯГ') and re.fullmatch(r'\d{2,3}', s):
            s = 'ø' + s
        return s.replace('O', 'ø').replace('o', 'ø').replace('Ø', 'ø')

    def normalize_thread(self, part, thread):
        """Резьба только у НКТ. У остальных видов — всегда пусто."""
        if part and part != 'НКТ':
            return ''
        t = str(thread or '').strip().upper()
        return THREAD_ALIASES.get(t, t)

    def add_location(self, name, loc_type='Прочее'):
        lw = self.ws(SH_LOC)
        existing = {_norm(v) for v in lw.col_values(1)[1:] if v}
        if _norm(name) in existing:
            return False
        lw.append_row([name, loc_type, 'добавлено ботом'],
                      value_input_option='USER_ENTERED', table_range='A1')
        return True

    # ── запись журналов ──
    def record_incoming(self, d, supplier, part, size, thread, location, status, qty, note=''):
        size = self.normalize_size(part, size)
        thread = self.normalize_thread(part, thread)
        location, _ = self.resolve_location(location)
        self.ws(SH_IN).append_row(
            [self._d(d), supplier, part, size, thread, location, status, qty, note or ''],
            value_input_option='USER_ENTERED', table_range='A1')

    def record_movement(self, d, loc_a, status_a, loc_b, status_b, part, size, thread, qty, note=''):
        size = self.normalize_size(part, size)
        thread = self.normalize_thread(part, thread)
        loc_a, _ = self.resolve_location(loc_a)
        loc_b, _ = self.resolve_location(loc_b)
        self.ws(SH_MOVE).append_row(
            [self._d(d), loc_a, status_a, loc_b, status_b, part, size, thread, qty, note or ''],
            value_input_option='USER_ENTERED', table_range='A1')

    # ── остатки (Python, из тех же источников, что и Apps Script) ──
    def _read_init_matrix(self):
        vals = self.ws(SH_INIT).get_all_values()
        if len(vals) < 4:
            return []
        vid, size, thr = vals[0], vals[1], vals[2]
        cols = []
        for c in range(2, len(size)):
            sz = (size[c] if c < len(size) else '').strip()
            if not sz:
                continue
            cols.append((c, (vid[c] if c < len(vid) else '').strip(), sz,
                         (thr[c] if c < len(thr) else '').strip()))
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
        for r in self.ws(SH_IN).get_all_values()[1:]:
            if len(r) >= 8 and r[2]:
                inv[key(r[6], r[5], r[2], r[3], r[4])] += self._num(r[7])
        for r in self.ws(SH_MOVE).get_all_values()[1:]:
            if len(r) >= 9 and r[5]:
                q = self._num(r[8])
                inv[key(r[2], r[1], r[5], r[6], r[7])] -= q
                inv[key(r[4], r[3], r[5], r[6], r[7])] += q

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
