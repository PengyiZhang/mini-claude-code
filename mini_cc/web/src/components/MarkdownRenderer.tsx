import React from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { MermaidRenderer } from "./MermaidRenderer";
import type { Components } from "react-markdown";

const CodeBlock: Components["code"] = ({
  className,
  children,
  ...props
}) => {
  const code = String(children).replace(/\n$/, "");

  const lang = className?.replace("language-", "");
  const isMermaid = lang === "mermaid";

  if (isMermaid) {
    return <MermaidRenderer chart={code} />;
  }

  return (
    <pre className={className}>
      <code {...props}>{code}</code>
    </pre>
  );
};

export const MarkdownRenderer = ({ content }: { content: string }) => {
  return (
    <div className="markdown-body">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          code: CodeBlock,
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
};