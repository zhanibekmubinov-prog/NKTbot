"""
bot.py — Telegram-бот учёта оборудования (гибрид: команды + Claude Haiku).

Экономия токенов:
  • Частые фразы (поступление / передвижение / остатки) разбираются в коде
    регулярками — БЕЗ обращения к Claude.
  • Нормализация локаций/размеров/резьбы — тоже в коде (см. google_sheets.py),
    поэтому свод не плодит дубли.
  • Только если фразу не удалось разобрать — вызывается Claude Haiku
    (дёшево) с коротким контекстом.

Переменные окружения (Railway → Variables):
  TELEGRAM_TOKEN, ANTHROPIC_API_KEY, GOOGLE_SHEET_ID, GOOGLE_CREDENTIALS
  (ANTHROPIC_API_KEY можно не задавать — тогда работает только режим команд.)
"""
import asyncio
import json
import logging
import os
import re
from collections import defaultdict
from datetime import date

from telegram import Update
from telegram.ext import (Application, CommandHandler, ContextTypes,
                          MessageHandler, filters)

from google_sheets import SheetEngine

logging.basicConfig(format='%(asctime)s │ %(levelname)s │ %(message)s', level=logging.INFO)
log = logging.getLogger(__name__)

CREDS = json.loads(os.environ['GOOGLE_CREDENTIALS'])
SHEET = os.environ['GOOGLE_SHEET_ID']
ENG   = SheetEngine(CREDS, SHEET)

# Claude — только как запасной парсер. Haiku = дёшево.
MODEL       = 'claude-haiku-4-5'
MAX_HISTORY = 6
LOCK        = asyncio.Lock()
histories   = defaultdict(list)

AI = None
if os.environ.get('ANTHROPIC_API_KEY'):
    import anthropic
    AI = anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'])

PARTS = {'нкт': 'НКТ', 'агрп': 'АГРП', 'пакер': 'Пакер', 'пакеры': 'Пакер',
         'яг': 'ЯГ', 'ясс': 'ЯГ'}
STATUS_WORDS = {
    'на устье': 'На устье', 'устье': 'На устье',
    'в скважине': 'В скважине', 'скважина': 'В скважине', 'скважине': 'В скважине',
    'дефектоскоп': 'Дефектоскопия', 'брак': 'Брак',
    'ремонт': 'Ремонт', 'хранени': 'Хранение', 'склад': 'Хранение'}
THREADS = ['VAM TOP', 'HCM-1', 'TMK', 'VAM', 'EUE', 'NUE',
           'ТМК', 'ВАМ ТОП', 'ВАМ', 'ВЫСАДКА', 'ГЛАДКИЙ']


# ═══════════════ детерминированный парсер (без Claude) ═══════════════
def _find_part(t):
    for k, v in PARTS.items():
        if re.search(r'\b' + k + r'\b', t):
            return v
    return None


KNOWN_DIA = ['60', '73', '89', '114', '92', '112', '116', '118', '122', '136', '142', '145', '152']

def _find_size(t):
    m = re.search(r'\b(65х70|80х105|65\*70|80\*105)\b', t)
    if m:
        return m.group(1).replace('*', 'х')
    m = re.search(r'ø\s?(\d{2,3})', t) or re.search(r'\b(\d{2,3})\s?(?:мм|mm)\b', t)
    if m:
        return 'ø' + m.group(1)
    # голое известное значение диаметра (напр. «пакер 142»)
    m = re.search(r'\b(' + '|'.join(KNOWN_DIA) + r')\b', t)
    if m:
        return 'ø' + m.group(1)
    return None


def _find_thread(t):
    up = t.upper()
    for th in THREADS:
        if th in up:
            return th
    return None


def _find_qty(t):
    m = re.search(r'(\d+)\s*(?:шт|штук)?', t)
    return int(m.group(1)) if m else None


def _status_for(location):
    """Статус по типу локации, если не указан явно."""
    for l in ENG.locations():
        if l['name'] == location:
            ty = (l['type'] or '').lower()
            if 'скваж' in ty:
                return 'На устье'
            if 'ремонт' in ty:
                return 'Ремонт'
            return 'Хранение'
    return 'Хранение'


def _explicit_status(t):
    for k, v in STATUS_WORDS.items():
        if k in t:
            return v
    return None


