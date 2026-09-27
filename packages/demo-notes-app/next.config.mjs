/** @type {import('next').NextConfig} */
const nextConfig = {
  // Serve dev resources to 127.0.0.1 too, so the app works when it is listed as a second
  // environment next to localhost (multi-environment runs of the pipeline).
  allowedDevOrigins: ["127.0.0.1"],
};

export default nextConfig;
