// Minimal Firebase web setup for Google Sign-In
// UI attaches ID tokens to API calls automatically.

let _auth = null;
let _user = null;
let _idToken = null;
window.__AUTH_AVAILABLE = false;

async function loadScript(src) {
  await new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = src; s.onload = resolve; s.onerror = reject; document.head.appendChild(s);
  });
}

async function initFirebase() {
  try {
    // Fetch config from server so we don't hardcode project details
    const cfgRes = await fetch('/api/firebase-config');
    if (!cfgRes.ok) throw new Error('No Firebase config');
    const firebaseConfig = await cfgRes.json();
    if (!firebaseConfig.apiKey) {
      console.warn('FIREBASE_WEB_API_KEY not set on server; auth disabled');
      return; // gracefully degrade
    }

    if (!window.firebase) {
      await loadScript('https://www.gstatic.com/firebasejs/10.12.0/firebase-app-compat.js');
      await loadScript('https://www.gstatic.com/firebasejs/10.12.0/firebase-auth-compat.js');
    }
    if (!window.firebase?.apps?.length) {
      window.firebase.initializeApp(firebaseConfig);
    }
    _auth = window.firebase.auth();
    window.__AUTH_AVAILABLE = true;
    // Persist across reloads/redirects
    try {
      await _auth.setPersistence(window.firebase.auth.Auth.Persistence.LOCAL);
    } catch (e) {
      console.warn('Auth persistence setup failed:', e);
    }
    // Complete redirect sign-in if present
    try {
      await _auth.getRedirectResult();
    } catch (e) {
      console.warn('Redirect result error:', e);
    }
    _auth.onAuthStateChanged(async (u) => {
      _user = u;
      _idToken = u ? await u.getIdToken() : null;
      // Clear redirect guard if set
      try { sessionStorage.removeItem('auth_redirecting'); } catch {}
      // Clean URL (remove OAuth params/hash)
      try {
        if (window.location.hash || window.location.search) {
          const url = window.location.origin + window.location.pathname;
          window.history.replaceState({}, document.title, url);
        }
      } catch {}
      document.dispatchEvent(new CustomEvent('auth-changed', { detail: { user: u, idToken: _idToken } }));
    });

    // Expose a helper to emit current auth state on demand (for late listeners)
    window.emitAuthState = async function() {
      if (!_auth) return;
      const u = _auth.currentUser || null;
      const t = u ? await u.getIdToken() : null;
      document.dispatchEvent(new CustomEvent('auth-changed', { detail: { user: u, idToken: t } }));
    }
  } catch (e) {
    console.warn('Firebase init failed:', e);
  }
}

window.getIdToken = async function() {
  if (!_auth) return null;
  try { return _user ? await _user.getIdToken(true) : null; } catch { return null; }
}

window.signInWithGoogle = async function() {
  if (!_auth) return Promise.reject(new Error('Auth not configured'));
  // Prevent repeated redirects
  try {
    if (sessionStorage.getItem('auth_redirecting') === '1') return;
    sessionStorage.setItem('auth_redirecting', '1');
  } catch {}
  const provider = new window.firebase.auth.GoogleAuthProvider();
  try {
    await _auth.signInWithRedirect(provider);
  } catch (e) {
    try { sessionStorage.removeItem('auth_redirecting'); } catch {}
    throw e;
  }
}

window.signOutFirebase = async function() {
  if (_auth) await _auth.signOut();
}

initFirebase();
