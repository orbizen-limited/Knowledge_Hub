import { NextResponse } from 'next/server';
import type { NextRequest } from 'next/server';

// A static `script-src 'self'` CSP blocks Next.js's own inline hydration
// script (`self.__next_f.push(...)`), which silently kills all client
// interactivity — the page renders but nothing responds to clicks/input.
// Next's documented fix is a per-request nonce: generate one here, pass it
// to the app via the `x-nonce` request header (Next reads this and stamps
// its own injected scripts with it), and require that same nonce in the
// response CSP instead of falling back to 'unsafe-inline'.
export function proxy(request: NextRequest) {
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
    frame-ancestors 'none';
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
    // Skip static assets/images — only pages/routes need the CSP+nonce.
    '/((?!_next/static|_next/image|favicon.ico).*)',
  ],
};
