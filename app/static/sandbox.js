const token = document.body.dataset.token;
const line = document.getElementById("result-line");

function cell(text) {
  const td = document.createElement("td");
  td.textContent = text;
  return td;
}

function renderTable(columns, rows) {
  const head = document.getElementById("result-head");
  head.replaceChildren(...columns.map((c) => {
    const th = document.createElement("th");
    th.textContent = c;
    return th;
  }));
  const body = document.getElementById("result-body");
  body.replaceChildren(...rows.map((row) => {
    const tr = document.createElement("tr");
    const first = cell("");
    if (row.url) {
      const link = document.createElement("a");
      link.href = row.url;
      link.target = "_blank";
      link.rel = "noopener";
      link.textContent = row.a;
      first.append(link);
    } else {
      first.textContent = row.a;
    }
    tr.append(first, cell(row.b), cell(row.c));
    return tr;
  }));
}

function resultMessage(result) {
  if (result.cached) return `Showing ${result.label} again. A repeat costs nothing.`;
  if (result.count === 0) return "That combination found nobody, and nothing was charged. Try another filter.";
  const total = result.total > result.count ? ` of ${result.total.toLocaleString()}` : "";
  return `Live search: ${result.count}${total} results in ${result.latency_ms} ms, ${result.credits} credits. That was your second call.`;
}

document.querySelectorAll(".pill").forEach((button) => {
  button.addEventListener("click", async () => {
    document.querySelectorAll(".pill").forEach((p) => (p.disabled = true));
    line.textContent = "Running...";
    line.className = "result-line";
    try {
      const response = await fetch(`/sandbox/${token}/pills/${button.dataset.pill}`, { method: "POST" });
      const result = await response.json();
      if (!response.ok || !result.ok) {
        line.textContent = result.message || "That didn't work. Nothing was charged.";
        line.className = "result-line bad";
        return;
      }
      renderTable(result.columns, result.rows);
      document.getElementById("code-json").textContent = result.request_json;
      document.getElementById("code-curl").textContent = result.curl;
      document.getElementById("code-python").textContent = result.python;
      document.getElementById("endpoint").textContent = result.request.endpoint;
      document.querySelectorAll(".pill").forEach((p) => p.classList.remove("on"));
      button.classList.add("on");
      line.textContent = resultMessage(result);
      line.className = result.count === 0 ? "result-line bad" : "result-line good";
    } catch {
      line.textContent = "We couldn't reach the server. Your code below still works.";
      line.className = "result-line bad";
    } finally {
      document.querySelectorAll(".pill").forEach((p) => (p.disabled = false));
    }
  });
});

document.querySelectorAll(".code-tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".code-tab").forEach((t) => t.classList.toggle("active", t === tab));
    document.querySelectorAll("pre.code").forEach((pre) => (pre.hidden = pre.id !== `code-${tab.dataset.code}`));
  });
});

document.getElementById("copy").addEventListener("click", async (event) => {
  const visible = document.querySelector("pre.code:not([hidden])");
  try {
    await navigator.clipboard.writeText(visible.textContent);
    event.target.textContent = "Copied";
  } catch {
    event.target.textContent = "Select and copy";
  }
  setTimeout(() => (event.target.textContent = "Copy"), 1500);
});
