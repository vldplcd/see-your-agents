// Data source for the static demo page: invented sessions of an invented team, no server.
// demo/build.py injects this before the app script; the app picks it up as window.SYA_API.
// Texts the real server words from a catalog (column titles, route reasons, built-in tools' notes) come from t().
(() => {
  const MIN = 60000;
  const t0 = Date.now();
  // Times are anchored to page load, so an unchanged session renders the same and the page doesn't redraw it.
  const iso = (minutesAgo) => new Date(t0 - minutesAgo * MIN).toISOString();
  const at = (sec) => -sec / 60;  // "minutes ago" of a moment `sec` seconds after page load
  const JIRA = "https://jira.example.com";
  const GIT = "https://git.example.com/acme";
  const mr = (repo, n) => ({ url: `${GIT}/${repo}/-/merge_requests/${n}`, number: n, repo: `acme/${repo}` });
  const task = (subject, status, active) => ({ subject, status, active_form: active || subject });
  const SUP = "5e1d0017-7c2a-4b19-9d0e-2f6a8c3b0017";  // = uuid(17), the supervisor session
  const uuid = (n) => `5e1d${String(n).padStart(4, "0")}-7c2a-4b19-9d0e-2f6a8c3b${String(n).padStart(4, "0")}`;

  const PLAN_REFUNDS = `<!doctype html><meta charset="utf-8"><title>DATA-210</title><style>
:root{--bg:#ffffff;--fg:#1c2530;--mute:#5d6b78;--line:#d9dee4;--done:#178a7e;--wait:#b7791f;--chip:#eef1f4}
@media (prefers-color-scheme:dark){:root{--bg:#1e2630;--fg:#e6ebf0;--mute:#9aa6b2;--line:#33404d;--done:#3cc2b2;--wait:#e0a54a;--chip:#273240}}
body{margin:0;padding:28px clamp(16px,4vw,48px) 48px;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,-apple-system,sans-serif}
h1{font-size:24px;line-height:1.25;margin:0 0 8px;text-wrap:balance}p{max-width:68ch;color:var(--mute)}
ol{list-style:none;padding:0;margin:24px 0;display:grid;gap:10px;max-width:760px}
li{border:1px solid var(--line);border-radius:10px;padding:12px 14px;display:grid;grid-template-columns:28px 1fr auto;gap:4px 12px;align-items:start}
li b{display:block}li span.n{font-weight:700;color:var(--mute)}li small{color:var(--mute)}
.st{font-size:12px;padding:2px 8px;border-radius:6px;background:var(--chip);white-space:nowrap}
.done{color:var(--done)}.wait{color:var(--wait);font-weight:600}
table{border-collapse:collapse;font-size:14px;margin-top:8px}td,th{border-bottom:1px solid var(--line);padding:6px 12px 6px 0;text-align:left}
code{background:var(--chip);padding:1px 5px;border-radius:4px;font-size:13px}
</style>
<h1>DATA-210: a refunds table in the data lake</h1>
<p>Refunds live only in the payment service’s Postgres today. We’re moving them to Iceberg so finance can compute net revenue without manual exports.</p>
<ol>
<li><span class="n">1</span><div><b>Schema and migration</b><small>Table <code>finance.refunds</code>, partitioned by refund date</small></div><span class="st done">done</span></li>
<li><span class="n">2</span><div><b>CDC loader</b><small>Debezium → Kafka → Iceberg, deduplicated by <code>refund_id</code></small></div><span class="st wait">awaiting approval</span></li>
<li><span class="n">3</span><div><b>Backfill from January 2024</b><small>One month per run, totals reconciled against Postgres</small></div><span class="st">not started</span></li>
<li><span class="n">4</span><div><b>Net revenue dashboard</b><small>Revenue minus refunds by day and country</small></div><span class="st">not started</span></li>
</ol>
<table><tr><th>File</th><th>What changes</th></tr>
<tr><td><code>schemas/finance/refunds.sql</code></td><td>new table</td></tr>
<tr><td><code>dags/refunds_cdc.py</code></td><td>new loader DAG</td></tr>
<tr><td><code>tests/test_refunds_dedup.py</code></td><td>duplicates and late events</td></tr></table>`;

  const PLAN_WEBHOOKS = `Plan: webhook secret rotation (PAY-412)

1. Add a second secret to vault without touching the first one.
2. Verify signatures against both secrets (dual verification), released behind a flag.
3. Apply on staging and run e2e against the provider’s real sandbox.
4. Switch the provider to the new secret.
5. After 24 hours without errors, remove the old secret and the flag.

Risks: webhooks that arrive during the switch are signed with the old secret, so step 2 has to ship before step 4.`;

  // Statuses in `live`: busy | shell | idle | waiting (+ waiting_for) | blocked. `null` = process has ended.
  const sessions = [
    {
      n: 1, title: "Webhook secret rotation", project: "payments-api", branch: "pay-412-webhook-rotation",
      keys: ["PAY-412"], prs: [mr("payments-api", 221)], cost: 4.12, lines: [186, 41], last: 3, started: 95,
      live: { status: "waiting", waiting_for: "permission prompt", since: 3 },
      recap: "The second secret is in vault, and dual signature verification is ready behind a flag. Next step: apply on staging, which needs your permission for kubectl apply.",
      tasks: [task("Add a second secret", "completed"), task("Dual signature verification", "completed"),
        task("Tests for both secrets", "completed"), task("Apply on staging", "in_progress", "Applying on staging"),
        task("Switch the provider", "pending"), task("Remove the old secret", "pending")],
      plans: [{ m: 80, text: PLAN_WEBHOOKS }],
      agents: [[70, "Review signature verification", "code-reviewer"]],
      events: [[95, "prompt", "We need to rotate the payment provider’s webhook secret with no downtime. PAY-412"],
        [80, "plan", "Plan: webhook secret rotation (PAY-412)"], [40, "pr", `${GIT}/payments-api/-/merge_requests/221`],
        [4, "msg_in", "From the supervisor: before applying on staging, keep MR !214 in mind, it changes the idempotency keys."],
        [3, "recap", null]],
      tools: [["Bash", 38], ["Edit", 14], ["Read", 22]],
      peer: { dir: "in", who: SUP, m: 4, text: "Before applying on staging, keep MR !214 in mind: it changes the idempotency keys, so don’t overwrite them." },
    },
    {
      n: 2, title: "Event format for analytics", project: "data-pipelines", branch: "data-207-events",
      keys: ["DATA-207"], prs: [], cost: 1.87, lines: [0, 0], last: 11, started: 40,
      live: { status: "waiting", waiting_for: "input needed", since: 11 },
      recap: "Compared two ways to store events: one wide table versus a table per type. Waiting for your pick, since the schema depends on it.",
      questions: [{ m: 11, answer: null, items: [{ header: "Format", question: "Should events go into one wide table or one table per event type?", options: ["One wide table (Recommended)", "A table per type"] }] }],
      events: [[40, "prompt", "Suggest how to store product events in the data lake, DATA-207"],
        [11, "question", "Should events go into one wide table or one table per event type?"]],
      tools: [["Read", 18], ["Bash", 6]],
    },
    {
      n: 3, title: "Dependency update for mobile-app", project: "mobile-app", branch: "mob-131-deps",
      keys: ["MOB-131"], prs: [mr("mobile-app", 77)], cost: 6.4, lines: [512, 498], last: 52, started: 300,
      live: { status: "blocked", kind: "background", job_id: "7f3c21ab", since: 52,
        needs: "Please confirm: (1) do we raise minSdk to 26? (2) do we do the major react-navigation upgrade in this same MR?",
        suggested_reply: "minSdk 26 is fine, react-navigation in a separate MR" },
      recap: "Updated 23 packages; the build and tests are green. Two decisions are left, and both change which devices we support.",
      events: [[300, "prompt", "Update the mobile-app dependencies, MOB-131, no major versions if you can avoid them"],
        [52, "recap", null]],
      tools: [["Bash", 61], ["Edit", 30]],
    },
    {
      n: 4, title: "Cart on server components", project: "web-checkout", branch: "web-981-rsc-cart",
      peer: { dir: "out", who: uuid(8), m: 9, text: "Discounts on refunds are calculated in lib/discounts.ts. Does refunds need a discount_total column?" },
      keys: ["WEB-981"], prs: [], cost: 3.05, lines: [240, 310], last: 0.5, started: 70,
      live: { status: "busy", since: 6 },
      recap: "Moved the cart and the order summary to the server, and the client bundle is 38 KB smaller. Now moving the discount calculation.",
      tasks: [task("Cart on the server", "completed"), task("Order summary", "completed"),
        task("Discount calculation", "in_progress", "Moving the discount calculation to the server"), task("E2E tests", "pending"), task("Bundle size check", "pending")],
      agents: [[30, "Find client-only imports", "Explore"], [12, "Review discount edge cases", "general-purpose"]],
      events: [[70, "prompt", "Move the cart to server components, WEB-981"], [30, "agent", "Find client-only imports (Explore)"],
        [12, "agent", "Review discount edge cases (general-purpose)"], [8, "recap", null]],
      tools: [["Edit", 44], ["Read", 51], ["Bash", 19]],
    },
    {
      n: 5, title: "September orders backfill", project: "data-pipelines", branch: "data-199-backfill",
      keys: ["DATA-199"], prs: [], cost: 2.2, lines: [12, 2], last: 1, started: 140,
      live: { status: "shell", since: 14 },
      recap: "Backfilling September 12–18: 4 of 7 days loaded, row counts match the source.",
      events: [[140, "prompt", "Backfill the orders for September 12–18 after the loader went down, DATA-199"], [14, "recap", null]],
      tools: [["Bash", 27], ["Monitor", 3]],
    },
    {
      n: 6, title: "Flaky CI tests", project: "infra", branch: "ops-77-flaky",
      keys: ["OPS-77"], prs: [mr("infra", 58)], cost: 1.1, lines: [34, 9], last: 2, started: 45,
      live: { status: "busy", since: 4 },
      recap: "Found three tests that depend on the run order. Two are fixed; working on the third.",
      events: [[45, "prompt", "Why does CI fail every other run? OPS-77"], [20, "pr", `${GIT}/infra/-/merge_requests/58`], [10, "recap", null]],
      tools: [["Bash", 33], ["Read", 12]],
    },
    {
      n: 7, title: "MR !214 review: payment idempotency", project: "payments-api", branch: "master",
      peer: { dir: "out", who: uuid(1), m: 7, text: "The !214 review is done, no blocking issues. Better leave the idempotency keys as they are." },
      keys: ["PAY-405"], prs: [mr("payments-api", 214)], cost: 2.9, lines: [0, 0], last: 125, started: 190,
      live: { status: "idle", since: 125 },
      recap: "Review done: 2 MAJOR (reprocessing on timeout, no unique index) and 3 MINOR. Your move: decide whether to post the comments to the MR.",
      agents: [[170, "QA review of MR 214", "code-reviewer"], [168, "Architecture review of MR 214", "architect"]],
      events: [[190, "prompt", "Review MR !214, PAY-405"], [125, "recap", null]],
      tools: [["Bash", 20], ["Read", 40], ["Agent", 2]],
    },
    {
      n: 8, title: "Schema for the refunds table", project: "data-pipelines", branch: "data-210-refunds",
      peer: { dir: "in", who: uuid(4), m: 9, text: "Discounts on refunds are calculated in lib/discounts.ts. Does refunds need a discount_total column?" },
      keys: ["DATA-210"], prs: [mr("data-pipelines", 132)], cost: 5.6, lines: [301, 12], last: 190, started: 420,
      live: { status: "idle", since: 190 },
      recap: "The four-phase plan is agreed, phase 1 (schema and migration) is done, and MR !132 is green. Your move: approve phase 2.",
      plans: [{ m: 400, path: "/Users/dev/work/data-pipelines/plans/data-210-refunds.mdx", html: PLAN_REFUNDS }],
      events: [[420, "prompt", "We need refunds in the data lake, DATA-210. Plan first."],
        [400, "plan", "/Users/dev/work/data-pipelines/plans/data-210-refunds.mdx"], [260, "pr", `${GIT}/data-pipelines/-/merge_requests/132`], [190, "recap", null]],
      tools: [["Write", 6], ["Edit", 21], ["Bash", 30]],
    },
    {
      n: 9, title: "Queue lag alerts", project: "infra", branch: "ops-81-queue-lag",
      keys: ["OPS-81"], prs: [mr("infra", 56)], done: true, cost: 0.94, lines: [48, 3], last: 310, started: 360,
      live: { status: "idle", since: 310 },
      recap: "The queue lag alert is in terraform and the plan is clean. Your move: merge MR !56 and apply on prod.",
      tools: [["Bash", 14], ["Edit", 5]],
    },
    {
      n: 10, title: "New checkout page: A/B", project: "web-checkout", branch: "web-975-checkout-v2",
      keys: ["WEB-975"], prs: [mr("web-checkout", 88)], cost: 7.3, lines: [880, 120], last: 1560, started: 1900,
      live: { status: "idle", since: 1560 },
      recap: "Variant B is built behind the checkout_v2 flag, with screenshots in MR !88. Your move: check it on staging and decide whether to turn it on for 10% of traffic.",
      tools: [["Edit", 60], ["Read", 70], ["Bash", 25]],
    },
    {
      n: 11, title: "Nightly orders_daily DAG failure", project: "data-pipelines", branch: "master",
      keys: ["DATA-201"], prs: [], cost: 0.61, lines: [2, 1], last: 1220, started: 1290, live: null,
      recap: "The cause was an expired service account token. The token is renewed, the DAG reran green, and the ticket is closed.",
      tools: [["Bash", 16]],
    },
    {
      n: 17, title: "Supervisor", project: "see-your-agents", branch: "master", keys: [], prs: [], cost: 2.4, lines: [0, 0], last: 6, started: 240,
      live: { status: "idle", since: 6 },
      recap: "Put together a summary of the sessions: two are waiting for you, and I sent tasks to two.",
      events: [[240, "prompt", "Keep an eye on my sessions, send me a summary every hour, and hand out tasks if something is stuck."],
        [26, "msg_out", "To “Webhook secret rotation”: keep MR !214 in mind."], [11, "msg_out", "To “MR !214 review”: send the review results to the webhooks session."],
        [6, "recap", null]],
      tools: [["Bash", 12]],
    },
    {
      n: 12, title: "README for payments-api", project: "payments-api", branch: "pay-399-readme",
      keys: ["PAY-399"], prs: [mr("payments-api", 209)], cost: 0.48, lines: [140, 20], last: 2900, started: 2960, live: null,
      recap: "The README is rewritten: running locally, environment variables, how to run e2e. MR !209 is merged.",
      tools: [["Read", 25], ["Write", 1]],
    },
    { n: 13, title: "Migration to Postgres 16", project: "infra", branch: "ops-60-pg16", keys: ["OPS-60"], prs: [mr("infra", 41)],
      cost: 31.2, lines: [420, 380], last: 13000, started: 14500, live: null,
      recap: "The migration went through on all three clusters; no rollback was needed." },
    { n: 14, title: "Delivery push notifications", project: "mobile-app", branch: "mob-118-push", keys: ["MOB-118"],
      prs: [mr("mobile-app", 70)], cost: 9.8, lines: [640, 90], last: 17300, started: 18000, live: null,
      recap: "Delivery status notifications work on iOS and Android, and MR !70 is merged." },
    { n: 15, title: "Exchange rate cache", project: "payments-api", branch: "master", keys: ["PAY-371"], prs: [],
      cost: 1.5, lines: [0, 0], last: 24500, started: 24600, live: null,
      recap: "Research: a 60-second cache removes 92% of the requests to the rates provider. Decision postponed." },
    { n: 16, title: "Broken CSV export", project: "web-checkout", branch: "web-940-csv", keys: ["WEB-940"],
      prs: [mr("web-checkout", 71)], cost: 0.7, lines: [18, 4], last: 30300, started: 30400, live: null,
      recap: "The export broke on commas in product names; the escaping is fixed." },
  ];

  // The demo moves on its own: a CI session asks for permission and the backfill progresses.
  function simulate() {
    const sec = (Date.now() - t0) / 1000;
    const ci = sessions.find((s) => s.n === 6);
    const cycle = sec - (sec % 70);  // every 70 s the CI agent asks for permission between seconds 12 and 40
    const asking = sec % 70 > 12 && sec % 70 < 40;
    ci.live = asking ? { status: "waiting", waiting_for: "permission prompt", since: at(cycle + 12) }
      : { status: "busy", since: at(sec % 70 < 12 ? cycle - 30 : cycle + 40) };
    ci.recap = asking
      ? "The third flaky test is fixed by retrying the request to the mock. I want to rerun the CI pipeline and need permission for gh workflow run."
      : "Found three tests that depend on the run order. Two are fixed; working on the third.";
    ci.last = asking ? at(cycle + 12) : at(sec - (sec % 20));

    const backfill = sessions.find((s) => s.n === 5);
    const days = Math.min(7, 4 + Math.floor(sec / 30));
    backfill.recap = days < 7
      ? `Backfilling September 12–18: ${days} of 7 days loaded, row counts match the source.`
      : "The September 12–18 backfill is done: 7 of 7 days, row counts match. Your move: turn the nightly loader back on.";
    backfill.live = days < 7 ? { status: "shell", since: 14 } : { status: "idle", since: at(90) };
    if (days === 7) backfill.last = at(90);
  }

  const RECENT_MIN = 3 * 24 * 60;
  function column(s) {
    if (s.live) {
      if (s.live.status === "waiting" || s.live.status === "blocked" || s.live.needs) return "needs_you";
      return s.live.status === "busy" || s.live.status === "shell" ? "working" : "your_move";
    }
    return s.last <= RECENT_MIN ? "recent" : "archive";
  }

  function card(s) {
    const tasks = (s.tasks || []).map((tk, i) => ({ id: String(i + 1), ...tk }));
    const current = tasks.find((tk) => tk.status === "in_progress");
    const prompt = (s.events || []).filter((e) => e[1] === "prompt").sort((a, b) => a[0] - b[0])[0];  // smallest "minutes ago"
    const live = s.live && {
      session_id: uuid(s.n), kind: s.live.kind || "interactive", status: s.live.status,
      waiting_for: s.live.waiting_for || "", name: `${s.project}-${s.n}`, needs: s.live.needs || "",
      suggested_reply: s.live.suggested_reply || "", job_id: s.live.job_id || "",
      status_since_ms: t0 - s.live.since * MIN,
    };
    return {
      session_id: uuid(s.n), title: s.title, column: column(s), live,
      project: s.project, cwd: `/Users/dev/work/${s.project}`, branch: s.branch, worktree: "",
      jira: s.keys.map((k) => ({ key: k, url: `${JIRA}/browse/${k}` })), prs: s.prs,
      recap: s.recap, recap_at: iso(s.last), last_prompt: prompt ? prompt[2] : "", last_prompt_at: prompt ? iso(prompt[0]) : "",
      last_assistant_text: "", last_activity: iso(s.last), started_at: iso(s.started),
      pending_tool: "", n_prompts: (s.events || []).filter((e) => e[1] === "prompt").length || 1,
      open_questions: (s.questions || []).filter((q) => q.answer === null).length,
      n_agents: (s.agents || []).length, n_plans: (s.plans || []).length,
      cost_usd: s.cost, transcript_missing: false, continued_in: "", tasks,
      tasks_done: tasks.filter((tk) => tk.status === "completed").length, tasks_total: tasks.length,
      current_task: current ? current.active_form : "",
      delivery: delivery(s), done: !!s.done, running_reply: !!s.running_reply,
      peer: s.peer ? { dir: s.peer.dir, who: s.peer.who, text: s.peer.text, at: iso(s.peer.m) } : null,
    };
  }

  function detail(s) {
    const c = card(s);
    const events = (s.events || [[s.started, "prompt", `Task ${s.keys[0]}: ${s.title}`], [s.last, "recap", null]])
      .map(([m, kind, text]) => ({ ts: iso(kind === "recap" ? s.last : m), kind, text: text ?? s.recap }))
      .sort((a, b) => a.ts.localeCompare(b.ts));
    return {
      ...c,
      plans: (s.plans || []).map((p, i) => ({ index: i, ts: iso(p.m), path: p.path || "", text: p.text || "", exists: true })),
      questions: (s.questions || []).map((q) => ({ ts: iso(q.m), items: q.items, answer: q.answer })),
      agents: (s.agents || []).map(([m, description, type]) => ({ ts: iso(m), description, type, background: false })),
      events, top_tools: s.tools || [["Bash", 10], ["Read", 8]], models: ["claude-opus-5-5", "claude-haiku-5-5"],
      lines_added: s.lines[0], lines_removed: s.lines[1], first_prompt: events[0]?.text || "",
      resume_command: c.live && c.live.kind === "background" ? `claude attach ${c.live.job_id}`
        : `cd /Users/dev/work/${s.project} && claude --resume ${c.session_id}`,
    };
  }

  const byId = (id) => sessions.find((s) => uuid(s.n) === id);
  const PLAN_TEXT = (text) => `<!doctype html><meta charset="utf-8"><style>
:root{--bg:#ffffff;--fg:#1c2530}@media (prefers-color-scheme:dark){:root{--bg:#1e2630;--fg:#e6ebf0}}
body{margin:0;padding:24px;background:var(--bg);color:var(--fg);font:15px/1.6 system-ui,sans-serif}
pre{white-space:pre-wrap;font:inherit;max-width:72ch}</style><pre>${text.replace(/&/g, "&amp;").replace(/</g, "&lt;")}</pre>`;

  // Tools: an invented catalog. No real names, paths or hosts; usage points at the demo sessions above.
  // Built-in items carry the server's catalog notes, as /api/tools words them.
  const toolsCatalog = () => {
    const use = (count, minutesAgo, picks) => count ? {
      count, last: iso(minutesAgo),
      sessions: picks.map(([n, c]) => ({ session_id: uuid(n), title: byId(uuid(n)).title, count: c })),
    } : { count: 0, last: null, sessions: [] };
    const none = use(0);
    const it = (kind, name, source, description, usage, extra = {}) => ({
      id: `${kind}:${source}:${extra.plugin || extra.project || ""}:${name}`, name, kind, source, description, usage,
      path: source === "builtin" || source === "connector" ? undefined : `/Users/dev/.claude/${kind}s/${name}`, ...extra,
    });
    return {
      generated_at: new Date().toISOString(),
      skills: [
        it("skill", "release-notes", "user", "Builds release notes from git history and MR descriptions: tickets, breaking changes, migration steps.", use(23, 2 * 1440, [[1, 9], [3, 8], [4, 6]])),
        it("skill", "review-checklist", "user", "A merge request review checklist: tests, migrations, security, API compatibility. Returns a verdict and the findings ranked by severity.", use(11, 6 * 60, [[2, 6], [1, 5]])),
        it("skill", "brainstorming", "plugin", "Talks an idea through before it’s built: clarifying questions, options, the chosen approach.", use(7, 3 * 1440, [[3, 4], [2, 3]]), { plugin: "superpowers", enabled: true }),
        it("skill", "payments-sandbox", "project", "How to bring up the payment provider’s sandbox and run the webhook e2e tests.", use(4, 4 * 1440, [[1, 4]]), { project: "payments-api" }),
        it("skill", "simplify", "builtin", t("tools.builtin.note"), use(2, 9 * 1440, [[4, 2]])),
        it("skill", "legacy-dashboards", "user", "An outdated helper for the old dashboards. Hasn’t been needed in ages.", none),
        it("skill", "db-migrations", "plugin", "Helps write and check zero-downtime schema migrations.", none, { plugin: "data-kit", enabled: false }),
      ],
      agents: [
        it("agent", "general-purpose", "builtin", t("tools.builtin.general_purpose"), use(31, 90, [[1, 12], [2, 11], [4, 8]])),
        it("agent", "Explore", "builtin", t("tools.builtin.explore"), use(18, 5 * 60, [[3, 10], [1, 8]])),
        it("agent", "code-critic", "user", "Critiques architecture decisions before implementation starts: risks, scaling, failure modes.", use(5, 2 * 1440, [[2, 5]]), { model: "sonnet", tools: ["Read", "Grep", "Glob"] }),
        it("agent", "Plan", "builtin", t("tools.builtin.plan"), none),
        it("agent", "data-reviewer", "project", "Checks SQL and pipelines for type correctness and idempotency.", none, { project: "analytics-dbt", model: "opus", tools: [] }),
      ],
      mcp: [
        it("mcp", "tracker", "user", "", use(46, 40, [[1, 20], [3, 14], [2, 12]]), { transport: "stdio", command: "uvx", args: ["tracker-mcp", "--token", "•••"], url: "", host: "", env_names: ["TRACKER_URL", "TRACKER_TOKEN"], header_names: [], tools: [{ name: "get_issue", count: 30 }, { name: "search", count: 12 }, { name: "add_comment", count: 4 }] }),
        it("mcp", "chat-search", "user", "", use(9, 1440, [[2, 5], [4, 4]]), { transport: "http", command: "", args: [], url: "https://chat-mcp.example.com/mcp", host: "chat-mcp.example.com", env_names: [], header_names: ["Authorization"], tools: [{ name: "messages_search", count: 9 }] }),
        it("mcp", "Calendar", "connector", t("tools.builtin.connector"), use(3, 5 * 1440, [[3, 3]]), { transport: "http", command: "", args: [], url: "", host: "", env_names: [], header_names: [], tools: [] }),
        it("mcp", "postgres", "project", "", none, { project: "analytics-dbt", transport: "stdio", command: "npx", args: ["-y", "pg-mcp", "--password", "•••"], url: "", host: "", env_names: ["PGHOST"], header_names: [], tools: [] }),
        it("mcp", "docs", "plugin", "", none, { plugin: "superpowers", transport: "http", command: "", args: [], url: "https://docs.example.com/mcp", host: "docs.example.com", env_names: [], header_names: [], tools: [] }),
      ],
      plugins: [
        it("plugin", "superpowers", "plugin", "A set of working practices: TDD, debugging, planning, review.", { count: 7, last: iso(3 * 1440), sessions: [] }, { plugin: "superpowers", marketplace: "official", version: "7.0.0", enabled: true, counts: { skills: 14, agents: 1, commands: 0, mcp: 1 } }),
        it("plugin", "data-kit", "plugin", "Helpers for working with data and schemas.", { count: 0, last: null, sessions: [] }, { plugin: "data-kit", marketplace: "community", version: "0.4.2", enabled: false, counts: { skills: 3, agents: 0, commands: 2, mcp: 0 } }),
      ],
      commands: [
        it("command", "compact", "builtin", t("tools.builtin.note"), use(14, 120, [[1, 6], [2, 5]])),
        it("command", "ship", "user", "Run the checks, commit and open an MR.", use(6, 1440, [[2, 4], [4, 2]]), { argument_hint: "[branch]" }),
        it("command", "standup", "user", "A summary of my sessions from yesterday.", none),
      ],
      hooks: [
        { id: "hook:user::0", name: "PreToolUse Bash", kind: "hook", source: "user", description: "", event: "PreToolUse", matcher: "Bash", command: "guard-shell.sh --strict", hook_type: "command", path: "/Users/dev/.claude/settings.json" },
        { id: "hook:user::1", name: "Stop", kind: "hook", source: "user", description: "", event: "Stop", matcher: "", command: "notify-done.sh --api-key •••", hook_type: "command", path: "/Users/dev/.claude/settings.json" },
      ],
    };
  };

  /* ---------- Chat: invented threads, delivery fields, a fake send ---------- */
  const DELIVERY = { 17: "vscode", 1: "vscode", 2: "vscode", 3: "tmux", 4: "tmux", 5: "tmux", 6: "vscode", 8: "vscode", 9: "tmux", 10: "vscode" };
  // A route's reason is the server's catalog string (core/providers/claude/routes.py).
  const ROUTE_REASON = {
    vscode: t("app.provider.claude.vscode"),
    tmux: t("app.provider.claude.tmux"),
    resume: t("app.provider.claude.resume"),
  };
  function delivery(s) {
    if (s.n === 7) {
      return { route: "takeover", can_send: true, reason: t("app.provider.claude.takeover_no_bridge") };
    }
    const route = s.live ? DELIVERY[s.n] || "vscode" : "resume";
    return { route, can_send: true, reason: ROUTE_REASON[route] };
  }

  const shot = (title, lines) => "data:image/svg+xml," + encodeURIComponent(
    `<svg xmlns="http://www.w3.org/2000/svg" width="480" height="270" viewBox="0 0 480 270"><rect width="480" height="270" fill="#1e2630"/>
<rect width="480" height="28" fill="#2e3844"/><circle cx="16" cy="14" r="5" fill="#ff6b6f"/><circle cx="34" cy="14" r="5" fill="#e0a54a"/><circle cx="52" cy="14" r="5" fill="#3cc2b2"/>
<text x="76" y="19" font-family="monospace" font-size="12" fill="#aab4bf">${title}</text>
${lines.map((l, i) => `<text x="20" y="${62 + i * 24}" font-family="monospace" font-size="14" fill="${l[0] === "!" ? "#ff6b6f" : "#e6ebf0"}">${l.replace(/^!/, "")}</text>`).join("")}</svg>`);

  // [minutes ago, kind, fields]; tool calls are [name, summary, failed?]
  const RICH_THREADS = {
    1: () => [
      [95, "you", { text: "We need to rotate the payment provider’s webhook secret with no downtime. PAY-412" }],
      [94, "agent", { text: "Got it. First I’ll look at how the signature is checked today:\n\n- where the secret is stored\n- how `verify_signature` works\n- whether there are flags we can already ship behind\n\nThen I’ll propose a plan." }],
      [93, "tools", { calls: [["Read", "Read: webhooks/verify.py"], ["Grep", "Grep: verify_signature src/"], ["Bash", "Bash: cat config/secrets.example.yaml"],
        ["Read", "Read: payments/provider.py", true]] }],
      [90, "agent", { text: "The signature is checked against a single secret from `WEBHOOK_SECRET`. To avoid losing webhooks during the switch, we need **dual verification**: the new secret first, then the old one.\n\n```python\ndef verify(body, sig):\n    return any(hmac_ok(body, sig, s) for s in secrets())\n```\n\nI’ve written up a plan." }],
      [80, "plan", { text: PLAN_WEBHOOKS, path: "plans/pay-412-rotation.md" }],
      [79, "you", { text: "The plan looks good, go ahead. Release it only behind a flag." }],
      [70, "tools", { calls: [["Edit", "Edit: webhooks/verify.py"], ["Edit", "Edit: config/flags.py"], ["Write", "Write: tests/test_verify_dual.py"],
        ["Bash", "Bash: pytest tests/test_verify_dual.py -q"], ["Agent", "Agent: Review signature verification"]] }],
      [62, "you", { text: "Here’s what staging shows. Is this normal?", images: [shot("staging: webhook-gateway", ["POST /webhooks/pay 200", "POST /webhooks/pay 200", "!POST /webhooks/pay 401 bad signature", "POST /webhooks/pay 200"])] }],
      [60, "agent", { text: "One 401 out of four: that’s a webhook signed with the old secret while the dual verification flag is off. I checked:\n\n| Secret | Requests | Accepted |\n|---|---|---|\n| old | 1 | 0 |\n| new | 3 | 3 |\n\nOnce the flag is on, the 401s will go away. Details are in the [provider’s documentation](https://docs.example.com/webhooks/signing)." }],
      [45, "compact", { text: "" }],
      [40, "tools", { calls: [["Bash", "Bash: git push -u origin pay-412-webhook-rotation"], ["Bash", "Bash: glab mr create --fill"]] }],
      [3, "recap", { text: "The second secret is in vault, and dual signature verification is ready behind a flag. Next step: apply on staging, which needs your permission for kubectl apply." }],
      [3, "tools", { calls: [["Bash", "Bash: kubectl apply -f deploy/staging/webhook-secrets.yaml"]] }],
    ],
    2: () => [
      [40, "you", { text: "Suggest how to store product events in the data lake, DATA-207" }],
      [39, "agent", { text: "Looking at what analytics sends today and how the dashboards read it." }],
      [38, "tools", { calls: [["Read", "Read: schemas/events/v3.json"], ["Bash", "Bash: duckdb -c \"select event_type, count(*) from events group by 1\""], ["Grep", "Grep: event_type dashboards/"]] }],
      [30, "agent", { text: "There are two workable options:\n\n1. **One wide table**: a simpler schema and simpler queries, but lots of empty columns and costly evolution\n2. **A table per event type**: clean schemas, but more DAGs and more joins in the dashboards\n\nWith 38 event types I’d lean toward the first, but it’s your call." }],
      [11, "question", { items: [{ header: "Format", question: "Should events go into one wide table or one table per event type?",
        options: [{ label: "One wide table (Recommended)", description: "Simpler queries" }, { label: "A table per type", description: "Cleaner schemas" }] }], answer: null }],
    ],
    4: () => [
      [70, "you", { text: "Move the cart to server components, WEB-981" }],
      [69, "agent", { text: "I’ll start with the cart and the order summary: they have the least client-side logic." }],
      [30, "tools", { calls: [["Agent", "Agent: Find client-only imports"], ["Read", "Read: app/cart/page.tsx"], ["Edit", "Edit: app/cart/CartSummary.tsx"], ["Edit", "Edit: app/cart/page.tsx"]] }],
      [20, "agent", { text: "The cart and the summary are moved, and the client bundle is **38 KB** smaller. Next is the discount calculation: it uses `useCart`, so it has to move into a server function." }],
      [12, "tools", { calls: [["Agent", "Agent: Review discount edge cases"], ["Bash", "Bash: pnpm build", true], ["Edit", "Edit: lib/discounts.ts"], ["Bash", "Bash: pnpm build"]] }],
      [8, "recap", { text: "Moved the cart and the order summary to the server, and the client bundle is 38 KB smaller. Now moving the discount calculation." }],
    ],
  };

  // Cross-session messages appended after a session's own thread: [minutes ago, kind, fields]
  const EXTRA_THREADS = {
    1: [[4, "peer_in", { from: SUP, msg_id: "m-71", text: "Before applying on staging, keep MR !214 in mind: it changes the **idempotency keys**. Don’t overwrite them when you rotate the secret." }]],
    4: [[9, "peer_out", { to: uuid(8), ok: true, summary: "Asked about a discount_total column in refunds",
      text: "Discounts on refunds are calculated in `lib/discounts.ts`. Does refunds need a `discount_total` column?" }]],
    7: [[7, "peer_out", { to: uuid(1), ok: false, summary: "The !214 review is done, no issues",
      text: "The !214 review is done, no blocking issues. Better leave the idempotency keys as they are.\n\nSee the comments in the MR." }]],
    8: [[9, "peer_in", { from: uuid(4), msg_id: "m-64", text: "Discounts on refunds are calculated in `lib/discounts.ts`. Does refunds need a `discount_total` column?" }]],
  };
  const SUPERVISOR_THREAD = () => [
    [240, "you", { text: "Keep an eye on my sessions, send me a summary every hour, and hand out tasks if something is stuck." }],
    [239, "agent", { text: "Got it. I’ll check the board every hour, write a summary, and send tasks to sessions through cross-session messages." }],
    [60, "tools", { calls: [["Bash", "Bash: sya ls --json"], ["Read", "Read: payments-api/MR !214"]] }],
    [26, "peer_out", { to: uuid(1), ok: true, summary: "Keep MR !214 in mind while rotating the secret",
      text: "Before applying on staging, keep MR !214 in mind: it changes the **idempotency keys**. Don’t overwrite them when you rotate the secret." }],
    [11, "peer_out", { to: uuid(7), ok: true, summary: "Send the review results to the webhooks session",
      text: "When you finish the !214 review, send the results to the webhooks session (PAY-412)." }],
    [8, "peer_out", { to: uuid(10), ok: null, summary: "Asked whether to turn on checkout_v2 for 10%",
      text: "Variant B is ready behind the flag. Should we turn on `checkout_v2` for 10% of traffic? I need an answer by tonight." }],
    [7, "peer_in", { from: uuid(7), msg_id: "m-70", text: "The !214 review is done, no blocking issues. Better leave the idempotency keys as they are." }],
    [6, "recap", { text: "Put together a summary of the sessions: two are waiting for you, and I sent tasks to two." }],
    [6, "agent", { text: "Where things stand:\n\n- **Waiting for you:** webhooks (permission for `kubectl apply`) and the mobile-app dependencies\n- **Working:** the cart, CI\n- The !214 review is finished, and the results went to the webhooks session" }],
  ];

  function generic(s) {
    const first = (s.events || []).find((e) => e[1] === "prompt");
    const calls = (s.tools || []).flatMap(([name, n]) => Array.from({ length: Math.min(n, 3) }, (_, i) => [name, `${name}: ${s.project}/${["src", "tests", "docs"][i % 3]}`]));
    return [
      [first ? first[0] : s.started, "you", { text: first ? first[2] : `Task ${s.keys[0]}: ${s.title}` }],
      [s.started - 1, "agent", { text: `Got it. Starting on “${s.title}”.` }],
      ...(calls.length ? [[s.started - 3, "tools", { calls }]] : []),
      ...(s.recap ? [[s.last, "agent", { text: s.recap }], [s.last, "recap", { text: s.recap }]] : []),
    ].map(([m, kind, f]) => [Math.min(m, s.started), kind, f]);
  }

  const threads = new Map();
  function threadOf(s) {
    if (!threads.has(s.n)) {
      const raw = [...(s.n === 17 ? SUPERVISOR_THREAD() : (RICH_THREADS[s.n] || (() => generic(s)))()), ...(EXTRA_THREADS[s.n] || [])];
      threads.set(s.n, raw.map(([minAgo, kind, f], id) => {
        const m = { id, at: iso(minAgo), kind, ...f };
        if (kind === "tools") m.calls = f.calls.map(([name, summary, error]) => ({ name, summary, error: !!error }));
        if (kind === "you") m.images = (f.images || []).map((url) => ({ url, media_type: "image/svg+xml" }));
        return m;
      }));
    }
    return threads.get(s.n);
  }

  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const say = (s, kind, f) => {
    const msgs = threadOf(s);
    msgs.push({ id: msgs.length, at: new Date().toISOString(), kind, ...f });
    s.last = at((Date.now() - t0) / 1000);
  };
  const chatApi = {
    thread: async (id, { limit = 60, before, after } = {}) => {
      const s = byId(id);
      if (!s) return null;
      const msgs = threadOf(s), end = msgs.length;
      if (after != null) return { session_id: id, messages: msgs.slice(Math.min(Number(after), end)), before: null, after: end, has_more: false };
      const stop = before != null ? Math.min(Number(before), end) : end, start = Math.max(0, stop - limit);
      return { session_id: id, messages: msgs.slice(start, stop), before: start || null, after: end, has_more: start > 0 };
    },
    imageSrc: (id, url) => url,
    send: async (id, text, files) => {
      const s = byId(id), d = s && delivery(s);
      if (!s) return { ok: false, status: 404, data: { ok: false, error: t("chat.no_session") } };
      if (!d.can_send) return { ok: false, status: 409, data: { ok: false, error: d.reason } };
      if (s.running_reply) return { ok: false, status: 409, data: { ok: false, error: t("app.provider.claude.reply_running") } };
      await wait(250);
      say(s, "you", { text: [text, ...files.map((f) => `File: ${f}`)].filter(Boolean).join("\n\n"), images: [] });
      s.running_reply = true;
      setTimeout(() => {
        say(s, "agent", { text: text
          ? `Got it: “${text.length > 80 ? text.slice(0, 80) + "…" : text}”. On it.\n\nThis is a demo, so no real agent answers here, but this is what a reply with **markdown** and \`code\` looks like.`
          : "Got the files, taking a look." });
        s.running_reply = false;
      }, 1500);
      return { ok: true, status: 200, data: { ok: true, route: d.route, detail: d.reason } };
    },
    upload: async (id, file) => {
      await wait(500);
      return { ok: true, status: 200, data: { ok: true, path: `/Users/dev/.cache/see-your-agents/uploads/${file.name}`, name: file.name } };
    },
    act: async (id, name, body) => {
      const s = byId(id);
      if (!s) return { ok: false, status: 404, data: { ok: false, error: t("chat.no_session") } };
      if (name === "done") s.done = !!body.done;
      if (name === "stop") { s.live = null; s.done = true; }
      return { ok: true, status: 200, data: { ok: true } };
    },
  };

  // Per-agent looks live in localStorage (the real page keeps them on the server). The first visit shows a few
  // styled agents, so the office is not all the same.
  const LOOKS_KEY = "sya.demo.looks";
  const look = (shape, hue, desk, items, label = "") => ({ shape, hue, desk, items, label });
  const SEED_LOOKS = {
    sessions: {
      [uuid(1)]: look("hexagon", 4, null, ["mug", "lamp"], "Secrets"),
      [uuid(3)]: look("star", 200, "round", ["headphones", "duck"]),
      [uuid(5)]: look("squircle", 150, null, ["cactus", "books", "globe"], "Backfill"),
      [uuid(6)]: look("blob", 320, "pouf", ["cat", "plant"]),
      [uuid(9)]: look("circle", 46, "console", ["trophy", "photo", "laptop", "mug"]),
    },
    projects: {},
  };
  const readLooks = () => {
    try { return JSON.parse(localStorage.getItem(LOOKS_KEY)) || SEED_LOOKS; } catch (e) { return SEED_LOOKS; }
  };
  let looksDoc = readLooks();
  const looksApi = {
    get: async () => looksDoc,
    post: async ({ session_id, project, look: next }) => {
      const table = session_id ? looksDoc.sessions : looksDoc.projects, key = session_id ?? project;
      if (next) table[key] = next; else delete table[key];
      try { localStorage.setItem(LOOKS_KEY, JSON.stringify(looksDoc)); } catch (e) { /* not persisted */ }
      return { ok: true, status: 200, data: looksDoc };
    },
  };

  // The board's column titles, as core/sessions.py words them.
  const COLUMNS = [["needs_you", t("app.column.needs_you")], ["working", t("app.column.working")],
    ["your_move", t("app.column.your_move")], ["recent", t("app.column.recent")], ["archive", t("app.column.archive")]];

  window.SYA_API = {
    demo: true,
    tools: async () => toolsCatalog(),
    looks: looksApi,
    board: async () => {
      simulate();
      const cards = sessions.map(card).sort((a, b) => b.last_activity.localeCompare(a.last_activity));
      return {
        generated_at: new Date().toISOString(), jira_base: JIRA, cards,
        supervisor_id: SUP,
        digest: {
          text: "Waiting for you: webhooks (permission for kubectl apply) and the mobile-app dependencies.\nWorking: the cart and CI.\nThe !214 review is finished, and the results went to the webhooks session.",
          at: iso(6), session_id: SUP,
        },
        columns: COLUMNS.map(([id, title]) => ({ id, title })),
      };
    },
    chat: chatApi,
    session: async (id) => (byId(id) ? detail(byId(id)) : null),
    search: async (q) => {
      const needle = q.toLowerCase();
      const hits = {};
      for (const s of sessions) {
        const d = detail(s);
        const found = [{ ts: d.last_activity, kind: "title", text: `${s.title} ${s.keys.join(" ")}` }, ...d.events]
          .filter((e) => e.text.toLowerCase().includes(needle));
        if (found.length) hits[d.session_id] = found;
      }
      return hits;
    },
    planFrame: (id, i) => {
      const plan = byId(id).plans[i];
      return { srcdoc: plan.html || PLAN_TEXT(plan.text) };
    },
  };

  // Browser notifications are refused inside the published page; say what this page is instead.
  // The host page owns light/dark here, so the theme switch is hidden too.
  document.getElementById("notify").classList.add("hidden");
  document.getElementById("theme-row").classList.add("hidden");
  const note = document.createElement("span");
  note.className = "demo-note";
  note.textContent = "Demo: the team and the sessions are made up, and statuses change on their own";
  document.querySelector(".bar").appendChild(note);
})();
