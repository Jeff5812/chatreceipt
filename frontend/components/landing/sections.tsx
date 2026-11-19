import Link from "next/link";
import type { CSSProperties, ReactNode } from "react";
import { ChitStatic } from "../chit";
import { Mascot } from "../mascot";
import { MAGNIFIER, PHONE, PixelArt } from "../pixel-art";
import { ArrowChip, buttonClass } from "../ui/button";
import { WhatsAppButton } from "../whatsapp-button";
import { whatsappLink } from "@/lib/whatsapp";
import { CtaFloats } from "./floats";
import { RecordPrinter } from "./printer";
import { TerminalDemo } from "./terminal-demo";

const H2 = "font-display text-[34px] leading-[1.05] font-bold tracking-[-0.032em] text-balance sm:text-[44px]";
const CARD = "rounded-none border border-line bg-surface shadow-e1";

function Eyebrow({ children }: { children: ReactNode }) {
  return <p className="pixel-label text-accent">{children}</p>;
}

/* ---------- The reading: the terminal session ---------- */

export function Demo() {
  return (
    <section id="demo" aria-labelledby="demo-title" className="lit relative scroll-mt-16 border-t border-line bg-paper-deep">
      <div aria-hidden className="bg-pixel-grid pointer-events-none absolute inset-0 [mask-image:linear-gradient(to_bottom,black,transparent_85%)]" />
      <div className="relative mx-auto max-w-5xl px-4 py-20 sm:px-6 sm:py-24">
        <div className="mx-auto max-w-2xl text-center">
          <Eyebrow>Example</Eyebrow>
          <h2 id="demo-title" data-reveal className={`${H2} mt-4`}>
            The price that was agreed, not the first one quoted.
          </h2>
          <p className="mt-4 text-lg leading-relaxed text-muted">
            ChatReceipt reads the messages in order and follows each offer and counter-offer to the price both sides
            accepted.
          </p>
        </div>
        <div data-reveal className="mt-12">
          <div className="hover-idle">
            <TerminalDemo />
          </div>
          <div aria-hidden className="hover-shadow mx-auto -mt-2 h-5 w-[62%] rounded-[50%] bg-black blur-[16px]" />
        </div>
      </div>
    </section>
  );
}

/* ---------- The record: copy beside the printer ---------- */

const RECORD_POINTS = [
  "Items, fees, discounts and delivery, each with its amount",
  "What was paid, and what is still outstanding",
  "Payment and delivery terms, when the chat mentions them",
];

export function RecordSection() {
  return (
    <section aria-labelledby="record-title" className="lit border-t border-line">
      <div className="mx-auto grid max-w-5xl items-start gap-12 px-4 py-20 sm:px-6 sm:py-24 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)] lg:gap-16">
        <div className="lg:sticky lg:top-28 lg:pt-10">
          <Eyebrow>The record</Eyebrow>
          <h2 id="record-title" data-reveal className={`${H2} mt-4`}>
            A one-page record of the deal.
          </h2>
          <p className="mt-4 max-w-md text-lg leading-relaxed text-muted">
            It shows what was agreed, what has been paid and what is still owed. Totals are calculated from the
            figures you confirm.
          </p>
          <ul className="mt-7 space-y-3">
            {RECORD_POINTS.map((point) => (
              <li key={point} className="flex gap-3 text-ui leading-relaxed text-ink-soft">
                <span aria-hidden className="mt-[0.55em] block size-2 shrink-0 bg-accent" />
                {point}
              </li>
            ))}
          </ul>
          <div className="mt-9 flex flex-wrap items-center gap-x-5 gap-y-3">
            <Link href="/create" className={buttonClass("primary", "lg")}>
              Create a receipt
              <ArrowChip />
            </Link>
            <span className="font-mono text-meta text-muted">PDF or PNG · no account needed</span>
          </div>
        </div>
        <div data-reveal>
          <div className="hover-idle [animation-delay:-3s]">
            <RecordPrinter />
          </div>
          <div aria-hidden className="hover-shadow mx-auto mt-4 h-5 w-[58%] rounded-[50%] bg-black blur-[16px] [animation-delay:-3s]" />
        </div>
      </div>
    </section>
  );
}

/* ---------- How it works: three steps, each with a pixel object ---------- */

const STEPS: { title: string; text: string; art: ReactNode }[] = [
  {
    title: "Upload screenshots",
    text: "Add one or more screenshots from WhatsApp, Instagram, Telegram or SMS, in the order the conversation happened.",
    art: <PixelArt {...PHONE} cell={5} depth={2} />,
  },
  {
    title: "Review the details",
    text: "ChatReceipt finds the agreed items, prices, fees and payment status, shows where each came from and flags anything unclear. You can change anything.",
    art: <PixelArt {...MAGNIFIER} cell={5} depth={2} />,
  },
  {
    title: "Download the record",
    text: "Get a clean PDF or image with the breakdown, what has been paid and what is still outstanding.",
    art: <ChitStatic cell={6} mood="happy" />,
  },
];