def try_parse(text):
    """Пытается разобрать фразу без Claude. Возвращает (kind, args) или None."""
    t = ' ' + text.lower().strip() + ' '

    # ПЕРЕДВИЖЕНИЕ: "... с/из A на/в B ..."
    if re.search(r'перевез|перемест|перекин|отправ|переброс|верн', t):
        m = re.search(r'\b(?:с|из|со)\s+(.+?)\s+(?:на|в)\s+(.+?)\s*$', t)
        if m:
            part = _find_part(t); size = _find_size(t); qty = _find_qty(t)
            if part and size and qty:
                a_raw, b_raw = m.group(1).strip(), m.group(2).strip()
                # обрезать хвост B от лишних слов номенклатуры
                b_raw = re.split(r'\s+(нкт|агрп|пакер|яг|\d)', b_raw)[0].strip() or b_raw
                loc_a, _ = ENG.resolve_location(a_raw)
                loc_b, _ = ENG.resolve_location(b_raw)
                return ('move', dict(loc_a=loc_a, loc_b=loc_b, part=part, size=size,
                                     thread=_find_thread(t), qty=qty))

    # ПОСТУПЛЕНИЕ: "поступило ... от S ... на L"
    if re.search(r'поступ|приход|пришл|привез|завез|получен', t):
        part = _find_part(t); size = _find_size(t); qty = _find_qty(t)
        if part and size and qty:
            sup = ''
            ms = re.search(r'\bот\s+(.+?)(?:\s+на\s+|\s*$)', t)
            if ms:
                sup = ms.group(1).strip()
            ml = re.search(r'\bна\s+(.+?)\s*$', t)
            loc = ml.group(1).strip() if ml else ''
            loc = re.split(r'\s+(склад|хранени|устье|скважин|ремонт)', loc)[0].strip() or loc
            location, _ = ENG.resolve_location(loc) if loc else ('Атырау - База ЖМС', False)
            status = _explicit_status(t) or 'Хранение'
            return ('in', dict(supplier=sup or 'н/д', part=part, size=size,
                               thread=_find_thread(t), location=location, status=status, qty=qty))

    # ОСТАТКИ: "сколько ... / остаток ..."
    if re.search(r'сколько|остат|наличи|есть\b|склад\b|покажи', t):
        return ('inv', dict(part=_find_part(t), status=_explicit_status(t),
                            location=_inv_location(t)))
    return None


def _inv_location(t):
    for l in ENG.locations():
        if l['name'].lower() in t:
            return l['name']
    return None


def _fmt_inv(items):
    if not items:
        return "📦 Пусто — ничего не найдено по этому запросу."
    total = sum(i['qty'] for i in items)
    lines = []
    for i in items[:40]:
        th = f" {i['thread']}" if i['thread'] else ''
        lines.append(f"• {i['location']} | {i['status']} | {i['part']} {i['size']}{th} — {i['qty']}")
    more = '' if len(items) <= 40 else f"\n… ещё {len(items)-40} строк"
    return "📦 Остатки (всего {}):\n{}{}".format(total, '\n'.join(lines), more)


def run_fast(kind, args):
    if kind == 'inv':
        items = ENG.inventory(args.get('location'), args.get('status'), args.get('part'))
        return _fmt_inv(items)
    if kind == 'move':
        ENG.record_movement(str(date.today()), args['loc_a'], _status_for(args['loc_a']),
                            args['loc_b'], _status_for(args['loc_b']), args['part'],
                            args['size'], args.get('thread'), args['qty'])
        aft = ENG.inventory(args['loc_b'], _status_for(args['loc_b']), args['part'])
        return "🔄 Записал передвижение: {} {} {}{} — {}→{} ({} шт).\n{}".format(
            args['part'], args['size'], args.get('thread') or '', '', args['loc_a'],
            args['loc_b'], args['qty'], _fmt_inv(aft))
    if kind == 'in':
        ENG.record_incoming(str(date.today()), args['supplier'], args['part'], args['size'],
                            args.get('thread'), args['location'], args['status'], args['qty'])
        aft = ENG.inventory(args['location'], args['status'], args['part'])
        return "✅ Записал поступление: {} {} {} шт → {} ({}).\n{}".format(
            args['part'], args['size'], args['qty'], args['location'], args['status'], _fmt_inv(aft))
    return "❌ Не понял команду."


# ═══════════════ запасной путь: Claude Haiku ═══════════════
SYSTEM = ("Ты помощник склада нефтепромыслового оборудования. Отвечай по-русски, кратко, с эмодзи. "
          "Виды: НКТ, АГРП, Пакер, ЯГ. Статусы: На устье, В скважине, Дефектоскопия, Брак, Ремонт, Хранение. "
          "Перемещение→record_movement, поступление→record_incoming, остатки→get_inventory, "
          "новая локация→add_location. Не хватает данных — уточни одним вопросом.")

