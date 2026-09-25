"use strict";

const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");

const PORT = Number.parseInt(process.env.PORT || "8080", 10);
const UPSTREAM = (process.env.PYTHON_API_BASE_URL || "http://api:8000").replace(/\/+$/, "");
const UPSTREAM_TIMEOUT_MS = Number.parseInt(process.env.UPSTREAM_TIMEOUT_MS || "120000", 10);
const HEALTH_TIMEOUT_MS = Number.parseInt(process.env.HEALTH_TIMEOUT_MS || "5000", 10);
const CORS_ORIGINS = process.env.CORS_ORIGINS || "*";

const openapi = JSON.parse(fs.readFileSync(path.join(__dirname, "openapi.json"), "utf8"));

const hopByHopHeaders = new Set([
  "connection",
  "content-length",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
  "host",
]);

const allowedRoutes = [
  ["POST", /^\/api\/auth\/login$/],
  ["POST", /^\/api\/auth\/logout$/],
  ["GET", /^\/api\/me$/],
  ["GET", /^\/api\/projects$/],
  ["POST", /^\/api\/projects$/],
  ["POST", /^\/api\/upload$/],
  ["GET", /^\/api\/documents$/],
  ["DELETE", /^\/api\/documents\/[^/]+$/],
  ["GET", /^\/api\/case10\/overview$/],
  ["GET", /^\/api\/case10\/documents$/],
  ["POST", /^\/api\/case10\/projects\/[^/]+\/official-dataset\/import$/],
  ["POST", /^\/api\/case10\/projects\/[^/]+\/processes$/],
  ["GET", /^\/api\/case10\/processes$/],
  ["GET", /^\/api\/case10\/processes\/[^/]+\/status$/],
  ["POST", /^\/api\/case10\/processes\/[^/]+\/run$/],
  ["GET", /^\/api\/case10\/matrix$/],
  ["POST", /^\/api\/case10\/matrix\/import$/],
  ["GET", /^\/api\/case10\/evidence-groups$/],
  ["GET", /^\/api\/case10\/evidence-groups\/[^/]+$/],
  ["POST", /^\/api\/case10\/evidence-groups\/[^/]+\/decisions$/],
  ["GET", /^\/api\/case10\/evidence-fragments\/[^/]+\/page\.png$/],
  ["GET", /^\/api\/case10\/protocol$/],
  ["GET", /^\/api\/case10\/protocols\/current$/],
  ["GET", /^\/api\/case10\/protocols\/[^/]+\/json$/],
  ["GET", /^\/api\/case10\/protocols\/[^/]+\/evaluation-predictions$/],
  ["GET", /^\/api\/case10\/protocols\/[^/]+\/submission$/],
  ["GET", /^\/api\/case10\/protocols\/[^/]+\/pdf$/],
  ["POST", /^\/api\/case10\/protocols\/[^/]+\/finalize$/],
  ["POST", /^\/api\/case10\/protocols\/[^/]+\/unfinalize$/],
  ["GET", /^\/api\/case10\/processes\/[^/]+\/protocols$/],
  ["GET", /^\/api\/case10\/processes\/[^/]+\/audit$/],
  ["POST", /^\/api\/case10\/projects\/[^/]+\/training-release$/],
  ["GET", /^\/api\/case10\/projects\/[^/]+\/ml-retraining-log$/],
  // S5 inspector workbench
  ["GET", /^\/api\/case10\/processes\/[^/]+\/workbench$/],
  ["GET", /^\/api\/case10\/evidence-groups\/[^/]+\/workbench$/],
  ["GET", /^\/api\/case10\/evidence-groups\/[^/]+\/edits$/],
  ["POST", /^\/api\/case10\/evidence-groups\/[^/]+\/fragments$/],
  ["POST", /^\/api\/case10\/evidence-groups\/[^/]+\/fragments\/[^/]+\/(refine|remove|restore)$/],
  ["POST", /^\/api\/case10\/evidence-groups\/bulk-decisions$/],
  ["GET", /^\/api\/case10\/processes\/[^/]+\/revisions$/],
  ["POST", /^\/api\/case10\/processes\/[^/]+\/revision-choices$/],
  ["GET", /^\/api\/case10\/processes\/[^/]+\/completeness$/],
  ["POST", /^\/api\/case10\/processes\/[^/]+\/verification\/open$/],
  ["GET", /^\/api\/case10\/processes\/[^/]+\/verification\/timing$/],
  ["GET", /^\/api\/case10\/document-versions\/[^/]+\/pages\/[^/]+\/geometry$/],
  ["GET", /^\/api\/case10\/document-versions\/[^/]+\/pages\/[^/]+\.(png|pdf)$/],
  ["GET", /^\/api\/entities$/],
  ["GET", /^\/api\/entities\/[^/]+\/portrait$/],
  ["POST", /^\/api\/issues\/[^/]+\/decisions$/],
];

