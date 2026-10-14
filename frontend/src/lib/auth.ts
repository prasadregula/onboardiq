/**
 * Auth for the review queue.
 *  VITE_AUTH_MODE=apikey  (dev)  -> X-API-Key + X-Analyst / X-Role headers
 *  VITE_AUTH_MODE=entra          -> MSAL (PKCE) sign-in against your Entra tenant; bearer token for scope
 *                                   api://<api-client-id>/access_as_user; app roles come back on /me.
 * redirectUri = window.location.origin, SPA platform redirect URI, no trailing slash.
 */
import { PublicClientApplication, type AccountInfo, InteractionRequiredAuthError } from '@azure/msal-browser'

const env = import.meta.env
export const AUTH_MODE: 'apikey' | 'entra' = (env.VITE_AUTH_MODE as any) === 'entra' ? 'entra' : 'apikey'
export const ANALYST = env.VITE_ANALYST ?? 'analyst'
const API_SCOPE = env.VITE_API_SCOPE ?? `api://${env.VITE_API_CLIENT_ID}/access_as_user`

export const msal = AUTH_MODE === 'entra'
  ? new PublicClientApplication({
      auth: { clientId: env.VITE_CLIENT_ID, authority: `https://login.microsoftonline.com/${env.VITE_TENANT_ID}`, redirectUri: window.location.origin },
      cache: { cacheLocation: 'sessionStorage' },
    })
  : null

let ready: Promise<void> | null = null
export function initAuth(): Promise<void> {
  if (!msal) return Promise.resolve()
  if (!ready) ready = msal.initialize().then(async () => { const r = await msal!.handleRedirectPromise(); if (r?.account) msal!.setActiveAccount(r.account) })
  return ready
}

export function account(): AccountInfo | null {
  return msal?.getActiveAccount() ?? msal?.getAllAccounts()[0] ?? null
}

export async function signIn() { await initAuth(); await msal!.loginRedirect({ scopes: [API_SCOPE] }) }
export async function signOut() { await msal!.logoutRedirect({ postLogoutRedirectUri: window.location.origin }) }

export async function authHeaders(): Promise<Record<string, string>> {
  if (AUTH_MODE === 'apikey') {
    return { 'X-API-Key': env.VITE_API_KEY ?? 'dev-key', 'X-Analyst': ANALYST, 'X-Role': env.VITE_ROLE ?? 'KycAnalyst' }
  }
  await initAuth()
  const acct = account()
  if (!acct) throw new Error('not signed in')
  try {
    const t = await msal!.acquireTokenSilent({ scopes: [API_SCOPE], account: acct })
    return { Authorization: `Bearer ${t.accessToken}` }
  } catch (e) {
    if (e instanceof InteractionRequiredAuthError) { await msal!.acquireTokenRedirect({ scopes: [API_SCOPE], account: acct }) }
    throw e
  }
}
