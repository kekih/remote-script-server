# VixHub Server (FastAPI)

Сервер для удалённого хостинга: файлы, JSON-базы, скрипты Python, статические сайты, самообновление.

## Быстрый старт (Termux / Linux)

```bash
pkg install python nodejs   # Termux
# или apt install python3 python3-pip nodejs

pip install -r requirements.txt
export API_KEY="твой-секретный-ключ"
export UPDATE_SECRET="тот-же-или-другой"
python main.py
```

Данные хранятся в `~/vixhub_data/` (или `DATA_DIR`).

## API (все кроме /health и /update_server требуют заголовок X-API-Key)

| Раздел   | Методы |
|----------|--------|
| Health   | GET /health |
| Files    | GET /api/v1/files?path= , POST mkdir/upload, DELETE |
| DB       | GET /api/v1/db , GET/POST/DELETE /api/v1/db/{name} |
| Scripts  | GET /api/v1/scripts , upload/start/stop/logs |
| Sites    | GET /api/v1/sites , POST (zip/files), DELETE , раздача /sites/{id}/ |
| Update   | POST /api/v1/update_server?secret=... + file |
| Execute  | POST /api/v1/execute (разовый запуск кода) |

Документация: http://IP:8000/docs
