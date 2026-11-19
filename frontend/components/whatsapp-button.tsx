import { whatsappLink } from "@/lib/whatsapp";
import { buttonClass, type ButtonSize } from "./ui/button";
import { ChatIcon } from "./ui/icons";

/** Opens a chat with ChatReceipt on WhatsApp. Renders nothing until the business number is configured. */
export function WhatsAppButton({
  size = "md",
  className = "",
  message,
  label = "Send it on WhatsApp",
}: {
  size?: ButtonSize;
  className?: string;
  message?: string;
  label?: string;
}) {
  const href = whatsappLink(message);
  if (!href) return null;
  return (
    <a href={href} target="_blank" rel="noopener noreferrer" className={buttonClass("secondary", size, className)}>
      <ChatIcon className="size-4 text-accent" />
      {label}
    </a>
  );
}
