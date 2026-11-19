import { Hero } from "@/components/landing/hero";
import { ClosingCta, Demo, Faq, HowItWorks, Privacy, RecordSection, Understands, UseCases } from "@/components/landing/sections";
import { RevealOnScroll } from "@/components/reveal";
import { SiteFooter } from "@/components/site-footer";
import { SiteHeader } from "@/components/site-header";
import { TiltOnHover } from "@/components/tilt";

export default function Home() {
  return (
    <>
      <SiteHeader />
      <main>
        <Hero />
        <Demo />
        <RecordSection />
        <HowItWorks />
        <Understands />
        <UseCases />
        <Privacy />
        <Faq />
        <ClosingCta />
      </main>
      <SiteFooter />
      <RevealOnScroll />
      <TiltOnHover />
    </>
  );
}
