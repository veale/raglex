// Client-side auth: fetch the effective role from /auth/me, gate the app behind a login
// screen when enforcement is on, and expose the role so the UI can hide what a reader must
// not touch (admin/maintain/settings, link-by-highlight). Enforcement is server-side; this
// is only affordance-shaping. See src/raglex/web/auth.py.
import { createContext, useContext, useEffect, useState, ReactNode } from "react";
import { auth, AuthMe } from "./api";

interface AuthState extends AuthMe {
  loading: boolean;
  isAdmin: boolean;
  isReader: boolean;       // reader OR admin (i.e. read access)
  canWrite: boolean;       // admin (full write); readers get only the tiny allow-list
  refresh: () => Promise<void>;
  loginPassword: (pw: string) => Promise<{ ok: boolean; error?: string }>;
  loginPasskey: () => Promise<{ ok: boolean; error?: string }>;
  logout: () => Promise<void>;
}

const Ctx = createContext<AuthState | null>(null);
export const useAuth = () => {
  const v = useContext(Ctx);
  if (!v) throw new Error("useAuth outside provider");
  return v;
};

// ---- WebAuthn base64url <-> ArrayBuffer helpers ----
const b64uToBuf = (s: string): ArrayBuffer => {
  const pad = "=".repeat((4 - (s.length % 4)) % 4);
  const b = atob((s + pad).replace(/-/g, "+").replace(/_/g, "/"));
  const u = new Uint8Array(b.length);
  for (let i = 0; i < b.length; i++) u[i] = b.charCodeAt(i);
  return u.buffer;
};
const bufToB64u = (buf: ArrayBuffer): string => {
  const bytes = new Uint8Array(buf);
  let s = "";
  for (const byte of bytes) s += String.fromCharCode(byte);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
};

export function AuthProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<AuthMe | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = async () => {
    try { setMe(await auth.me()); }
    catch { setMe({ authenticated: false, role: "anon", enforced: true }); }
    finally { setLoading(false); }
  };
  useEffect(() => { refresh(); }, []);
  // A 401 anywhere (session expired) drops us back to the login screen.
  useEffect(() => {
    const onUnauth = () => setMe((m) => (m && m.enforced ? { ...m, authenticated: false, role: "anon" } : m));
    window.addEventListener("raglex-unauthenticated", onUnauth as EventListener);
    return () => window.removeEventListener("raglex-unauthenticated", onUnauth as EventListener);
  }, []);

  const loginPassword = async (pw: string) => {
    try { await auth.login(pw); await refresh(); return { ok: true }; }
    catch (e: any) { return { ok: false, error: "Incorrect password" }; }
  };

  const loginPasskey = async () => {
    try {
      const opts = await auth.passkeyLoginOptions();
      const publicKey: any = {
        ...opts,
        challenge: b64uToBuf(opts.challenge),
        allowCredentials: (opts.allowCredentials || []).map((c: any) => ({ ...c, id: b64uToBuf(c.id) })),
      };
      const cred = (await navigator.credentials.get({ publicKey })) as PublicKeyCredential;
      const r = cred.response as AuthenticatorAssertionResponse;
      await auth.passkeyLoginVerify({
        id: cred.id, rawId: bufToB64u(cred.rawId), type: cred.type,
        response: {
          authenticatorData: bufToB64u(r.authenticatorData),
          clientDataJSON: bufToB64u(r.clientDataJSON),
          signature: bufToB64u(r.signature),
          userHandle: r.userHandle ? bufToB64u(r.userHandle) : null,
        },
      });
      await refresh();
      return { ok: true };
    } catch (e: any) { return { ok: false, error: "Passkey sign-in failed" }; }
  };

  const logout = async () => { try { await auth.logout(); } catch { /* ignore */ } await refresh(); };

  const role = me?.role || "anon";
  const value: AuthState = {
    ...(me || { authenticated: false, role: "anon", enforced: true }),
    loading,
    isAdmin: role === "admin",
    isReader: role === "admin" || role === "reader",
    canWrite: role === "admin",
    refresh, loginPassword, loginPasskey, logout,
  };
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

// Register a passkey (admin only) — bootstrap from an already-authenticated admin session.
export async function registerPasskey(): Promise<{ ok: boolean; error?: string }> {
  try {
    const opts = await auth.passkeyRegisterOptions();
    const publicKey: any = {
      ...opts,
      challenge: b64uToBuf(opts.challenge),
      user: { ...opts.user, id: b64uToBuf(opts.user.id) },
      excludeCredentials: (opts.excludeCredentials || []).map((c: any) => ({ ...c, id: b64uToBuf(c.id) })),
    };
    const cred = (await navigator.credentials.create({ publicKey })) as PublicKeyCredential;
    const r = cred.response as AuthenticatorAttestationResponse;
    await auth.passkeyRegisterVerify({
      id: cred.id, rawId: bufToB64u(cred.rawId), type: cred.type,
      response: {
        attestationObject: bufToB64u(r.attestationObject),
        clientDataJSON: bufToB64u(r.clientDataJSON),
      },
    });
    return { ok: true };
  } catch (e: any) { return { ok: false, error: "Passkey registration failed" }; }
}

export function LoginScreen() {
  const { loginPassword, loginPasskey, passkey_supported } = useAuth();
  const [pw, setPw] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true); setErr(null);
    const r = await loginPassword(pw);
    setBusy(false);
    if (!r.ok) setErr(r.error || "Sign-in failed");
  };
  const passkey = async () => {
    setBusy(true); setErr(null);
    const r = await loginPasskey();
    setBusy(false);
    if (!r.ok) setErr(r.error || "Passkey failed");
  };

  return (
    <div className="login-screen">
      <div className="login-card">
        <h1>RagLex</h1>
        <p className="login-sub">Sign in to continue</p>
        <form onSubmit={submit}>
          <input type="password" autoFocus placeholder="Password" value={pw}
            onChange={(e) => setPw(e.target.value)} autoComplete="current-password" />
          <button type="submit" disabled={busy || !pw}>{busy ? "…" : "Sign in"}</button>
        </form>
        {passkey_supported && (
          <button className="login-passkey" onClick={passkey} disabled={busy}>
            Sign in with a passkey
          </button>
        )}
        {err && <div className="login-err">{err}</div>}
        <p className="login-note">
          A reader password gives read-only access. The admin password (or a passkey) unlocks
          the full interface.
        </p>
      </div>
    </div>
  );
}

// Wraps the app: shows the login screen when enforcement is on and we're not authenticated.
export function AuthGate({ children }: { children: ReactNode }) {
  const { loading, enforced, authenticated } = useAuth();
  if (loading) return <div className="login-screen"><div className="login-card"><h1>RagLex</h1><p className="login-sub">Connecting…</p></div></div>;
  if (enforced && !authenticated) return <LoginScreen />;
  return <>{children}</>;
}
