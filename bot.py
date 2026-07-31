"""
bot.py — Telegram-бот учёта оборудования (меню-версия, пошаговые сценарии).

Принципы:
• Всё вводится через кнопки/пошаговые вопросы — БЕЗ распознавания фраз Клодом.
• Меню всегда доступно: нативная кнопка-меню Telegram (/start, /menu, /cancel),
  плюс любое сообщение открывает меню — писать «старт» руками не нужно.
• Виды/размеры/резьбы/статусы/локации — кнопками из «Справочника» и «Локаций».
• Передвижение/списание разрешены ТОЛЬКО с существующей локации (создать нельзя),
  и есть предупреждение, если на источнике не хватает остатка.
• Локация-получатель (куда) сверяется с существующими; при опечатке Claude подсказывает
  «вы имели в виду…». Новую заводим только по явному согласию.

Переменные окружения: TELEGRAM_TOKEN, GOOGLE_SHEET_ID, GOOGLE_CREDENTIALS,
ANTHROPIC_API_KEY (необязателен — без него работает всё, кроме умных подсказок имён).
"""

import json
import logging
import os
from datetime import date

from telegram import (BotCommand, InlineKeyboardButton, InlineKeyboardMarkup,
                      MenuButtonCommands, Update)
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, ConversationHandler, MessageHandler, filters)

from google_sheets import SheetEngine, STATUSES

logging.basicConfig(format='%(asctime)s │ %(levelname)s │ %(message)s', level=logging.INFO)
log = logging.getLogger(__name__)

ENG = SheetEngine(json.loads(os.environ['GOOGLE_CREDENTIALS']), os.environ['GOOGLE_SHEET_ID'])

AI = None
if os.environ.get('ANTHROPIC_API_KEY'):
    import anthropic
    AI = anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'])
    AI_MODEL = 'claude-haiku-4-5'

LOC_TYPES = ['База / склад', 'Скважина', 'Ремонт', 'Прочее']

# ── состояния ──
(MENU,
 MV_FROMSTAT, MV_TOSTAT, MV_PART, MV_SIZE, MV_THREAD, MV_QTY, MV_CONF,
 IN_SUP, IN_PART, IN_SIZE, IN_THREAD, IN_STAT, IN_QTY, IN_CONF,
 WO_PART, WO_SIZE, WO_THREAD, WO_STAT, WO_QTY, WO_REASON, WO_CONF,
 AL_NAME, AL_TYPE, AL_CONF,
 AR_PART, AR_SIZE, AR_THREAD, AR_CONF,
 INV_PART, INV_LOC,
 LOC_CHOOSE, LOC_TYPEIN, LOC_PICK) = range(34)

# ═══════════════ утилиты интерфейса ═══════════════

def kb(options, extra=None, cols=2, cancel=True, context=None):
    if context is not None:
        context.user_data['opts'] = list(options)
    rows, row = [], []
    for i, o in enumerate(options):
        row.append(InlineKeyboardButton(str(o), callback_data=f'opt:{i}'))
        if len(row) == cols:
            rows.append(row); row = []
    if row:
        rows.append(row)
    for label, data in (extra or []):
        rows.append([InlineKeyboardButton(label, callback_data=data)])
    if cancel:
        rows.append([InlineKeyboardButton('❌ Отмена', callback_data='cancel')])
    return InlineKeyboardMarkup(rows)


async def show(update, text, markup=None):
    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text(text, reply_markup=markup)
        except Exception:
            await update.callback_query.message.reply_text(text, reply_markup=markup)
    else:
        await update.message.reply_text(text, reply_markup=markup)


def picked(update, context):
    i = int(update.callback_query.data.split(':', 1)[1])
    return context.user_data.get('opts', [])[i]


