import os
import time
import logging
import requests

# --- Config from env ---
SILLI_URL        = os.environ["SILLI_URL"].rstrip("/")
RAILWAY_TOKEN    = os.environ["RAILWAY_TOKEN"]
SILLI_SERVICE_ID = os.environ["SILLI_SERVICE_ID"]
SILLI_ENV_ID     = os.environ["SILLI_ENV_ID"]
BOT_TOKEN        = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID          = os.environ["TELEGRAM_CHAT_ID"]

CHECK_INTERVAL    = 120   # секунд между проверками
FAIL_THRESHOLD    = 2     # фейлов подряд до редеплоя
REDEPLOY_COOLDOWN = 300   # секунд паузы после редеплоя

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WATCHDOG] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)


def check_health() -> bool:
    """GET /health → 200 OK = жива. Любая ошибка/таймаут = упала."""
    try:
        r = requests.get(f"{SILLI_URL}/health", timeout=10)
        return r.status_code == 200
    except Exception as e:
        log.warning(f"Health check exception: {e}")
        return False


def redeploy_silli() -> bool:
    mutation = """
    mutation serviceInstanceRedeploy($serviceId: String!, $environmentId: String!) {
        serviceInstanceRedeploy(serviceId: $serviceId, environmentId: $environmentId)
    }
    """
    try:
        resp = requests.post(
            "https://backboard.railway.com/graphql/v2",
            json={"query": mutation, "variables": {
                "serviceId": SILLI_SERVICE_ID,
                "environmentId": SILLI_ENV_ID
            }},
            headers={"Authorization": f"Bearer {RAILWAY_TOKEN}", "Content-Type": "application/json"},
            timeout=30
        )
        data = resp.json()
        return not data.get("errors")
    except Exception as e:
        log.error(f"Redeploy request failed: {e}")
        return False


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
    log.info("Railway Watchdog запущен (второй слой защиты после Cloudflare).")

    fail_count = 0
    in_redeploy = False

    while True:
        healthy = check_health()

        if healthy:
            if in_redeploy:
                log.info("Силли восстановилась!")
                tg("✅ <b>Силли восстановилась</b> (Railway Watchdog)")
                in_redeploy = False
            fail_count = 0
            log.info("OK")
        else:
            fail_count += 1
            log.warning(f"Силли не отвечает. Fail {fail_count}/{FAIL_THRESHOLD}")

            if fail_count >= FAIL_THRESHOLD and not in_redeploy:
                log.error("Порог достигнут. Запускаю редеплой...")
                tg(f"⚠️ <b>Railway Watchdog:</b> Силли не отвечает {fail_count} раза подряд. Редеплой...")

                if redeploy_silli():
                    tg("🚀 Редеплой запущен.")
                    in_redeploy = True
                    fail_count = 0
                    time.sleep(REDEPLOY_COOLDOWN)
                    continue
                else:
                    tg("🔴 <b>Railway API недоступен.</b> Cloudflare Watchdog должен уведомить отдельно.")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
