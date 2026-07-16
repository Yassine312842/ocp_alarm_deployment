/**
 * i18n.js — French/English translation layer for the OCP Alarm Intelligence
 * dashboard. No build step, no dependency — designed to paste directly into
 * frontend/index.html above the root component, or import as a <script>
 * before it if you split files.
 *
 * INTEGRATION (index.html is a single-file React app per the repo):
 *
 * 1. Paste the TRANSLATIONS object and useLanguage() hook into index.html,
 *    above your root App component.
 * 2. In App(): const { lang, setLang, t } = useLanguage();
 * 3. Replace hardcoded UI strings with t('key'), e.g.:
 *      <h1>Alarm Intelligence</h1>       ->  <h1>{t('appTitle')}</h1>
 *      <button>Confirm root cause</button> -> <button>{t('confirmRootCause')}</button>
 * 4. Add the toggle somewhere in the header, e.g. next to the SAMPLE DATA badge:
 *      <LanguageToggle lang={lang} setLang={setLang} />
 *
 * Only UI chrome is translated — alarm tag names, descriptions, and operator
 * IDs come from the data itself and stay as-is regardless of language.
 *
 * I built this dictionary against the feature set described in the repo
 * README (KPI dashboard, RCA view, incident confirmation). Send over the
 * actual index.html and I'll match keys to your exact strings instead of
 * you having to rename things to fit.
 */

const TRANSLATIONS = {
  en: {
    appTitle: "Alarm Intelligence",
    sampleDataBadge: "SAMPLE DATA",

    // Nav / views
    navDashboard: "Dashboard",
    navKpis: "KPIs",
    navIncidents: "Incidents",
    navRootCause: "Root Cause Analysis",
    navSettings: "Settings",

    // KPI tiles (EEMUA-191)
    kpiAlarmRate: "Alarm rate / operator / 10 min",
    kpiStaleAlarms: "Stale / standing alarms",
    kpiPriorityDist: "Priority distribution",
    kpiWithinTarget: "% windows within target",
    kpiPeakRate: "Peak rate (10 min)",
    kpiHoursStanding: "hrs standing",

    // Priorities
    priorityCritical: "Critical",
    priorityHigh: "High",
    priorityMedium: "Medium",
    priorityLow: "Low",

    // Incident / RCA view
    incidentStarted: "Started",
    incidentStatus: "Status",
    incidentStatusOpen: "Open",
    incidentStatusConfirmed: "Root cause confirmed",
    incidentStatusDismissed: "Dismissed",
    alarmCount: "alarms",
    rootCauseCandidates: "Root cause candidates",
    candidateRank: "Rank",
    candidateConfidence: "Confidence",
    candidateExplanation: "Why the engine flagged this",
    confirmRootCause: "Confirm root cause",
    confirmRootCauseModalTitle: "Confirm root cause for this incident",
    confirmedTagLabel: "Confirmed tag",
    operatorNoteLabel: "Note (optional)",
    submitConfirmation: "Submit",
    cancel: "Cancel",
    confirmationSaved: "Root cause confirmed and saved.",
    engineAccuracy: "Engine accuracy",
    engineTopPickCorrect: "Engine's top pick was correct",

    // Filters / export (Tier-4 frontend items)
    filterTimeRange: "Time range",
    filterLast1h: "Last hour",
    filterLast24h: "Last 24 hours",
    filterLast7d: "Last 7 days",
    filterCustom: "Custom range",
    exportCsv: "Export CSV",
    exportPdf: "Export PDF",

    // Generic
    loading: "Loading…",
    noData: "No data for this period.",
    error: "Something went wrong.",
    retry: "Retry",
  },

  fr: {
    appTitle: "Intelligence des alarmes",
    sampleDataBadge: "DONNÉES D'EXEMPLE",

    navDashboard: "Tableau de bord",
    navKpis: "Indicateurs",
    navIncidents: "Incidents",
    navRootCause: "Analyse de cause racine",
    navSettings: "Paramètres",

    kpiAlarmRate: "Taux d'alarmes / opérateur / 10 min",
    kpiStaleAlarms: "Alarmes persistantes",
    kpiPriorityDist: "Répartition par priorité",
    kpiWithinTarget: "% de fenêtres dans la cible",
    kpiPeakRate: "Taux de pointe (10 min)",
    kpiHoursStanding: "h en cours",

    priorityCritical: "Critique",
    priorityHigh: "Élevée",
    priorityMedium: "Moyenne",
    priorityLow: "Faible",

    incidentStarted: "Débuté",
    incidentStatus: "Statut",
    incidentStatusOpen: "Ouvert",
    incidentStatusConfirmed: "Cause racine confirmée",
    incidentStatusDismissed: "Rejeté",
    alarmCount: "alarmes",
    rootCauseCandidates: "Causes racines candidates",
    candidateRank: "Rang",
    candidateConfidence: "Confiance",
    candidateExplanation: "Pourquoi le moteur a signalé ceci",
    confirmRootCause: "Confirmer la cause racine",
    confirmRootCauseModalTitle: "Confirmer la cause racine de cet incident",
    confirmedTagLabel: "Tag confirmé",
    operatorNoteLabel: "Note (facultatif)",
    submitConfirmation: "Valider",
    cancel: "Annuler",
    confirmationSaved: "Cause racine confirmée et enregistrée.",
    engineAccuracy: "Précision du moteur",
    engineTopPickCorrect: "Le premier choix du moteur était correct",

    filterTimeRange: "Plage horaire",
    filterLast1h: "Dernière heure",
    filterLast24h: "Dernières 24 heures",
    filterLast7d: "7 derniers jours",
    filterCustom: "Plage personnalisée",
    exportCsv: "Exporter en CSV",
    exportPdf: "Exporter en PDF",

    loading: "Chargement…",
    noData: "Aucune donnée pour cette période.",
    error: "Une erreur est survenue.",
    retry: "Réessayer",
  },
};

