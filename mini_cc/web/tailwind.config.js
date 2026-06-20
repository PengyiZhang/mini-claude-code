/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: {
          DEFAULT: "#0b0d12",
          panel: "#11141b",
          card: "#161a23",
          hover: "#1c2230",
        },
        border: {
          DEFAULT: "#262c3a",
        },
        accent: {
          DEFAULT: "#7c5cff",
          hover: "#6b4cf0",
        },
        ink: {
          DEFAULT: "#e7ecf3",
          dim: "#9aa3b2",
          faint: "#5e6675",
        },
        ok: "#22c55e",
        warn: "#f59e0b",
        err: "#ef4444",
      },
      fontFamily: {
        mono: ["JetBrains Mono", "Cascadia Code", "Consolas", "ui-monospace", "monospace"],
      },
    },
  },
  plugins: [],
};
