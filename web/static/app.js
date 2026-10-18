/*
 * Dashboard controller. Vanilla JS, no CDN: the demo must render offline.
 *
 * All data reaches the DOM through textContent or canvas drawing, never through
 * innerHTML, so a resource name coming back from a live AWS account cannot
 * inject markup into the page.
 */
"use strict";

const state = {
  days: 30,
  mode: "demo",
  region: "us-east-1",
  payload: null,
  ebs: null,
  ebsError: null,
  ebsFetchedAt: null,
};

// Cost Explorer reports credited or empty lines as a signed zero, which would
// otherwise render as "-$0.00"; `value === 0` is true for -0, so this maps it
// back to a plain zero.
const usd = (value) =>
  typeof value === "number"
    ? (value === 0 ? 0 : value).toLocaleString("en-US", { style: "currency", currency: "USD" })
    : "n/a";

const pct = (value) => (typeof value === "number" ? value.toFixed(1) + "%" : "n/a");

const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
};

const clear = (node) => {
  while (node.firstChild) node.removeChild(node.firstChild);
};

const show = (id, visible) => {
  document.getElementById(id).hidden = !visible;
};

/* Sections the current payload failed to read. An empty table reads as "this
 * account has nothing here", which is a claim AWS did not confirm, so a failed
 * scan has to be labelled as unread rather than as empty. */
let failedSections = new Set();

// When the Cost Explorer client cannot be built, the spend queries are skipped
// rather than failed, so the payload carries one client error. The three
// sections that depend on it are still unread.
const SPEND_SECTIONS = ["services", "regions", "trend"];

function readFailedSections(errors) {
  const sections = new Set(errors.map((error) => error.Section));
  if (sections.has("cost explorer client")) {
    SPEND_SECTIONS.forEach((section) => sections.add(section));
  }
  return sections;
}

const notRead = (section, fallback) => (failedSections.has(section)
  ? `${section} data could not be read - see the error above`
  : fallback);

// An empty section is an answer in live mode and an absence in demo mode, so the
// two must not share one "no spend recorded" caption.
const noRowsText = (meta) => (meta && meta.mode === "live"
  ? "Cost Explorer returned no cost rows for this window - the account recorded no billable usage"
  : "No spend recorded for this window");

function fillTable(tableId, rows, columns, emptyText) {
  const body = document.querySelector(`#${tableId} tbody`);
  clear(body);
  if (!rows.length) {
    const tr = el("tr", "empty");
    tr.appendChild(el("td", "", emptyText || "No records for this period"));
    tr.firstChild.colSpan = columns;
    body.appendChild(tr);
    return;
  }
  rows.forEach((cells) => {
    const tr = el("tr");
    cells.forEach(({ value, numeric, cls, tag }) => {
      const td = el("td", cls || (numeric ? "num" : ""));
      // A tag is a span inside the cell, because border-radius and padding are
      // ignored on a td of a border-collapsed table.
      if (tag !== undefined) {
        td.appendChild(el("span", tag ? `tag ${tag}` : "tag", value));
      } else {
        td.textContent = value;
      }
      tr.appendChild(td);
    });
    body.appendChild(tr);
  });
}

/* ---------------------------------------------------------------- metrics */

function renderCards(totals, meta) {
  const wrap = document.getElementById("metric-cards");
  clear(wrap);

  // A metric derived from a section that could not be read has no value to
  // show; 0 would be a figure the account never reported.
  const spendUnknown = failedSections.has("services");
  const wasteUnknown = failedSections.has("waste");

  const cards = [
    {
      label: "Total spend",
      value: spendUnknown ? "n/a" : usd(totals.TotalCostUSD),
      // The currency code comes from the payload, so a non-USD account cannot be
      // misread as dollars; the symbol is still rendered by usd().
      sub: spendUnknown
        ? "Cost Explorer could not be read"
        : `last ${totals.PeriodDays} days, billed in ${totals.Currency || "USD"}`,
    },
    {
      label: "Monthly run rate",
      value: spendUnknown ? "n/a" : usd(totals.MonthlyRunRateUSD),
      sub: "normalised from the window",
    },
    {
      label: "Largest service",
      value: totals.TopService || "n/a",
      sub: spendUnknown
        ? "Cost Explorer could not be read"
        : totals.TopService === null
          ? (meta.mode === "live" ? "Cost Explorer returned no cost rows" : "no spend recorded")
          : typeof totals.TopServicePercent === "number"
            ? `${pct(totals.TopServicePercent)} of spend`
            : "largest line, but a zero-net window has no share to report",
    },
    {
      label: "Est. monthly savings",
      value: wasteUnknown ? "n/a" : usd(totals.EstimatedMonthlySavingsUSD),
      sub: wasteUnknown
        ? "the waste scan did not complete"
        : `${totals.WasteFindings} findings, ${totals.UnquantifiedFindings} unquantified`,
      accent: true,
    },
    {
      label: "Data source",
      value: meta.mode === "demo" ? "Demo sample" : "Live AWS",
      sub: meta.mode === "demo" ? "no AWS API called" : meta.regionScope,
    },
  ];

  cards.forEach((card) => {
    const node = el("div", "card" + (card.accent ? " is-accent" : ""));
    node.appendChild(el("div", "label", card.label));
    node.appendChild(el("div", "value", card.value));
    node.appendChild(el("div", "sub", card.sub));
    wrap.appendChild(node);
  });
}

