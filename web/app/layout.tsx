import type { Metadata } from "next";
import { Playfair_Display, Source_Serif_4, DM_Mono } from "next/font/google";
import "./globals.css";
import LeftNav from "@/components/LeftNav";
import { RecentlyVisited } from "@/components/RecentlyVisited";

const playfair = Playfair_Display({
  variable: "--font-playfair",
  subsets: ["latin"],
  weight: ["500", "600", "700"],
});

const sourceSerif = Source_Serif_4({
  variable: "--font-source-serif",
  subsets: ["latin"],
  weight: ["400", "600"],
});

const dmMono = DM_Mono({
  variable: "--font-dm-mono",
  subsets: ["latin"],
  weight: ["400", "500"],
});

export const metadata: Metadata = {
  title: "DoctorsHero Knowledge Hub",
  description: "Clinical knowledge base — browse and search evidence-based topics.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className={`${playfair.variable} ${sourceSerif.variable} ${dmMono.variable}`}>
      <body>
        <div className="app-shell">
          <LeftNav />
          <div className="app-shell-middle">{children}</div>
          <RecentlyVisited />
        </div>
      </body>
    </html>
  );
}
