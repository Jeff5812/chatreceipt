// The business number comes from NEXT_PUBLIC_WHATSAPP_NUMBER (digits, with the country code). Until it is
// set, every WhatsApp entry point stays hidden, so the site never shows a link that goes nowhere.
const NUMBER = (process.env.NEXT_PUBLIC_WHATSAPP_NUMBER ?? "").replace(/\D/g, "");

const GREETING = "Hi ChatReceipt, I'd like to turn a screenshot into a record.";

export function whatsappLink(message: string = GREETING): string | null {
  return NUMBER ? `https://wa.me/${NUMBER}?text=${encodeURIComponent(message)}` : null;
}
