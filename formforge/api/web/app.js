/* FormForge front end.
 *
 * Three rules run through all of it.
 *
 * 1. NOTHING HERE AUTHORISES ANYTHING. Every page fetches from the JSON API
 *    and renders what comes back. A hidden button is a courtesy, never a
 *    control: the same request typed into a terminal hits the same checks.
 *
 * 2. NO innerHTML, EVER, for anything that came from the API or a person.
 *    Only `textContent` and `createElement` below. A prompt is user-authored
 *    text and a model's source is machine-authored code; both are shown as
 *    text, and neither is ever parsed as markup.
 *
 * 3. NOTHING SENSITIVE IS LOGGED. No passwords, reset tokens, download
 *    tokens, session cookies or model contents reach the console. The session
 *    is an HttpOnly cookie this script cannot read even if it wanted to.
 */

(function () {
  "use strict";

  // -- fetch ---------------------------------------------------------------

  /* `credentials: "same-origin"` is the session. There is no Authorization
     header anywhere in this file and no token in storage. */
  async function api(path, options) {
    const opts = Object.assign({ credentials: "same-origin" }, options || {});
    opts.headers = Object.assign({ accept: "application/json" }, opts.headers || {});
    if (opts.body !== undefined && typeof opts.body !== "string") {
      opts.headers["content-type"] = "application/json";
      opts.body = JSON.stringify(opts.body);
    }
    const response = await fetch(path, opts);
    let payload = null;
    if (response.status !== 204) {
      try {
        payload = await response.json();
      } catch (_) {
        payload = null;
      }
    }
    if (!response.ok) {
      throw new ApiError(response.status, payload);
    }
    return payload;
  }

  class ApiError extends Error {
    constructor(status, payload) {
      super(messageFor(status, payload));
      this.status = status;
      this.payload = payload;
    }
  }

  /* The API's own `detail` is written for the person reading it and is
     deliberately vague where being specific would leak something -- "Email or
     password is incorrect" for both a wrong password and no such account.
     Passing it through unchanged is the right thing; inventing a friendlier
     message here would undo the care taken to write it. */
  function messageFor(status, payload) {
    const detail = payload && payload.detail;
    if (typeof detail === "string" && detail) return detail;
    if (Array.isArray(detail) && detail.length) {
      // FastAPI validation errors.
      const first = detail[0];
      const field = Array.isArray(first.loc) ? first.loc[first.loc.length - 1] : "";
      return field ? `${field}: ${first.msg}` : first.msg;
    }
    if (status === 401) return "Please sign in.";
    if (status === 404) return "Not found, or you do not have access to it.";
    if (status === 429) return "Too many attempts. Please wait a few minutes.";
    if (status === 0 || status >= 500) return "The server did not answer. Please try again.";
    return `Request failed (${status}).`;
  }

  // -- DOM helpers ---------------------------------------------------------

  const $ = (sel, root) => (root || document).querySelector(sel);

  function el(tag, attrs, text) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const key in attrs) {
        if (key === "class") node.className = attrs[key];
        else if (key === "dataset") Object.assign(node.dataset, attrs[key]);
        else node.setAttribute(key, attrs[key]);
      }
    }
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function show(box, kind, text) {
    if (!box) return;
    box.className = "msg " + kind;
    box.textContent = text;
    box.hidden = false;
  }

  function hide(box) {
    if (box) box.hidden = true;
  }

  function busy(button, on, label) {
    if (!button) return;
    button.disabled = on;
    if (on) {
      button.dataset.idle = button.dataset.idle || button.textContent;
      button.textContent = label || "Working…";
    } else if (button.dataset.idle) {
      button.textContent = button.dataset.idle;
    }
  }

  function go(path) {
    window.location.assign(path);
  }

  // -- session -------------------------------------------------------------

  async function whoami() {
    try {
      return await api("/v1/auth/me");
    } catch (error) {
      if (error.status === 401) return null;
      throw error;
    }
  }

  /* Pages that need a session ask for one here. A 401 is "sign in required",
     and `next` brings the person back where they were trying to go. */
  async function requireUser() {
    const user = await whoami();
    if (!user) {
      const here = window.location.pathname + window.location.search;
      go("/login?next=" + encodeURIComponent(here));
      return null;
    }
    return user;
  }

  function paintNav(user) {
    const nav = $("#nav");
    if (!nav) return;
    clear(nav);
    if (user) {
      nav.appendChild(el("a", { href: "/dashboard" }, "Dashboard"));
      nav.appendChild(el("a", { href: "/create" }, "Create"));
      nav.appendChild(el("a", { href: "/generators" }, "Generators"));
      nav.appendChild(el("a", { href: "/account" }, "Account"));
      nav.appendChild(el("span", { class: "pill" }, user.credits + " credits"));
    } else {
      nav.appendChild(el("a", { href: "/login" }, "Sign in"));
      nav.appendChild(el("a", { class: "btn ghost", href: "/signup" }, "Create account"));
    }
  }

  function localTime(value) {
    if (!value) return "—";
    const when = new Date(value);
    return isNaN(when.getTime()) ? String(value) : when.toLocaleString();
  }

  function statusPill(status) {
    const done = { ok: "ok", failed: "bad", error: "bad" };
    const kind = done[status] || (status === "queued" || status === "running" ? "work" : "");
    return el("span", { class: "pill " + kind }, status || "unknown");
  }

  // -- pages ---------------------------------------------------------------

  const pages = {};

  pages.home = async function () {
    const user = await whoami();
    paintNav(user);
    const box = $("#state");
    clear(box);
    if (user) {
      box.appendChild(el("p", null, `Signed in as ${user.email} — ${user.plan_name} plan, ${user.credits} credit(s) available.`));
      const row = el("div", { class: "row" });
      row.appendChild(el("a", { class: "btn", href: "/create" }, "Create a model"));
      row.appendChild(el("a", { class: "btn ghost", href: "/dashboard" }, "Dashboard"));
      const out = el("button", { class: "ghost", type: "button" }, "Sign out");
      out.addEventListener("click", async () => {
        busy(out, true, "Signing out…");
        try {
          await api("/v1/auth/logout", { method: "POST" });
        } catch (_) {
          /* The cookie is cleared server-side either way; reload settles it. */
        }
        go("/");
      });
      row.appendChild(out);
      box.appendChild(row);
    } else {
      box.appendChild(el("p", null, "You are not signed in."));
      const row = el("div", { class: "row" });
      row.appendChild(el("a", { class: "btn", href: "/signup" }, "Create account"));
      row.appendChild(el("a", { class: "btn ghost", href: "/login" }, "Sign in"));
      box.appendChild(row);
    }
  };

  pages.signup = function () {
    paintNav(null);
    const form = $("#form");
    const err = $("#err");
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      hide(err);
      const email = $("#email").value.trim();
      const password = $("#password").value;
      if (!email || email.indexOf("@") < 1) {
        return show(err, "err", "Enter an email address.");
      }
      if (password.length < 10) {
        return show(err, "err", "Password must be at least 10 characters.");
      }
      const button = $("#submit");
      busy(button, true, "Creating…");
      try {
        await api("/v1/auth/signup", { method: "POST", body: { email, password } });
        go("/dashboard");
      } catch (error) {
        show(err, "err", error.message);
        busy(button, false);
      }
    });
  };

  pages.login = function () {
    paintNav(null);
    const form = $("#form");
    const err = $("#err");
    const params = new URLSearchParams(window.location.search);
    /* Only a same-origin path is ever followed, so a crafted ?next= cannot
       bounce somebody off this site after they sign in. */
    const raw = params.get("next") || "/dashboard";
    const next = raw.startsWith("/") && !raw.startsWith("//") ? raw : "/dashboard";
    if (params.get("reset") === "1") {
      show($("#note"), "ok", "Password changed. Sign in with your new password.");
    }
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      hide(err);
      const email = $("#email").value.trim();
      const password = $("#password").value;
      if (!email || !password) return show(err, "err", "Enter your email and password.");
      const button = $("#submit");
      busy(button, true, "Signing in…");
      try {
        await api("/v1/auth/login", { method: "POST", body: { email, password } });
        go(next);
      } catch (error) {
        show(err, "err", error.message);
        busy(button, false);
      }
    });
  };

  pages.dashboard = async function () {
    const user = await requireUser();
    if (!user) return;
    paintNav(user);

    $("#email").textContent = user.email;
    $("#plan").textContent = user.plan_name + (user.plan_status === "active" ? "" : ` (${user.plan_status})`);
    $("#credits").textContent = user.credits;
    $("#formats").textContent = user.formats.join(", ").toUpperCase();

    const list = $("#models");
    clear(list);
    let history;
    try {
      history = await api("/v1/account/history?limit=25");
    } catch (error) {
      return show($("#err"), "err", error.message);
    }
    const models = (history && history.models) || [];
    if (!models.length) {
      const empty = el("div", { class: "empty" });
      empty.appendChild(el("p", null, "No models yet."));
      empty.appendChild(el("a", { class: "btn", href: "/create" }, "Create your first model"));
      list.appendChild(empty);
      return;
    }
    const ul = el("ul", { class: "plain" });
    for (const model of models) {
      const li = el("li");
      const link = el("a", { class: "item", href: "/models/" + encodeURIComponent(model.model_id) });
      const head = el("div", { class: "row" });
      head.appendChild(el("span", { class: "title" }, model.template || "freeform"));
      head.appendChild(statusPill(model.status));
      if (model.paid) head.appendChild(el("span", { class: "pill ok" }, "paid"));
      link.appendChild(head);
      link.appendChild(el("div", { class: "meta mono" }, model.model_id));
      link.appendChild(el("div", { class: "meta" }, localTime(model.created_at)));
      li.appendChild(link);
      ul.appendChild(li);
    }
    list.appendChild(ul);
  };

  pages.create = async function () {
    const user = await requireUser();
    if (!user) return;
    paintNav(user);

    if (user.credits <= 0) {
      show($("#note"), "err",
        "You have no credits left. A build is charged only when it succeeds, so nothing will run until your balance is topped up.");
    }

    // Real printer profiles from the API rather than a hard-coded list, so
    // this cannot drift from what the server will accept.
    const select = $("#profile");
    try {
      const data = await api("/v1/profiles");
      const profiles = (data && data.profiles) || [];
      for (const profile of profiles) {
        const id = profile.id || profile;
        const label = profile.display_name ? `${profile.display_name} (${id})` : id;
        const option = el("option", { value: id }, label);
        if (id === data.default) option.selected = true;
        select.appendChild(option);
      }
      select.disabled = profiles.length === 0;
    } catch (_) {
      select.appendChild(el("option", { value: "generic_fdm_0.4" }, "generic_fdm_0.4"));
    }

    const form = $("#form");
    const err = $("#err");
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      hide(err);
      const prompt = $("#prompt").value.trim();
      if (prompt.length < 3) return show(err, "err", "Describe what you want to make.");
      if (prompt.length > 2000) return show(err, "err", "Keep the description under 2000 characters.");

      const body = {
        prompt: prompt,
        material: $("#material").value.trim() || "PLA",
        interactive: false,
      };
      if (select.value) body.printer_profile = select.value;

      const button = $("#submit");
      busy(button, true, "Starting the build…");
      try {
        const job = await api("/v1/generate", { method: "POST", body: body });
        go("/models/" + encodeURIComponent(job.model_id));
      } catch (error) {
        show(err, "err", error.message);
        busy(button, false);
      }
    });
  };

  pages.generators = async function () {
    const user = await requireUser();
    if (!user) return;
    paintNav(user);

    const err = $("#err");
    const note = $("#note");
    const setup = $("#setup");
    const results = $("#results");
    let chosen = null;

    let catalogue;
    try {
      catalogue = await api("/v1/generators");
    } catch (error) {
      return show(err, "err", error.message);
    }

    const box = $("#catalogue");
    clear(box);
    const ul = el("ul", { class: "plain" });
    for (const generator of catalogue.generators || []) {
      const li = el("li");
      const button = el("button", { class: "ghost item", type: "button",
                                    style: "width:100%;text-align:left" });
      const head = el("div", { class: "row" });
      head.appendChild(el("span", { class: "title" }, generator.name));
      head.appendChild(el("span", { class: "pill" },
        generator.variants.length + " " + generator.variant_noun +
        (generator.variants.length === 1 ? "" : "s")));
      button.appendChild(head);
      button.appendChild(el("div", { class: "meta" }, generator.summary));
      button.addEventListener("click", () => choose(generator));
      li.appendChild(button);
      ul.appendChild(li);
    }
    box.appendChild(ul);

    function choose(generator) {
      chosen = generator;
      hide(err);
      hide(note);
      clear(results);
      setup.hidden = false;
      $("#setup-title").textContent = generator.name;
      $("#setup-summary").textContent = generator.summary;
      // The domain's own word for the control: a mushroom has species, a
      // vase has styles. The API sends the noun so the label is right
      // without a lookup table here.
      const noun = generator.variant_noun;
      $("#variant-label").textContent = noun.charAt(0).toUpperCase() + noun.slice(1);
      const select = $("#variant");
      clear(select);
      for (const variant of generator.variants) {
        select.appendChild(el("option", { value: variant }, variant.replace(/_/g, " ")));
      }
      const count = $("#count");
      count.max = String(Math.max(1, Math.min(24, user.credits)));
      $("#count-hint").textContent = user.credits > 0
        ? `One credit per model that builds. You have ${user.credits}.`
        : "You have no credits left, so nothing will build.";
      setup.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }

    $("#form").addEventListener("submit", async (event) => {
      event.preventDefault();
      hide(err);
      hide(note);
      if (!chosen) return show(err, "err", "Pick a generator first.");
      const count = parseInt($("#count").value, 10);
      if (!(count >= 1)) return show(err, "err", "Build at least one.");
      const body = {
        count: count,
        variant: $("#variant").value,
        seed: parseInt($("#seed").value, 10) || 0,
        variation: parseFloat($("#variation").value),
      };
      const button = $("#submit");
      busy(button, true, "Building…");
      let batch;
      try {
        batch = await api("/v1/generators/" + encodeURIComponent(chosen.name),
                          { method: "POST", body: body });
      } catch (error) {
        show(err, "err", error.message);
        busy(button, false);
        return;
      }
      paintBatch(batch);
      poll(batch, button);
    });

    function paintBatch(batch) {
      clear(results);
      results.appendChild(el("h2", null, "Batch"));
      const summary = el("p", { class: "sub" },
        `${batch.queued} of ${batch.requested} queued · seed ${batch.seed}`);
      results.appendChild(summary);
      const list = el("ul", { class: "plain", id: "specimens" });
      for (const specimen of batch.specimens) {
        const li = el("li", { dataset: { index: String(specimen.index) } });
        li.appendChild(specimenCard(specimen));
        list.appendChild(li);
      }
      results.appendChild(list);
    }

    function specimenCard(specimen) {
      const node = specimen.model_id
        ? el("a", { class: "item", href: "/models/" + encodeURIComponent(specimen.model_id) })
        : el("div", { class: "item", style: "cursor:default" });
      const head = el("div", { class: "row" });
      const label = chosen ? specimen[chosen.variant_noun] || specimen.variant : specimen.variant;
      head.appendChild(el("span", { class: "title" },
        "#" + (specimen.index + 1) + (label ? " · " + String(label).replace(/_/g, " ") : "")));
      head.appendChild(statusPill(specimen.status));
      node.appendChild(head);
      if (specimen.seed !== undefined && specimen.seed !== null) {
        node.appendChild(el("div", { class: "meta" }, "seed " + specimen.seed));
      }
      if (specimen.detail) node.appendChild(el("div", { class: "meta" }, specimen.detail));
      if (specimen.model_id) {
        node.appendChild(el("div", { class: "meta mono" }, specimen.model_id));
      }
      return node;
    }

    /* Each specimen is an ordinary model, so its progress is read from the
       ordinary model endpoint rather than anything batch-specific. */
    function poll(batch, button) {
      const live = batch.specimens.filter((s) => s.model_id);
      if (!live.length) {
        busy(button, false);
        return show(note, "err", "Nothing was built: every specimen fell outside the template's tested range.");
      }
      show(note, "info", "Building. Each specimen becomes a model of its own.");

      async function tick() {
        let settled = 0;
        for (const specimen of live) {
          if (specimen.status === "ok" || specimen.status === "failed") { settled++; continue; }
          try {
            const model = await api("/v1/models/" + encodeURIComponent(specimen.model_id));
            specimen.status = model.status || specimen.status;
          } catch (_) {
            /* Transient; the next tick asks again. */
          }
          const li = $(`#specimens li[data-index="${specimen.index}"]`);
          if (li) { clear(li); li.appendChild(specimenCard(specimen)); }
        }
        if (settled === live.length) {
          busy(button, false);
          const built = live.filter((s) => s.status === "ok").length;
          show(note, built ? "ok" : "err",
            `${built} of ${live.length} built. Credits are charged per model that succeeded.`);
          // The balance moved, so the header should say so.
          try { paintNav(await api("/v1/auth/me")); } catch (_) { /* ignore */ }
          return;
        }
        setTimeout(tick, 2500);
      }
      setTimeout(tick, 2000);
    }
  };

  pages.model = async function () {
    const user = await requireUser();
    if (!user) return;
    paintNav(user);

    const parts = window.location.pathname.split("/");
    const modelId = decodeURIComponent(parts[parts.length - 1] || "");
    if (!modelId) return show($("#err"), "err", "No model in the address.");
    $("#id").textContent = modelId;

    const err = $("#err");
    let model = null;

    async function load() {
      try {
        model = await api("/v1/models/" + encodeURIComponent(modelId));
        hide(err);
        paint();
      } catch (error) {
        if (error.status === 404) {
          show(err, "err", "Not found, or you do not have access to it.");
        } else {
          show(err, "err", error.message);
        }
        $("#body").hidden = true;
      }
    }

    function paint() {
      $("#body").hidden = false;
      const status = model.status || "unknown";
      // While a build is running the endpoint answers 202 with the job's
      // status rather than a result, so the detail fields are simply absent.
      const running = status === "queued" || status === "running";
      const pill = $("#status");
      clear(pill);
      pill.appendChild(statusPill(status));

      $("#prompt").textContent = model.prompt || (running ? "Building…" : "—");
      $("#template").textContent = model.template_id || model.template || "freeform";
      $("#created").textContent = model.created_at ? localTime(model.created_at) : "—";
      $("#phase").textContent = running
        ? [model.phase, model.step].filter(Boolean).join(" · ") || "starting"
        : "";

      // Parameters as text. `JSON.stringify` into `textContent`: never parsed
      // as markup, whatever a template happens to contain.
      const params = model.params || {};
      $("#params").textContent = Object.keys(params).length
        ? JSON.stringify(params, null, 2)
        : "No parameters recorded for this model.";

      const warnings = (model.validation && model.validation.warnings) || [];
      const warnBox = $("#warnings");
      clear(warnBox);
      if (warnings.length) {
        warnBox.appendChild(el("h3", null, "Validation warnings"));
        const ul = el("ul");
        for (const warning of warnings) {
          ul.appendChild(el("li", null, warning.message || String(warning)));
        }
        warnBox.appendChild(ul);
        warnBox.hidden = false;
      } else {
        warnBox.hidden = true;
      }

      if (status === "failed" || status === "error") {
        show($("#note"), "err",
          (model.error || "This build did not finish.") +
          " Nothing was charged: a credit is taken only when validation passes.");
      } else if (running) {
        show($("#note"), "info", "This build is still running. The page refreshes itself.");
        setTimeout(load, 3000);
      } else {
        hide($("#note"));
      }

      paintDownloads(status);
      $("#modify-card").hidden = status !== "ok";
    }

    /* Only the formats this account's plan allows, from `/v1/auth/me`. The
       server checks again when the link is minted -- this just avoids
       offering a button that is going to be refused. */
    function paintDownloads(status) {
      const box = $("#downloads");
      clear(box);
      if (status !== "ok") {
        box.appendChild(el("p", { class: "sub" }, "Downloads appear once the build finishes."));
        return;
      }
      const allowed = user.formats.slice();
      // The report and the parameters are metadata about your own build and
      // are never paywalled; the server says the same.
      for (const extra of ["report", "params"]) {
        if (allowed.indexOf(extra) === -1) allowed.push(extra);
      }
      const row = el("div", { class: "row" });
      for (const format of allowed) {
        const button = el("button", { class: "ghost", type: "button" }, format.toUpperCase());
        button.addEventListener("click", () => download(format, button));
        row.appendChild(button);
      }
      box.appendChild(row);
      const locked = ["3mf", "stl", "step", "source"].filter((f) => allowed.indexOf(f) === -1);
      if (locked.length) {
        box.appendChild(el("p", { class: "hint" },
          `Not included in the ${user.plan_name} plan: ${locked.join(", ").toUpperCase()}.`));
      }
    }

    async function download(format, button) {
      hide(err);
      busy(button, true, "…");
      try {
        const link = await api(
          "/v1/models/" + encodeURIComponent(modelId) +
          "/download-link?format=" + encodeURIComponent(format),
          { method: "POST" }
        );
        // Straight to the signed URL the server returned. The token is never
        // logged and never stored.
        window.location.assign(link.url);
      } catch (error) {
        if (error.status === 402) {
          show(err, "err", "This model has not been paid for yet.");
        } else if (error.status === 403) {
          show(err, "err", error.message);
        } else if (error.status === 404) {
          show(err, "err", "That file does not exist for this model.");
        } else if (error.status === 410) {
          show(err, "err", "This file has been deleted under the retention policy.");
        } else {
          show(err, "err", error.message);
        }
      } finally {
        busy(button, false);
      }
    }

    $("#modify").addEventListener("submit", async (event) => {
      event.preventDefault();
      const modErr = $("#mod-err");
      hide(modErr);
      const text = $("#changes").value.trim();
      if (!text) return show(modErr, "err", "Enter the parameters to change.");
      let changes;
      try {
        changes = JSON.parse(text);
      } catch (_) {
        return show(modErr, "err", 'That is not valid JSON. Example: {"height_mm": 90}');
      }
      if (!changes || typeof changes !== "object" || Array.isArray(changes)) {
        return show(modErr, "err", 'Parameters must be a JSON object, e.g. {"height_mm": 90}');
      }
      const button = $("#mod-submit");
      busy(button, true, "Rebuilding…");
      try {
        const job = await api(
          "/v1/models/" + encodeURIComponent(modelId) + "/modify",
          { method: "POST", body: { param_changes: changes } }
        );
        if (job && job.model_id && job.model_id !== modelId) {
          go("/models/" + encodeURIComponent(job.model_id));
          return;
        }
        show(modErr, "ok", "Rebuild started.");
        busy(button, false);
        load();
      } catch (error) {
        show(modErr, "err", error.message);
        busy(button, false);
      }
    });

    load();
  };

  pages.forgot = function () {
    paintNav(null);
    const form = $("#form");
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const email = $("#email").value.trim();
      const button = $("#submit");
      busy(button, true, "Sending…");
      try {
        await api("/v1/auth/reset/request", { method: "POST", body: { email } });
      } catch (_) {
        /* Deliberately ignored. The endpoint answers the same way whether or
           not the address has an account, and surfacing an error here would
           undo that. */
      }
      /* One message, always. It says nothing about whether that address has
         an account -- which is the whole point of the endpoint. */
      form.hidden = true;
      show($("#note"), "ok",
        "If that address has an account, a reset link is on its way. The link works once and expires in 30 minutes.");
      $("#local").hidden = false;
    });
  };

  pages.reset = function () {
    paintNav(null);
    const params = new URLSearchParams(window.location.search);
    const token = params.get("token") || "";
    const form = $("#form");
    const err = $("#err");
    if (!token) {
      form.hidden = true;
      return show(err, "err",
        "This page needs the reset link from your email. Open the link itself rather than typing the address.");
    }
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      hide(err);
      const password = $("#password").value;
      const again = $("#again").value;
      if (password.length < 10) return show(err, "err", "Password must be at least 10 characters.");
      if (password !== again) return show(err, "err", "The two passwords do not match.");
      const button = $("#submit");
      busy(button, true, "Saving…");
      try {
        await api("/v1/auth/reset/confirm", { method: "POST", body: { token, password } });
        // Every session for the account is now dead, including any on this
        // browser, so the only next step is signing in again.
        go("/login?reset=1");
      } catch (error) {
        show(err, "err",
          error.status === 400 ? "This reset link is invalid or has expired." : error.message);
        busy(button, false);
      }
    });
  };

  pages.account = async function () {
    const user = await requireUser();
    if (!user) return;
    paintNav(user);

    $("#email").textContent = user.email;
    $("#plan").textContent = user.plan_name;
    $("#plan-status").textContent = user.plan_status;
    $("#credits").textContent = user.credits;
    $("#formats").textContent = user.formats.join(", ").toUpperCase();
    $("#batch").textContent = user.batch;

    const ledger = $("#ledger");
    try {
      const data = await api("/v1/account/credits?limit=15");
      const rows = (data && data.history) || [];
      clear(ledger);
      if (!rows.length) {
        ledger.appendChild(el("p", { class: "sub" }, "No credit activity yet."));
      } else {
        const ul = el("ul", { class: "plain" });
        for (const row of rows) {
          const li = el("li", { class: "item" });
          const head = el("div", { class: "row" });
          head.appendChild(el("span", { class: "title" },
            (row.change > 0 ? "+" : "") + row.change));
          head.appendChild(el("span", { class: "sub" }, row.description || row.kind || ""));
          li.appendChild(head);
          if (row.model_id) li.appendChild(el("div", { class: "meta mono" }, row.model_id));
          li.appendChild(el("div", { class: "meta" }, localTime(row.when)));
          ul.appendChild(li);
        }
        ledger.appendChild(ul);
      }
    } catch (error) {
      clear(ledger);
      ledger.appendChild(el("p", { class: "sub" }, error.message));
    }

    const out = $("#logout");
    out.addEventListener("click", async () => {
      busy(out, true, "Signing out…");
      try {
        await api("/v1/auth/logout", { method: "POST" });
      } catch (_) {
        /* Cleared server-side regardless. */
      }
      go("/");
    });
  };

  // -- boot ----------------------------------------------------------------

  document.addEventListener("DOMContentLoaded", function () {
    const name = document.body.dataset.page;
    const page = pages[name];
    if (!page) return;
    Promise.resolve()
      .then(page)
      .catch(function (error) {
        // A message, not a stack trace, and nothing from the response body
        // beyond the API's own `detail`.
        show($("#err") || $("#note"), "err",
          error instanceof ApiError ? error.message : "Something went wrong loading this page.");
      });
  });
})();
