"""
bot.py — Telegram-бот учёта оборудования (Google Sheets версия).
Claude понимает свободный текст и управляет таблицей.

Переменные окружения (Railway → Variables):
  TELEGRAM_TOKEN     — токен от @BotFather
  ANTHROPIC_API_KEY  — ключ с console.anthropic.com
  GOOGLE_SHEET_ID    — ID таблицы из ссылки
  GOOGLE_CREDENTIALS — полное содержимое credentials.json (одной строкой)
"""
import asyncio
import json
import logging
import os
from collections import defaultdict
from datetime import date

import anthropic
from telegram import Update
from telegram.ext import (Application, CommandHandler, ContextTypes,
                           MessageHandler, filters)

from google_sheets import SheetEngine

logging.basicConfig(
    format='%(asctime)s │ %(levelname)s │ %(message)s', level=logging.INFO)
log = logging.getLogger(__name__)

AI    = anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'])
CREDS = json.loads(os.environ['GOOGLE_CREDENTIALS'])
SHEET = os.environ['GOOGLE_SHEET_ID']
ENG   = SheetEngine(CREDS, SHEET)

MODEL       = 'claude-sonnet-4-6'
MAX_HISTORY = 20
LOCK        = asyncio.Lock()
histories   = defaultdict(list)

SYSTEM = """
Ты — помощник по учёту нефтепромыслового оборудования. Управляешь Google-таблицей через инструменты.

ОБОРУДОВАНИЕ:
- НКТ: размеры ø60 / ø73 / ø89 / ø114 | резьба TMK / VAM TOP / HCM-1 / EUE / NUE
- АГРП: 65х70 / 80х105
- Пакер: ø92 / ø112 / ø114 / ø116 / ø118 / ø122 / ø136 / ø142 / ø145 / ø152 / ø152 ПРО-ЯМО
- ЯГ: ø152

СТАТУСЫ: На устье | В скважине | Дефектоскопия | Брак | Ремонт | Хранение

ПРАВИЛА:
- Отвечай только на русском
- Перемещение → record_movement; поступление → record_incoming; запрос остатков → get_inventory
- Новая локация → add_location, затем запись
- Не хватает данных (что/сколько) — уточни ОДНИМ вопросом перед записью
- После изменения подтверди и покажи новый остаток
- Дата по умолчанию — сегодня
- Кратко, по делу, с эмодзи (✅ 📦 🔄 ❌)
"""

TOOLS = [
    {"name": "get_inventory",
     "description": "Текущие остатки. Все фильтры необязательны.",
     "input_schema": {"type": "object", "properties": {
         "location": {"type": "string"}, "status": {"type": "string"},
         "part": {"type": "string", "description": "НКТ / АГРП / Пакер / ЯГ"}}}},
    {"name": "get_locations", "description": "Список локаций.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "record_movement",
     "description": "Передвижение из локации А в локацию Б.",
     "input_schema": {"type": "object",
        "required": ["loc_a", "status_a", "loc_b", "status_b", "part", "size", "qty"],
        "properties": {
            "date": {"type": "string"}, "loc_a": {"type": "string"},
            "status_a": {"type": "string"}, "loc_b": {"type": "string"},
            "status_b": {"type": "string"}, "part": {"type": "string"},
            "size": {"type": "string"}, "thread": {"type": "string"},
            "qty": {"type": "integer"}, "note": {"type": "string"}}}},
    {"name": "record_incoming",
     "description": "Поступление нового оборудования от поставщика.",
     "input_schema": {"type": "object",
        "required": ["supplier", "part", "size", "location", "status", "qty"],
        "properties": {
            "date": {"type": "string"}, "supplier": {"type": "string"},
            "part": {"type": "string"}, "size": {"type": "string"},
            "thread": {"type": "string"}, "location": {"type": "string"},
            "status": {"type": "string"}, "qty": {"type": "integer"},
            "note": {"type": "string"}}}},
    {"name": "add_location", "description": "Добавить локацию в справочник.",
     "input_schema": {"type": "object", "required": ["name"], "properties": {
         "name": {"type": "string"},
         "loc_type": {"type": "string", "description": "База / склад | Скважина | Прочее"}}}},
]


