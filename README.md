# Бот учёта оборудования — Google Sheets версия

Бот принимает сообщения в свободной форме, Claude понимает смысл и пишет данные
в Google-таблицу. Остатки пересчитываются автоматически. Новые локации
добавляются сами.

---

## Что уже сделано
- ✅ Таблица загружена в Google Sheets
- ✅ ID таблицы: `1wwiHZ1fySl4UEFHoNUJZ9XiK0vbjKCRnciYAtRBuDOs`

## Что осталось: 3 ключа + деплой

---

## 1. Telegram токен
@BotFather → `/newbot` → скопируй токен. **Старый токен из переписки отзови:**
`/revoke`.

## 2. Anthropic ключ
console.anthropic.com → API Keys → Create Key → пополни баланс ($5 хватит надолго).
**Старый ключ из переписки удали и сделай новый.**

## 3. Сервисный аккаунт Google (доступ бота к таблице)

### 3.1 Включить API
console.cloud.google.com → выбери проект → **APIs & Services → Library** →
включи (**Enable**):
- **Google Sheets API**
- **Google Drive API**

### 3.2 Создать сервисный аккаунт
**APIs & Services → Credentials → Create Credentials → Service account**
- Имя: `uchet-bot` → Create and Continue → роли пропусти → Done

### 3.3 Скачать ключ
Кликни на сервисный аккаунт → **Keys → Add Key → Create new key → JSON**.
Скачается файл `credentials.json` — храни надёжно.

### 3.4 Дать боту доступ к таблице
1. Открой `credentials.json`, найди поле `"client_email"` —
   вид `uchet-bot@проект.iam.gserviceaccount.com`
2. Открой свою Google-таблицу → **Настройки доступа (Share)** →
   вставь этот email → роль **Редактор** → отправить

---

## 4. Деплой на Railway

### 4.1 Код на GitHub
Создай **приватный** репозиторий, залей туда файлы:
`bot.py`, `google_sheets.py`, `requirements.txt`, `railway.toml`.
(`.env.example` — можно, он без секретов. Реальные ключи в GitHub НЕ клади.)

### 4.2 Проект Railway
railway.app → New Project → Deploy from GitHub repo → выбери репозиторий.

### 4.3 Переменные (Variables → Raw Editor)
```
TELEGRAM_TOKEN=новый_токен
ANTHROPIC_API_KEY=новый_ключ_sk-ant
GOOGLE_SHEET_ID=1wwiHZ1fySl4UEFHoNUJZ9XiK0vbjKCRnciYAtRBuDOs
GOOGLE_CREDENTIALS={...весь credentials.json одной строкой...}
```

**Как вставить GOOGLE_CREDENTIALS:** открой `credentials.json` в блокноте,
скопируй ВСЁ содержимое (вместе с `{` и `}`) и вставь в одну строку как значение.
Переносы внутри `private_key` (`\n`) трогать не нужно — оставь как есть.

### 4.4 Запуск
Railway стартует сам. В **Deployments → Logs** должно появиться:
```
Bot started (long-polling)
```

---

## 5. Проверка
В Telegram открой бота → `/start`, затем:
- «Сколько НКТ на хранении?» → бот ответит остатками
- «Перевезли 10 НКТ ø89 TMK с базы ЖМС на Прорву 513» → подтверждение, и в таблице
  на листе «Передвижение» появится строка, а «Учет (структура)» пересчитается

---

## Как работает (кратко)
- Бот пишет ТОЛЬКО в листы «Поступление» / «Передвижение» / «Локации»
- Лист «Учет (структура)» считается формулами Google — сам обновляется
- Новая локация → бот вставляет строку в Учёт, КОПИРУЯ формулы из соседней
  (формулы строкой не пишутся — это защищает от проблем с русской локалью)
- Ответы в Telegram бот считает на Python из журналов — всегда точны

## Файлы
```
bot.py            — бот + связка с Claude
google_sheets.py  — работа с Google-таблицей
requirements.txt  — зависимости
railway.toml      — конфиг запуска
.env.example      — шаблон переменных
```

## Если ошибка
Скинь текст из Railway → Logs. Частые причины: забыл дать доступ сервисному
аккаунту к таблице (403), не включён Sheets/Drive API, неверный GOOGLE_CREDENTIALS
(должен быть валидный JSON одной строкой).
