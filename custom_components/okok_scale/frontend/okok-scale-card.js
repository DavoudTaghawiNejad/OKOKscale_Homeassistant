/**
 * okok-scale-card - per-person weight + relative-body-fat history card for
 * the OKOK Body Composition Scale integration.
 *
 * No external dependencies (no charting library, no CDN fetches) so it
 * stays light on a Raspberry Pi 3: history is pulled through Home
 * Assistant's own history websocket API and rendered as two small inline
 * SVG line charts (weight, and body fat relative to the person's
 * baseline - 100% = baseline) stacked in one card, since the beginning of
 * their measurements.
 *
 * This card only handles history; for a compact "current value + reset
 * baseline button" card, use a stock entities/tile card with
 * sensor.okok_scale_<id>_weight, sensor.okok_scale_<id>_body_fat_relative,
 * and button.okok_scale_<id>_reset_baseline - see the README for a ready
 * YAML snippet.
 *
 * Usage (Lovelace YAML):
 *   type: custom:okok-scale-card
 *   people:            # optional - auto-discovered from
 *     - me              # sensor.okok_scale_<id>_weight entities if omitted
 *     - wife
 *
 * `people` entries may also be objects: {id: "me", name: "Me"}.
 */

const RANGES = {
  "30d": { label: "30d", days: 30 },
  "90d": { label: "90d", days: 90 },
  "1y": { label: "1y", days: 365 },
  all: { label: "All", days: 36500 },
};

const CHARTS = [
  { metric: "weight", suffix: "weight", title: "Weight (kg)", digits: 1 },
  { metric: "body_fat_relative", suffix: "body_fat_relative", title: "Body fat, relative to baseline (%)", digits: 1 },
];

function entityIdFor(personId, suffix) {
  return `sensor.okok_scale_${personId}_${suffix}`;
}

class OkokScaleCard extends HTMLElement {
  static getStubConfig() {
    return { people: [] };
  }

  setConfig(config) {
    this._config = config || {};
    this._selectedRange = this._config.default_range && RANGES[this._config.default_range] ? this._config.default_range : "30d";
    this._selectedPersonId = null;
    this._people = null;
    this._historyCache = new Map();
  }

  getCardSize() {
    return 8;
  }

  set hass(hass) {
    const firstRun = !this._hass;
    this._hass = hass;

    if (firstRun) {
      this._root = this.attachShadow({ mode: "open" });
      this._root.innerHTML = this._skeletonHtml();
      this._els = {
        tabs: this._root.querySelector(".okok-tabs"),
        ranges: this._root.querySelector(".okok-ranges"),
        charts: CHARTS.map((c) => this._root.querySelector(`.okok-chart[data-metric="${c.metric}"]`)),
        empty: this._root.querySelector(".okok-empty"),
        download: this._root.querySelector(".okok-download"),
      };
      this._root.querySelector("style").textContent = this._styles();
    }

    this._discoverPeople();

    if (!this._people.length) {
      this._els.empty.style.display = "block";
      this._els.tabs.style.display = "none";
      return;
    }
    this._els.empty.style.display = "none";
    this._els.tabs.style.display = "flex";

    this._renderTabs();
    this._renderRanges();
    this._renderDownloadLink();
    this._refreshCharts();
  }

  // ---- static shell ----------------------------------------------------

  _skeletonHtml() {
    const chartMarkup = CHARTS.map(
      (c) => `
        <div class="okok-chart-block">
          <div class="okok-chart-title">${c.title}</div>
          <svg class="okok-chart" data-metric="${c.metric}" viewBox="0 0 600 180" preserveAspectRatio="none"></svg>
        </div>`
    ).join("");

    return `
      <style></style>
      <ha-card>
        <div class="okok-header">
          <div class="okok-tabs"></div>
          <div class="okok-ranges"></div>
        </div>
        <div class="okok-empty" style="display:none">
          No OKOK Scale people found. Add one via
          Settings &rarr; Devices &amp; Services &rarr; OKOK Body Composition Scale &rarr; Configure,
          or set the <code>people</code> option on this card.
        </div>
        ${chartMarkup}
        <a class="okok-download" href="#" target="_blank" rel="noopener">Download full CSV</a>
      </ha-card>
    `;
  }

