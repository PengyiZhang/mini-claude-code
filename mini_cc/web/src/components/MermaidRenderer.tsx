import mermaid from "mermaid";
import { useEffect, useRef, useState } from "react";

mermaid.initialize({
  startOnLoad: false,
  theme: "default",
});

export const MermaidRenderer = ({ chart }: { chart: string }) => {
  const [html, setHtml] = useState("");

  useEffect(() => {
    let cancelled = false;

    const run = async () => {
      try {
        const id = `m-${Date.now()}`;
        const { svg } = await mermaid.render(id, chart);
        if (!cancelled) setHtml(svg);
      } catch {
        if (!cancelled) setHtml(`<pre>${chart}</pre>`);
      }
    };

    run();

    return () => {
      cancelled = true;
    };
  }, [chart]);

  return <div dangerouslySetInnerHTML={{ __html: html }} />;
};