/**
 * React hook. No localStorage by default (kiosk/HMI displays often run in
 * a locked-down browser profile) — falls back to browser language on first
 * load, then stays in memory for the session. Uncomment the localStorage
 * lines if you want the choice to persist across reloads on a given machine.
 */
function useLanguage() {
  const [lang, setLang] = React.useState(() => {
    // const saved = localStorage.getItem('ocp-lang');
    // if (saved === 'en' || saved === 'fr') return saved;
    return navigator.language?.startsWith('fr') ? 'fr' : 'en';
  });

  // React.useEffect(() => { localStorage.setItem('ocp-lang', lang); }, [lang]);

  const t = React.useCallback(
    (key) => TRANSLATIONS[lang]?.[key] ?? TRANSLATIONS.en[key] ?? key,
    [lang]
  );

  return { lang, setLang, t };
}

/** Small EN/FR switch — drop next to the SAMPLE DATA badge in the header. */
function LanguageToggle({ lang, setLang }) {
  return (
    <div style={{ display: 'flex', gap: '4px', fontSize: '12px' }}>
      <button
        onClick={() => setLang('en')}
        style={{
          padding: '2px 8px',
          borderRadius: '4px',
          border: '1px solid #ccc',
          background: lang === 'en' ? '#333' : 'transparent',
          color: lang === 'en' ? '#fff' : '#333',
          cursor: 'pointer',
        }}
        aria-pressed={lang === 'en'}
      >
        EN
      </button>
      <button
        onClick={() => setLang('fr')}
        style={{
          padding: '2px 8px',
          borderRadius: '4px',
          border: '1px solid #ccc',
          background: lang === 'fr' ? '#333' : 'transparent',
          color: lang === 'fr' ? '#fff' : '#333',
          cursor: 'pointer',
        }}
        aria-pressed={lang === 'fr'}
      >
        FR
      </button>
    </div>
  );
}