def menu_kb():
    b = [[InlineKeyboardButton('🔄 Передвижение', callback_data='act:move')],
         [InlineKeyboardButton('📥 Поступление', callback_data='act:in')],
         [InlineKeyboardButton('🗑 Списание', callback_data='act:woff')],
         [InlineKeyboardButton('📍 Добавить локацию', callback_data='act:loc')],
         [InlineKeyboardButton('🧩 Добавить вид оборудования', callback_data='act:ref')],
         [InlineKeyboardButton('📦 Остатки', callback_data='act:inv')]]
    return InlineKeyboardMarkup(b)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await show(update, "👷 Учёт оборудования. Выберите действие:", menu_kb())
    return MENU


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await show(update, "Отменено. Выберите действие:", menu_kb())
    return MENU


async def menu_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    act = update.callback_query.data.split(':', 1)[1]
    context.user_data['draft'] = {}
    if act == 'move':
        return await ask_location(update, context, after='mv_a',
                                  title="🔄 Передвижение.\nОткуда (локация А)?", allow_new=False)
    if act == 'in':
        await show(update, "📥 Поступление.\nВведите поставщика:")
        return IN_SUP
    if act == 'woff':
        return await wo_ask_part(update, context)
    if act == 'loc':
        await show(update, "📍 Введите название новой локации:")
        return AL_NAME
    if act == 'ref':
        return await ar_ask_part(update, context)
    if act == 'inv':
        return await inv_ask_part(update, context)


# ═══════════════ ВЫБОР ЛОКАЦИИ (общий) ═══════════════
# allow_new=True  → можно создать новую (получатель, поступление)
# allow_new=False → только существующие (источник передвижения/списания)

async def ask_location(update, context, after, title, allow_new=True):
    context.user_data['loc_after'] = after
    context.user_data['loc_allow_new'] = allow_new
    context.user_data['loc_title'] = title
    names = ENG.location_names()
    if allow_new:
        hint = "\nВыберите из списка или «Другая»:"
        extra = [('✏️ Другая (ввести)', 'loc_manual')]
    else:
        hint = "\nВыберите существующую (или найдите по названию):"
        extra = [('🔎 Найти по названию', 'loc_manual')]
    await show(update, title + hint, kb(names, extra=extra, context=context))
    return LOC_CHOOSE


async def loc_choose(update, context):
    if update.callback_query.data == 'loc_manual':
        await show(update, "Введите название локации:")
        return LOC_TYPEIN
    return await _loc_done(update, context, picked(update, context))


async def loc_typein(update, context):
    typed = update.message.text.strip()
    context.user_data['loc_typed'] = typed
    allow_new = context.user_data.get('loc_allow_new', True)

    exact = ENG.find_location(typed)
    if exact and exact.lower().replace(' ', '') == typed.lower().replace(' ', ''):
        return await _loc_done(update, context, exact)

    cands = ENG.suggest_locations(typed)
    if exact and exact not in cands:
        cands.insert(0, exact)
    if len(cands) < 2 and AI:
        for e in claude_suggest_location(typed, ENG.location_names()):
            if e not in cands:
                cands.append(e)

    extra = []
    if allow_new:
        extra.append((f'➕ Создать: «{typed}»', 'loc_create'))
    extra.append(('✏️ Ввести заново', 'loc_manual'))
    if not allow_new:
        extra.append(('📋 К списку', 'loc_back'))

    if cands:
        await show(update, f"Точного совпадения для «{typed}» нет. Вы имели в виду:",
                   kb(cands, extra=extra, cols=1, context=context))
    elif allow_new:
        await show(update, f"Локации «{typed}» нет. Создать новую?",
                   kb([], extra=extra, context=context))
    else:
        await show(update, f"❌ Локации «{typed}» нет в списке. Выберите существующую "
                           f"или введите заново.", kb([], extra=extra, context=context))
    return LOC_PICK


