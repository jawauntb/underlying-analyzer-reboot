const appUrl = (Bun.env.APP_URL || "").replace(/\/+$/, "");
const token = Bun.env.ALERT_SCHEDULER_TOKEN || "";

if (!appUrl) {
  throw new Error("APP_URL is required");
}

if (!token) {
  throw new Error("ALERT_SCHEDULER_TOKEN is required");
}

const response = await fetch(`${appUrl}/api/alerts/scheduled/run`, {
  method: "POST",
  headers: {
    Authorization: `Bearer ${token}`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify({ trigger: "railway-cron" }),
});

const body = await response.text();
console.log(body);

if (!response.ok) {
  throw new Error(`Scheduled alert digest failed with ${response.status}`);
}