  _styles() {
    return `
      ha-card { padding: 16px; }
      .okok-header { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; margin-bottom: 8px; }
      .okok-tabs { display: flex; gap: 4px; flex-wrap: wrap; }
      .okok-tab {
        border: none; cursor: pointer; padding: 4px 12px; border-radius: 16px;
        background: var(--secondary-background-color, #eee);
        color: var(--primary-text-color, #212121);
        font-size: 0.9em;
      }
      .okok-tab.active { background: var(--primary-color, #03a9f4); color: var(--text-primary-color, #fff); }
      .okok-ranges { display: flex; gap: 4px; }
      .okok-range-btn {
        border: none; cursor: pointer; padding: 4px 10px; border-radius: 8px;
        background: transparent; color: var(--secondary-text-color, #727272); font-size: 0.85em;
      }
      .okok-range-btn.active { background: var(--secondary-background-color, #eee); color: var(--primary-text-color, #212121); font-weight: 500; }
      .okok-chart-block { margin-top: 10px; }
      .okok-chart-title { font-size: 0.8em; color: var(--secondary-text-color, #727272); margin-bottom: 2px; }
      .okok-chart { width: 100%; height: 140px; display: block; }
      .okok-chart .line { fill: none; stroke: var(--primary-color, #03a9f4); stroke-width: 2; }
      .okok-chart .baseline-line { stroke: var(--divider-color, #e0e0e0); stroke-width: 1; stroke-dasharray: 4 3; }
      .okok-chart .dot { fill: var(--primary-color, #03a9f4); }
      .okok-chart .axis-label { fill: var(--secondary-text-color, #727272); font-size: 9px; }
      .okok-chart .grid { stroke: var(--divider-color, #e0e0e0); stroke-width: 1; }
      .okok-chart .empty-msg { fill: var(--secondary-text-color, #727272); font-size: 12px; }
      .okok-empty { color: var(--secondary-text-color, #727272); font-size: 0.9em; padding: 8px 0; }
      .okok-download { display: block; margin-top: 12px; font-size: 0.85em; color: var(--primary-color, #03a9f4); text-decoration: none; }
      .okok-download:hover { text-decoration: underline; }
    `;
  }

  // ---- people / person selection ----------------------------------------

  _discoverPeople() {
    if (this._config.people && this._config.people.length) {
      this._people = this._config.people.map((p) => (typeof p === "string" ? { id: p, name: p } : p));
    } else {
      const found = new Map();
      for (const entityId of Object.keys(this._hass.states)) {
        const match = entityId.match(/^sensor\.okok_scale_(.+)_weight$/);
        if (!match) continue;
        const personId = match[1];
        const state = this._hass.states[entityId];
        const rawName = (state.attributes.friendly_name || personId).replace(/\s+Weight$/i, "");
        found.set(personId, { id: personId, name: rawName });
      }
      this._people = [...found.values()];
    }
    if (!this._people.some((p) => p.id === this._selectedPersonId)) {
      this._selectedPersonId = this._people.length ? this._people[0].id : null;
    }
  }

  _renderTabs() {
    this._els.tabs.innerHTML = "";
    for (const person of this._people) {
      const btn = document.createElement("button");
      btn.className = "okok-tab" + (person.id === this._selectedPersonId ? " active" : "");
      btn.textContent = person.name;
      btn.addEventListener("click", () => {
        this._selectedPersonId = person.id;
        this._renderTabs();
        this._renderDownloadLink();
        this._refreshCharts();
      });
      this._els.tabs.appendChild(btn);
    }
  }

  _renderRanges() {
    this._els.ranges.innerHTML = "";
    for (const key of Object.keys(RANGES)) {
      const btn = document.createElement("button");
      btn.className = "okok-range-btn" + (key === this._selectedRange ? " active" : "");
      btn.textContent = RANGES[key].label;
      btn.addEventListener("click", () => {
        this._selectedRange = key;
        this._renderRanges();
        this._refreshCharts();
      });
      this._els.ranges.appendChild(btn);
    }
  }

  _renderDownloadLink() {
    const personId = this._selectedPersonId;
    if (!personId) return;
    const weightState = this._hass.states[entityIdFor(personId, "weight")];
    const url = weightState && weightState.attributes.csv_download_url;
    if (url) {
      this._els.download.href = url;
      this._els.download.style.display = "block";
      this._els.download.textContent = `Download ${this._personName(personId)}'s full CSV`;
    } else {
      this._els.download.style.display = "none";
    }
  }

  _personName(personId) {
    const person = this._people.find((p) => p.id === personId);
    return person ? person.name : personId;
  }

  // ---- charts -----------------------------------------------------------

  async _refreshCharts() {
    const personId = this._selectedPersonId;
    if (!personId) return;

    await Promise.all(
      CHARTS.map(async (chartDef, index) => {
        const cacheKey = `${personId}:${chartDef.metric}:${this._selectedRange}`;
        let points = this._historyCache.get(cacheKey);
        if (!points) {
          points = await this._fetchHistory(personId, chartDef.suffix);
          this._historyCache.set(cacheKey, points);
        }
        if (personId === this._selectedPersonId) {
          this._drawChart(this._els.charts[index], points, { showBaselineLine: chartDef.metric === "body_fat_relative" });
        }
      })
    );
  }

