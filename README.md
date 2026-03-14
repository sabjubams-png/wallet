# VAULT — Инструкция по установке

## Проблема которую решаем
GitHub Pages = статика, бот пишет в файл на сервере → балансы не синхронизируются.
**Решение:** Добавляем Flask сервер как посредника.

---

## Структура
```
vault2/
├── index.html       ← WebApp (загружается на GitHub Pages)
├── server.py        ← API сервер + Telegram бот (Railway/VPS)
├── requirements.txt
├── Procfile         ← для Railway/Render
└── README.md
```

---

## Шаг 1 — Задеплоить сервер на Railway (бесплатно)

1. Зайди на **railway.app** → New Project → Deploy from GitHub
2. Загрузи репозиторий с `server.py`, `requirements.txt`, `Procfile`
3. В Railway → Variables добавь:
   ```
   BOT_TOKEN    = 1234567890:AAxxxxxxxx
   WEBAPP_URL   = https://твой-ник.github.io/vault
   ADMIN_IDS    = 123456789
   ADMIN_SECRET = придумай-секрет-123
   PORT         = 5000
   ```
4. После деплоя скопируй URL вида: `https://vault-production-xxxx.up.railway.app`

**Альтернатива — Render.com:**
- Такой же процесс, тоже бесплатно
- Start command: `python server.py`

---

## Шаг 2 — Прописать URL сервера в WebApp

Открой `index.html`, найди строку:
```javascript
const API_BASE = window.VAULT_API || 'https://your-server.railway.app';
```
Замени на твой URL:
```javascript
const API_BASE = window.VAULT_API || 'https://vault-production-xxxx.up.railway.app';
```

Загрузи обновлённый `index.html` на GitHub Pages.

---

## Шаг 3 — Проверить что всё работает

Открой в браузере:
```
https://твой-сервер.railway.app/api/health
```
Должно вернуть: `{"status": "ok", ...}`

Проверь курсы:
```
https://твой-сервер.railway.app/api/prices
```

---

## Как теперь работает синхронизация

```
Пользователь открывает WebApp
       ↓
WebApp загружает данные с сервера (/api/user/{id})
       ↓
Каждые 10 секунд — полинг сервера
       ↓
Ты пишешь боту /give @user BTC 0.01
       ↓
Бот вызывает /api/admin/give на сервере
       ↓
Сервер обновляет балансы в vault_db.json
       ↓
При следующем полинге WebApp видит изменения
       ↓
Показывает тост "💰 Получено +0.01 BTC!"
```

---

## Команды бота

### Пользователь
| Команда | Описание |
|---------|----------|
| `/start` | Приветствие + кнопка открыть VAULT |
| `/balance` | Балансы всех кошельков |
| `/prices` | Актуальные курсы |

### Администратор
| Команда | Описание |
|---------|----------|
| `/admin` | Панель с кнопками |
| `/give @user BTC 0.01` | Добавить монеты |
| `/setbalance @user ETH 2.5` | Установить баланс |
| `/stats` | Статистика |

---

## Запуск локально (для теста)

```bash
pip install -r requirements.txt

export BOT_TOKEN="ваш_токен"
export WEBAPP_URL="http://localhost:3000"
export ADMIN_IDS="ваш_telegram_id"
export ADMIN_SECRET="секрет123"

python server.py
```

Сервер запустится на `http://localhost:5000`

---

## Деплой на VPS

```bash
# Установка
git clone ... && cd vault2
pip install -r requirements.txt

# .env файл
cat > .env << EOF
BOT_TOKEN=xxx
WEBAPP_URL=https://ник.github.io/vault
ADMIN_IDS=123456789
ADMIN_SECRET=секрет
PORT=5000
EOF

# systemd
sudo nano /etc/systemd/system/vault.service
```

```ini
[Unit]
Description=VAULT Server
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/vault2
EnvironmentFile=/home/ubuntu/vault2/.env
ExecStart=/usr/bin/python3 server.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable vault
sudo systemctl start vault
sudo journalctl -u vault -f  # логи
```
