import { NextResponse } from 'next/server';
import type { NextRequest } from 'next/server';
import { getToken } from 'next-auth/jwt';

// This app has no login of its own — it trusts the doctor portal's NextAuth
// session instead. doctorshero-frontend's NextAuth cookie is scoped to
// `.doctorshero.com` (see its own [...nextauth]/route.js), so the browser
// sends the same session cookie here too; getToken() decodes it with the
// same NEXTAUTH_SECRET. No valid session → bounce to the portal's login
// page with a callbackUrl back to whatever page was requested here (the
// login page already does `window.location.href = callbackUrl` on success).
const LOGIN_URL = 'https://doctorshero.com/login';
// This app only ever lives at this one fixed public origin — building the
// callback URL from `request.url` instead would leak the internal proxy
// target (Apache reverse-proxies https://knowledgehub.doctorshero.com to
// http://127.0.0.1:3002 without forwarding a trustworthy original-host
// header here), producing an unusable http://localhost:3002/... callback.
const PUBLIC_ORIGIN = 'https://knowledgehub.doctorshero.com';

// A static `script-src 'self'` CSP blocks Next.js's own inline hydration
// script (`self.__next_f.push(...)`), which silently kills all client
// interactivity — the page renders but nothing responds to clicks/input.
// Next's documented fix is a per-request nonce: generate one here, pass it
// to the app via the `x-nonce` request header (Next reads this and stamps
// its own injected scripts with it), and require that same nonce in the
// response CSP instead of falling back to 'unsafe-inline'.
export async function proxy(request: NextRequest) {
  const token = await getToken({
    req: request,
    secret: process.env.NEXTAUTH_SECRET,
    secureCookie: true,
  });

  if (!token) {
    const callbackUrl = encodeURIComponent(`${PUBLIC_ORIGIN}${request.nextUrl.pathname}${request.nextUrl.search}`);
    return NextResponse.redirect(`${LOGIN_URL}?callbackUrl=${callbackUrl}`);
  }

  const nonce = Buffer.from(crypto.randomUUID()).toString('base64');
  const csp = `
    default-src 'self';
    script-src 'self' 'nonce-${nonce}' 'strict-dynamic';
    style-src 'self' 'unsafe-inline';
    img-src 'self' data:;
    font-src 'self';
    connect-src 'self';
    object-src 'none';
    base-uri 'self';
    form-action 'self';
    frame-ancestors 'self' https://doctorshero.com;
  `
    .replace(/\s{2,}/g, ' ')
    .trim();

  const requestHeaders = new Headers(request.headers);
  requestHeaders.set('x-nonce', nonce);

  const response = NextResponse.next({ request: { headers: requestHeaders } });
  response.headers.set('Content-Security-Policy', csp);
  return response;
}

export const config = {
  matcher: [
    // Skip static assets/images — only pages/routes need the auth check + CSP+nonce.
    '/((?!_next/static|_next/image|favicon.ico).*)',
  ],
};
