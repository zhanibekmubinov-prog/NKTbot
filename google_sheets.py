"""
google_sheets.py — движок работы с Google Sheets через gspread.

Принцип: бот НИКОГДА не пишет формулы строкой (из-за локали Google ',' vs ';'
формулы-строки ломаются). Для новой локации он вставляет строку и КОПИРУЕТ
готовые формулы из соседней строки того же статуса — Google сам подстраивает
ссылки (формулы основаны на ROW(), поэтому всегда корректны).

Все остатки для ответов в Telegram считаются на Python из журналов,
поэтому не зависят от пересчёта Google.
"""
import json
import os
from collections import defaultdict
from datetime import date, datetime

import gspread

# ── раскладка листа «Учет (структура)» (как в Excel-сборке) ──────────────────
COL_FIRST = 3          # C — первая колонка данных
CR_PART   = 4          # строка 4 — скрытый критерий «Вид»
DATA0     = 7          # первая строка данных

SH_UCHET = 'Учет (структура)'
SH_LOC   = 'Локации'
SH_IN    = 'Поступление'
SH_MOVE  = 'Передвижение'
SH_BASE  = '_База'

STATUS_ORDER = ['На устье', 'В скважине', 'Дефектоскопия', 'Брак', 'Ремонт', 'Хранение']


class SheetEngine:
    def __init__(self, creds_dict: dict, sheet_id: str):
        self.gc = gspread.service_account_from_dict(creds_dict)
        self.ss = self.gc.open_by_key(sheet_id)
        self._ws = {}
        self._schema = None

    # ── helpers ───────────────────────────────────────────────────────────────
    def ws(self, title: str):
        if title not in self._ws:
            self._ws[title] = self.ss.worksheet(title)
        return self._ws[title]

    def schema(self):
        """Читает скрытую строку критериев (row 4) → число колонок данных."""
        if self._schema is None:
            row = self.ws(SH_UCHET).row_values(CR_PART)   # ['', '', НКТ, НКТ, ... ]
            parts = [v for v in row[COL_FIRST - 1:] if v]
            ncols = len(parts)
            self._schema = {
                'ncols':     ncols,
                'col_total': COL_FIRST + ncols,            # V
                'col_st':    COL_FIRST + ncols + 1,        # W (скрытый статус)
            }
        return self._schema

    # ── public API ──────────────────────────────────────────────────────────

    def add_location(self, name: str, loc_type: str = 'Прочее') -> bool:
        lw = self.ws(SH_LOC)
        existing = [v.strip() for v in lw.col_values(1)[1:] if v]
        if name.strip() in existing:
            return False
        lw.append_row([name, loc_type, ''], value_input_option='USER_ENTERED',
                      table_range='A1')
        return True

    def record_incoming(self, d, supplier, part, size, thread,
                        location, status, qty, note=''):
        self.ensure_uchet_row(status, location)
        self.ws(SH_IN).append_row(
            [self._d(d), supplier, part, size, thread or '',
             location, status, qty, note or ''],
            value_input_option='USER_ENTERED', table_range='A1')

    def record_movement(self, d, loc_a, status_a, loc_b, status_b,
                        part, size, thread, qty, note=''):
        self.ensure_uchet_row(status_a, loc_a)
        self.ensure_uchet_row(status_b, loc_b)
        self.ws(SH_MOVE).append_row(
            [self._d(d), loc_a, status_a, loc_b, status_b,
             part, size, thread or '', qty, note or ''],
            value_input_option='USER_ENTERED', table_range='A1')

    # ── row insertion (copy formulas, never write them) ──────────────────────
    def ensure_uchet_row(self, status: str, location: str) -> int:
        sc  = self.schema()
        ws  = self.ws(SH_UCHET)
        gid = ws.id
        col_b = ws.col_values(2)                # локации / метки итогов
        col_w = ws.col_values(sc['col_st'])     # статусы (только строки данных)

        loc = location.strip()

        # 1) уже есть?
        for i, st in enumerate(col_w):
            r = i + 1
            if r >= DATA0 and st == status and i < len(col_b) and col_b[i].strip() == loc:
                return r

        # 2) строка-итог этого статуса (вставляем перед ней)
        sub_row = None
        for i, b in enumerate(col_b):
            if (b or '').strip() == f'Итого: {status}':
                sub_row = i + 1
                break
        if sub_row is None:
            raise RuntimeError(f'Не найден блок статуса «{status}» в листе Учёт')

        # 3) образец данных этого же статуса (откуда копировать формулы)
        sample = None
        for i, st in enumerate(col_w):
            r = i + 1
            if r >= DATA0 and st == status:
                sample = r
                break
        if sample is None:
            sample = sub_row - 1

        # 4) вставить пустую строку перед итогом
        ws.insert_rows([[]], row=sub_row, inherit_from_before=True)
        new_row = sub_row
        if sample >= new_row:
            sample += 1   # образец сместился вниз

        # 5) скопировать формулы (колонки C..V) из образца в новую строку
        c0 = COL_FIRST - 1                       # 0-based start col (C)
        c1 = sc['col_total']                     # 0-based end-exclusive (после V)
        self.ss.batch_update({'requests': [{
            'copyPaste': {
                'source': {'sheetId': gid,
                           'startRowIndex': sample - 1, 'endRowIndex': sample,
                           'startColumnIndex': c0, 'endColumnIndex': c1},
                'destination': {'sheetId': gid,
                                'startRowIndex': new_row - 1, 'endRowIndex': new_row,
                                'startColumnIndex': c0, 'endColumnIndex': c1},
                'pasteType': 'PASTE_NORMAL', 'pasteOrientation': 'NORMAL',
            }
        }]})

        # 6) проставить локацию (B) и статус (W) как обычный текст
        ws.update_cell(new_row, 2, location)
        ws.update_cell(new_row, sc['col_st'], status)
        return new_row

    # ── inventory (Python, не зависит от пересчёта Google) ──────────────────
    def inventory(self, location=None, status=None, part=None):
        inv = defaultdict(int)

        def key(st, loc, p, s, t):
            return ((st or '').strip(), (loc or '').strip(),
                    (p or '').strip(), (s or '').strip(), (t or '').strip())

        for row in self.ws(SH_BASE).get_all_values()[1:]:
            if len(row) >= 6 and row[0]:
                inv[key(row[0], row[1], row[2], row[3], row[4])] += self._num(row[5])

        for row in self.ws(SH_IN).get_all_values()[1:]:
            if len(row) >= 8 and row[2]:
                inv[key(row[6], row[5], row[2], row[3], row[4])] += self._num(row[7])

        for row in self.ws(SH_MOVE).get_all_values()[1:]:
            if len(row) >= 9 and row[5]:
                q = self._num(row[8])
                inv[key(row[2], row[1], row[5], row[6], row[7])] -= q
                inv[key(row[4], row[3], row[5], row[6], row[7])] += q

        out = []
        for (st, loc, p, s, t), q in inv.items():
            if q <= 0:                       continue
            if status   and st  != status:   continue
            if location and loc != location.strip(): continue
            if part     and p   != part:     continue
            out.append({'status': st, 'location': loc, 'part': p,
                        'size': s, 'thread': t or None, 'qty': q})
        return sorted(out, key=lambda x: (x['status'], x['location'], x['part']))

    def locations(self):
        return [{'name': r[0], 'type': r[1] if len(r) > 1 else ''}
                for r in self.ws(SH_LOC).get_all_values()[1:] if r and r[0]]

    # ── utils ─────────────────────────────────────────────────────────────────
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
