import type { Config } from "tailwindcss";

export default {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: { DEFAULT: "#1c1f23", soft: "#4b5157", mute: "#7a8086" },
        line: "#e3e5e8",
        canvas: "#f6f7f8",
        brand: { DEFAULT: "#0f766e", soft: "#e6f2f1" },
      },
      fontFamily: { sans: ["ui-sans-serif", "system-ui", "-apple-system", "Segoe UI", "Roboto", "sans-serif"] },
    },
  },
  plugins: [],
} satisfies Config;