async def loc_pick(update, context):
    data = update.callback_query.data
    if data == 'loc_manual':
        await show(update, "Введите название локации:")
        return LOC_TYPEIN
    if data == 'loc_back':
        return await ask_location(update, context, context.user_data['loc_after'],
                                  context.user_data['loc_title'],
                                  context.user_data.get('loc_allow_new', True))
    if data == 'loc_create':
        if not context.user_data.get('loc_allow_new', True):
            await update.callback_query.answer("Здесь можно выбрать только существующую локацию", show_alert=True)
            return LOC_PICK
        name = context.user_data['loc_typed']
        ENG.add_location(name)
        return await _loc_done(update, context, name)
    return await _loc_done(update, context, picked(update, context))


async def _loc_done(update, context, name):
    after = context.user_data['loc_after']
    d = context.user_data['draft']
    if after == 'mv_a':
        d['loc_a'] = name
        return await mv_ask_fromstat(update, context)
    if after == 'mv_b':
        d['loc_b'] = name
        return await mv_ask_tostat(update, context)
    if after == 'in':
        d['location'] = name
        return await in_ask_stat(update, context)
    if after == 'wo':
        d['location'] = name
        return await wo_ask_stat(update, context)


def claude_suggest_location(typed, names):
    if not AI:
        return []
    try:
        prompt = ("Пользователь ввёл название локации: «{}».\nСписок существующих локаций:\n{}\n\n"
                  "Верни СТРОГО JSON-массив из 0-2 названий ТОЧНО из списка, которые он вероятнее всего "
                  "имел в виду (учитывай опечатки, сокращения, транслит). Если совпадений нет — []."
                  ).format(typed, "\n".join(f"- {n}" for n in names))
        r = AI.messages.create(model=AI_MODEL, max_tokens=200,
                               messages=[{"role": "user", "content": prompt}])
        txt = ''.join(b.text for b in r.content if hasattr(b, 'text')).strip()
        txt = txt[txt.find('['): txt.rfind(']') + 1]
        return [n for n in json.loads(txt) if n in names][:2]
    except Exception:
        log.exception("claude_suggest_location")
        return []


# ═══════════════ выбор вида/размера/резьбы ═══════════════

async def ask_part(update, context, prompt):
    await show(update, prompt, kb(ENG.parts(), context=context))


async def ask_size(update, context, part):
    await show(update, f"Размер ({part})?",
               kb(ENG.sizes_for(part), extra=[('✏️ Другой (ввести)', 'size_manual')], context=context))


async def ask_thread(update, context, part):
    await show(update, "Резьба (НКТ)?",
               kb(ENG.threads_for(part), extra=[('✏️ Другая (ввести)', 'thread_manual')], context=context))


def valid_qty(text):
    try:
        q = int(str(text).strip())
        return q if q > 0 else None
    except ValueError:
        return None


def _th(d):
    return f" {d['thread']}" if d.get('thread') else ''


# ═══════════════ ПЕРЕДВИЖЕНИЕ ═══════════════

async def mv_ask_fromstat(update, context):
    await show(update, "Статус на локации А (откуда)?", kb(STATUSES, context=context))
    return MV_FROMSTAT


async def mv_fromstat(update, context):
    context.user_data['draft']['status_a'] = picked(update, context)
    return await ask_location(update, context, after='mv_b', title="Куда (локация Б)?", allow_new=True)


async def mv_ask_tostat(update, context):
    await show(update, "Статус на локации Б (куда)?", kb(STATUSES, context=context))
    return MV_TOSTAT


async def mv_tostat(update, context):
    context.user_data['draft']['status_b'] = picked(update, context)
    await ask_part(update, context, "Вид оборудования?")
    return MV_PART


async def mv_part(update, context):
    p = picked(update, context)
    context.user_data['draft']['part'] = p
    await ask_size(update, context, p)
    return MV_SIZE


async def mv_size(update, context):
    d = context.user_data['draft']
    if update.callback_query:
        if update.callback_query.data == 'size_manual':
            await show(update, "Введите размер:")
            return MV_SIZE
        d['size'] = picked(update, context)
    else:
        d['size'] = update.message.text.strip()
    if d['part'] == 'НКТ':
        await ask_thread(update, context, d['part'])
        return MV_THREAD
    d['thread'] = ''
    await show(update, "Количество (шт)?")
    return MV_QTY


