const SNCF_TRAIN_DETAIL_URL =
  "https://www.sncf-voyageurs.com/fr/voyagez-avec-nous/horaires-et-itineraires/recherche-de-train/detail-train/";

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function formatLocalDateFromTs(tsSeconds, fallbackIso) {
  if (tsSeconds) {
    const d = new Date(tsSeconds * 1000);
    const year = d.getFullYear();
    const month = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
  }
  if (fallbackIso && fallbackIso.length >= 10) {
    return fallbackIso.slice(0, 10);
  }
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
}

function buildSncfTrainUrl(trainNumber, departureTs, fallbackIso) {
  if (!trainNumber) return null;
  const dateStr = formatLocalDateFromTs(departureTs, fallbackIso);
  return `${SNCF_TRAIN_DETAIL_URL}?dateCirculation=${encodeURIComponent(dateStr)}&numeroCirculation=${encodeURIComponent(trainNumber)}`;
}

/* ==========================================================================
   1. WIDGET POUR UN AXE COMPLET (SncfRouteSensor) -> custom:sncf-route-card
   ========================================================================== */
class SncfRouteCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._lastState = undefined;
  }

  setConfig(config) {
    if (!config.entity) {
      throw new Error("Vous devez définir une entité (entity)");
    }
    this.config = {
      max_trains: 4,
      ...config,
    };
    this._lastState = undefined;
    if (this._hass) {
      this._render();
    }
  }

  set hass(hass) {
    this._hass = hass;
    if (!this.config) return;
    const stateObj = hass.states[this.config.entity];
    if (this.content && this._lastState === stateObj) {
      return;
    }
    this._lastState = stateObj;
    this._render();
  }

  _ensureShell() {
    if (this.content) return;
    this.shadowRoot.innerHTML = `
      <style>
        ha-card {
          padding: 16px;
          border-radius: 12px;
          cursor: pointer;
        }
        .header {
          display: flex;
          justify-content: space-between;
          align-items: center;
          font-weight: 600;
          font-size: 1.1em;
          margin-bottom: 12px;
          padding-bottom: 8px;
          border-bottom: 1px solid var(--divider-color, rgba(255,255,255,0.12));
        }
        .header-title {
          display: flex;
          align-items: center;
          gap: 8px;
        }
        .header-count {
          font-size: 0.8em;
          opacity: 0.6;
        }
        .alert-box {
          background: rgba(255, 152, 0, 0.15);
          border-left: 4px solid #ff9800;
          padding: 8px 12px;
          border-radius: 6px;
          margin-bottom: 12px;
          font-size: 0.9em;
        }
        .alert-desc {
          margin-top: 4px;
          opacity: 0.85;
          font-size: 0.92em;
          line-height: 1.35;
        }
        .train-row {
          display: flex;
          justify-content: space-between;
          align-items: center;
          padding: 10px 12px;
          margin-bottom: 8px;
          border-radius: 8px;
          background: var(--secondary-background-color, rgba(127,127,127,0.08));
        }
        .train-row:last-child {
          margin-bottom: 0;
        }
        .train-row.next {
          border-left: 4px solid var(--primary-color, #03a9f4);
        }
        .times {
          font-size: 1.05em;
          font-weight: 600;
          display: flex;
          align-items: center;
          gap: 6px;
        }
        .arrow {
          opacity: 0.5;
          font-weight: normal;
        }
        .train-num {
          font-size: 0.8em;
          opacity: 0.75;
          background: rgba(127,127,127,0.15);
          padding: 2px 6px;
          border-radius: 4px;
          margin-left: 6px;
        }
        .badge {
          display: inline-flex;
          align-items: center;
          gap: 4px;
          padding: 4px 10px;
          border-radius: 12px;
          font-size: 0.85em;
          font-weight: 500;
          text-decoration: none;
          transition: filter 0.15s ease, transform 0.1s ease;
        }
        a.badge:hover {
          filter: brightness(1.18);
          transform: translateY(-1px);
        }
        .status-ok { background: rgba(76, 175, 80, 0.2); color: #4caf50; }
        .status-delay { background: rgba(255, 152, 0, 0.2); color: #ff9800; }
        .status-canceled { background: rgba(244, 67, 54, 0.2); color: #f44336; }
        .status-scheduled { background: rgba(158, 158, 158, 0.18); color: var(--secondary-text-color, #9e9e9e); }
        .empty {
          text-align: center;
          opacity: 0.6;
          padding: 12px 0;
          font-style: italic;
        }
      </style>
      <ha-card>
        <div class="card-content" id="container"></div>
      </ha-card>
    `;
    this.content = this.shadowRoot.querySelector("#container");
    this.shadowRoot.querySelector("ha-card").addEventListener("click", (e) => {
      if (e.target.closest("a.badge")) return;
      if (!this.config?.entity) return;
      this.dispatchEvent(
        new CustomEvent("hass-more-info", {
          bubbles: true,
          composed: true,
          detail: { entityId: this.config.entity },
        })
      );
    });
  }

  _render() {
    this._ensureShell();
    const entityId = this.config.entity;
    const stateObj = this._hass?.states?.[entityId];

    if (!stateObj) {
      this.content.innerHTML = `<div class="empty">Entité introuvable : ${escapeHtml(entityId)}</div>`;
      return;
    }

    const title = this.config.title || stateObj.attributes.friendly_name || entityId;
    const trains = (stateObj.attributes.trains || []).slice(0, this.config.max_trains);
    const alerts = stateObj.attributes.alerts || [];
    const downloadedAt = stateObj.attributes.downloaded_at;
    const trainCount = Number(stateObj.attributes.train_count || 0);

    let html = `
      <div class="header">
        <div class="header-title">
          <ha-icon icon="mdi:train"></ha-icon>
          <span>${escapeHtml(title)}</span>
        </div>
        <span class="header-count">${trainCount} train(s)</span>
      </div>
    `;

    if (alerts.length > 0) {
      const extraCount = alerts.length > 1 ? ` (+${alerts.length - 1})` : "";
      const alertTitle = escapeHtml(alerts[0].title);
      const desc =
        alerts[0].description && alerts[0].description !== alerts[0].title
          ? `<div class="alert-desc">${escapeHtml(alerts[0].description)}</div>`
          : "";
      html += `
        <div class="alert-box">
          ⚠️ <strong>${alertTitle}${extraCount}</strong>
          ${desc}
        </div>
      `;
    }

    if (trains.length === 0) {
      html += `<div class="empty">Aucun train à venir dans les 24h</div>`;
    } else {
      trains.forEach((t, index) => {
        const isRealtime = t.is_realtime !== false;
        let badgeClass = isRealtime ? "status-ok" : "status-scheduled";
        let statusLabel = isRealtime ? "À l'heure" : "Théorique";

        if (t.is_canceled) {
          badgeClass = "status-canceled";
          statusLabel = "Supprimé";
        } else if (t.delay_minutes > 0) {
          badgeClass = "status-delay";
          statusLabel = `+${Number(t.delay_minutes)} min`;
        }

        const depDisplay = escapeHtml(t.scheduled_departure || t.estimated_departure || "--:--");
        const arrDisplay = escapeHtml(t.scheduled_arrival || t.estimated_arrival || "--:--");
        const trainNumber = escapeHtml(t.train_number);

        const trainUrl = buildSncfTrainUrl(t.train_number, t.departure_ts, downloadedAt);
        const badgeHtml = trainUrl
          ? `<a class="badge ${badgeClass}" href="${escapeHtml(trainUrl)}" target="_blank" rel="noopener noreferrer" title="Voir le détail du train n°${trainNumber} sur SNCF Voyageurs">${escapeHtml(statusLabel)} <ha-icon icon="mdi:open-in-new" style="--mdc-icon-size: 13px;"></ha-icon></a>`
          : `<span class="badge ${badgeClass}">${escapeHtml(statusLabel)}</span>`;

        html += `
          <div class="train-row ${index === 0 ? "next" : ""}">
            <div class="times">
              <span>${depDisplay}</span>
              <span class="arrow">→</span>
              <span>${arrDisplay}</span>
              <span class="train-num">n°${trainNumber}</span>
            </div>
            ${badgeHtml}
          </div>
        `;
      });
    }

    this.content.innerHTML = html;
  }

  getCardSize() {
    return 3;
  }

  static getStubConfig(hass) {
    const routeEntity = Object.keys(hass.states).find(
      (e) => e.startsWith("sensor.") && hass.states[e].attributes.trains !== undefined
    );
    return { entity: routeEntity || "" };
  }
}

