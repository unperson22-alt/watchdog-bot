import os
import time
import logging
import requests

# --- Config from env ---
SILLI_URL       = os.environ["SILLI_URL"].rstrip("/")   # e.g. https://ai-office-shared-production.up.railway.app
RAILWAY_TOKEN   = os.environ["RAILWAY_TOKEN"]
SILLI_SERVICE_ID = os.environ["SILLI_SERVICE_ID"]       # efa6bd21
SILLI_ENV_ID    = os.environ["SILLI_ENV_ID"]            # Railway environment ID для ai-office-shared
BOT_TOKEN       = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID         = os.environ["TELEGRAM_CHAT_ID"]        # -5194783850

CHECK_INTERVAL   = 120   # секунд между проверками
FAIL_THRESHOLD   = 2     # сколько фейлов подряд до редеплоя
REDEPLOY_COOLDOWN = 300  # секунд ожидания после запуска редеплоя

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WATCHDOG] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)


def check_health() -> bool:
    try:
        r = requests.get(f"{SILLI_URL}/health", timeout=10)
        return r.status_code == 200
    except Exception as e:
        log.warning(f"Health check exception: {e}")
        return False


def redeploy_silli() -> dict:
    mutation = """
    mutation serviceInstanceRedeploy($serviceId: String!, $environmentId: String!) {
        serviceInstanceRedeploy(serviceId: $serviceId, environmentId: $environmentId)
    }
    """
    resp = requests.post(
        "https://backboard.railway.com/graphql/v2",
        json={
            "query": mutation,
            "variables": {
                "serviceId": SILLI_SERVICE_ID,
                "environmentId": SILLI_ENV_ID
            }
        },
        headers={
            "Authorization": f"Bearer {RAILWAY_TOKEN}",
            "Content-Type": "application/json"
        },
        timeout=30
    )
    return resp.json()


def tg(text: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        log.warning(f"Telegram send failed: {e}")


def main():
    log.info("Watchdog запущен. Слежу за Силли...")
    tg("🐕 <b>Watchdog запущен.</b> Слежу за Силли каждые 2 минуты.")

    fail_count = 0
    in_redeploy = False

    while True:
        healthy = check_health()

        if healthy:
            if in_redeploy:
                log.info("Силли восстановилась!")
                tg("✅ <b>Силли восстановилась</b> и отвечает на /health")
                in_redeploy = False
            fail_count = 0
            log.info("OK")
        else:
            fail_count += 1
            log.warning(f"Силли не отвечает. Fail {fail_count}/{FAIL_THRESHOLD}")

            if fail_count >= FAIL_THRESHOLD and not in_redeploy:
                log.error("Силли упала! Запускаю редеплой через Railway API...")
                tg(
                    "⚠️ <b>Силли не отвечает</b> 2 проверки подряд (4 минуты).\n"
                    "Запускаю редеплой через Railway API..."
                )
                try:
                    result = redeploy_silli()
                    log.info(f"Redeploy API ответ: {result}")
                    if result.get("errors"):
                        tg(f"❌ Railway API ошибка: {result['errors'][0]['message']}")
                    else:
                        tg("🚀 Редеплой запущен. Жду 5 минут...")
                except Exception as e:
                    log.error(f"Redeploy failed: {e}")
                    tg(f"❌ Не удалось вызвать Railway API: {e}")

                in_redeploy = True
                fail_count = 0
                time.sleep(REDEPLOY_COOLDOWN)
                continue

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