async def mv_thread(update, context):
    d = context.user_data['draft']
    if update.callback_query:
        if update.callback_query.data == 'thread_manual':
            await show(update, "Введите резьбу:")
            return MV_THREAD
        d['thread'] = picked(update, context)
    else:
        d['thread'] = update.message.text.strip()
    await show(update, "Количество (шт)?")
    return MV_QTY


async def mv_qty(update, context):
    q = valid_qty(update.message.text)
    if not q:
        await show(update, "Введите положительное число:")
        return MV_QTY
    d = context.user_data['draft']; d['qty'] = q
    avail = ENG.available(d['loc_a'], d['status_a'], d['part'], d['size'], d.get('thread') or '')
    warn = ''
    if q > avail:
        warn = (f"⚠️ Внимание: на «{d['loc_a']}» ({d['status_a']}) числится {avail} шт "
                f"{d['part']} {d['size']}{_th(d)}, а вы переносите {q}.\n\n")
    txt = (warn + "Проверьте передвижение:\n"
           f"• {d['part']} {d['size']}{_th(d)} — {q} шт\n"
           f"• {d['loc_a']} ({d['status_a']}) → {d['loc_b']} ({d['status_b']})")
    await show(update, txt, kb([], extra=[('✅ Записать', 'ok')], context=context))
    return MV_CONF


async def mv_confirm(update, context):
    d = context.user_data['draft']
    ENG.record_movement(str(date.today()), d['loc_a'], d['status_a'], d['loc_b'], d['status_b'],
                        d['part'], d['size'], d.get('thread'), d['qty'])
    aft = ENG.inventory(d['loc_b'], d['status_b'], d['part'])
    await show(update, "✅ Записано в «Передвижение».\n\n" + fmt_inv(aft), menu_kb())
    context.user_data.clear()
    return MENU


# ═══════════════ ПОСТУПЛЕНИЕ ═══════════════

async def in_supplier(update, context):
    context.user_data['draft']['supplier'] = update.message.text.strip()
    await ask_part(update, context, "Вид оборудования?")
    return IN_PART


async def in_part(update, context):
    p = picked(update, context); context.user_data['draft']['part'] = p
    await ask_size(update, context, p)
    return IN_SIZE


async def in_size(update, context):
    d = context.user_data['draft']
    if update.callback_query:
        if update.callback_query.data == 'size_manual':
            await show(update, "Введите размер:")
            return IN_SIZE
        d['size'] = picked(update, context)
    else:
        d['size'] = update.message.text.strip()
    if d['part'] == 'НКТ':
        await ask_thread(update, context, d['part'])
        return IN_THREAD
    d['thread'] = ''
    return await ask_location(update, context, after='in', title="Локация?", allow_new=True)


async def in_thread(update, context):
    d = context.user_data['draft']
    if update.callback_query:
        if update.callback_query.data == 'thread_manual':
            await show(update, "Введите резьбу:")
            return IN_THREAD
        d['thread'] = picked(update, context)
    else:
        d['thread'] = update.message.text.strip()
    return await ask_location(update, context, after='in', title="Локация?", allow_new=True)


async def in_ask_stat(update, context):
    await show(update, "Статус?", kb(STATUSES, context=context))
    return IN_STAT


async def in_stat(update, context):
    context.user_data['draft']['status'] = picked(update, context)
    await show(update, "Количество (шт)?")
    return IN_QTY


async def in_qty(update, context):
    q = valid_qty(update.message.text)
    if not q:
        await show(update, "Введите положительное число:")
        return IN_QTY
    d = context.user_data['draft']; d['qty'] = q
    txt = ("Проверьте поступление:\n"
           f"• {d['part']} {d['size']}{_th(d)} — {q} шт\n"
           f"• Поставщик: {d['supplier']}\n"
           f"• {d['location']} ({d['status']})")
    await show(update, txt, kb([], extra=[('✅ Записать', 'ok')], context=context))
    return IN_CONF


