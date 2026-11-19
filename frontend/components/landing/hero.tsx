import Link from "next/link";
import { HeroChit } from "../mascot";
import { ArrowChip } from "../ui/button";
import { WhatsAppButton } from "../whatsapp-button";
import { HeroFloats } from "./floats";

const META = ["No account needed", "Screenshots aren't stored", "PDF or PNG"];

export function Hero() {
  return (
    <section className="lit relative overflow-x-clip">
      <div aria-hidden className="floor" />
      <HeroFloats />
      <div className="relative mx-auto flex max-w-5xl flex-col items-center px-4 pt-12 pb-16 text-center sm:px-6 sm:pt-16 sm:pb-20">
        <div className="anim-rise">
          <HeroChit />
        </div>
        <p className="pixel-label anim-rise mt-7 text-accent" style={{ animationDelay: "0.08s" }}>
          For deals agreed in chat
        </p>
        <h1
          className="anim-rise mt-5 max-w-4xl font-display text-[40px] leading-[1.03] font-bold tracking-[-0.035em] text-balance sm:text-[58px] lg:text-[66px]"
          style={{ animationDelay: "0.14s" }}
        >
          Turn your conversations into <span className="text-accent">proper transaction records.</span>
        </h1>
        <p className="anim-rise mt-6 max-w-2xl text-[17px] leading-relaxed text-muted sm:text-lg" style={{ animationDelay: "0.2s" }}>
          Upload screenshots of a conversation, let ChatReceipt organize the transaction details, review everything and
          create a clean record you can share.
        </p>

        {/* The main action, set like a command line: one big link. */}
        <Link
          href="/create"
          className="press anim-rise mt-9 flex w-full max-w-[440px] items-center gap-3 border border-line-strong bg-surface p-1.5 pb-2.5 pl-4 text-left shadow-e2 transition-colors duration-200 hover:border-mist/45"
          style={{ animationDelay: "0.26s" }}
        >
          <span className="min-w-0 flex-1 truncate font-mono text-meta text-muted">
            <span className="text-accent">›</span> <span className="sm:hidden">your screenshots</span>
            <span className="hidden sm:inline">read my chat screenshots</span>
          </span>
          <span className="btn-px key-lime mb-0 inline-flex h-11 shrink-0 items-center gap-2 px-4 text-ui font-semibold">
            Create a receipt
            <ArrowChip />
          </span>
        </Link>
        <div className="anim-rise mt-4 empty:hidden" style={{ animationDelay: "0.3s" }}>
          <WhatsAppButton />
        </div>
        <a
          href="#how-it-works"
          className="mt-4 inline-flex min-h-10 items-center text-ui text-muted underline-offset-4 transition-colors hover:text-ink hover:underline"
        >
          See how it works
        </a>

        <ul className="mt-6 flex flex-wrap justify-center gap-x-5 gap-y-1.5 font-mono text-caption text-muted">
          {META.map((item) => (
            <li key={item} className="flex items-center gap-2">
              <span aria-hidden className="block size-1.5 bg-accent" />
              {item}
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}
