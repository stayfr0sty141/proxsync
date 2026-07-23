/** @type {import('next').NextConfig} */
const rawApiOrigin = process.env.PROXSYNC_API_ORIGIN;
const API_ORIGIN = typeof rawApiOrigin === "string" ? rawApiOrigin : "http://127.0.0.1:8000";

const nextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  output: "standalone",
  async rewrites() {
    // In development the Next dev server proxies the API so the browser talks to a
    // single origin (matching the nginx deployment where /api/* and /* share a host).
    return [
      {
        source: "/api/:path*",
        destination: `${API_ORIGIN}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