function isAllowed(method, pathname) {
  return allowedRoutes.some(([routeMethod, routePath]) => routeMethod === method && routePath.test(pathname));
}

function setCorsHeaders(res) {
  res.setHeader("Access-Control-Allow-Origin", CORS_ORIGINS);
  res.setHeader("Access-Control-Allow-Methods", "GET,POST,DELETE,OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Authorization,Content-Type,X-API-Key");
  res.setHeader("Access-Control-Max-Age", "600");
}

function sendJson(res, statusCode, payload) {
  setCorsHeaders(res);
  res.statusCode = statusCode;
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.end(JSON.stringify(payload));
}

function copyResponseHeaders(upstreamResponse, res) {
  for (const [name, value] of upstreamResponse.headers.entries()) {
    if (!hopByHopHeaders.has(name.toLowerCase())) {
      res.setHeader(name, value);
    }
  }
  res.setHeader("X-Case10-Gateway", "node");
}

function proxyRequestHeaders(req) {
  const headers = {};
  for (const [name, value] of Object.entries(req.headers)) {
    if (!hopByHopHeaders.has(name.toLowerCase()) && value !== undefined) {
      headers[name] = value;
    }
  }
  headers["x-forwarded-host"] = req.headers.host || "";
  headers["x-forwarded-proto"] = "http";
  return headers;
}

async function readBody(req) {
  const chunks = [];
  for await (const chunk of req) {
    chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
  }
  return Buffer.concat(chunks);
}

async function handleHealth(res) {
  try {
    const upstreamResponse = await fetch(`${UPSTREAM}/health`, {
      signal: AbortSignal.timeout(HEALTH_TIMEOUT_MS),
    });
    if (!upstreamResponse.ok) {
      sendJson(res, 503, {
        status: "error",
        upstream: { url: UPSTREAM, status: upstreamResponse.status },
      });
      return;
    }
    sendJson(res, 200, {
      status: "ok",
      service: "case10-node-gateway",
      upstream: { url: UPSTREAM, status: "ok" },
    });
  } catch (error) {
    sendJson(res, 503, {
      status: "error",
      service: "case10-node-gateway",
      upstream: { url: UPSTREAM, error: error.message },
    });
  }
}

async function proxy(req, res, url) {
  const body = req.method === "GET" || req.method === "HEAD" ? undefined : await readBody(req);
  const upstreamUrl = `${UPSTREAM}${url.pathname}${url.search}`;
  try {
    const upstreamResponse = await fetch(upstreamUrl, {
      method: req.method,
      headers: proxyRequestHeaders(req),
      body,
      redirect: "manual",
      signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
    });
    const responseBody = Buffer.from(await upstreamResponse.arrayBuffer());
    setCorsHeaders(res);
    res.statusCode = upstreamResponse.status;
    copyResponseHeaders(upstreamResponse, res);
    res.end(responseBody);
  } catch (error) {
    sendJson(res, 502, {
      detail: "Python API gateway upstream request failed",
      upstream: UPSTREAM,
      error: error.message,
    });
  }
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url || "/", `http://${req.headers.host || "localhost"}`);

  if (req.method === "OPTIONS") {
    setCorsHeaders(res);
    res.statusCode = 204;
    res.end();
    return;
  }

  if (req.method === "GET" && url.pathname === "/health") {
    await handleHealth(res);
    return;
  }

  if (req.method === "GET" && url.pathname === "/openapi.json") {
    sendJson(res, 200, openapi);
    return;
  }

  if (!isAllowed(req.method || "", url.pathname)) {
    sendJson(res, 404, {
      detail: "Route is not exposed by the CASE10 Node gateway",
      path: url.pathname,
    });
    return;
  }

  await proxy(req, res, url);
});

server.listen(PORT, "0.0.0.0", () => {
  console.log(`CASE10 Node gateway listening on ${PORT}, upstream=${UPSTREAM}`);
});
