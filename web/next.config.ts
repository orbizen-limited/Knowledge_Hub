import type { NextConfig } from "next";

// Content-Security-Policy is set per-request in proxy.ts instead (it needs a
// fresh nonce every request so Next's own hydration scripts can be allowed
// without falling back to 'unsafe-inline') — that's also where frame-ancestors
// lives, which is what actually governs framing in modern browsers.
// X-Frame-Options is dropped entirely: it only supports DENY/SAMEORIGIN (no
// origin allowlist), and this app IS meant to be framed by doctorshero.com's
// dashboard now — CSP's frame-ancestors is the correct, more expressive tool.
const securityHeaders = [
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
];

const nextConfig: NextConfig = {
  async headers() {
    return [
      {
        source: "/:path*",
        headers: securityHeaders,
      },
    ];
  },
};

export default nextConfig;