/* ------------------------------------------------------------------ charts */

function prepCanvas(canvas) {
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth || canvas.parentElement.clientWidth;
  const height = 220;
  if (!width) {
    // The panel is display:none (another view is open), so there is no width
    // to draw into. Navigation redraws once the panel is visible again.
    return null;
  }
  canvas.width = width * ratio;
  canvas.height = height * ratio;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);
  return { ctx, width, height };
}

const PALETTE = ["#4c8dff", "#6ee7b7", "#ffb020", "#a78bfa", "#f472b6", "#38bdf8", "#facc15"];

function axisLabel(value) {
  const abs = Math.abs(value);
  if (abs >= 10000) return `$${Math.round(value / 1000)}k`;
  if (abs >= 1000) return `$${(value / 1000).toFixed(1)}k`;
  if (abs >= 1) return `$${Math.round(value)}`;
  // A near-zero account is real data, so the axis must still read as money
  // rather than collapsing to "$0k" on every gridline.
  return `$${value.toFixed(2)}`;
}

function drawFlatTrend(ctx, width, height, pad, plotH, points, kind) {
  // Every period answered $0.00, so there is no range to scale against.
  // Inventing one (a "span" of 1 would put the zero line at the top edge and
  // label gridlines down to -$1.00) would draw numbers the account never
  // reported. Draw the zero line, mark each period on it, and say what it is.
  const plotW = width - pad.left - pad.right;
  const step = plotW / points.length;
  const y = pad.top + plotH / 2;
  const labelEvery = Math.ceil(points.length / 8);

  ctx.strokeStyle = "#43507a";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(pad.left, y);
  ctx.lineTo(width - pad.right, y);
  ctx.stroke();

  ctx.font = "10.5px Segoe UI, sans-serif";
  ctx.textAlign = "right";
  ctx.fillStyle = "#98a5c4";
  ctx.fillText(usd(0), pad.left - 8, y + 3);
  ctx.textAlign = "left";

  points.forEach((point, index) => {
    const x = pad.left + step * index + step / 2;
    ctx.fillStyle = index === points.length - 1 ? "#6ee7b7" : "#4c8dff";
    ctx.beginPath();
    ctx.arc(x, y, 2.5, 0, Math.PI * 2);
    ctx.fill();
    if (index % labelEvery === 0) {
      ctx.fillStyle = "#98a5c4";
      ctx.fillText(point.label, x - 10, height - 12);
    }
  });

  ctx.fillStyle = "#98a5c4";
  ctx.font = "12px Segoe UI, sans-serif";
  ctx.fillText(`${points.length} ${kind} period(s), each rounding to ${usd(0)}.`, 12, 20);
  ctx.fillText("Cost Explorer answered them, so this is live data with no billable spend.", 12, 38);
}

