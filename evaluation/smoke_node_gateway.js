"use strict";

const gatewayUrl = argValue("--gateway-url") || process.env.GATEWAY_URL || "http://127.0.0.1:8080";
const projectName = argValue("--project-name") || process.env.CASE10_GATEWAY_PROJECT || "CASE10 gateway smoke";
const defaultLogin = process.env.DEFAULT_ADMIN_LOGIN || "admin";
const defaultPassword = process.env.DEFAULT_ADMIN_PASSWORD || "Admin123!ChangeMe";
const requestedObjectId = argValue("--object-id") || process.env.CASE10_GATEWAY_OBJECT_ID || "";

function argValue(name) {
  const index = process.argv.indexOf(name);
  return index >= 0 && process.argv[index + 1] ? process.argv[index + 1] : "";
}

function assert(condition, message, details) {
  if (!condition) {
    const error = new Error(message);
    error.details = details;
    throw error;
  }
}

async function request(method, path, { body, token, expectedStatus = 200 } = {}) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(`${gatewayUrl}${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(600000),
  });
  const contentType = response.headers.get("content-type") || "";
  const data = contentType.includes("application/json") ? await response.json() : await response.arrayBuffer();
  if (response.status !== expectedStatus) {
    throw new Error(`${method} ${path} returned ${response.status}, expected ${expectedStatus}: ${JSON.stringify(data).slice(0, 1000)}`);
  }
  return data;
}

async function waitForReady(processId, token, timeoutMs = 240000) {
  const started = Date.now();
  let lastStatus = null;
  while (Date.now() - started < timeoutMs) {
    lastStatus = await request("GET", `/api/case10/processes/${encodeURIComponent(processId)}/status`, { token });
    if (lastStatus.status === "READY") return lastStatus;
    if (lastStatus.status === "FAILED") {
      throw new Error(`Process failed: ${JSON.stringify(lastStatus).slice(0, 2000)}`);
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error(`Process ${processId} did not reach READY: ${JSON.stringify(lastStatus).slice(0, 2000)}`);
}

async function main() {
  const health = await request("GET", "/health");
  assert(health.status === "ok", "Gateway health is not OK", health);

  const openapi = await request("GET", "/openapi.json");
  assert(openapi.openapi && openapi.paths && openapi.paths["/api/case10/matrix"], "OpenAPI document does not expose CASE10 matrix", openapi);

  const login = await request("POST", "/api/auth/login", {
    body: { login: defaultLogin, password: defaultPassword },
  });
  const token = login.access_token;
  assert(token, "Login did not return access_token", login);

  const projects = await request("GET", "/api/projects", { token });
  let project = projects.find((row) => row.name === projectName);
  if (!project) {
    project = await request("POST", "/api/projects", {
      token,
      body: { name: projectName, description: "Node gateway smoke project" },
    });
  }

  const imported = await request("POST", `/api/case10/projects/${project.id}/official-dataset/import`, {
    token,
    body: { run_processes: false },
  });
  assert(imported.matrix_params === 132, "Official dataset import did not expose 132 matrix params", imported);

  const matrix = await request("GET", `/api/case10/matrix?project_id=${project.id}`, { token });
  assert(matrix.matrix && matrix.matrix.params_count === 132 && matrix.params.length === 132, "Gateway matrix did not return 132 params", matrix.matrix);

  const objectId = requestedObjectId || imported.objects[0];
  assert(objectId, "Official import did not return object ids", imported);

  const process = await request("POST", `/api/case10/projects/${project.id}/processes`, {
    token,
    body: { object_id: objectId },
  });
  assert(["QUEUED", "PROCESSING", "PARSING", "READY"].includes(process.status), "Process was not accepted for async processing", process);
  const processId = process.process_id || process.id;

  const status = await waitForReady(processId, token);
  assert(status.status === "READY", "Process status endpoint did not proxy READY", status);

  const groups = await request("GET", `/api/case10/evidence-groups?project_id=${project.id}&process_id=${encodeURIComponent(processId)}`, { token });
  assert(Array.isArray(groups) && groups.length > 0, "Evidence groups endpoint returned no groups", groups);

  const protocol = await request("GET", `/api/case10/protocols/current?project_id=${project.id}&process_id=${encodeURIComponent(processId)}`, { token });
  assert(protocol && protocol.id, "Current protocol endpoint did not return protocol", protocol);

  const protocolJson = await request("GET", `/api/case10/protocols/${protocol.id}/json`, { token });
  assert(protocolJson.id === protocol.id, "Protocol JSON endpoint returned a different protocol", protocolJson);

  const predictions = await request("GET", `/api/case10/protocols/${protocol.id}/evaluation-predictions`, { token });
  assert(Array.isArray(predictions.findings), "Evaluation predictions endpoint did not return findings", predictions);

  const submission = await request("GET", `/api/case10/protocols/${protocol.id}/submission`, { token });
  assert(Array.isArray(submission.validation_errors) && submission.validation_errors.length === 0, "Submission endpoint returned schema errors", submission);

  const forwardedError = await request("GET", "/api/case10/processes/not-a-real-process/status", {
    token,
    expectedStatus: 404,
  });
  assert(forwardedError.detail, "Gateway did not forward Python HTTP error body", forwardedError);

  console.log(JSON.stringify({
    gateway_url: gatewayUrl,
    project_id: project.id,
    object_id: objectId,
    process_id: processId,
    protocol_id: protocol.id,
    matrix_params: matrix.params.length,
    evidence_groups: groups.length,
    prediction_findings: predictions.findings.length,
    submission_checks: submission.submission.checks.length,
    forwarded_error: forwardedError.detail,
  }));
}

main().catch((error) => {
  console.error(error.message);
  if (error.details) console.error(JSON.stringify(error.details).slice(0, 2000));
  process.exit(1);
});