  async _fetchHistory(personId, suffix) {
    const entityId = entityIdFor(personId, suffix);
    const days = RANGES[this._selectedRange].days;
    const start = new Date(Date.now() - days * 86400000);

    try {
      const result = await this._hass.callWS({
        type: "history/history_during_period",
        start_time: start.toISOString(),
        entity_ids: [entityId],
        minimal_response: false,
        no_attributes: true,
        significant_changes_only: false,
      });
      const raw = (result && result[entityId]) || [];
      return raw
        .map((item) => ({
          t: new Date(item.last_updated || item.lu).getTime(),
          v: Number(item.state !== undefined ? item.state : item.s),
        }))
        .filter((p) => Number.isFinite(p.v) && Number.isFinite(p.t))
        .sort((a, b) => a.t - b.t);
    } catch (err) {
      console.warn("okok-scale-card: history fetch failed", err); // eslint-disable-line no-console
      return [];
    }
  }

  _drawChart(svg, points, { showBaselineLine } = {}) {
    const W = 600;
    const H = 180;
    const padL = 36;
    const padR = 10;
    const padT = 10;
    const padB = 20;

    while (svg.firstChild) svg.removeChild(svg.firstChild);

    if (!points.length) {
      const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
      text.setAttribute("x", W / 2);
      text.setAttribute("y", H / 2);
      text.setAttribute("text-anchor", "middle");
      text.setAttribute("class", "empty-msg");
      text.textContent = "No data in this range yet";
      svg.appendChild(text);
      return;
    }

    let minV = Math.min(...points.map((p) => p.v));
    let maxV = Math.max(...points.map((p) => p.v));
    if (showBaselineLine) {
      // Always keep 100% (the baseline) in view, even if every point is
      // above or below it, so the reference line is never off-chart.
      minV = Math.min(minV, 100);
      maxV = Math.max(maxV, 100);
    }
    const vSpan = maxV - minV || 1;
    const minT = points[0].t;
    const maxT = points[points.length - 1].t;
    const tSpan = maxT - minT || 1;

    const x = (t) => padL + ((t - minT) / tSpan) * (W - padL - padR);
    const y = (v) => H - padB - ((v - minV) / vSpan) * (H - padT - padB);

    const ns = "http://www.w3.org/2000/svg";

    // Horizontal gridlines + y labels (min / mid / max).
    for (const frac of [0, 0.5, 1]) {
      const value = minV + frac * vSpan;
      const yy = y(value);
      const line = document.createElementNS(ns, "line");
      line.setAttribute("x1", padL);
      line.setAttribute("x2", W - padR);
      line.setAttribute("y1", yy);
      line.setAttribute("y2", yy);
      line.setAttribute("class", "grid");
      svg.appendChild(line);

      const label = document.createElementNS(ns, "text");
      label.setAttribute("x", 2);
      label.setAttribute("y", yy + 3);
      label.setAttribute("class", "axis-label");
      label.textContent = `${value.toFixed(1)}`;
      svg.appendChild(label);
    }

    if (showBaselineLine) {
      const yy = y(100);
      const line = document.createElementNS(ns, "line");
      line.setAttribute("x1", padL);
      line.setAttribute("x2", W - padR);
      line.setAttribute("y1", yy);
      line.setAttribute("y2", yy);
      line.setAttribute("class", "baseline-line");
      svg.appendChild(line);
    }

    // Date labels (first / last).
    const dateFmt = (t) => new Date(t).toLocaleDateString(undefined, { month: "short", day: "numeric" });
    for (const [t, anchor, xx] of [
      [minT, "start", padL],
      [maxT, "end", W - padR],
    ]) {
      const label = document.createElementNS(ns, "text");
      label.setAttribute("x", xx);
      label.setAttribute("y", H - 4);
      label.setAttribute("text-anchor", anchor);
      label.setAttribute("class", "axis-label");
      label.textContent = dateFmt(t);
      svg.appendChild(label);
    }

    const path = document.createElementNS(ns, "path");
    const d = points.map((p, i) => `${i === 0 ? "M" : "L"}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join(" ");
    path.setAttribute("d", d);
    path.setAttribute("class", "line");
    svg.appendChild(path);

    // Only draw dots when there aren't too many points, to stay legible.
    if (points.length <= 120) {
      for (const p of points) {
        const dot = document.createElementNS(ns, "circle");
        dot.setAttribute("cx", x(p.t).toFixed(1));
        dot.setAttribute("cy", y(p.v).toFixed(1));
        dot.setAttribute("r", 2.5);
        dot.setAttribute("class", "dot");
        svg.appendChild(dot);
      }
    }
  }
}

customElements.define("okok-scale-card", OkokScaleCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "okok-scale-card",
  name: "OKOK Scale Card",
  description: "Per-person weight + relative-body-fat history for the OKOK Body Composition Scale integration.",
});
