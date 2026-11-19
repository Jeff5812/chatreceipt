import Link from "next/link";
import { whatsappLink } from "@/lib/whatsapp";
import { Logo } from "./brand";

// On phones each link gets a 40px-tall tap area; from sm up the row spacing does that job.
const LINK = "inline-flex min-h-10 items-center text-ui text-muted underline-offset-4 transition-colors hover:text-ink hover:underline sm:min-h-0";

export function SiteFooter() {
  return (
    <footer className="border-t border-line">
      <div className="mx-auto max-w-5xl px-4 sm:px-6">
        <div className="flex flex-col gap-8 py-12 sm:flex-row sm:items-start sm:justify-between">
          <div className="max-w-sm">
            <Logo />
            <p className="mt-4 text-ui leading-relaxed text-muted">
              Transaction records from chat screenshots: what was agreed, what was paid and what is still owed.
            </p>
          </div>
          <nav aria-label="Footer" className="flex flex-col sm:flex-row sm:gap-6">
            <Link href="/create" className={LINK}>
              Create a receipt
            </Link>
            <a href="#how-it-works" className={LINK}>
              How it works
            </a>
            <a href="#privacy" className={LINK}>
              Privacy
            </a>
            {whatsappLink() && (
              <Link href="/records" className={LINK}>
                My records
              </Link>
            )}
          </nav>
        </div>
        <div className="flex flex-col gap-2 border-t border-line py-6 text-meta text-muted sm:flex-row sm:items-start sm:justify-between sm:gap-10">
          <p className="max-w-xl">
            A ChatReceipt record shows what a conversation says was agreed. It isn&apos;t a contract, and payments on it
            aren&apos;t verified.
          </p>
          <p className="shrink-0 font-mono">© 2026 ChatReceipt</p>
        </div>
      </div>
    </footer>
  );
}
