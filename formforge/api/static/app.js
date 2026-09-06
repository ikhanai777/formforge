/* FormForge key holder interface.
 *
 * Two calls do the work. `POST /v1/keyholder/plan` traces and lays out in about
 * a second and returns a drawing, so every control re-plans as you move it;
 * `POST /v1/keyholder` runs the CAD kernel and streams its steps over the
 * WebSocket the rest of the system already speaks.
 *
 * The split is the whole design of this page. Tracing a photograph is a guess,
 * and the drawing is the only thing that shows which guess was made -- so it is
 * free, immediate and always on screen, and the minute of geometry happens only
 * when someone has looked at it and pressed the button.
 */

const $ = (id) => document.getElementById(id);

const state = {
  image: null,      // base64, no data: prefix
  filename: null,
  planning: false,
  pending: false,   // a plan asked for while one was in flight
  modelId: null,
  socket: null,
};

/* ---------------------------------------------------------------- controls */

const CONTROLS = {
  width: { el: () => $("width"), out: (v) => `${v} mm` },
  hooks: { el: () => $("hooks"), out: (v) => (Number(v) === 0 ? "from the width" : v) },
  plaque: { el: () => $("plaque"), out: (v) => `${Number(v).toFixed(1)} mm` },
  feature: { el: () => $("feature"), out: (v) => `${Number(v).toFixed(1)} mm` },
  border: { el: () => $("border"), out: (v) => `${v} mm` },
  railh: { el: () => $("railh"), out: (v) => `${v} mm` },
  overlap: { el: () => $("overlap"), out: (v) => `${v} mm` },
  threshold: {
    el: () => $("threshold"),
    out: (v) => ($("auto-threshold").checked ? "automatic" : Number(v).toFixed(2)),
  },
};

function syncOutputs() {
  for (const [name, control] of Object.entries(CONTROLS)) {
    const out = $(`${name}-out`);
    if (out) out.textContent = control.out(control.el().value);
  }
}

function settings() {
  const body = {
    image_base64: state.image,
    filename: state.filename,
    width_mm: Number($("width").value),
    hooks: Number($("hooks").value),
    plaque_t_mm: Number($("plaque").value),
    min_feature_mm: Number($("feature").value),
    border_mm: Number($("border").value),
    rail_h_mm: Number($("railh").value),
    rail_overlap_mm: Number($("overlap").value),
    mount: $("mount").value,
    detail: $("detail").value,
    printer_profile: $("profile").value || undefined,
    material: $("material").value,
  };
  if (!$("auto-threshold").checked) body.threshold = Number($("threshold").value);
  if ($("invert").checked) body.invert = true;
  return body;
}

/* ------------------------------------------------------------------ upload */

// Images are sent as they are when they are small enough, which keeps a cut-out
// PNG's alpha channel exactly as it was -- the one case that traces perfectly.
// Anything larger is resized in the browser first: the tracer works at 512 px,
// so a 12 megapixel photograph is bytes nobody uses.
const SEND_AS_IS_BYTES = 4 * 1024 * 1024;
const RESIZE_TO_PX = 1400;

async function readImage(file) {
  const raw = await file.arrayBuffer();
  if (raw.byteLength <= SEND_AS_IS_BYTES) return toBase64(new Uint8Array(raw));

  const bitmap = await createImageBitmap(file);
  const scale = Math.min(1, RESIZE_TO_PX / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(bitmap.width * scale));
  canvas.height = Math.max(1, Math.round(bitmap.height * scale));
  canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  return canvas.toDataURL("image/png").split(",")[1];
}

function toBase64(bytes) {
  let binary = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

async function useFile(file) {
  if (!file || !file.type.startsWith("image/")) {
    setSummary("That is not an image this can read.", true);
    return;
  }
  state.filename = file.name;
  state.image = await readImage(file);

  const thumb = $("thumb");
  thumb.src = `data:${file.type};base64,${state.image}`;
  thumb.hidden = false;
  $("drop-copy").hidden = true;
  $("build").disabled = false;
  $("build-hint").textContent = "Check the outline first; building takes about a minute.";
  plan();
}

/* -------------------------------------------------------------------- plan */

let planTimer = null;
function planSoon() {
  if (!state.image) return;
  clearTimeout(planTimer);
  planTimer = setTimeout(plan, 350);
}

async function plan() {
  if (!state.image) return;
  if (state.planning) {
    state.pending = true;
    return;
  }
  state.planning = true;
  $("spinner").hidden = false;

  try {
    const response = await fetch("/v1/keyholder/plan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(settings()),
    });
    const payload = await response.json();
    if (!response.ok) {
      setSummary(payload.detail || "The image could not be traced.", true);
      setNotes([]);
      return;
    }
    showPlan(payload);
  } catch (error) {
    setSummary(`Could not reach the server: ${error}`, true);
  } finally {
    state.planning = false;
    $("spinner").hidden = true;
    if (state.pending) {
      state.pending = false;
      plan();
    }
  }
}

