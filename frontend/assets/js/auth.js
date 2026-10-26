/** Same-origin, cookie-based authentication. No credentials are persisted by JavaScript. */
class AuthError extends Error {
  constructor(status = 0) {
    super("Authentication request failed");
    this.status = status;
  }
}

async function request(path, { method = "GET", body, headers = {} } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch(`/api/v1/auth/${path}`, {
      method,
      credentials: "same-origin",
      cache: "no-store",
      redirect: "error",
      signal: controller.signal,
      headers: body ? { ...headers, "Content-Type": "application/json" } : headers,
      ...(body ? { body: JSON.stringify(body) } : {}),
    });
    if (!response.ok) throw new AuthError(response.status);
    if (response.status === 204) return null;
    return await response.json();
  } catch (error) {
    // Never display backend bodies, provider errors, request payloads or tokens.
    throw error instanceof AuthError ? error : new AuthError();
  } finally {
    clearTimeout(timer);
  }
}

function profile(value) {
  if (
    !value || typeof value.id !== "string" || !value.id ||
    typeof value.display_name !== "string" ||
    !["nurse", "doctor"].includes(value.role)
  ) throw new AuthError();
  return { id: value.id, display_name: value.display_name, role: value.role };
}

export async function signIn(email, password) {
  return profile(await request("login", { method: "POST", body: { email, password } }));
}

export async function currentStaff() {
  return profile(await request("me"));
}

/** Reuse the existing cookie session and CSRF endpoint; no token persistence. */
export async function csrfToken() {
  const result = await request("csrf");
  if (!result || typeof result.csrf_token !== "string" || !result.csrf_token ||
      result.csrf_token.length > 256 || /[\r\n\0]/.test(result.csrf_token)) throw new AuthError();
  return result.csrf_token;
}

export async function signOut() {
  let token;
  try {
    const result = await request("csrf");
    if (!result || typeof result.csrf_token !== "string" || !result.csrf_token) {
      throw new AuthError();
    }
    token = result.csrf_token;
  } catch (error) {
    if (error.status !== 401) throw error;
    // A missing session may be cleared without CSRF. If a server row still exists,
    // the backend will reject this attempt; that rejection must remain visible.
  }
  await request("logout", {
    method: "POST",
    headers: token ? { "x-csrf-token": token } : {},
  });
}

export function authMessage(error, operation = "login") {
  if (operation === "logout") {
    return "Sign out could not be confirmed. Your session may still be active. Retry, or sign in again if it expired.";
  }
  if (error.status === 401) return "Incorrect email or password, or your session has expired. Please sign in again.";
  if (error.status === 403) return "This staff account is inactive or has not been provisioned. Contact the project administrator.";
  if (error.status === 429) return "Too many sign-in attempts. Please wait before trying again.";
  if (error.status === 422) return "Check your email address and password and try again.";
  return "The authentication service is unavailable. Check your connection and try again.";
}