async def in_confirm(update, context):
    d = context.user_data['draft']
    ENG.record_incoming(str(date.today()), d['supplier'], d['part'], d['size'], d.get('thread'),
                        d['location'], d['status'], d['qty'])
    aft = ENG.inventory(d['location'], d['status'], d['part'])
    await show(update, "✅ Записано в «Поступление».\n\n" + fmt_inv(aft), menu_kb())
    context.user_data.clear()
    return MENU


# ═══════════════ СПИСАНИЕ ═══════════════

async def wo_ask_part(update, context):
    await ask_part(update, context, "🗑 Списание.\nВид оборудования?")
    return WO_PART


async def wo_part(update, context):
    p = picked(update, context); context.user_data['draft']['part'] = p
    await ask_size(update, context, p)
    return WO_SIZE


async def wo_size(update, context):
    d = context.user_data['draft']
    if update.callback_query:
        if update.callback_query.data == 'size_manual':
            await show(update, "Введите размер:")
            return WO_SIZE
        d['size'] = picked(update, context)
    else:
        d['size'] = update.message.text.strip()
    if d['part'] == 'НКТ':
        await ask_thread(update, context, d['part'])
        return WO_THREAD
    d['thread'] = ''
    return await ask_location(update, context, after='wo',
                              title="С какой локации списываем?", allow_new=False)


async def wo_thread(update, context):
    d = context.user_data['draft']
    if update.callback_query:
        if update.callback_query.data == 'thread_manual':
            await show(update, "Введите резьбу:")
            return WO_THREAD
        d['thread'] = picked(update, context)
    else:
        d['thread'] = update.message.text.strip()
    return await ask_location(update, context, after='wo',
                              title="С какой локации списываем?", allow_new=False)


async def wo_ask_stat(update, context):
    await show(update, "Статус (откуда списываем)?", kb(STATUSES, context=context))
    return WO_STAT


async def wo_stat(update, context):
    context.user_data['draft']['status'] = picked(update, context)
    await show(update, "Количество (шт)?")
    return WO_QTY


async def wo_qty(update, context):
    q = valid_qty(update.message.text)
    if not q:
        await show(update, "Введите положительное число:")
        return WO_QTY
    context.user_data['draft']['qty'] = q
    await show(update, "Причина списания (текст) или «—»:")
    return WO_REASON


async def wo_reason(update, context):
    d = context.user_data['draft']
    d['reason'] = update.message.text.strip()
    avail = ENG.available(d['location'], d['status'], d['part'], d['size'], d.get('thread') or '')
    warn = ''
    if d['qty'] > avail:
        warn = (f"⚠️ Внимание: на «{d['location']}» ({d['status']}) числится {avail} шт "
                f"{d['part']} {d['size']}{_th(d)}, а вы списываете {d['qty']}.\n\n")
    txt = (warn + "Проверьте списание:\n"
           f"• {d['part']} {d['size']}{_th(d)} — {d['qty']} шт\n"
           f"• {d['location']} ({d['status']})\n"
           f"• Причина: {d['reason']}")
    await show(update, txt, kb([], extra=[('✅ Записать', 'ok')], context=context))
    return WO_CONF


async def wo_confirm(update, context):
    d = context.user_data['draft']
    ENG.record_writeoff(str(date.today()), d['location'], d['status'], d['part'], d['size'],
                        d.get('thread'), d['qty'], d.get('reason', ''))
    aft = ENG.inventory(d['location'], d['status'], d['part'])
    await show(update, "✅ Записано в «Списание».\n\n" + fmt_inv(aft), menu_kb())
    context.user_data.clear()
    return MENU


# ═══════════════ ДОБАВИТЬ ЛОКАЦИЮ ═══════════════

