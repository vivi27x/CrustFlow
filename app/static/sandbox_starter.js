// Runs the signup's starter in real Postgres, in their browser. Nothing here reaches our server
// except a note that they ran something, which is what marks them as activated.
const PGLITE = "https://cdn.jsdelivr.net/npm/@electric-sql/pglite@0.2.17/dist";
const DIMENSIONS = 1536;
const OWN_SEARCH = "own_search";

const started = performance.now();
const starter = JSON.parse(document.getElementById("starter").textContent);
const token = document.body.dataset.token;
const $ = (id) => document.getElementById(id);
const state = { db: null, user: starter.user, search: starter.search, busy: false };

// A word hash, so this runs with no AI key. Shared words and word parts land on the same dimensions.
function bucket(text) {
  let hash = 2166136261;
  for (const char of text) {
    hash ^= char.codePointAt(0);
    hash = Math.imul(hash, 16777619);
  }
  return hash >>> 0;
}

function embed(text) {
  const vector = new Float64Array(DIMENSIONS);
  vector[0] = 0.001; // An all-zero vector has no direction, and cosine distance needs one.
  for (const word of text.toLowerCase().match(/[\p{L}\p{N}]+/gu) || []) {
    vector[bucket(word) % DIMENSIONS] += 2;
    const chars = [...word];
    for (let i = 0; i + 3 <= chars.length; i++) vector[bucket(chars.slice(i, i + 3).join("")) % DIMENSIONS] += 1;
  }
  const size = Math.sqrt(vector.reduce((sum, value) => sum + value * value, 0));
  return `[${Array.from(vector, (value) => (value / size).toFixed(6)).join(",")}]`;
}

async function signIn(user) {
  await state.db.exec("reset role");
  await state.db.query("select set_config('request.jwt.claim.sub', $1, false)", [user]);
  await state.db.exec("set role authenticated");
  state.user = user;
}

async function embedNewRows() {
  if (!starter.vector) return;
  const { rows } = await state.db.query(starter.unembedded);
  for (const row of rows) await state.db.query(starter.set_embedding, [embed(row.text), row.id]);
}

function shown(value) {
  if (value instanceof Date) return value.toISOString().slice(0, 10);
  return value === null || value === undefined ? "" : String(value);
}

function cell(tag, text) {
  const node = document.createElement(tag);
  node.textContent = shown(text);
  return node;
}

function show(rows) {
  const columns = rows.length ? Object.keys(rows[0]) : [];
  $("result-head").replaceChildren(...columns.map((name) => cell("th", name)));
  $("result-body").replaceChildren(...rows.map((row) => {
    const line = document.createElement("tr");
    line.append(...columns.map((name) => cell("td", row[name])));
    return line;
  }));
  $("results-wrap").hidden = false;
  const who = state.user === starter.user ? "the first sample user" : "a second sample user";
  $("signed-in").textContent = `Signed in as ${who}. ${rows.length} row${rows.length === 1 ? "" : "s"} visible.`;
  $("signed-in").hidden = false;
}

async function read() {
  const started = performance.now();
  const result = await state.db.query(starter.read, starter.vector ? [embed(state.search)] : []);
  return { rows: result.rows, ms: Math.round(performance.now() - started) };
}

async function report(pill) {
  try {
    await fetch(`/sandbox/${token}/ran`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ pill }),
    });
  } catch {
    // The run itself worked, so a lost note shouldn't bother them.
  }
}

async function secondRun(pillId, work, describe) {
  if (state.busy) return;
  state.busy = true;
  try {
    const rows = await work();
    show(rows);
    $("status").textContent = describe(rows);
    report(pillId);
  } catch (error) {
    $("status").textContent = `That run failed: ${error.message}`;
  } finally {
    state.busy = false;
  }
}

const count = (rows) => `${rows.length} row${rows.length === 1 ? "" : "s"}`;
const pillRuns = {
  search: async (pill) => {
    state.search = pill.search;
    if ($("search")) $("search").value = pill.search;
    return (await read()).rows;
  },
  user: async (pill) => {
    await signIn(state.user === pill.user ? starter.user : pill.user);
    userButton(pill).querySelector("span").textContent = state.user === pill.user ? "Back to the first user" : pill.label;
    return (await read()).rows;
  },
  insert: async (pill) => {
    await state.db.query(pill.sql);
    await embedNewRows();
    return (await read()).rows;
  },
  filter: async (pill) => (await state.db.query(pill.sql)).rows,
};

const userButton = (pill) => document.querySelector(`[data-pill="${pill.id}"]`);

function connect() {
  document.querySelectorAll("[data-pill]").forEach((button) => {
    const pill = starter.pills.find((item) => item.id === button.dataset.pill);
    button.addEventListener("click", () => {
      const describe = (rows) => (pill.kind === "user" ? `Same query, new user: ${count(rows)}.` : `${pill.label}: ${count(rows)}.`);
      secondRun(pill.id, () => pillRuns[pill.kind](pill), describe).then(() => {
        if (pill.kind === "insert") button.disabled = true;
      });
    });
  });
  const form = $("search-form");
  if (form) {
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      state.search = $("search").value.trim() || starter.search;
      secondRun(OWN_SEARCH, async () => (await read()).rows, (rows) => `Your search: ${count(rows)}.`);
    });
    form.hidden = false;
  }
  $("pills").hidden = false;
}

document.querySelectorAll("[data-copy]").forEach((button) => {
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText($(button.dataset.copy).textContent);
      button.textContent = "Copied";
    } catch {
      button.textContent = "Select it to copy";
    }
  });
});

async function start() {
  const [{ PGlite }, { vector }] = await Promise.all([import(`${PGLITE}/index.js`), import(`${PGLITE}/vector/index.js`)]);
  state.db = new PGlite({ extensions: { vector } });
  await state.db.exec(starter.bootstrap);
  await state.db.exec(starter.sql); // The same text as the email.
  await state.db.query(starter.assign_owners);
  await embedNewRows();
  await signIn(starter.user);
  const { rows } = await read();
  show(rows);
  connect();
  const total = ((performance.now() - started) / 1000).toFixed(1);
  $("status").textContent = `It works: your table, policy and sample rows ran in ${total} s, and this is the query result. Try a button.`;
  if ($("embedding-note")) $("embedding-note").hidden = false;
}

start().catch((error) => {
  console.error(error);
  $("status").textContent = "Postgres couldn't start in this browser, so nothing ran. The SQL below still works in your SQL editor.";
});