export function HowItWorks() {
  return (
    <section id="how-it-works" aria-labelledby="how-title" className="lit scroll-mt-16 border-t border-line bg-paper-deep">
      <div className="mx-auto max-w-5xl px-4 py-20 sm:px-6 sm:py-24">
        <div className="mx-auto max-w-2xl text-center">
          <Eyebrow>How it works</Eyebrow>
          <h2 id="how-title" data-reveal className={`${H2} mt-4`}>
            Upload, check, download.
          </h2>
        </div>
        <ol className="mt-12 grid gap-4 md:grid-cols-3">
          {STEPS.map((step, index) => (
            <li key={step.title} data-reveal data-tilt data-reveal-delay={index * 100} className={`group ${CARD} flex flex-col p-6`}>
              <div className="relative flex h-36 items-center justify-center rounded-none border border-line bg-well shadow-well">
                <span aria-hidden className="absolute bottom-5 h-2 w-20 rounded-[50%] bg-black/70 blur-[5px] transition-transform duration-300 group-hover:scale-x-75" />
                <span className="transition-transform duration-300 ease-soft group-hover:-translate-y-1.5">
                  <span className="float-bob block" style={{ "--dur": `${5.5 + index}s`, "--delay": `${-index * 1.3}s` } as CSSProperties}>
                    {step.art}
                  </span>
                </span>
              </div>
              <p className="mt-6 flex items-center gap-3">
                <span className="pixel-label text-accent">0{index + 1}</span>
                <span className="h-px flex-1 bg-line" />
              </p>
              <h3 className="mt-4 font-display text-[20px] font-bold tracking-[-0.02em]">{step.title}</h3>
              <p className="mt-2 text-ui leading-relaxed text-muted">{step.text}</p>
            </li>
          ))}
        </ol>
      </div>
    </section>
  );
}

/* ---------- What it understands: three outcomes, then the rules ---------- */

const OUTCOMES = [
  {
    key: "found",
    title: "Read from the chat",
    text: "Shown next to the message it came from.",
    look: "border-accent/55 bg-accent-soft",
    keyLook: "text-accent",
  },
  {
    key: "check",
    title: "Flagged",
    text: "Unclear or conflicting details are marked for you to decide.",
    look: "border-line bg-surface",
    keyLook: "text-warn",
  },
  {
    key: "empty",
    title: "Left blank",
    text: "If the chat doesn't say it, the field stays empty.",
    look: "border-line bg-surface",
    keyLook: "text-muted",
  },
];

const RULES: [ReactNode, string][] = [
  [<>&ldquo;Can you do ₦22,000?&rdquo;</>, "offer · not the agreed price"],
  [<>&ldquo;₦23,000 is my final price.&rdquo; &ldquo;Okay.&rdquo;</>, "agreed price · ₦23,000"],
  [<>&ldquo;I&apos;ll pay tomorrow.&rdquo;</>, "promise to pay · still unpaid"],
  [<>&ldquo;Payment sent.&rdquo;</>, "payment claim · not verified"],
  ["A screenshot of a bank transfer", "evidence in your upload · not verified"],
  [<>&ldquo;Delivery is ₦2,000.&rdquo;</>, "delivery fee · added to the total"],
  ["Two different totals in one chat", "conflict · you choose"],
];

