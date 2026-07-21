export const AUTH_START_ERROR = 'Sign-in could not start. Check your connection and try again.'
export const AUTH_CALLBACK_ERROR = 'Sign-in could not be completed. Try again; if it keeps failing, contact support.'

export async function beginLogin(getClient) {
  try {
    const authClient = await getClient()
    if (!authClient) return { started: false, error: AUTH_START_ERROR }
    await authClient.loginWithRedirect()
    return { started: true, error: null }
  } catch {
    return { started: false, error: AUTH_START_ERROR }
  }
}

export async function finishLoginRedirect({
  configured,
  search,
  getClient,
  audience,
  storeToken,
  cleanUrl,
}) {
  if (!configured || !/[?&]code=/.test(search) || !/[?&]state=/.test(search)) {
    return { handled: false, signedIn: false, error: null }
  }

  try {
    const authClient = await getClient()
    if (!authClient) throw new Error('auth client unavailable')
    await authClient.handleRedirectCallback()
    const token = await authClient.getTokenSilently({
      authorizationParams: { audience },
    })
    if (!token) throw new Error('access token missing')
    storeToken(token)
    return { handled: true, signedIn: true, error: null }
  } catch {
    return { handled: true, signedIn: false, error: AUTH_CALLBACK_ERROR }
  } finally {
    cleanUrl()
  }
}
