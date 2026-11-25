import type { Config } from "tailwindcss";
import animate from "tailwindcss-animate";

const config: Config = {
  darkMode: ["class"],
  content: [
    "./pages/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./app/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        // CDC brand navy, built around the logo's lettering (public/cdc-logo.png,
        // #003A89 = the 700 step, used for primary buttons; white text ~10:1).
        // Lighter steps for tints/active states, 300–400 for dark-mode text.
        brand: {
          50: "#EEF4FC",
          100: "#D9E6F8",
          200: "#B5CDF0",
          300: "#85ACE3",
          400: "#4F82D0",
          500: "#2662BA",
          600: "#0F4CA3",
          700: "#003A89",
          800: "#002F70",
          900: "#002456",
          950: "#001536",
        },
        // New CDC logo palette, sampled from cdc-logo.png: gold fill #FFC420,
        // navy lettering #003A89. Gold is for fills/glows only (low contrast as
        // text on white); navy carries text, links and primary buttons (~10:1).
        cdc: {
          gold: "#FFC420",
          navy: "#003A89",
          "navy-dark": "#002B66",
        },
        border: "hsl(var(--border))",
        input: "hsl(var(--input))",
        ring: "hsl(var(--ring))",
        background: "hsl(var(--background))",
        foreground: "hsl(var(--foreground))",
        primary: {
          DEFAULT: "hsl(var(--primary))",
          foreground: "hsl(var(--primary-foreground))",
        },
        secondary: {
          DEFAULT: "hsl(var(--secondary))",
          foreground: "hsl(var(--secondary-foreground))",
        },
        muted: {
          DEFAULT: "hsl(var(--muted))",
          foreground: "hsl(var(--muted-foreground))",
        },
        accent: {
          DEFAULT: "hsl(var(--accent))",
          foreground: "hsl(var(--accent-foreground))",
        },
        destructive: {
          DEFAULT: "hsl(var(--destructive))",
          foreground: "hsl(var(--destructive-foreground))",
        },
      },
      borderRadius: {
        lg: "var(--radius)",
        md: "calc(var(--radius) - 2px)",
        sm: "calc(var(--radius) - 4px)",
      },
      keyframes: {
        "accordion-down": {
          from: { height: "0" },
          to: { height: "var(--radix-accordion-content-height)" },
        },
        "accordion-up": {
          from: { height: "var(--radix-accordion-content-height)" },
          to: { height: "0" },
        },
        "fade-in": {
          from: { opacity: "0", transform: "translateY(8px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
        // Indeterminate progress: a 40%-wide segment sweeping across its track.
        "progress-indeterminate": {
          from: { transform: "translateX(-100%)" },
          to: { transform: "translateX(250%)" },
        },
        "cdc-rise": {
          from: { opacity: "0", transform: "translateY(16px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
      },
      animation: {
        "accordion-down": "accordion-down 0.2s ease-out",
        "accordion-up": "accordion-up 0.2s ease-out",
        "fade-in": "fade-in 0.3s ease-out",
        "progress-indeterminate": "progress-indeterminate 1.6s ease-in-out infinite",
        "cdc-rise": "cdc-rise 0.6s cubic-bezier(0.22, 1, 0.36, 1) both",
      },
    },
  },
  plugins: [animate],
};

export default config;
