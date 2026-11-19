import type { Metadata } from "next";
import { RecordsView } from "@/components/records/records-view";

export const metadata: Metadata = {
  title: "My records",
  robots: { index: false, follow: false },
};

export default function RecordsPage() {
  return <RecordsView />;
}
