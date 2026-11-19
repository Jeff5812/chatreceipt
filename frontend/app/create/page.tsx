import type { Metadata } from "next";
import { ReceiptWorkflow } from "@/components/workflow/receipt-workflow";

export const metadata: Metadata = {
  title: "Create a receipt",
};

export default function CreatePage() {
  return <ReceiptWorkflow />;
}
