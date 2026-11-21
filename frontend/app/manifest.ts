import type { MetadataRoute } from "next";

// Lets people add ChatReceipt to their home screen on Android and iPhone, where it opens like an app
// straight into a new receipt. The icons are Chit on the dark tile, rendered from app/icon.svg.
export default function manifest(): MetadataRoute.Manifest {
  return {
    name: "ChatReceipt",
    short_name: "ChatReceipt",
    description: "Turn conversations into proper transaction records.",
    start_url: "/create",
    scope: "/",
    display: "standalone",
    background_color: "#0a0c0b",
    theme_color: "#0a0c0b",
    icons: [
      { src: "/icons/icon-192.png", sizes: "192x192", type: "image/png" },
      { src: "/icons/icon-512.png", sizes: "512x512", type: "image/png" },
      { src: "/icons/icon-maskable-512.png", sizes: "512x512", type: "image/png", purpose: "maskable" },
    ],
  };
}
