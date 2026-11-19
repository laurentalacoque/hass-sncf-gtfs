/* ==========================================================================
   0. Functions to build official info URL for a given train
   ========================================================================== */
const SNCF_TRAIN_DETAIL_URL =
  "https://www.sncf-voyageurs.com/fr/voyagez-avec-nous/horaires-et-itineraires/recherche-de-train/detail-train/";

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
  setConfig(config) {
    if (!config.entity) {
      throw new Error("Vous devez définir une entité (entity)");
    }
    this.config = {
      max_trains: 4,
      ...config,
    };
  }

  set hass(hass) {
    this._hass = hass;
    const entityId = this.config.entity;
    const stateObj = hass.states[entityId];

    if (!this.content) {
      this.innerHTML = `
        <ha-card>
          <div class="card-content" id="container"></div>
        </ha-card>
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
          .delay-inline {
            color: #ff9800;
            font-size: 0.85em;
            font-weight: 700;
            display: none;
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
          .empty {
            text-align: center;
            opacity: 0.6;
            padding: 12px 0;
            font-style: italic;
          }
        </style>
      `;
      this.content = this.querySelector("#container");
      this.querySelector("ha-card").addEventListener("click", (e) => {
        if (e.target.closest("a.badge")) return;
        const event = new Event("hass-more-info", { bubbles: true, composed: true });
        event.detail = { entityId };
        this.dispatchEvent(event);
      });
    }

    if (!stateObj) {
      this.content.innerHTML = `<div class="empty">Entité introuvable : ${entityId}</div>`;
      return;
    }

    const title = this.config.title || stateObj.attributes.friendly_name || entityId;
    const trains = (stateObj.attributes.trains || []).slice(0, this.config.max_trains);
    const alerts = stateObj.attributes.alerts || [];
    const downloadedAt = stateObj.attributes.downloaded_at;

    let html = `
      <div class="header">
        <div class="header-title">
          <ha-icon icon="mdi:train"></ha-icon>
          <span>${title}</span>
        </div>
        <span style="font-size:0.8em; opacity:0.6;">${stateObj.attributes.train_count || 0} train(s)</span>
      </div>
    `;

    if (alerts.length > 0) {
      const extraCount = alerts.length > 1 ? ` (+${alerts.length - 1})` : "";
      const desc =
        alerts[0].description && alerts[0].description !== alerts[0].title
          ? `<div class="alert-desc">${alerts[0].description}</div>`
          : "";
      html += `
        <div class="alert-box">
          ⚠️ <strong>${alerts[0].title}${extraCount}</strong>
          ${desc}
        </div>
      `;
    }

    if (trains.length === 0) {
      html += `<div class="empty">Aucun train à venir dans la fenêtre</div>`;
    } else {
      trains.forEach((t, index) => {
        let badgeClass = "status-ok";
        let statusLabel = "À l'heure";
        if (t.is_canceled) {
          badgeClass = "status-canceled";
          statusLabel = "Supprimé";
        } else if (t.delay_minutes > 0) {
          badgeClass = "status-delay";
          statusLabel = `+${t.delay_minutes} min`;
        }

        const depDisplay = t.scheduled_departure || t.estimated_departure || "--:--";
        const arrDisplay = t.scheduled_arrival || t.estimated_arrival || "--:--";
        const delayInline =
          !t.is_canceled && t.delay_minutes > 0
            ? `<span class="delay-inline">+${t.delay_minutes} min</span>`
            : "";

        const trainUrl = buildSncfTrainUrl(t.train_number, t.departure_ts, downloadedAt);
        const badgeHtml = trainUrl
          ? `<a class="badge ${badgeClass}" href="${trainUrl}" target="_blank" rel="noopener noreferrer" title="Voir le détail du train n°${t.train_number} sur SNCF Voyageurs">${statusLabel} <ha-icon icon="mdi:open-in-new" style="--mdc-icon-size: 13px;"></ha-icon></a>`
          : `<span class="badge ${badgeClass}">${statusLabel}</span>`;

        html += `
          <div class="train-row ${index === 0 ? "next" : ""}">
            <div class="times">
              <span>${depDisplay}</span>
              ${delayInline}
              <span class="arrow">→</span>
              <span>${arrDisplay}</span>
              <span class="train-num">n°${t.train_number}</span>
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
  setConfig(config) {
    if (!config.entity) {
      throw new Error("Vous devez définir une entité (entity)");
    }
    this.config = config;
  }

  set hass(hass) {
    this._hass = hass;
    const entityId = this.config.entity;
    const stateObj = hass.states[entityId];

    if (!this.content) {
      this.innerHTML = `
        <ha-card>
          <div class="card-content" id="specific-container"></div>
        </ha-card>
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
          .delay-inline {
            color: #ff9800;
            font-size: 0.7em;
            font-weight: 700;
            margin-left: 4px;
            display: none;
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
      `;
      this.content = this.querySelector("#specific-container");
      this.querySelector("ha-card").addEventListener("click", (e) => {
        if (e.target.closest("a.status-pill")) return;
        const event = new Event("hass-more-info", { bubbles: true, composed: true });
        event.detail = { entityId };
        this.dispatchEvent(event);
      });
    }

    if (!stateObj) {
      this.content.innerHTML = `<div>Entité introuvable : ${entityId}</div>`;
      return;
    }

    const attrs = stateObj.attributes;
    const title = this.config.title || attrs.friendly_name || `Train ${attrs.train_number || ""}`;
    const trainNum = attrs.train_number || "";
    const depTime = attrs.estimated_departure || "--:--";
    const schedDep = attrs.scheduled_departure || depTime;
    const isCanceled = attrs.is_canceled === true;
    const delayMin = attrs.delay_minutes || 0;
    const alerts = attrs.alerts || [];
    const isInactive = stateObj.state === "Inactive / Outside window";

    let stateClass = "state-ok";
    let icon = "mdi:train-variant";
    let statusText = "À l'heure";

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

    const delayInline =
      !isInactive && !isCanceled && delayMin > 0
        ? `<span class="delay-inline">+${delayMin} min</span>`
        : "";

    const trainUrl = buildSncfTrainUrl(trainNum, attrs.departure_ts, attrs.downloaded_at);
    const pillHtml = trainUrl
      ? `<a class="status-pill" href="${trainUrl}" target="_blank" rel="noopener noreferrer" title="Voir le détail du train n°${trainNum} sur SNCF Voyageurs">${statusText} <ha-icon icon="mdi:open-in-new" style="--mdc-icon-size: 12px;"></ha-icon></a>`
      : `<span class="status-pill">${statusText}</span>`;

    const alertHtml =
      alerts.length > 0
        ? `<div class="alert-banner">⚠️ <strong>${alerts[0].title}</strong></div>`
        : "";

    this.content.innerHTML = `
      <div class="wrapper ${stateClass}">
        <div class="left">
          <div class="icon-circle">
            <ha-icon icon="${icon}"></ha-icon>
          </div>
          <div class="info">
            <span class="title">${title}</span>
            <span class="subtitle">Train n°${trainNum}</span>
          </div>
        </div>
        <div class="right">
          <div class="dep-time">${isInactive ? "--:--" : schedDep}${delayInline}</div>
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

customElements.define("sncf-route-card", SncfRouteCard);
customElements.define("sncf-specific-train-card", SncfSpecificTrainCard);

// Déclaration officielle pour le sélecteur visuel "Ajouter une carte" de Lovelace
window.customCards = window.customCards || [];
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