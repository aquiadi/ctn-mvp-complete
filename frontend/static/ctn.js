/**
 * Shared CTN frontend module — API base resolution, authenticated fetch,
 * session handling, and formatting.
 *
 * Loaded by every page so none of them has to restate the API location,
 * duplicate the auth logic, or hardcode prices the server already publishes.
 */
(function (global) {
  'use strict';

  // ── API base ─────────────────────────────────────────────────────────────
  //
  // Resolved at load time rather than compiled in, so the same files work when
  // served by the API itself, from a local static server, or from a CDN in
  // front of a separate backend. Precedence: explicit override, then meta tag,
  // then same-origin, then the deployed default.

  var DEFAULT_API = 'https://ctn-api-railway-production.up.railway.app';

  function resolveApiBase() {
    // 1. Explicit override set by the host page.
    if (global.CTN_API_BASE) return trimSlash(String(global.CTN_API_BASE));

    // 2. Build-time or deploy-time override via <meta name="ctn-api-base">.
    var meta = document.querySelector('meta[name="ctn-api-base"]');
    if (meta && meta.content) return trimSlash(meta.content);

    // 3. Developer override, useful for pointing a local page at staging.
    try {
      var stored = localStorage.getItem('ctn_api_base');
      if (stored) return trimSlash(stored);
    } catch (e) { /* storage blocked in private mode */ }

    // 4. Served locally means the backend is serving these pages itself,
    //    so the API is the same origin.
    var host = global.location.hostname;
    if (host === 'localhost' || host === '127.0.0.1' || host === '[::1]') {
      return global.location.origin;
    }

    // 5. Deployed frontend talking to the deployed backend.
    return DEFAULT_API;
  }

  function trimSlash(value) {
    return value.replace(/\/+$/, '');
  }

  var API = resolveApiBase();

  // ── Session ──────────────────────────────────────────────────────────────
  //
  // The session cookie is the primary credential. A copy of the token is kept
  // in localStorage as a fallback for browsers that block third-party cookies
  // when the frontend and API are on different origins.

  var TOKEN_KEY = 'ctn_token';

  function getToken() {
    try { return localStorage.getItem(TOKEN_KEY); } catch (e) { return null; }
  }

  function setToken(token) {
    try { token ? localStorage.setItem(TOKEN_KEY, token) : localStorage.removeItem(TOKEN_KEY); }
    catch (e) { /* storage unavailable; the cookie still carries the session */ }
  }

  // ── HTTP ─────────────────────────────────────────────────────────────────

  function ApiError(message, status, detail) {
    this.name = 'ApiError';
    this.message = message;
    this.status = status;
    this.detail = detail;
  }
  ApiError.prototype = Object.create(Error.prototype);

  /** Turn a FastAPI error body into a single readable sentence. */
  function describeError(body, status) {
    var detail = body && body.detail;
    if (!detail) return 'Request failed (HTTP ' + status + ')';
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail)) {
      return detail.map(function (d) { return d.msg || String(d); }).join('. ');
    }
    if (detail.message) {
      return detail.errors ? detail.message + ' ' + detail.errors.join(' ') : detail.message;
    }
    return JSON.stringify(detail);
  }

  function request(path, options) {
    options = options || {};
    var headers = Object.assign({}, options.headers);

    var token = getToken();
    if (token) headers['Authorization'] = 'Bearer ' + token;

    // FormData must set its own Content-Type so the browser can attach the
    // multipart boundary; declaring JSON here would corrupt file uploads.
    var isFormData = typeof FormData !== 'undefined' && options.body instanceof FormData;
    if (options.body && !isFormData && !headers['Content-Type']) {
      headers['Content-Type'] = 'application/json';
    }

    return fetch(API + path, Object.assign({}, options, {
      headers: headers,
      credentials: 'include',
    })).then(function (response) {
      if (response.status === 204) return null;

      return response.json().catch(function () { return null; }).then(function (body) {
        if (!response.ok) {
          throw new ApiError(describeError(body, response.status), response.status, body);
        }
        return body;
      });
    });
  }

  function get(path) { return request(path, { method: 'GET' }); }

  function post(path, body) {
    return request(path, {
      method: 'POST',
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  }

  function postForm(path, formData) {
    return request(path, { method: 'POST', body: formData });
  }

  // ── Auth helpers ─────────────────────────────────────────────────────────

  function currentUser() {
    return get('/api/auth/me').then(function (data) { return data.user; })
                              .catch(function () { return null; });
  }

  /**
   * Resolve the signed-in user, redirecting to login when the session is
   * missing or the role does not match. Resolves to null after redirecting so
   * callers can simply `return`.
   */
  function requireRole(role, returnTo) {
    return currentUser().then(function (user) {
      if (!user) {
        global.location.href = '/login?return=' + encodeURIComponent(returnTo || global.location.pathname);
        return null;
      }
      if (role && user.role !== role) {
        global.location.href = homeFor(user.role);
        return null;
      }
      return user;
    });
  }

  function homeFor(role) {
    if (role === 'installer') return '/app';
    if (role === 'buyer') return '/marketplace';
    if (role === 'admin') return '/admin';
    return '/';
  }

  function logout(redirectTo) {
    return post('/api/auth/logout').catch(function () { /* clear locally regardless */ })
      .then(function () {
        setToken(null);
        global.location.href = redirectTo || '/?logged_out=1';
      });
  }

  // ── Server-published settings ────────────────────────────────────────────
  //
  // Prices, thresholds, and contract details come from /config so the UI never
  // restates a value the backend owns.

  var configPromise = null;

  function settings() {
    if (!configPromise) {
      configPromise = get('/config').catch(function (error) {
        configPromise = null;
        throw error;
      });
    }
    return configPromise;
  }

  // ── Formatting ───────────────────────────────────────────────────────────

  function number(value, decimals) {
    var n = Number(value);
    if (!isFinite(n)) return '—';
    return n.toLocaleString('en-IN', {
      minimumFractionDigits: decimals || 0,
      maximumFractionDigits: decimals === undefined ? 0 : decimals,
    });
  }

  function inr(value) { return '₹' + number(value); }
  function usd(value) { return '$' + number(value, 2); }

  function timestamp(epochSeconds) {
    if (!epochSeconds) return '—';
    return new Date(epochSeconds * 1000)
      .toLocaleString('en-IN', { dateStyle: 'short', timeStyle: 'short' });
  }

  function date(value) { return value ? String(value).slice(0, 10) : '—'; }

  /** Escape interpolated values so API data cannot inject markup. */
  function escape(value) {
    if (value === null || value === undefined) return '';
    return String(value).replace(/[&<>"']/g, function (char) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char];
    });
  }

  global.CTN = {
    API: API,
    ApiError: ApiError,
    get: get,
    post: post,
    postForm: postForm,
    request: request,
    getToken: getToken,
    setToken: setToken,
    currentUser: currentUser,
    requireRole: requireRole,
    homeFor: homeFor,
    logout: logout,
    settings: settings,
    fmt: {
      number: number,
      inr: inr,
      usd: usd,
      timestamp: timestamp,
      date: date,
      escape: escape,
    },
  };
})(window);
