// Cloudflare Watchdog для AI Office — мониторит Силли каждые 5 минут
// Живёт на Cloudflare, не зависит от Railway вообще

const SILLI_URL        = "https://ai-office-shared-production.up.railway.app/health";
const SILLI_SERVICE_ID = "95999005-f1a9-4ce9-9cee-7e803394e14e";
const SILLI_ENV_ID     = "2efaaf60-ba39-492c-bf86-007fd505493f";
const TG_CHAT_ID       = "-5194783850";
const FAIL_THRESHOLD        = 5;  // 5 проверок = 25 минут — редеплой
const FAIL_ALERT_THRESHOLD  = 2;  // 2-4 проверки = тихо, без алерта

// KV ключи (персистентные между запусками)
const K_FAILS       = "watchdog:fail_count";
const K_REDEPLOYING = "watchdog:redeploying";

async function checkHealth() {
  try {
    const resp = await fetch(SILLI_URL, { signal: AbortSignal.timeout(10000) });
    return resp.ok;
  } catch {
    return false;
  }
}

async function tg(env, text) {
  await fetch(`https://api.telegram.org/bot${env.TG_BOT_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_id: TG_CHAT_ID, text, parse_mode: "HTML" }),
  }).catch(() => {});
}

async function redeploySilli(env) {
  const mutation = `mutation {
    serviceInstanceRedeploy(
      serviceId: "${SILLI_SERVICE_ID}",
      environmentId: "${SILLI_ENV_ID}"
    )
  }`;
  try {
    const resp = await fetch("https://backboard.railway.com/graphql/v2", {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${env.RAILWAY_TOKEN}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ query: mutation }),
    });
    const data = await resp.json();
    return !data.errors;
  } catch {
    return false;
  }
}

export default {
  // ── Cron (каждые 5 минут) ──────────────────────────────────────────────────
  async scheduled(event, env, ctx) {
    const kv = env.WATCHDOG_KV;
    const healthy = await checkHealth();

    if (healthy) {
      const wasRedeploying = await kv.get(K_REDEPLOYING);
      await kv.put(K_FAILS, "0");
      if (wasRedeploying === "1") {
        await kv.put(K_REDEPLOYING, "0");
        await tg(env, "✅ <b>Силли восстановилась</b> и отвечает на /health");
      }
      return;
    }

    // Не отвечает — увеличиваем счётчик
    const fails = parseInt(await kv.get(K_FAILS) ?? "0") + 1;
    await kv.put(K_FAILS, String(fails));
    const alreadyRedeploying = (await kv.get(K_REDEPLOYING)) === "1";

    // Тихий лог при 2-4 fail — без алерта в группу
    if (fails >= FAIL_ALERT_THRESHOLD && fails < FAIL_THRESHOLD && !alreadyRedeploying) {
      return; // кратковременный провал — ждём
    }

    if (fails >= FAIL_THRESHOLD && !alreadyRedeploying) {
      await tg(env,
        `⚠️ <b>Силли не отвечает</b> ${fails} проверок подряд (~${fails * 5} мин).\n` +
        `Запускаю редеплой через Railway API...`
      );

      const ok = await redeploySilli(env);

      if (ok) {
        await kv.put(K_REDEPLOYING, "1");
        await kv.put(K_FAILS, "0");
        await tg(env, "🚀 Редеплой запущен. Жду восстановления...");
      } else {
        // Railway API тоже не отвечает — инцидент на стороне Railway
        await tg(env,
          "🔴 <b>Railway API недоступен.</b>\n" +
          "Вероятно инцидент на Railway. Проверь: https://status.railway.app\n" +
          "Нужно ручное вмешательство."
        );
      }
    }
  },

  // ── HTTP fetch (ручная проверка через браузер) ─────────────────────────────
  async fetch(request, env, ctx) {
    const kv = env.WATCHDOG_KV;
    const healthy = await checkHealth();
    const fails = parseInt(await kv.get(K_FAILS) ?? "0");
    const redeploying = (await kv.get(K_REDEPLOYING)) === "1";

    return Response.json({
      silli_healthy: healthy,
      fail_count: fails,
      redeploying,
      checked_at: new Date().toISOString(),
    });
  },
};
