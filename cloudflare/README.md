# AI Office Watchdog — Cloudflare Workers

Мониторит Силли каждые 5 минут с инфраструктуры Cloudflare.
Не зависит от Railway — переживёт любой Railway инцидент.

## Деплой (один раз, ~5 минут)

```bash
# 1. Установить wrangler
npm install -g wrangler

# 2. Логин в Cloudflare
wrangler login

# 3. Создать KV namespace — скопировать ID в wrangler.toml
wrangler kv:namespace create WATCHDOG_KV

# 4. Добавить секреты
wrangler secret put RAILWAY_TOKEN   # 9cf51308-07ba-4161-b955-4a00d650c8da
wrangler secret put TG_BOT_TOKEN    # токен бота (CODER_BOT_TOKEN из переменных Силли)

# 5. Деплой
wrangler deploy
```

## После деплоя

- Статус вручную: GET https://ai-office-watchdog.<subdomain>.workers.dev
- Cron: автоматически каждые 5 минут
- Логи: wrangler tail

## Логика

| Ситуация                        | Действие                              |
|---------------------------------|---------------------------------------|
| Силли жива                      | тихо, сброс счётчика                  |
| 2 фейла подряд (10 мин)        | редеплой через Railway API            |
| Railway API тоже недоступен    | алерт об инциденте в Telegram         |
| Силли восстановилась            | уведомление в Telegram                |