async def al_name(update, context):
    context.user_data['draft']['name'] = update.message.text.strip()
    await show(update, "Тип локации?", kb(LOC_TYPES, context=context))
    return AL_TYPE


async def al_type(update, context):
    context.user_data['draft']['type'] = picked(update, context)
    d = context.user_data['draft']
    await show(update, f"Добавить локацию:\n• {d['name']} — {d['type']}?",
               kb([], extra=[('✅ Добавить', 'ok')], context=context))
    return AL_CONF


async def al_confirm(update, context):
    d = context.user_data['draft']
    added = ENG.add_location(d['name'], d['type'])
    await show(update, "✅ Локация добавлена." if added else "ℹ️ Такая локация уже есть.", menu_kb())
    context.user_data.clear()
    return MENU


# ═══════════════ ДОБАВИТЬ ВИД ОБОРУДОВАНИЯ ═══════════════

async def ar_ask_part(update, context):
    await show(update, "🧩 Добавить вид оборудования.\nВид?", kb(ENG.parts(), context=context))
    return AR_PART


async def ar_part(update, context):
    context.user_data['draft']['part'] = picked(update, context)
    await show(update, "Введите размер (напр. ø89, 65х70, ø152 ПРО-ЯМО):")
    return AR_SIZE


async def ar_size(update, context):
    d = context.user_data['draft']; d['size'] = update.message.text.strip()
    if d['part'] == 'НКТ':
        await show(update, "Введите резьбу (TMK / VAM TOP / HCM-1 / EUE / NUE):")
        return AR_THREAD
    d['thread'] = ''
    return await ar_confirm_screen(update, context)


async def ar_thread(update, context):
    context.user_data['draft']['thread'] = update.message.text.strip()
    return await ar_confirm_screen(update, context)


async def ar_confirm_screen(update, context):
    d = context.user_data['draft']
    th = f" / {d['thread']}" if d.get('thread') else ''
    await show(update, f"Добавить в справочник:\n• {d['part']} / {d['size']}{th}?",
               kb([], extra=[('✅ Добавить', 'ok')], context=context))
    return AR_CONF


async def ar_confirm(update, context):
    d = context.user_data['draft']
    added = ENG.add_ref(d['part'], d['size'], d.get('thread', ''))
    await show(update, "✅ Добавлено в справочник." if added else "ℹ️ Такая позиция уже есть.", menu_kb())
    context.user_data.clear()
    return MENU


# ═══════════════ ОСТАТКИ ═══════════════

async def inv_ask_part(update, context):
    await show(update, "📦 Остатки.\nПо какому виду?",
               kb(ENG.parts(), extra=[('Все виды', 'all')], context=context))
    return INV_PART


async def inv_part(update, context):
    data = update.callback_query.data
    context.user_data['draft']['part'] = None if data == 'all' else picked(update, context)
    await show(update, "По какой локации?",
               kb(ENG.location_names(), extra=[('Все локации', 'all')], context=context))
    return INV_LOC


async def inv_loc(update, context):
    data = update.callback_query.data
    loc = None if data == 'all' else picked(update, context)
    items = ENG.inventory(loc, None, context.user_data['draft'].get('part'))
    await show(update, fmt_inv(items), menu_kb())
    context.user_data.clear()
    return MENU


def fmt_inv(items):
    if not items:
        return "📦 Пусто — ничего не найдено."
    total = sum(i['qty'] for i in items)
    lines = []
    for i in items[:40]:
        th = f" {i['thread']}" if i['thread'] else ''
        lines.append(f"• {i['location']} | {i['status']} | {i['part']} {i['size']}{th} — {i['qty']}")
    more = '' if len(items) <= 40 else f"\n… ещё {len(items) - 40} строк"
    return "📦 Остатки (всего {}):\n{}{}".format(total, '\n'.join(lines), more)


# ═══════════════ сборка ═══════════════

