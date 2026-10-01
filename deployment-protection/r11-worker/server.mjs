import http from "node:http";
import worker, { redeliverLatestFailedProtection } from "./worker.mjs";

const port = Number(process.env.PORT || 10000);
const server = http.createServer(async (req, res) => {
  if (req.method === "GET" && req.url === "/healthz") {
    res.writeHead(200, { "content-type": "application/json" });
    return res.end(JSON.stringify({
      status: "ok",
      mode: "autonomous-policy",
      policy: "r11-autonomous-v2"
    }));
  }

  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  const body = Buffer.concat(chunks);
  const headers = new Headers();
  for (const [k, v] of Object.entries(req.headers)) {
    if (v) headers.set(k, Array.isArray(v) ? v.join(",") : v);
  }

  const request = new Request("https://" + (req.headers.host || "localhost") + req.url, {
    method: req.method,
    headers,
    body: body.length ? body : undefined
  });

  try {
    const response = await worker.fetch(request, process.env);
    res.writeHead(response.status, Object.fromEntries(response.headers));
    res.end(Buffer.from(await response.arrayBuffer()));
  } catch {
    res.writeHead(503, { "content-type": "application/json" });
    res.end(JSON.stringify({ error: "service unavailable" }));
  }
});

server.listen(port, "0.0.0.0", async () => {
  console.log("R11 autonomous protection service listening");
  if (process.env.PPI_REDELIVER_FAILED_ON_START === "true") {
    try {
      const result = await redeliverLatestFailedProtection(process.env);
      console.log(JSON.stringify({
        type: "r11_webhook_recovery",
        ...result
      }));
    } catch (error) {
      console.log(JSON.stringify({
        type: "r11_webhook_recovery",
        redelivered: false,
        reason: "recovery_failed"
      }));
    }
  }
});
