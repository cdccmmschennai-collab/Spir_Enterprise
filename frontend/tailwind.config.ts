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
        // CDC INT brand purple, sampled from the logo (public/cdc_logo.jpg, solid
        // fill ≈ #6C2462, hue 308°). The 700 step (primary buttons) is lifted a
        // touch lighter than the logo for visibility; white text on it is ~9:1.
        // Overrides Tailwind's stock violet so every existing `violet-*` utility
        // renders the brand hue instead of the default AI-violet.
        violet: {
          50: "#F9F0F8",
          100: "#F4E1F1",
          200: "#E9C4E4",
          300: "#D897CF",
          400: "#C464B7",
          500: "#A83899",
          600: "#8E2F81",
          700: "#77286C",
          800: "#602057",
          900: "#491842",
          950: "#2E0F2A",
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
      },
      animation: {
        "accordion-down": "accordion-down 0.2s ease-out",
        "accordion-up": "accordion-up 0.2s ease-out",
        "fade-in": "fade-in 0.3s ease-out",
        "progress-indeterminate": "progress-indeterminate 1.6s ease-in-out infinite",
      },
    },
  },
  plugins: [animate],
};

export default config;