/* ==========================================================================
   2. WIDGET POUR UN TRAIN SPÉCIFIQUE (SncfSpecificTrainSensor) -> custom:sncf-specific-train-card
   ========================================================================== */
class SncfSpecificTrainCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._lastState = undefined;
  }

  setConfig(config) {
    if (!config.entity) {
      throw new Error("Vous devez définir une entité (entity)");
    }
    this.config = config;
    this._lastState = undefined;
    if (this._hass) {
      this._render();
    }
  }

  set hass(hass) {
    this._hass = hass;
    if (!this.config) return;
    const stateObj = hass.states[this.config.entity];
    if (this.content && this._lastState === stateObj) {
      return;
    }
    this._lastState = stateObj;
    this._render();
  }

  _ensureShell() {
    if (this.content) return;
    this.shadowRoot.innerHTML = `
      <style>
        ha-card {
          padding: 14px 16px;
          border-radius: 12px;
          cursor: pointer;
        }
        .wrapper {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 12px;
        }
        .left {
          display: flex;
          align-items: center;
          gap: 12px;
        }
        .icon-circle {
          width: 42px;
          height: 42px;
          border-radius: 50%;
          display: flex;
          align-items: center;
          justify-content: center;
        }
        .info {
          display: flex;
          flex-direction: column;
        }
        .title {
          font-weight: 600;
          font-size: 1em;
        }
        .subtitle {
          font-size: 0.85em;
          opacity: 0.75;
          margin-top: 2px;
        }
        .right {
          text-align: right;
        }
        .dep-time {
          font-size: 1.25em;
          font-weight: 700;
        }
        .status-pill {
          display: inline-flex;
          align-items: center;
          gap: 4px;
          padding: 2px 8px;
          border-radius: 10px;
          font-size: 0.8em;
          font-weight: 600;
          margin-top: 2px;
          text-decoration: none;
          transition: filter 0.15s ease;
        }
        a.status-pill:hover {
          filter: brightness(1.18);
        }
        .alert-banner {
          margin-top: 10px;
          padding: 6px 10px;
          border-radius: 6px;
          background: rgba(255, 152, 0, 0.15);
          border-left: 3px solid #ff9800;
          font-size: 0.85em;
        }
        /* Thèmes de couleurs selon état */
        .state-ok .icon-circle { background: rgba(76, 175, 80, 0.2); color: #4caf50; }
        .state-ok .status-pill { background: rgba(76, 175, 80, 0.2); color: #4caf50; }

        .state-delay .icon-circle { background: rgba(255, 152, 0, 0.2); color: #ff9800; }
        .state-delay .status-pill { background: rgba(255, 152, 0, 0.2); color: #ff9800; }

        .state-canceled .icon-circle { background: rgba(244, 67, 54, 0.2); color: #f44336; }
        .state-canceled .status-pill { background: rgba(244, 67, 54, 0.2); color: #f44336; }

        .state-inactive .icon-circle { background: rgba(158, 158, 158, 0.2); color: #9e9e9e; }
        .state-inactive .status-pill { background: rgba(158, 158, 158, 0.2); color: #9e9e9e; }
      </style>
      <ha-card>
        <div class="card-content" id="specific-container"></div>
      </ha-card>
    `;
    this.content = this.shadowRoot.querySelector("#specific-container");
    this.shadowRoot.querySelector("ha-card").addEventListener("click", (e) => {
      if (e.target.closest("a.status-pill")) return;
      if (!this.config?.entity) return;
      this.dispatchEvent(
        new CustomEvent("hass-more-info", {
          bubbles: true,
          composed: true,
          detail: { entityId: this.config.entity },
        })
      );
    });
  }

  _render() {
    this._ensureShell();
    const entityId = this.config.entity;
    const stateObj = this._hass?.states?.[entityId];

    if (!stateObj) {
      this.content.innerHTML = `<div>Entité introuvable : ${escapeHtml(entityId)}</div>`;
      return;
    }

    const attrs = stateObj.attributes;
    const title = this.config.title || attrs.friendly_name || `Train ${attrs.train_number || ""}`;
    const trainNum = attrs.train_number || "";
    const depTime = attrs.estimated_departure || "--:--";
    const schedDep = attrs.scheduled_departure || depTime;
    const isCanceled = attrs.is_canceled === true;
    const isRealtime = attrs.is_realtime !== false;
    const delayMin = Number(attrs.delay_minutes || 0);
    const alerts = attrs.alerts || [];
    const isInactive =
      stateObj.state === "Inactive / Outside window" ||
      (!attrs.departure_ts && !attrs.scheduled_departure && !attrs.estimated_departure);

    let stateClass = isRealtime ? "state-ok" : "state-inactive";
    let icon = "mdi:train-variant";
    let statusText = isRealtime ? "À l'heure" : "Théorique";

    if (isInactive) {
      stateClass = "state-inactive";
      icon = "mdi:train-variant";
      statusText = "Hors fenêtre";
    } else if (isCanceled) {
      stateClass = "state-canceled";
      icon = "mdi:train-car-passenger-off";
      statusText = "Supprimé";
    } else if (delayMin > 0) {
      stateClass = "state-delay";
      icon = "mdi:clock-alert-outline";
      statusText = `Retard +${delayMin} min`;
    }

    const safeTrainNum = escapeHtml(trainNum);
    const trainUrl = buildSncfTrainUrl(trainNum, attrs.departure_ts, attrs.downloaded_at);
    const pillHtml = trainUrl
      ? `<a class="status-pill" href="${escapeHtml(trainUrl)}" target="_blank" rel="noopener noreferrer" title="Voir le détail du train n°${safeTrainNum} sur SNCF Voyageurs">${escapeHtml(statusText)} <ha-icon icon="mdi:open-in-new" style="--mdc-icon-size: 12px;"></ha-icon></a>`
      : `<span class="status-pill">${escapeHtml(statusText)}</span>`;

    const alertHtml =
      alerts.length > 0
        ? `<div class="alert-banner">⚠️ <strong>${escapeHtml(alerts[0].title)}</strong></div>`
        : "";

    this.content.innerHTML = `
      <div class="wrapper ${stateClass}">
        <div class="left">
          <div class="icon-circle">
            <ha-icon icon="${icon}"></ha-icon>
          </div>
          <div class="info">
            <span class="title">${escapeHtml(title)}</span>
            <span class="subtitle">Train n°${safeTrainNum}</span>
          </div>
        </div>
        <div class="right">
          <div class="dep-time">${isInactive ? "--:--" : escapeHtml(schedDep)}</div>
          ${pillHtml}
        </div>
      </div>
      ${alertHtml}
    `;
  }

  getCardSize() {
    return 1;
  }

  static getStubConfig(hass) {
    const trainEntity = Object.keys(hass.states).find(
      (e) => e.startsWith("sensor.") && hass.states[e].attributes.train_number !== undefined
    );
    return { entity: trainEntity || "" };
  }
}

if (!customElements.get("sncf-route-card")) {
  customElements.define("sncf-route-card", SncfRouteCard);
}
if (!customElements.get("sncf-specific-train-card")) {
  customElements.define("sncf-specific-train-card", SncfSpecificTrainCard);
}

window.customCards = window.customCards || [];
if (!window.customCards.some((c) => c.type === "sncf-route-card")) {
  window.customCards.push(
    {
      type: "sncf-route-card",
      name: "SNCF Route Card",
      description: "Affiche les prochains trains et les perturbations sur un axe SNCF.",
      preview: true,
    },
    {
      type: "sncf-specific-train-card",
      name: "SNCF Specific Train Card",
      description: "Affiche le statut en direct d'un numéro de train SNCF favori.",
      preview: true,
    }
  );
}
