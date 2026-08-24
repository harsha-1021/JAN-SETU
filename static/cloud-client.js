const FIREBASE_VERSION = '12.17.1';

let statePromise;
let googleMapsAuthFailed = false;

async function initializeCloudClient() {
  const configResponse = await fetch('/config/public');
  if (!configResponse.ok) throw new Error('Could not load cloud configuration');
  const publicConfig = await configResponse.json();
  const state = {
    publicConfig,
    firebaseEnabled: Boolean(publicConfig.firebase?.enabled),
    auth: null,
    database: null,
    authModule: null,
    databaseModule: null,
  };

  if (!state.firebaseEnabled) return state;

  const base = `https://www.gstatic.com/firebasejs/${FIREBASE_VERSION}`;
  const [appModule, authModule, databaseModule] = await Promise.all([
    import(`${base}/firebase-app.js`),
    import(`${base}/firebase-auth.js`),
    import(`${base}/firebase-database.js`),
  ]);
  const app = appModule.initializeApp(publicConfig.firebase.config);
  state.authModule = authModule;
  state.databaseModule = databaseModule;
  state.auth = authModule.getAuth(app);
  state.database = databaseModule.getDatabase(app);
  await new Promise(resolve => {
    const unsubscribe = authModule.onAuthStateChanged(state.auth, () => {
      unsubscribe();
      resolve();
    });
  });
  return state;
}

export function cloudState() {
  if (!statePromise) statePromise = initializeCloudClient();
  return statePromise;
}

export async function apiFetch(url, options = {}) {
  const state = await cloudState();
  const headers = new Headers(options.headers || {});
  if (state.firebaseEnabled && state.auth.currentUser) {
    headers.set('Authorization', `Bearer ${await state.auth.currentUser.getIdToken()}`);
  }
  return fetch(url, { ...options, headers });
}

export async function signInPolicymaker(identifier, password) {
  const state = await cloudState();
  if (state.firebaseEnabled) {
    await state.authModule.signInWithEmailAndPassword(state.auth, identifier, password);
    const response = await apiFetch('/auth/me');
    if (!response.ok) {
      await state.authModule.signOut(state.auth);
      throw new Error('This Firebase account is not authorized as a policymaker.');
    }
    return;
  }

  const body = new URLSearchParams({ username: identifier, password });
  const response = await fetch('/auth/login', { method: 'POST', body });
  if (!response.ok) throw new Error('Incorrect username or password.');
}

export async function requirePolicymaker() {
  const state = await cloudState();
  if (state.firebaseEnabled && !state.auth.currentUser) return false;
  const response = await apiFetch('/auth/me');
  return response.ok;
}

export async function signOutPolicymaker() {
  const state = await cloudState();
  if (state.firebaseEnabled && state.auth.currentUser) {
    await state.authModule.signOut(state.auth);
  }
  await fetch('/auth/logout', { method: 'POST' });
}

export async function listenForDashboardUpdates(callback) {
  const state = await cloudState();
  if (!state.firebaseEnabled || !state.auth.currentUser) return null;
  const updateRef = state.databaseModule.ref(state.database, 'dashboard/last_update');
  let firstValue = true;
  return state.databaseModule.onValue(updateRef, () => {
    if (firstValue) {
      firstValue = false;
      return;
    }
    callback();
  });
}

export async function loadGoogleMaps() {
  const state = await cloudState();
  const apiKey = state.publicConfig.google_maps_api_key;
  if (!apiKey) return null;
  if (window.google?.maps) return window.google.maps;
  await new Promise((resolve, reject) => {
    const previousAuthFailure = window.gm_authFailure;
    window.gm_authFailure = () => {
      googleMapsAuthFailed = true;
      window.dispatchEvent(new Event('google-maps-auth-failure'));
      if (typeof previousAuthFailure === 'function') previousAuthFailure();
    };
    const callbackName = `googleMapsReady${Date.now()}`;
    window[callbackName] = () => {
      delete window[callbackName];
      resolve();
    };
    const script = document.createElement('script');
    script.src = `https://maps.googleapis.com/maps/api/js?key=${encodeURIComponent(apiKey)}&loading=async&callback=${callbackName}`;
    script.async = true;
    script.onerror = () => reject(new Error('Google Maps failed to load'));
    document.head.appendChild(script);
  });
  if (googleMapsAuthFailed) throw new Error('Google Maps authorization failed');
  return window.google.maps;
}