TOOLS = [
    {"name": "get_inventory", "description": "Остатки. Фильтры необязательны.",
     "input_schema": {"type": "object", "properties": {
         "location": {"type": "string"}, "status": {"type": "string"}, "part": {"type": "string"}}}},
    {"name": "record_movement", "description": "Передвижение А→Б.",
     "input_schema": {"type": "object",
        "required": ["loc_a", "status_a", "loc_b", "status_b", "part", "size", "qty"],
        "properties": {"loc_a": {"type": "string"}, "status_a": {"type": "string"},
                       "loc_b": {"type": "string"}, "status_b": {"type": "string"},
                       "part": {"type": "string"}, "size": {"type": "string"},
                       "thread": {"type": "string"}, "qty": {"type": "integer"}}}},
    {"name": "record_incoming", "description": "Поступление от поставщика.",
     "input_schema": {"type": "object",
        "required": ["supplier", "part", "size", "location", "status", "qty"],
        "properties": {"supplier": {"type": "string"}, "part": {"type": "string"},
                       "size": {"type": "string"}, "thread": {"type": "string"},
                       "location": {"type": "string"}, "status": {"type": "string"},
                       "qty": {"type": "integer"}}}},
    {"name": "add_location", "description": "Добавить локацию.",
     "input_schema": {"type": "object", "required": ["name"],
        "properties": {"name": {"type": "string"}, "loc_type": {"type": "string"}}}},
]


def run_tool(name, a):
    try:
        if name == 'get_inventory':
            return {"items": ENG.inventory(a.get('location'), a.get('status'), a.get('part'))}
        if name == 'record_movement':
            ENG.record_movement(str(date.today()), a['loc_a'], a['status_a'], a['loc_b'],
                                a['status_b'], a['part'], a['size'], a.get('thread'), a['qty'])
            return {"ok": True, "after": ENG.inventory(a['loc_b'], a['status_b'], a['part'])}
        if name == 'record_incoming':
            ENG.record_incoming(str(date.today()), a['supplier'], a['part'], a['size'],
                                a.get('thread'), a['location'], a['status'], a['qty'])
            return {"ok": True, "after": ENG.inventory(a['location'], a['status'], a['part'])}
        if name == 'add_location':
            return {"ok": True, "added": ENG.add_location(a['name'], a.get('loc_type', 'Прочее'))}
        return {"error": "unknown tool"}
    except Exception as exc:
        log.exception("tool %s failed", name)
        return {"error": str(exc)}


def ask_claude(chat_id, text):
    if AI is None:
        return ("🤖 Не понял. Примеры команд:\n"
                "• Перевезли 200 НКТ ø89 TMK с База ЖМС на Каратобе\n"
                "• Поступило 50 Пакер ø142 от РД Пакер на База Атамбаева\n"
                "• Сколько НКТ на хранении?")
    histories[chat_id].append({"role": "user", "content": text})
    histories[chat_id] = histories[chat_id][-MAX_HISTORY:]
    messages = list(histories[chat_id])
    while True:
        resp = AI.messages.create(model=MODEL, max_tokens=900, system=SYSTEM,
                                  tools=TOOLS, messages=messages)
        calls = [b for b in resp.content if b.type == 'tool_use']
        if not calls:
            answer = ' '.join(b.text for b in resp.content if hasattr(b, 'text')).strip()
            histories[chat_id].append({"role": "assistant", "content": answer})
            return answer or "Готово."
        messages.append({"role": "assistant", "content": resp.content})
        messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": c.id,
             "content": json.dumps(run_tool(c.name, c.input), ensure_ascii=False)} for c in calls]})


def handle(chat_id, text):
    parsed = try_parse(text)
    if parsed:
        try:
            return run_fast(*parsed)
        except Exception as exc:
            log.exception("fast path failed")
            return f"❌ Ошибка записи: {exc}"
    return ask_claude(chat_id, text)


# ═══════════════ Telegram ═══════════════
async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or '').strip()
    if not text:
        return
    await ctx.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')
    async with LOCK:
        try:
            reply = await asyncio.to_thread(handle, update.effective_chat.id, text)
        except Exception as exc:
            log.exception("on_message")
            reply = f"❌ Ошибка: {exc}"
    await update.message.reply_text(reply)


async def cmd_start(update: Update, _):
    await update.message.reply_text(
        "👷 Бот учёта оборудования.\n\n"
        "Пишите свободно:\n"
        "• Перевезли 200 НКТ ø89 TMK с База ЖМС на Каратобе\n"
        "• Поступило 50 Пакер ø142 от РД Пакер на База Атамбаева\n"
        "• Сколько НКТ на хранении?\n\n"
        "Свод на листе «Учет» обновляется автоматически.\n/clear — очистить историю")


async def cmd_clear(update: Update, _):
    histories[update.effective_chat.id].clear()
    await update.message.reply_text("🗑 История очищена.")


def main():
    app = Application.builder().token(os.environ['TELEGRAM_TOKEN']).build()
    app.add_handler(CommandHandler('start', cmd_start))
    app.add_handler(CommandHandler('help', cmd_start))
    app.add_handler(CommandHandler('clear', cmd_clear))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    log.info("Bot started (long-polling)")
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