function drawTrend(points, emptyText, kind) {
  const box = prepCanvas(document.getElementById("trend-chart"));
  if (!box) return;
  const { ctx, width, height } = box;
  if (!points.length) {
    ctx.fillStyle = "#98a5c4";
    ctx.font = "12px Segoe UI, sans-serif";
    ctx.fillText(emptyText || "No trend data", 12, 24);
    return;
  }

  const pad = { top: 18, right: 14, bottom: 34, left: 58 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  const costs = points.map((point) => (typeof point.Cost === "number" ? point.Cost : 0));
  if (costs.every((value) => value === 0)) {
    drawFlatTrend(ctx, width, height, pad, plotH, points, kind || "billing");
    return;
  }
  // Cost Explorer returns credits, so the axis spans from the most negative
  // period to the largest one and bars grow from a zero line, not from the
  // bottom edge. `|| 1` keeps a perfectly flat window drawable at all.
  const top = Math.max(...costs, 0) * 1.15;
  const bottom = Math.min(...costs, 0) * 1.15;
  const span = top - bottom || 1;
  const zeroY = pad.top + (plotH * top) / span;
  const step = plotW / points.length;
  const labelEvery = Math.ceil(points.length / 8);

  ctx.strokeStyle = "#26314f";
  ctx.fillStyle = "#98a5c4";
  ctx.font = "10.5px Segoe UI, sans-serif";
  ctx.lineWidth = 1;

  for (let line = 0; line <= 4; line += 1) {
    const y = pad.top + (plotH * line) / 4;
    ctx.beginPath();
    ctx.moveTo(pad.left, y);
    ctx.lineTo(width - pad.right, y);
    ctx.stroke();
    ctx.fillText(axisLabel(top - (span * line) / 4), 8, y + 3);
  }

  points.forEach((point, index) => {
    const barW = Math.max(3, Math.min(step * 0.55, 40));
    const x = pad.left + step * index + (step - barW) / 2;
    const h = ((point.Cost || 0) / span) * plotH;
    ctx.fillStyle = index === points.length - 1 ? "#6ee7b7" : "#4c8dff";
    ctx.fillRect(x, h >= 0 ? zeroY - h : zeroY, barW, Math.abs(h));
    ctx.fillStyle = "#98a5c4";
    if (index % labelEvery === 0) {
      ctx.fillText(point.label, x, height - 12);
    }
  });

  ctx.strokeStyle = "#43507a";
  ctx.beginPath();
  ctx.moveTo(pad.left, zeroY);
  ctx.lineTo(width - pad.right, zeroY);
  ctx.stroke();
}

function drawServices(rows, emptyText) {
  const box = prepCanvas(document.getElementById("service-chart"));
  const legend = document.getElementById("service-legend");
  clear(legend);

  // The legend is DOM, not canvas, so it is rebuilt even when the panel is
  // hidden - otherwise a hidden view would keep the previous dataset's labels.
  rows.forEach((row, index) => {
    const item = el("li");
    const dot = el("span", "dot");
    dot.style.background = PALETTE[index % PALETTE.length];
    item.appendChild(dot);
    item.appendChild(el("span", "", `${row.Value} ${pct(row.Percentage)}`));
    legend.appendChild(item);
  });

  if (!box) return;
  const { ctx, width, height } = box;

  if (!rows.length) {
    ctx.fillStyle = "#98a5c4";
    ctx.font = "12px Segoe UI, sans-serif";
    ctx.fillText(emptyText || "No spend recorded", 12, 24);
    return;
  }

  const total = rows.reduce((sum, row) => sum + (row.Cost || 0), 0);
  if (!(total > 0)) {
    // A zero or credited window is real billing data, but slices of it would
    // be meaningless (or drawn backwards), so name the data rather than
    // implying the chart is broken.
    ctx.fillStyle = "#98a5c4";
    ctx.font = "12px Segoe UI, sans-serif";
    ctx.fillText(`${rows.length} cost line(s) returned, window total ${usd(total)}.`, 12, 24);
    ctx.fillText(
      total === 0
        ? "Nothing material to share out, so no slices are drawn - see the service table."
        : "A credit or a zero-net window has no shares to chart - see the service table.",
      12,
      42,
    );
    return;
  }

  // total stays the real figure for the centre label; only the slice maths needs
  // a positive denominator, so a $0 account never sees an invented "$1.00".
  const denominator = total;
  const radius = Math.max(8, Math.min(width, height) / 2 - 14);
  const cx = width / 2;
  const cy = height / 2;
  let angle = -Math.PI / 2;

  rows.forEach((row, index) => {
    const sweep = ((row.Cost || 0) / denominator) * Math.PI * 2;
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.arc(cx, cy, radius, angle, angle + sweep);
    ctx.closePath();
    ctx.fillStyle = PALETTE[index % PALETTE.length];
    ctx.fill();
    angle += sweep;
  });

  ctx.globalCompositeOperation = "destination-out";
  ctx.beginPath();
  ctx.arc(cx, cy, radius * 0.6, 0, Math.PI * 2);
  ctx.fill();
  ctx.globalCompositeOperation = "source-over";

  ctx.fillStyle = "#e8ecf6";
  ctx.font = "600 13px Segoe UI, sans-serif";
  ctx.textAlign = "center";
  ctx.fillText(usd(total), cx, cy - 2);
  ctx.fillStyle = "#98a5c4";
  ctx.font = "10.5px Segoe UI, sans-serif";
  ctx.fillText("window total", cx, cy + 14);
  ctx.textAlign = "left";
}

function drawRegionBars(rows) {
  const wrap = document.getElementById("region-bars");
  clear(wrap);
  const max = Math.max(...rows.map((row) => row.Cost), 1);

  rows.forEach((row) => {
    const line = el("div", "bar-row");
    line.appendChild(el("span", "", row.Value));
    const track = el("div", "bar-track");
    const fill = el("div", "bar-fill");
    fill.style.width = `${Math.max(0, (row.Cost / max) * 100)}%`;
    track.appendChild(fill);
    line.appendChild(track);
    line.appendChild(el("span", "num", usd(row.Cost)));
    wrap.appendChild(line);
  });

  // A zero window leaves every track empty, which reads as a broken chart
  // unless the page says the regions themselves were really returned.
  if (rows.length && rows.every((row) => !row.Cost)) {
    wrap.appendChild(el(
      "p",
      "chart-note",
      `${rows.length} region(s) reported, each at ${usd(0)} - live data with no billable spend.`,
    ));
  }
}

/* -------------------------------------------------------------- sections */

function renderWaste(waste, meta) {
  const findings = waste.findings || {};
  const body = [];

  Object.keys(findings).forEach((type) => {
    findings[type].forEach((finding) => {
      const resource =
        finding.InstanceId || finding.VolumeId || finding.AllocationId ||
        finding.SnapshotId || finding.DBInstanceIdentifier || "-";
      const saving =
        finding.EstimatedMonthlySavingsUSD === null ||
        finding.EstimatedMonthlySavingsUSD === undefined
          ? "not quantified"
          : usd(finding.EstimatedMonthlySavingsUSD);
      body.push([
        { value: `${resource} (${meta.mode === "demo" ? "sample" : finding.Region || "-"})` },
        { value: finding.Finding || type },
        { value: saving, numeric: true },
        { value: finding.Recommendation || "-" },
      ]);
    });
  });

  fillTable("waste-table", body, 4, notRead("waste", "No idle resources found in this scan"));

  const chips = document.getElementById("waste-chips");
  clear(chips);
  const byType = (waste.summary && waste.summary.FindingsByType) || {};
  Object.keys(byType).forEach((type) => {
    const chip = el("div", "chip");
    chip.appendChild(el("span", "", type.replace(/([A-Z])/g, " $1").trim() + ": "));
    chip.appendChild(el("b", "", byType[type]));
    chips.appendChild(chip);
  });

  document.getElementById("pricing-note").textContent =
    (waste.summary && waste.summary.PricingAssumptions) || "";
}

function renderRightsizing(rows) {
  fillTable(
    "rightsizing-table",
    rows.map((row) => [
      { value: `${row.InstanceName || row.InstanceId} - ${row.InstanceType}` },
      { value: pct(row.MaxCPUPercent), numeric: true },
      {
        value: typeof row.MaxMemoryPercent === "number" ? pct(row.MaxMemoryPercent) : "n/a",
        numeric: true,
      },
      { value: row.MemoryDataSource === "cw_agent" ? "CloudWatch Agent" : "no CloudWatch Agent" },
    ]),
    4,
    notRead("rightsizing", "No right-sizing recommendations returned"),
  );
}

/* One entry per CommitmentAnalyzer metric: `block`/`key` locate the metric dict
 * in the report and `metric` is the percentage key inside it. A null metric
 * means nothing of that kind is purchased, which is a finding rather than an
 * error, so it gets its own label instead of "n/a". */
const COMMITMENT_CARDS = [
  {
    label: "Savings Plans utilization",
    block: "SavingsPlans",
    key: "Utilization",
    metric: "UtilizationPercentage",
    empty: "No Savings Plans purchased.",
    sub: (data) => `${usd(data.UnusedCommitmentUSD)} commitment unused, net ${usd(data.NetSavingsUSD)}`,
  },
  {
    label: "Savings Plans coverage",
    block: "SavingsPlans",
    key: "Coverage",
    metric: "CoveragePercentage",
    empty: "No Savings Plans coverage reported.",
    sub: (data) => `${usd(data.UncoveredOnDemandCostUSD)} ran at on-demand rates`,
  },
  {
    label: "RI utilization",
    block: "ReservedInstances",
    key: "Utilization",
    metric: "UtilizationPercentage",
    empty: "No Reserved Instances purchased.",
    sub: (data) => `${data.UnusedHours} unused RI hours, net ${usd(data.NetRISavingsUSD)}`,
  },
  {
    label: "RI coverage",
    block: "ReservedInstances",
    key: "Coverage",
    metric: "CoverageHoursPercentage",
    empty: "No Reserved Instance coverage reported.",
    sub: (data) => `${data.ReservedHours}h reserved of ${data.ReservedHours + data.OnDemandHours}h total`,
  },
];

function renderCommitments(commitments, unreadable) {
  const wrap = document.getElementById("commitment-cards");
  clear(wrap);

  COMMITMENT_CARDS.forEach((card) => {
    const block = commitments[card.block] || {};
    const data = block[card.key];
    const node = el("div", "card");
    node.appendChild(el("div", "label", card.label));
    if (data && typeof data[card.metric] === "number") {
      node.appendChild(el("div", "value", pct(data[card.metric])));
      node.appendChild(el("div", "sub", card.sub(data)));
    } else if (unreadable) {
      // A null metric usually means "nothing purchased", which is a finding.
      // When the section failed to read at all it means "unknown", and the card
      // must not claim AWS never confirmed.
      node.appendChild(el("div", "value", "unknown"));
      node.appendChild(el("div", "sub", "Commitment data could not be read - see the error above."));
    } else {
      node.appendChild(el("div", "value", "none"));
      node.appendChild(el("div", "sub", card.empty));
    }
    wrap.appendChild(node);
  });

  const list = document.getElementById("commitment-observations");
  clear(list);
  (commitments.Observations || []).forEach((text) => list.appendChild(el("li", "", text)));
  if (!(commitments.Observations || []).length) {
    list.appendChild(
      el("li", "", unreadable
        ? "No observations: this section could not be read."
        : "No observations returned for this period."),
    );
  }
}

function renderBreakdown(payload) {
  const services = payload.services || [];
  const regions = payload.regions || [];

  fillTable(
    "service-table",
    services.map((row) => [
      { value: row.Value },
      { value: usd(row.Cost), numeric: true },
      { value: pct(row.Percentage), numeric: true },
    ]),
    3,
    notRead("services", noRowsText(payload.meta)),
  );
  fillTable(
    "region-table",
    regions.map((row) => [
      { value: row.Value },
      { value: usd(row.Cost), numeric: true },
      { value: pct(row.Percentage), numeric: true },
    ]),
    3,
    notRead("regions", noRowsText(payload.meta)),
  );
  drawRegionBars(regions);
}

/* ------------------------------------------------------------------ chrome */

function renderTrend(payload) {
  const meta = payload.meta;
  const kind = meta.granularity === "DAILY" ? "daily" : "monthly";
  const points = (payload.trend || []).map((point) => ({
    label: point.Label || point.Period,
    Cost: point.Cost,
  }));
  drawTrend(points, notRead("trend", "No trend data for this window"), kind);

  const estimated = (payload.trend || []).filter((point) => point.Estimated).length;
  const currency = (payload.trend || [])[0];
  let note =
    meta.mode === "demo"
      ? `Sample spend, one bar per ${kind} bucket in the selected window.`
      : `Unblended cost from Cost Explorer, one bar per ${kind} period` +
        `${currency && currency.Currency ? ` (${currency.Currency})` : ""}.`;
  if (estimated) {
    note += ` ${estimated} period${estimated === 1 ? "" : "s"} AWS returned as an estimate.`;
  }
  if (meta.period) {
    note += ` ${meta.period.Note}`;
  }
  document.getElementById("trend-note").textContent = note;
}

function renderBanners(payload) {
  const meta = payload.meta;
  if (meta.mode === "demo") {
    document.getElementById("demo-banner-text").textContent = meta.warning;
    show("demo-banner", true);
  } else {
    show("demo-banner", false);
  }

  const errors = payload.errors || [];
  const status = meta.status || {};
  // Only a clean live run gets the "connected" banner. A run with errors keeps
  // explaining what could not be read, even when the spend section itself
  // succeeded, so a green banner never sits above an unexplained failure.
  const connected = meta.mode === "live" && !errors.length
    && String(status.State || "").indexOf("connected") === 0;
  if (connected) {
    document.getElementById("live-banner-text").textContent =
      status.Message || "Live AWS data connected.";
    document.getElementById("live-banner-note").textContent =
      `Live Cost Explorer figures for ${meta.periodStart} to ${meta.periodEnd}` +
      ` (${meta.days} days, ${String(meta.granularity || "").toLowerCase()} periods) - not sample data.`;
    show("live-banner", true);
  } else {
    show("live-banner", false);
  }

  if (meta.mode === "live" && errors.length) {
    const list = document.getElementById("error-list");
    clear(list);
    errors.forEach((error) => {
      const head =
        `${error.Section}${error.Code ? ` (${error.Code})` : ""}: ${error.Message}`;
      list.appendChild(el("li", "", error.Hint ? `${head} - ${error.Hint}` : head));
    });
    // Partial data is a different message from no data: a presenter needs to
    // know whether the numbers on screen are still usable.
    document.getElementById("error-title").textContent = (payload.services || []).length
      ? "Some live AWS sections could not be read"
      : "Live AWS data unavailable";
    show("error-banner", true);
  } else {
    show("error-banner", false);
  }
}

function renderFooter(payload) {
  const meta = payload.meta;
  const period = meta.period;
  document.getElementById("period-line").textContent =
    `${meta.periodStart} to ${meta.periodEnd} - ${meta.days} days` +
    (period ? `, ${period.Granularity} periods` : "");
  document.getElementById("source-line").textContent = meta.dataSource;
  document.getElementById("generated-line").textContent = `generated ${meta.generatedAt}`;

  // Downloads must carry the same parameters the request did; meta.regionScope
  // is a human-readable scope string ("sample account (all regions)"), not a
  // region code the API would accept.
  const suffix = `days=${state.days}&mode=${state.mode}&region=${state.region}`;
  ["btn-csv", "btn-json", "btn-csv-2", "btn-json-2"].forEach((id) => {
    const link = document.getElementById(id);
    const path = id.includes("json") ? "/api/report/json?" : "/api/report/csv?";
    link.href = path + suffix;
  });
}

function renderCsvPreview() {
  const preview = document.getElementById("csv-preview");
  const url = `/api/report/csv?days=${state.days}&mode=${state.mode}&region=${state.region}`;
  fetch(url)
    .then((response) => response.text())
    .then((text) => {
      const lines = text.split(/\r?\n/).filter((line) => line.length);
      preview.textContent = lines.slice(0, 9).join("\n");
    })
    .catch((error) => {
      preview.textContent = `Preview unavailable: ${error}`;
    });
}

function paint(payload) {
  state.payload = payload;
  failedSections = readFailedSections(payload.errors || []);
  // Banners first: if anything below throws, the page must not keep the
  // previous mode's DEMO label over this mode's numbers.
  renderBanners(payload);
  renderCards(payload.totals, payload.meta);
  renderTrend(payload);
  renderServices(payload);
  renderBreakdown(payload);
  renderWaste(payload.waste, payload.meta);
  renderRightsizing(payload.rightsizing || []);
  renderCommitments(payload.commitments || {}, failedSections.has("commitments"));
  renderFooter(payload);
  renderCsvPreview();
}

function renderServices(payload) {
  drawServices(
    payload.services || [],
    notRead("services", noRowsText(payload.meta)),
  );
}

/* ------------------------------------------------------------- ebs volumes */

// Volumes are regional and change far more often than billing, so this panel
// polls `DescribeVolumes` on its own short timer instead of waiting for a full
// dashboard reload, which costs tens of seconds of Cost Explorer time.
const EBS_REFRESH_MS = 60000;

// Mirrors web/ebs.py MAX_REGIONS: trimming here keeps a typed-in list from
// silently disagreeing with what the server is willing to query.
const EBS_MAX_REGIONS = 8;

let ebsTimer = null;
let ebsInFlight = false;
let ebsRefreshing = false;

function ebsRegions() {
  const input = document.getElementById("ebs-regions");
  const codes = (input && input.value ? input.value : "")
    .split(",").map((code) => code.trim().toLowerCase()).filter(Boolean);
  const unique = [];
  codes.forEach((code) => {
    if (unique.indexOf(code) === -1) unique.push(code);
  });
  // Empty is not a mistake: the server falls back to the dashboard's own region.
  return unique.slice(0, EBS_MAX_REGIONS);
}

// The tag colour is a scanning cue only. Any state AWS returns that is not the
// documented attached/detached pair stays neutral instead of being dressed up as
// one of them.
const attachmentTag = (label) => {
  if (label === "Attached") return "is-attached";
  if (label === "Detached") return "is-detached";
  return "";
};

function ebsEmptyText(meta, status) {
  if (meta && meta.mode === "demo") return "No sample volumes listed";
  if (status.State === "unavailable") {
    return "No volume data - no region could be queried. See the error below.";
  }
  if (status.State === "connected-empty") {
    return "AWS answered every requested region and returned no volumes.";
  }
  if (status.State === "partial") {
    return "The regions that answered returned no volumes; the rest are listed as errors.";
  }
  return "No volumes to list";
}

function renderEbs() {
  const payload = state.ebs;
  const meta = payload ? payload.meta : null;
  const status = (meta && meta.status) || {};
  const volumes = (payload && payload.volumes) || [];
  const errors = (payload && payload.errors) || [];

  fillTable(
    "ebs-table",
    volumes.map((row) => ([
      { value: row.VolumeId, cls: "mono" },
      { value: row.VolumeState },
      { value: row.AttachmentState, tag: attachmentTag(row.AttachmentState) },
      {
        value: row.InstanceId
          ? row.InstanceId + (row.AttachmentCount > 1 ? ` (+${row.AttachmentCount - 1} more)` : "")
          : "none",
        cls: row.InstanceId ? "mono" : "",
      },
      { value: row.AvailabilityZone },
      { value: typeof row.SizeGiB === "number" ? String(row.SizeGiB) : "n/a", numeric: true },
      { value: row.Region },
    ])),
    7,
    ebsEmptyText(meta, status),
  );

  const line = document.getElementById("ebs-status");
  clear(line);
  const succeeded = status.State === "connected" || status.State === "connected-empty";
  const stale = Boolean(state.ebsError);
  line.classList.toggle("is-ok", succeeded && !stale && !ebsRefreshing);
  line.classList.toggle("is-warn", !succeeded || stale || ebsRefreshing);

  let text;
  if (ebsRefreshing && !volumes.length) {
    text = `Reading EBS volumes in ${ebsRegions().join(", ") || state.region}...`;
  } else if (stale) {
    text = payload
      ? `Refresh failed: ${state.ebsError}. Showing the volume list from ${state.ebsFetchedAt}.`
      : `Volume status unavailable: ${state.ebsError}`;
  } else if (!payload) {
    text = "Volume status not loaded yet.";
  } else {
    text = `${status.Message} Last refreshed ${state.ebsFetchedAt}.`;
  }
  line.appendChild(el("span", "", text));

  const note = document.getElementById("ebs-note");
  clear(note);
  const isDemo = Boolean(meta) && meta.mode === "demo";
  const requested = ((meta && meta.regionsRequested) || ebsRegions()).join(", ");
  const answered = (meta && meta.regionsQueried) || [];
  note.appendChild(el(
    "span",
    "",
    `Read-only ec2:DescribeVolumes. Requested ${requested || state.region}. Auto-refresh every `
    + `${EBS_REFRESH_MS / 1000}s while this tab is visible. `
    + (isDemo
      ? "No region was queried: these rows are labelled samples and no AWS API was called."
      : `Answered by ${answered.length ? answered.join(", ") : "no region"}. `
        + "States and attachments are exactly what AWS returned; nothing is inferred."),
  ));

  const list = document.getElementById("ebs-errors");
  clear(list);
  errors.forEach((error) => {
    const head = `${error.Region} (${error.Code}): ${error.Message}`;
    list.appendChild(el("li", "", error.Hint ? `${head} - ${error.Hint}` : head));
  });
  if (!errors.length && state.ebsError && !payload) {
    list.appendChild(el("li", "", state.ebsError));
  }
}

function loadEbs(trigger) {
  if (ebsInFlight) {
    // A regional scan can outlive a timer tick; overlapping polls would only
    // cost more AWS calls and race each other for the same table.
    return;
  }
  ebsInFlight = true;
  ebsRefreshing = trigger !== "auto";
  renderEbs();

  const regions = encodeURIComponent(ebsRegions().join(","));
  const url = `/api/ebs?mode=${state.mode}&region=${state.region}&regions=${regions}`;
  fetch(url)
    .then((response) =>
      response.json().then((body) => {
        if (!response.ok && !body.meta) {
          throw new Error(body.error || `request failed (${response.status})`);
        }
        return body;
      }))
    .then((payload) => {
      state.ebs = payload;
      state.ebsError = null;
      state.ebsFetchedAt = payload.meta.fetchedAt;
    })
    .catch((error) => {
      // Keep the last real volume list on screen and say it is stale; a failed
      // poll must never be shown as an empty account or replaced with samples.
      state.ebsError = String(error.message || error);
    })
    .finally(() => {
      ebsInFlight = false;
      ebsRefreshing = false;
      renderEbs();
    });
}

function startEbsTimer() {
  if (ebsTimer) clearInterval(ebsTimer);
  ebsTimer = setInterval(() => {
    // A hidden tab polling EC2 spends AWS calls nobody is looking at.
    if (document.hidden || ebsInFlight) return;
    loadEbs("auto");
  }, EBS_REFRESH_MS);
}

function refreshEbs(trigger) {
  loadEbs(trigger);
  startEbsTimer();
}

/* ------------------------------------------------------------------- fetch */

function setStatus(text, isError) {
  const node = document.getElementById("load-status");
  node.textContent = text || "";
  node.hidden = !text;
  node.classList.toggle("is-error", Boolean(isError));
}

function loadDashboard() {
  const target = document.querySelector(".main");
  target.classList.add("is-loading");
  // A live request is several Cost Explorer and CloudWatch calls, so say what
  // is happening and that the figures on screen are still the previous ones.
  setStatus(
    state.mode === "live"
      ? `Loading live billing for the last ${state.days} days from Cost Explorer. ` +
        "This takes up to a minute; the figures below are still the previous response."
      : "Loading sample data...",
  );

  const url = `/api/dashboard?days=${state.days}&mode=${state.mode}&region=${state.region}`;
  const startedAt = Date.now();
  fetch(url)
    .then((response) =>
      response.json().then((body) => {
        if (!response.ok && !body.meta) {
          throw new Error(body.error || `request failed (${response.status})`);
        }
        return body;
      }))
    .then((payload) => {
      setStatus(null);
      paint(payload);
    })
    .catch((error) => {
      const seconds = Math.round((Date.now() - startedAt) / 1000);
      setStatus(`Request failed after ${seconds}s: ${error.message || error}`, true);
      // The dashboard never received a payload, so nothing on screen can be
      // claimed as live data - and the previous response's banner would
      // otherwise still be describing it.
      show("live-banner", false);
      document.getElementById("error-title").textContent = "Live AWS request failed";
      const list = document.getElementById("error-list");
      clear(list);
      list.appendChild(el("li", "", String(error.message || error)));
      show("error-banner", true);
    })
    .finally(() => target.classList.remove("is-loading"));
}

function loadHealth() {
  fetch("/api/health")
    .then((response) => response.json())
    .then((body) => {
      const status = document.getElementById("health-status");
      const detail = document.getElementById("health-detail");
      clear(status);
      clear(detail);

      const aws = body.aws || {};
      const usable = aws.credentials === "available";
      status.classList.toggle("is-ok", usable);
      status.classList.toggle("is-warn", !usable);
      status.appendChild(
        el("span", "", usable ? "credentials resolved" : "no usable credentials"),
      );
      detail.appendChild(
        el(
          "span",
          "",
          usable
            ? "Live mode can query Cost Explorer. Demo mode never does."
            : `${aws.detail || "Credentials unavailable."} Demo mode is unaffected.`,
        ),
      );

      const liveInput = document.querySelector('input[name="mode"][value="live"]');
      if (!body.liveModeEnabled) {
        liveInput.disabled = true;
        liveInput.parentElement.style.opacity = "0.5";
      }
    })
    .catch(() => {
      const status = document.getElementById("health-status");
      clear(status);
      status.classList.add("is-warn");
      status.appendChild(el("span", "", "health check failed"));
    });
}

/* ---------------------------------------------------------------- wiring */

function wireNavigation() {
  const titles = {
    overview: "Overview",
    breakdown: "Cost breakdown",
    optimization: "Optimization plan",
    volumes: "EBS volume status",
    commitments: "Commitments",
    reports: "Reports",
  };

  document.querySelectorAll(".nav-item").forEach((button) => {
    button.addEventListener("click", () => {
      const view = button.dataset.view;
      document.querySelectorAll(".nav-item").forEach((item) => {
        item.classList.toggle("is-active", item === button);
      });
      document.querySelectorAll(".view").forEach((section) => {
        section.classList.toggle("is-active", section.dataset.view === view);
      });
      document.getElementById("view-title").textContent = titles[view];
      if (state.payload) {
        renderTrend(state.payload);
        renderServices(state.payload);
        drawRegionBars(state.payload.regions || []);
      }
    });
  });
}

function wireFilters() {
  document.querySelectorAll(".seg").forEach((button) => {
    button.addEventListener("click", () => {
      state.days = Number(button.dataset.days);
      document.querySelectorAll(".seg").forEach((item) => {
        item.classList.toggle("is-active", item === button);
      });
      loadDashboard();
    });
  });

  document.querySelectorAll('input[name="mode"]').forEach((input) => {
    input.addEventListener("change", () => {
      state.mode = input.value;
      // Sample volumes and real ones must never share the table across a mode
      // switch, so the EBS section is dropped and re-read under the new mode.
      state.ebs = null;
      state.ebsError = null;
      state.ebsFetchedAt = null;
      renderEbs();
      loadDashboard();
      refreshEbs("mode");
    });
  });

  document.getElementById("btn-refresh").addEventListener("click", () => {
    loadHealth();
    loadDashboard();
    refreshEbs("manual");
  });
}

function wireEbs() {
  document.getElementById("ebs-refresh").addEventListener("click", () => {
    refreshEbs("manual");
  });
  document.getElementById("ebs-regions").addEventListener("change", () => {
    // The previous response describes a different region list, so showing it
    // under the new one would be a lie; clear first, then read the new scope.
    state.ebs = null;
    state.ebsError = null;
    state.ebsFetchedAt = null;
    renderEbs();
    refreshEbs("regions");
  });
}

wireNavigation();
wireFilters();
wireEbs();
loadHealth();
loadDashboard();
renderEbs();
refreshEbs("initial");
window.addEventListener("resize", () => {
  if (state.payload) {
    renderTrend(state.payload);
    renderServices(state.payload);
  }
});