def cq(func, pattern):
    return CallbackQueryHandler(func, pattern=pattern)


def txt(func):
    return MessageHandler(filters.TEXT & ~filters.COMMAND, func)


async def post_init(app):
    await app.bot.set_my_commands([BotCommand('start', 'Меню'),
                                   BotCommand('menu', 'Меню'),
                                   BotCommand('cancel', 'Отмена')])
    try:
        await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except Exception:
        log.exception("set_chat_menu_button")


def main():
    app = Application.builder().token(os.environ['TELEGRAM_TOKEN']).post_init(post_init).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler('start', start), CommandHandler('menu', start),
                      CommandHandler('help', start),
                      MessageHandler(filters.TEXT & ~filters.COMMAND, start)],  # любое сообщение → меню
        states={
            MENU: [cq(menu_pick, r'^act:')],
            LOC_CHOOSE: [cq(loc_choose, r'^opt:'), cq(loc_choose, r'^loc_manual$')],
            LOC_TYPEIN: [txt(loc_typein)],
            LOC_PICK: [cq(loc_pick, r'^opt:'), cq(loc_pick, r'^loc_(create|manual|back)$')],
            # передвижение
            MV_FROMSTAT: [cq(mv_fromstat, r'^opt:')],
            MV_TOSTAT: [cq(mv_tostat, r'^opt:')],
            MV_PART: [cq(mv_part, r'^opt:')],
            MV_SIZE: [cq(mv_size, r'^opt:'), cq(mv_size, r'^size_manual$'), txt(mv_size)],
            MV_THREAD: [cq(mv_thread, r'^opt:'), cq(mv_thread, r'^thread_manual$'), txt(mv_thread)],
            MV_QTY: [txt(mv_qty)],
            MV_CONF: [cq(mv_confirm, r'^ok$')],
            # поступление
            IN_SUP: [txt(in_supplier)],
            IN_PART: [cq(in_part, r'^opt:')],
            IN_SIZE: [cq(in_size, r'^opt:'), cq(in_size, r'^size_manual$'), txt(in_size)],
            IN_THREAD: [cq(in_thread, r'^opt:'), cq(in_thread, r'^thread_manual$'), txt(in_thread)],
            IN_STAT: [cq(in_stat, r'^opt:')],
            IN_QTY: [txt(in_qty)],
            IN_CONF: [cq(in_confirm, r'^ok$')],
            # списание
            WO_PART: [cq(wo_part, r'^opt:')],
            WO_SIZE: [cq(wo_size, r'^opt:'), cq(wo_size, r'^size_manual$'), txt(wo_size)],
            WO_THREAD: [cq(wo_thread, r'^opt:'), cq(wo_thread, r'^thread_manual$'), txt(wo_thread)],
            WO_STAT: [cq(wo_stat, r'^opt:')],
            WO_QTY: [txt(wo_qty)],
            WO_REASON: [txt(wo_reason)],
            WO_CONF: [cq(wo_confirm, r'^ok$')],
            # локация
            AL_NAME: [txt(al_name)],
            AL_TYPE: [cq(al_type, r'^opt:')],
            AL_CONF: [cq(al_confirm, r'^ok$')],
            # справочник
            AR_PART: [cq(ar_part, r'^opt:')],
            AR_SIZE: [txt(ar_size)],
            AR_THREAD: [txt(ar_thread)],
            AR_CONF: [cq(ar_confirm, r'^ok$')],
            # остатки
            INV_PART: [cq(inv_part, r'^(opt:|all$)')],
            INV_LOC: [cq(inv_loc, r'^(opt:|all$)')],
        },
        fallbacks=[CommandHandler('cancel', cancel),
                   CallbackQueryHandler(cancel, pattern=r'^cancel$'),
                   CommandHandler('start', start), CommandHandler('menu', start)],
        allow_reentry=True,
    )
    app.add_handler(conv)
    log.info("Bot started (menu mode, long-polling)")
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