function showPlan(payload) {
  $("empty").hidden = true;
  $("drawing-wrap").hidden = false;
  $("drawing").src = payload.drawing_png;
  setSummary(payload.summary, false);

  const notes = [
    ...(payload.image.notes || []).map((t) => [`image: ${t}`, false]),
    ...(payload.trace.notes || []).map((t) => [`trace: ${t}`, false]),
    ...(payload.plan.notes || []).map((t) => [t, false]),
    ...(payload.plan.warnings || []).map((t) => [t, true]),
  ];
  setNotes(notes);
}

function setSummary(text, bad) {
  const node = $("summary");
  node.textContent = text || "";
  node.classList.toggle("bad", Boolean(bad));
}

function setNotes(notes) {
  const list = $("notes");
  list.replaceChildren();
  for (const [text, warn] of notes) {
    const item = document.createElement("li");
    item.textContent = text;
    if (warn) item.className = "warn";
    list.append(item);
  }
}

/* ------------------------------------------------------------------- build */

async function build() {
  if (!state.image) return;
  $("build").disabled = true;
  $("build-hint").textContent = "Building. The steps are on the right.";
  $("run").hidden = false;
  $("steps").replaceChildren();
  $("verdict").hidden = true;
  $("views").hidden = true;
  $("downloads").hidden = true;

  let submitted;
  try {
    const response = await fetch("/v1/keyholder", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(settings()),
    });
    submitted = await response.json();
    if (!response.ok) throw new Error(submitted.detail || "the server refused the request");
  } catch (error) {
    addStep("failed", false, String(error));
    $("build").disabled = false;
    return;
  }

  state.modelId = submitted.model_id;
  listen(submitted.model_id);
}

function listen(modelId) {
  if (state.socket) state.socket.close();
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${scheme}://${location.host}/v1/models/${modelId}/stream`);
  state.socket = socket;

  socket.onmessage = (message) => {
    const event = JSON.parse(message.data);
    if (event.phase === "keepalive") return;
    if (event.phase === "closed" || event.phase === "done") {
      socket.close();
      finish(modelId);
      return;
    }
    if (event.error) {
      addStep("failed", false, event.error);
      return;
    }
    addStep(event.phase, event.ok !== false, event.message || "");
  };
  socket.onerror = () => addStep("stream", false, "the event stream dropped; polling instead");
  socket.onclose = () => {
    // A close without a terminal event still needs the result fetched: the run
    // continues on the server whatever the browser does.
    if (!$("downloads").hidden) return;
    setTimeout(() => finish(modelId), 200);
  };
}

function addStep(phase, ok, message) {
  const item = document.createElement("li");
  const label = document.createElement("span");
  label.className = "phase";
  label.textContent = phase;
  const body = document.createElement("span");
  body.className = ok ? "ok" : "bad";
  body.textContent = `${ok ? "ok" : "!!"} ${message}`;
  item.append(label, body);
  $("steps").append(item);
}

async function finish(modelId) {
  let model;
  try {
    const response = await fetch(`/v1/models/${modelId}`);
    model = await response.json();
  } catch (error) {
    addStep("failed", false, String(error));
    $("build").disabled = false;
    return;
  }
  if (model.status !== "ok" && model.status !== "failed") return;  // still running

  $("build").disabled = false;
  $("build-hint").textContent = "Change anything and build again.";
  showVerdict(model);
  if (model.status !== "ok") return;
  showViews(modelId, model.previews || {});
  showDownloads(modelId, model.artifacts || {});
}

