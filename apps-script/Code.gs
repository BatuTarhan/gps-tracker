const GBFS_BASE = 'https://api.cyclocity.fr/contracts/dublin/gbfs/';
const POLL_MINUTES = 1;
const STATIONS_REFRESH_MS = 24 * 3600 * 1000;


function setup() {
  teardown();
  ScriptApp.newTrigger('pollBikes').timeBased().everyMinutes(POLL_MINUTES).create();
  refreshStations_(true);
  pollBikes();
  Logger.log('Trigger installed: pollBikes every %s minute(s)', POLL_MINUTES);
}

function teardown() {
  ScriptApp.getProjectTriggers()
    .filter(t => t.getHandlerFunction() === 'pollBikes')
    .forEach(t => ScriptApp.deleteTrigger(t));
}

function pollBikes() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(5000)) { Logger.log('Previous run still active – skipping'); return; }
  try {
    const props = PropertiesService.getScriptProperties();
    const fetchedAt = new Date();
    const status = fetchJson_(GBFS_BASE + 'station_status.json');
    const lastUpdated = status.last_updated;

    if (String(lastUpdated) === props.getProperty('LAST_GBFS_UPDATE')) {
      Logger.log('Feed unchanged (last_updated=%s) – nothing written', lastUpdated);
      return;
    }

    refreshStations_(false);
    const snapshot = buildSnapshot_(status, fetchedAt);
    const id = String(lastUpdated);
    commit_([
      writeOf_('bikes_snapshots/' + id, snapshot),
      writeOf_('bikes_latest/current', Object.assign({ snapshotId: id }, snapshot)),
    ]);
    props.setProperty('LAST_GBFS_UPDATE', String(lastUpdated));
    Logger.log('Wrote snapshot %s: %s stations, %s bikes, %s docks free (feed age %ss)',
      id, snapshot.stationCount, snapshot.totals.bikes, snapshot.totals.docks,
      Math.round(fetchedAt.getTime() / 1000 - lastUpdated));
  } finally {
    lock.releaseLock();
  }
}


function buildSnapshot_(status, fetchedAt) {
  const stations = {};
  const totals = { bikes: 0, docks: 0, bikesDisabled: 0, docksDisabled: 0, renting: 0 };
  status.data.stations.forEach(s => {
    const types = {};
    (s.vehicle_types_available || []).forEach(v => { types[v.vehicle_type_id] = v.count; });
    const st = {
      bikes:         num_(s.num_bikes_available, s.num_vehicles_available),
      docks:         num_(s.num_docks_available),
      bikesDisabled: num_(s.num_bikes_disabled, s.num_vehicles_disabled),
      docksDisabled: num_(s.num_docks_disabled),
      renting:       bool_(s.is_renting),
      returning:     bool_(s.is_returning),
      installed:     bool_(s.is_installed),
      lastReported:  toSeconds_(s.last_reported),
      vehicleTypes:  types,
    };
    stations[String(s.station_id)] = st;
    totals.bikes         += st.bikes || 0;
    totals.docks         += st.docks || 0;
    totals.bikesDisabled += st.bikesDisabled || 0;
    totals.docksDisabled += st.docksDisabled || 0;
    totals.renting       += st.renting ? 1 : 0;
  });
  return {
    source:          GBFS_BASE + 'station_status.json',
    gbfsLastUpdated: new Date(toSeconds_(status.last_updated) * 1000),
    gbfsTtl:         num_(status.ttl),
    fetchedAt:       fetchedAt,
    stationCount:    status.data.stations.length,
    totals:          totals,
    stations:        stations,
  };
}

function refreshStations_(force) {
  const props = PropertiesService.getScriptProperties();
  const last = Number(props.getProperty('STATIONS_FETCHED_AT') || 0);
  if (!force && Date.now() - last < STATIONS_REFRESH_MS) return;

  const info = fetchJson_(GBFS_BASE + 'station_information.json');
  const stations = {};
  info.data.stations.forEach(s => {
    stations[String(s.station_id)] = {
      name:     text_(s.name),
      address:  text_(s.address),
      lat:      s.lat,
      lon:      s.lon,
      capacity: num_(s.capacity),
    };
  });
  commit_([writeOf_('bike_stations/info', {
    source:       GBFS_BASE + 'station_information.json',
    fetchedAt:    new Date(),
    stationCount: info.data.stations.length,
    stations:     stations,
  })]);
  props.setProperty('STATIONS_FETCHED_AT', String(Date.now()));
  Logger.log('Refreshed station information (%s stations)', info.data.stations.length);
}