def run_tool(name, args):
    try:
        if name == 'get_inventory':
            items = ENG.inventory(args.get('location'), args.get('status'), args.get('part'))
            return {"items": items, "total": sum(i['qty'] for i in items)}
        if name == 'get_locations':
            return {"locations": ENG.locations()}
        if name == 'record_movement':
            ENG.record_movement(args.get('date') or str(date.today()),
                args['loc_a'], args['status_a'], args['loc_b'], args['status_b'],
                args['part'], args['size'], args.get('thread'), args['qty'], args.get('note', ''))
            return {"success": True,
                    "after_a": ENG.inventory(args['loc_a'], args['status_a'], args['part']),
                    "after_b": ENG.inventory(args['loc_b'], args['status_b'], args['part'])}
        if name == 'record_incoming':
            ENG.record_incoming(args.get('date') or str(date.today()), args['supplier'],
                args['part'], args['size'], args.get('thread'),
                args['location'], args['status'], args['qty'], args.get('note', ''))
            return {"success": True,
                    "after": ENG.inventory(args['location'], args['status'], args['part'])}
        if name == 'add_location':
            added = ENG.add_location(args['name'], args.get('loc_type', 'Прочее'))
            return {"success": True, "added": added, "name": args['name']}
        return {"error": f"unknown tool {name}"}
    except Exception as exc:
        log.exception("tool %s failed", name)
        return {"error": str(exc)}


def ask_claude(chat_id, text):
    histories[chat_id].append({"role": "user", "content": text})
    histories[chat_id] = histories[chat_id][-MAX_HISTORY:]
    messages = list(histories[chat_id])

    while True:
        resp = AI.messages.create(model=MODEL, max_tokens=1024,
                                  system=SYSTEM, tools=TOOLS, messages=messages)
        calls = [b for b in resp.content if b.type == 'tool_use']
        if not calls:
            answer = ' '.join(b.text for b in resp.content if hasattr(b, 'text')).strip()
            histories[chat_id].append({"role": "assistant", "content": answer})
            return answer
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for c in calls:
            results.append({"type": "tool_result", "tool_use_id": c.id,
                            "content": json.dumps(run_tool(c.name, c.input), ensure_ascii=False)})
        messages.append({"role": "user", "content": results})


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = (update.message.text or '').strip()
    if not text:
        return
    await ctx.bot.send_chat_action(chat_id=chat_id, action='typing')
    async with LOCK:
        try:
            reply = await asyncio.to_thread(ask_claude, chat_id, text)
        except Exception as exc:
            log.exception("on_message error")
            reply = f"❌ Ошибка: {exc}"
    await update.message.reply_text(reply)


async def cmd_start(update: Update, _):
    await update.message.reply_text(
        "👷 Привет! Я бот для учёта оборудования.\n\n"
        "Пишите в свободной форме:\n"
        "• Перевезли 200 НКТ ø89 TMK с базы ЖМС на Прорву 513\n"
        "• Сколько пакеров на хранении?\n"
        "• Поступило 50 НКТ ø89 TMK от Инсер на базу Атамбаева\n"
        "• Добавь скважину Гран-52\n\n"
        "/clear — очистить историю")


async def cmd_clear(update: Update, _):
    histories[update.effective_chat.id].clear()
    await update.message.reply_text("🗑 История очищена.")


def main():
    app = Application.builder().token(os.environ['TELEGRAM_TOKEN']).build()
    app.add_handler(CommandHandler('start', cmd_start))
    app.add_handler(CommandHandler('clear', cmd_clear))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    log.info("Bot started (long-polling)")
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
