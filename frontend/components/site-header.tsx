"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { Logo } from "./brand";
import { ArrowChip, buttonClass } from "./ui/button";

export function SiteHeader() {
  // Once the page scrolls, a hairline separates the header from the content under it.
  const [scrolled, setScrolled] = useState(false);
  useEffect(() => {
    const update = () => setScrolled(window.scrollY > 8);
    update();
    window.addEventListener("scroll", update, { passive: true });
    return () => window.removeEventListener("scroll", update);
  }, []);

  return (
    <header className={`sticky top-0 z-40 border-b bg-paper transition-colors duration-300 ${scrolled ? "border-line" : "border-transparent"}`}>
      <div className="mx-auto flex h-16 max-w-5xl items-center justify-between gap-4 px-4 sm:px-6">
        <Link href="/" aria-label="ChatReceipt home" className="rounded-none">
          <Logo />
        </Link>
        <nav aria-label="Main" className="flex items-center gap-6 text-ui">
          <a href="#how-it-works" className="hidden text-muted transition-colors hover:text-ink sm:inline">
            How it works
          </a>
          <a href="#privacy" className="hidden text-muted transition-colors hover:text-ink sm:inline">
            Privacy
          </a>
          <Link href="/create" className={buttonClass("secondary", "sm")}>
            Create a receipt
            <ArrowChip />
          </Link>
        </nav>
      </div>
    </header>
  );
}