export function Understands() {
  return (
    <section aria-labelledby="rules-title" className="lit border-t border-line">
      <div className="mx-auto max-w-5xl px-4 py-20 sm:px-6 sm:py-24">
        <div className="mx-auto max-w-2xl text-center">
          <Eyebrow>How details are handled</Eyebrow>
          <h2 id="rules-title" data-reveal className={`${H2} mt-4`}>
            Read, flagged or left blank.
          </h2>
        </div>
        <div className="mt-12 grid gap-4 md:grid-cols-3">
          {OUTCOMES.map((outcome, index) => (
            <div key={outcome.key} data-reveal data-tilt data-reveal-delay={index * 90} className={`border p-6 shadow-e1 ${outcome.look}`}>
              <p className={`font-mono text-meta font-semibold ${outcome.keyLook}`}>{outcome.key}</p>
              <h3 className="mt-3 font-display text-[19px] font-bold tracking-[-0.02em]">{outcome.title}</h3>
              <p className="mt-2 text-ui leading-relaxed text-muted">{outcome.text}</p>
            </div>
          ))}
        </div>

        <ul data-reveal className={`mt-4 divide-y divide-line overflow-hidden ${CARD}`}>
          {RULES.map(([said, reading], index) => (
            <li key={index} className="flex flex-col gap-1 px-5 py-4 sm:flex-row sm:items-center sm:justify-between sm:gap-6 sm:px-6">
              <span className="font-medium text-ink">{said}</span>
              <span className="font-mono text-meta text-muted sm:text-right">{reading}</span>
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}

/* ---------- Who it's for: a line of chat and the line it becomes ---------- */

const USE_CASES = [
  {
    title: "Sellers on WhatsApp and Instagram",
    text: "A record of each order: the items, delivery and what is still owed.",
    chat: "Two pairs at ₦18,000 each, size 42.",
    record: [
      ["Sneakers, size 42 × 2", "₦36,000.00"],
      ["Payment", "not specified"],
    ],
  },
  {
    title: "Freelancers and service providers",
    text: "The agreed price and terms for a job, in one record you can both refer to.",
    chat: "₦80,000 for the logo, half upfront.",
    record: [
      ["Logo design × 1", "₦80,000.00"],
      ["Terms", "half upfront"],
    ],
  },
  {
    title: "Buyers",
    text: "Keep what a seller agreed to: the price, delivery and any conditions.",
    chat: "Delivery is free if you pay today.",
    record: [
      ["Delivery", "₦0.00"],
      ["Condition", "pay today"],
    ],
  },
];

export function UseCases() {
  return (
    <section aria-labelledby="uses-title" className="lit border-t border-line bg-paper-deep">
      <div className="mx-auto max-w-5xl px-4 py-20 sm:px-6 sm:py-24">
        <div className="mx-auto max-w-2xl text-center">
          <Eyebrow>Who it&apos;s for</Eyebrow>
          <h2 id="uses-title" data-reveal className={`${H2} mt-4`}>
            Sellers, freelancers and buyers.
          </h2>
        </div>
        <div className="mt-12 grid gap-4 md:grid-cols-3">
          {USE_CASES.map((useCase, index) => (
            <article key={useCase.title} data-reveal data-tilt data-reveal-delay={index * 90} className={`${CARD} flex flex-col p-6`}>
              <div className="rounded-none border border-line bg-noir-raised p-4 font-mono text-[12.5px] leading-relaxed">
                <p className="text-muted">
                  <span className="text-accent">›</span> chat
                </p>
                <p className="mt-1 text-ink">&ldquo;{useCase.chat}&rdquo;</p>
                <p className="mt-3 text-muted">
                  <span className="text-accent">›</span> record
                </p>
                {useCase.record.map(([label, value]) => (
                  <p key={label} className="mt-1 flex justify-between gap-3">
                    <span className="text-muted">{label}</span>
                    <span className={value.startsWith("₦") ? "text-accent tabular-nums" : "text-ink"}>{value}</span>
                  </p>
                ))}
              </div>
              <h3 className="mt-6 font-display text-[19px] font-bold tracking-[-0.02em]">{useCase.title}</h3>
              <p className="mt-2 text-ui leading-relaxed text-muted">{useCase.text}</p>
            </article>
          ))}
        </div>
      </div>
    </section>
  );
}

/* ---------- Privacy and trust ---------- */

const PRINCIPLES = [
  {
    label: "screenshots",
    title: "Not stored",
    text: "They're sent to our AI provider to be read. ChatReceipt doesn't store them.",
    strong: false,
  },
  {
    label: "you",
    title: "You check every detail",
    text: "Nothing goes on a record until you've reviewed it, and you can change anything.",
    strong: true,
  },
  {
    label: "payments",
    title: "Payments aren't verified",
    text: "A record shows what the chat says about payment. ChatReceipt doesn't check bank transfers.",
    strong: false,
  },
];

export function Privacy() {
  return (
    <section id="privacy" aria-labelledby="privacy-title" className="lit scroll-mt-16 border-t border-line">
      <div className="mx-auto max-w-5xl px-4 py-20 sm:px-6 sm:py-24">
        <div className="mx-auto max-w-2xl text-center">
          <Eyebrow>Privacy</Eyebrow>
          <h2 id="privacy-title" data-reveal className={`${H2} mt-4`}>
            What happens to your screenshots
          </h2>
        </div>
        <div className="mt-12 grid gap-4 md:grid-cols-3">
          {PRINCIPLES.map((principle, index) => (
            <div
              key={principle.label}
              data-reveal
              data-tilt
              data-reveal-delay={index * 90}
              className={`rounded-none border p-6 shadow-e1 ${principle.strong ? "border-accent/55 bg-accent-soft" : "border-line bg-surface"}`}
            >
              <p className={`font-mono text-meta font-semibold ${principle.strong ? "text-accent" : "text-ink"}`}>{principle.label}</p>
              <h3 className="mt-3 font-display text-[19px] font-bold tracking-[-0.02em]">{principle.title}</h3>
              <p className="mt-2 text-ui leading-relaxed text-muted">{principle.text}</p>
            </div>
          ))}
        </div>
        <p className="mt-6 text-center text-meta text-muted">No account needed. Records made on this website aren&apos;t stored, so download a copy.</p>
        {whatsappLink() && (
          <p className="mx-auto mt-2 max-w-2xl text-center text-meta leading-relaxed text-balance text-muted">
            On WhatsApp, records you confirm are saved so you can find them later. Send &ldquo;delete my data&rdquo; to
            remove them.
          </p>
        )}
      </div>
    </section>
  );
}

/* ---------- FAQ ---------- */

const FAQ: [string, string][] = [
  [
    "Is a ChatReceipt record a legal document?",
    "No. It records what the conversation shows was agreed. It isn't a contract.",
  ],
  [
    "Does ChatReceipt confirm that I've been paid?",
    "No. It records what the chat says about payment. \"Payment sent\" goes on the record as a claim, and a transfer screenshot as evidence you uploaded. ChatReceipt doesn't check bank transfers.",
  ],
  [
    "What happens to my screenshots?",
    "They're sent to our AI provider to be read. ChatReceipt doesn't store them.",
  ],
  ["Do I need an account?", "No. Records made on this website aren't stored, so download the PDF or image to keep a copy."],
  [
    "What if it reads something wrong?",
    "You review every detail before the record is made and can change anything. Unclear details are flagged.",
  ],
  [
    "Which currencies does it handle?",
    "17, including naira, US dollars, pounds, euros and cedis. Naira is the default, and you can change the currency while you review.",
  ],
];

const WHATSAPP_FAQ: [string, string] = [
  "Can I use ChatReceipt on WhatsApp?",
  "Yes. Send your screenshots to the ChatReceipt number, check the details and confirm. Confirmed records are saved, and sending \"delete my data\" removes them.",
];

export function Faq() {
  const questions = whatsappLink() ? [...FAQ, WHATSAPP_FAQ] : FAQ;
  return (
    <section aria-labelledby="faq-title" className="lit border-t border-line bg-paper-deep">
      <div className="mx-auto max-w-5xl px-4 py-20 sm:px-6 sm:py-24">
        <h2 id="faq-title" data-reveal className={`${H2} text-center`}>
          Questions
        </h2>
        <div className="mt-10 border-t border-line">
          {questions.map(([question, answer]) => (
            <details key={question} className="faq group border-b border-line">
              <summary className="flex min-h-14 cursor-pointer items-center gap-3 py-4 font-display text-[17px] font-bold tracking-[-0.015em] transition-colors hover:text-accent">
                <svg aria-hidden viewBox="0 0 4 6" shapeRendering="crispEdges" className="faq-caret size-2.5 shrink-0 text-accent transition-transform duration-150" fill="currentColor">
                  <path d="M0 0h1v1h1v1h1v1h1v0H3v1H2v1H1v1H0z" />
                </svg>
                {question}
              </summary>
              <p className="max-w-3xl pb-5 pl-[22px] text-ui leading-relaxed text-muted">{answer}</p>
            </details>
          ))}
        </div>
        <nav aria-label="More" className="mt-6 flex flex-wrap justify-center gap-x-6 text-ui">
          <Link href="/create" className="inline-flex min-h-10 items-center text-muted underline underline-offset-4 transition-colors hover:text-ink">
            Create a receipt
          </Link>
          <a href="#how-it-works" className="inline-flex min-h-10 items-center text-muted underline underline-offset-4 transition-colors hover:text-ink">
            How it works
          </a>
          <a href="#privacy" className="inline-flex min-h-10 items-center text-muted underline underline-offset-4 transition-colors hover:text-ink">
            Privacy
          </a>
        </nav>
      </div>
    </section>
  );
}

/* ---------- Closing call to action ---------- */

export function ClosingCta() {
  return (
    <section aria-labelledby="cta-title" className="lit relative overflow-x-clip border-t border-line">
      <div aria-hidden className="floor" />
      <CtaFloats />
      <div className="relative mx-auto flex max-w-5xl flex-col items-center px-4 py-20 text-center sm:px-6 sm:py-28">
        <Mascot cell={6} />
        <h2 id="cta-title" data-reveal className={`${H2} mt-8 max-w-2xl`}>
          Make your first record
        </h2>
        <p className="mt-4 max-w-md text-lg leading-relaxed text-muted">
          Upload the screenshots, check the details and download the record. No account needed.
        </p>
        <div className="mt-9 flex flex-col items-center gap-3 sm:flex-row">
          <Link href="/create" className={buttonClass("primary", "lg")}>
            Create a receipt
            <ArrowChip />
          </Link>
          <WhatsAppButton size="lg" />
        </div>
      </div>
    </section>
  );
}
