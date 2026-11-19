import type { Metadata, Viewport } from "next";
import "./globals.css";

// Every face on the site contains the naira sign (₦), checked in the font files. All three are self-hosted
// from public/fonts (see globals.css): Space Grotesk for text, Space Mono for figures, Tiny5 for labels.

export const metadata: Metadata = {
  title: {
    default: "ChatReceipt: turn conversations into transaction records",
    template: "%s · ChatReceipt",
  },
  description:
    "Upload screenshots of a conversation, review the transaction details ChatReceipt finds, and download a clean record you can share.",
};

export const viewport: Viewport = {
  themeColor: "#0a0c0b",
  colorScheme: "dark",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className="antialiased">
      <head>
        <link rel="preload" href="/fonts/space-grotesk-latin.woff2" as="font" type="font/woff2" crossOrigin="anonymous" />
        <link rel="preload" href="/fonts/tiny5-latin.woff2" as="font" type="font/woff2" crossOrigin="anonymous" />
        <link rel="preload" href="/fonts/space-mono-latin-400.woff2" as="font" type="font/woff2" crossOrigin="anonymous" />
      </head>
      <body className="min-h-dvh bg-paper font-sans text-ink">{children}</body>
    </html>
  );
}