function text_(v) { return Array.isArray(v) ? (v[0] ? v[0].text : null) : (v === undefined ? null : v); }
function num_(a, b) { const v = a !== undefined ? a : b; return v === undefined ? null : v; }
function bool_(v) { return v === undefined ? null : (v === true || v === 1 || v === '1'); }
function toSeconds_(v) {
  if (v === undefined || v === null) return null;
  return typeof v === 'number' ? v : Math.round(new Date(v).getTime() / 1000);
}


function fetchJson_(url, params) {
  const res = UrlFetchApp.fetch(url, Object.assign({ muteHttpExceptions: true }, params || {}));
  const code = res.getResponseCode();
  if (code < 200 || code >= 300) {
    throw new Error('HTTP ' + code + ' from ' + url.split('?')[0] + ': ' + res.getContentText().slice(0, 300));
  }
  return JSON.parse(res.getContentText());
}

function config_() {
  const p = PropertiesService.getScriptProperties();
  const c = {
    apiKey:    p.getProperty('FIREBASE_API_KEY'),
    projectId: p.getProperty('FIREBASE_PROJECT_ID'),
    email:     p.getProperty('POLLER_EMAIL'),
    password:  p.getProperty('POLLER_PASSWORD'),
  };
  Object.keys(c).forEach(k => { if (!c[k]) throw new Error('Missing script property for ' + k); });
  return c;
}

function idToken_(forceRefresh) {
  const cache = CacheService.getScriptCache();
  if (!forceRefresh) {
    const cached = cache.get('ID_TOKEN');
    if (cached) return cached;
  }
  const c = config_();
  const res = fetchJson_(
    'https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=' + c.apiKey, {
      method: 'post',
      contentType: 'application/json',
      payload: JSON.stringify({ email: c.email, password: c.password, returnSecureToken: true }),
    });
  cache.put('ID_TOKEN', res.idToken, 3000);
  return res.idToken;
}


function docName_(path) {
  return 'projects/' + config_().projectId + '/databases/(default)/documents/' + path;
}

function writeOf_(path, obj) {
  return {
    update: { name: docName_(path), fields: toFields_(obj) },
    updateTransforms: [{ fieldPath: 'receivedAt', setToServerValue: 'REQUEST_TIME' }],
  };
}

function commit_(writes) {
  const url = 'https://firestore.googleapis.com/v1/projects/' + config_().projectId +
              '/databases/(default)/documents:commit';
  const send = token => UrlFetchApp.fetch(url, {
    method: 'post',
    contentType: 'application/json',
    headers: { Authorization: 'Bearer ' + token },
    payload: JSON.stringify({ writes: writes }),
    muteHttpExceptions: true,
  });
  let res = send(idToken_(false));
  if (res.getResponseCode() === 401 || res.getResponseCode() === 403) res = send(idToken_(true));
  if (res.getResponseCode() !== 200) {
    throw new Error('Firestore commit failed: HTTP ' + res.getResponseCode() + ' ' +
                    res.getContentText().slice(0, 500));
  }
  return JSON.parse(res.getContentText());
}

function toFields_(obj) {
  const fields = {};
  Object.keys(obj).forEach(k => { if (obj[k] !== undefined) fields[k] = toValue_(obj[k]); });
  return fields;
}

function toValue_(v) {
  if (v === null || v === undefined) return { nullValue: null };
  if (typeof v === 'boolean') return { booleanValue: v };
  if (typeof v === 'number') return Number.isInteger(v) ? { integerValue: String(v) } : { doubleValue: v };
  if (typeof v === 'string') return { stringValue: v };
  if (Object.prototype.toString.call(v) === '[object Date]') return { timestampValue: v.toISOString() };
  if (Array.isArray(v)) return { arrayValue: { values: v.map(toValue_) } };
  return { mapValue: { fields: toFields_(v) } };
}