function showVerdict(model) {
  const report = model.validation || {};
  const summary = report.summary || {};
  const node = $("verdict");
  node.replaceChildren();
  node.hidden = false;
  node.className = model.status === "ok" ? "pass" : "fail";

  const line = document.createElement("strong");
  if (model.status === "ok") {
    const size = (model.stats?.bbox_mm || []).map((v) => Math.round(v)).join(" × ");
    line.textContent = `${summary.checks || 0} checks passed — ${size} mm, ${
      model.stats?.triangles || 0} triangles`;
  } else {
    line.textContent = model.message || "The build did not pass validation.";
  }
  node.append(line);

  const problems = (report.checks || []).filter((c) => !c.passed);
  if (problems.length) {
    const list = document.createElement("ul");
    for (const check of problems.slice(0, 6)) {
      const item = document.createElement("li");
      item.textContent = `${check.id}: ${check.message}`;
      list.append(item);
    }
    node.append(list);
  }
}

const VIEWS = [
  ["trace", "what was traced"],
  ["iso", "isometric"],
  ["front", "front"],
  ["section", "section"],
];

function showViews(modelId, previews) {
  const wrap = $("views");
  wrap.replaceChildren();
  let shown = 0;
  for (const [name, caption] of VIEWS) {
    if (!previews[name]) continue;
    const figure = document.createElement("figure");
    const img = document.createElement("img");
    img.src = `/v1/models/${modelId}/previews/${name}`;
    img.alt = caption;
    const figcaption = document.createElement("figcaption");
    figcaption.textContent = caption;
    figure.append(img, figcaption);
    wrap.append(figure);
    shown += 1;
  }
  wrap.hidden = shown === 0;
}

const FORMATS = [
  ["3mf", "model.3mf", true],
  ["stl", "model.stl", false],
  ["step", "model.step", false],
  ["source", "source.py", false],
  ["report", "report.json", false],
];

function showDownloads(modelId, artifacts) {
  const links = $("links");
  links.replaceChildren();
  for (const [format, label, primary] of FORMATS) {
    if (!artifacts[format]) continue;
    const link = document.createElement("a");
    link.href = `/v1/models/${modelId}/download?format=${format}`;
    link.textContent = label;
    link.download = label;
    if (primary) link.className = "primary-link";
    links.append(link);
  }
  $("downloads").hidden = links.childElementCount === 0;
}

/* ------------------------------------------------------------------- setup */

async function loadProfiles() {
  try {
    const response = await fetch("/v1/profiles");
    const payload = await response.json();
    const select = $("profile");
    for (const profile of payload.profiles) {
      const option = document.createElement("option");
      option.value = profile.id;
      option.textContent = profile.display_name;
      option.selected = profile.id === payload.default;
      select.append(option);
    }
  } catch {
    /* The endpoint is optional to the page: the server's own default applies. */
  }
}

async function loadPosture() {
  try {
    const response = await fetch("/healthz");
    const health = await response.json();
    const sandbox = health.sandbox || {};
    const node = $("posture");
    node.textContent = `sandbox: ${sandbox.runtime || "unknown"}`;
    if (!sandbox.kernel_isolated) {
      node.className = "posture warn";
      node.title =
        "This runtime does not isolate the host kernel. Fine locally; " +
        "it must not serve untrusted traffic.";
      node.textContent += " — local use only";
    }
  } catch {
    /* Nothing to say if the health check itself is unreachable. */
  }
}

function wire() {
  syncOutputs();

  for (const control of Object.values(CONTROLS)) {
    control.el().addEventListener("input", () => {
      syncOutputs();
      planSoon();
    });
  }
  for (const id of ["mount", "detail", "profile", "material", "invert"]) {
    $(id).addEventListener("change", planSoon);
  }
  $("auto-threshold").addEventListener("change", (event) => {
    $("threshold").disabled = event.target.checked;
    syncOutputs();
    planSoon();
  });

  $("file").addEventListener("change", (event) => useFile(event.target.files[0]));

  const drop = $("drop");
  for (const name of ["dragenter", "dragover"]) {
    drop.addEventListener(name, (event) => {
      event.preventDefault();
      drop.classList.add("over");
    });
  }
  for (const name of ["dragleave", "drop"]) {
    drop.addEventListener(name, () => drop.classList.remove("over"));
  }
  drop.addEventListener("drop", (event) => {
    event.preventDefault();
    useFile(event.dataTransfer.files[0]);
  });

  $("build").addEventListener("click", build);

  loadProfiles();
  loadPosture();
}

wire();
